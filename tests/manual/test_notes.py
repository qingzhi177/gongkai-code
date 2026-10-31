"""小本子（app/notes.py）和它在 system / 窗口开头附注里的样子：临时库，不碰真实数据。

运行：python3 tests/manual/test_notes.py
"""
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


def main():
    tmp = Path(tempfile.mkdtemp(prefix="notestest_"))
    try:
        path = tmp / "m.db"
        os.environ["MEMORY_DB_PATH"] = str(path)
        import db  # noqa: E402
        db.migrate(path, log=lambda *a, **k: None)
        from app import config, settings as ST
        config.get_conn = lambda: sqlite3.connect(str(path))
        ST.get_conn = config.get_conn
        from app import notes as N
        c = sqlite3.connect(str(path))
        S = {"enabled": True, "days": 14, "max_items": 12, "max_chars": 700}

        print("\n[1] 记一笔、改、擦、恢复、钉住")
        c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status) VALUES ('cv', 0, 'user', '早', 'pluvia', 'active')")
        r = N.act(c, "write", text="  你说楼下的猫\\n今天换了一只新的项圈，是蓝色的 ", conv_id="cv", now="2026-10-29 06:00:00")
        n1 = r["note"]
        check("记下：去掉多余空白，记着在哪个对话、那时最新的一条", r["status"] == "ok" and n1["text"].startswith("你说楼下的猫")
              and n1["conv_id"] == "cv" and n1["anchor_l0_id"] == 1 and n1["source"] == "ai" and "#1" in r["message"], r)
        r = N.act(c, "write", text="你说楼下的猫今天换了一只新的项圈，是蓝色的！", now="2026-10-30 06:00:00")
        check("几天里记过差不多的话：不再记一遍，告诉模型是哪条", r["status"] == "dup" and r["note"]["id"] == 1 and "#1" in r["message"], r)
        r = N.act(c, "write", text="", now="2026-10-30 06:00:00")
        check("空的不记", r["status"] == "error")
        r = N.act(c, "write", text="x" * 500, now="2026-10-30 06:00:00")
        check("太长的截到 200 字", r["status"] == "ok" and len(r["note"]["text"]) == 200)
        N.act(c, "erase", r["note"]["id"], by="user")
        r = N.act(c, "edit", 1, "楼下的猫换了蓝色的项圈，你说它看起来很神气", now="2026-10-30 07:00:00")
        check("改：内容换掉，记着是模型改的", r["status"] == "ok" and N.get(c, 1)["text"].endswith("很神气") and N.get(c, 1)["edited_by"] == "ai")
        check("改的时候没带编号 / 编号不存在：说清楚", N.act(c, "edit", None, "x")["status"] == "error" and "#99" in N.act(c, "erase", 99)["message"])
        check("改成空的不行（要删用 erase）", N.act(c, "edit", 1, "  ")["status"] == "error")
        r = N.act(c, "write", text="你第一次说起想学吉他，说等寒假", now="2026-10-25 12:00:00")
        N.act(c, "pin", r["note"]["id"], by="user")
        check("钉住", N.get(c, r["note"]["id"])["pinned"] == 1)
        check("不认识的操作", N.act(c, "fly", 1)["status"] == "error")

        print("\n[2] 放进 system 的：今天之前最近几天的 + 钉住的；今天新记的放窗口开头")
        N.act(c, "write", text="你说今晚的月亮像一枚剥开的橘子瓣", now="2026-10-31 13:00:00")      # 北京时间 10-31 21:00：今天
        N.act(c, "write", text="很早以前记的一件小事，应该已经不放了", now="2026-10-01 12:00:00")
        now = "2026-10-31 14:00:00"
        st = N.system_text(c, now, S)
        check("今天之前的在 system 里，带日期和编号", "楼下的猫" in st and "10 月 29 日（周四）" in st and "（#1）" in st, st)
        check("钉住的一直在（不管多久以前）", "吉他" in st and "钉住" in st, st)
        check("今天新记的不在 system 里（不然每记一笔整段缓存都要重写）", "橘子瓣" not in st, st)
        check("太久以前的不放", "很早以前" not in st)
        check("擦掉的不放", "xxxxxxxx" not in st)
        check("按时间排（早的在前）", st.index("吉他") < st.index("楼下的猫"))
        check("同一天里 system 不变（不依赖钟点）", N.system_text(c, "2026-10-31 15:59:00", S) == st)
        check("凌晨 4 点前还算前一天", "橘子瓣" not in N.system_text(c, "2026-10-31 19:59:00", S)
              and "橘子瓣" in N.system_text(c, "2026-10-31 20:00:00", S))
        fl = N.fresh_lines(c, now, S)
        check("今天新记的给窗口开头那行，时间写绝对的", len(fl) == 1 and "10 月 31 日 21:00" in fl[0] and "橘子瓣" in fl[0], fl)
        st2 = N.system_text(c, now, {**S, "max_chars": 50})
        check("超过字数：去掉最早的（钉住的先留）", "吉他" in st2 and st2.count("·") == 1, st2)
        check("关掉：都不放", N.system_text(c, now, {**S, "enabled": False}) == "" and N.fresh_lines(c, now, {**S, "enabled": False}) == [])

        print("\n[3] Dashboard 的列表")
        o = N.overview(c)
        check("钉住的在前；数了 active / erased", o["notes"][0]["pinned"] == 1 and o["counts"]["erased"] == 1, o["counts"])
        check("看擦掉的", len(N.overview(c, "erased")["notes"]) == 1)
        N.act(c, "restore", 2, by="user")
        check("恢复", N.get(c, 2)["status"] == "active")
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
