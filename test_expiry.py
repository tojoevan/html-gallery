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
# tojoevan 已是保留用户名，不能走注册接口；直接插库模拟「迁移时已存在」的管理员。
c = sqlite3.connect(DB)
from werkzeug.security import generate_password_hash as _gph
c.execute('INSERT INTO users (username,email,password,created,handle,is_admin,is_guest) '
          'VALUES (?,?,?,?,?,?,0)',
          ('tojoevan', 'admin@test.local', _gph('test12345'),
           '2026-01-01T00:00:00', 'tojoevan', 1))
c.commit(); c.close()
# 让该客户端以管理员身份登录（走真实登录接口，而非抄近路改 session）
admin_client.post('/login', data={'username': 'tojoevan', 'password': 'test12345'})
admin_client.post('/new', data={'title': 'admin-page', 'html_source': '<body>x</body>', 'is_public': '1'})
adminp = page_by_title('admin-page')
check('管理员作品永久', adminp and adminp['share_expires_at'] is None)

# ── 5. 过期访问：410 且数据仍在库 ────────────────────────────────────────────
set_expired(ap['id'])
anon = app_module.app.test_client()  # 全新客户端 = 无 session 访客
share_url = f"/alice-h/{ap['slug']}"
r = anon.get(share_url)
check('过期作品分享返回 410', r.status_code == 410, f'got {r.status_code}')
gone_html = r.get_data(as_text=True)
check('410 页面有友好提示', '失效' in gone_html)
check('410 页面给作者留找回入口', '/dashboard' in gone_html and '我的看板' in gone_html)
check('410 页面说明会转入管理员回收站', '回收站' in gone_html and '续期' in gone_html)
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

# ── 7. 可见性三态：公开 / 仅链接 / 私密 ──────────────────────────────────────
def create_vis(title, visibility):
    r = client.post('/new', data={'title': title, 'html_source': '<body>v</body>',
                                  'visibility': visibility})
    assert r.status_code == 302, f'create failed: {r.status_code}'


for vis in ('public', 'unlisted', 'private'):
    create_vis('vis-' + vis, vis)

pub, unl, pri = (page_by_title('vis-' + v) for v in ('public', 'unlisted', 'private'))
check('public 写入 is_public=1', pub['visibility'] == 'public' and pub['is_public'] == 1)
check('unlisted 不入画廊', unl['visibility'] == 'unlisted' and unl['is_public'] == 0)
check('private 标记正确', pri['visibility'] == 'private' and pri['is_public'] == 0)

visitor = app_module.app.test_client()
home_all = visitor.get('/').get_data(as_text=True)
check('首页只有公开作品', 'vis-public' in home_all
      and 'vis-unlisted' not in home_all and 'vis-private' not in home_all)
check('仅链接作品持链接可看', visitor.get('/p/' + unl['slug']).status_code == 200)
check('私密作品他人不可见', visitor.get('/p/' + pri['slug']).status_code == 403)
check('作者仍可看自己私密作品', client.get('/p/' + pri['slug']).status_code == 200)

edit_html = client.get(f"/edit/{unl['id']}").get_data(as_text=True)
check('编辑页回填仅链接', 'value="unlisted" checked' in edit_html)
preview_unl = client.get('/p/' + unl['slug']).get_data(as_text=True)
check('预览页显示仅链接标识', 'nav-unlisted' in preview_unl)

# ── 8. 到期前提醒 + 一键续期 ────────────────────────────────────────────────
def set_expiry(page_id, delta):
    c = sqlite3.connect(DB)
    c.execute('UPDATE pages SET share_expires_at=? WHERE id=?',
              ((datetime.now() + delta).isoformat(), page_id))
    c.commit(); c.close()


set_expiry(grp['id'], timedelta(hours=5))
dash_soon = guest_client.get('/dashboard').get_data(as_text=True)
check('看板出现到期提醒', '个作品将在 24 小时内到期' in dash_soon)
check('看板提供一键续期', '一键续期全部' in dash_soon)
check('看板小时数向上取整', '剩余 5 小时' in dash_soon)

owner_preview = guest_client.get('/p/' + page_by_title('guest-renew')['slug']).get_data(as_text=True)
check('作者预览页有到期提醒条', '<div class="expiry-bar">' in owner_preview and '5 小时' in owner_preview)
visitor_preview = app_module.app.test_client().get('/p/' + page_by_title('guest-renew')['slug']).get_data(as_text=True)
check('访客看不到到期提醒条', '<div class="expiry-bar">' not in visitor_preview)

