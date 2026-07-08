"""
日线数据全量铺底 — 清表后重新下载所有股票不复权数据

用法：
  python rebuild_daily_price.py          # 全量重建 daily_price（会清空原表！）
  python rebuild_daily_price.py --with-qfq  # 重建后自动计算前复权数据

警告：此脚本会删除 daily_price 表中所有数据后重新下载！
"""

import sys
import time
from datetime import datetime
from multiprocessing import Pool

import baostock as bs
import pandas as pd

from common import db
from common import baostock_client as client

WORKERS = 4


def _worker_download(args):
    """
    子进程：独立 login，下载分配到的股票全量日线数据（不复权）。
    args: (worker_id, task_list)
    task_list: [(code, start_date, end_date), ...]
    返回: [DataFrame, ...]
    """
    import time as _time

    worker_id, task_list = args

    # 错开启动，避免同时 login 被服务端拒绝
    _time.sleep(worker_id * 2)

    # 带重试的登录
    for attempt in range(5):
        lg = bs.login()
        if lg.error_code == '0':
            break
        bs.logout()
        _time.sleep(3 + 2 ** attempt)
    else:
        print(f"  worker-{worker_id}: 登录失败，放弃")
        return []

    results = []
    consecutive_errors = 0

    for i, (code, start_date, end_date) in enumerate(task_list):
        # 连续错误过多时重连
        if consecutive_errors >= 10:
            bs.logout()
            _time.sleep(3)
            bs.login()
            consecutive_errors = 0

        try:
            rs = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",  # 不复权
            )

            if rs.error_code != '0':
                consecutive_errors += 1
                _time.sleep(1)
                bs.logout()
                bs.login()
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="3",
                )

            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                consecutive_errors = 0
                continue

            df = pd.DataFrame(rows, columns=[
                "date", "open", "high", "low", "close", "volume", "amount"
            ])
            df["code"] = code
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df[["code", "date", "open", "high", "low", "close", "volume", "amount"]]
            results.append(df)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors <= 3:
                print(f"  worker-{worker_id} 下载失败 {code}: {e}")

        # 每 500 只打印进度
        if (i + 1) % 500 == 0:
            print(f"  worker-{worker_id} 进度: {i+1}/{len(task_list)}")

    bs.logout()
    return results


def run():
    with_qfq = "--with-qfq" in sys.argv

    print("=" * 60)
    print("日线数据全量铺底（不复权）")
    print("警告：将清空 daily_price 表后重新下载全部历史数据")
    print("=" * 60)

    t0 = time.time()

    # 获取全量股票代码
    client.login()
    codes = client.get_all_a_codes()
    client.logout()

    if not codes:
        print("无法获取股票代码，退出")
        return

    print(f"股票总数: {len(codes)}")

    # 连接数据库并清表
    con = db.connect()
    con.execute("DROP TABLE IF EXISTS daily_price")
    db.init_daily_price(con)
    print("已清空 daily_price 表")

    # 构造全量下载任务
    today = datetime.now().strftime("%Y-%m-%d")
    tasks = [(code, "1990-01-01", today) for code in codes]

    # 均分任务到多个 worker
    chunks = [[] for _ in range(WORKERS)]
    for i, task in enumerate(tasks):
        chunks[i % WORKERS].append(task)

    # 多进程并发下载
    print(f"启动 {WORKERS} 个下载进程...")
    t1 = time.time()

    worker_args = [(i, chunks[i]) for i in range(WORKERS)]
    with Pool(WORKERS) as pool:
        all_results = pool.map(_worker_download, worker_args)

    t2 = time.time()
    print(f"下载完成，耗时 {t2 - t1:.1f} 秒")

    # 批量写入数据库
    print("写入数据库...")
    total_rows = 0
    buffer = []

    for worker_dfs in all_results:
        for df in worker_dfs:
            buffer.append(df)
            if len(buffer) >= 200:
                merged = pd.concat(buffer, ignore_index=True)
                db.batch_insert_daily(con, merged)
                total_rows += len(merged)
                buffer = []
                # 每写入一批打印进度
                if total_rows % 500000 < 50000:
                    print(f"  已写入 {total_rows:,} 条...")

    # 写入剩余
    if buffer:
        merged = pd.concat(buffer, ignore_index=True)
        db.batch_insert_daily(con, merged)
        total_rows += len(merged)

    t3 = time.time()
    print(f"写入完成，共 {total_rows:,} 条，耗时 {t3 - t2:.1f} 秒")

    # 验证
    db.validate_daily_price(con)

    # 可选：重建前复权数据
    if with_qfq:
        print("\n开始重建前复权数据...")
        from build_qfq_v6 import build_adjust_factor_daily, build_qfq, validate
        build_adjust_factor_daily(con)
        build_qfq(con)
        validate(con)

    con.close()
    print(f"\n全部完成，总耗时 {time.time() - t0:.1f} 秒")


if __name__ == "__main__":
    run()
