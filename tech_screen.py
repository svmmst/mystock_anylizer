"""
批量技术面快筛脚本

对接 real_stock 选股漏斗「第二层技术面快筛」。
从 daily_price_qfq 读取前复权数据，批量计算技术指标并按硬性门槛过滤。

用法：
  python3 tech_screen.py sh.600276,sz.300750,sh.688111
  python3 tech_screen.py --file candidates.txt
  echo "sh.600276" | python3 tech_screen.py --stdin
  python3 tech_screen.py sh.600276,sz.300750 --json --verbose
"""

import argparse
import json
import sys

import duckdb
import pandas as pd

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"
LOOKBACK_DAYS = 120


# ─── 板块识别 ───────────────────────────────────────────────

def identify_board(code: str) -> str:
    if code.startswith("bj."):
        return "北交所"
    numeric = code.split(".")[1]
    if numeric.startswith("688"):
        return "科创板"
    elif numeric.startswith("300") or numeric.startswith("301"):
        return "创业板"
    return "主板"


def kdj_threshold(board: str) -> int:
    # v2.1: 主板统一放宽至80，与创业板/科创板一致
    return 80


# ─── 代码格式转换 ───────────────────────────────────────────

def normalize_code(code: str) -> str:
    """支持 sh.600276 和 600276 两种输入格式，统一输出数据库格式"""
    code = code.strip()
    if "." in code:
        return code
    if code.startswith("6"):
        return f"sh.{code}"
    elif code.startswith("9") or code.startswith("4") or code.startswith("8"):
        return f"bj.{code}"
    return f"sz.{code}"


# ─── 数据加载 ───────────────────────────────────────────────

def get_latest_date(con) -> str:
    row = con.execute("SELECT MAX(date) FROM daily_price_qfq").fetchone()
    return str(row[0])


def load_kline(con, code: str, screen_date: str) -> pd.DataFrame:
    df = con.execute("""
        SELECT date, open, high, low, close, volume
        FROM daily_price_qfq
        WHERE code = ? AND date <= ?
        ORDER BY date
    """, [code, screen_date]).fetchdf()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df.tail(LOOKBACK_DAYS).reset_index(drop=True)