guest_client.post('/renew_all')
after = page_by_title('guest-renew')
left = datetime.fromisoformat(after['share_expires_at']) - datetime.now()
# 续期从「较晚者」起算：原到期在 5 小时后，续期后应为 3 天 + 5 小时
check('一键续期延长 3 天', 3.1 < left.days + left.seconds / 86400 < 3.3, f'{left}')
dash_after = guest_client.get('/dashboard').get_data(as_text=True)
check('续期后提醒消失', '个作品将在 24 小时内到期' not in dash_after)

# 提前续期不应缩短有效期（从较晚者起算）
set_expiry(grp['id'], timedelta(hours=30))
guest_client.post(f"/renew/{grp['id']}")
kept = datetime.fromisoformat(page_by_title('guest-renew')['share_expires_at']) - datetime.now()
check('提前续期不缩短有效期', kept.days + kept.seconds / 86400 > 3.5, f'{kept}')

# ── 9. 可见性对 API 同样生效（点赞 / 评论 / 举报）────────────────────────────
bob = app_module.app.test_client()
bob.post('/register', data={'username': 'bob', 'email': 'bob@test.local',
                            'password': 'test12345', 'confirm_password': 'test12345',
                            'handle': 'bob-h'})
check('私密作品评论列表他人 404', bob.get(f"/api/comments/{pri['id']}").status_code == 404)
check('私密作品点赞他人 404', bob.post(f"/api/like/{pri['id']}").status_code == 404)
check('仅链接作品评论列表可读', bob.get(f"/api/comments/{unl['id']}").status_code == 200)
report_pri = bob.post(f"/api/report/{pri['id']}", json={'reason': 'x'})
check('私密作品不可被举报', report_pri.status_code == 404, f'{report_pri.status_code}')
set_expired(pub['id'])
check('过期作品点赞 404', bob.post(f"/api/like/{pub['id']}").status_code == 404)

# ── 10. 纯净分享页同样有到期提醒与 noindex ──────────────────────────────────
set_expiry(grp['id'], timedelta(hours=5))
share_owner = guest_client.get(f"/view/{page_by_title('guest-renew')['slug']}").get_data(as_text=True)
check('纯净分享页作者有到期条', '<div class="expiry-bar">' in share_owner)
share_guest = app_module.app.test_client().get(f"/view/{page_by_title('guest-renew')['slug']}").get_data(as_text=True)
check('纯净分享页访客无到期条', '<div class="expiry-bar">' not in share_guest)
check('仅链接作品 noindex', 'name="robots" content="noindex' in
      app_module.app.test_client().get('/p/' + unl['slug']).get_data(as_text=True))
check('公开作品不加 noindex', 'name="robots"' not in
      app_module.app.test_client().get('/p/' + page_by_title('guest-renew')['slug']).get_data(as_text=True))

# ── 11. 跨用户同名作品：裸链接不再串页 ─────────────────────────────────────
one_owner = app_module.app.test_client()
one_owner.post('/new', data={'title': '同名作品', 'html_source': '<b>ONE</b>', 'visibility': 'public'})
two_owner = app_module.app.test_client()
two_owner.post('/new', data={'title': '同名作品', 'html_source': '<b>TWO</b>', 'visibility': 'public'})

_c = sqlite3.connect(DB)
_c.row_factory = sqlite3.Row
_same = _c.execute('''SELECT p.id, p.slug, u.handle FROM pages p JOIN users u ON u.id=p.user_id
                      WHERE p.title='同名作品' ORDER BY p.id''').fetchall()
check('同名标题各自生成不同短码', len(_same) == 2 and _same[0]['slug'] != _same[1]['slug'],
      f"{[r['slug'] for r in _same]}")
h_one, h_two = _same[0]['handle'], _same[1]['handle']
s_one = _same[0]['slug']
# 人为制造同 slug（模拟历史 ASCII 数据），验证消歧逻辑仍有效
_c.execute('UPDATE pages SET slug=? WHERE id=?', (s_one, _same[1]['id']))
_c.commit()
_c.close()

stranger = app_module.app.test_client()
amb = stranger.get('/p/' + s_one)
check('裸链接命中歧义时明确提示', amb.status_code == 404
      and '链接有歧义' in amb.get_data(as_text=True), f'{amb.status_code}')

