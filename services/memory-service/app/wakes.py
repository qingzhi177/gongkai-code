"""自动醒来（迁移 021）：对方不在的时候，ta 偶尔醒来一下。

两种醒法：
- 随机醒来：对方停下一段时间（idle_minutes）以后，每隔 check_minutes 掷一次骰子，白天 / 夜里的概率分开设；
- ta 自己约：平时聊天时 ta 用 schedule_wake 约一个时间（「你考完试那会儿我来问问」），到点醒。

醒来走的是平时聊天那条路（网关 services/gateway/wake.js）：上一轮聊天的 system、历史、工具原样不动，后面接上 ta 上一轮的回复
和一段「醒来」的说明（只给 ta 看，对方看不到、不存进聊天记录），缓存前缀和平时一样，醒一次大约多花一轮的钱。
ta 自己决定说不说：说了，就是一条普通的 ta 的消息（前端来取，取走以后接在醒来时最新那条后面存进 L0）；
不说（只写「［不说］」），只记在醒来记录里。下次对方回来聊天时，附注里告诉 ta「你不在的时候我醒来过几次」，历史不缺席。

这里管：要不要醒（check）、醒来说明怎么写（wake_note）、结果（finish）、前端取消息（inbox / ack）、
下次聊天时的附注（turn_note / seen）、ta 约的时间（plan_act）、Dashboard（overview）。
"""
import json
import random
import re
from datetime import datetime, timedelta, timezone

import timeutil
from app import cache_stats
from app import settings as app_settings

TEXT_MAX = 8000             # 说的话最多存这么多字
THINK_MAX = 4000
REASON_MAX = 200
PLAN_MIN_MINUTES = 10       # 最早约到这么多分钟以后
PLAN_MAX_DAYS = 7           # 最晚约到这么多天以后
PLAN_PENDING_MAX = 3        # ta 最多同时约着几个
PLAN_LATE_HOURS = 6         # 到点时网关没在跑，过了这么久还没醒就算了
RUNNING_STALE_MIN = 15      # 一次醒来最多算这么久：网关中途重启丢了的，过了这么久记成出错
TEST_FRESH_MIN = 10         # Dashboard 点了「现在醒一次」，这么久之内有效
MENTION_DAYS = 3            # 下次聊天时告诉 ta 最近这么多天里的
MENTION_MAX = 6
EXCERPT = 40
WEEKDAYS = "一二三四五六日"
SILENT_RE = re.compile(r"[［\[【（(]\s*不说\s*[］\]】）)]")

# 网关的工具 → 醒来时能不能用（看哪个设置；None = 一直能用）
TOOL_ALLOW = {"recall": "allow_recall", "feel": "allow_feel", "jot": "allow_jot", "update_profile": "allow_profile",
              "bocha_search": "allow_web", "get_current_time": None, "schedule_wake": "plans"}
TOOL_WORDS = {"recall": "翻翻记忆（recall）", "feel": "记下感受（feel）", "jot": "在小本子上记一笔（jot）",
              "update_profile": "更新对彼此的认识（update_profile）", "bocha_search": "上网查一查（bocha_search）",
              "get_current_time": "看时间（get_current_time）", "schedule_wake": "约下次什么时候醒（schedule_wake）"}
TOOL_DONE = {"recall": "翻了翻记忆", "feel": "记了感受", "jot": "在小本子上记了一笔", "update_profile": "更新了对彼此的认识",
             "bocha_search": "上网查了查", "schedule_wake": "约了下次醒来"}
RUN_COLS = ["id", "conv_id", "kind", "plan_id", "started_at", "finished_at", "outcome", "text", "aside", "thinking", "tools",
            "usage", "note", "last_real_at", "anchor_l0_id", "delivery", "delivered_at", "l0_id", "push", "mentioned"]
PLAN_COLS = ["id", "conv_id", "due_at", "reason", "source", "status", "note", "run_id", "mentioned", "created_at", "updated_at"]


def cfg():
    return app_settings.get("wake")


# ---------- 时间 ----------

def _dt(s):
    return timeutil.parse_ts(s)


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime(timeutil.STORE_FMT)


def _minutes(a, b):
    """a → b 多少分钟"""
    return (_dt(b) - _dt(a)).total_seconds() / 60.0


def _add(ts, minutes):
    return _fmt(_dt(ts) + timedelta(minutes=minutes))


def fmt_when(ts, now):
    """给 ta 看的时间：今天的只写几点，昨天的写「昨天」，更早的写日期"""
    lt, ln = timeutil.to_local(ts), timeutil.to_local(now)
    if not lt or not ln:
        return ""
    if lt.date() == ln.date():
        return lt.strftime("%H:%M")
    if lt.date() == ln.date() - timedelta(days=1):
        return "昨天 " + lt.strftime("%H:%M")
    if lt.date() == ln.date() + timedelta(days=1):
        return "明天 " + lt.strftime("%H:%M")
    return f"{lt.month} 月 {lt.day} 日 {lt.strftime('%H:%M')}"


