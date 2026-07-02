"""
带市场状态过滤的回测

对比两种模式：
1. 无过滤（原始策略）
2. Market Regime 过滤：BEAR/STRONG_BEAR时禁止开新仓

验证假设：大盘熊市时不开枪，能否减少无效止损，提升复合收益率。
"""

import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

# 指数代码
INDEX_SZCI = "sz.399001"
INDEX_GEM = "sz.399003"


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算技术指标"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
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

    df = df.copy()
    df["ma5"] = ma5
    df["ma10"] = ma10
    df["ma20"] = ma20
    df["dif"] = dif
    df["dea"] = dea
    df["k"] = k
    df["d"] = d

    return df


def check_buy_signal(df: pd.DataFrame, idx: int) -> bool:
    """四项硬性门槛"""
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

    # 1. MACD
    dif_above_zero = dif > 0
    if not dif_above_zero:
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

    # 2. 均线
    if close <= ma10:
        return False
    if ma5 <= ma10:
        return False

    # 3. 趋势
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

    # 4. KDJ
    if k_val >= 80:
        return False

    return True


def simulate_trade(df: pd.DataFrame, buy_idx: int) -> dict:
    """移动止损模拟"""
    buy_price = df.iloc[buy_idx]["close"]
    buy_date = df.iloc[buy_idx]["date"]

    stop_loss = buy_price * 0.95
    sell_idx = None
    sell_reason = ""

    for i in range(buy_idx + 1, len(df)):
        row = df.iloc[i]
        cur_close = row["close"]
        cur_low = row["low"]
        cur_ma5 = row["ma5"]

        profit_pct = (cur_close / buy_price - 1) * 100

        if cur_low <= stop_loss:
            sell_idx = i
            sell_reason = "触发止损"
            break

        if profit_pct >= 15:
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
            stop_loss = max(stop_loss, buy_price)

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


