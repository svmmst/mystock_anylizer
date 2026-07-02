"""
每日全市场选股脚本

全市场 L2 技术快筛 + 信号强度排序，输出 TOP N 候选列表。
与 backtest_dynamic.py 使用完全相同的选股逻辑，确保实盘与回测一致。

用法：
  python3 daily_pick.py                         # 用数据库最新日期
  python3 daily_pick.py --date 2026-06-26       # 指定日期
  python3 daily_pick.py --top 20                # 输出前20只
  python3 daily_pick.py --json                  # JSON格式输出
  python3 daily_pick.py --verbose               # 详细模式（含评分细项）
"""

import argparse
import json
import sys

import duckdb
import numpy as np
import pandas as pd

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

MIN_AMOUNT = 50000000  # 日均成交额门槛：5000万


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算MA/MACD/KDJ等技术指标"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values.astype(float)
    n = len(close)

    ma5 = pd.Series(close).rolling(5).mean().values
    ma10 = pd.Series(close).rolling(10).mean().values
    ma20 = pd.Series(close).rolling(20).mean().values

    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().values

    low9 = pd.Series(low).rolling(9).min().values
    high9 = pd.Series(high).rolling(9).max().values
    rsv = np.where(high9 - low9 > 0, (close - low9) / (high9 - low9) * 100, 50)
    k = np.zeros(n)
    d = np.zeros(n)
    k[0] = 50
    d[0] = 50
    for i in range(1, n):
        k[i] = 2/3 * k[i-1] + 1/3 * rsv[i]
        d[i] = 2/3 * d[i-1] + 1/3 * k[i]

    vol_ma5 = pd.Series(volume).rolling(5).mean().values
    amount_ma20 = pd.Series(df["amount"].values.astype(float)).rolling(20).mean().values

    df = df.copy()
    df["ma5"] = ma5
    df["ma10"] = ma10
    df["ma20"] = ma20
    df["dif"] = dif
    df["dea"] = dea
    df["k"] = k
    df["d"] = d
    df["vol_ma5"] = vol_ma5
    df["amount_ma20"] = amount_ma20

    return df


def check_buy_signal(df: pd.DataFrame, idx: int) -> bool:
    """四项硬性门槛检查（与回测完全一致）"""
    if idx < 26:
        return False

    row = df.iloc[idx]
    close = row["close"]
    dif = row["dif"]
    dea = row["dea"]
    ma5 = row["ma5"]
    ma10 = row["ma10"]
    k_val = row["k"]

    if pd.isna(ma10) or pd.isna(dif) or pd.isna(k_val):
        return False

    # 1. MACD：DIF>0，或零轴下金叉+底背离
    if dif <= 0:
        if idx > 0:
            prev_dif = df.iloc[idx-1]["dif"]
            prev_dea = df.iloc[idx-1]["dea"]
            golden_cross = (dif > dea) and (prev_dif <= prev_dea)
            if not golden_cross:
                return False
            window = df.iloc[max(0, idx-20):idx]
            lower_prices = window[window["close"] < close]
            if lower_prices.empty or not (lower_prices["dif"] < dif).any():
                return False
        else:
            return False

    # 2. 均线：close>MA10 且 MA5>MA10
    if close <= ma10:
        return False
    if ma5 <= ma10:
        return False

    # 3. 趋势：非5日连续下跌，20日跌幅<15%
    if idx >= 4:
        recent = df.iloc[idx-4:idx+1]["close"].values
        if all(recent[i] > recent[i+1] for i in range(4)):
            return False

    if idx >= 20:
        close_20ago = df.iloc[idx-20]["close"]
        if close_20ago > 0 and (close / close_20ago - 1) < -0.15:
            return False

    # 4. KDJ：K<80
    if k_val >= 80:
        return False

    return True


