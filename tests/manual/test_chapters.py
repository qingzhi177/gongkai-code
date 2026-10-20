"""章节（app/pipeline/chapters.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_chapters.py
"""
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="chaptertest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
try:
    import httpx  # noqa: F401
except ImportError:
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Timeout=None, Limits=None)
try:
    import jieba  # noqa: F401
except ImportError:
    sys.modules["jieba"] = types.SimpleNamespace(setLogLevel=lambda *a: None, lcut=lambda t, **k: list(t),
                                                 cut_for_search=lambda t: list(t), initialize=lambda: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app import settings as app_settings  # noqa: E402
from app.pipeline import chapters as C  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class SmartLLM:
    """按提示词的种类回答：写章节 → 引用这批的每个片段写一个小节；封存 → 标题和摘要；分章 → 按排好的回答。
    script 里排着的回答优先（字符串、dict 或 None）。"""
    def __init__(self, script=None, split=None, seal=None):
        self.script = list(script or [])
        self.split = list(split or [])
        self.seal = seal or {"title": "慢慢熟悉起来", "abstract": "这一章里我们从陌生到熟悉，聊了很多日常。"}
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.script:
            r = self.script.pop(0)
        elif "【这次要写进去的片段】" in prompt:
            n = len(re.findall(r"^\[#\d+\]", prompt.split("【这次要写进去的片段】")[1], re.M))
            body = "".join(f"那天我们聊了第 {k} 件事，聊得很开心，也说了很多别的。[#{k}]" for k in range(1, n + 1)) + "这些日子就这样慢慢过去了。"
            r = {"sections": [{"title": f"第 {len(self.prompts)} 次写的小节", "body": body}], "summary": f"这一章讲了 {n} 件事。"}
        elif "告一段落" in prompt:
            r = self.seal
        elif "分成章节" in prompt:
            r = self.split.pop(0) if self.split else {"chapters": []}
        else:
            r = None
        return (r if isinstance(r, str) or r is None else json.dumps(r, ensure_ascii=False)), "fake-model"

    def kinds(self):
        return ["write" if "【这次要写进去的片段】" in p else "seal" if "告一段落" in p else "split" if "分成章节" in p else "?" for p in self.prompts]


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


def episode(day, title, body=None, hour=13):
    ts = f"{day} {hour:02d}:00:00"
    mid = run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES ('A', 0, 'user', ?, ?, 'pluvia', 'active')",
              (f"{title}的原话", ts))
    body = body or f"我们聊了{title}。"
    return run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, status, version, created_at) "
               "VALUES (?, 'A', ?, ?, ?, ?, ?, '平静', '[]', ?, 'active', 1, ?)", (day, ts, ts, title, body, body, json.dumps([mid]), ts))


def ch(cid):
    conn = config.get_conn()
    try:
        return C._get(conn, cid)
    finally:
        conn.close()


def jobs(kind):
    return [r[0] for r in q("SELECT dedupe_key FROM pipeline_jobs WHERE kind=? AND status='queued' ORDER BY id", (kind,))]


def clear_jobs():
    run("DELETE FROM pipeline_jobs")


