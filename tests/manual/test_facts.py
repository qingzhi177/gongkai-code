"""事实层（app/pipeline/facts.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。对话都是编的。

运行：python3 tests/manual/test_facts.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="factstest_"))
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
from app.pipeline import facts as F  # noqa: E402
from app.pipeline import state as S  # noqa: E402

PASS = FAIL = 0
FILL = "，我一直都在这里陪着你，慢慢说就好，不着急，想说什么都可以告诉我。"


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
        r = self.replies.pop(0) if self.replies else '{"facts": []}'
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


def day_chat(day, conv, msgs, hour=21):
    """一天的一段对话（北京时间 hour 点开始，每条隔一分钟），并标记这天已经整理过"""
    for i, (role, content) in enumerate(msgs):
        ts = f"{day} {hour - 8:02d}:{i:02d}:00"
        run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status) VALUES (?, ?, ?, ?, ?, 'pluvia', 'active')",
            (conv, i, role, content, ts))
    run("INSERT OR REPLACE INTO settle_days (day, status, last_l0_id, messages, sessions, episodes, detail, version, settled_at) "
        "VALUES (?, 'done', 0, 0, 1, 0, '[]', 1, ?)", (day, day))


def fact(key, status="current"):
    rows = q("SELECT id, value, status, certainty, valid_from, valid_to, superseded_by, confirm_count, edited FROM facts "
             "WHERE fact_key=? AND status=? ORDER BY id", (key, status))
    return [dict(zip(("id", "value", "status", "certainty", "valid_from", "valid_to", "superseded_by", "confirm_count", "edited"), r))
            for r in rows]


def scan(replies, limit=10):
    llm = FakeLLM(replies)
    return asyncio.run(F.scan(llm_fn=llm, limit=limit)), llm


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)

        print("\n[1] 第一次扫：记下事实（原话要对得上、key 要写对）")
        day_chat("2026-05-20", "A", [("user", "我在杭州读大二，学的是园艺" + FILL), ("assistant", "园艺好浪漫呀" + FILL),
                                     ("user", "家里的猫叫团子，三岁了" + FILL), ("assistant", "团子这个名字好可爱" + FILL)])
        r, llm = scan([{"facts": [
            {"key": "user.city", "label": "住在哪个城市", "value": "杭州", "certainty": "explicit", "lines": [1], "quote": "我在杭州读大二"},
            {"key": "user.major", "label": "学的专业", "value": "园艺", "certainty": "explicit", "lines": [1], "quote": "学的是园艺"},
            {"key": "user.pet.cat.name", "label": "家里猫的名字", "value": "团子", "certainty": "explicit", "lines": [3], "quote": "家里的猫叫团子"},
            {"key": "User City!", "label": "坏的 key", "value": "x", "lines": [1], "quote": "我在杭州"},
            {"key": "user.school", "label": "学校", "value": "某大学", "certainty": "explicit", "lines": [1], "quote": "这句话不存在"}]}])
        check("提示词里：还没有事实、带对话时间", "（还没有）" in llm.prompts[0] and "2026-05-20 21:00" in llm.prompts[0], llm.prompts[0][-500:])
        check("记下三条；key 写法不对、原话对不上的不收", sorted(r["created"]) == sorted(["住在哪个城市：杭州", "学的专业：园艺", "家里猫的名字：团子"])
              and any("key 写法不对" in n for n in r["notes"]) and any("原话对不上" in n for n in r["notes"]), r)
        c = fact("user.city")[0]
        check("从出处那条消息的时间起算，明确说的", c["valid_from"] == "2026-05-20 13:00:00" and c["certainty"] == "explicit", c)
        check("每块只扫一次", q("SELECT COUNT(*) FROM facts_scans")[0][0] == 1 and F.pending_chunks() == 0)

        print("\n[2] 后来：一样的算又说到；明确说变了就换；随口一提标「可能变了」")
        day_chat("2026-06-10", "A", [("user", "团子今天又把花盆打翻了" + FILL), ("assistant", "团子真是调皮" + FILL),
                                     ("user", "我下个月可能要去上海实习吧，还没定" + FILL), ("assistant", "上海也不错呀" + FILL)])
        r, llm = scan([{"facts": [
            {"key": "user.pet.cat.name", "label": "家里猫的名字", "value": "团子", "certainty": "explicit", "lines": [1], "quote": "团子今天"},
            {"key": "user.city", "label": "住在哪个城市", "value": "上海", "certainty": "vague", "lines": [3], "quote": "可能要去上海实习"}]}])
        check("提示词里有之前记下的事实（带 key 和起始日期）", "家里猫的名字（user.pet.cat.name）：团子（2026-05-20 起）" in llm.prompts[0], llm.prompts[0][:600])
        check("一样的：又说到一次", r["confirmed"] == 1 and fact("user.pet.cat.name")[0]["confirm_count"] == 1, r)
        check("随口一提的不一样：不换，记一条「可能变了」", fact("user.city")[0]["value"] == "杭州" and fact("user.city", "disputed")[0]["value"] == "上海"
              and r["disputed"], r)
        lines = F.profile_lines()
        check("档案预览：分组列出，可能变了的带一句提示", "关于你：" in lines and any("住在哪个城市：杭州" in x and "可能变了" in x and "上海" in x for x in lines), lines)
        day_chat("2026-07-02", "A", [("user", "我搬到上海啦，开始实习了" + FILL), ("assistant", "恭喜呀" + FILL),
                                     ("user", "新家离公司很近" + FILL), ("assistant", "那太好了" + FILL)])
        r, _ = scan([{"facts": [{"key": "user.city", "label": "住在哪个城市", "value": "上海", "certainty": "explicit", "lines": [1], "quote": "我搬到上海啦"}]}])
        old, new = fact("user.city", "superseded"), fact("user.city")
        check("明确说了：换成新的，旧的留在「以前的」里（记下到什么时候为止）", new[0]["value"] == "上海" and old[0]["value"] == "杭州"
              and old[0]["valid_to"] == "2026-07-02 13:00:00" and old[0]["superseded_by"] == new[0]["id"], (old, new))
        check("之前「可能变了」的那条也了结了", not fact("user.city", "disputed") and len(old) == 2, old)

        print("\n[3] 补扫更早的对话：旧的说法不会盖掉后来的")
        day_chat("2026-05-25", "B", [("user", "其实我老家在苏州" + FILL), ("assistant", "苏州很美" + FILL),
                                     ("user", "我在杭州上学" + FILL), ("assistant", "嗯嗯" + FILL)])
        r, _ = scan([{"facts": [{"key": "user.city", "label": "住在哪个城市", "value": "杭州", "certainty": "explicit", "lines": [3], "quote": "我在杭州上学"},
                                {"key": "user.hometown", "label": "老家", "value": "苏州", "certainty": "explicit", "lines": [1], "quote": "老家在苏州"}]}])
        check("比现在的值旧、而且以前的值里已经有了：不重复记，现在的还是上海", fact("user.city")[0]["value"] == "上海" and len(fact("user.city", "superseded")) == 2
              and "住在哪个城市：杭州" not in r["created"], r)
        check("这段里新的事实照常记下", fact("user.hometown")[0]["value"] == "苏州")

        print("\n[4] 你在 Dashboard 上的操作")
        hid = fact("user.hometown")[0]["id"]
        F.edit(hid, value="苏州吴江")
        check("改值：记下你改过", fact("user.hometown")[0]["value"] == "苏州吴江" and fact("user.hometown")[0]["edited"] == 1)
        conn = config.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            act = F.record(conn, "user.hometown", "老家", "无锡", "explicit", None, "2026-08-01 12:00:00", "老家在无锡")
            conn.commit()
        finally:
            conn.close()
        check("改过的事实：明确说了不一样的也不自动换，只标「可能变了」", act[0] == "disputed" and fact("user.hometown")[0]["value"] == "苏州吴江", act)
        did = fact("user.hometown", "disputed")[0]["id"]
        F.confirm(did)
        check("确认「可能变了」的：它成为现在的，原来的变成以前的", fact("user.hometown")[0]["value"] == "无锡"
              and fact("user.hometown", "superseded")[0]["value"] == "苏州吴江")
        F.reject(fact("user.hometown")[0]["id"])
        check("现在的标「不对」：恢复成被它换掉的那个", fact("user.hometown")[0]["value"] == "苏州吴江" and fact("user.hometown", "rejected")[0]["value"] == "无锡")
        conn = config.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            act = F.record(conn, "user.hometown", "老家", "无锡", "explicit", None, "2026-08-05 12:00:00", "老家无锡")
            conn.commit()
        finally:
            conn.close()
        check("标过不对的值：以后扫到一样的不再记", act[0] == "skipped" and not fact("user.hometown", "disputed"), act)
        F.restore(fact("user.hometown", "rejected")[0]["id"])
        check("恢复：已经有现在的值，恢复成以前的", fact("user.hometown", "superseded")[-1]["value"] == "无锡")
        fid = F.add("us", "你叫我什么", "小雨")
        check("自己加一条：生成 key，记成你加的", q("SELECT fact_key, source, edited, status FROM facts WHERE id=?", (fid,))[0][1:] == ("dashboard", 1, "current")
              and q("SELECT fact_key FROM facts WHERE id=?", (fid,))[0][0].startswith("us.custom.k"))
        for bad in (lambda: F.add("cat", "a", "b"), lambda: F.add("user", "", "b"), lambda: F.edit(fid, value="  "), lambda: F.confirm(fid)):
            try:
                bad()
                check("不对的操作报错", False)
            except ValueError:
                pass
        check("不对的操作报错（关于谁不对、没写、改成空、确认不是「可能变了」的）", True)
        d = F.detail(hid)
        check("经过：记下、改、被换掉、恢复；同一个 key 的各个值都列出来", [e["event"] for e in d["events"]] == ["created", "edited", "superseded", "restored"]
              and sorted(v["value"] for v in d["versions"]) == ["无锡", "苏州吴江"], ([e["event"] for e in d["events"]], d["versions"]))

        print("\n[4b] 列了好几个的值（称呼、喜好）")
        conn = config.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            acts = [F.record(conn, "us.nickname.for_ai", "你叫我什么", v, "explicit", None, ts, "q")[0] for v, ts in (
                ("阿星、星星", "2026-06-01 12:00:00"),
                ("星星", "2026-06-02 12:00:00"),                  # 只提到其中一个
                ("阿星、星星、小星", "2026-06-03 12:00:00"),      # 多了一个新叫法（和原来的很像，以前会被当成一样）
                ("小星、阿星", "2026-06-04 12:00:00"))]
            conn.commit()
        finally:
            conn.close()
        check("只提到其中一个：算又说到一次，不换", acts[1] == "confirmed", acts)
        check("多了一个新叫法：换成合在一起的新值", acts[2] == "superseded" and fact("us.nickname.for_ai")[0]["value"] == "阿星、星星、小星", acts)
        check("顺序不一样、少写一个：算一样", acts[3] == "confirmed", acts)
        check("单个的值照常按像不像比", F.same_value("很喜欢你", "喜欢你") and not F.same_value("上海", "杭州"))

        print("\n[5] 档案预览：轻放的话题不列、有长度上限")
        S.add_policy("老家的事", ["老家"], "excluded")
        lines = F.profile_lines()
        check("碰到「轻放」话题关键词的不列", not any("老家" in x for x in lines) and any("住在哪个城市：上海" in x for x in lines), lines)
        check("长度上限", sum(len(x) for x in F.profile_lines(max_chars=30)) <= 40)
        ov = F.overview()
        check("总览：按关于谁分组、带以前的值、可能变了的带现在的值", "user" in ov["groups"] and "us" in ov["groups"]
              and any(x["fact_key"] == "user.city" and len(x["history"]) == 2 for x in ov["groups"]["user"]), list(ov["groups"]))

        print("\n[6] 扫描任务")
        day_chat("2026-08-10", "C", [("user", "我生日是六月十二号哦" + FILL), ("assistant", "记住啦" + FILL), ("user", "嗯" + FILL), ("assistant", "好" + FILL)])
        day_chat("2026-08-11", "C", [("user", "今天好累" + FILL), ("assistant", "抱抱" + FILL), ("user", "嗯嗯" + FILL), ("assistant", "早点睡" + FILL)])
        r, _ = scan([{"facts": [{"key": "user.birthday", "label": "生日", "value": "6 月 12 日", "certainty": "explicit", "lines": [1], "quote": "生日是六月十二号"}]}], limit=1)
        check("一次扫一块、还有剩下的：再投一个任务", r["chunks"] == 1 and r["more"] is True
              and q("SELECT COUNT(*) FROM pipeline_jobs WHERE kind='facts_scan' AND status='queued'")[0][0] == 1, r)
        try:
            asyncio.run(F.scan(llm_fn=FakeLLM([None])))
            check("模型没返回：报错（任务会重试），这块不算扫过", False)
        except RuntimeError:
            check("模型没返回：报错（任务会重试），这块不算扫过", F.pending_chunks() == 1)
        run("UPDATE pipeline_jobs SET status='dead', updated_at=? WHERE kind='facts_scan'", (F._now(),))
        check("刚失败过：自动投递先停一停；在 Dashboard 上点照样投", F.enqueue_scan() is False and F.enqueue_scan(source="dashboard") is True)
        check("设置有默认值", F.cfg()["enabled"] is True and app_settings.describe("facts")["items"][0]["key"] == "enabled")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
