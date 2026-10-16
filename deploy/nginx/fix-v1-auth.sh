#!/bin/bash
# 修复 nginx /v1/ 只认 "Authorization: Bearer" 的问题，让 Anthropic 格式客户端（x-api-key）也能通过。
#   1) 鉴权：Authorization: Bearer <token> 或 x-api-key: <token> 任一匹配即可（token 沿用现有配置里的，不改）
#   2) CORS 预检：放行浏览器实际请求的头（x-api-key / anthropic-version / x-conversation-id 等），
#      不再写死 "Authorization, Content-Type"
# 用法：sudo bash deploy/nginx/fix-v1-auth.sh
# 脚本本身不包含 token；会先备份原配置到 /etc/nginx/backup/，nginx -t 失败会自动还原。
set -euo pipefail
CONF=$(readlink -f /etc/nginx/sites-enabled/memory-system)
BK_DIR=/etc/nginx/backup
mkdir -p "$BK_DIR"
BK="$BK_DIR/memory-system.$(date +%Y%m%d-%H%M%S)"
cp -a "$CONF" "$BK"
echo "已备份到 $BK（不在 sites-enabled 里，不会被 nginx 加载）"

python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
if 'gk_auth' in s:
    print('已经修复过，跳过'); sys.exit(0)
m = re.search(r'([ \t]*)if \(\$http_authorization != "Bearer ([0-9A-Za-z._\-]+)"\) \{\s*return 401;\s*\}', s)
if not m:
    print('!! 没找到原来的 Bearer 校验块，未修改'); sys.exit(1)
ind, tok = m.group(1), m.group(2)
new_auth = (f'{ind}set $gk_auth 0;\n'
            f'{ind}if ($http_authorization = "Bearer {tok}") {{ set $gk_auth 1; }}\n'
            f'{ind}if ($http_x_api_key = "{tok}") {{ set $gk_auth 1; }}\n'
            f'{ind}if ($gk_auth = 0) {{ return 401; }}')
s = s[:m.start()] + new_auth + s[m.end():]
s, n = re.subn(r'add_header Access-Control-Allow-Headers "Authorization, Content-Type" always;',
               'add_header Access-Control-Allow-Headers $http_access_control_request_headers always;', s)
open(p, 'w', encoding='utf-8').write(s)
print(f'鉴权块已替换；CORS 预检头替换 {n} 处')
PY

if nginx -t; then
  systemctl reload nginx
  echo "✅ nginx 已重载"
else
  cp -a "$BK" "$CONF"
  echo "!! nginx -t 失败，已还原原配置"
  exit 1
fi
