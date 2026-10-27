"""换窗衔接（方案第 16 章，Phase 5 第一步）：窗口里的时间标记、滑出窗口的那段、新窗口接上一窗。

API 模式下，前端每一轮发的是「最近 N 条」的窗口（Pluvia 现在是 70 多条，每过几轮整体往后挪一截）。
窗口一直在往后滑，等于一直在「换窗」。这里补上三件事：
1. 窗口里看不出时间：昨晚的「晚安」和今早的「早安」紧挨着，模型会当成连着说的。
   → 窗口开头那条、和上一句隔了很久的那条，前面各加一行附注（这条是几点的、隔了多久）。
2. 滑出窗口、但还没整理成每日片段的那段（每日片段要等一天过完才写），模型哪儿都看不到。
   → 每一块（同一段对话里大约 16 条）写一两句小结（app/pipeline/bridge.py；快滑出去时就提前写好），
     放在窗口开头那行附注里，并带一个「那会儿」的温度词。
3. 新开一个对话（新窗口）时，模型不知道上一个窗口最后在聊什么。
   → 新对话的第一条附上「上一个窗口停在什么时候、最后几句」。
   从旧对话里挑几段搬过来（挑选式搬家，接入约定第 14 节）时：挑的几段之间隔了多久、中间省略了几条、
   那段时间有哪些片段，写在断口那条前面；搬过来的和新说的之间写明「从这里开始是新开的窗口」。

附注只加在发给模型的消息里（网关加），不存进 L0，前端也看不到。
附注里只写绝对时间（10 月 27 日 23:40），不写「3 小时前」：同样的窗口每一轮附注一模一样，缓存才能命中。
窗口开头那行（含滑出窗口的小结）按窗口的第一条消息定格（window_state）：窗口没挪，这一行就不变；
新写好的小结等窗口下次挪的时候再放进去（那一轮前缀本来就变了，不多花缓存）。

和 L0 怎么对上：网关把窗口里每条消息按存 L0 时的写法清理后算一个指纹（hash_text，两边写法一致），
这里和这个会话的历史（分支 / 搬家的会话含从原会话带过来的那段）的指纹按顺序对齐，对上的就知道是哪一行、什么时间。
对不上的消息不加附注（宁可不加，不能加错）。
"""
import difflib
import hashlib
import json
import re
from datetime import timedelta

import timeutil
from app import branches
from app import settings as app_settings
from app.pipeline import episodes as EP

FORMAT_VERSION = "1"
BRIDGE_CHUNK_MSGS = 16      # 小结一块大约这么多条（到了这个数，在下一条「你」说的话那里切开）
BRIDGE_CHUNK_CHARS = 6000
PREFETCH = 24               # 窗口最前面这么多条以内、已经写完的块：提前写好小结，等它滑出去时就有了
BRIDGE_SPAN_H = 48          # 往前最多看这么久（更早的应该已经整理成每日片段了）
OWN_TAIL_MIN = 400          # 每次最少读这个会话最后这么多条
SESSION_GAP_MIN = EP.GAP_MINUTES
PREV_LAST_MSGS = 4          # 新窗口：上一个窗口最后几句
PREV_MSG_CHARS = 80
PREV_MAX_DAYS = 7           # 上一个窗口停下超过这么多天就不提了
PREV_BRIDGE_LINES = 4
EPISODE_TITLES = 3
WEEKDAYS = "一二三四五六日"

IMG_RE = re.compile(r"\[图片(?:：[^\]\n]*)?\]")
# 和网关一致的空白字符（不用 \s：Python 和 JavaScript 的 \s 不完全一样）
WS_RE = re.compile("[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0  -     　]+")


def cfg():
    return app_settings.get("windows")


# ---------- 指纹（和网关 server.js 的 windowHash 一致） ----------

def norm(text):
    """去掉图片占位和所有空白；只发了图的消息按图的张数（和 l0_merge.norm 的规则一样）"""
    raw = text or ""
    s = WS_RE.sub("", IMG_RE.sub("", raw))
    if s:
        return s
    n = len(IMG_RE.findall(raw))
    return "[图片]" * n if n else WS_RE.sub("", raw)


