"""
从 daily_price 剔除指数行情

背景：
  指数行情已迁移到独立的 index_daily_price 表（见 sync_index_daily.py），
  daily_price 应只保留个股行情。本脚本删除 daily_price 中的指数行（sz.399xxx）。

前置守卫（关键，防数据真空）：
  删除前校验 index_daily_price 已建好且 sz.399001 有覆盖到近期的数据，
  不满足则拒绝删除——避免"指数还没迁好就把 daily_price 里的删了"。

执行顺序（重要）：
  本脚本剔除 daily_price 的指数后，必须重跑 rebuild_factor_pytdx.py（全量重建），
  才能让 adjust_factor_tushare / daily_price_qfq 也清掉指数（它们以 daily_price 为驱动重建）。

用法：
  python prune_index_from_daily.py            # 直接删除（含前置守卫）
  python prune_index_from_daily.py --dry-run  # 只统计不删除

一次性迁移动作，不纳入 daily_update.py。
"""

import sys

from common import db

# 指数行情前缀（本库指数全为 sz.399xxx）
PRUNE_WHERE = "code LIKE 'sz.399%'"
# 前置守卫检查用的样本指数
CHECK_CODE = "sz.399001"


def _guard(con):
    """前置守卫：确认 index_daily_price 已就绪，返回 (ok, 说明)。"""
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables"
    ).fetchall()}
    if "index_daily_price" not in tables:
        return False, "index_daily_price 表不存在，请先运行 sync_index_daily.py --full"

    n = con.execute(
        "SELECT COUNT(*) FROM index_daily_price WHERE code = ?", [CHECK_CODE]
    ).fetchone()[0]
    if n == 0:
        return False, f"index_daily_price 中无 {CHECK_CODE} 数据，指数尚未迁移完成"

    # 校验覆盖到近期：新表最新日期应 >= daily_price 个股最新日期附近
    idx_last = con.execute(
        "SELECT MAX(date) FROM index_daily_price WHERE code = ?", [CHECK_CODE]
    ).fetchone()[0]
    stk_last = con.execute(
        "SELECT MAX(date) FROM daily_price WHERE code NOT LIKE 'sz.399%'"
    ).fetchone()[0]
    if idx_last < stk_last:
        return False, (f"index_daily_price 最新 {idx_last} 落后于个股最新 {stk_last}，"
                       f"请先运行 sync_index_daily.py 补到最新")
    return True, f"index_daily_price 就绪（{CHECK_CODE} 最新 {idx_last}）"


def run():
    dry = "--dry-run" in sys.argv
    con = db.connect()
    print("从 daily_price 剔除指数行情" + ("（dry-run，不实际删除）" if dry else ""))

    n = con.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE {PRUNE_WHERE}"
    ).fetchone()[0]
    n_code = con.execute(
        f"SELECT COUNT(DISTINCT code) FROM daily_price WHERE {PRUNE_WHERE}"
    ).fetchone()[0]
    print(f"  待剔除指数行情: {n} 条（{n_code} 个指数）")

    if dry:
        ok, msg = _guard(con)
        print(f"  前置守卫检查: {'通过' if ok else '不通过'} — {msg}")
        con.close()
        print("dry-run 结束，未删除任何数据")
        return

    # 前置守卫
    ok, msg = _guard(con)
    if not ok:
        con.close()
        print(f"  ❌ 前置守卫拒绝删除: {msg}")
        sys.exit(1)
    print(f"  前置守卫通过: {msg}")

    # 剔除前记录个股行数（自检：不应被误删）
    stock_before = con.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE NOT ({PRUNE_WHERE})"
    ).fetchone()[0]

    con.execute(f"DELETE FROM daily_price WHERE {PRUNE_WHERE}")

    stock_after = con.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE NOT ({PRUNE_WHERE})"
    ).fetchone()[0]
    idx_left = con.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE {PRUNE_WHERE}"
    ).fetchone()[0]

    con.close()

    print(f"  已剔除 {n} 条指数行情，daily_price 中指数剩余 {idx_left}（应为0）")
    if stock_before == stock_after:
        print(f"  ✅ 个股行情未受影响：{stock_after} 条（删除前后一致）")
    else:
        print(f"  ⚠️ 个股行数异常：删除前 {stock_before} → 删除后 {stock_after}，请检查！")
    print("\n  ⚠️ 下一步：运行 python rebuild_factor_pytdx.py 全量重建，")
    print("     让 adjust_factor_tushare / daily_price_qfq 也清掉指数。")
    print("完成")


if __name__ == "__main__":
    run()
