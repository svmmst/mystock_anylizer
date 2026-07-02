"""
全市场动态选股回测系统

模拟真实操作：每个交易日对全市场执行 L2 技术快筛，按信号强度排序选最优标的，
统一资金池管理组合（仓位控制+移动止损），配合 Market Regime 过滤。

与现有回测的区别：
- 不局限于固定标的，每日从全市场筛选
- 统一100万资金池，最多5只持仓，单只≤25%
- 信号排序选最优，不是遇到就买
- Market Regime < 45 时禁止新开仓

用法：
  python3 backtest_dynamic.py [--start 2025-06-01] [--end 2026-06-26] [--capital 1000000]
"""

import argparse
import time
from dataclasses import dataclass, field
from datetime import timedelta

import duckdb
import numpy as np
import pandas as pd

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

# 指数代码
INDEX_SZCI = "sz.399001"  # 深证成指
INDEX_GEM = "sz.399003"   # 创业板指

# 回测参数
MAX_POSITIONS = 5          # 最大持仓数
MAX_SINGLE_PCT = 0.25      # 单只仓位上限
STOP_LOSS_PCT = 0.05       # 初始止损幅度
SLIPPAGE = 0.001           # 滑点（单边千一）
COMMISSION = 0.0015        # 佣金+印花税（单边万一点五，双边合计千三）
MIN_AMOUNT = 50000000      # 流动性门槛：日均成交额5000万
REGIME_THRESHOLD = 45      # 市场状态阈值：低于此分数禁止开新仓


@dataclass
class Position:
    """持仓记录"""
    code: str
    buy_date: str
    buy_price: float
    shares: int
    cost: float           # 总成本（含手续费）
    stop_loss: float      # 当前止损位
    highest_profit_pct: float = 0.0  # 历史最高浮盈%


@dataclass
class Portfolio:
    """组合状态"""
    cash: float
    positions: list = field(default_factory=list)
    trades: list = field(default_factory=list)  # 已完成交易记录
    daily_nav: list = field(default_factory=list)  # 每日净值

    @property
    def position_count(self):
        return len(self.positions)

    def total_value(self, prices: dict) -> float:
        """计算总市值（现金+持仓市值）"""
        stock_value = sum(
            prices.get(p.code, p.buy_price) * p.shares
            for p in self.positions
        )
        return self.cash + stock_value


# ─── 技术指标计算 ────────────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算MA/MACD/KDJ等技术指标"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values.astype(float)
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

    # 量均线
    vol_ma5 = pd.Series(volume).rolling(5).mean().values
    # 20日均成交额（滚动，避免未来信息泄露）
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


# ─── 买入信号检测 ────────────────────────────────────────────

def check_buy_signal(df: pd.DataFrame, idx: int) -> bool:
    """四项硬性门槛检查"""
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


# ─── 信号评分排序 ────────────────────────────────────────────

def score_signal(df: pd.DataFrame, idx: int) -> float:
    """对通过硬性门槛的信号打分，用于排序选最优"""
    row = df.iloc[idx]

    # MACD动量分（30%）：DIF-DEA越大越好，归一化到0-100
    macd_bar = row["dif"] - row["dea"]
    prev_bar = df.iloc[idx-1]["dif"] - df.iloc[idx-1]["dea"] if idx > 0 else 0
    macd_momentum = macd_bar - prev_bar  # 增量
    # 归一化：增量在 0-0.5 映射到 50-100，<0 映射到 0-50
    macd_score = min(100, max(0, 50 + macd_momentum * 100))

    # 量比分（25%）：1.2-3.0之间最好
    vol_ratio = row["volume"] / row["vol_ma5"] if row["vol_ma5"] > 0 else 1.0
    if 1.2 <= vol_ratio <= 3.0:
        vol_score = 60 + (vol_ratio - 1.2) * 20  # 1.2→60, 3.0→96
    elif vol_ratio > 3.0:
        vol_score = 50  # 过度放量扣分
    else:
        vol_score = vol_ratio / 1.2 * 60  # 不放量偏低

    # KDJ位置分（20%）：越低空间越大
    k_val = row["k"]
    if k_val <= 30:
        kdj_score = 90
    elif k_val <= 50:
        kdj_score = 70 + (50 - k_val) * 1
    elif k_val <= 70:
        kdj_score = 50 + (70 - k_val) * 1
    else:
        kdj_score = 30

    # 趋势强度分（25%）：20日涨幅0-10%最佳
    close_20ago = df.iloc[idx-20]["close"] if idx >= 20 else row["close"]
    change_20d = (row["close"] / close_20ago - 1) * 100 if close_20ago > 0 else 0
    if 0 <= change_20d <= 10:
        trend_score = 80 + change_20d  # 0%→80, 10%→90
    elif -5 <= change_20d < 0:
        trend_score = 60 + change_20d * 4  # -5%→40, 0%→60
    elif change_20d > 10:
        trend_score = max(20, 90 - (change_20d - 10) * 3)  # 追高扣分
    else:
        trend_score = 30

    total = macd_score * 0.30 + vol_score * 0.25 + kdj_score * 0.20 + trend_score * 0.25
    return total