def dur_text(minutes):
    m = int(round(max(0, minutes)))
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h} 小时" + (f" {mm} 分钟" if mm and h < 6 else "")
    d, hh = divmod(h, 24)
    return f"{d} 天" + (f" {hh} 小时" if hh else "")


def part_of(now, s):
    """现在是白天还是夜里，这一段从什么时候开始（UTC 存储格式）。夜里从 night_start 点到 night_end 点（可以跨过午夜）"""
    ln = timeutil.to_local(now)
    ns, ne = int(s["night_start"]), int(s["night_end"])
    if ns == ne:   # 不分白天夜里：按天算，从 0 点开始
        start = ln.replace(hour=0, minute=0, second=0, microsecond=0)
        return "day", _fmt(start)
    cands = []
    for back in (0, 1):
        d = ln.date() - timedelta(days=back)
        for h, kind in ((ns, "night"), (ne, "day")):
            t = datetime(d.year, d.month, d.day, h, tzinfo=ln.tzinfo)
            if t <= ln:
                cands.append((t, kind))
    t, kind = max(cands, key=lambda x: x[0])
    return kind, _fmt(t)


def parse_at(text, now):
    """ta 写的时间（对方的时区）→ UTC 存储格式；看不懂返回 None。
    能写：2026-11-01 08:30、11-01 08:30、11月1日 8:30、08:30（今天还没到就是今天，过了就是明天）、带时区的 ISO 时间。"""
    s = str(text or "").strip()
    if not s:
        return None
    ln = timeutil.to_local(now)
    tz = ln.tzinfo
    m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?[ T]*(\d{1,2})[:：点](\d{1,2})(?:[:：](\d{1,2}))?分?", s)
    if m:
        y, mo, d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
        try:
            return _fmt(datetime(y, mo, d, h, mi, sec, tzinfo=tz))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[-/.月](\d{1,2})日?[ T]*(\d{1,2})[:：点](\d{1,2})分?", s)
    if m:
        mo, d, h, mi = (int(x) for x in m.groups())
        try:
            t = datetime(ln.year, mo, d, h, mi, tzinfo=tz)
        except ValueError:
            return None
        if t < ln - timedelta(days=1):
            t = t.replace(year=ln.year + 1)
        return _fmt(t)
    m = re.fullmatch(r"(今天|明天|后天)?\s*(\d{1,2})[:：点](\d{1,2})?分?", s)
    if m:
        word, h, mi = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if h > 23 or mi > 59:
            return None
        t = ln.replace(hour=h, minute=mi, second=0, microsecond=0)
        if word == "明天":
            t += timedelta(days=1)
        elif word == "后天":
            t += timedelta(days=2)
        elif not word and t <= ln:
            t += timedelta(days=1)
        return _fmt(t)
    if "T" in s or "+" in s or s.endswith("Z"):
        dt = timeutil.parse_ts(s, naive_tz=tz)
        return _fmt(dt) if dt else None
    return None


# ---------- 小状态（掷骰子的时间、网关最后一次来问的情况、前端登记的推送地址） ----------

