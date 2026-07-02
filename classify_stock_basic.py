"""
stock_basic 代码分类 — 区分个股 / 指数

背景：
  本脚本给 stock_basic 打 type 标签区分个股/指数。

  注意：指数基础信息已迁至独立的 index_basic 表，stock_basic 中的指数记录也已
  由 prune_index_from_stock_basic.py 剔除，因此正常情况下本表不再含指数（index=0）。
  但本脚本的 index 分类规则保留作“兜底护栏”——万一 Tushare stock_basic 未来意外
  返回带指数号段的 code，仍会被正确标成 index 而不会混进个股。
  它主要由 sync_stock_basic.py 在同步后调用，给新 INSERT 的个股补 type='stock'，
  保证 get_stock_codes() 不漏新股。指数的“行情”仍留在 daily_price/daily_price_qfq 供择时用。

分类规则（与 market_regime.py 的既有惯例保持一致）：
  指数号段：sh.000%、sz.399%、sh.880%  -> type = 'index'（兜底）
  其余                                  -> type = 'stock'

用法：
  python classify_stock_basic.py

幂等：可反复运行，每次按当前规则重算 type。
"""

from common import db

# 指数号段前缀（来自 market_regime.py:165-167）
INDEX_PREFIXES = ["sh.000", "sz.399", "sh.880"]


def ensure_type_column(con):
    """幂等地为 stock_basic 补充 type 列。"""
    cols = {row[0] for row in con.execute("DESCRIBE stock_basic").fetchall()}
    if "type" not in cols:
        con.execute("ALTER TABLE stock_basic ADD COLUMN type VARCHAR")
        print("  新增列: type VARCHAR")


def classify(con):
    """按号段规则回填 type 列。"""
    like_clause = " OR ".join(f"code LIKE '{p}%'" for p in INDEX_PREFIXES)
    con.execute(f"UPDATE stock_basic SET type = 'index' WHERE {like_clause}")
    con.execute(f"UPDATE stock_basic SET type = 'stock' WHERE NOT ({like_clause})")


def run():
    con = db.connect()
    print("stock_basic 代码分类（个股 / 指数）")

    ensure_type_column(con)
    classify(con)

    stock_n = con.execute("SELECT COUNT(*) FROM stock_basic WHERE type='stock'").fetchone()[0]
    index_n = con.execute("SELECT COUNT(*) FROM stock_basic WHERE type='index'").fetchone()[0]
    print(f"  个股(stock): {stock_n}")
    print(f"  指数(index): {index_n}")

    print("  指数样例:")
    for r in con.execute(
        "SELECT code, name FROM stock_basic WHERE type='index' LIMIT 5"
    ).fetchall():
        print("   ", r)

    con.close()
    print("完成")


if __name__ == "__main__":
    run()
