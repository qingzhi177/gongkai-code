"""运行时可调参数（Dashboard 上能改的那些）。

存在 app_settings 表（迁移 005）：「组名.参数名」→ JSON 值。没改过的项用默认值（.env 或代码里的常量），
「恢复默认」就是删掉那一行。现在只有一组：召回参数（recall.*）。

召回编排每一轮读一次 get("recall")：有 5 秒缓存，保存 / 恢复默认后立即失效，不用重启。
"""
import json
import sqlite3
import threading
import time

import timeutil
from app.config import get_conn
from app.recall import decide as D
from app.recall import rerank as R

CACHE_TTL = 5.0


def _item(key, typ, default, lo, hi, step, section, label, help_text):
    return {"key": key, "type": typ, "default": default, "min": lo, "max": hi, "step": step,
            "section": section, "label": label, "help": help_text}


SPECS = {
    "recall": [
        _item("t_high", float, D.T_HIGH, 0.0, 1.0, 0.01, "门槛", "强门槛",
              "精排分 ≥ 这个值：一定会想起（受「每轮最多几条」限制）。调低会想起得更多、更频繁。"),
        _item("t_low", float, D.T_LOW, 0.0, 1.0, 0.01, "门槛", "中门槛",
              "精排分在中门槛和强门槛之间：按「中层概率」偶尔想起；低于中门槛的不会想起。不能高于强门槛。"),
        _item("min_sim", float, D.MIN_SIM, 0.0, 1.0, 0.01, "门槛", "向量兜底门槛",
              "精排暂时不可用（超时、出错、关掉了）时改看向量相似度：≥ 这个值的只算「中」，不会有「强」。"),
        _item("medium_prob", float, D.MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "中层概率",
              "有「中」候选时，这一轮真的想起的概率（0.3 = 30%）。"),
        _item("task_medium_prob", float, D.TASK_MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "做事时的中层概率",
              "识别成「当下任务」（帮我写、帮我查……）时改用这个概率，默认更低，专心做事。"),
        _item("serendipity_prob", float, D.SERENDIPITY_PROB, 0.0, 1.0, 0.01, "概率", "无由来浮现概率",
              "每一轮和话题无关、忽然想起一件情绪浓或核心的事的概率（0.05 = 5%）。"),
        _item("emotion_bonus", float, D.EMOTION_BONUS, 0.0, 0.3, 0.01, "概率", "情绪加分",
              "聊到情绪（难过、想你……）时，给情绪浓的记忆额外加的分。"),
        _item("max_picks", int, D.MAX_PICKS, 0, 5, 1, "条数和冷却", "每轮最多几条",
              "平时每一轮最多想起几条（0 = 不想起）。"),
        _item("recall_top", int, D.RECALL_TOP, 1, 6, 1, "条数和冷却", "回忆时最多几条",
              "识别成「回忆」（还记得吗、上次……）时最多取几条。"),
        _item("recent_turns", int, D.RECENT_TURNS, 0, 50, 1, "条数和冷却", "最近几轮不重复",
              "同一个对话里，最近这么多轮出现过的不再出现。"),
        _item("medium_cooldown_h", float, D.MEDIUM_COOLDOWN_H, 0.0, 720.0, 1, "条数和冷却", "中层冷却（小时）",
              "因为「中」被想起过的，这么久之内不再因为「中」出现（跨对话）。"),
        _item("serendipity_cooldown_d", float, D.SERENDIPITY_COOLDOWN_D, 0.0, 90.0, 1, "条数和冷却", "无由来冷却（天）",
              "无由来浮现过的，这么多天内不再无由来出现。"),
        _item("rerank_enabled", bool, R.RERANK_ENABLED, None, None, None, "精排", "启用精排",
              "关掉以后一直用向量相似度（只有「中」，没有「强」）。"),
        _item("rerank_model", str, R.RERANK_MODEL, None, None, None, "精排", "精排模型",
              "阿里云百炼的文本排序模型，比如 qwen3.7-text-rerank、qwen3-rerank、gte-rerank-v2。"),
        _item("rerank_timeout", float, R.RERANK_TIMEOUT, 0.3, 10.0, 0.1, "精排", "精排超时（秒）",
              "超过这个时间就放弃精排、改用向量相似度。"),
    ],
}

TYPE_NAMES = {float: "float", int: "int", bool: "bool", str: "str"}

_lock = threading.Lock()
_cache = {}   # 组名 -> (时间, {参数: 值})


def _spec(group):
    if group not in SPECS:
        raise ValueError(f"没有这组参数：{group}")
    return {s["key"]: s for s in SPECS[group]}


def _fmt(x):
    return int(x) if isinstance(x, float) and x.is_integer() else x


