# mystock_anylizer

A 股量化数据下载与复权工具。基于 [baostock](http://baostock.com) 免费数据源，使用 DuckDB 存储，支持全量下载、增量更新、前复权计算和财务数据同步。

## 项目结构

```
.
├── common/
│   ├── config.py              # 数据库路径、请求间隔、重试次数等配置
│   ├── baostock_client.py     # baostock 封装：登录、股票列表、K线、财务数据
│   └── db.py                  # DuckDB 操作：建表、批量写入（去重）、数据校验
├── full_a_share_download.py   # 全量 A 股日线数据下载（前复权）
├── update_daily_price.py      # 日线数据增量更新（获取最新交易日数据）
├── build_qfq_v5.py            # 前复权价格计算系统（不复权 → 前复权）
├── sync_financials_by_quarter.py  # 季度财务数据同步（营收、净利润、ROE 等）
└── requirements.txt           # Python 依赖
```

## 数据源

所有行情与财务数据均来自 [baostock](http://baostock.com/) —— 一个免费、无需 API Key 的 A 股数据源。

## 安装

```bash
pip install -r requirements.txt
```

| 依赖 | 用途 |
|------|------|
| `baostock>=0.8.8` | 行情与财务数据下载 |
| `duckdb>=1.0.0` | 本地嵌入式数据库，存储所有数据 |
| `pandas>=2.0.0` | 数据处理与清洗 |

## 数据库表结构

数据库文件为 `stock.db`（本地文件，不纳入版本管理）。

### daily_price（日线行情 — 前复权）

| 列 | 类型 | 说明 |
|----|------|------|
| code | VARCHAR | 股票代码，如 `sh.600000` |
| date | DATE | 交易日 |
| open/high/low/close | DOUBLE | 开/高/低/收（前复权价格） |
| volume | DOUBLE | 成交量 |
| amount | DOUBLE | 成交额 |

主键：`(code, date)`

### daily_price_qfq（日线行情 — 前复权）

由 `build_qfq_v5.py` 生成，结构与 `daily_price` 相同，但价格已通过复权因子转换为前复权。

### financials_raw（季度财务数据）

| 列 | 类型 | 说明 |
|----|------|------|
| code | VARCHAR | 股票代码 |
| report_date | DATE | 报告期 |
| pub_date | DATE | 发布日期 |
| revenue | DOUBLE | 营业收入 |
| net_profit | DOUBLE | 净利润 |
| roe | DOUBLE | 净资产收益率（ROE） |
| gross_margin | DOUBLE | 毛利率 |
| net_margin | DOUBLE | 净利率 |
| revenue_yoy | DOUBLE | 营业收入同比增长率 |
| net_profit_yoy | DOUBLE | 净利润同比增长率 |

### adjust_factor / adjust_factor_daily

复权因子表，由 `build_qfq_v5.py` 内部使用，用于将不复权价格换算为前复权价格。

## 使用指南

### 第一步：全量下载日线数据

下载全部 A 股的历史日线数据（约 5000+ 只股票，耗时较长）：

```bash
python full_a_share_download.py
```

- 数据范围：2020-01-01 至今
- 下载的是**前复权**数据（`adjustflag="2"`）
- 每批 100 只股票，批次间间隔 5 秒，单次请求间隔 0.8–1.8 秒（可在 `common/config.py` 调整）

### 第二步：增量更新日线

定期运行，获取每只股票自上次下载以来的新交易日数据：

```bash
python update_daily_price.py
```

- 下载**不复权**数据（`adjustflag="3"`）
- 自动跳过已是最新数据的股票
- 写入前会按 `(code, date)` 去重

### 第三步：生成前复权价格

基于不复权的日线数据 + baostock 复权因子，计算出前复权价格：

```bash
python build_qfq_v5.py
```

计算流程：
1. 下载每只股票的复权因子序列
2. 将复权因子展开到每个交易日（`adjust_factor_daily` 表）
3. 按公式 `价格 × 当日因子 ÷ 最新因子` 生成前复权数据（`daily_price_qfq` 表）
4. 校验行数一致性，检查异常值

### 第四步：同步财务数据

按季度同步财务指标：

```bash
python sync_financials_by_quarter.py
```

- 目标年份/季度在脚本内修改 `TARGET_YEAR` / `TARGET_QUARTER`
- 自动跳过已有该季度数据的股票，仅下载缺失部分
- 批量写入（每 200 条一批），按 `(code, report_date)` 去重

## 配置参数

所有配置集中在 `common/config.py`：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| DB_PATH | `stock.db` | 数据库文件路径 |
| SLEEP_SEC | 0.05 | 财务数据请求间隔（秒） |
| SLEEP_RANGE | (0.8, 1.8) | 日线下载随机间隔范围（秒） |
| RETRY | 3 | 失败重试次数 |
| BATCH_INSERT | 200 | 批量写入条数 |
| BATCH_SIZE | 100 | 全量下载批次大小 |

## 数据流程图

```
baostock 数据源
      │
      ├─ query_history_k_data_plus() ──► daily_price（前复权/不复权）
      │                                       │
      │                              build_qfq_v5.py
      │                                       │
      │                              adjust_factor（复权因子）
      │                                       │
      │                              adjust_factor_daily（日频展开）
      │                                       │
      │                              daily_price_qfq（前复权价格）
      │
      ├─ query_profit_data() ──┬──► financials_raw（季度财务数据）
      └─ query_growth_data() ──┘
```

## 注意事项

- 数据库文件 `stock.db` 体积较大（GB 级别），已加入 `.gitignore`，不会提交到版本管理
- baostock 有请求频率限制，已内置随机间隔和重试机制，请勿移除
- 全量下载 5000+ 只股票约需 2–3 小时，建议在网络稳定时运行
- 增量更新建议每个交易日收盘后运行一次
