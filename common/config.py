# common/config.py

DB_PATH = "stock.db"

# Baostock 请求控制
SLEEP_SEC = 0.05          # 单次请求间隔（秒）
SLEEP_RANGE = (0.8, 1.8)  # 全量下载时的随机间隔范围
RETRY = 3                 # 失败重试次数
BATCH_INSERT = 200        # 批量写入条数
BATCH_SIZE = 100          # 全量下载批次大小

# 财务数据年份范围
FINANCIAL_START_YEAR = 2022
FINANCIAL_END_YEAR = 2025
