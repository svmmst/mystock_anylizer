# mystock_anylizer 使用手册

## 日常操作流程

每个交易日收盘后（建议 16:00 之后，Tushare 当日日线齐全），一键执行：

```bash
python daily_update.py
```

该脚本按顺序串联执行以下三步（前两步任一失败则中止，不再执行后续）：

```bash
# 1. 增量更新日线数据（不复权，Tushare）
python update_daily_price_v3.py

# 2. 增量更新复权因子 + 重建前复权表（pytdx）
python rebuild_factor_pytdx.py --update

# 3. 同步股票基础信息（名称/行业等，Tushare）
python sync_stock_basic.py
```

也可单独手动执行上述各步。

> 注：第 3 步与前两步无依赖，放在最后，即使失败也不影响核心的日线/因子更新。
> `sync_stock_basic.py` 依赖 Tushare `stock_basic` 接口，该接口限 **1 次/小时**，
> 一天多次运行 `daily_update.py` 时该步可能因限流报错，属正常现象，不影响行情与因子。

---

## 核心脚本说明

### 一键更新

| 脚本 | 用途 | 说明 |
|------|------|------|
| `daily_update.py` | **推荐** 收盘后一键更新（日线 + 复权因子） | 串联下面两步，第一步失败即中止 |

### 日线数据

| 脚本 | 用途 | 数据源 | 耗时 |
|------|------|--------|------|
| `rebuild_daily_price.py` | 全量重建 daily_price（清表重下） | Baostock | 2-3小时 |
| `update_daily_price_v3.py` | 增量更新日线（按交易日补全） | Tushare Pro | 几秒/天 |
| `full_a_share_download.py` | 全量下载（旧版，不清表） | Baostock | 2-3小时 |

### 复权因子与前复权

| 脚本 | 用途 | 数据源 | 耗时 |
|------|------|--------|------|
| `rebuild_factor_pytdx.py` | **推荐** 全量/增量复权因子 | pytdx（通达信） | 全量5分钟，增量5分钟 |
| `rebuild_factor_pytdx.py --update` | 增量更新（只重算有新除权事件的股票） | pytdx | ~5分钟 |
| `rebuild_factor_pytdx.py --qfq-only` | 仅重建前复权表（不重下因子） | 本地SQL | <30秒 |
| `build_qfq_v7.py` | 备选方案（Tushare因子） | Tushare Pro | 受频率限制 |
| `rebuild_adjust_factor.py` | 备选方案（Baostock因子） | Baostock多进程 | 10-20分钟 |

### 股票基础信息

| 脚本 | 用途 | 数据源 | 耗时 |
|------|------|--------|------|
| `sync_stock_basic.py` | 同步全市场基础信息（名称/行业/市场/上市日期等），补充到 stock_basic 表 | Tushare Pro | 几秒（接口限1次/小时） |
| `classify_stock_basic.py` | 给 stock_basic 打 type 标签（个股/指数），规则同 market_regime.py | 本地SQL | <1秒 |
| `prune_stock_basic.py` | 清理空壳代码（无名称且两张行情表均无数据），`--dry-run` 只统计不删 | 本地SQL | <1秒 |

> `sync_stock_basic.py` 会在同步后自动调用 `classify_stock_basic` 的分类逻辑，
> 新纳入的股票会立即带上 `type='stock'`，无需手动再跑分类。

### 财务数据

| 脚本 | 用途 |
|------|------|
| `sync_financials_by_quarter.py` | 季度财务数据同步（需手动改脚本中的目标季度） |

### 选股与回测

| 脚本 | 用途 |
|------|------|
| `tech_screen.py` | 技术指标选股 |
| `daily_pick.py` | 每日选股 |
| `backtest_strategy.py` | 回测策略 |
| `backtest_dynamic.py` | 动态回测 |
| `backtest_with_regime.py` | 带市场状态的回测 |
| `market_regime.py` | 市场状态判断 |

### 工具脚本

| 脚本 | 用途 |
|------|------|
| `verify_qfq.py` | 验证前复权数据质量 |
| `verify_qfq_baostock.py` | 与 Baostock 对比验证前复权准确性 |
| `fix_factor_anomaly.py` | 修复因子异常 |

---

## 数据库结构

数据库文件：`stock.db`（DuckDB 格式）

| 表名 | 说明 | 主键 |
|------|------|------|
| `daily_price` | 不复权日线 OHLCV | (code, date) |
| `daily_price_qfq` | 前复权日线（由因子计算生成） | 无（CREATE TABLE AS） |
| `adjust_factor_tushare` | 每日复权因子 | 无 |
| `xdxr_events` | 除权除息事件记录（增量对比基准） | (code, date) |
| `financials_raw` | 季度财务数据 | (code, report_date) |
| `stock_basic` | 股票基础信息（代码/名称/行业/市场/上市日期/type 等） | 无 |

### stock_basic 表字段

| 字段 | 说明 |
|------|------|
| `code` | 本地格式代码（如 sz.300154），项目内所有表统一以此为准 |
| `name` | 股票中文名称（指数及无数据代码可能为空） |
| `area` / `industry` / `market` | 地域 / 所属行业 / 市场类型 |
| `list_date` / `list_status` / `delist_date` | 上市日期 / 上市状态 / 退市日期 |
| `type` | `stock`（个股）或 `index`（指数：sh.000/sz.399/sh.880 号段） |

> `common.db.get_stock_codes(con)` 默认只返回 `type='stock'` 的个股，
> 传 `include_index=True` 返回全部（含指数）。指数（如 sz.399001 深证成指）
> 被 `market_regime.py`、`daily_pick.py` 用于择时，保留在表内不删除。

---

## 复权因子计算原理

```
前复权公式: qfq_price = raw_price × factor / latest_factor

其中：
- factor: 该股票该日的累积复权因子（从首个除权日 cumprod 得到）
- latest_factor: 该股票最新交易日的因子（锚点）
- 每次有新除权事件，历史因子不变，只是 latest_factor 增大
```

增量更新逻辑（`--update`）：
1. 下载全部股票除权事件（pytdx，~5分钟）
2. 与 `xdxr_events` 表对比，找出有新增除权事件的股票
3. 仅对受影响股票重算全历史因子
4. 未受影响的股票只需延伸最后一天的因子到新交易日
5. 全量重建 `daily_price_qfq`（SQL秒级完成）

---

## 数据源对比

| 数据源 | 优势 | 劣势 | 用途 |
|--------|------|------|------|
| Baostock | 免费无Key，稳定 | 逐股查询慢 | 日线全量铺底、验证对比 |
| Tushare Pro | 全市场单次请求 | 低积分频率限制（1次/分钟） | 日线增量更新 |
| pytdx（通达信） | 无频率限制，5分钟全市场 | 除权数据偶有虚假事件 | 复权因子计算 |

---

## 配置

`common/config.py` 中关键配置：
- `DB_PATH = "stock.db"` — 数据库路径
- `TUSHARE_TOKEN` — Tushare Pro API token
- `SLEEP_RANGE = (0.8, 1.8)` — Baostock 请求间隔

---

## 已知问题

1. **pytdx 虚假除权事件**：通达信数据中极少数股票（<3%）包含未实施的除权记录，导致这些股票的历史前复权价格与 Baostock 有偏差（最大~28%）。近年数据不受影响。
2. **Tushare 频率限制**：低积分 token 调用 `adj_factor` 限制 1次/分钟，`pro_bar` 更严。日线增量用 `daily` 接口无此问题。
3. **前复权基准变化**：每次除权事件后，历史前复权价格会整体调整（这是前复权的数学特性，非bug）。
