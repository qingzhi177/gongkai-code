"""每日片段（方案 6.5 节，Phase 4 第一步）：每天把对话整理成几段「我们的经历」。

一天怎么算：用户时区，凌晨 4 点之前的消息算前一天（DAY_START_HOUR）。
怎么分段：同一个会话里，隔 45 分钟以上没说话就算另一段；一段对话按「开始时间」算在哪一天
（晚上 11 点聊到凌晨 1 点的，整段算在开始那天）。太短的段（不到 4 条或不到 120 字）不写。
怎么写：每一段（太长的切成几块）交给模型，按 app/prompts/episode.md 写 0 到 2 个片段，每个细节标出处 [#行号]。
怎么校验（方案 7.6 节）：出处必须存在；引号里的原话必须能在出处原文里找到；对不上的句子删掉，
删掉一半以上、或者一个出处都没有的，整个片段不要。
怎么存：同一天重新整理时，旧版本标成 superseded，新版本 version + 1；settle_days 记下整理到了哪条消息，
这一天之后再有新消息进来（比如改了、重新生成了），会重新整理。
重新整理时，内容没变的那几块（chunk_key 一样）沿用原来的片段、不再调模型；手动点「重新整理」（force）才全部重写。

现在只生成、只在 Dashboard 上看，还不注入聊天。以后它是主题卷、章节、近七天摘要的素材，也是「那天……」的召回单元。
导入旧聊天以后，按日期从早到晚一天一天整理（POST /episodes/settle），就是从原文生成的。
"""
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import timeutil
from app.config import get_conn

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "episode.md"
DAY_START_HOUR = 4          # 凌晨 4 点前的消息算前一天
GAP_MINUTES = 45            # 隔这么久没说话，算另一段
MIN_MESSAGES = 4            # 少于这么多条的一段不写
MIN_CHARS = 120
MAX_LINE_CHARS = 800        # 给模型看时每条消息最多这么多字
MAX_CHUNK_CHARS = 12000     # 一段太长就切成几块，每块分别写
MAX_CHUNK_MESSAGES = 60
MAX_PER_CHUNK = 2
MIN_BODY_CHARS = 40         # 校验删句以后正文少于这么多字，整段不要
SETTLE_AFTER_IDLE_H = 6     # 当天最后一条消息过了这么久，不用等日界也可以整理
MAX_TOKENS = 16000          # 推理模型的思考也算在里面（deepseek-v4-pro 一段 30 条的对话就能想 6000 多），给足

REF = re.compile(r"\[#(\d+)\]")
QUOTE = re.compile(r"[「『“\"]([^」』”\"]{2,})[」』”\"]")
SENT = re.compile(r"[^。！？!?\n]+(?:[。！？!?]+(?:\s*\[#\d+\])*)?")
_NORM = re.compile(r"[\s，。！？、,.!?~～…·：:；;「」『』“”\"'（）()\-—]")


# ---------- 日期和分段 ----------

def to_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def day_of(ts):
    """这条消息算哪一天（用户时区，凌晨 4 点前算前一天）"""
    local = timeutil.to_local(ts)
    return (local - timedelta(hours=DAY_START_HOUR)).date() if local else None


def day_window_utc(day):
    """这一天的 [开始, 结束)（UTC 存储格式）"""
    start = datetime.combine(to_day(day), time(DAY_START_HOUR), tzinfo=timeutil.USER_TZ)
    end = start + timedelta(days=1)
    f = lambda d: d.astimezone(timezone.utc).strftime(timeutil.STORE_FMT)
    return f(start), f(end)


def _split_sessions(rows):
    """rows：同一个会话、按时间排好的 (id, role, content, ts)。隔 GAP_MINUTES 以上算另一段。"""
    out, cur, prev = [], [], None
    for r in rows:
        t = timeutil.parse_ts(r[3])
        if cur and prev and t and (t - prev).total_seconds() > GAP_MINUTES * 60:
            out.append(cur)
            cur = []
        cur.append(r)
        prev = t or prev
    if cur:
        out.append(cur)
    return out


