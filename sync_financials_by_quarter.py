import duckdb
import time
from datetime import datetime

from common import db
from common import baostock_client as client
from common.config import SLEEP_SEC, BATCH_INSERT

TARGET_YEAR = 2026
TARGET_QUARTER = 1


def get_max_quarter_map(con, codes):
    df = con.execute("""
        SELECT
            code,
            MAX(report_date) as max_date
        FROM financials_raw
        WHERE code IN (SELECT unnest(?))
        GROUP BY code
    """, [codes]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        code = row["code"]
        max_date = row["max_date"]
        if pd.notnull(max_date):
            year = max_date.year
            quarter = (max_date.month - 1) // 3 + 1
            result[code] = (year, quarter)
    return result


def main():
    print(f"财务数据同步 - 目标: {TARGET_YEAR}年 Q{TARGET_QUARTER}")

    client.login()
    con = db.connect()

    codes = db.get_stock_codes(con)
    print(f"股票总数: {len(codes)}")

    max_quarter_map = get_max_quarter_map(con, codes)
    print(f"已有数据的股票数: {len(max_quarter_map)}")

    target_q_int = TARGET_YEAR * 4 + TARGET_QUARTER

    need_codes = []
    for code in codes:
        max_q = max_quarter_map.get(code)
        if max_q is None:
            need_codes.append(code)
        else:
            max_q_int = max_q[0] * 4 + max_q[1]
            if max_q_int < target_q_int:
                need_codes.append(code)

    print(f"需要下载的股票数: {len(need_codes)}")

    if not need_codes:
        print("所有股票的目标季度数据已存在，无需更新")
        client.logout()
        return

    buffer = []
    for i, code in enumerate(need_codes):
        if i % 100 == 0:
            print(f"进度: {i+1}/{len(need_codes)}")

        df = client.fetch_financial_data(code, TARGET_YEAR, TARGET_QUARTER)
        if df is not None:
            buffer.append(df)

        if len(buffer) >= BATCH_INSERT:
            db.batch_insert_financials(con, buffer)
            buffer.clear()

        time.sleep(SLEEP_SEC)

    db.batch_insert_financials(con, buffer)

    client.logout()
    print(f"{TARGET_YEAR}年 Q{TARGET_QUARTER} 财务数据同步完成")


if __name__ == "__main__":
    import pandas as pd
    main()