def hash_text(role, text):
    r = "u" if role == "user" else "a"
    return hashlib.sha1(f"{r}\x00{norm(text)}".encode("utf-8")).hexdigest()[:12]


# ---------- 时间的写法 ----------

def fmt_abs(ts, now=None):
    """10 月 27 日（周二）23:40；不是今年的带上年份"""
    d = timeutil.to_local(ts)
    if not d:
        return ""
    n = timeutil.to_local(now) if now else timeutil.to_local(timeutil.utc_now())
    y = f"{d.year} 年 " if n and d.year != n.year else ""
    return f"{y}{d.month} 月 {d.day} 日（周{WEEKDAYS[d.weekday()]}）{d.strftime('%H:%M')}"


def fmt_short(ts, ref_date=None):
    """和 ref_date（本地日期）是同一天只写 08:18，否则写 10 月 26 日 23:10"""
    d = timeutil.to_local(ts)
    if not d:
        return ""
    if ref_date and d.date() == ref_date:
        return d.strftime("%H:%M")
    return f"{d.month} 月 {d.day} 日 {d.strftime('%H:%M')}"


def dur_text(a, b):
    """两条消息之间隔了多久（a、b 是存储格式的时间）"""
    ta, tb = timeutil.parse_ts(a), timeutil.parse_ts(b)
    if not ta or not tb:
        return ""
    secs = (tb - ta).total_seconds()
    if secs < 3600:
        return f"{max(1, round(secs / 60))} 分钟"
    if secs < 86400:
        return f"{round(secs / 3600)} 小时"
    days = (timeutil.to_local(tb).date() - timeutil.to_local(ta).date()).days
    return f"{max(1, days)} 天"


def _hours(a, b):
    ta, tb = timeutil.parse_ts(a), timeutil.parse_ts(b)
    return (tb - ta).total_seconds() / 3600 if ta and tb else 0.0


def _one_line(text, limit):
    t = WS_RE.sub(" ", text or "").strip()
    return t if len(t) <= limit else t[:limit].rstrip() + "……"


# ---------- 会话从哪来（conv_windows） ----------

def lineage(c, conv_id):
    r = c.execute("SELECT kind, prev_conv_id, created_at FROM conv_windows WHERE conv_id=?", (conv_id,)).fetchone()
    return {"kind": r[0], "prev": r[1], "created_at": r[2]} if r else None


def _latest_other_conv(c, conv_id, now):
    lo = (timeutil.parse_ts(now) - timedelta(days=PREV_MAX_DAYS)).strftime(timeutil.STORE_FMT)
    r = c.execute("SELECT conv_id, MAX(ts) t FROM l0_messages WHERE status='active' AND conv_id!=? AND datetime(ts) >= ? "
                  "GROUP BY conv_id ORDER BY t DESC LIMIT 1", (conv_id, lo)).fetchone()
    return r[0] if r else None


def register_lineage(c, conv_id, link, history_len, now):
    """第一次见到一个会话时记下它从哪来。link：前端这一轮带的 {kind: branch/forged, conversation_id}。
    history_len：这一轮窗口里这一条之前有几条。只有窗口里只有这一条（真的是新开的）才算 new。"""
    cur = lineage(c, conv_id)
    if cur:
        return cur
    if c.execute("SELECT 1 FROM l0_messages WHERE conv_id=? LIMIT 1", (conv_id,)).fetchone():
        return None          # 以前就存过（这个功能上线前开的会话）：不登记
    kind, prev = "unknown", None
    if link and link.get("conversation_id") and link.get("conversation_id") != conv_id:
        kind, prev = ("forged" if link.get("kind") == "forged" else "branch"), str(link["conversation_id"])[:200]
    elif history_len == 0:
        kind, prev = "new", _latest_other_conv(c, conv_id, now)
    c.execute("INSERT OR IGNORE INTO conv_windows (conv_id, kind, prev_conv_id, created_at) VALUES (?, ?, ?, ?)",
              (conv_id, kind, prev, now))
    return lineage(c, conv_id)


