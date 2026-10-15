# 共同经历叙事虚构 & recall 检索不到原文 —— 根因分析与修复方案

> 给 Claude Code 的任务说明。项目是记忆库网关，涉及两个服务：
> `services/memory-service/main.py`（FastAPI，记忆库核心）、
> `services/memory-service/extract_l1.py`（L0→L1 提取 cron 脚本）、
> `services/gateway/server.js`（Node 网关，负责执行 `recall` 等工具并拼装
> system prompt）、仓库根目录的 `Narrative生成提示词.md`。
>
> 本文档分析两个现象的根因（均已在源码中定位到具体行），并给出分层的
> 修复方案。**请按"问题二 → 问题一"的顺序实施**：问题二的修复（尤其是
> `get_l0_context` 的 bug）会直接提升问题一里"喂给叙事生成的素材质量"，
> 顺序反过来也不影响正确性，但先修二再修一逻辑上更顺。
>
> 只修改本文档提到的文件和函数，不要顺手改动其他逻辑。每完成一个
> P0 步骤就自测一遍（见各节"验证方法"），确认无误再进行下一步，
> 分步骤 git commit，方便回滚。

> **⚠️ 如果同时存在《Bug4：Kelivo 改变滑动窗口条数后，L0 存储顺序
> 混乱》的修复任务，请先完整执行 Bug4，再执行本文档。** 两者都会
> 改到 `get_l0_context()` 这同一个函数：Bug4 把它改成"先查
> `msg_idx`、再按 `msg_idx` 做区间查询"（前提是 Bug4 第 1 步已经把
> `msg_idx` 修成可信的真实对话顺序）；本文档在这个基础上只是再加一层
> "优先用更精确的 `event_msg_id` 而不是粗略的 `source_msg_id` 去定位"。
> 下面【问题二】的步骤 2、步骤 4 里给出的代码，已经是**按"Bug4 已经
> 修好"为前提写的合并版本**——如果 Bug4 还没修，`get_l0_context()`
> 里查 `msg_idx` 这一步暂时仍然不可信，但不影响其他步骤照常推进，
> 等 Bug4 修完这一块自然就补齐了。

---

## 问题二：recall 检索不到 L1 未提及、但 L0 原文里确实存在的内容

先讲这个，因为根因是四个可独立验证的具体代码问题，修完就是修完，
不存在"架构该怎么设计"的模糊地带。

### 现象

用户回忆起一段以前真实聊过的经历（事件 A），AI 用 `recall` 工具去搜，
搜不到；但翻 L0 原始对话库，事件 A 确实在里面。

### 根因（4 条，按影响从大到小排序）

**根因 1（决定性 bug）：`exact` 模式从来没有真正生效过。**

`services/memory-service/main.py` 的 `search_memories()`
（约 403-434 行）：

```python
if req.mode == "exact":
    ...
    memories = []
    for row in rows:
        memories.append({...})
    # 这里没有 return！
# 意图判断
intent = detect_intent(req.query)
...
query_embedding = await get_embedding(req.query)
...
memories = []   # ← 490 行，无条件重新赋值，上面 exact 分支算出来的
                #    memories 被直接丢弃
```

`exact` 分支算完 `memories` 之后**没有 `return`**，代码会继续往下走，
在第 490 行把 `memories` 重新赋值为空列表，然后跑一遍语义
向量 + BM25 融合检索，最终返回的是**语义检索结果，和 `mode="exact"`
完全无关**。也就是说，无论 LLM 传 `mode="exact"`（工具描述里写的是
"逐字搜原文"）还是 `mode="semantic"`，实际执行的都是同一套语义检索，
而且这套检索**只查询 `l1_memories` / ChromaDB 的 `l1_collection`，
从来没有碰过 `l0_messages` 原始表**。这是"号称能逐字搜原文，实际
从不检索原文"的直接原因。

**根因 2：`get_l0_context()` 把两个完全不同量级的字段搞混了。**

同文件 321-359 行：

```python
def get_l0_context(sqlite_path, l1_id_str):
    ...
    c.execute('SELECT source_msg_id, conv_id FROM l1_memories WHERE id=?', (l1_id_str,))
    source_id, conv_id = row
    ...
    if source_id:
        c.execute(
            'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
            (conv_id, 'active', max(0, source_id - 3), source_id + 1)
        )
```

`source_id`（即 `l1_memories.source_msg_id`）存的是 `l0_messages.id`
—— **全表全局自增主键**；而这条 SQL 用它去卡 `msg_idx` —— **每个
对话内部从 0 开始的本地序号**。这是两个完全不在一个量级上的数字
（`id` 可能是几千几万，`msg_idx` 一般只有几十到几百）。除了极少数
最早期的对话，`msg_idx BETWEEN (source_id-3) AND (source_id+1)`
这个查询条件基本不可能命中任何一行 —— `get_l0_context()` 长期以来
一直在"悄悄"返回空字符串，不报错，只是查不到东西。

对照同文件 `reextract_l1()`（约 1573-1578 行）里**正确**的写法：

```python
ctx_rows = c.execute(
    "SELECT role, content, ts FROM l0_messages "
    "WHERE conv_id=? AND status='active' AND id BETWEEN ? AND ? ORDER BY id ASC",
    (conv_id, max(0, source_id - 3), source_id + 5)
).fetchall()
```

这里用的是 `id BETWEEN`，是对的（前提是 `id` 的大小顺序本身能反映
真实对话顺序——**这个前提在存在《Bug4：L0 存储顺序混乱》问题时并不
成立**：如果某条历史消息因为客户端上下文窗口调整，很晚才第一次被
写进数据库，它会拿到一个很大的 `id`，但在真实对话里它其实发生得很
早。所以下面【解决方案】部分不会照抄 `reextract_l1()` 这种
`id BETWEEN` 的写法，而是采用"先查这条消息真实的 `msg_idx`，再按
`msg_idx` 做区间查询"——这依赖 `msg_idx` 本身是可信的，也就是依赖
Bug4 的修复已经先落地。两个问题在这一个函数上是有先后依赖关系的，
具体合并方式见下面步骤 2。

