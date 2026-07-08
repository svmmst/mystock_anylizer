"""
前复权数据验证脚本（baostock 同源对比）
将本地 daily_price_qfq 与 baostock adjustflag="2" 直接下载的前复权数据对比。
同源对比可以排除数据源差异，专门验证我们的计算逻辑是否正确。
"""

import duckdb
import pandas as pd
from common import baostock_client as client

SAMPLES = [
    ("sh.600519", "贵州茅台"),
    ("sh.600000", "浦发银行"),
    ("sz.000001", "平安银行"),
    ("sz.300750", "宁德时代"),
    ("sh.601318", "中国平安"),
]

START_DATE = "2025-01-01"
END_DATE   = "2026-05-06"
TOLERANCE  = 0.0001  # 同源对比容差收紧到 0.01%


def fetch_baostock_qfq(code):
    df = client.download_kline(code, START_DATE, END_DATE, adjustflag="2")
    if df is None:
        return None
    df = df[["date", "open", "high", "low", "close"]].rename(columns={
        "open": "bs_open", "high": "bs_high",
        "low":  "bs_low",  "close": "bs_close",
    })
    return df.sort_values("date").reset_index(drop=True)


def fetch_local(con, code):
    df = con.execute("""
        SELECT date, open, high, low, close
        FROM daily_price_qfq
        WHERE code = ?
          AND date BETWEEN ? AND ?
        ORDER BY date
    """, [code, START_DATE, END_DATE]).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    return df.rename(columns={
        "open": "db_open", "high": "db_high",
        "low":  "db_low",  "close": "db_close",
    })


def compare(db_df, bs_df, name):
    merged = pd.merge(db_df, bs_df, on="date", how="inner")
    n = len(merged)
    if n == 0:
        return {"name": name, "共同交易日": 0, "结论": "无重叠数据"}, None

    merged["rel_err"] = (
        (merged["db_close"] - merged["bs_close"]).abs()
        / merged["bs_close"].abs().replace(0, float("nan"))
    )
    bad = merged[merged["rel_err"] > TOLERANCE]

    return {
        "name": name,
        "共同交易日": n,
        "最大误差": f"{merged['rel_err'].max():.6f} ({merged['rel_err'].max()*100:.4f}%)",
        "平均误差": f"{merged['rel_err'].mean():.6f} ({merged['rel_err'].mean()*100:.4f}%)",
        "超容差行数": len(bad),
        "结论": "PASS" if len(bad) == 0 else "FAIL",
    }, merged


def run():
    con = duckdb.connect("stock.db")
    client.login()
    all_pass = True

    for code, name in SAMPLES:
        print(f"\n{'='*55}")
        print(f"  {name}  {code}")
        print(f"{'='*55}")

        db_df = fetch_local(con, code)
        if db_df.empty:
            print("  [SKIP] 本地无数据")
            continue
        print(f"  本地数据:     {len(db_df)} 条")

        bs_df = fetch_baostock_qfq(code)
        if bs_df is None:
            print("  [SKIP] baostock 无数据")
            continue
        print(f"  baostock数据: {len(bs_df)} 条")

        result, merged = compare(db_df, bs_df, name)

        print(f"  共同交易日:  {result['共同交易日']}")
        print(f"  最大误差:    {result['最大误差']}")
        print(f"  平均误差:    {result['平均误差']}")
        print(f"  超容差行数:  {result['超容差行数']}  (容差={TOLERANCE*100}%)")
        print(f"  结论:        {result['结论']}")

        if result["结论"] == "FAIL":
            all_pass = False
            print("\n  [超差样本]")
            bad = merged[merged["rel_err"] > TOLERANCE][
                ["date", "db_close", "bs_close", "rel_err"]
            ].head(10)
            print(bad.to_string(index=False))

        print("\n  [最近5个交易日对比]")
        recent = merged.tail(5)[["date", "db_close", "bs_close", "rel_err"]].copy()
        recent["date"] = recent["date"].dt.date
        print(recent.to_string(index=False))

    client.logout()
    con.close()
    print(f"\n{'='*55}")
    print(f"  总体结论: {'ALL PASS ✓' if all_pass else 'SOME FAIL ✗'}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    run()
