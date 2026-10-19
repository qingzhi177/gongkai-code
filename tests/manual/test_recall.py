"""召回编排测试（app/recall/）

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_recall.py

用临时库、假的嵌入 / 精排 / 向量检索，不调用任何外部接口，不碰真实数据。
"""
import asyncio
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="recalltest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["RERANK_ENABLED"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import config  # noqa: E402
from app import settings as S  # noqa: E402
from app.recall import cards, decide as D, golden as G, intent, orchestrator, traces  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ---------- 1. 意图 ----------
def test_intent():
    print("\n[1] 意图识别")
    cases = {
        "你还记得我们第一次聊天吗": "recall", "上次那首歌叫什么来着": "recall", "我们之前说过要去看海": "recall",
        "睡觉之前想跟你说说话": "normal", "我叫什么名字呀": "fact", "我的生日是哪天你知道吗": "fact",
        "帮我写一段代码": "task", "今天好累啊，有点难过": "emotion", "嗯嗯": "greeting", "哈哈哈哈": "greeting",
        "晚安～": "greeting", "好呀": "greeting", "今天去公园看到很多银杏叶": "normal", "然后呢": "followup",
        "真的吗？": "followup",
    }
    bad = {t: (intent.classify(t)["type"], want) for t, want in cases.items() if intent.classify(t)["type"] != want}
    check(f"{len(cases)} 句全部判对", not bad, bad)
    check("短消息拼上上文", orchestrator.build_query("然后呢", "我今天去面试了", "怎么样？") == "我今天去面试了 怎么样？ 然后呢")
    check("长消息不拼", orchestrator.build_query("今天去公园看到很多银杏叶", "a", "b") == "今天去公园看到很多银杏叶")


# ---------- 2. 模糊时间与卡片 ----------
def test_cards():
    print("\n[2] 模糊时间、念头式卡片")
    now = "2026-10-18 04:00:00"            # 北京时间 10-18 12:00
    check("北京时间前一天晚上 → 昨天", cards.fuzzy_time("2026-10-17 15:30:00", now=now) == "昨天")
    check("UTC 前一天、北京时间当天凌晨 → 今天早些时候", cards.fuzzy_time("2026-10-17 16:30:00", now=now) == "今天早些时候")
    check("十天前 → 前阵子", cards.fuzzy_time("2026-10-08 04:00:00", now=now) == "前阵子")
    check("三个月前 → 季节", cards.fuzzy_time("2026-07-10 04:00:00", now=now) == "夏天")
    check("去年 → 去年+季节", cards.fuzzy_time("2025-04-10 04:00:00", now=now) == "去年春天")
    check("没有时间", cards.fuzzy_time(None) == "不知何时")
    picks = [{"id": 1, "content": "一起听过晴天", "quote": "下雨就听这首吧", "ts": "2026-10-17 12:00:00", "is_core": 1, "tier": "strong"},
             {"id": 2, "content": "那次她说累，我很想多陪她一会儿", "ts": "2026-10-17 12:00:00", "author": "ai_felt", "tier": "medium"}]
    c = cards.format_card(picks, "normal", now=now)
    check("核心记忆有星标、感受有标签、平时不带原话",
          "（⭐ 昨天，一起听过晴天）" in c and "我记下的感受：" in c and "原话" not in c, c)
    c2 = cards.format_card(picks[:1], "recall", now=now)
    check("回忆模式附原话", "——原话「下雨就听这首吧」" in c2, c2)
    check("没有要注入的返回空串", cards.format_card([], "normal") == "")


# ---------- 3. 融合与决策 ----------
def C(i, score, vec=None, arousal=0.3):
    return {"id": i, "score": score, "vec_sim": vec if vec is not None else score, "arousal": arousal, "content": f"m{i}"}


