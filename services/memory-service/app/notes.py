"""小本子：模型自己随手记下的小事（迁移 020）。

想法来自 Kelivo 旧版记忆的体验：模型一次次零碎地记下来，长线叙事不太好，但会留下一些细碎的细节和深刻的小瞬间——
每日片段、摘要抓的是「那天发生了什么」，常常漏掉这些。这里和别的层分开：
- 「对彼此的认识和理解」（画像，update_profile）：稳定的、会长期成立的认识；
- feel：我自己的感受；
- 小本子（jot）：一件件具体的小事，第一人称一两句，日期自动记上。

放进聊天时照顾缓存（system 一变，后面整段都要重新写）：
- 今天之前记的（最近几天 + 钉住的）放在 system 的日更块里，只在每天第一次聊天时变一次；
- 今天新记的放在窗口开头那行附注里（app/windows.py），那一行按窗口的第一条定格，窗口下次往后挪时才带上新的——
  那一轮前缀本来就变了，不多花缓存（和滑出窗口那段的小结一样）。
「今天」按每日片段的算法：用户时区，凌晨 4 点前算前一天。
"""
import difflib
import re
from datetime import timedelta

import timeutil
from app import settings as app_settings
from app.pipeline import episodes as EP

TEXT_MAX = 200             # 一条最多多少字
DUP_DAYS = 3               # 这么多天里记过差不多的话，不再记一遍
DUP_RATIO = 0.82
FRESH_MAX = 8              # 窗口开头那行最多带几条今天新记的
_WS = re.compile(r"\s+")
COLS = ["id", "text", "source", "status", "pinned", "conv_id", "anchor_l0_id", "created_at", "updated_at", "edited_by"]


def cfg():
    return app_settings.get("notebook")


def clean(text):
    t = _WS.sub(" ", str(text or "")).strip()
    return t[:TEXT_MAX]


def _row(r):
    return dict(zip(COLS, r)) if r else None


def get(c, note_id):
    return _row(c.execute("SELECT " + ",".join(COLS) + " FROM notes WHERE id=?", (note_id,)).fetchone())


def day_start(now=None):
    """「今天」从什么时候开始（UTC 存储格式）：用户时区凌晨 4 点"""
    now = now or timeutil.utc_now_str()
    return EP.day_window_utc(EP.day_of(now))[0]


def _norm(t):
    return re.sub(r"[\s，。！？、,.!?~～…·：:；;「」『』“”\"'（）()]", "", t or "")


def similar_recent(c, text, now=None):
    now = now or timeutil.utc_now_str()
    since = (timeutil.parse_ts(now) - timedelta(days=DUP_DAYS)).strftime(timeutil.STORE_FMT)
    a = _norm(text)
    for r in c.execute("SELECT " + ",".join(COLS) + " FROM notes WHERE status='active' AND created_at >= ? ORDER BY id DESC",
                       (since,)).fetchall():
        b = _norm(r[1])
        if a and b and (a == b or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= DUP_RATIO):
            return _row(r)
    return None


def act(c, action, note_id=None, text=None, conv_id=None, by="ai", now=None):
    """write / edit / erase / restore / pin / unpin。返回 {"status": ok|dup|error, "note", "message"}（message 是给模型 / 人看的一句话）"""
    now = now or timeutil.utc_now_str()
    by = "user" if by == "user" else "ai"
    action = (action or "write").strip().lower()
    if action == "write":
        t = clean(text)
        if not t:
            return {"status": "error", "message": "没有要记的内容"}
        dup = similar_recent(c, t, now)
        if dup:
            return {"status": "dup", "note": dup, "message": f"这件事前几天已经记过了（#{dup['id']}）：「{dup['text']}」，没有再记一遍"}
        anchor = None
        if conv_id:
            r = c.execute("SELECT MAX(id) FROM l0_messages WHERE conv_id=? AND status='active'", (conv_id,)).fetchone()
            anchor = r[0] if r else None
        cur = c.execute("INSERT INTO notes (text, source, status, pinned, conv_id, anchor_l0_id, created_at, updated_at, edited_by) "
                        "VALUES (?, ?, 'active', 0, ?, ?, ?, ?, ?)", (t, by, conv_id or None, anchor, now, now, by))
        n = get(c, cur.lastrowid)
        return {"status": "ok", "note": n, "message": f"记下了（#{n['id']}）：「{t}」"}
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "要改哪一条？请带上编号（id）"}
    n = get(c, note_id)
    if not n:
        return {"status": "error", "message": f"小本子上没有 #{note_id} 这一条"}
    if action == "edit":
        t = clean(text)
        if not t:
            return {"status": "error", "message": "改成什么？内容不能是空的（要删掉请用 erase）"}
        c.execute("UPDATE notes SET text=?, updated_at=?, edited_by=?, status='active' WHERE id=?", (t, now, by, note_id))
        return {"status": "ok", "note": get(c, note_id), "message": f"改好了（#{note_id}）：「{t}」"}
    if action in ("erase", "restore"):
        st = "erased" if action == "erase" else "active"
        c.execute("UPDATE notes SET status=?, updated_at=?, edited_by=? WHERE id=?", (st, now, by, note_id))
        return {"status": "ok", "note": get(c, note_id),
                "message": (f"擦掉了（#{note_id}）" if st == "erased" else f"恢复了（#{note_id}）") + f"：「{n['text']}」"}
    if action in ("pin", "unpin"):
        c.execute("UPDATE notes SET pinned=?, updated_at=? WHERE id=?", (1 if action == "pin" else 0, now, note_id))
        return {"status": "ok", "note": get(c, note_id), "message": ("钉住了" if action == "pin" else "取消钉住") + f"（#{note_id}）"}
    return {"status": "error", "message": f"不认识的操作：{action}（可以是 write / edit / erase）"}