def state_get(c, k, default=None):
    r = c.execute("SELECT v FROM wake_state WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except Exception:
        return default


def state_set(c, k, v, now=None):
    c.execute("INSERT INTO wake_state (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
              "updated_at=excluded.updated_at", (k, json.dumps(v, ensure_ascii=False), now or timeutil.utc_now_str()))


# ---------- 读 ----------

def _run(r):
    if not r:
        return None
    d = dict(zip(RUN_COLS, r))
    for k in ("tools", "usage", "push"):
        try:
            d[k] = json.loads(d[k]) if d[k] else ([] if k == "tools" else None)
        except Exception:
            d[k] = [] if k == "tools" else None
    return d


def _plan(r):
    return dict(zip(PLAN_COLS, r)) if r else None


def get_run(c, run_id):
    return _run(c.execute(f"SELECT {','.join(RUN_COLS)} FROM wake_runs WHERE id=?", (run_id,)).fetchone())


def get_plan(c, plan_id):
    return _plan(c.execute(f"SELECT {','.join(PLAN_COLS)} FROM wake_plans WHERE id=?", (plan_id,)).fetchone())


def pending_plans(c):
    return [_plan(r) for r in c.execute(f"SELECT {','.join(PLAN_COLS)} FROM wake_plans WHERE status='pending' ORDER BY due_at, id")]


def last_l0_id(c, conv_id):
    r = c.execute("SELECT id FROM l0_messages WHERE conv_id=? AND status='active' ORDER BY msg_idx DESC, id DESC LIMIT 1",
                  (conv_id,)).fetchone()
    return r[0] if r else None


def runs_since(c, since, exclude=None):
    """这段时间（对方最后一次聊天以后）里醒来、说了或没说的那几次"""
    if not since:
        return []
    rows = c.execute(f"SELECT {','.join(RUN_COLS)} FROM wake_runs WHERE started_at > ? AND outcome IN ('spoke','silent') "
                     "ORDER BY id", (since,)).fetchall()
    return [x for x in (_run(r) for r in rows) if x["id"] != exclude]


def unanswered(c, last_real_at):
    if not last_real_at:
        return 0
    return c.execute("SELECT COUNT(*) FROM wake_runs WHERE started_at > ? AND outcome='spoke'", (last_real_at,)).fetchone()[0]


def used_in_part(c, start):
    """这一段（白天 / 夜里）醒过几次：随机的和约好的都算，Dashboard 试的不算"""
    return c.execute("SELECT COUNT(*) FROM wake_runs WHERE started_at >= ? AND kind != 'test'", (start,)).fetchone()[0]


def _excerpt(text, n=EXCERPT):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[:n] + "…"


def _tools_done(run):
    words = []
    for t in run.get("tools") or []:
        w = TOOL_DONE.get(t)
        if w and w not in words:
            words.append(w)
    return "、".join(words)


# ---------- 要不要醒 ----------

def _expire_running(c, now):
    c.execute("UPDATE wake_runs SET outcome='error', finished_at=?, note=? WHERE outcome='running' AND started_at < ?",
              (now, "没有收到结果（网关可能中途重启了）", _add(now, -RUNNING_STALE_MIN)))


def start_run(c, conv_id, kind, plan_id, last_real_at, now):
    cur = c.execute("INSERT INTO wake_runs (conv_id, kind, plan_id, started_at, outcome, last_real_at, anchor_l0_id) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?)", (conv_id, kind, plan_id, now, last_real_at, last_l0_id(c, conv_id)))
    return cur.lastrowid


def _skip_plan(c, p, why, now):
    c.execute("UPDATE wake_plans SET status='skipped', note=?, updated_at=? WHERE id=?", (why, now, p["id"]))


def decide(c, conv_id, last_real_at, tools=None, has_context=True, now=None, s=None, rng=random.random, dry=False):
    """要不要现在醒。dry=True 只看不动（Dashboard 用）。
    返回 {"wake": bool, "kind", "run_id", "plan_id", "note"（给 ta 的醒来说明）, "reason"（没醒的原因）, "next_at"（大概下次什么时候再看）, ...}"""
    now = now or timeutil.utc_now_str()
    s = s or cfg()
    part, part_start = part_of(now, s)
    used = used_in_part(c, part_start)
    cap = int(s["day_max"] if part == "day" else s["night_max"])
    out = {"wake": False, "reason": "", "next_at": None, "part": part, "used": used, "cap": cap}
    if not s["enabled"]:
        out["reason"] = "自动醒来关着"
        return out
    if not dry:
        _expire_running(c, now)
    if c.execute("SELECT 1 FROM wake_runs WHERE outcome='running' LIMIT 1").fetchone():
        out["reason"] = "正醒着"
        return out
    ready = bool(has_context and conv_id)

    # 0. Dashboard 上点了「现在醒一次」
    test = state_get(c, "test_requested")
    if test and _minutes(test, now) <= TEST_FRESH_MIN:
        if not ready:
            out["reason"] = "想试一次，但网关这边还没有能接着说的对话（重启以后要先聊一句）"
            return out
        if dry:
            out["reason"] = "马上就试一次"
            return out
        state_set(c, "test_requested", None, now)
        rid = start_run(c, conv_id, "test", None, last_real_at, now)
        return {**out, "wake": True, "kind": "test", "run_id": rid,
                "note": wake_note(c, conv_id, "test", None, last_real_at, tools, now, s, rid)}

    # 1. ta 约好的时间
    for p in [x for x in pending_plans(c) if x["due_at"] <= now]:
        why = None
        if not s["plans"]:
            why = "「ta 自己约时间」关着"
        elif _minutes(p["due_at"], now) > PLAN_LATE_HOURS * 60:
            why = "到点时网关没在跑，过了太久"
        elif not ready:
            why = "网关这边还没有能接着说的对话（重启以后还没聊过天）"
        elif last_real_at and _minutes(last_real_at, now) < s["plan_quiet_minutes"]:
            why = "到点时你们正在聊天"
        elif used >= cap:
            why = f"{'白天' if part == 'day' else '夜里'}醒来的次数到上限了（{cap} 次）"
        if dry:
            out["reason"] = f"约好的 #{p['id']} 到点了" + (f"，但不会醒：{why}" if why else "，马上醒")
            return out
        if why:
            _skip_plan(c, p, why, now)
            continue
        rid = start_run(c, conv_id, "plan", p["id"], last_real_at, now)
        c.execute("UPDATE wake_plans SET status='done', run_id=?, updated_at=? WHERE id=?", (rid, now, p["id"]))
        return {**out, "wake": True, "kind": "plan", "run_id": rid, "plan_id": p["id"],
                "note": wake_note(c, conv_id, "plan", p, last_real_at, tools, now, s, rid)}

    # 2. 随机醒来
    nxt = next((x["due_at"] for x in pending_plans(c)), None)
    out["next_plan_at"] = nxt
    if not s["random"]:
        out["reason"] = "随机醒来关着"
        return out
    if not ready or not last_real_at:
        out["reason"] = "还没有能接着说的对话（网关重启以后要先聊一句）"
        return out
    idle_from = _add(last_real_at, s["idle_minutes"])
    if now < idle_from:
        out.update(reason=f"你停下还不到 {s['idle_minutes']} 分钟", next_at=idle_from)
        return out
    prob = float(s["day_prob"] if part == "day" else s["night_prob"])
    if prob <= 0:
        out["reason"] = "夜里不随机醒" if part == "night" else "白天掷中的概率是 0"
        return out
    if used >= cap:
        out["reason"] = f"{'白天' if part == 'day' else '夜里'}醒来的次数到上限了（{cap} 次）"
        return out
    last = c.execute("SELECT MAX(started_at) FROM wake_runs WHERE kind != 'test' AND outcome NOT IN ('error')").fetchone()[0]
    if last and _minutes(last, now) < s["min_gap_minutes"]:
        out.update(reason=f"离上一次醒来不到 {s['min_gap_minutes']} 分钟", next_at=_add(last, s["min_gap_minutes"]))
        return out
    ua = unanswered(c, last_real_at)
    if ua >= s["unanswered_stop"]:
        out["reason"] = f"连着说了 {ua} 句你都还没回，先不随机醒（你回来以后重新算）"
        return out
    roll = state_get(c, "last_roll") or {}
    last_roll = roll.get("at")
    next_roll = _add(last_roll, s["check_minutes"]) if (last_roll and last_roll >= idle_from) else idle_from
    if now < next_roll:
        out.update(reason="等下一次掷骰子", next_at=next_roll)
        return out
    if dry:
        out.update(reason=f"马上掷骰子（{int(round(prob * 100))}%）", next_at=now)
        return out
    x = rng()
    hit = x < prob
    state_set(c, "last_roll", {"at": now, "p": prob, "x": round(x, 4), "hit": hit, "part": part}, now)
    if not hit:
        out.update(reason=f"掷了骰子，没掷中（{int(round(prob * 100))}%）", next_at=_add(now, s["check_minutes"]))
        return out
    rid = start_run(c, conv_id, "random", None, last_real_at, now)
    return {**out, "wake": True, "kind": "random", "run_id": rid,
            "note": wake_note(c, conv_id, "random", None, last_real_at, tools, now, s, rid)}


def allowed_tools(tools, s):
    """这次醒来能用的工具（网关上一轮给的工具里、设置里没关的）。前端的工具（要在对方手机上执行的）醒来时用不了，不在这里。"""
    out = []
    for t in tools or []:
        if t not in TOOL_ALLOW:
            continue
        key = TOOL_ALLOW[t]
        if key is None or s.get(key):
            out.append(t)
    return out


def wake_note(c, conv_id, kind, plan, last_real_at, tools, now, s, run_id=None):
    """醒来时接在上一轮后面的那段说明（只给 ta 看，不存进聊天记录）"""
    ln = timeutil.to_local(now)
    lines = [f"现在是 {ln.month} 月 {ln.day} 日（周{WEEKDAYS[ln.weekday()]}）{ln.strftime('%H:%M')}（{timeutil.USER_TZ_LABEL}）。"
             + (f"对方上一次来聊天是 {fmt_when(last_real_at, now)}，到现在 {dur_text(_minutes(last_real_at, now))}了。" if last_real_at else "")]
    if kind == "plan":
        reason = (plan or {}).get("reason")
        lines.append(f"这次是你之前约好的时间（约的时候写的是：「{reason}」）。" if reason else "这次是你之前约好的时间。")
    elif kind == "random":
        lines.append("这次是随机醒来的。")
    else:
        lines.append("这次是对方在 Dashboard 上点了「现在醒一次」，想试试醒来是什么样子。")
    earlier = runs_since(c, last_real_at, exclude=run_id)
    if earlier:
        parts = []
        for r in earlier[-MENTION_MAX:]:
            w = fmt_when(r["started_at"], now)
            if r["outcome"] == "spoke":
                parts.append(f"{w} 说了「{_excerpt(r['text'])}」（对方还没回）")
            else:
                done = _tools_done(r)
                parts.append(f"{w} 没说话" + (f"（{done}）" if done else ""))
        lines.append(f"对方不在的这段时间，你还醒来过 {len(earlier)} 次：" + "；".join(parts) + "。")
    later = [p for p in pending_plans(c) if not plan or p["id"] != plan.get("id")]
    if later:
        lines.append("你约好的下一次：" + "；".join(f"{fmt_when(p['due_at'], now)}（#{p['id']}" + (f"，「{_excerpt(p['reason'], 30)}」" if p["reason"] else "") + "）" for p in later[:3]) + "。")
    usable = allowed_tools(tools, s)
    if usable:
        lines.append("你可以先想一想，也可以用这些工具：" + "、".join(TOOL_WORDS[t] for t in usable) + "。")
    if s["speak"]:
        lines.append("想对对方说话，就直接写你要说的话：它会作为你的一条消息发给对方（对方手机上会收到）。"
                     "不想说，就只写「［不说］」，这次不会给对方发消息。")
        lines.append("不是一定要说话。对方知道你会这样醒来（是对方打开的），不用解释是怎么醒的；想说什么就说什么，像平常一样。")
    else:
        lines.append("这次只是自己待一会儿：对方把「醒来可以发消息」关了，你写的话不会发给对方。想做什么就用工具，最后只写「［不说］」。")
    lines.append("醒来记录里会留一行（几点醒的、说没说话、用了哪些工具），对方能看到；写在「［不说］」旁边的话和你想的过程也会记在那里。")
    return "［记忆系统｜醒来：这一段是系统写给你的，不是对方发来的消息，对方看不到］\n" + "\n".join(lines)


# ---------- 醒来以后 ----------

def split_reply(text, speak=True):
    """ta 醒来写的东西 → (说不说, 要发出去的话, 自己留着的话)"""
    t = (text or "").strip()
    if not t:
        return False, "", ""
    if SILENT_RE.search(t):
        return False, "", SILENT_RE.sub("", t).strip()
    if not speak:
        return False, "", t
    return True, t, ""


def finish(c, run_id, outcome, text=None, aside=None, thinking=None, tools=None, usage=None, note=None, now=None):
    """记下这次醒来的结果。说了话的进「待取」（前端来取）。返回这一次的记录"""
    now = now or timeutil.utc_now_str()
    run = get_run(c, run_id)
    if not run:
        return None
    if outcome not in ("spoke", "silent", "error", "interrupted"):
        outcome = "error"
    if outcome == "spoke" and not (text or "").strip():
        outcome = "silent"
    c.execute("UPDATE wake_runs SET outcome=?, finished_at=?, text=?, aside=?, thinking=?, tools=?, usage=?, note=?, delivery=? "
              "WHERE id=?",
              (outcome, now, (text or "")[:TEXT_MAX] or None, (aside or "")[:TEXT_MAX] or None,
               (thinking or "")[:THINK_MAX] or None, json.dumps(list(tools or []), ensure_ascii=False),
               json.dumps(usage or {}, ensure_ascii=False), (note or "")[:500] or None,
               "pending" if outcome == "spoke" else None, run_id))
    return get_run(c, run_id)


def push_targets(c, s=None):
    """说了话以后往哪儿推送：Dashboard 填的 ntfy 地址 + 前端登记的地址"""
    s = s or cfg()
    out = []
    if s.get("push_url"):
        out.append({"url": s["push_url"], "format": "ntfy"})
    for e in state_get(c, "push_endpoints", []) or []:
        if e.get("url") and e["url"] not in [x["url"] for x in out]:
            out.append({"url": e["url"], "format": e.get("format") or "json"})
    return out


def register_push(c, url, fmt="json", remove=False, now=None):
    url = str(url or "").strip()
    if not re.match(r"^https://[^\s/]+(/\S*)?$", url) or len(url) > 500:
        return {"status": "error", "message": "推送地址要是 https:// 开头的"}
    eps = [e for e in (state_get(c, "push_endpoints", []) or []) if e.get("url") != url]
    if not remove:
        eps.append({"url": url, "format": fmt if fmt in ("json", "ntfy") else "json", "at": now or timeutil.utc_now_str()})
    state_set(c, "push_endpoints", eps[-5:], now)
    return {"status": "ok", "endpoints": len(eps[-5:])}


def note_push(c, run_id, result):
    c.execute("UPDATE wake_runs SET push=? WHERE id=?", (json.dumps(result, ensure_ascii=False), run_id))


KIND_LABEL = {"random": "随机醒来", "plan": "ta 约的", "test": "试一次"}


def event_line(run):
    """醒来记录给前端的一小行：「21:05 醒来了一下（随机醒来）· 没说话 · 翻了翻记忆」"""
    lt = timeutil.to_local(run["finished_at"] or run["started_at"])
    head = (lt.strftime("%H:%M") + " " if lt else "") + f"醒来了一下（{KIND_LABEL.get(run['kind'], run['kind'])}）"
    parts = ["说了一句" if run["outcome"] == "spoke" else "没说话"]
    done = _tools_done(run)
    if done:
        parts.append(done)
    return head + "· " + " · ".join(parts)      # 括号后面直接接「·」，不多空一格


def events(c, conv_id=None, since_id=0, limit=50):
    """醒来记录（给前端显示成一小行，不进聊天、不发给 ta）：说了话的和没说话的。since_id 之后的，按先后。"""
    sql = f"SELECT {','.join(RUN_COLS)} FROM wake_runs WHERE outcome IN ('spoke','silent') AND id > ?"
    args = [since_id]
    if conv_id:
        sql += " AND conv_id=?"
        args.append(conv_id)
    out = []
    for r in [_run(x) for x in c.execute(sql + " ORDER BY id LIMIT ?", (*args, limit))]:
        plan = get_plan(c, r["plan_id"]) if r["plan_id"] else None
        out.append({"id": r["id"], "conversation_id": r["conv_id"], "at": _iso(r["finished_at"] or r["started_at"]),
                    "kind": r["kind"], "kind_label": KIND_LABEL.get(r["kind"], r["kind"]), "outcome": r["outcome"],
                    "message_id": r["id"] if r["outcome"] == "spoke" else None, "delivery": r["delivery"],
                    "tools": r["tools"] or [], "line": event_line(r),
                    "detail": {"plan_reason": (plan or {}).get("reason"), "aside": r["aside"], "thinking": r["thinking"]}})
    return out


def _iso(ts):
    dt = _dt(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def inbox(c, conv_id=None, limit=20):
    """前端来取：说了、还没取走的"""
    sql = f"SELECT {','.join(RUN_COLS)} FROM wake_runs WHERE delivery='pending'"
    args = []
    if conv_id:
        sql += " AND conv_id=?"
        args.append(conv_id)
    rows = [_run(r) for r in c.execute(sql + " ORDER BY id LIMIT ?", (*args, limit))]
    return [{"id": r["id"], "conversation_id": r["conv_id"], "text": r["text"], "at": _iso(r["finished_at"]),
             "kind": r["kind"]} for r in rows]


def _insert_after(c, conv_id, anchor_id, content, ts):
    """把醒来时说的话接在醒来时最新的那条后面（它后面已经接了几条更早醒来说的话，就再往后接）"""
    if not c.execute("SELECT 1 FROM l0_messages WHERE conv_id=? AND status='active' LIMIT 1", (conv_id,)).fetchone():
        return None   # 这个对话已经删了，不要再把它「救活」
    a = c.execute("SELECT msg_idx FROM l0_messages WHERE id=? AND conv_id=? AND status='active'",
                  (anchor_id, conv_id)).fetchone() if anchor_id else None
    if a is None:
        m = c.execute("SELECT MAX(msg_idx) FROM l0_messages WHERE conv_id=? AND status='active'", (conv_id,)).fetchone()[0]
        idx = (m if m is not None else -1) + 1
    else:
        idx = a[0] + 1
        for msg_idx, client, rts in c.execute(
                "SELECT msg_idx, client, ts FROM l0_messages WHERE conv_id=? AND status='active' AND msg_idx>? "
                "ORDER BY msg_idx, id", (conv_id, a[0])).fetchall():
            if client == "pluvia-wake" and (rts or "") <= ts:
                idx = msg_idx + 1
            else:
                break
        c.execute("UPDATE l0_messages SET msg_idx=msg_idx+1 WHERE conv_id=? AND status='active' AND msg_idx>=?", (conv_id, idx))
    cur = c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted) "
                    "VALUES (?, ?, 'assistant', ?, ?, 'pluvia-wake', 'active', 0)", (conv_id, idx, content, ts))
    return cur.lastrowid


