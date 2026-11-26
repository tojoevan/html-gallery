"""DNS 重绑定防护测试：校验与连接必须用同一次解析的公网 IP。

核心场景：域名首次解析返回公网 IP（过校验），二次解析被攻击者改成
127.0.0.1（重绑定）。修复后连接阶段直接 pin 首次的公网 IP，不会再次
解析域名，从而杜绝 SSRF。

运行：python3 test_pin.py
"""
import socket
from unittest import mock

import link_import as li

results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


# ── 1. pin 连接类：socket.create_connection 收到的是 IP 字面量而非域名 ──────
PUB = '93.184.216.34'  # example.com 的公网 IP（仅作地址字面量，不真连接）
with mock.patch.object(li.socket, 'create_connection', return_value=mock.MagicMock()) as cc:
    conn = li._PinnedHTTPConnection('example.com', PUB, 80)
    # 绕开真实网络，直接调被替换的 _create_connection
    conn._create_connection(('example.com', 80), 5)
    args = cc.call_args[0][0]
    check('连接目标是 pin 的公网 IP', args == (PUB, 80), str(args))
    check('连接目标不是域名', args[0] != 'example.com')

# HTTPS 版同理：TLS socket 也已 pin，但 SNI/证书 host 保持域名
with mock.patch.object(li.socket, 'create_connection', return_value=mock.MagicMock()) as cc:
    conn = li._PinnedHTTPSConnection('example.com', PUB, 443)
    conn._create_connection(('example.com', 443), 5)
    check('HTTPS 连接目标也 pin 公网 IP', cc.call_args[0][0] == (PUB, 443))
check('HTTPS 的 host 仍是原域名（SNI/证书有效）', conn.host == 'example.com', conn.host)


# ── 2. 端到端：二次解析被改成内网 IP，仍连首次校验的公网 IP ────────────────
# 模拟 getaddrinfo 首次返回公网、再返回内网（重绑定攻击）
seq = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUB, 80)),
       (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
call_count = {'n': 0}


def fake_getaddrinfo(host, port, **kw):
    call_count['n'] += 1
    # 第一次校验返回公网；之后（若有二次解析）返回内网
    return [seq[0]] if call_count['n'] == 1 else [seq[1]]


with mock.patch.object(li.socket, 'getaddrinfo', side_effect=fake_getaddrinfo), \
        mock.patch.object(li.socket, 'create_connection', return_value=mock.MagicMock()) as cc:
    # 走真实 handler 的解析路径：解析一次拿到 pin IP
    host, port, ips = li._resolve_public_ips('http://example.com/')
    check('首次解析得到公网 IP', ips == [PUB], str(ips))
    # 用该 IP 建 pin 连接，即使此时 DNS 已"变成"内网
    conn = li._PinnedHTTPConnection(host, ips[0], port)
    conn._create_connection((host, port), 5)
    check('重绑定后仍连公网 IP 而非 127.0.0.1',
          cc.call_args[0][0] == (PUB, 80), str(cc.call_args[0][0]))
    check('全程只解析一次 DNS（无二次解析窗口）', call_count['n'] == 1,
          '解析次数 ' + str(call_count['n']))


# ── 3. 首次解析即内网：拒绝，不给连接机会 ───────────────────────────────────
bad = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
with mock.patch.object(li.socket, 'getaddrinfo', return_value=bad), \
        mock.patch.object(li.socket, 'create_connection') as cc:
    try:
        li._resolve_public_ips('http://evil.example/')
        check('首次解析内网即拒绝', False, '未拒绝')
    except li.LinkImportError:
        check('首次解析内网即拒绝', True)
    check('拒绝时不建立连接', not cc.called)


# ── 4. 多 IP 全部公网才放行；混有内网整体拒绝 ──────────────────────────────
mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUB, 80)),
         (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 80))]
with mock.patch.object(li.socket, 'getaddrinfo', return_value=mixed):
    try:
        li._resolve_public_ips('http://mixed.example/')
        check('多 IP 含内网整体拒绝', False, '未拒绝')
    except li.LinkImportError:
        check('多 IP 含内网整体拒绝', True)


# ── 4b. 真实 opener 的 handler 链已用 pin 类替换默认类 ──────────────────────
op = li._build_opener()
names = [type(h).__name__ for h in op.handlers]
check('HTTP pin handler 已生效', '_PinnedHTTPHandler' in names, str(names))
check('HTTPS pin handler 已生效', '_PinnedHTTPSHandler' in names, str(names))
check('默认 HTTP handler 已被替换', 'HTTPHandler' not in names)
check('默认 HTTPS handler 已被替换', 'HTTPSHandler' not in names)


# ── 5. 重定向逐跳重新校验（_SafeRedirect 调的是新函数）──────────────────────
with mock.patch.object(li, '_resolve_public_ips', side_effect=li.LinkImportError('blocked')), \
        mock.patch.object(li.urllib.request.HTTPRedirectHandler, 'redirect_request') as parent:
    handler = li._SafeRedirect()
    try:
        handler.redirect_request(mock.MagicMock(), None, 302, 'Found', {},
                                 'http://127.0.0.1/admin')
        check('重定向到内网被拒', False, '未拒绝')
    except li.LinkImportError:
        check('重定向到内网被拒', True)
    check('被拒时不生成后续请求', not parent.called)


failed = [n for n, c, _ in results if not c]
print('\n%d/%d passed' % (len(results) - len(failed), len(results)))
if failed:
    raise SystemExit('FAILED: ' + ', '.join(failed))
