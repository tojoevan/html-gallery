"""阶段5：链接导入功能测试（本地，不依赖外网）。

覆盖：
  - link_import: SSRF 拦截、DeepSeek 代码块提取、围栏优先级、上限
  - API 路由：正常导入返回 html、敏感词拦截、非法链接报错、不入库
  - 两个入口：editor.html 与首页均含导入 UI
  - 阶段1/2/3 回归另行跑
"""
import json
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

print('=== 3b. 元宝分享解析 ===')
check('识别 yb 短链', bool(li.YUANBAO_SHARE_RE.match('https://yb.tencent.com/s/HyaLNWkGUaG1')))
check('识别 yuanbao sc 链接', bool(li.YUANBAO_SHARE_RE.match('https://yuanbao.tencent.com/sc/abc123')))
check('不误识普通链接', not li.YUANBAO_SHARE_RE.match('https://example.com/s/abc'))
media = {'title': 'index.html', 'mediaType': 'code', 'url': 'https://cdn.example.com/a.html'}
chat = {'convs': [
    {'speaker': 0, 'speechesV2': [{'content': [{'type': 'text', 'msg': '用户问题'}],
       'extra': {'replaces': [{'id': 'm1', 'multimedias': [media]}]}}]},
    {'speaker': 1, 'speechesV2': [{'content': [{'type': 'text', 'msg': '回答<script>alert(1)</script>'}]}]},
]}
data = {'props': {'pageProps': {'fullChatShareData': {'chat': chat}}}}
collected = []
li._collect_yuanbao_media(data, collected)
check('递归收集 artifact 代码链接', len(collected) == 1 and collected[0]['title'] == 'index.html')
fallback = li._yuanbao_conversation_html(chat, '标题', 'https://yb.tencent.com/s/abc')
check('对话页含双方内容', '用户问题' in fallback and '回答' in fallback)
check('对话页已转义脚本', '<script>' not in fallback and '&lt;script&gt;' in fallback)
check('对话页文档式结构', '<h1>标题</h1>' in fallback
      and '查看元宝原始分享' in fallback and '元宝对话记录' in fallback)
# 说话人标签：真实数据 speaker 为 human / ai
chat_real = {'convs': [
    {'speaker': 'human', 'speechesV2': [{'content': [{'type': 'text', 'msg': '提问内容'}]}]},
    {'speaker': 'ai', 'speechesV2': [{'content': [{'type': 'text', 'msg': '回答内容'}]}]},
]}
page_real = li._yuanbao_conversation_html(chat_real, '标题', 'https://yb.tencent.com/s/abc')
check('human 判为提问方', '我的提问' in page_real
      and page_real.index('我的提问') < page_real.index('提问内容'))
check('ai 判为元宝', page_real.index('元宝') < page_real.index('回答内容')
      and 'Tencent Yuan Bao' not in page_real)
check('对话用气泡/分段结构', page_real.count('class="msg ') == 2 and 'msg-me' in page_real)
# Markdown 渲染
chat_md = {'convs': [{'speaker': 'ai', 'speechesV2': [{'content': [
    {'type': 'text', 'msg': '月内 **−23.1%**，代码 `<div>`。\n\n### 要点\n- 第一\n- 第二\n\n```html\n<b>x</b>\n```'}]}]}]}
page_md = li._yuanbao_conversation_html(chat_md, '标题', 'https://yb.tencent.com/s/abc')
check('Markdown 粗体渲染', '<strong>−23.1%</strong>' in page_md and '**−23.1%**' not in page_md)
check('Markdown 行内代码渲染', '<code>&lt;div&gt;</code>' in page_md)
check('Markdown 标题降级渲染', '<h4>要点</h4>' in page_md)
check('Markdown 列表渲染', '<ul><li>第一</li><li>第二</li></ul>' in page_md)
check('Markdown 代码块渲染且转义', '<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>' in page_md)
check('Markdown 表格渲染', '<div class="table-wrap"><table><thead><tr><th>日期</th></tr></thead>'
      '<tbody><tr><td>11/2</td></tr></tbody></table></div>'
      in li._md_to_html('| 日期 |\n|---|\n| 11/2 |'))
