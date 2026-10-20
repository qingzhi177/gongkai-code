"""主题卷（方案 6.9 节，Phase 4 第二步）：把每日片段按主题归成跨越时间的长线（比如「一起听歌」「下雨天」）。

1. 归卷（route_arcs 任务）：还没归过卷的片段按时间顺序，每批最多 ROUTE_BATCH 个，连同现有的卷目录交给模型，
   每个片段放进 0 到 2 个卷（附一句话标签）；属于某条长线、但还没有卷的，提一个新主题（「候选」卷）。
2. 候选 → 待确认：候选卷积累到 3 个片段、跨 2 天以上，变成「待确认」，在 Dashboard 上由你确认（可以改名）或拒绝。
   拒绝过的标题会告诉模型，不再提。你也可以直接确认一个候选。
3. 写卷（narrate_arc 任务）：确认以后整篇写一次；之后每攒够 2 个新片段，追加一小节。每句具体细节标出处（片段），
   引号里的原话必须能在片段或它的原文里找到；对不上的句子删掉，删得太多就让模型重写一次，还不行就不发布、保留旧版本。
4. 30 天没有新素材的卷转为「休眠」，有新素材时自动醒来。
片段被重新整理（旧版本作废）以后，它在卷里的素材会去掉；正文引用过它的卷会整篇重写。
现在只生成、只在 Dashboard 上看，不放进聊天（以后开窗包只放卷的目录：标题 + 一句话）。
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import episodes as E

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
ROUTE_BATCH = 6             # 每次归卷交给模型的片段数（推理模型一次给 12 个会想太久，16000 tokens 都想不完）
MAX_ROUTE_BATCHES = 30      # 一个任务最多跑这么多批，剩下的再投一个任务
CATALOG_MAX = 40            # 给模型看的卷目录最多这么多条（以后卷多了再加向量预筛）
MAX_ARCS_PER_EPISODE = 2
WRITE_BATCH = 16            # 写卷时每次交给模型的片段数
EXISTING_CHARS = 1500       # 写卷时给模型看的「已经写好的部分」（取最后这么多字）
ROUTE_BODY_CHARS = 220      # 归卷时每个片段给模型看的正文长度
SUMMARY_MAX = 120
MIN_SECTION_CHARS = 40
MAX_TOKENS = 16000          # 写卷
ROUTE_MAX_TOKENS = 24000    # 归卷（推理模型的思考也算在里面）
WRITE_PURPOSES = ("arc", "episode", "narrative", "summary")
ROUTE_PURPOSES = ("arc_route",) + WRITE_PURPOSES

STATUS_CN = {"candidate": "候选", "proposed": "待确认", "active": "进行中", "dormant": "休眠", "rejected": "已拒绝"}
LIVE = ("candidate", "proposed", "active", "dormant")
WRITTEN = ("active", "dormant")


def cfg():
    return app_settings.get("arcs")


def _prompt(name):
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"<!--\s*(\S+\s+v\d+)[^>]*-->\s*", text)
    return (text[m.end():] if m else text), (m.group(1).replace(" ", "-") if m else f"{name}-v?")


def _norm_title(t):
    return re.sub(r"[\s「」『』“”\"'，。、·:：\-—]", "", t or "").lower()


def _loads(s, default):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except ValueError:
        return default


def parse_json(text):
    """模型输出 → dict（容忍代码块和前后多余文字）；不是 JSON 抛 ValueError"""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("模型没有输出 JSON")
    data = json.loads(t[a:b + 1])
    if not isinstance(data, dict):
        raise ValueError("模型输出的不是 JSON 对象")
    return data


async def default_llm(prompt, purposes=WRITE_PURPOSES, max_tokens=MAX_TOKENS):
    """写卷用「模块模型配置」里的「叙事卷」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    from app.llm import call_llm, get_model_config
    for purpose in purposes:
        c = await get_model_config(purpose)
        if c.get("configured"):
            return await call_llm(c, prompt, max_tokens), c.get("model")
    raise RuntimeError("没有可用的模型：请在 Dashboard「模块模型配置」里设置「叙事卷」或「共同经历叙事」")


