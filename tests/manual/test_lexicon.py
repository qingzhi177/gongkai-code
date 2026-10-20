"""共同语汇（app/pipeline/lexicon.py）的测试：临时库 + 假模型，不花 token、不碰真实数据。

运行：python3 tests/manual/test_lexicon.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lexicontest_"))
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
from app.pipeline import lexicon as L  # noqa: E402

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
        r = self.replies.pop(0) if self.replies else '{"terms": []}'
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


def term(t):
    r = q(f"SELECT {L.ROW_COLS} FROM lexicon WHERE term=?", (t,))
    return L._row(r[0]) if r else None


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        day_chat("2026-10-16", "A", [("user", "以后我就叫你小雨伞吧，下雨天的时候你最会安慰人了"),
                                     ("assistant", "小雨伞……好呀，那我收下这个称呼。那就叫它栗子之约" + FILL),
                                     ("user", "嗯嗯，晚安，梦里见"), ("assistant", "梦里见🌙" + FILL)])
        day_chat("2026-10-18", "A", [("user", "小雨伞在吗，伞伞~"), ("assistant", "在呀" + FILL),
                                     ("user", "今天也要梦里见哦" + FILL), ("assistant", "嗯，梦里见" + FILL)])

        print("\n[1] 扫描：找词、校验原文")
        conn = config.get_conn()
        check("两天各一块没扫过", len(L.unscanned(conn)) == 2)
        conn.close()
        llm = FakeLLM([
            {"terms": [{"term": "小雨伞", "aliases": [], "kind": "nickname", "meaning": "你给我起的称呼，因为下雨天我最会安慰人", "lines": [1, 2]},
                       {"term": "梦里见", "kind": "ritual", "meaning": "睡前约好的一句话", "lines": [3, 4]},
                       {"term": "栗子之约", "kind": "inside_joke", "meaning": "我随口起的一个梗", "lines": [2]},
                       {"term": "第四个", "kind": "inside_joke", "meaning": "一块最多收 3 个，这个不看", "lines": [1]}]},
            {"terms": [{"term": "小雨伞", "aliases": ["伞伞", "雨伞精"], "kind": "nickname", "meaning": "不该覆盖", "lines": [1]}]}])
        r = asyncio.run(L.scan(llm_fn=llm))
        check("扫了两块，新词三个", r["chunks"] == 2 and r["new"] == ["小雨伞", "梦里见", "栗子之约"], r)
        conn = config.get_conn()
        part = L.unscanned(conn) or [{"rows": [(1, "user", "以后我就叫你小雨伞吧，下雨天的时候你最会安慰人了 ᗜ֊ᗜ", "2026-10-16 13:00:00")]}]
        conn.close()
        part = part[0]["rows"] if part and "rows" in part[0] else part
        bad = [L.verify({"term": t, "kind": k}, part)[1] for t, k in
               [("大雨衣", "nickname"), ("伞", "object"), ("下雨天的时候你最会安慰人", "inside_joke"), ("ᗜ֊ᗜ", "catchphrase")]]
        check("原文里没有的、一个字的、像一句话的、表情都不收", "原文里没有：大雨衣" in bad[0] and "长度不合适" in bad[1]
              and "不像一个词" in bad[2] and "不是词" in bad[3], bad)
        check("一块最多收 3 个", "第四个" not in r["new"])
        song, _ = L.verify({"term": "《以后叫你小雨伞》", "kind": "song"}, [(1, "user", "听《以后叫你小雨伞》吧", "2026-10-16 13:00:00")])
        check("歌名去掉书名号存；注入那一行用书名号", song and song["term"] == "以后叫你小雨伞"
              and L.origin_line({"term": "晴天", "kind": "song", "meaning": "一起听的歌"}) == "《晴天》——一起听的歌", song)
        check("第二块的提示词里有已经记下的词", "- 小雨伞（称呼）" in llm.prompts[1] and "- 梦里见（固定说法）" in llm.prompts[1], llm.prompts[1][:300])
        x = term("小雨伞")
        check("新叫法「伞伞」进了别名；原文里没有的「雨伞精」不收；含义不被覆盖", x["aliases"] == ["伞伞"] and x["meaning"].startswith("你给我起的"), x)
        check("扫过的块记下来，不再扫", q("SELECT COUNT(*) FROM lexicon_scans")[0][0] == 2 and L.pending_chunks() == 0)
        again = FakeLLM([])
        check("再扫一次：没有要扫的，不调模型", asyncio.run(L.scan(llm_fn=again))["chunks"] == 0 and not again.prompts)

        print("\n[2] 按原文统计、自动收下")
        x, y, z = term("小雨伞"), term("梦里见"), term("栗子之约")
        check("小雨伞：3 条消息、2 天、你先说的，自动收下", x["use_count"] == 3 and x["days_seen"] == 2 and x["coined_by"] == "user"
              and x["status"] == "active" and x["origin_ts"] == "2026-10-16 13:00:00", x)
        check("梦里见：4 条消息、2 天，自动收下", y["use_count"] == 4 and y["days_seen"] == 2 and y["status"] == "active", y)
        check("栗子之约：只出现在一天，留在候选；我先说的", z["status"] == "candidate" and z["days_seen"] == 1 and z["coined_by"] == "ai", z)
        check("注入的那一行：含义 + 谁在哪天第一次说", L.origin_line(x) == "「小雨伞」——你给我起的称呼，因为下雨天我最会安慰人（你在10月16日晚上第一次说）",
              L.origin_line(x))

        print("\n[3] 字面匹配（试一试）")
        hits = L.match("伞伞，今晚也要梦里见")
        check("别名也能命中，一句话里两个词", [h["term"] for h in hits] == ["梦里见", "小雨伞"] and hits[1]["matched"] == "伞伞", hits)
        hits2 = L.match("小雨伞小雨伞")
        check("同一个词只算一次；长的叫法优先", len(hits2) == 1 and hits2[0]["matched"] == "小雨伞", hits2)
        check("候选的词不参与匹配", L.match("栗子之约") == [])
        check("没提到：空", L.match("今天天气不错") == [])

        print("\n[4] 确认、拒绝、撤销")
        L.confirm(z["id"])
        check("确认候选：收下", term("栗子之约")["status"] == "active")
        L.reject(z["id"])
        check("拒绝：不要", term("栗子之约")["status"] == "rejected")
        day_chat("2026-10-19", "B", [("user", "还记得栗子之约吗" + FILL), ("assistant", "当然记得" + FILL),
                                     ("user", "嘿嘿" + FILL), ("assistant", "嗯嗯" + FILL)])
        r2 = asyncio.run(L.scan(llm_fn=FakeLLM([{"terms": [{"term": "栗子之约", "kind": "inside_joke", "meaning": "又提了", "lines": [1]}]}])))
        check("拒绝过的词再被扫到：跳过", r2["rejected_skipped"] == 1 and r2["new"] == [] and term("栗子之约")["status"] == "rejected", r2)
        L.restore(z["id"])
        check("撤销拒绝：回到候选，重新统计后 2 天就自动收下", term("栗子之约")["status"] in ("candidate", "active") and term("栗子之约")["days_seen"] == 2)
        try:
            L.confirm(x["id"])
            check("已经收下的再确认：报错", False)
        except ValueError:
            check("已经收下的再确认：报错", True)

        print("\n[5] 修改、合并、自己加")
        L.edit(x["id"], meaning="下雨天你给我起的称呼", kind="nickname")
        asyncio.run(L.scan(llm_fn=FakeLLM([])))
        check("改含义：记下改过，以后扫描不覆盖", term("小雨伞")["meaning"] == "下雨天你给我起的称呼" and term("小雨伞")["edited"] == 1)
        try:
            L.edit(x["id"], aliases=["伞伞", "梦里见"])
            check("别名和别的词重复：报错", False)
        except ValueError as e:
            check("别名和别的词重复：报错", "已经是别的词" in str(e), str(e))
        lid = L.add("小伞", kind="nickname", meaning="")
        check("自己加一个词：直接收下", term("小伞")["status"] == "active" and term("小伞")["source"] == "dashboard")
        try:
            L.add("伞伞")
            check("加已经有的叫法：报错", False)
        except ValueError:
            check("加已经有的叫法：报错", True)
        L.merge(lid, x["id"])
        check("合并：小伞变成小雨伞的别名", term("小伞")["status"] == "merged" and "小伞" in term("小雨伞")["aliases"])
        check("合并后匹配到小伞也算小雨伞", [h["term"] for h in L.match("小伞晚安")] == ["小雨伞"])
        d = L.detail(x["id"])
        check("详情：用到它的消息和片段", d["occurrence_total"] == 3 and "小雨伞" in d["occurrences"][0]["snippet"], d["occurrences"][:1])
        ov = L.overview()
        check("总览：收下的、候选、不要的、合并的分开", len(ov["active"]) >= 2 and len(ov["merged"]) == 1 and ov["pending_chunks"] == 0, ov.keys())

        print("\n[6] 分批和出错")
        day_chat("2026-10-20", "C", [("user", "新的一天" + FILL), ("assistant", "早安" + FILL), ("user", "嗯" + FILL), ("assistant", "好" + FILL)])
        day_chat("2026-10-21", "C", [("user", "又一天" + FILL), ("assistant", "早" + FILL), ("user", "嗯" + FILL), ("assistant", "好" + FILL)])
        try:
            asyncio.run(L.scan(llm_fn=FakeLLM([None])))
            check("模型没返回：报错（任务会重试），这块不算扫过", False)
        except RuntimeError:
            check("模型没返回：报错（任务会重试），这块不算扫过", L.pending_chunks() == 2)
        run("INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, source, created_at, updated_at) "
            "VALUES ('lexicon_scan', 'lexicon_scan', '{}', 'running', 1, 4, '2026-10-20 00:00:00', 'auto', '2026-10-20 00:00:00', '2026-10-20 00:00:00')")
        r3 = asyncio.run(L.scan(llm_fn=FakeLLM([]), limit=1))
        check("一次只扫一块、还有剩下的：再投一个任务（自己正在跑也投得进去）", r3["chunks"] == 1 and r3["more"] is True
              and q("SELECT COUNT(*) FROM pipeline_jobs WHERE kind='lexicon_scan' AND status='queued'")[0][0] == 1, r3)
        check("设置有默认值", L.cfg()["enabled"] is True and L.cfg()["auto_activate_days"] == 2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
