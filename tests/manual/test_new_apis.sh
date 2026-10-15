#!/bin/bash
# 测试新增的记忆浮现 API

MEMORY_SERVICE="http://localhost:8001"

echo "========== 测试新 API 接口 =========="

echo -e "\n1. 测试 search_serendipity（无由来浮现）"
curl -s -X POST "$MEMORY_SERVICE/search_serendipity" \
  -H "Content-Type: application/json" \
  -d '{"min_arousal": 0.5, "cooldown_days": 7, "n": 5}' | jq '.total'

echo -e "\n2. 测试 increment_display_count"
curl -s -X POST "$MEMORY_SERVICE/memories/increment_display_count" \
  -H "Content-Type: application/json" \
  -d '{"memory_ids": ["l1_1", "l1_2"]}' | jq '.status'

echo -e "\n3. 测试 update_last_shown"
curl -s -X POST "$MEMORY_SERVICE/memories/update_last_shown" \
  -H "Content-Type: application/json" \
  -d "{\"memory_ids\": [\"l1_1\"], \"timestamp\": $(date +%s)000}" | jq '.status'

echo -e "\n4. 测试 increment_access_count"
curl -s -X POST "$MEMORY_SERVICE/memories/increment_access_count" \
  -H "Content-Type: application/json" \
  -d '{"memory_ids": ["l1_1"]}' | jq '.status'

echo -e "\n5. 检查数据库更新结果"
sqlite3 /home/qingzhi/memory-system/data/sqlite/memory.db \
  "SELECT id, display_count, last_shown_at, visibility FROM l1_memories WHERE id IN (1, 2) LIMIT 5;"

echo -e "\n========== 测试完成 =========="
