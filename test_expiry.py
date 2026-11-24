"""阶段 2 限时功能端到端测试（临时 DB，不碰生产/本地库）。

运行：python3.11 test_expiry.py
"""
import os
import tempfile
from datetime import datetime, timedelta
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-expiry-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()
DB = os.path.join(TMP, 'gallery.db')

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


def create(title, public=True):
    data = {'title': title, 'html_source': '<body>hello</body>', 'description': ''}
    if public:
        data['is_public'] = '1'
    r = client.post('/new', data=data)
    assert r.status_code == 302, f'create failed: {r.status_code}'


def page_by_title(title):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute('SELECT * FROM pages WHERE title=?', (title,)).fetchone()
    c.close()
    return dict(row) if row else None


def handle_of(username):
    c = sqlite3.connect(DB)
    row = c.execute('SELECT handle, id, is_guest, is_admin FROM users WHERE username=?', (username,)).fetchone()
    c.close()
    return row


def set_expired(page_id):
    c = sqlite3.connect(DB)
    c.execute('UPDATE pages SET share_expires_at=? WHERE id=?',
              ((datetime.now() - timedelta(days=1)).isoformat(), page_id))
    c.commit()
    c.close()


# ── 1. 游客匿名创建：3 天到期 ────────────────────────────────────────────────
client.get('/logout')
create('guest-page')
gp = page_by_title('guest-page')
check('匿名作品写入到期时间', bool(gp and gp['share_expires_at']))
if gp and gp['share_expires_at']:
    delta = datetime.fromisoformat(gp['share_expires_at']) - datetime.now()
    check('匿名到期约 3 天', 2.9 < delta.days + delta.seconds / 86400 < 3.1, f'{delta}')
guest_info = handle_of([r[0] for r in sqlite3.connect(DB).execute(
    'SELECT username FROM users WHERE is_guest=1')][0])
check('游客账号被标记 is_guest', bool(guest_info and guest_info[2] == 1))

# 游客 dashboard 显示剩余天数
dash = client.get('/dashboard').get_data(as_text=True)
check('dashboard 显示游客提示', '临时身份' in dash)
check('dashboard 显示剩余天数', '剩余' in dash and '天' in dash)

# ── 2. 游客注册原地升级：user_id 不变、作品转永久 ────────────────────────────
old_uid = gp['user_id']
old_handle = guest_info[0]
r = client.post('/register', data={
    'username': 'upgraded', 'email': 'upgraded@test.local',
    'password': 'test12345', 'confirm_password': 'test12345', 'handle': 'upgraded'
})
check('游客注册成功', r.status_code == 302)
up = handle_of('upgraded')
check('原地升级 user_id 不变', bool(up and up[1] == old_uid), f'{old_uid} -> {up[1] if up else None}')
check('升级后 is_guest=0', bool(up and up[2] == 0))
gp2 = page_by_title('guest-page')
check('升级后作品归属不变', gp2['user_id'] == old_uid)
check('升级后作品转永久', gp2['share_expires_at'] is None)

# ── 3. 注册用户创建：永久 ────────────────────────────────────────────────────
client.get('/logout')
register('alice', 'alice-h')
create('alice-page')
ap = page_by_title('alice-page')
check('注册作品无到期时间（永久）', ap and ap['share_expires_at'] is None)

# ── 4. 管理员豁免 ────────────────────────────────────────────────────────────
# 注意：migrate_db 的 tojoevan 置位只在启动跑，临时库此时用户尚未创建，
# 因此手动置位以模拟线上「迁移时已存在」的真实状态。
admin_client = app_module.app.test_client()
admin_client.post('/register', data={
    'username': 'tojoevan', 'email': 'admin@test.local',
    'password': 'test12345', 'confirm_password': 'test12345', 'handle': 'tojoevan'
})
c = sqlite3.connect(DB)
c.execute("UPDATE users SET is_admin=1 WHERE username='tojoevan'")
c.commit(); c.close()
admin_client.post('/new', data={'title': 'admin-page', 'html_source': '<body>x</body>', 'is_public': '1'})
adminp = page_by_title('admin-page')
check('管理员作品永久', adminp and adminp['share_expires_at'] is None)

# ── 5. 过期访问：410 且数据仍在库 ────────────────────────────────────────────
set_expired(ap['id'])
anon = app_module.app.test_client()  # 全新客户端 = 无 session 访客
share_url = f"/alice-h/{ap['slug']}"
r = anon.get(share_url)
check('过期作品分享返回 410', r.status_code == 410, f'got {r.status_code}')
check('410 页面有友好提示', '失效' in r.get_data(as_text=True))
check('过期后数据仍在库', page_by_title('alice-page') is not None)

# 首页过滤
home = anon.get('/').get_data(as_text=True)
check('首页不显示过期作品', 'alice-page' not in home)

# 作者仍可访问（重新登录 alice，避免沿用前面的 session）
alice_client = app_module.app.test_client()
alice_client.post('/login', data={'username': 'alice', 'password': 'test12345'})
r = alice_client.get(share_url)
check('作者可访问自己过期作品', r.status_code == 200)

# 管理员仍可访问他人过期作品
r = admin_client.get(share_url)
check('管理员可访问他人过期作品', r.status_code == 200)

# ── 6. 续期：游客作品（注册作品本身永久，无需续期）───────────────────────────
guest_client = app_module.app.test_client()
guest_client.post('/new', data={'title': 'guest-renew', 'html_source': '<body>y</body>', 'is_public': '1'})
grp = page_by_title('guest-renew')
set_expired(grp['id'])
guest_client.post(f"/renew/{grp['id']}")
renewed = page_by_title('guest-renew')
check('游客续期后到期时间更新', renewed['share_expires_at'] and
      datetime.fromisoformat(renewed['share_expires_at']) > datetime.now())
# 注册作品请求续期应无副作用：不写入新的到期时间（真实场景该字段本就是 NULL）
before = page_by_title('alice-page')['share_expires_at']
alice_client.post(f"/renew/{ap['id']}")
check('注册作品续期无副作用', page_by_title('alice-page')['share_expires_at'] == before)

# ── 汇总 ─────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f'\n==== {passed}/{len(results)} 通过 ====')
if passed != len(results):
    raise SystemExit(1)
