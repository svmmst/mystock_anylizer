"""
指数基础信息一次性铺底 — index_basic 作为「指数同步清单」的权威来源

背景：
  指数集合几乎不变，没必要每天从 Tushare 同步 index_basic（还受 1次/小时限流）。
  改为用本脚本一次性把「要同步行情的全部指数」灌入 index_basic 表，之后
  sync_index_daily.py 的指数清单以 index_basic 为准（其取数来源1即 index_basic）。
  需要增删指数时，改本脚本的清单再重跑即可，不进日常 daily_update 链。

清单来源：
  1. 深市 286 个指数：code 取自库里 index_daily_price 现有 sz.399xxx，
     name 从 pytdx get_security_list(0, ...) 拉取（实测 286/286 全部能拿到）。
  2. 上证 8 个宏基指数：code + name 硬编码（沪市指数 pytdx 证券列表拿不到，
     但这 8 个名称已知，且择时/回测只用这几个上证真身）。

字段策略：只填 code / name / ts_code，其余（fullname/market/publisher/... /desc）
  留 NULL —— 择时只需 code 清单，扩展字段无用途；ts_code 由 code 反推生成。

用法：
  python seed_index_basic.py            # 全量刷新 index_basic（清空后重灌）
  python seed_index_basic.py --dry-run  # 只统计不写库
"""

import sys
import time

import pandas as pd

from common import db
from rebuild_factor_pytdx import _connect_tdx

# pytdx 市场号：深市证券列表用 market=0
TDX_MARKET_SZ = 0

# 上证 8 个宏基指数（code, name）——沪市指数 pytdx 证券列表拿不到，故硬编码。
# 这些是择时/回测实际使用的上证真身指数。
SH_INDEXES = [
    ("sh.000001", "上证综指"),
    ("sh.000300", "沪深300"),
    ("sh.000905", "中证500"),
    ("sh.000016", "上证50"),
    ("sh.000852", "中证1000"),
    ("sh.000688", "科创50"),
    ("sh.000010", "上证180"),
    ("sh.000009", "上证380"),
]


def _code_to_ts_code(code):
    """本地格式 code 反推 Tushare 格式 ts_code。

    sz.399006 -> 399006.SZ，sh.000300 -> 000300.SH
    """
    prefix, num = code.split(".")
    return f"{num}.{prefix.upper()}"


def fetch_sz_names(api, sz_codes):
    """翻页拉深市证券列表，返回 {纯数字code: name}，只保留 sz_codes 里的。

    sz_codes: 形如 {'399001', '399006', ...} 的纯数字代码集合。
    """
    wanted = set(sz_codes)
    names = {}
    total = api.get_security_count(TDX_MARKET_SZ)
    start = 0
    while start < total + 1000:
        lst = api.get_security_list(TDX_MARKET_SZ, start)
        if not lst:
            break
        for x in lst:
            if x["code"] in wanted:
                names[x["code"]] = x["name"]
        if len(names) == len(wanted):
            break
        start += 1000
    return names


def build_rows(con, api):
    """组装 index_basic 的全部行（深市 286 + 上证 8），返回 DataFrame。"""
    # 1. 深市指数 code（本地格式），取自库里现有行情
    sz_local_codes = [
        r[0] for r in con.execute(
            "SELECT DISTINCT code FROM index_daily_price "
            "WHERE code LIKE 'sz.%' ORDER BY code"
        ).fetchall()
    ]
    # 纯数字 code -> 本地 code 的映射，便于回填名称
    num_to_local = {c.split(".")[1]: c for c in sz_local_codes}

    # 2. pytdx 拉深市名称
    print(f"  拉取深市 {len(sz_local_codes)} 个指数名称（pytdx）...")
    sz_names = fetch_sz_names(api, num_to_local.keys())
    missing = set(num_to_local) - set(sz_names)
    if missing:
        # 拿不到名称的极少数指数：name 留空，不中断
        print(f"  ⚠️ {len(missing)} 个深市指数未取到名称，name 留空：{sorted(missing)[:10]}")

    rows = []
    # 深市行
    for num, local in num_to_local.items():
        raw_name = sz_names.get(num)
        # pytdx 少数名称含空格（如“中证 500”“300 医药”），清洗为紧凑中文名
        clean_name = raw_name.replace(" ", "").replace("　", "") if raw_name else None
        rows.append({
            "code": local,
            "ts_code": _code_to_ts_code(local),
            "name": clean_name,  # 取不到则 None
            "fullname": None, "market": None, "publisher": None,
            "index_type": None, "category": None, "base_date": None,
            "base_point": None, "list_date": None, "weight_rule": None,
            "desc": None,
        })
    # 上证行（硬编码）
    for code, name in SH_INDEXES:
        rows.append({
            "code": code,
            "ts_code": _code_to_ts_code(code),
            "name": name,
            "fullname": None, "market": None, "publisher": None,
            "index_type": None, "category": None, "base_date": None,
            "base_point": None, "list_date": None, "weight_rule": None,
            "desc": None,
        })

    return pd.DataFrame(rows)


def refresh(con, df):
    """全量刷新 index_basic：清空后整表重写（参考 sync_index_basic.py 的 refresh）。"""
    con.register("tmp_seed_idx", df)
    con.execute("DELETE FROM index_basic")
    # desc 是 SQL 保留字，需双引号转义；列顺序与 df 对齐
    cols = ["code", "ts_code", "name", "fullname", "market", "publisher",
            "index_type", "category", "base_date", "base_point",
            "list_date", "weight_rule", "desc"]
    col_list = ", ".join(f'"{c}"' if c == "desc" else c for c in cols)
    con.execute(f"INSERT INTO index_basic ({col_list}) "
                f"SELECT {col_list} FROM tmp_seed_idx")
    con.unregister("tmp_seed_idx")


def run():
    t_start = time.time()
    dry = "--dry-run" in sys.argv
    print("指数基础信息一次性铺底（index_basic 作为同步清单权威源）")

    con = db.connect()
    db.init_index_basic(con)
    api = _connect_tdx()

    df = build_rows(con, api)
    api.disconnect()

    print(f"  组装完成：共 {len(df)} 条（深市 {len(df[df['code'].str.startswith('sz.')])} "
          f"+ 上证 {len(df[df['code'].str.startswith('sh.')])}）")

    if dry:
        print("  [dry-run] 不写库。抽样：")
        for _, r in df.head(3).iterrows():
            print(f"    {r['code']}  {r['ts_code']}  {r['name']}")
        for code, _ in SH_INDEXES[:3]:
            r = df[df["code"] == code].iloc[0]
            print(f"    {r['code']}  {r['ts_code']}  {r['name']}")
        con.close()
        return

    refresh(con, df)

    total = con.execute("SELECT COUNT(*) FROM index_basic").fetchone()[0]
    print(f"  index_basic 现有 {total} 条")
    print("  抽样核对（深市名称来自 pytdx，上证为硬编码）：")
    for code in ["sz.399001", "sz.399006", "sh.000001", "sh.000300", "sh.000905"]:
        r = con.execute(
            "SELECT code, ts_code, name FROM index_basic WHERE code = ?", [code]
        ).fetchone()
        print("   ", r)

    con.close()
    print(f"完成，耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    run()