def sessions_for_day(conn, day):
    """这一天的几段对话：[{conv_id, rows: [(id, role, content, ts)]}]，按开始时间排序"""
    day = to_day(day)
    start, end = day_window_utc(day)
    lo = (timeutil.parse_ts(start) - timedelta(hours=12)).strftime(timeutil.STORE_FMT)
    hi = (timeutil.parse_ts(end) + timedelta(hours=12)).strftime(timeutil.STORE_FMT)
    rows = conn.execute(
        "SELECT id, conv_id, role, content, ts FROM l0_messages WHERE status='active' "
        "AND datetime(ts) >= ? AND datetime(ts) < ? ORDER BY conv_id, datetime(ts), id", (lo, hi)).fetchall()
    by_conv = {}
    for mid, conv, role, content, ts in rows:
        by_conv.setdefault(conv, []).append((mid, role, content or "", ts))
    out = []
    for conv, rs in by_conv.items():
        for s in _split_sessions(rs):
            if day_of(s[0][3]) == day:
                out.append({"conv_id": conv, "rows": s})
    out.sort(key=lambda s: timeutil.to_utc_str(s["rows"][0][3]) or "")
    return out


def chunks(rows):
    """一段太长时切成几块（尽量在「你」说话的地方切开）"""
    out, cur, size = [], [], 0
    for r in rows:
        n = min(len(r[2]), MAX_LINE_CHARS)
        if cur and (size + n > MAX_CHUNK_CHARS or len(cur) >= MAX_CHUNK_MESSAGES) and r[1] == "user":
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def chunk_key(part):
    """一块对话的指纹（消息 id + 内容）：重新整理时，指纹没变的块沿用原来的片段"""
    h = hashlib.sha1()
    for r in part:
        h.update(f"{r[0]}:{hashlib.sha1((r[2] or '').encode('utf-8')).hexdigest()[:8]}\n".encode())
    return h.hexdigest()[:16]


def substantial(rows):
    return len(rows) >= MIN_MESSAGES and sum(len(r[2]) for r in rows) >= MIN_CHARS


# ---------- 提示词和模型 ----------

def prompt_template():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    m = re.match(r"<!--\s*(\S+\s+v\d+)[^>]*-->\s*", text)
    version = m.group(1).replace(" ", "-") if m else "episode-v?"
    return (text[m.end():] if m else text), version


def build_prompt(rows):
    """返回 (prompt, line_map, version)。line_map：行号 → (l0_id, 原文全文)"""
    tpl, version = prompt_template()
    lines, line_map = [], {}
    for n, (mid, role, content, ts) in enumerate(rows, 1):
        text = content if len(content) <= MAX_LINE_CHARS else content[:MAX_LINE_CHARS] + "……"
        lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {text}")
        line_map[n] = (mid, content)
    t0, t1 = timeutil.local_str(rows[0][3]), timeutil.local_str(rows[-1][3])
    span = t0 if (not t1 or t1 == t0) else t0 + " 至 " + (t1[11:] if t1[:10] == t0[:10] else t1)
    prompt = tpl.replace("{span}", span).replace("{tz}", timeutil.USER_TZ_LABEL).replace("{lines}", "\n".join(lines))
    return prompt, line_map, version


async def default_llm(prompt):
    """用 Dashboard「模块模型配置」里的「每日片段」；没设置就用「共同经历叙事」的，再没有就用「近期摘要」的"""
    from app.llm import call_llm, get_model_config
    cfg = None
    for purpose in ("episode", "narrative", "summary"):
        c = await get_model_config(purpose)
        if c.get("configured"):
            cfg = c
            break
    if not cfg:
        raise RuntimeError("没有可用的模型：请在 Dashboard「模块模型配置」里设置「每日片段」或「共同经历叙事」")
    text = await call_llm(cfg, prompt, MAX_TOKENS)
    return text, cfg.get("model")


