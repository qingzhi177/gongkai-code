"""窗外衔接的小结（方案第 16 章，Phase 5 第一步）：滑出窗口、但还没整理成每日片段的那段，每块写一两句。

哪些块要写（app/windows.py）：同一段对话里大约 16 条一块，块写完了（后面还有话，或者这段对话停了 45 分钟以上）、
而且已经滑出窗口，或者就在窗口最前面快要滑出去了（提前写好，滑出去时就有）。每日片段整理过的那几天不用写。
每块交给模型（app/prompts/window_bridge.md）写 gist（一两句）和 mood（那会儿的温度词）。
校验：gist 太长截短；引号里的原话必须在原文里找得到，找不到的那一句去掉；mood 超过 8 个字不要。
寒暄、太短的块（少于 4 条且不到 200 字）不调模型，记成 empty。
模型调用失败时抛异常（任务按退避时间重试），不写半截。
"""
import json
import re
from pathlib import Path

import timeutil
from app import windows as W
from app.config import get_conn
from app.pipeline import arcs as A

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "window_bridge.md"
MAX_TOKENS = 6000
MAX_PER_JOB = 6
MIN_MESSAGES = 4
MIN_CHARS = 200
LINE_CHARS = 400
GIST_MAX = 150
MOOD_MAX = 8
QUOTE = re.compile(r"[「『“\"]([^」』”\"]{2,})[」』”\"]")
SENT = re.compile(r"[^。！？!?；;]+[。！？!?；;]*")
_NORM = re.compile(r"[\s，。！？、,.!?~～…·：:；;「」『』“”\"'（）()\-—]")


async def default_llm(prompt):
    """用「模块模型配置」里的「窗外衔接」；没设置就依次用「每日片段」「共同经历叙事」「近期摘要」的"""
    return await A.default_llm(prompt, ("bridge", "episode", "narrative", "summary"), MAX_TOKENS)


def prompt_template():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    m = re.match(r"<!--\s*(\S+\s+v\d+)[^>]*-->\s*", text)
    version = m.group(1).replace(" ", "-") if m else "window_bridge-v?"
    return (text[m.end():] if m else text), version


def build_prompt(rows):
    tpl, version = prompt_template()
    lines = []
    for r in rows:
        t = r["content"] if len(r["content"]) <= LINE_CHARS else r["content"][:LINE_CHARS] + "……"
        lines.append(f"[{'你' if r['role'] == 'user' else '我'} {timeutil.local_str(r['ts'], '%H:%M')}] {t}")
    t0, t1 = timeutil.local_str(rows[0]["ts"]), timeutil.local_str(rows[-1]["ts"])
    span = t0 if (not t1 or t1 == t0) else t0 + " 至 " + (t1[11:] if t1[:10] == t0[:10] else t1)
    prompt = tpl.replace("{span}", span).replace("{tz}", timeutil.USER_TZ_LABEL).replace("{lines}", "\n".join(lines))
    return prompt, version


def _n(t):
    return _NORM.sub("", t or "")


def verify(data, rows):
    """返回 (gist, mood, 问题列表)"""
    issues = []
    data = data if isinstance(data, dict) else {}
    gist = re.sub(r"\s+", " ", str(data.get("gist") or "")).strip()
    mood = re.sub(r"\s+", "", str(data.get("mood") or "")).strip("。.，, ")
    src = _n("".join(r["content"] for r in rows))
    kept = []
    for s in SENT.findall(gist):
        bad = [q for q in QUOTE.findall(s) if _n(q) and _n(q) not in src]
        if bad:
            issues.append(f"原话对不上「{bad[0][:20]}」，这一句去掉")
            continue
        kept.append(s.strip())
    gist = "".join(kept).strip()
    if len(gist) > GIST_MAX:
        issues.append(f"太长（{len(gist)} 字），截短")
        cut = gist[:GIST_MAX]
        k = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("，"))
        gist = (cut[:k + 1] if k > GIST_MAX // 2 else cut).rstrip("，") + ("" if cut.endswith("。") else "……")
    if len(mood) > MOOD_MAX:
        issues.append(f"温度词太长「{mood[:12]}」，不要")
        mood = ""
    return gist, mood, issues


def _store(conn, conv_id, ch, gist, mood, status, issues, model, version):
    rows = ch["rows"]
    convs = ",".join(sorted({r["conv"] for r in rows}))
    conn.execute(
        "INSERT INTO window_bridges (chunk_key, conv_id, src_convs, first_id, last_id, start_ts, end_ts, n, gist, mood, status, issues, "
        "model, prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chunk_key) DO UPDATE SET gist=excluded.gist, mood=excluded.mood, status=excluded.status, issues=excluded.issues, "
        "model=excluded.model, prompt_version=excluded.prompt_version, created_at=excluded.created_at",
        (ch["key"], conv_id, convs, rows[0]["id"], rows[-1]["id"], rows[0]["ts"], rows[-1]["ts"], len(rows), gist, mood, status,
         json.dumps(issues, ensure_ascii=False), model, version, timeutil.utc_now_str()))


async def summarize(conv_id, ch, llm_fn=None):
    """给一块写小结并存下。返回 {"status", "gist", "mood", "issues"}"""
    rows = ch["rows"]
    if len(rows) < MIN_MESSAGES and sum(len(r["content"]) for r in rows) < MIN_CHARS:
        res = {"status": "empty", "gist": "", "mood": "", "issues": ["太短，不写"]}
        model = version = None
    else:
        prompt, version = build_prompt(rows)
        text, model = await (llm_fn or default_llm)(prompt)
        if not text:
            raise RuntimeError("写窗外小结：调用模型失败（没有返回内容），稍后重试")
        try:
            data = A.parse_json(text)
        except ValueError as e:
            data, err = {}, f"模型的输出不是 JSON：{e}"
        else:
            err = None
        gist, mood, issues = verify(data, rows)
        if err:
            issues.insert(0, err)
        res = {"status": "active" if gist else "empty", "gist": gist, "mood": mood if gist else "", "issues": issues}
    conn = get_conn()
    try:
        _store(conn, conv_id, ch, res["gist"], res["mood"], res["status"], res["issues"], model, version)
        conn.commit()
    finally:
        conn.close()
    return res


async def run(conv_id, llm_fn=None, limit=MAX_PER_JOB):
    """写小结的任务：这个会话现在要写的块，一次最多 limit 块，剩下的再投一个任务"""
    conn = get_conn()
    try:
        todo = W.pending_for(conn, conv_id)
        _prune(conn)
        conn.commit()
    finally:
        conn.close()
    done = []
    for ch in todo[:limit]:
        r = await summarize(conv_id, ch, llm_fn)
        done.append({"from": timeutil.local_str(ch["rows"][0]["ts"]), "to": timeutil.local_str(ch["rows"][-1]["ts"]),
                     "messages": len(ch["rows"]), "status": r["status"], "gist": r["gist"], "mood": r["mood"], "issues": r["issues"]})
    more = len(todo) > limit
    if more:
        W.enqueue_bridge(conv_id, continuation=True)
    return {"status": "ok", "conv_id": conv_id, "written": len(done), "chunks": done, "more": more}


def _prune(conn, keep_days=14):
    """两周以前的小结用不到了（那几天早就整理成每日片段）"""
    lo = timeutil.to_utc_str(timeutil.utc_now().timestamp() - keep_days * 86400)
    conn.execute("DELETE FROM window_bridges WHERE datetime(end_ts) < datetime(?)", (lo,))
