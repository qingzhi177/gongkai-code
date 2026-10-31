"""缓存记录与缓存心跳的状态（Dashboard「缓存与心跳」页）。

网关每一轮聊天、每一次缓存心跳结束后 POST /cache/events 报一下用量（只有数字，不带内容），存进 cache_events（迁移 019）；
心跳现在在等谁、下一次什么时候，网关变化时 POST /cache/keepwarm，这里只放在内存里（重启就清空，网关下一轮会再报）。

费用只是按公开价格粗算（缓存读 0.1 倍、5 分钟缓存写 1.25 倍、1 小时缓存写 2 倍），不是账单。
"""
import re
import threading
from datetime import timedelta

import timeutil

KEEP_DAYS = 30
MISS_WRITE = 2000          # 一轮写缓存超过这么多 token：消息部分（或者整个前缀）没命中，重新写了一遍
_state_lock = threading.Lock()
_keepwarm = {}             # 网关报来的心跳状态


def price(model):
    """(输入, 输出) 每百万 token 的美元价格；认不出的返回 None"""
    m = (model or "").lower()
    if "opus" in m:
        mm = re.search(r"opus-4[.-](\d)", m)
        return (5.0, 25.0) if (mm and int(mm.group(1)) >= 5) else (15.0, 75.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "haiku" in m:
        return (1.0, 5.0) if "haiku-4" in m else (0.8, 4.0)
    return None


def cost(e):
    """这一条大约多少美元（认不出模型时 None）"""
    p = price(e.get("model"))
    if not p:
        return None
    w = 2.0 if (e.get("ttl") or "1h") == "1h" else 1.25
    return round((e.get("input", 0) * p[0] + e.get("cache_write", 0) * p[0] * w + e.get("cache_read", 0) * p[0] * 0.1
                  + e.get("output", 0) * p[1]) / 1e6, 5)


def hit_rate(e):
    total = (e.get("input") or 0) + (e.get("cache_write") or 0) + (e.get("cache_read") or 0)
    return round((e.get("cache_read") or 0) / total, 3) if total else None


def _int(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def record(c, ev, now=None):
    now = now or timeutil.utc_now_str()
    kind = ev.get("kind") if ev.get("kind") in ("turn", "beat") else "turn"
    c.execute("INSERT INTO cache_events (ts, kind, conv_id, provider, relay, model, ttl, input, cache_write, cache_read, output, note) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (now, kind, str(ev.get("conv_id") or "")[:120] or None, str(ev.get("provider") or "")[:40] or None,
               str(ev.get("relay") or "")[:60] or None, str(ev.get("model") or "")[:80] or None,
               "5m" if ev.get("ttl") == "5m" else "1h", _int(ev.get("input")), _int(ev.get("cache_write")),
               _int(ev.get("cache_read")), _int(ev.get("output")), str(ev.get("note") or "")[:200] or None))
    cut = (timeutil.parse_ts(now) - timedelta(days=KEEP_DAYS)).strftime(timeutil.STORE_FMT)
    c.execute("DELETE FROM cache_events WHERE ts < ?", (cut,))


def set_keepwarm(state):
    with _state_lock:
        _keepwarm.clear()
        _keepwarm.update({k: state.get(k) for k in ("armed", "conv_id", "provider", "ttl", "next_at", "idle_until",
                                                      "beats_today", "reason", "updated_at") if k in state})


def keepwarm():
    with _state_lock:
        return dict(_keepwarm)


def overview(c, now=None, limit=80):
    now = now or timeutil.utc_now_str()
    cols = ["id", "ts", "kind", "conv_id", "provider", "relay", "model", "ttl", "input", "cache_write", "cache_read", "output", "note"]
    try:
        rows = c.execute("SELECT " + ",".join(cols) + " FROM cache_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        rows = []
    events = []
    for r in rows:
        e = dict(zip(cols, r))
        e["hit_rate"], e["cost"] = hit_rate(e), cost(e)
        e["miss"] = e["kind"] == "turn" and e["cache_write"] > MISS_WRITE
        events.append(e)
    day0 = timeutil.parse_ts(now) - timedelta(hours=24)
    since = day0.strftime(timeutil.STORE_FMT)
    try:
        recent = [dict(zip(cols, r)) for r in c.execute("SELECT " + ",".join(cols) + " FROM cache_events WHERE ts >= ?", (since,))]
    except Exception:
        recent = []
    turns = [e for e in recent if e["kind"] == "turn"]
    beats = [e for e in recent if e["kind"] == "beat"]
    rates = [hit_rate(e) for e in turns if hit_rate(e) is not None]

    def total(es):
        cs = [cost(e) for e in es]
        return round(sum(x for x in cs if x is not None), 4) if any(x is not None for x in cs) else None

    by_provider = {}
    for e in turns:
        by_provider[e["provider"] or "?"] = by_provider.get(e["provider"] or "?", 0) + 1
    return {
        "events": events,
        "day": {"turns": len(turns), "misses": sum(1 for e in turns if e["cache_write"] > MISS_WRITE),
                "avg_hit_rate": round(sum(rates) / len(rates), 3) if rates else None,
                "turn_cost": total(turns), "beats": len(beats), "beat_cost": total(beats),
                "by_provider": by_provider},
        "keepwarm": keepwarm(),
    }