def score_signal(df: pd.DataFrame, idx: int) -> dict:
    """打分并返回各分项详情"""
    row = df.iloc[idx]

    # MACD动量分（30%）
    macd_bar = row["dif"] - row["dea"]
    prev_bar = df.iloc[idx-1]["dif"] - df.iloc[idx-1]["dea"] if idx > 0 else 0
    macd_momentum = macd_bar - prev_bar
    macd_score = min(100, max(0, 50 + macd_momentum * 100))

    # 量比分（25%）
    vol_ratio = row["volume"] / row["vol_ma5"] if row["vol_ma5"] > 0 else 1.0
    if 1.2 <= vol_ratio <= 3.0:
        vol_score = 60 + (vol_ratio - 1.2) * 20
    elif vol_ratio > 3.0:
        vol_score = 50
    else:
        vol_score = vol_ratio / 1.2 * 60

    # KDJ位置分（20%）
    k_val = row["k"]
    if k_val <= 30:
        kdj_score = 90
    elif k_val <= 50:
        kdj_score = 70 + (50 - k_val) * 1
    elif k_val <= 70:
        kdj_score = 50 + (70 - k_val) * 1
    else:
        kdj_score = 30

    # 趋势强度分（25%）
    close_20ago = df.iloc[idx-20]["close"] if idx >= 20 else row["close"]
    change_20d = (row["close"] / close_20ago - 1) * 100 if close_20ago > 0 else 0
    if 0 <= change_20d <= 10:
        trend_score = 80 + change_20d
    elif -5 <= change_20d < 0:
        trend_score = 60 + change_20d * 4
    elif change_20d > 10:
        trend_score = max(20, 90 - (change_20d - 10) * 3)
    else:
        trend_score = 30

    total = macd_score * 0.30 + vol_score * 0.25 + kdj_score * 0.20 + trend_score * 0.25

    return {
        "total": total,
        "macd_score": macd_score,
        "vol_score": vol_score,
        "kdj_score": kdj_score,
        "trend_score": trend_score,
        "vol_ratio": vol_ratio,
        "k_val": k_val,
        "change_20d": change_20d,
        "dif": row["dif"],
        "dea": row["dea"],
        "ma5": row["ma5"],
        "ma10": row["ma10"],
        "close": row["close"],
    }


def extra_filters(df: pd.DataFrame, idx: int) -> bool:
    """额外过滤：防追高、排除涨停"""
    row = df.iloc[idx]

    # 涨停排除
    if idx > 0:
        prev_close = df.iloc[idx-1]["close"]
        if prev_close > 0:
            change_pct = (row["close"] / prev_close - 1) * 100
            if change_pct > 9.5:
                return False

    # 偏离MA5>5%排除
    if row["ma5"] > 0:
        ma5_dev = (row["close"] / row["ma5"] - 1) * 100
        if ma5_dev > 5:
            return False

    return True


def get_stock_name(con, code: str) -> str:
    """从数据库获取股票名称（如无名称表则返回代码后缀）"""
    # 数据库无名称字段，返回简化代码
    parts = code.split(".")
    return parts[1] if len(parts) == 2 else code


