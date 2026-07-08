"""
市场状态判断脚本 (Market Regime)

四维评分体系：
1. 指数趋势 (35%)：深证成指/创业板指 MACD + 均线
2. 市场宽度 (30%)：全市场涨跌家数比、涨停/跌停比
3. 量能趋势 (20%)：指数成交额 vs 5日/20日均量
4. 风险偏好 (15%)：创业板 vs 深证成指相对强弱

输出状态分类：
  80-100: STRONG_BULL（强势上升）— 仓位上限90%
  65-80:  BULL（上升趋势）     — 仓位上限80%
  45-65:  CONSOLIDATION（震荡）— 仓位上限60%
  30-45:  BEAR（下跌趋势）     — 仓位上限40%
  0-30:   STRONG_BEAR（强势下跌）— 仓位上限20%
  特殊:   RECOVERY（反弹）     — 仓位上限70%

用法：
  python3 market_regime.py [--date YYYY-MM-DD] [--json]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"

# 指数代码 → 中文名（集中映射，替代散落的行内注释）
INDEX_SZCI = "sz.399001"   # 深证成指
INDEX_GEM = "sz.399006"    # 创业板指（正确代码 399006，数据自 2010 年起）
INDEX_HS300 = "sh.000300"  # 沪深300（大盘蓝筹，上证官方真身，数据自 2005 年）
INDEX_ZZ500 = "sh.000905"  # 中证500（中盘，上证官方真身，数据自 2007 年）
INDEX_NAMES = {
    INDEX_SZCI: "深证成指",
    INDEX_GEM: "创业板指",
    INDEX_HS300: "沪深300",
    INDEX_ZZ500: "中证500",
}

REGIME_MAP = {
    "STRONG_BULL": {"cn": "强势上升", "max_pos": 90},
    "BULL": {"cn": "上升趋势", "max_pos": 80},
    "CONSOLIDATION": {"cn": "震荡", "max_pos": 60},
    "BEAR": {"cn": "下跌趋势", "max_pos": 40},
    "STRONG_BEAR": {"cn": "强势下跌", "max_pos": 20},
    "RECOVERY": {"cn": "反弹", "max_pos": 70},
}


def load_index_data(con, code: str, end_date: str, days: int = 60) -> pd.DataFrame:
    """加载指数近N天数据"""
    df = con.execute("""
        SELECT date, open, high, low, close, volume, amount
        FROM index_daily_price
        WHERE code = ? AND date <= ?
        ORDER BY date DESC
        LIMIT ?
    """, [code, end_date, days]).fetchdf()

    if df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)
    return df


def calc_macd(close: np.ndarray):
    """计算MACD指标"""
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().values
    return dif, dea


def calc_ma(close: np.ndarray, period: int):
    """计算均线"""
    return pd.Series(close).rolling(period).mean().values


def score_index_trend(con, end_date: str) -> dict:
    """
    维度一：指数趋势评分 (0-100)
    综合深证成指、创业板指、沪深300 的 MACD+均线状态
    （纳入沪深300 代表大盘蓝筹，趋势不再只看深市成长股）
    """
    scores = []
    actual_date = None  # 记录实际取到的最新数据日期（用于数据日期校验）

    for code in [INDEX_SZCI, INDEX_GEM, INDEX_HS300]:
        df = load_index_data(con, code, end_date, 60)
        if len(df) < 30:
            continue

        # 各指数最新日期应一致，取其一即可
        if actual_date is None:
            actual_date = str(df["date"].iloc[-1])[:10]

        close = df["close"].values
        dif, dea = calc_macd(close)
        ma5 = calc_ma(close, 5)
        ma10 = calc_ma(close, 10)
        ma20 = calc_ma(close, 20)

        score = 50  # 基准分
        latest = -1

        # MACD方向 (+/- 20分)
        if dif[latest] > 0:
            score += 15
            if dif[latest] > dea[latest]:
                score += 5  # DIF在零轴上方且金叉
        else:
            score -= 15
            if dif[latest] < dea[latest]:
                score -= 5  # DIF在零轴下方且死叉

        # MACD动量变化 (+/- 10分)
        macd_bar = dif[latest] - dea[latest]
        macd_bar_prev = dif[latest-1] - dea[latest-1]
        if macd_bar > macd_bar_prev:
            score += 10  # 红柱变长或绿柱缩短
        else:
            score -= 5

        # 均线排列 (+/- 15分)
        if ma5[latest] > ma10[latest] > ma20[latest]:
            score += 15  # 多头排列
        elif ma5[latest] < ma10[latest] < ma20[latest]:
            score -= 15  # 空头排列
        elif close[latest] > ma10[latest]:
            score += 5

        # 股价vs均线位置 (+/- 5分)
        if close[latest] > ma5[latest]:
            score += 5
        elif close[latest] < ma20[latest]:
            score -= 5

        scores.append(max(0, min(100, score)))

    if not scores:
        return {"score": 50, "detail": "数据不足", "actual_date": actual_date}

    final_score = int(np.mean(scores))

    detail_parts = []
    if final_score >= 65:
        detail_parts.append("指数趋势向上")
    elif final_score <= 35:
        detail_parts.append("指数趋势向下")
    else:
        detail_parts.append("指数趋势中性")

    return {"score": final_score, "detail": "，".join(detail_parts), "actual_date": actual_date}


def score_market_breadth(con, end_date: str) -> dict:
    """
    维度二：市场宽度评分 (0-100)
    涨跌家数比 + 涨停/跌停比
    """
    # 获取最近5天的涨跌数据
    breadth_df = con.execute("""
        SELECT date,
            SUM(CASE WHEN close > open THEN 1 ELSE 0 END) as up_count,
            SUM(CASE WHEN close < open THEN 1 ELSE 0 END) as down_count,
            SUM(CASE WHEN (close / NULLIF(open, 0) - 1) >= 0.095 THEN 1 ELSE 0 END) as limit_up,
            SUM(CASE WHEN (1 - close / NULLIF(open, 0)) >= 0.095 THEN 1 ELSE 0 END) as limit_down,
            COUNT(*) as total
        FROM daily_price_qfq
        WHERE date <= ? AND date >= ?
          AND code NOT LIKE 'sh.000%'
          AND code NOT LIKE 'sz.399%'
          AND code NOT LIKE 'sh.880%'
        GROUP BY date
        ORDER BY date DESC
        LIMIT 5
    """, [end_date, (pd.to_datetime(end_date) - timedelta(days=15)).strftime("%Y-%m-%d")]).fetchdf()

    if breadth_df.empty:
        return {"score": 50, "detail": "数据不足", "actual_date": None}

    # 当天数据
    today = breadth_df.iloc[0]
    actual_date = str(today["date"])[:10]  # 实际取到的最新交易日（个股表基准）
    up = today["up_count"]
    down = today["down_count"]
    limit_up = today["limit_up"]
    limit_down = today["limit_down"]
    total = today["total"]

    score = 50

    # 涨跌家数比 (+/- 25分)
    if down > 0:
        ratio = up / down
    else:
        ratio = 10
    if ratio >= 4:
        score += 25
    elif ratio >= 2:
        score += 15
    elif ratio >= 1:
        score += 5
    elif ratio >= 0.5:
        score -= 15
    else:
        score -= 25

    # 涨停/跌停比 (+/- 15分)
    if limit_down > 0:
        ld_ratio = limit_up / limit_down
    else:
        ld_ratio = limit_up if limit_up > 0 else 1
    if ld_ratio >= 5:
        score += 15
    elif ld_ratio >= 2:
        score += 8
    elif ld_ratio >= 1:
        score += 3
    elif ld_ratio >= 0.5:
        score -= 8
    else:
        score -= 15

    # 连续性：近3日涨跌家数趋势 (+/- 10分)
    if len(breadth_df) >= 3:
        recent_ratios = []
        for _, row in breadth_df.iloc[:3].iterrows():
            d = row["down_count"]
            r = row["up_count"] / d if d > 0 else 5
            recent_ratios.append(r)
        # 连续改善
        if recent_ratios[0] > recent_ratios[1] > recent_ratios[2]:
            score += 10
        elif recent_ratios[0] < recent_ratios[1] < recent_ratios[2]:
            score -= 10

    score = max(0, min(100, score))

    detail = f"涨{int(up)}/跌{int(down)}(比{ratio:.1f}:1)，涨停{int(limit_up)}/跌停{int(limit_down)}"
    return {"score": score, "detail": detail, "up": int(up), "down": int(down),
            "limit_up": int(limit_up), "limit_down": int(limit_down),
            "actual_date": actual_date,
            "breadth_improving": len(breadth_df) >= 3 and recent_ratios[0] > recent_ratios[1] > recent_ratios[2]}


def score_volume_trend(con, end_date: str) -> dict:
    """
    维度三：量能趋势评分 (0-100)
    沪深两市成交额（深证成指 + 沪深300 按日相加）vs 5日/20日均量。
    纳入沪深300 量能，避免只看深市量能在“沪强深弱”时失真；
    量价配合仍以深证成指价格为准（口径单一）。
    """
    df = load_index_data(con, INDEX_SZCI, end_date, 25)
    hs300 = load_index_data(con, INDEX_HS300, end_date, 25)
    if len(df) < 20:
        return {"score": 50, "detail": "数据不足", "actual_date": None}

    actual_date = str(df["date"].iloc[-1])[:10]  # 深证成指最新日期

    # 合并沪深两市量能：以深证成指的交易日为基准，按日期左连接沪深300 成交额后相加。
    # 沪深300 缺某日则该日只计深证成指量能（不因缺一市而漏判）。
    merged = df[["date", "amount", "close"]].merge(
        hs300[["date", "amount"]].rename(columns={"amount": "amount_hs300"}),
        on="date", how="left",
    )
    merged["amount_hs300"] = merged["amount_hs300"].fillna(0)
    combined_amount = (merged["amount"] + merged["amount_hs300"]).values

    amounts = combined_amount
    latest_amount = amounts[-1]
    ma5_amount = np.mean(amounts[-5:])
    ma20_amount = np.mean(amounts[-20:])

    score = 50

    # 当日量 vs 5日均量 (+/- 20分)
    vol_ratio_5 = latest_amount / ma5_amount if ma5_amount > 0 else 1
    if vol_ratio_5 >= 1.3:
        score += 20  # 明显放量
    elif vol_ratio_5 >= 1.1:
        score += 10
    elif vol_ratio_5 <= 0.7:
        score -= 20  # 明显缩量
    elif vol_ratio_5 <= 0.9:
        score -= 10

    # 5日均量 vs 20日均量 (+/- 15分) — 量能趋势
    vol_ratio_20 = ma5_amount / ma20_amount if ma20_amount > 0 else 1
    if vol_ratio_20 >= 1.2:
        score += 15  # 量能趋势性放大
    elif vol_ratio_20 >= 1.05:
        score += 5
    elif vol_ratio_20 <= 0.8:
        score -= 15  # 量能趋势性萎缩
    elif vol_ratio_20 <= 0.95:
        score -= 5

    # 量价配合 (+/- 15分)
    price_chg = (df["close"].values[-1] / df["close"].values[-2] - 1) if len(df) >= 2 else 0
    if price_chg > 0 and vol_ratio_5 > 1:
        score += 15  # 放量上涨
    elif price_chg < 0 and vol_ratio_5 > 1.2:
        score -= 15  # 放量下跌
    elif price_chg > 0 and vol_ratio_5 < 0.9:
        score -= 5   # 缩量上涨（动能不足）
    elif price_chg < 0 and vol_ratio_5 < 0.9:
        score += 5   # 缩量下跌（抛压减弱）

    score = max(0, min(100, score))
    detail = f"量比(vs5日){vol_ratio_5:.2f}，量能趋势(5/20){vol_ratio_20:.2f}"
    return {"score": score, "detail": detail, "actual_date": actual_date}


def score_risk_appetite(con, end_date: str) -> dict:
    """
    维度四：风险偏好评分 (0-100)
    创业板 vs 沪深300 的相对强弱（成长 vs 价值）。
    改用沪深300 而非深证成指做基准：创业板与深证成指相关性高达 0.96（同涨跌，
    信号弱），与沪深300 相关性 0.91、分化更明显，才是有效的“成长强于价值→
    风险偏好上升”信号。
    """
    gem_df = load_index_data(con, INDEX_GEM, end_date, 25)
    base_df = load_index_data(con, INDEX_HS300, end_date, 25)

    if len(gem_df) < 20 or len(base_df) < 20:
        return {"score": 50, "detail": "数据不足", "actual_date": None}

    actual_date = str(gem_df["date"].iloc[-1])[:10]  # 创业板指最新日期

    # 近5日相对强弱
    gem_5d_chg = gem_df["close"].values[-1] / gem_df["close"].values[-5] - 1
    base_5d_chg = base_df["close"].values[-1] / base_df["close"].values[-5] - 1
    relative_5d = gem_5d_chg - base_5d_chg

    # 近20日相对强弱
    gem_20d_chg = gem_df["close"].values[-1] / gem_df["close"].values[-20] - 1
    base_20d_chg = base_df["close"].values[-1] / base_df["close"].values[-20] - 1
    relative_20d = gem_20d_chg - base_20d_chg

    score = 50

    # 短期相对强弱 (+/- 20分)
    if relative_5d > 0.02:
        score += 20  # 创业板明显强于沪深300，风险偏好高
    elif relative_5d > 0.005:
        score += 10
    elif relative_5d < -0.02:
        score -= 20  # 创业板明显弱于沪深300，风险偏好低
    elif relative_5d < -0.005:
        score -= 10

    # 中期相对强弱 (+/- 15分)
    if relative_20d > 0.03:
        score += 15
    elif relative_20d > 0.01:
        score += 5
    elif relative_20d < -0.03:
        score -= 15
    elif relative_20d < -0.01:
        score -= 5

    score = max(0, min(100, score))
    detail = f"创业板vs沪深300：5日{relative_5d*100:+.1f}%，20日{relative_20d*100:+.1f}%"
    return {"score": score, "detail": detail, "actual_date": actual_date}


def determine_regime(total_score: int, breadth_result: dict) -> str:
    """综合评分确定市场状态"""
    # 特殊状态：RECOVERY（市场宽度连续3日改善 + 当前分数30-50）
    if 30 <= total_score <= 55 and breadth_result.get("breadth_improving"):
        return "RECOVERY"

    if total_score >= 80:
        return "STRONG_BULL"
    elif total_score >= 65:
        return "BULL"
    elif total_score >= 45:
        return "CONSOLIDATION"
    elif total_score >= 30:
        return "BEAR"
    else:
        return "STRONG_BEAR"


def run_regime(date_str: str = None, output_json: bool = False):
    """主入口"""
    con = duckdb.connect(DB_PATH, read_only=True)

    # 确定日期：默认取个股表与指数表两者最新日期的较小值，
    # 确保默认跑时两表都有数据，从源头避免维度间日期不一致。
    if date_str is None:
        stock_max = con.execute("SELECT MAX(date) FROM daily_price_qfq").fetchone()[0]
        index_max = con.execute("SELECT MAX(date) FROM index_daily_price").fetchone()[0]
        max_date = min(stock_max, index_max)
        date_str = str(max_date)[:10]

    # 四维评分
    trend = score_index_trend(con, date_str)
    breadth = score_market_breadth(con, date_str)
    volume = score_volume_trend(con, date_str)
    risk = score_risk_appetite(con, date_str)

    con.close()

    # ── 数据日期校验（防止请求日无数据时静默用旧行情冒充）──
    # 基准日：以市场宽度（个股表）实际日期为准，代表全市场当天；无则取任一非空维度
    breadth_date = breadth.get("actual_date")
    index_date = trend.get("actual_date")
    dim_dates = {
        "市场宽度": breadth_date,
        "指数趋势": index_date,
        "量能": volume.get("actual_date"),
        "风险偏好": risk.get("actual_date"),
    }
    non_null_dates = [d for d in dim_dates.values() if d]
    data_date = breadth_date or (non_null_dates[0] if non_null_dates else date_str)

    warnings = []
    # ① 请求日无数据，回退到更早日
    if data_date != date_str:
        warnings.append(f"⚠️ 请求日期 {date_str} 无数据，实际基于 {data_date} 的行情计算")
    # ② 维度间数据日期不一致（个股表与指数表未同步）
    if breadth_date and index_date and breadth_date != index_date:
        warnings.append(
            f"⚠️ 维度间数据日期不一致：市场宽度={breadth_date}，指数趋势={index_date}"
            f"（个股表与指数表可能未同步）"
        )
    data_warning = "；".join(warnings) if warnings else None

    # 加权综合
    total_score = int(
        trend["score"] * 0.35 +
        breadth["score"] * 0.30 +
        volume["score"] * 0.20 +
        risk["score"] * 0.15
    )

    regime = determine_regime(total_score, breadth)
    regime_info = REGIME_MAP[regime]

    # 生成建议
    if regime in ("STRONG_BEAR", "BEAR"):
        suggestion = f"仓位上限{regime_info['max_pos']}%，不宜新建仓，等待企稳信号"
    elif regime == "RECOVERY":
        suggestion = "市场宽度连续改善，可试探性建仓，仓位上限70%"
    elif regime == "CONSOLIDATION":
        suggestion = f"仓位上限{regime_info['max_pos']}%，精选强势个股操作"
    elif regime == "BULL":
        suggestion = f"仓位上限{regime_info['max_pos']}%，可积极布局主线板块"
    else:
        suggestion = f"仓位上限{regime_info['max_pos']}%，全面做多但注意过热风险"

    result = {
        "date": data_date,                 # 反映真实数据日期（回退时即为实际行情日）
        "requested_date": date_str,        # 用户请求的日期
        "data_date": data_date,            # 实际用于计算的行情日期
        "data_warning": data_warning,      # 数据日期异常提醒（无异常为 None）
        "regime": regime,
        "regime_cn": regime_info["cn"],
        "score": total_score,
        "max_position_pct": regime_info["max_pos"],
        "dimensions": {
            "index_trend": {"score": trend["score"], "weight": "35%", "detail": trend["detail"], "actual_date": trend.get("actual_date")},
            "market_breadth": {"score": breadth["score"], "weight": "30%", "detail": breadth["detail"], "actual_date": breadth.get("actual_date")},
            "volume_trend": {"score": volume["score"], "weight": "20%", "detail": volume["detail"], "actual_date": volume.get("actual_date")},
            "risk_appetite": {"score": risk["score"], "weight": "15%", "detail": risk["detail"], "actual_date": risk.get("actual_date")},
        },
        "suggestion": suggestion,
    }

    if output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)

    return result


def print_report(r: dict):
    """打印文本格式报告"""
    print(f"\n{'='*50}")
    print(f"  市场状态报告 — {r['data_date']}")
    print(f"{'='*50}")
    # 数据日期异常时醒目提醒
    if r.get("data_warning"):
        print(f"\n  {'!'*40}")
        for line in r["data_warning"].split("；"):
            print(f"  {line}")
        print(f"  {'!'*40}")
    print(f"\n  综合评分：{r['score']} / 100")
    print(f"  市场状态：{r['regime']}（{r['regime_cn']}）")
    print(f"  仓位上限：{r['max_position_pct']}%")
    print(f"\n  {'─'*40}")
    print(f"  四维明细：")
    for key, dim in r["dimensions"].items():
        name_map = {
            "index_trend": "指数趋势",
            "market_breadth": "市场宽度",
            "volume_trend": "量能趋势",
            "risk_appetite": "风险偏好",
        }
        name = name_map.get(key, key)
        print(f"    {name}({dim['weight']}): {dim['score']}分 — {dim['detail']}")
    print(f"\n  建议：{r['suggestion']}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="市场状态判断 (Market Regime)")
    parser.add_argument("--date", default=None, help="评估日期 (YYYY-MM-DD)，默认使用数据库最新日期")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    args = parser.parse_args()

    run_regime(args.date, args.json)