async def route_llm(prompt):
    """归卷是分类的小活：先用「归卷」的模型（可以选便宜、快的），没设置就和写卷用同一个"""
    return await default_llm(prompt, ROUTE_PURPOSES, ROUTE_MAX_TOKENS)


def _now():
    return timeutil.utc_now_str()


# ---------- 素材整理（片段作废以后） ----------

def _cited(sections):
    return {int(i) for s in sections for i in (s.get("refs") or [])}


def cleanup(conn):
    """去掉已作废片段的素材；正文引用过它们的卷标记为要整篇重写；候选卷素材空了就删掉，待确认的不够数了退回候选。"""
    gone = conn.execute("SELECT m.arc_id, m.episode_id FROM arc_materials m LEFT JOIN episodes e ON e.id = m.episode_id "
                        "WHERE e.id IS NULL OR e.status != 'active'").fetchall()
    touched = {}
    for arc_id, eid in gone:
        conn.execute("DELETE FROM arc_materials WHERE arc_id=? AND episode_id=?", (arc_id, eid))
        touched.setdefault(arc_id, set()).add(eid)
    for arc_id, eids in touched.items():
        r = conn.execute("SELECT sections FROM arcs WHERE id=?", (arc_id,)).fetchone()
        if r and _cited(_loads(r[0], [])) & eids:
            conn.execute("UPDATE arcs SET needs_rebuild=1 WHERE id=?", (arc_id,))
    conn.execute("DELETE FROM arcs WHERE status='candidate' AND id NOT IN (SELECT DISTINCT arc_id FROM arc_materials)")
    return {a: sorted(e) for a, e in touched.items()}


def refresh_status(conn, now=None):
    """候选够数 → 待确认；进行中 / 休眠按最近一条素材的时间切换。返回新变成待确认的卷 id。"""
    c = cfg()
    now = timeutil.parse_ts(now) if now else timeutil.utc_now()
    proposed = []
    rows = conn.execute("SELECT a.id, a.status, COUNT(m.episode_id), COUNT(DISTINCT m.day), MAX(m.ts) FROM arcs a "
                        "LEFT JOIN arc_materials m ON m.arc_id = a.id WHERE a.status IN ('candidate','proposed','active','dormant') "
                        "GROUP BY a.id").fetchall()
    for aid, status, n, days, last in rows:
        conn.execute("UPDATE arcs SET last_material_ts=? WHERE id=?", (last, aid))
        enough = n >= c["propose_min_episodes"] and days >= c["propose_min_days"]
        if status == "candidate" and enough:
            conn.execute("UPDATE arcs SET status='proposed', updated_at=? WHERE id=?", (_now(), aid))
            proposed.append(aid)
        elif status == "proposed" and not enough:
            conn.execute("UPDATE arcs SET status='candidate', updated_at=? WHERE id=?", (_now(), aid))
        elif status in WRITTEN and last:
            t = timeutil.parse_ts(last)
            new = "dormant" if t and (now - t) > timedelta(days=c["dormant_days"]) else "active"
            if new != status:
                conn.execute("UPDATE arcs SET status=?, updated_at=? WHERE id=?", (new, _now(), aid))
    return proposed


# ---------- 归卷 ----------

def unrouted(conn, limit=None):
    sql = ("SELECT e.id, e.day, e.title, e.body, e.mood, e.topics, e.start_ts FROM episodes e "
           "LEFT JOIN arc_routing r ON r.episode_id = e.id WHERE e.status='active' AND r.episode_id IS NULL "
           "ORDER BY e.day, e.start_ts, e.id")
    rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    return [{"id": r[0], "day": r[1], "title": r[2], "body": r[3] or "", "mood": r[4] or "",
             "topics": _loads(r[5], []), "ts": r[6]} for r in rows]


