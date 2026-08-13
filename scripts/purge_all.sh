#!/bin/bash
# Memory System 数据硬清除脚本
# 警告：此操作不可逆，将删除所有对话、记忆、向量、叙事、摘要数据

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DB_PATH="$PROJECT_ROOT/data/sqlite/memory.db"
CHROMA_PATH="$PROJECT_ROOT/data/chroma"

echo "============================================"
echo "  Memory System 数据硬清除"
echo "============================================"
echo ""
echo "警告：此操作将删除以下所有数据："
echo "  - L0 原文对话"
echo "  - L1 提取记忆"
echo "  - ChromaDB 向量库"
echo "  - Shared Narrative 共同经历叙事"
echo "  - Recent Summary 近期摘要"
echo "  - User Profile 用户画像"
echo "  - AI Profile AI画像"
echo ""
echo "此操作不可逆！"
echo ""
read -p "确认清空所有数据？输入 YES 继续: " confirm

if [ "$confirm" != "YES" ]; then
    echo "操作已取消"
    exit 0
fi

echo ""
echo "[1/5] 停止 memory-service..."
sudo systemctl stop memory-service
sleep 2

echo "[2/5] 清空 SQLite 数据库..."
sqlite3 "$DB_PATH" <<SQL
DELETE FROM l0_messages;
DELETE FROM l1_memories;
DELETE FROM shared_narrative;
DELETE FROM recent_summary;
DELETE FROM user_profile;
DELETE FROM ai_profile;
VACUUM;
SQL
echo "  ✓ SQLite 已清空（保留表结构）"

echo "[3/5] 清空 ChromaDB 向量库..."
if [ -d "$CHROMA_PATH" ]; then
    rm -rf "$CHROMA_PATH"/*
    echo "  ✓ ChromaDB 已清空"
else
    echo "  ⚠ ChromaDB 目录不存在，跳过"
fi

echo "[4/5] 重启 memory-service..."
sudo systemctl start memory-service
sleep 6

echo "[5/5] 验证清空结果..."
L0_COUNT=$(curl -s localhost:8001/l0/page/0 | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('items', [])))" 2>/dev/null || echo "N/A")
L1_COUNT=$(curl -s localhost:8001/l1/count | python3 -c "import json,sys; print(json.load(sys.stdin).get('count', 'N/A'))" 2>/dev/null || echo "N/A")

echo ""
echo "============================================"
echo "  清空完成"
echo "============================================"
echo "  L0 原文数: $L0_COUNT"
echo "  L1 记忆数: $L1_COUNT"
echo ""
echo "现在可以重新导入对话数据。"
