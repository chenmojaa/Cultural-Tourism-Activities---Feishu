# -*- coding: utf-8 -*-
"""config.py 的占位模板。把这份复制成 config.py 后填入真实值。

提示:
- 所有敏感字段(API_KEY / PASSWORD / SECRET / TOKEN)都通过 os.getenv 读取,
  支持环境变量注入,推荐用 .env 或部署平台的 secret manager 管理,
  而不是把真实值直接写进 config.py。
- 如果用环境变量,直接命令行 export 后再运行 python main.py 即可。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

PROVIDER = os.getenv("WENGLU_PROVIDER", "qwen")

# === 千问 / 通义 ===
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "sk-your-api-key-here")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.6-max-preview")
QWEN_ENABLE_SEARCH = os.getenv("QWEN_ENABLE_SEARCH", "1") == "1"
QWEN_DASHSCOPE_BASE_URL = os.getenv("WENGLU_DASHSCOPE_BASE_URL", "")

# === MySQL (查重表) ===
MYSQL_HOST = os.getenv("WENGLU_MYSQL_HOST", "your-mysql-host")
MYSQL_PORT = int(os.getenv("WENGLU_MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("WENGLU_MYSQL_USER", "your-user")
MYSQL_PASSWORD = os.getenv("WENGLU_MYSQL_PASSWORD", "your-password")
MYSQL_DATABASE = os.getenv("WENGLU_MYSQL_DATABASE", "your-database")
MYSQL_TABLE = os.getenv("WENGLU_MYSQL_TABLE", "jd_rb_case")
MYSQL_DEDUP_ENABLED = os.getenv("WENGLU_MYSQL_DEDUP_ENABLED", "1") == "1"
DEDUP_SUMMARY_THRESHOLD = float(os.getenv("WENGLU_DEDUP_SUMMARY_THRESHOLD", "0.85"))
DEDUP_LOOKBACK_DAYS = int(os.getenv("WENGLU_DEDUP_LOOKBACK_DAYS", "120"))

# === 本地 bge-m3 (摘要相似度查重) ===
LOCAL_SCORE_URL = os.getenv("WENGLU_LOCAL_SCORE_URL", "http://localhost:8002/v1/score")
LOCAL_EMBED_URL = os.getenv("WENGLU_LOCAL_EMBED_URL", "http://localhost:8002/v1/embeddings")
LOCAL_MODEL_NAME = os.getenv("WENGLU_LOCAL_MODEL_NAME", "/path/to/bge-m3")
LOCAL_MODEL_API_KEY = os.getenv("WENGLU_LOCAL_MODEL_API_KEY", "your-local-key")

# === 飞书多维表格 ===
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_xxx")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "your-app-secret")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "your-app-token")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "your-table-id")
FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"

# === Webhook ===
WEBHOOK_HOST = os.getenv("WENGLU_WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WENGLU_WEBHOOK_PORT", "13001"))
WEBHOOK_PATH = os.getenv("WENGLU_WEBHOOK_PATH", "/timingTasks/wenlv_db")
WEBHOOK_TOKEN = os.getenv("WENGLU_WEBHOOK_TOKEN", "your-webhook-token")

# === 飞书工作流反向回调 ===
FEISHU_WORKFLOW_WEBHOOK_URL = os.getenv(
    "WENGLU_FEISHU_WORKFLOW_WEBHOOK_URL",
    "https://your.feishu.cn/base/workflow/webhook/event/XXX",
)
FEISHU_WORKFLOW_WEBHOOK_TOKEN = os.getenv(
    "WENGLU_FEISHU_WORKFLOW_WEBHOOK_TOKEN",
    "your-feishu-workflow-token",
)

# === 字段映射(飞书表头) ===
FEISHU_FIELD_MAPPING = {
    "title": "title",
    "url": "url",
    "summary": "summary",
    "contentAddressNew": "contentAddressNew",
    "source": "source",
    "publish_time": "publish_time",
    "author": "author",
    "info_type": "info_type",
}

FEISHU_FIELD_TYPES = {
    "title": "text",
    "url": "text",
    "summary": "text",
    "contentAddressNew": "text",
    "source": "text",
    "publish_time": "date",
    "author": "text",
    "info_type": "text",
}

# === 业务参数 ===
RECENT_DAYS = 90
NEW_OPEN_DAYS = 15
MIN_RECEPTION = 5000
TARGET_COUNT = 40
REQ_BATCH = 20
MAX_BATCH_ROUNDS = 10
MAX_RETRY_ROUNDS = int(os.getenv("WENGLU_MAX_RETRY_ROUNDS", "3"))
PARALLEL_WORKERS = 8
VALIDATE_URLS = os.getenv("WENGLU_VALIDATE_URLS", "1") == "1"
URL_VALIDATE_TIMEOUT = float(os.getenv("WENGLU_URL_VALIDATE_TIMEOUT", "4.0"))
SUMMARY_MAX_TOKENS = 800
RECOMMEND_MAX_TOKENS = 8000

COLUMNS = list(FEISHU_FIELD_MAPPING.keys())
