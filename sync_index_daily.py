"""
指数日线行情同步 — 基于 pytdx（通达信）

背景：
  指数行情此前混在个股 daily_price 里，且因每日增量用的 Tushare pro.daily() 只返回
  个股不含指数，指数行情早已停更。本脚本用 pytdx（无频率限制）独立维护指数行情，
  写入 index_daily_price 表（含指数专有的 up_count/down_count）。

数据源选型：Tushare index_daily 限流 1次/小时不可行；pytdx get_index_bars 无频率限制、
  数据更新到当天、与库里历史 close 吻合。

量纲（实测对齐库里旧数据）：
  volume = pytdx vol × 10000   （指数成交量单位换算，与库里 daily_price 口径一致）
  amount = pytdx amount        （已是元，无需换算）

用法：
  python sync_index_daily.py          # 增量：每个指数只拉比库里最新日期更新的部分
  python sync_index_daily.py --full   # 全量：每个指数从头翻页重灌（去重靠主键）
"""

import sys
import time
from datetime import datetime, time as dtime

import pandas as pd

# 复用 rebuild_factor_pytdx 的通达信连接/解析/断线重连
from rebuild_factor_pytdx import _connect_tdx, _parse_code
from common import db

# pytdx 指数量纲换算系数（实测：库里 vol = pytdx vol × 10000，amount 已是元）
VOL_MULTIPLIER = 10000
# pytdx 单次 K 线上限
PAGE_SIZE = 800
# 指数日线的 category 参数
CATEGORY_DAY = 9
# A 股交易时段（含集合竞价），用于判断“当前是否盘中”
TRADE_START = dtime(9, 15)
TRADE_END = dtime(15, 0)


