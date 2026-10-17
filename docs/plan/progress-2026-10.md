# 优化进度记录（2026-10 起）

> 对应《gongkai-code 优化与架构完善方案》（claude.ai 项目文档）。每完成一批改动在这里追加一节。

---

## 2026-10-16 · Phase 0 第一批（止血）

### 文档整理
- 根目录的根因分析、交接、bug 报告、需求等文档移入 `docs/`（见 `docs/README.md` 索引），用 `git mv` 保留历史。

### 记忆服务（`services/memory-service`）
| 编号 | 问题 | 改动 |
|---|---|---|
| B3 | `mode=emotion` 时访问不存在的 `req.emotion_valence` → 报错被吞 → 永远返回空 | `SearchRequest` 增加 `emotion_valence` 字段 |
| B4 | 先截取前 n 条再过滤，带 `event_type` / 时间条件时结果变少甚至为空 | 遍历全部候选，攒够 n 条为止 |
| B2/B6 | 检索用的元数据来自 Chroma（形状不一、不随编辑同步、没有 `last_shown_at`），导致冷却失效、`is_core` 置顶不生效 | 元数据一律从 SQLite 读取；`visibility=0` 不参与检索，score 乘以 visibility |
| B1 | 浮现门槛用 RRF 分数（最大约 0.016），阈值却是 0.15 → 主通道几乎从不浮现 | 检索结果新增 `vec_sim`（余弦相似度）和 `rrf_score` |
| B5 | exact 模式 LIKE 未转义、无排序 | 转义 `%` `_`，按时间倒序 |
| B7 | DeepSeek 出错返回 `[]`，随后照样标记已提取 → 整批永久丢失 | 出错返回 `None`；失败累加 `extract_attempts`、记录 `extract_error`；连续 3 次转死信 `extracted=-1` |
| B11 | `resolve_purpose` 用 `MEMORY_DATA_DIR`，其余用 `DATA_DIR`；requirements 缺依赖且版本与线上不一致 | 统一 `DATA_DIR`；requirements 按线上实际版本更新 |
| B12 | 叙事水位线取 `MAX(id)`，一次新增超过 60 条重要记忆时多出的被永久跳过 | 水位线改为本批最后一条已消费的 id，剩余的下次自动检查继续处理 |
| B13 | cron 提取后又触发一次叙事更新，与 APScheduler 并存 | 删除 cron 一侧的触发 |
| H1 | 记忆服务与 cron 并发写库 | 开启 WAL |
| S2 | 记忆服务监听 `0.0.0.0:8001` | 只监听 `127.0.0.1` 和 `::1`（`MEMORY_BIND_HOSTS` 可覆盖） |

### 网关（`services/gateway/server.js`）
- **B1**：浮现门槛改用 `vec_sim >= SURFACE_MIN_SIM`（默认 0.55，可用环境变量覆盖）。日志里的 `[SURFACE] 候选 vec_sim:` 行只记 id 和分数，用于以后校准。
- **展示指标时机**：以前组装记忆菜单时就计入 `display_count` / `last_shown_at`，请求失败或重新生成也会算"展示过"并进入冷却。现在改为回复正常结束后才计入（标题生成请求不计）。
- **B3**：recall 工具转发 `emotion_valence`；记忆服务返回错误时提示"检索出错"，不再和"没找到"混在一起。
- **S1**：只监听 `127.0.0.1:3000` 和 `[::1]:3000`（`GATEWAY_BIND_HOSTS` 可覆盖）。外部访问统一走 nginx（`https://域名/v1/`，nginx 校验 Bearer token）。
- 启动时如果记忆服务还没起来，拉取配置失败后每 5 秒重试，最多 6 次（以前会一直回退到 .env，直到手动 reload）。

### Chroma（`docker-compose.yml`）
- **S3**：端口只绑定 `127.0.0.1` 和 `[::1]`。Docker 发布的端口会绕过 ufw，以前等于对公网开放；同时关闭 `ALLOW_RESET`。
- **数据目录修复**：新版镜像的数据目录是 `/data`，以前挂载的 `/chroma/chroma` 没被使用，向量只存在容器内部，重建容器就会丢失，也不在备份里。现在挂载 `./data/chroma:/data`。重建前已确认两个集合都是 0 条，没有数据损失。

### 仓库卫生
- `sync-to-repos.sh`：个人专属的泄露检查词移到不入库的 `.leakcheck.local`。检查范围从"未提交文件"扩大到"还没推到公开仓库的提交"，因为 MCP 会自动快照提交，旧逻辑会漏查。提交说明改为参数传入。

### 验证
- `tests/manual/phase0_smoke.py`：用虚构数据跑通"模拟失败 → 重试 → 真实提取 → 四种检索 → 清理"，全部通过。
- 少量样本下的 `vec_sim`：相关查询约 0.68–0.73，"嗯嗯"这类无关查询约 0.45–0.49。
- 端口：3000、8000、8001 只在 `127.0.0.1` / `::1` 上监听。

