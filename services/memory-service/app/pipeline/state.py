"""关系状态卡（方案 6.12 节，Phase 4 第六步）：不是分数，是一张有根据的「我们最近」。

- 卡片四行由模型写：节奏（多久聊一次、多在什么时候、比之前多了还是少了）、你的天气（你最近的状态）、
  正在一起经历的（一起在做的、在聊的、还挂着的事）、最近的亲近时刻（可以引一小句原话）。
  另外两行不用模型：轻放（你标了「你提起时才提」「不主动提起」的话题）、上次聊天（大约多久以前，看的时候现算）。
- 材料：最近七天的日摘、最近几个片段、还没了结的事（不含「不主动提」的）、进行中的主题卷。
  节奏只给模型看统计出来的事实（几天聊过、多在几点），卡片里不写具体数字。
- 校验和日摘一样：除了节奏，每行都要标出处，出处必须存在，引号里的原话必须能在出处里找到；
  碰到「轻放」话题关键词的行直接去掉，材料里碰到的也先去掉。全不通过就重写一次，还不行记一条「没通过」。
- 什么时候重写：材料变了才重写（日摘、片段、未完的事、主题卷、话题边界）；最近七天里一天都没聊，就不调模型，只写「最近七天没有聊天」。
- 统计信号（每天几条消息、几点来、心情走向）只在 Dashboard 上给你看，不放进卡片。
- 话题边界 topic_policies：normal（正常）/ explicit_only（你提起时才提）/ excluded（不主动提起）。
  现在用在状态卡上；以后召回也会照着它来。
现在只生成、只在 Dashboard 上看，不放进聊天。
"""
import hashlib
import json
import re
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import digests as D
from app.pipeline import episodes as E
from app.pipeline import loops as LP

LINES = {"rhythm": "节奏", "weather": "你的天气", "ongoing": "正在一起经历的", "close": "最近的亲近时刻"}
POLICIES = {"normal": "正常", "explicit_only": "你提起时才提", "excluded": "不主动提起"}
RECENT_DAYS = 7
SIGNAL_DAYS = 14
RECENT_EPISODES = 8
EPISODE_CHARS = 400
MAX_LINE = 120
MAX_TOKENS = 12000
NIGHT = (23, 4)            # 深夜：23 点到凌晨 4 点
WEEKDAYS = "一二三四五六日"


def cfg():
    return app_settings.get("state")


def _now():
    return timeutil.utc_now_str()


