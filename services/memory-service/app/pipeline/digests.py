"""日摘、周摘、近七天摘要（方案 6.11 节，Phase 4 第三步）：以后接替现在的「近期摘要」。

- 日摘（digest_day 任务）：一天的片段汇总成 1 到 6 行，每行标出处（片段）。每日片段整理完那天就写。
- 周摘（digest_week 任务）：一周（周一到周日）结束后，由这周的日摘写成一段，标出处（日摘）。
- 近七天摘要（digest_recent 任务）：最近七天（按真实日期）的日摘写成一段；没有聊天的日子如实写进去。
  这一段以后放进聊天，替代现在按「最近 100 条消息」压缩的近期摘要。七天里一天都没聊，就直接写「最近七天没有聊天」，不调模型。
- 什么时候重写：输入变了才重写（日摘看那天的片段，周摘和近七天看日摘）；近七天的窗口每天往前挪，窗口里的日摘变了也会重写。
- 校验和片段、卷一样：出处必须存在，引号里的原话必须能在出处里找到，对不上的删掉；全删光了就让模型重写一次，
  还不行就记一条「没通过」（不发布，也不会反复重试，直到输入再变）。
现在只生成、只在 Dashboard 上看，不放进聊天。
"""
import json
import re
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import episodes as E

RECENT_DAYS = 7
MAX_TOKENS = 12000
MAX_DAY_LINES = 6
WEEKDAYS = "一二三四五六日"


def cfg():
    return app_settings.get("digests")


def _now():
    return timeutil.utc_now_str()


def _label(day):
    d = E.to_day(day)
    return f"{d.month}月{d.day}日（周{WEEKDAYS[d.weekday()]}）"


def monday_of(day):
    d = E.to_day(day)
    return d - timedelta(days=d.weekday())


def window(now=None):
    """近七天的窗口：(第一天, 今天)。「今天」按凌晨 4 点的日界算。"""
    now = timeutil.parse_ts(now) if now else timeutil.utc_now()
    today = E.day_of(now)
    return today - timedelta(days=RECENT_DAYS - 1), today


