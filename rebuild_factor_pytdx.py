"""
复权因子系统 — 基于 pytdx（通达信行情服务器）

优势：无频率限制，5500只股票约5分钟完成，数据免费。

用法：
  python rebuild_factor_pytdx.py              # 全量重建（下载除权数据 → 计算因子 → 生成 qfq）
  python rebuild_factor_pytdx.py --update     # 增量更新（只重算有新除权事件的股票）
  python rebuild_factor_pytdx.py --qfq-only   # 跳过下载，用现有因子重建 qfq
"""

import sys
import time

import pandas as pd
from pytdx.hq import TdxHq_API

from common import db

# 通达信服务器列表（备用）
TDX_SERVERS = [
    ('180.153.18.170', 7709),
    ('14.17.75.71', 7709),
    ('202.108.253.130', 7709),
    ('60.12.136.250', 7709),
    ('115.238.56.198', 7709),
]


def _connect_tdx():
    """连接通达信服务器，自动选最快的"""
    api = TdxHq_API()
    for host, port in TDX_SERVERS:
        try:
            if api.connect(host, port):
                print(f"  连接成功: {host}:{port}")
                return api
        except Exception:
            continue
    raise ConnectionError("所有通达信服务器均无法连接")


def _parse_code(code):
    """sz.000001 -> (0, '000001'), sh.600000 -> (1, '600000')"""
    parts = code.split('.')
    market = 0 if parts[0] == 'sz' else 1
    return market, parts[1]


