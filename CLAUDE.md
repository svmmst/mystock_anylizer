# mystock_anylizer 使用手册

## 日常操作流程

每个交易日收盘后（建议 16:00 之后，Tushare 当日日线齐全），一键执行：

```bash
python daily_update.py
```

该脚本按顺序串联执行以下五步：

```bash
# 1. 同步股票基础信息（名称/行业等，Tushare）——非关键步骤
python sync_stock_basic.py --if-stale

# 2. 增量更新日线数据（不复权，Tushare）——关键步骤
python update_daily_price_v3.py

# 3. 增量更新复权因子 + 重建前复权表（pytdx）——关键步骤
python rebuild_factor_pytdx.py --update

# 4. 增量更新指数行情（pytdx）——非关键步骤
python sync_index_daily.py

# 5. 交叉验证指数行情（与 baostock 对比）——非关键步骤
python verify_index_baostock.py
```

也可单独手动执行上述各步。

**中断规则**：

- 第 1 步为**非关键步骤**，失败或跳过都不中止，继续执行行情/因子更新
  （行情本身不依赖 stock_basic，新股一上市当天就会进 daily_price）。
- 第 2、3 步为**关键步骤**，有依赖关系（因子依赖日线），顺序不可颠倒，任一失败即中止。
- 第 4 步更新指数行情（pytdx 无限流），与个股因子链无依赖，**非关键**：指数只服务
  择时，失败可次日补，不应中断个股主链。
- 第 5 步交叉验证指数行情（与 baostock 抽样对比），**非关键**：指数数据重要，每次更新后
  自动体检防脏数据误导。验证失败只告警不中断（更新已完成），需人工核查。

**指数基础信息（index_basic）不进日常链**：指数集合几乎不变，无每日同步的意义（还受
Tushare 1次/小时限流）。index_basic 现作为**「指数同步清单」的权威源**，由
`seed_index_basic.py` 一次性铺底（286 深市指数名称来自 pytdx + 8 个上证宏基指数硬编码），
需增删指数时手动重跑该脚本。`sync_index_daily.py` 的指数清单以 index_basic 为准。

**为何把个股基础信息放最前**：让新代码信息先就位，避免出现「daily_price 有行情但
stock_basic 查不到名称」的窗口。

**限流自保护**（`--if-stale`）：Tushare `stock_basic` 接口限 **1 次/小时**。脚本用时间戳文件
（`.stock_basic_last_sync`）记录上次成功时间，距上次不足 1 小时则直接打印提示并跳过（退出码 0）。
即便真撞上限流，脚本也会捕获并以跳过处理，不影响后续行情与因子更新。

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

### 指数管理（与个股彻底分离）

指数的**基础信息**和**行情**都已从个股表分离，各自独立成表：

| 脚本 | 用途 | 数据源 | 耗时 |
|------|------|--------|------|
| `sync_stock_basic.py` | 同步全市场**个股**基础信息到 stock_basic 表；`--if-stale` 距上次成功不足 1 小时则跳过 | Tushare Pro | 几秒（接口限1次/小时） |
| `seed_index_basic.py` | **推荐** 一次性铺底 index_basic（指数同步清单权威源）：286 深市指数名称从 pytdx 拉取 + 8 个上证宏基指数硬编码，只填 code/name/ts_code；`--dry-run` 只统计不写 | pytdx（通达信） | <1秒 |
| `sync_index_basic.py` | 备用：从 Tushare 同步指数基础信息到 index_basic（全字段）。**不进日常链**（指数集合稳定，无每日同步意义，且受限流） | Tushare Pro | 几秒（接口限1次/小时） |
| `sync_index_daily.py` | 同步**指数行情**到 index_daily_price 表（含涨跌家数）；清单以 index_basic 为准；默认增量，`--full` 全量翻页重灌 | pytdx（通达信） | 增量秒级，全量约12分钟 |
| `classify_stock_basic.py` | 给 stock_basic 打 type 标签（个股/指数），index 规则现仅作兜底护栏 | 本地SQL | <1秒 |
| `prune_stock_basic.py` | 清理空壳代码（无名称且两张行情表均无数据），`--dry-run` 只统计不删 | 本地SQL | <1秒 |
| `prune_index_from_stock_basic.py` | 一次性迁移：从 stock_basic 剔除指数（`type='index'`），不动行情表 | 本地SQL | <1秒 |
| `prune_index_from_daily.py` | 一次性迁移：从 daily_price 剔除指数行情（带前置守卫，确认 index_daily_price 已就绪才删） | 本地SQL | 秒级 |

