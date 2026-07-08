"""
交易复盘验证脚本

对 real_stock/trades.jsonl 中每笔买入，用 daily_price_qfq 历史数据还原买入当天的
技术指标状态，对照 CLAUDE.md 的技术面硬性门槛逐条检查，并追踪持有期价格走势。

用法：
  python backtest_review.py
"""

import json
import os
import duckdb
import pandas as pd

TRADES_FILE = "/Users/sunxibao/projects/real_stock/trades.jsonl"
DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

# 需要多少天的历史数据来计算指标（EMA26需要足够历史）
LOOKBACK_DAYS = 120


def code_to_db(code: str) -> str:
    """000729 -> sz.000729, 600276 -> sh.600276, 920001 -> bj.920001"""
    if code.startswith("6"):
        return f"sh.{code}"
    elif code.startswith("9") or code.startswith("4") or code.startswith("8"):
        return f"bj.{code}"
    else:
        return f"sz.{code}"


def load_trades() -> list[dict]:
    trades = []
    with open(TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def load_kline(con, db_code: str, before_date: str) -> pd.DataFrame:
    """加载某只股票在指定日期前的历史K线（含当日）"""
    df = con.execute("""
        SELECT date, open, high, low, close, volume
        FROM daily_price_qfq
        WHERE code = ?
          AND date <= ?
        ORDER BY date
    """, [db_code, before_date]).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    return df


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 均线
    df["ma5"]  = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    # 成交量均量
    df["vol_ma5"] = volume.rolling(5).mean()

    # MACD (12/26/9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"]  = ema12 - ema26
    df["dea"]  = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2

    # KDJ (9日)
    low9  = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, float("nan")) * 100
    df["k"] = rsv.ewm(com=2, adjust=False).mean()
    df["d"] = df["k"].ewm(com=2, adjust=False).mean()
    df["j"] = 3 * df["k"] - 2 * df["d"]

    # RSI (14日)
    diff = close.diff()
    gain = diff.clip(lower=0).rolling(14).mean()
    loss = (-diff.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

    # 布林带 (20日, 2σ)
    df["boll_mid"]   = close.rolling(20).mean()
    boll_std         = close.rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * boll_std
    df["boll_lower"] = df["boll_mid"] - 2 * boll_std

    return df


def check_macd_divergence(df: pd.DataFrame, idx: int) -> bool:
    """简化底背离：前20日内价格更低但DIF更高"""
    window = df.iloc[max(0, idx - 20):idx]
    if len(window) < 5:
        return False
    cur_close = df.iloc[idx]["close"]
    cur_dif   = df.iloc[idx]["dif"]
    lower_price_rows = window[window["close"] < cur_close]
    if lower_price_rows.empty:
        return False
    return (lower_price_rows["dif"] < cur_dif).any()


def check_thresholds(df: pd.DataFrame, idx: int) -> dict:
    """检查 CLAUDE.md 技术面买入硬性门槛，返回每项结果"""
    row = df.iloc[idx]
    results = {}

    # 1. MACD 门槛
    dif_above_zero = row["dif"] > 0
    golden_cross = (row["dif"] > row["dea"]) and (
        idx > 0 and df.iloc[idx - 1]["dif"] <= df.iloc[idx - 1]["dea"]
    )
    divergence = check_macd_divergence(df, idx)
    zero_cross_golden_divergence = (row["dif"] < 0) and golden_cross and divergence

    if dif_above_zero:
        results["macd"] = (True, "DIF在零轴上方")
    elif zero_cross_golden_divergence:
        results["macd"] = (True, "零轴下方金叉+底背离")
    else:
        pos = "零轴上方" if row["dif"] > 0 else "零轴下方"
        cross = "金叉" if row["dif"] > row["dea"] else "死叉"
        results["macd"] = (False, f"DIF={row['dif']:.3f}({pos})，{cross}，无底背离")

    # 2. 均线门槛
    above_ma10    = row["close"] > row["ma10"]
    ma5_above_ma10 = row["ma5"] > row["ma10"]
    bull_arrange  = (row["ma5"] > row["ma10"]) and (row["ma10"] > row["ma20"])

    if above_ma10 and (ma5_above_ma10 or bull_arrange):
        arrange = "多头排列" if bull_arrange else "MA5上穿MA10"
        results["ma"] = (True, f"股价>{row['ma10']:.2f}(MA10)，{arrange}")
    else:
        issues = []
        if not above_ma10:
            issues.append(f"股价{row['close']:.2f}<MA10={row['ma10']:.2f}")
        if not ma5_above_ma10:
            issues.append(f"MA5={row['ma5']:.2f}<MA10={row['ma10']:.2f}")
        results["ma"] = (False, "，".join(issues))

    # 3. 趋势门槛
    recent5 = df.iloc[max(0, idx - 4):idx + 1]["close"]
    all_down_5 = all(recent5.diff().dropna() < 0)
    close_20ago = df.iloc[idx - 20]["close"] if idx >= 20 else None
    drop_20d = (row["close"] / close_20ago - 1) * 100 if close_20ago else 0

    trend_ok = (not all_down_5) and (drop_20d > -15)
    if trend_ok:
        results["trend"] = (True, f"近5日非全跌，20日涨跌={drop_20d:+.1f}%")
    else:
        issues = []
        if all_down_5:
            issues.append("近5日连续下跌")
        if drop_20d <= -15:
            issues.append(f"近20日跌幅{drop_20d:.1f}%")
        results["trend"] = (False, "，".join(issues))

    return results


def ma_arrangement(row) -> str:
    if row["ma5"] > row["ma10"] > row["ma20"]:
        return "多头排列"
    elif row["ma5"] < row["ma10"] < row["ma20"]:
        return "空头排列"
    else:
        return "粘合/交叉"


def boll_position(row) -> str:
    c = row["close"]
    if c >= row["boll_upper"]:
        return "上轨以上"
    elif c >= row["boll_mid"]:
        return "上轨~中轨"
    elif c >= row["boll_lower"]:
        return "中轨~下轨"
    else:
        return "下轨以下"


def print_separator(char="=", width=60):
    print(char * width)


def review_trade(con, buy_trade: dict, sell_trade: dict | None, latest_prices: dict):
    db_code   = code_to_db(buy_trade["code"])
    buy_date  = buy_trade["date"]
    buy_price = buy_trade["price"]
    name      = buy_trade["name"]

    # 加载历史K线（买入日及之前）
    df = load_kline(con, db_code, buy_date)
    if df.empty or len(df) < 30:
        print(f"  [跳过] {name} 数据不足")
        return None

    df = calc_indicators(df)
    idx = len(df) - 1  # 最后一行即买入当日
    row = df.iloc[idx]

    # ── 打印标题 ──
    print_separator()
    print(f"  {name} ({db_code})  买入日: {buy_date}  买入价: {buy_price:.3f}")
    print_separator()

    # ── 技术面还原 ──
    vol_ratio = (row["volume"] / row["vol_ma5"] * 100) if row["vol_ma5"] > 0 else float("nan")
    print(f"\n  [技术面还原 - 买入当日]")
    print(f"  收盘价: {row['close']:.2f}   成交量: {row['volume']/10000:.0f}万手  (相对5日均量: {vol_ratio:.0f}%)")
    print(f"  MA5:  {row['ma5']:.2f}   MA10: {row['ma10']:.2f}   MA20: {row['ma20']:.2f}   MA60: {row['ma60']:.2f}")
    print(f"  均线排列: {ma_arrangement(row)}")
    cross = "金叉" if row["dif"] > row["dea"] else "死叉"
    zero  = "零轴上方" if row["dif"] > 0 else "零轴下方"
    print(f"  MACD:  DIF={row['dif']:.3f}  DEA={row['dea']:.3f}  MACD柱={row['macd']:.3f}  {zero}  {cross}")
    print(f"  KDJ:   K={row['k']:.1f}  D={row['d']:.1f}  J={row['j']:.1f}")
    print(f"  RSI14: {row['rsi14']:.1f}")
    print(f"  布林带: 上轨={row['boll_upper']:.2f} 中轨={row['boll_mid']:.2f} 下轨={row['boll_lower']:.2f}  价格位置: {boll_position(row)}")

    # ── 技术面门槛检查 ──
    thresholds = check_thresholds(df, idx)
    passed = sum(1 for v, _ in thresholds.values() if v)
    print(f"\n  [技术面门槛检查 (CLAUDE.md 买入硬性条件)]")
    icons = {"macd": "MACD", "ma": "均线", "trend": "趋势"}
    for key, label in icons.items():
        ok, reason = thresholds[key]
        mark = "✅" if ok else "❌"
        print(f"  {mark} {label}: {reason}")
    verdict = "通过" if passed == 3 else f"未通过 ({passed}/3项满足)"
    print(f"  → 门槛结论: {verdict}")

    # ── 持有期追踪 ──
    print(f"\n  [持有期追踪]")
    if sell_trade:
        sell_date  = sell_trade["date"]
        sell_price = sell_trade["sell_price"]
        pnl        = sell_trade["pnl"]
        pnl_pct    = sell_trade["pnl_pct"]
        hold_days  = (pd.to_datetime(sell_date) - pd.to_datetime(buy_date)).days
        reason     = sell_trade.get("reason", "")

        # 持有期内每日价格
        hold_df = con.execute("""
            SELECT date, high, low, close
            FROM daily_price_qfq
            WHERE code = ? AND date BETWEEN ? AND ?
            ORDER BY date
        """, [db_code, buy_date, sell_date]).fetchdf()

        if not hold_df.empty:
            peak     = hold_df["high"].max()
            trough   = hold_df["low"].min()
            peak_pct = (peak / buy_price - 1) * 100
            trough_pct = (trough / buy_price - 1) * 100
            # 最大回撤：从持有期内最高收盘到最低收盘
            hold_close = hold_df["close"]
            rolling_max = hold_close.cummax()
            drawdown = ((hold_close - rolling_max) / rolling_max * 100).min()
            print(f"  卖出日: {sell_date}  持有: {hold_days}天  卖出价: {sell_price:.2f}  盈亏: {pnl:+.2f}元 ({pnl_pct:+.2f}%)")
            print(f"  持有期最高: {peak:.2f} ({peak_pct:+.1f}%)  最低: {trough:.2f} ({trough_pct:+.1f}%)  最大回撤: {drawdown:.1f}%")
            if reason:
                print(f"  卖出原因: {reason}")
        result_label = "盈利 ✅" if pnl > 0 else "亏损 ❌"
        print(f"  → 结果: {result_label}")
        print()
        return {
            "name": name,
            "buy_date": buy_date,
            "threshold_passed": passed == 3,
            "passed_count": passed,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_days": hold_days,
            "closed": True,
        }
    else:
        # 持仓未平，用最新价
        cur_price = latest_prices.get(buy_trade["code"])
        if cur_price:
            shares  = buy_trade["shares"]
            pnl     = (cur_price - buy_price) * shares
            pnl_pct = (cur_price / buy_price - 1) * 100
            hold_days = (pd.Timestamp.today() - pd.to_datetime(buy_date)).days

            hold_df = con.execute("""
                SELECT date, high, low, close
                FROM daily_price_qfq
                WHERE code = ? AND date >= ?
                ORDER BY date
            """, [db_code, buy_date]).fetchdf()

            if not hold_df.empty:
                peak     = hold_df["high"].max()
                trough   = hold_df["low"].min()
                peak_pct = (peak / buy_price - 1) * 100
                trough_pct = (trough / buy_price - 1) * 100
                hold_close = hold_df["close"]
                rolling_max = hold_close.cummax()
                drawdown = ((hold_close - rolling_max) / rolling_max * 100).min()
                print(f"  [持仓中]  当前价: {cur_price:.2f}  持有: {hold_days}天  浮盈亏: {pnl:+.2f}元 ({pnl_pct:+.2f}%)")
                print(f"  持有期最高: {peak:.2f} ({peak_pct:+.1f}%)  最低: {trough:.2f} ({trough_pct:+.1f}%)  最大回撤: {drawdown:.1f}%")
        else:
            pnl = None
            pnl_pct = None
            hold_days = None
            print(f"  [持仓中]  无当前价格信息")
        print()
        return {
            "name": name,
            "buy_date": buy_date,
            "threshold_passed": passed == 3,
            "passed_count": passed,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_days": hold_days,
            "closed": False,
        }


def build_sell_map(trades: list[dict]) -> dict:
    """为每笔买入找到对应的卖出记录，key = (code, buy_date)"""
    sell_map = {}
    for t in trades:
        if t["action"] == "sell":
            key = (t["code"], t["buy_date"])
            sell_map[key] = t
    return sell_map


def print_summary(results: list[dict]):
    print_separator("=")
    print("  汇总统计")
    print_separator("=")

    closed = [r for r in results if r["closed"] and r["pnl"] is not None]
    all_r  = [r for r in results if r is not None]

    total = len(all_r)
    passed = [r for r in all_r if r["threshold_passed"]]
    failed = [r for r in all_r if not r["threshold_passed"]]

    print(f"\n  总买入笔数: {total}")
    print(f"  技术门槛通过: {len(passed)} 笔 / 未通过: {len(failed)} 笔")

    if closed:
        wins  = [r for r in closed if r["pnl"] > 0]
        losses = [r for r in closed if r["pnl"] <= 0]
        total_pnl = sum(r["pnl"] for r in closed)
        avg_pnl_pct = sum(r["pnl_pct"] for r in closed) / len(closed)

        print(f"\n  已平仓: {len(closed)} 笔  盈利: {len(wins)} 笔  亏损: {len(losses)} 笔")
        print(f"  胜率: {len(wins)/len(closed)*100:.0f}%")
        print(f"  累计盈亏: {total_pnl:+.2f}元")
        print(f"  平均单笔: {avg_pnl_pct:+.2f}%")

        passed_closed = [r for r in closed if r["threshold_passed"]]
        failed_closed = [r for r in closed if not r["threshold_passed"]]

        if passed_closed:
            pw = sum(1 for r in passed_closed if r["pnl"] > 0)
            pa = sum(r["pnl_pct"] for r in passed_closed) / len(passed_closed)
            print(f"\n  [技术门槛通过的已平仓] {len(passed_closed)}笔  胜率: {pw/len(passed_closed)*100:.0f}%  平均: {pa:+.2f}%")
        if failed_closed:
            fw = sum(1 for r in failed_closed if r["pnl"] > 0)
            fa = sum(r["pnl_pct"] for r in failed_closed) / len(failed_closed)
            print(f"  [技术门槛未通过的已平仓] {len(failed_closed)}笔  胜率: {fw/len(failed_closed)*100:.0f}%  平均: {fa:+.2f}%")

    print()


def run():
    trades = load_trades()
    con = duckdb.connect(DB_PATH)

    # 构建卖出映射
    sell_map = build_sell_map(trades)

    # 获取数据库最新价（用于持仓中的股票）
    latest_row = con.execute("""
        SELECT code, close
        FROM daily_price_qfq
        WHERE date = (SELECT MAX(date) FROM daily_price_qfq)
    """).fetchdf()
    # 转为 short code -> price
    latest_prices = {}
    for _, r in latest_row.iterrows():
        short = r["code"].split(".")[1]
        latest_prices[short] = r["close"]

    # 只处理买入记录（加仓合并到同一只股票的第一笔买入）
    buy_trades = [t for t in trades if t["action"] == "buy"]

    results = []
    for buy in buy_trades:
        key = (buy["code"], buy["date"])
        sell = sell_map.get(key)
        result = review_trade(con, buy, sell, latest_prices)
        if result:
            results.append(result)

    print_summary(results)
    con.close()


if __name__ == "__main__":
    run()
