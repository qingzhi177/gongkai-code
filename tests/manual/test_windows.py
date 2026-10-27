"""换窗衔接（app/windows.py、app/pipeline/bridge.py）的测试

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_windows.py

用临时库和虚构的对话，不调用任何外部接口（写小结用假模型），不碰真实数据。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="windowtest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import branches as B  # noqa: E402
from app import config  # noqa: E402
from app import settings as S  # noqa: E402
from app import windows as W  # noqa: E402
from app.pipeline import bridge as BR  # noqa: E402
from app.pipeline import state as ST  # noqa: E402

PASS = FAIL = 0
TZ = timezone(timedelta(hours=8))


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {str(detail)[:1200]}")


def utc(local):
    """'2026-10-20 08:55' （北京时间）→ UTC 存储格式"""
    d = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def conn():
    return config.get_conn()


def add(conv, start_local, n, step_min=2, idx0=0, tag="", first_role="user"):
    """一个会话里加 n 条，从 start_local 开始每 step_min 分钟一条，user / assistant 轮流。返回 [(id, role, content)]"""
    c = conn()
    out = []
    t = datetime.strptime(start_local, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    roles = ("user", "assistant") if first_role == "user" else ("assistant", "user")
    try:
        for k in range(n):
            role = roles[k % 2]
            content = f"{tag}第{idx0 + k}句：" + ("今天想和你说说小雨伞的事" if role == "user" else "好呀，我在听，慢慢说")
            ts = (t + timedelta(minutes=step_min * k)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cur = c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES (?,?,?,?,?,?,?)",
                            (conv, idx0 + k, role, content, ts, "pluvia", "active"))
            out.append((cur.lastrowid, role, content))
        c.commit()
    finally:
        c.close()
    return out


def entries_of(rows):
    return [{"r": "u" if r[1] == "user" else "a", "h": W.hash_text(r[1], r[2])} for r in rows]


def compose(conv, rows, link=None, now=None):
    c = conn()
    try:
        r = W.compose(c, conv, entries_of(rows), link, now=now)
        c.commit()
        return r
    finally:
        c.close()


def settle(day, last_id):
    c = conn()
    try:
        c.execute("INSERT OR REPLACE INTO settle_days (day, status, last_l0_id, messages, sessions, episodes, detail, version, settled_at) "
                  "VALUES (?, 'done', ?, 0, 0, 0, '[]', 1, ?)", (day, last_id, utc("2026-10-20 05:00")))
        c.commit()
    finally:
        c.close()


N = 0


async def fake_llm(prompt):
    global N
    N += 1
    return json.dumps({"gist": f"你和我聊了第{N}段的事，你说晚点再接着讲。", "mood": "松下来"}, ensure_ascii=False), "fake-model"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        print("\n[1] 指纹（和网关一致的写法）")
        check("空白、换行、图片占位不算", W.hash_text("user", " 你好\n[图片：a.png] 呀") == W.hash_text("user", "你好呀")
              and W.hash_text("user", "你好呀") != W.hash_text("assistant", "你好呀"))
        check("只发了图：按张数", W.hash_text("user", "[图片]") == W.hash_text("user", "[图片：x.jpg]")
              and W.hash_text("user", "[图片][图片]") != W.hash_text("user", "[图片]"))
        check("全角空格、不换行空格也算空白", W.hash_text("user", "早　安\xa0啦") == W.hash_text("user", "早安啦"))
        check("固定写法（网关测试 T30 核对同样的值）", W.hash_text("user", "早安～") == "5b18fe3e3260"
              and W.hash_text("assistant", "好呀\n[图片：猫.jpg]") == "bcea416799bf", W.hash_text("user", "早安～"))

        # 会话 P：昨晚 20:00–21:18（40 条）；今天 08:55 起（60 条，每 2 分钟一条）
        night = add("P", "2026-10-19 20:00", 40, tag="昨晚")
        day = add("P", "2026-10-20 08:55", 60, idx0=40, tag="今天")
        allp = night + day
        settle("2026-10-19", night[-1][0])
        NOW = utc("2026-10-20 11:00")

        print("\n[2] 窗口在往后滑：窗口开头那条写明几点的，滑出去的那段先说还没记下")
        win = allp[70:100]          # 窗口 30 条（最后一条是这一轮之前 AI 的回复；这一轮的用户消息不在里面）
        r = compose("P", win, now=NOW)
        n0 = next((x for x in r["notes"] if x["i"] == 0), None)
        check("附注加在窗口第一条（用户说的）", n0 is not None and r["info"]["matched"] == 30, r)
        check("写明窗口最早那条的时间（绝对时间，带周几）", n0 and "窗口里最早的一条是 10 月 20 日（周二）09:55 的" in n0["text"]
              and n0["text"].startswith("［记忆系统附注｜") and n0["text"].endswith("］"), n0)
        check("还没写小结：说有几条已经不在窗口里", n0 and "还有 30 条也已经不在窗口里" in n0["text"], n0)
        check("要写小结的块（滑出去的 + 快滑出去的）", len(r["needs"]) == 3 and all(ch["closed"] for ch in r["needs"]), len(r["needs"]))
        check("昨晚整理过的不写", all(row["id"] > night[-1][0] for ch in r["needs"] for row in ch["rows"]))
        check("没有相对时间（缓存要每轮一样）", not any(w in n0["text"] for w in ("前", "刚才")), n0["text"])

        print("\n[3] 后台写小结")
        res = run(BR.run("P", llm_fn=fake_llm))
        check("写了小结", res["written"] == 3 and all(x["status"] == "active" and x["mood"] == "松下来" for x in res["chunks"]), res)
        c = conn()
        nb = c.execute("SELECT COUNT(*) FROM window_bridges WHERE status='active'").fetchone()[0]
        c.close()
        check("存进 window_bridges", nb == res["written"], nb)
        r2 = compose("P", win, now=NOW)
        check("窗口没挪：附注一模一样（新写好的小结等窗口挪了再放）", r2["notes"] == r["notes"] and r2["info"].get("frozen"), r2["notes"])
        check("写过的不再要写", not r2["needs"] or len(r2["needs"]) < len(r["needs"]), len(r2["needs"]))

        win2 = allp[78:100]         # 窗口往后挪了 8 条
        r3 = compose("P", win2, now=NOW)
        t3 = r3["notes"][0]["text"] if r3["notes"] else ""
        check("窗口挪了：附注里换成小结（时间段 + 一两句 + 那会儿）", "我在它们滑出窗口前记下了几句" in t3 and "· 08:55–" in t3
              and "（那会儿：松下来）" in t3 and "你和我聊了第" in t3, t3)
        check("开头是新的窗口第一条", "10 月 20 日（周二）10:11 的" in t3, t3)

        print("\n[4] 窗口里隔了很久的地方（昨晚的晚安 → 今早）")
        win3 = allp[30:50]          # 昨晚最后 10 条 + 今早前 10 条
        r4 = compose("P", win3, now=NOW)
        gap = next((x for x in r4["notes"] if x["i"] == 10), None)
        check("今早第一句前面写明隔了多久（绝对时间）", gap and "和上一句隔了 12 小时（10 月 19 日（周一）21:18 → 10 月 20 日（周二）08:55）" in gap["text"], r4["notes"])
        check("窗口开头那条也有（昨晚整理过了，没有小结）", r4["notes"][0]["i"] == 0 and "10 月 19 日（周一）21:00" in r4["notes"][0]["text"]
              and "小结" not in r4["notes"][0]["text"], r4["notes"][0])
        S.update("windows", {"gap_hours": 13})
        r5 = compose("P", win3, now=NOW)
        check("隔多久算断口可以调", not any(x["i"] == 10 for x in r5["notes"]), r5["notes"])
        S.reset("windows")
        win4 = allp[31:50]          # 第一条是 AI 的回复
        r6 = compose("P", win4, now=NOW)
        check("窗口第一条是 AI 说的：附注加在第一条用户的话上", r6["notes"][0]["i"] == 1, r6["notes"][:1])

        print("\n[5] 对不上的不加")
        fake = [{"r": "u", "h": "000000000000"}, {"r": "a", "h": "111111111111"}]
        c = conn()
        r7 = W.compose(c, "P", fake, now=NOW)
        c.close()
        check("一条都对不上：什么都不加", r7["notes"] == [] and r7["info"]["matched"] == 0, r7)
        c = conn()
        c.execute("INSERT INTO l0_edits (l0_id, conv_id, old_content, new_content, mode, created_at) VALUES (?, 'P', ?, ?, 'edit', ?)",
                  (win3[10][0], "改之前的早安", win3[10][2], NOW))
        c.commit()
        c.close()
        ent = entries_of(win3)
        ent[10] = {"r": "u", "h": W.hash_text("user", "改之前的早安")}
        c = conn()
        r8 = W.compose(c, "P", ent, now=NOW)
        c.close()
        check("在 Dashboard 上改过的：前端拿的是改之前的原文，也认得", any(x["i"] == 10 for x in r8["notes"]) and r8["info"]["matched"] == 20,
              r8["info"])

        print("\n[6] 新开一个窗口：接上一个窗口")
        r9 = compose("N", [], now=utc("2026-10-20 12:00"))
        check("第一轮：这一轮的附注里写上一个窗口停在哪、最后几句", r9["turn_note"].startswith("[新窗口] 这是新开的窗口")
              and "上一个窗口停在 10 月 20 日（周二）10:53" in r9["turn_note"] and "最后几句是：" in r9["turn_note"]
              and "你：今天第" in r9["turn_note"] and "我：今天第" in r9["turn_note"], r9["turn_note"])
        check("上一个窗口还没整理成片段的那部分也带上", "还没整理成每日片段的部分" in r9["turn_note"], r9["turn_note"])
        c = conn()
        lin = W.lineage(c, "N")
        c.close()
        check("记下来处：新开的，上一个窗口是 P", lin and lin["kind"] == "new" and lin["prev"] == "P", lin)
        nrows = add("N", "2026-10-20 12:00", 2, tag="新窗口")
        r10 = compose("N", nrows, now=utc("2026-10-20 12:05"))
        t10 = r10["notes"][0]["text"] if r10["notes"] else ""
        check("第二轮：第一条前面写明是新开的窗口 + 上一个窗口", "这是新开的窗口（10 月 20 日（周二）12:00 开始）" in t10
              and "上一个窗口停在" in t10, t10)
        r10b = compose("N", nrows, now=utc("2026-10-20 12:30"))
        check("之后每一轮一样（缓存）", r10b["notes"] == r10["notes"])
        old = add("K", "2026-10-20 12:00", 0)
        c = conn()
        rk = W.compose(c, "K", entries_of(nrows), now=NOW)
        c.commit()
        lin_k = W.lineage(c, "K")
        c.close()
        check("第一次见到就带着历史的会话（别处开的）：不当成新窗口", lin_k and lin_k["kind"] == "unknown" and not rk["turn_note"], lin_k)

        print("\n[7] 挑选式搬家：挑几段带进新会话")
        picks = [night[2], night[3], day[20], day[21]]      # 昨晚的一轮 + 今天的一轮（中间省略了很多条）
        c = conn()
        c.execute("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, status, salience, created_at) "
                  "VALUES ('2026-10-19', 'P', ?, ?, '一起数星星', '…', '…', 'active', 0.6, ?)", (utc("2026-10-19 20:30"), utc("2026-10-19 21:00"), NOW))
        c.execute("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, status, salience, created_at) "
                  "VALUES ('2026-10-19', 'P', ?, ?, '老家的事', '…', '…', 'active', 0.9, ?)", (utc("2026-10-19 20:40"), utc("2026-10-19 21:10"), NOW))
        c.commit()
        c.close()
        ST.add_policy("老家的事", ["老家"], "excluded")
        link = {"kind": "forged", "conversation_id": "P"}
        r11 = compose("F", picks, link=link, now=utc("2026-10-20 12:10"))
        gapn = next((x for x in r11["notes"] if x["i"] == 2), None)
        check("第一轮（还没登记）：断口写明隔了多久、省略了几条", gapn and "隔了 13 小时，中间省略了 56 条" in gapn["text"], r11["notes"])
        check("断口带上那段时间的片段（轻放的不写）", gapn and "《一起数星星》" in gapn["text"] and "老家" not in gapn["text"], gapn)
        check("这一轮的附注：从这一句开始是新开的窗口", r11["turn_note"].startswith("[新窗口] 从这一句开始是新开的窗口"), r11["turn_note"])
        check("开头那条写明是挑过来的（挑剩下的不补小结）", r11["notes"][0]["i"] == 0 and "从上一个窗口挑出来带过来的" in r11["notes"][0]["text"]
              and "小结" not in r11["notes"][0]["text"] and not r11["needs"], r11["notes"][0])
        c = conn()
        lin_f = W.lineage(c, "F")
        msgs = [(r[1], r[2]) for r in picks] + [("user", "搬家后的第一句"), ("assistant", "嗯，我都还记得")]
        b = B.register(c, "F", {"conversation_id": "P"}, msgs, utc("2026-10-20 12:10"), kind="forged")
        c.commit()
        c.close()
        check("来处：搬家，旧会话 P", lin_f and lin_f["kind"] == "forged" and lin_f["prev"] == "P", lin_f)
        check("登记：带过来的 4 条（不连续）对上原会话的行", b and b["kind"] == "forged" and b["shared"] == 4
              and [e[2] for e in b["prefix"]] == [p[0] for p in picks], b and b["prefix"])
        own = add("F", "2026-10-20 12:10", 2, tag="搬家后")
        r12 = compose("F", picks + own, now=utc("2026-10-20 12:15"))
        bnd = next((x for x in r12["notes"] if x["i"] == 4), None)
        check("登记以后：新说的第一句前面写明从这里开始是新窗口", bnd and "从这里开始是 10 月 20 日（周二）12:10 新开的窗口" in bnd["text"], r12["notes"])
        check("断口照样有", any(x["i"] == 2 and "省略了 56 条" in x["text"] for x in r12["notes"]), r12["notes"])
        c = conn()
        b2 = B.register(c, "G", {"conversation_id": "P", "carried": 2}, msgs, NOW, kind="forged")
        same = [(day[30][1], day[30][2])]
        b3 = B.register(c, "H", {"conversation_id": "P"}, [(r[1], r[2]) for r in picks[:2]] + [("user", day[30][2]), ("assistant", "新回复")],
                        NOW, kind="forged")
        c.commit()
        c.close()
        check("前端说了带过来几条：最多认这么多", b2 and b2["shared"] == 2, b2 and b2["shared"])
        check("这一轮新说的话碰巧和旧会话里的一样：不算带过来的（不会漏存）", b3 and b3["shared"] == 2, b3 and b3["shared"])

        print("\n[8] 整理成每日片段以后不再写小结")
        settle("2026-10-20", day[-1][0])
        win5 = allp[-15:-1]
        r13 = compose("P", win5, now=NOW)
        t13 = r13["notes"][0]["text"] if r13["notes"] else ""
        check("整天整理过了：没有小结、不要写", "小结" not in t13 and "还有" not in t13 and not r13["needs"], (t13, len(r13["needs"])))

        print("\n[9] 小结的校验")
        rows = [{"id": 1, "conv": "P", "role": "user", "content": "下午三点有实验课，回来跟你讲", "ts": NOW},
                {"id": 2, "conv": "P", "role": "assistant", "content": "好，我等你", "ts": NOW}]
        g, m, iss = BR.verify({"gist": "你说「下午三点有实验课」。你还说「明天去北京」。", "mood": "期待"}, rows)
        check("引号里的原话对不上的那一句去掉", g == "你说「下午三点有实验课」。" and m == "期待" and iss, (g, iss))
        g, m, iss = BR.verify({"gist": "你" * 300, "mood": "一个很长很长的温度词"}, rows)
        check("太长截短、温度词太长不要", len(g) <= BR.GIST_MAX + 2 and m == "" and len(iss) == 2, (len(g), m, iss))
        ch = {"rows": rows, "key": "k-short"}
        called = []

        async def boom(prompt):
            called.append(1)
            return "不是 JSON", "fake"
        rs = run(BR.summarize("P", ch, llm_fn=boom))
        check("太短的块不调模型，记成 empty", rs["status"] == "empty" and not called, rs)
        ch2 = {"rows": rows * 3, "key": "k-bad"}
        rb = run(BR.summarize("P", ch2, llm_fn=boom))
        check("模型输出不是 JSON：记成 empty，写明原因", rb["status"] == "empty" and called and "不是 JSON" in rb["issues"][0], rb)

        async def light(prompt):
            return json.dumps({"gist": "你说起老家的事，我陪你聊了很久。", "mood": "心疼"}, ensure_ascii=False), "fake"
        c = conn()
        c.execute("DELETE FROM settle_days WHERE day='2026-10-20'")
        c.execute("DELETE FROM window_bridges")
        c.execute("DELETE FROM window_state")
        c.commit()
        c.close()
        compose("P", allp[70:100], now=NOW)
        run(BR.run("P", llm_fn=light))
        r14 = compose("P", allp[78:100], now=NOW)
        check("碰到「轻放」话题的小结不放", r14["notes"] and "老家" not in r14["notes"][0]["text"], r14["notes"][:1])

        print("\n[10] 开关、删除、Dashboard")
        S.update("windows", {"notes": False, "bridge": False, "prev_window": False})
        r15 = compose("P", allp[78:100], now=NOW)
        check("都关掉：什么都不加", r15["notes"] == [] and r15["turn_note"] == "", r15)
        S.reset("windows")
        c = conn()
        ov = W.overview(c, "P", now=NOW)
        c.close()
        check("Dashboard 总览：会话、窗口开头那行、小结", ov["conv_id"] == "P" and ov["note"] and ov["bridges"]
              and any(x["conv_id"] == "F" and x["kind"] == "forged" and x["from"] == "P" and x["carried"] == 4 for x in ov["convs"]), ov["convs"])
        c = conn()
        W.forget_conv(c, "P")
        c.commit()
        left = [c.execute(f"SELECT COUNT(*) FROM {t} WHERE conv_id='P'").fetchone()[0] for t in ("window_state", "window_bridges")]
        c.close()
        check("删掉会话：窗口状态和小结一起删", left == [0, 0], left)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


def _has_cols(c, table, cols):
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    return all(x in have for x in cols)


if __name__ == "__main__":
    main()
