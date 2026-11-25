"""作品短码（slug）测试：新链接全 ASCII、旧中文链接 301、迁移幂等。

运行：python3 test_slug.py
"""
import os
import re
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-slug-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()
DB = os.path.join(TMP, 'gallery.db')
SHORT = re.compile(r'^[%s]{%d}$' % (app_module.SLUG_ALPHABET, app_module.SLUG_LEN))

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


def create(title):
    r = client.post('/new', data={'title': title, 'html_source': '<body>hello</body>',
                                  'description': '', 'visibility': 'public'})
    assert r.status_code == 302, r.status_code
    return page_by_title(title)


def page_by_title(title):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute('SELECT * FROM pages WHERE title=?', (title,)).fetchone()
    c.close()
    return dict(row) if row else None


def migrate():
    with app_module.app.test_request_context():
        db = app_module.get_db()
        app_module._migrate_slugs(db)
        db.commit()
        db.close()


# ── 1. 新作品：短码、纯 ASCII、无中文 ────────────────────────────────────────
p = create('英伟达股价动荡海报生成')
check('中文标题生成短码', bool(SHORT.match(p['slug'])), p['slug'])
check('短码是纯 ASCII', p['slug'].isascii())
check('短码不含易混字符', not set(p['slug']) & set('01ilo'))

p2 = create('Topology Notes')
check('英文标题也用短码', bool(SHORT.match(p2['slug'])), p2['slug'])
check('两个作品短码不同', p['slug'] != p2['slug'])

slash_p = client.get('/p/' + p['slug'])
check('短链可访问', slash_p.status_code == 200, f"got {slash_p.status_code}")
home = client.get('/').get_data(as_text=True)
check('首页链接不含中文转义', '%E' not in home and '/p/' + p['slug'] in home)
check('首页链接不带 ?u=', 'u=' not in ' '.join(re.findall(r'href="(/p/[^"]+)"', home)))

# ── 2. 把作品改回中文 slug，模拟历史数据 ────────────────────────────────────
c = sqlite3.connect(DB)
c.execute("UPDATE pages SET slug='英伟达股价动荡海报生成' WHERE id=?", (p['id'],))
c.commit()
c.close()

old_slug = '英伟达股价动荡海报生成'
check('迁移前旧中文 slug 照常可访问', client.get('/p/' + old_slug).status_code == 200)

# ── 3. 迁移：中文 slug → 短码 + 别名 ────────────────────────────────────────
migrate()
after = page_by_title('英伟达股价动荡海报生成')
check('迁移后 slug 变短码', bool(SHORT.match(after['slug'])), after['slug'])
check('迁移后 slug 与原名不同', after['slug'] != old_slug)

c = sqlite3.connect(DB)
aliases = [r[0] for r in c.execute('SELECT slug FROM page_aliases WHERE page_id=?', (after['id'],))]
c.close()
check('旧 slug 记入别名表', aliases == [old_slug], str(aliases))

r = client.get('/p/' + old_slug)
check('旧链接 301 到短码', r.status_code == 301 and old_slug not in r.headers['Location']
      and after['slug'] in r.headers['Location'], r.headers.get('Location', ''))
check('301 后短链可用', client.get('/p/' + after['slug']).status_code == 200)

c = sqlite3.connect(DB)
handle = c.execute('SELECT handle FROM users WHERE id=?', (after['user_id'],)).fetchone()[0]
c.close()
r2 = client.get(f'/{handle}/{old_slug}')
check('完整旧路径也 301', r2.status_code == 301 and after['slug'] in r2.headers['Location'],
      r2.headers.get('Location', ''))
r3 = client.get('/view/' + old_slug)
check('纯净分享旧链接 301', r3.status_code == 301 and '/view/' in r3.headers['Location'],
      r3.headers.get('Location', ''))

# ── 4. 幂等 + 不误伤历史 ASCII slug ─────────────────────────────────────────
c = sqlite3.connect(DB)
c.execute("UPDATE pages SET slug='topology-notes' WHERE id=?", (p2['id'],))
c.commit()
c.close()
migrate()
migrate()
again = page_by_title('英伟达股价动荡海报生成')
check('重复迁移不换码', again['slug'] == after['slug'], f"{again['slug']} vs {after['slug']}")
short_ascii = page_by_title('Topology Notes')
check('历史 ASCII slug 保持原样', short_ascii['slug'] == 'topology-notes', short_ascii['slug'])
home2 = client.get('/').get_data(as_text=True)
check('历史 ASCII slug 仍带 ?u= 消歧',
      f"/p/{short_ascii['slug']}?u={handle}" in home2)

# ── 汇总 ─────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f'\n==== {passed}/{len(results)} 通过 ====')
if passed != len(results):
    raise SystemExit(1)
