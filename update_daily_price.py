from datetime import datetime, timedelta

from common import db
from common import baostock_client as client


def update_daily_price(con, codes):
    print("开始增量更新日线数据...")

    today = datetime.now().strftime("%Y-%m-%d")

    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code}", end=" ", flush=True)

        last_date = db.get_last_date(con, code)

        if last_date:
            start_date = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start_date = "1990-01-01"

        if start_date > today:
            print("已是最新")
            continue

        print(f"下载 {start_date} 至 {today}")
        df = client.download_kline(code, start_date, today, adjustflag="3")

        if df is None or df.empty:
            print("  无新数据")
            continue

        db.batch_insert_daily(con, df)
        print(f"  新增 {len(df)} 条")


def run():
    print("日线数据增量更新系统")

    client.login()
    con = db.connect()

    db.init_daily_price(con)

    codes = client.get_all_a_codes()
    if not codes:
        print("无股票代码，更新终止")
        return

    update_daily_price(con, codes)
    db.validate_daily_price(con)

    con.close()
    client.logout()
    print("完成")


if __name__ == "__main__":
    run()
