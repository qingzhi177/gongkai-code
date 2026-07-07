#!/bin/bash

# 记忆系统备份脚本
BACKUP_DIR="$HOME/memory-system-backup"
DATA_DIR="$HOME/memory-system/data"
DATE=$(date +%Y-%m-%d_%H-%M)

echo "[$DATE] Starting backup..."

# 1. SQLite 热备份（不需要停服务）
sqlite3 "$DATA_DIR/sqlite/memory.db" ".backup '$BACKUP_DIR/memory.db'"

# 2. 复制画像文件
cp -r "$DATA_DIR/profile" "$BACKUP_DIR/"

# 3. 复制配置文件
cp "$HOME/memory-system/services/memory-service/.env" "$BACKUP_DIR/memory-service.env"
cp "$HOME/memory-system/services/gateway/.env" "$BACKUP_DIR/gateway.env"

# 4. 导出统计信息
echo "Backup date: $DATE" > "$BACKUP_DIR/status.txt"
curl -s http://localhost:8001/stats >> "$BACKUP_DIR/status.txt"

# 5. 推送到 GitHub
cd "$BACKUP_DIR"
git add -A
git commit -m "backup $DATE" --allow-empty
git push origin main -f

echo "[$DATE] Backup complete."