def catalog(conn):
    arcs = []
    for aid, title, theme, status, summary in conn.execute(
            "SELECT id, title, theme, status, summary FROM arcs WHERE status IN ('candidate','proposed','active','dormant') "
            "ORDER BY COALESCE(last_material_ts, created_at) DESC LIMIT ?", (CATALOG_MAX,)):
        labels = [r[0] for r in conn.execute("SELECT label FROM arc_materials WHERE arc_id=? AND label != '' "
                                             "ORDER BY ts DESC LIMIT 3", (aid,))]
        arcs.append({"id": aid, "title": title, "theme": theme or "", "status": status, "summary": summary or "",
                     "labels": labels})
    rejected = [r[0] for r in conn.execute("SELECT title FROM arcs WHERE status='rejected' ORDER BY updated_at DESC LIMIT 30")]
    return arcs, rejected


PERIODS = ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (19, "傍晚"), (23, "晚上"), (24, "深夜"))


def _when(ts, day):
    """片段开始的真实日期和时段（用户时区），比如「2026年10月19日凌晨」。
    不用「哪一天」：凌晨 4 点前的算前一天，写成日期会差一天；也不给几点几分，免得写进正文里。"""
    t = timeutil.to_local(ts) if ts else None
    if not t:
        return str(day)
    period = next(name for end, name in PERIODS if t.hour < end)
    return f"{t.year}年{t.month}月{t.day}日{period}"


def _short_title(t, limit=24):
    t = str(t or "").strip()
    if len(t) <= limit:
        return t
    cut = max(t.rfind(p, 0, limit) for p in "，,、；;：:")
    return t[:cut] if cut >= 4 else t[:limit]


def build_route_prompt(arcs, rejected, eps):
    tpl, version = _prompt("arc_route")
    cat = "\n".join(f"[A{a['id']}]（{STATUS_CN[a['status']]}）{a['title']}：{a['theme'] or a['summary']}"
                    + (f"；最近：{'；'.join(a['labels'])}" if a["labels"] else "") for a in arcs) or "（还没有）"
    lines = []
    for e in eps:
        body = e["body"] if len(e["body"]) <= ROUTE_BODY_CHARS else e["body"][:ROUTE_BODY_CHARS] + "……"
        lines.append(f"[E{e['id']}] {_when(e['ts'], e['day'])}《{e['title']}》｜话题：{'、'.join(e['topics'])}｜情绪：{e['mood']}\n{body}")
    prompt = (tpl.replace("{catalog}", cat).replace("{rejected}", "、".join(rejected) or "（没有）")
              .replace("{episodes}", "\n\n".join(lines)))
    return prompt, version


def _ref(v, prefix):
    m = re.fullmatch(rf"\s*{prefix}?(\d+)\s*", str(v or ""), re.I)
    return int(m.group(1)) if m else None