# ---------- 历史 ----------

ROW_COLS = "id, conv_id, role, content, ts, msg_idx"


def _row(r, own):
    return {"id": r[0], "conv": r[1], "role": r[2], "content": r[3] or "", "ts": r[4], "msg_idx": r[5], "own": own}


def history_rows(c, conv_id, tail=None):
    """这个会话的历史（按顺序）：[{id, conv, role, content, ts, msg_idx, own}]，以及读全了没有。
    分支 / 搬家的会话：从原会话带过来的那段（conv_branches.prefix 指向原会话的行，own=False）+ 自己存的。
    tail：自己存的只读最后这么多条（带过来的那段总是全读）。"""
    out = []
    b = branches.get(c, conv_id)
    if b and b["prefix"]:
        ids = [e[2] for e in b["prefix"]]
        got = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in c.execute(f"SELECT {ROW_COLS} FROM l0_messages WHERE status='active' AND id IN ({','.join('?' * len(part))})", part):
                got[r[0]] = r
        out += [_row(got[i], False) for i in ids if i in got]
    sql = f"SELECT {ROW_COLS} FROM l0_messages WHERE conv_id=? AND status='active' ORDER BY msg_idx DESC, id DESC"
    own = c.execute(sql + (" LIMIT ?" if tail else ""), (conv_id, tail) if tail else (conv_id,)).fetchall()
    complete = not tail or len(own) < tail
    out += [_row(r, True) for r in reversed(own)]
    return out, complete


def _aliases(c, rows):
    """在 Dashboard 上改过的消息：前端手里还是改之前的原文。{改之前的指纹: 现在的指纹}"""
    ids = [r["id"] for r in rows]
    by_id = {r["id"]: r for r in rows}
    out = {}
    try:
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for lid, old in c.execute(f"SELECT l0_id, old_content FROM l0_edits WHERE l0_id IN ({','.join('?' * len(part))})", part):
                r = by_id.get(lid)
                if r:
                    a, b = hash_text(r["role"], old), hash_text(r["role"], r["content"])
                    if a != b:
                        out[a] = b
    except Exception:
        pass
    return out


def align(rows, entries, aliases=None):
    """窗口里的第 i 条 → 历史里的第 j 行。按顺序对齐（difflib），对不上的不给。"""
    aliases = aliases or {}
    h = [hash_text(r["role"], r["content"]) for r in rows]
    e = [aliases.get(x.get("h"), x.get("h")) for x in entries]
    sm = difflib.SequenceMatcher(None, h, e, autojunk=False)
    pairs = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            pairs[b + k] = a + k
    return pairs


# ---------- 整理到哪了 ----------

def _settled(c, rows):
    """每日片段已经整理过的消息（它们在「最近几天的片段」里，不用再写小结）"""
    days = {EP.day_of(r["ts"]) for r in rows if r["ts"]}
    info = {}
    for d in days:
        if not d:
            continue
        x = c.execute("SELECT status, last_l0_id FROM settle_days WHERE day=?", (d.isoformat(),)).fetchone()
        if x and x[0] == "done" and x[1] is not None:
            info[d] = x[1]
    return {r["id"] for r in rows if r["ts"] and EP.day_of(r["ts"]) in info and r["id"] <= info[EP.day_of(r["ts"])]}


# ---------- 小结的块 ----------

def chunk_key(rows):
    return EP.chunk_key([(r["id"], r["role"], r["content"], r["ts"]) for r in rows])


def _sessions(rows):
    out, cur, prev = [], [], None
    for r in rows:
        t = timeutil.parse_ts(r["ts"])
        if cur and prev and t and (t - prev).total_seconds() > SESSION_GAP_MIN * 60:
            out.append(cur)
            cur = []
        cur.append(r)
        prev = t or prev
    if cur:
        out.append(cur)
    return out


def _split(session):
    out, cur, size = [], [], 0
    for r in session:
        n = min(len(r["content"]), 800)
        if cur and (len(cur) >= BRIDGE_CHUNK_MSGS or size + n > BRIDGE_CHUNK_CHARS) and r["role"] == "user":
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def chunks(rows, now=None):
    """rows（按顺序、带 pos = 在历史里的位置）→ [{rows, key, closed, p0, p1}]。
    closed：这一块不会再变长了（同一段对话后面还有下一块，或者这段对话已经停了 45 分钟以上）。"""
    now_t = timeutil.parse_ts(now) if now else timeutil.utc_now()
    sess = _sessions(rows)
    out = []
    for si, s in enumerate(sess):
        parts = _split(s)
        last_t = timeutil.parse_ts(s[-1]["ts"])
        ended = si < len(sess) - 1 or (last_t and (now_t - last_t).total_seconds() >= SESSION_GAP_MIN * 60)
        for pi, p in enumerate(parts):
            out.append({"rows": p, "key": chunk_key(p), "closed": bool(ended or pi < len(parts) - 1),
                        "p0": p[0]["pos"], "p1": p[-1]["pos"]})
    return out


def _bridges(c, keys):
    out = {}
    keys = list(keys)
    for k in range(0, len(keys), 500):
        part = keys[k:k + 500]
        for r in c.execute(f"SELECT chunk_key, status, gist, mood, start_ts, end_ts FROM window_bridges "
                           f"WHERE chunk_key IN ({','.join('?' * len(part))})", part):
            out[r[0]] = {"status": r[1], "gist": r[2] or "", "mood": r[3] or "", "start_ts": r[4], "end_ts": r[5]}
    return out


def span_chunks(c, rows, pw, now=None):
    """窗口第一条（历史里的位置 pw）之前、还没整理成片段的那段，加上窗口最前面 PREFETCH 条以内写完的块。
    返回 [{rows, key, closed, p0, p1, out, partial, prefetch}]（按时间）。
    切块用的是从那以后的全部消息：一块写没写完（closed）要看后面还有没有话。"""
    if pw is None or pw < 0 or pw >= len(rows):
        return []
    ref = timeutil.parse_ts(rows[pw]["ts"])
    lo_t = ref - timedelta(hours=BRIDGE_SPAN_H) if ref else None
    seq = []
    for j, r in enumerate(rows):
        t = timeutil.parse_ts(r["ts"])
        if lo_t and t and t < lo_t:
            continue
        seq.append(dict(r, pos=j))
    if not seq:
        return []
    settled = _settled(c, [r for r in seq if r["pos"] < pw + PREFETCH + BRIDGE_CHUNK_MSGS * 2])
    out = []
    for ch in chunks(seq, now):
        if ch["p0"] >= pw + PREFETCH:
            break
        if all(r["id"] in settled for r in ch["rows"]):
            continue
        ch["out"] = ch["p1"] < pw
        ch["partial"] = ch["p0"] < pw <= ch["p1"]
        ch["prefetch"] = ch["closed"] and ch["p1"] < pw + PREFETCH
        ch["pw"] = pw
        if ch["out"] or ch["partial"] or ch["prefetch"]:
            out.append(ch)
    return out


def needs_from(c, chs):
    """这些块里还没写小结、可以写了的（写完的块才写）"""
    have = _bridges(c, [ch["key"] for ch in chs])
    return [ch for ch in chs if ch["closed"] and ch["key"] not in have]


# ---------- 拼附注 ----------

def _light_kws(c):
    try:
        from app.pipeline import state as S
        return S.light_keywords(S.policies(c))
    except Exception:
        return []


def _touches(text, kws):
    t = str(text or "").lower()
    return any(k and k in t for k in kws)


