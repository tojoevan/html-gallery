"""游客找回码测试（临时 DB，不碰生产/本地库）。

运行：python3.11 test_recovery.py
"""
import os
import re
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-recovery-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
DB = os.path.join(TMP, 'gallery.db')
client = app_module.app.test_client()

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


def new_client():
    """全新 test_client = 新浏览器（无 session cookie）。"""
    return app_module.app.test_client()


def guest_publish(title):
    """当前 client 以游客身份发一个作品，返回作品 slug。"""
    r = client.post('/new', data={'title': title, 'html_source': '<body>hi</body>',
                                  'description': '', 'visibility': 'public'})
    assert r.status_code == 302, f'publish failed {r.status_code}'
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT slug FROM pages WHERE title=?', (title,)).fetchone()['slug']


def user_row(username_like=None, uid=None):
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        if uid:
            return db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return db.execute("SELECT * FROM users WHERE username LIKE ?", (username_like,)).fetchone()


# ── 1. 游客生成找回码 ────────────────────────────────────────────────────────
slug = guest_publish('recovery-page')
guest = user_row('guest-%')
check('游客身份已建立', guest is not None and guest['is_guest'] == 1)
check('生成前没有找回码', guest['recovery_code'] is None)

r = client.post('/recovery-code')
check('生成找回码返回 200', r.status_code == 200, f'got {r.status_code}')
html = r.get_data(as_text=True)
m = re.search(r'id="code">([A-Z0-9-]+)<', html)
code = m.group(1) if m else ''
check('找回码为 4-4-4-4 格式', bool(re.fullmatch(r'[A-Z2-9]{4}(-[A-Z2-9]{4}){3}', code)), code)
check('找回码不含易混字符 0O1I', not set(code.replace('-', '')) & set('0O1I'))

row = user_row(uid=guest['id'])
check('库里存的是 64 位摘要', bool(re.fullmatch(r'[0-9a-f]{64}', row['recovery_code'] or '')))
check('库里不存明文', code.replace('-', '') not in (row['recovery_code'] or ''))
check('看板显示已生成', '已生成找回码' in client.get('/dashboard').get_data(as_text=True))

# ── 2. 新设备用码找回 ────────────────────────────────────────────────────────
other = new_client()
check('新设备一开始没有会话', other.get('/dashboard').status_code == 302)

app_module._recover_hits.clear()
r = other.post('/recover', data={'code': code})
check('正确找回码 302 到看板', r.status_code == 302 and 'recovered=1' in r.headers['Location'],
      r.headers.get('Location', ''))
check('找回后能打开原临时作品', other.get(f'/{guest["handle"]}/{slug}').status_code == 200)
check('看板出现恢复提示', '已用找回码恢复临时身份' in other.get('/dashboard?recovered=1').get_data(as_text=True))

# 归一化：小写 + 无分隔符
third = new_client()
app_module._recover_hits.clear()
r = third.post('/recover', data={'code': code.lower().replace('-', '')})
check('小写无分隔符也能找回', r.status_code == 302 and 'recovered=1' in r.headers['Location'])

# ── 3. 错误输入 ──────────────────────────────────────────────────────────────
app_module._recover_hits.clear()
bad = new_client().post('/recover', data={'code': 'AAAA-BBBB-CCCC-DDDD'})
check('错误找回码给出提示', bad.status_code == 200 and '无效' in bad.get_data(as_text=True))
app_module._recover_hits.clear()
short = new_client().post('/recover', data={'code': 'ABC'})
check('格式不对也按无效处理', short.status_code == 200 and '无效' in short.get_data(as_text=True))

# ── 4. 重新生成：旧码作废 ────────────────────────────────────────────────────
app_module._recover_hits.clear()
r = client.post('/recovery-code')
code2 = re.search(r'id="code">([A-Z0-9-]+)<', r.get_data(as_text=True)).group(1)
check('重新生成得到不同的码', code2 != code and len(code2) == 19)
old = new_client().post('/recover', data={'code': code})
check('旧码已作废', old.status_code == 200 and '无效' in old.get_data(as_text=True))
app_module._recover_hits.clear()
fresh = new_client().post('/recover', data={'code': code2})
check('新码可用', fresh.status_code == 302 and 'recovered=1' in fresh.headers['Location'])

# ── 5. 注册升级后找回码失效 ──────────────────────────────────────────────────
r = client.post('/register', data={'username': 'alice', 'email': 'alice@test.local',
                                   'password': 'test12345', 'confirm_password': 'test12345'})
check('游客原地注册成功', r.status_code == 302)
upgraded = user_row(uid=guest['id'])
check('升级后 is_guest=0', upgraded['is_guest'] == 0)
check('升级后找回码被清空', upgraded['recovery_code'] is None)
app_module._recover_hits.clear()
r = new_client().post('/recover', data={'code': code2})
check('已注册账号的旧码不能再用', r.status_code == 200 and '无效' in r.get_data(as_text=True))

# ── 6. 正式用户不能生成找回码 ────────────────────────────────────────────────
r = client.post('/recovery-code')
check('注册用户生成被驳回', r.status_code == 302 and '/dashboard' in r.headers['Location'])
check('注册用户仍无找回码', user_row(uid=guest['id'])['recovery_code'] is None)

# ── 7. 限流 ──────────────────────────────────────────────────────────────────
app_module._recover_hits.clear()
for _ in range(app_module.RECOVER_MAX_TRIES):
    new_client().post('/recover', data={'code': 'AAAA-BBBB-CCCC-DDDD'})
blocked = new_client().post('/recover', data={'code': code})
check('超过次数后被限流', blocked.status_code == 200 and '过多' in blocked.get_data(as_text=True))
app_module._recover_hits.clear()

passed = sum(1 for _, c, _ in results if c)
print(f'\n==== {passed}/{len(results)} 通过 ====')
raise SystemExit(0 if passed == len(results) else 1)