def apply_route(conn, data, eps, arcs, rejected, model=None):
    """把模型的归卷结果写进库（调用方负责事务）。返回 {"assigned": n, "new_arcs": [id], "notes": [...]}"""
    ep_ids = {e["id"]: e for e in eps}
    live = {a["id"] for a in arcs}
    titles = {_norm_title(t): aid for aid, t in conn.execute(
        "SELECT id, title FROM arcs WHERE status IN ('candidate','proposed','active','dormant')")}
    rejected_n = {_norm_title(t) for t in rejected}
    count = {eid: 0 for eid in ep_ids}
    notes, new_arcs, assigned = [], [], 0
    now = _now()

    def add(arc_id, eid, label):
        nonlocal assigned
        if count[eid] >= MAX_ARCS_PER_EPISODE:
            return
        e = ep_ids[eid]
        cur = conn.execute("INSERT OR IGNORE INTO arc_materials (arc_id, episode_id, label, ts, day, narrated, added_at) "
                           "VALUES (?, ?, ?, ?, ?, 0, ?)", (arc_id, eid, str(label or "").strip()[:40], e["ts"], e["day"], now))
        if cur.rowcount:
            count[eid] += 1
            assigned += 1

    for item in data.get("assign") or []:
        if not isinstance(item, dict):
            continue
        eid = _ref(item.get("episode"), "E")
        if eid not in ep_ids:
            notes.append(f"片段编号不对：{item.get('episode')}")
            continue
        for a in (item.get("arcs") or [])[:MAX_ARCS_PER_EPISODE]:
            aid = _ref(a.get("arc") if isinstance(a, dict) else a, "A")
            if aid in live:
                add(aid, eid, a.get("label") if isinstance(a, dict) else "")
            else:
                notes.append(f"卷编号不对：{a}")
    for item in data.get("new") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:16]
        members = [(_ref(x.get("episode") if isinstance(x, dict) else x, "E"), x.get("label", "") if isinstance(x, dict) else "")
                   for x in (item.get("episodes") or [])]
        members = [(eid, lb) for eid, lb in members if eid in ep_ids]
        if not title or not members:
            continue
        key = _norm_title(title)
        if key in rejected_n:
            notes.append(f"提了拒绝过的主题，跳过：{title}")
            continue
        aid = titles.get(key)
        if aid is None:
            cur = conn.execute("INSERT INTO arcs (title, theme, status, sections, version, needs_rebuild, created_at, updated_at) "
                               "VALUES (?, ?, 'candidate', '[]', 0, 0, ?, ?)",
                               (title, str(item.get("theme") or "").strip()[:80], now, now))
            aid = cur.lastrowid
            titles[key] = aid
            new_arcs.append(aid)
        for eid, lb in members:
            add(aid, eid, lb)
    for eid in ep_ids:
        conn.execute("INSERT OR REPLACE INTO arc_routing (episode_id, routed_at, model, result) VALUES (?, ?, ?, ?)",
                     (eid, now, model, json.dumps({"arcs": count[eid]}, ensure_ascii=False)))
    return {"assigned": assigned, "new_arcs": new_arcs, "notes": notes}


async def route_arcs(llm_fn=None, now=None):
    """归卷任务：整理作废的素材 → 一批一批归卷 → 更新卷的状态 → 投递要写的卷。模型失败时抛异常（已经写进库的批次保留）。"""
    import jobs
    from app.config import SQLITE_PATH
    llm_fn = llm_fn or route_llm
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        removed = cleanup(conn)
        conn.commit()
    finally:
        conn.close()
    batches, routed, new_arcs, notes = 0, 0, [], []
    while batches < MAX_ROUTE_BATCHES:
        conn = get_conn()
        try:
            eps = unrouted(conn, ROUTE_BATCH)
            arcs, rejected = catalog(conn)
        finally:
            conn.close()
        if not eps:
            break
        prompt, version = build_route_prompt(arcs, rejected, eps)
        text, model = await llm_fn(prompt)
        if not text:
            raise RuntimeError("归卷：调用模型失败（没有返回内容），稍后重试")
        data = parse_json(text)
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = apply_route(conn, data, eps, arcs, rejected, model=model)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        batches += 1
        routed += len(eps)
        new_arcs += r["new_arcs"]
        notes += r["notes"]
    more = False
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposed = refresh_status(conn, now)
        conn.commit()
        more = bool(unrouted(conn, 1))
    finally:
        conn.close()
    if more:      # 接着归剩下的。不能用 "route_arcs" 去重：这个任务自己正在跑，会被当成重复，新任务投不进去
        jobs.enqueue(SQLITE_PATH, "route_arcs", payload={}, dedupe_key="route_arcs:next", source="auto", delay_sec=5)
    narrate = schedule_narrations()
    return {"status": "ok", "batches": batches, "routed": routed, "new_arcs": new_arcs, "proposed": proposed,
            "removed": removed, "narrate": narrate, "more": more, "notes": notes[:20]}


def enqueue_route(source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    job, created = jobs.enqueue(SQLITE_PATH, "route_arcs", payload={}, dedupe_key="route_arcs", source=source)
    return created


def pending_route_count():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM episodes e LEFT JOIN arc_routing r ON r.episode_id = e.id "
                            "WHERE e.status='active' AND r.episode_id IS NULL").fetchone()[0]
    finally:
        conn.close()


# ---------- 写卷 ----------

