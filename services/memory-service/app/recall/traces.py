"""召回轨迹的查询（Dashboard「召回对比」页用）。

每一轮只记了候选记忆的编号，看的时候按编号去记忆表里取内容（_snippets）。
清空记忆库之前先 freeze()：把每一轮用到的记忆内容存进 frozen，以后看这一轮就用存下来的，
不会因为记忆没了、或者编号指到了新记忆上而显示错。"""
import json
from datetime import timedelta

import timeutil
from app.config import get_conn
from app.recall import golden

COLS = ("id", "turn_key", "backend", "session_id", "client", "mode", "query", "user_text", "intent", "channels",
        "rerank", "decision", "card", "legacy", "injected_chars", "latency", "created_at", "frozen")
JSON_COLS = ("intent", "channels", "rerank", "decision", "legacy", "latency", "frozen")
FROZEN_CHARS = 200


def _row(r):
    d = dict(zip(COLS, r))
    for k in JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _snippets(conn, ids, n=60):
    ids = sorted({int(i) for i in ids if str(i).isdigit()})
    if not ids:
        return {}
    rows = conn.execute(f"SELECT id, content, ts, event_type, author FROM l1_memories WHERE id IN ({','.join('?' * len(ids))})",
                        ids).fetchall()
    return {r[0]: {"content": (r[1] or "")[:n], "ts": r[2], "event_type": r[3], "author": r[4]} for r in rows}


def _frozen_snippets(t, ids, n=60):
    items = (t.get("frozen") or {}).get("items") or {}
    out = {}
    for i in ids:
        s = items.get(str(i))
        if s:
            out[int(i)] = {**s, "content": (s.get("content") or "")[:n]}
    return out


def _snips(conn, t, ids, n):
    """这一轮的记忆片段：定格过的用存下来的，没定格的去记忆表里取"""
    return _frozen_snippets(t, ids, n) if t.get("frozen") else _snippets(conn, ids, n)


def trace_ids(t, labels=None):
    """这一轮详情里会用到的记忆编号：候选、选中、两路检索的前 10、旧版选中和候选、标注过的"""
    dec = t.get("decision") or {}
    ids = {c["id"] for c in dec.get("candidates", [])} | {p["id"] for p in dec.get("picks", [])}
    ch = t.get("channels") or {}
    ids |= {x[0] for x in ch.get("vector", [])[:10]} | {x[0] for x in ch.get("fts", [])[:10]}
    lg = t.get("legacy") or {}
    ids |= {int(x) for x in lg.get("picked", []) if str(x).isdigit()}
    ids |= {int(c[0]) for c in lg.get("candidates", []) if isinstance(c, list) and str(c[0]).isdigit()}
    ids |= {int(i) for i in (labels or {}) if i}
    return ids


def freeze(conn, source=None):
    """把还没定格的每一轮用到的记忆内容存进 frozen（清空记忆库之前调用）。
    source：去哪个库取记忆内容（默认就是 conn；记忆已经清掉了的时候可以给一个备份库）。
    事务由调用方管。返回定格了几轮。"""
    source = source or conn
    now = timeutil.utc_now_str()
    n = 0
    for r in conn.execute(f"SELECT {', '.join(COLS)} FROM recall_traces WHERE frozen IS NULL").fetchall():
        t = _row(r)
        snip = _snippets(source, trace_ids(t, golden.labels_for(conn, t["id"])), n=FROZEN_CHARS)
        conn.execute("UPDATE recall_traces SET frozen=? WHERE id=?",
                     (json.dumps({"at": now, "items": {str(k): v for k, v in snip.items()}}, ensure_ascii=False), t["id"]))
        n += 1
    return n


def list_traces(limit=50, session_id=None):
    sql = f"SELECT {', '.join(COLS)} FROM recall_traces"
    args = []
    if session_id:
        sql += " WHERE session_id=?"
        args.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 300)))
    conn = get_conn()
    try:
        items = [_row(r) for r in conn.execute(sql, args).fetchall()]
        shown = {}
        for t in items:
            ids = {p["id"] for p in (t.get("decision") or {}).get("picks", [])}
            lg = t.get("legacy") or {}
            shown[t["id"]] = ids | {int(x) for x in lg.get("picked", []) if str(x).isdigit()}
        live = _snippets(conn, set().union(*[shown[t["id"]] for t in items if not t.get("frozen")]), n=40)
        labeled = golden.label_counts(conn, [t["id"] for t in items])
    finally:
        conn.close()
    out = []
    for t in items:
        dec = t.get("decision") or {}
        lg = t.get("legacy") or {}
        snip = _frozen_snippets(t, shown[t["id"]], n=40) if t.get("frozen") else live
        out.append({
            "id": t["id"], "created_at": t["created_at"], "session_id": t["session_id"], "client": t["client"],
            "mode": t["mode"], "user_text": (t["user_text"] or "")[:80],
            "intent": (t.get("intent") or {}).get("type"),
            "new": [{**p, **snip.get(p["id"], {})} for p in dec.get("picks", [])],
            "new_gate": dec.get("gate"), "basis": dec.get("basis"),
            "legacy": {"gate": lg.get("gate"), "picked": [{"id": int(x), **snip.get(int(x), {})}
                                                          for x in lg.get("picked", []) if str(x).isdigit()],
                       "trivial": lg.get("trivial")} if lg else None,
            "rerank_ok": bool((t.get("rerank") or {}).get("ok")),
            "latency": (t.get("latency") or {}).get("total"),
            "labeled": labeled.get(t["id"], 0),
            "frozen": bool(t.get("frozen")),
        })
    return out


def get_trace(trace_id):
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {', '.join(COLS)} FROM recall_traces WHERE id=?", (trace_id,)).fetchone()
        if not r:
            return None
        t = _row(r)
        labels = golden.labels_for(conn, trace_id)
        t["labels"] = {str(k): v for k, v in labels.items()}
        t["snippets"] = {str(k): v for k, v in _snips(conn, t, trace_ids(t, labels), 120).items()}
        fz = t.pop("frozen", None)
        t["frozen_at"] = fz.get("at") if isinstance(fz, dict) else None
        return t
    finally:
        conn.close()


def stats(days=7):
    since = (timeutil.utc_now() - timedelta(days=days)).strftime(timeutil.STORE_FMT)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT decision, legacy, latency, rerank, intent FROM recall_traces WHERE created_at >= ?",
                            (since,)).fetchall()
    finally:
        conn.close()
    n = len(rows)
    new_inj = old_inj = rr_ok = rr_tried = 0
    lats, intents = [], {}
    for dec, lg, lat, rr, it in rows:
        try:
            if (json.loads(dec or "{}").get("picks")):
                new_inj += 1
            if lg and json.loads(lg).get("picked"):
                old_inj += 1
            rrd = json.loads(rr or "{}")
            if "skipped" not in rrd:
                rr_tried += 1
                if rrd.get("ok"):
                    rr_ok += 1
            tot = json.loads(lat or "{}").get("total")
            if tot is not None:
                lats.append(tot)
            k = json.loads(it or "{}").get("type") or "?"
            intents[k] = intents.get(k, 0) + 1
        except ValueError:
            continue
    lats.sort()
    pct = lambda p: lats[min(len(lats) - 1, int(len(lats) * p))] if lats else None
    return {"days": days, "turns": n, "new_inject_turns": new_inj, "legacy_inject_turns": old_inj,
            "rerank_tried_turns": rr_tried, "rerank_ok_turns": rr_ok, "latency_p50": pct(0.5), "latency_p90": pct(0.9), "intents": intents}
