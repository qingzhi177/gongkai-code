Bug4：Kelivo 改变滑动窗口条数后，L0 存储顺序混乱 / 消息被错误顶替
================================================================

你在排查并修复一个 bug。项目是记忆库网关+记忆服务，代码在
`services/memory-service/main.py`（FastAPI，Python）。**本次只改这一个文件**，
以及新增一个一次性修复脚本（放 `scripts/` 下），不要动 `services/gateway/server.js`
和其他任何文件。

【前置知识】
- Kelivo 前端把 API 指向本网关（gateway），网关只做透传 + 记忆注入 + 异步存 L0，
  不做自己的滑动窗口截断——真实发给网关的 `messages` 数组，条数完全由 **Kelivo
  客户端自己的"上下文条数"设置**决定（50 / 300 / 全量1500+）。
- 网关每次都会把这次请求收到的完整 `messages`（清洗掉 system/空消息/相邻重复后）
  整体转发给 `memory-service` 的 `POST /save_conversation`（见
  `server.js` 第 1544 行、1663 行的 `cleanMessagesForSave(req.body.messages, aiText)`）。
  也就是说：**每一轮对话，memory-service 收到的都是"Kelivo 当前窗口内的完整消息
  数组"**，而不是"只有本轮新增的 1-2 条"。

【现象（用户描述）】
Kelivo 把上下文条数从 50 调到 300 / 全量 1500+ 之后：
1. L0 存储顺序变得混乱，"窗口开头的几条上下文"莫名其妙地插到了 L0 存储的最后面。
2. 手动删掉几条错位的之后，最新对话内容显示正常，但消息总数似乎没怎么变化。
3. 怀疑还有没观察到的隐藏问题。

【根因（已定位）】

问题出在 `main.py` 的 `save_conversation()`（约第 780-868 行），具体是**没有
`group_id` 的消息走的那一分支**（约第 832-844 行）：

```python
for idx, msg in enumerate(req.messages):
    ...
    c.execute('SELECT id, content, extracted FROM l0_messages WHERE conv_id=? AND msg_idx=? AND status=?',
              (req.conv_id, idx, 'active'))
    existing = c.fetchone()
    if existing:
        if existing[1] == content:
            continue
        c.execute('UPDATE l0_messages SET status=? WHERE id=?', ('superseded', existing[0]))
        ...
    c.execute('INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted) VALUES (?,?,?,?,?,?,?)',
              (req.conv_id, idx, role, content, req.client, 'active', 0))
```

这里的 `idx` = `enumerate(req.messages)` 的下标，也就是**这条消息在"Kelivo 这一次
发过来的窗口数组"里的位置**，然后直接把它当成 `msg_idx` 存进库，并且用
`(conv_id, msg_idx)` 去数据库里找"是不是同一条消息的新版本"。

这个设计隐含的假设是："同一个位置 = 同一条逻辑消息"。这个假设**只有在 Kelivo
每次都发送从第 1 条开始的完整对话、且从不截断时才成立**。而实际情况是 Kelivo
会做客户端侧滑动窗口截断，窗口大小还能被用户随时调整。于是：

- 窗口从 50 调到 300 时，同一条物理消息在数组里的下标会整体发生变化（比如原来
  是"这次窗口的第 3 条"，窗口变大后可能变成"这次窗口的第 210 条"）。
- 更关键的是：窗口从 300→50 再变回 300（或反复调整）后，**原来从没被存过的、
  更早的历史消息**（因为一直被小窗口挡在外面，从来没进过 L0）现在第一次出现在
  请求里，此时它们的下标是"小号"（比如 0、1、2...），因为它们排在这次大窗口的
  最前面。
- 但数据库里 `msg_idx=0,1,2...` 这些槽位早就被之前小窗口存过的、**完全不相关的
  另一批消息**占用了。代码一看"同 msg_idx 但内容不同"，就误判成"这条消息被编辑
  过，是新版本"，于是：
  - 把数据库里**本来正确、无关的旧消息**标记成 `superseded`（软删除），
  - 把**这次窗口里第一次出现的、其实是更早历史的消息**当成"新版本"插入，
    拿到一个全新的、比现有所有行都大的自增 `id`。

