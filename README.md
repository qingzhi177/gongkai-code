# Memory System v1.6

AI 长期记忆系统，基于 Paramecium 架构。

## 功能
- L0 对话原文存储（带版本管理）
- L1 记忆索引（DeepSeek 提取，保留语感）
- L2 人格画像（关于用户/AI/我们）
- recall/feel/update_profile 工具（AI 自主调用）
- bocha_search 联网搜索工具（博查 API，可信中文实时信息）
- BM25 + 向量 RRF 融合检索
- 意图路由（时间/事实/情感/语义）
- Web Dashboard（管理面板 + 多供应商模型配置）
- 自动备份（GitHub）

### 网关能力（services/gateway）
- 流式输出（SSE）：前端实时逐字显示，同时保留非流式兼容（可回退）
- 思维链透传：按 Anthropic 标准透传 thinking 块，前端折叠区渲染
- 思考预算调节：透传 output_config(effort)，前端预算滑块真正生效
- 工具调用可见：工具卡片原生渲染在前端工具区，正文与工具分离
- 工具执行始终在网关内部完成，结果不混入正文、不存入记忆
- Token usage 透传：真实 token 用量回传前端（流式/非流式，跨工具轮次累加）
- Prompt caching：system 前缀（角色+画像+工具）带缓存断点，命中省钱省延迟
- 多供应商热切换：从记忆服务读配置，Dashboard 改完自动热重载（不重启进程）
- 联网搜索：bocha_search 走博查 API；供应商内置 web_search（server-side）自动生效
- 工具格式归一：OpenAI 格式工具自动转 Anthropic 原生格式，兼容新版 API

## 更新日志
- v1.6 — Shared Narrative + Dashboard 扩展 + 更新检查周期
  - Shared Narrative：月相式记忆总结，读取所有 L1 memories 生成整体叙事
    - 新增 narrative.db：存储 narrative 内容、生成时间、使用的模型配置
    - POST /narrative/generate：生成新 narrative（支持强制重新生成）
    - GET /narrative/latest：获取最新 narrative
    - GET /narrative/history：查看历史 narrative 列表
    - DELETE /narrative/{id}：删除指定 narrative
    - Dashboard 新增 Narrative 页面：显示月相进度条、生成按钮、历史记录
  - Dashboard 配置扩展：Provider 多模型管理 + Narrative 独立配置
    - Provider 管理：新增/编辑/删除多个 API 供应商配置
    - 每个 provider 支持多个模型列表（如 claude-opus-4-6, claude-opus-4-7）
    - Narrative 专用模型配置：独立于主对话模型，可选择更强大的模型生成总结
    - API Key 加密存储：使用 Fernet 对称加密保护敏感信息（CONFIG_SECRET_KEY）
    - Dashboard 显示 provider 列表、激活状态、测试连接功能
  - 更新检查周期：识别长期未更新的 L1 memories
    - GET /updates/check：检查超过指定周期未更新的 memories
    - POST /updates/mark：标记 memory 为已更新
    - GET/PUT /updates/config：配置更新检查周期（默认 30 天）
    - Dashboard 新增更新检查页面：显示待更新列表、一键标记、批量操作
  - Narrative 生成提示词：月相比喻 + 重要性评分（⚫⚪）+ 时间线叙事
- v1.5 — 重构 L1 删除/重提取：统一硬删 + 单条重提取
  - L1 统一硬删（SQLite DELETE + ChromaDB delete）：修复原来只标 superseded
    从不删向量、已删记忆仍被 /search 命中的问题（ai_self 感受删不掉最明显）
  - 覆盖 delete_l1 / 批次联动 / delete_l0_conversation / delete_l0_message；
    L0 仍保持软删（superseded）
  - 新增 POST /l1/{id}/reextract：有 L0 关联的 L1 基于原文重新提炼→生成新 L1
    →硬删旧 L1，新 L1 沿用旧 source_msg_id（批次锚点）/conv_id/client；
    提取失败则旧记忆保留；ai_self（无 L0 关联）自然被拒
  - update_l1 加存在性校验（已删 id 返 404）；/l1/list 加 has_source 字段；
    /search 加 SQLite active 兜底校验（双保险，防残留向量）
  - 看板：有关联的 L1 显示「重提取」按钮，ai_self 不显示
- v1.4 — 时间感知：时间锚点 + 会话间隔感知
  - 问题1 时间锚点：每轮在 memoryMenu 头部注入「当前时间（北京时间）」，
    让 LLM 正确理解历史记忆里的相对时间是过去事件，而非当前状态
  - 问题2 会话间隔感知：注入「距上次对话约N小时/天」（阈值 4 小时触发，
    连续对话不打断）；memory-service 新增 GET /last_session 接口
  - 时间感知信息注入 memoryMenu（不进 system 前缀），不影响 prompt caching
  - L1 提取规则：不再写相对时间描述（「过了十一天」），时间感知交给系统注入
  - 附带修复：会话间隔计算的 8 小时偏移（间隔用真实 UTC epoch，+8 仅用于显示串）
  - Bug3 排查结论：工具卡片重进窗口变「联网搜索」的污染源在 kelivo 侧
    （web_search_tool_result 被硬编码成 search_web），已于 kelivo 侧按
    tool_use_id 取回真实工具名修复；网关侧保留 toolResolve（负责解除 loading）
- v1.3 — 第二阶段：Token usage 透传、上下文缓存优化、Dashboard 多供应商配置、联网搜索
  - 功能4 Token usage 透传（流式/非流式，跨轮累加）
  - 功能5 Prompt caching（稳定 system 前缀带缓存断点，memoryMenu 移出 system）
  - 功能6 Dashboard 模型配置（多供应商增删改 + 切换 + 热重载，API Key 用 Fernet 加密存储）
  - 功能7 联网搜索（bocha_search 博查 API）
  - 附带修复：工具格式兼容新版 Anthropic API（OpenAI 格式自动转原生 type:custom）
- v1.2 — 网关流式化：SSE 流式输出、思维链透传、思考预算生效、工具调用前端可见
  - 附带修复：时间工具名匹配（get_time_info）、流式空回（headers-sent）、
    预算调节失效（output_config 被丢弃）、工具卡片导致的重复回答（回环拦截）
- v1.1 — 支持中转站 Claude 模型 + Anthropic 格式路由
- v1.0 — 记忆系统完整初始版本

## 部署
1. 安装 Docker/Node.js/Python3/nginx
2. docker compose up -d（启动 ChromaDB）
3. 记忆服务：cd services/memory-service && python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
4. 网关：cd services/gateway && npm install
5. 复制 .env.example 为 .env，填入 API Key
   - 记忆服务 .env 需含 CONFIG_SECRET_KEY（Fernet 密钥，加密存储供应商 API Key 用；
     用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成，
     务必单独备份——丢失则已存的供应商 API Key 无法解密）
   - 网关 .env 需含 BOCHA_API_KEY（博查联网搜索）
6. 配置 systemd 服务 + nginx 反向代理 + certbot SSL
7. 配置 cron 定时 L1 提取（每10分钟）

## 文件结构
- docker-compose.yml：ChromaDB 容器
- backup.sh：自动备份脚本
- services/memory-service/：Python 记忆服务
- services/gateway/：Node.js 网关
- services/dashboard/：前端管理面板
- data/：数据目录（不入 Git）
