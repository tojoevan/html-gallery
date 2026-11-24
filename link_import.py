"""从外部链接导入作品源码：零依赖实现。

支持两类来源：
  1. 独立网页（dochost / viewhtml.app / GitHub Pages / 静态 .html 等）：
     服务端 GET 抓 HTML 源码。
  2. DeepSeek 对话分享（chat.deepseek.com/share/<id>）：
     调公开接口 /api/v0/share/content?share_id=<id>（无需登录）取对话 JSON，
     从 ASSISTANT 的 RESPONSE 片段里提取 ```html 代码块。

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


def import_from_link(url):
    """统一入口。返回 dict(html, title, source_type)；失败抛 LinkImportError。"""
    url = (url or '').strip()
    if not url:
        raise LinkImportError('请填写链接')
    if len(url) > 2048:
        raise LinkImportError('链接过长')

    ds = DEEPSEEK_SHARE_RE.match(url)
    if ds:
        return _fetch_deepseek(ds.group(1))
    return _fetch_webpage(url)