def _arc_row(conn, arc_id):
    r = conn.execute("SELECT id, title, theme, status, summary, sections, version, needs_rebuild, issues, model, "
                     "prompt_version, created_at, updated_at, confirmed_at, last_material_ts FROM arcs WHERE id=?",
                     (int(arc_id),)).fetchone()
    if not r:
        return None
    keys = ("id", "title", "theme", "status", "summary", "sections", "version", "needs_rebuild", "issues", "model",
            "prompt_version", "created_at", "updated_at", "confirmed_at", "last_material_ts")
    a = dict(zip(keys, r))
    a["sections"] = _loads(a["sections"], [])
    a["issues"] = _loads(a["issues"], [])
    return a


def _materials(conn, arc_id):
    rows = conn.execute("SELECT m.episode_id, m.label, m.ts, m.day, m.narrated, e.title, e.body, e.refs FROM arc_materials m "
                        "JOIN episodes e ON e.id = m.episode_id AND e.status='active' WHERE m.arc_id=? "
                        "ORDER BY m.ts, m.episode_id", (int(arc_id),)).fetchall()
    return [{"episode_id": r[0], "label": r[1] or "", "ts": r[2], "day": r[3], "narrated": r[4], "title": r[5],
             "body": r[6] or "", "l0_refs": _loads(r[7], [])} for r in rows]


def schedule_narrations():
    """要写的卷：进行中或休眠，并且（要整篇重写 / 还没写过 / 新素材够数）。投递 narrate_arc，返回卷 id。"""
    import jobs
    from app.config import SQLITE_PATH
    need = cfg()["narrate_min_new"]
    conn = get_conn()
    try:
        rows = conn.execute("SELECT a.id, a.needs_rebuild, a.version, SUM(CASE WHEN m.narrated=0 THEN 1 ELSE 0 END), COUNT(m.episode_id) "
                            "FROM arcs a JOIN arc_materials m ON m.arc_id = a.id JOIN episodes e ON e.id = m.episode_id "
                            "AND e.status='active' WHERE a.status IN ('active','dormant') GROUP BY a.id").fetchall()
    finally:
        conn.close()
    out = []
    for aid, rebuild, version, new, total in rows:
        if total and (rebuild or version == 0 or (new or 0) >= need):
            jobs.enqueue(SQLITE_PATH, "narrate_arc", payload={"arc_id": aid}, dedupe_key=f"narrate_arc:{aid}", source="auto")
            out.append(aid)
    return out


def _sources(conn, mats):
    """每个素材片段的原文（片段正文 + 它引用的 L0 原文），校验引号用"""
    ids = sorted({int(i) for m in mats for i in m["l0_refs"]})
    l0 = {}
    if ids:
        for mid, content in conn.execute(f"SELECT id, content FROM l0_messages WHERE id IN ({','.join('?' * len(ids))})", ids):
            l0[mid] = content or ""
    return {m["episode_id"]: m["body"] + "\n" + "\n".join(l0.get(int(i), "") for i in m["l0_refs"]) for m in mats}


def build_write_prompt(arc, sections, batch, summary):
    tpl, version = _prompt("arc_write")
    if sections:
        done = "\n\n".join(f"【{s['title']}】\n{s['body']}" for s in sections)
        done = ("……" + done[-EXISTING_CHARS:]) if len(done) > EXISTING_CHARS else done
        if summary:
            done = f"（目前的一句话：{summary}）\n\n" + done
    else:
        done = "（还没有，这是第一次写）"
    mats = "\n\n".join(f"[#{k}] {_when(m['ts'], m['day'])}《{m['title']}》" + (f"（{m['label']}）" if m["label"] else "") + f"\n{m['body']}"
                       for k, m in enumerate(batch, 1))
    prompt = (tpl.replace("{title}", arc["title"]).replace("{theme}", arc["theme"] or "（没有说明）")
              .replace("{existing}", done).replace("{materials}", mats))
    return prompt, version


