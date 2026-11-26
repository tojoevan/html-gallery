"""保留用户名防抢注测试（2026-11-27）。

覆盖：管理员名/常见功能名不可注册、大小写不敏感、普通用户名不受影响、
已存在的 tojoevan 账号仍可正常登录（不误伤存量）。
运行：python3 test_reserved.py
"""
import os
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-reserved-')
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


def try_register(username):
    return client.post('/register', data={
        'username': username, 'email': f'{username}@test.local',
        'password': 'test12345', 'confirm_password': 'test12345'
    })


# ── 1. 保留名不可注册（大小写不敏感）────────────────────────────────────────
for name in ['tojoevan', 'admin', 'ADMIN', 'Administrator', 'root', 'system',
             'support', 'noreply', 'api', 'www', 'guest', 'test', 'bot', 'official']:
    r = try_register(name)
    body = r.get_data(as_text=True)
    check('保留名被拒 %s' % name, '系统保留字' in body, str(r.status_code))

# ── 2. 库里确实没有这些保留名（防「报错但写入」）─────────────────────────────
c = sqlite3.connect(DB)
rows = [r[0] for r in c.execute('SELECT username FROM users')]
c.close()
check('被拒用户名未写入库', not (set(rows) & {'tojoevan', 'admin', 'root', 'api', 'guest', 'test'}))

# ── 3. 普通用户名正常注册 ───────────────────────────────────────────────────
r = try_register('alice')
check('普通用户名可注册', r.status_code == 302, str(r.status_code))

# ── 4. 存量 tojoevan 管理员不受影响（直接插库模拟线上已存在）──────────────────
from werkzeug.security import generate_password_hash
c = sqlite3.connect(DB)
c.execute('INSERT INTO users (username,email,password,created,handle,is_admin,is_guest) '
          'VALUES (?,?,?,?,?,?,0)',
          ('tojoevan', 'admin@test.local', generate_password_hash('test12345'),
           '2026-01-01T00:00:00', 'tojoevan', 1))
c.commit(); c.close()
admin = app_module.app.test_client()
r = admin.post('/login', data={'username': 'tojoevan', 'password': 'test12345'})
check('存量管理员仍可登录', r.status_code == 302, str(r.status_code))

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
