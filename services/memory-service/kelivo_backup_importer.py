# -*- coding: utf-8 -*-
"""kelivo-backup (v2 sqlite) 导入器：解析备份 zip → 预览/选择会话/版本折叠 → 写入记忆库 L0 / thinking_records。

备份格式（kelivo v1.2.2+ / v1.2.3 导出）：
  kelivo_backup_*.zip
    database/kelivo.db   # drift SQLite（conversation_rows / message_rows / message_part_rows）
    manifest.json

关键设计：
- 版本折叠：message_rows 含全部版本（同 group 多条），conversation.version_selections_json 记录
  用户当前选择的版本（{groupId: 版本号}）→ 导入取"用户选择版本"；无选择取最高版本。
- 会话选择：convs 参数可指定导入部分会话；preview 返回会话列表。
- 保留旧版导入（convert_kelivo.py / import_conversation JSON 路径）不动。
"""
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


def _iso_ts(micro) -> str:
    try:
        return datetime.utcfromtimestamp(int(micro) / 1_000_000).strftime(
            '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def _open_backup_db(zip_path: str):
    with zipfile.ZipFile(zip_path) as z:
        names = [n.replace('\\', '/') for n in z.namelist()]
        dbname = next((n for n in names if n.endswith('kelivo.db')), None)
        if not dbname:
            raise ValueError('backup: kelivo.db not found in zip')
        blob = z.read(dbname)
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.write(blob)
    tmp.close()
    return sqlite3.connect(tmp.name), tmp.name


def preview_kelivo_backup(zip_path: str) -> list:
    """返回备份中的会话列表：{conv_id, title, msg_count, first_ts, last_ts}"""
    conn, tmp_path = _open_backup_db(zip_path)
    try:
        rows = conn.execute(
            "SELECT c.id, c.title, COUNT(m.id), MIN(m.timestamp), MAX(m.timestamp) "
            "FROM conversation_rows c LEFT JOIN message_rows m ON m.conversation_id=c.id "
            "GROUP BY c.id ORDER BY MAX(m.timestamp) DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                'conv_id': r[0],
                'title': r[1] or '',
                'msg_count': r[2] or 0,
                'first_ts': _iso_ts(r[3]),
                'last_ts': _iso_ts(r[4]),
            })
        return out
    finally:
        conn.close()
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass


def _conv_existing(conn, conv_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM l0_messages WHERE conv_id=? AND status='active' LIMIT 1",
        (conv_id,)).fetchone() is not None


def _collapse_rows(rows, sel: dict):
    """按 group 版本折叠：选择版本优先，无选择取最高；无 group 消息按原序保留。

    rows: [(mid, role, ts, gid, ver, order)] 已按 message_order 升序
    返回折叠后的行列表（同样元组），顺序=首次出现位置。
    """
    by_group = {}
    group_first_order = {}
    raw = []
    for r in rows:
        mid, role, ts, gid, ver, order = r
        if gid:
            if gid not in by_group:
                by_group[gid] = []
                group_first_order[gid] = order
            by_group[gid].append(r)
        else:
            raw.append(r)
    final = []
    for gid, group_rows in by_group.items():
        target = sel.get(gid)
        pick = None
        if target is not None:
            for x in group_rows:
                if x[4] == target:
                    pick = x
                    break
        if pick is None:
            pick = max(group_rows, key=lambda x: x[4])  # 最高版本
        final.append((group_first_order[gid], pick))
    for r in raw:
        final.append((r[5], r))
    final.sort(key=lambda x: x[0])
    return [x[1] for x in final]