> - **指数行情用 pytdx（无频率限制），不用 Tushare**（Tushare index_daily 限流 1次/小时不可行）。
>   pytdx 指数量纲：`volume = pytdx vol × 10000`（与个股 daily_price 口径一致），`amount` 已是元。
> - 指数行情已从 `daily_price` / `daily_price_qfq` **剥离**，只在 `index_daily_price`。
>   `market_regime.py` / `daily_pick.py` / `backtest_*` 均已改为从 index_daily_price 读指数。
> - 指数不分红不除权，**无复权概念**（因子恒为 1.0），故指数行情不进因子/前复权流程。
> - `market_regime.py` 的创业板指已由旧代码 sz.399003 修正为正确的 **sz.399006**（数据自 2010 年起）。
>   `backtest_with_regime.py` / `backtest_dynamic.py` 曾遗留同一处 sz.399003 错误（老指数非创业板），
>   已一并修正为 sz.399006，三处择时逻辑现使用一致的指数集合。

**择时指数代码 ↔ 中文名对照**（`market_regime.py` / `backtest_*` 统一使用）：

| 代码 | 中文名 | 说明 |
|------|--------|------|
| `sz.399001` | 深证成指 | 深市综合，趋势/量能维度 |
| `sz.399006` | 创业板指 | 成长风格代表，数据自 2010 年 |
| `sh.000300` | 沪深300 | 大盘蓝筹（上证官方真身），趋势/量能/风险偏好维度，数据自 2005 年 |
| `sh.000905` | 中证500 | 中盘（上证官方真身），数据自 2007 年 |

> - **风险偏好维度**（`market_regime.py`）由「创业板 vs 深证成指」改为「创业板 vs 沪深300」：
>   前者相关性高达 0.96（同涨跌、信号弱），后者 0.91、分化明显，才是有效的「成长 vs 价值」信号。
> - **趋势/量能维度**已纳入沪深300，使市场判断覆盖大盘蓝筹，不再只看深市成长股。
> - 择时已由**深市镜像码升级为上证官方真身**（沪深300 sz.399300→sh.000300、中证500 sz.399905→sh.000905）。
>   8 个上证宏基指数（含上证综指/上证50/中证1000/科创50 等）已补入 `index_daily_price`，
>   由 `seed_index_basic.py` 纳入 index_basic 清单、每日自动跟新。深市镜像 sz.399300/sz.399905 行情
>   保留在表里（不删），但不再用于择时。

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
| `verify_index_baostock.py` | 与 Baostock 交叉验证指数行情（index_daily_price），`--full` 深度核对；daily_update 第 6 步自动调用 |
| `fix_factor_anomaly.py` | 修复因子异常 |

---

## 数据库结构

数据库文件：`stock.db`（DuckDB 格式）

| 表名 | 说明 | 主键 |
|------|------|------|
| `daily_price` | 不复权日线 OHLCV（**仅个股**） | (code, date) |
| `daily_price_qfq` | 前复权日线（由因子计算生成，**仅个股**） | 无（CREATE TABLE AS） |
| `adjust_factor_tushare` | 每日复权因子（**仅个股**） | 无 |
| `xdxr_events` | 除权除息事件记录（增量对比基准） | (code, date) |
| `financials_raw` | 季度财务数据 | (code, report_date) |
| `stock_basic` | **个股**基础信息（代码/名称/行业/市场/上市日期/type） | 无 |
| `index_basic` | **指数**基础信息（Tushare index_basic 全字段，全量刷新） | code |
| `index_daily_price` | **指数**日线行情（pytdx，OHLCV+amount+涨跌家数） | (code, date) |

### stock_basic 表字段（仅个股）

| 字段 | 说明 |
|------|------|
| `code` | 本地格式代码（如 sz.300154），项目内所有表统一以此为准 |
| `name` | 股票中文名称 |
| `area` / `industry` / `market` | 地域 / 所属行业 / 市场类型 |
| `list_date` / `list_status` / `delist_date` | 上市日期 / 上市状态 / 退市日期 |
| `type` | 恒为 `stock`（指数已迁出，`index` 分类仅作兜底护栏） |

> 指数已从 stock_basic **剔除**并迁至 `index_basic` 表。`common.db.get_stock_codes(con)`
> 默认只返回 `type='stock'` 的个股。注意：指数的**行情**仍保留在 `daily_price` /
> `daily_price_qfq`（供 `market_regime.py`、`daily_pick.py` 择时用），只是基础信息不在 stock_basic。

### index_basic 表字段（指数）

| 字段 | 说明 |
|------|------|
| `code` | 本地格式代码（如 sz.399006），主键 |
| `ts_code` | Tushare 原始代码（如 399006.SZ） |
| `name` / `fullname` | 简称 / 全称（如 创业板指） |
| `market` / `publisher` | 市场（SZSE 等） / 发布方 |
| `index_type` / `category` | 指数风格 / 类别 |
| `base_date` / `base_point` | 基期 / 基点 |
| `list_date` / `weight_rule` / `desc` | 发布日期 / 加权方式 / 描述 |

### index_daily_price 表字段（指数行情）

| 字段 | 说明 |
|------|------|
| `code` / `date` | 指数代码 / 交易日，复合主键 |
| `open` / `high` / `low` / `close` | 开/高/低/收（指数点位，不复权） |
| `volume` / `amount` | 成交量（pytdx vol × 10000） / 成交额（元） |
| `up_count` / `down_count` | 指数专有：成分股当日上涨 / 下跌家数 |

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
