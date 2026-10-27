"""章节（方案 6.10 节，Phase 4 第七步）：按时间走的共同经历，一章是关系里的一个时期。以后接替现在的「共同经历叙事」。

- 结构：一串章节，按日期首尾相接；最后一章是「进行中」（没有结束日期），前面的都「已封存」。
  一章的素材 = 日期落在这一章里的每日片段（不另外分配，按日期算）。
- 写（write_chapter 任务）：片段整理好以后，进行中的那章把还没写进去的新片段追加成 1～2 个新小节（每批最多 12 个片段）；
  校验和叙事卷一样（出处必须存在、引号里的原话必须能在片段原文里找到，对不上的句子删掉；整批不通过就重写一次，还不行跳过这批）。
  章节范围变了、或者引用过的片段作废了，就整章重写。一次做不完会接着投递一个任务。
- 封存：你在 Dashboard 上点「这一章到这里」（最推荐，章节的边界本身就是关系的意义标记），可以顺便起标题；
  封存后写一个两三句话的摘要（没起标题的话顺便起一个），后面的日子进入新的一章，新一章的第一小节会先接上上一章。
- 分章提议（propose_chapters 任务）：进行中的这章太长了（默认跨了 30 天以上、写了 2500 字以上），或者你点「帮我分章」，
  模型按关系和生活的变化提议在哪里分开（每章至少 7 天）；你确认（可以改标题和日期）以后才生效。
  以后导入旧聊天、补上更早的片段以后，进行中的那章会从最早的一天重写，然后就会提议分章。
- 导入旧聊天的时候（一下子补进很多更早的片段）：
  - 每日片段还在排队整理时先不写，整理完再一起写，免得一边补片段一边反复重写；
  - 补进来的片段比这一章已经写到的地方早两天以上，就整章重写，免得顺序乱掉；
  - 进行中的这章要从头写、又跨得很久时，先提议分章，你确认（或者点「不用了」，或者 3 天没处理）以后再写，免得写完一整章又拆开重写；
  - 新的一章等上一章写完、有了摘要再写，这样开头能接上上一章（最多等 40 分钟）。
- 目录 index_text()：每章一行（标题、时间、摘要），以后放进聊天；旧章节需要时再按需读。
- 旧的「共同经历叙事」原样保留，在 Dashboard 上和章节放在一起对比；现在放进聊天的还是它。
放进聊天的是章节目录 + 进行中的这一章（app/compose.py）。
"""
import json
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import digests as D
from app.pipeline import episodes as E

STATUS_CN = {"current": "进行中", "sealed": "已封存"}
WRITE_BATCH = 12
MAX_BATCHES_PER_JOB = 8
EXISTING_CHARS = 1500
MAX_TOKENS = 16000
MAX_SPLIT = 12
DAY_LINE_CHARS = 150
INDEX_MAX = 800
TITLE_MAX = 20
LATE_DAYS = 2           # 补进来的片段比已经写到的地方早这么多天以上：整章重写
HOLD_DAYS = 3           # 等你确认分章提议，最多先不写这么多天
MAX_WAITS = 20          # 新的一章等上一章写完：最多等这么多次
WAIT_SEC = 120
WAITING_CN = {"settling": "每日片段还在整理，整理完再写章节",
              "proposing": "进行中的这章跨得很久，正在看要不要先分章，分好再写",
              "pending": "有一个分章提议等你确认：确认或者点「不用了」以后才开始写（3 天没处理就先照现在的样子写）"}
WRITE_PURPOSES = ("chapter", "arc", "episode", "narrative", "summary")


def cfg():
    return app_settings.get("chapters")


def _now():
    return timeutil.utc_now_str()


async def default_llm(prompt):
    """用「模块模型配置」里的「章节」；没设置就依次用「叙事卷」「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, WRITE_PURPOSES, MAX_TOKENS)


def _d(v):
    return E.to_day(v) if v else None


def _label(day, year=False):
    d = _d(day)
    return f"{d.year}年{d.month}月{d.day}日" if year else f"{d.month}月{d.day}日"


def _range_label(ch):
    start = _d(ch["start_day"])
    if not ch["end_day"]:
        return f"{_label(start, True)}至今"
    end = _d(ch["end_day"])
    return f"{_label(start, True)}到{_label(end, end.year != start.year)}"


# ---------- 读库 ----------

COLS = ("id, n, title, abstract, summary, start_day, end_day, status, sections, version, needs_rebuild, abstract_version, "
        "issues, model, prompt_version, created_at, updated_at, sealed_at")
KEYS = [c.strip() for c in COLS.split(",")]


def _row(r):
    c = dict(zip(KEYS, r))
    c["sections"] = A._loads(c["sections"], [])
    c["issues"] = A._loads(c["issues"], [])
    return c


def _chapters(conn):
    return [_row(r) for r in conn.execute(f"SELECT {COLS} FROM chapters ORDER BY n")]


def _get(conn, cid):
    r = conn.execute(f"SELECT {COLS} FROM chapters WHERE id=?", (int(cid),)).fetchone()
    if not r:
        raise ValueError("没有这一章")
    return _row(r)


def _current(conn):
    r = conn.execute(f"SELECT {COLS} FROM chapters WHERE status='current' ORDER BY n DESC LIMIT 1").fetchone()
    return _row(r) if r else None


def _episodes_in(conn, start, end=None):
    sql = "SELECT id, day, start_ts, title, body, refs, mood FROM episodes WHERE status='active' AND day >= ?"
    args = [str(start)]
    if end:
        sql += " AND day <= ?"
        args.append(str(end))
    rows = conn.execute(sql + " ORDER BY day, start_ts, id", args).fetchall()
    return [{"episode_id": r[0], "day": r[1], "ts": r[2], "title": r[3], "body": r[4] or "", "l0_refs": A._loads(r[5], []),
             "mood": r[6] or ""} for r in rows]


def _done_ids(conn, cid):
    return {r[0]: r[1] for r in conn.execute("SELECT episode_id, status FROM chapter_episodes WHERE chapter_id=?", (cid,))}


def unwritten(conn, ch):
    done = _done_ids(conn, ch["id"])
    return [e for e in _episodes_in(conn, ch["start_day"], ch["end_day"]) if e["episode_id"] not in done]


def _cited(sections):
    return {int(i) for s in sections for i in (s.get("refs") or [])}


def _chars(sections):
    return sum(len(s.get("body") or "") for s in sections)


def needs_abstract(ch):
    """封存的章、有正文、这一版还没写过摘要（abstract_version 是负数表示这一版试过、没写成，等内容再变）"""
    return (ch["status"] == "sealed" and bool(ch["sections"]) and ch["version"] > 0
            and ch["abstract_version"] not in (ch["version"], -ch["version"]))


# ---------- 结构 ----------

def _insert(conn, n, start, end=None, status="current", title=None):
    now = _now()
    cur = conn.execute("INSERT INTO chapters (n, title, start_day, end_day, status, sections, version, needs_rebuild, abstract_version, "
                       "created_at, updated_at, sealed_at) VALUES (?, ?, ?, ?, ?, '[]', 0, 0, 0, ?, ?, ?)",
                       (n, title, str(start), str(end) if end else None, status, now, now, now if status == "sealed" else None))
    return cur.lastrowid


def _renumber(conn):
    for k, (cid,) in enumerate(conn.execute("SELECT id FROM chapters ORDER BY start_day, n").fetchall(), 1):
        conn.execute("UPDATE chapters SET n=? WHERE id=?", (k, cid))


def ensure_structure(conn):
    """有片段就保证有章节：第一次建第 1 章（进行中）；有了比第一章还早的片段（比如导入了旧聊天），
    第一章还在进行中就往前扩、整章重写；第一章已经封存了，就在前面补一章「更早的时候」。返回做了什么。"""
    r = conn.execute("SELECT MIN(day) FROM episodes WHERE status='active'").fetchone()
    if not r or not r[0]:
        return []
    earliest = _d(r[0])
    chs = _chapters(conn)
    if not chs:
        _insert(conn, 1, earliest)
        return ["created"]
    out = []
    first = chs[0]
    if earliest < _d(first["start_day"]):
        if first["status"] == "current":
            conn.execute("UPDATE chapters SET start_day=?, needs_rebuild=1, updated_at=? WHERE id=?", (str(earliest), _now(), first["id"]))
            out.append("extended")
        else:
            _insert(conn, 0, earliest, _d(first["start_day"]) - timedelta(days=1), status="sealed")
            _renumber(conn)
            out.append("prepended")
    if not any(c["status"] == "current" for c in _chapters(conn)):
        last = _chapters(conn)[-1]
        _insert(conn, last["n"] + 1, _d(last["end_day"]) + timedelta(days=1))
        out.append("current_added")
    return out


def cleanup(conn):
    """片段作废了、或者不在这一章的日期里了：从「写过的」里去掉；正文引用过的话整章重写"""
    touched = {}
    rows = conn.execute("SELECT ce.chapter_id, ce.episode_id, e.status, e.day, c.start_day, c.end_day FROM chapter_episodes ce "
                        "JOIN chapters c ON c.id = ce.chapter_id LEFT JOIN episodes e ON e.id = ce.episode_id").fetchall()
    for cid, eid, status, day, start, end in rows:
        if status != "active" or not day or day < start or (end and day > end):
            conn.execute("DELETE FROM chapter_episodes WHERE chapter_id=? AND episode_id=?", (cid, eid))
            touched.setdefault(cid, set()).add(eid)
    for cid, eids in touched.items():
        r = conn.execute("SELECT sections FROM chapters WHERE id=?", (cid,)).fetchone()
        if r and _cited(A._loads(r[0], [])) & eids:
            conn.execute("UPDATE chapters SET needs_rebuild=1 WHERE id=?", (cid,))
    return {c: sorted(e) for c, e in touched.items()}


def late_arrivals(conn):
    """补进来的片段比这一章已经写到的地方早 LATE_DAYS 天以上（比如导入了中间某段时间的旧聊天）：标成整章重写，免得顺序乱掉。
    早一两天的（比如前一天的对话后来又整理了一次）就接着往后写。返回标了的章。"""
    out = []
    for ch in _chapters(conn):
        if ch["needs_rebuild"]:
            continue
        done = _done_ids(conn, ch["id"])
        eps = _episodes_in(conn, ch["start_day"], ch["end_day"])
        written = [e["day"] for e in eps if done.get(e["episode_id"]) == 1]
        if not written:
            continue
        edge = _d(max(written)) - timedelta(days=LATE_DAYS)
        if any(e["episode_id"] not in done and _d(e["day"]) < edge for e in eps):
            conn.execute("UPDATE chapters SET needs_rebuild=1, updated_at=? WHERE id=?", (_now(), ch["id"]))
            out.append(ch["id"])
    return out


def settling(conn):
    """还有每日片段在排队整理（比如刚导入了旧聊天）"""
    return bool(conn.execute("SELECT 1 FROM pipeline_jobs WHERE kind='settle_day' AND status='queued' LIMIT 1").fetchone())


def _span(conn, ch):
    last = conn.execute("SELECT MAX(day) FROM episodes WHERE status='active' AND day >= ?" + (" AND day <= ?" if ch["end_day"] else ""),
                        (ch["start_day"], ch["end_day"]) if ch["end_day"] else (ch["start_day"],)).fetchone()[0]
    return (_d(last) - _d(ch["start_day"])).days + 1 if last else 0


def _long(conn, ch, c):
    return _span(conn, ch) >= max(c["seal_after_days"], 2 * c["min_chapter_days"])


def _fresh(ch):
    """要从头写（还没写过、或者要整章重写）"""
    return bool(ch["needs_rebuild"] or not ch["sections"])


def _propose_job(conn, exclude_job=None):
    """最近一次分章提议任务：'active'（排队或正在跑）/ 'dead'（24 小时内失败到停止重试）/ None。
    exclude_job：提议任务自己跑完时调用 schedule()，不算它自己"""
    r = conn.execute("SELECT status, updated_at FROM pipeline_jobs WHERE kind='propose_chapters' AND id != ? ORDER BY id DESC LIMIT 1",
                     (exclude_job or 0,)).fetchone()
    if not r:
        return None
    if r[0] in ("queued", "running"):
        return "active"
    t = timeutil.parse_ts(r[1]) if r[0] == "dead" else None
    return "dead" if t and (timeutil.utc_now() - t).total_seconds() < 24 * 3600 else None


def hold(conn, ch, c=None, exclude_job=None):
    """进行中的这章要从头写、又跨得很久（比如导入了旧聊天）：先看要不要分章，免得写完一整章又拆开重写。
    正在提议（'proposing'）或者有 HOLD_DAYS 天内等你确认的提议（'pending'）时先不写；否则返回 None，照常写。"""
    c = c or cfg()
    if not ch or ch["status"] != "current" or not _fresh(ch) or not _long(conn, ch, c):
        return None
    if _propose_job(conn, exclude_job) == "active":
        return "proposing"
    if conn.execute("SELECT 1 FROM chapter_proposals WHERE chapter_id=? AND status='pending' AND datetime(created_at) > datetime('now', ?)",
                    (ch["id"], f"-{HOLD_DAYS} days")).fetchone():
        return "pending"
    return None


def schedule(now=None, exclude_job=None):
    """理顺结构 → 投递要写的章（有没写进去的片段、要整章重写、封存了还没写摘要）→ 进行中的章太长了就投递分章提议。
    每日片段还在排队整理时只理顺结构、不投递；进行中的这章要从头写又跨得很久时，先投递分章提议、这章先不写。"""
    import jobs
    from app.config import SQLITE_PATH
    c = cfg()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changes = ensure_structure(conn)
        cleanup(conn)
        if late_arrivals(conn):
            changes.append("late")
        conn.commit()
        if settling(conn):
            return {"changes": changes, "queued": [], "waiting": "settling"}
        todo, propose, waiting = [], False, None
        cur = _current(conn)
        if cur:
            recent = conn.execute("SELECT 1 FROM chapter_proposals WHERE chapter_id=? AND (status='pending' OR "
                                  "datetime(created_at) > datetime('now', '-7 days'))", (cur["id"],)).fetchone()
            propose = (_long(conn, cur, c) and not recent and _propose_job(conn, exclude_job) is None
                       and (_fresh(cur) or _chars(cur["sections"]) >= c["seal_min_chars"]))
            waiting = "proposing" if propose and _fresh(cur) else hold(conn, cur, c, exclude_job)
        for ch in _chapters(conn):
            if waiting and ch["id"] == cur["id"]:
                continue
            pend = unwritten(conn, ch)
            has_eps = bool(_episodes_in(conn, ch["start_day"], ch["end_day"]))
            if has_eps and (pend or ch["needs_rebuild"] or needs_abstract(ch)):
                todo.append(ch["id"])
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    out = []
    for cid in todo:
        if jobs.enqueue(SQLITE_PATH, "write_chapter", payload={"chapter_id": cid}, dedupe_key=f"write_chapter:{cid}", source="auto")[1]:
            out.append(f"write_chapter:{cid}")
    if propose and jobs.enqueue(SQLITE_PATH, "propose_chapters", payload={}, dedupe_key="propose_chapters", source="auto")[1]:
        out.append("propose_chapters")
    return {"changes": changes, "queued": out, "waiting": waiting}


def pending_work():
    """还有没有要写的（给定时检查用，只读）"""
    conn = get_conn()
    try:
        if not _chapters(conn):
            return bool(conn.execute("SELECT 1 FROM episodes WHERE status='active' LIMIT 1").fetchone())
        r = conn.execute("SELECT MIN(day) FROM episodes WHERE status='active'").fetchone()
        if r and r[0] and r[0] < _chapters(conn)[0]["start_day"]:
            return True
        for ch in _chapters(conn):
            if unwritten(conn, ch) or ch["needs_rebuild"] or needs_abstract(ch):
                return True
        return False
    finally:
        conn.close()


# ---------- 写 ----------

def build_write_prompt(ch, prev, sections, batch, summary):
    tpl, version = A._prompt("chapter_write")
    name = f"第 {ch['n']} 章" + (f"「{ch['title']}」" if ch["title"] else "")
    if sections:
        done = "\n\n".join(f"【{s['title']}】\n{s['body']}" for s in sections)
        done = ("……" + done[-EXISTING_CHARS:]) if len(done) > EXISTING_CHARS else done
        if summary:
            done = f"（这一章到现在讲的是：{summary}）\n\n" + done
    else:
        done = "（还没有，这是这一章的开头）"
    prev_line = f"上一章：第 {prev['n']} 章「{prev['title'] or ''}」——{prev['abstract']}" if prev and prev.get("abstract") else ""
    opening = ("这是新一章的开头：第一小节先用一两句话接上上一章，再写这一章的事。" if prev and prev.get("abstract") and not sections else "")
    mats = "\n\n".join(f"[#{k}] {A._when(m['ts'], m['day'])}《{m['title']}》（{m['mood']}）\n{m['body']}" for k, m in enumerate(batch, 1))
    prompt = (tpl.replace("{chapter}", name).replace("{range}", _range_label(ch)).replace("{prev}", prev_line)
              .replace("{existing}", done).replace("{opening}", opening).replace("{materials}", mats))
    return prompt, version


def _save_progress(cid, sections, summary, ok_ids, bad_ids, issues, model, version):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE chapters SET sections=?, summary=?, issues=?, model=COALESCE(?, model), prompt_version=COALESCE(?, prompt_version), "
                     "updated_at=? WHERE id=?", (json.dumps(sections, ensure_ascii=False), summary, json.dumps(issues[-30:], ensure_ascii=False),
                                                 model, version, _now(), cid))
        for eid in ok_ids:
            conn.execute("INSERT OR REPLACE INTO chapter_episodes (chapter_id, episode_id, status) VALUES (?, ?, 1)", (cid, eid))
        for eid in bad_ids:
            conn.execute("INSERT OR REPLACE INTO chapter_episodes (chapter_id, episode_id, status) VALUES (?, ?, 2)", (cid, eid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _abstract(ch, llm_fn):
    """封存的章：写标题（你没起的话）和两三句话的摘要。返回 (title, abstract, 问题)"""
    tpl, version = A._prompt("chapter_seal")
    secs = "\n\n".join(f"【{s['title']}】\n{s['body']}" for s in ch["sections"])
    hint = f"你已经给这一章起了标题「{ch['title']}」。" if ch["title"] else ""
    prompt = (tpl.replace("{n}", str(ch["n"])).replace("{range}", _range_label(ch)).replace("{title_hint}", hint)
              .replace("{sections}", secs).replace("{title_rule}", "已经有标题了，照抄就好" if ch["title"] else "不要用引号"))
    src = {k: s["body"] for k, s in enumerate(ch["sections"])}
    issues = []
    for attempt in range(2):
        text, model = await llm_fn(prompt if attempt == 0 else prompt + "\n\n【上一次的问题】\n" + "\n".join(f"- {x}" for x in issues))
        if not text:
            raise RuntimeError("章节摘要：调用模型失败（没有返回内容），稍后重试")
        data = A.parse_json(text)
        title = ch["title"] or A._short_title(str(data.get("title") or "").strip().strip("「」『』“”\"'《》"), TITLE_MAX)
        abstract = A.clean_summary(data.get("abstract"), src)
        issues = []
        if not title:
            issues.append("没有标题")
        if not abstract:
            issues.append("摘要是空的，或者引了内容里没有的原话")
        if not issues:
            return title, abstract, version
    return None, None, version


def _prev_busy(conn, prev):
    """上一章还没写完（还有没写进去的片段、要整章重写、还没写摘要）"""
    return bool(prev and (prev["needs_rebuild"] or unwritten(conn, prev) or needs_abstract(prev)))


async def write_chapter(chapter_id, llm_fn=None, waited=0):
    """把这一章还没写进去的片段追加成新小节；要整章重写时先清空。一次最多写 MAX_BATCHES_PER_JOB 批，剩下的接着投递。
    每批写完就存（以后导入大量旧聊天时，写到一半出错也不会白写）；这次任务写完后版本 + 1，留一份历史。
    封存的章全部写完以后，写标题和摘要。
    先不写的几种情况：每日片段还在排队整理（整理完会再投递）；在等分章提议（见 hold()）；
    这章要从头写、上一章还没写完（等一会儿再试，最多 MAX_WAITS 次，这样开头能接上上一章）。"""
    import jobs
    from app.config import SQLITE_PATH
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        try:
            ch = _get(conn, chapter_id)
        except ValueError:
            return {"status": "skipped", "message": "这一章不存在了"}
        prev = conn.execute(f"SELECT {COLS} FROM chapters WHERE n < ? ORDER BY n DESC LIMIT 1", (ch["n"],)).fetchone()
        prev = _row(prev) if prev else None
        if settling(conn):
            return {"status": "skipped", "message": WAITING_CN["settling"]}
        why = hold(conn, ch)
        if why:
            return {"status": "skipped", "message": WAITING_CN[why]}
        if _fresh(ch) and waited < MAX_WAITS and _prev_busy(conn, prev):
            jobs.enqueue(SQLITE_PATH, "write_chapter", payload={"chapter_id": ch["id"], "waited": waited + 1},
                         dedupe_key=f"write_chapter:{ch['id']}:wait", source="auto", delay_sec=WAIT_SEC,
                         continuation=True)
            return {"status": "skipped", "message": f"等第 {prev['n']} 章写完再写（开头要接上上一章）"}
        rebuild = bool(ch["needs_rebuild"])
        if rebuild:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM chapter_episodes WHERE chapter_id=?", (ch["id"],))
            conn.execute("UPDATE chapters SET sections='[]', summary=NULL, needs_rebuild=0, updated_at=? WHERE id=?", (_now(), ch["id"]))
            conn.commit()
            ch["sections"], ch["summary"] = [], None
        pending = unwritten(conn, ch)
        src = A._sources(conn, pending[:WRITE_BATCH * MAX_BATCHES_PER_JOB])
    finally:
        conn.close()
    sections, summary = list(ch["sections"]), ch["summary"]
    written = skipped = 0
    issues, model_used, version_used = list(ch["issues"]) if not rebuild else [], None, None
    todo = pending[:WRITE_BATCH * MAX_BATCHES_PER_JOB]
    for i in range(0, len(todo), WRITE_BATCH):
        batch = todo[i:i + WRITE_BATCH]
        ref_map = {k: m["episode_id"] for k, m in enumerate(batch, 1)}
        prompt, version_used = build_write_prompt(ch, prev, sections, batch, summary)
        accepted, round_issues = [], []
        for attempt in range(2):
            p = prompt if attempt == 0 else prompt + "\n\n【上一次写的没通过校验，问题如下，请改正后重写】\n" + "\n".join(f"- {x}" for x in round_issues[:10])
            text, model_used = await llm_fn(p)
            if not text:
                raise RuntimeError("写章节：调用模型失败（没有返回内容），稍后重试")
            data = A.parse_json(text)
            accepted, round_issues = [], []
            for sec in (data.get("sections") or [])[:2]:
                if not isinstance(sec, dict):
                    continue
                sec = {**sec, "title": str(sec.get("title") or "").strip().strip("《》「」『』“”\"'")}
                clean, iss = A.verify_section(sec, ref_map, src)
                round_issues += iss
                if clean:
                    clean["created_at"] = _now()
                    accepted.append(clean)
            if accepted:
                summary = A.clean_summary(data.get("summary"), src) or summary
                break
        issues += round_issues
        ok = [m["episode_id"] for m in batch] if accepted else []
        bad = [] if accepted else [m["episode_id"] for m in batch]
        sections += accepted
        written += len(ok)
        skipped += len(bad)
        _save_progress(ch["id"], sections, summary, ok, bad, issues, model_used, version_used)
    more = len(pending) > len(todo)
    if more:      # 接着写剩下的。不能用 write_chapter:{id} 去重：这个任务自己正在跑，会被当成重复
        jobs.enqueue(SQLITE_PATH, "write_chapter", payload={"chapter_id": ch["id"]}, dedupe_key=f"write_chapter:{ch['id']}:next",
                     source="auto", delay_sec=5, continuation=True)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = _get(conn, ch["id"])
        version = cur["version"]
        if written or rebuild:
            version += 1
            conn.execute("UPDATE chapters SET version=?, updated_at=? WHERE id=?", (version, _now(), ch["id"]))
            conn.execute("INSERT INTO chapter_versions (chapter_id, version, title, abstract, summary, sections, start_day, end_day, model, "
                         "prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (ch["id"], version, cur["title"], cur["abstract"], cur["summary"], json.dumps(cur["sections"], ensure_ascii=False),
                          cur["start_day"], cur["end_day"], model_used, version_used, _now()))
        conn.commit()
        cur = _get(conn, ch["id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = {"status": "ok", "chapter_id": ch["id"], "n": ch["n"], "version": version, "rebuild": rebuild,
              "written": written, "skipped": skipped, "sections": len(sections), "more": more, "issues": issues[-10:]}
    if not more and needs_abstract(cur):
        title, abstract, pv = await _abstract(cur, llm_fn)
        if abstract:
            conn = get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE chapters SET title=COALESCE(title, ?), abstract=?, abstract_version=?, updated_at=? WHERE id=?",
                             (title, abstract, cur["version"], _now(), ch["id"]))
                conn.commit()
            finally:
                conn.close()
            result["abstract"] = abstract
            result["title"] = cur["title"] or title
        else:
            conn = get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE chapters SET abstract_version=?, updated_at=? WHERE id=?", (-cur["version"], _now(), ch["id"]))
                conn.commit()
            finally:
                conn.close()
            result["abstract_failed"] = True
    if not written and not rebuild and skipped:
        result["status"] = "rejected"
        result["message"] = "写出来的都没通过校验"
    return result


def enqueue_write(cid, source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    return jobs.enqueue(SQLITE_PATH, "write_chapter", payload={"chapter_id": int(cid)}, dedupe_key=f"write_chapter:{int(cid)}", source=source)[1]


# ---------- 封存、改名、重写（你在 Dashboard 上的操作） ----------

def _clean_title(title):
    if title is None:
        return None
    t = str(title).strip()
    if not t:
        return None
    if len(t) > TITLE_MAX:
        raise ValueError(f"标题不能超过 {TITLE_MAX} 个字")
    return t


def seal(chapter_id, end_day=None, title=None):
    """这一章到这里：进行中的章封存到 end_day（默认最后一天聊天的日子），后面的日子进入新的一章"""
    title = _clean_title(title)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ch = _get(conn, chapter_id)
        if ch["status"] != "current":
            raise ValueError("只有进行中的那一章可以封存")
        last = conn.execute("SELECT MAX(day) FROM episodes WHERE status='active' AND day >= ?", (ch["start_day"],)).fetchone()[0]
        if not last:
            raise ValueError("这一章还没有内容")
        try:
            end = _d(end_day) if end_day else _d(last)
        except ValueError:
            raise ValueError("日期要写成 2026-10-21 这样")
        if end < _d(ch["start_day"]):
            raise ValueError("结束的日子不能早于这一章开始的日子")
        if end > today_day():
            raise ValueError("结束的日子不能晚于今天")
        conn.execute("UPDATE chapters SET status='sealed', end_day=?, title=COALESCE(?, title), sealed_at=?, updated_at=? WHERE id=?",
                     (str(end), title, _now(), _now(), ch["id"]))
        new_id = _insert(conn, ch["n"] + 1, end + timedelta(days=1))
        cleanup(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    enqueue_write(chapter_id, "dashboard")
    enqueue_write(new_id, "dashboard")
    return new_id


def unseal(chapter_id):
    """撤销封存（只能撤销最近封存的那一章）：后面进行中的那章并回来，那些片段接着写进这一章"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ch = _get(conn, chapter_id)
        cur = _current(conn)
        if ch["status"] != "sealed" or not cur or cur["n"] != ch["n"] + 1:
            raise ValueError("只能撤销最近封存的那一章")
        conn.execute("DELETE FROM chapter_episodes WHERE chapter_id=?", (cur["id"],))
        conn.execute("DELETE FROM chapter_versions WHERE chapter_id=?", (cur["id"],))
        conn.execute("DELETE FROM chapters WHERE id=?", (cur["id"],))
        conn.execute("UPDATE chapters SET status='current', end_day=NULL, sealed_at=NULL, updated_at=? WHERE id=?", (_now(), ch["id"]))
        conn.execute("UPDATE chapter_proposals SET status='dismissed', decided_at=? WHERE status='pending'", (_now(),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    enqueue_write(chapter_id, "dashboard")


def rename(chapter_id, title):
    title = _clean_title(title)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get(conn, chapter_id)
        conn.execute("UPDATE chapters SET title=?, updated_at=? WHERE id=?", (title, _now(), int(chapter_id)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rebuild(chapter_id):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get(conn, chapter_id)
        conn.execute("UPDATE chapters SET needs_rebuild=1, updated_at=? WHERE id=?", (_now(), int(chapter_id)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    enqueue_write(chapter_id, "dashboard")


def today_day(now=None):
    return E.day_of(timeutil.parse_ts(now) if now else timeutil.utc_now())


# ---------- 分章提议 ----------

def _chat_days(conn, ch):
    days = {}
    for e in _episodes_in(conn, ch["start_day"], ch["end_day"]):
        days.setdefault(e["day"], []).append(e["title"])
    return days


def _day_lines(conn, days):
    digests = {r[0]: r[1] for r in conn.execute("SELECT period_start, body FROM digests WHERE kind='day' AND status='active'")}
    out = []
    for day in sorted(days):
        text = "；".join(ln.strip() for ln in (digests.get(day) or "").split("\n") if ln.strip()) or "、".join(f"《{t}》" for t in days[day])
        out.append(f"{day}（周{D.WEEKDAYS[_d(day).weekday()]}）：{text[:DAY_LINE_CHARS]}")
    return out


def validate_split(items, chat_days, min_days):
    """检查分章：按时间、首尾相接（中间只能隔着没聊天的日子）、每章至少 min_days 天（最后一章除外）。
    返回 (规整后的 [{title, start, end, why}] 或 None, 问题)"""
    days = sorted(chat_days)
    issues = []
    if not isinstance(items, list) or not items:
        return None, ["没有分章"]
    if len(items) > MAX_SPLIT:
        return None, [f"最多 {MAX_SPLIT} 章"]
    out = []
    for k, it in enumerate(items):
        if not isinstance(it, dict):
            return None, ["格式不对"]
        title = str(it.get("title") or "").strip().strip("「」『』“”\"'《》")
        if not title or len(title) > TITLE_MAX:
            issues.append(f"第 {k + 1} 章的标题是空的或者太长")
        try:
            start = _d(str(it.get("start") or "")[:10])
        except ValueError:
            return None, issues + [f"第 {k + 1} 章的开始日期不对"]
        end = None
        if k < len(items) - 1:
            try:
                end = _d(str(it.get("end") or "")[:10])
            except ValueError:
                return None, issues + [f"第 {k + 1} 章的结束日期不对"]
        out.append({"title": title, "start": start, "end": end, "why": str(it.get("why") or "").strip()[:80]})
    first, last = _d(days[0]), _d(days[-1])
    if out[0]["start"] != first:
        issues.append(f"第一章要从 {first} 开始")
    for k in range(len(out)):
        cur = out[k]
        if k < len(out) - 1:
            nxt = out[k + 1]
            if cur["end"] < cur["start"]:
                issues.append(f"第 {k + 1} 章的结束早于开始")
                continue
            if nxt["start"] <= cur["end"]:
                issues.append(f"第 {k + 2} 章要在第 {k + 1} 章结束以后才开始")
                continue
            between = [d for d in days if cur["end"] < _d(d) < nxt["start"]]
            if between:
                issues.append(f"第 {k + 1} 章和第 {k + 2} 章中间漏了聊过天的日子（{between[0]}）")
            if (cur["end"] - cur["start"]).days + 1 < min_days:
                issues.append(f"第 {k + 1} 章不到 {min_days} 天")
        elif cur["start"] > last:
            issues.append("最后一章开始的日子之后没有聊天")
    if issues:
        return None, issues
    return [{"title": x["title"], "start": str(x["start"]), "end": str(x["end"]) if x["end"] else None, "why": x["why"]} for x in out], []


async def propose(llm_fn=None, now=None):
    """给进行中的那章提议分章（存下来等你确认）"""
    llm_fn = llm_fn or default_llm
    c = cfg()
    conn = get_conn()
    try:
        cur = _current(conn)
        if not cur:
            return {"status": "skipped", "message": "还没有章节"}
        days = _chat_days(conn, cur)
        lines = _day_lines(conn, days)
    finally:
        conn.close()
    if len(days) < 2 or (_d(max(days)) - _d(min(days))).days + 1 < 2 * c["min_chapter_days"]:
        return {"status": "skipped", "message": "这一章还不够长，不用分"}
    tpl, version = A._prompt("chapter_split")
    prompt = (tpl.replace("{start}", _label(min(days), True)).replace("{end}", _label(max(days), True)).replace("{days}", "\n".join(lines))
              .replace("{min_days}", str(c["min_chapter_days"])).replace("{max}", str(MAX_SPLIT)))
    issues, model, result = [], None, None
    for attempt in range(2):
        text, model = await llm_fn(prompt if attempt == 0 else prompt + "\n\n【上一次的分法有问题，请改正】\n" + "\n".join(f"- {x}" for x in issues[:10]))
        if not text:
            raise RuntimeError("分章提议：调用模型失败（没有返回内容），稍后重试")
        result, issues = validate_split(A.parse_json(text).get("chapters"), days, c["min_chapter_days"])
        if result:
            break
    status = "pending" if result and len(result) > 1 else ("nochange" if result else "rejected")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE chapter_proposals SET status='dismissed', decided_at=? WHERE status='pending'", (_now(),))
        cur_id = conn.execute("INSERT INTO chapter_proposals (chapter_id, chapters, status, issues, model, prompt_version, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", (cur["id"], json.dumps(result or [], ensure_ascii=False), status,
                                                              json.dumps(issues[:20], ensure_ascii=False), model, version, _now())).lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": "ok" if result else "rejected", "proposal_id": cur_id, "proposal": status,
            "chapters": [x["title"] for x in (result or [])], "issues": issues[:10]}


def enqueue_propose(source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    return jobs.enqueue(SQLITE_PATH, "propose_chapters", payload={}, dedupe_key="propose_chapters", source=source)[1]


def apply_proposal(pid, chapters=None):
    """确认分章（chapters 是你改过的分法，不传就用提议的）：进行中的那章拆成几章，前面的封存，最后一章接着进行。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT chapter_id, chapters, status FROM chapter_proposals WHERE id=?", (int(pid),)).fetchone()
        if not r:
            raise ValueError("没有这个提议")
        if r[2] != "pending":
            raise ValueError("这个提议已经处理过了")
        cur = _current(conn)
        if not cur or cur["id"] != r[0]:
            raise ValueError("章节已经变了，这个提议不能用了，请重新提议")
        plan, issues = validate_split(chapters if chapters is not None else A._loads(r[1], []), _chat_days(conn, cur),
                                      cfg()["min_chapter_days"])
        if not plan:
            raise ValueError("分法有问题：" + "；".join(issues[:3]))
        if len(plan) == 1:
            conn.execute("UPDATE chapters SET title=?, updated_at=? WHERE id=?", (plan[0]["title"], _now(), cur["id"]))
            ids = [cur["id"]]
        else:
            conn.execute("UPDATE chapters SET title=?, end_day=?, status='sealed', sealed_at=?, needs_rebuild=1, updated_at=? WHERE id=?",
                         (plan[0]["title"], plan[0]["end"], _now(), _now(), cur["id"]))
            ids = [cur["id"]]
            for k, x in enumerate(plan[1:], 1):
                last = k == len(plan) - 1
                ids.append(_insert(conn, cur["n"] + k, x["start"], None if last else x["end"],
                                   status="current" if last else "sealed", title=x["title"]))
            _renumber(conn)
            cleanup(conn)
        conn.execute("UPDATE chapter_proposals SET status='applied', chapters=?, decided_at=? WHERE id=?",
                     (json.dumps(plan, ensure_ascii=False), _now(), int(pid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for cid in ids:
        enqueue_write(cid, "dashboard")
    return ids


def dismiss_proposal(pid):
    """不用了：要是进行中的这章在等这个提议，现在就开始写"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("UPDATE chapter_proposals SET status='dismissed', decided_at=? WHERE id=? AND status='pending'",
                         (_now(), int(pid))).rowcount
        if not n:
            raise ValueError("没有这个提议，或者已经处理过了")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if cfg().get("enabled"):
        try:
            schedule()
        except Exception:
            pass        # 定时检查会补上


# ---------- 查看 ----------

def index_text(chs=None):
    """章节目录（以后放进聊天）：每章一行"""
    if chs is None:
        conn = get_conn()
        try:
            chs = _chapters(conn)
        finally:
            conn.close()
    lines = []
    for ch in chs:
        name = f"第 {ch['n']} 章" + (f"「{ch['title']}」" if ch["title"] else "")
        if ch["status"] == "sealed":
            lines.append(f"· {name}（{_range_label(ch)}）：{ch['abstract'] or '（摘要还没写）'}")
        else:
            lines.append(f"· {name}（{_range_label(ch)}，进行中）：{ch['summary'] or '（还没写）'}")
    text = "\n".join(lines)
    return text if len(text) <= INDEX_MAX else "……\n" + text[-INDEX_MAX:]


def _with_sources(conn, ch):
    ids = sorted(_cited(ch["sections"]))
    titles = {}
    if ids:
        for eid, day, title, status in conn.execute(f"SELECT id, day, title, status FROM episodes WHERE id IN ({','.join('?' * len(ids))})", ids):
            titles[eid] = {"id": eid, "day": day, "title": title, "status": status}
    for s in ch["sections"]:
        s["sources"] = [dict(titles.get(int(i), {"id": int(i)}), n=k) for k, i in enumerate(s.get("refs") or [], 1)]
    return ch


def overview():
    conn = get_conn()
    try:
        chs = _chapters(conn)
        items = []
        for ch in chs:
            eps = _episodes_in(conn, ch["start_day"], ch["end_day"])
            done = _done_ids(conn, ch["id"])
            items.append({**{k: ch[k] for k in ("id", "n", "title", "abstract", "summary", "start_day", "end_day", "status", "version",
                                                "needs_rebuild", "updated_at", "sealed_at")},
                          "range": _range_label(ch), "sections": len(ch["sections"]), "chars": _chars(ch["sections"]),
                          "episodes": len(eps), "written": sum(1 for v in done.values() if v == 1),
                          "skipped": sum(1 for v in done.values() if v == 2), "unwritten": sum(1 for e in eps if e["episode_id"] not in done),
                          "days": len({e["day"] for e in eps}), "last_day": eps[-1]["day"] if eps else None})
        cur = _current(conn)
        current = _with_sources(conn, cur) if cur else None
        props = [{"id": r[0], "chapter_id": r[1], "chapters": A._loads(r[2], []), "status": r[3], "issues": A._loads(r[4], []),
                  "created_at": r[5]} for r in conn.execute(
            "SELECT id, chapter_id, chapters, status, issues, created_at FROM chapter_proposals ORDER BY id DESC LIMIT 5")]
        legacy = conn.execute("SELECT content, ts, version FROM shared_narrative WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        waiting = "settling" if settling(conn) else hold(conn, cur)
    finally:
        conn.close()
    return {"chapters": items, "current": current, "index": index_text(chs) if chs else "", "proposals": props,
            "waiting": WAITING_CN.get(waiting) if waiting else None,
            "legacy": {"content": legacy[0], "ts": legacy[1], "version": legacy[2]} if legacy and (legacy[0] or "").strip() else None,
            "status_cn": STATUS_CN, "settings": cfg(), "enabled": bool(cfg().get("enabled"))}


def get_chapter(cid):
    conn = get_conn()
    try:
        try:
            ch = _get(conn, cid)
        except ValueError:
            return None
        _with_sources(conn, ch)
        ch["range"] = _range_label(ch)
        ch["versions"] = [{"version": r[0], "created_at": r[1], "model": r[2]} for r in conn.execute(
            "SELECT version, created_at, model FROM chapter_versions WHERE chapter_id=? ORDER BY version DESC LIMIT 20", (int(cid),))]
        return ch
    finally:
        conn.close()