def _import_conv(mem_conn, cid, messages, thinkings, mode):
    saved = 0
    for idx, m in enumerate(messages):
        gid = m.get('group_id') or m.get('id')
        ver = int(m.get('version') or 0)
        if mode == 'skip':
            dup = mem_conn.execute(
                "SELECT id FROM l0_messages WHERE conv_id=? AND role=? AND status='active' AND "
                "REPLACE(REPLACE(content,char(10),''),char(13),'')=REPLACE(REPLACE(?,char(10),''),char(13),'') LIMIT 1",
                (cid, m['role'], m['content'])).fetchone()
            if dup:
                continue
        anchor_val = None
        if gid:
            old = mem_conn.execute(
                "SELECT id, version, group_anchor FROM l0_messages WHERE conv_id=? AND group_id=? AND status='active' ORDER BY id LIMIT 1",
                (cid, gid)).fetchone()
            if old:
                if ver <= (old[1] or 0):
                    continue
                mem_conn.execute("UPDATE l0_messages SET status='superseded' WHERE id=?", (old[0],))
                anchor_val = old[2] if old[2] else old[0]
        cur = mem_conn.execute(
            "INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted, group_id, version, group_anchor) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, idx, m['role'], m['content'], m['ts'] or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
             'kelivo', 'active', 0, gid, ver, anchor_val))
        gid_ins = cur.lastrowid
        mem_conn.execute("UPDATE l0_messages SET group_anchor=? WHERE id=?",
                         (anchor_val or gid_ins, gid_ins))
        saved += 1
    t_saved = 0
    for t in thinkings:
        dup = mem_conn.execute(
            "SELECT 1 FROM thinking_records WHERE conv_id=? AND answer_ref=? LIMIT 1",
            (cid, t['answer_ref'], t.get('full_reply'))).fetchone()
        if dup:
            continue
        mem_conn.execute(
            "INSERT INTO thinking_records (conv_id, ts, thinking, answer_ref, full_reply) VALUES (?,?,?,?,?)",
            (cid, t['ts'] or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), t['thinking'], t['answer_ref']))
        t_saved += 1
    return saved, t_saved


def import_kelivo_backup(zip_path: str, mode: str = 'skip', memory_db_path: str = '',
                         convs=None):
    """主入口。convs: 指定导入的 conv_id 列表（None/空=全部）"""
    if not memory_db_path:
        raise ValueError('memory_db_path required')
    db_conn, tmp_path = _open_backup_db(zip_path)
    mem_conn = sqlite3.connect(memory_db_path)
    try:
        convs = set(convs or [])
        conv_rows = db_conn.execute(
            "SELECT id, title, created_at FROM conversation_rows").fetchall()
        if convs:
            conv_rows = [c for c in conv_rows if c[0] in convs]
        stats = {'convs': 0, 'messages': 0, 'thinkings': 0, 'skipped_convs': 0}
        for conv in conv_rows:
            cid = conv[0]
            if mode == 'skip' and _conv_existing(mem_conn, cid):
                stats['skipped_convs'] += 1
                continue

            vsj_raw = db_conn.execute(
                "SELECT version_selections_json FROM conversation_rows WHERE id=?",
                (cid,)).fetchone()
            sel = {}
            if vsj_raw and vsj_raw[0]:
                try:
                    sel = json.loads(vsj_raw[0]) or {}
                except Exception:
                    sel = {}

            rows = db_conn.execute(
                "SELECT id, role, timestamp, group_id, version, message_order "
                "FROM message_rows WHERE conversation_id=? ORDER BY message_order",
                (cid,)).fetchall()
            # 折叠：传给 _collapse_rows 的元组 (mid, role, ts, gid, ver, order)
            collapsed = _collapse_rows(rows, sel)
            msgs = []
            thinkings = []
            for (mid, role, ts, gid, ver, _order) in collapsed:
                parts = db_conn.execute(
                    "SELECT kind, payload FROM message_part_rows WHERE conversation_id=? AND revision_id=? ORDER BY ordinal",
                    (cid, mid)).fetchall()
                text = '\n'.join(
                    p[1] for p in parts if p[0] == 'text' and p[1]
                ).strip()
                if not text:
                    continue
                if role not in ('user', 'assistant'):
                    continue
                msgs.append({
                    'role': role, 'content': text, 'ts': _iso_ts(ts),
                    'group_id': gid or mid, 'version': ver or 0,
                })
                if role == 'assistant':
                    reasoning = db_conn.execute(
                        "SELECT reasoning_segments_json FROM message_rows WHERE id=?",
                        (mid,)).fetchone()
                    if reasoning and reasoning[0]:
                        try:
                            seg = json.loads(reasoning[0])
                            parts_txt = [s.get('text', '') for s in (seg.get('segments') or [])]
                            thinking_text = '\n'.join(x for x in parts_txt if x).strip()
                            if thinking_text:
                                thinkings.append({
                                    'thinking': thinking_text,
                                    'answer_ref': text[:200],
                                    'full_reply': text,
                                    'ts': _iso_ts(ts),
                                })
                        except Exception:
                            pass
            if not msgs:
                continue
            saved, t_saved = _import_conv(mem_conn, cid, msgs, thinkings, mode)
            stats['convs'] += 1
            stats['messages'] += saved
            stats['thinkings'] += t_saved
        mem_conn.commit()
        return stats
    finally:
        db_conn.close()
        mem_conn.close()
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass