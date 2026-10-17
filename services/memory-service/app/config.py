"""所有环境变量、路径和日志配置的唯一入口（方案 4.3 节）。

记忆服务（main.py）和 worker 都从这里取，保证两边用的是同一个库、同一份 .env。
以前 main.py、extract_l1.py、worker.py 各自拼一遍 DATA_DIR，改一处容易漏一处。
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

import db

SERVICE_DIR = Path(__file__).resolve().parent.parent     # services/memory-service
REPO_DIR = SERVICE_DIR.parent.parent                     # 仓库根目录
load_dotenv(SERVICE_DIR / ".env")                        # 已经设置的环境变量优先，不会被 .env 覆盖

DATA_DIR = Path(os.getenv("DATA_DIR", os.path.expanduser("~/memory-system/data")))
SQLITE_PATH = DATA_DIR / "sqlite" / "memory.db"
# 日志目录跟着代码位置走（仓库根目录下的 data/logs），不跟 DATA_DIR——沿用以前的行为
LOG_DIR = REPO_DIR / "data" / "logs"
AUTO_TRIGGER_LOG = LOG_DIR / "auto_trigger.log"
# 功能6：网关地址，用于配置变更后通知网关热重载
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:3000")

SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_conn():
    """统一的数据库连接（等锁最多 10 秒，见 db.py）"""
    return db.connect(SQLITE_PATH)


def setup_logging():
    """日志写 data/logs/auto_trigger.log（Dashboard「系统日志」读它）并输出到终端。记忆服务和 worker 共用。
    已经配置过时什么都不做。"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(AUTO_TRIGGER_LOG, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