而 Dashboard 浏览 L0 原文的接口 `get_l0_messages()`（约第 1299 行）是按
`ORDER BY id ASC` 展示的：

```python
'SELECT ... FROM l0_messages WHERE conv_id=? AND status=? ORDER BY id ASC LIMIT ? OFFSET ?'
```

`id` 只反映"这一行是什么时候被写进数据库的"，不反映"这条消息在真实对话里发生的
先后顺序"。上面这批"窗口开头、实际是更早历史"的消息，因为是刚刚才第一次被写入
数据库，`id` 反而是全表最大的一批 —— 于是显示的时候就"跑到了最后面"，正好对应
你观察到的现象①。

至于现象②"删掉几条之后总数没怎么变"：因为大部分窗口内的消息其实内容没变，会被
save_conversation 一开始的"内容级去重"逻辑（约第 797-805 行，按 conv_id+role+
去掉换行后的 content 全表匹配）拦下来直接跳过，不会重复插入。**真正出问题的只是
"窗口边界附近、第一次被看到 / 因为下标撞车被误判成新版本"的那一小撮消息**，所以
总数看起来变化不大，但顺序和 active/superseded 状态已经被搞乱了——这也是为什么
你感觉"很乱但又说不清哪里乱"：大部分数据完好，只有边界附近的一批被污染。

还有一个关联的小 bug，顺手一起修：`get_l0_context()`（约第 321-359 行）里

```python
c.execute(
    'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
    (conv_id, 'active', max(0, source_id - 3), source_id + 1)
)
```

`source_id` 实际上是 `l1_memories.source_msg_id`，语义是 `l0_messages.id`（主键，
参考 `_cascade_l1_on_version_switch` 里的注释"source_msg_id 是批次首条消息 id"），
但这里却拿它去跟 `msg_idx` 这一列做区间比较——两个完全不同量纲的字段被混用了，
在 `msg_idx` 本来就已经不可靠的情况下，这个函数取出来的"上下文"基本是随机的。

【修改要求】

### 1. 把"按下标匹配"改成"按内容做序列对齐"，不再用 idx 当身份证

修改 `save_conversation()` 里没有 `group_id` 的那部分逻辑：不要再用
`enumerate(req.messages)` 的下标直接当 `msg_idx` 去查库、去判断"是否同一条消息"。
改成：**用 `difflib.SequenceMatcher` 把"这次 Kelivo 发来的窗口消息序列"和"数据库里
这个 conv_id 当前 active 且无 group_id 的消息序列（按现有 msg_idx ASC, id ASC 排好）"
做一次整体的序列对齐**，只有在对齐后仍然"内容不同但处于同一相对位置（前后邻居都
对得上）"时，才判定为编辑/版本替换；对齐不上、只在新序列里出现的，一律当成"新增"
处理（不管它在这次窗口里的下标是大是小）；只在旧序列里出现、新序列里没有的，
**原样保留、不做任何操作**（大概率只是因为这次窗口比较小没带到，不代表被删除）。

参考实现（把这段逻辑抽成一个函数，替换掉原来第 832-844 行那部分）：