def bridge_lines(c, chs, ref_date, kws, max_chars):
    """已经滑出窗口的块 → 「· 08:18–09:40：小结（那会儿：困）」；还没写好小结的、已经滑出窗口的条数"""
    have = _bridges(c, [ch["key"] for ch in chs])
    lines, pending = [], 0
    for ch in chs:
        if not (ch["out"] or ch["partial"]):
            continue
        b = have.get(ch["key"])
        if not b:
            pending += sum(1 for r in ch["rows"] if r["pos"] < ch.get("pw", 10 ** 9))
            continue
        if b["status"] != "active" or not b["gist"] or _touches(b["gist"], kws):
            continue
        t0, t1 = fmt_short(ch["rows"][0]["ts"], ref_date), fmt_short(ch["rows"][-1]["ts"], ref_date)
        if t1 and t1 != t0 and t1[:-5] == t0[:-5]:
            t1 = t1[-5:]
        label = t0 if not t1 or t1 == t0 else f"{t0}–{t1}"
        lines.append(f"· {label}：{b['gist']}" + (f"（那会儿：{b['mood']}）" if b["mood"] else ""))
    kept, size = [], 0
    for line in reversed(lines):
        if kept and size + len(line) > max_chars:
            kept.insert(0, "· （更早的几段略）")
            break
        kept.insert(0, line)
        size += len(line) + 1
    return kept, pending