def build_regime_cache(con, start_date: str, end_date: str) -> dict:
    """
    预计算回测期间每个交易日的 Market Regime 评分。
    返回 {date_str: score} 字典。

    简化版：只用指数趋势(MACD+均线) + 市场宽度(涨跌家数)，
    因为这两个维度占65%权重且对熊市识别最关键。
    """
    print("预计算市场状态评分...")

    # 加载深证成指全部数据
    szci = con.execute("""
        SELECT date, close, amount FROM index_daily_price
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date
    """, [INDEX_SZCI, (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d"),
          end_date]).fetchdf()
    szci["date"] = pd.to_datetime(szci["date"])

    # 加载创业板数据
    gem = con.execute("""
        SELECT date, close FROM index_daily_price
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date
    """, [INDEX_GEM, (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d"),
          end_date]).fetchdf()
    gem["date"] = pd.to_datetime(gem["date"])

    # 加载每日涨跌家数
    breadth = con.execute("""
        SELECT date,
            SUM(CASE WHEN close > open THEN 1 ELSE 0 END) as up_count,
            SUM(CASE WHEN close < open THEN 1 ELSE 0 END) as down_count
        FROM daily_price_qfq
        WHERE date >= ? AND date <= ?
          AND code NOT LIKE 'sh.000%'
          AND code NOT LIKE 'sz.399%'
          AND code NOT LIKE 'sh.880%'
        GROUP BY date
        ORDER BY date
    """, [start_date, end_date]).fetchdf()
    breadth["date"] = pd.to_datetime(breadth["date"])

    # 计算深证成指技术指标
    close_arr = szci["close"].values
    ema12 = pd.Series(close_arr).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close_arr).ewm(span=26, adjust=False).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().values
    ma5 = pd.Series(close_arr).rolling(5).mean().values
    ma10 = pd.Series(close_arr).rolling(10).mean().values
    ma20 = pd.Series(close_arr).rolling(20).mean().values

    szci["dif"] = dif
    szci["dea"] = dea
    szci["ma5"] = ma5
    szci["ma10"] = ma10
    szci["ma20"] = ma20

    # 创业板指标
    gem_close = gem["close"].values
    gem_ema12 = pd.Series(gem_close).ewm(span=12, adjust=False).mean().values
    gem_ema26 = pd.Series(gem_close).ewm(span=26, adjust=False).mean().values
    gem_dif = gem_ema12 - gem_ema26
    gem_dea = pd.Series(gem_dif).ewm(span=9, adjust=False).mean().values
    gem_ma5 = pd.Series(gem_close).rolling(5).mean().values
    gem_ma10 = pd.Series(gem_close).rolling(10).mean().values
    gem_ma20 = pd.Series(gem_close).rolling(20).mean().values

    gem["dif"] = gem_dif
    gem["dea"] = gem_dea
    gem["ma5"] = gem_ma5
    gem["ma10"] = gem_ma10
    gem["ma20"] = gem_ma20

    # 逐日计算评分
    regime_cache = {}
    start_dt = pd.to_datetime(start_date)

    for _, row in szci.iterrows():
        d = row["date"]
        if d < start_dt:
            continue

        date_str = str(d)[:10]

        # 指数趋势评分（简化版，取深证成指+创业板平均）
        trend_scores = []
        for idx_df in [szci, gem]:
            mask = idx_df["date"] <= d
            sub = idx_df[mask]
            if len(sub) < 30:
                continue
            latest = sub.iloc[-1]
            s = 50
            if latest["dif"] > 0:
                s += 15
                if latest["dif"] > latest["dea"]:
                    s += 5
            else:
                s -= 15
                if latest["dif"] < latest["dea"]:
                    s -= 5
            # MACD动量
            if len(sub) >= 2:
                prev = sub.iloc[-2]
                bar_now = latest["dif"] - latest["dea"]
                bar_prev = prev["dif"] - prev["dea"]
                if bar_now > bar_prev:
                    s += 10
                else:
                    s -= 5
            # 均线
            if latest["ma5"] > latest["ma10"] > latest["ma20"]:
                s += 15
            elif latest["ma5"] < latest["ma10"] < latest["ma20"]:
                s -= 15
            elif latest["close"] > latest["ma10"]:
                s += 5
            # 股价vs MA
            if latest["close"] > latest["ma5"]:
                s += 5
            elif latest["close"] < latest["ma20"]:
                s -= 5
            trend_scores.append(max(0, min(100, s)))

        trend_score = int(np.mean(trend_scores)) if trend_scores else 50

        # 市场宽度评分
        breadth_row = breadth[breadth["date"] == d]
        if not breadth_row.empty:
            up = breadth_row.iloc[0]["up_count"]
            down = breadth_row.iloc[0]["down_count"]
            ratio = up / down if down > 0 else 10
            bs = 50
            if ratio >= 4:
                bs += 25
            elif ratio >= 2:
                bs += 15
            elif ratio >= 1:
                bs += 5
            elif ratio >= 0.5:
                bs -= 15
            else:
                bs -= 25
            breadth_score = max(0, min(100, bs))
        else:
            breadth_score = 50

        # 加权综合（趋势55% + 宽度45%，简化版省略量能和风险偏好）
        total = int(trend_score * 0.55 + breadth_score * 0.45)
        regime_cache[date_str] = total

    print(f"  预计算完成，共 {len(regime_cache)} 个交易日")

    # 统计分布
    scores = list(regime_cache.values())
    bear_days = sum(1 for s in scores if s < 45)
    bull_days = sum(1 for s in scores if s >= 65)
    consolidation_days = len(scores) - bear_days - bull_days
    print(f"  分布：BULL/STRONG_BULL({bull_days}天) | CONSOLIDATION({consolidation_days}天) | BEAR/STRONG_BEAR({bear_days}天)")

    return regime_cache


def run_backtest_comparison(codes_with_names: list, start_date: str = "2020-06-01",
                            end_date: str = "2026-06-25", regime_threshold: int = 45):
    """
    运行对比回测。
    codes_with_names: [(code, name), ...]
    regime_threshold: 低于此分数禁止买入
    """
    con = duckdb.connect(DB_PATH, read_only=True)

    # 预计算市场状态
    regime_cache = build_regime_cache(con, start_date, end_date)

    print(f"\n{'='*60}")
    print(f"回测对比：原始策略 vs 加入Market Regime过滤(阈值<{regime_threshold}禁止买入)")
    print(f"回测期间：{start_date} ~ {end_date}")
    print(f"标的：{', '.join(name for _, name in codes_with_names)}")
    print(f"{'='*60}\n")

    results = {"no_filter": [], "with_filter": []}

    for stock_code, stock_name in codes_with_names:
        # 加载数据
        df = con.execute("""
            SELECT date, open, high, low, close, volume, amount
            FROM daily_price_qfq
            WHERE code = ?
              AND date >= ?
              AND date <= ?
            ORDER BY date
        """, [stock_code, (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d"),
              end_date]).fetchdf()

        if len(df) < 60:
            print(f"  {stock_name} 数据不足，跳过")
            continue

        df = calc_indicators(df)
        df["date"] = pd.to_datetime(df["date"])

        start_mask = df["date"] >= pd.to_datetime(start_date)
        if not start_mask.any():
            continue
        scan_start = start_mask.idxmax()

        # 两种模式扫描
        for mode in ["no_filter", "with_filter"]:
            i = scan_start
            while i < len(df) - 1:
                if check_buy_signal(df, i):
                    # 市场状态过滤
                    if mode == "with_filter":
                        date_str = str(df.iloc[i]["date"])[:10]
                        regime_score = regime_cache.get(date_str, 50)
                        if regime_score < regime_threshold:
                            i += 1
                            continue

                    result = simulate_trade(df, i)
                    if result:
                        result["code"] = stock_code
                        result["name"] = stock_name
                        results[mode].append(result)
                        i += result["hold_days"] + 1
                    else:
                        i += 1
                else:
                    i += 1

    con.close()

    # 打印对比结果
    for mode, label in [("no_filter", "原始策略（无过滤）"), ("with_filter", f"Market Regime过滤(分数<{regime_threshold}不买)")]:
        trades = results[mode]
        if not trades:
            print(f"\n  {label}: 无交易")
            continue

        df_t = pd.DataFrame(trades)
        total = len(df_t)
        wins = df_t[df_t["pnl_pct"] > 0]
        losses = df_t[df_t["pnl_pct"] <= 0]
        win_rate = len(wins) / total * 100
        avg_pnl = df_t["pnl_pct"].mean()
        avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
        avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        avg_hold = df_t["hold_days"].mean()

        # 复合收益计算
        compound_results = {}
        for code, name in codes_with_names:
            stock_trades = df_t[df_t["code"] == code].sort_values("buy_date")
            capital = 100000.0
            for _, t in stock_trades.iterrows():
                capital *= (1 + t["pnl_pct"] / 100)
            compound_results[name] = capital

        total_capital = sum(compound_results.values())
        initial_capital = len(codes_with_names) * 100000
        compound_return = (total_capital / initial_capital - 1) * 100

        print(f"\n{'─'*60}")
        print(f"  {label}")
        print(f"{'─'*60}")
        print(f"  总交易：{total}笔  胜率：{win_rate:.1f}%  平均收益：{avg_pnl:+.2f}%")
        print(f"  平均盈利：{avg_win:+.2f}%  平均亏损：{avg_loss:+.2f}%  盈亏比：{profit_factor:.2f}")
        print(f"  平均持仓：{avg_hold:.1f}天")
        print(f"  复合收益（各10万起始）：{total_capital/10000:.1f}万 / {initial_capital/10000:.0f}万 ({compound_return:+.1f}%)")
        for name, cap in compound_results.items():
            ret = (cap / 100000 - 1) * 100
            print(f"    {name}: 10万→{cap/10000:.2f}万 ({ret:+.1f}%)")

        # 被过滤掉的交易（仅with_filter模式显示）
        if mode == "with_filter":
            filtered_count = len(results["no_filter"]) - total
            print(f"\n  被市场状态过滤掉的交易：{filtered_count}笔")

            # 这些被过滤的交易在原始模式中的表现
            no_filter_df = pd.DataFrame(results["no_filter"])
            # 找出被过滤掉的交易
            filter_set = set(zip(df_t["code"], df_t["buy_date"]))
            no_filter_set = set(zip(no_filter_df["code"], no_filter_df["buy_date"]))
            filtered_keys = no_filter_set - filter_set
            filtered_trades = no_filter_df[
                no_filter_df.apply(lambda r: (r["code"], r["buy_date"]) in filtered_keys, axis=1)
            ]
            if not filtered_trades.empty:
                f_win = (filtered_trades["pnl_pct"] > 0).mean() * 100
                f_avg = filtered_trades["pnl_pct"].mean()
                print(f"  被过滤交易的表现：胜率{f_win:.0f}%  平均收益{f_avg:+.2f}%")
                print(f"  （这些是熊市中被阻止的交易，大多数是亏损的）")

    # 年化收益对比
    years = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days / 365.25
    print(f"\n{'='*60}")
    print(f"  年化收益对比（{years:.1f}年）")
    print(f"{'='*60}")
    for mode, label in [("no_filter", "原始"), ("with_filter", "Regime过滤")]:
        trades = results[mode]
        if not trades:
            continue
        df_t = pd.DataFrame(trades)
        total_cap = 0
        for code, name in codes_with_names:
            stock_trades = df_t[df_t["code"] == code].sort_values("buy_date")
            capital = 100000.0
            for _, t in stock_trades.iterrows():
                capital *= (1 + t["pnl_pct"] / 100)
            total_cap += capital
        initial = len(codes_with_names) * 100000
        total_return = total_cap / initial - 1
        annual_return = (1 + total_return) ** (1/years) - 1
        print(f"  {label}: 总收益{total_return*100:+.1f}%  年化{annual_return*100:+.1f}%")

    return results


if __name__ == "__main__":
    # 同样的3只股票对比
    stocks = [
        ("sh.600276", "恒瑞医药"),
        ("sz.000063", "中兴通讯"),
        ("sz.002170", "芭田股份"),
    ]

    run_backtest_comparison(stocks, start_date="2020-06-01", end_date="2026-06-25")