p_one = stranger.get('/p/' + s_one + '?u=' + h_one).get_data(as_text=True)
p_two = stranger.get('/p/' + s_one + '?u=' + h_two).get_data(as_text=True)
check('带作者标识打开第一份', '&lt;b&gt;ONE&lt;/b&gt;' in p_one and '&lt;b&gt;TWO&lt;/b&gt;' not in p_one)
check('带作者标识打开第二份', '&lt;b&gt;TWO&lt;/b&gt;' in p_two and '&lt;b&gt;ONE&lt;/b&gt;' not in p_two)
check('作者裸链接拿到的是自己的作品',
      '&lt;b&gt;ONE&lt;/b&gt;' in one_owner.get('/p/' + s_one).get_data(as_text=True))
check('看板链接使用短码，不含 ?u=',
      '/p/' + s_one in one_owner.get('/dashboard').get_data(as_text=True)
      and 'u=' + h_one not in one_owner.get('/dashboard').get_data(as_text=True))
check('不存在的作者标识 404', stranger.get('/p/' + s_one + '?u=nobody').status_code == 404)
check('完整分享路径仍可访问', stranger.get(f'/{h_one}/{s_one}').status_code == 200)
check('纯净分享页同样支持消歧',
      '&lt;b&gt;TWO&lt;/b&gt;' in stranger.get('/view/' + s_one + '?u=' + h_two).get_data(as_text=True))

# ── 12. 游客续期额度：3 天试用不能被无限续 ──────────────────────────────────
renewer = app_module.app.test_client()
renewer.post('/new', data={'title': '额度测试', 'html_source': '<b>x</b>', 'visibility': 'public'})
rid = page_by_title('额度测试')['id']
for _ in range(app_module.RENEW_MAX_TIMES):
    renewer.post(f'/renew/{rid}')
_c = sqlite3.connect(DB)
_used = _c.execute('SELECT renew_count FROM pages WHERE id=?', (rid,)).fetchone()[0]
_c.close()
check('续期次数被记录', _used == app_module.RENEW_MAX_TIMES, f'{_used}')

denied = renewer.post(f'/renew/{rid}', follow_redirects=True)
check('超出额度给出注册引导', '续期次数已用完' in denied.get_data(as_text=True))
_c = sqlite3.connect(DB)
_used2 = _c.execute('SELECT renew_count FROM pages WHERE id=?', (rid,)).fetchone()[0]
_c.close()
check('被拒后不再增加计数', _used2 == app_module.RENEW_MAX_TIMES, f'{_used2}')
check('看板标记续期已用完', '续期已用完' in renewer.get('/dashboard').get_data(as_text=True))

# ── 13. 隐私说明与删除账号 ──────────────────────────────────────────────────
pv = app_module.app.test_client().get('/privacy')
pv_html = pv.get_data(as_text=True)
check('隐私与数据页可访问', pv.status_code == 200)
check('隐私页覆盖三种可见性', all(k in pv_html for k in ('公开', '仅链接', '私密')))
check('隐私页说明删除方式', '删除账号' in pv_html and '保留多久' in pv_html)

victim = app_module.app.test_client()
victim.post('/register', data={'username': 'victim', 'email': 'v@test.local',
                               'password': 'test12345', 'confirm_password': 'test12345',
                               'handle': 'victim-h'})
victim.post('/new', data={'title': '将被删除的作品', 'html_source': '<b>x</b>',
                          'visibility': 'public'})
wrong = victim.post('/delete_account', data={'password': 'nope', 'confirm': 'DELETE'},
                    follow_redirects=True)
check('密码错误时不删除账号',
      '密码错误' in wrong.get_data(as_text=True) and handle_of('victim') is not None)
no_confirm = victim.post('/delete_account', data={'password': 'test12345', 'confirm': 'yes'},
                         follow_redirects=True)
check('未键入 DELETE 不删除', '输入 DELETE' in no_confirm.get_data(as_text=True))
done = victim.post('/delete_account', data={'password': 'test12345', 'confirm': 'DELETE'},
                   follow_redirects=True)
done_html = done.get_data(as_text=True)
check('删除账号给出成功提示', '账号与全部作品已删除' in done_html)
check('账号已从库中移除', handle_of('victim') is None)
check('作品一并删除', page_by_title('将被删除的作品') is None)
check('删除后回到未登录状态', 'href="/register"' in done_html)

# ── 汇总 ─────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f'\n==== {passed}/{len(results)} 通过 ====')
if passed != len(results):
    raise SystemExit(1)