### 本批未做（留待后续）
- **B14 叙事校验拦截**：现有校验在增量模式下会误报。它拿"整篇叙事"去对照"本批新素材"，旧叙事里的引号和长度都会被判为可疑。直接拦截会挡住正常更新，需要先改成只校验新增内容，再启用拦截。
- **B8 / B9 / B10**（分段提取、quote 校验、证据指针）：放到 Phase 1 的提取流水线 v2 一起做。
- **S4 备份脚本**：仍会复制 `.env` 并 `push -f` 到私有备份仓库，需要先和你确认备份方式。
- 需要 sudo 才能做的：nginx `sites-enabled` 里有一份 `memory-system.bak`（重复的 server 块）；`ufw` 状态检查；`0.0.0.0:52528` 端口的归属确认。

---

## 2026-10-16（下午）· Phase 0 收尾 + Phase 1 第一步

### 问题修复
| 问题 | 原因 | 改动 |
|---|---|---|
| Anthropic 格式客户端拉不到模型列表 | nginx `/v1/` 只认 `Authorization: Bearer`，Anthropic 格式发的是 `x-api-key` → 一律 401；CORS 预检写死只放行两个头 | `deploy/nginx/fix-v1-auth.sh`（需 sudo 执行一次，脚本不含 token）：两种头任一匹配即可，预检放行浏览器实际请求的头；网关的 Anthropic 模型列表补 `has_more/first_id/last_id` |
| Pluvia 会话前两句被存成单独会话 | Pluvia 只给"当前"消息加"（此刻：…）"时间戳，历史里不带 → 会话 id 兜底 MD5 前后不一致；同一条消息也被存两次 | 网关在算会话 id、存 L0、做浮现检索前剥掉时间戳（发给模型的原文不变）；已拆出的 3 个重复会话软删除 |
| 清空数据后叙事长期不更新 | 清空后 L1 自增 id 从 1 开始，但 `narrative_state` 水位线还停在 247 | purge 时清空 `narrative_state`；当前库已归零 |
| 叙事校验不拦截（B14） | 增量模式拿整篇叙事对照本批新素材，误报太多 | 只校验新增句子；fail 时存为 `rejected` 不发布（同批最多拦 2 次） |
| 备份把 `.env` 推到 GitHub（S4） | — | `backup.sh` 不再复制 `.env`、不再 `push -f`，备份前做 `integrity_check` |

### Phase 1 第一步：存储统一
- **ChromaDB 退役**：向量存在 SQLite 的 `vectors` 表（`vector_store.py`，接口兼容原 Chroma Collection）。已迁移 7/7 条，容器已停止、不随开机启动（回滚：启动容器 + 回退代码）。
- **关键词检索**：FTS5 + jieba 预分词（`text_index.py`），替代每次查询全量重建 BM25。
- **索引对账**：`l1_memories` 触发器递增版本号；`reconcile_indexes` 每 5 分钟（及 `POST /index/reconcile`）增量更新 FTS、清理孤儿向量、补齐缺失向量。

### 提取流水线 v2
- 按会话分段（间隔 > 30 分钟或满 40 条切段），每段带前 6 条只读上下文。
- **等会话安静 10 分钟再提取**（积压超过 60 分钟不再等）。所以聊天后约 10 分钟，记忆才会出现在 L1 里；Dashboard 的"立即提取"不等待。
- 每行带行号，模型返回 `msg_refs`（依据的行号）和 `author`（`user_said` / `ai_said` / `co_created` / `ai_felt`）。
- 原句校验：逐字命中 → `verified`；相似度 ≥ 0.9 → 用原文替换（`verified_fuzzy`）；否则清空原句（`unverified`）。
- 新列 `evidence_json` / `evidence_status` / `author`。`get_l0_context` 以证据为中心取上下文。

### 网关
- 寒暄或极短回复（去标点后不超过 3 个字）不走相关度召回。实测"嗯嗯"的 vec_sim 也能到 0.5。

## 2026-10-17（凌晨）· Pluvia 接入约定 v0.2

完整约定见 [`pluvia-gateway-contract.md`](pluvia-gateway-contract.md)。以后主要用 Pluvia，Kelivo 的路径保留不动。

### 网关新增
- `GET /v1/pluvia/contract`：能力协商，声明 `turn` / `tail` / `client_tools` / `tool_events` 四项。
- **turn**：一条回复只在最后一轮存 L0，各轮文字合成一条；用户点「停」时中止上游、不存。
- **tail**：按前端给的「此刻」原文精确剥离，没带时用放宽的兜底规则。
- **client_tools**：网关只执行 5 个记忆工具，前端工具交回前端执行。交出时这一轮的上游状态（含思考签名、记忆工具结果）存 10 分钟，续问时原样接回，缓存可命中。
- **tool_events**：记忆工具的执行过程以 `pluvia_tool` 事件推给前端显示，recall 结果去掉了内部打分。
- **删除会话**：`POST /v1/pluvia/conversations/{id}/delete`（另有 DELETE 写法）。L0 软删，对应 L1 硬删。
- **时间头开关**：`.env` 里的 `GATEWAY_TIME_HEADER=auto|on|off`，默认 auto。前端带了「此刻」，网关就不再加 `[当前时间]`；带了「离上一句」，就不再加 `[距上次对话]`。
- **顺带修复**（对所有客户端生效）：
  - 上游报错时，前端能看到真实原因（如「账户余额不足」）；4xx 不再白白重试 3 次。
  - 工具循环回传上游时，保留被加密的思考块（`redacted_thinking`）。
  - OpenAI 格式的 `tool_choice` 会转换成 Anthropic 格式。
  - `cache_control` 超过 4 个时，自动去掉最早的几个。
  - `role:"tool"` 的消息不再进 L0。

