"""事实层（方案 6.6 节，Phase 4 第八步）：一段时间内不会变、以后聊天时我应该知道的「键 → 值」。

- 发现（facts_scan 任务）：和共同语汇、未完的事一样，把整理好的天的对话块按时间顺序交给模型，连同那时已经记下的事实；
  模型列出这段里说到的事实（key、中文名 label、value、明确说的 explicit / 随口的 vague、出处行号和原话）。每块只扫一次。
- 校验：key 必须是 user. / ai. / us. 开头的小写点分写法；原话必须在标的行里一字不差地找得到；每块最多 MAX_PER_CHUNK 条。
- 冲突审计（借鉴 LMC-5 的思路，自己的实现）：
  - 这个 key 还没有：记成「现在的」；
  - 和现在的值一样（或很像）：算又说到一次；列了好几个的（称呼、喜好）只提到其中几个也算，多了新的就是变了；
  - 不一样、而且是明确说的（「我搬到杭州了」）：旧值变成「以前的」（记下到什么时候为止），新值成为「现在的」；
  - 不一样、但只是随口一提：不换，记一条「可能变了」，等你确认，或者等以后明确说了再换；
  - 你在 Dashboard 上改过的事实，扫描不会自动换掉，只会标「可能变了」；你说「不对」的值，以后扫到一样的不再记。
  - 只给模型看这段对话之前就有的事实；补扫更早的对话时，旧的说法不会盖掉后来的，只记成「以前的」。
- profile_lines()：以后放进聊天的「档案」几行（关于你 / 关于我 / 我们之间，带「可能变了，合适时可以确认一下」的提示）；
  碰到「轻放」话题关键词的不列。现在只在 Dashboard 上预览，不放进聊天。
"""
import difflib
import hashlib
import json
import re

import timeutil
from app import settings as app_settings
from app.config import get_conn
from app.pipeline import arcs as A
from app.pipeline import episodes as E

SUBJECTS = {"user": "关于你", "ai": "关于我", "us": "我们之间"}
STATUS_CN = {"current": "现在的", "superseded": "以前的", "disputed": "可能变了", "rejected": "不对"}
CERTAINTY_CN = {"explicit": "明确说的", "vague": "随口提到"}
KEY_RE = re.compile(r"^(user|ai|us)(\.[a-z0-9_]+){1,5}$")
MAX_PER_CHUNK = 5
MAX_CHUNKS_PER_JOB = 10
KNOWN_MAX = 120             # 给模型看的已经记下的事实最多这么多条（最近说到的优先）
SAME = 0.85                 # 值这么像就算一样
MAX_VALUE = 60
MAX_LABEL = 20
MAX_QUOTE = 80
PROFILE_MAX_CHARS = 800
MAX_TOKENS = 16000


def cfg():
    return app_settings.get("facts")


def _now():
    return timeutil.utc_now_str()


async def default_llm(prompt):
    """用「模块模型配置」里的「事实」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("facts", "episode", "narrative", "summary"), MAX_TOKENS)


# ---------- 读库 ----------

COLS = ("id, fact_key, label, subject, value, status, certainty, valid_from, valid_to, superseded_by, l0_id, quote, "
        "last_confirmed_at, confirm_count, source, edited, created_at, updated_at")
KEYS = [c.strip() for c in COLS.split(",")]


def _row(r):
    return dict(zip(KEYS, r))


def _get(conn, fid):
    r = conn.execute(f"SELECT {COLS} FROM facts WHERE id=?", (int(fid),)).fetchone()
    if not r:
        raise ValueError("没有这条事实")
    return _row(r)


def _rows(conn, where, args=()):
    return [_row(r) for r in conn.execute(f"SELECT {COLS} FROM facts WHERE {where} ORDER BY id", args)]


def _current(conn, key):
    r = conn.execute(f"SELECT {COLS} FROM facts WHERE fact_key=? AND status='current' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    return _row(r) if r else None


def _event(conn, fid, event, l0_id=None, ts=None, quote=None, detail=None):
    conn.execute("INSERT INTO fact_events (fact_id, event, l0_id, ts, quote, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (fid, event, l0_id, ts, quote, detail, _now()))


def _norm(t):
    return E._norm(str(t or "")).lower()


ITEM_SPLIT = re.compile(r"[、，,;；/|｜]+")


def _items(t):
    return {x for x in (_norm(p) for p in ITEM_SPLIT.split(str(t or ""))) if x}


def same_value(a, b):
    """a 是这次说到的，b 是已经记下的。
    列了好几个的（称呼、喜好，用顿号隔开）按一个一个比：a 里的都在 b 里算一样（只提到其中一个），多了新的就不一样；
    其他的值很像就算一样"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ia, ib = _items(a), _items(b)
    if len(ia) > 1 or len(ib) > 1:
        return ia <= ib
    return difflib.SequenceMatcher(None, na, nb).ratio() >= SAME


