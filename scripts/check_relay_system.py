#!/usr/bin/env python3
"""检查上游中转站有没有把我们的 system 提示词送到模型那里。

背景（2026-10-18）：测试时发现，中转站有几分钟把请求转到了另一条通道——那段时间模型完全看不到我们的
system 提示词，还自称「一个 AI 开发环境，可以帮你写代码」；几分钟后又恢复正常。网关发出去的内容是对的，
问题在中转站。陪伴时如果 ta 突然「不认识你」、口吻变成编程助手，可以跑一下这个脚本看看是不是又发生了。

做法：用 Dashboard 里当前启用的供应商配置，直接（不经过网关）发 N 条很短的请求，
system 里只写一条要求「每条回复都要出现『西瓜』」，统计有几条照做了。会花很少的 token。

用法：python3 scripts/check_relay_system.py [次数，默认 10]
"""
import concurrent.futures as cf
import json
import sys
import urllib.request

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
cfg = json.load(urllib.request.urlopen('http://127.0.0.1:8001/config/current', timeout=5))
if not cfg.get('configured'):
    sys.exit('记忆服务里没有启用的供应商配置')
base = cfg['base_url'].rstrip('/')
url = base if base.endswith('/messages') else base + '/messages'
key = cfg['api_key']
SYSTEM = [
    {"type": "text", "text": "你是一个友好的聊天伙伴。"},
    {"type": "text", "text": "【要求】你的每条回复里都必须出现词语「西瓜」。"},
]


def one(_):
    body = {"model": cfg.get('model'), "max_tokens": 60, "system": SYSTEM,
            "messages": [{"role": "user", "content": "你好，随便说一句话。"}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        'Authorization': 'Bearer ' + key, 'x-api-key': key,
        'content-type': 'application/json', 'anthropic-version': '2023-06-01'})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=90))
    except urllib.error.HTTPError as e:
        return ('error', f'HTTP {e.code} ' + e.read()[:200].decode('utf-8', 'replace').replace(key, '***'))
    except Exception as e:
        return ('error', str(e)[:200])
    text = ''.join(b.get('text', '') for b in d.get('content', []) if b.get('type') == 'text')
    if '西瓜' in text:
        return ('ok', text)
    coder = any(w in text for w in ('写代码', '编程', '开发', 'Claude Code'))
    return ('coder' if coder else 'miss', text)


with cf.ThreadPoolExecutor(5) as ex:
    results = list(ex.map(one, range(N)))
count = {k: sum(1 for r in results if r[0] == k) for k in ('ok', 'miss', 'coder', 'error')}
print(f"模型：{cfg.get('model')}  共 {N} 次")
print(f"  system 生效：{count['ok']}")
print(f"  没照做：{count['miss']}（其中自称编程 / 开发助手：{count['coder']}）")
print(f"  出错：{count['error']}")
for kind, text in results:
    if kind != 'ok':
        print(f"  [{kind}] {text.replace(chr(10), ' ')[:80]}")
if count['ok'] == N:
    print('结论：正常，system 提示词都送到了。')
elif count['ok'] + count['error'] == N:
    print('结论：system 正常，但有请求出错，看上面的错误信息。')
else:
    print('结论：有请求没拿到 system 提示词，中转站可能把请求转到了会替换提示词的通道。过一会儿再测；一直这样的话，联系中转站或换一个分组 / 渠道。')
