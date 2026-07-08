"""
策略回测脚本

回测 CLAUDE.md v2.0 技术面四项硬性门槛的历史表现：
1. MACD：DIF在零轴上方，或零轴下方金叉+底背离
2. 均线：股价站上MA10，且MA5上穿MA10或多头排列
3. 趋势：近5日非连续下跌，近20日跌幅<15%
4. KDJ：K<80

买入后使用移动止损规则持有：
- 浮盈<3%：固定止损-5%
- 浮盈3-5%：止损上移至成本价
- 浮盈5-10%：止损上移至成本+3%
- 浮盈10-15%：止损上移至成本+7%
- 浮盈>15%：跌破MA5清仓

出场完全由移动止损决定，不设最长持有天数限制（与实盘策略一致）。

用法：
  python3 backtest_strategy.py [--start 2024-06-01] [--end 2026-06-26] [--max-trades 5000]
"""

import argparse
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

# 每日最多触发的信号数（防止某天信号过多导致内存爆炸）
MAX_SIGNALS_PER_DAY = 50


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算技术指标"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)

    # MA
    ma5 = pd.Series(close).rolling(5).mean().values
    ma10 = pd.Series(close).rolling(10).mean().values
    ma20 = pd.Series(close).rolling(20).mean().values

    # MACD
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().values

    # KDJ
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

    # vol_ma5
    vol_ma5 = pd.Series(volume).rolling(5).mean().values

    df = df.copy()
    df["ma5"] = ma5
    df["ma10"] = ma10
    df["ma20"] = ma20
    df["dif"] = dif
    df["dea"] = dea
    df["k"] = k
    df["d"] = d
    df["vol_ma5"] = vol_ma5

    return df


def check_buy_signal(df: pd.DataFrame, idx: int) -> bool:
    """检查第idx行是否满足四项硬性买入门槛"""
    if idx < 26:
        return False

    row = df.iloc[idx]
    close = row["close"]
    dif = row["dif"]
    dea = row["dea"]
    ma5 = row["ma5"]
    ma10 = row["ma10"]
    k_val = row["k"]

    # 跳过nan
    if pd.isna(ma10) or pd.isna(dif) or pd.isna(k_val):
        return False

    # 1. MACD门槛
    dif_above_zero = dif > 0
    if not dif_above_zero:
        # 检查金叉
        if idx > 0:
            prev_dif = df.iloc[idx-1]["dif"]
            prev_dea = df.iloc[idx-1]["dea"]
            golden_cross = (dif > dea) and (prev_dif <= prev_dea)
            if not golden_cross:
                return False
            # 简化底背离检查：近20日内有更低价格但DIF更低的点
            window = df.iloc[max(0, idx-20):idx]
            lower_prices = window[window["close"] < close]
            if lower_prices.empty or not (lower_prices["dif"] < dif).any():
                return False
        else:
            return False

    # 2. 均线门槛
    if close <= ma10:
        return False
    if not (ma5 > ma10 or (ma5 > ma10 and ma10 > row["ma20"])):
        # ma5 > ma10 已经包含了多头排列的前提
        if ma5 <= ma10:
            return False

    # 3. 趋势门槛
    if idx >= 4:
        recent_closes = [df.iloc[idx-i]["close"] for i in range(5)]
        recent_closes.reverse()
        all_down = all(recent_closes[i] > recent_closes[i+1] for i in range(4))
        if all_down:
            return False

    if idx >= 20:
        close_20ago = df.iloc[idx-20]["close"]
        if close_20ago > 0 and (close / close_20ago - 1) < -0.15:
            return False

    # 4. KDJ门槛
    if k_val >= 80:
        return False

    return True


def simulate_trade(df: pd.DataFrame, buy_idx: int) -> dict:
    """
    从buy_idx+1开始模拟持有，完全靠移动止损出场，不设最长持有天数。
    """
    buy_price = df.iloc[buy_idx]["close"]
    buy_date = df.iloc[buy_idx]["date"]

    stop_loss = buy_price * 0.95  # 初始止损-5%
    sell_idx = None
    sell_reason = ""

    for i in range(buy_idx + 1, len(df)):
        row = df.iloc[i]
        cur_close = row["close"]
        cur_low = row["low"]
        cur_ma5 = row["ma5"]

        # 计算浮盈
        profit_pct = (cur_close / buy_price - 1) * 100

        # 检查是否触发止损（用最低价判断日内是否跌破）
        if cur_low <= stop_loss:
            sell_idx = i
            sell_reason = "触发止损"
            break

        # 更新移动止损
        if profit_pct >= 15:
            # 跌破MA5清仓
            if not pd.isna(cur_ma5) and cur_close < cur_ma5:
                sell_idx = i
                sell_reason = "浮盈>15%跌破MA5"
                break
            stop_loss = max(stop_loss, buy_price * 1.07)
        elif profit_pct >= 10:
            stop_loss = max(stop_loss, buy_price * 1.07)
        elif profit_pct >= 5:
            stop_loss = max(stop_loss, buy_price * 1.03)
        elif profit_pct >= 3:
            stop_loss = max(stop_loss, buy_price)  # 保本

    # 未触发卖出条件，持有至数据末尾
    if sell_idx is None:
        sell_idx = len(df) - 1
        if sell_idx <= buy_idx:
            return None
        sell_reason = "持有至今"

    sell_price = df.iloc[sell_idx]["close"]
    sell_date = df.iloc[sell_idx]["date"]
    pnl_pct = (sell_price / buy_price - 1) * 100
    hold_days = sell_idx - buy_idx

    return {
        "buy_date": str(buy_date)[:10],
        "sell_date": str(sell_date)[:10],
        "buy_price": buy_price,
        "sell_price": sell_price,
        "pnl_pct": pnl_pct,
        "hold_days": hold_days,
        "sell_reason": sell_reason,
    }