def test_decide():
    print("\n[3] 融合、分层决策、冷却")
    fused = orchestrator.fuse([(1, 0.9), (2, 0.8), (3, 0.7)], [(3, 5.0), (4, 4.0)], "normal")
    check("RRF：两个通道都排在前面的最靠前", fused[0][0] == 3 and {m for m, _ in fused} == {1, 2, 3, 4}, fused)
    nocd = {"recent": set(), "medium": set(), "serendipity": set()}
    r = D.decide([C(1, 0.9), C(2, 0.8), C(3, 0.78), C(4, 0.6)], "normal", nocd, True, rng=random.Random(1))
    check("强层一定注入，最多 2 条", [p["id"] for p in r["picks"]] == [1, 2] and all(p["tier"] == "strong" for p in r["picks"]), r["picks"])
    r = D.decide([C(4, 0.6), C(5, 0.55)], "normal", nocd, True, rng=random.Random(0))
    check("只有中层时按 30% 概率开放（这次关闭）", r["picks"] == [] and r["gate"] == "medium_closed" and r["eligible"]["medium"] == [4, 5], r)
    opened = 0
    for seed in range(200):
        rr = D.decide([C(4, 0.6), C(5, 0.55)], "normal", nocd, True, rng=random.Random(seed))
        opened += bool(rr["gate"] and rr["gate"].startswith("medium_open"))
    check("中层开放比例约 30%", 40 <= opened <= 85, opened)
    r = D.decide([C(1, 0.3), C(2, 0.2)], "normal", nocd, True, rng=random.Random(1))
    check("弱层不注入", r["picks"] == [] and r["eligible"]["strong"] == [] and r["eligible"]["medium"] == [])
    r = D.decide([C(1, 0.9), C(2, 0.6), C(3, 0.55), C(4, 0.52)], "recall", nocd, True, rng=random.Random(1))
    check("回忆模式：前 3 条、不走概率", [p["id"] for p in r["picks"]] == [1, 2, 3] and r["gate"] == "recall")
    r = D.decide([C(1, 0.95)], "greeting", nocd, True, rng=random.Random(1))
    check("寒暄不走相关度", r["picks"] == [] and r["gate"] == "greeting")
    cd = {"recent": {1}, "medium": {4}, "serendipity": set()}
    r = D.decide([C(1, 0.9), C(2, 0.8), C(4, 0.6)], "normal", cd, True, rng=random.Random(1))
    check("最近 5 轮出现过的拿掉；不回填弱的", [p["id"] for p in r["picks"]] == [2]
          and {x["id"] for x in r["dropped"]} == {1, 4}, r)
    r = D.decide([C(1, 0.9, vec=0.9), C(2, 0.3, vec=0.5)], "normal", nocd, False, rng=random.Random(5))
    check("精排不可用：退回向量门槛，只算中层", r["basis"] == "vec_sim" and r["eligible"]["strong"] == []
          and r["eligible"]["medium"] == [1], r["eligible"])
    r = D.decide([C(1, 0.72, arousal=0.8), C(2, 0.74)], "emotion", nocd, True, rng=random.Random(1))
    check("情绪意图给情绪浓的加分（0.72+0.05 进入强层）", r["picks"][0]["id"] == 1 and r["picks"][0]["tier"] == "strong"
          and r["eligible"]["strong"] == [1], r["picks"])
    hits = 0
    pool = [{"id": 9, "arousal": 0.9, "is_core": 1, "content": "x"}]
    for seed in range(400):
        rr = D.decide([], "greeting", nocd, True, serendipity_pool=pool, rng=random.Random(seed))
        hits += any(p["tier"] == "serendipity" for p in rr["picks"])
    check("无由来浮现约 5%（寒暄时也可能有）", 8 <= hits <= 40, hits)
    hits = 0
    for seed in range(400):
        rr = D.decide([], "normal", {"recent": set(), "medium": set(), "serendipity": {9}}, True,
                      serendipity_pool=pool, rng=random.Random(seed))
        hits += bool(rr["picks"])
    check("无由来 7 天冷却", hits == 0)

    # 可调参数（Dashboard 上改的）
    r = D.decide([C(1, 0.7), C(2, 0.68), C(3, 0.66)], "normal", nocd, True, rng=random.Random(1),
                 params={"t_high": 0.65, "max_picks": 1})
    check("参数：调低强门槛、每轮 1 条", [p["id"] for p in r["picks"]] == [1] and r["eligible"]["strong"] == [1, 2, 3], r)
    r = D.decide([C(4, 0.6)], "normal", nocd, True, rng=random.Random(0), params={"medium_prob": 1.0})
    check("参数：中层概率 100%", r["gate"].startswith("medium_open") or r["picks"] == [], r)
    opened = sum(bool(D.decide([C(4, 0.6)], "normal", nocd, True, rng=random.Random(sd), params={"medium_prob": 1.0})["gate"]
                      .startswith("medium_open")) for sd in range(50))
    check("参数：中层概率 100% 时每次都开放", opened == 50, opened)
    hits = sum(bool(D.decide([], "greeting", nocd, True, serendipity_pool=pool, rng=random.Random(sd),
                             params={"serendipity_prob": 0})["picks"]) for sd in range(200))
    check("参数：无由来概率 0 时不出现", hits == 0, hits)
    r = D.decide([C(1, 0.9)], "normal", {"recent": {1}, "medium": set(), "serendipity": set()}, True,
                 rng=random.Random(1), params={"recent_turns": 8})
    check("参数：冷却说明跟着改", r["dropped"] == [{"id": 1, "reason": "最近 8 轮出现过"}], r["dropped"])
    r = D.decide([C(1, 0.9)], "normal", nocd, True, rng=random.Random(1), params={"max_picks": 0, "serendipity_prob": 1})
    check("参数：每轮 0 条就什么都不想起", r["picks"] == [], r["picks"])
    check("参数：没传的用默认值", D.params_from({"t_high": 0.8})["t_low"] == D.T_LOW and D.params_from(None)["t_high"] == D.T_HIGH)
    r = D.decide([{**C(1, 0.7), "in_time": True}, C(2, 0.72)], "normal", nocd, True, rng=random.Random(1))
    check("时间加分：发生在话里那段时间的 0.7 + 0.08 进入强层", r["eligible"]["strong"] == [1] and r["picks"][0]["id"] == 1, r["eligible"])