```python
import difflib
import re

def _cmp_key(role, content):
    # 与现有 SQL 里的去重键保持一致：忽略换行差异
    return role + "\x00" + re.sub(r'[\r\n]', '', content or '')

def _merge_l0_sequence(c, conv_id, client, messages):
    """把本轮 Kelivo 窗口消息（已去重、去 group_id 的那部分）与数据库里已有的
    active 消息做整体序列对齐，而不是按数组下标做一一匹配。

    返回 (saved_count, superseded_old_ids)
    """
    # 1) 取出数据库里现有的 active、无 group_id 的行，按现有顺序排好
    rows = c.execute(
        "SELECT id, msg_idx, role, content, extracted FROM l0_messages "
        "WHERE conv_id=? AND status='active' AND (group_id IS NULL OR group_id='') "
        "ORDER BY msg_idx ASC, id ASC",
        (conv_id,)
    ).fetchall()
    old_ids = [r[0] for r in rows]
    old_keys = [_cmp_key(r[2], r[3]) for r in rows]
    old_extracted = {r[0]: r[4] for r in rows}

    new_keys = [_cmp_key(m["role"], m["content"]) for m in messages]

    sm = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    saved = 0
    superseded_old_ids = []

    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == 'equal':
            continue  # 内容和相对位置都对得上，什么都不用做
        if tag == 'delete':
            # 只存在于旧序列：大概率是这次窗口没带到，不是真的被删，原样保留
            continue
        if tag == 'replace':
            # 同一相对位置内容变了：视为编辑/重新生成，旧的作废
            for oid in old_ids[a1:a2]:
                c.execute("UPDATE l0_messages SET status='superseded' WHERE id=?", (oid,))
                if old_extracted.get(oid) == 1:
                    superseded_old_ids.append(oid)
            for m in messages[b1:b2]:
                c.execute(
                    'INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (conv_id, -1, m["role"], m["content"], client, 'active', 0))
                saved += 1
        if tag == 'insert':
            # 只存在于新序列：真正的新消息（不管它在这次窗口里下标是大是小）
            for m in messages[b1:b2]:
                c.execute(
                    'INSERT INTO l0_messages (conv_id, msg_idx, role, content, client, status, extracted) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (conv_id, -1, m["role"], m["content"], client, 'active', 0))
                saved += 1

    # 2) 整个 merge 做完后，重排这个 conv_id 下全部 active 行的 msg_idx，
    #    让它保持"0,1,2...连续、且反映真实对话顺序"。
    #    排序键：旧行按 merge 前的相对顺序，新行按插入顺序穿插在正确位置——
    #    这里用一个简单办法：重新拉一遍 active 行，按 (原 msg_idx 是否为 -1,
    #    id) 排序不够准，直接按下面【重排算法】小节实现。
    _reindex_conv_msg_idx(c, conv_id)
    return saved, superseded_old_ids
```

上面这个骨架里最后一步"重排 msg_idx"是关键，不能省略，需要单独写一个
`_reindex_conv_msg_idx(c, conv_id)`：**不能简单按 id 排序**（这正是当前 bug
的根源），而是要保持"刚刚这次 merge 计算出来的相对顺序"。建议实现方式：

- 在 `_merge_l0_sequence` 里，不要用 `INSERT ... msg_idx=-1` 之后再猜顺序，
  而是直接维护一个"合并后的完整消息 id 顺序表"：遍历 `get_opcodes()` 时，
  同时构建一个列表 `final_order`，`equal`/`replace`/`insert` 分支里，
  该位置最终应该放哪一行的 id（对 replace/insert 是刚 insert 出来的
  `c.lastrowid`，对 equal 是 `old_ids[a1:a2]` 原样），最后按 `final_order`
  的顺序把 `msg_idx` 依次写成 `0,1,2,...`。
- 有 `group_id` 的行（如果以后有调用方会传 group_id）不参与这次重排，
  维持原有 group 逻辑不变。

请你（Claude Code）在实现时，把 `final_order` 的构建和最终 `UPDATE
l0_messages SET msg_idx=? WHERE id=?` 的批量重写补全，保证：
- 同一 conv_id 下，所有 active 且无 group_id 的行，合并后 `msg_idx` 连续、
  无空洞、无重复。
- 这一步只更新 `msg_idx` 列，不改 `id`、不改 `content`、不改 `status`。

`save_conversation()` 主函数里，原来第 832-844 行那一段（没有 group_id 的
分支）改成：先把本轮所有"没有 group_id 且没被内容级去重命中"的消息收集成一个
列表，循环结束后统一调用一次 `_merge_l0_sequence(c, req.conv_id, req.client,
collected_messages)`，而不是在 `for idx, msg in enumerate(...)` 循环内部
逐条处理。**有 group_id 的分支（第 806-831 行）保持原样不动**。

### 2. 修掉 `get_l0_context()` 里 id/msg_idx 混用的 bug

约第 336-341 行，改成先用 `source_id`（它是 `l0_messages.id`）查出对应行的
`msg_idx`，再用这个 `msg_idx` 去做区间查询：

```python
if source_id:
    anchor_row = c.execute(
        'SELECT msg_idx FROM l0_messages WHERE id=?', (source_id,)
    ).fetchone()
    if anchor_row:
        anchor_idx = anchor_row[0]
        c.execute(
            'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
            (conv_id, 'active', max(0, anchor_idx - 3), anchor_idx + 1)
        )
    else:
        rows = []
```