def run_backtest(start_date: str, end_date: str, max_trades: int = 10000):
    con = duckdb.connect(DB_PATH, read_only=True)

    # 获取符合条件的股票列表（沪深主板+创业板，排除科创/北证）
    stocks = con.execute("""
        SELECT DISTINCT code FROM daily_price_qfq
        WHERE (code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%')
          AND code NOT LIKE 'sh.688%'
    """).fetchdf()["code"].tolist()

    print(f"回测范围：{start_date} ~ {end_date}")
    print(f"标的数量：{len(stocks)} 只（沪深主板+创业板）")
    print(f"策略：技术面四项硬性门槛 + 移动止损（无持有天数限制）")
    print("-" * 60)

    all_trades = []
    processed = 0
    signal_count = 0

    for stock_code in stocks:
        # 加载该股票在回测期间（加上前60天用于计算指标）的数据
        df = con.execute("""
            SELECT date, open, high, low, close, volume, amount
            FROM daily_price_qfq
            WHERE code = ?
              AND date >= (SELECT MAX(d) FROM (
                  SELECT date as d FROM daily_price_qfq
                  WHERE code = ? AND date < ?
                  ORDER BY date DESC LIMIT 60
              ))
              AND date <= ?
            ORDER BY date
        """, [stock_code, stock_code, start_date, end_date]).fetchdf()

        if len(df) < 60:
            continue

        # 计算指标
        df = calc_indicators(df)

        # 找到start_date对应的起始索引
        df["date"] = pd.to_datetime(df["date"])
        start_mask = df["date"] >= pd.to_datetime(start_date)
        if not start_mask.any():
            continue
        scan_start = start_mask.idxmax()

        # 流动性过滤：日均成交额>5000万
        avg_amount = df["amount"].mean()
        if avg_amount < 50000000:
            continue

        # 扫描买入信号
        i = scan_start
        while i < len(df) - 1:
            if check_buy_signal(df, i):
                signal_count += 1
                result = simulate_trade(df, i)
                if result:
                    result["code"] = stock_code
                    all_trades.append(result)
                    # 卖出后才能再次买入同一只股
                    i += result["hold_days"] + 1
                else:
                    i += 1
            else:
                i += 1

            if len(all_trades) >= max_trades:
                break

        processed += 1
        if processed % 500 == 0:
            print(f"  已处理 {processed}/{len(stocks)} 只股票，累计信号 {signal_count}，成交 {len(all_trades)} 笔")

        if len(all_trades) >= max_trades:
            print(f"  达到最大交易数限制 {max_trades}，停止扫描")
            break

    con.close()

    print(f"\n扫描完成：处理 {processed} 只股票，发现 {signal_count} 个买入信号")
    print("=" * 60)

    if not all_trades:
        print("无交易记录")
        return

    # 统计分析
    df_trades = pd.DataFrame(all_trades)
    print_stats(df_trades)

    return df_trades


def print_stats(df: pd.DataFrame):
    total = len(df)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]

    win_rate = len(wins) / total * 100
    avg_pnl = df["pnl_pct"].mean()
    median_pnl = df["pnl_pct"].median()
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    avg_hold = df["hold_days"].mean()

    print(f"\n{'='*60}")
    print(f"  回测结果统计")
    print(f"{'='*60}")
    print(f"\n  总交易笔数：{total}")
    print(f"  盈利笔数：{len(wins)}    亏损笔数：{len(losses)}")
    print(f"  胜率：{win_rate:.1f}%")
    print(f"  平均收益：{avg_pnl:+.2f}%")
    print(f"  收益中位数：{median_pnl:+.2f}%")
    print(f"  平均盈利：{avg_win:+.2f}%    平均亏损：{avg_loss:+.2f}%")
    print(f"  盈亏比：{profit_factor:.2f}")
    print(f"  平均持仓天数：{avg_hold:.1f}")

    # 收益分布
    print(f"\n  收益分布：")
    bins = [-100, -10, -5, -3, 0, 3, 5, 10, 20, 100]
    labels = ["<-10%", "-10~-5%", "-5~-3%", "-3~0%", "0~3%", "3~5%", "5~10%", "10~20%", ">20%"]
    df["bin"] = pd.cut(df["pnl_pct"], bins=bins, labels=labels)
    dist = df["bin"].value_counts().sort_index()
    for label, count in dist.items():
        pct = count / total * 100
        bar = "█" * int(pct / 2)
        print(f"    {label:>8s}: {count:>4d} ({pct:>5.1f}%) {bar}")

    # 按卖出原因统计
    print(f"\n  卖出原因分布：")
    for reason, group in df.groupby("sell_reason"):
        cnt = len(group)
        avg = group["pnl_pct"].mean()
        wr = (group["pnl_pct"] > 0).mean() * 100
        print(f"    {reason}: {cnt}笔 胜率{wr:.0f}% 平均{avg:+.2f}%")

    # 按月统计
    df["month"] = pd.to_datetime(df["buy_date"]).dt.to_period("M")
    print(f"\n  月度表现：")
    for month, group in df.groupby("month"):
        cnt = len(group)
        avg = group["pnl_pct"].mean()
        wr = (group["pnl_pct"] > 0).mean() * 100
        print(f"    {month}: {cnt}笔 胜率{wr:.0f}% 平均{avg:+.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="策略回测")
    parser.add_argument("--start", default="2024-06-01", help="回测开始日期")
    parser.add_argument("--end", default="2026-06-26", help="回测结束日期")
    parser.add_argument("--max-trades", type=int, default=10000, help="最大交易笔数")
    args = parser.parse_args()

    result = run_backtest(args.start, args.end, args.max_trades)
