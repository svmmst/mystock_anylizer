"""
收盘后一键更新 — 日线 + 复权因子

按 CLAUDE.md 标准流程串联执行：
  1. update_daily_price_v3.py        增量更新日线（不复权，Tushare）
  2. rebuild_factor_pytdx.py --update 增量更新复权因子 + 重建前复权表（pytdx）

任一步失败立即中止，不再执行后续步骤（因子计算依赖日线，顺序不可颠倒）。

时效提醒：Tushare daily 接口约在交易日 15:30~16:00 后数据齐全，
建议交易日 16:00 之后再运行本脚本。

用法：
  python daily_update.py
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 脚本所在目录，保证无论从哪里调用都能定位到子脚本
BASE_DIR = Path(__file__).resolve().parent

# 按顺序执行的步骤：(描述, [命令参数], 是否关键步骤)
# - 第一步同步个股基础信息放最前，保证新代码信息先就位（行情本身不依赖它）；
#   标记为非关键（critical=False）：失败或跳过都不中止，继续后面的行情/因子更新。
#   带 --if-stale：距上次成功同步不足 1 小时则自动跳过，避免撞 Tushare 1次/小时限流。
# - 中间两步是核心行情+因子，有依赖关系（因子依赖日线），顺序不可颠倒，任一失败即中止。
# - 更新指数行情（pytdx，无限流），与个股因子链无依赖，非关键：
#   指数只服务择时，失败可次日补，不应中断个股主链。
# - 最后一步交叉验证指数行情（与 baostock 对比），非关键：指数数据重要，每次更新后
#   自动体检防脏数据误导；验证失败只告警不中断（更新已完成，需人工核查后处理）。
#
# 注：指数基础信息（index_basic）不再进日常链——指数集合几乎不变，无每日同步的意义。
#   index_basic 现作为「指数同步清单」的权威源，由 seed_index_basic.py 一次性铺底，
#   需增删指数时手动重跑该脚本即可。sync_index_daily.py 的清单以 index_basic 为准。
STEPS = [
    ("同步股票基础信息（名称/行业等，Tushare）", ["sync_stock_basic.py", "--if-stale"], False),
    ("增量更新日线数据（Tushare）", ["update_daily_price_v3.py"], True),
    ("增量更新复权因子 + 重建前复权表（pytdx）", ["rebuild_factor_pytdx.py", "--update"], True),
    ("增量更新指数行情（pytdx）", ["sync_index_daily.py"], False),
    ("交叉验证指数行情（baostock）", ["verify_index_baostock.py"], False),
]


def run_step(index, total, desc, args):
    """执行单个步骤，返回是否成功"""
    print("=" * 60)
    print(f"步骤 {index}/{total}: {desc}")
    print(f"命令: python {' '.join(args)}")
    print("=" * 60)

    # 用当前 Python 解释器运行，cwd 固定在项目目录（子脚本依赖相对路径 stock.db）
    result = subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
    )
    return result.returncode == 0


def main():
    t_start = time.time()
    now = datetime.now()
    print(f"收盘后一键更新  {now:%Y-%m-%d %H:%M:%S}")

    # 收盘前运行的友好提醒（不阻断，仅提示）
    if now.weekday() < 5 and now.hour < 16:
        print("⚠️ 提示：当前可能早于 Tushare 当日数据齐全时间（约16:00），")
        print("   当天日线可能尚未生成，建议 16:00 之后再运行。")

    total = len(STEPS)
    for i, (desc, args, critical) in enumerate(STEPS, start=1):
        ok = run_step(i, total, desc, args)
        if not ok:
            if critical:
                print(f"\n❌ 步骤 {i}（{desc}）执行失败，已中止后续步骤。")
                print("   请检查上方报错；修复后可重新运行 python daily_update.py")
                sys.exit(1)
            # 非关键步骤失败：仅告警，继续后续（行情/因子更新不受影响）
            print(f"\n⚠️ 步骤 {i}（{desc}）未成功，但为非关键步骤，继续后续更新。\n")
            continue
        print(f"✅ 步骤 {i} 完成\n")

    print("=" * 60)
    print(f"🎉 全部完成，总耗时 {time.time() - t_start:.1f} 秒")


if __name__ == "__main__":
    main()
