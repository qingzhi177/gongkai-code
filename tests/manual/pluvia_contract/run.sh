#!/bin/bash
# Pluvia 接入约定 v0.2 的端到端测试：假上游 + 假记忆服务 + 一个临时网关实例（端口 3999）。
# 不花 token、不碰真实记忆库、不读个人画像；正在运行的网关（3000）不受影响。
# 用法：bash tests/manual/pluvia_contract/run.sh [要测的网关文件，默认 services/gateway/server.js]
set -u
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
HERE="$ROOT/tests/manual/pluvia_contract"
GW_FILE=$(readlink -f "${1:-$ROOT/services/gateway/server.js}")
T=${GWTEST_DIR:-/tmp/gwtest}
rm -rf "$T"; mkdir -p "$T/data/profile"
export GWTEST_DIR="$T" NODE_PATH="$ROOT/services/gateway/node_modules"
cd "$T"   # 不在网关目录下启动，免得读到真实 .env
node "$HERE/fake_upstream.js" > "$T/up.log" 2>&1 & P1=$!
node "$HERE/fake_memory.js" > "$T/mem.log" 2>&1 & P2=$!
sleep 0.5
PORT=3999 GATEWAY_BIND_HOSTS=127.0.0.1 MEMORY_SERVICE_URL=http://127.0.0.1:3998 MEMORY_DATA_DIR="$T/data" \
  node "$GW_FILE" > "$T/gw.log" 2>&1 & P3=$!
for i in $(seq 1 40); do curl -sf http://127.0.0.1:3999/health >/dev/null && break; sleep 0.25; done
sleep 0.5
node "$HERE/run_test.js"; RC=$?
kill $P1 $P2 $P3 2>/dev/null; wait 2>/dev/null
[ $RC -ne 0 ] && echo "网关日志：$T/gw.log"
exit $RC
