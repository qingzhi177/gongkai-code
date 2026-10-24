"""大文件分块上传和导入任务（app/uploads.py）、Kelivo 备份导入（kelivo_backup_importer.py）的测试：
临时目录 + 编出来的数据，不碰真实记忆库。

运行：python3 tests/manual/test_uploads.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="uploadtest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app import uploads as U  # noqa: E402
import kelivo_backup_importer as K  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def raises(fn):
    try:
        fn()
        return None
    except ValueError as e:
        return str(e)


def kelivo_db(path):
    """编一个最小的 kelivo.db：两个会话，一条消息有两个版本（用户选了第 2 版），一条带思考"""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE conversation_rows (id TEXT PRIMARY KEY, title TEXT, created_at INTEGER, version_selections_json TEXT)")
    c.execute("CREATE TABLE message_rows (id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, timestamp INTEGER, group_id TEXT, "
              "version INTEGER, message_order INTEGER, reasoning_segments_json TEXT)")
    c.execute("CREATE TABLE message_part_rows (conversation_id TEXT, revision_id TEXT, ordinal INTEGER, kind TEXT, payload TEXT)")
    t = 1_780_000_000_000_000
    c.execute("INSERT INTO conversation_rows VALUES ('c1', '第一个会话', ?, ?)", (t, json.dumps({"g2": 1})))
    c.execute("INSERT INTO conversation_rows VALUES ('c2', '第二个会话', ?, NULL)", (t,))
    rows = [("m1", "c1", "user", t, "g1", 0, 0, None), ("m2a", "c1", "assistant", t + 1, "g2", 0, 1, None),
            ("m2b", "c1", "assistant", t + 2, "g2", 1, 1, json.dumps({"segments": [{"text": "想一想"}]})),
            ("m3", "c2", "user", t + 3, "g3", 0, 0, None)]
    c.executemany("INSERT INTO message_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    parts = [("c1", "m1", 0, "text", "你好呀 (ᗜ֊ᗜ)"), ("c1", "m2a", 0, "text", "第一版回复"), ("c1", "m2b", 0, "text", "第二版回复"),
             ("c2", "m3", 0, "text", "另一个会话")]
    c.executemany("INSERT INTO message_part_rows VALUES (?, ?, ?, ?, ?)", parts)
    c.commit()
    c.close()


def upload(data, name, chunk_order=None):
    st = U.start(name, len(data))
    order = chunk_order or list(range(st["chunks"]))
    for i in order:
        U.put_chunk(st["upload_id"], i, data[i * st["chunk_size"]:(i + 1) * st["chunk_size"]])
    return st, U.finish(st["upload_id"])


def main():
    try:
        U.CHUNK_SIZE = 1000             # 测试时块小一点
        print("\n[1] 分块上传")
        data = os.urandom(3500)
        st, r = upload(data, "备份.zip", [2, 0, 3, 1])
        check("切成 4 块，块的大小按服务器说的", st["chunks"] == 4 and st["chunk_size"] == 1000)
        check("乱序传也能拼对，扩展名保留", Path(r["path"]).read_bytes() == data and r["path"].endswith(".zip") and r["size"] == 3500, r)
        check("文件放在 data/run/uploads 里", Path(r["path"]).parent == Path(os.environ["DATA_DIR"]) / "run" / "uploads")
        check("已经完成的再 finish 一次：返回同一个文件", U.finish(st["upload_id"])["path"] == r["path"])
        st = U.start("a.db", 2500)
        U.put_chunk(st["upload_id"], 0, data[:1000])
        U.put_chunk(st["upload_id"], 0, data[:1000])
        U.put_chunk(st["upload_id"], 1, data[1000:2000])
        check("同一块重传不出错", U.put_chunk(st["upload_id"], 2, data[2000:2500])["received"] == 3)
        st = U.start("x.exe", 10)
        U.put_chunk(st["upload_id"], 0, b"0123456789")
        check("扩展名不在名单里的改成 .bin", U.finish(st["upload_id"])["path"].endswith(".bin"))

        print("\n[2] 不对的情况")
        st = U.start("b.zip", 2500)
        uid = st["upload_id"]
        check("块的大小不对", "大小不对" in (raises(lambda: U.put_chunk(uid, 0, b"x" * 999)) or ""))
        check("最后一块按剩下的大小算", "大小不对" in (raises(lambda: U.put_chunk(uid, 2, b"x" * 1000)) or ""))
        check("块的编号超出范围", "超出范围" in (raises(lambda: U.put_chunk(uid, 3, b"x")) or ""))
        U.put_chunk(uid, 0, b"x" * 1000)
        check("还有块没传就 finish：报错，说是第几块", "第 2 块" in (raises(lambda: U.finish(uid)) or ""))
        check("上传编号不对（防止拿去读别的文件）", "编号不对" in (raises(lambda: U.put_chunk("../../etc/passwd", 0, b"")) or ""))
        check("没有这次上传", "没有这次上传" in (raises(lambda: U.finish("0" * 32)) or ""))
        check("空文件、太大的文件", raises(lambda: U.start("c", 0)) and raises(lambda: U.start("c", U.MAX_SIZE + 1)))
        old = U.upload_dir() / "old.part"
        old.write_bytes(b"1")
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        U.start("d.zip", 5)
        check("两天以前的上传自动删掉", not old.exists())

        print("\n[3] 后台任务")
        tid = U.run_task("demo", lambda a, b: a + b, 1, 2)
        for _ in range(50):
            if U.task(tid)["state"] != "running":
                break
            time.sleep(0.05)
        check("做完了，有结果", U.task(tid)["state"] == "done" and U.task(tid)["result"] == 3, U.task(tid))

        def boom():
            raise ValueError("备份坏了")
        tid = U.run_task("demo", boom)
        for _ in range(50):
            if U.task(tid)["state"] != "running":
                break
            time.sleep(0.05)
        check("出错了，记下原因", U.task(tid)["state"] == "error" and U.task(tid)["error"] == "备份坏了", U.task(tid))
        check("没有这个任务", U.task("nope") is None)

        print("\n[4] Kelivo 备份：zip 和单独的 kelivo.db 都能读")
        kdb = TMP / "kelivo.db"
        kelivo_db(str(kdb))
        zpath = TMP / "kelivo_backup.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(kdb, "database/kelivo.db")
            z.writestr("upload/big_image.png", os.urandom(20000))
            z.writestr("manifest.json", "{}")
        before = hashlib.md5(kdb.read_bytes()).hexdigest()
        tmps = set(Path(tempfile.gettempdir()).glob("tmp*.db"))
        pz = K.preview_kelivo_backup(str(zpath))
        pd = K.preview_kelivo_backup(str(kdb))
        check("预览：zip 和 kelivo.db 一样，两个会话", pz == pd and sorted(c["conv_id"] for c in pz) == ["c1", "c2"], (pz, pd))
        check("读完不动原来的文件（索引建在临时副本上），临时副本用完就删", hashlib.md5(kdb.read_bytes()).hexdigest() == before
              and set(Path(tempfile.gettempdir()).glob("tmp*.db")) == tmps)
        bad = TMP / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("nothing.txt", "x")
        try:
            K.preview_kelivo_backup(str(bad))
            check("zip 里没有 kelivo.db：报错", False)
        except ValueError:
            check("zip 里没有 kelivo.db：报错", True)
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)
        stats = K.import_kelivo_backup(str(kdb), mode="skip", memory_db_path=str(config.SQLITE_PATH), convs=["c1"])
        rows = sqlite3.connect(str(config.SQLITE_PATH)).execute(
            "SELECT role, content FROM l0_messages WHERE conv_id='c1' AND status='active' ORDER BY msg_idx").fetchall()
        check("直接导入 kelivo.db：只导选中的会话，取用户选的那一版，带思考记录", stats["convs"] == 1 and stats["thinkings"] == 1
              and [c for _, c in rows] == ["你好呀 (ᗜ֊ᗜ)", "第二版回复"], (stats, rows))
        stats = K.import_kelivo_backup(str(zpath), mode="skip", memory_db_path=str(config.SQLITE_PATH))
        check("再导入整个 zip：已有的会话跳过，新的导进来", stats["skipped_convs"] == 1 and stats["convs"] == 1, stats)

        print("\n[5] Kelivo 真实的版本结构、重复的话、图片")
        k2 = TMP / "kelivo2.db"
        c = sqlite3.connect(k2)
        c.execute("CREATE TABLE conversation_rows (id TEXT PRIMARY KEY, title TEXT, created_at INTEGER, version_selections_json TEXT)")
        c.execute("CREATE TABLE message_rows (id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, timestamp INTEGER, group_id TEXT, "
                  "version INTEGER, message_order INTEGER, reasoning_segments_json TEXT)")
        c.execute("CREATE TABLE message_part_rows (conversation_id TEXT, revision_id TEXT, ordinal INTEGER, kind TEXT, payload TEXT)")
        t = 1_780_000_000_000_000
        # a1 的第一个版本 group_id 是空的，后来的版本 a1v1、a1v2 指向它；用户选的是第一个版本（0）
        # b1 的第一个版本 group_id 等于自己的 id；用户选的是版本 1
        c.execute("INSERT INTO conversation_rows VALUES ('k', '会话', ?, ?)", (t, json.dumps({"a1": 0, "b1": 1})))
        rows = [("u1", "user", 0, None, 0, 0), ("a1", "assistant", 1, None, 0, 1), ("a1v1", "assistant", 2, "a1", 1, 2),
                ("a1v2", "assistant", 3, "a1", 2, 3), ("u2", "user", 4, None, 0, 4), ("b1", "assistant", 5, "b1", 0, 5),
                ("b1v1", "assistant", 6, "b1", 1, 6), ("u3", "user", 7, None, 0, 7), ("c1", "assistant", 8, None, 0, 8),
                ("u4", "user", 9, None, 0, 9), ("d1", "assistant", 10, None, 0, 10)]
        c.executemany("INSERT INTO message_rows VALUES (?, 'k', ?, ?, ?, ?, ?, NULL)", [(i, r, t + n * 1000000, g, v, o) for i, r, n, g, v, o in rows])
        parts = [("u1", "text", "晚安"), ("a1", "text", "晚安，选中的第一个版本"), ("a1v1", "text", "第二个版本"), ("a1v2", "text", "第三个版本"),
                 ("u2", "image", json.dumps({"uri": "kelivo-file:///upload/sky%201.jpg"})), ("u2", "text", "看看今天的天"),
                 ("b1", "text", "没选的第一个版本"), ("b1v1", "text", "选中的版本 1"),
                 ("u3", "text", "晚安"), ("c1", "text", "晚安呀"), ("u4", "image", json.dumps({"uri": "kelivo-file:///upload/cat.png"})),
                 ("d1", "text", "好可爱")]
        c.executemany("INSERT INTO message_part_rows VALUES ('k', ?, 0, ?, ?)", parts)
        c.commit()
        c.close()
        stats = K.import_kelivo_backup(str(k2), mode="skip", memory_db_path=str(config.SQLITE_PATH), convs=["k"])
        got = sqlite3.connect(str(config.SQLITE_PATH)).execute(
            "SELECT content FROM l0_messages WHERE conv_id='k' AND status='active' ORDER BY msg_idx").fetchall()
        got = [x[0] for x in got]
        check("用户选的是第一个版本（group_id 空）：导入第一个版本，不是最高版本", "晚安，选中的第一个版本" in got and "第三个版本" not in got, got)
        check("第一个版本 group_id 等于自己的 id、选了版本 1：导入版本 1", "选中的版本 1" in got and "没选的第一个版本" not in got, got)
        check("同一个会话里一样的话（两次「晚安」）都留着", got.count("晚安") == 2, got)
        check("图片写成占位放在前面；只发了图的消息也导入", "[图片：sky 1.jpg]\n看看今天的天" in got and "[图片：cat.png]" in got, got)
        check("顺序和备份里一样，没选的版本不导入", got == ["晚安", "晚安，选中的第一个版本", "[图片：sky 1.jpg]\n看看今天的天", "选中的版本 1",
                                                 "晚安", "晚安呀", "[图片：cat.png]", "好可爱"] and stats["messages"] == 8, (got, stats))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