def write(cid, llm):
    return asyncio.run(C.write_chapter(cid, llm_fn=llm))


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)

        print("\n[1] 第一次：建第 1 章，把片段写进去")
        check("没有片段：不建章节", C.schedule()["changes"] == [] and not q("SELECT 1 FROM chapters"))
        e1 = episode("2026-10-16", "第一次聊天", "我们第一次聊天，你说「很高兴认识你」。")
        e2 = episode("2026-10-16", "一起听歌")
        e3 = episode("2026-10-18", "下雨天")
        r = C.schedule()
        c1 = q("SELECT id FROM chapters")[0][0]
        check("建了第 1 章（进行中，从最早的一天开始），投递了写章节", r["changes"] == ["created"] and ch(c1)["status"] == "current"
              and ch(c1)["start_day"] == "2026-10-16" and jobs("write_chapter") == [f"write_chapter:{c1}"], r)
        llm = SmartLLM()
        r = write(c1, llm)
        x = ch(c1)
        check("写进 3 个片段、一个小节、版本 1", r["written"] == 3 and len(x["sections"]) == 1 and x["version"] == 1 and x["summary"] == "这一章讲了 3 件事。", r)
        check("提示词：第 1 章、开头、片段带日期和时段", "第 1 章（2026年10月16日至今）" in llm.prompts[0] and "（还没有，这是这一章的开头）" in llm.prompts[0]
              and "[#1] 2026年10月16日晚上《第一次聊天》" in llm.prompts[0], llm.prompts[0][:400])
        check("小节的出处是片段 id", x["sections"][0]["refs"] == [e1, e2, e3], x["sections"][0])
        check("写过的片段记下来了，留了一份历史", sorted(r[0] for r in q("SELECT episode_id FROM chapter_episodes WHERE chapter_id=? AND status=1", (c1,))) == [e1, e2, e3]
              and q("SELECT COUNT(*) FROM chapter_versions WHERE chapter_id=?", (c1,))[0][0] == 1)
        clear_jobs()
        check("没有新片段：不投递", C.schedule()["queued"] == [] and not C.pending_work())

        print("\n[2] 新的片段追加；没通过校验的跳过")
        e4 = episode("2026-10-19", "旅行清单")
        check("有新片段：投递", C.pending_work() and C.schedule()["queued"] == [f"write_chapter:{c1}"])
        llm = SmartLLM()
        r = write(c1, llm)
        check("追加一个小节，版本 2，提示词里有已经写好的部分", r["written"] == 1 and len(ch(c1)["sections"]) == 2 and ch(c1)["version"] == 2
              and "那天我们聊了第 1 件事" in llm.prompts[0].split("【这一章已经写好的部分】")[1], r)
        e5 = episode("2026-10-20", "编出来的事")
        bad = {"sections": [{"title": "乱写", "body": "你说「这句话不存在」。[#1] 我们去了月球。[#7]"}], "summary": "x"}
        r = write(c1, SmartLLM(script=[bad, bad]))
        check("两次都没通过：这批跳过（不卡住后面的），任务标成没通过", r["status"] == "rejected" and r["skipped"] == 1
              and q("SELECT status FROM chapter_episodes WHERE episode_id=?", (e5,))[0][0] == 2 and len(ch(c1)["sections"]) == 2, r)
        check("校验的原因记下来了", any("原话对不上" in i for i in ch(c1)["issues"]), ch(c1)["issues"])

        print("\n[3] 封存：这一章到这里")
        for name, fn in [("结束早于开始", lambda: C.seal(c1, "2026-10-01")), ("结束晚于今天", lambda: C.seal(c1, "2099-01-01")),
                         ("日期格式不对", lambda: C.seal(c1, "10月19日")), ("标题太长", lambda: C.seal(c1, None, "这是一个特别特别特别特别特别特别特别特别长的标题"))]:
            try:
                fn()
                check(f"{name}：报错", False)
            except ValueError:
                check(f"{name}：报错", True)
        clear_jobs()
        c2 = C.seal(c1, "2026-10-19")
        x1, x2 = ch(c1), ch(c2)
        check("第 1 章封存到 10 月 19 日；第 2 章从 10 月 20 日开始，进行中", x1["status"] == "sealed" and x1["end_day"] == "2026-10-19"
              and x2["status"] == "current" and x2["start_day"] == "2026-10-20" and x2["n"] == 2)
        check("10 月 20 日的片段不再算第 1 章的（从「写过的」里去掉）", not q("SELECT 1 FROM chapter_episodes WHERE chapter_id=? AND episode_id=?", (c1, e5)))
        check("两章都投递了写章节", sorted(jobs("write_chapter")) == sorted([f"write_chapter:{c1}", f"write_chapter:{c2}"]))
        try:
            C.seal(c1)
            check("已经封存的不能再封存", False)
        except ValueError:
            check("已经封存的不能再封存", True)
        r = write(c2, SmartLLM())
        wait_job = q("SELECT payload FROM pipeline_jobs WHERE dedupe_key=? AND status='queued'", (f"write_chapter:{c2}:wait",))
        check("新的一章先不写：等上一章写完、有了摘要（开头要接上上一章），过一会儿再试",
              r["status"] == "skipped" and not ch(c2)["sections"] and wait_job and json.loads(wait_job[0][0])["waited"] == 1, r)
        llm = SmartLLM()
        r = write(c1, llm)
        x1 = ch(c1)
        check("封存的章写完以后写标题和摘要（没起标题的话顺便起一个）", llm.kinds() == ["seal"] and x1["title"] == "慢慢熟悉起来"
              and x1["abstract"] == "这一章里我们从陌生到熟悉，聊了很多日常。" and x1["abstract_version"] == x1["version"], (llm.kinds(), x1))
        check("摘要的提示词里有这一章的正文和时间", "2026年10月16日到10月19日" in llm.prompts[0] and "那天我们聊了第 1 件事" in llm.prompts[0])
        llm = SmartLLM()
        r = write(c2, llm)
        check("新一章第一次写：先接上上一章", "上一章：第 1 章「慢慢熟悉起来」——这一章里我们从陌生到熟悉" in llm.prompts[0]
              and "这是新一章的开头" in llm.prompts[0] and r["written"] == 1, llm.prompts[0][:300])
        idx = C.index_text()
        check("章节目录", idx == "· 第 1 章「慢慢熟悉起来」（2026年10月16日到10月19日）：这一章里我们从陌生到熟悉，聊了很多日常。\n"
                               "· 第 2 章（2026年10月20日至今，进行中）：这一章讲了 1 件事。", idx)
        clear_jobs()
        check("都写完了：不投递", C.schedule()["queued"] == [])

        print("\n[4] 撤销封存、改标题")
        try:
            C.unseal(c2)
            check("进行中的不能撤销封存", False)
        except ValueError:
            check("进行中的不能撤销封存", True)
        C.unseal(c1)
        x1 = ch(c1)
        check("撤销封存：第 2 章并回来，第 1 章又是进行中", x1["status"] == "current" and x1["end_day"] is None
              and not q("SELECT 1 FROM chapters WHERE id=?", (c2,)) and len(q("SELECT 1 FROM chapters")) == 1)
        r = write(c1, SmartLLM())
        check("并回来的片段接着写进第 1 章", r["written"] == 1 and q("SELECT status FROM chapter_episodes WHERE chapter_id=? AND episode_id=?", (c1, e5))[0][0] == 1, r)
        late1 = episode("2026-10-19", "前一天补的")
        clear_jobs()
        r = C.schedule()
        check("补进来的片段只比已经写到的地方早一两天：接着往后写，不整章重写",
              "late" not in r["changes"] and ch(c1)["needs_rebuild"] == 0 and jobs("write_chapter") == [f"write_chapter:{c1}"], r)
        write(c1, SmartLLM())
        late2 = episode("2026-10-16", "中间补的一段")
        clear_jobs()
        r = C.schedule()
        check("补进来的片段比已经写到的地方早两天以上（比如导入了中间一段旧聊天）：整章重写，免得顺序乱掉",
              r["changes"] == ["late"] and ch(c1)["needs_rebuild"] == 1 and jobs("write_chapter") == [f"write_chapter:{c1}"], r)
        r = write(c1, SmartLLM())
        check("重写以后按日期顺序", r["rebuild"] and ch(c1)["sections"][0]["refs"][:3] == [e1, e2, late2], ch(c1)["sections"][0]["refs"])
        C.rename(c1, "开始")
        check("改标题", ch(c1)["title"] == "开始")
        C.rename(c1, "")
        check("标题清空", ch(c1)["title"] is None)

        print("\n[5] 片段作废、补上更早的片段（以后导入旧聊天）")
        run("UPDATE episodes SET status='superseded' WHERE id=?", (e3,))
        clear_jobs()
        C.schedule()
        check("引用过的片段作废了：整章重写", ch(c1)["needs_rebuild"] == 1 and jobs("write_chapter") == [f"write_chapter:{c1}"])
        llm = SmartLLM()
        r = write(c1, llm)
        x1 = ch(c1)
        check("整章重写：清空以后从头写（不含作废的片段）", r["rebuild"] and r["written"] == 6 and len(x1["sections"]) == 1
              and e3 not in x1["sections"][0]["refs"] and x1["needs_rebuild"] == 0, r)
        old = episode("2026-09-01", "更早的一天")
        clear_jobs()
        r = C.schedule()
        check("有比第一章还早的片段、第一章还在进行中：往前扩，整章重写", r["changes"] == ["extended"] and ch(c1)["start_day"] == "2026-09-01"
              and ch(c1)["needs_rebuild"] == 1, r)
        check("要从头写、又跨得很久：先投递分章提议，这一章先不写", r["waiting"] == "proposing" and jobs("propose_chapters") == ["propose_chapters"]
              and not jobs("write_chapter"), r)
        r = write(c1, SmartLLM())
        check("正在提议：写章节任务也先不写", r["status"] == "skipped" and ch(c1)["needs_rebuild"] == 1, r)
        check("总览里说明在等什么", C.overview()["waiting"] == C.WAITING_CN["proposing"])
        clear_jobs()
        pr = asyncio.run(C.propose(llm_fn=SmartLLM(split=[{"chapters": [{"title": "前", "start": "2026-09-01", "end": "2026-09-30"},
                                                                        {"title": "后", "start": "2026-10-16"}]}])))
        r = C.schedule()
        check("有等你确认的提议：先不写", pr["proposal"] == "pending" and r["waiting"] == "pending" and not jobs("write_chapter"), (pr, r))
        C.dismiss_proposal(pr["proposal_id"])
        check("点了「不用了」：马上开始写", jobs("write_chapter") == [f"write_chapter:{c1}"])
        write(c1, SmartLLM())
        check("重写以后从最早的一天开始", ch(c1)["sections"][0]["refs"][0] == old)
        C.seal(c1, "2026-10-19", "第一段")
        write(c1, SmartLLM())
        older = episode("2026-08-01", "再早一点")
        r = C.schedule()
        chs = q("SELECT n, start_day, end_day, status, title FROM chapters ORDER BY n")
        check("第一章已经封存了：在前面补一章（封存的，标题等写完再起），章号重新排",
              r["changes"] == ["prepended"] and chs[0][:4] == (1, "2026-08-01", "2026-08-31", "sealed") and chs[1][0] == 2 and chs[1][4] == "第一段", chs)

        print("\n[6] 分章提议")
        clear_jobs()
        run("DELETE FROM chapter_episodes")
        run("DELETE FROM chapter_versions")
        run("DELETE FROM chapters")
        run("UPDATE episodes SET status='superseded'")
        start = date(2026, 7, 1)
        for k in range(0, 40, 2):
            episode((start + timedelta(days=k)).isoformat(), f"第{k}天的事")
        C.schedule()
        cur = q("SELECT id FROM chapters WHERE status='current'")[0][0]
        app_settings.update("chapters", {"min_chapter_days": 30})
        check("这一章还不够长（不到每章最短天数的两倍）：不提议", asyncio.run(C.propose(llm_fn=SmartLLM()))["status"] == "skipped")
        app_settings.reset("chapters", ["min_chapter_days"])
        days = sorted(r[0] for r in q("SELECT DISTINCT day FROM episodes WHERE status='active'"))
        bad_split = {"chapters": [{"title": "开始", "start": "2026-07-01", "end": "2026-07-05"}, {"title": "后来", "start": "2026-07-15"}]}
        good_split = {"chapters": [{"title": "开始", "start": "2026-07-01", "end": "2026-07-13", "why": "刚认识"},
                                   {"title": "中间", "start": "2026-07-15", "end": "2026-07-27", "why": "一起做了一件事"},
                                   {"title": "现在", "start": "2026-07-29", "why": "最近"}]}
        llm = SmartLLM(split=[bad_split, good_split])
        r = asyncio.run(C.propose(llm_fn=llm))
        check("第一次的分法不合格（漏了日子、太短）→ 带着问题重新分 → 存成等你确认的提议", r["proposal"] == "pending" and r["chapters"] == ["开始", "中间", "现在"]
              and "上一次的分法有问题" in llm.prompts[1], r)
        check("分章的提示词：只列聊过天的日子，带日摘或片段标题", "2026-07-01（周三）：《第0天的事》" in llm.prompts[0] and "2026-07-02" not in llm.prompts[0]
              and "每章至少 7 天" in llm.prompts[0], llm.prompts[0][:600])
        pid = r["proposal_id"]
        edited = [{"title": "刚开始", "start": "2026-07-01", "end": "2026-07-13"}, {"title": "中间", "start": "2026-07-15", "end": "2026-07-27"},
                  {"title": "现在", "start": "2026-07-29", "end": None}]
        clear_jobs()
        ids = C.apply_proposal(pid, edited)
        chs = q("SELECT id, n, title, start_day, end_day, status, needs_rebuild FROM chapters ORDER BY n")
        check("确认分章（用你改过的标题）：前两章封存、最后一章进行中、章号连续", [(c[1], c[2], c[3], c[4], c[5]) for c in chs] == [
            (1, "刚开始", "2026-07-01", "2026-07-13", "sealed"), (2, "中间", "2026-07-15", "2026-07-27", "sealed"),
            (3, "现在", "2026-07-29", None, "current")] and chs[0][0] == cur and chs[0][6] == 1, chs)
        check("三章都投递了写章节", sorted(jobs("write_chapter")) == sorted(f"write_chapter:{i}" for i in ids))
        check("提议标成已确认", q("SELECT status FROM chapter_proposals WHERE id=?", (pid,))[0][0] == "applied")
        try:
            C.apply_proposal(pid)
            check("同一个提议不能确认两次", False)
        except ValueError:
            check("同一个提议不能确认两次", True)
        V = lambda items, mind=7: C.validate_split(items, days, mind)
        check("检查分法：中间漏了聊过天的日子", V([{"title": "a", "start": "2026-07-01", "end": "2026-07-13"}, {"title": "b", "start": "2026-07-17"}])[0] is None)
        check("检查分法：第一章要从第一天开始", V([{"title": "a", "start": "2026-07-03"}])[0] is None)
        check("检查分法：一章不到最短天数", V([{"title": "a", "start": "2026-07-01", "end": "2026-07-03"}, {"title": "b", "start": "2026-07-05"}])[0] is None)
        check("检查分法：最后一章开始的日子之后没有聊天", V([{"title": "a", "start": "2026-07-01", "end": "2026-08-07"}, {"title": "b", "start": "2026-08-09"}])[0] is None)
        check("检查分法：标题是空的", V([{"title": "", "start": "2026-07-01"}])[0] is None)
        check("检查分法：只分一章也行（最后一章不写 end）", V([{"title": "一章", "start": "2026-07-01", "end": "2026-08-01"}]) [0] == [
            {"title": "一章", "start": "2026-07-01", "end": None, "why": ""}])

        print("\n[7] 自动提议、看不出要分、不用了")
        cur3 = [c for c in chs if c[5] == "current"][0][0]
        check("上一章还没写：新的一章先等", write(cur3, SmartLLM())["status"] == "skipped")
        r = asyncio.run(C.write_chapter(cur3, llm_fn=SmartLLM(), waited=C.MAX_WAITS))
        check("等够次数了（上一章一直没写完）：不等了，照常写", r["status"] == "ok" and r["written"] > 0, r)
        secs = ch(cur3)["sections"]
        secs[0]["body"] = secs[0]["body"] + "我们又聊了很久。" * 80
        run("UPDATE chapters SET sections=? WHERE id=?", (json.dumps(secs, ensure_ascii=False), cur3))
        app_settings.update("chapters", {"seal_after_days": 7, "seal_min_chars": 500, "min_chapter_days": 3})
        clear_jobs()
        C.schedule()
        check("进行中的章跨得够久、写得够长：自动投递分章提议", jobs("propose_chapters") == ["propose_chapters"])
        r = asyncio.run(C.propose(llm_fn=SmartLLM(split=[{"chapters": [{"title": "现在", "start": "2026-07-29"}]}])))
        check("看不出要分：记一条「看不出要分章」，不用你确认", r["proposal"] == "nochange", r)
        clear_jobs()
        C.schedule()
        check("七天内提议过：不再自动提议", jobs("propose_chapters") == [])
        r = asyncio.run(C.propose(llm_fn=SmartLLM(split=[{"chapters": [{"title": "前", "start": "2026-07-29", "end": "2026-08-04"},
                                                                       {"title": "后", "start": "2026-08-06"}]}])))
        C.dismiss_proposal(r["proposal_id"])
        check("不用了", q("SELECT status FROM chapter_proposals WHERE id=?", (r["proposal_id"],))[0][0] == "dismissed")
        app_settings.reset("chapters", ["seal_after_days", "seal_min_chars", "min_chapter_days"])

        print("\n[8] 摘要写不成、一次写不完、出错")
        sealed1 = chs[0][0]
        r = write(sealed1, SmartLLM(seal={"title": "", "abstract": "你说「从来没说过的话」。"}))
        x = ch(sealed1)
        check("摘要两次都写不成：记下这一版试过了，不会反复重试", r.get("abstract_failed") and x["abstract_version"] == -x["version"] and not C.needs_abstract(x), r)
        clear_jobs()
        C.schedule()
        check("摘要试过了：不再投递这一章", f"write_chapter:{sealed1}" not in jobs("write_chapter"))
        C.WRITE_BATCH, C.MAX_BATCHES_PER_JOB = 2, 1
        sealed2 = chs[1][0]
        clear_jobs()
        run("INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, source, created_at, updated_at) "
            "VALUES ('write_chapter', ?, '{}', 'running', 1, 4, '2026-10-20 00:00:00', 'auto', '2026-10-20 00:00:00', '2026-10-20 00:00:00')",
            (f"write_chapter:{sealed2}",))
        r = write(sealed2, SmartLLM())
        check("一次最多写几批、还有剩下的：接着投递（自己正在跑也投得进去），先不写摘要", r["more"] and r["written"] == 2
              and jobs("write_chapter") == [f"write_chapter:{sealed2}:next"] and not r.get("abstract"), r)
        C.WRITE_BATCH, C.MAX_BATCHES_PER_JOB = 12, 8
        try:
            write(sealed2, SmartLLM(script=[None]))
            check("模型没返回：报错（任务会重试）", False)
        except RuntimeError:
            check("模型没返回：报错（任务会重试）", True)
        check("写到一半出错：已经写好的批次留着", len(ch(sealed2)["sections"]) == 1)
        r = write(sealed2, SmartLLM())
        check("重试以后接着写完，再写摘要", r["written"] == 5 and r.get("abstract") and ch(sealed2)["title"] == "中间", r)
        check("不存在的章：跳过", write(9999, SmartLLM())["status"] == "skipped")

        print("\n[9] 查看")
        ov = C.overview()
        check("总览：每一章的进度、进行中这章的正文、目录、提议", len(ov["chapters"]) == 3 and ov["current"]["sections"] and ov["index"].count("· 第") == 3
              and ov["proposals"] and ov["legacy"] is None and ov["enabled"] is True, ov["chapters"])
        d = C.get_chapter(sealed2)
        check("一章的详情：小节的出处（片段标题、日期）、历史版本", d["sections"][0]["sources"][0]["title"].startswith("第") and d["versions"] and d["range"], d["sections"][0]["sources"][:1])
        check("设置有默认值", C.cfg()["seal_after_days"] == 30 and C.cfg()["min_chapter_days"] == 7)

        print("\n[10] 导入旧聊天时：片段还在整理、提议任务失败、提议放久了")
        clear_jobs()
        J = ("INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, source, created_at, updated_at) "
             "VALUES (?, ?, '{}', ?, 1, 4, datetime('now'), 'auto', datetime('now'), datetime('now'))")
        run(J, ("settle_day", "settle_day:2026-08-20", "queued"))
        episode("2026-08-20", "导入的一天")
        r = C.schedule()
        check("每日片段还在排队整理：先不投递写章节", r["waiting"] == "settling" and r["queued"] == [], r)
        check("写章节任务也先不写", write(cur3, SmartLLM())["status"] == "skipped")
        check("总览里说明在等什么", C.overview()["waiting"] == C.WAITING_CN["settling"])
        run("DELETE FROM pipeline_jobs WHERE kind='settle_day'")
        r = C.schedule()
        check("整理完了：照常投递", r["queued"] == [f"write_chapter:{cur3}"] and r["waiting"] is None, r)
        clear_jobs()
        run("DELETE FROM chapter_proposals")
        run("UPDATE chapters SET needs_rebuild=1 WHERE id=?", (cur3,))
        episode("2026-09-05", "又一天")
        run(J, ("propose_chapters", "propose_chapters", "dead"))
        r = C.schedule()
        check("上一次提议任务刚失败过：不再投递提议，照常写", r["waiting"] is None and r["queued"] == [f"write_chapter:{cur3}"], r)
        clear_jobs()
        run("INSERT INTO chapter_proposals (chapter_id, chapters, status, created_at) VALUES (?, '[]', 'pending', datetime('now', '-4 days'))", (cur3,))
        r = C.schedule()
        check("等你确认的提议放了 3 天以上：先照现在的样子写（提议还留着）", r["waiting"] is None and r["queued"] == [f"write_chapter:{cur3}"]
              and "propose_chapters" not in r["queued"], r)
        clear_jobs()
        run("UPDATE chapter_proposals SET status='dismissed'")
        jid = run(J, ("propose_chapters", "propose_chapters", "running"))
        check("正在提议：先不写", C.schedule()["waiting"] == "proposing" and not jobs("write_chapter"))
        r = C.schedule(exclude_job=jid)
        check("提议任务自己跑完时投递：不算它自己", r["waiting"] is None and r["queued"] == [f"write_chapter:{cur3}"], r)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
