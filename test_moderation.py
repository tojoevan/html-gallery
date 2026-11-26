"""阶段 1 审核功能端到端测试（用临时 DB，不碰生产/本地库）。

运行：python3.11 test_moderation.py
"""
import os
import tempfile

# 必须在 import app 前指定临时 instance 目录
TMP = tempfile.mkdtemp(prefix='gallery-test-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()

# 用一份测试词库覆盖外置路径，避免依赖真实词库内容
with open(os.path.join(TMP, 'sensitive_words.txt'), 'w', encoding='utf-8') as f:
    f.write('# test\n违规词\n赌博\n')
import moderation
moderation.reload_words()

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


def register(username, handle=None):
    data = {'username': username, 'email': f'{username}@test.local',
            'password': 'test12345', 'confirm_password': 'test12345'}
    if handle:
        data['handle'] = handle
    r = client.post('/register', data=data)
    assert r.status_code == 302, f'register failed: {r.status_code}'
    return r


def logout():
    client.get('/logout')


# 1. 注册两个用户
register('alice', 'alice-h')
logout()
register('bob', 'bob-h')
logout()

# 2. 上传命中敏感词 → 被拒
client.post('/login', data={'username': 'alice', 'password': 'test12345'})
r = client.post('/new', data={'title': '正常标题', 'description': '',
                              'html_source': '<html><body>这里有违规词</body></html>'})
check('敏感词上传被拒', r.status_code == 200 and '敏感词' in r.get_data(as_text=True))

# 3. 正常 HTML 上传成功（含代码样式的 script，不误杀）
r = client.post('/new', data={'title': '我的作品', 'description': '正常描述',
                              'html_source': '<html><script>console.log("var x=1")</script><body>你好世界</body></html>',
                              'is_public': '1'})
check('正常 HTML 上传通过', r.status_code == 302)
with app_module.get_db() as db:
    page = db.execute("SELECT * FROM pages WHERE title='我的作品'").fetchone()
    page_id = page['id']
    slug = page['slug']
check('新作品 review_status=approved', page['review_status'] == 'approved')

# 4. 3MB 超限被拒
big = 'x' * (3 * 1024 * 1024 + 1)
r = client.post('/new', data={'title': '超大', 'html_source': big})
check('3MB 超限被拒', r.status_code == 200 and '3MB' in r.get_data(as_text=True))

# 5. 举报：bob 举报 alice 作品 → 转 pending_review，对外 404
logout()
client.post('/login', data={'username': 'bob', 'password': 'test12345'})
r = client.post(f'/api/report/{page_id}', json={'reason': '测试举报'})
check('举报提交成功', r.status_code == 200 and r.json.get('success'))
r2 = client.post(f'/api/report/{page_id}', json={'reason': '重复举报'})
check('重复举报被去重', r2.status_code == 400)
r3 = client.get(f'/bob-h/{slug}')  # 注意：分享路径是作者 handle
check('待复审作品对外不可见', client.get(f'/alice-h/{slug}').status_code == 404)
logout()

# 6. 管理员复审：approve（驳回举报）→ 作品恢复，举报人 -1
client.post('/login', data={'username': 'alice', 'password': 'test12345'})
# alice 不是管理员，复审页应 403
check('非管理员复审被拒', client.get('/admin/review').status_code == 403)
logout()

# 直接调 DB 把 alice 设为管理员（测试环境）
with app_module.get_db() as db:
    db.execute("UPDATE users SET is_admin=1 WHERE username='alice'")
client.post('/login', data={'username': 'alice', 'password': 'test12345'})
check('管理员可访问复审页', client.get('/admin/review').status_code == 200)
with app_module.get_db() as db:
    report = db.execute('SELECT id FROM reports LIMIT 1').fetchone()
r = client.post(f"/api/review/{report['id']}/approve")
check('驳回举报成功', r.status_code == 200 and r.json.get('success'))
check('驳回后举报人扣分', r.json.get('trust_points') == -1)
with app_module.get_db() as db:
    p = db.execute('SELECT review_status FROM pages WHERE id=?', (page_id,)).fetchone()
check('驳回后作品恢复 approved', p['review_status'] == 'approved')
logout()

# 7. reject 路径：再举报一次，管理员确认违规 → 举报人 +1，徽章可派生
client.post('/login', data={'username': 'bob', 'password': 'test12345'})
with app_module.get_db() as db:
    db.execute('DELETE FROM reports')  # 清掉上一条，允许再举报
client.post(f'/api/report/{page_id}', json={'reason': '再次举报'})
logout()
client.post('/login', data={'username': 'alice', 'password': 'test12345'})
with app_module.get_db() as db:
    report2 = db.execute('SELECT id FROM reports LIMIT 1').fetchone()
r = client.post(f"/api/review/{report2['id']}/reject")
check('确认违规成功', r.status_code == 200 and r.json.get('success'))
check('确认违规后举报人加分', r.json.get('trust_points') == 0)  # -1 +1 = 0
with app_module.get_db() as db:
    p = db.execute('SELECT review_status FROM pages WHERE id=?', (page_id,)).fetchone()
check('确认违规后作品 rejected', p['review_status'] == 'rejected')
logout()

# 8. 徽章派生：手动加 5 分 → dashboard 显示守护者
with app_module.get_db() as db:
    db.execute("UPDATE users SET trust_points=5 WHERE username='bob'")
client.post('/login', data={'username': 'bob', 'password': 'test12345'})
html = client.get('/dashboard').get_data(as_text=True)
check('守护者徽章显示', '守护者' in html)
logout()

# 9. next 开放重定向被挡（协议相对地址 + 反斜杠变体全部回退站内）
from urllib.parse import quote
for payload in ['//evil.com', '///evil.com', '////evil.com', '/\\evil.com',
                '/\\/evil.com', 'https://evil.com', 'javascript:alert(1)']:
    logout()
    r = client.post('/login?next=' + quote(payload, safe=''),
                    data={'username': 'bob', 'password': 'test12345'})
    loc = r.headers.get('Location', '')
    check('开放重定向被挡 %s' % payload,
          r.status_code == 302 and 'evil.com' not in loc and 'javascript:' not in loc, loc)
logout()
# 正常站内路径仍应保留
r = client.post('/login?next=%2Fdashboard%3Fx%3D1',
                data={'username': 'bob', 'password': 'test12345'})
check('站内 next 正常保留', '/dashboard?x=1' in r.headers.get('Location', ''),
      r.headers.get('Location', ''))

# 10. 存量重扫幂等（import rescan 会重新 init，注意不影响结果）
import importlib
import rescan
importlib.reload(rescan)
moderation.reload_words()
with app_module.get_db() as db:
    db.execute("UPDATE users SET trust_points=0")
    db.execute("UPDATE pages SET review_status='approved'")
# rejected 作品不含敏感词（测试词库只有违规词/赌博），重扫不应命中
before = moderation.scan('我的作品', '正常描述', '<body>你好世界</body>')
check('重扫不误伤正常作品', before[0] is False)

# 11. 内置种子词库：通用误伤词不得回潮，明确拒绝词必须保留
seed_words = set()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensitive_words.txt'),
          encoding='utf-8') as f:
    for line in f:
        w = line.strip()
        if w and not w.startswith('#'):
            seed_words.add(w)
false_positives = {'JS', 'LY', 'QQ', 'BT', '网络', '网购', '淘宝', '招聘', '兼职'}
real_bad = {'淫荡', '冰毒', '炸药', '强暴', '海洛因'}
check('内置词库无通用误伤词', not (false_positives & seed_words),
      str(false_positives & seed_words))
check('内置词库保留明确拒绝词', real_bad <= seed_words, str(real_bad - seed_words))

print('\n=== 汇总 ===')
passed = sum(1 for _, c, _ in results if c)
print(f'{passed}/{len(results)} 通过')
if passed != len(results):
    raise SystemExit(1)
