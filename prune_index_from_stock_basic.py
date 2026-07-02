"""
从 stock_basic 剔除指数记录

背景：
  指数已迁移到独立的 index_basic 表（见 sync_index_basic.py），stock_basic 应
  名副其实只装个股。本脚本删除 stock_basic 中 type='index' 的记录（本库为 sz.399xxx）。

安全约束（重要）：
  只删除 stock_basic 表里的行，绝不触碰 daily_price / daily_price_qfq。
  指数的“行情”仍保留在两张行情表，供 market_regime.py / daily_pick.py 择时使用。
  脚本内建自检：删除前后校验 daily_price_qfq 中 sz.399001 的行数不变，确保行情未受影响。

用法：
  python prune_index_from_stock_basic.py            # 直接删除
  python prune_index_from_stock_basic.py --dry-run  # 只统计不删除

一次性迁移动作，不纳入 daily_update.py（每天跑只会删 0 行，无意义）。
"""

import sys

from common import db

# 剔除条件：type 列已由 classify_stock_basic.py 标好，比按 code 前缀更稳、可读
PRUNE_WHERE = "type = 'index'"

# 行情未受影响自检用的样本指数（择时脚本实际使用的代码）
CHECK_CODE = "sz.399001"


def _qfq_bars(con, code):
    """daily_price_qfq 中某代码的行数，用于删除前后对比。"""
    return con.execute(
        "SELECT COUNT(*) FROM daily_price_qfq WHERE code = ?", [code]
    ).fetchone()[0]


def run():
    dry = "--dry-run" in sys.argv
    con = db.connect()
    print("从 stock_basic 剔除指数" + ("（dry-run，不实际删除）" if dry else ""))

    n = con.execute(
        f"SELECT COUNT(*) FROM stock_basic WHERE {PRUNE_WHERE}"
    ).fetchone()[0]
    print(f"  待剔除指数记录: {n}")

    for r in con.execute(
        f"SELECT code FROM stock_basic WHERE {PRUNE_WHERE} LIMIT 5"
    ).fetchall():
        print("   ", r[0])

    if dry:
        con.close()
        print("dry-run 结束，未删除任何数据")
        return

    # 删除前记录行情基线（自检：行情表不应被本操作影响）
    bars_before = _qfq_bars(con, CHECK_CODE)

    before = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    con.execute(f"DELETE FROM stock_basic WHERE {PRUNE_WHERE}")
    after = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]

    # 删除后校验行情未受影响
    bars_after = _qfq_bars(con, CHECK_CODE)

    con.close()

    print(f"  已剔除 {before - after} 条，stock_basic 现有 {after} 条")
    if bars_before == bars_after and bars_before > 0:
        print(f"  ✅ 行情未受影响：daily_price_qfq 中 {CHECK_CODE} 行数 {bars_after}（删除前后一致）")
    else:
        print(f"  ⚠️ 行情行数异常：{CHECK_CODE} 删除前 {bars_before} → 删除后 {bars_after}，请检查！")
    print("完成")


if __name__ == "__main__":
    run()
