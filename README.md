# mystock_anylizer

A 股量化数据下载、复权与选股工具。使用 DuckDB 本地存储,多数据源协作:
**Tushare Pro**(日线增量)、**pytdx / 通达信**(复权因子)、**Baostock**(全量铺底与验证)。
支持日线增量更新、前复权计算、股票基础信息与财务数据同步,并内置选股与回测脚本。

> 日常操作、脚本参数、复权原理等**完整手册**见 [`CLAUDE.md`](CLAUDE.md)。本文只讲「是什么」与「快速上手」。

## 快速开始

```bash
pip install -r requirements.txt
```

每个交易日收盘后(建议 16:00 之后,Tushare 当日日线齐全),一键更新:

```bash
python daily_update.py
```

该命令按顺序串联四步:

1. **同步指数基础信息**(Tushare index_basic)—— 非关键步骤,失败/跳过不影响后续
2. **同步股票基础信息**(名称/行业等,Tushare)—— 非关键步骤,失败/跳过不影响后续
3. **增量更新日线数据**(不复权,Tushare)—— 关键步骤
4. **增量更新复权因子 + 重建前复权表**(pytdx)—— 关键步骤

前两步带 1 小时限流自保护(Tushare index_basic / stock_basic 接口各限 1 次/小时,
用独立时间戳文件),距上次成功不足 1 小时会自动跳过。第 3、4 步有依赖关系(因子依赖日线),
任一失败即中止。

## 数据源

| 数据源 | 优势 | 劣势 | 用途 |
|--------|------|------|------|
| Baostock | 免费无 Key,稳定 | 逐股查询慢 | 日线全量铺底、验证对比 |
| Tushare Pro | 全市场单次请求 | 低积分有频率限制 | 日线增量更新、基础信息 |
| pytdx(通达信) | 无频率限制,5 分钟全市场 | 除权数据偶有虚假事件 | 复权因子计算 |

## 安装

```bash
pip install -r requirements.txt
```

| 依赖 | 用途 |
|------|------|
| `tushare>=1.4` | 日线增量、股票基础信息 |
| `pytdx` | 复权因子(通达信数据) |
| `baostock>=0.8.8` | 日线全量铺底与验证 |
| `duckdb>=1.0.0` | 本地嵌入式数据库,存储所有数据 |
| `pandas>=2.0.0` | 数据处理与清洗 |

Tushare token 配置在 `common/config.py` 的 `TUSHARE_TOKEN`。

## 常用脚本

完整清单见 [`CLAUDE.md`](CLAUDE.md),以下为高频入口:

| 脚本 | 用途 |
|------|------|
| `daily_update.py` | **推荐** 收盘后一键更新(基础信息 + 日线 + 因子) |
| `update_daily_price_v3.py` | 增量更新日线(Tushare) |
| `rebuild_factor_pytdx.py --update` | 增量更新复权因子 + 重建前复权表(pytdx) |
| `rebuild_daily_price.py` | 全量重建日线(Baostock,2-3 小时) |
| `sync_stock_basic.py` | 同步个股基础信息(名称/行业等,Tushare) |
| `sync_index_basic.py` | 同步指数基础信息到 index_basic 表(Tushare) |
| `sync_financials_by_quarter.py` | 季度财务数据同步 |
| `tech_screen.py` / `daily_pick.py` | 技术选股 / 每日选股 |
| `backtest_strategy.py` / `market_regime.py` | 回测策略 / 市场状态判断 |

## 数据库表结构

数据库文件为 `stock.db`(DuckDB,体积 GB 级,已加入 `.gitignore` 不纳入版本管理)。

| 表名 | 说明 | 主键 |
|------|------|------|
| `daily_price` | 不复权日线 OHLCV | (code, date) |
| `daily_price_qfq` | 前复权日线(由因子计算生成) | 无(CREATE TABLE AS) |
| `adjust_factor_tushare` | 每日复权因子 | 无 |
| `xdxr_events` | 除权除息事件记录(增量对比基准) | (code, date) |
| `stock_basic` | **个股**基础信息(代码/名称/行业/市场/上市日期/type) | 无 |
| `index_basic` | **指数**基础信息(Tushare index_basic 全字段) | code |
| `financials_raw` | 季度财务数据 | (code, report_date) |

### stock_basic 关键字段(仅个股)

| 字段 | 说明 |
|------|------|
| `code` | 本地格式代码(如 `sz.300154`),项目内所有表统一以此为准 |
| `name` | 股票中文名称 |
| `industry` / `market` / `list_date` | 所属行业 / 市场类型 / 上市日期 |
| `type` | 恒为 `stock`(指数已迁至 index_basic 表,index 分类仅作兜底) |

> 指数已从 stock_basic 剔除并迁至 `index_basic` 表,`common.db.get_stock_codes(con)`
> 默认只返回个股。注意:指数的**行情**仍保留在 `daily_price` / `daily_price_qfq`
> (供 `market_regime.py`、`daily_pick.py` 择时使用),只是基础信息不在 stock_basic。

## 复权因子计算原理

```
前复权公式: qfq_price = raw_price × factor / latest_factor
```

其中 `factor` 是该股票该日的累积复权因子,`latest_factor` 是其最新交易日的因子(锚点)。
每次发生分红、配股等除权除息事件,历史因子不变,只是 `latest_factor` 增大,历史前复权价格
会整体调整——这是前复权的数学特性,非 bug。因此每有新除权事件都需重算,`daily_update.py`
的第 3 步已自动处理。详细的增量更新逻辑见 [`CLAUDE.md`](CLAUDE.md)。

## 配置参数

集中在 `common/config.py`:

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DB_PATH` | `stock.db` | 数据库文件路径 |
| `TUSHARE_TOKEN` | — | Tushare Pro API token |
| `SLEEP_RANGE` | (0.8, 1.8) | Baostock 下载随机间隔范围(秒) |
| `RETRY` | 3 | 失败重试次数 |
| `BATCH_INSERT` | 200 | 批量写入条数 |
| `BATCH_SIZE` | 100 | 全量下载批次大小 |

## 注意事项

- 数据库文件 `stock.db` 体积较大(GB 级),已加入 `.gitignore`,不会提交到版本管理。
- Tushare 低积分 token 对 `adj_factor` / `pro_bar` 有频率限制;日线增量用 `daily` 接口无此问题,
  复权因子改用 pytdx 规避限制。
- pytdx 极少数股票(<3%)有未实施的虚假除权记录,导致历史前复权价格与 Baostock 有偏差,
  近年数据不受影响。详见 [`CLAUDE.md`](CLAUDE.md) 的「已知问题」。
- 全量重建日线约需 2-3 小时,建议在网络稳定时运行;日常只需 `daily_update.py` 增量更新。
