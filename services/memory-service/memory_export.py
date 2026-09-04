# -*- coding: utf-8 -*-
"""记忆库整体导出/导入（zip）+ feel 单独导出/导入。"""
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

EXPORT_TABLES = ['l0_messages', 'l1_memories', 'thinking_records',
                 'shared_narrative', 'recent_summary', 'custom_prompts',
                 'conv_settings']


def export_memory_zip(db_path, profile_dir) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        manifest = {'format': 'memory-export', 'version': 1,
                    'createdAtUtc': datetime.utcnow().isoformat(),
                    'tables': {}, 'profiles': []}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for t in EXPORT_TABLES:
            try:
                rows = [dict(r) for r in conn.execute(f'SELECT * FROM {t}').fetchall()]
                z.writestr(f'{t}.json', json.dumps(rows, ensure_ascii=False, default=str))
                manifest['tables'][t] = len(rows)
            except Exception as e:
                manifest['tables'][t] = f'ERR:{e}'
        conn.close()
        pdir = Path(profile_dir)
        if pdir.exists():
            for f in sorted(pdir.glob('*.md')):
                z.writestr(f'profile/{f.name}', f.read_text(encoding='utf-8'))
                manifest['profiles'].append(f.name)
        z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=1))
    return buf.getvalue()


def import_memory_zip(zip_bytes, db_path, profile_dir, mode='merge'):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        conn = sqlite3.connect(db_path)
        stats = {}
        for t in EXPORT_TABLES:
            name = f'{t}.json'
            if name not in z.namelist():
                continue
            try:
                rows = json.loads(z.read(name))
            except Exception:
                continue
            if not rows:
                stats[t] = 0
                continue
            cols_def = [r[1] for r in conn.execute(f'PRAGMA table_info({t})').fetchall()]
            cols = ','.join(c for c in cols_def if c in rows[0])
            if not cols:
                continue
            ph = ','.join('?' * len(cols.split(',')))
            conn.executemany(
                f'INSERT OR IGNORE INTO {t} ({cols}) VALUES ({ph})',
                [tuple(r.get(c) for c in cols.split(',')) for r in rows])
            stats[t] = len(rows)
        pdir = Path(profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        for n in z.namelist():
            if n.startswith('profile/') and n.endswith('.md'):
                (pdir / n.split('/')[-1]).write_bytes(z.read(n))
                stats['profile'] = stats.get('profile', 0) + 1
        conn.commit()
        conn.close()
    return stats


def export_feel(db_path) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM l1_memories WHERE event_type='feel' AND status='active'").fetchall()]
    conn.close()
    return rows


def import_feel(db_path, rows, chroma_add=None) -> int:
    """导入 feel（l1_memories 子集），按 content+event_type 去重；chroma_add(l1_id,content) 可选补向量。

    完整保留所有字段：anchor_json（和弦锚点）, display_count, last_shown_at, visibility, supersedes
    """
    conn = sqlite3.connect(db_path)
    saved = 0
    for r in rows:
        content = (r.get('content') or '').strip()
        if not content:
            continue
        dup = conn.execute(
            "SELECT 1 FROM l1_memories WHERE content=? AND event_type='feel' AND status='active' LIMIT 1",
            (content,)).fetchone()
        if dup:
            continue

        # 完整字段列表（包括新增字段）
        cur = conn.execute(
            """INSERT INTO l1_memories
            (content, quote, conv_id, client, event_type, tags, valence, arousal, ts, status, is_core,
             anchor_json, display_count, last_shown_at, visibility, supersedes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (content, r.get('quote') or content, r.get('conv_id') or '', r.get('client') or 'ai_self',
             'feel', r.get('tags') or '["感受"]', r.get('valence'), r.get('arousal'),
             r.get('ts') or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'active',
             r.get('is_core') or 0,
             # 新增字段（保留原值或使用默认值）
             r.get('anchor_json'),           # 和弦情绪锚点
             r.get('display_count') or 0,    # 展示计数
             r.get('last_shown_at'),         # 最后展示时间
             r.get('visibility') or 1.0,     # 可见度
             r.get('supersedes')             # 修正关系
            ))
        l1_id = cur.lastrowid
        saved += 1
        if chroma_add:
            try:
                chroma_add(l1_id, content)
            except Exception:
                pass
    conn.commit()
    conn.close()
    return saved


def export_l1_with_context(db_path, filters=None) -> dict:
    """导出 L1 记忆 + 关联的 L0 上下文片段（完整版）

    filters: {"event_type": "preference_change", "is_core": 1, ...}

    返回格式：
    {
        "format": "l1-with-context",
        "version": 1,
        "exported_at": "2026-09-04T15:00:00",
        "l1_memories": [...],  # 完整 L1 行
        "l0_context": {
            "123": {  # 旧 L0 id
                "anchor_msg_idx": 100,
                "conv_id": "xxx",
                "context": [...]  # 前后各2条 L0 消息
            }
        }
    }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. 导出 L1
    query = "SELECT * FROM l1_memories WHERE status='active'"
    params = []
    if filters:
        for k, v in filters.items():
            query += f" AND {k}=?"
            params.append(v)

    l1_rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    # 2. 收集需要导出的 L0 id
    l0_ids = set()
    for l1 in l1_rows:
        if l1.get('source_msg_id'):
            l0_ids.add(l1['source_msg_id'])

    # 3. 导出关联的 L0 消息（包括上下文：前后各2条）
    l0_context = {}
    for l0_id in l0_ids:
        # 找到这条 L0 的 msg_idx
        anchor = conn.execute(
            "SELECT msg_idx, conv_id FROM l0_messages WHERE id=? AND status='active'",
            (l0_id,)
        ).fetchone()

        if anchor:
            msg_idx, conv_id = anchor['msg_idx'], anchor['conv_id']
            # 导出前后各2条（共5条上下文）
            context_rows = conn.execute(
                "SELECT id, msg_idx, role, content, ts, conv_id, client "
                "FROM l0_messages "
                "WHERE conv_id=? AND status='active' AND msg_idx BETWEEN ? AND ? "
                "ORDER BY msg_idx ASC",
                (conv_id, max(0, msg_idx - 2), msg_idx + 2)
            ).fetchall()

            l0_context[str(l0_id)] = {
                'anchor_msg_idx': msg_idx,
                'conv_id': conv_id,
                'context': [dict(r) for r in context_rows]
            }

    conn.close()

    return {
        'format': 'l1-with-context',
        'version': 1,
        'exported_at': datetime.utcnow().isoformat(),
        'l1_memories': l1_rows,
        'l0_context': l0_context
    }


def import_l1_with_context(db_path, data, chroma_add=None) -> dict:
    """导入 L1 记忆，智能重映射 source_msg_id（完整版）

    策略：
    1. 如果 L0 上下文在数据库中能找到（按 conv_id + msg_idx + content 匹配），重映射 source_msg_id
    2. 如果找不到，创建一个"孤立"的 L1（source_msg_id=NULL，但不影响导入）

    返回统计：
    {
        "imported": 10,   # 成功导入的 L1 数量
        "remapped": 8,    # 成功重映射 source_msg_id 的数量
        "orphaned": 2,    # 找不到 L0 对应的数量（变成孤立 L1）
        "skipped": 5      # 因重复跳过的数量
    }
    """
    conn = sqlite3.connect(db_path)
    stats = {'imported': 0, 'remapped': 0, 'orphaned': 0, 'skipped': 0}

    old_to_new_id = {}  # {旧 L0 id: 新 L0 id}

    # 1. 尝试重映射 L0 id（按内容匹配）
    for old_l0_id, context_data in data.get('l0_context', {}).items():
        conv_id = context_data['conv_id']
        anchor_msg_idx = context_data['anchor_msg_idx']
        anchor_content = None

        # 找到锚点消息的内容
        for ctx in context_data['context']:
            if ctx['msg_idx'] == anchor_msg_idx:
                anchor_content = ctx['content']
                break

        if not anchor_content:
            continue

        # 在新数据库中搜索匹配的 L0（按 conv_id + msg_idx 范围 + content 精确匹配）
        matches = conn.execute(
            """SELECT id, msg_idx FROM l0_messages
               WHERE conv_id=? AND status='active'
               AND msg_idx BETWEEN ? AND ?
               AND REPLACE(REPLACE(content, char(10), ''), char(13), '')
                   = REPLACE(REPLACE(?, char(10), ''), char(13), '')
               LIMIT 1""",
            (conv_id, max(0, anchor_msg_idx - 5), anchor_msg_idx + 5, anchor_content)
        ).fetchone()

        if matches:
            old_to_new_id[int(old_l0_id)] = matches[0]
            stats['remapped'] += 1

    # 2. 导入 L1，使用重映射的 source_msg_id
    for l1 in data.get('l1_memories', []):
        content = (l1.get('content') or '').strip()
        if not content:
            continue

        # 去重检查（按 content + event_type）
        dup = conn.execute(
            "SELECT 1 FROM l1_memories WHERE content=? AND event_type=? AND status='active' LIMIT 1",
            (content, l1.get('event_type'))
        ).fetchone()

        if dup:
            stats['skipped'] += 1
            continue

        # 重映射 source_msg_id
        old_source_id = l1.get('source_msg_id')
        new_source_id = None
        if old_source_id:
            new_source_id = old_to_new_id.get(old_source_id)
            if not new_source_id:
                stats['orphaned'] += 1  # 找不到对应的 L0，变成孤立 L1

        # 插入 L1（完整字段）
        cur = conn.execute(
            """INSERT INTO l1_memories
            (content, quote, source_msg_id, conv_id, client, event_type, tags,
             valence, arousal, is_core, ts, status,
             anchor_json, display_count, last_shown_at, visibility, supersedes, access_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                content,
                l1.get('quote'),
                new_source_id,  # 重映射后的 id（可能为 NULL）
                l1.get('conv_id'),
                l1.get('client'),
                l1.get('event_type'),
                l1.get('tags'),
                l1.get('valence'),
                l1.get('arousal'),
                l1.get('is_core') or 0,
                l1.get('ts') or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'active',
                l1.get('anchor_json'),
                l1.get('display_count') or 0,
                l1.get('last_shown_at'),
                l1.get('visibility') or 1.0,
                l1.get('supersedes'),
                l1.get('access_count') or 0
            )
        )

        stats['imported'] += 1

        # 补充向量
        if chroma_add:
            try:
                chroma_add(cur.lastrowid, content)
            except Exception:
                pass

    conn.commit()
    conn.close()
    return stats


def export_l1_simple(db_path, filters=None) -> list:
    """导出 L1 记忆（简化版，不保留 L0 关联）

    filters: {"event_type": "preference_change", "is_core": 1, ...}

    返回完整的 L1 行数据（包括所有字段）
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM l1_memories WHERE status='active'"
    params = []
    if filters:
        for k, v in filters.items():
            query += f" AND {k}=?"
            params.append(v)

    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return rows


def import_l1_simple(db_path, rows, chroma_add=None) -> int:
    """导入 L1 记忆（简化版，source_msg_id 全部设为 NULL）

    适用场景：
    - 只需要保留记忆内容，不需要回溯原文上下文
    - 跨会话/跨系统迁移记忆
    """
    conn = sqlite3.connect(db_path)
    saved = 0

    for r in rows:
        content = (r.get('content') or '').strip()
        if not content:
            continue

        # 去重检查
        dup = conn.execute(
            "SELECT 1 FROM l1_memories WHERE content=? AND event_type=? AND status='active' LIMIT 1",
            (content, r.get('event_type'))
        ).fetchone()

        if dup:
            continue

        # 完整字段，但 source_msg_id 设为 NULL
        cur = conn.execute(
            """INSERT INTO l1_memories
            (content, quote, source_msg_id, conv_id, client, event_type, tags,
             valence, arousal, is_core, ts, status,
             anchor_json, display_count, last_shown_at, visibility, supersedes, access_count)
            VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                content,
                r.get('quote'),
                # source_msg_id 固定为 NULL
                r.get('conv_id'),
                r.get('client'),
                r.get('event_type'),
                r.get('tags'),
                r.get('valence'),
                r.get('arousal'),
                r.get('is_core') or 0,
                r.get('ts') or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'active',
                r.get('anchor_json'),
                r.get('display_count') or 0,
                r.get('last_shown_at'),
                r.get('visibility') or 1.0,
                r.get('supersedes'),
                r.get('access_count') or 0
            )
        )

        saved += 1

        if chroma_add:
            try:
                chroma_add(cur.lastrowid, content)
            except Exception:
                pass

    conn.commit()
    conn.close()
    return saved