def verify_section(sec, ref_map, src_text):
    """ref_map：编号 → 片段 id；src_text：片段 id → 原文。返回 (校验后的小节 或 None, 问题列表)"""
    issues = []
    title = _short_title(sec.get("title"))
    raw = str(sec.get("body") or "").strip()
    if not title or not raw:
        return None, ["小节没有标题或正文"]
    sents = [s for s in E.SENT.findall(raw) if s.strip()]
    kept, used = [], []
    for s in sents:
        refs = [int(x) for x in E.REF.findall(s)]
        bad = [r for r in refs if r not in ref_map]
        if bad:
            issues.append(f"出处不存在 #{bad[0]}：{E.REF.sub('', s).strip()[:30]}")
            continue
        quotes = E.QUOTE.findall(E.REF.sub("", s))
        pool = [E._norm(src_text[ref_map[r]]) for r in refs] if refs else [E._norm(v) for v in src_text.values()]
        missing = [q for q in quotes if E._norm(q) and not any(E._norm(q) in x for x in pool)]
        if missing:
            issues.append(f"原话对不上「{missing[0][:20]}」")
            continue
        kept.append(s.strip())
        for r in refs:
            if ref_map[r] not in used:
                used.append(ref_map[r])
    used.sort(key=lambda eid: min(k for k, v in ref_map.items() if v == eid))
    order = {eid: k for k, eid in enumerate(used, 1)}
    with_refs = E.REF.sub(lambda m: f"[#{order.get(ref_map.get(int(m.group(1))), m.group(1))}]", "".join(kept).strip())
    body = re.sub(r"[ \t]{2,}", " ", E.REF.sub("", with_refs)).strip()
    if not used:
        return None, issues + [f"「{title}」一个出处都没有标，不要"]
    if len(kept) * 2 < len(sents) or len(body) < MIN_SECTION_CHARS:
        return None, issues + [f"「{title}」去掉对不上的句子以后剩得太少，不要"]
    return {"title": title, "body": body, "body_with_refs": with_refs, "refs": used}, issues


def clean_summary(text, src_text):
    s = E.REF.sub("", str(text or "")).strip()
    if not s:
        return None
    pool = [E._norm(v) for v in src_text.values()]
    if any(E._norm(q) and not any(E._norm(q) in x for x in pool) for q in E.QUOTE.findall(s)):
        return None
    if len(s) > SUMMARY_MAX:
        cut = max(s.rfind(p, 0, SUMMARY_MAX) for p in "。！？")
        s = s[:cut + 1] if cut > 20 else s[:SUMMARY_MAX] + "……"
    return s


