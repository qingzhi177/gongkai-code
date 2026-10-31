# cc-bridge

网关和本机 Claude Code 之间的桥。设计见 `docs/plan/claude-code-bridge.md`。

- `bridge.js`：HTTP 服务（只听 `127.0.0.1:3010`）。现在只有 print 跑法：每个对话一个常驻的 `claude -p`（stream-json 进、stream-json 出），转成统一的事件流给网关。
- `mcp_memory.js`：Claude Code 里的记忆工具（stdio MCP server，不依赖 npm 包），清单和执行都转给网关的内部接口（`127.0.0.1:3011`）。
- `lifeline.js`：PreToolUse 钩子，不许关掉自己、桥、网关，不许删记忆库的数据、碰登录凭证。

## 跑起来

```bash
# 登录：VPS 上的 claude 用订阅的长期 token 登录（环境变量 CLAUDE_CODE_OAUTH_TOKEN）。
# 做成服务时放在只有自己能读的环境文件里（chmod 600），不要进仓库、不要打印。
CC_HOME=~/companion-cc node services/cc-bridge/bridge.js
```

环境变量：`CC_BRIDGE_PORT`（3010）、`CC_MODE`（print）、`CC_HOME`（`~/companion-cc`，claude 的工作目录，会话记录按它归档）、`CC_MODEL`（opus）、`CC_THINKING_DISPLAY`（summarized；隐藏参数，不能用了设 off）、`CC_IDLE_CLOSE_MIN`（180）、`CC_MCP_WAIT_MS`（12000）、`GATEWAY_INTERNAL_URL`（`http://127.0.0.1:3011`）、`CLAUDE_BIN`（claude）。

子进程会去掉 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`：订阅额度只经过官方的 claude 程序用。

## 试一句（测试对话，不落盘）

```bash
curl -N -X POST localhost:3010/v1/turn -H 'content-type: application/json' \
  -d '{"conv_id":"cc-test","ephemeral":true,"model":"haiku","system":"你是测试用的助手。","content":"现在几点？用工具查一下。"}'
```

## 测试

`node tests/manual/cc_bridge/run_test.js`：生命线、MCP（假网关）、桥（假 claude），不花订阅额度。
Claude Code 升级以后，先用上面的「试一句」在测试对话里冒烟一遍（逐字、思考、工具、用量都要有），再用在真的聊天上。