**根因 3：`source_msg_id` 本身定位很粗糙 —— 它是"提取批次锚点"，
不是"这条记忆真正的原文位置"。**

`services/memory-service/extract_l1.py` 的 `main()`（约 320-330 行）：

```python
for conv_id, msgs in groups.items():
    text = ""
    ids = []
    for msg in msgs:
        ...
        text += f"[{prefix}] {content}\n"
        ids.append(mid)
    memories = call_deepseek(text, resolve_purpose('l1_extract'))
    if memories:
        for memory in memories:
            save_l1(memory, conv_id, msgs[0][4], msgs[0][0])   # ← 注意最后一个参数
```

`get_unextracted()` 一次最多取 50 条未提取的 L0 消息，按 `conv_id`
分组后整批喂给 LLM 提取。**这一整批**（最多 50 条消息）里提取出来的
所有记忆，`source_msg_id` 全部填的是 `msgs[0][0]`，也就是**这一批
第一条消息的 id**——跟这条记忆实际对应的是这一批里第几句话完全无关。

这个设计不是笔误，而是有意为之：`_cascade_l1_on_version_switch()`
（约 721-777 行）依赖"同一批提取出来的 L1 共享同一个
`source_msg_id`"这个不变量，用它反推出每个提取批次的 id 区间边界，
从而在用户编辑/撤回某条 L0 消息时，能批量作废+重提对应批次的 L1。
`reextract_l1()`（约 1547 行注释："新 L1 必须沿用旧 L1 的
source_msg_id（批次锚点，绝不能丢）"）和 `/viz/l0l1`
也都依赖这个语义。**所以 `source_msg_id` 不能改**，得加一个新字段
专门表示"这条记忆真正的原文位置"，见下面的修复方案。

**根因 4（用户已经猜到的架构性缺口）：L1 提取会主动丢弃"不值得
记住"的内容，而 `/search` 从不索引 `l0_messages`。**

`extract_l1.py` 的 `EXTRACT_PROMPT` 第 8 条明确写着"只提取真正值得
记住的，日常寒暄不要提取"——这是有意的降噪设计，本身没问题。但
`/search`（recall 唯一的后端）无论哪个模式，检索语料都只有
`l1_memories`（向量库 `l1_collection` 只收录 L1 摘要的 embedding，
BM25 语料也只取 `l1_memories.content`），`l0_messages` 完全没有
被建过任何索引。结果是：一旦某段真实事件在提取时被判定"不值得记住"
而跳过，这段内容就永久性地从"可被回忆"的范围里消失了——不管用户
描述得多准确、用哪个模式搜，都不可能搜到，因为它压根不在被检索的
语料库里。

### 解决方案

**P0（必须实现，直接解决"查不到原文"）**

1. 修复 `search_memories()`：让 `exact` 模式真正独立生效并 `return`，
   同时给它接上 `l0_messages` 的原文兜底检索。
2. 修复 `get_l0_context()` 的 `id`/`msg_idx` 混淆 bug。
3. 新增 `l1_memories.event_msg_id` 列，在写入 L1 时精确定位这条记忆
   在批次内真正对应的原始消息（不改动 `source_msg_id` 的"批次锚点"
   语义，`_cascade_l1_on_version_switch` / `reextract_l1` / `/viz/l0l1`
   三处都不用动）。
4. 同步更新 gateway 里 `recall` 工具的描述文字，如实反映各模式的
   能力边界，避免 LLM 对 `exact` 模式产生错误预期。

**P1（建议实现，进一步降低"L1 没提但 L0 有"的概率，非本次必须）**

5. L0 语义分块检索：按对话滑窗生成 chunk，embedding 后存入新的
   Chroma collection，`semantic` 模式命中不足时用同一个 query
   embedding 补充检索，命中的原始片段标注为"未整理原始片段"。
6. 覆盖率巡检任务：定期找出从未被任何 L1 引用过的 L0 区间，用更
   宽松的阈值补跑一次提取，减少"当时被过滤掉"造成的永久遗漏。

---

### 实施步骤（P0）

#### 步骤 1：`main.py` —— 数据库迁移，新增 `event_msg_id` 列

在 `init_db()` 里找到已有的这一段迁移代码（约 242-249 行）：

```python
    # ③ 和弦情绪锚点：l1_memories 加 anchor_json（可空；AI 自述 feel 的可选结构化锚）
    for _col, _coldef in (("anchor_json", "TEXT"),):
```

在它下面追加一段同样写法的迁移：

```python
    # 修复：l1_memories 加 event_msg_id（可空）。
    # source_msg_id 是"提取批次锚点"（同批多条 L1 共享同一个值，
    # _cascade_l1_on_version_switch/reextract_l1/viz 依赖它，不能改）。
    # event_msg_id 是"这条记忆真正对应的原始消息 id"，用于精确定位
    # 原文上下文，二者语义不同、互不影响。
    for _col2, _coldef2 in (("event_msg_id", "INTEGER"),):
        try:
            c.execute(f"ALTER TABLE l1_memories ADD COLUMN {_col2} {_coldef2}")
            conn.commit()
        except Exception:
            pass
```

#### 步骤 2：`main.py` —— 修复 `get_l0_context()`，并拆出可复用的窗口查询函数

> **前提：这一步假设 Bug4（L0 存储顺序混乱）已经修完**，也就是
> `l0_messages.msg_idx` 现在能可信地反映真实对话顺序（Bug4 文档里的
> `_merge_l0_sequence` + `_reindex_conv_msg_idx`）。如果 Bug4 还没修，
> 跳过这一步，先去修 Bug4，回头再执行这一步——不要图快改成
> `id BETWEEN` 的写法，那种写法在 Bug4 描述的场景下会拿到错误的
> 上下文（原因见上面"根因 2"末尾的说明）。
>
> 如果你（Claude Code）是先看到本文档、还没看过 Bug4 那份文档：
> 请先向用户确认 Bug4 是否已经修复；如果还没有，请先完整执行 Bug4
> 文档里的修复，再回来做这一步。