def _label(ts):
    d = timeutil.to_local(ts)
    return f"{d.month} 月 {d.day} 日（周{'一二三四五六日'[d.weekday()]}）" if d else ""


def system_text(c, now=None, s=None):
    """system 日更块里的「小本子」：今天之前最近几天记的 + 钉住的（今天新记的不放这里，见模块说明）。只按天变。"""
    s = s or cfg()
    if not s["enabled"]:
        return ""
    start = day_start(now)
    since = (timeutil.parse_ts(start) - timedelta(days=s["days"])).strftime(timeutil.STORE_FMT)
    rows = c.execute("SELECT " + ",".join(COLS) + " FROM notes WHERE status='active' AND created_at < ? "
                     "AND (created_at >= ? OR pinned=1) ORDER BY created_at DESC", (start, since)).fetchall()
    notes = [_row(r) for r in rows]
    pinned = [n for n in notes if n["pinned"]]
    recent = [n for n in notes if not n["pinned"]][: max(0, s["max_items"] - len(pinned))]
    keep, size = [], 0
    for n in pinned + recent:            # 钉住的先占位；超长时去掉最早的
        line = f"· {_label(n['created_at'])}：{n['text']}（#{n['id']}{'，钉住' if n['pinned'] else ''}）"
        if size + len(line) > s["max_chars"] and keep:
            break
        keep.append((n["created_at"], line))
        size += len(line) + 1
    if not keep:
        return ""
    keep.sort()
    return ("[小本子]（我自己随手记下的小事：细碎的细节、有意思的小瞬间。用得上时自然地想起来就好，不用复述；"
            "记错了或过时了可以用 jot 的 edit / erase 改，编号在括号里）\n" + "\n".join(x[1] for x in keep))


def fresh_lines(c, now=None, s=None):
    """今天新记的（还没进 system 的），给窗口开头那行附注用。时间写绝对的（附注会定格一阵子，不能写「今天」）"""
    s = s or cfg()
    if not s["enabled"]:
        return []
    rows = c.execute("SELECT " + ",".join(COLS) + " FROM notes WHERE status='active' AND created_at >= ? ORDER BY created_at",
                     (day_start(now),)).fetchall()
    out = []
    for r in rows[-FRESH_MAX:]:
        n = _row(r)
        d = timeutil.to_local(n["created_at"])
        out.append(f"· {d.month} 月 {d.day} 日 {d:%H:%M}：{n['text']}（#{n['id']}）")
    return out


def overview(c, status="active", limit=200):
    if status not in ("active", "erased", "all"):
        status = "active"
    q = "SELECT " + ",".join(COLS) + " FROM notes" + ("" if status == "all" else " WHERE status=?") + " ORDER BY pinned DESC, created_at DESC LIMIT ?"
    rows = c.execute(q, ((limit,) if status == "all" else (status, limit))).fetchall()
    counts = dict(c.execute("SELECT status, COUNT(*) FROM notes GROUP BY status").fetchall())
    return {"notes": [_row(r) for r in rows], "counts": {"active": counts.get("active", 0), "erased": counts.get("erased", 0)}}
