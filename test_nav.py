"""全站导航与页脚一致性测试。

覆盖：主要页面共用同一导航/页脚、菜单项随身份变化、当前页高亮、回收站计数、
管理员入口对普通用户不可见。
运行：python3 test_nav.py
"""
import os
import tempfile
import sqlite3

TMP = tempfile.mkdtemp(prefix='gallery-nav-')
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


def body(path):
    return client.get(path).get_data(as_text=True)


def nav_of(html):
    i = html.find('<nav class="site-nav"')
    j = html.find('</nav>', i)
    return html[i:j] if i >= 0 else ''


# ── 1. 游客视角 ─────────────────────────────────────────────────────────────
anon = body('/')
nav = nav_of(anon)
check('游客导航含找回码/登录/注册', '找回码' in nav and '登录' in nav and '注册' in nav)
check('游客导航不含管理入口', '复审' not in nav and '回收站' not in nav)
check('首页有页脚', '<footer class="site-foot">' in anon)
check('首页隐私入口只在页脚，不再单列说明行',
      anon.count('href="/privacy"') == 1 and '<p class="list-note">' not in anon and '<p class="site-foot">' not in anon)
check('游客导航靠品牌回首页', 'site-nav-brand' in nav and 'class="on">首页' not in nav)

client.post('/new', data={'title': 'nav-a', 'html_source': '<body>a</body>',
                          'visibility': 'public', 'description': ''})

# ── 2. 登录用户视角（统一导航在所有主要页面出现）──────────────────────────
for path in ['/', '/dashboard', '/settings', '/privacy', '/new']:
    html = body(path)
    check(f'{path} 使用统一导航', '<nav class="site-nav"' in html)
    check(f'{path} 有统一页脚', '<footer class="site-foot">' in html or path == '/new')

nav = nav_of(body('/dashboard'))
check('用户导航含四个主入口',
      all(k in nav for k in ['我的作品', '首页', '设置', '退出']), nav[:0])
check('用户导航高亮当前页', 'class="on">我的作品' in nav)
check('普通用户看不到管理入口', '复审' not in nav and '回收站' not in nav)

# ── 3. 管理员视角 ──────────────────────────────────────────────────────────
db = sqlite3.connect(DB)
uid = db.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()[0]
db.execute('UPDATE users SET is_admin=1 WHERE id=?', (uid,))
db.execute("UPDATE pages SET share_expires_at='2026-01-01T00:00:00' WHERE title='nav-a'")
db.commit()
db.close()

for path, label in [('/admin/review', '复审'), ('/admin/trash', '回收站')]:
    html = body(path)
    nav = nav_of(html)
    check(f'{label}页有统一导航', '<nav class="site-nav"' in html)
    check(f'{label}页有统一页脚', '<footer class="site-foot">' in html)
    check(f'{label}页导航高亮自己', f'class="on">{label}' in nav, nav[:0])
    check(f'{label}页导航含回收站计数', '（1）' in nav or label == '复审')

dash = body('/dashboard')
check('管理员看板出现复审与回收站', '复审' in nav_of(dash) and '回收站' in nav_of(dash))

# ── 4. 结构完整性 ──────────────────────────────────────────────────────────
review = body('/admin/review')
check('复审页保留返回看板链接', '返回看板' in review)
check('复审页不含旧内联返回标题', '<h1>举报复审 <span' not in review)
trash = body('/admin/trash')
check('回收站页保留举报复审互链', '举报复审' in trash)

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