def coerce(spec, v):
    """把传进来的值转成参数的类型并检查范围；不合法时抛 ValueError（中文说明）"""
    t, label = spec["type"], spec["label"]
    if t is bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "on", "yes", "0", "false", "off", "no"):
            return v.strip().lower() in ("1", "true", "on", "yes")
        raise ValueError(f"「{label}」只能是开或关")
    if t is str:
        s = str(v if v is not None else "").strip()
        if not s or len(s) > 100:
            raise ValueError(f"「{label}」不能为空，也不能超过 100 个字符")
        return s
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"「{label}」要填一个数字")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"「{label}」要填一个数字")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"「{label}」要填一个数字")
    if t is int:
        if not x.is_integer():
            raise ValueError(f"「{label}」要填整数")
        x = int(x)
    if (spec["min"] is not None and x < spec["min"]) or (spec["max"] is not None and x > spec["max"]):
        raise ValueError(f"「{label}」要在 {_fmt(spec['min'])} 到 {_fmt(spec['max'])} 之间")
    return x


def _check_recall(vals):
    if vals["t_low"] > vals["t_high"]:
        raise ValueError(f"中门槛（{vals['t_low']}）不能高于强门槛（{vals['t_high']}）")


CHECKS = {"recall": _check_recall}


def _overrides(conn, group):
    """{参数: (值, 更新时间)}；表还没建（迁移前）时返回空"""
    try:
        rows = conn.execute("SELECT key, value, updated_at FROM app_settings WHERE key LIKE ?",
                            (group + ".%",)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for k, v, ts in rows:
        try:
            out[k[len(group) + 1:]] = (json.loads(v), ts)
        except Exception:
            pass
    return out


def _effective(group, ov):
    vals = {}
    for s in SPECS[group]:
        v = s["default"]
        if s["key"] in ov:
            try:
                v = coerce(s, ov[s["key"]][0])
            except ValueError:
                pass   # 库里的值不合法（手改过？）就当没改过
        vals[s["key"]] = v
    return vals


def invalidate(group=None):
    with _lock:
        if group:
            _cache.pop(group, None)
        else:
            _cache.clear()


def get(group, fresh=False):
    """这一组参数的当前值（改过的用改过的，其余用默认值）"""
    _spec(group)
    now = time.monotonic()
    if not fresh:
        with _lock:
            hit = _cache.get(group)
            if hit and now - hit[0] < CACHE_TTL:
                return dict(hit[1])
    conn = get_conn()
    try:
        vals = _effective(group, _overrides(conn, group))
    finally:
        conn.close()
    with _lock:
        _cache[group] = (now, vals)
    return dict(vals)


def describe(group):
    """给 Dashboard 用：每个参数的名称、说明、范围、默认值、当前值、是否改过"""
    conn = get_conn()
    try:
        ov = _overrides(conn, group)
    finally:
        conn.close()
    vals = _effective(group, ov)
    items = []
    for s in SPECS[group]:
        items.append({**{k: s[k] for k in ("key", "label", "help", "section", "min", "max", "step", "default")},
                      "type": TYPE_NAMES[s["type"]], "value": vals[s["key"]],
                      "overridden": s["key"] in ov, "updated_at": ov[s["key"]][1] if s["key"] in ov else None})
    times = [ov[k][1] for k in ov]
    return {"group": group, "items": items, "updated_at": max(times) if times else None}


def update(group, values):
    """保存一批参数（只存传进来的）。任何一个不合法就整批不存，抛 ValueError。返回保存后的当前值。"""
    spec = _spec(group)
    clean = {}
    for k, v in (values or {}).items():
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
        clean[k] = coerce(spec[k], v)
    if not clean:
        return get(group, fresh=True)
    now = timeutil.utc_now_str()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        merged = {**_effective(group, _overrides(conn, group)), **clean}
        if group in CHECKS:
            CHECKS[group](merged)
        for k, v in clean.items():
            conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (f"{group}.{k}", json.dumps(v, ensure_ascii=False), now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)


def reset(group, keys=None):
    """恢复默认：删掉这些参数（不传就是整组）。恢复后如果和别的改过的参数冲突（比如中门槛高于强门槛），抛 ValueError、不改。"""
    spec = _spec(group)
    keys = list(keys or [])
    for k in keys:
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if keys:
            conn.executemany("DELETE FROM app_settings WHERE key = ?", [(f"{group}.{k}",) for k in keys])
        else:
            conn.execute("DELETE FROM app_settings WHERE key LIKE ?", (group + ".%",))
        merged = _effective(group, _overrides(conn, group))
        if group in CHECKS:
            CHECKS[group](merged)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)
