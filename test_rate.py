"""上传频率限制测试（P2，2026-11-27）。

覆盖：每分钟/每小时阈值、不同 IP 互不干扰、TESTING 模式跳过（不影响其余测试）、
/new 与 /api/publish_batch 两条发布路径在超限时返回 429。
运行：python3 test_rate.py
"""
import os
import tempfile
import time

TMP = tempfile.mkdtemp(prefix='gallery-rate-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True   # 其余路由测试照常跳过限流
client = app_module.app.test_client()
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


# ── 1. 内部函数：阈值与 IP 隔离 ─────────────────────────────────────────────
app_module.app.config['TESTING'] = False
app_module._publish_hits.clear()
app_module.PUBLISH_MINUTE_MAX = 3
app_module.PUBLISH_HOUR_MAX = 5

ip = '203.0.113.10'
for i in range(3):
    check(f'前 3 次放行 #{i+1}', app_module._publish_rate_ok(ip) is True)
check('第 4 次被限流（每分钟 3 次）', app_module._publish_rate_ok(ip) is False)
check('不同 IP 不受影响', app_module._publish_rate_ok('203.0.113.11') is True)

# 时间窗滑过 60 秒后恢复，但仍在 1 小时内累计
app_module._publish_hits[ip] = [time.time() - 61] * 5
check('一分钟窗外恢复，但小时累计仍卡', app_module._publish_rate_ok(ip) is False)
app_module._publish_hits[ip] = [time.time() - 3601] * 5
check('一小时窗外彻底恢复', app_module._publish_rate_ok(ip) is True)

# ── 2. 全局开关 ─────────────────────────────────────────────────────────────
app_module.PUBLISH_RATE_ENABLED = False
app_module._publish_hits.clear()
check('关闭开关后无限流', all(app_module._publish_rate_ok(ip) for _ in range(10)))
app_module.PUBLISH_RATE_ENABLED = True

# ── 3. 端到端：/new 与 /api/publish_batch 超限返回 429 ───────────────────────
app_module.PUBLISH_MINUTE_MAX = 2
app_module.PUBLISH_HOUR_MAX = 10
app_module._publish_hits.clear()

# 先登录拿身份（游客也行），/new 走 identity_required 自动建游客
new_data = {'title': 'rate', 'html_source': '<body>x</body>', 'visibility': 'public'}
r1 = client.post('/new', data=new_data)
r2 = client.post('/new', data=new_data)
r3 = client.post('/new', data=new_data)
check('前 2 次发布成功', r1.status_code == 302 and r2.status_code == 302,
      f'{r1.status_code}/{r2.status_code}')
check('第 3 次发布返回 429', r3.status_code == 429, str(r3.status_code))
check('429 页面给出可读提示', '发布太频繁' in r3.get_data(as_text=True))

# 另一路径
r4 = client.post('/api/publish_batch', json={'items': [
    {'title': 'b', 'html_source': '<body>b</body>', 'visibility': 'public'}]})
check('publish_batch 同样被限流', r4.status_code == 429, str(r4.status_code))

# ── 4. TESTING 跳过（保护其余测试套件）──────────────────────────────────────
app_module.app.config['TESTING'] = True
app_module._publish_hits.clear()
check('TESTING 模式不计数',
      all(app_module._publish_rate_ok(ip) for _ in range(100)) and not app_module._publish_hits)

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
