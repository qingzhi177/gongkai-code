# Claude Code 接入设计（《优化与架构完善方案》Phase 3 · 9.10 节 cc-bridge 的细化）

> 2026-10-31 · 第一步已做：桥（print 跑法）、记忆工具（MCP）、生命线、网关的内部接口，真机冒烟通过。
> **聊天还没有切过去**：先做着，切换前用户要先在聊天里和 ta 说一声。
> 参考：一份以前和 Claude 讨论 tmux 常驻 Claude Code 的记录；两份教程《CC SDK / Codex SDK「近裸模型」教程》《Claude Code -p + stream-json 接入自建聊天前端（机教版）》。
> 代码：`services/cc-bridge/`（`bridge.js`、`mcp_memory.js`、`lifeline.js`）；网关 `server.js` 搜 `INTERNAL`。测试：`tests/manual/cc_bridge/run_test.js`、网关 T33。

---

## 1. 总体：网关还是中间层

```
Pluvia ──▶ 网关（存 L0、找记忆、换窗附注、缓存、工具事件）──▶ 上游 API          ← 现在（API 模式）
                                                       └──▶ cc-bridge ──▶ 官方 claude 程序（订阅登录）← 以后（Claude Code 模式）
                                                                 │
                                                  记忆工具（MCP）└──▶ 网关内部接口（127.0.0.1:3011）──▶ 记忆服务
```