把 321-359 行的 `get_l0_context()` 整个替换为：

```python
def _l0_window_by_id(c, conv_id, center_l0_id, before=3, after=1):
    """给定某条 l0_messages.id，取它在真实对话顺序（msg_idx，依赖
    Bug4 已经把 msg_idx 修成可信状态）中前后若干条 active 消息，
    格式化为 [你]/[我] 文本。

    注意：这里不能直接拿 center_l0_id 当 msg_idx 用（两者不是一回事，
    这正是本函数要修的 bug），也不能直接按 id 做区间查询（在 L0
    存储顺序被打乱过的情况下，id 大小不代表真实对话顺序）——必须先
    查出这条消息真实的 msg_idx，再用 msg_idx 做区间查询。
    """
    anchor_row = c.execute('SELECT msg_idx FROM l0_messages WHERE id=?', (center_l0_id,)).fetchone()
    if not anchor_row:
        return ""
    anchor_idx = anchor_row[0]
    c.execute(
        'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
        (conv_id, 'active', max(0, anchor_idx - before), anchor_idx + after)
    )
    rows = c.fetchall()
    if not rows:
        return ""
    parts = []
    for role, content in rows:
        prefix = "你" if role == "user" else "我"
        parts.append(prefix + ": " + content[:200])
    return "\n".join(parts)


def get_l0_context(sqlite_path, l1_id_str):
    """根据 L1 ID 查找对应的 L0 上下文。

    修复：原实现直接拿 source_msg_id（l0_messages.id，全局自增主键）
    去卡 msg_idx（每个对话内部从 0 开始的本地序号）做区间查询，两者
    量级完全不同，导致除极早期对话外，几乎所有查询都命中不到任何
    行——这个函数长期"悄悄"返回空字符串。现改为先查出这条消息真实的
    msg_idx，再按 msg_idx 做区间查询（`_l0_window_by_id`，依赖 Bug4
    已经让 msg_idx 变得可信）。

    优先使用 event_msg_id（这条记忆在提取批次内的精确定位，比
    source_msg_id 更准）；旧数据没有 event_msg_id 时，回退到
    source_msg_id（批次首条消息，位置粗略但好过没有）。
    """
    try:
        conn = sqlite3.connect(str(sqlite_path))
        c = conn.cursor()
        c.execute('SELECT source_msg_id, event_msg_id, conv_id FROM l1_memories WHERE id=?', (l1_id_str,))
        row = c.fetchone()
        if not row:
            conn.close()
            return ""
        source_id, event_id, conv_id = row
        if not conv_id:
            conn.close()
            return ""
        anchor_id = event_id or source_id
        if anchor_id:
            result = _l0_window_by_id(c, conv_id, anchor_id)
        else:
            c.execute(
                'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? ORDER BY msg_idx ASC LIMIT 5',
                (conv_id, 'active')
            )
            rows = c.fetchall()
            result = "\n".join(
                (("你" if role == "user" else "我") + ": " + content[:200]) for role, content in rows
            ) if rows else ""
        conn.close()
        return result
    except Exception as e:
        print(f"L0 context error: {e}")
        return ""
```

如果 Bug4 文档里已经单独给过 `get_l0_context()` 的修复版本
（它那份文档"关联的小 bug"一节给的版本，只用 `source_msg_id`、
没有 `event_msg_id`），以那次修改后的结果为准，在此基础上追加
"优先用 `event_msg_id`"这一小段逻辑即可，不用整个函数重写两遍。

#### 步骤 3：`extract_l1.py` —— 精确定位 `event_msg_id`（不改动 `source_msg_id`）

在 `save_l1()` 前面新增一个定位函数：

```python
def locate_event_msg_id(msgs, memory):
    """在本次提取批次的原始消息里，为这条记忆定位真正对应的原始消息 id。

    优先用 quote（EXTRACT_PROMPT 强制要求的 10-40 字逐字引用）做精确
    子串匹配；quote 匹配不到时退而用 content 做子串匹配；都匹配不到
    时返回 None（上层回退到 source_msg_id，即批次首条，行为不变，
    不会比现状更差）。

    注意：只用于新增的 event_msg_id 字段，绝不影响 source_msg_id
    本身的"批次锚点"语义——source_msg_id 仍然是 msgs[0][0]，
    _cascade_l1_on_version_switch / reextract_l1 / /viz/l0l1 三处
    都依赖它"同批次共享同一个值"，不能动。
    """
    quote = (memory.get("quote") or "").strip()
    content = (memory.get("content") or "").strip()
    for cand in (quote, content):
        if not cand or len(cand) < 4:
            continue
        for msg in msgs:
            mid, _conv_id, _role, msg_content, _client, _ts = msg
            if cand in msg_content:
                return mid
    return None
```

把 `save_l1()` 签名改成接受可选的 `event_msg_id`，并把它一起写入表：

```python
def save_l1(memory, conv_id, client, source_id, event_msg_id=None):
    anchor_for_ts = event_msg_id or source_id
    ts = get_l0_ts(anchor_for_ts) or datetime.now().isoformat()
    conn = sqlite3.connect(str(SQLITE_PATH))
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO l1_memories (content, quote, source_msg_id, event_msg_id, conv_id, client, event_type, tags, valence, arousal, status, ts) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (memory["content"], memory.get("quote", ""), source_id, event_msg_id, conv_id, client,
             memory.get("event_type", "general"),
             json.dumps(memory.get("tags", []), ensure_ascii=False), memory.get("valence"), memory.get("arousal"),
             'active', ts)
        )
        l1_id = c.lastrowid
        conn.commit()
        # 后面 embedding / l1_collection.add 部分不变，原样保留
        ...
```
（省略号部分是函数里 `embedding = get_embedding(...)` 及之后的代码，保持原样不动，只需要在
`INSERT` 语句和函数签名这两处按上面改。）