async def default_llm(prompt):
    """用「模块模型配置」里的「关系状态」；没设置就依次用「日摘」「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("state", "digest", "episode", "narrative", "summary"), MAX_TOKENS)


def today(now=None):
    return E.day_of(timeutil.parse_ts(now) if now else timeutil.utc_now())


# ---------- 话题边界 ----------

def _policy_row(r):
    return {"id": r[0], "topic": r[1], "keywords": A._loads(r[2], []), "policy": r[3], "note": r[4] or "",
            "created_at": r[5], "updated_at": r[6]}


def policies(conn=None):
    own = conn is None
    conn = conn or get_conn()
    try:
        return [_policy_row(r) for r in conn.execute(
            "SELECT id, topic, keywords, policy, note, created_at, updated_at FROM topic_policies ORDER BY id")]
    finally:
        if own:
            conn.close()


def light_keywords(rows):
    """「轻放」话题的关键词（没填关键词就用话题名本身），小写"""
    out = []
    for p in rows:
        if p["policy"] == "normal":
            continue
        kws = [k for k in p["keywords"] if str(k).strip()] or [p["topic"]]
        out += [str(k).strip().lower() for k in kws if str(k).strip()]
    return out


def touches(text, kws):
    t = str(text or "").lower()
    return next((k for k in kws if k and k in t), None)


def _clean_policy(topic, keywords, policy):
    topic = str(topic or "").strip()
    if not topic or len(topic) > 20:
        raise ValueError("话题名不能为空，也不能超过 20 个字")
    if policy not in POLICIES:
        raise ValueError("没有这种边界")
    if isinstance(keywords, str):
        keywords = re.split(r"[、,，\s]+", keywords)
    kws = []
    for k in keywords or []:
        k = str(k).strip()
        if k and k not in kws:
            if len(k) > 20:
                raise ValueError("关键词太长了（一个词不超过 20 个字）")
            kws.append(k)
    return topic, kws[:30], policy


def add_policy(topic, keywords=None, policy="explicit_only", note=""):
    topic, kws, policy = _clean_policy(topic, keywords, policy)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM topic_policies WHERE topic=?", (topic,)).fetchone():
            raise ValueError("已经有这个话题了")
        cur = conn.execute("INSERT INTO topic_policies (topic, keywords, policy, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (topic, json.dumps(kws, ensure_ascii=False), policy, str(note or "")[:100], _now(), _now()))
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def edit_policy(pid, topic=None, keywords=None, policy=None, note=None):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT id, topic, keywords, policy, note, created_at, updated_at FROM topic_policies WHERE id=?",
                         (int(pid),)).fetchone()
        if not r:
            raise ValueError("没有这个话题")
        old = _policy_row(r)
        t, kws, pol = _clean_policy(topic if topic is not None else old["topic"],
                                    keywords if keywords is not None else old["keywords"], policy or old["policy"])
        if t != old["topic"] and conn.execute("SELECT 1 FROM topic_policies WHERE topic=? AND id != ?", (t, int(pid))).fetchone():
            raise ValueError("已经有这个话题了")
        conn.execute("UPDATE topic_policies SET topic=?, keywords=?, policy=?, note=?, updated_at=? WHERE id=?",
                     (t, json.dumps(kws, ensure_ascii=False), pol, str(note if note is not None else old["note"])[:100], _now(), int(pid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_policy(pid):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("DELETE FROM topic_policies WHERE id=?", (int(pid),)).rowcount:
            raise ValueError("没有这个话题")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 统计信号（只给 Dashboard，不进卡片） ----------

def _sessions(times, gap_min=E.GAP_MINUTES):
    """按时间排好的消息时间 → 一次次聊天 [(开始, 结束, 条数)]，隔 45 分钟以上算另一次"""
    out = []
    for t in times:
        if out and (t - out[-1][1]).total_seconds() <= gap_min * 60:
            out[-1] = (out[-1][0], t, out[-1][2] + 1)
        else:
            out.append((t, t, 1))
    return out


def signals(conn, d0):
    start = d0 - timedelta(days=SIGNAL_DAYS - 1)
    lo, _ = E.day_window_utc(start)
    rows = conn.execute("SELECT role, ts FROM l0_messages WHERE status='active' AND datetime(ts) >= ? ORDER BY datetime(ts)",
                        (lo,)).fetchall()
    days = {(start + timedelta(days=i)).isoformat(): {"user": 0, "ai": 0, "sessions": 0, "minutes": 0} for i in range(SIGNAL_DAYS)}
    hours = [0] * 24
    times = []
    for role, ts in rows:
        t = timeutil.to_local(ts)
        if not t:
            continue
        day = E.day_of(ts).isoformat()
        if day not in days:
            continue
        days[day]["user" if role == "user" else "ai"] += 1
        if role == "user":
            hours[t.hour] += 1
        times.append(t)
    for s, e, n in _sessions(times):
        day = (s - timedelta(hours=E.DAY_START_HOUR)).date().isoformat()
        if day in days:
            days[day]["sessions"] += 1
            days[day]["minutes"] += max(1, int((e - s).total_seconds() // 60))
    val = {}
    for v, ts in conn.execute("SELECT valence, ts FROM l1_memories WHERE status='active' AND valence IS NOT NULL AND datetime(ts) >= ?",
                              (lo,)):
        d = E.day_of(ts)
        if d:
            val.setdefault(d.isoformat(), []).append(float(v))
    last = conn.execute("SELECT MAX(datetime(ts)) FROM l0_messages WHERE status='active'").fetchone()[0]
    order = sorted(days)

    def span(keys):
        ds = [days[k] for k in keys]
        return {"days_chatted": sum(1 for x in ds if x["user"]), "messages": sum(x["user"] + x["ai"] for x in ds),
                "sessions": sum(x["sessions"] for x in ds), "minutes": sum(x["minutes"] for x in ds)}
    total_user = sum(hours)
    night = sum(hours[h] for h in range(24) if h >= NIGHT[0] or h < NIGHT[1])
    return {"days": [{"day": k, **days[k], "valence": round(sum(val[k]) / len(val[k]), 2) if k in val else None} for k in order],
            "this_week": span(order[-RECENT_DAYS:]), "last_week": span(order[:-RECENT_DAYS]),
            "hours": hours, "night_share": round(night / total_user, 2) if total_user else None,
            "last_ts": timeutil.to_utc_str(last) if last else None}


def _hour_band(hours):
    bands = [("凌晨（0 点到 4 点）", range(0, 4)), ("清晨（4 点到 8 点）", range(4, 8)), ("上午（8 点到 12 点）", range(8, 12)),
             ("下午（12 点到 18 点）", range(12, 18)), ("晚上（18 点到 23 点）", range(18, 23)), ("深夜（23 点到 24 点）", range(23, 24))]
    total = sum(hours)
    if not total:
        return "（没有消息）"
    parts = sorted(((sum(hours[h] for h in r), name) for name, r in bands), reverse=True)
    return "、".join(f"{name}占{round(n * 10 / total)}成" for n, name in parts if n and round(n * 10 / total) >= 1)


def rhythm_facts(sig):
    a, b = sig["this_week"], sig["last_week"]
    lines = [f"最近七天：{a['days_chatted']} 天聊过，聊了 {a['sessions']} 次，一共 {a['messages']} 条消息，大约 {a['minutes']} 分钟。",
             f"再往前七天：{b['days_chatted']} 天聊过，聊了 {b['sessions']} 次，一共 {b['messages']} 条消息。",
             f"你发消息的时间：{_hour_band(sig['hours'])}。"]
    chatted = [d["day"] for d in sig["days"] if d["user"]]
    if chatted:
        lines.append("聊过的日子：" + "、".join(D._label(d) for d in chatted[-RECENT_DAYS:]) + "。")
    return "\n".join(lines)


# ---------- 材料 ----------

def gather(conn, d0):
    """材料（已去掉碰到「轻放」话题的部分）：[{n, kind, id, text（给模型看）, src（校验用）}]，以及话题边界和材料的指纹"""
    start = d0 - timedelta(days=RECENT_DAYS - 1)
    pols = policies(conn)
    kws = light_keywords(pols)
    items = []
    digests = D._day_digests(conn, start, d0)
    dsrc = D._digest_sources(conn, digests)
    for d in digests:
        lines = [ln for ln in d["body"].split("\n") if ln.strip() and not touches(ln, kws)]
        if lines:
            items.append({"kind": "digest", "id": d["id"], "text": f"{D._label(d['day'])}的日摘\n" + "\n".join("- " + ln for ln in lines),
                          "src": dsrc.get(d["id"], d["body"])})
    eps = conn.execute("SELECT id, day, title, mood, body, start_ts, refs FROM episodes WHERE status='active' AND day >= ? AND day <= ? "
                       "ORDER BY day DESC, start_ts DESC, id DESC LIMIT ?", (str(start), str(d0), RECENT_EPISODES)).fetchall()
    eps = [e for e in reversed(eps) if not touches(f"{e[2]} {e[4]}", kws)]
    esrc = A._sources(conn, [{"episode_id": e[0], "body": e[4] or "", "l0_refs": A._loads(e[6], [])} for e in eps])
    for e in eps:
        body = (e[4] or "")
        body = body if len(body) <= EPISODE_CHARS else body[:EPISODE_CHARS] + "……"
        items.append({"kind": "episode", "id": e[0], "text": f"片段 {A._when(e[5], e[1])}《{e[2]}》（{e[3] or ''}）\n{body}",
                      "src": esrc.get(e[0], e[4] or "")})
    loops = conn.execute("SELECT id, kind, text, due_at, created_quote FROM open_loops WHERE status='open' AND follow_up_policy != 'never_push' "
                         "ORDER BY COALESCE(last_mentioned_at, created_ts) DESC LIMIT 8").fetchall()
    for lid, kind, text, due, quote in loops:
        if touches(f"{text} {quote or ''}", kws):
            continue
        items.append({"kind": "loop", "id": lid, "text": f"还没了结的事（{LP.KINDS.get(kind, '')}" + (f"，软期限 {due}" if due else "") + f"）：{text}",
                      "src": f"{text}\n{quote or ''}"})
    for aid, title, summary in conn.execute("SELECT id, title, summary FROM arcs WHERE status='active' ORDER BY COALESCE(last_material_ts, created_at) DESC LIMIT 5"):
        if touches(f"{title} {summary or ''}", kws):
            continue
        items.append({"kind": "arc", "id": aid, "text": f"进行中的主题卷《{title}》" + (f"：{summary}" if summary else ""),
                      "src": f"{title}\n{summary or ''}"})
    for n, it in enumerate(items, 1):
        it["n"] = n
    arcs_v = conn.execute("SELECT id, title, version, summary FROM arcs WHERE status='active' ORDER BY id").fetchall()
    key_src = {"digests": [d["id"] for d in digests], "episodes": [e[0] for e in eps],
               "loops": [(x[0], x[2], x[3]) for x in loops], "arcs": [list(a) for a in arcs_v],
               "policies": [(p["topic"], p["keywords"], p["policy"]) for p in pols]}
    key = hashlib.sha1(json.dumps(key_src, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return items, pols, kws, key, bool(digests)


def build_prompt(items, sig, d0):
    tpl, version = A._prompt("state_card")
    src = "\n\n".join(f"[#{it['n']}] {it['text']}" for it in items) or "（没有）"
    prompt = (tpl.replace("{today}", f"{d0.year}年{D._label(d0)}").replace("{rhythm}", rhythm_facts(sig)).replace("{sources}", src))
    return prompt, version


# ---------- 校验 ----------

def _cut(text):
    """一行太长就在句号处截断（出处标记不算长度）"""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lstrip("-·•　 ")
    if len(E.REF.sub("", t)) <= MAX_LINE:
        return t
    cut = max(t.rfind(p, 0, MAX_LINE + 20) for p in "。！？")
    return t[:cut + 1] if cut > 20 else t[:MAX_LINE] + "……"


def verify_lines(data, items, kws):
    """返回 (按 LINES 顺序的行 [{key, text, with_refs, refs}], 用到的材料 [{kind, id}], 问题)"""
    by_n = {it["n"]: it for it in items}
    issues, raw = [], {}
    for x in data.get("lines") or []:
        if not isinstance(x, dict):
            continue
        k = x.get("key")
        if k not in LINES:
            issues.append(f"没有这一行：{k}")
            continue
        if k in raw:
            continue
        raw[k] = _cut(x.get("text"))
    kept, used = [], []
    for k in LINES:
        t = raw.get(k)
        if not t:
            continue
        plain = E.REF.sub("", t).strip()
        hit = touches(plain, kws)
        if hit:
            issues.append(f"{LINES[k]}：碰到了轻放的话题，去掉")
            continue
        refs = [int(r) for r in E.REF.findall(t)]
        if k == "rhythm":
            if re.search(r"\d", plain):
                issues.append("节奏：写了具体数字，去掉")
                continue
            kept.append({"key": k, "text": plain, "refs_n": []})
            continue
        if not refs:
            issues.append(f"{LINES[k]}：没有标出处")
            continue
        bad = [r for r in refs if r not in by_n]
        if bad:
            issues.append(f"{LINES[k]}：出处不存在 #{bad[0]}")
            continue
        pool = [E._norm(by_n[r]["src"]) for r in refs]
        missing = [q for q in E.QUOTE.findall(plain) if E._norm(q) and not any(E._norm(q) in s for s in pool)]
        if missing:
            issues.append(f"{LINES[k]}：原话对不上「{missing[0][:20]}」")
            continue
        kept.append({"key": k, "text": t, "refs_n": refs})
        for r in refs:
            if r not in used:
                used.append(r)
    order = {n: i for i, n in enumerate(used, 1)}
    out = []
    for line in kept:
        with_refs = E.REF.sub(lambda m: f"[#{order[int(m.group(1))]}]", line["text"]) if line["refs_n"] else line["text"]
        out.append({"key": line["key"], "text": re.sub(r"[ \t]{2,}", " ", E.REF.sub("", with_refs)).strip(), "with_refs": with_refs,
                    "refs": [order[n] for n in line["refs_n"]]})
    return out, [{"kind": by_n[n]["kind"], "id": by_n[n]["id"]} for n in used], issues


# ---------- 生成 ----------

def _latest(conn):
    r = conn.execute("SELECT id, status, source_key FROM relationship_states ORDER BY id DESC LIMIT 1").fetchone()
    return {"id": r[0], "status": r[1], "source_key": r[2]} if r else None


def _write(conn, d0, lines, refs, sig, key, issues, model, version, status="active"):
    prev = conn.execute("SELECT MAX(version) FROM relationship_states").fetchone()[0] or 0
    if status == "active":
        conn.execute("UPDATE relationship_states SET status='superseded' WHERE status='active'")
    conn.execute("INSERT INTO relationship_states (day, lines, refs, signals, source_key, status, version, issues, model, prompt_version, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (str(d0), json.dumps(lines, ensure_ascii=False), json.dumps(refs), json.dumps(sig, ensure_ascii=False), key, status,
                  prev + 1, json.dumps(issues[:30], ensure_ascii=False), model, version, _now()))
    return prev + 1


def _save(d0, lines, refs, sig, key, issues, model, version, status="active"):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        v = _write(conn, d0, lines, refs, sig, key, issues, model, version, status)
        conn.commit()
        return v
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def refresh(llm_fn=None, now=None, force=False):
    """材料变了就重写状态卡（force=True 不管变没变）"""
    llm_fn = llm_fn or default_llm
    d0 = today(now)
    conn = get_conn()
    try:
        items, pols, kws, key, has_digest = gather(conn, d0)
        sig = signals(conn, d0)
        latest = _latest(conn)
        stale_days = D.stale_days(conn)
    finally:
        conn.close()
    if stale_days and not force:
        return {"status": "skipped", "message": "还有日摘没写完，写完会再来"}
    if latest and latest["source_key"] == key and not force:
        return {"status": "skipped", "message": "材料没变"}
    if not has_digest:
        lines = [{"key": "rhythm", "text": "最近七天我们没有聊天。", "with_refs": "最近七天我们没有聊天。", "refs": []}]
        v = _save(d0, lines, [], sig, key, [], None, None)
        return {"status": "ok", "version": v, "empty": True}
    prompt, version = build_prompt(items, sig, d0)
    issues, all_issues, model, result = [], [], None, None
    for attempt in range(2):
        p = prompt if attempt == 0 else prompt + "\n\n【上一次写的没通过校验，问题如下，请改正后重写】\n" + "\n".join(f"- {x}" for x in issues[:10])
        text, model = await llm_fn(p)
        if not text:
            raise RuntimeError("关系状态：调用模型失败（没有返回内容），稍后重试")
        lines, refs, issues = verify_lines(A.parse_json(text), items, kws)
        all_issues += [f"（第 {attempt + 1} 次）{x}" for x in issues]
        if lines:
            result = (lines, refs)
            break
    if not result:
        _save(d0, [], [], sig, key, all_issues, model, version, status="rejected")
        return {"status": "rejected", "message": "写出来的都没通过校验", "issues": all_issues[:10]}
    v = _save(d0, result[0], result[1], sig, key, all_issues, model, version)
    return {"status": "ok", "version": v, "lines": len(result[0]), "issues": all_issues[:10]}


def is_stale(now=None):
    """材料变了、而且日摘都写完了（日摘还没写完就先等等，写完会再来）"""
    conn = get_conn()
    try:
        if D.stale_days(conn):
            return False
        _, _, _, key, _ = gather(conn, today(now))
        latest = _latest(conn)
    finally:
        conn.close()
    return not latest or latest["source_key"] != key


def enqueue(source="auto", delay_sec=0, force=False):
    import jobs
    from app.config import SQLITE_PATH
    return jobs.enqueue(SQLITE_PATH, "state_refresh", payload={"force": bool(force)}, dedupe_key="state_refresh", source=source,
                        delay_sec=delay_sec, update_if_queued=bool(force))[1]


# ---------- 呈现 ----------

def gap_phrase(last_ts, now=None):
    t = timeutil.parse_ts(last_ts)
    if not t:
        return None
    now = timeutil.parse_ts(now) if now else timeutil.utc_now()
    mins = (now - t).total_seconds() / 60
    if mins < 60:
        return "不到一小时前"
    hours = mins / 60
    if hours < 24:
        return f"大约 {int(round(hours))} 小时前"
    days = hours / 24
    if days < 2:
        return "大约一天前"
    if days < 14:
        return f"大约 {int(round(days))} 天前"
    if days < 60:
        return f"大约 {int(round(days / 7))} 个星期前"
    return f"大约 {int(round(days / 30))} 个月前"


def render(lines, pols, last_ts, now=None):
    """以后放进聊天的样子（现在只在 Dashboard 上预览）"""
    out = ["【我们最近】"]
    for line in lines:
        out.append(f"· {LINES.get(line['key'], line['key'])}：{line['text']}")
    light = [p["topic"] for p in pols if p["policy"] != "normal"]
    if light:
        out.append(f"· 轻放：{'、'.join(light)}——你不主动提，我就不提。")
    gap = gap_phrase(last_ts, now)
    if gap:
        out.append(f"· 上次聊天：{gap}。")
    return "\n".join(out)


def _row(r):
    keys = ("id", "day", "lines", "refs", "signals", "source_key", "status", "version", "issues", "model", "prompt_version", "created_at")
    d = dict(zip(keys, r))
    for k, dv in (("lines", []), ("refs", []), ("signals", {}), ("issues", [])):
        d[k] = A._loads(d[k], dv)
    return d


COLS = "id, day, lines, refs, signals, source_key, status, version, issues, model, prompt_version, created_at"


def _sources(conn, refs):
    """出处的标题（日摘 → 日期；片段 → 标题；未完的事 → 内容；主题卷 → 标题）"""
    out = []
    for k, ref in enumerate(refs, 1):
        kind, rid = ref.get("kind"), ref.get("id")
        title, extra = None, ""
        if kind == "digest":
            r = conn.execute("SELECT period_start, body FROM digests WHERE id=?", (rid,)).fetchone()
            title, extra = (f"{D._label(r[0])}的日摘", r[1]) if r else (None, "")
        elif kind == "episode":
            r = conn.execute("SELECT day, title FROM episodes WHERE id=?", (rid,)).fetchone()
            title = f"片段《{r[1]}》（{r[0]}）" if r else None
        elif kind == "loop":
            r = conn.execute("SELECT text FROM open_loops WHERE id=?", (rid,)).fetchone()
            title = f"未完的事：{r[0]}" if r else None
        elif kind == "arc":
            r = conn.execute("SELECT title FROM arcs WHERE id=?", (rid,)).fetchone()
            title = f"主题卷《{r[0]}》" if r else None
        out.append({"n": k, "kind": kind, "id": rid, "title": title or "（已经不在了）", "body": extra})
    return out


def overview(now=None):
    conn = get_conn()
    try:
        cur = conn.execute(f"SELECT {COLS} FROM relationship_states WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        hist = [{"id": r[0], "version": r[1], "status": r[2], "created_at": r[3]} for r in conn.execute(
            "SELECT id, version, status, created_at FROM relationship_states ORDER BY id DESC LIMIT 15")]
        pols = policies(conn)
        d0 = today(now)
        sig = signals(conn, d0)
        _, _, _, key, _ = gather(conn, d0)
        latest = _latest(conn)
        cur = _row(cur) if cur else None
        if cur:
            cur["sources"] = _sources(conn, cur["refs"])
    finally:
        conn.close()
    return {"current": cur, "card": render(cur["lines"], pols, sig["last_ts"], now) if cur else None,
            "signals": sig, "policies": pols, "policy_names": POLICIES, "line_names": LINES, "history": hist,
            "stale": not latest or latest["source_key"] != key, "enabled": bool(cfg().get("enabled"))}


def get_version(sid):
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {COLS} FROM relationship_states WHERE id=?", (int(sid),)).fetchone()
        if not r:
            return None
        d = _row(r)
        d["sources"] = _sources(conn, d["refs"])
        return d
    finally:
        conn.close()
