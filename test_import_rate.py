"""链接导入频率限制测试（2026-11-27）。

覆盖：独立计数桶、每分钟/每小时阈值、IP 隔离、全局开关、
/api/import 超限返回 429 且抓取不被调用、identity_required 建立游客身份、
TESTING 模式跳过（保护其余测试套件）。
运行：python3 test_import_rate.py
"""
import os
import tempfile
import time

TMP = tempfile.mkdtemp(prefix='gallery-import-rate-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


# ── 1. 内部函数：独立桶、阈值、IP 隔离 ───────────────────────────────────────
app_module.app.config['TESTING'] = False
app_module._import_hits.clear()
app_module._publish_hits.clear()
app_module.IMPORT_MINUTE_MAX = 3
app_module.IMPORT_HOUR_MAX = 5

ip = '203.0.113.20'
for i in range(3):
    check('导入前 3 次放行', app_module._import_rate_ok(ip) is True)
check('第 4 次被限流（每分钟 3 次）', app_module._import_rate_ok(ip) is False)
check('导入桶与发布桶独立', app_module._publish_hits == {})
check('不同 IP 不受影响', app_module._import_rate_ok('203.0.113.21') is True)

app_module._import_hits[ip] = [time.time() - 61] * 5
check('一分钟窗外恢复但小时累计仍卡', app_module._import_rate_ok(ip) is False)
app_module._import_hits[ip] = [time.time() - 3601] * 5
check('一小时窗外彻底恢复', app_module._import_rate_ok(ip) is True)

# ── 2. 全局开关 ─────────────────────────────────────────────────────────────
app_module.IMPORT_RATE_ENABLED = False
app_module._import_hits.clear()
check('关闭开关后无限流', all(app_module._import_rate_ok(ip) for _ in range(10)))
app_module.IMPORT_RATE_ENABLED = True

# ── 3. 端到端：/api/import 超限 429，且抓取不被调用 ──────────────────────────
app_module.IMPORT_MINUTE_MAX = 2
app_module.IMPORT_HOUR_MAX = 10
app_module._import_hits.clear()

calls = {'n': 0}


def fake_import(url):
    calls['n'] += 1
    return {'html': '<body>ok</body>', 'title': '测试', 'source_type': 'webpage', 'variants': None}


orig = app_module.link_import.import_from_link
app_module.link_import.import_from_link = fake_import

r1 = client.post('/api/import', data={'url': 'https://example.com/a'})
r2 = client.post('/api/import', data={'url': 'https://example.com/b'})
r3 = client.post('/api/import', data={'url': 'https://example.com/c'})
check('前 2 次导入成功', r1.status_code == 200 and r2.status_code == 200,
      f'{r1.status_code}/{r2.status_code}')
check('第 3 次导入返回 429', r3.status_code == 429, str(r3.status_code))
check('429 带可读中文提示', '导入太频繁' in (r3.get_json() or {}).get('error', ''))
check('超限时未真正抓取（省 worker）', calls['n'] == 2, str(calls['n']))

# identity_required 建立游客身份：匿名调用后应拿到 session user_id
with client.session_transaction() as sess:
    check('匿名导入也建立游客身份', 'user_id' in sess)

app_module.link_import.import_from_link = orig

# ── 4. TESTING 跳过（保护其余测试套件）──────────────────────────────────────
app_module.app.config['TESTING'] = True
app_module._import_hits.clear()
check('TESTING 模式不计数',
      all(app_module._import_rate_ok(ip) for _ in range(100)) and not app_module._import_hits)

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
