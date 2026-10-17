#!/usr/bin/env python3
"""检查各个中转站有没有把我们的 system 提示词送到模型，顺便看速度和出错情况。

背景（2026-10-18）：测试时发现，cam 中转站有几分钟把请求转到了另一条通道——那段时间模型完全看不到我们的
system 提示词，还自称「一个 AI 开发环境，可以帮你写代码」；几分钟后又恢复正常。网关发出去的内容是对的，
问题在中转站。陪伴时如果 ta 突然「不认识你」、口吻变成编程助手，可以跑一下这个脚本看看是不是又发生了。

做法：读 Dashboard 里配置的所有「聊天」供应商（用记忆服务的密钥解密 API Key，不打印），
直接（不经过网关）给每个发 N 条很短的请求：system 分两块，第二块写着「每条回复都要出现『西瓜』」，
统计有几条照做了。会花很少的 token。

用法（要用记忆服务的 Python，里面有解密用的 cryptography）：
  services/memory-service/venv/bin/python scripts/check_relay_system.py            # 每个中转测 6 次
  services/memory-service/venv/bin/python scripts/check_relay_system.py 10 cam     # 只测 cam，10 次
"""
import concurrent.futures as cf
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'sqlite' / 'memory.db'
ENV = ROOT / 'services' / 'memory-service' / '.env'

args = sys.argv[1:]
N = int(args[0]) if args and args[0].isdigit() else 6
ONLY = [a for a in args if not a.isdigit()]

SYSTEM_TEXTS = ["你是一个友好的聊天伙伴。", "【要求】你的每条回复里都必须出现词语「西瓜」。"]
USER_TEXT = "你好，随便说一句话。"
# 有的中转站在 Cloudflare 后面，会拦掉 Python 默认的 User-Agent（报 403 error code: 1010），所以自己带一个
UA = 'Mozilla/5.0 (compatible; memory-gateway-relay-check)'


def load_fernet():
    secret = None
    if ENV.exists():
        for line in ENV.read_text(encoding='utf-8').splitlines():
            if line.startswith('CONFIG_SECRET_KEY='):
                secret = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        sys.exit('缺少 cryptography：请用 services/memory-service/venv/bin/python 运行本脚本')
    return Fernet(secret.encode())


def decrypt(fernet, token):
    if not token:
        return ''
    if not fernet:
        return token
    try:
        return fernet.decrypt(token.encode()).decode()
    except Exception:
        return ''


def load_providers():
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            "SELECT id, name, base_url, api_key_enc, models, api_format FROM providers WHERE role='chat' ORDER BY id").fetchall()
        active = conn.execute("SELECT provider_id FROM active_config WHERE id=1").fetchone() if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='active_config'").fetchone() else None
    finally:
        conn.close()
    return rows, (active[0] if active else None)


def one_request(p):
    fmt = p['format']
    base = p['base_url'].rstrip('/')
    key = p['key']
    if fmt == 'anthropic':
        url = base if base.endswith('/messages') else base + '/messages'
        body = {"model": p['model'], "max_tokens": 60,
                "system": [{"type": "text", "text": t} for t in SYSTEM_TEXTS],
                "messages": [{"role": "user", "content": USER_TEXT}]}
        headers = {'Authorization': 'Bearer ' + key, 'x-api-key': key, 'User-Agent': UA,
                   'content-type': 'application/json', 'anthropic-version': '2023-06-01'}
    else:
        url = base if base.endswith('/chat/completions') else base + '/chat/completions'
        body = {"model": p['model'], "max_tokens": 60,
                "messages": [{"role": "system", "content": "\n\n".join(SYSTEM_TEXTS)}, {"role": "user", "content": USER_TEXT}]}
        headers = {'Authorization': 'Bearer ' + key, 'content-type': 'application/json', 'User-Agent': UA}
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        d = json.load(urllib.request.urlopen(req, timeout=90))
    except urllib.error.HTTPError as e:
        return ('error', f'HTTP {e.code} ' + e.read()[:160].decode('utf-8', 'replace').replace(key, '***'), time.time() - t0)
    except Exception as e:
        return ('error', str(e)[:160], time.time() - t0)
    dt = time.time() - t0
    if fmt == 'anthropic':
        text = ''.join(b.get('text', '') for b in d.get('content', []) if b.get('type') == 'text')
    else:
        text = ((d.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    if '西瓜' in text:
        return ('ok', text, dt)
    coder = any(w in text for w in ('写代码', '编程', '开发', 'Claude Code'))
    return ('coder' if coder else 'miss', text, dt)


def main():
    fernet = load_fernet()
    rows, active_id = load_providers()
    targets = []
    for pid, name, base_url, key_enc, models, fmt in rows:
        if ONLY and name not in ONLY:
            continue
        try:
            model_list = json.loads(models or '[]')
        except Exception:
            model_list = []
        key = decrypt(fernet, key_enc)
        targets.append({'id': pid, 'name': name, 'base_url': base_url, 'key': key,
                        'model': model_list[0] if model_list else '', 'format': fmt or 'openai',
                        'active': pid == active_id})
    if not targets:
        sys.exit('没有找到聊天供应商' + (f'（名字：{", ".join(ONLY)}）' if ONLY else ''))
    for p in targets:
        host = p['base_url'].split('//')[-1].split('/')[0]
        title = f"{p['name']}（{host} · {p['model']} · {p['format']} 格式）" + ('  ← 当前启用' if p['active'] else '')
        print(title)
        if not p['key']:
            print('  跳过：API Key 解密失败或为空\n')
            continue
        with cf.ThreadPoolExecutor(3) as ex:
            results = list(ex.map(lambda _: one_request(p), range(N)))
        cnt = {k: sum(1 for r in results if r[0] == k) for k in ('ok', 'miss', 'coder', 'error')}
        good = [r[2] for r in results if r[0] != 'error']
        avg = f'{sum(good) / len(good):.1f} 秒' if good else '—'
        print(f"  system 生效 {cnt['ok']}/{N} | 没照做 {cnt['miss'] + cnt['coder']}（自称编程助手 {cnt['coder']}）| 出错 {cnt['error']} | 平均耗时 {avg}")
        shown = 0
        for kind, text, _ in results:
            if kind != 'ok' and shown < 2:
                print(f"    [{kind}] {text.replace(chr(10), ' ')[:90]}")
                shown += 1
        print()


if __name__ == '__main__':
    main()
