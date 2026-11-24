"""阶段 3 增长动作端到端测试（临时 DB，不碰生产/本地库）。

运行：python3.11 test_growth.py
"""
import os
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-growth-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
DB = os.path.join(TMP, 'gallery.db')
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


def signup(client, username, handle=None):
    data = {'username': username, 'email': f'{username}@test.local',
            'password': 'test12345', 'confirm_password': 'test12345'}
    if handle:
        data['handle'] = handle
    r = client.post('/register', data=data)
    assert r.status_code == 302, f'register failed: {r.status_code}'


def create(client, title):
    return client.post('/new', data={
        'title': title, 'html_source': '<body>hello</body>',
        'description': '', 'is_public': '1'
    })


def page_row(title):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute('SELECT * FROM pages WHERE title=?', (title,)).fetchone()
    c.close()
    return dict(row) if row else None


# ── 动作2：游客创建成功后注册引导 ───────────────────────────────────────────
guest = app_module.app.test_client()
r = create(guest, 'trial-page')
check('游客创建跳 dashboard?trial=1', r.status_code == 302 and 'trial=1' in r.headers['Location'],
      r.headers.get('Location', ''))
dash = guest.get('/dashboard?trial=1').get_data(as_text=True)
check('强引导文案出现', '作品已发布' in dash and '永久保存' in dash)
check('注册链接带 from=trial', '/register?from=trial' in dash)

# 注册用户创建不触发引导
user = app_module.app.test_client()
signup(user, 'alice', 'alice-h')
r = create(user, 'normal-page')
check('注册用户创建直接回看板', r.status_code == 302 and 'trial' not in r.headers['Location'],
      r.headers.get('Location', ''))
dash = user.get('/dashboard').get_data(as_text=True)
check('注册用户看板无游客引导', '作品已发布' not in dash and '临时身份' not in dash)

# ── 动作1：/view 裂变入口 ───────────────────────────────────────────────────
p = page_row('normal-page')
view_url = f"/alice-h/{p['slug']}"

# 访客（无 session）可见
anon = app_module.app.test_client()
html = anon.get(view_url).get_data(as_text=True)
check('访客看到裂变条', '我也要做' in html and '我也要做' in html)
check('裂变条链接 /new?from=view', '/new?from=view' in html)

# 作者本人不可见
html = user.get(view_url).get_data(as_text=True)
check('作者本人看不到裂变条', '我也要做' not in html)

# 管理员（非作者）可见（默认开关开）
admin = app_module.app.test_client()
signup(admin, 'adminx', 'adminx')
c = sqlite3.connect(DB)
c.execute("UPDATE users SET is_admin=1 WHERE username='adminx'")
c.commit(); c.close()
html = admin.get(view_url).get_data(as_text=True)
check('管理员作为访客可见裂变条', '我也要做' in html)

# 关闭开关后访客不可见
user.post('/settings', data={'action': 'toggle_fission'})  # 无 show_fission 字段 → 关闭
c = sqlite3.connect(DB)
flag = c.execute("SELECT show_fission FROM users WHERE username='alice'").fetchone()[0]
c.close()
check('关闭开关 show_fission=0', flag == 0)
html = anon.get(view_url).get_data(as_text=True)
check('关闭后访客看不到裂变条', '我也要做' not in html)

# 重新开启
user.post('/settings', data={'action': 'toggle_fission', 'show_fission': '1'})
html = anon.get(view_url).get_data(as_text=True)
check('重新开启后访客可见', '我也要做' in html)

# /new?from=view 可达（无需登录也能进编辑器，自动建游客）
r = anon.get('/new?from=view')
check('/new?from=view 可达', r.status_code == 200)

# ── 汇总 ─────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f'\n==== {passed}/{len(results)} 通过 ====')
if passed != len(results):
    raise SystemExit(1)
