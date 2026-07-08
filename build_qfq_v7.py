"""
前复权计算 V7 — 基于 Tushare Pro

相比 V6 的优势：
- Tushare adj_factor 接口支持按交易日批量获取全市场因子（1次请求5000+只）
- 无需逐只查询，日常增量只需1次API调用，几秒完成
- 因子为每日值，无需窗口函数填充

用法：
  python build_qfq_v7.py              # 增量获取因子 + 重建 qfq
  python build_qfq_v7.py --qfq-only   # 跳过下载，直接重建 qfq
  python build_qfq_v7.py --backfill   # 从已有 adjust_factor_daily 表迁移历史数据
"""

import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import tushare as ts

from common import db
from common.config import TUSHARE_TOKEN

# Tushare 频率限制（当前 token 为 1次/小时，保守设置）
API_INTERVAL = 65


def _ts_code_to_local(ts_code):
    """000001.SZ -> sz.000001"""
    parts = ts_code.split('.')
    return f"{parts[1].lower()}.{parts[0]}"


def _init_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS adjust_factor_tushare (
            code VARCHAR,
            date DATE,
            factor DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)


def fetch_tushare_factor(pro, trade_date):
    """
    获取指定交易日全市场复权因子。
    trade_date: 格式 YYYYMMDD
    返回 DataFrame [code, date, factor] 或 None
    """
    try:
        df = pro.adj_factor(trade_date=trade_date)
    except Exception as e:
        print(f"  API调用失败 ({trade_date}): {e}")
        return None

    if df is None or df.empty:
        return None

    df["code"] = df["ts_code"].apply(_ts_code_to_local)
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.rename(columns={"adj_factor": "factor"})
    return df[["code", "date", "factor"]]


def get_trade_dates(con, start_date, end_date):
    """从 daily_price 表获取交易日列表"""
    rows = con.execute("""
        SELECT DISTINCT date FROM daily_price
        WHERE date >= ? AND date <= ?
        ORDER BY date
    """, [start_date, end_date]).fetchall()
    return [row[0] for row in rows]


def _sanitize_factors(con, df, trade_date):
    """
    清洗新下载的因子：检测因子骤降或虚假骤升的异常值。

    规则：
    1. 因子下降>1%（排除指数）→ 用前值替代
       复权因子理论上只会不变或增大，下降必定是API错误
    2. 因子骤升>5%但对应价格未出现除权缺口（>-5%）→ 用前值替代
       真除权时开盘价会大幅低开（送股/分红导致），无缺口说明是API虚报
    """
    if df is None or df.empty:
        return df

    # 获取这批股票前一天的因子
    codes = df["code"].tolist()
    prev_date = con.execute("""
        SELECT MAX(date) FROM adjust_factor_tushare
        WHERE date < ?
    """, [trade_date]).fetchone()[0]

    if prev_date is None:
        return df

    prev_df = con.execute("""
        SELECT code, factor as prev_factor
        FROM adjust_factor_tushare
        WHERE date = ?
    """, [prev_date]).fetchdf()

    merged = df.merge(prev_df, on="code", how="left")

    # 规则1：因子下降>1%（排除指数），一律用前值替代
    drop_mask = (
        merged["prev_factor"].notna() &
        (merged["factor"] < merged["prev_factor"] * 0.99) &
        (~merged["code"].str.startswith("sz.399")) &
        (~merged["code"].str.startswith("sh.000"))
    )

    n_drop = drop_mask.sum()
    if n_drop > 0:
        anomaly_codes = merged.loc[drop_mask, "code"].tolist()
        print(f"\n  ⚠️ 检测到 {n_drop} 只因子下降（>1%），用前值替代:")
        for c in anomaly_codes[:5]:
            row = merged[merged["code"] == c].iloc[0]
            print(f"    {c}: {row['prev_factor']:.4f} → {row['factor']:.4f} (丢弃)")
        if n_drop > 5:
            print(f"    ... 等共 {n_drop} 只")
        merged.loc[drop_mask, "factor"] = merged.loc[drop_mask, "prev_factor"]

    # 规则2：因子骤升>5%，需用价格缺口验证是否为真除权
    surge_mask = (
        merged["prev_factor"].notna() &
        (merged["factor"] > merged["prev_factor"] * 1.05)
    )

    n_surge = surge_mask.sum()
    if n_surge > 0:
        surge_codes = merged.loc[surge_mask, "code"].tolist()

        # 获取前一日收盘价和当日开盘价，计算价格缺口
        price_df = con.execute("""
            SELECT
                p1.code,
                (p1.open - p2.close) / p2.close as gap_pct
            FROM daily_price p1
            JOIN daily_price p2 ON p1.code = p2.code AND p2.date = ?
            WHERE p1.date = ?
        """, [prev_date, trade_date]).fetchdf()

        merged = merged.merge(price_df, on="code", how="left")

        # 因子骤升但价格未大幅低开（gap > -5%）→ 虚假除权信号
        false_surge_mask = (
            surge_mask &
            (merged["gap_pct"].isna() | (merged["gap_pct"] > -0.05))
        )

        n_false = false_surge_mask.sum()
        if n_false > 0:
            false_codes = merged.loc[false_surge_mask, "code"].tolist()
            print(f"\n  ⚠️ 检测到 {n_false} 只因子虚假骤升（>5%但无除权缺口），用前值替代:")
            for c in false_codes[:5]:
                row = merged[merged["code"] == c].iloc[0]
                gap = row.get("gap_pct", float("nan"))
                print(f"    {c}: {row['prev_factor']:.4f} → {row['factor']:.4f} (缺口{gap:+.2%}, 丢弃)")
            if n_false > 5:
                print(f"    ... 等共 {n_false} 只")
            merged.loc[false_surge_mask, "factor"] = merged.loc[false_surge_mask, "prev_factor"]

        n_real = n_surge - n_false
        if n_real > 0:
            print(f"\n  ✅ {n_real} 只因子骤升为真除权（有对应价格缺口），保留")

        # 清理临时列
        if "gap_pct" in merged.columns:
            merged = merged.drop(columns=["gap_pct"])

    return merged[["code", "date", "factor"]]


