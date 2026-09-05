# Feel 导出导入功能分析与改进方案

## 📊 当前状态

### ✅ 现有功能
**位置**: `services/memory-service/memory_export.py`

#### 导出 feel
```python
def export_feel(db_path) -> list:
    # SELECT * FROM l1_memories WHERE event_type='feel' AND status='active'
    # 返回完整的行数据（包括所有字段）
```

#### 导入 feel
```python
def import_feel(db_path, rows, chroma_add=None) -> int:
    # INSERT INTO l1_memories (content, quote, conv_id, client, event_type, 
    #                          tags, valence, arousal, ts, status, is_core)
    # 按 content+event_type 去重
```

### ❌ 发现的问题

**导入时缺少 `anchor_json` 字段！**

```python
# 导出：SELECT * 包含了 anchor_json ✅
# 导入：INSERT 语句只有 11 个字段，缺少 anchor_json ❌
```

**影响**：
- 和弦情绪锚点（anchor_json）会丢失
- 其他新增字段（display_count, last_shown_at, visibility, supersedes）也会丢失

---

## 🔧 改进方案

### 方案 1：修复 import_feel() - 支持所有字段（推荐）

#### 修改 `memory_export.py` 的 `import_feel()` 函数

```python
def import_feel(db_path, rows, chroma_add=None) -> int:
    """导入 feel（l1_memories 子集），按 content+event_type 去重；chroma_add(l1_id,content) 可选补向量。
    
    保留所有字段：anchor_json, display_count, last_shown_at, visibility, supersedes
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
            (
                content, 
                r.get('quote') or content, 
                r.get('conv_id') or '', 
                r.get('client') or 'ai_self',
                'feel', 
                r.get('tags') or '["感受"]', 
                r.get('valence'), 
                r.get('arousal'),
                r.get('ts') or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 
                'active',
                r.get('is_core') or 0,
                # 新增字段（保留原值或使用默认值）
                r.get('anchor_json'),           # 和弦情绪锚点
                r.get('display_count') or 0,    # 展示计数
                r.get('last_shown_at'),         # 最后展示时间
                r.get('visibility') or 1.0,     # 可见度
                r.get('supersedes')             # 修正关系
            )
        )
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
```

**优点**：
- ✅ 和弦情绪锚点完整保留
- ✅ 所有新增字段都保留
- ✅ 向后兼容（旧导出的 JSON 中没有这些字段时用默认值）

**缺点**：
- 需要修改代码并重启服务

---

### 方案 2：通用 L1 导出导入功能（你提到的需求）

#### 设计思路

**与 feel 导出的区别**：
- feel 导出：只导出 `event_type='feel'` 的 L1，不需要关联 L0
- 通用 L1 导出：需要保持与 L0 的关联（`source_msg_id`）

**核心挑战**：`source_msg_id` 是 L0 的主键 id，重新导入后会失效

#### 实现方案 2A：L1 + L0 片段一起导出（完整关联）

```python
def export_l1_with_context(db_path, filters=None) -> dict:
    """导出 L1 记忆 + 关联的 L0 上下文片段
    
    filters: {"event_type": "preference_change", "is_core": 1, ...}
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
            
            l0_context[l0_id] = {
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
    """导入 L1 记忆，智能重映射 source_msg_id
    
    策略：
    1. 如果 L0 上下文在数据库中能找到（按 content 匹配），重映射 source_msg_id
    2. 如果找不到，创建一个"孤立"的 L1（source_msg_id=NULL）
    """
    conn = sqlite3.connect(db_path)
    stats = {'imported': 0, 'remapped': 0, 'orphaned': 0, 'skipped': 0}
    
    old_to_new_id = {}  # {旧 L0 id: 新 L0 id}
    
    # 1. 尝试重映射 L0 id
    for old_l0_id, context_data in data['l0_context'].items():
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
        
        # 在新数据库中搜索匹配的 L0（按 conv_id + msg_idx + content 模糊匹配）
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
    for l1 in data['l1_memories']:
        content = (l1.get('content') or '').strip()
        if not content:
            continue
        
        # 去重检查
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
             anchor_json, display_count, last_shown_at, visibility, supersedes) 
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                content,
                l1.get('quote'),
                new_source_id,  # 重映射后的 id
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
                l1.get('supersedes')
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
```