def _is_intraday(now=None):
    """判断当前是否处于 A 股交易时段（工作日 09:15–15:00）。

    盘中 pytdx 返回的“当天 K 线”close 是实时价、非真实收盘，volume/amount 也不完整，
    误入库后会被主键去重永久固化（盘后不再覆盖），故盘中默认跳过写当天数据。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六日非交易日
        return False
    return TRADE_START <= now.time() <= TRADE_END


def get_index_codes(con):
    """获取待同步的指数代码清单。

    来源优先级（依次回退，取到非空即用）：
      1. index_basic 表（若已用 Tushare 补全，最全）
      2. index_daily_price 表自身已有的指数（日常增量的主来源，自我维持、最可靠）
      3. daily_price 中的 sz.399 存量（历史遗留兜底；指数已从 daily_price 剔除后此源为空）

    说明：指数已从 daily_price 剥离，故来源 3 通常为空；日常增量依赖来源 2 自给自足，
    不再依赖任何外部表。首次全量建表（表还空）时，若前两者都空，靠来源 3 或需手动指定。
    """
    codes = con.execute("SELECT code FROM index_basic").fetchdf()["code"].tolist()
    if codes:
        print(f"  指数清单来源: index_basic 表（{len(codes)} 个）")
        return codes

    codes = con.execute(
        "SELECT DISTINCT code FROM index_daily_price ORDER BY code"
    ).fetchdf()["code"].tolist()
    if codes:
        print(f"  指数清单来源: index_daily_price 已有指数（{len(codes)} 个，自我维持）")
        return codes

    codes = con.execute(
        "SELECT DISTINCT code FROM daily_price WHERE code LIKE 'sz.399%' ORDER BY code"
    ).fetchdf()["code"].tolist()
    print(f"  指数清单来源: daily_price 存量兜底（{len(codes)} 个）")
    return codes


def _bars_to_rows(code, bars):
    """把 pytdx bar 列表转成写库用的 dict 列表，应用量纲换算。"""
    rows = []
    for b in bars:
        rows.append({
            "code": code,
            "date": b["datetime"][:10],  # "2026-07-02 15:00" -> "2026-07-02"
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": (b["vol"] or 0) * VOL_MULTIPLIER,
            "amount": b["amount"] or 0.0,
            "up_count": b.get("up_count"),
            "down_count": b.get("down_count"),
        })
    return rows


def _fetch_all_history(api, market, stock_code):
    """翻页拉取某指数全部历史 bar（从最新往回翻到头）。"""
    all_bars = []
    start = 0
    while True:
        bars = api.get_index_bars(CATEGORY_DAY, market, stock_code, start, PAGE_SIZE)
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < PAGE_SIZE:  # 不足一页说明到头
            break
        start += PAGE_SIZE
    return all_bars


def _fetch_recent(api, market, stock_code):
    """只拉最近一页（增量用，覆盖最近 800 个交易日足够）。"""
    return api.get_index_bars(CATEGORY_DAY, market, stock_code, 0, PAGE_SIZE) or []


def run():
    full = "--full" in sys.argv
    # 盘中保护：默认跳过写入“当天日期”的行（避免固化盘中快照）；--allow-intraday 可绕过
    allow_intraday = "--allow-intraday" in sys.argv
    t_start = time.time()
    print(f"指数日线行情同步（pytdx）{'[全量]' if full else '[增量]'}")

    # 盘中运行时，除非显式 --allow-intraday，否则丢弃“今天”这一行，只写历史。
    # 盘后（15:00 后）跑同一命令即可正常补当天真实收盘。
    now = datetime.now()
    skip_today = _is_intraday(now) and not allow_intraday
    today = now.date()
    if _is_intraday(now):
        if skip_today:
            print(f"  ⚠️ 当前为交易时段（{now:%H:%M}），当天({today})数据为盘中快照、非真实收盘，"
                  f"本次将跳过写入当天数据（历史照常更新）。盘后 15:00 后再跑可补当天收盘。")
            print("     如确需写入当天（例如仅补历史、不在意当天），加 --allow-intraday。")
        else:
            print(f"  ⚠️ 当前为交易时段但已指定 --allow-intraday，将写入当天({today})盘中快照。")

    con = db.connect()
    db.init_index_daily_price(con)
    codes = get_index_codes(con)

    # 增量模式下取每个指数库里的最新日期
    last_dates = {}
    if not full:
        rows = con.execute(
            "SELECT code, MAX(date) FROM index_daily_price GROUP BY code"
        ).fetchall()
        last_dates = {r[0]: r[1] for r in rows}

    api = _connect_tdx()
    total_written = 0
    fail_count = 0
    reconnect_count = 0

    for i, code in enumerate(codes):
        market, stock_code = _parse_code(code)
        try:
            bars = _fetch_all_history(api, market, stock_code) if full \
                else _fetch_recent(api, market, stock_code)
        except Exception:
            # 断线重连一次
            reconnect_count += 1
            try:
                api.disconnect()
            except Exception:
                pass
            api = _connect_tdx()
            try:
                bars = _fetch_all_history(api, market, stock_code) if full \
                    else _fetch_recent(api, market, stock_code)
            except Exception:
                fail_count += 1
                continue

        if not bars:
            continue

        rows = _bars_to_rows(code, bars)
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).dt.date

        # 增量：只保留比库里最新日期更新的行
        if not full and code in last_dates and last_dates[code] is not None:
            df = df[df["date"] > last_dates[code]]

        # 盘中保护：丢弃“当天”这一行（盘中快照非真实收盘），对全量/增量均生效
        if skip_today:
            df = df[df["date"] < today]

        if df.empty:
            continue

        db.batch_insert_index_daily(con, df)
        total_written += len(df)

        if (i + 1) % 50 == 0:
            print(f"  进度: {i+1}/{len(codes)}，已写入 {total_written} 条，耗时 {time.time()-t_start:.1f}s")

    api.disconnect()

    # 自检输出
    n_code = con.execute("SELECT COUNT(DISTINCT code) FROM index_daily_price").fetchone()[0]
    n_row = con.execute("SELECT COUNT(*) FROM index_daily_price").fetchone()[0]
    last = con.execute("SELECT MAX(date) FROM index_daily_price").fetchone()[0]
    print(f"\n本次写入 {total_written} 条" + (f"，失败 {fail_count} 个" if fail_count else ""))
    if reconnect_count:
        print(f"  重连次数: {reconnect_count}")
    print(f"  index_daily_price 现有: {n_code} 个指数，{n_row} 条，最新日期 {last}")
    print("  sz.399001 最近 3 条（供核对口径）:")
    for r in con.execute(
        "SELECT date, close, volume, amount, up_count, down_count "
        "FROM index_daily_price WHERE code='sz.399001' ORDER BY date DESC LIMIT 3"
    ).fetchall():
        print("   ", r)

    con.close()
    print(f"完成，耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    run()
