"""共同语汇（方案 6.7 节，Phase 4 第四步）：只有我们之间才懂的称呼、小梗、固定说法、口头禅、歌、地方、东西。

- 发现（lexicon_scan 任务）：每日片段整理好的天，按对话块（和每日片段同样的分段）交给模型，找新出现的说法，
  连同已经记下的词（避免换个说法重复提）。每块只扫一次（lexicon_scans 记着块的指纹）。
- 校验：词（或别名）必须在原文里一字不差地出现过，不然不收；只有一个字的不收。
- 统计全部按原文直接数，不让模型猜：用了几条消息、几天、第一次在哪条消息、是谁先说的（coined_by）、最近一次。
- 状态：candidate（候选）→ 在 2 天以上出现过就自动变成 active（收下）；你也可以直接确认。
  rejected（你说不要）：以后扫到同样的词（或它的别名）不再提。merged：并进了另一个词（它的叫法变成那个词的别名）。
- 你在 Dashboard 上改过的含义，以后扫描不会覆盖。
- match()：一句话里提到了哪些词，以后聊天时每轮做字面匹配、注入一行来处（现在只在 Dashboard 的「试一试」里看）。
收下的语汇放进聊天（app/compose.py）。
"""
import json
import re
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import episodes as E

KINDS = {"nickname": "称呼", "inside_joke": "小梗", "ritual": "固定说法", "catchphrase": "口头禅",
         "song": "歌", "place": "地方", "object": "东西", "other": "其他"}
MAX_TERMS_PER_CHUNK = 3
MAX_CHUNKS_PER_JOB = 10
KNOWN_MAX = 80              # 给模型看的「已经记下的」最多这么多条
MAX_TOKENS = 24000           # 推理模型（包括 deepseek-flash）的思考也算在里面，12000 时偶尔想完就没有正文
MIN_TERM_LEN = 2
MAX_TERM_LEN = 20          # 歌名可以长一点
MAX_WORD_LEN = 10          # 歌以外的，超过这么长就不像一个词，更像一句话


def cfg():
    return app_settings.get("lexicon")


def _now():
    return timeutil.utc_now_str()


def norm(t):
    """比较用：去掉空白和引号、英文转小写"""
    return re.sub(r"[\s「」『』“”\"'《》]", "", str(t or "")).lower()