# 增强语法：任务项、嵌套列表、删除线、图片
md_extra = li._md_to_html(
    '- [x] 完成\n- [ ] 待办\n  - 子项\n\n1. 一\n2. 二\n\n~~旧~~ 与 ![图](https://x/a.png)')
check('任务项渲染为勾选样式', 'class="task done"' in md_extra and 'class="task"' in md_extra
      and '[ ]' not in md_extra and '[x]' not in md_extra)
check('嵌套列表渲染', '<span class="t">待办</span><ul><li>子项</li></ul></li>' in md_extra)
check('有序列表连续编号', '<ol><li>一</li><li>二</li></ol>' in md_extra)
check('删除线渲染', '<del>旧</del>' in md_extra)
check('图片渲染', '<img src="https://x/a.png" alt="图"' in md_extra)
# 附件占位符：替换成可读标签，不留原始编码
chat_att = {'convs': [{'speaker': 'ai', 'speechesV2': [{
    'content': [{'type': 'text', 'msg': '海报见附件 [] (@replace=media_abc) 以及未知 [](@replace=media_zzz)'.replace('[] (', '[](')}],
    'extra': {'replaces': [{'id': 'media_abc', 'multimedias': [
        {'title': '海报.html', 'type': 'fileLink', 'mediaType': 'code', 'url': 'https://x/a.html'}]}]},
}]}]}
page_att = li._yuanbao_conversation_html(chat_att, '标题', 'https://yb.tencent.com/s/abc')
check('附件占位符换成可读标签', 'class="attachment"' in page_att and '海报.html' in page_att)
check('未知占位符被清除', '@replace=' not in page_att and 'media_zzz' not in page_att)
check('无控制字符残留', '\x02' not in page_att)

# 图片附件：元宝的图多无文件名，旧逻辑会整张丢掉；且直链带签名会过期，需内联
class _FakeResp:
    def __init__(self, body, ctype):
        self._body = body
        self.headers = {'Content-Type': ctype}

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 128
img_media = [{'id': 'media_img', 'title': '', 'media_type': 'image',
              'kind': 'image', 'url': 'https://userassets.yuanbao.tencent.com/a.png'}]
with mock.patch.object(li, 'assert_public_url') as ssrf, \
     mock.patch.object(li, '_build_opener') as op:
    op.return_value.open.return_value = _FakeResp(PNG, 'image/png')
    inline = li._inline_yuanbao_images(img_media)
check('图片内联为 data URI', inline.get('media_img', '').startswith('data:image/png;base64,'))
check('内联前做了 SSRF 校验', ssrf.called)

chat_img = {'convs': [{'speaker': 'ai', 'speechesV2': [{
    'content': [{'type': 'text', 'msg': '效果见图 [](@replace=media_img)'}],
    'extra': {'replaces': [{'id': 'media_img', 'multimedias': [
        {'title': '', 'mediaType': 'image', 'type': 'image',
         'url': 'https://userassets.yuanbao.tencent.com/a.png'}]}]}}]}]}
page_img = li._yuanbao_conversation_html(chat_img, '标题', 'https://yb.tencent.com/s/abc',
                                         {'media_img': 'data:image/png;base64,QUJD'})
check('无标题图片不再丢失', '<img class="att-img" src="data:image/png;base64,QUJD"' in page_img)
check('图片有可读 alt', 'alt="图片"' in page_img)
page_img_off = li._yuanbao_conversation_html(chat_img, '标题', '')
check('未内联的图片降级为标签', '🖼 图片' in page_img_off
      and '<img class="att-img"' not in page_img_off)
# 非图片响应 / 超过预算 / SSRF 拒绝，一律跳过而不是报错
with mock.patch.object(li, 'assert_public_url'), \
     mock.patch.object(li, '_build_opener') as op:
    op.return_value.open.return_value = _FakeResp(b'<html>', 'text/html')
    check('非图片 Content-Type 跳过', li._inline_yuanbao_images(img_media) == {})