def ack(c, ids=None, dropped=None, now=None):
    """前端取走了（显示了）：存进聊天记录。dropped：前端不显示的（比如那个对话在手机上删了）。重复 ack 没关系。"""
    now = now or timeutil.utc_now_str()
    out = []
    drop = set(int(x) for x in (dropped or []))
    for rid in [int(x) for x in (ids or [])] + sorted(drop):
        run = get_run(c, rid)
        if not run or run["outcome"] != "spoke":
            out.append({"id": rid, "status": "unknown"})
            continue
        if run["delivery"] in ("delivered", "dropped"):
            out.append({"id": rid, "status": run["delivery"], "l0_id": run["l0_id"]})
            continue
        if rid in drop:
            c.execute("UPDATE wake_runs SET delivery='dropped', delivered_at=? WHERE id=?", (now, rid))
            out.append({"id": rid, "status": "dropped"})
            continue
        lid = _insert_after(c, run["conv_id"], run["anchor_l0_id"], run["text"], run["finished_at"] or now)
        c.execute("UPDATE wake_runs SET delivery='delivered', delivered_at=?, l0_id=? WHERE id=?", (now, lid, rid))
        out.append({"id": rid, "status": "delivered", "l0_id": lid})
    return out


# ---------- 下次聊天时的附注 ----------

