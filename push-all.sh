#!/bin/bash

# 代码推送脚本：一次推送到私有仓库(origin)和公开仓库(public)
# 用法：./push-all.sh
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "当前分支：$BRANCH"

# 1. 推送到私有仓库 memory-system-code
echo "推送到 origin (私有 memory-system-code)..."
git push origin "$BRANCH"

# 2. 推送到公开仓库 gongkai-code
echo "推送到 public (公开 gongkai-code)..."
git push public "$BRANCH"

echo "两个仓库均已同步到最新。"