def update_factors(con):
    """增量更新因子：从本地最新日期到今天"""
    _init_table(con)

    # 查本地最新日期
    result = con.execute(
        "SELECT MAX(date) FROM adjust_factor_tushare"
    ).fetchone()[0]

    if result is None:
        print("adjust_factor_tushare 表为空，请先运行 --backfill 迁移历史数据")
        return False

    last_date = result
    today = datetime.now().date()

    if last_date >= today:
        print(f"因子数据已是最新（{last_date}），无需更新")
        return True

    # 获取需要补全的交易日
    start = last_date + timedelta(days=1)
    trade_dates = get_trade_dates(con, start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

    if not trade_dates:
        print(f"从 {start} 到 {today} 无交易日，无需更新")
        return True

    print(f"需补全 {len(trade_dates)} 个交易日的因子（{trade_dates[0]} ~ {trade_dates[-1]}）")

    if len(trade_dates) > 1:
        print(f"  注意: 当前 token 频率限制为 1次/{API_INTERVAL}秒，预计耗时 {len(trade_dates) * API_INTERVAL // 60} 分钟")

    pro = ts.pro_api(TUSHARE_TOKEN)

    for i, td in enumerate(trade_dates):
        date_str = td.strftime("%Y%m%d")
        print(f"  获取 {date_str} ({i+1}/{len(trade_dates)})...", end=" ")

        df = fetch_tushare_factor(pro, date_str)
        if df is not None:
            # 清洗异常因子（factor骤降>50%的用前值替代）
            df = _sanitize_factors(con, df, td)
            con.execute("INSERT OR REPLACE INTO adjust_factor_tushare SELECT * FROM df")
            print(f"{len(df)} 条")
        else:
            print("无数据")

        # 频率限制（最后一个不需要等）
        if i < len(trade_dates) - 1:
            time.sleep(API_INTERVAL)

    print("因子增量更新完成")
    return True


def backfill_from_baostock(con):
    """从已有的 adjust_factor_daily 表迁移数据到 adjust_factor_tushare"""
    _init_table(con)

    # 检查源表是否存在
    tables = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'adjust_factor_daily'"
    ).fetchall()
    if not tables:
        print("adjust_factor_daily 表不存在，请先运行 build_qfq_v6.py 生成")
        return False

    print("从 adjust_factor_daily 迁移数据...")
    con.execute("""
        INSERT OR REPLACE INTO adjust_factor_tushare
        SELECT code, date, factor FROM adjust_factor_daily
    """)

    count = con.execute("SELECT COUNT(*) FROM adjust_factor_tushare").fetchone()[0]
    print(f"迁移完成，共 {count} 条记录")
    return True


def build_qfq(con):
    """生成前复权数据"""
    print("生成前复权数据...")

    con.execute("DROP TABLE IF EXISTS daily_price_qfq")

    con.execute("""
        CREATE TABLE daily_price_qfq AS
        WITH latest AS (
            SELECT code, MAX(date) AS max_date
            FROM adjust_factor_tushare
            GROUP BY code
        ),
        latest_factor AS (
            SELECT f.code, f.factor AS latest_factor
            FROM adjust_factor_tushare f
            JOIN latest l
              ON f.code = l.code AND f.date = l.max_date
        )
        SELECT
            p.code,
            p.date,
            p.open  * f.factor / lf.latest_factor AS open,
            p.high  * f.factor / lf.latest_factor AS high,
            p.low   * f.factor / lf.latest_factor AS low,
            p.close * f.factor / lf.latest_factor AS close,
            p.volume,
            p.amount
        FROM daily_price p
        JOIN adjust_factor_tushare f
          ON p.code = f.code AND p.date = f.date
        JOIN latest_factor lf
          ON p.code = lf.code
    """)

    print("前复权数据生成完成")


def validate(con):
    """验证数据"""
    print("验证数据...")

    raw = con.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    qfq = con.execute("SELECT COUNT(*) FROM daily_price_qfq").fetchone()[0]
    print(f"  raw: {raw}, qfq: {qfq}")

    if raw != qfq:
        diff = raw - qfq
        print(f"  警告: 行数差异 {diff} 条（部分股票可能缺少因子数据）")
    else:
        print("  行数一致")

    abnormal = con.execute("""
        SELECT COUNT(*) FROM daily_price_qfq
        WHERE close > 10000 OR close < 0.01
    """).fetchone()[0]
    print(f"  异常价格: {abnormal}")
    print("验证完成")


def run():
    qfq_only = "--qfq-only" in sys.argv
    backfill = "--backfill" in sys.argv

    t_start = time.time()
    con = db.connect()
    print("QFQ系统 V7（Tushare Pro 版）")

    if backfill:
        if not backfill_from_baostock(con):
            con.close()
            return
    elif not qfq_only:
        if not update_factors(con):
            con.close()
            return

    build_qfq(con)
    validate(con)

    con.close()
    print(f"全部完成，总耗时 {time.time() - t_start:.1f} 秒")


if __name__ == "__main__":
    run()