def _seen(x):
    return x.get("last_confirmed_at") or x.get("valid_from") or ""


# ---------- 发现 ----------

def unscanned(conn, limit=None):
    """还没扫过的对话块（只看整理好的天、够长的块），按时间从早到晚"""
    done = {r[0] for r in conn.execute("SELECT chunk_key FROM facts_scans")}
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


def visible(conn, part):
    """给模型看的已经记下的事实：这段对话结束前就有的「现在的」（最近说到的优先，最多 KNOWN_MAX 条，按 key 排）"""
    end = timeutil.to_utc_str(part[-1][3]) or ""
    rows = [x for x in _rows(conn, "status='current'") if (timeutil.to_utc_str(x["valid_from"]) or "") <= end]
    rows.sort(key=_seen, reverse=True)
    return sorted(rows[:KNOWN_MAX], key=lambda x: x["fact_key"])


def build_prompt(known, part):
    tpl, version = A._prompt("facts_scan")
    lines = []
    for n, (mid, role, content, ts) in enumerate(part, 1):
        text = content if len(content) <= E.MAX_LINE_CHARS else content[:E.MAX_LINE_CHARS] + "……"
        lines.append(f"[#{n}][{'你' if role == 'user' else '我'}] {text}")
    kn = "\n".join(f"- {x['label']}（{x['fact_key']}）：{x['value']}（{timeutil.local_date(x['valid_from'])} 起"
                   + ("，随口提到的" if x.get("certainty") == "vague" else "") + "）" for x in known) or "（还没有）"
    t0, t1 = timeutil.local_str(part[0][3]), timeutil.local_str(part[-1][3])
    span = f"{t0} 到 " + (t1[11:] if t1[:10] == t0[:10] else t1)
    return tpl.replace("{known}", kn).replace("{span}", span).replace("{lines}", "\n".join(lines)), version


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
    return hit, quote[:MAX_QUOTE], None


def _insert(conn, key, label, value, status, certainty, valid_from, l0_id, quote, source, model=None, version=None,
            valid_to=None, superseded_by=None, edited=0):
    now = _now()
    cur = conn.execute(
        "INSERT INTO facts (fact_key, label, subject, value, status, certainty, valid_from, valid_to, superseded_by, l0_id, quote, "
        "last_confirmed_at, confirm_count, source, edited, model, prompt_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
        (key, label, key.split(".", 1)[0], value, status, certainty, valid_from, valid_to, superseded_by, l0_id, quote,
         valid_from, source, edited, model, version, now, now))
    return cur.lastrowid


