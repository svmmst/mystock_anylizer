import time
import random

from common import db
from common import baostock_client as client
from common.config import SLEEP_RANGE, BATCH_SIZE


def run():
    print("全A股日线数据下载")

    con = db.connect()
    db.init_daily_price(con)
    client.login()

    codes = client.get_stock_basic_codes()
    print(f"股票总数: {len(codes)}")

    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i : i + BATCH_SIZE]
        print(f"\n===== batch {i}-{i+len(batch)} =====")

        for j, code in enumerate(batch):
            print(f"[{i+j+1}/{len(codes)}] {code}")

            try:
                df = client.download_kline(
                    code, start_date="2020-01-01", end_date="2026-04-22", adjustflag="3"
                )

                if df is not None:
                    db.batch_insert_daily(con, df)
                    print("  done")
                else:
                    print("  empty")

            except Exception as e:
                print("  error:", e)

            time.sleep(random.uniform(*SLEEP_RANGE))

        print("sleep batch...")
        time.sleep(5)

    con.close()
    client.logout()
    print("ALL DONE")


if __name__ == "__main__":
    run()