顺带说明一个附带收益：`ts` 现在优先用 `event_msg_id` 对应消息的真实
时间，而不是永远用批次第一条消息的时间——对同一批次里较晚发生的
事件，时间戳会更准确。

最后改 `main()` 里的调用点（约 320-330 行）：

```python
for conv_id, msgs in groups.items():
    text = ""
    ids = []
    for msg in msgs:
        mid, _, role, content, client, ts = msg
        prefix = "你" if role == "user" else "我"
        text += f"[{prefix}] {content}\n"
        ids.append(mid)
    print(f"Processing {conv_id}: {len(msgs)} messages")
    memories = call_deepseek(text, resolve_purpose('l1_extract'))
    if memories:
        print(f"  Extracted {len(memories)} memories")
        for memory in memories:
            event_id = locate_event_msg_id(msgs, memory)
            save_l1(memory, conv_id, msgs[0][4], msgs[0][0], event_msg_id=event_id)
    else:
        print("  No memories")
    mark_extracted(ids)
```

`reextract_l1()`（main.py 里那个 HTTP 接口，不是 extract_l1.py 的
函数）不用改：它是针对单条记忆的重提取，本来就只处理一条，不存在
"批次内定位"的问题；如果想让重提取出来的记忆也精确定位，可以顺手
在它的 INSERT 语句里把 `event_msg_id` 设成 `source_id` 本身（因为
重提取场景下 `source_id` 已经是这条记忆专属的锚点），但这不是必须。

#### 步骤 4：`main.py` —— 让 `exact` 模式真正生效，并接上 L0 原文兜底

把整个 `search_memories()` 函数（403-580 行）替换为：

```python
@app.post("/search")
async def search_memories(req: SearchRequest):
    """检索记忆：exact=L1精确匹配+L0原文兜底；semantic/emotion=向量+BM25融合（仅L1）"""
    try:
        if req.mode == "exact":
            conn = sqlite3.connect(str(SQLITE_PATH), timeout=30)
            c = conn.cursor()

            # 1) L1 摘要里的逐字匹配（原有逻辑，原样保留）
            c.execute(
                "SELECT id, content, quote, event_type, tags, ts, client, valence, arousal "
                "FROM l1_memories WHERE status='active' AND content LIKE ?",
                (f"%{req.query}%",)
            )
            rows = c.fetchall()
            memories = []
            for row in rows:
                memories.append({
                    "id": f"l1_{row[0]}",
                    "l1_summary": row[1],
                    "l0_context": get_l0_context(SQLITE_PATH, str(row[0])),
                    "quote": row[2] or "",
                    "metadata": {
                        "event_type": row[3] or "",
                        "tags": row[4] or "",
                        "ts": row[5] or "",
                        "client": row[6] or "",
                        "valence": row[7] or 0,
                        "arousal": row[8] or 0,
                        "is_core": 0
                    },
                    "score": 1.0
                })

            # 2) 新增：L0 原文兜底检索。
            # 背景：L1 提取会主动过滤"不值得记住"的内容（extract_l1.py
            # EXTRACT_PROMPT 第8条），一部分真实发生过的事件从未被写成
            # L1、从未被 embedding，只存在于 l0_messages 里。exact 模式
            # 号称"逐字搜原文"，理应真正碰一次原文表。
            c.execute(
                "SELECT id, conv_id, role, content, ts, client FROM l0_messages "
                "WHERE status='active' AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{req.query}%", max(req.n * 3, 15))
            )
            l0_rows = c.fetchall()
            conn.close()

            seen = set()
            for l0_id, conv_id, role, content, ts, client in l0_rows:
                key = (conv_id, content[:50])
                if key in seen:
                    continue
                seen.add(key)
                conn2 = sqlite3.connect(str(SQLITE_PATH))
                c2 = conn2.cursor()
                ctx = _l0_window_by_id(c2, conv_id, l0_id)  # 复用步骤2里定义的辅助函数
                conn2.close()
                memories.append({
                    "id": f"l0_{l0_id}",
                    "l1_summary": content[:200],
                    "l0_context": ctx,
                    "quote": content[:80],
                    "metadata": {
                        "event_type": "raw_l0",
                        "tags": "",
                        "ts": ts or "",
                        "client": client or "",
                        "valence": 0,
                        "arousal": 0,
                        "is_core": 0,
                        "unprocessed": True   # 标记：未经 L1 提炼的原始片段，前端/prompt 可据此提示"这是原始记录，未必措辞准确"
                    },
                    "score": 0.8
                })

            return {"memories": memories[:max(req.n * 2, req.n)], "total": len(memories)}

        # ============ 以下 semantic / emotion 模式，逻辑不变 ============
        intent = detect_intent(req.query)
        print(f"Intent detected: {intent}")

        query_embedding = await get_embedding(req.query)
        if not query_embedding:
            return {"memories": [], "total": 0}

        vec_results = l1_collection.query(
            query_embeddings=[query_embedding],
            n_results=min(req.n * 3, 30),
            include=["documents", "metadatas", "distances"]
        )

        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()
        c.execute("SELECT id, content FROM l1_memories WHERE status='active'")
        all_memories = c.fetchall()
        conn.close()

        bm25_scores = {}
        if all_memories:
            corpus = [list(jieba.cut(m[1])) for m in all_memories]
            bm25 = BM25Okapi(corpus)
            query_tokens = list(jieba.cut(req.query))
            scores = bm25.get_scores(query_tokens)
            for i, m in enumerate(all_memories):
                bm25_scores[f"l1_{m[0]}"] = scores[i]

        K = 60
        rrf_scores = {}
        if vec_results["ids"] and len(vec_results["ids"][0]) > 0:
            for rank, mid in enumerate(vec_results["ids"][0]):
                vec_weight = 0.7 if intent["type"] == "semantic" else 0.5
                rrf_scores[mid] = rrf_scores.get(mid, 0) + vec_weight * (1.0 / (K + rank + 1))

        bm25_sorted = sorted(bm25_scores.items(), key=lambda x: -x[1])
        for rank, (mid, score) in enumerate(bm25_sorted[:30]):
            if score > 0:
                keyword_weight = intent["keyword_weight"]
                rrf_scores[mid] = rrf_scores.get(mid, 0) + keyword_weight * (1.0 / (K + rank + 1))

        sorted_ids = sorted(rrf_scores.items(), key=lambda x: -x[1])

        memories = []
        conn = sqlite3.connect(str(SQLITE_PATH))
        c = conn.cursor()

        for mid, rrf_score in sorted_ids[:req.n]:
            l1_id_str = mid.replace("l1_", "")
            try:
                _row = conn.execute("SELECT status FROM l1_memories WHERE id=?", (l1_id_str,)).fetchone()
            except Exception:
                _row = None
            if not _row or _row[0] != 'active':
                continue

            try:
                chroma_result = l1_collection.get(ids=[mid], include=["documents", "metadatas"])
                if not chroma_result["ids"]:
                    continue
                doc = chroma_result["documents"][0]
                meta = chroma_result["metadatas"][0]
            except Exception:
                continue

            if req.event_type and meta.get("event_type") != req.event_type:
                continue
            if req.after and meta.get("ts", "") < req.after:
                continue
            if req.before and meta.get("ts", "") > req.before:
                continue
            if req.mode == "emotion" and getattr(req, "emotion_valence", None):
                v = meta.get("valence", 0)
                if req.emotion_valence == "positive" and v <= 0:
                    continue
                if req.emotion_valence == "negative" and v >= 0:
                    continue

            if intent["emotion_filter"]:
                arousal = meta.get("arousal", 0)
                if arousal < 0.3:
                    continue

            l0_context = get_l0_context(SQLITE_PATH, l1_id_str)

            memories.append({
                "id": mid,
                "l1_summary": doc,
                "l0_context": l0_context,
                "quote": meta.get("quote", ""),
                "metadata": meta,
                "score": rrf_score
            })

        conn.close()

        if intent["time_weight"] > 0:
            for m in memories:
                ts = m["metadata"].get("ts", "")
                if ts:
                    from datetime import datetime
                    try:
                        days_ago = (datetime.now() - datetime.fromisoformat(ts)).days
                        time_boost = max(0, 1 - days_ago / 365) * intent["time_weight"]
                        m["score"] = m["score"] * (1 + time_boost)
                    except:
                        pass

        memories.sort(key=lambda m: (-m["metadata"].get("is_core", 0), -m["score"]))

        return {"memories": memories[:req.n], "total": len(memories)}

    except Exception as e:
        print(f"Search error: {e}")
        return {"memories": [], "total": 0}
```