def record(conn, key, label, value, certainty, l0_id, ts, quote, source="scan", model=None, version=None):
    """一条说到的事实写进库（冲突审计在这里）。调用方负责事务。返回 (动作, 事实 id, 说明)。
    动作：created / confirmed / superseded（换掉了旧值）/ disputed（记成可能变了）/ history（比现在的旧，记成以前的）/ skipped"""
    now = _now()
    cur = _current(conn, key)
    if any(same_value(value, r["value"]) for r in _rows(conn, "fact_key=? AND status='rejected'", (key,))):
        return "skipped", None, f"「{value}」你说过不对"
    if cur and same_value(value, cur["value"]):
        conn.execute("UPDATE facts SET last_confirmed_at=MAX(COALESCE(last_confirmed_at, ''), ?), confirm_count=confirm_count+1, "
                     "updated_at=? WHERE id=?", (ts, now, cur["id"]))
        _event(conn, cur["id"], "confirmed", l0_id, ts, quote)
        return "confirmed", cur["id"], None
    if not cur:
        old = [r for r in _rows(conn, "fact_key=? AND status='superseded'", (key,)) if same_value(value, r["value"])]
        fid = _insert(conn, key, label, value, "current", certainty, ts, l0_id, quote, source, model, version)
        _event(conn, fid, "created", l0_id, ts, quote, value)
        return "created", fid, (f"和以前的 #{old[0]['id']} 一样" if old else None)
    if (timeutil.to_utc_str(cur["valid_from"]) or "") > (timeutil.to_utc_str(ts) or ""):
        # 补扫更早的对话：这个说法比现在知道的旧，记成以前的，不盖掉后来的
        if any(same_value(value, r["value"]) for r in _rows(conn, "fact_key=? AND status='superseded'", (key,))):
            return "skipped", None, "以前的值里已经有了"
        fid = _insert(conn, key, label, value, "superseded", certainty, ts, l0_id, quote, source, model, version,
                      valid_to=cur["valid_from"], superseded_by=cur["id"])
        _event(conn, fid, "created", l0_id, ts, quote, f"比现在的「{cur['value']}」早，记成以前的")
        return "history", fid, None
    if certainty == "explicit" and not cur["edited"]:
        fid = _insert(conn, key, cur["label"] or label, value, "current", certainty, ts, l0_id, quote, source, model, version)
        conn.execute("UPDATE facts SET status='superseded', valid_to=?, superseded_by=?, updated_at=? WHERE id=?", (ts, fid, now, cur["id"]))
        _event(conn, cur["id"], "superseded", l0_id, ts, quote, f"换成「{value}」")
        _event(conn, fid, "created", l0_id, ts, quote, f"原来是「{cur['value']}」")
        for d in _rows(conn, "fact_key=? AND status='disputed'", (key,)):
            conn.execute("UPDATE facts SET status='superseded', valid_to=?, superseded_by=?, updated_at=? WHERE id=?", (ts, fid, now, d["id"]))
            _event(conn, d["id"], "resolved", l0_id, ts, quote, f"后来明确说了「{value}」")
        return "superseded", fid, f"「{cur['value']}」→「{value}」"
    for d in _rows(conn, "fact_key=? AND status='disputed'", (key,)):
        if same_value(value, d["value"]):
            conn.execute("UPDATE facts SET last_confirmed_at=MAX(COALESCE(last_confirmed_at, ''), ?), confirm_count=confirm_count+1, "
                         "updated_at=? WHERE id=?", (ts, now, d["id"]))
            _event(conn, d["id"], "confirmed", l0_id, ts, quote)
            return "disputed", d["id"], "又提到了这个不一样的说法"
    fid = _insert(conn, key, cur["label"] or label, value, "disputed", certainty, ts, l0_id, quote, source, model, version)
    _event(conn, fid, "disputed", l0_id, ts, quote, f"现在记的是「{cur['value']}」" + ("（你改过，不自动换）" if cur["edited"] else ""))
    return "disputed", fid, f"「{cur['value']}」可能变成了「{value}」"