- 以前担心「走订阅就不能当中间层了，只能在侧面靠钩子读 transcript」。实际上桥和网关在同一台机器上，网关照样拿到用户的话和 ta 的回复，存 L0、找记忆、换窗、工具事件都不用改路线。钩子只用来补 Claude Code 自己的事（压缩前、生命线）。
- 合规（方案 9.11 节）：订阅额度只经过官方的 `claude` 程序用；桥不读、不转发登录凭证，只让它留在子进程的环境里；启动子进程前去掉 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`（有它们时 claude 会改走 API 计费）。自己的订阅、自己的机器、自己用。

## 2. 三种跑法（`CC_MODE`），对网关是同一个接口

| | print（已做） | sdk（之后） | tmux（之后） |
|---|---|---|---|
| 怎么跑 | 每个对话一个常驻的 `claude -p --input-format stream-json --output-format stream-json` | Claude Agent SDK 的 `query()`（流式输入） | 交互式 `claude` 常驻在 tmux 里 |
| 输出 | 结构化事件：逐字、思考、工具调用、用量 | 同左（同一条通道） | 终端画面：给前端的话要靠 `reply` 工具推出去，工具卡片靠钩子 |
| 好处 | 零依赖、官方参数、事件干净 | system 能分成静态 / 动态两段（`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`），每天变的那段不拖累前面的缓存；`canUseTool` 回调做审批 | 能「看着它干活」：网页终端镜像、随时接手 |
| 用在 | 陪着聊天（默认） | print 稳定后，换成它更省缓存 | 想让它在机器上干活、调试的时候 |

桥的接口（只听 127.0.0.1:3010）：
- `POST /v1/turn` `{conv_id, system, content, model?, effort?, ephemeral?, tools?}` → SSE：`init`（session_id、工具、MCP 状态）→ `thinking` / `text`（逐字）/ `tool`（运行中 → 完成，带名字和结果）/ `retry` / `compact` → `done`（用量、session_id、本地估算的花费）。
- `POST /v1/interrupt`（用户点了停：发 `control_request` 的 interrupt，这一轮停下，会话还在）、`POST /v1/close`、`GET /v1/sessions`、`GET /health`。

## 3. print 跑法的细节（教程里的做法 + 实测）

- 参数：`--input-format stream-json --output-format stream-json --verbose --include-partial-messages`（常驻 + 逐字）；`--system-prompt-file`（整段换掉官方的 system，只剩一句身份行）；`--tools ""`（不给内置工具，陪着聊天用不上 Bash / 读写文件，也省掉几万字的工具说明）；`--mcp-config … --strict-mcp-config`（只挂记忆工具）；`--permission-mode dontAsk --allowedTools mcp__memory`（没预先放行的直接拒，不会卡在等确认）；`--thinking-display summarized`（看得到思考过程）。
- `--system-prompt-file`、`--thinking-display` 是 `--help` 里没有的隐藏参数，2.1.206 上实测都有效（假参数会报 unknown option）。版本升级后要先用测试对话（`--no-session-persistence`）冒烟一遍，绿了再用；`--thinking-display` 不能用了就设 `CC_THINKING_DISPLAY=off`。
- **记忆工具是异步连上的**：实测新进程要 3 秒左右 MCP 才从 pending 变成 connected。第一次冒烟时第一句话发得太早，模型手里没有工具。现在桥在新进程起来后用 `control_request` 的 `mcp_status` 轮询，连上了再发第一句话（最多等 12 秒）。
- 会话：一个对话一个进程，常驻；对话 id ↔ session_id 存盘，桥重启或进程闲太久（默认 3 小时）关掉以后，下次 `--resume` 接上。system 或模型变了（比如每天一次的日更块），在两轮之间换进程、用新的 system `--resume` 同一段会话——历史还在，只是那一轮缓存要重写一次（和 API 模式每天一次一样）。
- 登录：VPS 上用的是长期 token（写在 `~/.bashrc`）。桥做成服务时用一个只有自己能读的环境文件（600）传进去，不进仓库、不打印。

## 4. 记忆怎么接（和 API 模式同一套）

- **工具**：`mcp_memory.js` 是一个手写的 stdio MCP server（不依赖 npm 包），清单和执行都转给网关的内部接口：`GET /internal/tools`（和 API 模式同一份工具说明）、`POST /internal/tool`（同一套 `executeTool`，带上对话 id）。改工具说明只改网关一处，两条路不会走样。内部接口单独一个只听本机的端口（3011），nginx 不转发，外面访问不到；联网搜索不给（Claude Code 自己有 WebSearch，要不要开另说）。
- **每一轮的附注**：网关照旧算「现在几点、距上次多久、想起的几条记忆、还没整理进 system 的小本子」，接在用户这句话前面一起发给桥（以前放在最后一条 user 消息里，现在就是这一轮的 user 消息）。
- **system**：网关拼好的那几段（静态 + 画像 + 事实、半静态、日更）整段当 `--system-prompt-file`。
- **历史**：Claude Code 自己拿着这一段会话（`--resume`）；L0 仍然是网关存的那份，是唯一的原文。教程建议「自管历史」：重开进程时把历史重放进去，换窗（轮换会话）时就按这个做（见《换窗衔接设计》第 4 节的 Forge Reload），平时用 `--resume` 省事。
- **重新生成 / 编辑**：Pluvia 里重新生成了上一句，Claude Code 的会话里还留着旧的那句。第一版的做法：这一轮的附注里写明「对方让我重新回答上一句」；之后再考虑用 `--fork-session` 从上一轮分出去。

## 5. 缓存和心跳（Claude Code 模式）

- API 模式的心跳是「把上一轮的前缀原样再发一次」。Claude Code 模式不能直接往会话里发（会写进历史）。打算用一次性的分叉进程：`claude -p --resume <session> --fork-session --no-session-persistence`，读的是同一段前缀（system + 历史），不碰主会话的记录。要先实测：订阅登录下 Claude Code 用的缓存时长、分叉会不会命中同一段缓存、算不算额度。
- 订阅额度的用量和缓存的关系没有公开的细节，`done` 事件里的 `total_cost_usd` 只是按价目表估的，不是账单。

## 6. 那份 tmux 讨论里，哪些借鉴了、哪些留给 tmux 跑法

| 讨论里的点 | 现在 |
|---|---|
| 心跳保活 | API 模式已做（`keepwarm.js`）；Claude Code 模式见第 5 节 |
| 生命线红线（别让它把自己的 tmux 关了） | 已做：`lifeline.js`，PreToolUse 钩子，拦关 tmux / 结束桥和网关的进程 / 停服务 / 删记忆库数据和会话记录 / 碰登录凭证 / 强推。print 跑法默认没有 Bash，这条是给以后开工具时兜底的 |
| hub + reply 工具（终端输出不是结构化的） | print / sdk 跑法不需要：网关就是 hub，stream-json 本身就是结构化事件。tmux 跑法时再加 `reply` 工具 |
| 工具调用变成前端的小卡片 | print 跑法里工具调用和结果直接在事件流里（桥转成 `tool` 事件，网关转成 Pluvia 的 `tool_events`）。tmux 跑法用 PreToolUse / PostToolUse 的 HTTP 钩子，按 `tool_use_id` 对上 |
| 权限审批（PermissionRequest） | 现在只放行记忆工具（dontAsk），不会有确认框。以后开了别的工具：`--permission-prompt-tool` 指向一个 MCP 工具，把确认框做进 Pluvia，超时自动拒 |
| 可直接操作的镜像终端（ttyd / gotty） | 只给 tmux 跑法：只听本机，nginx 另加一层认证（不用聊天的 token），和结构化页面之间加「现在谁在控制」的锁 |
| 打断（Ctrl-C） | print 跑法用 `control_request` 的 interrupt；tmux 跑法用 `send-keys C-c` |
| 读 transcript 存原文 | 不需要：网关就在中间，L0 照旧存 |

## 7. 两份教程里用上的

- 《近裸模型》：去掉内置工具（`tools: []` / `--tools ""`）、`strictMcpConfig`、自定义工具的说明要写清楚什么时候用 / 不用（网关的工具说明本来就是这么写的）；验收看最终的工具清单和事件流，不看模型说「我没有工具」（冒烟时就是看 `init` 里的工具清单和 MCP 状态）；sdk 跑法的 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`。
- 《-p + stream-json》：一聊天一进程、按行解析加尾巴缓冲、启动前删 `ANTHROPIC_API_KEY`、`--system-prompt-file`、`--thinking-display summarized`、`--no-session-persistence` 做测试、锁版本升级先冒烟、`system/api_retry` 事件感知限流、`--fallback-model` 防高峰失联（之后加）。
- 没用的：`--bare`（不走订阅登录）、`--dangerously-skip-permissions`（用 dontAsk + 白名单代替）。

