#!/bin/bash
# 安装日志轮转：不需要 sudo，用的是当前用户自己的 crontab。重复运行没关系（会替换掉旧的那一行）。
#   每天 03:30 检查一次（在 03:00 的备份之后），超过 5MB 的日志转存，保留 5 份；规则见 deploy/logrotate.conf.template。
# 用法：bash scripts/install_logrotate.sh          安装并试运行一次（只看会做什么，不真的转存）
#       bash scripts/install_logrotate.sh --now    安装后立刻按规则跑一次
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
LR=$(command -v logrotate || echo /usr/sbin/logrotate)
mkdir -p "$REPO/data/run" "$REPO/data/logs"
CONF="$REPO/data/run/logrotate.conf"
STATE="$REPO/data/run/logrotate.state"
sed "s#@REPO@#$REPO#g" "$REPO/deploy/logrotate.conf.template" > "$CONF"
LINE="30 3 * * * $LR -s $STATE $CONF"
( crontab -l 2>/dev/null | grep -v 'data/run/logrotate.conf' ; echo "$LINE" ) | crontab -
echo "已加进 crontab：$LINE"
if [ "${1:-}" = "--now" ]; then
  "$LR" -v -s "$STATE" "$CONF" 2>&1 | grep -E "rotating|log does not need|considering log" | head -20
else
  "$LR" -d -s "$STATE" "$CONF" 2>&1 | grep -E "rotating|log does not need|considering log" | head -20
fi