async def default_llm(prompt):
    """用「模块模型配置」里的「日摘·周摘」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("digest", "episode", "narrative", "summary"), MAX_TOKENS)


# ---------- 读库 ----------

def _episodes(conn, day):
    rows = conn.execute("SELECT id, title, mood, body, start_ts, refs FROM episodes WHERE day=? AND status='active' "
                        "ORDER BY start_ts, id", (E.to_day(day).isoformat(),)).fetchall()
    return [{"id": r[0], "title": r[1], "mood": r[2] or "", "body": r[3] or "", "ts": r[4], "l0_refs": A._loads(r[5], [])}
            for r in rows]


def _latest(conn, kind, start):
    """这个时段最近一次的结果（不管发布了没有），看输入有没有变"""
    r = conn.execute("SELECT id, status, source_key, version FROM digests WHERE kind=? AND period_start=? ORDER BY id DESC LIMIT 1",
                     (kind, str(start))).fetchone()
    return {"id": r[0], "status": r[1], "source_key": r[2], "version": r[3]} if r else None


def _day_digests(conn, start, end):
    """[start, end] 里每天的日摘（已发布的），按日期"""
    rows = conn.execute("SELECT id, period_start, body, refs FROM digests WHERE kind='day' AND status='active' "
                        "AND period_start >= ? AND period_start <= ? ORDER BY period_start", (str(start), str(end))).fetchall()
    return [{"id": r[0], "day": r[1], "body": r[2], "refs": A._loads(r[3], [])} for r in rows]


def _write(conn, kind, start, end, body, with_refs, refs, key, issues, model, version, status="active"):
    """发布（或记一条没通过）。调用方负责事务。"""
    prev = conn.execute("SELECT MAX(version) FROM digests WHERE kind=? AND period_start=?", (kind, str(start))).fetchone()[0] or 0
    if status == "active":
        conn.execute("UPDATE digests SET status='superseded' WHERE kind=? AND period_start=? AND status='active'", (kind, str(start)))
    conn.execute("INSERT INTO digests (kind, period_start, period_end, body, body_with_refs, refs, source_key, status, version, "
                 "issues, model, prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (kind, str(start), str(end), body, with_refs, json.dumps(refs), key, status, prev + 1,
                  json.dumps(issues[:30], ensure_ascii=False), model, version, _now()))
    return prev + 1


def _clear(conn, kind, start):
    conn.execute("UPDATE digests SET status='superseded' WHERE kind=? AND period_start=? AND status='active'", (kind, str(start)))


# ---------- 校验 ----------

def verify_lines(lines, ref_map, src):
    """日摘：一行一句，每行都要有出处；出处按片段的时间顺序重新编号。返回 (行列表 with_refs, 片段 id 列表, 问题)"""
    issues, kept, used = [], [], []
    for raw in lines:
        if len(kept) >= MAX_DAY_LINES:
            break
        line = str(raw or "").strip().lstrip("-·•　 ")
        if not line:
            continue
        refs = [int(x) for x in E.REF.findall(line)]
        if not refs:
            issues.append(f"没有标出处：{line[:30]}")
            continue
        bad = [r for r in refs if r not in ref_map]
        if bad:
            issues.append(f"出处不存在 #{bad[0]}：{E.REF.sub('', line)[:30]}")
            continue
        pool = [E._norm(src[ref_map[r]]) for r in refs]
        missing = [q for q in E.QUOTE.findall(E.REF.sub("", line)) if E._norm(q) and not any(E._norm(q) in x for x in pool)]
        if missing:
            issues.append(f"原话对不上「{missing[0][:20]}」")
            continue
        kept.append(line)
        for r in refs:
            if ref_map[r] not in used:
                used.append(ref_map[r])
    used.sort(key=lambda i: min(k for k, v in ref_map.items() if v == i))
    order = {i: k for k, i in enumerate(used, 1)}
    out = [E.REF.sub(lambda m: f"[#{order[ref_map[int(m.group(1))]]}]", ln) for ln in kept]
    return out, used, issues


async def _ask(llm_fn, prompt, check):
    """调模型 + 校验；全没通过就带着问题重写一次。返回 (结果 或 None, 问题, 模型名)"""
    issues, model = [], None
    for attempt in range(2):
        p = prompt if attempt == 0 else prompt + "\n\n【上一次写的没通过校验，问题如下，请改正后重写】\n" + "\n".join(f"- {x}" for x in issues[:10])
        text, model = await llm_fn(p)
        if not text:
            raise RuntimeError("调用模型失败（没有返回内容），稍后重试")
        result, issues = check(A.parse_json(text))
        if result:
            return result, issues, model
    return None, issues, model


# ---------- 日摘 ----------

async def digest_day(day, llm_fn=None, now=None):
    llm_fn = llm_fn or default_llm
    day = E.to_day(day)
    conn = get_conn()
    try:
        eps = _episodes(conn, day)
        latest = _latest(conn, "day", day)
        src = A._sources(conn, [{"episode_id": e["id"], "body": e["body"], "l0_refs": e["l0_refs"]} for e in eps])
    finally:
        conn.close()
    key = ",".join(str(e["id"]) for e in eps)
    if not eps:
        if latest and latest["status"] == "active":
            conn = get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                _clear(conn, "day", day)
                conn.commit()
            finally:
                conn.close()
            _after_day(day, now)
            return {"status": "ok", "day": day.isoformat(), "cleared": True}
        return {"status": "skipped", "message": "这天没有片段"}
    if latest and latest["source_key"] == key:
        return {"status": "skipped", "message": "片段没变"}
    tpl, version = A._prompt("digest_day")
    mats = "\n\n".join(f"[#{k}] {A._when(e['ts'], day)}《{e['title']}》（{e['mood']}）\n{e['body']}" for k, e in enumerate(eps, 1))
    prompt = tpl.replace("{date}", _label(day)).replace("{episodes}", mats)
    ref_map = {k: e["id"] for k, e in enumerate(eps, 1)}

    def check(data):
        lines, used, iss = verify_lines(data.get("lines") or [], ref_map, src)
        return ((lines, used) if lines else None), iss

    result, issues, model = await _ask(llm_fn, prompt, check)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if result:
            lines, used = result
            v = _write(conn, "day", day, day, "\n".join(E.REF.sub("", ln).strip() for ln in lines), "\n".join(lines), used,
                       key, issues, model, version)
        else:
            v = _write(conn, "day", day, day, "", "", [], key, issues, model, version, status="rejected")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _after_day(day, now)
    if not result:
        return {"status": "rejected", "day": day.isoformat(), "message": "写出来的都没通过校验", "issues": issues[:10]}
    return {"status": "ok", "day": day.isoformat(), "version": v, "lines": len(result[0]), "issues": issues[:10]}


def _after_day(day, now=None):
    """日摘变了：窗口里的投递近七天摘要；那周已经结束的投递周摘"""
    import jobs
    from app.config import SQLITE_PATH
    start, today = window(now)
    if start <= day <= today:
        jobs.enqueue(SQLITE_PATH, "digest_recent", payload={}, dedupe_key="digest_recent", source="auto")
    mon = monday_of(day)
    if mon + timedelta(days=6) < today:
        jobs.enqueue(SQLITE_PATH, "digest_week", payload={"week": mon.isoformat()}, dedupe_key=f"digest_week:{mon}", source="auto")


# ---------- 周摘、近七天 ----------

def _gaps(start, end, have):
    """[start, end] 里没有日摘的日子，连着的合成一段，比如「10月20日（周二）到 10月22日（周四）」"""
    out, run = [], []
    d = E.to_day(start)
    while d <= E.to_day(end):
        if d.isoformat() in have:
            if run:
                out.append(run)
            run = []
        else:
            run.append(d)
        d += timedelta(days=1)
    if run:
        out.append(run)
    return "、".join(_label(r[0]) if len(r) == 1 else f"{_label(r[0])}到{_label(r[-1])}" for r in out) or "（没有，每天都聊了）"


def _summary_prompt(name, digests, start, end, extra, gaps_end=None):
    tpl, version = A._prompt(name)
    lines = []
    for k, d in enumerate(digests, 1):
        lines.append(f"[#{k}] {_label(d['day'])}\n" + "\n".join("- " + ln for ln in d["body"].split("\n") if ln.strip()))
    prompt = (tpl.replace("{start}", _label(start)).replace("{end}", _label(end))
              .replace("{days}", "\n\n".join(lines)).replace("{gaps}", _gaps(start, gaps_end or end, {d["day"] for d in digests})))
    for k, v in extra.items():
        prompt = prompt.replace("{" + k + "}", v)
    return prompt, version


def _digest_sources(conn, digests):
    """每条日摘的原文（日摘本身 + 它引用的片段和片段的原文），校验引号用"""
    ep_ids = sorted({int(i) for d in digests for i in d["refs"]})
    eps = {}
    if ep_ids:
        for eid, body, refs in conn.execute(f"SELECT id, body, refs FROM episodes WHERE id IN ({','.join('?' * len(ep_ids))})", ep_ids):
            eps[eid] = {"episode_id": eid, "body": body or "", "l0_refs": A._loads(refs, [])}
    ep_src = A._sources(conn, list(eps.values()))
    return {d["id"]: d["body"] + "\n" + "\n".join(ep_src.get(int(i), "") for i in d["refs"]) for d in digests}


def _stale_days_between(conn, start, end):
    return [d for d in stale_days(conn) if str(start) <= d <= str(end)]


async def _paragraph(kind, name, start, end, llm_fn, extra, key_prefix=""):
    """周摘和近七天共用：输入是 [start, end] 的日摘，写一段话"""
    conn = get_conn()
    try:
        if _stale_days_between(conn, start, end):
            return {"status": "skipped", "message": "这段时间还有日摘没写完，写完会再来"}
        digests = _day_digests(conn, start, end)
        latest = _latest(conn, kind, start if kind == "week" else "recent")
        src = _digest_sources(conn, digests)
    finally:
        conn.close()
    pstart = start if kind == "week" else "recent"          # 近七天只保留一份（period_start 固定写 recent）
    key = key_prefix + ",".join(str(d["id"]) for d in digests)
    if latest and latest["source_key"] == key:
        return {"status": "skipped", "message": "日摘没变"}
    if not digests:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "week":
                _clear(conn, kind, pstart)
                v = None
            else:
                text = f"最近七天（{_label(start)}到{_label(end)}）没有聊天。"
                v = _write(conn, kind, pstart, f"{start}~{end}", text, text, [], key, [], None, None)
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "kind": kind, "empty": True, "version": v}
    # 近七天：今天还没结束，今天没有日摘不算「没聊天」
    gaps_end = end - timedelta(days=1) if kind == "recent" and str(end) not in {d["day"] for d in digests} else end
    prompt, version = _summary_prompt(name, digests, start, end, extra, gaps_end)
    ref_map = {k: d["id"] for k, d in enumerate(digests, 1)}

    def check(data):
        sec, iss = A.verify_section({"title": kind, "body": data.get("body") or ""}, ref_map, src)
        return sec, iss

    sec, issues, model = await _ask(llm_fn, prompt, check)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        period_end = end if kind == "week" else f"{start}~{end}"
        if sec:
            v = _write(conn, kind, pstart, period_end, sec["body"], sec["body_with_refs"], sec["refs"], key, issues, model, version)
        else:
            v = _write(conn, kind, pstart, period_end, "", "", [], key, issues, model, version, status="rejected")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not sec:
        return {"status": "rejected", "kind": kind, "message": "写出来的都没通过校验", "issues": issues[:10]}
    return {"status": "ok", "kind": kind, "start": str(start), "version": v, "days": len(digests), "issues": issues[:10]}


async def digest_week(week, llm_fn=None):
    mon = monday_of(week)
    return await _paragraph("week", "digest_week", mon, mon + timedelta(days=6), llm_fn or default_llm, {})


async def digest_recent(llm_fn=None, now=None):
    start, today = window(now)
    return await _paragraph("recent", "digest_recent", start, today, llm_fn or default_llm, {"today": _label(today)},
                            key_prefix=f"{start}|")


# ---------- 哪些要写 ----------

def stale_days(conn):
    """要重写日摘的天：有片段、但最近一次日摘的输入不一样；或者没有片段了、却还留着日摘"""
    out = []
    have = {}
    for day, eid in conn.execute("SELECT day, id FROM episodes WHERE status='active' ORDER BY day, start_ts, id"):
        have.setdefault(day, []).append(str(eid))
    latest = {}
    for start, status, key in conn.execute("SELECT period_start, status, source_key FROM digests WHERE kind='day' ORDER BY id"):
        latest[start] = (status, key)
    for day, ids in have.items():
        if latest.get(day, (None, None))[1] != ",".join(ids):
            out.append(day)
    for day, (status, key) in latest.items():
        if day not in have and status == "active":
            out.append(day)
    return sorted(set(out))


def stale_weeks(conn, now=None):
    """已经结束的周里，日摘变了（或周摘还没写）的周一"""
    _, today = window(now)
    this_monday = monday_of(today)
    weeks = {}
    for did, start in conn.execute("SELECT id, period_start FROM digests WHERE kind='day' AND status='active' ORDER BY period_start"):
        mon = monday_of(start)
        if mon < this_monday:
            weeks.setdefault(mon.isoformat(), []).append(str(did))
    latest = {}
    for start, status, key in conn.execute("SELECT period_start, status, source_key FROM digests WHERE kind='week' ORDER BY id"):
        latest[start] = (status, key)
    out = [w for w, ids in weeks.items() if latest.get(w, (None, None))[1] != ",".join(ids)]
    out += [w for w, (status, _) in latest.items() if w not in weeks and status == "active"]
    return sorted(set(out))


def recent_stale(conn, now=None):
    start, today = window(now)
    key = f"{start}|" + ",".join(str(d["id"]) for d in _day_digests(conn, start, today))
    latest = _latest(conn, "recent", "recent")
    return not latest or latest["source_key"] != key


def enqueue_stale(source="auto", now=None):
    """投递要写的：先写日摘（日摘写完会自己投递周摘和近七天）；日摘都是新的，才直接投递周摘和近七天。返回投递了什么。"""
    import jobs
    from app.config import SQLITE_PATH
    conn = get_conn()
    try:
        days = stale_days(conn)
        weeks = [] if days else stale_weeks(conn, now)
        recent = (not days) and recent_stale(conn, now)
    finally:
        conn.close()
    out = []
    for d in days:
        if jobs.enqueue(SQLITE_PATH, "digest_day", payload={"day": d}, dedupe_key=f"digest_day:{d}", source=source)[1]:
            out.append(f"digest_day:{d}")
    for w in weeks:
        if jobs.enqueue(SQLITE_PATH, "digest_week", payload={"week": w}, dedupe_key=f"digest_week:{w}", source=source)[1]:
            out.append(f"digest_week:{w}")
    if recent and jobs.enqueue(SQLITE_PATH, "digest_recent", payload={}, dedupe_key="digest_recent", source=source)[1]:
        out.append("digest_recent")
    return out


def enqueue_day(day, source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    return jobs.enqueue(SQLITE_PATH, "digest_day", payload={"day": str(day)}, dedupe_key=f"digest_day:{day}", source=source)[1]


# ---------- 查看 ----------

def _row(r):
    keys = ("id", "kind", "period_start", "period_end", "body", "body_with_refs", "refs", "status", "version", "issues", "model",
            "prompt_version", "created_at")
    d = dict(zip(keys, r))
    d["refs"] = A._loads(d["refs"], [])
    d["issues"] = A._loads(d["issues"], [])
    return d


COLS = "id, kind, period_start, period_end, body, body_with_refs, refs, status, version, issues, model, prompt_version, created_at"


def overview(now=None, days=60):
    conn = get_conn()
    try:
        recent = conn.execute(f"SELECT {COLS} FROM digests WHERE kind='recent' AND status='active' ORDER BY id DESC LIMIT 1").fetchone()
        day_rows = conn.execute(f"SELECT {COLS} FROM digests WHERE kind='day' AND status='active' ORDER BY period_start DESC LIMIT ?",
                                (days,)).fetchall()
        week_rows = conn.execute(f"SELECT {COLS} FROM digests WHERE kind='week' AND status='active' ORDER BY period_start DESC LIMIT 20").fetchall()
        failed = conn.execute("SELECT kind, period_start, created_at FROM digests d WHERE status='rejected' AND id = "
                              "(SELECT MAX(id) FROM digests WHERE kind=d.kind AND period_start=d.period_start)").fetchall()
        pending = {"days": len(stale_days(conn)), "weeks": len(stale_weeks(conn, now)), "recent": recent_stale(conn, now)}
    finally:
        conn.close()
    start, today = window(now)
    return {"recent": _row(recent) if recent else None, "window": [str(start), str(today)],
            "days": [_row(r) for r in day_rows], "weeks": [_row(r) for r in week_rows],
            "failed": [{"kind": f[0], "period_start": f[1], "created_at": f[2]} for f in failed],
            "pending": pending, "enabled": bool(cfg().get("enabled"))}


def get_digest(did):
    """详情：出处（日摘 → 片段；周摘、近七天 → 日摘）"""
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {COLS} FROM digests WHERE id=?", (int(did),)).fetchone()
        if not r:
            return None
        d = _row(r)
        ids = [int(i) for i in d["refs"]]
        src = {}
        if ids:
            q = ",".join("?" * len(ids))
            if d["kind"] == "day":
                for eid, day, title, status in conn.execute(f"SELECT id, day, title, status FROM episodes WHERE id IN ({q})", ids):
                    src[eid] = {"id": eid, "day": day, "title": title, "status": status}
            else:
                for gid, start, body, status in conn.execute(f"SELECT id, period_start, body, status FROM digests WHERE id IN ({q})", ids):
                    src[gid] = {"id": gid, "day": start, "title": _label(start), "body": body, "status": status}
        d["sources"] = [dict(src.get(i, {"id": i}), n=k) for k, i in enumerate(ids, 1)]
        return d
    finally:
        conn.close()
