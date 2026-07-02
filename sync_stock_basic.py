"""
股票基础信息同步 — 基于 Tushare Pro stock_basic 接口

用途：
- 一次 API 调用拉取全市场上市股票（list_status=L）的基础信息
- 为现有 stock_basic 表补充中文名称、行业、市场、上市日期等字段
- 采用纯增量的结构变更（ALTER TABLE ADD COLUMN），不重建表、不动 code 列，
  因此不影响任何依赖 `SELECT code FROM stock_basic` 的现有程序

用法：
  python sync_stock_basic.py

字段说明（全量）：
  name        股票中文名称
  area        地域
  industry    所属行业
  market      市场类型（主板/创业板/科创板/北交所/CDR）
  list_date   上市日期（YYYYMMDD）
  list_status 上市状态（本脚本仅拉 L）
  delist_date 退市日期
"""

import sys
import time
from pathlib import Path

import tushare as ts

from common import db
from common.config import TUSHARE_TOKEN

# 记录上次成功同步时间的时间戳文件（与脚本同目录，内容为 unix 秒的浮点数）
LAST_SYNC_FILE = Path(__file__).resolve().parent / ".stock_basic_last_sync"
# Tushare stock_basic 接口限 1 次/小时，据此设最小同步间隔
MIN_SYNC_INTERVAL = 3600  # 秒


def _read_last_sync():
    """读取上次成功同步的 unix 时间戳；无记录或损坏则返回 None。"""
    try:
        return float(LAST_SYNC_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_last_sync(ts_now):
    """记录本次成功同步时间。"""
    LAST_SYNC_FILE.write_text(str(ts_now))

# 需要补充到 stock_basic 表的列（列名 -> DuckDB 类型）
# code 列已存在，不在此列表中，保证既有结构不被破坏
NEW_COLUMNS = {
    "name": "VARCHAR",
    "area": "VARCHAR",
    "industry": "VARCHAR",
    "market": "VARCHAR",
    "list_date": "VARCHAR",
    "list_status": "VARCHAR",
    "delist_date": "VARCHAR",
}


def _ts_code_to_local(ts_code):
    """000001.SZ -> sz.000001（与 update_daily_price_v3 保持一致）"""
    parts = ts_code.split(".")
    return f"{parts[1].lower()}.{parts[0]}"


def fetch_stock_basic(pro):
    """
    拉取全市场上市股票（list_status=L）基础信息。
    返回 DataFrame，code 列已转为本地格式（sz.000001）。
    """
    df = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,area,industry,market,list_date,list_status,delist_date",
    )
    if df is None or df.empty:
        raise RuntimeError("stock_basic 接口返回空数据，请检查 token 或积分权限")

    df["code"] = df["ts_code"].apply(_ts_code_to_local)
    return df


def ensure_columns(con):
    """幂等地为 stock_basic 表补充新列（列已存在则跳过）。"""
    existing = {row[0] for row in con.execute("DESCRIBE stock_basic").fetchall()}
    for col, col_type in NEW_COLUMNS.items():
        if col not in existing:
            con.execute(f"ALTER TABLE stock_basic ADD COLUMN {col} {col_type}")
            print(f"  新增列: {col} {col_type}")


def upsert(con, df):
    """
    将基础信息回填到 stock_basic。
    - 已存在的 code：UPDATE 补充名称等字段
    - 库中没有、Tushare 新返回的 code：INSERT 进来
    通过临时表做 UPDATE ... FROM，避免逐行操作。
    """
    cols = ["code"] + list(NEW_COLUMNS.keys())
    con.register("tmp_basic", df[cols])

    # 1) 更新已存在的代码
    set_clause = ", ".join(f"{c} = t.{c}" for c in NEW_COLUMNS)
    con.execute(f"""
        UPDATE stock_basic AS s
        SET {set_clause}
        FROM tmp_basic AS t
        WHERE s.code = t.code
    """)

    # 2) 插入库中缺失的新代码
    col_list = ", ".join(cols)
    con.execute(f"""
        INSERT INTO stock_basic ({col_list})
        SELECT {col_list} FROM tmp_basic
        WHERE code NOT IN (SELECT code FROM stock_basic)
    """)
    con.unregister("tmp_basic")


def run():
    t_start = time.time()
    print("股票基础信息同步（Tushare Pro stock_basic）")

    # --if-stale：距上次成功同步不足 1 小时则跳过（避免撞 Tushare 1次/小时限流）。
    # 跳过时以成功退出码返回，供 daily_update.py 继续后续步骤。
    if "--if-stale" in sys.argv:
        last = _read_last_sync()
        if last is not None:
            elapsed = t_start - last
            if elapsed < MIN_SYNC_INTERVAL:
                mins = int((MIN_SYNC_INTERVAL - elapsed) / 60)
                print(f"  距上次成功同步仅 {int(elapsed / 60)} 分钟，未满 1 小时，"
                      f"跳过本次同步（约 {mins} 分钟后可再同步）")
                return

    con = db.connect()
    pro = ts.pro_api(TUSHARE_TOKEN)

    print("拉取全市场上市股票基础信息 ...")
    try:
        df = fetch_stock_basic(pro)
    except Exception as e:
        # Tushare 限流（1次/小时）不视为错误：打印提示并跳过，不影响后续行情更新
        if "频率超限" in str(e) or "每分钟" in str(e) or "每小时" in str(e):
            print(f"  Tushare 接口限流，本次跳过同步：{e}")
            con.close()
            return
        raise
    print(f"  获取 {len(df)} 只上市股票")

    # 变更前记录代码总数，用于事后核对 code 列未被破坏
    before = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]

    ensure_columns(con)
    upsert(con, df)

    after = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    named = con.execute(
        "SELECT COUNT(*) FROM stock_basic WHERE name IS NOT NULL"
    ).fetchone()[0]

    print(f"  同步前代码数: {before}，同步后: {after}（新增 {after - before}）")
    print(f"  已带中文名称的代码数: {named}")

    # 新 INSERT 的代码 type 为空，立即分类，保证 get_stock_codes() 不漏新股
    import classify_stock_basic
    classify_stock_basic.ensure_type_column(con)
    classify_stock_basic.classify(con)
    print("  已刷新 type 分类（个股/指数）")

    # 抽样展示
    print("  样例:")
    for r in con.execute(
        "SELECT code, name, industry, market, list_date "
        "FROM stock_basic WHERE name IS NOT NULL LIMIT 5"
    ).fetchall():
        print("   ", r)

    con.close()

    # 记录本次成功同步时间，供 --if-stale 判断间隔
    _write_last_sync(t_start)
    print(f"完成，耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    run()