async def default_llm(prompt):
    """用「模块模型配置」里的「共同语汇」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("lexicon", "episode", "narrative", "summary"), MAX_TOKENS)


# ---------- 读库 ----------

ROW_COLS = ("id, term, aliases, kind, meaning, status, merged_into, coined_by, origin_l0_id, origin_ts, first_seen, last_used, "
            "use_count, days_seen, evidence, source, edited, created_at, updated_at")
ROW_KEYS = [c.strip() for c in ROW_COLS.split(",")]


def _row(r):
    d = dict(zip(ROW_KEYS, r))
    d["aliases"] = A._loads(d["aliases"], [])
    d["evidence"] = A._loads(d["evidence"], [])
    return d


def _all(conn, statuses=None):
    sql = f"SELECT {ROW_COLS} FROM lexicon"
    args = []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args = list(statuses)
    return [_row(r) for r in conn.execute(sql + " ORDER BY id", args)]


def _index(conn):
    """叫法（词和别名，比较用的写法）→ 词条 id。并进别的词的，指向最终那个词。"""
    rows = _all(conn)
    by_id = {r["id"]: r for r in rows}

    def final(i):
        seen = set()
        while by_id.get(i) and by_id[i]["status"] == "merged" and by_id[i]["merged_into"] and i not in seen:
            seen.add(i)
            i = by_id[i]["merged_into"]
        return i
    idx = {}
    for r in rows:
        for name in [r["term"]] + r["aliases"]:
            k = norm(name)
            if k and k not in idx:
                idx[k] = final(r["id"])
    return idx, by_id


# ---------- 发现 ----------

def _settled_days(conn):
    return [r[0] for r in conn.execute("SELECT day FROM settle_days WHERE status='done' ORDER BY day")]


def unscanned(conn, limit=None):
    """还没扫过的对话块（只看整理好的天、够长的块），按时间从早到晚"""
    done = {r[0] for r in conn.execute("SELECT chunk_key FROM lexicon_scans")}
    out = []
    for day in _settled_days(conn):
        for s in E.sessions_for_day(conn, day):
            for part in E.chunks(s["rows"]):
                if not E.substantial(part):
                    continue
                key = E.chunk_key(part)
                if key in done:
                    continue
                out.append({"day": day, "conv_id": s["conv_id"], "rows": part, "key": key})
                if limit and len(out) >= limit:
                    return out
    return out


def build_prompt(known, part):
    tpl, version = A._prompt("lexicon_scan")
    lines = []
    for n, (mid, role, content, ts) in enumerate(part, 1):
        text = content if len(content) <= E.MAX_LINE_CHARS else content[:E.MAX_LINE_CHARS] + "……"
        lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {text}")
    k = "\n".join(f"- {r['term']}（{KINDS.get(r['kind'], '其他')}）" + (f"，也叫{'、'.join(r['aliases'])}" if r["aliases"] else "")
                  for r in known[:KNOWN_MAX]) or "（还没有）"
    span = timeutil.local_str(part[0][3]) + " 至 " + timeutil.local_str(part[-1][3])[11:]
    return tpl.replace("{known}", k).replace("{span}", span).replace("{lines}", "\n".join(lines)), version


def verify(item, part):
    """词必须在这块原文里一字不差地出现过。返回 (清理后的词条 或 None, 问题)"""
    term = str(item.get("term") or "").strip().strip("「」『』“”\"'《》")
    if not (MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN):
        return None, f"长度不合适：{term[:20]}"
    kind = item.get("kind") if item.get("kind") in KINDS else "other"
    if kind != "song" and len(term) > MAX_WORD_LEN:
        return None, f"太长，不像一个词：{term[:20]}"
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", term):
        return None, f"不是词（表情、符号）：{term}"
    texts = [(n, norm(r[2])) for n, r in enumerate(part, 1)]
    hits = [n for n, t in texts if norm(term) in t]
    if not hits:
        return None, f"原文里没有：{term}"
    aliases = []
    for a in item.get("aliases") or []:
        a = str(a or "").strip().strip("「」『』“”\"'《》")
        if MIN_TERM_LEN <= len(a) <= MAX_TERM_LEN and norm(a) != norm(term) and any(norm(a) in t for _, t in texts) \
                and norm(a) not in [norm(x) for x in aliases]:
            aliases.append(a)
    return {"term": term, "aliases": aliases, "kind": kind, "meaning": str(item.get("meaning") or "").strip()[:120],
            "evidence": [part[n - 1][0] for n in hits[:10]]}, None


def upsert(conn, cand, idx, by_id):
    """新词插入候选；已有的词（或它的叫法）补别名和出处；你拒绝过的跳过。调用方负责事务。返回 (动作, 词条 id)"""
    now = _now()
    target = None
    for name in [cand["term"]] + cand["aliases"]:
        if norm(name) in idx:
            target = idx[norm(name)]
            break
    if target is not None:
        row = by_id[target]
        if row["status"] == "rejected":
            return "skipped_rejected", target
        names = {norm(x) for x in [row["term"]] + row["aliases"]}
        new_aliases = [a for a in [cand["term"]] + cand["aliases"]
                       if norm(a) not in names and idx.get(norm(a), target) == target]
        aliases = row["aliases"] + new_aliases
        evidence = list(dict.fromkeys(row["evidence"] + cand["evidence"]))[:30]
        meaning = row["meaning"] if (row["edited"] or row["meaning"]) else cand["meaning"]
        conn.execute("UPDATE lexicon SET aliases=?, evidence=?, meaning=?, updated_at=? WHERE id=?",
                     (json.dumps(aliases, ensure_ascii=False), json.dumps(evidence), meaning, now, target))
        row.update(aliases=aliases, evidence=evidence, meaning=meaning)
        for a in new_aliases:
            idx[norm(a)] = target
        return ("alias" if new_aliases else "seen"), target
    cur = conn.execute("INSERT INTO lexicon (term, term_key, aliases, kind, meaning, status, evidence, source, edited, use_count, "
                       "days_seen, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'candidate', ?, 'scan', 0, 0, 0, ?, ?)",
                       (cand["term"], norm(cand["term"]), json.dumps(cand["aliases"], ensure_ascii=False), cand["kind"],
                        cand["meaning"], json.dumps(cand["evidence"]), now, now))
    lid = cur.lastrowid
    by_id[lid] = {"id": lid, "term": cand["term"], "aliases": cand["aliases"], "kind": cand["kind"], "meaning": cand["meaning"],
                  "status": "candidate", "merged_into": None, "evidence": cand["evidence"], "edited": 0}
    for name in [cand["term"]] + cand["aliases"]:
        idx.setdefault(norm(name), lid)
    return "new", lid


async def scan(llm_fn=None, now=None, limit=MAX_CHUNKS_PER_JOB):
    """扫描任务：没扫过的对话块一块一块找词 → 重新统计 → 够天数的候选自动收下。剩下的块再投一个任务。"""
    import jobs
    from app.config import SQLITE_PATH
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        parts = unscanned(conn, limit + 1)
    finally:
        conn.close()
    more = len(parts) > limit
    stats = {"chunks": 0, "new": [], "alias": 0, "rejected_skipped": 0, "notes": []}
    for p in parts[:limit]:
        conn = get_conn()
        try:
            known = _all(conn, ("active", "candidate"))
        finally:
            conn.close()
        prompt, version = build_prompt(known, p["rows"])
        text, model = await llm_fn(prompt)
        if not text:
            raise RuntimeError("找共同语汇：调用模型失败（没有返回内容），稍后重试")
        items = A.parse_json(text).get("terms") or []
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            idx, by_id = _index(conn)
            result = []
            for it in items[:MAX_TERMS_PER_CHUNK]:
                if not isinstance(it, dict):
                    continue
                cand, issue = verify(it, p["rows"])
                if not cand:
                    stats["notes"].append(issue)
                    continue
                action, lid = upsert(conn, cand, idx, by_id)
                result.append({"term": cand["term"], "action": action, "id": lid})
                if action == "new":
                    stats["new"].append(cand["term"])
                elif action == "alias":
                    stats["alias"] += 1
                elif action == "skipped_rejected":
                    stats["rejected_skipped"] += 1
            conn.execute("INSERT OR REPLACE INTO lexicon_scans (chunk_key, day, conv_id, scanned_at, model, prompt_version, result) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (p["key"], p["day"], p["conv_id"], _now(), model, version,
                                                          json.dumps(result, ensure_ascii=False)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        stats["chunks"] += 1
    activated = refresh_stats()
    if more:      # 接着扫剩下的。不能用 "lexicon_scan" 去重：这个任务自己正在跑，会被当成重复，新任务投不进去
        jobs.enqueue(SQLITE_PATH, "lexicon_scan", payload={}, dedupe_key="lexicon_scan:next", source="auto", delay_sec=5,
                     continuation=True)
    return {"status": "ok", **stats, "activated": activated, "more": more, "notes": stats["notes"][:20]}


def enqueue_scan(source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    return jobs.enqueue(SQLITE_PATH, "lexicon_scan", payload={}, dedupe_key="lexicon_scan", source=source)[1]


def pending_chunks():
    conn = get_conn()
    try:
        return len(unscanned(conn))
    finally:
        conn.close()


# ---------- 统计（按原文直接数） ----------

def _occurrences(conn, names):
    """包含这些叫法的消息（active），按时间"""
    found = {}
    for name in names:
        if len(name) < MIN_TERM_LEN:
            continue
        for mid, role, ts, content in conn.execute(
                "SELECT id, role, ts, content FROM l0_messages WHERE status='active' AND instr(lower(content), lower(?)) > 0",
                (name,)):
            found[mid] = (mid, role, ts, content)
    return sorted(found.values(), key=lambda x: (timeutil.to_utc_str(x[2]) or "", x[0]))


def refresh_stats(lid=None):
    """重新数一遍：几条消息、几天、第一次（谁先说的）、最近一次；候选够天数就自动收下。返回自动收下的词。"""
    need = cfg()["auto_activate_days"]
    activated = []
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _all(conn, ("candidate", "active"))
        for r in rows:
            if lid is not None and r["id"] != lid:
                continue
            occ = _occurrences(conn, [r["term"]] + r["aliases"])
            days = {str(E.day_of(o[2])) for o in occ if E.day_of(o[2])}
            first, last = (occ[0], occ[-1]) if occ else (None, None)
            status = r["status"]
            if status == "candidate" and len(days) >= need:
                status = "active"
                activated.append(r["term"])
            conn.execute("UPDATE lexicon SET use_count=?, days_seen=?, first_seen=?, last_used=?, origin_l0_id=?, origin_ts=?, "
                         "coined_by=?, status=? WHERE id=?",
                         (len(occ), len(days), timeutil.to_utc_str(first[2]) if first else None,
                          timeutil.to_utc_str(last[2]) if last else None, first[0] if first else None,
                          timeutil.to_utc_str(first[2]) if first else None,
                          ("user" if first[1] == "user" else "ai") if first else None, status, r["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return activated


# ---------- 字面匹配（以后每轮聊天用；现在给「试一试」） ----------

def origin_line(r):
    """注入时的那一行，比如「小雨伞」——你给我起的称呼……（你在 10 月 17 日晚上第一次说）"""
    who = {"user": "你", "ai": "我"}.get(r.get("coined_by"), "")
    when = A._when(r["origin_ts"], "") if r.get("origin_ts") else ""
    when = re.sub(r"^\d{4}年", "", when)
    tail = f"（{who}在{when}第一次说）" if who and when else ""
    meaning = r.get("meaning") or KINDS.get(r.get("kind"), "")
    name = f"《{r['term']}》" if r.get("kind") == "song" else f"「{r['term']}」"
    return f"{name}——{meaning}{tail}"


def match(text, rows=None):
    """一句话里提到了哪些收下的词（含别名）。长的叫法优先，同一个词只算一次。"""
    if rows is None:
        conn = get_conn()
        try:
            rows = _all(conn, ("active",))
        finally:
            conn.close()
    t = norm(text)
    names = sorted(((norm(n), n, r) for r in rows for n in [r["term"]] + r["aliases"] if len(norm(n)) >= MIN_TERM_LEN),
                   key=lambda x: -len(x[0]))
    hits, taken, used = [], [], set()
    for key, name, r in names:
        if r["id"] in used:
            continue
        pos = t.find(key)
        while pos >= 0 and any(a < pos + len(key) and pos < b for a, b in taken):
            pos = t.find(key, pos + 1)
        if pos < 0:
            continue
        taken.append((pos, pos + len(key)))
        used.add(r["id"])
        hits.append({"id": r["id"], "term": r["term"], "matched": name, "kind": r["kind"], "line": origin_line(r)})
    return hits


# ---------- 你在 Dashboard 上的操作 ----------

def _get(conn, lid):
    r = conn.execute(f"SELECT {ROW_COLS} FROM lexicon WHERE id=?", (int(lid),)).fetchone()
    if not r:
        raise ValueError("没有这个词")
    return _row(r)


def _set_status(lid, status, allowed):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = _get(conn, lid)
        if r["status"] not in allowed:
            raise ValueError(f"这个词现在是「{STATUS_CN.get(r['status'], r['status'])}」，不能这样操作")
        conn.execute("UPDATE lexicon SET status=?, updated_at=? WHERE id=?", (status, _now(), int(lid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


STATUS_CN = {"candidate": "候选", "active": "收下的", "rejected": "不要的", "merged": "已合并"}


def confirm(lid):
    _set_status(lid, "active", ("candidate",))


def reject(lid):
    _set_status(lid, "rejected", ("candidate", "active"))


def restore(lid):
    _set_status(lid, "candidate", ("rejected",))
    refresh_stats(int(lid))


def edit(lid, term=None, aliases=None, kind=None, meaning=None):
    """改词、别名、种类、含义。改过含义的，以后扫描不会覆盖。叫法不能和别的词重复。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = _get(conn, lid)
        if r["status"] == "merged":
            raise ValueError("这个词已经并进别的词了")
        idx, _ = _index(conn)
        new_term = r["term"] if term is None else str(term).strip()
        if not (MIN_TERM_LEN <= len(new_term) <= MAX_TERM_LEN):
            raise ValueError(f"词要 {MIN_TERM_LEN} 到 {MAX_TERM_LEN} 个字")
        new_aliases = r["aliases"] if aliases is None else \
            list(dict.fromkeys(a.strip() for a in aliases if MIN_TERM_LEN <= len(a.strip()) <= MAX_TERM_LEN and norm(a) != norm(new_term)))
        for name in [new_term] + new_aliases:
            other = idx.get(norm(name))
            if other is not None and other != r["id"]:
                raise ValueError(f"「{name}」已经是别的词（或它的别名）了，可以用「并入」")
        if kind is not None and kind not in KINDS:
            raise ValueError("没有这个种类")
        new_meaning = r["meaning"] if meaning is None else str(meaning).strip()[:120]
        conn.execute("UPDATE lexicon SET term=?, term_key=?, aliases=?, kind=?, meaning=?, edited=?, updated_at=? WHERE id=?",
                     (new_term, norm(new_term), json.dumps(new_aliases, ensure_ascii=False), kind or r["kind"], new_meaning,
                      1 if (meaning is not None or r["edited"]) else 0, _now(), int(lid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_stats(int(lid))


def merge(lid, into):
    """把一个词并进另一个词：它的词和别名都变成那个词的别名，出处合在一起"""
    if int(lid) == int(into):
        raise ValueError("不能并进自己")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        src, dst = _get(conn, lid), _get(conn, into)
        if src["status"] == "merged" or dst["status"] in ("merged", "rejected"):
            raise ValueError("这两个词现在不能合并")
        names = {norm(x) for x in [dst["term"]] + dst["aliases"]}
        aliases = dst["aliases"] + [a for a in [src["term"]] + src["aliases"] if norm(a) not in names]
        evidence = list(dict.fromkeys(dst["evidence"] + src["evidence"]))[:30]
        conn.execute("UPDATE lexicon SET aliases=?, evidence=?, meaning=?, updated_at=? WHERE id=?",
                     (json.dumps(aliases, ensure_ascii=False), json.dumps(evidence), dst["meaning"] or src["meaning"], _now(), int(into)))
        conn.execute("UPDATE lexicon SET status='merged', merged_into=?, updated_at=? WHERE id=?", (int(into), _now(), int(lid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_stats(int(into))


def add(term, kind="other", meaning="", aliases=None):
    """你自己加一个词（直接收下）"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        term = str(term or "").strip()
        if not (MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN):
            raise ValueError(f"词要 {MIN_TERM_LEN} 到 {MAX_TERM_LEN} 个字")
        idx, _ = _index(conn)
        aliases = [a.strip() for a in (aliases or []) if MIN_TERM_LEN <= len(a.strip()) <= MAX_TERM_LEN]
        for name in [term] + aliases:
            if norm(name) in idx:
                raise ValueError(f"「{name}」已经记下了")
        cur = conn.execute("INSERT INTO lexicon (term, term_key, aliases, kind, meaning, status, evidence, source, edited, use_count, "
                           "days_seen, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', '[]', 'dashboard', ?, 0, 0, ?, ?)",
                           (term, norm(term), json.dumps(aliases, ensure_ascii=False), kind if kind in KINDS else "other",
                            str(meaning or "").strip()[:120], 1 if meaning else 0, _now(), _now()))
        lid = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_stats(lid)
    return lid


# ---------- 查看 ----------

def overview():
    conn = get_conn()
    try:
        rows = _all(conn)
        scanned = conn.execute("SELECT COUNT(*) FROM lexicon_scans").fetchone()[0]
    finally:
        conn.close()
    for r in rows:
        r["origin_line"] = origin_line(r) if r["status"] in ("active", "candidate") else ""
    order = lambda r: (r["last_used"] or "", r["use_count"] or 0)
    groups = {s: sorted([r for r in rows if r["status"] == s], key=order, reverse=True) for s in STATUS_CN}
    return {"active": groups["active"], "candidate": groups["candidate"], "rejected": groups["rejected"],
            "merged": groups["merged"], "kinds": KINDS, "scanned_chunks": scanned, "pending_chunks": pending_chunks(),
            "enabled": bool(cfg().get("enabled"))}


def detail(lid, limit=20):
    """词条详情：最近用到它的消息（原文片段）"""
    conn = get_conn()
    try:
        r = _get(conn, lid)
        occ = _occurrences(conn, [r["term"]] + r["aliases"])
    finally:
        conn.close()
    r["origin_line"] = origin_line(r)
    names = [n for n in [r["term"]] + r["aliases"] if len(n) >= MIN_TERM_LEN]

    def snippet(content):
        low = content.lower()
        pos = min([p for p in (low.find(n.lower()) for n in names) if p >= 0] or [0])
        a = max(0, pos - 30)
        return ("…" if a else "") + content[a:pos + 50].replace("\n", " ") + ("…" if pos + 50 < len(content) else "")
    pick = occ[:3] + [o for o in occ[3:][-(limit - 3):]] if len(occ) > limit else occ
    r["occurrences"] = [{"id": o[0], "role": o[1], "ts": timeutil.to_utc_str(o[2]), "snippet": snippet(o[3] or "")} for o in pick]
    r["occurrence_total"] = len(occ)
    return r
