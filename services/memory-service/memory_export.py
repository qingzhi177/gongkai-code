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
    """导入 feel（l1_memories 子集），按 content+event_type 去重；chroma_add(l1_id,content) 可选补向量。"""
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
        cur = conn.execute(
            "INSERT INTO l1_memories (content, quote, conv_id, client, event_type, tags, valence, arousal, ts, status, is_core) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (content, r.get('quote') or content, r.get('conv_id') or '', r.get('client') or 'ai_self',
             'feel', r.get('tags') or '["感受"]', r.get('valence'), r.get('arousal'),
             r.get('ts') or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'active',
             r.get('is_core') or 0))
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