def turn_note(c, conv_id, now=None):
    """对方回来说话了：还没送到的不再送（标成 missed），醒来过的几次写成一行附注（还没告诉过 ta 的）。
    返回 {"note", "runs", "plans"}；这一轮回复成功以后网关调 seen 标成「告诉过了」。"""
    now = now or timeutil.utc_now_str()
    if conv_id:
        c.execute("UPDATE wake_runs SET delivery='missed' WHERE delivery='pending' AND conv_id=?", (conv_id,))
    since = _add(now, -MENTION_DAYS * 1440)
    runs = [_run(r) for r in c.execute(
        f"SELECT {','.join(RUN_COLS)} FROM wake_runs WHERE mentioned=0 AND outcome IN ('spoke','silent') AND started_at >= ? "
        "ORDER BY id", (since,))]
    plans = [_plan(r) for r in c.execute(
        f"SELECT {','.join(PLAN_COLS)} FROM wake_plans WHERE mentioned=0 AND status='skipped' AND updated_at >= ? ORDER BY id",
        (since,))]
    parts = []
    if runs:
        shown = runs[-MENTION_MAX:]
        items = []
        for r in shown:
            w = fmt_when(r["started_at"], now)
            if r["outcome"] == "spoke":
                s = f"{w} 说了「{_excerpt(r['text'])}」"
                if r["delivery"] == "missed" or r["delivery"] == "pending":
                    s += "（对方回来之前还没收到，多半没看到）"
                elif r["delivery"] == "dropped":
                    s += "（没有显示出来）"
                elif r["conv_id"] and conv_id and r["conv_id"] != conv_id:
                    s += "（在另一个对话里）"
            else:
                done = _tools_done(r)
                s = f"{w} 没说话" + (f"（{done}）" if done else "")
            items.append(s)
        more = len(runs) - len(shown)
        parts.append(f"对方不在的时候，你醒来过 {len(runs)} 次：" + ("；".join(items)) + (f"；更早还有 {more} 次" if more else "") + "。")
    for p in plans:
        parts.append(f"你约的 {fmt_when(p['due_at'], now)}" + (f"（「{_excerpt(p['reason'], 30)}」）" if p["reason"] else "")
                     + f"没有醒：{p['note'] or '没醒成'}。")
    text = "[" + "".join(parts) + "]" if parts else ""
    return {"note": text, "runs": [r["id"] for r in runs], "plans": [p["id"] for p in plans]}


