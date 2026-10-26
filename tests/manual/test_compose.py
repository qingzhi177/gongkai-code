"""放进聊天的 system 内容测试（app/compose.py）

用法（在 services/memory-service 目录下）：
    venv/bin/python ../../tests/manual/test_compose.py

用临时库和虚构的数据，不调用任何外部接口，不碰真实数据。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="composetest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import compose as CP  # noqa: E402
from app import config  # noqa: E402
from app import settings as S  # noqa: E402
from app.pipeline import chapters as C  # noqa: E402
from app.pipeline import loops as LP  # noqa: E402
from app.pipeline import state as ST  # noqa: E402

PASS = FAIL = 0
NOW = "2026-10-20 00:00:00"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {str(detail)[:900]}")


def run(sql, args=()):
    conn = config.get_conn()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def seed():
    run("INSERT INTO facts (fact_key, label, subject, value, status, certainty, created_at, updated_at) "
        "VALUES ('user.city', '住在哪个城市', 'user', '杭州', 'current', 'explicit', ?, ?)", (NOW, NOW))
    run("INSERT INTO facts (fact_key, label, subject, value, status, certainty, created_at, updated_at) "
        "VALUES ('us.nickname.for_ai', '你叫我什么', 'us', '阿星', 'current', 'explicit', ?, ?)", (NOW, NOW))
    sec = [{"title": "雨季", "body": "这一章我们一起等雨停。", "body_with_refs": "…", "refs": [1]}]
    run("INSERT INTO chapters (n, title, abstract, summary, start_day, end_day, status, sections, created_at, updated_at) "
        "VALUES (1, '初见', '我们第一次聊天。', '', '2026-05-01', '2026-06-30', 'sealed', '[]', ?, ?)", (NOW, NOW))
    run("INSERT INTO chapters (n, title, abstract, summary, start_day, status, sections, created_at, updated_at) "
        "VALUES (2, '雨季', '', '一起等雨停的日子。', '2026-07-01', 'current', ?, ?, ?)", (json.dumps(sec, ensure_ascii=False), NOW, NOW))
    for title, status, summary in (("一起听歌", "active", "下雨天听同一首歌。"), ("旧的那条", "dormant", "很久没提了。"),
                                   ("老家的事", "active", "关于老家。"), ("还在提议", "proposed", "")):
        run("INSERT INTO arcs (title, status, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (title, status, summary, NOW, NOW))
    for term, kind, status, uses, aliases, meaning in (
            ("小雨伞", "inside_joke", "active", 20, [], "你下雨天的口头禅"), ("阿星", "nickname", "active", 3, ["星星"], "你叫我的"),
            ("候选词", "inside_joke", "candidate", 99, [], "还没收下"), ("老家话", "catchphrase", "active", 50, [], "老家的口音")):
        run("INSERT INTO lexicon (term, term_key, aliases, kind, meaning, status, use_count, days_seen, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)", (term, term, json.dumps(aliases, ensure_ascii=False), kind, meaning, status, uses, NOW, NOW))
    ST.add_policy("老家的事", ["老家"], "excluded")
    lines = [{"key": "rhythm", "text": "这周多是晚上来。"}, {"key": "close", "text": "周日你说有我在挺好的。"}]
    run("INSERT INTO relationship_states (day, lines, status, version, created_at) VALUES ('2026-10-19', ?, 'active', 1, ?)",
        (json.dumps(lines, ensure_ascii=False), NOW))
    run("INSERT INTO digests (kind, period_start, period_end, body, body_with_refs, status, created_at) "
        "VALUES ('recent', 'recent', 'recent', '这七天我们一起等雨停。', '…', 'active', ?)", (NOW,))
    for day, ts, title in (("2026-10-17", "2026-10-17 12:00:00", "最早那天"), ("2026-10-18", "2026-10-18 12:00:00", "看雨"),
                           ("2026-10-19", "2026-10-19 02:00:00", "回老家的路上"), ("2026-10-19", "2026-10-19 12:00:00", "一起听歌")):
        run("INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, status, created_at) "
            "VALUES (?, 'c', ?, ?, ?, '…', '…', 'active', ?)", (day, ts, ts, title, NOW))
    run("INSERT INTO shared_narrative (content, version, status) VALUES ('旧的整篇叙事', 1, 'active')")
    run("INSERT INTO recent_summary (content, status) VALUES ('旧的近期摘要', 'active')")
    LP.add("plan", "一起去看海", owner="both")


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        print("\n[1] 什么都没有的时候")
        b = CP.system_blocks()
        check("空库：三块都是空的，没有旧内容可用", b["profile_extra"] == "" and b["semi"] == "" and b["daily"] == ""
              and b["legacy"] == [] and b["recall_live"] is True, b)

        seed()
        print("\n[2] 各层都有")
        b = CP.system_blocks()
        pe, semi, daily = b["profile_extra"], b["semi"], b["daily"]
        check("事实：接在画像后面的档案几行", pe.startswith("[档案 · 事实]") and "住在哪个城市：杭州" in pe and "你叫我什么：阿星" in pe, pe)
        check("半静态块：章节目录 + 进行中这一章的正文", "[我们的章节]" in semi and "第 1 章「初见」" in semi and "我们第一次聊天" in semi
              and "[进行中的这一章 · 第 2 章「雨季」" in semi and "这一章我们一起等雨停。" in semi, semi)
        check("主题线：只放进行中的；碰到轻放关键词的不放", "· 一起听歌：下雨天听同一首歌。" in semi and "旧的那条" not in semi
              and "老家的事" not in semi and "还在提议" not in semi, semi)
        check("共同语汇：称呼在前、带别名；候选和轻放的不放", "「阿星」（也叫星星）：你叫我的" in semi
              and semi.index("「阿星」") < semi.index("「小雨伞」") and "候选词" not in semi and "老家话" not in semi, semi)
        check("不放旧的叙事", "旧的整篇叙事" not in semi and "narrative" not in b["legacy"])
        check("日更块：关系状态（带轻放、不带「上次聊天」）", "[我们最近]\n· 节奏：这周多是晚上来。" in daily and "轻放：老家的事" in daily
              and "上次聊天" not in daily, daily)
        check("近七天摘要，不放旧的近期摘要", "[近七天]\n这七天我们一起等雨停。" in daily and "旧的近期摘要" not in daily, daily)
        check("最近两个聊过天的日子的片段标题（轻放的不放）", "· 10 月 18 日（周日）：看雨" in daily and "· 10 月 19 日（周一）：一起听歌" in daily
              and "最早那天" not in daily and "回老家" not in daily, daily)
        check("还没了结的事", "[还没了结的事]" in daily and "一起去看海" in daily, daily)
        check("各部分的字数都记下了", set(b["parts"]) == {"facts", "chapters", "arcs", "lexicon", "state", "digest", "episodes", "loops"}, b["parts"])
        check("同样的数据每次一模一样（缓存才命中）", CP.system_blocks() == b)

        print("\n[3] 在 Dashboard 上关掉某一层")
        S.update("compose", {"chapters": False, "digest": False, "lexicon": False, "facts": False})
        b = CP.system_blocks()
        check("章节关掉：放回旧的整篇叙事", "[共同经历叙事]\n\n旧的整篇叙事" in b["semi"] and "[我们的章节]" not in b["semi"]
              and "narrative" in b["legacy"], b["semi"][:200])
        check("近七天关掉：放回旧的近期摘要", "[近期阶段摘要]\n\n旧的近期摘要" in b["daily"] and "summary" in b["legacy"], b["daily"][:300])
        check("语汇、事实关掉：不放", "共同语汇" not in b["semi"] and b["profile_extra"] == "")
        S.update("compose", {"recall_live": False})
        check("「用新的召回」的开关也带上", CP.system_blocks()["recall_live"] is False)
        S.reset("compose")

        print("\n[4] 某一层出错、太长")
        orig = C._range_label
        C._range_label = lambda ch: (_ for _ in ()).throw(RuntimeError("坏了"))
        try:
            b = CP.system_blocks()
        finally:
            C._range_label = orig
        check("章节出错：别的层照常，章节退回旧叙事", "[共同经历叙事]" in b["semi"] and "一起听歌" in b["semi"] and "[我们最近]" in b["daily"], b["parts"])
        for k in range(120):
            run("INSERT INTO lexicon (term, term_key, aliases, kind, meaning, status, use_count, days_seen, created_at, updated_at) "
                "VALUES (?, ?, '[]', 'inside_joke', ?, 'active', 1, 1, ?, ?)", (f"词{k}", f"ci{k}", "很长的意思" * 20, NOW, NOW))
        b = CP.system_blocks()
        lex = b["semi"][b["semi"].index("[我们的共同语汇]"):]
        check("共同语汇有长度上限，每条的意思也截短", len(lex) < CP.LEXICON_MAX + 200 and "……" in lex, len(lex))
        for k in range(3, 12):
            run("INSERT INTO chapters (n, title, abstract, summary, start_day, end_day, status, sections, created_at, updated_at) "
                "VALUES (?, ?, ?, '', '2026-05-01', '2026-06-30', 'sealed', '[]', ?, ?)", (k + 100, f"旧章{k}", "很长的摘要" * 30, NOW, NOW))
        b = CP.system_blocks()
        idx = b["semi"][b["semi"].index("[我们的章节]"):b["semi"].index("[进行中的这一章")]
        check("章节目录太长：去掉最早的几章（整行），写明略了几章", "更早的" in idx and "章略" in idx and "第 1 章「初见」" not in idx
              and "\n……" not in idx and len(idx) < CP.CHAPTER_INDEX_MAX + 300, idx[:300])
        sec = [{"title": "长", "body": "雨" * 4000, "body_with_refs": "…", "refs": []}]
        run("UPDATE chapters SET sections=? WHERE n=2", (json.dumps(sec, ensure_ascii=False),))
        b = CP.system_blocks()
        check("进行中这一章太长：只留后面的部分", "……" + "雨" * CP.CHAPTER_MAX in b["semi"] and "雨" * (CP.CHAPTER_MAX + 1) not in b["semi"])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