# ---------- 4. 端到端（假嵌入 / 假精排 / 假向量）----------
class FakeCollection:
    def __init__(self, sims):
        self.sims = sims

    def query(self, query_embeddings, n_results=10, include=None):
        items = sorted(self.sims.items(), key=lambda x: -x[1])[:n_results]
        return {"ids": [[f"l1_{i}" for i, _ in items]], "similarities": [[s for _, s in items]]}


def seed_l1(rows):
    conn = config.get_conn()
    for i, content, quote, arousal in rows:
        conn.execute("INSERT INTO l1_memories (id, content, quote, ts, event_type, arousal, status) "
                     "VALUES (?, ?, ?, '2026-10-17 12:00:00', 'event', ?, 'active')", (i, content, quote, arousal))
    conn.commit()
    conn.close()


def test_e2e():
    print("\n[4] 端到端：写轨迹、写影子账本、冷却生效")
    db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
    seed_l1([(1, "下雨天一起听晴天", "下雨就听这首吧", 0.7), (2, "她说加班很累", "", 0.4),
             (3, "讨论前端部署", "", 0.2), (4, "第一次叫我小雨伞", "小雨伞", 0.9)])
    calls = {"embed": 0, "rerank": 0}

    async def fake_embed(q):
        calls["embed"] += 1
        return [0.1] * 4

    async def fake_fts(q, n=30):
        return [(1, 3.0), (3, 1.0)]

    async def fake_rerank(q, docs):
        calls["rerank"] += 1
        table = {"下雨天一起听晴天（原话：下雨就听这首吧）": 0.9, "她说加班很累": 0.6, "讨论前端部署": 0.1}
        return [table.get(d, 0.2) for d in docs], {"model": "fake", "ok": True, "ms": 5}

    coll = FakeCollection({1: 0.82, 2: 0.66, 3: 0.4})
    req = {"session_id": "conv_a", "user_text": "下雨了，想起那首歌", "backend": "api", "shadow": True, "client": "pluvia",
           "legacy": {"gate": "open", "picked": [2], "candidates": [[2, 0.66]]}}
    run = lambda r, seed=1: asyncio.run(orchestrator.compose_turn(r, coll, embed_fn=fake_embed, rerank_fn=fake_rerank,
                                                                 fts_fn=fake_fts, rng=random.Random(seed)))
    res = run(req)
    check("强相关的那条被选中", [p["id"] for p in res["picks"]][:1] == [1] and res["picks"][0]["tier"] == "strong", res)
    check("影子模式不返回给网关注入", res["card"] == "" and "晴天" in res["shadow_card"])
    conn = config.get_conn()
    tr = conn.execute("SELECT mode, intent, legacy, injected_chars FROM recall_traces WHERE id=?", (res["trace_id"],)).fetchone()
    inj = conn.execute("SELECT item_id, tier, status FROM injections WHERE trace_id=?", (res["trace_id"],)).fetchall()
    conn.close()
    check("轨迹写入（带旧逻辑的决定）", tr[0] == "shadow" and '"picked": [2]' in tr[2] and tr[3] > 0, tr)
    check("影子账本写入", ("1", "strong", "shadow") in inj, inj)
    res2 = run(req, seed=2)
    check("同一会话下一轮：刚出现过的不再出现", 1 not in [p["id"] for p in res2["picks"]], res2["picks"])
    res3 = run({**req, "session_id": "conv_b"}, seed=3)
    check("换一个会话：强相关的可以出现", 1 in [p["id"] for p in res3["picks"]], res3["picks"])
    before = dict(calls)
    res4 = run({**req, "user_text": "嗯嗯"})
    check("寒暄不调用嵌入和精排", calls == before and res4["intent"] == "greeting")

    async def broken_rerank(q, docs):
        return None, {"model": "fake", "ok": False, "error": "timeout"}
    res5 = asyncio.run(orchestrator.compose_turn({**req, "session_id": "conv_c"}, coll, embed_fn=fake_embed,
                                                 rerank_fn=broken_rerank, fts_fn=fake_fts, rng=random.Random(0)))
    t5 = traces.get_trace(res5["trace_id"])
    check("精排失败时退回向量相似度", t5["decision"]["basis"] == "vec_sim" and t5["rerank"]["ok"] is False)
    check("轨迹详情带候选内容", t5["snippets"].get("1", {}).get("content", "").startswith("下雨天"))
    lst = traces.list_traces(limit=10)
    first = [x for x in lst if x["id"] == res["trace_id"]][0]
    check("列表：新旧两边的选择都在", [p["id"] for p in first["new"]][:1] == [1]
          and [p["id"] for p in first["legacy"]["picked"]] == [2] and first["legacy"]["picked"][0]["content"] == "她说加班很累")
    st = traces.stats(days=7)
    check("统计", st["turns"] == 5 and st["legacy_inject_turns"] == 5 and st["intents"].get("greeting") == 1
          and st["rerank_tried_turns"] == 4 and st["rerank_ok_turns"] == 3, st)


