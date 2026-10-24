"""未完的事（方案 6.8 节，Phase 4 第五步）：答应的事、一起的计划、在等的结果、牵挂、没聊完的话题、定期的约定。

- 发现（loops_scan 任务）：和共同语汇一样，把整理好的天的对话块（分段和每日片段一样）按时间顺序交给模型，
  连同那时还没了结的事；模型列出新出现的（new）、在这段里有了结果或不用再管的（closed：done / dropped，附一句「后续」）、
  又被提起但还没了结的（mentioned）。每块只扫一次（loops_scans 记着块的指纹）。
- 校验：每一条都要附一小段原话（quote），必须在它标的行里一字不差地找得到，不然不收；
  新的事和还没了结的事太像，就当成「又提起」；只给模型看这段对话之前就有的事（以后导入旧聊天时，旧的对话不会去了结后来的事）。
- 状态：open（进行中）/ done（有结果了）/ dropped（不用再管了）/ expired（自动过期：软期限过了 N 天、之后没再提；
  或者没有期限、很久没再提）。过期和了结的都可以在 Dashboard 上重新打开。
  导入旧聊天、后面还有没扫到的对话时，过期按已经扫到的最后一天算，不按今天算（见 clock()）。
- 跟进方式 follow_up_policy：when_relevant（相关时可以提，默认）/ when_due（到期时才提）/ never_push（不主动提，只有你自己提起时才接）。
- pack()：以后开窗时列出的几件（不含「不主动提」的）：先列已经到期和快到期的，再按最近提起的时间。
  每轮按话题相关注入、每个会话最多主动提一次，等新召回正式启用以后再接（现在只在 Dashboard 上预览）。
- 了结时只记在这件事上（后续、原话、出处），不写进 L1，免得影响现在的聊天召回。
"""
import difflib
import json
from datetime import date, timedelta

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import episodes as E

KINDS = {"promise": "答应的事", "plan": "一起的计划", "waiting": "在等的结果", "worry": "牵挂",
         "unfinished_topic": "没聊完的话题", "ritual_due": "定期的约定"}
POLICIES = {"when_relevant": "相关时可以提", "when_due": "到期时才提", "never_push": "不主动提"}
OWNERS = {"user": "你", "ai": "我", "both": "我们"}
STATUS_CN = {"open": "进行中", "done": "有结果了", "dropped": "不用再管了", "expired": "过期了"}
MAX_NEW_PER_CHUNK = 3
MAX_CHUNKS_PER_JOB = 10
OPEN_MAX = 60               # 给模型看的「还没了结的」最多这么多条（最近提起的优先）
SIMILAR = 0.75              # 新的事和还没了结的事这么像，就当成「又提起」
DUE_SOON_DAYS = 3           # 软期限在这么多天以内，开窗时优先列
MAX_TEXT = 80
MAX_RESULT = 120
MAX_TOKENS = 24000
WEEKDAYS = "一二三四五六日"


def cfg():
    return app_settings.get("loops")


def _now():
    return timeutil.utc_now_str()


