"""前端渲染 XSS 回归：评论用户名/内容必须经 escapeHtml 转义。
运行：python3 test_xss.py
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
results = []


def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('PASS' if cond else 'FAIL'), name, detail)


with open(os.path.join(ROOT, 'templates', 'preview.html'), encoding='utf-8') as f:
    tpl = f.read()

# 评论用户名：不得裸插 innerHTML，必须经 escapeHtml
check('评论用户名经 escapeHtml 转义', '${escapeHtml(c.username ||' in tpl)
check('无裸插的用户名表达式', '${c.username' not in tpl)

# 评论内容：原有转义不能被回退掉
check('评论内容仍经 escapeHtml 转义', '${escapeHtml(c.content)}' in tpl)

# escapeHtml 本身覆盖 HTML 敏感字符（& < > "）
import re
fn = re.search(r'function escapeHtml\(str\) \{[^}]*\}', tpl, re.S)
check('escapeHtml 函数存在', bool(fn))
if fn:
    body = fn.group(0)
    for ch in ['&', '<', '>', '"']:
        check('escapeHtml 处理 %s' % ch, ("replace(/%s/g" % ch) in body)

ok = sum(1 for _, c, _ in results if c)
print('\n%d/%d 通过' % (ok, len(results)))
raise SystemExit(0 if ok == len(results) else 1)
