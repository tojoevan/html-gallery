"""首页/看板卡片缩略图（?embed=1）测试。

覆盖：卡片改用独立地址而非内联 srcdoc、大作品照样出缩略图、权限口径、
缓存头、旧别名 301 保留 embed、无源码时占位。
运行：python3 test_thumb.py
"""
import os
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-thumb-')
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


def slug_of(title):
    db = sqlite3.connect(DB)
    row = db.execute('SELECT slug FROM pages WHERE title=? ORDER BY id DESC', (title,)).fetchone()
    db.close()
    return row[0] if row else None


BIG = '<html><body>' + 'x' * 400000 + '<b>BIGMARK</b></body></html>'
client.post('/new', data={'title': '巨图作品', 'html_source': BIG,
                          'visibility': 'public', 'description': ''})
client.post('/new', data={'title': '小作品', 'html_source': '<body>tiny</body>',
                          'visibility': 'public', 'description': ''})
client.post('/new', data={'title': '私密作品', 'html_source': '<body>secret</body>',
                          'visibility': 'private', 'description': ''})
big_slug, small_slug, priv_slug = slug_of('巨图作品'), slug_of('小作品'), slug_of('私密作品')

home = client.get('/').get_data(as_text=True)
check('首页卡片用独立缩略地址', f'/p/{big_slug}?embed=1' in home, big_slug)
check('首页不再内联源码', 'srcdoc' not in home)
check('首页无「图文内容较大」占位', '图文内容较大' not in home)

r = client.get(f'/p/{big_slug}?embed=1')
body = r.get_data(as_text=True)
check('大作品也能出缩略图', r.status_code == 200 and 'BIGMARK' in body, f'{r.status_code}')
check('缩略响应只含作品源码', '<body>' in body and 'card-title' not in body)
check('缩略响应带短缓存', 'max-age=600' in (r.headers.get('Cache-Control') or ''))
check('缩略响应 noindex', 'noindex' in (r.headers.get('X-Robots-Tag') or ''))

check('小作品缩略同样可用', client.get(f'/p/{small_slug}?embed=1').status_code == 200)
check('游客访问私密缩略被拒', app_module.app.test_client()
      .get(f'/p/{priv_slug}?embed=1').status_code == 403)
check('作者本人可看私密缩略', client.get(f'/p/{priv_slug}?embed=1').status_code == 200)

db = sqlite3.connect(DB)
db.execute('INSERT OR IGNORE INTO page_aliases (page_id, slug) '
           'VALUES ((SELECT id FROM pages WHERE slug=?), ?)', (small_slug, '旧中文标题'))
db.commit()
db.close()
alias = client.get('/p/旧中文标题?embed=1')
check('旧链接带 embed 也 301 到短码',
      alias.status_code == 301 and f'embed=1' in (alias.headers.get('Location') or ''),
      alias.headers.get('Location') or '')

dash = client.get('/dashboard').get_data(as_text=True)
check('看板卡片同样用缩略地址', f'/p/{small_slug}?embed=1' in dash)

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
