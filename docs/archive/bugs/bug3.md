Bug3
你在排查并修复一个 bug。项目是记忆库网关，代码在 services/gateway/server.js（Node.js）。
前置知识：kelivo 是 Flutter 客户端，通过 Anthropic 格式流式调用本网关；
网关内部执行工具（recall 等），只向前端发只读 tool_use 卡片做展示。

【Bug 现象】
用 kelivo 对话，工具卡片（如 recall）在对话窗口内显示正常；但退出/切换窗口重进后，
所有工具卡片都变成了"联网搜索"（联网检索）卡片。

【根因（已定位，不要改动以下文件以外的逻辑）】
1. server.js 的 toolResolve() 方法（约668-682行）在工具执行完后发一个type='web_search_tool_result' 的 content_block，tool_use_id 用真实工具调用 id，content 为空数组。唯一调用点在约1068行：emitter.toolResolve(toolUse.id, toolResult);
2. kelivo 的 Claude 流式解析器（claude_official.dart）对web_search_tool_result 块硬编码转成ToolResultInfo(name: 'search_web')，并按 tool_use_id 命中数据库中已保存的 recall 工具事件，整体替换其 name 为'search_web'（upsertToolEvent 逻辑）。于是数据库里被污染成 search_web，重进窗口从数据库恢复时卡片就显示成"联网搜索"。
3. kelivo 本地执行路径其实已经能正确解除工具卡片 loading：kelivo 收到 tool_use 块后，在 content_block_stop 时会调用本地 onToolCall，对网关侧工具（recall 等）会立即返回空字符串（不抛异常、不卡住），并产出 name 为真实工具名（如 recall）的 ToolResultInfo，从而解除 loading。所以 toolResolve 补发的 web_search_tool_result 是多余的，且是污染源。

【修改要求（只改 server.js）】
1. 注释掉约1068行的 emitter.toolResolve(toolUse.id, toolResult); 这一行调用（保留 toolResolve 方法定义本身不动，以后可能有用）。
注意：只注释、不要替换成其他块类型——kelivo 的 Claude 流式解析器没有tool_result 分支（标准 tool_result 是客户端发给服务端的，不出现在服务端流里），改成标准 tool_result 块会导致 kelivo 无法解析、loading 永不解除，是无效方案。也不要改动 toolResult 变量声明，它下面仍被 toolResults.push 使用。
2. 在注释旁加一行中文注释说明原因：删除后依赖 kelivo 本地路径解除工具卡片 loading，name 保持真实工具名（如 recall），避免 web_search_tool_result 把持久化的工具名污染成 search_web（会导致重进窗口工具卡片变成"联网搜索"）。
3. 不要改动任何其他逻辑（包括 toolUseCard、isToolLoopback、工具循环、usage 统计等）。

【验证步骤】
1. 重启网关服务（pm2 restart 或按你项目的方式）。
2. 用 kelivo 发一条会触发 recall 的消息，观察：
a. 对话窗口内工具卡片是否正常显示"recall"且不再一直 loading；
b. 退出该窗口再重新进入，工具卡片是否仍然是 recall（而不是"联网搜索"）；
c. token 显示是否正常（不恒为 0）。
3. 注意：必须用【修复后新产生的对话】验证。修复前已经产生的旧对话，kelivo 本地数据库里已存了被污染的事件，重进仍会显示"联网搜索"，那是历史数据问题，不代表修复失败。
4. 若 a/c 出现"卡片一直 loading / token 恒 0"（即 kelivo 本地路径不解除 loading），则回滚第1步的注释，恢复 emitter.toolResolve 调用，并回报"本地路径不工作"，我们再讨论其他方案。

【注意】
- 只改 server.js 这一个文件，别动memory-service、dashboard 等其他目录。
- 改完先自己读一遍 diff 确认没有多余改动，再让我确认和验证。
