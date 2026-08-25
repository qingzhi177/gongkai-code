#!/bin/bash
# 一键同步到双仓库：脱敏检查 → 提交 → 推私有(origin) → 推公开(public)
# 用法：cd ~/memory-system && ./sync-to-repos.sh
set -e
cd "$(dirname "$0")"

echo "[1/4] 脱敏检查..."
FILES=$(git status --porcelain | awk '{print $2}' | grep -vE '(\.env$|venv/|node_modules/)' | head -60)
if [ -n "$FILES" ]; then
  LEAK=$(grep -n -E 'sk-[a-zA-Z0-9]{16,}|qingzhi520525525|8989884128|Bearer [A-Za-z0-9]{20,}' $FILES 2>/dev/null | grep -v '^sync-to-repos.sh:' | head -5)
  if [ -n "$LEAK" ]; then
    echo "!! 发现疑似敏感信息，中止："
    echo "$LEAK"
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
MSG="$(date +%Y-%m-%d) 数据管理全家桶（备份导入/导出导入/feel）+ 和弦情绪锚点接入 + README 更新

- 数据管理：
  * Kelivo 备份导入（kelivo-backup v2 sqlite）：预览会话列表 / 版本折叠（version_selections 优先）/ 指定会话导入 / skip|append；旧版 JSON 导入保留
  * 记忆库整体导出/导入（zip：L0/L1/thinking/narrative/summary/custom_prompt/conv_settings/profile + manifest）
  * feel 单独导出/导入（?source=all|self|extracted；导入补 ChromaDB 向量）
  * Dashboard「备份管理」面板（上传-预览-勾选-导入 / 导出下载）；nginx /import/ /export/ 直连记忆服务
- 和弦情绪锚点（v0.1 接入）：
  * l1_memories.anchor_json 列（幂等 migration）+ parse_anchor 解析（>情境行 + >和弦行 ·bpm ·力度）
  * 网关 feel 工具描述引导规则（记录/回顾用，日常不用；≤4和弦；紧张系加动作词）
  * recall 结果标注 feel 来源（AI自述/对话提取）
- README：数据管理 + 和弦锚点 section；架构设计分析见《和弦锚点架构设计.md》"
git commit -m "$MSG" --allow-empty

echo "[3/4] 推送私有仓库 origin..."
git push origin HEAD

echo "[4/4] 推送公开仓库 public..."
git push public HEAD

echo "全部完成。commit: $(git log -1 --oneline)"