def run_daily_pick(target_date: str = None, top_n: int = 10,
                   verbose: bool = False, output_json: bool = False):
    con = duckdb.connect(DB_PATH, read_only=True)

    # 确定目标日期
    if target_date is None:
        result = con.execute("""
            SELECT MAX(date) FROM index_daily_price
            WHERE code = 'sz.399001'
        """).fetchone()
        target_date = str(result[0])[:10]

    print(f"全市场技术快筛 | 日期：{target_date} | 输出TOP{top_n}")
    print("=" * 70)

    # 获取流动性合格的股票
    qualified = con.execute("""
        SELECT code, AVG(amount) as avg_amount
        FROM daily_price_qfq
        WHERE date >= (
            SELECT MAX(date) - INTERVAL '30' DAY FROM index_daily_price WHERE code = 'sz.399001'
        )
          AND date <= ?
          AND (code LIKE 'sh.6%' OR code LIKE 'sz.0%'
               OR code LIKE 'sz.30%' OR code LIKE 'sz.31%')
          AND code NOT LIKE 'sh.688%'
        GROUP BY code
        HAVING AVG(amount) > ?
    """, [target_date, MIN_AMOUNT]).fetchdf()

    codes = qualified["code"].tolist()
    print(f"流动性合格股票：{len(codes)} 只")

    # 逐只加载数据+筛选
    signals = []
    scanned = 0

    for code in codes:
        df = con.execute("""
            SELECT date, open, high, low, close, volume, amount
            FROM daily_price_qfq
            WHERE code = ?
              AND date >= (? ::DATE - INTERVAL '120' DAY)
              AND date <= ?
            ORDER BY date
        """, [code, target_date, target_date]).fetchdf()

        if len(df) < 60:
            continue

        df["date"] = pd.to_datetime(df["date"])
        df = calc_indicators(df)

        # 定位目标日期
        target_ts = pd.to_datetime(target_date)
        mask = df["date"] == target_ts
        if not mask.any():
            continue

        idx = df.index[mask][0]

        # 流动性滚动检查
        row = df.iloc[idx] if isinstance(idx, int) else df.loc[idx]
        idx_pos = df.index.get_loc(idx)

        if pd.isna(df.iloc[idx_pos]["amount_ma20"]) or df.iloc[idx_pos]["amount_ma20"] < MIN_AMOUNT:
            continue

        # 四项硬性门槛
        if not check_buy_signal(df, idx_pos):
            scanned += 1
            continue

        # 额外过滤
        if not extra_filters(df, idx_pos):
            scanned += 1
            continue

        # 打分
        score_detail = score_signal(df, idx_pos)
        name = get_stock_name(con, code)

        signals.append({
            "code": code,
            "name": name,
            "score": score_detail["total"],
            "detail": score_detail,
        })
        scanned += 1

    con.close()

    # 排序
    signals.sort(key=lambda x: x["score"], reverse=True)
    top_signals = signals[:top_n]

    print(f"通过硬性门槛：{len(signals)} 只 | 扫描总数：{len(codes)} 只")
    print(f"通过率：{len(signals)/len(codes)*100:.1f}%")
    print()

    if output_json:
        output = []
        for s in top_signals:
            item = {
                "code": s["code"],
                "name": s["name"],
                "score": round(s["score"], 1),
                "close": round(s["detail"]["close"], 2),
                "k_val": round(s["detail"]["k_val"], 1),
                "vol_ratio": round(s["detail"]["vol_ratio"], 2),
                "change_20d": round(s["detail"]["change_20d"], 1),
                "dif": round(s["detail"]["dif"], 3),
            }
            output.append(item)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"{'排名':<4} {'代码':<12} {'评分':<6} {'收盘':<8} "
              f"{'K值':<6} {'量比':<6} {'20日涨幅':<10} {'DIF':<8}")
        print("-" * 70)

        for i, s in enumerate(top_signals, 1):
            d = s["detail"]
            print(f"{i:<4} {s['code']:<12} "
                  f"{s['score']:<6.1f} {d['close']:<8.2f} "
                  f"{d['k_val']:<6.1f} {d['vol_ratio']:<6.2f} "
                  f"{d['change_20d']:+.1f}%{'':>4} {d['dif']:<8.3f}")

        if verbose and top_signals:
            print(f"\n{'='*70}")
            print("详细评分：")
            for i, s in enumerate(top_signals, 1):
                d = s["detail"]
                print(f"\n  [{i}] {s['code']} — 综合评分 {s['score']:.1f}")
                print(f"      MACD动量: {d['macd_score']:.0f}分(×30%) | "
                      f"量比: {d['vol_score']:.0f}分(×25%) | "
                      f"KDJ位置: {d['kdj_score']:.0f}分(×20%) | "
                      f"趋势: {d['trend_score']:.0f}分(×25%)")
                print(f"      DIF={d['dif']:.3f} DEA={d['dea']:.3f} | "
                      f"MA5={d['ma5']:.2f} MA10={d['ma10']:.2f} | "
                      f"K={d['k_val']:.1f} | 量比={d['vol_ratio']:.2f}")

    # 写入候选文件供后续L3验证使用
    if top_signals:
        with open("/tmp/funnel_l1.txt", "w") as f:
            for s in top_signals:
                f.write(s["code"] + "\n")
        print(f"\n候选代码已写入 /tmp/funnel_l1.txt（{len(top_signals)}只，供L3资金验证使用）")

    return signals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每日全市场选股")
    parser.add_argument("--date", default=None, help="目标日期，默认用数据库最新日期")
    parser.add_argument("--top", type=int, default=10, help="输出前N只候选（默认10）")
    parser.add_argument("--json", action="store_true", help="JSON格式输出")
    parser.add_argument("--verbose", action="store_true", help="详细评分输出")
    args = parser.parse_args()

    run_daily_pick(args.date, args.top, args.verbose, args.json)