# ─── 技术指标计算 ───────────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    df["vol_ma5"] = volume.rolling(5).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2

    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, float("nan")) * 100
    df["k"] = rsv.ewm(com=2, adjust=False).mean()
    df["d"] = df["k"].ewm(com=2, adjust=False).mean()
    df["j"] = 3 * df["k"] - 2 * df["d"]

    diff = close.diff()
    gain = diff.clip(lower=0).rolling(14).mean()
    loss = (-diff.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

    df["boll_mid"] = close.rolling(20).mean()
    boll_std = close.rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * boll_std
    df["boll_lower"] = df["boll_mid"] - 2 * boll_std

    return df


# ─── 底背离检测 ─────────────────────────────────────────────

def check_macd_divergence(df: pd.DataFrame, idx: int) -> bool:
    window = df.iloc[max(0, idx - 20):idx]
    if len(window) < 5:
        return False
    cur_close = df.iloc[idx]["close"]
    cur_dif = df.iloc[idx]["dif"]
    lower_price_rows = window[window["close"] < cur_close]
    if lower_price_rows.empty:
        return False
    return (lower_price_rows["dif"] < cur_dif).any()


# ─── 趋势启动日检测 ────────────────────────────────────────

def check_trend_initiation(df: pd.DataFrame, idx: int) -> tuple:
    """
    检测是否为趋势启动日。需同时满足：
    1. MACD当日金叉（DIF上穿DEA），或DIF当日由负转正
    2. 股价站上MA10
    3. 当日成交量 > 5日均量的1.5倍
    返回 (is_initiation: bool, reason: str)
    注：第4项（主力资金净流入）需通过问财API外部验证，本地无法计算
    """
    if idx < 1:
        return False, ""
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    # 条件1: MACD金叉或DIF由负转正
    macd_signal = False
    macd_reason = ""
    golden_cross = (row["dif"] > row["dea"]) and (prev["dif"] <= prev["dea"])
    dif_turn_positive = (row["dif"] > 0) and (prev["dif"] <= 0)
    if golden_cross:
        macd_signal = True
        macd_reason = "当日MACD金叉"
    elif dif_turn_positive:
        macd_signal = True
        macd_reason = "DIF当日由负转正"

    # 条件2: 股价站上MA10
    above_ma10 = row["close"] > row["ma10"]

    # 条件3: 放量（>5日均量1.5倍）
    vol_boost = row["volume"] > row["vol_ma5"] * 1.5 if row["vol_ma5"] > 0 else False

    if macd_signal and above_ma10 and vol_boost:
        return True, f"趋势启动日({macd_reason}，放量{row['volume']/row['vol_ma5']:.1f}倍)"
    return False, ""


# ─── 硬性门槛检查 ───────────────────────────────────────────

def check_thresholds(df: pd.DataFrame, idx: int, board: str) -> dict:
    row = df.iloc[idx]
    results = {}

    # 检测趋势启动日
    is_initiation, initiation_reason = check_trend_initiation(df, idx)

    # 1. MACD
    dif_above_zero = row["dif"] > 0
    golden_cross = (row["dif"] > row["dea"]) and (
        idx > 0 and df.iloc[idx - 1]["dif"] <= df.iloc[idx - 1]["dea"]
    )
    divergence = check_macd_divergence(df, idx)
    zero_cross_ok = (row["dif"] < 0) and golden_cross and divergence

    macd_data = {"dif": round(row["dif"], 3), "dea": round(row["dea"], 3),
                 "macd_bar": round(row["macd"], 3)}
    if dif_above_zero:
        results["macd"] = (True, "DIF在零轴上方", macd_data)
    elif zero_cross_ok:
        results["macd"] = (True, "零轴下方金叉+底背离", macd_data)
    elif is_initiation:
        results["macd"] = (True, f"豁免({initiation_reason})", macd_data)
    else:
        pos = "零轴上方" if row["dif"] > 0 else "零轴下方"
        cross = "金叉" if row["dif"] > row["dea"] else "死叉"
        results["macd"] = (False, f"DIF={row['dif']:.3f}({pos})，{cross}，无底背离", macd_data)

    # 2. 均线
    above_ma10 = row["close"] > row["ma10"]
    ma5_above_ma10 = row["ma5"] > row["ma10"]
    bull_arrange = (row["ma5"] > row["ma10"]) and (row["ma10"] > row["ma20"])

    ma_data = {"close": round(row["close"], 2), "ma5": round(row["ma5"], 2),
               "ma10": round(row["ma10"], 2), "ma20": round(row["ma20"], 2)}
    if above_ma10 and (ma5_above_ma10 or bull_arrange):
        arrange = "多头排列" if bull_arrange else "MA5上穿MA10"
        results["ma"] = (True, f"股价{row['close']:.2f}>MA10={row['ma10']:.2f}，{arrange}", ma_data)
    elif is_initiation and above_ma10:
        results["ma"] = (True, f"豁免({initiation_reason})，股价>MA10", ma_data)
    else:
        issues = []
        if not above_ma10:
            issues.append(f"股价{row['close']:.2f}<MA10={row['ma10']:.2f}")
        if not ma5_above_ma10:
            issues.append(f"MA5={row['ma5']:.2f}<MA10={row['ma10']:.2f}")
        results["ma"] = (False, "，".join(issues), ma_data)

    # 3. 趋势
    recent5 = df.iloc[max(0, idx - 4):idx + 1]["close"]
    all_down_5 = all(recent5.diff().dropna() < 0)
    close_20ago = df.iloc[idx - 20]["close"] if idx >= 20 else None
    drop_20d = (row["close"] / close_20ago - 1) * 100 if close_20ago else 0

    trend_data = {"drop_20d": round(drop_20d, 1), "all_down_5": all_down_5}
    trend_ok = (not all_down_5) and (drop_20d > -15)
    if trend_ok:
        results["trend"] = (True, f"近5日非全跌，20日涨跌={drop_20d:+.1f}%", trend_data)
    else:
        issues = []
        if all_down_5:
            issues.append("近5日连续下跌")
        if drop_20d <= -15:
            issues.append(f"近20日跌幅{drop_20d:.1f}%")
        results["trend"] = (False, "，".join(issues), trend_data)

    # 4. KDJ
    k_val = row["k"]
    threshold = kdj_threshold(board)
    kdj_data = {"k": round(k_val, 1), "d": round(row["d"], 1),
                "j": round(row["j"], 1), "threshold": threshold}
    if k_val < threshold:
        results["kdj"] = (True, f"K={k_val:.1f}<{threshold}({board})", kdj_data)
    else:
        results["kdj"] = (False, f"K={k_val:.1f}>={threshold}({board}) — 超买区", kdj_data)

    return results


# ─── 辅助指标 ───────────────────────────────────────────────

def calc_extra_indicators(df: pd.DataFrame, idx: int) -> dict:
    row = df.iloc[idx]
    close = row["close"]

    # 偏离MA5百分比
    ma5_dev = (close / row["ma5"] - 1) * 100 if row["ma5"] > 0 else 0

    # 量比
    vol_ratio = row["volume"] / row["vol_ma5"] if row["vol_ma5"] > 0 else 0

    # 近5日涨跌幅
    if idx >= 5:
        change_5d = (close / df.iloc[idx - 5]["close"] - 1) * 100
    else:
        change_5d = 0

    # 布林带位置
    if pd.notna(row["boll_upper"]) and pd.notna(row["boll_lower"]):
        boll_range = row["boll_upper"] - row["boll_lower"]
        if boll_range > 0:
            boll_pct = (close - row["boll_lower"]) / boll_range
            if boll_pct > 0.8:
                boll_pos = "接近上轨"
            elif boll_pct > 0.6:
                boll_pos = "中轨~上轨"
            elif boll_pct > 0.4:
                boll_pos = "中轨附近"
            elif boll_pct > 0.2:
                boll_pos = "中轨~下轨"
            else:
                boll_pos = "接近下轨"
        else:
            boll_pos = "布林收窄"
    else:
        boll_pos = "数据不足"

    # 均线排列
    ma5, ma10, ma20, ma60 = row["ma5"], row["ma10"], row["ma20"], row.get("ma60", None)
    if pd.notna(ma60) and ma5 > ma10 > ma20 > ma60:
        ma_arr = "完全多头排列"
    elif ma5 > ma10 > ma20:
        ma_arr = "多头排列(MA5>10>20)"
    elif pd.notna(ma60) and ma5 < ma10 < ma20 < ma60:
        ma_arr = "完全空头排列"
    elif ma5 < ma10 < ma20:
        ma_arr = "空头排列(MA5<10<20)"
    else:
        ma_arr = "交叉/震荡"

    # 全空头排列红旗
    ma_all_bearish = False
    if pd.notna(ma60):
        ma_all_bearish = close < ma5 < ma10 < ma20 < ma60

    # 连续下跌天数
    consec_down = 0
    for i in range(idx, 0, -1):
        if df.iloc[i]["close"] < df.iloc[i - 1]["close"]:
            consec_down += 1
        else:
            break

    return {
        "close": round(close, 2),
        "ma5_deviation_pct": round(ma5_dev, 1),
        "vol_ratio_5d": round(vol_ratio, 2),
        "recent_5d_change_pct": round(change_5d, 1),
        "rsi14": round(row["rsi14"], 1) if pd.notna(row["rsi14"]) else None,
        "boll_position": boll_pos,
        "ma_arrangement": ma_arr,
        "ma_all_bearish": ma_all_bearish,
        "consecutive_down_days": consec_down,
        "ma60": round(ma60, 2) if pd.notna(ma60) else None,
    }


# ─── 单只股票筛选 ───────────────────────────────────────────

def screen_single(code: str, df: pd.DataFrame) -> dict:
    if len(df) < 30:
        return {"code": code, "error": "数据不足(不足30日)"}

    board = identify_board(code)
    df = calc_indicators(df)
    idx = len(df) - 1

    thresholds = check_thresholds(df, idx, board)
    passed = all(t[0] for t in thresholds.values())
    failed_items = [k for k, v in thresholds.items() if not v[0]]

    result = {
        "code": code,
        "board": board,
        "passed": passed,
        "failed_items": failed_items,
        "thresholds": {
            k: {"passed": v[0], "reason": v[1], **v[2]}
            for k, v in thresholds.items()
        },
    }

    extra = calc_extra_indicators(df, idx)
    result["extra"] = extra

    return result


# ─── 批量筛选 ───────────────────────────────────────────────

def batch_screen(codes: list, screen_date: str = None) -> dict:
    con = duckdb.connect(DB_PATH, read_only=True)

    if not screen_date:
        screen_date = get_latest_date(con)

    # 北交所不在交易范围内，自动跳过
    codes = [c for c in codes if not c.startswith("bj.")]

    results = []
    for code in codes:
        df = load_kline(con, code, screen_date)
        result = screen_single(code, df)
        results.append(result)

    con.close()

    passed_codes = [r["code"] for r in results if r.get("passed")]
    failed_codes = [r["code"] for r in results if not r.get("passed") and "error" not in r]
    error_codes = [r["code"] for r in results if "error" in r]

    return {
        "screen_date": screen_date,
        "input_count": len(codes),
        "passed_count": len(passed_codes),
        "results": results,
        "passed_codes": passed_codes,
        "failed_codes": failed_codes,
        "error_codes": error_codes,
    }


# ─── 输出格式化 ─────────────────────────────────────────────

def format_text_output(report: dict, verbose: bool = False) -> str:
    lines = []
    lines.append("=" * 50)
    lines.append(f"技术面快筛报告")
    lines.append(f"筛选日期: {report['screen_date']}")
    lines.append(f"输入股票: {report['input_count']} 只")
    lines.append(f"通过筛选: {report['passed_count']} 只")
    lines.append("=" * 50)
    lines.append("")

    passed_results = [r for r in report["results"] if r.get("passed")]
    failed_results = [r for r in report["results"] if not r.get("passed") and "error" not in r]
    error_results = [r for r in report["results"] if "error" in r]

    for r in passed_results:
        lines.append(f"✅ {r['code']} [{r['board']}]")
        for key in ["macd", "ma", "trend", "kdj"]:
            t = r["thresholds"][key]
            label = key.upper().ljust(5)
            mark = "✅" if t["passed"] else "❌"
            lines.append(f"  {label}: {mark} {t['reason']}")
        if verbose and "extra" in r:
            ex = r["extra"]
            lines.append(f"  ---辅助指标---")
            lines.append(f"  收盘: {ex['close']} | 偏离MA5: {ex['ma5_deviation_pct']:+.1f}% | 量比(5日): {ex['vol_ratio_5d']:.0%}")
            lines.append(f"  RSI14: {ex['rsi14']} | 布林: {ex['boll_position']} | 均线: {ex['ma_arrangement']}")
            lines.append(f"  近5日涨跌: {ex['recent_5d_change_pct']:+.1f}% | 连跌天数: {ex['consecutive_down_days']}")
            if ex["ma_all_bearish"]:
                lines.append(f"  ⚠️ 红旗: 均线全面空头排列")
            if abs(ex["ma5_deviation_pct"]) > 5:
                lines.append(f"  ⚠️ 红旗: 偏离MA5超过5%({ex['ma5_deviation_pct']:+.1f}%)")
        lines.append("")

    if failed_results:
        lines.append("-" * 50)
        lines.append("")

    for r in failed_results:
        failed_str = "、".join(r["failed_items"])
        lines.append(f"❌ {r['code']} [{r['board']}] — 未通过: {failed_str}")
        for key in ["macd", "ma", "trend", "kdj"]:
            t = r["thresholds"][key]
            label = key.upper().ljust(5)
            mark = "✅" if t["passed"] else "❌"
            lines.append(f"  {label}: {mark} {t['reason']}")
        lines.append("")

    for r in error_results:
        lines.append(f"⚠️  {r['code']} — {r['error']}")
        lines.append("")

    lines.append("=" * 50)
    if report["passed_codes"]:
        lines.append(f"✅ 通过: {', '.join(report['passed_codes'])}")
    if report["failed_codes"]:
        lines.append(f"❌ 未通过: {', '.join(report['failed_codes'])}")
    if report["error_codes"]:
        lines.append(f"⚠️  异常: {', '.join(report['error_codes'])}")
    lines.append("=" * 50)

    return "\n".join(lines)


def format_json_output(report: dict) -> str:
    def default_handler(obj):
        import numpy as np
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    return json.dumps(report, ensure_ascii=False, indent=2, default=default_handler)


def format_quiet_output(report: dict) -> str:
    return ",".join(report["passed_codes"])


# ─── CLI ────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="批量技术面快筛")
    parser.add_argument("codes", nargs="?", help="逗号分隔的股票代码")
    parser.add_argument("--file", help="从文件读取代码(每行一个)")
    parser.add_argument("--stdin", action="store_true", help="从stdin读取")
    parser.add_argument("--date", help="筛选日期(默认数据库最新日期)")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    parser.add_argument("--verbose", action="store_true", help="输出辅助指标")
    parser.add_argument("--quiet", action="store_true", help="仅输出通过的代码")
    return parser.parse_args()


def resolve_codes(args) -> list:
    if args.stdin:
        raw = sys.stdin.read().strip()
    elif args.file:
        with open(args.file) as f:
            raw = f.read().strip()
    elif args.codes:
        raw = args.codes
    else:
        print("错误: 请提供股票代码（位置参数、--file 或 --stdin）", file=sys.stderr)
        sys.exit(2)

    codes = []
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part:
            codes.append(normalize_code(part))
    if not codes:
        print("错误: 未解析到有效股票代码", file=sys.stderr)
        sys.exit(2)
    return codes


def main():
    args = parse_args()
    codes = resolve_codes(args)
    report = batch_screen(codes, args.date)

    if args.json:
        print(format_json_output(report))
    elif args.quiet:
        print(format_quiet_output(report))
    else:
        print(format_text_output(report, verbose=args.verbose))

    sys.exit(0 if report["passed_count"] > 0 else 1)


if __name__ == "__main__":
    main()
