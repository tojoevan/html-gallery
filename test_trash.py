"""回收站（过期作品人工处置）端到端测试。

覆盖：权限、入站判定（只收过期）、保留转永久、级联删除、非法操作。
运行：python3 test_trash.py
"""
import os
import tempfile
import sqlite3
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix='gallery-trash-')
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


def create(title, visibility='public'):
    r = client.post('/new', data={'title': title, 'html_source': '<body>hello</body>',
                                  'description': '', 'visibility': visibility})
    assert r.status_code == 302, f'create failed: {r.status_code}'


def page_by_title(title):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute('SELECT * FROM pages WHERE title=?', (title,)).fetchone()
    c.close()
    return dict(row) if row else None


def handle_of(username):
    c = sqlite3.connect(DB)
    row = c.execute('SELECT id, handle, is_guest, is_admin FROM users WHERE username=?',
                    (username,)).fetchone()
    c.close()
    return row


def user_id_by_title(title):
    c = sqlite3.connect(DB)
    row = c.execute('SELECT user_id FROM pages WHERE title=?', (title,)).fetchone()
    c.close()
    return row[0] if row else None


def set_expired(page_id, days=1):
    c = sqlite3.connect(DB)
    c.execute('UPDATE pages SET share_expires_at=? WHERE id=?',
              ((datetime.now() - timedelta(days=days)).isoformat(), page_id))
    c.commit()
    c.close()


def count_related(page_id):
    c = sqlite3.connect(DB)
    out = tuple(c.execute(f'SELECT COUNT(*) FROM {t} WHERE page_id=?', (page_id,)).fetchone()[0]
                for t in ('likes', 'comments', 'reports'))
    c.close()
    return out


# ── 1. 造数据：游客 A 两个作品，其中一个过期并带互动 ──────────────────────────
client.get('/logout')
create('过期作品-a')
create('未过期作品-b')
a = page_by_title('过期作品-a')
b = page_by_title('未过期作品-b')
set_expired(a['id'], days=2)

c = sqlite3.connect(DB)
c.execute('INSERT INTO likes (page_id, user_id, created) VALUES (?,?,?)',
          (a['id'], a['user_id'], datetime.now().isoformat()))
c.execute('INSERT INTO comments (page_id, user_id, content, status, created) VALUES (?,?,?,?,?)',
          (a['id'], a['user_id'], '测试评论', 'approved', datetime.now().isoformat()))
c.execute('INSERT INTO reports (page_id, reporter_id, reason, status, created) VALUES (?,?,?,?,?)',
          (a['id'], a['user_id'], '测试举报', 'pending', datetime.now().isoformat()))
c.commit()
c.close()
check('过期作品有互动数据', count_related(a['id']) == (1, 1, 1), str(count_related(a['id'])))

# ── 2. 注册管理员（新游客身份原地升级为正式用户，再提权）────────────────────
client.get('/logout')
r = client.post('/register', data={'username': 'boss', 'email': 'boss@test.local',
                                   'password': 'test12345', 'confirm_password': 'test12345',
                                   'handle': 'boss'})
assert r.status_code == 302, f'register failed: {r.status_code}'
c = sqlite3.connect(DB)
c.execute('UPDATE users SET is_admin=1 WHERE username=?', ('boss',))
c.commit()
c.close()

# ── 3. 权限：匿名跳登录，普通用户 403 ────────────────────────────────────────
anon = app_module.app.test_client()
anon_r = anon.get('/admin/trash')
check('匿名访问回收站跳登录', anon_r.status_code == 302
      and '/login' in anon_r.headers.get('Location', ''), f'got {anon_r.status_code}')

plain = app_module.app.test_client()
pr = plain.post('/register', data={'username': 'plain', 'email': 'plain@test.local',
                                   'password': 'test12345', 'confirm_password': 'test12345',
                                   'handle': 'plain'})
assert pr.status_code == 302, f'register plain failed: {pr.status_code}'
plain_page = plain.get('/admin/trash')
check('普通用户访问回收站被拒', plain_page.status_code == 403, f'got {plain_page.status_code}')

