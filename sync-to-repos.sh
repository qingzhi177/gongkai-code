#!/bin/bash
# 一键同步到双仓库：脱敏检查 → 提交 → 推私有(origin) → 推公开(public)
# 用法：cd ~/memory-system && ./sync-to-repos.sh "提交说明"
set -e
cd "$(dirname "$0")"

echo "[1/4] 脱敏检查..."
# 检查范围：未提交的改动 + 还没推到公开仓库的提交里改过的文件
# （MCP 运维会自动 git 快照提交，只查未提交文件会漏掉）
git fetch -q public 2>/dev/null || true
FILES=$( { git status --porcelain | awk '{print $NF}'; git diff --name-only public/main..HEAD 2>/dev/null; } \
  | sort -u | grep -vE '(\.env$|venv/|node_modules/|\.leakcheck\.local$)' | while read -r f; do [ -f "$f" ] && echo "$f"; done | head -200)
# 通用模式写在这里；个人专属的检查词（口令、编号等）放在不入库的 .leakcheck.local 里，每行一个正则。
# 以前个人检查词直接写在本脚本里，而本脚本在公开仓库中 —— 检查本身就把要保护的内容公开了。
PATTERNS='sk-[a-zA-Z0-9]{16,}|Bearer [A-Za-z0-9]{20,}'
if [ -f .leakcheck.local ]; then
  EXTRA=$(grep -vE '^[[:space:]]*(#|$)' .leakcheck.local | paste -sd'|' -)
  [ -n "$EXTRA" ] && PATTERNS="$PATTERNS|$EXTRA"
fi
if [ -n "$FILES" ]; then
  LEAK=$(echo "$FILES" | xargs -d '\n' grep -H -n -E "$PATTERNS" 2>/dev/null | grep -v '^sync-to-repos.sh:' | head -5)
  if [ -n "$LEAK" ]; then
    echo "!! 发现疑似敏感信息，中止（只显示文件和行号）："
    echo "$LEAK" | cut -d: -f1,2
    exit 1
  fi
fi
if git diff --cached --name-only 2>/dev/null | grep -qE '\.env$'; then
  echo "!! .env 被暂存，中止"
  exit 1
fi
echo "通过"

echo "[2/4] 暂存与提交..."
git add -A
MSG="${1:-$(date +%Y-%m-%d) 同步}"   # 用法：./sync-to-repos.sh "提交说明"
git commit -m "$MSG" --allow-empty

echo "[3/4] 推送私有仓库 origin..."
git push origin HEAD

echo "[4/4] 推送公开仓库 public..."
git push public HEAD

echo "全部完成。commit: $(git log -1 --oneline)"