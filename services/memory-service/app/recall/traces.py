"""召回轨迹的查询（Dashboard「召回对比」页用）。"""
import json
from datetime import timedelta

import timeutil
from app.config import get_conn

COLS = ("id", "turn_key", "backend", "session_id", "client", "mode", "query", "user_text", "intent", "channels",
        "rerank", "decision", "card", "legacy", "injected_chars", "latency", "created_at")
JSON_COLS = ("intent", "channels", "rerank", "decision", "legacy", "latency")


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
        ids = set()
        for t in items:
            ids |= {p["id"] for p in (t.get("decision") or {}).get("picks", [])}
            lg = t.get("legacy") or {}
            ids |= {int(x) for x in lg.get("picked", []) if str(x).isdigit()}
        snip = _snippets(conn, ids, n=40)
    finally:
        conn.close()
    out = []
    for t in items:
        dec = t.get("decision") or {}
        lg = t.get("legacy") or {}
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
        })
    return out


def get_trace(trace_id):
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {', '.join(COLS)} FROM recall_traces WHERE id=?", (trace_id,)).fetchone()
        if not r:
            return None
        t = _row(r)
        dec = t.get("decision") or {}
        ids = {c["id"] for c in dec.get("candidates", [])} | {p["id"] for p in dec.get("picks", [])}
        ch = t.get("channels") or {}
        ids |= {x[0] for x in ch.get("vector", [])[:10]} | {x[0] for x in ch.get("fts", [])[:10]}
        lg = t.get("legacy") or {}
        ids |= {int(x) for x in lg.get("picked", []) if str(x).isdigit()}
        ids |= {int(c[0]) for c in lg.get("candidates", []) if isinstance(c, list) and str(c[0]).isdigit()}
        t["snippets"] = {str(k): v for k, v in _snippets(conn, ids, n=120).items()}
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
