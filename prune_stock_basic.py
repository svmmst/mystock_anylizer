"""
清理 stock_basic 空壳代码

空壳代码定义：无中文名称（不在 Tushare 上市列表）且 daily_price、
daily_price_qfq 两张行情表中都没有任何数据。这类代码多为早年 Baostock
铺底残留的已退市死代码，无任何用处。

安全性：仅删除「无名称 + 无行情」的代码，指数（有行情）和正常个股均不受影响。

用法：
  python prune_stock_basic.py            # 直接删除
  python prune_stock_basic.py --dry-run  # 只统计不删除
"""

import sys

from common import db

# 空壳判定：无名称，且两张行情表都查不到该 code
EMPTY_SHELL_WHERE = """
    name IS NULL
    AND NOT EXISTS (SELECT 1 FROM daily_price     d WHERE d.code = stock_basic.code)
    AND NOT EXISTS (SELECT 1 FROM daily_price_qfq q WHERE q.code = stock_basic.code)
"""


def run():
    dry = "--dry-run" in sys.argv
    con = db.connect()
    print("清理 stock_basic 空壳代码" + ("（dry-run，不实际删除）" if dry else ""))

    n = con.execute(
        f"SELECT COUNT(*) FROM stock_basic WHERE {EMPTY_SHELL_WHERE}"
    ).fetchone()[0]
    print(f"  符合条件的空壳代码: {n}")

    # 样例
    for r in con.execute(
        f"SELECT code FROM stock_basic WHERE {EMPTY_SHELL_WHERE} LIMIT 5"
    ).fetchall():
        print("   ", r[0])

    if dry:
        con.close()
        print("dry-run 结束，未删除任何数据")
        return

    before = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    con.execute(f"DELETE FROM stock_basic WHERE {EMPTY_SHELL_WHERE}")
    after = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]

    con.close()
    print(f"  已删除 {before - after} 条，stock_basic 现有 {after} 条")
    print("完成")


if __name__ == "__main__":
    run()
