"""从外部链接导入作品源码：零依赖实现。

支持两类来源：
  1. 独立网页（dochost / viewhtml.app / GitHub Pages / 静态 .html 等）：
     服务端 GET 抓 HTML 源码。
  2. DeepSeek 对话分享（chat.deepseek.com/share/<id>）：
     调公开接口 /api/v0/share/content?share_id=<id>（无需登录）取对话 JSON，
     从 ASSISTANT 的 RESPONSE 片段里提取 ```html 代码块。
  3. 元宝对话分享（yb.tencent.com/s/<id> 或 yuanbao.tencent.com/sc/<id>）：
     分享页 SSR 数据在 __NEXT_DATA__ 的 fullChatShareData.chat.convs 里；
     优先从 extra.replaces[].multimedias[] 取 mediaType=code 的 .html 文件直链下载，
     无代码文件时降级为对话文本转 HTML。

安全约束（用户可控 URL 的服务端抓取，必须做）：
  - 仅 http/https；禁止 userinfo（user:pass@host）
  - 域名解析后所有 IP 必须是公网（拦截回环/私网/链路本地/元数据/保留地址）
  - 重定向逐跳重新校验（防 http→内网 的跳转绕过）
  - 响应体上限 3MB，超时 12 秒，仅接受 HTML 内容类型

抓取结果不直接入库，由路由层跑 moderation.scan 后才交给前端。
"""
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 12
UA = 'Mozilla/5.0 (compatible; HTMLGallery/1.0; +link-import)'

DEEPSEEK_SHARE_RE = re.compile(
    r'^https?://(?:www\.)?chat\.deepseek\.com/share/([A-Za-z0-9_-]{1,64})(?:[/?#].*)?$',
    re.I,
)
DEEPSEEK_API = 'https://chat.deepseek.com/api/v0/share/content?share_id={}'

YUANBAO_SHARE_RE = re.compile(
    r'^https?://(?:[a-z0-9-]+\.)?(?:yb\.tencent\.com/s|yuanbao\.tencent\.com/sc)/([A-Za-z0-9_-]{1,64})(?:[/?#].*)?$',
    re.I,
)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)

# 代码块围栏：```html / ```HTML / ```htm，允许语言标识后有空白
_FENCE_RE = re.compile(r'```\s*(?:html?|HTML?)\s*\r?\n(.*?)```', re.S | re.I)


class LinkImportError(Exception):
    """可展示给用户的导入失败原因（不含内部细节）。"""
    pass


def _ip_is_public(ip_text):
    """IP 是否公网可用；私网/回环/链路本地/元数据/保留一律拒绝。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def assert_public_url(url):
    """校验 URL 的 scheme/host/port 与解析出的所有 IP；不安全即抛错。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ('http', 'https'):
        raise LinkImportError('仅支持 http / https 链接')
    if parsed.username or parsed.password:
        raise LinkImportError('链接不应包含账号密码')
    host = parsed.hostname
    if not host:
        raise LinkImportError('链接缺少域名')
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == 'https' else 80)
    except ValueError:
        raise LinkImportError('链接端口无效')
    if port not in (80, 443):
        raise LinkImportError('仅支持 80 / 443 端口的链接')

    # 字面量 IP 直接判定
    try:
        ipaddress.ip_address(host)
        if not _ip_is_public(host):
            raise LinkImportError('不允许访问内网或本地地址')
        return
    except ValueError:
        pass  # 域名，继续 DNS 解析

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise LinkImportError('域名无法解析')
    if not infos or not all(_ip_is_public(info[4][0]) for info in infos):
        raise LinkImportError('域名解析到非公网地址，已拒绝')


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新做 SSRF 校验，防 http://公网 → http://内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    return urllib.request.build_opener(_SafeRedirect())


