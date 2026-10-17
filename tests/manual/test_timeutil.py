"""timeutil 的小测试：python3 tests/manual/test_timeutil.py（不需要第三方库）"""
import os
import sys
from pathlib import Path

os.environ['USER_TZ'] = 'Asia/Shanghai'
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services' / 'memory-service'))
import timeutil as t  # noqa: E402

CASES = [
    (t.to_utc_str('2026-10-17 16:56:50'), '2026-10-17 16:56:50', '存储格式原样'),
    (t.to_utc_str('2026-10-17T16:56:50Z'), '2026-10-17 16:56:50', 'ISO 带 Z'),
    (t.to_utc_str('2026-10-18T00:56:50+08:00'), '2026-10-17 16:56:50', '带时区偏移'),
    (t.to_utc_str('2026-10-16T10:14:47.337Z'), '2026-10-16 10:14:47', '毫秒 + Z（向量表的写法）'),
    (t.to_utc_str('2026-10-18 00:56:50', naive_tz=t.USER_TZ), '2026-10-17 16:56:50', '导入文件里的本地时间'),
    (t.to_utc_str('2026-10-17'), '2026-10-17 00:00:00', '纯日期'),
    (t.to_utc_str('乱写的'), None, '解析失败'),
    (t.local_str('2026-10-17 16:56:50'), '2026-10-18 00:56', 'UTC → 北京时间'),
    (t.local_date('2026-10-01 20:30:00'), '2026-10-02', '北京时间凌晨跨日'),
    (t.local_day_label('2026-10-17 16:56:50'), '10月18日 周日', '日标签'),
    (t.local_str(''), '', '空值'),
    (t.USER_TZ_LABEL, '北京时间', '时区名称'),
]
bad = [(name, got, want) for got, want, name in CASES if got != want]
for name, got, want in bad:
    print(f'✗ {name}: 得到 {got!r}，应为 {want!r}')
d = t.days_since(t.utc_now_str())
if d is None or abs(d) > 0.01:
    bad.append(('days_since', d, 0))
    print(f'✗ days_since: {d!r}')
print(f'timeutil：通过 {len(CASES) + 1 - len(bad)}，失败 {len(bad)}')
sys.exit(1 if bad else 0)