async def default_llm(prompt):
    """用「模块模型配置」里的「未完的事」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("loops", "episode", "narrative", "summary"), MAX_TOKENS)


# ---------- 读库 ----------

COLS = ("id, kind, text, owner, due_at, status, follow_up_policy, created_l0_id, created_ts, created_quote, closed_l0_id, "
        "closed_ts, closed_quote, closed_text, closed_by, last_mentioned_at, mention_count, source, edited, created_at, updated_at")
KEYS = [c.strip() for c in COLS.split(",")]


def _row(r):
    return dict(zip(KEYS, r))


def _get(conn, lid):
    r = conn.execute(f"SELECT {COLS} FROM open_loops WHERE id=?", (int(lid),)).fetchone()
    if not r:
        raise ValueError("没有这件事")
    return _row(r)


def _open(conn):
    return [_row(r) for r in conn.execute(f"SELECT {COLS} FROM open_loops WHERE status='open' ORDER BY id")]


def _event(conn, lid, event, l0_id=None, ts=None, quote=None, detail=None):
    conn.execute("INSERT INTO loop_events (loop_id, event, l0_id, ts, quote, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (lid, event, l0_id, ts, quote, detail, _now()))


def _norm(t):
    return E._norm(str(t or "")).lower()


def _last_seen(x):
    return x.get("last_mentioned_at") or x.get("created_ts") or ""


# ---------- 发现 ----------

def unscanned(conn, limit=None):
    """还没扫过的对话块（只看整理好的天、够长的块），按时间从早到晚"""
    done = {r[0] for r in conn.execute("SELECT chunk_key FROM loops_scans")}
    out = []
    for (day,) in conn.execute("SELECT day FROM settle_days WHERE status='done' ORDER BY day").fetchall():
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


def visible_open(conn, part):
    """给模型看的还没了结的事：只要这段对话结束前就有的（最近提起的优先，最多 OPEN_MAX 条，按编号排）"""
    end = timeutil.to_utc_str(part[-1][3]) or ""
    rows = [x for x in _open(conn) if (timeutil.to_utc_str(x["created_ts"]) or "") <= end]
    rows.sort(key=_last_seen, reverse=True)
    return sorted(rows[:OPEN_MAX], key=lambda x: x["id"])


def _date_label(ts):
    t = timeutil.to_local(ts)
    return f"{t.year}-{t.month:02d}-{t.day:02d}（周{WEEKDAYS[t.weekday()]}）" if t else ""


def build_prompt(open_loops, part):
    tpl, version = A._prompt("loops_scan")
    lines = []
    for n, (mid, role, content, ts) in enumerate(part, 1):
        text = content if len(content) <= E.MAX_LINE_CHARS else content[:E.MAX_LINE_CHARS] + "……"
        lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {text}")
    ol = "\n".join(f"[L{x['id']}]（{KINDS.get(x['kind'], '')}，{timeutil.local_date(x['created_ts'])} 起"
                   + (f"，软期限 {x['due_at']}" if x["due_at"] else "") + f"）{x['text']}" for x in open_loops) or "（没有）"
    t0, t1 = timeutil.local_str(part[0][3]), timeutil.local_str(part[-1][3])
    span = f"{_date_label(part[0][3])} {t0[11:]} 到 " + (t1[11:] if t1[:10] == t0[:10] else f"{_date_label(part[-1][3])} {t1[11:]}")
    return tpl.replace("{open}", ol).replace("{span}", span).replace("{lines}", "\n".join(lines)), version


def evidence(item, part):
    """出处：标的行号 + 原话（必须在这些行里的某一行一字不差地找得到）。返回 (行号, 原话, 问题)"""
    lines = [int(n) for n in (item.get("lines") or []) if str(n).strip().isdigit() and 1 <= int(n) <= len(part)]
    quote = str(item.get("quote") or "").strip().strip("「」『』“”\"'")
    if not lines:
        return None, None, "没有标出处的行号"
    if len(_norm(quote)) < 2:
        return None, None, "没有附原话"
    hit = next((n for n in lines if _norm(quote) in _norm(part[n - 1][2])), None)
    if hit is None:
        return None, None, f"原话对不上「{quote[:20]}」"
    return hit, quote[:MAX_TEXT], None


def parse_due(value, base_day):
    """软期限：YYYY-MM-DD，在这段对话那天的前一天到一年多以后之间；不合格就当没说"""
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None
    return d.isoformat() if base_day - timedelta(days=1) <= d <= base_day + timedelta(days=400) else None


def similar(text, candidates):
    best, score = None, 0.0
    for x in candidates:
        s = difflib.SequenceMatcher(None, _norm(text), _norm(x["text"])).ratio()
        if s > score:
            best, score = x, s
    return best if score >= SIMILAR else None


def apply_scan(conn, data, part, model=None, version=None):
    """把模型的结果写进库（调用方负责事务）。返回 {"new": [...], "closed": [...], "mentioned": [...], "notes": [...]}"""
    visible = visible_open(conn, part)
    by_id = {x["id"]: x for x in visible}
    base_day = E.day_of(part[0][3]) or E.day_of(timeutil.utc_now())
    now = _now()
    out = {"new": [], "closed": [], "mentioned": [], "notes": []}
    touched = set()

    def at(n):
        return part[n - 1][0], timeutil.to_utc_str(part[n - 1][3])

    def mention(x, n, quote, due=None):
        mid, ts = at(n)
        conn.execute("UPDATE open_loops SET last_mentioned_at=MAX(COALESCE(last_mentioned_at, ''), ?), "
                     "mention_count=mention_count+1, updated_at=? WHERE id=?", (ts, now, x["id"]))
        detail = None
        if due and due != x.get("due_at") and not x.get("edited"):
            conn.execute("UPDATE open_loops SET due_at=? WHERE id=?", (due, x["id"]))
            detail = f"软期限改成 {due}"
        _event(conn, x["id"], "mentioned", mid, ts, quote, detail)
        out["mentioned"].append(x["id"])

    for it in data.get("closed") or []:
        if not isinstance(it, dict):
            continue
        lid = A._ref(it.get("loop"), "L")
        x = by_id.get(lid)
        if not x or lid in touched:
            out["notes"].append(f"了结：没有这件还没了结的事 {it.get('loop')}")
            continue
        n, quote, issue = evidence(it, part)
        if issue:
            out["notes"].append(f"了结 L{lid}：{issue}")
            continue
        status = it.get("status") if it.get("status") in ("done", "dropped") else "done"
        mid, ts = at(n)
        result = str(it.get("result") or "").strip()[:MAX_RESULT] or None
        conn.execute("UPDATE open_loops SET status=?, closed_l0_id=?, closed_ts=?, closed_quote=?, closed_text=?, closed_by='scan', "
                     "last_mentioned_at=MAX(COALESCE(last_mentioned_at, ''), ?), updated_at=? WHERE id=?",
                     (status, mid, ts, quote, result, ts, now, lid))
        _event(conn, lid, status, mid, ts, quote, result)
        touched.add(lid)
        out["closed"].append({"id": lid, "status": status, "result": result})
    for it in data.get("mentioned") or []:
        if not isinstance(it, dict):
            continue
        lid = A._ref(it.get("loop"), "L")
        x = by_id.get(lid)
        if not x or lid in touched:
            continue
        n, quote, issue = evidence(it, part)
        if issue:
            out["notes"].append(f"又提起 L{lid}：{issue}")
            continue
        mention(x, n, quote, parse_due(it.get("due"), base_day))
        touched.add(lid)
    for it in (data.get("new") or [])[:MAX_NEW_PER_CHUNK]:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        text = str(it.get("text") or "").strip()[:MAX_TEXT]
        if kind not in KINDS:
            out["notes"].append(f"新的事：种类不对 {kind}")
            continue
        if len(_norm(text)) < 4:
            out["notes"].append(f"新的事：太短「{text}」")
            continue
        n, quote, issue = evidence(it, part)
        if issue:
            out["notes"].append(f"新的事「{text[:20]}」：{issue}")
            continue
        due = parse_due(it.get("due"), base_day)
        same = similar(text, [x for x in visible if x["id"] not in touched])
        if same:
            mention(same, n, quote, due)
            touched.add(same["id"])
            out["notes"].append(f"「{text[:20]}」和 L{same['id']} 是同一件事，当成又提起")
            continue
        owner = it.get("owner") if it.get("owner") in OWNERS else "user"
        mid, ts = at(n)
        cur = conn.execute(
            "INSERT INTO open_loops (kind, text, owner, due_at, status, follow_up_policy, created_l0_id, created_ts, created_quote, "
            "last_mentioned_at, mention_count, source, edited, model, prompt_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', 'when_relevant', ?, ?, ?, ?, 0, 'scan', 0, ?, ?, ?, ?)",
            (kind, text, owner, due, mid, ts, quote, ts, model, version, now, now))
        lid = cur.lastrowid
        _event(conn, lid, "created", mid, ts, quote, text)
        visible.append({"id": lid, "text": text, "kind": kind, "due_at": due, "created_ts": ts, "edited": 0})
        touched.add(lid)
        out["new"].append({"id": lid, "kind": kind, "text": text})
    return out


async def scan(llm_fn=None, now=None, limit=MAX_CHUNKS_PER_JOB):
    """扫描任务：没扫过的对话块按时间顺序一块一块看 → 自动过期。剩下的块再投一个任务。"""
    import jobs
    from app.config import SQLITE_PATH
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        parts = unscanned(conn, limit + 1)
    finally:
        conn.close()
    more = len(parts) > limit
    stats = {"chunks": 0, "new": [], "closed": [], "mentioned": 0, "notes": []}
    for p in parts[:limit]:
        conn = get_conn()
        try:
            open_loops = visible_open(conn, p["rows"])
        finally:
            conn.close()
        prompt, version = build_prompt(open_loops, p["rows"])
        text, model = await llm_fn(prompt)
        if not text:
            raise RuntimeError("找未完的事：调用模型失败（没有返回内容），稍后重试")
        data = A.parse_json(text)
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = apply_scan(conn, data, p["rows"], model, version)
            conn.execute("INSERT OR REPLACE INTO loops_scans (chunk_key, day, conv_id, scanned_at, model, prompt_version, result) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (p["key"], p["day"], p["conv_id"], _now(), model, version,
                                                          json.dumps(r, ensure_ascii=False)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        stats["chunks"] += 1
        stats["new"] += [x["text"] for x in r["new"]]
        stats["closed"] += [x["result"] or f"L{x['id']}" for x in r["closed"]]
        stats["mentioned"] += len(r["mentioned"])
        stats["notes"] += r["notes"]
    expired = expire(now)
    if more:      # 接着扫剩下的。不能用 "loops_scan" 去重：这个任务自己正在跑，会被当成重复，新任务投不进去
        jobs.enqueue(SQLITE_PATH, "loops_scan", payload={}, dedupe_key="loops_scan:next", source="auto", delay_sec=5,
                     continuation=True)
    stats["notes"] = stats["notes"][:20]
    return {"status": "ok", **stats, "expired": expired, "more": more}


def recently_dead(hours=6):
    """最近一次扫描任务是不是 hours 小时内失败（重试几次都不行）的。比如模型账户没余额了：
    自动投递就先停一停，免得每次定时检查都投一个注定失败的任务；在 Dashboard 上点「现在扫描」不受影响。"""
    conn = get_conn()
    try:
        r = conn.execute("SELECT status, updated_at FROM pipeline_jobs WHERE kind='loops_scan' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not r or r[0] != "dead":
        return False
    t = timeutil.parse_ts(r[1])
    return bool(t and (timeutil.utc_now() - t).total_seconds() < hours * 3600)


def enqueue_scan(source="auto"):
    import jobs
    from app.config import SQLITE_PATH
    if source == "auto" and recently_dead():
        return False
    return jobs.enqueue(SQLITE_PATH, "loops_scan", payload={}, dedupe_key="loops_scan", source=source)[1]


def pending_chunks():
    conn = get_conn()
    try:
        return len(unscanned(conn))
    finally:
        conn.close()


# ---------- 自动过期、开窗预览 ----------

def today(now=None):
    return E.day_of(timeutil.parse_ts(now) if now else timeutil.utc_now())


def clock(conn, now=None):
    """自动过期按哪一天算。平时就是今天；后面还有没扫到的对话时（比如导入了旧聊天、正一天一天往后补），
    按已经扫到的最后一天算——不然五月记下的事会因为「现在已经十月了」马上过期，后面六月的对话就看不到它，
    没法再了结它、提起它。今天的对话还没整理时，也按扫到的最后一天算（晚一天过期，没关系）。"""
    d0 = today(now)
    r = conn.execute("SELECT MAX(day) FROM loops_scans").fetchone()
    if not r or not r[0]:
        return d0
    frontier = E.to_day(r[0])
    last = conn.execute("SELECT MAX(ts) FROM l0_messages WHERE status='active'").fetchone()[0]
    last_day = E.day_of(last) if last else None
    return min(d0, frontier) if last_day and last_day > frontier else d0


def expire(now=None):
    """自动过期（不调用模型，定时检查和每次扫描后都会跑）：
    - 有软期限的：期限过了 expire_after_days 天、期限之后没再提；
    - 没有期限的：stale_days 天没再提。
    「今天」按 clock() 算。返回过期的 id。"""
    c = cfg()
    out = []
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        d0 = clock(conn, now)
        for x in _open(conn):
            last = E.day_of(_last_seen(x))
            if x["due_at"]:
                due = date.fromisoformat(x["due_at"])
                if (d0 - due).days <= c["expire_after_days"] or (last and last > due):
                    continue
                why = f"软期限 {x['due_at']} 过了 {c['expire_after_days']} 天以上，之后没再提"
            else:
                if not last or (d0 - last).days <= c["stale_days"]:
                    continue
                why = f"{c['stale_days']} 天以上没再提"
            conn.execute("UPDATE open_loops SET status='expired', closed_by='auto', closed_ts=?, updated_at=? WHERE id=?",
                         (_now(), _now(), x["id"]))
            _event(conn, x["id"], "expired", detail=why)
            out.append(x["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return out


def due_phrase(due_at, d0):
    if not due_at:
        return ""
    d = date.fromisoformat(due_at)
    gap = (d - d0).days
    label = f"{d.month}月{d.day}日"
    if gap > 1:
        return f"软期限 {label}，还有 {gap} 天"
    if gap == 1:
        return f"软期限是明天（{label}）"
    if gap == 0:
        return f"软期限是今天（{label}）"
    return f"软期限 {label}，已经过了 {-gap} 天"


def line(x, d0):
    """一件事的一行，比如「你在等周三面试的结果（在等的结果，10月19日说的，软期限是明天（10月21日））」"""
    bits = [KINDS.get(x["kind"], "")]
    t = timeutil.to_local(x.get("created_ts"))
    if t:
        bits.append(f"{t.month}月{t.day}日说的")
    due = due_phrase(x.get("due_at"), d0)
    if due:
        bits.append(due)
    return f"{x['text']}（{'，'.join(b for b in bits if b)}）"


def pack(now=None, size=None):
    """以后开窗时列出的几件：
    - 不含「不主动提」的；
    - 先列已经到期和快到期的（按期限）：「相关时可以提」的提前 DUE_SOON_DAYS 天列，「到期时才提」的当天才列；
    - 再按最近提起的时间列「相关时可以提」的。"""
    size = size or cfg()["pack_size"]
    d0 = today(now)
    conn = get_conn()
    try:
        rows = [x for x in _open(conn) if x["follow_up_policy"] != "never_push"]
    finally:
        conn.close()

    def urgent(x):
        if not x["due_at"]:
            return False
        gap = (date.fromisoformat(x["due_at"]) - d0).days
        return gap <= (0 if x["follow_up_policy"] == "when_due" else DUE_SOON_DAYS)
    first = sorted([x for x in rows if urgent(x)], key=lambda x: x["due_at"])
    rest = sorted([x for x in rows if not urgent(x) and x["follow_up_policy"] == "when_relevant"], key=_last_seen, reverse=True)
    return [{"id": x["id"], "line": line(x, d0), "policy": x["follow_up_policy"]} for x in (first + rest)[:size]]


# ---------- 你在 Dashboard 上的操作 ----------

def _check(kind=None, text=None, owner=None, due=None, policy=None):
    if kind is not None and kind not in KINDS:
        raise ValueError("没有这个种类")
    if text is not None and len(_norm(text)) < 4:
        raise ValueError("写得太短了")
    if owner is not None and owner not in OWNERS:
        raise ValueError("「谁的事」只能是你、我或我们")
    if policy is not None and policy not in POLICIES:
        raise ValueError("没有这种跟进方式")
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ValueError("软期限要写成 2026-10-21 这样的日期")


def _tx(fn):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        r = fn(conn)
        conn.commit()
        return r
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close(lid, status="done", result=None):
    """你标「有结果了」/「不用再管了」，可以顺手写一句后续"""
    if status not in ("done", "dropped"):
        raise ValueError("只能标成「有结果了」或「不用再管了」")
    result = (result or "").strip()[:MAX_RESULT] or None

    def run(conn):
        x = _get(conn, lid)
        if x["status"] != "open":
            raise ValueError(f"这件事现在是「{STATUS_CN.get(x['status'])}」")
        conn.execute("UPDATE open_loops SET status=?, closed_text=?, closed_by='dashboard', closed_ts=?, updated_at=? WHERE id=?",
                     (status, result, _now(), _now(), x["id"]))
        _event(conn, x["id"], status, detail=result or "你在 Dashboard 上标的")
    _tx(run)


def reopen(lid):
    def run(conn):
        x = _get(conn, lid)
        if x["status"] == "open":
            raise ValueError("这件事还没了结")
        # 重新打开算「刚提起过」，免得马上又被自动过期
        conn.execute("UPDATE open_loops SET status='open', closed_l0_id=NULL, closed_ts=NULL, closed_quote=NULL, closed_text=NULL, "
                     "closed_by=NULL, last_mentioned_at=?, updated_at=? WHERE id=?", (_now(), _now(), x["id"]))
        _event(conn, x["id"], "reopened", detail="你在 Dashboard 上重新打开")
    _tx(run)


def edit(lid, text=None, kind=None, owner=None, due=None, policy=None):
    """改内容、种类、谁的事、软期限（空字符串 = 去掉期限）、跟进方式。改过的事，扫描不再自动改它的期限。"""
    _check(kind, text, owner, due, policy)

    def run(conn):
        x = _get(conn, lid)
        new = {"text": text.strip()[:MAX_TEXT] if text is not None else x["text"], "kind": kind or x["kind"],
               "owner": owner or x["owner"], "due_at": (due or None) if due is not None else x["due_at"],
               "follow_up_policy": policy or x["follow_up_policy"]}
        changed = [k for k in new if new[k] != x[k]]
        if not changed:
            return
        names = {"text": "内容", "kind": "种类", "owner": "谁的事", "due_at": "软期限", "follow_up_policy": "跟进方式"}
        conn.execute("UPDATE open_loops SET text=?, kind=?, owner=?, due_at=?, follow_up_policy=?, edited=1, updated_at=? WHERE id=?",
                     (new["text"], new["kind"], new["owner"], new["due_at"], new["follow_up_policy"], _now(), x["id"]))
        _event(conn, x["id"], "edited", detail="改了" + "、".join(names[k] for k in changed))
    _tx(run)


def add(kind, text, owner="user", due=None, policy="when_relevant"):
    if not text:
        raise ValueError("写一下是什么事")
    _check(kind, text, owner, due, policy)

    def run(conn):
        now = _now()
        cur = conn.execute(
            "INSERT INTO open_loops (kind, text, owner, due_at, status, follow_up_policy, created_ts, last_mentioned_at, mention_count, "
            "source, edited, created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, 0, 'dashboard', 1, ?, ?)",
            (kind, text.strip()[:MAX_TEXT], owner, due or None, policy, now, now, now, now))
        _event(conn, cur.lastrowid, "created", detail="你在 Dashboard 上加的")
        return cur.lastrowid
    return _tx(run)


# ---------- 查看 ----------

def overview(now=None):
    d0 = today(now)
    conn = get_conn()
    try:
        open_rows = _open(conn)
        closed = [_row(r) for r in conn.execute(
            f"SELECT {COLS} FROM open_loops WHERE status != 'open' ORDER BY COALESCE(closed_ts, updated_at) DESC, id DESC LIMIT 50")]
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM open_loops GROUP BY status").fetchall())
        scanned = conn.execute("SELECT COUNT(*) FROM loops_scans").fetchone()[0]
    finally:
        conn.close()
    for x in open_rows + closed:
        x["line"] = line(x, d0)
    open_rows.sort(key=_last_seen, reverse=True)
    open_rows.sort(key=lambda x: (x["due_at"] is None, x["due_at"] or ""))
    return {"open": open_rows, "closed": closed, "counts": counts, "pack": pack(now), "kinds": KINDS, "policies": POLICIES,
            "owners": OWNERS, "status_cn": STATUS_CN, "scanned_chunks": scanned, "pending_chunks": pending_chunks(),
            "settings": cfg()}


def detail(lid):
    """详情：经过（出现、又提起、了结……每一步的原话）"""
    conn = get_conn()
    try:
        x = _get(conn, lid)
        x["events"] = [{"event": r[0], "ts": r[1], "quote": r[2], "detail": r[3], "created_at": r[4]} for r in conn.execute(
            "SELECT event, ts, quote, detail, created_at FROM loop_events WHERE loop_id=? ORDER BY id", (int(lid),))]
    finally:
        conn.close()
    x["line"] = line(x, today())
    return x