> 改动说明：唯一实质性改动是 exact 分支末尾加了 `return`（并接上
> L0 原文兜底），以及原本 `req.emotion_valence` 直接访问改成
> `getattr(req, "emotion_valence", None)` 防止 `SearchRequest` 模型
> 未声明该字段时报错（这是顺手的防御性写法，不属于本次两个问题的
> 范围，如果你确认 `SearchRequest` 已经声明了该字段可以不改）。
> semantic/emotion 分支的逻辑一字未动。

#### 步骤 5：`server.js` —— 更新 `recall` 工具描述，避免 LLM 对能力边界产生错误预期

约 247-251 行，把 `mode` 的 `description` 改为：

```js
mode: {
  type: "string",
  enum: ["semantic", "exact", "emotion"],
  description: "semantic=语义联想，只在已经提炼好的记忆摘要里找，如果某段经历当时被判定'不值得记'就没有摘要、语义模式也搜不到；exact=逐字搜索原始对话原文（包含未被提炼进摘要的内容），怀疑摘要漏掉了细节、或想确认某句话到底怎么说的、或者印象模糊但觉得真实发生过，用这个更可靠；emotion=按情感强度/正负搜"
}
```

约 1341 行、1356 行附近 `systemPrefix` 里对 `recall` 的说明，改为：

```
- recall: 需要更多细节时搜索记忆库。semantic 模式只能搜到已经被提炼
  成"记忆摘要"的内容；如果隐约觉得某件事聊过、但记忆目录/语义搜索
  都没找到，换 exact 模式试试——它会直接搜原始逐字对话记录，能找到
  一些当时没被摘要收录的细节。
```

### 验证方法

1. 重启 memory-service（会触发 `init_db()` 迁移，检查启动日志确认
   没有报错、`event_msg_id` 列已加上：`sqlite3 data/sqlite/memory.db
   "PRAGMA table_info(l1_memories);"` 能看到这一列）。
2. 手动跑一次 `extract_l1.py`，找几条刚提取出来的 L1，
   `SELECT id, source_msg_id, event_msg_id FROM l1_memories ORDER BY id DESC LIMIT 5;`
   确认 `event_msg_id` 不再是清一色的批次首条 id（大概率和
   `source_msg_id` 不同）。
3. 直接调用 `POST /search`，body 传一句你确定**只存在于 L0、从未被
   提炼进 L1** 的原话（可以先手动挑一条 `extracted=0` 之前、但没进
   `l1_memories` 的老消息内容），`mode="exact"`，确认返回结果里出现
   `"id": "l0_xxx"` 的条目，且 `unprocessed: true`。
4. 再挑一条已经有对应 L1 的记忆，正常语义搜索命中后检查
   `l0_context` 字段不再是空字符串，且内容确实是这条记忆发生时候
   前后的真实对话（而不是同一批次开头无关的寒暄）。
