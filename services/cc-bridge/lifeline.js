#!/usr/bin/env node
// 生命线：Claude Code 的 PreToolUse 钩子（cc-bridge 用 --settings 挂上，只管 Bash）。
// 就算以后给 Claude Code 开了 Bash，也不许它：关掉自己所在的 tmux / 桥 / 网关 / 记忆服务，删记忆库的数据或会话记录，
// 改登录凭证，强推代码。命中了就拒绝这一次调用，并告诉它为什么（它会看到原因，可以换个做法或者问你）。
// 输入是钩子的 JSON（stdin）：{ tool_name, tool_input: { command } }；不命中时什么都不输出。
'use strict';

const RULES = [
  [/\btmux\s+(kill-session|kill-server|kill-window)\b/, '不能关掉 tmux 会话（Claude Code 自己就在里面）'],
  [/\b(pkill|killall)\b[^\n]*\b(claude|node|tmux|ttyd|python|uvicorn)\b/, '不能结束 Claude Code、桥、网关或记忆服务的进程'],
  [/\bkill\s+(-\w+\s+)*(-1|1)\b/, '不能结束所有进程'],
  [/\bsystemctl\s+(stop|disable|mask|kill)\b/, '不能停掉系统服务'],
  [/\b(rm|shred|truncate)\b[^\n]*(memory-system\/data|memory\.db|\/data\/sqlite|\.claude\b|companion-cc)/, '不能删记忆库的数据、会话记录或陪伴目录'],
  [/\bgit\s+push\b[^\n]*(--force|-f\b)/, '不能强推'],
  [/\.credentials\.json|CLAUDE_CODE_OAUTH_TOKEN|setup-token/, '不能碰登录凭证'],
  [/\b(mkfs|dd\s+if=|:\(\)\s*\{)/, '危险命令']
];

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { input += c; });
process.stdin.on('end', () => {
  let cmd = '';
  try {
    const j = JSON.parse(input || '{}');
    cmd = String((j.tool_input && j.tool_input.command) || '');
  } catch (e) { return; }
  for (const [re, why] of RULES) {
    if (re.test(cmd)) {
      process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny',
        permissionDecisionReason: `生命线：${why}。这条命令没有执行。` } }));
      return;
    }
  }
});
