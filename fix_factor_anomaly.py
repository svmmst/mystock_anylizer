"""
修复 adjust_factor 表中 factor≈1.0 的异常记录

异常特征：factor 从正常值（>5）突然跌至≈1.0，这是 baostock/tushare 在除权日未及时更新的 bug。

修复策略：
- 已恢复的（有后续正常 factor）：用前一条正常 factor 填充（因为除权会导致 factor 增加，用前值是保守选择）
- 未恢复的（该股最后一条为异常）：同样用前一条正常 factor 填充

修复后重建 adjust_factor_daily + daily_price_qfq。
"""

import duckdb
import pandas as pd

DB_PATH = "/Users/sunxibao/projects/mystock_anylizer/stock.db"


def find_anomalies(con):
    """找出所有 factor≈1.0 且前一条 factor>5 的异常记录"""
    df = con.execute("""
        WITH ordered AS (
            SELECT code, date, factor,
                   LAG(factor) OVER (PARTITION BY code ORDER BY date) as prev_factor,
                   LEAD(factor) OVER (PARTITION BY code ORDER BY date) as next_factor
            FROM adjust_factor
        )
        SELECT code, date, factor, prev_factor, next_factor
        FROM ordered
        WHERE factor <= 1.1 AND prev_factor > 5
        ORDER BY code, date
    """).fetchdf()
    return df


def fix_adjust_factor(con, anomalies):
    """用 prev_factor 替换异常的 factor 值"""
    fixed_count = 0
    for _, row in anomalies.iterrows():
        code = row['code']
        date = row['date']
        prev_factor = row['prev_factor']

        # 用前一条正常值填充（保守策略：除权只会增加factor，用前值不会高估）
        con.execute("""
            UPDATE adjust_factor
            SET factor = ?
            WHERE code = ? AND date = ?
        """, [prev_factor, code, date])
        fixed_count += 1

    return fixed_count


def fix_adjust_factor_tushare(con, anomalies):
    """同步修复 adjust_factor_tushare 表中的对应异常"""
    # adjust_factor_tushare 是每日因子表，异常会扩散到多天
    total_fixed = 0

    for _, row in anomalies.iterrows():
        code = row['code']
        bug_date = str(row['date'])[:10]  # 确保是字符串格式
        prev_factor = float(row['prev_factor'])

        # 确定异常结束日期：用 adjust_factor 事件表找下一个正常factor的日期
        recovery = con.execute("""
            SELECT MIN(date) as recovery_date
            FROM adjust_factor
            WHERE code = ? AND date > ?::DATE AND factor > 2
        """, [code, bug_date]).fetchone()

        if recovery and recovery[0]:
            end_date = str(recovery[0])[:10]
        else:
            end_date = '2099-12-31'

        # 先统计受影响行数
        cnt = con.execute("""
            SELECT COUNT(*) FROM adjust_factor_tushare
            WHERE code = ? AND date >= ?::DATE AND date < ?::DATE AND factor <= 1.1
        """, [code, bug_date, end_date]).fetchone()[0]

        if cnt > 0:
            # 修复
            con.execute("""
                UPDATE adjust_factor_tushare
                SET factor = ?
                WHERE code = ? AND date >= ?::DATE AND date < ?::DATE AND factor <= 1.1
            """, [prev_factor, code, bug_date, end_date])
            total_fixed += cnt

    return total_fixed


def rebuild_adjust_factor_daily(con):
    """重建 adjust_factor_daily（从 adjust_factor 事件表展开到每日）"""
    print("重建 adjust_factor_daily...")
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
    cnt = con.execute("SELECT COUNT(*) FROM adjust_factor_daily").fetchone()[0]
    print(f"  adjust_factor_daily: {cnt} 行")


def rebuild_qfq(con):
    """重建 daily_price_qfq"""
    print("重建 daily_price_qfq...")
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
    cnt = con.execute("SELECT COUNT(*) FROM daily_price_qfq").fetchone()[0]
    print(f"  daily_price_qfq: {cnt} 行")


def validate(con, anomaly_codes):
    """验证修复后数据正常"""
    print("\n验证修复结果...")

    # 检查恒瑞医药 2024-07-12 是否已修复
    df = con.execute("""
        SELECT date, factor FROM adjust_factor_daily
        WHERE code = 'sh.600276' AND date BETWEEN '2024-07-10' AND '2024-07-15'
        ORDER BY date
    """).fetchdf()
    print(f"\n  恒瑞医药 2024-07-10~15 因子:")
    print(f"  {df.to_string(index=False)}")

    # 检查复权价格是否合理（无极端异常值）
    abnormal = con.execute("""
        SELECT COUNT(*) FROM daily_price_qfq
        WHERE close > 10000 OR close < 0.01
    """).fetchone()[0]
    print(f"\n  极端异常价格记录数: {abnormal}")

    # 检查修复过的股票是否还有 factor≈1.0 异常
    remaining = con.execute("""
        WITH ordered AS (
            SELECT code, date, factor,
                   LAG(factor) OVER (PARTITION BY code ORDER BY date) as prev_factor
            FROM adjust_factor
        )
        SELECT COUNT(*)
        FROM ordered
        WHERE factor <= 1.1 AND prev_factor > 5
    """).fetchone()[0]
    print(f"  剩余异常记录数: {remaining}")

    if remaining > 0:
        print("  ⚠️ 仍有异常未修复")
    else:
        print("  ✅ 所有异常已修复")


def main():
    print("=" * 60)
    print("  修复 adjust_factor 中 factor≈1.0 的异常数据")
    print("=" * 60)

    con = duckdb.connect(DB_PATH)

    # 步骤1：找出异常
    print("\n[1/5] 查找异常记录...")
    anomalies = find_anomalies(con)
    print(f"  发现 {len(anomalies)} 条异常（factor从正常值骤降至≈1.0）")
    print(f"  涉及 {anomalies['code'].nunique()} 只股票")

    if anomalies.empty:
        print("  无异常需要修复")
        con.close()
        return

    # 步骤2：修复 adjust_factor 表
    print("\n[2/5] 修复 adjust_factor 事件表...")
    fixed = fix_adjust_factor(con, anomalies)
    print(f"  已修复 {fixed} 条记录（用前一条正常因子填充）")

    # 步骤3：同步修复 adjust_factor_tushare
    print("\n[3/5] 同步修复 adjust_factor_tushare...")
    tushare_fixed = fix_adjust_factor_tushare(con, anomalies)
    print(f"  已同步修复 {tushare_fixed} 条 tushare 每日因子记录")

    # 步骤4：重建 adjust_factor_daily 和 daily_price_qfq
    print("\n[4/5] 重建衍生表...")
    rebuild_adjust_factor_daily(con)
    rebuild_qfq(con)

    # 步骤5：验证
    anomaly_codes = anomalies['code'].unique().tolist()
    validate(con, anomaly_codes)

    con.close()
    print("\n" + "=" * 60)
    print("  修复完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
