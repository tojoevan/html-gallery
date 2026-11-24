"""阶段5：链接导入功能测试（本地，不依赖外网）。

覆盖：
  - link_import: SSRF 拦截、DeepSeek 代码块提取、围栏优先级、上限
  - API 路由：正常导入返回 html、敏感词拦截、非法链接报错、不入库
  - 两个入口：editor.html 与首页均含导入 UI
  - 阶段1/2/3 回归另行跑
"""
import os
import sys
import tempfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix='glink-')
os.environ['GALLERY_INSTANCE'] = TMP

import app as app_module
import link_import as li

app_module.app.config['TESTING'] = True
client = app_module.app.test_client()

PASS = 0
FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ok   ' + name)
    else:
        FAIL += 1
        print('  FAIL ' + name + ((' — ' + str(detail)) if detail else ''))


print('=== 1. SSRF 防护 ===')
for url in ['http://127.0.0.1/', 'http://localhost/secret', 'http://192.168.0.1/',
            'http://10.0.0.1/', 'http://169.254.169.254/latest/meta-data/',
            'file:///etc/passwd', 'http://user:pw@example.com/', 'http://example.com:8080/']:
    try:
        li.assert_public_url(url)
        check('拦截 ' + url, False, '未拦截')
    except li.LinkImportError:
        check('拦截 ' + url, True)

print('=== 2. DeepSeek 代码块提取 ===')
msgs = [
    {'role': 'USER', 'fragments': [{'type': 'REQUEST', 'content': '做个页面'}]},
    {'role': 'ASSISTANT', 'fragments': [
        {'type': 'THINK', 'content': '```html <h1>思考不算</h1> ```'},
        {'type': 'RESPONSE', 'content': '回答：\n```html\n<!DOCTYPE html><html><body>作品内容</body></html>\n```\n完'},
    ]},
]
code = li._extract_html_block(msgs)
check('提取 RESPONSE 中的 html 围栏', code and '作品内容' in code)
check('忽略 THINK 片段', code and '思考不算' not in code)

msgs2 = [{'role': 'ASSISTANT', 'fragments': [
    {'type': 'RESPONSE', 'content': '短：\n```html\n<h1>短</h1>\n```'},
    {'type': 'RESPONSE', 'content': '长：\n```html\n' + '<p>x</p>' * 50 + '\n```'},
]}]
code2 = li._extract_html_block(msgs2)
check('多块取最长', code2 and code2.count('<p>x</p>') == 50)

check('无代码块返回 None', li._extract_html_block(
    [{'role': 'ASSISTANT', 'fragments': [{'type': 'RESPONSE', 'content': '普通文字'}]}]) is None)

# 纯 HTML 源码（无围栏）
check('裸 HTML 源码也能提取', '<html' in (li._extract_html_block([
    {'role': 'ASSISTANT', 'fragments': [{'type': 'RESPONSE', 'content': '<html><body>裸源码</body></html>'}]}
]) or ''))

print('=== 3. DeepSeek 分享链接识别 ===')
check('识别 share 链接', bool(li.DEEPSEEK_SHARE_RE.match('https://chat.deepseek.com/share/abc123XYZ')))
check('识别带 query 的 share 链接', bool(li.DEEPSEEK_SHARE_RE.match('https://chat.deepseek.com/share/abc123?x=1')))
check('不误识普通链接', not li.DEEPSEEK_SHARE_RE.match('https://example.com/share/abc'))

print('=== 4. API 路由（mock 抓取，避免外网）===')
html_ok = '<!DOCTYPE html><html><head><title>测试作品</title></head><body>正常内容</body></html>'
with mock.patch.object(li, 'import_from_link', return_value={
        'html': html_ok, 'title': '测试作品', 'source_type': 'webpage'}):
    r = client.post('/api/import', data={'url': 'https://example.com/p.html'})
    d = r.get_json()
    check('正常导入 200', r.status_code == 200)
    check('正常导入返回 ok', d and d.get('ok') is True)
    check('返回 html 源码', d and d.get('html') == html_ok)
    check('返回来源类型', d and d.get('source_type') == 'webpage')

# 敏感词拦截：用种子库真实词，不打印内容
with mock.patch.object(li, 'import_from_link', return_value={
        'html': '<body>' + sorted(app_module.moderation.load_words())[0] + '</body>',
        'title': 't', 'source_type': 'webpage'}):
    r = client.post('/api/import', data={'url': 'https://example.com/x'})
    d = r.get_json()
    check('敏感内容被拦截', r.status_code == 400 and d.get('ok') is False)
    check('提示含审核文案', '审核' in d.get('error', ''))

# 抓取失败
with mock.patch.object(li, 'import_from_link', side_effect=li.LinkImportError('链接无效或已失效')):
    r = client.post('/api/import', data={'url': 'https://chat.deepseek.com/share/xxx'})
    d = r.get_json()
    check('抓取失败返回 400', r.status_code == 400 and d.get('ok') is False)
    check('错误信息透传', '无效' in d.get('error', ''))

# 空链接
r = client.post('/api/import', data={'url': ''})
check('空链接返回 400', r.status_code == 400)

# API 不入库：作品数不变
import sqlite3
db_path = os.path.join(TMP, 'gallery.db')
conn = sqlite3.connect(db_path)
count = conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
conn.close()
check('导入 API 不写数据库', count == 0, f'pages={count}')

print('=== 5. 两个入口页面可达且含 UI ===')
r = client.get('/new')
h = r.get_data(as_text=True)
check('/new 含导入输入框', 'import_url' in h and '从链接导入' in h)
check('/new 含 api/import 调用', '/api/import' in h)

r = client.get('/')
h = r.get_data(as_text=True)
check('首页含快速添加框', 'quick_url' in h and '链接一键发布' in h)
check('首页含 api/import 调用', '/api/import' in h)

print('=== 结果 ===')
print(f'通过 {PASS} / 失败 {FAIL}')
sys.exit(1 if FAIL else 0)