**API 接口**：
```python
@app.get("/export/l1")
async def export_l1_api(
    event_type: Optional[str] = None,
    is_core: Optional[int] = None
):
    """导出 L1 记忆（带 L0 上下文）"""
    from memory_export import export_l1_with_context
    filters = {}
    if event_type:
        filters['event_type'] = event_type
    if is_core is not None:
        filters['is_core'] = is_core
    
    data = export_l1_with_context(str(SQLITE_PATH), filters)
    return JSONResponse(data)


@app.post("/import/l1")
async def import_l1_api(req: dict):
    """导入 L1 记忆（智能重映射 source_msg_id）"""
    from memory_export import import_l1_with_context
    
    # chroma_add 回调
    def chroma_add(l1_id, content):
        embedding = get_embedding_sync(content)
        if embedding:
            l1_collection.add(
                ids=[f"l1_{l1_id}"],
                embeddings=[embedding],
                documents=[content],
                metadatas=[{"id": l1_id}]
            )
    
    stats = import_l1_with_context(str(SQLITE_PATH), req, chroma_add)
    return stats
```

**优点**：
- ✅ 智能重映射 source_msg_id（按内容匹配）
- ✅ 完整保留所有字段
- ✅ 支持筛选导出（按 event_type、is_core 等）
- ✅ 找不到对应 L0 时变成孤立 L1（不会失败）

**缺点**：
- 需要新增代码
- 重映射成功率取决于 L0 内容是否变化（编辑过的消息匹配不上）

---

#### 实现方案 2B：简化版 - 不保留 L0 关联

```python
def export_l1_simple(db_path, filters=None) -> list:
    """简化版：只导出 L1，不保留 L0 关联"""
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
    """简化版：导入 L1，source_msg_id 全部设为 NULL"""
    conn = sqlite3.connect(db_path)
    saved = 0
    
    for r in rows:
        content = (r.get('content') or '').strip()
        if not content:
            continue
        
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
             anchor_json, display_count, last_shown_at, visibility, supersedes) 
            VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                r.get('supersedes')
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
```

**优点**：
- ✅ 实现简单
- ✅ 不依赖 L0 状态
- ✅ 所有字段都保留（包括 anchor_json）

**缺点**：
- ❌ 丢失 L0 关联（source_msg_id=NULL）
- ❌ 导入后 `get_l0_context()` 无法找到上下文

---

## 📋 总结建议

### 1. 立即修复：feel 导出导入缺少 anchor_json

**修改文件**：`services/memory-service/memory_export.py`

**改动**：
- 修改 `import_feel()` 函数的 INSERT 语句
- 添加 `anchor_json, display_count, last_shown_at, visibility, supersedes` 字段

**影响**：
- ✅ 和弦情绪锚点完整保留
- ✅ 向后兼容

**工作量**：5 分钟

---

### 2. 新功能：通用 L1 导出导入

**选项 A：完整版（带智能重映射）**
- 导出 L1 + L0 上下文片段
- 导入时智能重映射 source_msg_id
- 工作量：1-2 小时

**选项 B：简化版（不保留 L0 关联）**
- 只导出 L1，source_msg_id 设为 NULL
- 适合"纯记忆"场景（不需要回溯原文）
- 工作量：30 分钟

**我的建议**：
- 如果你主要用 L1 做"知识库"（不需要频繁查看原文）→ 选简化版
- 如果你需要保持完整关联 → 选完整版（但重映射成功率~80%）

---

## 🎯 针对你的需求

### 你说的流程
> "清除记忆库 → 重新导入对话 → 重新提取 L1"

**这是最干净的方案**，因为：
1. L0 顺序完全正确（从 Kelivo 导入，msg_idx 正确）
2. L1 重新提取，source_msg_id 自然关联正确
3. 只需要保留 feel（现有功能支持，但需要修复 anchor_json）

**推荐步骤**：
1. **先修复** feel 导出导入（补上 anchor_json）
2. **导出** 当前的 feel：`GET /export/feel?source=all`
3. **清空** 记忆库
4. **导入** Kelivo 备份
5. **导入** feel：`POST /import/feel`
6. **重新提取** L1：`POST /extract/bulk`

---

## 💡 我现在可以做什么？

1. **立即修复 feel 导出导入** → 补上 anchor_json 字段
2. **实现简化版 L1 导出导入** → 快速、通用
3. **实现完整版 L1 导出导入** → 智能重映射（工作量较大）

你想要哪个？告诉我，我立即开始！

---

**分析完成时间**: 2026-09-04  
**文档**: `feel_export_import_analysis.md`
