# Memory System v1.3

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