def apply_scan(conn, data, part, model=None, version=None):
    """把模型的结果写进库（调用方负责事务）。返回 {"created": [...], "confirmed": n, "superseded": [...], "disputed": [...], "notes": [...]}"""
    out = {"created": [], "confirmed": 0, "superseded": [], "disputed": [], "history": 0, "notes": []}
    seen = set()
    for it in (data.get("facts") or [])[:MAX_PER_CHUNK]:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip().lower()
        label = str(it.get("label") or "").strip()[:MAX_LABEL]
        value = str(it.get("value") or "").strip()[:MAX_VALUE]
        if not KEY_RE.match(key):
            out["notes"].append(f"key 写法不对「{key[:40]}」")
            continue
        if not label or not _norm(value):
            out["notes"].append(f"{key}：没有中文名或值")
            continue
        if key in seen:
            continue
        n, quote, issue = evidence(it, part)
        if issue:
            out["notes"].append(f"{key}「{value[:20]}」：{issue}")
            continue
        certainty = it.get("certainty") if it.get("certainty") in CERTAINTY_CN else "vague"
        mid, ts = part[n - 1][0], timeutil.to_utc_str(part[n - 1][3])
        action, fid, why = record(conn, key, label, value, certainty, mid, ts, quote, "scan", model, version)
        seen.add(key)
        if action == "created":
            out["created"].append(f"{label}：{value}")
        elif action == "confirmed":
            out["confirmed"] += 1
        elif action == "superseded":
            out["superseded"].append(f"{label}：{why}")
        elif action == "disputed":
            out["disputed"].append(f"{label}：{why}")
        elif action == "history":
            out["history"] += 1
        if why and action in ("skipped", "created"):
            out["notes"].append(f"{label}：{why}")
    return out