### 测试
- `tests/manual/pluvia_contract/run.sh`：假上游 + 假记忆服务 + 端口 3999 上的临时网关，49 项全部通过（含 Kelivo 路径不变的检查）。
- 线上：Pluvia 格式的真实请求正常返回。

### 发现的问题
- **上游中转站余额已用完**，所有请求现在返回 403。充值后恢复。
- 测试用的一条极短请求（带 `tool_choice: "none"` 和一个前端工具），中转站报了 **77,753 个输入 token**，同样大小的请求平常约 3 千，原因未明。充值后用同一条请求、分别带与不带 `tool_choice` 对比一次；如果确实和 `tool_choice: "none"` 有关，就让网关在第 5 轮改成不发工具列表。

## 2026-10-18（凌晨）· Pluvia 约定第 8 节 + 真实模型测试

前端在约定第 8 节答复了第 7 节的三件事，另外提了辅助请求（8.5）。网关的答复写在约定第 9 节。

### 网关新增
- **前端系统提示词**（8.1）：`pluvia.system = "append"` 时，Pluvia 的 system 接在记忆库 system 后面，统一放一个 1 小时缓存断点；默认 `ignore`，行为不变。
- **存 L0 时剥掉前端加的装饰**（8.2）：assistant 历史开头的 `[图片：…]` / `[工具记录 · …]` 行，以及末尾的 `[链接卡片 · …]` / `[整帖 · …]`。发给模型的原文不变。多轮回复的各轮正文改用空行连接，和前端一致。
- **辅助请求**（8.5）：带 `pluvia.aux` 时，不检索、不注入记忆库的 system / 目录 / 时间头，不带记忆工具，不存。
- **缓存断点裁剪**：把 system 断点也算进去，按「较早的消息 → 工具 → 较早的 system → 最后一个消息 → 最后一个 system」的顺序去掉。
- 没有工具时请求里不再带 `tools` 字段。
- 每次请求在 `gateway.log` 记一行 `[UPSTREAM]`：只记 system、消息、工具各占多少字符，不记内容。用来核对中转站报的 token 数。

### 测试
- 假上游：`tests/manual/pluvia_contract/run.sh`，58 项全部通过。
- 真实模型：`tests/manual/pluvia_contract/real_smoke.js`，7 组全部通过（全部带 `memory_skip`，L0 里没有留下测试数据）：
  - 前端工具两轮：Anthropic 格式，带 thinking，签名能回传；OpenAI 格式。
  - 辅助请求（翻译）。
  - 前端 system 拼接后，模型能看到里面的内容。

### 发现
- **上次报的 77,753 token 没有复现**：同一条请求现在合计约 2.2 千 token。
- **中转站的 usage 数字不可信**：每条请求「读缓存」的数量，几乎正好等于上一条请求的总量；就算第一块 system 的内容完全不同也是这样，真实的 Anthropic 缓存不会这样。中转站很可能自己估算用量，77k 那次大概也是它的统计问题。
- **中转站会间歇性丢掉 system 提示词**：
  - 00:10–00:14 这几分钟里，直接（不经过网关）发给中转站的请求，大多数看不到 system 提示词，模型自称「一个 AI 开发环境，可以帮你写代码」。
  - 00:15 以后恢复，各种写法都是 10/10。
  - 这是中转站把请求转到了会替换提示词的通道，不是网关的问题。陪伴时 ta 如果突然「不认识你」、口吻变成编程助手，多半是这个原因。
  - 新增 `scripts/check_relay_system.py`，直接测中转站有没有把 system 送到，随时可以跑。

### 待办（下一步）
- **Pluvia**：前端按约定第 9 节联调；有对不上的地方再看前端代码。
- **中转站**：如果经常出现提示词被替换，联系中转站或换一个分组 / 渠道（先用 `check_relay_system.py` 确认）。
- **浮现门槛校准**：等真实数据多一些后，看 `gateway.log` 里的 `[SURFACE]` 行，重新校准 `SURFACE_MIN_SIM`（当前 0.55）。
- **Phase 1 剩余**：统一 UTC 时间、任务表 + worker（替代 cron）、迁移框架。
- **Phase 2**：召回编排（`/compose/*`、投递账本、Reranker、分层注入）。
