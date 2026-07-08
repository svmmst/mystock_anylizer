import baostock as bs
import pandas as pd
import time
from datetime import datetime
from .config import SLEEP_SEC, RETRY


# ======================================================================
# 连接
# ======================================================================
def login():
    for attempt in range(RETRY):
        lg = bs.login()
        if lg.error_code == '0':
            return lg
        # 登录失败，先登出再重试
        bs.logout()
        if attempt < RETRY - 1:
            wait = 2 ** attempt
            print(f"baostock 登录失败({lg.error_msg})，{wait}秒后第{attempt+2}次尝试...")
            time.sleep(wait)
    raise Exception(f"baostock 登录失败（重试{RETRY}次后仍失败）: {lg.error_msg}")


def logout():
    bs.logout()


# ======================================================================
# 股票列表
# ======================================================================
def get_all_a_codes():
    """在线获取全A股代码（用最近一个有数据的交易日）"""
    from datetime import timedelta
    # query_all_stock 只对已收盘的交易日有效，往前最多找7天
    data = []
    for delta in range(1, 8):
        day = (datetime.now() - timedelta(days=delta)).strftime("%Y-%m-%d")
        rs = bs.query_all_stock(day=day)
        data = []
        while rs.error_code == '0' and rs.next():
            data.append(rs.get_row_data())
        if data:
            break
    df = pd.DataFrame(data, columns=rs.fields)
    df = df[df["code"].str.startswith(("sh.6", "sz.0", "sz.3"))]
    return df["code"].tolist()


def get_stock_basic_codes():
    """获取股票基本信息中的全量代码（type=1 正常上市）"""
    rs = bs.query_stock_basic()
    data = []
    while rs.next():
        data.append(rs.get_row_data())
    df = pd.DataFrame(data, columns=rs.fields)
    df = df[df["type"] == "1"]
    return df["code"].tolist()


# ======================================================================
# 日线 K 线
# ======================================================================
def download_kline(code, start_date, end_date, adjustflag="3"):
    """
    下载日线 K 线数据。
    adjustflag: "2"=前复权, "3"=不复权
    返回 DataFrame，列为 [code, date, open, high, low, close, volume, amount]
    """
    rs = bs.query_history_k_data_plus(
        code,
        "date,open,high,low,close,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag=adjustflag,
    )

    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=[
        "date", "open", "high", "low", "close", "volume", "amount"
    ])

    df["code"] = code
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]]


# ======================================================================
# 财务数据
# ======================================================================
def fetch_financial_data(code, year, quarter):
    """
    获取单只股票某季度的财务数据。
    返回处理后的 DataFrame，或 None。
    """
    for _ in range(RETRY):
        try:
            rs_profit = bs.query_profit_data(code=code, year=year, quarter=quarter)
            rs_growth = bs.query_growth_data(code=code, year=year, quarter=quarter)

            profit_rows = []
            while rs_profit.next():
                profit_rows.append(rs_profit.get_row_data())

            if not profit_rows:
                return None

            df_profit = pd.DataFrame(profit_rows, columns=rs_profit.fields)

            growth_rows = []
            while rs_growth.next():
                growth_rows.append(rs_growth.get_row_data())

            df_growth = (
                pd.DataFrame(growth_rows, columns=rs_growth.fields)
                if growth_rows
                else None
            )

            return _process_financial(code, df_profit, df_growth)

        except Exception:
            time.sleep(1)

    return None


def _process_financial(code, df_profit, df_growth):
    """清洗财务数据"""
    df = df_profit.copy()
    df["code"] = code

    df["report_date"] = pd.to_datetime(df["statDate"])
    df["pub_date"] = pd.to_datetime(df["pubDate"])

    def to_num(col_name):
        return pd.to_numeric(df.get(col_name), errors="coerce")

    df["revenue"] = to_num("revenue")
    df["net_profit"] = to_num("netProfit")
    df["roe"] = to_num("roeAvg")
    df["gross_margin"] = to_num("grossProfitMargin")
    df["net_margin"] = to_num("netProfitMargin")

    if df_growth is not None:
        g = df_growth.copy()
        g["report_date"] = pd.to_datetime(g["statDate"])
        g["revenue_yoy"] = pd.to_numeric(g.get("YOYRevenue"), errors="coerce")
        g["net_profit_yoy"] = pd.to_numeric(g.get("YOYNetProfit"), errors="coerce")
        df = df.merge(
            g[["report_date", "revenue_yoy", "net_profit_yoy"]],
            on="report_date",
            how="left",
        )

    df["created_at"] = datetime.now()

    return df[[
        "code", "report_date", "pub_date",
        "revenue", "net_profit", "roe",
        "gross_margin", "net_margin",
        "revenue_yoy", "net_profit_yoy",
        "created_at",
    ]]
