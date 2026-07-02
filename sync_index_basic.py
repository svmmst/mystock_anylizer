"""
指数基础信息同步 — 基于 Tushare Pro index_basic 接口

背景：
  指数与个股已分离。指数基础信息存独立的 index_basic 表（不再混在 stock_basic 里）。
  Tushare 的 stock_basic 接口只返回个股，指数信息需用 index_basic 接口获取。
  本库的指数全部是 sz.399xxx（深证系列），因此只需拉取 SZSE 市场。

  注意：本脚本只维护指数的“基础信息”。指数的“行情”仍保留在 daily_price /
  daily_price_qfq，供 market_regime.py / daily_pick.py 择时使用，本脚本不涉及。

用法：
  python sync_index_basic.py            # 直接同步（全量刷新 index_basic 表）
  python sync_index_basic.py --if-stale # 距上次成功同步不足 1 小时则跳过（退出码 0）

限流：index_basic 接口限 1 次/小时，与 stock_basic 各自独立计费。
--if-stale 用独立时间戳文件 .index_basic_last_sync 记录上次成功时间，避免撞限流。
"""

import sys
import time
from pathlib import Path

import tushare as ts

from common import db
from common.config import TUSHARE_TOKEN

# 记录上次成功同步时间的时间戳文件（与脚本同目录，内容为 unix 秒的浮点数）
# 独立于 stock_basic 的时间戳，互不影响
LAST_SYNC_FILE = Path(__file__).resolve().parent / ".index_basic_last_sync"
# Tushare index_basic 接口限 1 次/小时，据此设最小同步间隔
MIN_SYNC_INTERVAL = 3600  # 秒

# index_basic 表的字段（与 common/db.py init_index_basic 一致，desc 需双引号转义）
COLUMNS = [
    "code", "ts_code", "name", "fullname", "market", "publisher",
    "index_type", "category", "base_date", "base_point", "list_date",
    "weight_rule", "desc",
]


def _read_last_sync():
    """读取上次成功同步的 unix 时间戳；无记录或损坏则返回 None。"""
    try:
        return float(LAST_SYNC_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_last_sync(ts_now):
    """记录本次成功同步时间。"""
    LAST_SYNC_FILE.write_text(str(ts_now))


def _ts_code_to_local(ts_code):
    """399006.SZ -> sz.399006（与项目其它脚本保持一致）"""
    parts = ts_code.split(".")
    return f"{parts[1].lower()}.{parts[0]}"


def fetch_index_basic(pro):
    """拉取深证指数基础信息，code 列转为本地格式。返回 DataFrame。"""
    df = pro.index_basic(
        market="SZSE",
        fields="ts_code,name,fullname,market,publisher,index_type,"
               "category,base_date,base_point,list_date,weight_rule,desc",
    )
    if df is None or df.empty:
        raise RuntimeError("index_basic 接口返回空数据")
    df["code"] = df["ts_code"].apply(_ts_code_to_local)
    return df


def refresh(con, df):
    """全量刷新 index_basic 表：清空后整表重写。

    指数集合小、每天几乎不变、无外部表以外键引用它，全量刷新最简单，
    天然处理退市/更名/新增。
    """
    con.register("tmp_idx", df)
    con.execute("DELETE FROM index_basic")
    # desc 是 SQL 保留字，需双引号转义
    col_list = ", ".join(f'"{c}"' if c == "desc" else c for c in COLUMNS)
    con.execute(f"""
        INSERT INTO index_basic ({col_list})
        SELECT {col_list} FROM tmp_idx
    """)
    con.unregister("tmp_idx")


def run():
    t_start = time.time()
    print("指数基础信息同步（Tushare Pro index_basic，market=SZSE）")

    # --if-stale：距上次成功同步不足 1 小时则跳过（避免撞 index_basic 1次/小时限流）。
    # 跳过时以成功退出码返回，供 daily_update.py 继续后续步骤。
    if "--if-stale" in sys.argv:
        last = _read_last_sync()
        if last is not None:
            elapsed = t_start - last
            if elapsed < MIN_SYNC_INTERVAL:
                mins = int((MIN_SYNC_INTERVAL - elapsed) / 60)
                print(f"  距上次成功同步仅 {int(elapsed / 60)} 分钟，未满 1 小时，"
                      f"跳过本次同步（约 {mins} 分钟后可再同步）")
                return

    con = db.connect()
    db.init_index_basic(con)
    pro = ts.pro_api(TUSHARE_TOKEN)

    print("拉取深证指数基础信息 ...")
    try:
        df = fetch_index_basic(pro)
    except Exception as e:
        # Tushare 限流不视为错误：打印提示并跳过，不影响后续步骤
        if "频率超限" in str(e) or "每分钟" in str(e) or "每小时" in str(e):
            print(f"  Tushare 接口限流，本次跳过同步：{e}")
            con.close()
            return
        raise
    print(f"  获取深证指数 {len(df)} 条")

    refresh(con, df)

    total = con.execute("SELECT COUNT(*) FROM index_basic").fetchone()[0]
    print(f"  index_basic 表现有 {total} 条")
    print("  样例:")
    for r in con.execute(
        "SELECT code, name, category FROM index_basic ORDER BY code LIMIT 8"
    ).fetchall():
        print("   ", r)

    con.close()

    # 记录本次成功同步时间，供 --if-stale 判断间隔
    _write_last_sync(t_start)
    print(f"完成，耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    run()
