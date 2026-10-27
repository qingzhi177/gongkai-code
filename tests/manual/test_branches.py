"""分支会话（app/branches.py）的测试：临时库，不碰真实数据，只用标准库。

运行：python3 tests/manual/test_branches.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "services" / "memory-service"))

import db  # noqa: E402
from app import branches as B  # noqa: E402

PASS = FAIL = 0
NOW = "2026-10-19 12:00:00"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def add(c, conv, msgs, start=0):
    ids = []
    for i, (role, content) in enumerate(msgs):
        cur = c.execute("INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status) VALUES (?,?,?,?,?,?)",
                        (conv, start + i, role, content, "pluvia", "active"))
        ids.append(cur.lastrowid)
    return ids


def main():
    tmp = Path(tempfile.mkdtemp(prefix="branchtest_"))
    try:
        path = tmp / "m.db"
        db.migrate(path, log=lambda *a, **k: None)
        c = sqlite3.connect(str(path))
        u1, a1, u2, a2 = ("user", "早"), ("assistant", "早呀\n今天想做什么"), ("user", "去看海吧"), ("assistant", "好")
        u3, a3, u4 = ("user", "换个说法：去爬山吧"), ("assistant", "爬山也好"), ("user", "那就周六")
        pid = add(c, "P", [u1, a1, u2, a2])

        print("\n[1] 登记分支")
        check("不是分支", B.get(c, "X") is None and B.anchor(c, "X") is None)
        check("从自己分出来：不算", B.register(c, "P", {"conversation_id": "P"}, [u1], NOW) is None)
        check("没有原会话 id：不算", B.register(c, "Y", {"turn_id": "t"}, [u1], NOW) is None)
        b = B.register(c, "Bq", {"conversation_id": "P", "turn_id": "t2"}, [u1, ("assistant", "早呀今天想做什么"), u3], NOW)
        check("登记：开头和原会话一样的 2 条（换行差异不算不一样）", b and b["shared"] == 2 and b["parent"] == "P"
              and b["parent_turn_id"] == "t2" and [e[2] for e in b["prefix"]] == pid[:2], b)
        check("分支点在原会话的第 2 行", B.anchor(c, "Bq") == ("P", pid[1]))
        b2 = B.register(c, "Bq", {"conversation_id": "OTHER"}, [u3], NOW)
        check("已经登记过的不再改", b2["parent"] == "P" and b2["shared"] == 2)

        print("\n[2] 以后每次存记录跳过重合部分")
        check("分支继续聊：跳过 2 条", B.shared_len(c, "Bq", [u1, a1, u3, a3, u4]) == 2)
        add(c, "Bq", [u3, a3])
        c.execute("UPDATE l0_messages SET status='superseded' WHERE conv_id='P' AND id=?", (pid[0],))
        add(c, "P", [("user", "早（改过）")], start=10)
        check("原会话后来改了开头：分支照样跳过（指纹是分支那一刻记下的）", B.shared_len(c, "Bq", [u1, a1, u3, a3]) == 2)
        c.execute("UPDATE l0_messages SET status='superseded' WHERE conv_id='P'")
        check("原会话整个删了：分支也不会把重合部分存回来", B.shared_len(c, "Bq", [u1, a1, u3, a3]) == 2)
        check("在分支里改了重合部分的第 2 条：只跳过第 1 条", B.shared_len(c, "Bq", [u1, ("assistant", "改过的"), u3]) == 1)
        check("历史对不上：一条都不跳", B.shared_len(c, "Bq", [("user", "别的"), a1]) == 0)

        print("\n[3] 分支的分支")
        hist = B.history(c, "Bq")
        check("分支的完整历史 = 重合部分 + 自己存的", [e[1] for e in hist] == ["P", "P", "Bq", "Bq"], hist)
        bc = B.register(c, "Cq", {"conversation_id": "Bq"}, [u1, a1, u3, ("user", "再换一个")], NOW)
        check("从分支再分：重合 3 条（2 条在原会话、1 条在分支）", bc["shared"] == 3 and [e[1] for e in bc["prefix"]] == ["P", "P", "Bq"])
        check("分支点在第一个分支自己的那一行", B.anchor(c, "Cq")[0] == "Bq")

        print("\n[4] 原会话没有存过（比如是不进记忆的会话）")
        bn = B.register(c, "Dq", {"conversation_id": "NOPE"}, [u1, a1, u3], NOW)
        check("登记了但没有重合部分：全部照常存", bn["shared"] == 0 and B.shared_len(c, "Dq", [u1, a1, u3]) == 0
              and B.anchor(c, "Dq") is None)

        print("\n[5] 挑选式搬家（kind=forged）：带过来的几段在原会话里可以不连续")
        u5, a5, u6 = ("user", "那就周六见"), ("assistant", "周六见"), ("user", "搬家以后第一句")
        qid = add(c, "Q", [u1, a1, u2, a2, u3, a3, u5, a5])
        f = B.register(c, "Fq", {"conversation_id": "Q"}, [u1, a1, u5, a5, u6, ("assistant", "嗯")], NOW, kind="forged")
        check("登记：挑过来的 4 条对上原会话的第 1、2、7、8 行", f and f["kind"] == "forged" and f["shared"] == 4
              and [e[2] for e in f["prefix"]] == [qid[0], qid[1], qid[6], qid[7]], f and f["prefix"])
        check("以后存记录跳过这 4 条；分支点是最后一条挑过来的",
              B.shared_len(c, "Fq", [u1, a1, u5, a5, u6, ("assistant", "嗯"), ("user", "再说一句")]) == 4
              and B.anchor(c, "Fq") == ("Q", qid[7]))
        check("分支照旧是 branch", B.get(c, "Bq")["kind"] == "branch")
        f2 = B.register(c, "Fr", {"conversation_id": "Q"}, [u1, a1, u3, ("assistant", "好")], NOW, kind="forged")
        check("这一轮的话不算挑过来的（哪怕原会话里有一样的）", f2["shared"] == 2, f2["shared"])
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
