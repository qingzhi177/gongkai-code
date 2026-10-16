# Pluvia ↔ 记忆网关 接入约定（v0.2 · 网关已实现 · 2026-10-17）

> 回复前端的 v0.2 草案。
> - **第 0–5 节**：网关现在的行为（四项 feature 都已上线）。
> - **第 6 节**：答复前端第 6 节的六个问题。
> - **第 7 节**：网关这边请前端确认或配合的事。
>
> 网关代码：`services/gateway/server.js`（搜 `Pluvia`）。测试：`bash tests/manual/pluvia_contract/run.sh`，用假上游和假记忆服务跑 49 项，不花 token，不碰真实记忆库。

## 0. 沿用 v0.1 的部分
- **地址**：
  - OpenAI 格式 `POST https://pluviora.cc/v1/chat/completions`
  - Anthropic 格式 `POST https://pluviora.cc/v1/messages`
  - 模型列表 `GET https://pluviora.cc/v1/models`
- **鉴权**：`Authorization: Bearer <token>` 或 `x-api-key: <token>`。
- **会话 id**：body 里的 `conversation_id`（优先），或请求头 `x-conversation-id`。
- **客户端标识**：请求头 `x-client-name: pluvia`。名字里不要带 `tg`。
- **不进记忆的请求**：body 里带 `memory_skip: true`，或请求头 `x-memory-skip: 1`。

## 1. 能力协商
```
GET {base}/pluvia/contract
→ {"contract":"0.2","features":["turn","tail","client_tools","tool_events"],"time_header":"auto"}
```
- `time_header` 是多给的字段：网关时间头当前的模式（见第 3 节）。前端可以拿来提示用户，不用也行。
- **扩展字段不会发给上游**：网关发给上游的请求体是自己重新组装的，`conversation_id`、`memory_skip`、`pluvia` 都不会出现在里面（已测试）。
- **OpenAI 格式的消息**：网关会先转成 Anthropic 格式再发给上游：
  - `tool_calls` → `tool_use`
  - `role: "tool"` → `tool_result`
  - `image_url` → `image`
  - `reasoning_content` 没有签名，没法回传，会被丢掉。

## 2. `turn`
- **只在一条回复的最后一轮存 L0**：
  - 各轮的文字合成一条 assistant 存，轮与轮之间用换行连接。
  - 中间轮次的 assistant（工具调用）、工具结果、附图消息，都不进 L0。
- **用户点「停」**（断开连接）：网关立刻中止上游请求，这一轮不存。
- **上游出错**：这一轮不存，但网关存的上一轮状态会保留，前端可以用同一个 `round` 重试。
  - 出错原因会以 `[网关错误] <上游给的说明>` 的形式出现在正文里，比如「账户余额不足」。
  - 网关只对可能是偶发的错误重试（网络错误、5xx、429）。
- `regen_of` 见 6.3。

## 3. `tail` 与时间头
- **精确剥离**按草案实现，额外容忍首尾空白的差异：
  - 字符串内容：要求 tail 前面是换行。
  - 数组内容：去掉最后一个文本等于 tail 的块。
  - 网关从后往前，找第一条能精确剥掉 tail 的 user 消息。
- **没带 tail 时**用草案里放宽的兜底规则：剥掉末尾连续的、整行被全角括号包住、并以「此刻：」「离上一句」「正在听」「ta 排的那首」开头的段落。
- **网关时间头**：在 `.env` 里设 `GATEWAY_TIME_HEADER`，默认 `auto`。
  - `auto`：tail 里有「（此刻：」，网关就不加 `[当前时间]`；有「（离上一句」，就不加 `[距上次对话]`。
    - 用户在 Pluvia 里关掉「此刻」以后，网关会自动补上自己的钟。**所以任何时候都只有一个钟。**
  - `on`：总是加。
  - `off`：都不加。
- **记忆目录的注入位置**：
  - Anthropic 格式：最后一条 user 消息里如果有带 `cache_control` 的块，网关就把记忆目录和时间头插在这个块后面、tail 前面。这样缓存标记之前的前缀每轮不变，下一轮还能命中缓存。
  - OpenAI 格式：照旧放在消息开头。
- **缓存断点上限**：Anthropic 一次请求最多 4 个 `cache_control`，网关的 system 自己用 1–3 个。超出时，网关会去掉消息里最早的几个。**建议前端在消息里最多打 1 个。**

## 4. `client_tools`
- **网关执行**：`recall` / `feel` / `update_profile` / `get_current_time` / `bocha_search`。其余工具原样交回前端。
  - 和前端工具撞名时以网关为准，前端那份会被去掉。
- **OpenAI 流式交出**：
  - 每个前端工具调用发一个 `tool_calls` chunk。`index` 从 0 开始，只包含前端工具，`arguments` 是完整的 JSON 字符串。
  - 然后发 `finish_reason: "tool_calls"`、usage、`[DONE]`。
- **Anthropic 流式交出**：
  - `tool_use` 块（依次是 `content_block_start` → `input_json_delta` → `content_block_stop`），然后 `stop_reason: "tool_use"`。
  - 多次上游调用合成一条流时，`index` 连续递增。
  - 每次上游调用结束，网关都会关掉当前块，所以每一轮的 thinking 和 text 各自成块，不会拼在一起。