async def narrate_arc(arc_id, llm_fn=None):
    """写卷：要整篇重写（或第一次写）时从头写；否则只把没写进去的新片段追加成新小节。
    每批最多 WRITE_BATCH 个片段；一批没通过校验就带着问题让模型重写一次，还不行就跳过这批（素材标成「没写进去」）。
    全部写完后一次发布（version + 1，留一份历史）；模型调用失败时抛异常，不写半截。"""
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        arc = _arc_row(conn, arc_id)
        if not arc or arc["status"] not in WRITTEN:
            return {"status": "skipped", "message": "这个卷不存在，或者还没确认"}
        mats = _materials(conn, arc_id)
        src = _sources(conn, mats)
    finally:
        conn.close()
    rebuild = bool(arc["needs_rebuild"]) or arc["version"] == 0 or not arc["sections"]
    sections = [] if rebuild else list(arc["sections"])
    pending = mats if rebuild else [m for m in mats if m["narrated"] == 0]
    if not pending:
        return {"status": "skipped", "message": "没有新素材"}
    summary, issues, ok_ids, bad_ids, model_used, version_used = (None if rebuild else arc["summary"]), [], [], [], None, None
    for i in range(0, len(pending), WRITE_BATCH):
        batch = pending[i:i + WRITE_BATCH]
        ref_map = {k: m["episode_id"] for k, m in enumerate(batch, 1)}
        prompt, version_used = build_write_prompt(arc, sections, batch, summary)
        accepted, round_issues = [], []
        for attempt in range(2):
            p = prompt if attempt == 0 else (prompt + "\n\n【上一次写的没通过校验，问题如下，请改正后重写】\n"
                                             + "\n".join(f"- {x}" for x in round_issues[:10]))
            text, model_used = await llm_fn(p)
            if not text:
                raise RuntimeError("写卷：调用模型失败（没有返回内容），稍后重试")
            data = parse_json(text)
            accepted, round_issues = [], []
            for sec in (data.get("sections") or [])[:2]:
                if not isinstance(sec, dict):
                    continue
                clean, iss = verify_section(sec, ref_map, src)
                round_issues += iss
                if clean:
                    clean["created_at"] = _now()
                    accepted.append(clean)
            if accepted:
                s2 = clean_summary(data.get("summary"), src)
                summary = s2 or summary
                break
        issues += round_issues
        if accepted:
            sections += accepted
            ok_ids += [m["episode_id"] for m in batch]
        else:
            bad_ids += [m["episode_id"] for m in batch]
    if not ok_ids:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"UPDATE arc_materials SET narrated=2 WHERE arc_id=? AND episode_id IN ({','.join('?' * len(bad_ids))})",
                         (arc_id, *bad_ids))
            conn.execute("UPDATE arcs SET issues=?, updated_at=? WHERE id=?", (json.dumps(issues[:30], ensure_ascii=False), _now(), arc_id))
            conn.commit()
        finally:
            conn.close()
        return {"status": "rejected", "message": "写出来的都没通过校验，保留旧版本", "issues": issues[:30]}
    now = _now()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT version FROM arcs WHERE id=?", (arc_id,)).fetchone()
        version = (cur[0] or 0) + 1
        alive = {r[0] for r in conn.execute(
            f"SELECT id FROM episodes WHERE status='active' AND id IN ({','.join('?' * len(_cited(sections))) or 'NULL'})",
            tuple(_cited(sections)))}
        still_stale = 1 if _cited(sections) - alive else 0
        if rebuild:
            conn.execute("UPDATE arc_materials SET narrated=0 WHERE arc_id=?", (arc_id,))
        if ok_ids:
            conn.execute(f"UPDATE arc_materials SET narrated=1 WHERE arc_id=? AND episode_id IN ({','.join('?' * len(ok_ids))})",
                         (arc_id, *ok_ids))
        if bad_ids:
            conn.execute(f"UPDATE arc_materials SET narrated=2 WHERE arc_id=? AND episode_id IN ({','.join('?' * len(bad_ids))})",
                         (arc_id, *bad_ids))
        sec_json = json.dumps(sections, ensure_ascii=False)
        conn.execute("UPDATE arcs SET sections=?, summary=?, version=?, needs_rebuild=?, issues=?, model=?, prompt_version=?, "
                     "updated_at=? WHERE id=?", (sec_json, summary, version, still_stale, json.dumps(issues[:30], ensure_ascii=False),
                                                 model_used, version_used, now, arc_id))
        conn.execute("INSERT INTO arc_versions (arc_id, version, summary, sections, model, prompt_version, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (arc_id, version, summary, sec_json, model_used, version_used, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": "ok", "arc_id": arc_id, "title": arc["title"], "version": version, "rebuild": rebuild,
            "sections": len(sections), "written": len(ok_ids), "skipped": len(bad_ids), "issues": issues[:30]}


# ---------- 你在 Dashboard 上的操作 ----------

def _set(arc_id, sql, args, allowed):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT status FROM arcs WHERE id=?", (int(arc_id),)).fetchone()
        if not r:
            conn.rollback()
            raise ValueError("没有这个卷")
        if r[0] not in allowed:
            conn.rollback()
            raise ValueError(f"这个卷现在是「{STATUS_CN.get(r[0], r[0])}」，不能这样操作")
        conn.execute(sql, (*args, int(arc_id)))
        conn.commit()
    finally:
        conn.close()


def _clean_title(title):
    t = str(title or "").strip()
    if not t or len(t) > 20:
        raise ValueError("标题不能为空，也不能超过 20 个字")
    return t


