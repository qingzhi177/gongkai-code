# Pluvia ↔ 记忆网关 接入约定（v0.1 · 2026-10-16）

> 给负责 Pluvia 前端的窗口看的。描述的是网关**现在**的行为，以及前端怎么配合才能让记忆存得准。

## 1. 地址与鉴权
- OpenAI 格式：`POST https://pluviora.cc/v1/chat/completions`
- Anthropic 格式：`POST https://pluviora.cc/v1/messages`
- 模型列表：`GET https://pluviora.cc/v1/models`。请求头带 `anthropic-version` 或 `x-api-key` 时，按 Anthropic 格式返回。
- 鉴权：`Authorization: Bearer <token>`，或 `x-api-key: <token>`（后者需要先执行一次 `deploy/nginx/fix-v1-auth.sh`）。
- 浏览器跨域：同样执行修复脚本之后，预检会放行前端实际发送的所有请求头。

## 2. 会话 id（最重要）
- **每个请求都带上稳定的会话 id**，用 Pluvia 自己的会话 UUID。优先级如下：
  1. body 里的 `conversation_id`（推荐，不涉及跨域问题）；
  2. 请求头 `x-conversation-id`。
- 都不带时，网关用"首条 user 消息的 MD5"兜底。这种方式很脆弱：首条消息一改，或者带上时间戳，会话就会被拆开。

## 3. 客户端标识
- 请求头 `x-client-name: pluvia`（不带时记作 `kelivo`）。
- ⚠️ 名字里**不要**包含 `tg`：网关看到 `tg` 会走 TG 组装模式。

## 4. 消息与时间戳
- 照常发送可见的历史消息。网关做记忆检索、存 L0、算兜底会话 id 之前，会先剥掉当前消息末尾的 `（此刻：…）`；发给模型的原文不变。
- 网关自己也会注入时间头 `[当前时间：…]`。前端的时间戳可以保留，也可以去掉。

## 5. 不进记忆的请求
- 标题生成、连接测试这类请求，请在 body 里带上 `memory_skip: true`，或者加请求头 `x-memory-skip: 1`。
- 带上以后，网关照常返回结果，但**不存进记忆库**，也不计入记忆展示次数。

## 6. 工具调用（尚未适配）
- 现状：网关会把前端传来的 `tools` 和自己的记忆工具（`recall` / `feel` / `update_profile` / `get_current_time` / `bocha_search`）合并后发给模型，**所有工具调用都在服务端执行**。前端自己的工具（比如"拾贝"）在服务端找不到实现，会返回"未知工具"。
- 计划：网关只执行自己的记忆工具。遇到前端工具时，结束本轮并把 tool_use（Anthropic）或 tool_calls（OpenAI）原样返回给前端；前端执行完后，在下一次请求里带上 tool_result 继续。具体协议需要和 Pluvia 一起定（流式事件格式、多工具并发、记忆工具执行过程要不要展示给前端等）。