## 8. 冒烟（2026-10-31，真机，haiku，测试对话不落盘）

- 两轮：同一个进程；第二轮记得第一轮问了什么；思考过程（summarized）和正文都逐字出来；`done` 带用量。
- 第一次：第一轮时 MCP 还是 pending，模型说「看不到工具」。加了「等 MCP 连上再发第一句」以后：等了 3.1 秒，模型调了 `get_current_time`（经 MCP → 网关内部接口 → executeTool），回答了正确的时间；读缓存 9693。

## 9. 接下来

1. 网关：Dashboard 上加「聊天走哪条路」（API / Claude Code，默认 API）；Claude Code 这条路把 Pluvia 的请求转给桥、把桥的事件转回 Pluvia（逐字、思考、工具事件、用量），存 L0 照旧；第一轮带开窗包；桥出错时退回 API。**切换前用户先在聊天里和 ta 说一声。**
2. 桥做成服务（登录 token 用只有自己能读的环境文件）。
3. 换窗：按 `done` 里的用量算上下文占用，60–70% 时在空闲时轮换会话（Forge Reload，见《换窗衔接设计》第 4 节）；压缩前（PreCompact）把还没写的小结投出去。
4. Claude Code 模式的心跳（第 5 节，先实测）。
5. sdk 跑法；tmux 跑法（镜像终端、reply 工具、钩子出工具卡片、审批、控制锁）。