def prev_text(c, prev_conv, first_ts, kws, max_chars, now=None):
    """新窗口：上一个窗口停在什么时候、还没整理成片段的那部分说了什么、最后几句"""
    if not prev_conv:
        return ""
    rows, _ = history_rows(c, prev_conv, tail=OWN_TAIL_MIN)
    if not rows:
        return ""
    last = rows[-1]
    if _hours(last["ts"], first_ts or timeutil.utc_now_str()) > PREV_MAX_DAYS * 24:
        return ""
    tail = rows[-PREV_LAST_MSGS:]
    ref = timeutil.to_local(last["ts"]).date() if last["ts"] else None
    parts = [f"上一个窗口停在 {fmt_abs(last['ts'], now)}。"]
    pw = len(rows) - len(tail)
    chs = [ch for ch in span_chunks(c, rows, pw, now) if ch["out"]]
    lines, _ = bridge_lines(c, chs, ref, kws, max_chars // 2)
    if lines:
        parts.append("那个窗口里还没整理成每日片段的部分：\n" + "\n".join(lines[-PREV_BRIDGE_LINES:]))
    said = [f"{'你' if r['role'] == 'user' else '我'}：{_one_line(r['content'], PREV_MSG_CHARS)}" for r in tail]
    parts.append("最后几句是：\n" + "\n".join(said))
    return "\n".join(parts)


def episode_titles(c, a_ts, b_ts, kws):
    """两条消息之间那段时间的每日片段标题（最多 EPISODE_TITLES 个，按时间）"""
    try:
        rows = c.execute("SELECT title, start_ts FROM episodes WHERE status='active' AND datetime(start_ts) > datetime(?) "
                         "AND datetime(start_ts) < datetime(?) ORDER BY salience DESC, start_ts LIMIT 12", (a_ts, b_ts)).fetchall()
    except Exception:
        return []
    rows = [r for r in rows if r[0] and not _touches(r[0], kws)][:EPISODE_TITLES]
    return [f"《{t}》" for t, _ in sorted(rows, key=lambda r: r[1] or "")]


def _skipped(c, a, b):
    """原会话里 a、b 两行之间还有几条（同一个会话里才算）"""
    if a["conv"] != b["conv"] or a["msg_idx"] is None or b["msg_idx"] is None or b["msg_idx"] - a["msg_idx"] <= 1:
        return 0
    return c.execute("SELECT COUNT(*) FROM l0_messages WHERE conv_id=? AND status='active' AND msg_idx > ? AND msg_idx < ?",
                     (a["conv"], a["msg_idx"], b["msg_idx"])).fetchone()[0]


def note(text):
    return f"［记忆系统附注｜{text}］"


def compose(c, conv_id, entries, link=None, now=None, settings=None):
    """窗口里每条消息前面要加的附注。

    entries：[{"r": "u"/"a", "h": 指纹}]，窗口里这一轮之前的消息（按顺序）。
    link：前端这一轮带的 {kind: branch/forged, conversation_id, carried?}（分支 / 搬家的第一轮还没登记时用）。
    返回 {"notes": [{"i", "text"}], "turn_note": 这一轮的附注里加的一段, "info": {...}, "needs": 要写小结的块}"""
    now = now or timeutil.utc_now_str()
    s = settings or cfg()
    out = {"notes": [], "turn_note": "", "info": {}, "needs": []}
    entries = [e for e in (entries or []) if isinstance(e, dict) and e.get("h")]
    if not (s["notes"] or s["bridge"] or s["prev_window"]):
        return out
    lin = register_lineage(c, conv_id, link, len(entries), now)
    kind = lin["kind"] if lin else None
    out["info"]["kind"] = kind
    kws = _light_kws(c)

    rows, complete = history_rows(c, conv_id, tail=max(OWN_TAIL_MIN, len(entries) * 2 + 40))
    via_parent = False
    if not any(r["own"] for r in rows) and lin and kind in ("branch", "forged") and lin["prev"] and not branches.get(c, conv_id):
        rows, complete = history_rows(c, lin["prev"])     # 分支 / 搬家的第一轮：还没登记，直接和原会话对
        for r in rows:
            r["own"] = False                              # 都是原会话的（不是这个会话自己的）
        via_parent = True

    if not entries:
        if kind == "new" and s["prev_window"] and lin and lin["prev"]:
            t = prev_text(c, lin["prev"], now, kws, s["bridge_max_chars"], now)
            if t:
                out["turn_note"] = f"[新窗口] 这是新开的窗口（{fmt_abs(now, now)} 开始）。" + t
        return out

    pairs = align(rows, entries, _aliases(c, rows))
    out["info"].update(entries=len(entries), matched=len(pairs))
    if not pairs:
        return out
    notes = {}

    def add(i, text):
        if text:
            notes.setdefault(i, []).append(text)

    first_i = min(pairs)
    pw = pairs[first_i]
    r0 = rows[pw]
    i0 = next((i for i, e in enumerate(entries) if e.get("r") == "u"), 0)
    out["info"].update(window_from=r0["ts"], first_row_id=r0["id"])

    # 1. 窗口开头那条：几点的；更早的不在窗口里了（+ 滑出窗口、还没整理成片段的那段的小结）；新窗口接上一窗
    sig = json.dumps([FORMAT_VERSION, s["notes"], s["bridge"], s["prev_window"], s["bridge_max_chars"], kind], ensure_ascii=False)
    st = c.execute("SELECT first_row_id, sig, note FROM window_state WHERE conv_id=?", (conv_id,)).fetchone()
    want_bridge = s["bridge"] and not (pw == 0 and complete) and not (kind == "forged" and not r0["own"])
    chs = span_chunks(c, rows, pw, now) if want_bridge else []
    out["needs"] = needs_from(c, chs) if chs else []
    if st and st[0] == r0["id"] and st[1] == sig:
        start = st[2] or ""
        out["info"]["frozen"] = True
    else:
        start, pending = "", 0
        if pw == 0 and complete and not via_parent:
            if kind == "new" and s["prev_window"] and lin and lin["prev"]:
                t = prev_text(c, lin["prev"], r0["ts"], kws, s["bridge_max_chars"], now)
                start = f"这是新开的窗口（{fmt_abs(r0['ts'], now)} 开始）。" + t if t else ""
            if not start and s["notes"]:
                start = f"这是这个对话的开头（{fmt_abs(r0['ts'], now)}）。"
        elif kind == "forged" and not r0["own"]:
            # 挑选式搬家：开头几段是挑过来的，挑剩下的不用补小结
            if s["notes"]:
                start = f"下面几段是从上一个窗口挑出来带过来的，最早的一条是 {fmt_abs(r0['ts'], now)} 的。"
        else:
            ref = timeutil.to_local(r0["ts"]).date() if r0["ts"] else None
            lines, pending = bridge_lines(c, chs, ref, kws, s["bridge_max_chars"]) if s["bridge"] else ([], 0)
            if s["notes"] or lines:
                head = "这个窗口是从上一个窗口分出来的，" if kind == "branch" and not r0["own"] else ""
                start = f"{head}窗口里最早的一条是 {fmt_abs(r0['ts'], now)} 的，更早的对话已经不在窗口里了。"
                if lines:
                    start += "更早、还没整理成每日片段的那部分，我在它们滑出窗口前记下了几句：\n" + "\n".join(lines)
                if pending >= 4:
                    start += f"\n（还有 {pending} 条也已经不在窗口里，还没来得及记下）"
        start = note(start) if start else ""
        c.execute("INSERT INTO window_state (conv_id, first_row_id, sig, note, pending, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(conv_id) DO UPDATE SET first_row_id=excluded.first_row_id, sig=excluded.sig, note=excluded.note, "
                  "pending=excluded.pending, updated_at=excluded.updated_at", (conv_id, r0["id"], sig, start, pending, now))
    add(i0, start)

    # 2. 隔了很久、中间省略了的地方；3. 搬过来 / 分出来的和新说的之间
    if s["notes"]:
        for i in range(1, len(entries)):
            if entries[i].get("r") != "u" or i not in pairs or (i - 1) not in pairs:
                continue
            a, b = rows[pairs[i - 1]], rows[pairs[i]]
            hours = _hours(a["ts"], b["ts"])
            skipped = _skipped(c, a, b)
            boundary = (not a["own"]) and b["own"]
            if boundary:
                add(i, f"从这里开始是 {fmt_abs(b['ts'], now)} 新开的窗口，前面几段是从上一个窗口带过来的。")
            if hours >= s["gap_hours"] or skipped:
                text = f"和上一句隔了 {dur_text(a['ts'], b['ts'])}" if hours >= s["gap_hours"] else "和上一句之间"
                if skipped:
                    text += f"，中间省略了 {skipped} 条" if hours >= s["gap_hours"] else f"省略了 {skipped} 条"
                text += f"（{fmt_abs(a['ts'], now)} → {fmt_abs(b['ts'], now)}）"
                if hours >= 24 or skipped >= 8:
                    titles = episode_titles(c, a["ts"], b["ts"], kws)
                    if titles:
                        text += "；那段时间的片段：" + "".join(titles)
                add(i, text + "。")
    if via_parent:
        out["turn_note"] = "[新窗口] 从这一句开始是新开的窗口，前面几段是从上一个窗口带过来的。"
    for i in sorted(notes):
        texts = [t if t.startswith("［") else note(t) for t in notes[i]]
        out["notes"].append({"i": i, "text": "\n".join(texts)})
    out["info"]["notes"] = len(out["notes"])
    return out


# ---------- 后台写小结要用的 ----------

def pending_for(c, conv_id, now=None):
    """这个会话现在要写小结的块（按窗口状态里记下的窗口第一条）。新窗口的上一个窗口最后那段也算上。"""
    st = c.execute("SELECT first_row_id FROM window_state WHERE conv_id=?", (conv_id,)).fetchone()
    if not st or not st[0]:
        return []
    rows, _ = history_rows(c, conv_id, tail=OWN_TAIL_MIN)
    pw = next((j for j, r in enumerate(rows) if r["id"] == st[0]), None)
    if pw is None:
        return []
    return needs_from(c, span_chunks(c, rows, pw, now))


def recently_dead(c, conv_id, hours=6):
    r = c.execute("SELECT status, updated_at FROM pipeline_jobs WHERE dedupe_key IN (?, ?) ORDER BY id DESC LIMIT 1",
                  (f"window_bridge:{conv_id}", f"window_bridge:{conv_id}~2")).fetchone()
    if not r or r[0] != "dead":
        return False
    t = timeutil.parse_ts(r[1])
    return bool(t and (timeutil.utc_now() - t).total_seconds() < hours * 3600)


def enqueue_bridge(conv_id, source="auto", continuation=False):
    """投递「写小结」任务（同一个会话排着队的不重复投；自动投递时，最近失败到停止重试的先停 6 小时）"""
    import jobs
    from app.config import SQLITE_PATH, get_conn
    if source == "auto" and not continuation:
        conn = get_conn()
        try:
            if recently_dead(conn, conv_id):
                return False
        finally:
            conn.close()
    return jobs.enqueue(SQLITE_PATH, "window_bridge", payload={"conv_id": conv_id}, dedupe_key=f"window_bridge:{conv_id}",
                        source=source, delay_sec=3 if continuation else 0, continuation=continuation)[1]


def forget_conv(c, conv_id):
    """删掉一个会话时：它的窗口状态、来处、用到它的消息写的小结一起删掉"""
    c.execute("DELETE FROM window_state WHERE conv_id=?", (conv_id,))
    c.execute("DELETE FROM conv_windows WHERE conv_id=?", (conv_id,))
    c.execute("DELETE FROM window_bridges WHERE conv_id=? OR instr(',' || IFNULL(src_convs, '') || ',', ',' || ? || ',') > 0",
              (conv_id, conv_id))


# ---------- Dashboard ----------

def overview(c, conv_id=None, now=None):
    """「换窗衔接」页：最近聊过的几个会话、它们的来处和窗口开头那行附注；选中的会话的小结。"""
    convs = [{"conv_id": r[0], "last_ts": r[1], "messages": r[2]} for r in c.execute(
        "SELECT conv_id, MAX(ts), COUNT(*) FROM l0_messages WHERE status='active' GROUP BY conv_id ORDER BY MAX(ts) DESC LIMIT 12")]
    for x in convs:
        lin = lineage(c, x["conv_id"])
        b = branches.get(c, x["conv_id"])
        x["kind"] = (b["kind"] if b else None) or (lin["kind"] if lin else None)
        x["from"] = (b["parent"] if b else None) or (lin["prev"] if lin else None)
        x["carried"] = len(b["prefix"]) if b else 0
    conv_id = conv_id or (convs[0]["conv_id"] if convs else None)
    out = {"convs": convs, "conv_id": conv_id, "note": "", "state": None, "bridges": [], "pending": []}
    if not conv_id:
        return out
    st = c.execute("SELECT first_row_id, note, pending, updated_at FROM window_state WHERE conv_id=?", (conv_id,)).fetchone()
    if st:
        r = c.execute("SELECT ts FROM l0_messages WHERE id=?", (st[0],)).fetchone()
        out["state"] = {"first_row_id": st[0], "window_from": r[0] if r else None, "pending": st[2], "updated_at": st[3]}
        out["note"] = st[1] or ""
    now_t = timeutil.parse_ts(now) if now else timeutil.utc_now()
    lo = (now_t - timedelta(hours=BRIDGE_SPAN_H + 24)).strftime(timeutil.STORE_FMT)
    out["bridges"] = [{"id": r[0], "start_ts": r[1], "end_ts": r[2], "n": r[3], "gist": r[4], "mood": r[5], "status": r[6],
                       "issues": json.loads(r[7] or "[]"), "model": r[8], "created_at": r[9]} for r in c.execute(
        "SELECT id, start_ts, end_ts, n, gist, mood, status, issues, model, created_at FROM window_bridges "
        "WHERE (conv_id=? OR instr(',' || IFNULL(src_convs, '') || ',', ',' || ? || ',') > 0) AND datetime(end_ts) >= datetime(?) "
        "ORDER BY start_ts DESC LIMIT 60", (conv_id, conv_id, lo))]
    out["pending"] = [{"start_ts": ch["rows"][0]["ts"], "end_ts": ch["rows"][-1]["ts"], "n": len(ch["rows"])}
                      for ch in pending_for(c, conv_id, now)]
    return out
