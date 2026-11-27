"""轻量 CSRF 防护（写请求 Origin 同源校验）测试。

SameSite=Lax 已挡经典跨站 POST；这里验证补上的同站子域/老浏览器漏口，
以及无 Origin 的 curl/API 客户端与正常同源请求不被误伤。
运行：python3 test_origin.py
"""
import os
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-origin-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP
os.environ['GALLERY_SITE_URL'] = 'http://localhost'  # 与 test_client 的 Host 对齐

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()
DB = os.path.join(TMP, 'gallery.db')
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


def post(path, origin=None, **kw):
    headers = kw.pop('headers', {})
    if origin is not None:
        headers['Origin'] = origin
    return client.post(path, headers=headers, **kw)


FORM = {'username': 'no-such-user', 'password': 'wrong'}

# 无 Origin：curl / 服务端 API 客户端 / 测试，保持向后兼容
check('无 Origin 的 POST 放行', post('/login', data=FORM).status_code != 403)
# 同源（test_client 的 Host 是 localhost）
check('同源 POST 放行', post('/login', data=FORM, origin='http://localhost').status_code != 403)
check('同源默认端口写法放行',
      post('/login', data=FORM, origin='http://localhost:80').status_code != 403)
check('Origin 大小写不敏感',
      post('/login', data=FORM, origin='HTTP://LOCALHOST').status_code != 403)

# 跨站 / 同站子域 / opaque origin / 畸形 Origin 一律 403
for name, origin in (
    ('跨站 POST 被 403', 'https://evil.example'),
    ('同站子域 POST 被 403', 'http://sub.localhost'),
    ('端口不一致被 403', 'http://localhost:9999'),
    ('opaque origin 被 403', 'null'),
    ('畸形 Origin 被 403', 'not a url'),
    ('无 scheme 的 Origin 被 403', '//evil.example'),
):
    r = post('/login', data=FORM, origin=origin)
    check(name, r.status_code == 403, str(r.status_code))

# 代理头不能用来伪造同源：校验只看浏览器给的 Origin，不看 Host/X-Forwarded-Host
r = post('/login', data=FORM, origin='https://evil.example',
         headers={'X-Forwarded-Host': 'localhost'})
check('X-Forwarded-Host 不能伪造同源', r.status_code == 403, str(r.status_code))

# 只管写方法：GET 带跨站 Origin 不受影响
r = client.get('/', headers={'Origin': 'https://evil.example'})
check('GET 不校验 Origin', r.status_code == 200, str(r.status_code))

# DELETE 同样覆盖
r = client.delete('/api/comment/1', headers={'Origin': 'https://evil.example'})
check('跨站 DELETE 被 403', r.status_code == 403, str(r.status_code))

# 真实功能不受误伤：同源 Origin 下正常发布
r = post('/new', origin='http://localhost', data={
    'title': 'CSRF 同源发布', 'html_source': '<body>ok</body>',
    'visibility': 'public', 'description': ''})
db = sqlite3.connect(DB)
row = db.execute('SELECT slug FROM pages WHERE title=?', ('CSRF 同源发布',)).fetchone()
db.close()
check('同源请求可正常发布', row is not None, f'{r.status_code} {row}')

# 跨站发布不得落库
r = post('/new', origin='https://evil.example', data={
    'title': 'CSRF 跨站发布', 'html_source': '<body>bad</body>',
    'visibility': 'public', 'description': ''})
db = sqlite3.connect(DB)
row = db.execute('SELECT slug FROM pages WHERE title=?', ('CSRF 跨站发布',)).fetchone()
db.close()
check('跨站发布被拒且不落库', r.status_code == 403 and row is None, str(r.status_code))

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