# ---------- 5. 可调参数（app_settings）----------
def test_settings():
    print("\n[5] 召回参数：保存、校验、恢复默认，召回编排用上")

    def err(fn):
        try:
            fn()
            return None
        except ValueError as e:
            return str(e)

    d = S.describe("recall")
    items = {x["key"]: x for x in d["items"]}
    check("默认值", items["t_high"]["value"] == D.T_HIGH and not items["t_high"]["overridden"] and d["updated_at"] is None
          and items["rerank_enabled"]["type"] == "bool" and items["max_picks"]["type"] == "int", items["t_high"])
    v = S.update("recall", {"t_high": 0.7, "medium_prob": "0.5", "rerank_enabled": "0", "max_picks": 3})
    check("保存（字符串会转成对应类型）", v["t_high"] == 0.7 and v["medium_prob"] == 0.5 and v["rerank_enabled"] is False
          and v["max_picks"] == 3, v)
    check("保存后马上生效（不等缓存过期）", S.get("recall")["t_high"] == 0.7)
    items = {x["key"]: x for x in S.describe("recall")["items"]}
    check("标记改过的", items["t_high"]["overridden"] and items["t_high"]["updated_at"] and not items["t_low"]["overridden"])
    e = err(lambda: S.update("recall", {"t_low": 0.8, "medium_prob": 0.9}))
    check("中门槛高于强门槛：拒绝，整批不存", e and "中门槛" in e and S.get("recall")["medium_prob"] == 0.5, e)
    check("超出范围：拒绝", "之间" in (err(lambda: S.update("recall", {"t_high": 1.5})) or ""))
    check("整数参数填小数：拒绝", "整数" in (err(lambda: S.update("recall", {"max_picks": 2.5})) or ""))
    check("不认识的参数：拒绝", "没有这个参数" in (err(lambda: S.update("recall", {"foo": 1})) or ""))
    check("空字符串：拒绝", err(lambda: S.update("recall", {"t_high": ""})) is not None
          and err(lambda: S.update("recall", {"rerank_model": "  "})) is not None)
    S.update("recall", {"t_high": 0.9, "t_low": 0.8})
    e = err(lambda: S.reset("recall", ["t_high"]))
    check("恢复默认后会冲突（强门槛 0.75 < 中门槛 0.8）：拒绝、不改", e and S.get("recall")["t_high"] == 0.9, e)
    v = S.reset("recall", ["t_high", "t_low"])
    check("恢复单项", v["t_high"] == D.T_HIGH and v["t_low"] == D.T_LOW and v["medium_prob"] == 0.5, v)

    # 召回编排用上 Dashboard 的参数
    S.update("recall", {"t_high": 0.95, "medium_prob": 0, "serendipity_prob": 0, "rerank_enabled": False,
                        "rerank_model": "test-rerank"})

    async def fake_embed(q):
        return [0.1] * 4

    async def fake_fts(q, n=30):
        return [(3, 3.0)]

    async def fake_rerank(q, docs):
        return [0.9 for _ in docs], {"model": "fake", "ok": True, "ms": 5}

    coll = FakeCollection({3: 0.82})   # 3 号前面没被注入过，不受冷却影响
    req = {"session_id": "conv_s", "user_text": "下雨了，想起那首歌", "backend": "api", "shadow": True}
    res = asyncio.run(orchestrator.compose_turn(req, coll, embed_fn=fake_embed, rerank_fn=fake_rerank, fts_fn=fake_fts,
                                                rng=random.Random(1)))
    t = traces.get_trace(res["trace_id"])
    check("强门槛调到 0.95：0.9 分只算中层；中层概率 0：不注入", res["picks"] == [] and t["decision"]["gate"] == "medium_closed"
          and t["decision"]["params"]["t_high"] == 0.95, t["decision"])
    res = asyncio.run(orchestrator.compose_turn({**req, "session_id": "conv_s2"}, coll, embed_fn=fake_embed, fts_fn=fake_fts,
                                                rng=random.Random(1)))
    t = traces.get_trace(res["trace_id"])
    check("精排开关和模型也用 Dashboard 的", t["rerank"]["model"] == "test-rerank" and t["rerank"]["ok"] is False
          and t["rerank"]["error"] == "未启用" and t["decision"]["basis"] == "vec_sim", t["rerank"])
    v = S.reset("recall")
    check("全部恢复默认", v == {x["key"]: x["default"] for x in S.SPECS["recall"]} and S.describe("recall")["updated_at"] is None)


