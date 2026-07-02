"""
指数行情验证脚本（baostock 交叉验证）

把本地 index_daily_price（pytdx 数据源）与 baostock 独立下载的指数行情对比，
交叉验证指数数据的准确性。指数数据是择时/回测的重要输入，每次更新后应自动校验，
防止 pytdx 偶发脏数据误导策略。

验证方式：抽样若干指数 + 关键指数，对比近期若干交易日的 OHLC，容差 0.05%。
（指数不复权，baostock 用 adjustflag="3"。只比价格，不比 volume——两者量纲口径不同。）

用法：
  python verify_index_baostock.py            # 默认抽样验证（快速，供每日更新后自动调用）
  python verify_index_baostock.py --full     # 更多指数 + 更长时段（人工深度核对）

退出码：全部 PASS 返回 0；有 FAIL 返回 1（便于 daily_update.py 感知并告警）。
"""

import sys

import duckdb
import pandas as pd

from common import baostock_client as client

# 必验的关键指数（择时脚本实际使用）
KEY_CODES = ["sz.399001", "sz.399006", "sz.399106"]
# 随机抽样个数（在关键指数之外）
SAMPLE_N = 5
SAMPLE_N_FULL = 15
# 验证时段（近期若干交易日）
START_DATE = "2026-05-01"
END_DATE = "2026-07-02"
# 容差：0.05%（pytdx 与 baostock 实测偏差 <0.001%，留足浮点余量）
TOLERANCE = 0.0005


def fetch_baostock(code):
    """baostock 不复权指数行情。"""
    df = client.download_kline(code, START_DATE, END_DATE, adjustflag="3")
    if df is None:
        return None
    df = df[["date", "open", "high", "low", "close"]].rename(columns={
        "open": "bs_open", "high": "bs_high", "low": "bs_low", "close": "bs_close",
    })
    return df.sort_values("date").reset_index(drop=True)


def fetch_local(con, code):
    df = con.execute("""
        SELECT date, open, high, low, close
        FROM index_daily_price
        WHERE code = ? AND date BETWEEN ? AND ?
        ORDER BY date
    """, [code, START_DATE, END_DATE]).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    return df.rename(columns={
        "open": "db_open", "high": "db_high", "low": "db_low", "close": "db_close",
    })


def compare(db_df, bs_df):
    """对比 OHLC，返回 (共同交易日, 最大误差, 超容差行数)。"""
    merged = pd.merge(db_df, bs_df, on="date", how="inner")
    if merged.empty:
        return 0, 0.0, 0
    max_err = 0.0
    bad = 0
    for col in ["open", "high", "low", "close"]:
        rel = (merged[f"db_{col}"] - merged[f"bs_{col}"]).abs() / \
              merged[f"bs_{col}"].abs().replace(0, float("nan"))
        max_err = max(max_err, rel.max())
        bad += int((rel > TOLERANCE).sum())
    return len(merged), max_err, bad


def run():
    full = "--full" in sys.argv
    con = duckdb.connect("stock.db")

    # 组装待验证指数：关键指数 + 随机抽样
    n_sample = SAMPLE_N_FULL if full else SAMPLE_N
    sampled = con.execute(
        "SELECT code FROM index_daily_price "
        "WHERE code NOT IN (SELECT UNNEST(?)) "
        "GROUP BY code ORDER BY random() LIMIT ?",
        [KEY_CODES, n_sample],
    ).fetchdf()["code"].tolist()
    codes = KEY_CODES + sampled

    print(f"指数行情交叉验证（index_daily_price vs baostock）{'[full]' if full else ''}")
    print(f"  时段 {START_DATE} ~ {END_DATE}，容差 {TOLERANCE*100}%，共 {len(codes)} 个指数")
    print("=" * 60)

    client.login()
    all_pass = True
    checked = 0
    for code in codes:
        db_df = fetch_local(con, code)
        if db_df.empty:
            print(f"  {code}: [SKIP] 本地无数据")
            continue
        bs_df = fetch_baostock(code)
        if bs_df is None or bs_df.empty:
            print(f"  {code}: [SKIP] baostock 无数据")
            continue

        n, max_err, bad = compare(db_df, bs_df)
        checked += 1
        status = "PASS" if bad == 0 else "FAIL"
        if bad > 0:
            all_pass = False
        print(f"  {code}: {status}  共同{n}日  最大偏差 {max_err*100:.4f}%  超容差 {bad}")

    client.logout()
    con.close()

    print("=" * 60)
    if checked == 0:
        print("  ⚠️ 未验证任何指数（无重叠数据），请检查")
        sys.exit(1)
    if all_pass:
        print(f"  ✅ 总体结论: ALL PASS（{checked} 个指数全部通过）")
        sys.exit(0)
    else:
        print(f"  ❌ 总体结论: SOME FAIL —— 指数数据与 baostock 存在偏差，请人工核查！")
        sys.exit(1)


if __name__ == "__main__":
    run()
