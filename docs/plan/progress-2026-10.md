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
