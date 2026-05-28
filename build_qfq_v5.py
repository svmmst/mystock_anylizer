import baostock as bs
import pandas as pd
from datetime import datetime

from common import db
from common import baostock_client as client


def update_adjust_factor(con, codes):
    print("更新复权因子...")

    con.execute("DROP TABLE IF EXISTS adjust_factor")

    con.execute("""
        CREATE TABLE adjust_factor (
            code VARCHAR,
            date DATE,
            factor DOUBLE
        )
    """)

    client.login()

    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code}")

        rs = bs.query_adjust_factor(
            code=code,
            start_date="1990-01-01",
            end_date=datetime.now().strftime("%Y-%m-%d"),
        )

        rows = []
        while (rs.error_code == '0') and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            continue

        df = pd.DataFrame(rows, columns=rs.fields)
        df = df.rename(columns={"dividOperateDate": "date", "adjustFactor": "factor"})
        df["code"] = code
        df["date"] = pd.to_datetime(df["date"])
        df["factor"] = df["factor"].astype(float)
        df = df[["code", "date", "factor"]]
        df = df.sort_values("date").drop_duplicates(subset=["code", "date"], keep="last")

        con.execute("INSERT INTO adjust_factor SELECT * FROM df")

    client.logout()
    print("复权因子下载完成")


def build_adjust_factor_daily(con):
    print("构建每日复权因子...")

    con.execute("DROP TABLE IF EXISTS adjust_factor_daily")

    con.execute("""
        CREATE TABLE adjust_factor_daily AS
        SELECT
            p.code,
            p.date,
            COALESCE(
                LAST_VALUE(f.factor IGNORE NULLS) OVER (
                    PARTITION BY p.code
                    ORDER BY p.date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ),
                1.0
            ) AS factor
        FROM daily_price p
        LEFT JOIN adjust_factor f
          ON p.code = f.code AND p.date = f.date
    """)

    print("每日因子完成")


def build_qfq(con):
    print("生成前复权数据...")

    con.execute("DROP TABLE IF EXISTS daily_price_qfq")

    con.execute("""
        CREATE TABLE daily_price_qfq AS
        WITH latest AS (
            SELECT code, MAX(date) AS max_date
            FROM adjust_factor_daily
            GROUP BY code
        ),
        latest_factor AS (
            SELECT d.code, d.factor AS latest_factor
            FROM adjust_factor_daily d
            JOIN latest l
              ON d.code = l.code AND d.date = l.max_date
        )

        SELECT
            p.code,
            p.date,
            p.open  * d.factor / lf.latest_factor AS open,
            p.high  * d.factor / lf.latest_factor AS high,
            p.low   * d.factor / lf.latest_factor AS low,
            p.close * d.factor / lf.latest_factor AS close,
            p.volume,
            p.amount
        FROM daily_price p
        JOIN adjust_factor_daily d
          ON p.code = d.code AND p.date = d.date
        JOIN latest_factor lf
          ON p.code = lf.code
    """)

    print("前复权数据生成完成")


def validate(con):
    print("验证数据...")

    raw = con.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    qfq = con.execute("SELECT COUNT(*) FROM daily_price_qfq").fetchone()[0]

    print(f"raw: {raw}, qfq: {qfq}")

    if raw != qfq:
        raise Exception("行数不一致")

    abnormal = con.execute("""
        SELECT COUNT(*) FROM daily_price_qfq
        WHERE close > 10000 OR close < 0.01
    """).fetchone()[0]

    print(f"abnormal: {abnormal}")
    print("数据正常")


def run():
    con = db.connect()

    print("QFQ系统 V5")

    codes = db.get_codes_from_daily(con)

    update_adjust_factor(con, codes)
    build_adjust_factor_daily(con)
    build_qfq(con)
    validate(con)

    con.close()
    print("完成")


if __name__ == "__main__":
    run()