def download_xdxr_events(con):
    """
    从通达信下载全部股票的除权除息数据。
    返回 DataFrame [code, date, fenhong, songzhuangu, peigu, peigujia]
    """
    codes = db.get_codes_from_daily(con)
    print(f"需下载除权除息数据: {len(codes)} 只股票")

    api = _connect_tdx()

    all_events = []
    no_event_count = 0
    fail_count = 0
    reconnect_count = 0

    t0 = time.time()
    for i, code in enumerate(codes):
        market, stock_code = _parse_code(code)

        try:
            xdxr = api.get_xdxr_info(market, stock_code)
        except Exception:
            # 连接断开，重连
            reconnect_count += 1
            try:
                api.disconnect()
            except Exception:
                pass
            api = _connect_tdx()
            try:
                xdxr = api.get_xdxr_info(market, stock_code)
            except Exception:
                fail_count += 1
                continue

        if not xdxr:
            no_event_count += 1
            continue

        # 只取 category=1 的除权除息事件
        events = [r for r in xdxr if r['category'] == 1]
        if not events:
            no_event_count += 1
            continue

        for e in events:
            all_events.append({
                'code': code,
                'year': e['year'],
                'month': e['month'],
                'day': e['day'],
                'fenhong': e['fenhong'] or 0.0,
                'songzhuangu': e['songzhuangu'] or 0.0,
                'peigu': e['peigu'] or 0.0,
                'peigujia': e['peigujia'] or 0.0,
            })

        if (i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            print(f"  进度: {i+1}/{len(codes)}, 耗时 {elapsed:.1f}s, 有除权事件 {len(all_events)} 条")

    api.disconnect()
    elapsed = time.time() - t0

    print(f"下载完成: 耗时 {elapsed:.1f}s")
    print(f"  有除权记录: {len(codes) - no_event_count - fail_count} 只")
    print(f"  无除权记录: {no_event_count} 只（因子=1.0）")
    if fail_count:
        print(f"  查询失败: {fail_count} 只")
    if reconnect_count:
        print(f"  重连次数: {reconnect_count}")

    if not all_events:
        print("无除权除息数据，终止")
        return None

    df = pd.DataFrame(all_events)
    df['date'] = pd.to_datetime(
        df['year'].astype(str) + '-' +
        df['month'].astype(str).str.zfill(2) + '-' +
        df['day'].astype(str).str.zfill(2)
    )
    df = df[['code', 'date', 'fenhong', 'songzhuangu', 'peigu', 'peigujia']]
    df = df.sort_values(['code', 'date']).reset_index(drop=True)

    print(f"共获取 {len(df)} 条除权除息记录（{df['code'].nunique()} 只股票）")
    return df


def compute_factors(con, xdxr_df):
    """
    根据除权除息事件 + daily_price 前收盘价计算复权因子。

    复权因子公式（前复权）：
      除权日因子 = 前一日因子 * (1 + 送转/10 + 配股/10) / (1 + (配股/10*配股价 - 分红/10) / 前收盘价)
      等价于：factor_new = factor_old * ratio
      其中 ratio = (1 + songzhuangu/10 + peigu/10) / (1 - (fenhong/10 - peigujia*peigu/10) / prev_close)

    非除权日因子不变。
    """
    print("计算复权因子...")

    # 获取所有交易日的前收盘价（除权日前一天的收盘价）
    # 策略：将除权事件与 daily_price 关联，取除权日前一个交易日的 close
    xdxr_df = xdxr_df.copy()

    # 写入临时表
    con.execute("DROP TABLE IF EXISTS _xdxr_events")
    con.execute("""
        CREATE TABLE _xdxr_events (
            code VARCHAR,
            date DATE,
            fenhong DOUBLE,
            songzhuangu DOUBLE,
            peigu DOUBLE,
            peigujia DOUBLE
        )
    """)
    con.execute("INSERT INTO _xdxr_events SELECT * FROM xdxr_df")

    # 用 SQL 获取除权日前一交易日的收盘价
    event_with_prev_close = con.execute("""
        WITH ranked AS (
            SELECT
                p.code,
                p.date,
                p.close,
                e.date AS event_date,
                e.fenhong,
                e.songzhuangu,
                e.peigu,
                e.peigujia,
                ROW_NUMBER() OVER (PARTITION BY e.code, e.date ORDER BY p.date DESC) as rn
            FROM _xdxr_events e
            JOIN daily_price p
              ON p.code = e.code AND p.date < e.date
        )
        SELECT code, event_date as date, close as prev_close,
               fenhong, songzhuangu, peigu, peigujia
        FROM ranked
        WHERE rn = 1
    """).fetchdf()

    if event_with_prev_close.empty:
        print("无法匹配除权事件与前收盘价，终止")
        return False

    # 计算每次除权的因子倍数
    df = event_with_prev_close
    # ratio = (1 + 送转/10 + 配股/10) / (1 - (分红/10 - 配股价*配股/10) / 前收盘)
    numerator = 1 + df['songzhuangu'] / 10 + df['peigu'] / 10
    cash_effect = (df['fenhong'] / 10 - df['peigujia'] * df['peigu'] / 10) / df['prev_close']
    denominator = 1 - cash_effect
    df = df.copy()
    df['ratio'] = numerator / denominator

    # 过滤异常比例（理论上 ratio 应该 >= 1.0，但浮点精度可能有微小偏差）
    abnormal = (df['ratio'] < 0.5) | (df['ratio'] > 20)
    if abnormal.sum() > 0:
        print(f"  ⚠️ 过滤 {abnormal.sum()} 条异常比例记录")
        df = df[~abnormal]

    # 按股票分组，累乘得到每个除权日的绝对因子
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    df['factor'] = df.groupby('code')['ratio'].cumprod()

    sparse_factors = df[['code', 'date', 'factor']].copy()
    print(f"  除权事件因子计算完成: {len(sparse_factors)} 条")

    # 写入稀疏因子表
    con.execute("DROP TABLE IF EXISTS _adjust_factor_sparse")
    con.execute("""
        CREATE TABLE _adjust_factor_sparse (
            code VARCHAR,
            date DATE,
            factor DOUBLE
        )
    """)
    con.execute("INSERT INTO _adjust_factor_sparse SELECT code, date, factor FROM sparse_factors")

    # 清理临时表
    con.execute("DROP TABLE IF EXISTS _xdxr_events")
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

    count = con.execute("SELECT COUNT(*) FROM adjust_factor_tushare").fetchone()[0]
    print(f"每日因子填充完成，共 {count} 条")

    con.execute("DROP TABLE IF EXISTS _adjust_factor_sparse")
    return True


def build_qfq(con):
    """生成前复权数据"""
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
    # 下限用 0.001：累积因子大的老股（如 sh.600601）前复权后，30 年前的价格
    # 会被压到 1 分以下，这是前复权的数学必然，非异常，故阈值放宽避免误报。
    abnormal = con.execute("""
        SELECT COUNT(*) FROM daily_price_qfq
        WHERE (close > 10000 OR close < 0.001)
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

    # 抽样对比：平安银行最新因子
    sample = con.execute("""
        SELECT factor FROM adjust_factor_tushare
        WHERE code = 'sz.000001'
        ORDER BY date DESC LIMIT 1
    """).fetchone()
    if sample:
        print(f"  抽样: sz.000001 最新因子 = {sample[0]:.6f}")

    print("验证完成")


def save_xdxr_events(con, xdxr_df):
    """持久化除权事件表，用于下次增量对比"""
    con.execute("DROP TABLE IF EXISTS xdxr_events")
    con.execute("""
        CREATE TABLE xdxr_events (
            code VARCHAR,
            date DATE,
            fenhong DOUBLE,
            songzhuangu DOUBLE,
            peigu DOUBLE,
            peigujia DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    con.execute("INSERT INTO xdxr_events SELECT * FROM xdxr_df")
    count = con.execute("SELECT COUNT(*) FROM xdxr_events").fetchone()[0]
    print(f"除权事件表已保存: {count} 条")


def find_affected_codes(con, new_xdxr_df):
    """
    对比新下载的除权事件与数据库中已有的，返回有新增/变更事件的股票列表。
    """
    # 检查 xdxr_events 表是否存在
    exists = con.execute("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name = 'xdxr_events'
    """).fetchone()[0]

    if not exists:
        print("无历史除权事件基准表")
        return None

    # 获取已有事件
    old_events = con.execute("""
        SELECT code, date FROM xdxr_events
    """).fetchdf()

    # 将新旧事件都转为 (code, date) 集合进行对比
    old_set = set(zip(old_events['code'], old_events['date'].astype(str)))
    new_set = set(zip(new_xdxr_df['code'], new_xdxr_df['date'].astype(str)))

    # 新增的事件
    added = new_set - old_set
    # 被删除的事件（也需要重算）
    removed = old_set - new_set

    affected = set()
    for code, _ in added:
        affected.add(code)
    for code, _ in removed:
        affected.add(code)

    return sorted(affected)


def update_affected_factors(con, affected_codes, xdxr_df):
    """
    对受影响的股票重新计算因子：
    1. 筛选这些股票的除权事件
    2. 计算稀疏因子
    3. 删除旧的每日因子
    4. 用窗口函数填充新的每日因子
    """
    print(f"重算 {len(affected_codes)} 只受影响股票的因子...")

    # 筛选受影响股票的除权事件
    affected_xdxr = xdxr_df[xdxr_df['code'].isin(affected_codes)].copy()

    if affected_xdxr.empty:
        # 这些股票的除权事件被删除了，因子全部重置为 1.0
        print("  这些股票无除权事件，因子全部设为 1.0")
        affected_list = affected_codes
        con.execute("""
            DELETE FROM adjust_factor_tushare
            WHERE code IN (SELECT UNNEST(?::VARCHAR[]))
        """, [affected_list])
        con.execute("""
            INSERT INTO adjust_factor_tushare
            SELECT code, date, 1.0 AS factor
            FROM daily_price
            WHERE code IN (SELECT UNNEST(?::VARCHAR[]))
        """, [affected_list])
        return

    # 写入临时表
    con.execute("DROP TABLE IF EXISTS _xdxr_events")
    con.execute("""
        CREATE TABLE _xdxr_events (
            code VARCHAR, date DATE,
            fenhong DOUBLE, songzhuangu DOUBLE,
            peigu DOUBLE, peigujia DOUBLE
        )
    """)
    con.execute("INSERT INTO _xdxr_events SELECT * FROM affected_xdxr")

    # 获取除权日前一交易日的收盘价
    event_with_prev_close = con.execute("""
        WITH ranked AS (
            SELECT
                p.code, p.date, p.close,
                e.date AS event_date,
                e.fenhong, e.songzhuangu, e.peigu, e.peigujia,
                ROW_NUMBER() OVER (PARTITION BY e.code, e.date ORDER BY p.date DESC) as rn
            FROM _xdxr_events e
            JOIN daily_price p ON p.code = e.code AND p.date < e.date
        )
        SELECT code, event_date as date, close as prev_close,
               fenhong, songzhuangu, peigu, peigujia
        FROM ranked WHERE rn = 1
    """).fetchdf()

    if event_with_prev_close.empty:
        print("  无法匹配前收盘价，这些股票因子设为 1.0")
        con.execute("DELETE FROM adjust_factor_tushare WHERE code IN (SELECT UNNEST(?::VARCHAR[]))", [affected_codes])
        con.execute("""
            INSERT INTO adjust_factor_tushare
            SELECT code, date, 1.0 FROM daily_price
            WHERE code IN (SELECT UNNEST(?::VARCHAR[]))
        """, [affected_codes])
        con.execute("DROP TABLE IF EXISTS _xdxr_events")
        return

    # 计算因子
    df = event_with_prev_close
    numerator = 1 + df['songzhuangu'] / 10 + df['peigu'] / 10
    cash_effect = (df['fenhong'] / 10 - df['peigujia'] * df['peigu'] / 10) / df['prev_close']
    denominator = 1 - cash_effect
    df = df.copy()
    df['ratio'] = numerator / denominator

    # 过滤异常
    abnormal = (df['ratio'] < 0.5) | (df['ratio'] > 20)
    if abnormal.sum() > 0:
        print(f"  过滤 {abnormal.sum()} 条异常比例记录")
        df = df[~abnormal]

    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    df['factor'] = df.groupby('code')['ratio'].cumprod()

    sparse_factors = df[['code', 'date', 'factor']].copy()
    print(f"  计算完成: {len(sparse_factors)} 条稀疏因子")

    # 写入稀疏因子临时表
    con.execute("DROP TABLE IF EXISTS _affected_sparse")
    con.execute("""
        CREATE TABLE _affected_sparse (code VARCHAR, date DATE, factor DOUBLE)
    """)
    con.execute("INSERT INTO _affected_sparse SELECT code, date, factor FROM sparse_factors")

    # 删除旧因子
    affected_list = affected_codes
    con.execute("""
        DELETE FROM adjust_factor_tushare
        WHERE code IN (SELECT UNNEST(?::VARCHAR[]))
    """, [affected_list])

    # 用窗口函数填充每日因子
    con.execute("""
        INSERT INTO adjust_factor_tushare
        SELECT p.code, p.date,
            COALESCE(
                LAST_VALUE(f.factor IGNORE NULLS) OVER (
                    PARTITION BY p.code ORDER BY p.date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ), 1.0
            ) AS factor
        FROM daily_price p
        LEFT JOIN _affected_sparse f ON p.code = f.code AND p.date = f.date
        WHERE p.code IN (SELECT UNNEST(?::VARCHAR[]))
    """, [affected_list])

    # 清理
    con.execute("DROP TABLE IF EXISTS _xdxr_events")
    con.execute("DROP TABLE IF EXISTS _affected_sparse")
    print(f"  受影响股票因子更新完成")


def extend_factors_for_new_days(con, last_factor_date, affected_codes):
    """
    对无新除权事件的股票，将其各自最后一天的因子值延伸到新交易日。

    锚点用每只股票自己的最后因子日（MAX(date)），而非全市场统一的
    last_factor_date：停牌股在统一日期当天可能没有因子记录，用统一锚点会漏延伸。
    新股（daily_price 有数据但从无因子记录）单独兜底为 factor=1.0。
    """
    n_new_days = con.execute("""
        SELECT COUNT(DISTINCT date) FROM daily_price
        WHERE date > ?
    """, [last_factor_date]).fetchone()[0]

    if n_new_days == 0:
        print("无新交易日需要延伸因子")
        return

    print(f"为未受影响的股票延伸因子（{n_new_days} 个新交易日）...")

    # 排除受影响股票（它们的因子已由 update_affected_factors 全量重算）
    exclude_clause = ""
    params = []
    if affected_codes:
        exclude_clause = "AND p.code NOT IN (SELECT UNNEST(?::VARCHAR[]))"
        params.append(affected_codes)

    # 每只股票用自己的最后因子日作锚点，将该因子延伸到其后的所有新交易日
    con.execute(f"""
        INSERT INTO adjust_factor_tushare
        SELECT p.code, p.date, prev.factor
        FROM daily_price p
        JOIN (
            SELECT af.code, af.date AS anchor_date, af.factor
            FROM adjust_factor_tushare af
            JOIN (
                SELECT code, MAX(date) AS max_date
                FROM adjust_factor_tushare
                GROUP BY code
            ) m ON af.code = m.code AND af.date = m.max_date
        ) prev ON p.code = prev.code AND p.date > prev.anchor_date
        WHERE 1=1
          {exclude_clause}
    """, params)

    # 新股兜底：daily_price 有记录但 adjust_factor_tushare 完全没有的 code，补 factor=1.0
    con.execute("""
        INSERT INTO adjust_factor_tushare
        SELECT p.code, p.date, 1.0 AS factor
        FROM daily_price p
        WHERE p.code NOT IN (SELECT DISTINCT code FROM adjust_factor_tushare)
    """)

    new_count = con.execute("""
        SELECT COUNT(*) FROM adjust_factor_tushare WHERE date > ?
    """, [last_factor_date]).fetchone()[0]
    print(f"  延伸完成，新增 {new_count} 条因子记录")


def incremental_update(con):
    """
    增量更新主流程：
    1. 下载全部除权事件
    2. 与已有事件对比找出受影响股票
    3. 只对受影响股票重算因子
    4. 未受影响股票延伸因子到新交易日
    5. 重建前复权表
    """
    # 检查是否需要更新
    last_factor_date = con.execute(
        "SELECT MAX(date) FROM adjust_factor_tushare"
    ).fetchone()[0]
    last_price_date = con.execute(
        "SELECT MAX(date) FROM daily_price"
    ).fetchone()[0]

    if last_factor_date is None:
        print("因子表为空，请先运行全量重建（不带参数）")
        return False

    print(f"因子表最新日期: {last_factor_date}")
    print(f"日线表最新日期: {last_price_date}")

    # 即使日期相同，也要检查是否有新除权事件
    has_new_days = last_price_date > last_factor_date
    if has_new_days:
        print(f"有 {(last_price_date - last_factor_date).days} 天新数据需要处理")

    # 下载全部除权事件
    xdxr_df = download_xdxr_events(con)
    if xdxr_df is None:
        if has_new_days:
            # 无除权事件但有新交易日，仅延伸因子
            print("无除权事件，仅延伸因子到新交易日")
            extend_factors_for_new_days(con, last_factor_date, [])
            return True
        print("无除权事件且无新交易日，无需更新")
        return True

    # 对比找出受影响的股票
    affected_codes = find_affected_codes(con, xdxr_df)

    if affected_codes is None:
        # 首次运行增量模式，无基准表，只保存基准不重算
        print("首次增量运行：保存除权事件基准表")
        save_xdxr_events(con, xdxr_df)
        if has_new_days:
            extend_factors_for_new_days(con, last_factor_date, [])
        return True

    print(f"\n受影响的股票: {len(affected_codes)} 只")
    if affected_codes and len(affected_codes) <= 20:
        print(f"  {affected_codes}")

    # 处理受影响的股票
    if affected_codes:
        update_affected_factors(con, affected_codes, xdxr_df)

    # 延伸未受影响股票的因子到新交易日
    if has_new_days:
        extend_factors_for_new_days(con, last_factor_date, affected_codes)

    # 保存最新的除权事件（用于下次增量对比）
    save_xdxr_events(con, xdxr_df)

    return True


def run():
    qfq_only = "--qfq-only" in sys.argv
    update_mode = "--update" in sys.argv

    t_start = time.time()
    con = db.connect()
    print("=" * 50)

    if update_mode:
        print("复权因子增量更新（pytdx 通达信版）")
        print("=" * 50)

        if not incremental_update(con):
            con.close()
            return

    elif qfq_only:
        print("复权因子 — 仅重建前复权表")
        print("=" * 50)

    else:
        print("复权因子全量重建（pytdx 通达信版）")
        print("=" * 50)

        xdxr_df = download_xdxr_events(con)
        if xdxr_df is None:
            con.close()
            return

        if not compute_factors(con, xdxr_df):
            con.close()
            return

        build_daily_factors(con)

        # 全量重建时也保存除权事件，为后续增量更新提供基准
        save_xdxr_events(con, xdxr_df)

    build_qfq(con)
    validate(con)

    con.close()
    print(f"\n全部完成，总耗时 {time.time() - t_start:.1f} 秒")


if __name__ == "__main__":
    run()