def _read_response(resp):
    ctype = (resp.headers.get('Content-Type') or '').lower()
    # 允许 absence（部分静态服务器不返回），但显式非 HTML 类型拒绝
    if ctype and not any(t in ctype for t in ('text/html', 'application/xhtml+xml',
                                              'text/plain', 'application/octet-stream')):
        raise LinkImportError('链接返回的不是网页内容（类型：' + ctype.split(';')[0] + '）')
    raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise LinkImportError('网页内容超过 3MB，无法导入')
    # 按响应编码解码，兜底 UTF-8
    charset = 'utf-8'
    m = re.search(r'charset=([\w-]+)', ctype)
    if m:
        charset = m.group(1)
    try:
        return raw.decode(charset, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def _title_from_html(html):
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if not m:
        return ''
    title = re.sub(r'<[^>]+>', '', m.group(1))
    title = re.sub(r'\s+', ' ', title).strip()
    return title[:120]


def _fetch_webpage(url):
    assert_public_url(url)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    try:
        resp = _build_opener().open(req, timeout=TIMEOUT)
        with resp:
            html = _read_response(resp)
    except LinkImportError:
        raise
    except urllib.error.HTTPError as e:
        raise LinkImportError('链接返回 HTTP ' + str(e.code))
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise LinkImportError('无法抓取该链接（' + type(e).__name__ + '）')
    if not html.strip():
        raise LinkImportError('链接返回了空内容')
    return {'html': html, 'title': _title_from_html(html), 'source_type': 'webpage'}


def _extract_html_block(messages):
    """从对话 JSON 的 ASSISTANT/RESPONSE 片段里取最长的 html 代码块。"""
    blocks = []
    for msg in messages or []:
        if str(msg.get('role', '')).upper() != 'ASSISTANT':
            continue
        for frag in msg.get('fragments') or []:
            if str(frag.get('type', '')).upper() != 'RESPONSE':
                continue
            content = frag.get('content') or ''
            for match in _FENCE_RE.finditer(content):
                code = match.group(1).strip()
                if '<html' in code.lower() or '<!doctype html' in code.lower() or len(code) > 200:
                    blocks.append(code)
            # 无围栏但整段就是 HTML 源码的情况
            stripped = content.strip()
            if stripped.lower().startswith('<!doctype html') or stripped.lower().startswith('<html'):
                blocks.append(stripped)
    if not blocks:
        return None
    return max(blocks, key=len)


def _fetch_deepseek(share_id):
    api = DEEPSEEK_API.format(urllib.parse.quote(share_id, safe=''))
    req = urllib.request.Request(api, headers={'User-Agent': UA, 'Accept': 'application/json'})
    try:
        resp = _build_opener().open(req, timeout=TIMEOUT)
        with resp:
            raw = resp.read(MAX_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, OSError) as e:
        raise LinkImportError('读取 DeepSeek 分享失败（' + type(e).__name__ + '）')
    if len(raw) > MAX_BYTES:
        raise LinkImportError('分享内容超过 3MB')
    try:
        data = json.loads(raw.decode('utf-8', errors='replace'))
    except (ValueError, UnicodeDecodeError):
        raise LinkImportError('DeepSeek 分享读取失败，可能触发了限流')
    payload = data.get('data') or {}
    if payload.get('biz_code') != 0 and not payload.get('biz_data'):
        raise LinkImportError('分享链接无效或已失效')
    biz = payload.get('biz_data') or {}
    html = _extract_html_block(biz.get('messages'))
    if not html:
        raise LinkImportError('该分享中没有找到 HTML 代码块')
    title = (biz.get('title') or '').strip()[:120] or 'DeepSeek 作品'
    return {'html': html, 'title': title, 'source_type': 'deepseek'}


_FILE_EXT = ('.html', '.htm', '.pdf', '.md', '.txt', '.zip', '.csv', '.xlsx', '.docx', '.json')
_IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp')


def _media_kind(media):
    """判断附件类型：file（文件）/ image（图片）/ other。用于替换对话里的占位符。"""
    title = (media.get('title') or '').strip().lower()
    media_type = (media.get('mediaType') or '').lower()
    file_type = (media.get('fileType') or '').lower()
    node_type = (media.get('type') or '').lower()
    if title.endswith(_FILE_EXT) or media_type == 'code':
        return 'file'
    if file_type in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp') or title.endswith(_IMAGE_EXT) \
            or 'image' in node_type or 'image' in media_type:
        return 'image'
    return 'other'


def _collect_yuanbao_media(node, out):
    """递归收集 __NEXT_DATA__ 里所有 replaces[].multimedias[] 的文件项。"""
    if isinstance(node, dict):
        for rep in node.get('replaces') or []:
            if not isinstance(rep, dict):
                continue
            for media in rep.get('multimedias') or []:
                if not isinstance(media, dict):
                    continue
                url = media.get('url') or ''
                if url:
                    out.append({
                        'id': rep.get('id') or '',
                        'title': (media.get('title') or '').strip(),
                        'media_type': (media.get('mediaType') or '').lower(),
                        'kind': _media_kind(media),
                        'url': url,
                    })
        for value in node.values():
            _collect_yuanbao_media(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_yuanbao_media(value, out)


# 元宝对话里的附件占位符：[](@replace=media_xxx)，有时带 alt 文字 [文字](@replace=media_xxx)
_REPLACE_PLACEHOLDER_RE = re.compile(r'\[([^\]]*)\]\(@replace=([^)\s]+)\)')


def _replace_media_placeholders(text, media_map, attachments):
    """把 [](@replace=media_xxx) 占位符换成附件哨兵，渲染后再还原为带样式的标签。

    未收录的附件、以及残留的占位符一律清除，避免出现原始编码。
    """
    def _sub(m):
        info = media_map.get(m.group(2).strip()) or {}
        name = (info.get('title') or '').strip()
        if not name:
            return m.group(1).strip()
        icon = '🖼 ' if info.get('kind') == 'image' else '📎 '
        attachments.append(icon + name)
        return '\x02ATT%d\x02' % (len(attachments) - 1)
    cleaned = _REPLACE_PLACEHOLDER_RE.sub(_sub, text)
    # 兜底：任何残留的 @replace= 占位编码都剔除
    return re.sub(r'\[[^\]]*\]\(@replace=[^)\s]+\)', '', cleaned)


def _restore_attachments(html, attachments):
    from html import escape
    for idx, label in enumerate(attachments):
        html = html.replace('\x02ATT%d\x02' % idx,
                            '<span class="attachment">' + escape(label) + '</span>')
    return html


def _md_inline(text):
    """行内 Markdown（输入已 HTML 转义）：行内代码、粗体、斜体、删除线、图片、链接。"""
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)
    # 图片：仅 http(s)，转义后的 &amp; 无需还原
    text = re.sub(r'!\[([^\]]*)\]\((https?://[^)\s]+)\)',
                  r'<img src="\2" alt="\1" loading="lazy" referrerpolicy="no-referrer">', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return text


_LIST_ITEM_RE = re.compile(r'^(?:[-*+]|\d+[.)])\s+')
_ORDERED_ITEM_RE = re.compile(r'^\d+[.)]\s+')
_TASK_ITEM_RE = re.compile(r'^\[([ xX])\]\s+(.*)$')


def _md_li(text, extra=''):
    """生成一个 li；`- [ ]` / `- [x]` 渲染为勾选样式。"""
    m = _TASK_ITEM_RE.match(text.strip())
    if m:
        done = m.group(1).lower() == 'x'
        return ('<li class="task' + (' done' if done else '') + '">'
                '<span class="cb">' + ('✓' if done else '') + '</span>'
                '<span class="t">' + _md_inline(m.group(2)) + '</span>'
                + extra + '</li>')
    return '<li>' + _md_inline(text) + extra + '</li>'


def _md_list_block(lines, i, n, ordered):
    """收集一段列表（可混合有序/无序），支持多层缩进与任务项。

    返回 (html, 下一个未消费的行号)。
    """
    tag = 'ol' if ordered else 'ul'
    items = []
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not _LIST_ITEM_RE.match(s):
            break
        indent = len(raw) - len(raw.lstrip())
        head = re.sub(_LIST_ITEM_RE, '', s)
        i += 1
        sub_lines = []
        while i < n:
            nxt = lines[i]
            nst = nxt.strip()
            if not nst:
                break
            if len(nxt) - len(nxt.lstrip()) > indent and _LIST_ITEM_RE.match(nst):
                sub_lines.append(nxt)
                i += 1
            else:
                break
        extra = ''
        if sub_lines:
            pad = min(len(x) - len(x.lstrip()) for x in sub_lines)
            trimmed = [x[pad:] for x in sub_lines]
            block, _ = _md_list_block(trimmed, 0, len(trimmed),
                                      bool(_ORDERED_ITEM_RE.match(trimmed[0].strip())))
            extra = block
        items.append(_md_li(head, extra))
        if i < n and not lines[i].strip():
            break
    return '<' + tag + '>' + ''.join(items) + '</' + tag + '>', i


def _md_to_html(text):
    """轻量 Markdown → HTML（先转义再渲染，零依赖）。

    支持：围栏代码块、标题、粗体/斜体/删除线、行内代码、链接、图片、
    有序/无序列表（含嵌套与任务项）、引用、表格、分隔线、段落。
    标题降一级（作品页已有 h1）。
    """
    from html import escape
    lines = escape(str(text or '')).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = []
    buf = []

    def flush():
        if buf:
            out.append('<p>' + _md_inline('<br>'.join(buf)) + '</p>')
            buf.clear()

    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if re.match(r'^```[\w+-]*$', s):
            flush()
            i += 1
            code = []
            while i < n and lines[i].strip() != '```':
                code.append(lines[i])
                i += 1
            i += 1
            out.append('<pre><code>' + '\n'.join(code) + '</code></pre>')
            continue
        if not s:
            flush()
            i += 1
            continue
        if re.match(r'^(?:---+|\*\*\*+|___+)$', s):
            flush()
            out.append('<hr>')
            i += 1
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            flush()
            level = min(len(m.group(1)) + 1, 6)
            out.append(f'<h{level}>{_md_inline(m.group(2).strip())}</h{level}>')
            i += 1
            continue
        if s.startswith('&gt;'):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith('&gt;'):
                quote.append(lines[i].strip()[4:].strip())
                i += 1
            out.append('<blockquote>' + _md_inline('<br>'.join(quote)) + '</blockquote>')
            continue
        if s.startswith('|') and i + 1 < n and re.match(r'^\|[\s:|-]+\|$', lines[i + 1].strip()):
            flush()

            def cells(row):
                return [c.strip() for c in row.strip().strip('|').split('|')]

            head = cells(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith('|'):
                rows.append(cells(lines[i].strip()))
                i += 1
            thead = ''.join('<th>' + _md_inline(c) + '</th>' for c in head)
            tbody = ''.join('<tr>' + ''.join('<td>' + _md_inline(c) + '</td>' for c in r) + '</tr>'
                            for r in rows)
            out.append('<div class="table-wrap"><table><thead><tr>' + thead
                       + '</tr></thead><tbody>' + tbody + '</tbody></table></div>')
            continue
        if re.match(r'^[-*+]\s+', s):
            flush()
            block, i = _md_list_block(lines, i, n, ordered=False)
            out.append(block)
            continue
        if re.match(r'^\d+[.)]\s+', s):
            flush()
            block, i = _md_list_block(lines, i, n, ordered=True)
            out.append(block)
            continue
        buf.append(s)
        i += 1
    flush()
    return '\n'.join(out)


_YB_DOC_CSS = '''
*{box-sizing:border-box}
body{margin:0;padding:32px 16px 56px;background:#f4f5f7;color:#24272e;
font-family:system-ui,-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
font-size:16px;line-height:1.78;-webkit-font-smoothing:antialiased}
.doc{max-width:800px;margin:0 auto;background:#fff;border:1px solid #e8e9ee;border-radius:18px;
box-shadow:0 1px 2px rgba(16,24,40,.04),0 18px 40px -24px rgba(16,24,40,.18);overflow:hidden}
.hd{padding:34px 44px 24px;border-bottom:1px solid #f0f1f5;
background:linear-gradient(180deg,#fbfbfd,#fff)}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;
letter-spacing:.08em;color:#5b6270;background:#f2f3f7;border:1px solid #e7e9ef;
border-radius:999px;padding:5px 11px}
.badge i{display:block;width:6px;height:6px;border-radius:50%;background:#3b5bdb}
.hd h1{margin:14px 0 10px;font-size:29px;line-height:1.34;font-weight:700;
letter-spacing:-.015em;color:#14161b}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;font-size:13.5px;color:#6f7683}
.meta a{color:#3b5bdb;text-decoration:none;font-weight:500}
.meta a:hover{text-decoration:underline}
.bd{padding:30px 44px 46px}
.msg{margin:0 0 28px}
.msg:last-child{margin-bottom:0}
.who{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:12.5px;
font-weight:600;letter-spacing:.04em;color:#8b919c}
.who .av{display:grid;place-items:center;width:22px;height:22px;border-radius:7px;
font-size:11px;font-weight:700;letter-spacing:0;color:#fff;background:#3b5bdb}
.msg-me .who{justify-content:flex-end}
.msg-me .who .av{background:#9aa3b2}
.msg-me .txt{max-width:86%;margin-left:auto;padding:14px 18px;font-size:15.5px;
background:#f5f6f9;border:1px solid #eceef3;border-radius:16px}
.txt>:first-child{margin-top:0}
.txt>:last-child{margin-bottom:0}
p{margin:0 0 14px}
h2{margin:30px 0 12px;font-size:21px;line-height:1.45;font-weight:600;
letter-spacing:-.01em;color:#14161b}
h3{margin:24px 0 10px;font-size:18.5px;line-height:1.5;font-weight:600;color:#1a1d24}
h4{margin:20px 0 8px;font-size:17px;font-weight:600;color:#1a1d24}
h5,h6{margin:18px 0 6px;font-size:16px;font-weight:600;color:#1a1d24}
a{color:#3b5bdb;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
strong{font-weight:600;color:#0f1115}
em{font-style:italic;color:#3d4450}
del{color:#8b919c}
ul,ol{margin:0 0 14px;padding-left:22px}
li{margin:6px 0}
li::marker{color:#9aa3b2}
ul ul,ul ol,ol ul,ol ol{margin:6px 0 0}
blockquote{margin:16px 0;padding:12px 16px;color:#4b5261;background:#f8f9fc;
border-left:3px solid #c9d0f0;border-radius:0 10px 10px 0}
blockquote p:last-child{margin-bottom:0}
hr{margin:28px 0;border:0;border-top:1px solid #eef0f4}
code{padding:2px 6px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:.87em;color:#374151;background:#f3f4f8;border:1px solid #e9eaf0;border-radius:5px}
pre{margin:16px 0;padding:18px 20px;overflow-x:auto;background:#1e2330;border-radius:12px;
box-shadow:inset 0 0 0 1px rgba(255,255,255,.05)}
pre code{padding:0;font-size:13.5px;line-height:1.68;color:#e6e9f0;background:none;border:0}
.table-wrap{margin:16px 0;overflow-x:auto;border:1px solid #ecedf2;border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th,td{padding:9px 14px;text-align:left;vertical-align:top;border-bottom:1px solid #eef0f4}
thead th{font-weight:600;color:#3a4050;background:#f8f9fb;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:nth-child(even){background:#fcfcfd}
img{max-width:100%;height:auto;margin:6px 0;border-radius:10px}
.attachment{display:inline-flex;align-items:center;gap:4px;padding:3px 11px;
font-size:13px;font-weight:500;color:#3b4fd8;background:#f4f6ff;
border:1px solid #dfe5ff;border-radius:999px}
li.task{display:flex;flex-wrap:wrap;gap:9px;align-items:flex-start;
margin-left:-20px;list-style:none}
li.task .cb{display:grid;place-items:center;flex:0 0 16px;height:16px;margin-top:5px;
font-size:10px;line-height:1;color:#fff;border:1.5px solid #c8cdd8;border-radius:4px}
li.task .t{flex:1 1 auto;min-width:0}
li.task>ul,li.task>ol{flex:0 0 100%;margin:6px 0 0 25px}
li.task.done .cb{background:#3b5bdb;border-color:#3b5bdb}
li.task.done .t{color:#8b919c;text-decoration:line-through}
@media(max-width:680px){
body{padding:0;background:#fff}
.doc{border:0;border-radius:0;box-shadow:none}
.hd{padding:24px 20px 18px}
.hd h1{font-size:23px}
.bd{padding:22px 20px 40px}
.msg-me .txt{max-width:92%}
pre{padding:14px 16px;border-radius:10px}
}
'''


def _yuanbao_conversation_html(chat, title, source_url):
    """对话记录（自有样式）：标题区 + 用户/元宝分段，Markdown 渲染，转义防注入。

    不引用元宝站外 CSS/JS —— iframe srcdoc 内跨域样式会被 CORS 拦下导致裸文本。
    """
    from html import escape
    convs = [c for c in (chat.get('convs') or [])
             if not (c.get('skipConv') or c.get('hideConv'))]
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta name="color-scheme" content="light">',
             '<title>' + escape(title) + '</title><style>' + _YB_DOC_CSS + '</style>',
             '</head><body><div class="doc"><header class="hd">',
             '<span class="badge"><i></i>元宝对话记录</span>',
             '<h1>' + escape(title) + '</h1>',
             '<div class="meta"><span>AI 生成内容，仅供参考</span>']
    if source_url:
        parts.append('<a href="' + escape(source_url, quote=True) + '" target="_blank" '
                     'rel="noopener noreferrer">查看元宝原始分享 ↗</a>')
    parts.append('</div></header><main class="bd">')
    has_content = False
    for conv in convs:
        is_me = str(conv.get('speaker')).lower() in ('0', 'human', 'user')
        texts = []
        attachments = []
        for speech in conv.get('speechesV2') or []:
            # 该条发言内的附件映射：replaces[].id → 媒体信息（标题/类型）
            media_map = {}
            for rep in ((speech.get('extra') or {}).get('replaces') or []):
                if not isinstance(rep, dict):
                    continue
                for media in rep.get('multimedias') or []:
                    if isinstance(media, dict):
                        media_map[rep.get('id') or ''] = {
                            'title': (media.get('title') or '').strip(),
                            'kind': _media_kind(media),
                        }
            for block in speech.get('content') or []:
                if not isinstance(block, dict):
                    continue
                raw = block.get('msg') or block.get('text')
                if not raw:
                    continue
                raw = _replace_media_placeholders(str(raw), media_map, attachments)
                if raw.strip():
                    texts.append(raw)
        if not texts:
            continue
        has_content = True
        body_html = _restore_attachments(_md_to_html('\n\n'.join(texts)), attachments)
        who = ('我的提问' if is_me else '<span class="av">元</span>元宝')
        parts.append(('<section class="msg ' + ('msg-me' if is_me else 'msg-ai') + '">'
                      '<div class="who">' + who + '</div>'
                      '<div class="txt">' + body_html + '</div></section>'))
    parts.append('</main></div></body></html>')
    return ''.join(parts) if has_content else ''


def _fetch_yuanbao(share_id, original_url):
    result = _fetch_webpage(original_url)
    html_page = result['html']
    m = _NEXT_DATA_RE.search(html_page)
    if not m:
        # 老 yuanbao.tencent.com/sc/ 是纯 CSR，SSR 数据不在 HTML 里
        raise LinkImportError('该元宝分享页未返回可解析内容，请在元宝里复制源码后手动上传')
    try:
        data = json.loads(m.group(1))
    except (ValueError, UnicodeDecodeError):
        raise LinkImportError('元宝分享数据解析失败')
    chat = (((data.get('props') or {}).get('pageProps') or {}).get('fullChatShareData') or {}).get('chat')
    if not chat:
        raise LinkImportError('分享内容为空或已失效')

    title = result['title'] or '元宝作品'

    # 变体1：对话记录 —— 自有样式文档页（不引用元宝站外 CSS/JS）
    dialogue = _yuanbao_conversation_html(chat, title, original_url)
    variants = []
    if dialogue:
        variants.append({'key': 'dialogue', 'group': 'dialogue',
                         'label': '对话记录',
                         'html': dialogue, 'title': title,
                         'description': '完整还原问答内容，清晰文档排版'})

    # 变体2：从 artifact 取 .html 代码文件直链（有则生成）
    medias = []
    _collect_yuanbao_media(data, medias)
    code_items = [x for x in medias
                  if (x['title'].lower().endswith('.html') or x['media_type'] == 'code')
                  and x['url'].lower().startswith(('http://', 'https://'))]
    for item in code_items:
        # 代码直链来自页面 JSON，仍须走 SSRF 校验（不可信来源）
        try:
            assert_public_url(item['url'])
            req = urllib.request.Request(item['url'], headers={'User-Agent': UA, 'Accept': '*/*'})
            with _build_opener().open(req, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_BYTES + 1)
        except LinkImportError:
            continue
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, OSError):
            continue
        if len(raw) > MAX_BYTES:
            continue
        code = raw.decode('utf-8', errors='replace').strip()
        if not code or '<html' not in code.lower() and '<!doctype html' not in code.lower():
            continue
        variants.append({'key': 'code', 'group': 'code', 'label': '代码作品', 'html': code,
                         'title': title, 'description': '提取自对话中的 ' + (item['title'] or 'HTML 文件')})
        break

    if not variants:
        raise LinkImportError('该分享中没有可提取的 HTML 代码或对话内容')
    # 主字段保持"代码优先"：有代码则用代码，否则用对话（向后兼容旧调用方）
    primary = next((v for v in variants if v['key'] == 'code'), variants[0])
    return {'html': primary['html'], 'title': title, 'source_type': 'yuanbao',
            'variants': variants}


# 从整段分享文字里提取链接：元宝等 App 分享出来的是「标题 + 提示语 + 链接」一整段
_URL_IN_TEXT_RE = re.compile(r'https?://[^\s<>"\'，。、；：！？（）【】《》「」\u4e00-\u9fff]+')


def _extract_url(text):
    """从粘贴内容中取出第一个 http(s) 链接，支持直接粘贴整段分享文字。"""
    text = (text or '').strip()
    m = _URL_IN_TEXT_RE.search(text)
    if not m:
        return text
    return m.group(0).rstrip('.,;:!?)]}\'"')


def import_from_link(url):
    """统一入口。返回 dict(html, title, source_type)；失败抛 LinkImportError。"""
    url = _extract_url(url)
    if not url:
        raise LinkImportError('请填写链接')
    if '://' not in url:
        raise LinkImportError('没有识别到链接，请粘贴包含 http(s) 链接的分享内容或网址')
    if len(url) > 2048:
        raise LinkImportError('链接过长')

    ds = DEEPSEEK_SHARE_RE.match(url)
    if ds:
        return _fetch_deepseek(ds.group(1))
    yb = YUANBAO_SHARE_RE.match(url)
    if yb:
        return _fetch_yuanbao(yb.group(1), url)
    return _fetch_webpage(url)
