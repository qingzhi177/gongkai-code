#!/bin/bash
# 一键同步到双仓库：脱敏检查 → 提交 → 推私有(origin) → 推公开(public)
# 用法：cd ~/memory-system && ./sync-to-repos.sh
set -e
cd "$(dirname "$0")"

echo "[1/4] 脱敏检查..."
# 只检查将要提交的文件（.env 已被 gitignore，不会提交；仍显式排除）
FILES=$(git status --porcelain | awk '{print $2}' | grep -vE '(\.env$|venv/|node_modules/)' | head -60)
if [ -n "$FILES" ]; then
  LEAK=$(grep -n -E 'sk-[a-zA-Z0-9]{16,}|qingzhi520525525|8989884128|Bearer [A-Za-z0-9]{20,}' $FILES 2>/dev/null | grep -v '^sync-to-repos.sh:' | head -5)
  if [ -n "$LEAK" ]; then
    echo "!! 发现疑似敏感信息，中止："
    echo "$LEAK"
    exit 1
  fi
fi
# .env 若意外被暂存也中止
if git diff --cached --name-only 2>/dev/null | grep -qE '\.env$'; then
  echo "!! .env 被暂存，中止"
  exit 1
fi
echo "通过"

echo "[2/4] 暂存与提交..."
git add -A
MSG="$(date +%Y-%m-%d) TG↔Kelivo 会话同步增强

- 网关(server.js)：/v1/models OpenAI/Anthropic 双格式；组装模式(handleTgBotAssembled：last 历史 + 图片转 Anthropic + 会话窗口配置)；per-conv 串行队列；effort/thinking 注入；/api 薄代理；工具轮重发去重(cleanMessagesForSave)；dataDir 脱敏(env/homedir)
- 记忆服务(main.py)：conv_settings 表 + GET/PUT API；/conversations 列表 + 增量消息接口(origin 推导 + last 参数)；origin 归一化(tg 前缀→tg)；DATA_DIR 脱敏(expanduser)
- Dashboard(index.html)：新增「会话配置」面板(窗口模式/条数/token 预算/effort/模型)
- 文档：tg 对话功能.md"
git commit -m "$MSG" --allow-empty

echo "[3/4] 推送私有仓库 origin..."
git push origin HEAD

echo "[4/4] 推送公开仓库 public..."
git push public HEAD

echo "全部完成。commit: $(git log -1 --oneline)"