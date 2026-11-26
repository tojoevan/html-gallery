"""别名孤儿清理测试：删作品时 page_aliases 同步清理。

三条删除路径（/delete、回收站永久删、注销账号）都收敛到 _delete_pages_cascade，
因此这里覆盖 /delete 与注销两条真实路由即可验证级联逻辑本身。

运行：python3 test_alias_cleanup.py
"""
import os
import re
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-alias-')
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


def create(title, c=None):
    c = c or client
    r = c.post('/new', data={'title': title, 'html_source': '<body>hi</body>',
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


def aliases_for(pid):
    c = sqlite3.connect(DB)
    rows = [r[0] for r in c.execute('SELECT slug FROM page_aliases WHERE page_id=?', (pid,))]
    c.close()
    return rows


def total_aliases():
    c = sqlite3.connect(DB)
    n = c.execute('SELECT count(*) FROM page_aliases').fetchone()[0]
    c.close()
    return n


# ── 1. 造两个中文旧 slug 的作品，迁移出别名 ────────────────────────────────
OLD1 = '别名清理测试甲-旧链接'
OLD2 = '别名清理测试乙-旧链接'
p1 = create('别名清理测试甲')
p2 = create('别名清理测试乙')
c = sqlite3.connect(DB)
c.execute('UPDATE pages SET slug=? WHERE id=?', (OLD1, p1['id']))
c.execute('UPDATE pages SET slug=? WHERE id=?', (OLD2, p2['id']))
c.commit()
c.close()
migrate()
new1 = page_by_title('别名清理测试甲')
new2 = page_by_title('别名清理测试乙')
check('两作品迁移后都是短码', bool(SHORT.match(new1['slug'])) and bool(SHORT.match(new2['slug'])))
check('两作品各有别名', aliases_for(p1['id']) == [OLD1] and aliases_for(p2['id']) == [OLD2],
      str(aliases_for(p1['id'])) + str(aliases_for(p2['id'])))
check('旧链接甲 301', client.get('/p/' + OLD1).status_code == 301)
check('旧链接乙 301', client.get('/p/' + OLD2).status_code == 301)

# ── 2. 通过 /delete 删甲：别名同步清、旧链接 404、不误伤乙 ──────────────────
r = client.post('/delete/%d' % p1['id'])
check('删除甲成功', r.status_code == 302, str(r.status_code))
check('甲的别名已清空', aliases_for(p1['id']) == [], str(aliases_for(p1['id'])))
check('甲的旧链接变 404', client.get('/p/' + OLD1).status_code == 404)
check('乙的别名未误伤', aliases_for(p2['id']) == [OLD2], str(aliases_for(p2['id'])))
check('乙的旧链接仍 301', client.get('/p/' + OLD2).status_code == 301)

# ── 3. 注销账号：剩余别名与作品一并清空 ────────────────────────────────────
r = client.post('/delete_account', data={'confirm': 'DELETE'})
check('注销成功', r.status_code == 302, str(r.status_code))
check('注销后别名全清', total_aliases() == 0, str(total_aliases()))
check('注销后作品全清', page_by_title('别名清理测试乙') is None)

# ── 4. cascade 单测：批量删多作品时别名一次性清掉 ───────────────────────────
q1 = create('批量清理丙')
q2 = create('批量清理丁')
c = sqlite3.connect(DB)
c.execute('UPDATE pages SET slug=? WHERE id=?', ('批量丙-旧', q1['id']))
c.execute('UPDATE pages SET slug=? WHERE id=?', ('批量丁-旧', q2['id']))
c.commit()
c.close()
migrate()
check('批量别名已建立', total_aliases() == 2, str(total_aliases()))
with app_module.app.test_request_context():
    db = app_module.get_db()
    removed = app_module._delete_pages_cascade(db, [q1['id'], q2['id']])
    db.commit()
    db.close()
check('cascade 返回删除数 2', removed == 2, str(removed))
check('cascade 后别名全清', total_aliases() == 0, str(total_aliases()))

failed = [n for n, c, _ in results if not c]
print('\n%d/%d passed' % (len(results) - len(failed), len(results)))
if failed:
    raise SystemExit('FAILED: ' + ', '.join(failed))