def confirm(arc_id, title=None, theme=None):
    """确认（候选或待确认 → 进行中），可以顺便改名；然后投递写卷"""
    import jobs
    from app.config import SQLITE_PATH
    sets, args = ["status='active'", "confirmed_at=?", "updated_at=?", "needs_rebuild=1"], [_now(), _now()]
    if title is not None:
        sets.append("title=?")
        args.append(_clean_title(title))
    if theme is not None:
        sets.append("theme=?")
        args.append(str(theme).strip()[:80])
    _set(arc_id, f"UPDATE arcs SET {', '.join(sets)} WHERE id=?", args, ("candidate", "proposed"))
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        refresh_status(conn)          # 很久没有新素材的，确认后直接是「休眠」（照样会写）
        conn.commit()
    finally:
        conn.close()
    jobs.enqueue(SQLITE_PATH, "narrate_arc", payload={"arc_id": int(arc_id)}, dedupe_key=f"narrate_arc:{int(arc_id)}", source="dashboard")


def reject(arc_id):
    _set(arc_id, "UPDATE arcs SET status='rejected', updated_at=? WHERE id=?", [_now()], ("candidate", "proposed"))


def restore(arc_id):
    """撤销拒绝：回到候选（够数的话下次归卷后变成待确认）"""
    _set(arc_id, "UPDATE arcs SET status='candidate', updated_at=? WHERE id=?", [_now()], ("rejected",))
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        refresh_status(conn)
        conn.commit()
    finally:
        conn.close()


def rename(arc_id, title=None, theme=None):
    sets, args = ["updated_at=?"], [_now()]
    if title is not None:
        sets.append("title=?")
        args.append(_clean_title(title))
    if theme is not None:
        sets.append("theme=?")
        args.append(str(theme).strip()[:80])
    _set(arc_id, f"UPDATE arcs SET {', '.join(sets)} WHERE id=?", args, LIVE)


def rebuild(arc_id):
    import jobs
    from app.config import SQLITE_PATH
    _set(arc_id, "UPDATE arcs SET needs_rebuild=1, updated_at=? WHERE id=?", [_now()], WRITTEN)
    jobs.enqueue(SQLITE_PATH, "narrate_arc", payload={"arc_id": int(arc_id)}, dedupe_key=f"narrate_arc:{int(arc_id)}", source="dashboard")


# ---------- 查看 ----------

def list_arcs():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT a.id, a.title, a.theme, a.status, a.summary, a.version, a.needs_rebuild, a.updated_at, a.confirmed_at, "
            "COUNT(e.id), COUNT(DISTINCT m.day), MIN(m.day), MAX(m.day), SUM(CASE WHEN e.id IS NOT NULL AND m.narrated=0 THEN 1 ELSE 0 END) "
            "FROM arcs a LEFT JOIN arc_materials m ON m.arc_id = a.id LEFT JOIN episodes e ON e.id = m.episode_id AND e.status='active' "
            "GROUP BY a.id ORDER BY COALESCE(a.last_material_ts, a.created_at) DESC").fetchall()
    finally:
        conn.close()
    keys = ("id", "title", "theme", "status", "summary", "version", "needs_rebuild", "updated_at", "confirmed_at",
            "episodes", "days", "first_day", "last_day", "unwritten")
    return [dict(zip(keys, r)) for r in rows]


def get_arc(arc_id):
    conn = get_conn()
    try:
        a = _arc_row(conn, arc_id)
        if not a:
            return None
        a["materials"] = [{k: m[k] for k in ("episode_id", "label", "ts", "day", "narrated", "title")} for m in _materials(conn, arc_id)]
        a["versions"] = [{"version": r[0], "created_at": r[1], "model": r[2]} for r in conn.execute(
            "SELECT version, created_at, model FROM arc_versions WHERE arc_id=? ORDER BY version DESC LIMIT 20", (int(arc_id),))]
        titles = {}
        ids = sorted(_cited(a["sections"]))
        if ids:
            for eid, day, title, status in conn.execute(
                    f"SELECT id, day, title, status FROM episodes WHERE id IN ({','.join('?' * len(ids))})", ids):
                titles[eid] = {"id": eid, "day": day, "title": title, "status": status}
        for s in a["sections"]:
            s["sources"] = [dict(titles.get(int(i), {"id": int(i)}), n=k) for k, i in enumerate(s.get("refs") or [], 1)]
        return a
    finally:
        conn.close()
