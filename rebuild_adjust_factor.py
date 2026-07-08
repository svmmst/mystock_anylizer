"""
复权因子全量重建 — Baostock 多进程下载 + 每日因子填充

用法：
  python rebuild_adjust_factor.py              # 全量重建（下载 + 填充 + 生成 qfq）
  python rebuild_adjust_factor.py --qfq-only   # 跳过下载，用现有因子重建 qfq
"""

import sys
import time
import multiprocessing as mp

import baostock as bs
import pandas as pd

from common import db

WORKERS = 4
RECONNECT_EVERY = 200


def _worker_download_factors(args):
    """
    子进程：独立 login baostock，下载分配到的股票复权因子。
    返回 (queried_codes, [DataFrame, ...])
    - queried_codes: 成功查询的 code 集合（含无除权记录的股票）
    - DataFrame 列表: 含 [code, date, factor]
    """
    worker_id, task_list = args

    lg = bs.login()
    if lg.error_code != '0':
        print(f"  Worker-{worker_id} login失败: {lg.error_msg}")
        return (set(), [])

    results = []
    queried_codes = set()  # 成功查询过的（含空结果）
    consecutive_fails = 0
    total_fails = 0

    for i, code in enumerate(task_list):
        # 定期重连，防长连接超时
        if i > 0 and i % RECONNECT_EVERY == 0:
            time.sleep(1)
            bs.login()

        success = False
        for attempt in range(3):
            try:
                rs = bs.query_adjust_factor(
                    code=code,
                    start_date="1990-01-01",
                    end_date="2099-12-31",
                )

                if rs.error_code != '0':
                    time.sleep(2 ** attempt)
                    bs.login()
                    continue

                rows = []
                while rs.error_code == '0' and rs.next():
                    rows.append(rs.get_row_data())

                # 空结果也算成功（该股票确实没有除权记录）
                if not rows:
                    success = True
                    break

                df = pd.DataFrame(rows, columns=rs.fields)
                df = df.rename(columns={"dividOperateDate": "date", "adjustFactor": "factor"})
                df["code"] = code
                df["date"] = pd.to_datetime(df["date"])
                df["factor"] = df["factor"].astype(float)
                df = df[["code", "date", "factor"]]
                df = df.sort_values("date").drop_duplicates(subset=["code", "date"], keep="last")
                results.append(df)
                success = True
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt + 1)
                    bs.login()
                else:
                    total_fails += 1
                    if total_fails <= 10:
                        print(f"  Worker-{worker_id} 失败 {code}: {e}")

        if success:
            queried_codes.add(code)
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if consecutive_fails >= 5:
                # 连续失败5次说明被限流，直接放弃剩余任务
                print(f"  Worker-{worker_id} 连续失败{consecutive_fails}次，判定为限流，放弃剩余 {len(task_list)-i-1} 只")
                break

        if (i + 1) % 500 == 0:
            print(f"  Worker-{worker_id} 进度: {i+1}/{len(task_list)}, 已成功 {len(results)} 只(有数据), 已查询 {len(queried_codes)} 只")

    print(f"  Worker-{worker_id} 完成: 有数据 {len(results)}, 已查询 {len(queried_codes)}/{len(task_list)}, 失败 {total_fails}")
    return (queried_codes, results)


def _run_download(codes, round_label=""):
    """执行一轮多进程下载，返回成功查询的 code 集合和 DataFrame 列表"""
    chunks = [[] for _ in range(WORKERS)]
    for i, code in enumerate(codes):
        chunks[i % WORKERS].append(code)

    worker_args = [(i, chunks[i]) for i in range(WORKERS) if chunks[i]]

    print(f"  {round_label}启动 {len(worker_args)} 个 worker，共 {len(codes)} 只...")
    t0 = time.time()

    ctx = mp.get_context("spawn")
    with ctx.Pool(WORKERS) as pool:
        all_results = pool.map(_worker_download_factors, worker_args)

    print(f"  {round_label}耗时 {time.time() - t0:.1f} 秒")

    # worker 返回 (queried_codes, [DataFrame, ...])
    success_codes = set()
    buffer = []
    for queried_codes, worker_dfs in all_results:
        success_codes.update(queried_codes)
        buffer.extend(worker_dfs)

    return success_codes, buffer


