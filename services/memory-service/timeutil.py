"""时间工具（Phase 1：统一 UTC）

约定（方案 3.5 节 H2）：
- **存储一律用 UTC**，格式 'YYYY-MM-DD HH:MM:SS'（和 SQLite 的 CURRENT_TIMESTAMP、datetime('now') 一致，能直接比较和排序）；
- **只在给人或模型看的地方**换算成用户时区（.env 的 USER_TZ，默认 Asia/Shanghai）；
- 读到的值：带 Z 或时区偏移的按它本身换算；不带时区的当 UTC（库里的数据都是 UTC）。
  例外是外部导入的数据（导出文件里的时间通常是本地时间），调用方传 naive_tz=USER_TZ。

以前的问题：有的地方用 datetime.now()（VPS 是北京时间）写入，有的用 CURRENT_TIMESTAMP（UTC），
读的时候又都当成同一种，于是「距上次对话」、记忆日期都可能差 8 小时——北京时间凌晨的事会被记成前一天。
"""
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

USER_TZ_NAME = os.getenv("USER_TZ", "Asia/Shanghai")
try:
    USER_TZ = ZoneInfo(USER_TZ_NAME) if ZoneInfo else timezone(timedelta(hours=8))
except Exception:
    USER_TZ = timezone(timedelta(hours=8))
USER_TZ_LABEL = "北京时间" if USER_TZ_NAME == "Asia/Shanghai" else USER_TZ_NAME
STORE_FMT = "%Y-%m-%d %H:%M:%S"
_WEEKDAYS = "一二三四五六日"


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_str():
    """现在的 UTC 时间（存储格式）"""
    return utc_now().strftime(STORE_FMT)


def parse_ts(value, naive_tz=timezone.utc):
    """把各种时间写法解析成带时区的 datetime；解析不了返回 None。

    支持 'YYYY-MM-DD HH:MM:SS'、ISO（T / Z / +08:00 / 小数秒）、纯日期、秒或毫秒时间戳、datetime 对象。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, timezone.utc)
    else:
        s = str(value).strip()
        if not s:
            return None
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        if len(s) > 10 and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt


def to_utc_str(value, naive_tz=timezone.utc):
    """任意写法 → UTC 存储格式；解析不了返回 None"""
    dt = parse_ts(value, naive_tz)
    return dt.astimezone(timezone.utc).strftime(STORE_FMT) if dt else None


def to_local(value):
    dt = parse_ts(value)
    return dt.astimezone(USER_TZ) if dt else None


def local_str(value, fmt="%Y-%m-%d %H:%M"):
    """给人 / 模型看：换算成用户时区；解析不了返回空串"""
    dt = to_local(value)
    return dt.strftime(fmt) if dt else ""


def local_date(value):
    return local_str(value, "%Y-%m-%d")


def local_day_label(value):
    """'10月18日 周日'（用户时区）"""
    dt = to_local(value)
    if not dt:
        return ""
    return f"{dt.month}月{dt.day}日 周{_WEEKDAYS[dt.weekday()]}"


def days_since(value):
    """距今多少天（小数）；解析不了返回 None"""
    dt = parse_ts(value)
    if not dt:
        return None
    return (utc_now() - dt).total_seconds() / 86400.0