# ---------- 6. 黄金集标注和门槛校准 ----------
def mk_trace(cands, rerank_ok=True, user_text="测试"):
    import json as _j
    conn = config.get_conn()
    rr = {"ok": True} if rerank_ok is True else ({"skipped": "寒暄或空消息"} if rerank_ok == "skip" else {"ok": False})
    cur = conn.execute(
        "INSERT INTO recall_traces (turn_key, backend, session_id, mode, query, user_text, intent, rerank, decision, created_at) "
        "VALUES ('k', 'api', 'gold', 'shadow', ?, ?, '{}', ?, ?, '2026-10-19 00:00:00')",
        (user_text, user_text, _j.dumps(rr), _j.dumps({"candidates": [{"id": i, "rerank": sc} for i, sc in cands]})))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def test_golden():
    print("\n[6] 黄金集：标注、校准、导出")

    def err(fn):
        try:
            fn()
            return None
        except (ValueError, LookupError) as e:
            return type(e).__name__

    t0 = mk_trace([(1, 0.9), (2, 0.4)])
    check("标注：该想起", G.set_label(t0, 1, "should") == {1: "should"})
    check("标注：不该想起", G.set_label(t0, 2, "should_not") == {1: "should", 2: "should_not"})
    check("整轮「什么都不该想起」会去掉「该想起」，保留「不该想起」", G.set_label(t0, 0, "none") == {0: "none", 2: "should_not"})
    check("再标「该想起」会去掉整轮的「什么都不该想起」", G.set_label(t0, 1, "should") == {1: "should", 2: "should_not"})
    check("清除", G.set_label(t0, 2, None) == {1: "should"})
    check("参数不对：拒绝", err(lambda: G.set_label(t0, 1, "none")) == "ValueError"
          and err(lambda: G.set_label(t0, 0, "should")) == "ValueError" and err(lambda: G.set_label(t0, 1, "maybe")) == "ValueError")
    check("没有这一轮：拒绝", err(lambda: G.set_label(999999, 1, "should")) == "LookupError")
    tr = traces.get_trace(t0)
    check("轨迹详情带标注", tr["labels"] == {"1": "should"})
    lst = {x["id"]: x for x in traces.list_traces(limit=300)}
    check("列表里标了几条", lst[t0]["labeled"] == 1 and all(v["labeled"] == 0 for k, v in lst.items() if k != t0))
    G.set_label(t0, 1, None)

    cal = G.calibration()
    check("标注不够时不给建议", cal["suggest"]["ok"] is False and "再多标一些" in cal["suggest"]["reason"], cal["suggest"])

    should = [0.9, 0.85, 0.8, 0.78, 0.7, 0.65, 0.6, 0.55, 0.5, 0.3]
    neg = [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.62, 0.66]
    for k, (a, b) in enumerate(zip(should, neg)):
        tid = mk_trace([(100 + k, a), (200 + k, b), (300 + k, 0.1)])
        G.set_label(tid, 100 + k, "should")
        G.set_label(tid, 200 + k, "should_not")
    G.set_label(mk_trace([], rerank_ok="skip", user_text="嗯嗯"), 0, "none")       # 寒暄：本来就不检索，算反例
    G.set_label(mk_trace([(400, 0.9)], rerank_ok=False), 400, "should")          # 精排失败的轮次不算
    cal = G.calibration()
    c = cal["counts"]
    check("计数", c["turns"] == 12 and c["should"] == 10 and c["should_not"] == 10 and c["none_turns"] == 1 and c["skipped"] == 1, c)
    row = {r["t"]: r for r in cal["rows"]}
    check("命中率 / 误浮现率", row[0.65]["hit"] == 0.6 and row[0.65]["fp"] == round(1 / 11, 3)
          and row[0.55]["hit"] == 0.8 and row[0.3]["hit"] == 1.0, [row[0.55], row[0.65]])
    sg = cal["suggest"]
    check("建议门槛：强 0.65（误浮现 ≤ 10% 的最低值）、中 0.55（命中率 ≥ 80% 的最高值）",
          sg["ok"] and sg["t_high"] == 0.65 and sg["t_low"] == 0.55 and sg["notes"] == [], sg)
    q = cal["quality"]
    expect_mrr = round((sum(1.0 for a, b in zip(should, neg) if a >= b) + sum(0.5 for a, b in zip(should, neg) if a < b)) / 10, 3)
    check("排序质量：前 5 命中、MRR", q["recall_at_5"] == 1.0 and q["mrr"] == expect_mrr, q)
    ex = G.export()
    check("导出", len(ex["items"]) == 12 and sum(1 for i in ex["items"] if i["nothing"]) == 1
          and any(i["should"] and i["should_not"] for i in ex["items"]), len(ex["items"]))