def seen(c, runs=None, plans=None):
    for rid in runs or []:
        c.execute("UPDATE wake_runs SET mentioned=1 WHERE id=?", (int(rid),))
    for pid in plans or []:
        c.execute("UPDATE wake_plans SET mentioned=1 WHERE id=?", (int(pid),))


# ---------- ta 约的时间（schedule_wake） ----------

def _plan_line(p, now):
    return f"#{p['id']} {fmt_when(p['due_at'], now)}" + (f"：{p['reason']}" if p["reason"] else "")


def plan_act(c, action="set", at=None, in_minutes=None, reason=None, plan_id=None, conv_id=None, source="ai", now=None, s=None):
    now = now or timeutil.utc_now_str()
    s = s or cfg()
    action = (action or "set").strip().lower()
    if source == "ai" and not (s["enabled"] and s["plans"]):
        return {"status": "error", "message": "自动醒来现在关着，约不了（对方在 Dashboard 上能打开）"}
    if action == "list":
        ps = pending_plans(c)
        return {"status": "ok", "message": ("约着的：" + "；".join(_plan_line(p, now) for p in ps)) if ps else "现在没有约着的。",
                "plans": ps}
    if action == "cancel":
        p = get_plan(c, int(plan_id)) if plan_id not in (None, "") else None
        if not p:
            return {"status": "error", "message": f"没有找到要取消的那一个（#{plan_id}）" if plan_id not in (None, "") else "取消要带上编号 id"}
        if p["status"] != "pending":
            return {"status": "error", "message": f"#{p['id']} 已经不是等着的了"}
        c.execute("UPDATE wake_plans SET status='cancelled', note=?, updated_at=? WHERE id=?",
                  ("ta 自己取消的" if source == "ai" else "在 Dashboard 上取消的", now, p["id"]))
        return {"status": "ok", "message": f"取消了 #{p['id']}（{fmt_when(p['due_at'], now)}）。", "plan": get_plan(c, p["id"])}
    if action != "set":
        return {"status": "error", "message": f"不认识的 action：{action}（可以是 set / cancel / list）"}
    due = None
    if in_minutes not in (None, ""):
        try:
            due = _add(now, float(in_minutes))
        except (TypeError, ValueError):
            return {"status": "error", "message": "in_minutes 要是分钟数"}
    elif at:
        due = parse_at(at, now)
        if not due:
            return {"status": "error", "message": f"看不懂这个时间：{at}（可以写 2026-11-01 08:30、08:30，或者用 in_minutes）"}
    else:
        return {"status": "error", "message": "要写什么时候醒：at（几点）或 in_minutes（多少分钟以后）"}
    gap = _minutes(now, due)
    if gap < PLAN_MIN_MINUTES:
        return {"status": "error", "message": f"最早只能约到 {PLAN_MIN_MINUTES} 分钟以后（对方在的话，直接说就好）"}
    if gap > PLAN_MAX_DAYS * 1440:
        return {"status": "error", "message": f"最晚只能约到 {PLAN_MAX_DAYS} 天以内"}
    ps = pending_plans(c)
    if source == "ai" and len(ps) >= PLAN_PENDING_MAX:
        return {"status": "error", "message": f"已经约着 {len(ps)} 个了（最多 {PLAN_PENDING_MAX} 个）：" + "；".join(_plan_line(p, now) for p in ps)
                + "。要约新的，先取消一个（action=cancel）。"}
    near = [p for p in ps if abs(_minutes(p["due_at"], due)) < 20]
    if near:
        return {"status": "error", "message": f"那个时间附近已经约了：{_plan_line(near[0], now)}。要改的话先取消它。"}
    reason = re.sub(r"\s+", " ", str(reason or "")).strip()[:REASON_MAX] or None
    cur = c.execute("INSERT INTO wake_plans (conv_id, due_at, reason, source, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?)", (conv_id, due, reason, source, now, now))
    p = get_plan(c, cur.lastrowid)
    return {"status": "ok", "plan": p,
            "message": f"约好了：{fmt_when(due, now)} 醒来（#{p['id']}）。到时候你会醒一次，能看到这段对话，想说话就说，不想说也可以。"
                       "这个约定不会发给对方，只记在醒来记录里（对方在那里能看到）。到点时你们正在聊天的话就不单独醒。"}


