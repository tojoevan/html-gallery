from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, re
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
DATABASE = os.path.join(app.instance_path, 'gallery.db')

# ── DB helpers ──────────────────────────────────────────────────────────────

def get_db():
    os.makedirs(app.instance_path, exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库，创建所有必要的表"""
    with get_db() as db:
        # users 表 - 支持 email 字段
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT    UNIQUE NOT NULL,
                password TEXT    NOT NULL,
                created  TEXT    NOT NULL,
                email    TEXT
            )
        ''')
        
        # pages 表
        db.execute('''
            CREATE TABLE IF NOT EXISTS pages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                title       TEXT    NOT NULL,
                slug        TEXT    UNIQUE NOT NULL,
                html_source TEXT    NOT NULL,
                description TEXT    DEFAULT '',
                is_public   INTEGER DEFAULT 1,
                created     TEXT    NOT NULL,
                updated     TEXT    NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        
        # likes 表
        db.execute('''
            CREATE TABLE IF NOT EXISTS likes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id   INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                created   TEXT    NOT NULL,
                UNIQUE(page_id, user_id),
                FOREIGN KEY(page_id) REFERENCES pages(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        
        # comments 表
        db.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                content     TEXT    NOT NULL,
                status      TEXT    DEFAULT 'pending',
                created     TEXT    NOT NULL,
                name        TEXT    NOT NULL DEFAULT '',
                email       TEXT    NOT NULL DEFAULT '',
                FOREIGN KEY(page_id) REFERENCES pages(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        
        db.commit()

init_db()

# ── Auth decorator ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text[:60]

def unique_slug(base, db, exclude_id=None):
    slug = slugify(base) or 'page'
    candidate = slug
    i = 1
    while True:
        row = db.execute(
            'SELECT id FROM pages WHERE slug=? AND id!=?',
            (candidate, exclude_id or -1)
        ).fetchone()
        if not row:
            return candidate
        candidate = f'{slug}-{i}'
        i += 1

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    pages = db.execute(
        '''SELECT p.*, u.username FROM pages p
           JOIN users u ON p.user_id = u.id
           WHERE p.is_public = 1
           ORDER BY p.updated DESC LIMIT 60'''
    ).fetchall()
    return render_template('index.html', pages=pages)

# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form.get('email', '').strip()
        password = request.form['password']
        confirm_password = request.form.get('confirm_password', '')
        error = None
        if not username or len(username) < 2:
            error = '用户名至少 2 个字符'
        elif not email:
            error = '请填写邮箱'
        elif not password or len(password) < 6:
            error = '密码至少 6 位'
        elif password != confirm_password:
            error = '两次密码输入不一致'
        else:
            db = get_db()
            if db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
                error = '用户名已存在'
            else:
                db.execute(
                    'INSERT INTO users (username, email, password, created) VALUES (?,?,?,?)',
                    (username, email, generate_password_hash(password), datetime.now().isoformat())
                )
                db.commit()
                user = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
                session['user_id'] = user['id']
                session['username'] = username
                return redirect(url_for('dashboard'))
        return render_template('auth.html', mode='register', error=error)
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            next_url = request.args.get('next') or url_for('dashboard')
            return redirect(next_url)
        return render_template('auth.html', mode='login', error='用户名或密码错误')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    db = get_db()
    user = db.execute('SELECT id, username, email FROM users WHERE id=?', (session['user_id'],)).fetchone()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_email':
            password = request.form.get('password')
            email = request.form.get('email', '').strip()
            error = None
            
            # 验证密码
            user_check = db.execute('SELECT password FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            if not user_check or not check_password_hash(user_check['password'], password):
                error = '密码错误'
            
            if error:
                return render_template('settings.html', user=user, error=error)
            
            if email and '@' not in email:
                error = '邮箱格式不正确'
            if not error:
                db.execute('UPDATE users SET email=? WHERE id=?', (email, session['user_id']))
                db.commit()
                flash('邮箱已更新')
                return redirect(url_for('settings'))
            return render_template('settings.html', user=user, error=error)
        
        elif action == 'change_password':
            old_password = request.form.get('old_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')
            error = None
            
            if not check_password_hash(user['password'], old_password):
                error = '原密码错误'
            elif not new_password or len(new_password) < 6:
                error = '新密码至少 6 位'
            elif new_password != confirm_password:
                error = '两次新密码输入不一致'
            else:
                db.execute('UPDATE users SET password=? WHERE id=?', 
                           (generate_password_hash(new_password), session['user_id']))
                db.commit()
                flash('密码已修改')
                return redirect(url_for('settings'))
            return render_template('settings.html', user=user, error=error)
    
    return render_template('settings.html', user=user)

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    pages = db.execute(
        'SELECT * FROM pages WHERE user_id=? ORDER BY updated DESC',
        (session['user_id'],)
    ).fetchall()
    return render_template('dashboard.html', pages=pages)

# ── Create / Edit ─────────────────────────────────────────────────────────────

@app.route('/new', methods=['GET', 'POST'])
@login_required
def new_page():
    if request.method == 'POST':
        title = request.form.get('title', '').strip() or '未命名'
        html_source = request.form.get('html_source', '')
        description = request.form.get('description', '').strip()
        is_public = 1 if request.form.get('is_public') else 0
        now = datetime.now().isoformat()
        db = get_db()
        slug = unique_slug(title, db)
        db.execute(
            'INSERT INTO pages (user_id,title,slug,html_source,description,is_public,created,updated) VALUES (?,?,?,?,?,?,?,?)',
            (session['user_id'], title, slug, html_source, description, is_public, now, now)
        )
        db.commit()
        return redirect(url_for('dashboard'))
    return render_template('editor.html', page=None, mode='new')

@app.route('/edit/<int:page_id>', methods=['GET', 'POST'])
@login_required
def edit_page(page_id):
    db = get_db()
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or page['user_id'] != session['user_id']:
        abort(403)
    if request.method == 'POST':
        title = request.form.get('title', '').strip() or '未命名'
        html_source = request.form.get('html_source', '')
        description = request.form.get('description', '').strip()
        is_public = 1 if request.form.get('is_public') else 0
        now = datetime.now().isoformat()
        db.execute(
            'UPDATE pages SET title=?,html_source=?,description=?,is_public=?,updated=? WHERE id=?',
            (title, html_source, description, is_public, now, page_id)
        )
        db.commit()
        return redirect(url_for('dashboard'))
    return render_template('editor.html', page=page, mode='edit')

@app.route('/delete/<int:page_id>', methods=['POST'])
@login_required
def delete_page(page_id):
    db = get_db()
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or page['user_id'] != session['user_id']:
        abort(403)
    db.execute('DELETE FROM pages WHERE id=?', (page_id,))
    db.commit()
    return redirect(url_for('dashboard'))

# ── Preview ───────────────────────────────────────────────────────────────────

@app.route('/p/<slug>')
def preview(slug):
    db = get_db()
    page = db.execute(
        'SELECT p.*, u.username FROM pages p JOIN users u ON p.user_id=u.id WHERE p.slug=?',
        (slug,)
    ).fetchone()
    if not page:
        abort(404)
    if not page['is_public']:
        if 'user_id' not in session or session['user_id'] != page['user_id']:
            abort(403)
    return render_template('preview.html', page=page, current_user_id=session.get('user_id'))

# ── API: live preview ─────────────────────────────────────────────────────────

@app.route('/api/preview', methods=['POST'])
@login_required
def api_preview():
    html = request.json.get('html', '')
    return jsonify({'html': html})

# ── Like ───────────────────────────────────────────────────────────────────────

@app.route('/api/like/<int:page_id>', methods=['POST'])
def api_like(page_id):
    if 'user_id' not in session:
        return jsonify({'error': '请先登录'}), 401
    db = get_db()
    page = db.execute('SELECT id FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page:
        return jsonify({'error': '页面不存在'}), 404
    existing = db.execute('SELECT id FROM likes WHERE page_id=? AND user_id=?', (page_id, session['user_id'])).fetchone()
    if existing:
        db.execute('DELETE FROM likes WHERE page_id=? AND user_id=?', (page_id, session['user_id']))
        liked = False
    else:
        db.execute('INSERT INTO likes (page_id, user_id, created) VALUES (?, ?, ?)',
                   (page_id, session['user_id'], datetime.now().isoformat()))
        liked = True
    db.commit()
    count = db.execute('SELECT COUNT(*) as c FROM likes WHERE page_id=?', (page_id,)).fetchone()['c']
    return jsonify({'liked': liked, 'count': count})

@app.route('/api/likes/<int:page_id>')
def api_likes(page_id):
    db = get_db()
    count = db.execute('SELECT COUNT(*) as c FROM likes WHERE page_id=?', (page_id,)).fetchone()['c']
    liked = False
    if 'user_id' in session:
        existing = db.execute('SELECT id FROM likes WHERE page_id=? AND user_id=?', (page_id, session['user_id'])).fetchone()
        if existing:
            liked = True
    return jsonify({'count': count, 'liked': liked})

# ── Comment ───────────────────────────────────────────────────────────────────

@app.route('/api/comment/<int:page_id>', methods=['POST'])
def api_comment(page_id):
    if 'user_id' not in session:
        return jsonify({'error': '请先登录'}), 401
    content = request.json.get('content', '').strip()
    if not content:
        return jsonify({'error': '内容不能为空'}), 400
    db = get_db()
    page = db.execute('SELECT id, user_id FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page:
        return jsonify({'error': '页面不存在'}), 404
    is_author = session['user_id'] == page['user_id']
    status = 'approved' if is_author else 'pending'
    db.execute('INSERT INTO comments (page_id, user_id, name, email, content, status, created) VALUES (?, ?, ?, ?, ?, ?, ?)',
               (page_id, session['user_id'], session.get('username', ''), '', content, status, datetime.now().isoformat()))
    db.commit()
    msg = '发布成功' if status == 'approved' else '评论已提交，需作者审核后显示'
    return jsonify({'success': True, 'message': msg, 'status': status})

@app.route('/api/comments/<int:page_id>')
def api_comments(page_id):
    db = get_db()
    page = db.execute('SELECT user_id FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page:
        return jsonify({'error': '页面不存在'}), 404
    is_author = 'user_id' in session and session['user_id'] == page['user_id']
    current_user_id = session.get('user_id')
    
    if is_author:
        # 作者可以看到所有评论（包括待审核）
        comments = db.execute(
            '''SELECT c.*, u.username FROM comments c
               LEFT JOIN users u ON c.user_id = u.id
               WHERE c.page_id = ? ORDER BY c.created DESC''',
            (page_id,)
        ).fetchall()
    elif current_user_id:
        # 非作者可以看到已通过的评论 + 自己提交的待审核评论
        comments = db.execute(
            '''SELECT c.*, u.username FROM comments c
               LEFT JOIN users u ON c.user_id = u.id
               WHERE c.page_id = ? AND (c.status = 'approved' OR (c.status = 'pending' AND c.user_id = ?)) 
               ORDER BY c.created DESC''',
            (page_id, current_user_id)
        ).fetchall()
    else:
        # 未登录用户只能看到已通过的评论
        comments = db.execute(
            '''SELECT c.*, u.username FROM comments c
               LEFT JOIN users u ON c.user_id = u.id
               WHERE c.page_id = ? AND c.status = 'approved' 
               ORDER BY c.created DESC''',
            (page_id,)
        ).fetchall()
    return jsonify({'comments': [dict(r) for r in comments], 'is_author': is_author})

# ── Comment: approve ─────────────────────────────────────────────────────────

@app.route('/api/comment/<int:comment_id>/approve', methods=['POST'])
@login_required
def approve_comment(comment_id):
    db = get_db()
    comment = db.execute('SELECT c.*, p.user_id as page_user_id FROM comments c JOIN pages p ON c.page_id = p.id WHERE c.id=?', (comment_id,)).fetchone()
    if not comment or comment['page_user_id'] != session['user_id']:
        return jsonify({'error': '无权限'}), 403
    db.execute("UPDATE comments SET status='approved' WHERE id=?", (comment_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/comment/<int:comment_id>/reject', methods=['POST'])
@login_required
def reject_comment(comment_id):
    db = get_db()
    comment = db.execute('SELECT c.*, p.user_id as page_user_id FROM comments c JOIN pages p ON c.page_id = p.id WHERE c.id=?', (comment_id,)).fetchone()
    if not comment or comment['page_user_id'] != session['user_id']:
        return jsonify({'error': '无权限'}), 403
    # 取消已通过的评论，将其改为待审核状态
    db.execute("UPDATE comments SET status='pending' WHERE id=?", (comment_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/comment/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    db = get_db()
    comment = db.execute('SELECT * FROM comments WHERE id=?', (comment_id,)).fetchone()
    if not comment:
        return jsonify({'error': '评论不存在'}), 404
    # 只有评论作者可以删除自己的评论
    if comment['user_id'] != session['user_id']:
        return jsonify({'error': '无权限删除'}), 403
    db.execute('DELETE FROM comments WHERE id=?', (comment_id,))
    db.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True, port=5050)