def download_all_factors(con):
    """多进程下载全量复权因子（稀疏，只有除权日有记录），自动重试失败的"""
    codes = db.get_codes_from_daily(con)
    print(f"需下载复权因子: {len(codes)} 只股票")

    # 先建好临时表
    con.execute("DROP TABLE IF EXISTS _adjust_factor_sparse")
    con.execute("""
        CREATE TABLE _adjust_factor_sparse (
            code VARCHAR,
            date DATE,
            factor DOUBLE
        )
    """)

    remaining = codes
    total_data_codes = 0

    # 最多尝试 3 轮
    for round_num in range(1, 4):
        print(f"\n第 {round_num} 轮下载（{len(remaining)} 只）...")
        success_codes, buffer = _run_download(remaining, f"[轮{round_num}] ")

        # 每轮结束立即写入数据库，防止中断丢失
        if buffer:
            round_df = pd.concat(buffer, ignore_index=True)
            round_df = round_df.drop_duplicates(subset=["code", "date"], keep="last")
            con.execute("INSERT INTO _adjust_factor_sparse SELECT * FROM round_df")
            total_data_codes += round_df["code"].nunique()
            print(f"  本轮写入 {len(round_df)} 条记录（{round_df['code'].nunique()} 只有数据）")

        # 计算失败的
        failed = [c for c in remaining if c not in success_codes]
        print(f"  第 {round_num} 轮结果: 成功查询 {len(success_codes)}, 连接失败 {len(failed)}")

        if not failed:
            break

        remaining = failed
        if round_num < 3:
            print(f"  等待 15 秒后重试失败的 {len(failed)} 只...")
            time.sleep(15)

    if failed:
        fail_rate = len(failed) / len(codes) * 100
        print(f"\n⚠️ 最终仍有 {len(failed)} 只连接失败（{fail_rate:.1f}%）")
        if fail_rate > 20:
            print("失败率过高（>20%），终止。请检查网络后重试。")
            return False
        print("失败率可接受，这些股票因子填充时将使用默认值 1.0")

    count = con.execute("SELECT COUNT(*) FROM _adjust_factor_sparse").fetchone()[0]
    if count == 0:
        print("无数据下载，终止")
        return False

    n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM _adjust_factor_sparse").fetchone()[0]
    print(f"\n共获取 {count} 条稀疏因子记录（{n_codes} 只股票有除权数据）")
    print("稀疏因子写入完成")
    return True


def build_daily_factors(con):
    """用窗口函数将稀疏因子填充为每日因子，写入 adjust_factor_tushare"""
    print("填充每日因子（窗口函数）...")

    con.execute("DROP TABLE IF EXISTS adjust_factor_tushare")
    con.execute("""
        CREATE TABLE adjust_factor_tushare AS
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
        LEFT JOIN _adjust_factor_sparse f
          ON p.code = f.code AND p.date = f.date
    """)

    # 添加主键
    count = con.execute("SELECT COUNT(*) FROM adjust_factor_tushare").fetchone()[0]
    print(f"每日因子填充完成，共 {count} 条")

    # 清理临时表
    con.execute("DROP TABLE IF EXISTS _adjust_factor_sparse")
    return True


def build_qfq(con):
    """生成前复权数据（与 V7 一致）"""
    print("生成前复权数据...")

    con.execute("DROP TABLE IF EXISTS daily_price_qfq")
    con.execute("""
        CREATE TABLE daily_price_qfq AS
        WITH latest AS (
            SELECT code, MAX(date) AS max_date
            FROM adjust_factor_tushare
            GROUP BY code
        ),
        latest_factor AS (
            SELECT f.code, f.factor AS latest_factor
            FROM adjust_factor_tushare f
            JOIN latest l
              ON f.code = l.code AND f.date = l.max_date
        )
        SELECT
            p.code,
            p.date,
            p.open  * f.factor / lf.latest_factor AS open,
            p.high  * f.factor / lf.latest_factor AS high,
            p.low   * f.factor / lf.latest_factor AS low,
            p.close * f.factor / lf.latest_factor AS close,
            p.volume,
            p.amount
        FROM daily_price p
        JOIN adjust_factor_tushare f
          ON p.code = f.code AND p.date = f.date
        JOIN latest_factor lf
          ON p.code = lf.code
    """)

    print("前复权数据生成完成")


def validate(con):
    """验证数据质量"""
    print("验证数据...")

    raw = con.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    qfq = con.execute("SELECT COUNT(*) FROM daily_price_qfq").fetchone()[0]
    print(f"  daily_price: {raw}, daily_price_qfq: {qfq}")

    if raw != qfq:
        diff = raw - qfq
        print(f"  ⚠️ 行数差异 {diff} 条（部分股票可能缺少因子数据）")
    else:
        print(f"  ✅ 行数一致")

    # 排除指数后检查异常价格
    abnormal = con.execute("""
        SELECT COUNT(*) FROM daily_price_qfq
        WHERE (close > 10000 OR close < 0.01)
          AND code NOT LIKE 'sz.399%'
          AND code NOT LIKE 'sh.000%'
    """).fetchone()[0]
    print(f"  个股异常价格（排除指数）: {abnormal}")

    # 检查因子单调性（排除指数）
    non_mono = con.execute("""
        WITH ordered AS (
            SELECT code, date, factor,
                   LAG(factor) OVER (PARTITION BY code ORDER BY date) as prev_factor
            FROM adjust_factor_tushare
            WHERE code NOT LIKE 'sz.399%' AND code NOT LIKE 'sh.000%'
        )
        SELECT COUNT(*) FROM ordered
        WHERE prev_factor IS NOT NULL AND factor < prev_factor * 0.99
    """).fetchone()[0]
    print(f"  因子下降>1%的记录数（排除指数）: {non_mono}")

    if abnormal == 0 and non_mono == 0:
        print("  ✅ 数据质量正常")
    else:
        print("  ⚠️ 存在异常，请检查")

    print("验证完成")


def run():
    qfq_only = "--qfq-only" in sys.argv

    t_start = time.time()
    con = db.connect()
    print("=" * 50)
    print("复权因子全量重建系统（Baostock 多进程版）")
    print("=" * 50)

    if qfq_only:
        print("跳过下载，直接用现有因子重建 qfq")
    else:
        if not download_all_factors(con):
            con.close()
            return
        build_daily_factors(con)

    build_qfq(con)
    validate(con)

    con.close()
    print(f"\n全部完成，总耗时 {time.time() - t_start:.1f} 秒")


if __name__ == "__main__":
    run()
