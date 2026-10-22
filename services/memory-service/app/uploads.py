"""大文件上传和导入任务（Dashboard「备份管理」）。

为什么要有：网站前面有 Cloudflare，一个请求的大小有上限（免费版 100 MB），一个请求等太久（100 秒左右）也会被断开。
以前整个备份文件用一个请求传，文件一大就一直停在「上传中」。现在：
- 分块上传：浏览器把文件切成小块（每块 CHUNK_SIZE）一块一块传，哪一块失败了自动重传，能看到进度；
  每一块按位置写进文件，重传同一块不会出错。传完以后核对块数和大小，文件放在 data/run/uploads/（两天后自动删）。
- 导入任务：导入在后台线程里跑，浏览器隔几秒问一次进度，不会因为等太久被断开，也不会卡住记忆服务的其他请求。
  任务状态只放在内存里（服务重启就没了，导入本身已经写进库的不受影响）。
"""
import json
import math
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import timeutil

CHUNK_SIZE = 5 * 1024 * 1024
MAX_SIZE = 4 * 1024 ** 3
KEEP_HOURS = 48
DISK_RESERVE = 1024 ** 3          # 至少留 1 GB 空闲
EXTS = {".zip", ".db", ".json", ".sqlite"}
_ID = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.Lock()


def upload_dir():
    from app.config import DATA_DIR
    d = Path(DATA_DIR) / "run" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _paths(uid):
    if not _ID.match(str(uid or "")):
        raise ValueError("上传编号不对")
    d = upload_dir()
    return d / f"{uid}.part", d / f"{uid}.json"


def _meta(uid):
    part, meta = _paths(uid)
    if not meta.exists():
        raise ValueError("没有这次上传（可能已经过期了，请重新选择文件）")
    return part, meta, json.loads(meta.read_text(encoding="utf-8"))


def cleanup(now=None):
    """删掉 KEEP_HOURS 小时以前的上传"""
    now = now or time.time()
    n = 0
    for f in upload_dir().iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > KEEP_HOURS * 3600:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def start(name, size):
    """开始一次上传：返回 {upload_id, chunk_size, chunks}"""
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise ValueError("文件大小不对")
    if size <= 0:
        raise ValueError("文件是空的")
    if size > MAX_SIZE:
        raise ValueError(f"文件太大了（最多 {MAX_SIZE // 1024 ** 3} GB）")
    cleanup()
    d = upload_dir()
    if shutil.disk_usage(d).free < size + DISK_RESERVE:
        raise ValueError("服务器磁盘空间不够放下这个文件")
    uid = uuid.uuid4().hex
    part, meta = _paths(uid)
    with open(part, "wb") as f:
        f.truncate(size)
    chunks = math.ceil(size / CHUNK_SIZE)
    meta.write_text(json.dumps({"name": str(name or "")[:200], "size": size, "chunk_size": CHUNK_SIZE, "chunks": chunks,
                                "received": [], "created_at": timeutil.utc_now_str(), "path": None}), encoding="utf-8")
    return {"upload_id": uid, "chunk_size": CHUNK_SIZE, "chunks": chunks}


def put_chunk(uid, index, data):
    """写第 index 块（从 0 开始）。返回 {received, chunks}"""
    with _lock:
        part, meta, m = _meta(uid)
        if m.get("path"):
            raise ValueError("这次上传已经完成了")
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError("块的编号不对")
        if not 0 <= index < m["chunks"]:
            raise ValueError("块的编号超出范围")
        offset = index * m["chunk_size"]
        expect = min(m["chunk_size"], m["size"] - offset)
        if len(data) != expect:
            raise ValueError(f"第 {index + 1} 块的大小不对（收到 {len(data)} 字节，应该是 {expect} 字节）")
        with open(part, "r+b") as f:
            f.seek(offset)
            f.write(data)
        got = set(m["received"])
        got.add(index)
        m["received"] = sorted(got)
        meta.write_text(json.dumps(m), encoding="utf-8")
        return {"received": len(got), "chunks": m["chunks"]}


def finish(uid):
    """核对块数和大小，拼好的文件改成正式的名字。返回 {path, name, size}"""
    with _lock:
        part, meta, m = _meta(uid)
        if m.get("path"):
            return {"path": m["path"], "name": m["name"], "size": m["size"]}
        missing = sorted(set(range(m["chunks"])) - set(m["received"]))
        if missing:
            raise ValueError(f"还有 {len(missing)} 块没传上来（第 {missing[0] + 1} 块起），请重新上传")
        if part.stat().st_size != m["size"]:
            raise ValueError("文件大小对不上，请重新上传")
        ext = Path(m["name"]).suffix.lower()
        final = part.with_suffix(ext if ext in EXTS else ".bin")
        part.rename(final)
        m["path"] = str(final)
        meta.write_text(json.dumps(m), encoding="utf-8")
        return {"path": m["path"], "name": m["name"], "size": m["size"]}


# ---------- 导入任务 ----------

_tasks = {}
MAX_TASKS = 50


def run_task(kind, fn, *args, **kwargs):
    """在后台线程里跑 fn，返回任务编号"""
    tid = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[tid] = {"id": tid, "kind": kind, "state": "running", "result": None, "error": None,
                       "started_at": timeutil.utc_now_str(), "finished_at": None}
        for old in sorted(_tasks, key=lambda k: _tasks[k]["started_at"])[:-MAX_TASKS]:
            if _tasks[old]["state"] != "running":
                _tasks.pop(old, None)

    def work():
        try:
            res = fn(*args, **kwargs)
            upd = {"state": "done", "result": res}
        except Exception as e:      # 导入出错：记下原因给 Dashboard 看
            upd = {"state": "error", "error": str(e) or type(e).__name__}
        with _lock:
            _tasks[tid].update(upd, finished_at=timeutil.utc_now_str())

    threading.Thread(target=work, name=f"import-{kind}-{tid}", daemon=True).start()
    return tid


def task(tid):
    with _lock:
        t = _tasks.get(str(tid))
        return dict(t) if t else None