5. 用 Kelivo 实际对话，让 AI 调用 `recall`，观察工具结果卡片和
   `[当时的对话]` 部分是否符合预期。

---

## 问题一：共同经历叙事（Shared Narrative）会编造从未发生过的经历

### 现象

叙事更新只依据"精选 L1 记忆"（`IMPORTANT_L1_WHERE = "status='active'
AND (is_core=1 OR arousal >= 0.6)"`，main.py 2483 行），写出来的经历
有时候是联想出来的，不是真实发生过的。

### 根因

`_do_narrative()` → `build_narrative_prompt()`（main.py 2908-2943
行）喂给 LLM 的素材，每条只有三样东西：`content`（L1 摘要，本身就是
一句话概括）、`quote`（10-40 字原话片段）、`ts`（日期）。这些是从
几十甚至上百条原始对话里提炼出来的**离散、去语境化的信息点**，两条
记忆之间可能隔了几天到几个月的真实对话，但 LLM 只看得到这两个孤立
的点。

而 `Narrative生成提示词.md` 对模型的要求是"按时间顺序分章节"、
"连贯流动、有呼吸感"、"增量演化：不推翻、不重来"——**只要求写得
连贯，完全没有"不能编造材料之外的具体内容"这条约束**。素材本身是
稀疏的点状信息，任务要求是连成流畅的线，中间的空白只能靠模型自己
"合理推测"来填——这就是虚构的来源：不是模型"学坏了"，是这套
"稀疏素材 + 连贯性要求 + 零约束 + 零校验"的组合天然会产生联想内容。

更麻烦的是"增量演化"这个设计本身：已经写下的章节永远不会被重写
（`_do_narrative()` 里已有叙事直接原样保留，只在后面追加新内容）。
这意味着一旦某次生成里混入了虚构内容，它会**永久留在这份叙事历史
里**，之后任何一次更新都不会去检查、纠正它——"不重写"的设计初衷
是保留成长的连续性，但客观上也让虚构内容一旦产生就无法自愈。

### 解决方案：三层架构

你提到不知道怎样的架构合适，这里给的是一个成熟的、RAG/生成式写作
里常见的"素材—约束—校验"三层设计，思路是：**能靠给材料解决的，
不要指望靠提示词约束；能靠提示词约束解决的，不要指望模型自觉；
剩下靠不住的部分，用一次独立的校验调用兜底**。

```
┌─────────────────────────────────────────────────────┐
│ 第一层：素材层（让模型没那么需要"编"）                  │
│  给每条重要 L1 记忆额外附上它真实对应的 L0 原始对话片段  │
│  （现在问题二修完后，get_l0_context 已经能正确取到）    │
│  → 模型能看到真实的对话质感，用它来"还原"而不是"联想"   │
├─────────────────────────────────────────────────────┤
│ 第二层：约束层（明确划出不能碰的红线）                   │
│  在 Narrative生成提示词.md 里加一段"禁止编造"规则：      │
│  具体对话/场景/事件/承诺必须能在素材里找到依据，          │
│  素材之间的空白只能用概括语言过渡，不能编造过渡情节        │
├─────────────────────────────────────────────────────┤
│ 第三层：校验层（模型没守住约束时的最后一道闸）            │
│  生成后追加一次独立的"忠实度核查"调用：把新增内容和       │
│  素材原文放在一起，让模型找出"素材里没有依据的具体内容"    │
│  核查结果记进 trigger_details，供 Dashboard 展示/复核     │
└─────────────────────────────────────────────────────┘
```

三层缺一不可：只做第一层，模型仍可能在真实素材之外自由发挥；只做
第二层，约束经常会被模型在"写得连贯"的驱动下悄悄打破（这也是现在
唯一缺的一层，但单独补上不够保险）；只做第三层，没有素材支撑的话
核查也没什么依据可核。三层叠加，风险按层递减。

### 实施步骤

#### 步骤 1：`Narrative生成提示词.md` —— 加入"禁止编造"章节

在文件末尾（"## 示例格式"章节之前）插入：

```markdown
## 禁止编造（重要）

这份叙事最终是写给共同经历过这些事的两个人看的。任何"读起来很真实
但其实没发生过"的内容，对这段关系的伤害比一句话说错更大。因此：

1. 只能使用【新增记忆材料】和材料里附带的【当时的真实对话片段】中
   明确出现过的内容。不能凭借对相似情境的联想，补写材料中没有的
   具体对话、动作、场景、心理活动、承诺或结论。
2. 两条记忆之间如果存在时间空白（隔了很久，或者主题完全不相关），
   只能用概括性的语言过渡（"这段时间里，我们各自忙碌""再聊起时，
   已经是……"），不能编造这段空白期间具体发生了什么。
3. 如果某条记忆本身信息不完整（比如只有孤零零一句话），只写这句话
   传达出的信息，不要为了让故事完整而脑补前因后果。
4. 不确定的地方，宁可写得简短、留白，也不要为了"读起来完整"添加
   没有依据的细节。
5. 自查标准：写完每一段后问自己——这段话里的具体情节/对话/细节，
   能不能在【新增记忆材料】或【当时的真实对话片段】里找到对应依据？
   找不到依据的，删掉或者改写成更概括、不涉及具体情节的表述。
```

#### 步骤 2：`main.py` —— `build_narrative_prompt()` 附带真实 L0 原文片段

把 2908-2943 行的函数替换为：