# ─── 额外过滤 ────────────────────────────────────────────────

def extra_filters(df: pd.DataFrame, idx: int) -> bool:
    """额外过滤：防追高、排除涨停"""
    row = df.iloc[idx]

    # 涨停排除（收盘涨幅>9.5%的大概率涨停，次日无法买入）
    if idx > 0:
        prev_close = df.iloc[idx-1]["close"]
        if prev_close > 0:
            change_pct = (row["close"] / prev_close - 1) * 100
            if change_pct > 9.5:
                return False

    # 偏离MA5>5%排除（防追高）
    if row["ma5"] > 0:
        ma5_dev = (row["close"] / row["ma5"] - 1) * 100
        if ma5_dev > 5:
            return False

    return True


# ─── 移动止损更新 ────────────────────────────────────────────

def update_stop_loss(pos: Position, current_close: float, current_ma5: float):
    """根据浮盈更新移动止损位"""
    profit_pct = (current_close / pos.buy_price - 1) * 100
    pos.highest_profit_pct = max(pos.highest_profit_pct, profit_pct)

    if profit_pct >= 15:
        pos.stop_loss = max(pos.stop_loss, pos.buy_price * 1.07)
    elif profit_pct >= 10:
        pos.stop_loss = max(pos.stop_loss, pos.buy_price * 1.07)
    elif profit_pct >= 5:
        pos.stop_loss = max(pos.stop_loss, pos.buy_price * 1.03)
    elif profit_pct >= 3:
        pos.stop_loss = max(pos.stop_loss, pos.buy_price)


def check_sell_signal(pos: Position, row: pd.Series) -> tuple:
    """检查是否触发卖出条件，返回 (should_sell, reason)"""
    cur_low = row["low"]
    cur_close = row["close"]
    cur_ma5 = row["ma5"]

    # 1. 日内最低跌破止损
    if cur_low <= pos.stop_loss:
        return True, "触发止损"

    # 2. 浮盈>15% 跌破MA5
    profit_pct = (cur_close / pos.buy_price - 1) * 100
    if profit_pct >= 15 and not pd.isna(cur_ma5) and cur_close < cur_ma5:
        return True, "浮盈>15%跌破MA5"

    return False, ""


# ─── Market Regime 缓存 ──────────────────────────────────────

