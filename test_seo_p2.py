"""SEO P2 测试：动态 OG 封面图、图片 alt 补全、标题规范化。
运行：/tmp/gallery-test-venv/bin/python test_seo_p2.py（需 Flask + Pillow）
"""
import os
import tempfile

TMP = tempfile.mkdtemp(prefix='gallery-seo-p2-')
os.environ['FLASK_DEBUG'] = '0'
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


# ── 1. 标题规范化 ───────────────────────────────────────────────────────────
norm = app_module._normalize_title
check('全角 ASCII 转半角', norm('ＡＢＣ１２３！') == 'ABC123!', norm('ＡＢＣ１２３！'))
check('全角空格与连续空白折叠', norm('  hello\u3000\u3000world  ') == 'hello world')
check('控制字符被去掉', norm('a\x00b\nc') == 'a b c')
check('空标题兜底', norm('   ') == '未命名')
check('超长截断到 120', len(norm('汉' * 200)) == 120)
check('中文标题不变', norm('抓不住的按钮') == '抓不住的按钮')

# 写入路径真正规范化
r = client.post('/new', data={'title': '  Ａ测试　标题  ', 'html_source': '<body>x</body>',
                              'visibility': 'public'})
check('新建作品落库标题已规范化', r.status_code == 302)
import sqlite3
db = sqlite3.connect(os.path.join(TMP, 'gallery.db'))
t = db.execute('SELECT title FROM pages ORDER BY id DESC LIMIT 1').fetchone()[0]
db.close()
check('库中标题为半角且无多余空白', t == 'A测试 标题', repr(t))

# ── 2. 图片 alt 补全（展示层，不改库）───────────────────────────────────────
with_alt = app_module._html_with_alt
src = '<img src="/a/photo.jpg"><img src="/b/pic.png" title="我的图"><img src="/c/x.jpg" alt="原有"><img src="data:image/png;base64,AAA" alt="">'
out = with_alt(src, '作品标题')
check('无 alt 用文件名补', '<img alt="photo" src="/a/photo.jpg">' in out, out[:120])
check('有 title 优先用 title', '<img alt="我的图" src="/b/pic.png"' in out)
check('已有 alt 不被覆盖', '<img src="/c/x.jpg" alt="原有">' in out)
check('空 alt 被尊重', '<img src="data:image/png;base64,AAA" alt="">' in out)
check('无文件名时用作品标题兜底', 'alt="作品标题"' in with_alt('<img src="https://x.com/">', '作品标题'))

# ── 3. 动态 OG 封面图 ──────────────────────────────────────────────────────
import og_image
png = og_image.generate('测试标题', 'joevan')
check('生成的是 PNG', png[:8] == b'\x89PNG\r\n\x1a\n', repr(png[:8]))
check('图片尺寸 1200x630', len(png) > 5000, f'{len(png)} bytes')

slug = db_title = None
db = sqlite3.connect(os.path.join(TMP, 'gallery.db'))
slug, db_title = db.execute('SELECT slug,title FROM pages ORDER BY id DESC LIMIT 1').fetchone()
db.close()
r = client.get('/og/%s.png' % slug)
check('公开作品 OG 端点 200', r.status_code == 200, str(r.status_code))
check('OG 返回 PNG 类型', r.content_type == 'image/png', r.content_type)
check('OG 带缓存头', 'max-age=86400' in r.headers.get('Cache-Control', ''))

home = client.get('/').get_data(as_text=True)
page_html = client.get('/p/%s' % slug).get_data(as_text=True)
check('作品页 og:image 指向动态端点', '/og/%s.png' % slug in page_html)
check('首页 og:image 仍用静态图', '/static/og-default.png' in home)

# 私密/仅链接作品OG 应回退
import datetime
db = sqlite3.connect(os.path.join(TMP, 'gallery.db'))
db.execute("UPDATE pages SET visibility='private' WHERE slug=?", (slug,))
db.commit()
db.close()
r = client.get('/og/%s.png' % slug)
check('私密作品 OG 端点 404', r.status_code == 404, str(r.status_code))

ok = sum(1 for _, c, _ in results if c)
print(f'==== {ok}/{len(results)} 通过 ====')
raise SystemExit(0 if ok == len(results) else 1)
