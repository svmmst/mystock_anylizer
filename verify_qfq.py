"""
前复权数据验证脚本
将本地 daily_price_qfq 与 akshare（东方财富数据源）对比，抽查若干股票。
"""

import time
import duckdb
import pandas as pd
import akshare as ak


# 抽查的股票（代码, 名称, akshare格式）
SAMPLES = [
    ("sh.600519", "贵州茅台", "sh600519"),
    ("sh.600000", "浦发银行", "sh600000"),
    ("sz.000001", "平安银行", "sz000001"),
    ("sz.300750", "宁德时代", "sz300750"),
    ("sh.601318", "中国平安", "sh601318"),
]

# 对比的日期范围（选近期有数据的区间）
START_DATE = "20250101"
END_DATE   = "20260506"
# DuckDB 查询用
DB_START = "2025-01-01"
DB_END   = "2026-05-06"

# 误差容忍：相对误差 < 0.1% 视为一致
TOLERANCE = 0.001


def fetch_akshare(ak_code: str) -> pd.DataFrame:
    """从 akshare 获取前复权日线数据"""
    df = ak.stock_zh_a_daily(symbol=ak_code, adjust="qfq")
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= DB_START) & (df["date"] <= DB_END)].copy()
    df = df[["date", "open", "high", "low", "close"]].rename(columns={
        "open": "ak_open", "high": "ak_high",
        "low": "ak_low",   "close": "ak_close",
    })
    df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_local(con, db_code: str) -> pd.DataFrame:
    """从本地 DuckDB 获取前复权日线数据"""
    df = con.execute("""
        SELECT date, open, high, low, close
        FROM daily_price_qfq
        WHERE code = ?
          AND date BETWEEN ? AND ?
        ORDER BY date
    """, [db_code, DB_START, DB_END]).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={
        "open": "db_open", "high": "db_high",
        "low": "db_low",   "close": "db_close",
    })
    return df


def compare(db_df: pd.DataFrame, ak_df: pd.DataFrame, name: str) -> dict:
    """对比两份数据，返回统计摘要"""
    merged = pd.merge(db_df, ak_df, on="date", how="inner")
    n = len(merged)
    if n == 0:
        return {"name": name, "共同交易日": 0, "结论": "无重叠数据"}

    for col in ["open", "high", "low", "close"]:
        db_col, ak_col = f"db_{col}", f"ak_{col}"
        diff = (merged[db_col] - merged[ak_col]).abs()
        rel_err = diff / merged[ak_col].abs().replace(0, float("nan"))
        merged[f"rel_err_{col}"] = rel_err

    close_err = merged["rel_err_close"]
    max_err = close_err.max()
    mean_err = close_err.mean()
    bad_rows = merged[close_err > TOLERANCE]

    result = {
        "name": name,
        "共同交易日": n,
        "收盘价最大误差": f"{max_err:.6f} ({max_err*100:.4f}%)",
        "收盘价平均误差": f"{mean_err:.6f} ({mean_err*100:.4f}%)",
        "超容差行数": len(bad_rows),
        "结论": "PASS" if len(bad_rows) == 0 else "FAIL",
    }

    if len(bad_rows) > 0:
        result["超差样本"] = bad_rows[["date", "db_close", "ak_close", "rel_err_close"]].head(5).to_dict("records")

    return result, merged


def run():
    con = duckdb.connect("stock.db")
    all_pass = True

    for db_code, name, ak_code in SAMPLES:
        print(f"\n{'='*55}")
        print(f"  {name}  {db_code}")
        print(f"{'='*55}")

        # 获取本地数据
        db_df = fetch_local(con, db_code)
        if db_df.empty:
            print("  [SKIP] 本地无数据")
            continue
        print(f"  本地数据: {len(db_df)} 条  ({db_df['date'].min().date()} ~ {db_df['date'].max().date()})")

        # 获取 akshare 数据
        try:
            ak_df = fetch_akshare(ak_code)
        except Exception as e:
            print(f"  [ERROR] akshare 获取失败: {e}")
            continue
        print(f"  akshare 数据: {len(ak_df)} 条  ({ak_df['date'].min().date()} ~ {ak_df['date'].max().date()})")

        # 对比
        result, merged = compare(db_df, ak_df, name)

        print(f"  共同交易日:    {result['共同交易日']}")
        print(f"  收盘价最大误差: {result['收盘价最大误差']}")
        print(f"  收盘价平均误差: {result['收盘价平均误差']}")
        print(f"  超容差行数:    {result['超容差行数']}  (容差={TOLERANCE*100}%)")
        print(f"  结论:          {result['结论']}")

        if result["结论"] == "FAIL":
            all_pass = False
            print("\n  [超差样本（收盘价）]")
            bad = merged[merged["rel_err_close"] > TOLERANCE][
                ["date", "db_close", "ak_close", "rel_err_close"]
            ].head(10)
            print(bad.to_string(index=False))

        # 打印最近5天对比
        print("\n  [最近5个交易日对比（收盘价）]")
        recent = merged.tail(5)[["date", "db_close", "ak_close", "rel_err_close"]].copy()
        recent["date"] = recent["date"].dt.date
        print(recent.to_string(index=False))

        time.sleep(1)

    con.close()
    print(f"\n{'='*55}")
    print(f"  总体结论: {'ALL PASS' if all_pass else 'SOME FAIL'}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    run()