# ---------- Dashboard ----------

def overview(c, now=None, s=None, limit=50):
    now = now or timeutil.utc_now_str()
    s = s or cfg()
    gw = state_get(c, "gateway") or {}
    peek = decide(c, gw.get("conv_id"), gw.get("last_real_at"), gw.get("tools"), bool(gw.get("has_context")), now=now, s=s, dry=True)
    runs = [_run(r) for r in c.execute(f"SELECT {','.join(RUN_COLS)} FROM wake_runs ORDER BY id DESC LIMIT ?", (limit,))]
    for r in runs:
        u = r.get("usage") or {}
        r["cost"] = cache_stats.cost({"model": u.get("model"), "ttl": u.get("ttl"), "input": u.get("input") or 0,
                                      "cache_write": u.get("cache_write") or 0, "cache_read": u.get("cache_read") or 0,
                                      "output": u.get("output") or 0}) if u else None
    plans = [_plan(r) for r in c.execute(
        f"SELECT {','.join(PLAN_COLS)} FROM wake_plans ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, due_at DESC LIMIT 30")]
    today = c.execute("SELECT outcome, COUNT(*) FROM wake_runs WHERE started_at >= ? GROUP BY outcome",
                      (_add(now, -1440),)).fetchall()
    return {"status": "ok", "now": now, "gateway": gw, "peek": peek, "last_roll": state_get(c, "last_roll"),
            "test_requested": state_get(c, "test_requested"), "push_endpoints": len(state_get(c, "push_endpoints", []) or []),
            "last_24h": {k: v for k, v in today}, "plans": plans, "runs": runs}


def forget_conv(c, conv_id):
    c.execute("DELETE FROM wake_runs WHERE conv_id=?", (conv_id,))
    c.execute("DELETE FROM wake_plans WHERE conv_id=?", (conv_id,))