def build_regime_cache(con, start_date: str, end_date: str) -> dict:
    """预计算市场状态评分。复用 backtest_with_regime.py 的逻辑。"""
    print("预计算市场状态评分...")

    # 加载深证成指
    szci = con.execute("""
        SELECT date, close, amount FROM index_daily_price
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date
    """, [INDEX_SZCI, (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d"),
          end_date]).fetchdf()
    szci["date"] = pd.to_datetime(szci["date"])

    # 加载创业板指
    gem = con.execute("""
        SELECT date, close FROM index_daily_price
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date
    """, [INDEX_GEM, (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d"),
          end_date]).fetchdf()
    gem["date"] = pd.to_datetime(gem["date"])

    # 全市场涨跌家数
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

    # 深证成指技术指标
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

    regime_cache = {}
    start_dt = pd.to_datetime(start_date)

    for _, row in szci.iterrows():
        d = row["date"]
        if d < start_dt:
            continue

        date_str = str(d)[:10]

        # 指数趋势评分
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
            if len(sub) >= 2:
                prev = sub.iloc[-2]
                bar_now = latest["dif"] - latest["dea"]
                bar_prev = prev["dif"] - prev["dea"]
                if bar_now > bar_prev:
                    s += 10
                else:
                    s -= 5
            if latest["ma5"] > latest["ma10"] > latest["ma20"]:
                s += 15
            elif latest["ma5"] < latest["ma10"] < latest["ma20"]:
                s -= 15
            elif latest["close"] > latest["ma10"]:
                s += 5
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

        total = int(trend_score * 0.55 + breadth_score * 0.45)
        regime_cache[date_str] = total

    print(f"  完成，共 {len(regime_cache)} 个交易日")
    scores = list(regime_cache.values())
    bear_days = sum(1 for s in scores if s < 45)
    bull_days = sum(1 for s in scores if s >= 65)
    print(f"  分布：BULL({bull_days}天) | CONSOLIDATION({len(scores)-bear_days-bull_days}天) | BEAR({bear_days}天)")

    return regime_cache


# ─── 数据预加载 ───────────────────────────────────────────────

def preload_stock_data(con, start_date: str, end_date: str) -> dict:
    """
    预加载全部合格股票的数据并计算指标。
    返回 {code: DataFrame}
    """
    print("预加载股票数据...")

    # 获取流动性合格的股票（日均成交额>5000万）
    qualified_codes = con.execute("""
        SELECT code, AVG(amount) as avg_amount
        FROM daily_price_qfq
        WHERE date >= ? AND date <= ?
          AND (code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%')
          AND code NOT LIKE 'sh.688%'
        GROUP BY code
        HAVING AVG(amount) > ?
    """, [start_date, end_date, MIN_AMOUNT]).fetchdf()

    codes = qualified_codes["code"].tolist()
    print(f"  流动性合格股票：{len(codes)} 只（日均成交额>{MIN_AMOUNT/10000:.0f}万）")

    # 加载数据（含指标计算预热期60天）
    load_start = (pd.to_datetime(start_date) - timedelta(days=90)).strftime("%Y-%m-%d")

    stock_data = {}
    loaded = 0

    for code in codes:
        df = con.execute("""
            SELECT date, open, high, low, close, volume, amount
            FROM daily_price_qfq
            WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """, [code, load_start, end_date]).fetchdf()

        if len(df) < 60:
            continue

        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date", drop=False)
        df = calc_indicators(df)

        stock_data[code] = df
        loaded += 1

        if loaded % 500 == 0:
            print(f"  已加载 {loaded}/{len(codes)} ...")

    print(f"  加载完成：{loaded} 只股票")
    return stock_data


# ─── 获取交易日列表 ───────────────────────────────────────────

def get_trade_dates(con, start_date: str, end_date: str) -> list:
    """获取回测期间的交易日列表"""
    rows = con.execute("""
        SELECT DISTINCT date FROM index_daily_price
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date
    """, [INDEX_SZCI, start_date, end_date]).fetchall()
    return [row[0] for row in rows]


# ─── 主回测引擎 ──────────────────────────────────────────────

def run_backtest(start_date: str, end_date: str, initial_capital: float = 1000000):
    t_start = time.time()
    con = duckdb.connect(DB_PATH, read_only=True)

    # 1. 预计算 Market Regime
    regime_cache = build_regime_cache(con, start_date, end_date)

    # 2. 预加载股票数据
    stock_data = preload_stock_data(con, start_date, end_date)

    # 3. 获取交易日列表
    trade_dates = get_trade_dates(con, start_date, end_date)
    print(f"\n回测期间：{start_date} ~ {end_date}，共 {len(trade_dates)} 个交易日")
    print(f"初始资金：{initial_capital:,.0f}")
    print(f"参数：最多{MAX_POSITIONS}只持仓，单只≤{MAX_SINGLE_PCT*100:.0f}%，Regime阈值{REGIME_THRESHOLD}")
    print("-" * 60)

    con.close()

    # 初始化组合
    portfolio = Portfolio(cash=initial_capital)
    regime_filtered_count = 0
    total_signals = 0

    # 逐日回测
    for day_idx, trade_date in enumerate(trade_dates):
        date_str = str(trade_date)[:10]
        date_ts = pd.to_datetime(date_str)

        # 获取当日所有持仓的行情
        current_prices = {}
        for pos in portfolio.positions:
            if pos.code in stock_data:
                df = stock_data[pos.code]
                if date_ts in df.index:
                    current_prices[pos.code] = df.loc[date_ts, "close"]

        # ──── 步骤1：更新持仓状态（检查止损/止盈）────
        positions_to_close = []
        for pos in portfolio.positions:
            if pos.code not in stock_data:
                continue
            df = stock_data[pos.code]
            if date_ts not in df.index:
                continue

            row = df.loc[date_ts]
            cur_close = row["close"]

            # 检查卖出信号
            should_sell, reason = check_sell_signal(pos, row)
            if should_sell:
                # 卖出：以收盘价计算（保守假设）
                sell_price = cur_close * (1 - SLIPPAGE)
                proceeds = sell_price * pos.shares * (1 - COMMISSION)
                pnl = proceeds - pos.cost
                pnl_pct = (sell_price / pos.buy_price - 1) * 100
                hold_days = (date_ts - pd.to_datetime(pos.buy_date)).days

                portfolio.trades.append({
                    "code": pos.code,
                    "buy_date": pos.buy_date,
                    "sell_date": date_str,
                    "buy_price": pos.buy_price,
                    "sell_price": sell_price,
                    "shares": pos.shares,
                    "cost": pos.cost,
                    "proceeds": proceeds,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_days": hold_days,
                    "sell_reason": reason,
                })
                portfolio.cash += proceeds
                positions_to_close.append(pos)
            else:
                # 更新移动止损
                update_stop_loss(pos, cur_close, row["ma5"])

        for pos in positions_to_close:
            portfolio.positions.remove(pos)

        # 记录每日净值
        nav = portfolio.total_value(current_prices)
        portfolio.daily_nav.append({"date": date_str, "nav": nav})

        # ──── 步骤2：Market Regime 过滤 ────
        regime_score = regime_cache.get(date_str, 50)
        if regime_score < REGIME_THRESHOLD:
            regime_filtered_count += 1
            continue

        # ──── 步骤3：检查是否有空余仓位 ────
        available_slots = MAX_POSITIONS - portfolio.position_count
        if available_slots <= 0:
            continue

        # Regime 45-65 限制仓位上限60%
        if regime_score < 65:
            max_stock_value = nav * 0.60 / MAX_POSITIONS
        else:
            max_stock_value = nav * MAX_SINGLE_PCT

        # ──── 步骤4：全市场L2技术快筛 ────
        signals = []
        for code, df in stock_data.items():
            # 跳过已持仓的
            if any(p.code == code for p in portfolio.positions):
                continue

            if date_ts not in df.index:
                continue

            # 获取该日在 df 中的位置索引
            idx = df.index.get_loc(date_ts)
            if isinstance(idx, slice):
                idx = idx.start

            # 流动性滚动检查（20日均成交额）
            row = df.iloc[idx]
            if pd.isna(row["amount_ma20"]) or row["amount_ma20"] < MIN_AMOUNT:
                continue

            # 四项硬性门槛
            if not check_buy_signal(df, idx):
                continue

            # 额外过滤
            if not extra_filters(df, idx):
                continue

            # 打分
            score = score_signal(df, idx)
            signals.append((code, idx, score))

        total_signals += len(signals)

        if not signals:
            continue

        # ──── 步骤5：排序选最优 ────
        signals.sort(key=lambda x: x[2], reverse=True)
        selected = signals[:available_slots]

        # ──── 步骤6：次日开盘买入 ────
        if day_idx + 1 >= len(trade_dates):
            break

        next_date = trade_dates[day_idx + 1]
        next_date_ts = pd.to_datetime(str(next_date)[:10])

        for code, _, _ in selected:
            df = stock_data[code]
            if next_date_ts not in df.index:
                continue

            next_row = df.loc[next_date_ts]
            buy_price = next_row["open"] * (1 + SLIPPAGE)

            if buy_price <= 0:
                continue

            # 计算可用资金和仓位
            position_value = min(max_stock_value, portfolio.cash * 0.95)
            if position_value < 10000:
                break  # 资金不足

            shares = int(position_value / buy_price / 100) * 100  # 整手
            if shares <= 0:
                continue

            cost = buy_price * shares * (1 + COMMISSION)
            if cost > portfolio.cash:
                shares = int(portfolio.cash / buy_price / (1 + COMMISSION) / 100) * 100
                if shares <= 0:
                    continue
                cost = buy_price * shares * (1 + COMMISSION)

            portfolio.cash -= cost
            portfolio.positions.append(Position(
                code=code,
                buy_date=str(next_date)[:10],
                buy_price=buy_price,
                shares=shares,
                cost=cost,
                stop_loss=buy_price * (1 - STOP_LOSS_PCT),
            ))

        # 进度报告
        if (day_idx + 1) % 50 == 0:
            print(f"  第{day_idx+1}/{len(trade_dates)}天 | "
                  f"持仓{portfolio.position_count}只 | "
                  f"净值{nav:,.0f} | "
                  f"累计交易{len(portfolio.trades)}笔")

    # ──── 回测结束，统计结果 ────
    elapsed = time.time() - t_start

    # 处理未平仓持仓（以最后一天收盘价标记）
    last_date = trade_dates[-1]
    last_date_ts = pd.to_datetime(str(last_date)[:10])
    for pos in portfolio.positions:
        if pos.code in stock_data:
            df = stock_data[pos.code]
            if last_date_ts in df.index:
                cur_close = df.loc[last_date_ts, "close"]
                sell_price = cur_close
                proceeds = sell_price * pos.shares
                pnl = proceeds - pos.cost
                pnl_pct = (sell_price / pos.buy_price - 1) * 100
                hold_days = (last_date_ts - pd.to_datetime(pos.buy_date)).days
                portfolio.trades.append({
                    "code": pos.code,
                    "buy_date": pos.buy_date,
                    "sell_date": str(last_date)[:10],
                    "buy_price": pos.buy_price,
                    "sell_price": sell_price,
                    "shares": pos.shares,
                    "cost": pos.cost,
                    "proceeds": proceeds,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "hold_days": hold_days,
                    "sell_reason": "持有至今",
                })

    print_results(portfolio, initial_capital, start_date, end_date,
                  regime_filtered_count, total_signals, len(trade_dates), elapsed)

    return portfolio


# ─── 结果输出 ─────────────────────────────────────────────────

def print_results(portfolio: Portfolio, initial_capital: float,
                  start_date: str, end_date: str,
                  regime_filtered: int, total_signals: int,
                  total_days: int, elapsed: float):
    """打印回测结果统计"""

    trades_df = pd.DataFrame(portfolio.trades) if portfolio.trades else pd.DataFrame()

    # 计算期末净值
    nav_df = pd.DataFrame(portfolio.daily_nav)
    final_nav = nav_df["nav"].iloc[-1] if not nav_df.empty else initial_capital
    total_return = (final_nav / initial_capital - 1) * 100
    years = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days / 365.25
    annual_return = ((final_nav / initial_capital) ** (1/years) - 1) * 100 if years > 0 else 0

    # 最大回撤
    if not nav_df.empty:
        nav_arr = nav_df["nav"].values
        peak = np.maximum.accumulate(nav_arr)
        drawdown = (nav_arr - peak) / peak * 100
        max_drawdown = drawdown.min()
    else:
        max_drawdown = 0

    print(f"\n{'='*60}")
    print(f"  动态选股回测结果（{start_date} ~ {end_date}）")
    print(f"{'='*60}")
    print(f"\n  初始资金：{initial_capital:,.0f}")
    print(f"  期末净值：{final_nav:,.0f}")
    print(f"  总收益率：{total_return:+.2f}%")
    print(f"  年化收益：{annual_return:+.2f}%")
    print(f"  最大回撤：{max_drawdown:.2f}%")
    print(f"  运行耗时：{elapsed:.1f}秒")

    if trades_df.empty:
        print("\n  无交易记录")
        return

    total = len(trades_df)
    wins = trades_df[trades_df["pnl_pct"] > 0]
    losses = trades_df[trades_df["pnl_pct"] <= 0]
    win_rate = len(wins) / total * 100
    avg_pnl = trades_df["pnl_pct"].mean()
    median_pnl = trades_df["pnl_pct"].median()
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    avg_hold = trades_df["hold_days"].mean()

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
    trades_df["bin"] = pd.cut(trades_df["pnl_pct"], bins=bins, labels=labels)
    dist = trades_df["bin"].value_counts().sort_index()
    for label, count in dist.items():
        pct = count / total * 100
        bar = "#" * int(pct / 2)
        print(f"    {label:>8s}: {count:>4d} ({pct:>5.1f}%) {bar}")

    # 卖出原因分布
    print(f"\n  卖出原因分布：")
    for reason, group in trades_df.groupby("sell_reason"):
        cnt = len(group)
        avg = group["pnl_pct"].mean()
        wr = (group["pnl_pct"] > 0).mean() * 100
        print(f"    {reason}: {cnt}笔 胜率{wr:.0f}% 平均{avg:+.2f}%")

    # 月度收益
    print(f"\n  月度收益：")
    if not nav_df.empty:
        nav_df["date"] = pd.to_datetime(nav_df["date"])
        nav_df["month"] = nav_df["date"].dt.to_period("M")
        # 计算每月首末净值差
        monthly = nav_df.groupby("month").agg(
            start_nav=("nav", "first"),
            end_nav=("nav", "last")
        )
        monthly["return_pct"] = (monthly["end_nav"] / monthly["start_nav"] - 1) * 100
        # 月度交易笔数
        trades_df["month"] = pd.to_datetime(trades_df["buy_date"]).dt.to_period("M")
        monthly_trades = trades_df.groupby("month").size()

        for month, row in monthly.iterrows():
            n_trades = monthly_trades.get(month, 0)
            print(f"    {month}: {row['return_pct']:+.2f}%  ({n_trades}笔)")

    # Market Regime 效果
    print(f"\n  Market Regime 过滤效果：")
    print(f"    熊市跳过天数：{regime_filtered}/{total_days} ({regime_filtered/total_days*100:.0f}%)")
    print(f"    触发买入信号总数：{total_signals}")

    # 最大同时持仓统计（从daily_nav推算）
    # 简单统计用交易记录
    if not trades_df.empty:
        max_concurrent = 0
        for d in nav_df["date"].dt.strftime("%Y-%m-%d"):
            active = trades_df[(trades_df["buy_date"] <= d) & (trades_df["sell_date"] >= d)]
            if len(active) > max_concurrent:
                max_concurrent = len(active)
        print(f"    最大同时持仓：{max_concurrent}只")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全市场动态选股回测")
    parser.add_argument("--start", default="2025-06-01", help="回测开始日期")
    parser.add_argument("--end", default="2026-06-26", help="回测结束日期")
    parser.add_argument("--capital", type=float, default=1000000, help="初始资金")
    parser.add_argument("--export", default="backtest_trades.csv", help="导出交易记录到CSV")
    args = parser.parse_args()

    portfolio = run_backtest(args.start, args.end, args.capital)

    if portfolio and portfolio.trades:
        trades_df = pd.DataFrame(portfolio.trades)
        trades_df.to_csv(args.export, index=False)
        print(f"\n交易记录已导出到: {args.export}")