plain_post = plain.post(f"/admin/trash/{a['id']}/delete")
check('普通用户不能处置作品', plain_post.status_code == 403, f'got {plain_post.status_code}')
check('越权请求后作品仍在', page_by_title('过期作品-a') is not None)

# ── 4. 管理员视角：只收过期作品 ──────────────────────────────────────────────
page = client.get('/admin/trash')
html = page.get_data(as_text=True)
check('管理员可打开回收站', page.status_code == 200, f'got {page.status_code}')
check('过期作品出现在回收站', '过期作品-a' in html)
check('未过期作品不在回收站', '未过期作品-b' not in html)
check('展示作者标识', 'boss' in html or 'guest-' in html)

dash = client.get('/dashboard').get_data(as_text=True)
check('看板有回收站入口', '回收站' in dash)

# ── 5. 保留：转永久并恢复访问 ────────────────────────────────────────────────
keep = client.post(f"/admin/trash/{a['id']}/keep")
check('保留接口成功', keep.status_code == 200 and keep.get_json().get('success') is True,
      keep.get_data(as_text=True)[:80])
check('到期时间被清空', page_by_title('过期作品-a')['share_expires_at'] is None)

html = client.get('/admin/trash').get_data(as_text=True)
check('保留后离开回收站', '过期作品-a' not in html)

slug = a['slug']
c = sqlite3.connect(DB)
author_handle = c.execute('SELECT handle FROM users WHERE id=?', (a['user_id'],)).fetchone()[0]
c.close()
pub = client.get(f'/p/{slug}?u={author_handle}')
check('保留后作品恢复访问', pub.status_code == 200, f'got {pub.status_code}')

# ── 6. 删除：级联清理 ────────────────────────────────────────────────────────
set_expired(b['id'], days=1)
check('新过期作品重新入站', '未过期作品-b' in client.get('/admin/trash').get_data(as_text=True))

rm = client.post(f"/admin/trash/{b['id']}/delete")
check('删除接口成功', rm.status_code == 200 and rm.get_json().get('success') is True,
      rm.get_data(as_text=True)[:80])
check('作品已从库中删除', page_by_title('未过期作品-b') is None)
check('关联数据一并清理', count_related(b['id']) == (0, 0, 0), str(count_related(b['id'])))
check('删除后回收站为空', '未过期作品-b' not in client.get('/admin/trash').get_data(as_text=True))

# ── 7. 边界：非法动作与不存在的作品 ──────────────────────────────────────────
bad = client.post(f"/admin/trash/{a['id']}/explode")
check('非法动作被拒', bad.status_code == 400, f'got {bad.status_code}')
missing = client.post('/admin/trash/999999/delete')
check('不存在的作品返回 404', missing.status_code == 404, f'got {missing.status_code}')
check('非法/失败请求不误删', page_by_title('过期作品-a') is not None)

# ── 8. 作者续期后离开回收站 ──────────────────────────────────────────────────
set_expired(a['id'], days=1)
check('续期前在回收站', '过期作品-a' in client.get('/admin/trash').get_data(as_text=True))
# 作者身份（游客 A 的会话已丢失，直接改库模拟续期效果：作者续期会推后到期时间）
c = sqlite3.connect(DB)
c.execute('UPDATE pages SET share_expires_at=? WHERE id=?',
          ((datetime.now() + timedelta(days=3)).isoformat(), a['id']))
c.commit()
c.close()
check('续期后离开回收站', '过期作品-a' not in client.get('/admin/trash').get_data(as_text=True))

late = client.post(f"/admin/trash/{a['id']}/delete")
check('已续期作品不能被回收站删除', late.status_code == 409, f'got {late.status_code}')
check('被拒后作品仍在库', page_by_title('过期作品-a') is not None)

# ── 汇总 ─────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f'\n==== {passed}/{len(results)} 通过 ====')
if passed != len(results):
    raise SystemExit(1)
