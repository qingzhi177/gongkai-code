"""完整备份（services/memory-service/full_backup.py）的测试：临时库 + 编出来的数据，不碰真实记忆库。

运行：python3 tests/manual/test_full_backup.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import zipfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fullbackuptest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
import full_backup as F  # noqa: E402

PASS = FAIL = 0
SECRET = "sk-test-not-a-real-key-0123456789abcdef"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def run(conn, sql, args=()):
    return conn.execute(sql, args).lastrowid


def seed(path):
    conn = sqlite3.connect(path)
    T = "2026-10-16 12:00:00"
    run(conn, "INSERT INTO providers (name, base_url, api_key_enc, models, ts) VALUES ('p', 'https://example.invalid', ?, '[]', ?)", (SECRET, T))
    run(conn, "INSERT INTO model_configs (purpose, provider_id, model_name, enabled, ts) VALUES ('narrative', 1, 'm', 1, ?)", (T,))
    run(conn, "INSERT INTO pipeline_jobs (kind, dedupe_key, payload, status, attempts, max_attempts, next_run_at, source, created_at, updated_at) "
              "VALUES ('extract', 'extract', ?, 'done', 1, 4, ?, 'auto', ?, ?)", (json.dumps({"k": SECRET}), T, T, T))
    msgs = [("A", 0, "user", "第一次聊天，你好呀", "active", 1), ("A", 1, "assistant", "你好，很高兴认识你", "superseded", 1),
            ("A", 1, "assistant", "你好呀，很高兴认识你 (ᗜ֊ᗜ)", "active", 2), ("B", 0, "user", "第二个会话", "active", 1)]
    for cid, idx, role, content, st, ver in msgs:
        run(conn, "INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, version) VALUES (?, ?, ?, ?, ?, 'pluvia', ?, ?)",
            (cid, idx, role, content, T, st, ver))
    run(conn, "INSERT INTO l1_memories (content, quote, conv_id, client, event_type, ts, status, is_core) VALUES ('你喜欢下雨天', '下雨天真好', 'A', 'pluvia', 'fact', ?, 'active', 1)", (T,))
    run(conn, "INSERT INTO l1_memories (content, conv_id, client, event_type, ts, status) VALUES ('今天聊得很开心', 'A', 'ai_self', 'feel', ?, 'active')", (T,))
    run(conn, "INSERT INTO thinking_records (conv_id, ts, thinking, full_reply) VALUES ('A', ?, '想一想怎么回', '你好呀')", (T,))
    run(conn, "INSERT INTO shared_narrative (content, version, ts, status) VALUES ('旧版叙事的内容', 1, ?, 'active')", (T,))
    run(conn, "INSERT INTO episodes (day, conv_id, start_ts, end_ts, title, body, body_with_refs, mood, topics, refs, status, version, created_at) "
              "VALUES ('2026-10-16', 'A', ?, ?, '第一次聊天', '我们第一次聊天。', '我们第一次聊天。[#1]', '开心', '[]', '[1]', 'active', 1, ?)", (T, T, T))
    run(conn, "INSERT INTO digests (kind, period_start, period_end, body, body_with_refs, refs, status, version, created_at) "
              "VALUES ('day', '2026-10-16', '2026-10-16', '- 第一次聊天', '- 第一次聊天[#1]', '[]', 'active', 1, ?)", (T,))
    run(conn, "INSERT INTO chapters (n, title, start_day, status, sections, version, summary, created_at, updated_at) "
              "VALUES (1, NULL, '2026-10-16', 'current', ?, 1, '这一章刚开始。', ?, ?)",
        (json.dumps([{"title": "开头", "body": "我们第一次聊天。", "refs": [1]}], ensure_ascii=False), T, T))
    run(conn, "INSERT INTO relationship_states (day, lines, refs, signals, source_key, status, version, created_at) "
              "VALUES ('2026-10-16', ?, '[]', '{}', 'k', 'active', 1, ?)",
        (json.dumps([{"key": "rhythm", "text": "最近常常聊天。"}], ensure_ascii=False), T))
    run(conn, "INSERT INTO lexicon (term, term_key, aliases, kind, meaning, status, first_seen, use_count, created_at, updated_at) "
              "VALUES ('小雨', '小雨', '[]', 'nickname', '你的小名', 'active', ?, 3, ?, ?)", (T, T, T))
    lid = run(conn, "INSERT INTO open_loops (kind, text, owner, status, follow_up_policy, created_ts, created_quote, created_at, updated_at) "
                    "VALUES ('plan', '一起看电影', 'both', 'open', 'when_relevant', ?, '下次一起看电影吧', ?, ?)", (T, T, T))
    run(conn, "INSERT INTO loop_events (loop_id, event, ts, quote, created_at) VALUES (?, 'mentioned', ?, '还记得看电影吗', ?)", (lid, T, T))
    run(conn, "INSERT INTO recall_traces (turn_key, mode, user_text, decision, legacy, card, created_at) VALUES ('t1', 'shadow', '下雨了', ?, ?, '你喜欢下雨天', ?)",
        (json.dumps({"picks": [{"id": 1, "tier": "strong"}], "gate": "ok"}), json.dumps({"picked": ["2"], "gate": "ok"}), T))
    run(conn, "INSERT INTO recall_traces (turn_key, mode, user_text, decision, created_at, frozen) VALUES ('t2', 'shadow', '清空前的一轮', ?, ?, ?)",
        (json.dumps({"picks": [{"id": 1}], "gate": "ok"}), T, json.dumps({"at": T, "items": {"1": {"content": "清空前定格的那条记忆"}}}, ensure_ascii=False)))
    run(conn, "INSERT INTO facts (fact_key, label, subject, value, status, certainty, valid_from, quote, created_at, updated_at) "
              "VALUES ('user.city', '住在哪个城市', 'user', '杭州', 'superseded', 'explicit', '2026-05-01 00:00:00', '我在杭州', ?, ?)", (T, T))
    run(conn, "INSERT INTO facts (fact_key, label, subject, value, status, certainty, valid_from, quote, created_at, updated_at) "
              "VALUES ('user.city', '住在哪个城市', 'user', '上海', 'current', 'explicit', ?, '我搬到上海了', ?, ?)", (T, T, T))
    conn.execute("CREATE TABLE IF NOT EXISTS vectors (owner_type TEXT, owner_id INTEGER, model TEXT, dim INTEGER, vec BLOB, "
                 "text_hash TEXT, updated_at TEXT)")         # 正式环境里由向量库建
    run(conn, "INSERT INTO vectors (owner_type, owner_id, model, dim, vec, text_hash, updated_at) VALUES ('l1', 1, 'm', 2, ?, 'h', ?)",
        (b"\x00\x01\x02\x03", T))
    conn.commit()
    conn.close()


def old_db(path):
    """旧版的库：表少一些、列少一些"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE l0_messages (id INTEGER PRIMARY KEY, conv_id TEXT, msg_idx INTEGER, role TEXT, content TEXT, ts TEXT, status TEXT)")
    conn.execute("CREATE TABLE l1_memories (id INTEGER PRIMARY KEY, content TEXT, quote TEXT, client TEXT, event_type TEXT, ts TEXT, status TEXT)")
    conn.execute("CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT, api_key_enc TEXT)")
    conn.execute("CREATE TABLE shared_narrative (id INTEGER PRIMARY KEY, content TEXT, version INTEGER, ts TEXT, status TEXT)")
    conn.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, status) VALUES ('old', 0, 'user', '很久以前的一句话', '2026-05-20 15:39:01', 'active')")
    conn.execute("INSERT INTO l1_memories (content, client, event_type, ts, status) VALUES ('很久以前的记忆', 'kelivo', 'fact', '2026-05-20 15:40:00', 'active')")
    conn.execute("INSERT INTO providers (name, api_key_enc) VALUES ('old', ?)", (SECRET,))
    conn.execute("INSERT INTO shared_narrative (content, version, ts, status) VALUES ('很久以前的叙事', 1, '2026-06-01 00:00:00', 'active')")
    conn.commit()
    conn.close()


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        seed(str(config.SQLITE_PATH))
        data = Path(os.environ["DATA_DIR"])
        (data / "profile").mkdir(parents=True, exist_ok=True)
        (data / "profile" / "user.md").write_text("# 画像\n喜欢下雨天", encoding="utf-8")
        (data / "chroma").mkdir(parents=True, exist_ok=True)
        (data / "chroma" / "chroma.sqlite3").write_bytes(b"fake")
        (data / "archive").mkdir(parents=True, exist_ok=True)
        old_db(str(data / "archive" / "old-1.db"))
        old_db(str(data / "archive" / "old-2.db"))
        (data / "archive" / "labels.json").write_text(json.dumps({"old-1.db": {"title": "以前的记忆库", "note": "五月开始的"},
                                                                   "old-2.db": {"readable": False}}, ensure_ascii=False), encoding="utf-8")
        out = TMP / "backup.zip"

        print("\n[1] 打包")
        m = F.build(str(config.SQLITE_PATH), str(data), str(out))
        z = zipfile.ZipFile(out)
        names = set(z.namelist())
        check("有说明、清单、现在的库、每张表的 JSON、可读版", {"说明.md", "manifest.json", "current/memory.db", "current/tables/l0_messages.json",
                                                         "current/可读版/01_对话原文.md"} <= names, sorted(names)[:20])
        check("画像和向量库也在", "current/profile/user.md" in names and "current/chroma/chroma.sqlite3" in names)
        check("二进制向量、全文索引不导出 JSON（在 memory.db 里）", "current/tables/vectors.json" not in names
              and not any(n.startswith("current/tables/l1_fts") for n in names))
        check("清单：每张表有多少行，清掉了哪些", m["current"]["counts"]["l0_messages"] == 4 and m["current"]["cleared"].get("providers") == 1
              and m["current"]["cleared"].get("pipeline_jobs") == 1, m["current"])

        print("\n[2] 不带密钥")
        check("整个 zip 里找不到密钥", not any(SECRET.encode() in z.read(n) for n in names))
        tmpdb = TMP / "check.db"
        tmpdb.write_bytes(z.read("current/memory.db"))
        c = sqlite3.connect(str(tmpdb))
        check("库的副本：密钥、模型配置、任务队列是空的，表还在", all(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
                                                             for t in ("providers", "model_configs", "pipeline_jobs")))
        check("库的副本：记忆数据都在（含旧版本、向量）", c.execute("SELECT COUNT(*) FROM l0_messages").fetchone()[0] == 4
              and c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 1 and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        check("库的副本：不是 WAL 模式，单个文件就能用", c.execute("PRAGMA journal_mode").fetchone()[0] == "delete")
        c.close()
        src = sqlite3.connect(str(config.SQLITE_PATH))
        check("原来的库没被动过", src.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 1)
        src.close()

        print("\n[3] 可读版")
        R = lambda n: z.read(f"current/可读版/{n}").decode("utf-8")
        d = R("01_对话原文.md")
        check("对话原文：两个会话、你和 ta、旧版本标出来、颜文字原样", "## 会话 A" in d and "## 会话 B" in d and "**你**" in d and "**ta**" in d
              and "（旧版本）" in d and "(ᗜ֊ᗜ)" in d, d[:500])
        check("对话原文：时间换成你那边的时间", "2026-10-16 20:00" in d)
        mem = R("02_记忆.md")
        check("记忆：提取的和 ta 自己记下的分开，原话、核心", "## 从对话里提取的" in mem and "## ta 自己记下的" in mem and "原话：下雨天真好" in mem
              and "核心" in mem)
        check("思考记录", "想一想怎么回" in R("03_思考记录.md"))
        check("每日片段和日摘", "《第一次聊天》" in R("05_每日片段和日摘.md") and "**日摘**" in R("05_每日片段和日摘.md"))
        ch = R("07_章节和旧版叙事对比.md")
        check("章节，后面附旧版叙事对比", "第 1 章" in ch and "**开头**" in ch and "旧版叙事的内容" in ch)
        check("关系状态", "节奏：最近常常聊天。" in R("08_关系状态.md"))
        ll = R("09_共同语汇和未完的事.md")
        check("共同语汇和未完的事（含经过）", "**小雨**" in ll and "称呼" in ll and "一起看电影" in ll and "还记得看电影吗" in ll, ll)
        rc = R("10_召回对比.md")
        check("召回对比：新旧各想起了哪条记忆", "新的召回：#1 你喜欢下雨天" in rc and "旧的召回：#2 今天聊得很开心" in rc, rc)
        check("召回对比：清空前的一轮用定格的记忆内容", "新的召回：#1 清空前定格的那条记忆" in rc, rc)
        ft = R("11_事实.md")
        check("事实：现在的值、原话、以前的值", "**住在哪个城市**：上海" in ft and "我搬到上海了" in ft and "以前的：杭州" in ft, ft)
        check("没有内容的部分不生成文件", "current/可读版/06_叙事卷.md" not in names)

        print("\n[4] 旧版")
        check("旧库各放一份（清掉密钥）", "archive/old-1/memory.db" in names and "archive/old-2/memory.db" in names)
        check("旧库的可读版（表少、列少也能读）", "很久以前的一句话" in z.read("archive/old-1/可读版/01_对话原文.md").decode("utf-8")
              and "很久以前的叙事" in z.read("archive/old-1/可读版/04_共同经历叙事和近期摘要.md").decode("utf-8"))
        check("标了不要可读版的旧库：只放库和 JSON", not any(n.startswith("archive/old-2/可读版") for n in names)
              and "archive/old-2/tables/l0_messages.json" in names)
        readme = z.read("说明.md").decode("utf-8")
        check("说明里有标题、说明、条数和没放进来的", "以前的记忆库" in readme and "五月开始的" in readme and "对话原文（条）：4" in readme
              and "没有放进来的" in readme, readme[:600])
        tmpdb.write_bytes(z.read("archive/old-1/memory.db"))
        c = sqlite3.connect(str(tmpdb))
        check("旧库副本里密钥清掉了，数据在", c.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 0
              and c.execute("SELECT COUNT(*) FROM l0_messages").fetchone()[0] == 1)
        c.close()
        z.close()

        print("\n[5] 没有旧库、没有画像也能打包")
        shutil.rmtree(data / "archive")
        shutil.rmtree(data / "profile")
        m = F.build(str(config.SQLITE_PATH), str(data), str(TMP / "b2.zip"))
        check("只有现在的库", m["archive"] == [] and "current/memory.db" in zipfile.ZipFile(TMP / "b2.zip").namelist())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