with mock.patch.object(li, 'assert_public_url'), \
     mock.patch.object(li, '_build_opener') as op, \
     mock.patch.object(li, 'INLINE_IMAGE_MAX', 64):
    op.return_value.open.return_value = _FakeResp(PNG, 'image/png')
    check('超出单图预算跳过', li._inline_yuanbao_images(img_media) == {})
with mock.patch.object(li, 'assert_public_url', side_effect=li.LinkImportError('私有地址')), \
     mock.patch.object(li, '_build_opener') as op:
    check('SSRF 拦截时不发请求', li._inline_yuanbao_images(img_media) == {} and not op.called)
# 端到端：mock 网络，验证双变体 + 主字段代码优先
# 真实页面会把 < 转义为 \u003c，避免 </script> 提前闭合；测试模拟这一点
fake_page = '<html><head><title>元宝作品</title></head><body></body>' \
            '<script id="__NEXT_DATA__">' + json.dumps(data).replace('<', '\\u003c') + '</script></html>'
with mock.patch.object(li, '_fetch_webpage', return_value={
        'html': fake_page, 'title': '元宝作品', 'source_type': 'webpage'}), \
     mock.patch.object(li, '_build_opener') as opener, \
     mock.patch.object(li, 'assert_public_url'):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = '<!DOCTYPE html><html><body>元宝代码作品</body></html>'.encode('utf-8')
    opener.return_value.open.return_value = resp
    r = li._fetch_yuanbao('abc', 'https://yb.tencent.com/s/abc')
    check('主字段仍是代码（兼容）', '元宝代码作品' in r['html'])
    check('返回两个变体（对话 + 代码）', len(r.get('variants') or []) == 2)
    check('变体键正确', {v['key'] for v in r['variants']} == {'dialogue', 'code'})
    check('不再输出依赖站外样式的原页面变体', all(v['key'] != 'dialogue_raw' for v in r['variants']))
    check('对话变体含来源链接', 'https://yb.tencent.com/s/abc' in r['variants'][0]['html'])
    check('对话变体为自有样式', '元宝对话记录' in r['variants'][0]['html']
          and 'yuanbao.tencent.com' not in r['variants'][0]['html'])
    check('代码 artifact 标题正确', r['title'] == '元宝作品')
# 无代码 artifact 时只返回对话
chat_no_code = {'convs': [
    {'speaker': 0, 'speechesV2': [{'content': [{'type': 'text', 'msg': '问题'}]}]},
]}
fake_page2 = '<html><head><title>纯对话</title></head><body></body>' \
             '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {
                 'fullChatShareData': {'chat': chat_no_code}}}}).replace('<', '\\u003c') + '</script></html>'
with mock.patch.object(li, '_fetch_webpage', return_value={
        'html': fake_page2, 'title': '纯对话', 'source_type': 'webpage'}):
    r2 = li._fetch_yuanbao('abc', 'https://yb.tencent.com/s/abc')
    check('无代码时只返对话', len(r2['variants']) == 1
          and r2['variants'][0]['key'] == 'dialogue')

print('=== 3b. 整段分享文字（App 分享出来的标题+提示+链接）===')
extract_cases = [
    ('"英伟达股价动荡海报生成"点击查看元宝的回答 https://yb.tencent.com/s/hK46WPnbwOM2',
     'https://yb.tencent.com/s/hK46WPnbwOM2'),
    ('分享给你 https://yb.tencent.com/s/abc 复制打开', 'https://yb.tencent.com/s/abc'),
    ('看这个 https://yb.tencent.com/s/abc。谢谢', 'https://yb.tencent.com/s/abc'),
    ('https://example.com/p.html?x=1&y=2', 'https://example.com/p.html?x=1&y=2'),
]
for text, expect in extract_cases:
    check('提取链接 ' + expect[:34], li._extract_url(text) == expect)
