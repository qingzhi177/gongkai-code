"""主题卷（app/pipeline/arcs.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_arcs.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="arctest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:                                    # 本地没装依赖时也能跑（这里用不到 .env）
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
try:
    import httpx  # noqa: F401
except ImportError:                                    # 设置模块会间接导入召回模块；这里不发网络请求
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Timeout=None, Limits=None)
try:
    import jieba  # noqa: F401
except ImportError:
    sys.modules["jieba"] = types.SimpleNamespace(setLogLevel=lambda *a: None, lcut=lambda t, **k: list(t),
                                                 cut_for_search=lambda t: list(t), initialize=lambda: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app.pipeline import arcs as A  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0) if self.replies else "{}"
        return (r if isinstance(r, str) or r is None else json.dumps(r, ensure_ascii=False)), "fake-model"


def q(sql, args=()):
    conn = config.get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def run(sql, args=()):
    conn = config.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_episode(day, title, body, l0_text, hour=22):
    """一条 L0 原文 + 一个引用它的片段，返回片段 id"""
    ts = f"{day} {hour - 8:02d}:00:00"             # 北京时间 hour 点
    mid = run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES ('c', 0, 'user', ?, ?, 'pluvia', 'active')",
              (l0_text, ts))
    return run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, salience, "
               "status, version, issues, model, prompt_version, created_at) VALUES (?, 'c', ?, ?, ?, ?, ?, '平静', ?, ?, 0.5, 'active', 1, "
               "'[]', 'fake', 'episode-v2', ?)", (day, ts, ts, title, body, body, json.dumps(["月亮"], ensure_ascii=False),
                                                   json.dumps([mid]), ts))


def jobs_of(kind):
    return [(json.loads(r[0]), r[1]) for r in q("SELECT payload, status FROM pipeline_jobs WHERE kind=? ORDER BY id", (kind,))]


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        now = "2026-10-20 02:00:00"
        e1 = add_episode("2026-10-16", "深夜的月亮", "那天凌晨你说「今晚的月亮好圆」，我们一起看了很久。", "今晚的月亮好圆啊")
        e2 = add_episode("2026-10-16", "巷口糖炒栗子", "你说巷口的糖炒栗子很好吃，我记下了。", "巷口的糖炒栗子超好吃")
        e3 = add_episode("2026-10-18", "睡前聊星空照片", "凌晨你说「好想再多聊一会儿」，我们又聊起你拍的星空照片。", "好想再多聊一会儿，星空照片还在相册里")

        print("\n[1] 归卷：提新主题 → 候选 → 待确认")
        llm = FakeLLM([{"assign": [{"episode": f"E{e2}", "arcs": []}],
                        "new": [{"title": "一起看星空", "theme": "我们一起看星空、聊月亮的那些夜晚",
                                 "episodes": [{"episode": f"E{e1}", "label": "凌晨一起看圆月"}, {"episode": f"E{e3}", "label": "聊起星空照片"}]}]}])
        r = asyncio.run(A.route_arcs(llm_fn=llm, now=now))
        arc = q("SELECT id, title, status FROM arcs")[0]
        check("第一次归卷：卷目录是空的，提示词里写「还没有」", "（还没有）" in llm.prompts[0] and f"[E{e1}]" in llm.prompts[0], llm.prompts[0][:300])
        check("新主题建成候选卷，两个片段进卷，三个片段都算归过", arc[1] == "一起看星空" and r["routed"] == 3 and r["new_arcs"] == [arc[0]]
              and q("SELECT COUNT(*) FROM arc_materials")[0][0] == 2 and q("SELECT COUNT(*) FROM arc_routing")[0][0] == 3, (arc, r))
        check("2 个片段、跨 2 天：还不够 3 个，留在候选", arc[2] == "candidate" and r["proposed"] == [], arc)
        aid = arc[0]
        e4 = add_episode("2026-10-19", "雨后的月亮", "那天晚上雨停了，你说「月亮出来了」，发来一张照片。", "雨停了，月亮出来了！")
        llm2 = FakeLLM([{"assign": [{"episode": f"E{e4}", "arcs": [{"arc": f"A{aid}", "label": "雨后看到月亮"}, {"arc": "A999", "label": "x"}]},
                                    {"episode": "E999", "arcs": [{"arc": f"A{aid}"}]}], "new": []}])
        r2 = asyncio.run(A.route_arcs(llm_fn=llm2, now=now))
        check("卷目录带着候选卷和最近的标签给模型看", f"[A{aid}]（候选）一起看星空" in llm2.prompts[0] and "聊起星空照片" in llm2.prompts[0], llm2.prompts[0][:400])
        check("够 3 个片段、跨 3 天：变成待确认", r2["proposed"] == [aid] and q("SELECT status FROM arcs WHERE id=?", (aid,))[0][0] == "proposed", r2)
        check("编号不对的卷和片段：跳过并记下", any("A999" in n for n in r2["notes"]) and any("E999" in n for n in r2["notes"]), r2["notes"])
        check("还没确认的卷不写", jobs_of("narrate_arc") == [] and r2["narrate"] == [])

        print("\n[2] 提了重复或拒绝过的主题")
        e5 = add_episode("2026-10-19", "又说起月亮", "你说今晚的月亮还是那么亮。", "今晚的月亮还是那么亮")
        bad = A.cfg()
        run("INSERT INTO arcs (title, theme, status, sections, version, needs_rebuild, created_at, updated_at) "
            "VALUES ('加班', '', 'rejected', '[]', 0, 0, ?, ?)", (now, now))
        llm3 = FakeLLM([{"assign": [], "new": [{"title": "一起看 星空", "theme": "重复", "episodes": [{"episode": f"E{e5}", "label": "又说起月亮"}]},
                                               {"title": "加班", "theme": "被拒绝过", "episodes": [{"episode": f"E{e5}", "label": "x"}]}]}])
        r3 = asyncio.run(A.route_arcs(llm_fn=llm3, now=now))
        check("拒绝过的主题写进提示词", "加班" in llm3.prompts[0].split("【新片段】")[0])
        check("和已有的卷同名（空格不算）：并进那个卷，不新建", r3["new_arcs"] == []
              and q("SELECT COUNT(*) FROM arc_materials WHERE arc_id=?", (aid,))[0][0] == 4, r3)
        check("拒绝过的主题：跳过", any("拒绝过" in n for n in r3["notes"]), r3["notes"])

        print("\n[3] 确认开卷、写第一版")
        try:
            A.reject(99999)
            check("没有这个卷：报错", False)
        except ValueError:
            check("没有这个卷：报错", True)
        A.confirm(aid, title="我们的月亮")
        a = A.get_arc(aid)
        check("确认（顺便改名）：进行中，投递写卷", a["status"] == "active" and a["title"] == "我们的月亮" and a["confirmed_at"]
              and jobs_of("narrate_arc") == [({"arc_id": aid}, "queued")], (a["status"], jobs_of("narrate_arc")))
        try:
            A.confirm(aid)
            check("已经确认过的再确认：报错", False)
        except ValueError:
            check("已经确认过的再确认：报错", True)
        good = {"sections": [{"title": "第一次一起看星空", "body": f"10 月 16 日凌晨，你说「今晚的月亮好圆」，我们一起看了很久。[#1]"
                                                         f"两天后你说「好想再多聊一会儿」，我们又聊起你拍的星空照片。[#2]"},
                             {"title": "雨后", "body": "10 月 19 日晚上雨停了，你说「月亮出来了」，还发来一张照片。[#3]你说今晚的月亮还是那么亮。[#4]"}],
                "summary": "从凌晨的圆月到雨后的照片，看月亮成了我们反复回到的小事。"}
        wl = FakeLLM([good])
        r4 = asyncio.run(A.narrate_arc(aid, llm_fn=wl))
        a = A.get_arc(aid)
        check("第一版：两个小节、出处对得上，version 1", r4["status"] == "ok" and r4["rebuild"] and a["version"] == 1
              and [s["title"] for s in a["sections"]] == ["第一次一起看星空", "雨后"] and a["sections"][0]["refs"] == [e1, e3]
              and "[#" not in a["sections"][0]["body"], (r4, a["sections"]))
        check("提示词：第一次写、片段按时间编号", "还没有，这是第一次写" in wl.prompts[0] and f"[#1] 2026年10月16日晚上《深夜的月亮》（凌晨一起看圆月）" in wl.prompts[0],
              wl.prompts[0][:600])
        check("摘要、历史版本、素材都标成写进去了", a["summary"].startswith("从凌晨的圆月") and a["versions"][0]["version"] == 1
              and all(m["narrated"] == 1 for m in a["materials"]), a)
        check("太长的小标题在标点处截断", A._short_title("旧照片里的月亮，然后是一个很长很长很长的夜晚的对话") == "旧照片里的月亮"
              and A._when("2026-10-18 17:36:00", "2026-10-18") == "2026年10月19日凌晨")
        check("出处详情带片段日期和标题", a["sections"][0]["sources"][0]["title"] == "深夜的月亮" and a["sections"][0]["sources"][0]["n"] == 1,
              a["sections"][0]["sources"])

        print("\n[4] 校验：原话对不上、没有出处")
        sec, iss = A.verify_section({"title": "测试", "body": "那天你说「月亮好方」，很奇怪。[#1]后来你说「今晚的月亮好圆」，我们看了很久很久很久。[#1]"},
                                    {1: e1}, {e1: "今晚的月亮好圆啊"})
        check("原话对不上的句子删掉，其余保留", sec is None or "好方" not in sec["body"], (sec, iss))
        check("…并记下问题", any("原话对不上" in x for x in iss), iss)
        sec2, iss2 = A.verify_section({"title": "测试", "body": "我们聊了很多很多关于月亮的事，彼此都很放松很开心，一直聊到很晚很晚。"}, {1: e1}, {e1: "x"})
        check("一个出处都没有：整节不要", sec2 is None and any("没有标" in x for x in iss2), iss2)
        check("摘要里引号对不上：不用这句摘要", A.clean_summary("你说「月亮好方」。", {e1: "今晚的月亮好圆啊"}) is None
              and A.clean_summary("看月亮是我们的小事。[#1]", {e1: "x"}) == "看月亮是我们的小事。")

        print("\n[5] 攒够新片段：追加一小节")
        e6 = add_episode("2026-10-20", "月食那晚", "那天晚上有月食，你说「月亮被咬了一口」。", "快看！月亮被咬了一口", hour=21)
        e7 = add_episode("2026-10-20", "月食之后", "月食过后你说「它又圆回来了」，我们都松了口气。", "它又圆回来了～", hour=23)
        rl = FakeLLM([{"assign": [{"episode": f"E{e6}", "arcs": [{"arc": f"A{aid}", "label": "一起看月食"}]},
                                  {"episode": f"E{e7}", "arcs": [{"arc": f"A{aid}", "label": "月亮又圆回来"}]}], "new": []}])
        r5 = asyncio.run(A.route_arcs(llm_fn=rl, now="2026-10-20 16:00:00"))
        check("2 个新片段进了进行中的卷：投递写卷", r5["narrate"] == [aid], r5)
        bad_then_good = FakeLLM([{"sections": [{"title": "月食", "body": "那天我们聊了很多很多，彼此都很开心，聊到很晚很晚才睡。"}], "summary": ""},
                                 {"sections": [{"title": "月食那晚", "body": "10 月 20 日晚上有月食，你说「月亮被咬了一口」。[#1]后来你说「它又圆回来了」。[#2]"}],
                                  "summary": "看月亮从圆月、雨后一直延续到一起看月食。"}])
        r6 = asyncio.run(A.narrate_arc(aid, llm_fn=bad_then_good))
        a = A.get_arc(aid)
        check("第一次写的没出处 → 带着问题重写一次", len(bad_then_good.prompts) == 2 and "没通过校验" in bad_then_good.prompts[1], r6)
        check("追加：前两节保留，加了第三节，version 2", r6["status"] == "ok" and not r6["rebuild"] and a["version"] == 2
              and [s["title"] for s in a["sections"]] == ["第一次一起看星空", "雨后", "月食那晚"] and a["sections"][2]["refs"] == [e6, e7], a["sections"])
        check("追加时提示词里有已经写好的部分", "【雨后】" in bad_then_good.prompts[0] and "目前的一句话" in bad_then_good.prompts[0])
        check("没有新素材：跳过", asyncio.run(A.narrate_arc(aid, llm_fn=FakeLLM([])))["status"] == "skipped")

        print("\n[6] 写不出来：保留旧版本")
        e8 = add_episode("2026-10-20", "月亮的歌", "你哼了一首关于月亮的歌。", "我在哼月亮代表我的心", hour=23)
        run("INSERT INTO arc_materials (arc_id, episode_id, label, ts, day, narrated, added_at) VALUES (?, ?, '哼月亮的歌', ?, '2026-10-20', 0, ?)",
            (aid, e8, "2026-10-20 15:30:00", now))
        nothing = {"sections": [{"title": "x", "body": "我们聊了很多很多很多，很开心很开心，很晚很晚很晚很晚很晚才睡觉。"}]}
        r7 = asyncio.run(A.narrate_arc(aid, llm_fn=FakeLLM([nothing, nothing])))
        a = A.get_arc(aid)
        check("两次都没通过：不发布、version 不变，素材标成「没写进去」", r7["status"] == "rejected" and a["version"] == 2
              and [m["narrated"] for m in a["materials"] if m["episode_id"] == e8] == [2] and a["issues"], (r7, a["version"]))
        check("「没写进去」的不算新素材，不会反复投递", aid not in A.schedule_narrations())

        print("\n[7] 片段重新整理（作废）以后")
        run("UPDATE episodes SET status='superseded' WHERE id=?", (e3,))
        conn = config.get_conn()
        removed = A.cleanup(conn)
        conn.commit()
        conn.close()
        a = A.get_arc(aid)
        check("作废片段的素材去掉，正文引用过它：要整篇重写", removed == {aid: [e3]} and a["needs_rebuild"] == 1
              and e3 not in [m["episode_id"] for m in a["materials"]], (removed, a["needs_rebuild"]))
        rb = FakeLLM([{"sections": [{"title": "圆月和雨后", "body": "10 月 16 日凌晨，你说「今晚的月亮好圆」。[#1]10 月 19 日雨停了，你说「月亮出来了」。[#2]"}],
                       "summary": "重写后的摘要，看月亮是我们反复回到的小事。"}])
        r8 = asyncio.run(A.narrate_arc(aid, llm_fn=rb))
        a = A.get_arc(aid)
        check("整篇重写：从头写、所有素材都给模型、不再引用作废的片段", r8["rebuild"] and "还没有，这是第一次写" in rb.prompts[0]
              and "睡前" not in rb.prompts[0] and "月亮的歌" in rb.prompts[0] and a["needs_rebuild"] == 0
              and e3 not in A._cited(a["sections"]) and a["version"] == 3, (r8, a["sections"]))

        print("\n[8] 休眠、拒绝、撤销、改名")
        conn = config.get_conn()
        A.refresh_status(conn, now="2026-12-31 00:00:00")
        conn.commit()
        conn.close()
        check("30 天以上没新素材：休眠", q("SELECT status FROM arcs WHERE id=?", (aid,))[0][0] == "dormant")
        conn = config.get_conn()
        A.refresh_status(conn, now="2026-10-25 00:00:00")
        conn.commit()
        conn.close()
        check("最近有素材：进行中", q("SELECT status FROM arcs WHERE id=?", (aid,))[0][0] == "active")
        cid = run("INSERT INTO arcs (title, theme, status, sections, version, needs_rebuild, created_at, updated_at) "
                  "VALUES ('糖炒栗子', '', 'candidate', '[]', 0, 0, ?, ?)", (now, now))
        run("INSERT INTO arc_materials (arc_id, episode_id, label, ts, day, narrated, added_at) VALUES (?, ?, '巷口', ?, '2026-10-16', 0, ?)",
            (cid, e2, now, now))
        A.reject(cid)
        check("拒绝候选", q("SELECT status FROM arcs WHERE id=?", (cid,))[0][0] == "rejected")
        A.restore(cid)
        check("撤销拒绝：回到候选", q("SELECT status FROM arcs WHERE id=?", (cid,))[0][0] == "candidate")
        A.rename(cid, title="巷口美食", theme="我们聊过的好吃的")
        check("改名", q("SELECT title, theme FROM arcs WHERE id=?", (cid,))[0] == ("巷口美食", "我们聊过的好吃的"))
        try:
            A.rename(cid, title="")
            check("空标题：报错", False)
        except ValueError:
            check("空标题：报错", True)
        ls = {x["id"]: x for x in A.list_arcs()}
        check("列表：片段数、天数、还没写进去的数", ls[aid]["episodes"] == 6 and ls[aid]["days"] == 3 and ls[cid]["episodes"] == 1, ls[aid])

        print("\n[9] 出错")
        add_episode("2026-10-21", "新的一天", "这是新的一天。", "新的一天")
        before = A.pending_route_count()
        try:
            asyncio.run(A.route_arcs(llm_fn=FakeLLM([None])))
            check("归卷时模型没返回：报错（任务会重试）", False)
        except RuntimeError:
            check("归卷时模型没返回：报错（任务会重试）", True)
        try:
            asyncio.run(A.route_arcs(llm_fn=FakeLLM(["我觉得都不用归卷"])))
            check("模型没输出 JSON：报错", False)
        except ValueError:
            check("模型没输出 JSON：报错，片段还算没归过", before >= 1 and A.pending_route_count() == before)
        check("设置有默认值", A.cfg()["propose_min_episodes"] == 3 and A.cfg()["narrate_min_new"] == 2 and bad["enabled"] is True)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