### 3. Dashboard 浏览 L0 原文的排序，改成按 msg_idx

约第 1299 行，`conv_id` 不为空时的那条 SQL，`ORDER BY id ASC` 改成
`ORDER BY msg_idx ASC, id ASC`（修好 msg_idx 之后，这样才会按真实对话顺序
展示，而不是按写入数据库的先后顺序展示）。**不要改第 1304 行那条全局列表
的排序**（它是按 `ts DESC` 给"最近活跃对话"用的，跟本次问题无关，不用动）。

### 4. 历史脏数据怎么办

代码修好之后只能保证"以后不再变乱"，已经错位的历史数据需要单独处理，
两个选项二选一，写在 `scripts/fix_l0_window_drift.py`（新建）里，默认
dry-run，加 `--apply` 才写库（参考仓库里已有的 `scripts/dedup_l0_adjacent.py`
的写法风格）：

- **方案 A（推荐，如果有 Kelivo 备份）**：Kelivo app 本地数据库本身就有
  每条消息真实的 `id` / `group_id` / `timestamp` / `message_order`
  （参考 `services/memory-service/kelivo_backup_importer.py`），是唯一
  真正可靠的顺序来源。建议导出一份 Kelivo 备份 zip，用现有的
  `/import/kelivo-backup` 接口重新导入一次，用它来核对/覆盖被搞乱的
  conv_id 对应的 L0 顺序。
- **方案 B（没有备份时的兜底）**：写一个脚本，对每个 conv_id：
  1. 先跑一遍 `scripts/dedup_l0_adjacent.py --apply` 清掉相邻重复；
  2. 对剩下的 active 行，按 `id ASC` 重新赋值连续的 `msg_idx`
     （这只是"不再比现在更乱"的兜底方案，不能百分百恢复真实顺序，
     因为部分行的 `id` 顺序本身已经是错的——这一点需要在脚本注释和
     控制台输出里明确告诉用户，不要假装能完全修复）。

【验证步骤】

1. 重启 memory-service。
2. 找一个已有一些历史的测试对话，把 Kelivo 的上下文条数先设成一个较小的值
   （比如 20），发几轮消息，确认 L0 里正常增长。
3. 把 Kelivo 上下文条数改大（比如 300 / 全量），再发一轮消息，然后：
   - 调用 `GET /l0_messages?conv_id=xxx`，确认新出现的历史消息按真实对话
     顺序插在正确位置，而不是全部堆在列表最后；
   - 确认之前已经存在的消息没有被错误标记成 `superseded`（可以查
     `status='superseded'` 的行，看是不是突然多了一批内容其实没变的）。
4. 反复把窗口调小调大几次，重复步骤 3，确认顺序始终保持稳定、不再随窗口
   大小变化而漂移。
5. 找一条真正被 Kelivo 编辑/重新生成过的消息，确认它依然能被正确识别成
   "版本替换"（旧的 superseded，新的 active），而不是被当成普通新增。

【注意】
- 只改 `services/memory-service/main.py`，新增一个 `scripts/` 下的迁移脚本。
  不要动 `services/gateway/server.js`、Dashboard 前端、L1/narrative 相关逻辑。
- 有 `group_id` 的分支（约第 806-831 行）保持不动，目前实际上没有调用方会
  带 `group_id` 过来（只有 `kelivo_backup_importer.py` 的离线导入路径会用到，
  它是直接写库、不经过 `save_conversation`），但保留这条分支以防以后接入。
- 改完先自己读一遍 diff，确认 `_reindex_conv_msg_idx` 真的按合并后的相对
  顺序重排、而不是退化成按 `id` 排序（那样等于没修）；再跑一遍上面的验证
  步骤，最后再给我确认。
  
我给你配置了免密重启权限，你可以直接用 
!sudo systemctl restart memory-gateway 重启等
每次修改完一项，帮我 git commit 并 push 到 public 仓库。并用一键脚本同步到两个仓库（注意进行信息脱敏处理）
commit message 写清楚改了什么。
