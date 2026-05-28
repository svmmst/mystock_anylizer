import duckdb
import pandas as pd
from .config import DB_PATH


def connect():
    return duckdb.connect(DB_PATH)


def init_daily_price(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_price (
            code VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)


def get_stock_codes(con):
    """从 stock_basic 表获取所有股票代码"""
    return con.execute("SELECT code FROM stock_basic").fetchdf()["code"].tolist()


def get_codes_from_daily(con):
    """从 daily_price 表获取有数据的股票代码"""
    return con.execute("SELECT DISTINCT code FROM daily_price").fetchdf()["code"].tolist()


def get_last_date(con, code):
    """获取某只股票在 daily_price 中的最新日期"""
    result = con.execute(
        "SELECT MAX(date) FROM daily_price WHERE code = ?", [code]
    ).fetchone()[0]
    return result


def batch_insert_daily(con, df):
    """批量写入日线数据（去重：按 code+date 防重复）"""
    if df is None or df.empty:
        return
    con.register("_tmp_daily", df)
    con.execute("""
        INSERT INTO daily_price
        SELECT * FROM _tmp_daily d
        WHERE NOT EXISTS (
            SELECT 1 FROM daily_price t
            WHERE t.code = d.code AND t.date = d.date
        )
    """)
    con.unregister("_tmp_daily")


def batch_insert_financials(con, buffer):
    """批量写入财务数据（去重：按 code+report_date 防重复）"""
    if not buffer:
        return
    df = pd.concat(buffer, ignore_index=True)
    con.register("_tmp_fin", df)
    con.execute("""
        INSERT INTO financials_raw
        SELECT * FROM _tmp_fin t
        WHERE NOT EXISTS (
            SELECT 1 FROM financials_raw f
            WHERE f.code = t.code
              AND f.report_date = t.report_date
        )
    """)
    con.unregister("_tmp_fin")


def validate_daily_price(con):
    """检查 daily_price 表的数据质量"""
    total = con.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    dup = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT code, date, COUNT(*) as cnt
            FROM daily_price
            GROUP BY code, date
            HAVING cnt > 1
        )
    """).fetchone()[0]
    print(f"total rows: {total}, duplicates: {dup}")
    return total, dup
