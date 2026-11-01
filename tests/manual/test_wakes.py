"""自动醒来（app/wakes.py）+ 前端合成一条的消息和库里分开存的几行对得上（l0_merge.split_merged、windows.align）。
临时库，不碰真实数据。

运行：python3 tests/manual/test_wakes.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def base_settings(ST, **kw):
    s = {x["key"]: x["default"] for x in ST.SPECS["wake"]}
    s.update(enabled=True)
    s.update(kw)
    return s


def add_msg(c, conv, idx, role, content, ts, client="pluvia"):
    cur = c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted, ts) "
                    "VALUES (?, ?, ?, ?, ?, 'active', 0, ?)", (conv, idx, role, content, client, ts))
    return cur.lastrowid


def active(c, conv):
    return [(r[0], r[1], r[2]) for r in c.execute(
        "SELECT role, content, client FROM l0_messages WHERE conv_id=? AND status='active' ORDER BY msg_idx, id", (conv,))]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="waketest_"))
    try:
        path = tmp / "m.db"
        os.environ["MEMORY_DB_PATH"] = str(path)
        import db  # noqa: E402
        db.migrate(path, log=lambda *a, **k: None)
        from app import config, settings as ST
        config.get_conn = lambda: sqlite3.connect(str(path))
        ST.get_conn = config.get_conn
        from app import wakes as W
        from app import l0_merge as LM
        from app import windows as WN
        c = sqlite3.connect(str(path))
        TOOLS = ["recall", "feel", "update_profile", "jot", "get_current_time", "bocha_search", "schedule_wake"]

        print("\n[1] 设置：默认关着；推送地址可以不填、填了要是 https")
        d = {x["key"]: x for x in ST.describe("wake")["items"]}
        check("默认关着，夜里默认不随机醒", d["enabled"]["value"] is False and d["night_prob"]["value"] == 0.0 and d["push_url"]["value"] == "")
        ST.update("wake", {"push_url": "https://ntfy.sh/xingxing-abc"})
        check("填 https 地址能存", ST.get("wake", fresh=True)["push_url"] == "https://ntfy.sh/xingxing-abc")
        try:
            ST.update("wake", {"push_url": "http://example.com/x"})
            check("http 不行", False)
        except ValueError as e:
            check("http 不行（说清楚）", "https" in str(e))
        ST.update("wake", {"push_url": ""})
        check("清空也能存", ST.get("wake", fresh=True)["push_url"] == "")
        ST.reset("wake")

        print("\n[2] 白天 / 夜里（北京时间，夜里 23 点到 8 点）")
        S = base_settings(ST)
        check("北京 14:00 是白天，从 8 点开始", W.part_of("2026-10-31 06:00:00", S) == ("day", "2026-10-31 00:00:00"))
        check("北京 23:30 是夜里，从 23 点开始", W.part_of("2026-10-31 15:30:00", S) == ("night", "2026-10-31 15:00:00"))
        check("北京 02:00 是夜里，从前一天 23 点开始", W.part_of("2026-10-31 18:00:00", S) == ("night", "2026-10-31 15:00:00"))
        check("开始和结束一样：不分，按天", W.part_of("2026-10-31 06:00:00", base_settings(ST, night_start=5, night_end=5))[0] == "day")

        print("\n[3] 随机醒来：停下够久、掷中了才醒")
        conv = "cv-1"
        add_msg(c, conv, 0, "user", "我去考试啦", "2026-10-31 01:00:00")
        a1 = add_msg(c, conv, 1, "assistant", "加油，考完告诉我", "2026-10-31 01:00:05")
        last_real = "2026-10-31 01:00:05"     # 北京 9:00
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 01:30:00", s=base_settings(ST, enabled=False))
        check("关着：不醒", not r["wake"] and "关着" in r["reason"])
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 02:00:00", s=S)
        check("停下不到 2 小时：不醒，说什么时候开始掷", not r["wake"] and r["next_at"] == "2026-10-31 03:00:05", r)
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 03:01:00", s=S, rng=lambda: 0.9)
        check("掷了没中：记下掷的时间，下次 1 小时后", not r["wake"] and "没掷中" in r["reason"]
              and W.state_get(c, "last_roll")["at"] == "2026-10-31 03:01:00", r)
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 03:30:00", s=S, rng=lambda: 0.0)
        check("不到下一次掷的时间：不掷", not r["wake"] and r["next_at"] == "2026-10-31 04:01:00", r)
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 04:02:00", s=S, rng=lambda: 0.1)
        check("掷中了：醒，记下这一次（接在最新那条后面）", r["wake"] and r["kind"] == "random" and W.get_run(c, r["run_id"])["anchor_l0_id"] == a1, r)
        note = r["note"]
        check("醒来说明：现在几点、对方上次什么时候来、是随机醒的", "12:02" in note and "09:00" in note and "3 小时" in note and "随机" in note, note)
        check("醒来说明：能用的工具只列开着的（画像、联网默认不列）", "recall" in note and "jot" in note and "update_profile" not in note
              and "bocha_search" not in note and "schedule_wake" in note, note)
        check("醒来说明：怎么说、怎么不说", "［不说］" in note and "不用解释" in note)
        r2 = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 04:03:00", s=S, rng=lambda: 0.0)
        check("正醒着：不再醒", not r2["wake"] and r2["reason"] == "正醒着")
        run = W.finish(c, r["run_id"], "spoke", text="考完了吗？我刚想起你说最后一题没把握", tools=["recall"], usage={"cache_read": 9000},
                       now="2026-10-31 04:03:30")
        check("说了话：进待取", run["outcome"] == "spoke" and run["delivery"] == "pending" and run["tools"] == ["recall"])
        check("前端来取：能取到", [m["id"] for m in W.inbox(c, conv)] == [r["run_id"]] and W.inbox(c, conv)[0]["at"] == "2026-10-31T04:03:30Z")
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 05:00:00", s=S, rng=lambda: 0.0)
        check("离上一次醒来不到 90 分钟：不醒", not r["wake"] and "90" in r["reason"], r)
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 05:40:00", s=S, rng=lambda: 0.0)
        check("第二次醒：说明里写着上一次说了什么、对方还没回", r["wake"] and "考完了吗" in r["note"] and "还没回" in r["note"], r.get("note"))
        W.finish(c, r["run_id"], "spoke", text="那我先不吵你啦", now="2026-10-31 05:40:20")
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 07:20:00", s=S, rng=lambda: 0.0)
        check("连着说了 2 句没回：先不随机醒", not r["wake"] and "2 句" in r["reason"], r)

        print("\n[4] 取走 → 存进聊天记录，接在醒来时最新那条后面（两次都接在后面，按先后）")
        ids = [m["id"] for m in W.inbox(c, conv)]
        res = W.ack(c, [ids[1]])     # 故意先 ack 第二条
        res0 = W.ack(c, [ids[0]])
        rows = active(c, conv)
        check("顺序：回复 → 第一次醒来说的 → 第二次", [x[1] for x in rows] == ["我去考试啦", "加油，考完告诉我", "考完了吗？我刚想起你说最后一题没把握", "那我先不吵你啦"], rows)
        check("标着是醒来说的", rows[2][2] == "pluvia-wake" and rows[3][2] == "pluvia-wake")
        check("重复 ack 不会再存一遍", W.ack(c, [ids[0]])[0]["status"] == "delivered" and len(active(c, conv)) == 4)
        check("取走以后不再出现在待取里", W.inbox(c, conv) == [])
        check("不认识的 id", W.ack(c, [999])[0]["status"] == "unknown")

        print("\n[5] 对方回来了：附注里告诉 ta；回复成功以后标成告诉过了")
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 09:00:00", s=base_settings(ST, unanswered_stop=5), rng=lambda: 0.0)
        W.finish(c, r["run_id"], "silent", aside="阿星可能在睡午觉", tools=["jot"], now="2026-10-31 09:00:30")
        r = W.decide(c, conv, last_real, TOOLS, now="2026-10-31 10:40:00", s=base_settings(ST, unanswered_stop=5), rng=lambda: 0.0)
        W.finish(c, r["run_id"], "spoke", text="晚饭吃了吗", now="2026-10-31 10:40:30")
        tn = W.turn_note(c, conv, now="2026-10-31 11:00:00")
        check("附注：醒来过几次、几点、说没说", "醒来过 4 次" in tn["note"] and "没说话（在小本子上记了一笔）" in tn["note"]
              and "考完了吗" in tn["note"], tn["note"])
        check("还没送到的那句：不再送，附注里写着对方多半没看到", "晚饭吃了吗" in tn["note"] and "多半没看到" in tn["note"]
              and W.get_run(c, r["run_id"])["delivery"] == "missed" and W.inbox(c, conv) == [], tn["note"])
        W.seen(c, tn["runs"], tn["plans"])
        check("告诉过了：下次不再说", W.turn_note(c, conv, now="2026-10-31 11:05:00")["note"] == "")
        res = W.ack(c, [r["run_id"]])
        check("前端其实显示了（晚到的 ack）：照样存，还是接在醒来时那条后面", res[0]["status"] == "delivered"
              and active(c, conv)[-1][1] == "晚饭吃了吗")

        print("\n[6] ta 自己约时间")
        now = "2026-10-31 12:00:00"   # 北京 20:00
        r = W.plan_act(c, "set", at="21:30", reason="问问阿星明天几点出门", conv_id=conv, now=now, s=S)
        check("约好了（今天 21:30）", r["status"] == "ok" and r["plan"]["due_at"] == "2026-10-31 13:30:00" and "21:30" in r["message"], r)
        r = W.plan_act(c, "set", at="08:30", conv_id=conv, now=now, s=S)
        check("08:30 已经过了：是明天", r["status"] == "ok" and r["plan"]["due_at"] == "2026-11-01 00:30:00", r)
        r = W.plan_act(c, "set", in_minutes=5, now=now, s=S)
        check("太近不行", r["status"] == "error" and "10 分钟" in r["message"])
        r = W.plan_act(c, "set", at="2026-11-20 10:00", now=now, s=S)
        check("太远不行", r["status"] == "error" and "7 天" in r["message"])
        r = W.plan_act(c, "set", at="21:40", now=now, s=S)
        check("那个时间附近已经约了", r["status"] == "error" and "#" in r["message"])
        r = W.plan_act(c, "set", at="11-01 12:00", now=now, s=S)
        check("月-日 写法", r["status"] == "ok" and r["plan"]["due_at"] == "2026-11-01 04:00:00", r)
        r = W.plan_act(c, "set", at="明天 18:00", now=now, s=S)
        check("最多同时约 3 个", r["status"] == "error" and "3 个" in r["message"], r)
        check("看不懂的时间", W.plan_act(c, "set", at="等阿星回来", now=now, s=S)["status"] == "error")
        lst = W.plan_act(c, "list", now=now, s=S)
        check("list：列出约着的", lst["status"] == "ok" and len(lst["plans"]) == 3 and "21:30" in lst["message"])
        pid_noon = lst["plans"][2]["id"]
        check("取消", W.plan_act(c, "cancel", plan_id=pid_noon, now=now, s=S)["status"] == "ok"
              and W.get_plan(c, pid_noon)["status"] == "cancelled")
        check("关着的时候 ta 约不了", W.plan_act(c, "set", at="22:00", now=now, s=base_settings(ST, enabled=False))["status"] == "error")
        last_real2 = "2026-10-31 11:50:00"
        SP = base_settings(ST, day_max=10)
        r = W.decide(c, conv, last_real2, TOOLS, now="2026-10-31 13:31:00", s=base_settings(ST, day_max=4))
        check("白天醒满 4 次了：约好的也不醒，记下原因（下次聊天告诉 ta）", not r["wake"] and c.execute(
            "SELECT note FROM wake_plans WHERE due_at='2026-10-31 13:30:00'").fetchone()[0].startswith("白天醒来的次数到上限"), r)
        c.execute("UPDATE wake_plans SET status='pending', note=NULL WHERE due_at='2026-10-31 13:30:00'")
        r = W.decide(c, conv, last_real2, TOOLS, now="2026-10-31 13:31:00", s=SP)
        check("到点了：醒，说明里写着约的时候写的是什么", r["wake"] and r["kind"] == "plan" and "明天几点出门" in r["note"], r)
        check("约定标成醒过了", W.get_plan(c, r["plan_id"])["status"] == "done" and W.get_plan(c, r["plan_id"])["run_id"] == r["run_id"])
        check("说明里写着下一个约好的时间", "明天 08:30" in r["note"], r["note"])
        W.finish(c, r["run_id"], "silent", now="2026-10-31 13:31:30")
        r = W.decide(c, conv, "2026-11-01 00:25:00", TOOLS, now="2026-11-01 00:31:00", s=SP)
        check("到点时正在聊天：不单独醒，记下原因", not r["wake"])
        p = [x for x in c.execute("SELECT status, note FROM wake_plans WHERE due_at='2026-11-01 00:30:00'")][0]
        check("…原因是正在聊天", p[0] == "skipped" and "正在聊天" in p[1], p)
        tn = W.turn_note(c, conv, now="2026-11-01 00:40:00")
        check("下次聊天的附注里告诉 ta 约的那次没醒", "你约的 08:30" in tn["note"] and "正在聊天" in tn["note"], tn["note"])
        W.seen(c, tn["runs"], tn["plans"])

        print("\n[7] 上限、夜里、静默档")
        c.execute("DELETE FROM wake_runs")
        c.execute("DELETE FROM wake_state")
        S2 = base_settings(ST, day_max=1, min_gap_minutes=10)
        r = W.decide(c, conv, "2026-11-01 01:00:00", TOOLS, now="2026-11-01 03:00:00", s=S2, rng=lambda: 0.0)
        W.finish(c, r["run_id"], "silent", now="2026-11-01 03:00:10")
        r = W.decide(c, conv, "2026-11-01 01:00:00", TOOLS, now="2026-11-01 04:00:00", s=S2, rng=lambda: 0.0)
        check("白天到上限：不醒", not r["wake"] and "上限" in r["reason"], r)
        r = W.decide(c, conv, "2026-11-01 13:00:00", TOOLS, now="2026-11-01 16:30:00", s=S2, rng=lambda: 0.0)
        check("夜里概率是 0：不随机醒", not r["wake"] and "夜里不随机醒" in r["reason"], r)
        r = W.decide(c, conv, "2026-11-01 13:00:00", TOOLS, now="2026-11-01 16:30:00", s=base_settings(ST, night_prob=0.5), rng=lambda: 0.1)
        check("夜里开了概率：单独计数（白天用满了也能醒）", r["wake"], r)
        W.finish(c, r["run_id"], "silent", now="2026-11-01 16:30:10")
        check("静默档：说明里写着话不会发出去", "不会发给对方" in W.wake_note(c, conv, "random", None, None, TOOLS, "2026-11-01 17:00:00",
                                                                     base_settings(ST, speak=False)))
        check("切分：［不说］→ 不说，前面写的留着", W.split_reply("想阿星了。［不说］") == (False, "", "想阿星了。"))
        check("切分：（不说）也认", W.split_reply("（不说）")[0] is False)
        check("切分：正常说话", W.split_reply(" 早呀 ") == (True, "早呀", ""))
        check("切分：静默档里说的也不发", W.split_reply("早呀", speak=False) == (False, "", "早呀"))

        print("\n[8] Dashboard 试一次、只看不动、网关重启后没有上下文")
        c.execute("DELETE FROM wake_runs")
        W.state_set(c, "test_requested", "2026-11-02 01:00:00")
        r = W.decide(c, conv, None, TOOLS, has_context=False, now="2026-11-02 01:00:30", s=S)
        check("没有上下文：试不了，说清楚", not r["wake"] and "先聊一句" in r["reason"], r)
        peek = W.decide(c, conv, "2026-11-02 00:00:00", TOOLS, now="2026-11-02 01:00:40", s=S, dry=True)
        check("只看不动：说马上试，不记任何东西", peek["reason"] == "马上就试一次" and c.execute("SELECT COUNT(*) FROM wake_runs").fetchone()[0] == 0)
        r = W.decide(c, conv, "2026-11-02 00:00:00", TOOLS, now="2026-11-02 01:00:50", s=S)
        check("试一次：醒（不算进上限）", r["wake"] and r["kind"] == "test" and "Dashboard" in r["note"]
              and W.used_in_part(c, "2026-11-02 00:00:00") == 0, r)
        W.finish(c, r["run_id"], "silent", now="2026-11-02 01:01:00")
        check("试过以后不再试", not W.decide(c, conv, "2026-11-02 00:00:00", TOOLS, now="2026-11-02 01:02:00", s=S, rng=lambda: 0.9)["wake"])
        c.execute("UPDATE wake_runs SET outcome='running', finished_at=NULL WHERE id=?", (r["run_id"],))
        W.decide(c, conv, "2026-11-02 00:00:00", TOOLS, now="2026-11-02 01:30:00", s=S, rng=lambda: 0.9)
        check("中途丢了的（网关重启）：过 15 分钟记成出错", W.get_run(c, r["run_id"])["outcome"] == "error")
        ov = W.overview(c, now="2026-11-02 01:30:00", s=S)
        check("Dashboard 总览", ov["status"] == "ok" and len(ov["runs"]) >= 1 and "peek" in ov)
        check("推送：Dashboard 的地址 + 前端登记的", W.push_targets(c, base_settings(ST, push_url="https://ntfy.sh/x")) == [{"url": "https://ntfy.sh/x", "format": "ntfy"}])
        W.register_push(c, "https://push.example.org/up/abc")
        check("前端登记的地址也推", W.push_targets(c, S) == [{"url": "https://push.example.org/up/abc", "format": "json"}])
        check("登记 http 不行", W.register_push(c, "http://x")["status"] == "error")
        W.register_push(c, "https://push.example.org/up/abc", remove=True)
        check("取消登记", W.push_targets(c, S) == [])

        print("\n[9] 删了的对话：不会被醒来的话「救活」；删对话时醒来记录一起删")
        conv2 = "cv-2"
        x = add_msg(c, conv2, 0, "user", "嗨", "2026-11-02 02:00:00")
        rid = W.start_run(c, conv2, "random", None, "2026-11-02 02:00:00", "2026-11-02 05:00:00")
        W.finish(c, rid, "spoke", text="在吗", now="2026-11-02 05:00:10")
        c.execute("UPDATE l0_messages SET status='superseded' WHERE conv_id=?", (conv2,))
        W.ack(c, [rid])
        check("对话删了：不插回去", active(c, conv2) == [])
        W.forget_conv(c, conv2)
        check("删对话：醒来记录一起删", W.get_run(c, rid) is None)

        print("\n[10] 前端把相邻两条合成一条发来：存 L0 时拆回库里那两行（不会当成改过）")
        conv3 = "cv-3"
        add_msg(c, conv3, 0, "user", "我去洗澡", "2026-11-02 10:00:00")
        add_msg(c, conv3, 1, "assistant", "去吧", "2026-11-02 10:00:05")
        add_msg(c, conv3, 2, "assistant", "洗完了吗？水温还好吗", "2026-11-02 12:30:00", client="pluvia-wake")
        window = [{"role": "user", "content": "我去洗澡"}, {"role": "assistant", "content": "去吧\n\n洗完了吗？水温还好吗"},
                  {"role": "user", "content": "洗完啦"}, {"role": "assistant", "content": "那就好"}]
        st = LM.merge(c, conv3, "pluvia", window, now="2026-11-02 13:00:00")
        rows = active(c, conv3)
        check("没有作废任何一行，新的两条接在后面", st["superseded"] == [] and st["saved"] == 2
              and [x[1] for x in rows] == ["我去洗澡", "去吧", "洗完了吗？水温还好吗", "洗完啦", "那就好"], (st, rows))
        check("被作废的一行都没有", c.execute("SELECT COUNT(*) FROM l0_messages WHERE conv_id=? AND status!='active'", (conv3,)).fetchone()[0] == 0)
        st = LM.merge(c, conv3, "pluvia", window, now="2026-11-02 13:01:00")
        check("再发一遍同样的窗口：什么都不动", st["saved"] == 0 and st["superseded"] == [] and len(active(c, conv3)) == 5)
        check("拆：只拆完全一样的", LM.split_merged([(1, "assistant", "去吧"), (2, "assistant", "洗完了吗")],
                                              [{"role": "assistant", "content": "去吧 洗完了"}]) == [{"role": "assistant", "content": "去吧 洗完了"}])

        print("\n[11] 窗口附注对齐：合成的一条对到库里连着的最后一行")
        hist, _ = WN.history_rows(c, conv3)
        ents = [{"r": "u" if m["role"] == "user" else "a", "h": WN.hash_text(m["role"], m["content"])} for m in window[:3]]
        pairs = WN.align(hist, ents)
        check("合成的那条对到醒来说的那行（后面「洗完啦」离它多久按它算）", pairs == {0: 0, 1: 2, 2: 3}, pairs)
        out = WN.compose(c, conv3, ents, now="2026-11-02 13:00:00",
                         settings={"notes": True, "bridge": False, "prev_window": False, "gap_hours": 0.4, "bridge_max_chars": 300})
        gap_note = [n for n in out["notes"] if n["i"] == 2]
        check("「洗完啦」前面的附注按醒来那行的时间算（20:30 → 21:00）", gap_note and "20:30" in gap_note[0]["text"], out["notes"])

        print("\n[12] 醒来记录给前端的一小行（不进聊天）")
        c.execute("DELETE FROM wake_runs")
        r1 = W.start_run(c, conv3, "random", None, "2026-11-02 10:00:05", "2026-11-02 13:05:00")
        W.finish(c, r1, "silent", aside="先不吵阿星", thinking="这会儿可能在忙", tools=["recall", "jot", "recall"], now="2026-11-02 13:05:20")
        pid = W.plan_act(c, "set", in_minutes=30, reason="问问阿星吃饭没", now="2026-11-02 13:06:00", s=S)["plan"]["id"]
        r2 = W.start_run(c, conv3, "plan", pid, "2026-11-02 10:00:05", "2026-11-02 13:36:00")
        W.finish(c, r2, "spoke", text="吃饭了吗", now="2026-11-02 13:36:10")
        r3 = W.start_run(c, conv3, "random", None, "2026-11-02 10:00:05", "2026-11-02 14:00:00")
        W.finish(c, r3, "error", note="overloaded", now="2026-11-02 14:00:05")
        ev = W.events(c, conv3)
        check("只列说了话的和没说话的（出错的不列）", [e["id"] for e in ev] == [r1, r2], ev)
        check("一小行：时间、怎么醒的、说没说、做了什么", ev[0]["line"] == "21:05 醒来了一下（随机醒来）· 没说话 · 翻了翻记忆、在小本子上记了一笔", ev[0]["line"])
        check("说了话的：带上消息 id（和待取里的是同一条）", ev[1]["line"].endswith("说了一句") and ev[1]["message_id"] == r2 and ev[1]["kind_label"] == "ta 约的")
        check("点开能看：约的时候写的、没发出去的话、想了什么", ev[1]["detail"]["plan_reason"] == "问问阿星吃饭没"
              and ev[0]["detail"]["aside"] == "先不吵阿星" and ev[0]["detail"]["thinking"] == "这会儿可能在忙")
        check("since_id：只要后面的", [e["id"] for e in W.events(c, conv3, since_id=r1)] == [r2])
        note = W.wake_note(c, conv3, "random", None, None, TOOLS, "2026-11-02 15:00:00", S)
        check("醒来说明照实说：醒来记录对方能看到，［不说］旁边写的也会记下", "对方能看到" in note and "旁边的话" in note, note)
        check("约时间的回复照实说：约定不发给对方，但记在醒来记录里", "醒来记录" in W.plan_act(c, "set", in_minutes=90, now="2026-11-02 15:00:00", s=S)["message"])

        print("\n[13] 窗口详情（给前端的换窗详情页）")
        old_ids = []
        for k in range(10):
            old_ids.append(add_msg(c, "cv-old", k, "user" if k % 2 == 0 else "assistant", f"旧对话第{k}句", f"2026-11-01 0{k}:00:00"))
        c.execute("INSERT INTO conv_branches (conv_id, parent_conv_id, parent_turn_id, prefix, shared, created_at, kind) VALUES (?, ?, NULL, ?, 4, ?, 'forged')",
                  ("cv-new", "cv-old", json.dumps([["h", "cv-old", old_ids[i]] for i in (1, 2, 6, 7)]), "2026-11-02 01:00:00"))
        c.execute("INSERT INTO conv_windows (conv_id, kind, prev_conv_id, created_at) VALUES ('cv-new', 'forged', 'cv-old', '2026-11-02 01:00:00')")
        n1 = add_msg(c, "cv-new", 0, "user", "新窗口第一句", "2026-11-02 01:00:00")
        add_msg(c, "cv-new", 1, "assistant", "嗯", "2026-11-02 01:00:05")
        c.execute("INSERT INTO window_state (conv_id, first_row_id, sig, note, pending, updated_at) VALUES ('cv-new', ?, 'x', '［记忆系统附注｜下面几段是挑出来带过来的］', 0, '2026-11-02 01:00:10')",
                  (old_ids[1],))
        d = WN.detail(c, "cv-new", now="2026-11-02 02:00:00")
        check("从哪来：挑选式搬家，来自旧对话", d["kind"] == "forged" and d["kind_label"].startswith("挑选式搬家") and d["from_conversation_id"] == "cv-old", d)
        check("带过来的两段：每段几条、时间、开头结尾一句；第二段前面省略了 3 条",
              [(x["n"], x["skipped_before"]) for x in d["carried"]] == [(2, 0), (2, 3)] and d["carried"][1]["first"]["text"] == "旧对话第6句", d["carried"])
        check("窗口状态和开头那行附注", d["window"]["from_ts"] == "2026-11-01 01:00:00" and "挑出来" in d["start_note"] and d["messages"] == 2)
        check("一条线上的前几个窗口", [x["conversation_id"] for x in d["chain"]] == ["cv-old"] and d["chain"][0]["messages"] == 10, d["chain"])
        c.commit()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