# ---------- 7. 时间通道 ----------
def test_time_channel():
    print("\n[7] 时间通道：话里提到「昨天」，昨天的记忆进候选并加分")
    from datetime import timedelta
    import timeutil
    now = timeutil.utc_now()
    y_noon = (timeutil.to_local(now) - timedelta(days=1)).replace(hour=12, minute=0, second=0)
    y_ts = timeutil.to_utc_str(y_noon.isoformat())
    conn = config.get_conn()
    conn.execute("INSERT INTO l1_memories (id, content, ts, event_type, arousal, status) VALUES "
                 "(60, '昨天一起听了雨声', ?, 'event', 0.5, 'active')", (y_ts,))
    conn.execute("INSERT INTO l1_memories (id, content, ts, event_type, arousal, status) VALUES "
                 "(61, '很早以前也听过雨', '2025-01-01 12:00:00', 'event', 0.5, 'active')")
    conn.commit()
    conn.close()

    async def fake_embed(q):
        return [0.1] * 4

    async def fake_fts(q, n=30):
        return []

    async def fake_rerank(q, docs):
        return [0.7 for _ in docs], {"model": "fake", "ok": True, "ms": 5}

    coll = FakeCollection({61: 0.6})          # 向量只找到了很早以前那条；昨天那条只能靠时间通道
    req = {"session_id": "conv_time", "user_text": "昨天我们听的那个声音真好", "backend": "api", "shadow": True}
    res = asyncio.run(orchestrator.compose_turn(req, coll, embed_fn=fake_embed, rerank_fn=fake_rerank, fts_fn=fake_fts,
                                                rng=random.Random(1), params={"medium_prob": 0, "serendipity_prob": 0}))
    t = traces.get_trace(res["trace_id"])
    tm = (t["channels"] or {}).get("time") or {}
    check("轨迹记下了时间范围和命中", tm.get("ranges", [{}])[0].get("label") == "昨天" and 60 in tm.get("hits", []), tm)
    cands = {c["id"]: c for c in t["decision"]["candidates"]}
    check("昨天那条进了候选，标了在这段时间；很早以前那条没标", cands.get(60, {}).get("in_time") is True
          and 61 in cands and not cands[61].get("in_time"), cands)
    check("昨天那条 0.7 + 0.08 成了强层，另一条没有", [p["id"] for p in res["picks"]] == [60]
          and res["picks"][0]["tier"] == "strong", res["picks"])
    res2 = asyncio.run(orchestrator.compose_turn({**req, "session_id": "conv_time2"}, coll, embed_fn=fake_embed,
                                                 rerank_fn=fake_rerank, fts_fn=fake_fts, rng=random.Random(1),
                                                 params={"medium_prob": 0, "serendipity_prob": 0, "time_enabled": False}))
    t2 = traces.get_trace(res2["trace_id"])
    check("关掉时间通道：不找、不加分", "time" not in (t2["channels"] or {}) and res2["picks"] == []
          and 60 not in {c["id"] for c in t2["decision"]["candidates"]}, t2["channels"])


def main():
    try:
        test_intent()
        test_cards()
        test_decide()
        test_e2e()
        test_settings()
        test_golden()
        test_time_channel()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