async def scan(llm_fn=None, limit=MAX_CHUNKS_PER_JOB):
    """扫描任务：没扫过的对话块按时间顺序一块一块看。剩下的块再投一个任务。"""
    import jobs
    from app.config import SQLITE_PATH
    llm_fn = llm_fn or default_llm
    conn = get_conn()
    try:
        parts = unscanned(conn, limit + 1)
    finally:
        conn.close()
    more = len(parts) > limit
    stats = {"chunks": 0, "created": [], "confirmed": 0, "superseded": [], "disputed": [], "history": 0, "notes": []}
    for p in parts[:limit]:
        conn = get_conn()
        try:
            known = visible(conn, p["rows"])
        finally:
            conn.close()
        prompt, version = build_prompt(known, p["rows"])
        text, model = await llm_fn(prompt)
        if not text:
            raise RuntimeError("找事实：调用模型失败（没有返回内容），稍后重试")
        data = A.parse_json(text)
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = apply_scan(conn, data, p["rows"], model, version)
            conn.execute("INSERT OR REPLACE INTO facts_scans (chunk_key, day, conv_id, scanned_at, model, prompt_version, result) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (p["key"], p["day"], p["conv_id"], _now(), model, version,
                                                          json.dumps(r, ensure_ascii=False)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        stats["chunks"] += 1
        for k in ("created", "superseded", "disputed", "notes"):
            stats[k] += r[k]
        stats["confirmed"] += r["confirmed"]
        stats["history"] += r["history"]
    if more:      # 接着扫剩下的（continuation：自己正在跑不算重复）
        jobs.enqueue(SQLITE_PATH, "facts_scan", payload={}, dedupe_key="facts_scan:next", source="auto", delay_sec=5,
                     continuation=True)
    stats["notes"] = stats["notes"][:20]
    return {"status": "ok", **stats, "more": more}


def recently_dead(hours=6):
    """最近一次扫描任务是不是 hours 小时内失败（重试几次都不行）的：自动投递先停一停"""
    conn = get_conn()
    try:
        r = conn.execute("SELECT status, updated_at FROM pipeline_jobs WHERE kind='facts_scan' ORDER BY id DESC LIMIT 1").fetchone()
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
    return jobs.enqueue(SQLITE_PATH, "facts_scan", payload={}, dedupe_key="facts_scan", source=source)[1]


def pending_chunks():
    conn = get_conn()
    try:
        return len(unscanned(conn))
    finally:
        conn.close()


# ---------- 以后放进聊天的「档案」 ----------

def _light(conn):
    from app.pipeline import state as S
    try:
        return S.light_keywords(S.policies(conn))
    except Exception:
        return []


def profile_lines(max_chars=PROFILE_MAX_CHARS):
    """关于你 / 关于我 / 我们之间：每条「中文名：值」；可能变了的写一句「合适时可以自然地确认一下」。碰到「轻放」话题的不列。"""
    conn = get_conn()
    try:
        cur = _rows(conn, "status='current'")
        disputed = _rows(conn, "status='disputed'")
        kws = _light(conn)
    finally:
        conn.close()
    from app.pipeline import state as S
    dis = {}
    for d in disputed:
        dis.setdefault(d["fact_key"], d)
    out, size = [], 0
    for subj, name in SUBJECTS.items():
        rows = sorted([x for x in cur if x["subject"] == subj], key=_seen, reverse=True)
        block = []
        for x in rows:
            if S.touches(f"{x['label']} {x['value']}", kws):
                continue
            line = f"- {x['label']}：{x['value']}" + ("（随口提到的，不一定准）" if x["certainty"] == "vague" else "")
            d = dis.get(x["fact_key"])
            if d and not S.touches(d["value"], kws):
                line += f"（最近好像说过「{d['value']}」，可能变了，合适时可以自然地确认一下）"
            if size + len(line) > max_chars:
                break
            block.append(line)
            size += len(line)
        if block:
            out.append(f"{name}：")
            out += block
    return out


# ---------- 你在 Dashboard 上的操作 ----------

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


def confirm(fid):
    """「可能变了」的那个说法是对的：它成为现在的，原来的变成以前的"""
    def run(conn):
        d = _get(conn, fid)
        if d["status"] != "disputed":
            raise ValueError(f"这条现在是「{STATUS_CN.get(d['status'])}」")
        now = _now()
        cur = _current(conn, d["fact_key"])
        if cur:
            conn.execute("UPDATE facts SET status='superseded', valid_to=?, superseded_by=?, updated_at=? WHERE id=?",
                         (d["valid_from"] or now, d["id"], now, cur["id"]))
            _event(conn, cur["id"], "superseded", detail=f"你确认换成「{d['value']}」")
        conn.execute("UPDATE facts SET status='current', certainty='explicit', updated_at=? WHERE id=?", (now, d["id"]))
        _event(conn, d["id"], "resolved", detail="你在 Dashboard 上确认")
    _tx(run)


def reject(fid):
    """这条不对：不要了，以后扫到一样的值也不再记。如果它是现在的，最近的一个以前的值恢复成现在的"""
    def run(conn):
        x = _get(conn, fid)
        if x["status"] == "rejected":
            raise ValueError("已经标过不对了")
        now = _now()
        conn.execute("UPDATE facts SET status='rejected', updated_at=? WHERE id=?", (now, x["id"]))
        _event(conn, x["id"], "rejected", detail="你在 Dashboard 上标的")
        if x["status"] == "current":
            prev = conn.execute("SELECT id FROM facts WHERE fact_key=? AND status='superseded' AND superseded_by=? ORDER BY id DESC LIMIT 1",
                                (x["fact_key"], x["id"])).fetchone()
            if prev:
                conn.execute("UPDATE facts SET status='current', valid_to=NULL, superseded_by=NULL, updated_at=? WHERE id=?", (now, prev[0]))
                _event(conn, prev[0], "restored", detail="后来的那个值你说不对，恢复成这个")
    _tx(run)


def restore(fid):
    """标错了「不对」：恢复。这个 key 现在没有值，就恢复成现在的；已经有了，就恢复成以前的"""
    def run(conn):
        x = _get(conn, fid)
        if x["status"] != "rejected":
            raise ValueError("这条没有标过不对")
        status = "superseded" if _current(conn, x["fact_key"]) else "current"
        conn.execute("UPDATE facts SET status=?, updated_at=? WHERE id=?", (status, _now(), x["id"]))
        _event(conn, x["id"], "restored", detail="你在 Dashboard 上恢复")
    _tx(run)


def edit(fid, label=None, value=None):
    """改中文名或值。改过的事实，扫描不会自动换掉它（不一样的说法只标「可能变了」）"""
    if value is not None and not _norm(value):
        raise ValueError("值不能为空")
    if label is not None and not label.strip():
        raise ValueError("中文名不能为空")

    def run(conn):
        x = _get(conn, fid)
        new_label = label.strip()[:MAX_LABEL] if label is not None else x["label"]
        new_value = value.strip()[:MAX_VALUE] if value is not None else x["value"]
        if (new_label, new_value) == (x["label"], x["value"]):
            return
        conn.execute("UPDATE facts SET label=?, value=?, edited=1, updated_at=? WHERE id=?", (new_label, new_value, _now(), x["id"]))
        _event(conn, x["id"], "edited", detail=f"「{x['label']}：{x['value']}」→「{new_label}：{new_value}」")
    _tx(run)


def add(subject, label, value):
    """自己加一条：key 按中文名生成（user.custom.xxxx），以后扫描看得到它，同一件事会用这个 key"""
    if subject not in SUBJECTS:
        raise ValueError("关于谁只能是你、我或我们")
    label, value = (label or "").strip()[:MAX_LABEL], (value or "").strip()[:MAX_VALUE]
    if not label or not _norm(value):
        raise ValueError("写一下中文名和值")
    key = f"{subject}.custom.k{hashlib.sha1(label.encode('utf-8')).hexdigest()[:8]}"

    def run(conn):
        now = _now()
        cur = _current(conn, key)
        if cur:
            conn.execute("UPDATE facts SET status='superseded', valid_to=?, updated_at=? WHERE id=?", (now, now, cur["id"]))
        fid = _insert(conn, key, label, value, "current", "explicit", now, None, None, "dashboard", edited=1)
        if cur:
            conn.execute("UPDATE facts SET superseded_by=? WHERE id=?", (fid, cur["id"]))
        _event(conn, fid, "created", detail="你在 Dashboard 上加的")
        return fid
    return _tx(run)


# ---------- 查看 ----------

def overview():
    conn = get_conn()
    try:
        cur = _rows(conn, "status='current'")
        disputed = _rows(conn, "status='disputed'")
        history = _rows(conn, "status IN ('superseded', 'rejected')")
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM facts GROUP BY status").fetchall())
        scanned = conn.execute("SELECT COUNT(*) FROM facts_scans").fetchone()[0]
    finally:
        conn.close()
    cur_by_key = {x["fact_key"]: x for x in cur}
    for d in disputed:
        c = cur_by_key.get(d["fact_key"])
        d["current_value"] = c["value"] if c else None
        d["current_id"] = c["id"] if c else None
    hist = {}
    for h in history:
        hist.setdefault(h["fact_key"], []).append(h)
    groups = {}
    for x in sorted(cur, key=lambda x: (x["fact_key"])):
        x["history"] = sorted(hist.get(x["fact_key"], []), key=lambda h: h.get("valid_from") or "", reverse=True)
        groups.setdefault(x["subject"], []).append(x)
    orphan = [h for k, hs in hist.items() if k not in cur_by_key for h in hs]
    return {"groups": groups, "disputed": disputed, "orphan_history": orphan, "counts": counts, "subjects": SUBJECTS,
            "status_cn": STATUS_CN, "certainty_cn": CERTAINTY_CN, "scanned_chunks": scanned, "pending_chunks": pending_chunks(),
            "profile": profile_lines(), "settings": cfg()}


def detail(fid):
    conn = get_conn()
    try:
        x = _get(conn, fid)
        x["events"] = [{"event": r[0], "ts": r[1], "quote": r[2], "detail": r[3], "created_at": r[4]} for r in conn.execute(
            "SELECT event, ts, quote, detail, created_at FROM fact_events WHERE fact_id=? ORDER BY id", (int(fid),))]
        x["versions"] = _rows(conn, "fact_key=?", (x["fact_key"],))
    finally:
        conn.close()
    return x
