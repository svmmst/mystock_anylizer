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


def init_index_basic(con):
    """创建指数基础信息表（Tushare index_basic 接口的字段映射）。

    与 stock_basic 分离：指数不再混在个股表里。字段直接映射 Tushare 的 12 个
    输出字段，code 存本地格式（sz.399006，由 ts_code 转换），全库统一以 code 为准。
    注：desc 是 SQL 保留字，建表/写入时必须双引号转义。
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS index_basic (
            code        VARCHAR PRIMARY KEY,
            ts_code     VARCHAR,
            name        VARCHAR,
            fullname    VARCHAR,
            market      VARCHAR,
            publisher   VARCHAR,
            index_type  VARCHAR,
            category    VARCHAR,
            base_date   VARCHAR,
            base_point  DOUBLE,
            list_date   VARCHAR,
            weight_rule VARCHAR,
            "desc"      VARCHAR
        )
    """)


def get_stock_codes(con, include_index=False):
    """从 stock_basic 表获取股票代码。

    默认只返回个股（type='stock'），排除指数（sh.000/sz.399/sh.880 等）。
    include_index=True 时返回全部代码（含指数），兼容需要指数的场景。

    注：若 type 列尚未生成（未运行 classify_stock_basic.py），则回退为返回全部代码，
    保证旧库仍可正常工作。
    """
    cols = {row[0] for row in con.execute("DESCRIBE stock_basic").fetchall()}
    if "type" in cols and not include_index:
        sql = "SELECT code FROM stock_basic WHERE type = 'stock'"
    else:
        sql = "SELECT code FROM stock_basic"
    return con.execute(sql).fetchdf()["code"].tolist()


def get_codes_from_daily(con):
    """从 daily_price 表获取有数据的股票代码"""
    return con.execute("SELECT DISTINCT code FROM daily_price").fetchdf()["code"].tolist()


def get_last_date(con, code):
    """获取某只股票在 daily_price 中的最新日期"""
    result = con.execute(
        "SELECT MAX(date) FROM daily_price WHERE code = ?", [code]
    ).fetchone()[0]
    return result


def get_all_last_dates(con):
    """批量获取所有股票的最新日期，返回 {code: date} 字典"""
    rows = con.execute(
        "SELECT code, MAX(date) as last_date FROM daily_price GROUP BY code"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


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