def parse_output(text):
    """模型的输出 → 片段列表；不是 JSON 抛 ValueError"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("模型没有输出 JSON")
    data = json.loads(t[a:b + 1])
    eps = data.get("episodes") if isinstance(data, dict) else None
    if not isinstance(eps, list):
        raise ValueError("JSON 里没有 episodes 数组")
    return [e for e in eps if isinstance(e, dict)]


# ---------- 校验 ----------

def _norm(t):
    return _NORM.sub("", t or "")


def verify(ep, line_map):
    """返回 (校验后的片段 或 None, 问题列表)。对不上出处或原话的句子删掉；删掉一半以上、或者没有出处的，整个不要。"""
    issues = []
    raw = str(ep.get("body") or "").strip()
    title = str(ep.get("title") or "").strip()[:30]
    if not raw or not title:
        return None, ["没有标题或正文"]
    sents = [s for s in SENT.findall(raw) if s.strip()]
    kept, used = [], set()
    for s in sents:
        refs = [int(x) for x in REF.findall(s)]
        bad = [r for r in refs if r not in line_map]
        if bad:
            issues.append(f"出处不存在 #{bad[0]}：{REF.sub('', s).strip()[:30]}")
            continue
        quotes = QUOTE.findall(REF.sub("", s))
        src = [_norm(line_map[r][1]) for r in refs] if refs else [_norm(v[1]) for v in line_map.values()]
        missing = [q for q in quotes if _norm(q) and not any(_norm(q) in x for x in src)]
        if missing:
            issues.append(f"原话对不上「{missing[0][:20]}」")
            continue
        kept.append(s.strip())
        used.update(refs)
    # 出处重新编号：[#行号] → [#k]，k 是它在 refs（按原文顺序）里的位置，查看时「[#2]」就是第 2 条原文
    order = {n: k for k, n in enumerate(sorted(used), 1)}
    body_with_refs = REF.sub(lambda m: f"[#{order.get(int(m.group(1)), m.group(1))}]", "".join(kept).strip())
    body = re.sub(r"[ \t]{2,}", " ", REF.sub("", body_with_refs)).strip()
    if not used:
        return None, issues + ["一个出处都没有标，整段不要"]
    if len(kept) * 2 < len(sents) or len(body) < MIN_BODY_CHARS:
        return None, issues + ["去掉对不上的句子以后剩得太少，整段不要"]
    try:
        sal = max(0.0, min(1.0, float(ep.get("salience", 0.4))))
    except (TypeError, ValueError):
        sal = 0.4
    topics = [str(x).strip()[:12] for x in (ep.get("topics") or []) if str(x).strip()][:4]
    return {"title": title, "body": body, "body_with_refs": body_with_refs, "mood": str(ep.get("mood") or "")[:40],
            "topics": topics, "salience": round(sal, 2), "refs": [line_map[n][0] for n in sorted(used)]}, issues


# ---------- 整理一天 ----------

async def settle_day(day, llm_fn=None, force=False):
    """整理一天：每段对话写 0 到 2 个片段，校验后整天一起写库（旧版本作废）。模型调用失败时抛异常（任务会重试），不写半截。
    force=False 时，内容没变的块沿用原来的片段（也记得上次「没什么要记的」块），只重写变了的块。"""
    day = to_day(day)
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        sessions = sessions_for_day(conn, day)
        existing, prev_empty = {}, set()
        if not force:
            for eid, key in conn.execute("SELECT id, chunk_key FROM episodes WHERE day=? AND status='active' AND chunk_key IS NOT NULL",
                                         (day.isoformat(),)):
                existing.setdefault(key, []).append(eid)
            r = conn.execute("SELECT detail FROM settle_days WHERE day=?", (day.isoformat(),)).fetchone()
            try:
                prev_empty = {x.get("chunk") for x in json.loads(r[0] or "[]") if x.get("result") == "没什么要记的"} if r else set()
            except (ValueError, AttributeError):
                prev_empty = set()
    finally:
        conn.close()
    keep = []
    last_id = max((r[0] for s in sessions for r in s["rows"]), default=None)
    n_msgs = sum(len(s["rows"]) for s in sessions)
    detail, new_eps, version_used, model_used = [], [], None, None
    for s in sessions:
        for part in chunks(s["rows"]):
            key = chunk_key(part)
            info = {"conv_id": s["conv_id"], "messages": len(part), "chunk": key,
                    "from": timeutil.local_str(part[0][3]), "to": timeutil.local_str(part[-1][3])}
            if not substantial(part):
                info["result"] = "太短，没写"
                detail.append(info)
                continue
            if key in existing:
                keep += existing[key]
                info["result"] = f"没变，沿用 {len(existing[key])} 段"
                detail.append(info)
                continue
            if key in prev_empty:
                info.update(result="没什么要记的", reused=True)
                detail.append(info)
                continue
            prompt, line_map, version_used = build_prompt(part)
            text, model_used = await llm_fn(prompt)
            if not text:
                raise RuntimeError("调用模型失败（没有返回内容），稍后重试")
            eps = parse_output(text)[:MAX_PER_CHUNK]
            written = 0
            for ep in eps:
                clean, issues = verify(ep, line_map)
                if issues:
                    info.setdefault("issues", []).extend(issues)
                if clean:
                    clean.update(conv_id=s["conv_id"], start_ts=timeutil.to_utc_str(part[0][3]),
                                 end_ts=timeutil.to_utc_str(part[-1][3]), issues=issues, chunk_key=key)
                    new_eps.append(clean)
                    written += 1
            info["result"] = f"写了 {written} 段" if written else ("没什么要记的" if not eps else "都没通过校验")
            detail.append(info)
    now = timeutil.utc_now_str()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute("SELECT MAX(version) FROM episodes WHERE day=?", (day.isoformat(),)).fetchone()[0] or 0
        keep_sql = f" AND id NOT IN ({','.join('?' * len(keep))})" if keep else ""     # 注意：NOT IN (NULL) 什么都匹配不上
        conn.execute("UPDATE episodes SET status='superseded' WHERE day=? AND status='active'" + keep_sql, (day.isoformat(), *keep))
        for e in new_eps:
            conn.execute(
                "INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, "
                "salience, status, version, issues, model, prompt_version, created_at, chunk_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), e["conv_id"], e["start_ts"], e["end_ts"], e["title"], e["body"], e["body_with_refs"],
                 e["mood"], json.dumps(e["topics"], ensure_ascii=False), json.dumps(e["refs"]), e["salience"],
                 prev + 1, json.dumps(e["issues"], ensure_ascii=False), model_used, version_used, now, e["chunk_key"]))
        conn.execute(
            "INSERT INTO settle_days (day, status, last_l0_id, messages, sessions, episodes, detail, version, settled_at) "
            "VALUES (?, 'done', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET status='done', last_l0_id=excluded.last_l0_id, "
            "messages=excluded.messages, sessions=excluded.sessions, episodes=excluded.episodes, detail=excluded.detail, "
            "version=excluded.version, settled_at=excluded.settled_at",
            (day.isoformat(), last_id, n_msgs, len(sessions), len(new_eps) + len(keep), json.dumps(detail, ensure_ascii=False), prev + 1, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": "ok", "day": day.isoformat(), "sessions": len(sessions), "episodes": len(new_eps) + len(keep),
            "new": len(new_eps), "kept": len(keep), "version": prev + 1, "detail": detail}


# ---------- 哪些天要整理、查看 ----------

def day_overview(now=None):
    """每一天：消息数、最后一条消息、整理状态、要不要整理。按日期从新到旧。
    整理过以后这一天又多了消息、或者删了消息（条数或最后一条的 id 变了），就要重新整理；
    消息全删光了、但还留着片段的那天，也要重新整理一次（把片段作废）。"""
    now = timeutil.parse_ts(now) if now else timeutil.utc_now()
    today = day_of(now)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, ts FROM l0_messages WHERE status='active'").fetchall()
        settled = {r[0]: r for r in conn.execute(
            "SELECT day, status, last_l0_id, sessions, episodes, version, settled_at, messages FROM settle_days").fetchall()}
    finally:
        conn.close()
    days = {}
    for mid, ts in rows:
        d = day_of(ts)
        if d is None:
            continue
        x = days.setdefault(d, {"messages": 0, "max_id": 0, "last": None})
        x["messages"] += 1
        x["max_id"] = max(x["max_id"], mid)
        t = timeutil.parse_ts(ts)
        if t and (x["last"] is None or t > x["last"]):
            x["last"] = t
    for k, s in settled.items():          # 消息全删光了、但还留着片段的那天
        d = to_day(k)
        if d not in days and (s[4] or 0) > 0:
            days[d] = {"messages": 0, "max_id": None, "last": None}
    out = []
    for d in sorted(days, reverse=True):
        x = days[d]
        s = settled.get(d.isoformat())
        closed = d < today or not x["messages"] or (x["last"] and (now - x["last"]).total_seconds() >= SETTLE_AFTER_IDLE_H * 3600)
        stale = (not s) or s[1] != "done" or s[2] != x["max_id"] or (s[7] is not None and s[7] != x["messages"])
        out.append({"day": d.isoformat(), "messages": x["messages"],
                    "last": timeutil.to_utc_str(x["last"]) if x["last"] else None, "closed": bool(closed),
                    "settled": {"status": s[1], "sessions": s[3], "episodes": s[4], "version": s[5], "settled_at": s[6]} if s else None,
                    "pending": bool(closed and stale)})
    return out


def pending_days(now=None):
    """要整理的天（日期从早到晚）"""
    return [d["day"] for d in reversed(day_overview(now)) if d["pending"]]


def _recently_dead(conn, day, hours=24):
    r = conn.execute("SELECT status, updated_at FROM pipeline_jobs WHERE dedupe_key=? ORDER BY id DESC LIMIT 1",
                     (f"settle_day:{day}",)).fetchone()
    if not r or r[0] != "dead":
        return False
    t = timeutil.parse_ts(r[1])
    return bool(t and (timeutil.utc_now() - t).total_seconds() < hours * 3600)


def enqueue_days(days, source="auto", skip_dead=True, force=False):
    """按日期从早到晚投递 settle_day 任务（同一天已经在排队的不重复投）。
    自动投递时跳过 24 小时内失败到停止重试的那几天，免得每次检查都再失败一遍（手动点「整理」不跳过）。
    force=True（手动点「重新整理」）：整天全部重写；那天已经在排队的话，改成 force。返回新投递（或改成 force）的日期。"""
    import jobs
    from app.config import SQLITE_PATH
    conn = get_conn()
    try:
        dead = {d for d in days if skip_dead and _recently_dead(conn, d)}
    finally:
        conn.close()
    queued = []
    for d in sorted(days):
        if d in dead:
            continue
        payload = {"day": d, "force": True} if force else {"day": d}
        job, created = jobs.enqueue(SQLITE_PATH, "settle_day", payload=payload, dedupe_key=f"settle_day:{d}", source=source,
                                    update_if_queued=force)
        if created or (force and job and job.get("status") == "queued" and (job.get("payload") or {}).get("force")):
            queued.append(d)
    return queued


def list_episodes(day=None, limit=100):
    conn = get_conn()
    try:
        sql = ("SELECT id, day, conv_id, start_ts, end_ts, title, body, mood, topics, salience, version, issues, model, "
               "created_at FROM episodes WHERE status='active'")
        args = []
        if day:
            sql += " AND day=?"
            args.append(str(day)[:10])
        sql += " ORDER BY day DESC, start_ts ASC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    keys = ("id", "day", "conv_id", "start_ts", "end_ts", "title", "body", "mood", "topics", "salience", "version",
            "issues", "model", "created_at")
    out = []
    for r in rows:
        e = dict(zip(keys, r))
        for k in ("topics", "issues"):
            try:
                e[k] = json.loads(e[k] or "[]")
            except ValueError:
                e[k] = []
        out.append(e)
    return out


def get_episode(eid):
    """片段详情：带出处的原稿，以及每个出处对应的原文"""
    conn = get_conn()
    try:
        r = conn.execute("SELECT id, day, title, body, body_with_refs, mood, topics, refs, salience, status, version, "
                         "issues, model, prompt_version, start_ts, end_ts FROM episodes WHERE id=?", (int(eid),)).fetchone()
        if not r:
            return None
        keys = ("id", "day", "title", "body", "body_with_refs", "mood", "topics", "refs", "salience", "status",
                "version", "issues", "model", "prompt_version", "start_ts", "end_ts")
        e = dict(zip(keys, r))
        for k in ("topics", "refs", "issues"):
            try:
                e[k] = json.loads(e[k] or "[]")
            except ValueError:
                e[k] = []
        ids = [int(i) for i in e["refs"]]
        src = {}
        if ids:
            for mid, role, content, ts, status in conn.execute(
                    f"SELECT id, role, content, ts, status FROM l0_messages WHERE id IN ({','.join('?' * len(ids))})", ids):
                src[mid] = {"id": mid, "role": role, "content": (content or "")[:300], "ts": ts, "status": status}
        e["sources"] = [dict(src[i], n=k) for k, i in enumerate(ids, 1) if i in src]   # n 对应原稿里的 [#n]
        return e
    finally:
        conn.close()