```python
async def build_narrative_prompt(existing_narrative: str, new_memories: list) -> dict:
    """构建 Narrative 生成提示词，返回 {system, user} 两段。

    new_memories: [(id, content, quote, ts), ...] 按提取顺序（≈时间顺序）。
    每条记忆额外附上它真实对应的 L0 原始对话片段（问题二修复后
    get_l0_context 才能正确取到），给模型提供可依据的真实素材，
    减少靠联想去"编"过渡情节的空间。
    """
    prompt_file = Path(__file__).parent.parent.parent / "Narrative生成提示词.md"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except:
        system_prompt = """你是一段亲密关系的记忆守护者。维护一份连续的共同经历叙事。
按时间顺序分章节书写，保留时间锚点、情感转折点、共同经历、关系演化，
重要的原话用引号保留。温暖但不矫饰、连贯流动。只使用材料中明确出现过
的内容，不编造材料之外的具体情节。"""

    memory_text = "### 核心记忆（附当时的真实对话片段作为事实依据）\n"
    for m in new_memories:
        l1_id, content, quote, ts = m[0], m[1], m[2], m[3]
        ts_date = ts[:10] if ts else "未知时间"
        memory_text += f"\n- [{ts_date}] {content}\n"
        if quote:
            memory_text += f"  > 原话：{quote}\n"
        l0_ctx = get_l0_context(SQLITE_PATH, str(l1_id))
        if l0_ctx:
            memory_text += "  [当时的真实对话片段]\n"
            for line in l0_ctx.split("\n"):
                if line.strip():
                    memory_text += f"  {line}\n"

    user_message = f"""请基于以下材料，更新我们的共同经历叙事。

## 现有叙事
{existing_narrative or "（这是第一次生成，请基于记忆从头写起）"}

## 新增记忆材料
{memory_text}

请生成更新后的完整叙事（按时间顺序，markdown 章节格式）。记住：只写
材料里有依据的内容，材料之间的空白用概括语言过渡，不要编造具体情节："""

    return {"system": system_prompt, "user": user_message}
```

#### 步骤 3：`main.py` —— 新增忠实度校验，并接入 `_do_narrative()`

在 `call_llm_for_narrative()` 函数后面新增：

```python
VERIFY_PROMPT_TMPL = """你是一名严格的事实核查员，只做核查，不做任何创作或修改。

下面是【素材】（记忆摘要 + 当时的真实对话片段）和一段基于这些素材新写的
【新增叙事内容】。请逐句检查【新增叙事内容】，找出其中出现了、但在
【素材】里完全找不到依据的具体内容——包括：具体的对话原话、具体的
动作细节、具体的场景描写、具体的事件经过、具体的承诺或结论。

不算编造的情况：合理的语言过渡句、对已有事实的概括转述、不涉及新增
具体事实的文学化措辞。

只输出一个 JSON 数组，每一项是【新增叙事内容】里被判定为"无依据编造"
的一句原文（逐字摘录，不要改写）。没有问题就输出 []。不要输出任何
其他文字、不要输出 markdown 代码块标记。

【素材】
{materials}

【新增叙事内容】
{new_text}
"""

async def verify_narrative_faithfulness(config: dict, materials: str, new_text: str) -> list:
    """对新增叙事内容做一次忠实度核查，返回被判定为编造的句子列表（可能为空）。
    校验本身失败（网络错误/解析失败）时不阻塞主流程，视为"未发现问题"。"""
    if not new_text.strip():
        return []
    prompt = VERIFY_PROMPT_TMPL.format(materials=materials[:12000], new_text=new_text[:8000])
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            is_claude = (config['model'].startswith("claude")
                         or "anthropic/" in config['model']
                         or "claude-" in config['model']
                         or "claude." in config['model'])
            if is_claude:
                response = await client.post(
                    f"{config['base_url']}/messages",
                    headers={
                        "x-api-key": config['api_key'],
                        "Authorization": f"Bearer {config['api_key']}",
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    },
                    json={"model": config['model'], "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000}
                )
                response.raise_for_status()
                blocks = response.json().get("content", [])
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            else:
                response = await client.post(
                    f"{config['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                    json={"model": config['model'], "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000, "temperature": 0}
                )
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"].strip()

            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            flagged = json.loads(text)
            return flagged if isinstance(flagged, list) else []
    except Exception as e:
        logger.error(f"narrative verify error: {e}")
        return []
```

然后在 `_do_narrative()`（2032-2140 行）里，找到这一段：

```python
        # 6. 构建提示词并调用 LLM
        prompt_parts = await build_narrative_prompt(existing_narrative, new_memories)
        new_narrative = await call_llm_for_narrative(config, prompt_parts)

        if not new_narrative:
            return {"status": "error", "message": "生成失败"}
```

改成：

```python
        # 6. 构建提示词并调用 LLM
        prompt_parts = await build_narrative_prompt(existing_narrative, new_memories)
        new_narrative = await call_llm_for_narrative(config, prompt_parts)

        if not new_narrative:
            return {"status": "error", "message": "生成失败"}

        # 6.5 忠实度校验：只核查新增的部分（增量演化保留已有全文，
        # 已有部分之前已经核查过，不用重复核查）。
        new_part = new_narrative[len(existing_narrative):] if (
            existing_narrative and new_narrative.startswith(existing_narrative)
        ) else new_narrative
        flagged = await verify_narrative_faithfulness(config, prompt_parts["user"], new_part)
        verification_info = {"checked": True, "flagged_count": len(flagged), "flagged_samples": flagged[:5]}
        if flagged:
            logger.warning(f"[NARRATIVE VERIFY] 发现 {len(flagged)} 处疑似无依据内容: {flagged[:3]}")
```

再找到写 `trigger_details` 的地方（约 2104-2112 行），把
`verification_info` 一起塞进去：

```python
        trigger_details = json.dumps({
            "timestamp": datetime.now().isoformat(),
            "reason": req.reason or ("强制全量重写" if req.force else
                                     "首次生成" if not existing else
                                     f"新增 {len(consumed_ids)} 条重要记忆"),
            "mode": "full" if (req.force or not existing) else "incremental",
            "l1_ids": consumed_ids,
            "l1_count": len(consumed_ids),
            "verification": verification_info,
        }, ensure_ascii=False)
```

以及返回值里也带上，方便调用方（比如自动触发的日志）看到：

