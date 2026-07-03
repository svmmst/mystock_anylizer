"""
日线数据增量更新 v3 — 基于 Tushare Pro

相比 v2 的优势：
- 一次 API 调用获取全市场当天数据（5000+ 只）
- 无需多进程，单线程即可秒级完成
- 无 baostock 会话管理问题

用法：
  python update_daily_price_v3.py              # 增量更新
  python update_daily_price_v3.py --backfill 20260101  # 从指定日期补全
"""

import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import tushare as ts

from common import db
from common.config import TUSHARE_TOKEN


def _ts_code_to_local(ts_code):
    """000001.SZ -> sz.000001"""
    parts = ts_code.split('.')
    return f"{parts[1].lower()}.{parts[0]}"


def fetch_daily(pro, trade_date):
    """
    获取指定交易日全市场日线数据。
    trade_date: 格式 YYYYMMDD
    返回 DataFrame [code, date, open, high, low, close, volume, amount] 或 None
    """
    try:
        df = pro.daily(trade_date=trade_date)
    except Exception as e:
        print(f"  API调用失败 ({trade_date}): {e}")
        return None

    if df is None or df.empty:
        return None

    # 转换格式
    df["code"] = df["ts_code"].apply(_ts_code_to_local)
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")

    # 单位转换：vol(手) -> volume(股), amount(千元) -> amount(元)
    df["volume"] = (df["vol"] * 100).round(2)
    df["amount"] = (df["amount"] * 1000).round(2)

    return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]]


def get_last_date(con):
    """获取 daily_price 表的全局最新日期"""
    result = con.execute("SELECT MAX(date) FROM daily_price").fetchone()[0]
    return result


def run():
    t_start = time.time()
    print("日线数据增量更新 v3（Tushare Pro 版）")

    con = db.connect()
    db.init_daily_price(con)
    pro = ts.pro_api(TUSHARE_TOKEN)

    # 确定起始日期
    if "--backfill" in sys.argv:
        idx = sys.argv.index("--backfill")
        if idx + 1 < len(sys.argv):
            start_str = sys.argv[idx + 1]
            start_date = datetime.strptime(start_str, "%Y%m%d").date()
        else:
            print("请指定起始日期，例如: --backfill 20260101")
            con.close()
            return
        print(f"补全模式：从 {start_date} 开始")
    else:
        last_date = get_last_date(con)
        if last_date is None:
            print("daily_price 表为空，请使用 --backfill 指定起始日期")
            con.close()
            return
        start_date = last_date + timedelta(days=1)
        print(f"增量模式：从 {start_date} 开始（上次更新到 {last_date}）")

    today = datetime.now().date()
    if start_date > today:
        print("数据已是最新，无需更新")
        con.close()
        return

    # 逐日拉取
    total_rows = 0
    total_days = 0
    current = start_date

    while current <= today:
        date_str = current.strftime("%Y%m%d")
        df = fetch_daily(pro, date_str)

        if df is not None and len(df) > 0:
            db.batch_insert_daily(con, df)
            total_rows += len(df)
            total_days += 1
            print(f"  {date_str}: {len(df)} 条")

        current += timedelta(days=1)

    print(f"\n写入完成：{total_days} 个交易日，共 {total_rows} 条")

    db.validate_daily_price(con)
    con.close()
    print(f"全部完成，总耗时 {time.time() - t_start:.1f} 秒")


if __name__ == "__main__":
    run()
