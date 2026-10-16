#!/bin/bash
# 记忆系统每日备份（cron 每天 03:00 运行）
#
# 2026-10-16 修改：
#   - 不再复制两份 .env（密钥不进任何 git 仓库；请在密码管理器里另存一份 .env）
#   - 不再 git push -f（保留远端历史，任何一天的备份都能找回）
#   - 先备份到临时文件并做完整性检查，损坏的库不会覆盖上一次的好备份
set -euo pipefail

BACKUP_DIR="$HOME/memory-system-backup"
DATA_DIR="$HOME/memory-system/data"
DATE=$(date +%Y-%m-%d_%H-%M)

echo "[$DATE] Starting backup..."

# 1. SQLite 热备份（不需要停服务）→ 完整性检查通过后才替换
TMP_DB=$(mktemp /tmp/memory-backup.XXXXXX.db)
trap 'rm -f "$TMP_DB"' EXIT
sqlite3 "$DATA_DIR/sqlite/memory.db" ".backup '$TMP_DB'"
CHECK=$(sqlite3 "$TMP_DB" "PRAGMA integrity_check;")
if [ "$CHECK" != "ok" ]; then
  echo "[$DATE] !! 备份副本完整性检查失败（$CHECK），保留上一次备份，本次中止"
  exit 1
fi
cp -f "$TMP_DB" "$BACKUP_DIR/memory.db"

# 2. 复制画像文件（先清空再复制，删除过的画像文件不会残留）
rm -rf "$BACKUP_DIR/profile"
cp -r "$DATA_DIR/profile" "$BACKUP_DIR/profile"

# 3. 清理以前复制进来的 .env，并确保以后不会再被提交
rm -f "$BACKUP_DIR/memory-service.env" "$BACKUP_DIR/gateway.env"
touch "$BACKUP_DIR/.gitignore"
grep -qxF '*.env' "$BACKUP_DIR/.gitignore" || echo '*.env' >> "$BACKUP_DIR/.gitignore"

# 4. 导出统计信息
echo "Backup date: $DATE" > "$BACKUP_DIR/status.txt"
curl -s -m 10 http://127.0.0.1:8001/stats >> "$BACKUP_DIR/status.txt" || echo "(stats 获取失败)" >> "$BACKUP_DIR/status.txt"

# 5. 推送到 GitHub 私有备份仓库（普通 push，不覆盖历史）
cd "$BACKUP_DIR"
git add -A
git commit -q -m "backup $DATE" --allow-empty
git push -q origin main

echo "[$DATE] Backup complete."