```python
        return {
            "status": "ok",
            "id": new_id,
            "version": new_version,
            "mode": "full" if (req.force or not existing) else "incremental",
            "consumed_l1": len(consumed_ids),
            "content": new_narrative,
            "verification": verification_info,
        }
```

> 设计取舍说明：这里选择"核查后只记录+打印警告，不阻塞落库、不自动
> 删改文字"。理由是自动删改文字本身也可能引入新的错误（比如删掉半句
> 话导致语义断裂），风险不比留着更小；更稳妥的做法是让人在
> Dashboard 里看到 `verification.flagged_count > 0` 时手动打开这个
> 版本复核、用已有的 `PUT /narrative/{id}` 编辑接口修正。如果之后
> 想要"自动重试一次、带上更严格提醒"的加强版，可以在
> `flagged` 非空时，把 `flagged` 列表拼进 user prompt 提示模型"这些
> 句子被判定为编造，请去掉或改写成概括性表述，重新生成"，再调一次
> `call_llm_for_narrative`，作为 P1 增强，非本次必须。

校验复用的是 `narrative` 这个 purpose 的模型配置（跟生成用同一个
模型），不需要新增模型配置项/UI。

### 验证方法

1. 手动触发一次 `POST /narrative/generate`（`force=true` 方便测试），
   跑完后 `GET /narrative/current`，检查 `trigger_details` 里是否
   多了 `verification` 字段。
2. 找几条时间上离得比较远、主题不相关的重要记忆，观察新生成的叙事
   在这两条记忆之间的过渡文字，是否变成了概括性描述而不是具体场景。
3. 故意构造一批"信息量很少、彼此不相关"的记忆去触发生成，人工通读
   一遍新生成的部分，检查有没有明显不是材料里来的具体情节；再对照
   `verification.flagged_samples`，看校验步骤有没有抓到你自己发现
   的那些编造点（用来判断校验这一层本身管不管用，如果长期抓不到你
   人工发现的问题，需要再调整 `VERIFY_PROMPT_TMPL` 的措辞）。
4. 确认已有叙事内容（`existing_narrative` 部分）在多轮增量更新后
   始终保持不变，只有新增部分在变化。

---

## P1（可选，非本次必须）：进一步降低"L1 没提但 L0 有"的概率

这部分是架构性增强，工作量明显更大，建议在 P0 稳定运行一段时间、
确认效果后再考虑：

1. **L0 语义分块检索**：extract_l1.py 的 cron 任务里，除了现有的
   L1 提取，再按对话滑窗（比如每 4-6 条消息一组，重叠 2 条）生成
   chunk 文本，embedding 后存入新的 Chroma collection（如
   `l0_chunks`）。`/search` 的 `semantic` 模式里，当 L1 命中数量不足
   `req.n` 时，用同一个 query embedding 去查这个 collection 做补充，
   命中的原始片段同样标注 `unprocessed: true`。这样"语义联想到了、
   但当时没被提炼成 L1"的内容也有机会被搜到，而不是只有 `exact`
   模式（逐字匹配）才能兜底。
2. **覆盖率巡检任务**：定期（比如每周）跑一次统计，找出从未被任何
   L1 的 `event_msg_id`/`source_msg_id` 覆盖到的 L0 id 区间，用更
   宽松的提取阈值（比如去掉"日常寒暄不提取"这条限制）重新跑一次
   提取，产出的记忆标记为"补充记忆"，供人工筛选是否保留。

---

## 总体注意事项

- 只改本文档提到的文件和函数：`services/memory-service/main.py`、
  `services/memory-service/extract_l1.py`、`services/gateway/server.js`、
  `Narrative生成提示词.md`。不要顺手改动其他逻辑。
- 数据库改动只是新增可空列（`event_msg_id`），不删除、不改变任何
  现有字段的语义，历史数据无需迁移即可正常工作（`event_msg_id` 为
  `NULL` 时自动回退到原有的 `source_msg_id`）。
- `source_msg_id` 的"批次锚点"语义全程不变，`_cascade_l1_on_version_switch`
  / `reextract_l1` / `/viz/l0l1` 三处不用改、也不要改。
- **如果《Bug4：L0 存储顺序混乱》也在修复计划里，必须先修 Bug4，再修
  本文档**（尤其是"问题二"的步骤 2、步骤 4）。两者会改到同一个函数
  `get_l0_context()`，本文档给出的是"假设 Bug4 已修好"的合并版本，
  顺序不能反，理由见文档开头和"根因 2"小节。
- 建议按"Bug4（如有）→ 问题二的 P0 → 问题一"的顺序实施，每步改完
  先自测（见各节"验证方法"），确认无误后再进行下一步，分步骤 git
  commit。
- 改完先自己读一遍 diff，确认没有多余改动，再验证效果。

## 验收清单

- [ ]（如适用）Bug4（L0 存储顺序混乱）已先修复并验证通过
- [ ] `l1_memories` 表新增 `event_msg_id` 列，历史数据不受影响
- [ ] 新提取的 L1 记忆里，`event_msg_id` 不再清一色等于批次首条消息 id
- [ ] `get_l0_context()` 改为"先查 msg_idx、再按 msg_idx 区间查询"，
      能正确取到非空且真正对应的上下文
- [ ] `mode="exact"` 的 `/search` 请求会真正 `return`，不再被
      semantic 分支覆盖
- [ ] `mode="exact"` 能搜到只存在于 L0、从未被提炼进 L1 的内容
      （返回结果里出现 `l0_xxx` 且 `unprocessed: true`）
- [ ] gateway 的 `recall` 工具描述和 systemPrefix 已更新
- [ ] `Narrative生成提示词.md` 新增"禁止编造"章节
- [ ] `build_narrative_prompt()` 会给每条记忆附上真实 L0 原文片段
- [ ] `_do_narrative()` 生成后会跑一次忠实度校验，结果写进
      `trigger_details.verification`
- [ ] 人工通读几次新生成的叙事，主观感受上编造内容明显减少
