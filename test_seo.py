"""SEO / GEO 基础设施测试：robots/sitemap/llms、文本层、canonical、JSON-LD、noindex。
运行：python3 test_seo.py
"""
import os
import tempfile
import sqlite3
import json
import re

TMP = tempfile.mkdtemp(prefix='gallery-seo-')
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


def mk(title, vis='public', review='approved', expired=False, html=None, desc=''):
    payload = {'title': title,
               'html_source': html or ('<html><body><h2>%s</h2><p>可索引的正文内容ABC</p>'
                                       '<script>var SECRET_JS=1;</script></body></html>' % title),
               'visibility': vis, 'description': desc}
    client.post('/new', data=payload)
    db = sqlite3.connect(DB)
    pid = db.execute('SELECT id FROM pages WHERE title=? ORDER BY id DESC LIMIT 1', (title,)).fetchone()[0]
    db.execute('UPDATE pages SET review_status=? WHERE id=?', (review, pid))
    if expired:
        db.execute("UPDATE pages SET share_expires_at='2020-01-01T00:00:00' WHERE id=?", (pid,))
    db.commit()
    db.close()
    return pid


def slug_of(pid):
    db = sqlite3.connect(DB)
    s = db.execute('SELECT slug FROM pages WHERE id=?', (pid,)).fetchone()[0]
    db.close()
    return s


pub = mk('SEO公开作品ABC')
unl = mk('SEO仅链接ABC', vis='unlisted')
pri = mk('SEO私密ABC', vis='private')
pend = mk('SEO待审ABC', review='pending')
exp = mk('SEO过期ABC', expired=True)

# ── 1. robots.txt ────────────────────────────────────────────────────────────
r = client.get('/robots.txt')
check('robots.txt 200', r.status_code == 200, str(r.status_code))
rb = r.get_data(as_text=True)
check('robots 允许全站', 'Allow: /' in rb)
check('robots 屏蔽管理路径', 'Disallow: /admin' in rb and 'Disallow: /dashboard' in rb)
check('robots 指向 sitemap', '/sitemap.xml' in rb)
check('robots 是纯文本', 'text/plain' in r.headers.get('Content-Type', ''))

# ── 2. sitemap.xml ───────────────────────────────────────────────────────────
r = client.get('/sitemap.xml')
check('sitemap.xml 200', r.status_code == 200, str(r.status_code))
sm = r.get_data(as_text=True)
check('sitemap 是 XML', '<?xml' in sm and '<urlset' in sm)
check('sitemap 含公开作品短链', '/p/' + slug_of(pub) in sm)
check('sitemap 不含仅链接', 'SEO仅链接' not in sm and slug_of(unl) not in sm)
check('sitemap 不含私密', slug_of(pri) not in sm)
check('sitemap 不含待审', slug_of(pend) not in sm)
check('sitemap 不含过期', slug_of(exp) not in sm)
check('sitemap 带 lastmod', '<lastmod>' in sm)
check('sitemap 无中文转义残留', '%E' not in sm)

# ── 3. llms.txt ──────────────────────────────────────────────────────────────
r = client.get('/llms.txt')
check('llms.txt 200', r.status_code == 200, str(r.status_code))
ll = r.get_data(as_text=True)
check('llms 含站点定义', '# HTML Gallery' in ll and '站点用途' in ll)
check('llms 含支持来源与典型用法', '支持的来源' in ll and '典型用法' in ll)
check('llms 含索引说明', '索引说明' in ll and 'sitemap.xml' in ll)

# ── 4. 作品页文本层 ──────────────────────────────────────────────────────────
pub_html = body('/p/' + slug_of(pub))
check('作品页有 h1', '<h1' in pub_html and 'SEO公开作品ABC' in pub_html)
check('作品页有文本层 details', '文本内容（供复制与检索）' in pub_html)
text_block = re.search(r'<div class="text-body">(.*?)</div>', pub_html, re.S)
check('文本层含正文纯文本', bool(text_block) and '可索引的正文内容ABC' in text_block.group(1))
check('文本层剥掉 script', bool(text_block) and 'SECRET_JS' not in text_block.group(1))
check('作品页 canonical 指向 /p/', '<link rel="canonical" href="https://gallery.njit.top/p/' in pub_html)
check('公开作品 JSON-LD CreativeWork', 'application/ld+json' in pub_html and 'CreativeWork' in pub_html)
ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', pub_html, re.S)
check('CreativeWork JSON 合法', bool(ld_match) and json.loads(ld_match.group(1))['@type'] == 'CreativeWork')

# ── 5. 非公开作品不给文本层 ─────────────────────────────────────────────────
unl_html = body('/p/' + slug_of(unl))
check('仅链接作品无文本层', '文本内容（供复制与检索）' not in unl_html)
check('仅链接作品 noindex', 'noindex' in unl_html)
pri_html = body('/p/' + slug_of(pri))
check('私密作品无文本层', '文本内容（供复制与检索）' not in pri_html)
check('待审作品无文本层', '文本内容（供复制与检索）' not in body('/p/' + slug_of(pend)))
check('过期作品无文本层', '文本内容（供复制与检索）' not in body('/p/' + slug_of(exp)))

# ── 6. /view/ 与 /<handle>/ 的 canonical 统一 ────────────────────────────────
db = sqlite3.connect(DB)
handle = db.execute("SELECT u.handle FROM users u JOIN pages p ON p.user_id=u.id WHERE p.id=?", (pub,)).fetchone()[0]
db.close()
for path in ['/view/' + slug_of(pub), '/' + handle + '/' + slug_of(pub)]:
    h = body(path)
    check('%s 补 description' % path, '<meta name="description"' in h)
    check('%s canonical 指向 /p/' % path,
          '<link rel="canonical" href="https://gallery.njit.top/p/%s">' % slug_of(pub) in h)
    check('%s 有 OG 标签' % path, 'og:title' in h and 'og:image' in h)

# ── 7. 首页与 410 ────────────────────────────────────────────────────────────
home = body('/')
check('首页 JSON-LD WebSite', 'application/ld+json' in home and '"@type":"WebSite"' in home)
check('首页说明块', '关于 HTML Gallery' in home and '常见问题' in home)
check('首页 FAQ 自包含段落', '作品链接会过期吗' in home and '换了设备还能找回作品吗' in home)
gone = body('/gone-nonexistent')
check('410 页 noindex', 'noindex' in gone)

# ── 8. JSON-LD 注入防护（标题含 </script> 不得闭合 script 标签）──────────────
evil = '</script><svg onload=alert(1)>'
evil_pid = mk(evil)
evil_page = body('/p/' + slug_of(evil_pid))
check('恶意标题不原样出现在 JSON-LD', '</script><svg' not in evil_page)
check('恶意标题已转义为 \\u003c', '\\u003c/script\\u003e' in evil_page)
m = re.search(r'<script type="application/ld\+json">(.*?)</script>', evil_page, re.S)
check('JSON-LD 块仍可被解析且内容完整',
      bool(m) and json.loads(m.group(1))['name'] == evil)

failed = [n for n, c, _ in results if not c]
print('\n%d/%d 通过' % (len(results) - len(failed), len(results)))
if failed:
    print('失败项:', failed)
    raise SystemExit(1)