- **续问**：按草案。网关以自己存的那一轮为准，包括思考块签名、记忆工具的调用和结果、第 1 轮注入的记忆目录。前端回传的 assistant 只用来核对 `tool_call_id`。
  - 第 2 轮起不再重新检索记忆，也不重新加时间头，保证缓存命中。
  - 工具结果按原来的调用顺序回填。前端漏了哪个结果，网关会补一条 `is_error` 的「前端没有返回这个工具的结果」。
  - **附图**：工具结果后面另起的 user 消息，会并进同一条（`tool_result` 在前，图在后）。也可以直接把 `image_url` 放进 tool 消息的 content 数组，网关会转换。
  - **状态存 10 分钟**。找不到时（过期、网关重启、id 对不上），网关会按前端回传的历史转换后继续，但这样拿不回思考签名，这一轮就不带 `thinking` 参数。
- **`tool_choice`**：OpenAI 的 `"none"` / `"auto"` / `"required"` / `{type:"function"}` 会转成 Anthropic 格式。
- **记忆工具连续调用**：一次请求里，网关内部最多调 5 次上游。

## 5. `tool_events`
- 按草案实现，有两处细节：
  - `id` 用上游的 tool_use id（`toolu_…`），不是 `mem_1`。
  - `status` 依次是 `running`，然后 `done` 或 `error`。
- `content` 是给模型的那段结果，最多 2000 字。recall 结果里的情感打分（valence / arousal 那一行）已经去掉。
- **非流式**：在顶层放 `pluvia_tools`，只放最终状态。
- 只对带 `pluvia` 字段的请求发这个事件，Kelivo 不受影响。

## 6. 答复前端第 6 节
1. **协商地址**：用 `GET /v1/pluvia/contract`。不放进 `/v1/models`，因为那里要同时兼容两种 SDK 的格式，多加的字段容易被校验掉。
2. **记忆工具的结果给不给看**：给看。
   - recall 返回的是你们自己的记忆原文，内部打分已经去掉。
   - feel / update_profile 回显记录内容的前 80 字。
   - bocha_search 是网页摘要。
   - 哪一类不想显示，前端按 `name` 过滤就行。
3. **重新生成（`regen_of`）**：网关不用特别处理。
   - 新回复存进 L0 的同一个位置，记忆服务按序列对齐，把旧版本标成 `superseded`。
   - 旧版本如果已经被提取成 L1 记忆，那一批 L1 会作废并重新提取。这种情况很少，因为提取要等对话安静 10 分钟。
   - 切回旧版本再接着聊，会再切换一次，处理方式相同。
4. **删除会话**：要通知。网关已经提供两个地址：
   - `POST /v1/pluvia/conversations/{id}/delete`（**推荐**，因为 nginx 预检目前只放行 GET / POST）
   - `DELETE /v1/pluvia/conversations/{id}`（nginx 预检加上 DELETE 以后可用）
   - **效果**：这个会话的 L0 软删；从它提取出的 L1 记忆**硬删**，向量一并清掉。**不可恢复。** 已经写进共同叙事（narrative）的内容不会自动修改。
   - 建议删除确认框里单独问一句「同时删除记忆库里的记录？」，用户勾选了才调用。
   - 返回 `{"ok":true,"l1_removed":N}`。
5. **`edit_of`**：暂时不需要。编辑后重发，新的 user 消息会替换 L0 里原来那条（同样按序列对齐），效果和重新生成一样。以后要做「编辑历史」再加。
6. **预检放行的请求头**：已经放行。nginx 预检会把浏览器实际请求的头原样放行，包括 `Authorization` / `x-api-key` / `anthropic-version` / `anthropic-dangerous-direct-browser-access` / `Content-Type` / `x-client-name`。方法目前只放行 GET / POST / OPTIONS。

## 7. 请前端确认或配合
1. **系统提示词会被丢掉**：
   - 网关目前不使用客户端发来的 system（OpenAI 的 `role: "system"` 和 Anthropic 的 `system` 字段都一样），而是用记忆库自己的 system：画像 + 共同叙事 + 近期摘要 + Dashboard 里的自定义提示词。这是当初为 Kelivo 定的设计。
   - **Pluvia 的系统提示要不要生效，等用户决定。** 要生效的话，网关会把它接在记忆库 system 的后面。
2. **「工具记录」那一行**：
   - 以后的历史里，前端会给前端工具留一行「工具记录」。网关存 L0 时，存的是整条回复的文字。
   - 如果下一轮历史里，这条回复多出一行记录，记忆服务会当成「内容改了」再存一个版本。这不会出错，但会多一次版本切换；如果那一批已经提取过，还会重新提取。
   - **请告诉我这一行的固定格式**，比如单独一行、以「（工具记录：」开头。网关存 L0 时把它剥掉就好。
3. **历史里过去的工具轮次**：以后的历史里，前端会不会保留过去回复的 `tool_calls` / `role:"tool"` 消息？
   - 保留也能正常工作：网关会转换格式，存 L0 时把这类消息跳过，相邻的几条 assistant 合成一条。
   - 只留「工具记录」那一行更省 token。