with mock.patch.object(li, '_fetch_webpage', return_value={
        'html': '<html><body>文字分享页</body></html>', 'title': '文字分享',
        'source_type': 'webpage'}) as fp:
    li.import_from_link('"海报生成"点击查看元宝的回答 https://example.com/p.html')
    check('整段文字按提取后的链接抓取', fp.call_args[0][0] == 'https://example.com/p.html')
try:
    li.import_from_link('只有文字没有链接')
    check('无链接应报错', False)
except li.LinkImportError as e:
    check('无链接给出友好提示', '没有识别到链接' in str(e))

print('=== 4. API 路由（mock 抓取，避免外网）===')
html_ok = '<!DOCTYPE html><html><head><title>测试作品</title></head><body>正常内容</body></html>'
variants_two = [
    {'key': 'dialogue', 'label': '对话记录', 'html': html_ok, 'title': '测试作品'},
    {'key': 'code', 'label': '代码作品', 'html': html_ok, 'title': '测试作品'},
]
with mock.patch.object(li, 'import_from_link', return_value={
        'html': html_ok, 'title': '测试作品', 'source_type': 'webpage', 'variants': variants_two}):
    r = client.post('/api/import', data={'url': 'https://example.com/p.html'})
    d = r.get_json()
    check('正常导入 200', r.status_code == 200)
    check('正常导入返回 ok', d and d.get('ok') is True)
    check('返回 html 源码', d and d.get('html') == html_ok)
    check('返回来源类型', d and d.get('source_type') == 'webpage')
    check('API 透传 variants', len(d.get('variants') or []) == 2)

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

print('=== 4b. 批量发布接口 ===')
batch_items = [
    {'title': '对话记录', 'html_source': html_ok, 'description': '从链接导入', 'is_public': True},
    {'title': '代码作品', 'html_source': html_ok, 'description': '从链接导入', 'is_public': False},
]
r = client.post('/api/publish_batch', json={'items': batch_items})
d = r.get_json()
check('批量发布 200', r.status_code == 200 and d.get('ok'))
check('批量发布数量正确', d.get('count') == 2 and len(d.get('slugs') or []) == 2)
conn = sqlite3.connect(db_path)
rows = conn.execute('SELECT title,is_public FROM pages ORDER BY id').fetchall()
conn.close()
check('两份作品都入库', len(rows) == 2 and rows[0][1] == 1 and rows[1][1] == 0, str(rows))
# 审核不通过时整批拒绝
bad_items = [
    {'title': '正常', 'html_source': html_ok},
    {'title': '违规', 'html_source': '<body>' + sorted(app_module.moderation.load_words())[0] + '</body>'},
]
r = client.post('/api/publish_batch', json={'items': bad_items})
d = r.get_json()
check('含违规整批拒绝', r.status_code == 400 and d.get('ok') is False)
conn = sqlite3.connect(db_path)
count_after = conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
conn.close()
check('整批拒绝不半入库', count_after == 2, f'pages={count_after}')
# 空列表/超量拒绝
check('空列表拒绝', client.post('/api/publish_batch', json={'items': []}).status_code == 400)
check('超量拒绝', client.post('/api/publish_batch', json={'items': batch_items * 3}).status_code == 400)

print('=== 5. 两个入口页面可达且含 UI ===')
r = client.get('/new')
h = r.get_data(as_text=True)
check('/new 含导入输入框', 'import_url' in h and '从链接导入' in h)
check('/new 含 api/import 调用', '/api/import' in h)
check('/new 含变体选择面板', 'id="variant_pick"' in h and 'id="variant_confirm"' in h)
check('/new placeholder 含元宝', '元宝' in h)

r = client.get('/')
h = r.get_data(as_text=True)
check('首页含快速添加框', 'quick_url' in h and '链接一键发布' in h)
check('首页含 api/import 调用', '/api/import' in h)
check('首页含变体选择面板', 'id="variant_pick"' in h and 'id="variant_confirm"' in h)
check('首页含批量发布调用', '/api/publish_batch' in h)

print('=== 结果 ===')
print(f'通过 {PASS} / 失败 {FAIL}')
sys.exit(1 if FAIL else 0)
