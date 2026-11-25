from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, flash
from flask.wrappers import Request as FlaskRequest
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, re, secrets, time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse
import moderation
import link_import

class GalleryRequest(FlaskRequest):
    """覆盖 Werkzeug 默认 500KB 表单内存限制，放行 3MB 作品上传。"""
    max_form_memory_size = 3 * 1024 * 1024 + 64 * 1024


app = Flask(__name__)
app.request_class = GalleryRequest
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
# 环境变量可覆盖 instance 目录（测试用；生产不设即用 Flask 默认 instance/）
INSTANCE_DIR = os.environ.get('GALLERY_INSTANCE', app.instance_path)
app.instance_path = INSTANCE_DIR
DATABASE = os.path.join(INSTANCE_DIR, 'gallery.db')

# 请求体总上限，留 64KB 给表单其他字段
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024 + 64 * 1024

# 对外站点地址：用于 canonical / OG 卡片的绝对链接（反代后 request.scheme 可能失真）
SITE_URL = (os.environ.get('GALLERY_SITE_URL') or 'https://gallery.njit.top').rstrip('/')
app.jinja_env.globals['site_url'] = SITE_URL

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
                email    TEXT,
                handle   TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                trust_points INTEGER NOT NULL DEFAULT 0,
                is_guest INTEGER NOT NULL DEFAULT 0,
                show_fission INTEGER NOT NULL DEFAULT 1
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
                review_status TEXT   NOT NULL DEFAULT 'approved',
                share_expires_at TEXT,
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

        # reports 表：用户举报 + 复审状态
        db.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id     INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                reason      TEXT,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created     TEXT    NOT NULL,
                reviewed    TEXT,
                UNIQUE(page_id, reporter_id),
                FOREIGN KEY(page_id) REFERENCES pages(id),
                FOREIGN KEY(reporter_id) REFERENCES users(id)
            )
        ''')

        db.commit()

    migrate_db()

def migrate_db():
    """幂等迁移：users 加公开标识 handle；pages 的 slug 改为用户内唯一。
    新库和老库都走这里，重复执行安全。"""
    import secrets
    with get_db() as db:
        cols = {r['name'] for r in db.execute('PRAGMA table_info(users)')}
        if 'handle' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN handle TEXT')
        # 给缺失/空的 handle 生成随机公开标识（不含登录用户名）
        for row in db.execute("SELECT id, handle FROM users WHERE handle IS NULL OR handle=''").fetchall():
            handle = 'user-' + secrets.token_hex(3)
            while db.execute('SELECT 1 FROM users WHERE handle=?', (handle,)).fetchone():
                handle = 'user-' + secrets.token_hex(3)
            db.execute('UPDATE users SET handle=? WHERE id=?', (handle, row['id']))
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_handle ON users(handle)')

        # 管理员标记：项目所有者不受作品有效期限制（供限时功能读取）
        if 'is_admin' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
        db.execute("UPDATE users SET is_admin=1 WHERE username='tojoevan'")

        # 审核：举报声誉点（徽章由此派生）
        if 'trust_points' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN trust_points INTEGER NOT NULL DEFAULT 0')

        # 限时：游客标记（注册时原地升级为普通用户）
        if 'is_guest' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN is_guest INTEGER NOT NULL DEFAULT 0')

        # 增长：/view 裂变入口开关（默认显示，作者可关）
        if 'show_fission' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN show_fission INTEGER NOT NULL DEFAULT 1')

        # reports 表（老库补建）
        db.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id     INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                reason      TEXT,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created     TEXT    NOT NULL,
                reviewed    TEXT,
                UNIQUE(page_id, reporter_id),
                FOREIGN KEY(page_id) REFERENCES pages(id),
                FOREIGN KEY(reporter_id) REFERENCES users(id)
            )
        ''')

        # pages：仅当 slug 仍是全局唯一时重建（老结构）
        page_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='pages'").fetchone()[0]
        if re.search(r'\bslug\s+TEXT\s+UNIQUE', page_sql):
            db.execute("ALTER TABLE pages RENAME TO pages_old")
            db.execute('''
                CREATE TABLE pages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    title       TEXT    NOT NULL,
                    slug        TEXT    NOT NULL,
                    html_source TEXT    NOT NULL,
                    description TEXT    DEFAULT '',
                    is_public   INTEGER DEFAULT 1,
                    created     TEXT    NOT NULL,
                    updated     TEXT    NOT NULL,
                    UNIQUE(user_id, slug),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            ''')
            db.execute('''
                INSERT INTO pages (id,user_id,title,slug,html_source,description,is_public,created,updated)
                SELECT id,user_id,title,slug,html_source,description,is_public,created,updated FROM pages_old
            ''')
            db.execute('DROP TABLE pages_old')

        # pages 新列放在重建之后，确保老库升级不丢字段
        #   存量作品：review_status=approved（不误伤）、share_expires_at=NULL（视为永久）
        page_cols = {r['name'] for r in db.execute('PRAGMA table_info(pages)')}
        if 'review_status' not in page_cols:
            db.execute("ALTER TABLE pages ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'")
        if 'share_expires_at' not in page_cols:
            db.execute('ALTER TABLE pages ADD COLUMN share_expires_at TEXT')

init_db()

# ── Auth decorator ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# ── 游客身份（阶段2）─────────────────────────────────────────────────────────
TRIAL_DAYS = 3  # 匿名作品分享有效期


def _is_guest(db, user_id):
    if not user_id:
        return False
    row = db.execute('SELECT is_guest FROM users WHERE id=?', (user_id,)).fetchone()
    return bool(row and row['is_guest'])


def _ensure_identity():
    """未登录则自动创建游客账号并写入 session；返回 user_id。"""
    if 'user_id' in session:
        return session['user_id']
    db = get_db()
    while True:
        username = 'guest-' + secrets.token_hex(5)
        if not db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            break
    handle = 'guest-' + secrets.token_hex(4)
    while db.execute('SELECT 1 FROM users WHERE handle=?', (handle,)).fetchone():
        handle = 'guest-' + secrets.token_hex(4)
    db.execute(
        '''INSERT INTO users (username, password, created, email, handle, is_guest)
           VALUES (?,?,?,?,?,1)''',
        (username, generate_password_hash(secrets.token_urlsafe(24)),
         datetime.now().isoformat(), None, handle)
    )
    db.commit()
    uid = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']
    session['user_id'] = uid
    session['username'] = username
    return uid


def identity_required(f):
    """游客或注册用户都可访问；未登录自动创建游客身份。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        _ensure_identity()
        return f(*args, **kwargs)
    return decorated


def _expiry_for(user_id, db):
    """游客作品 3 天到期；注册用户与管理员永久（NULL）。"""
    row = db.execute('SELECT is_guest, is_admin FROM users WHERE id=?', (user_id,)).fetchone()
    if row and row['is_guest'] and not row['is_admin']:
        return (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
    return None


def _is_expired(page):
    """到期判断：NULL 视为永久；坏数据不误伤（视为未到期）。"""
    raw = page['share_expires_at']
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) <= datetime.now()
    except (ValueError, TypeError):
        return False

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text[:60]

def unique_slug(base, db, user_id, exclude_id=None):
    """slug 在用户内唯一：不同用户可各用 topology。"""
    slug = slugify(base) or 'page'
    candidate = slug
    i = 1
    while True:
        row = db.execute(
            'SELECT id FROM pages WHERE user_id=? AND slug=? AND id!=?',
            (user_id, candidate, exclude_id or -1)
        ).fetchone()
        if not row:
            return candidate
        candidate = f'{slug}-{i}'
        i += 1

# ── Routes ───────────────────────────────────────────────────────────────────

def _is_admin(user_id):
    if not user_id:
        return False
    db = get_db()
    row = db.execute('SELECT is_admin FROM users WHERE id=?', (user_id,)).fetchone()
    return bool(row and row['is_admin'])


@app.route('/')
def index():
    db = get_db()
    now = datetime.now().isoformat()
    pages = db.execute(
        '''SELECT p.*, u.username, u.handle FROM pages p
           JOIN users u ON p.user_id = u.id
           WHERE p.is_public = 1 AND p.review_status = 'approved'
             AND (p.share_expires_at IS NULL OR p.share_expires_at > ?)
           ORDER BY p.updated DESC LIMIT 60''',
        (now,)
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
        handle = request.form.get('handle', '').strip().lower()
        error = None
        if not username or len(username) < 2:
            error = '用户名至少 2 个字符'
        elif not email:
            error = '请填写邮箱'
        elif not password or len(password) < 6:
            error = '密码至少 6 位'
        elif password != confirm_password:
            error = '两次密码输入不一致'
        elif handle and (not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,29}', handle)):
            error = '公开标识限 3-30 位小写字母、数字、下划线或连字符'
        else:
            db = get_db()
            # 游客注册：原地升级同一 user_id，避免作品迁移
            guest_uid = session.get('user_id')
            upgrade_uid = guest_uid if _is_guest(db, guest_uid) else None
            name_row = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
            handle_row = db.execute('SELECT id FROM users WHERE handle=?', (handle,)).fetchone() if handle else None
            if name_row and name_row['id'] != upgrade_uid:
                error = '用户名已存在'
            elif handle_row and handle_row['id'] != upgrade_uid:
                error = '公开标识已被占用'
            else:
                if not handle:
                    handle = 'user-' + secrets.token_hex(3)
                    while db.execute('SELECT 1 FROM users WHERE handle=? AND id!=?',
                                     (handle, upgrade_uid or -1)).fetchone():
                        handle = 'user-' + secrets.token_hex(3)
                if upgrade_uid:
                    # 原地升级：保留 user_id 与全部作品，游客作品一并转永久
                    db.execute(
                        '''UPDATE users SET username=?, email=?, password=?, handle=?, is_guest=0
                           WHERE id=?''',
                        (username, email, generate_password_hash(password), handle, upgrade_uid)
                    )
                    db.execute('UPDATE pages SET share_expires_at=NULL WHERE user_id=?', (upgrade_uid,))
                    uid = upgrade_uid
                else:
                    db.execute(
                        'INSERT INTO users (username, email, password, created, handle) VALUES (?,?,?,?,?)',
                        (username, email, generate_password_hash(password), datetime.now().isoformat(), handle)
                    )
                    uid = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']
                db.commit()
                session['user_id'] = uid
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
            # 只允许站内相对路径，防开放重定向（//evil.com 的 netloc 非空会被挡）
            parsed = urlparse(next_url)
            if parsed.netloc or not parsed.path.startswith('/'):
                next_url = url_for('dashboard')
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
    user = db.execute('SELECT id, username, email, handle, password, show_fission FROM users WHERE id=?', (session['user_id'],)).fetchone()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'toggle_fission':
            show = 1 if request.form.get('show_fission') else 0
            db.execute('UPDATE users SET show_fission=? WHERE id=?', (show, session['user_id']))
            db.commit()
            flash('分享页引导条已' + ('开启' if show else '关闭'))
            return redirect(url_for('settings'))

        if action == 'update_handle':
            handle = request.form.get('handle', '').strip().lower()
            error = None
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,29}', handle):
                error = '公开标识限 3-30 位小写字母、数字、下划线或连字符'
            elif db.execute('SELECT id FROM users WHERE handle=? AND id!=?', (handle, session['user_id'])).fetchone():
                error = '公开标识已被占用'
            if error:
                return render_template('settings.html', user=user, error=error)
            db.execute('UPDATE users SET handle=? WHERE id=?', (handle, session['user_id']))
            db.commit()
            flash('公开标识已更新')
            return redirect(url_for('settings'))

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
    rows = db.execute(
        'SELECT * FROM pages WHERE user_id=? ORDER BY updated DESC',
        (session['user_id'],)
    ).fetchall()
    pages = []
    for row in rows:
        item = dict(row)
        item['days_left'] = None
        if item.get('share_expires_at'):
            try:
                delta = datetime.fromisoformat(item['share_expires_at']) - datetime.now()
                item['days_left'] = max(0, delta.days + (1 if delta.total_seconds() > 0 else 0))
            except (ValueError, TypeError):
                item['days_left'] = None
        pages.append(item)
    return render_template('dashboard.html', pages=pages,
                           is_admin=_is_admin(session['user_id']),
                           is_guest=_is_guest(db, session['user_id']),
                           trial=bool(request.args.get('trial')),
                           badges=_badges(session['user_id']))


@app.route('/renew/<int:page_id>', methods=['POST'])
@login_required
def renew_page(page_id):
    """匿名作品续期 3 天（从当前时间起算）；注册/管理员作品永久无需续期。"""
    db = get_db()
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or page['user_id'] != session['user_id']:
        abort(403)
    if not page['share_expires_at']:
        return redirect(url_for('dashboard'))
    expiry = _expiry_for(session['user_id'], db)
    if expiry:
        db.execute('UPDATE pages SET share_expires_at=?, updated=? WHERE id=?',
                   (expiry, datetime.now().isoformat(), page_id))
        db.commit()
    return redirect(url_for('dashboard'))

# ── Create / Edit ─────────────────────────────────────────────────────────────

MAX_HTML_BYTES = 3 * 1024 * 1024  # 3MB 后端硬限制（前端已有校验，此处防绕过）


def _check_submission(title, html_source, description):
    """统一校验：3MB 上限 + 敏感词扫描。返回错误文案或 None。"""
    if len(html_source.encode('utf-8')) > MAX_HTML_BYTES:
        return 'HTML 文件不能超过 3MB'
    hit, word, field = moderation.scan(title, description, html_source)
    if hit:
        return f'内容未通过审核：{field}含敏感词「{word}」，请修改后重试'
    return None


def _insert_page(title, html_source, description, is_public, db):
    """创建作品的唯一写入口：/new 与批量发布共用，避免逻辑漂移。"""
    now = datetime.now().isoformat()
    slug = unique_slug(title, db, session['user_id'])
    expiry = _expiry_for(session['user_id'], db)
    db.execute(
        '''INSERT INTO pages (user_id,title,slug,html_source,description,is_public,review_status,
                              share_expires_at,created,updated)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (session['user_id'], title, slug, html_source, description, is_public, 'approved',
         expiry, now, now)
    )
    return slug


@app.route('/new', methods=['GET', 'POST'])
@identity_required
def new_page():
    if request.method == 'POST':
        title = request.form.get('title', '').strip() or '未命名'
        html_source = request.form.get('html_source', '')
        description = request.form.get('description', '').strip()
        is_public = 1 if request.form.get('is_public') else 0
        error = _check_submission(title, html_source, description)
        if error:
            return render_template('editor.html', page={'title': title, 'html_source': html_source,
                                                          'description': description, 'is_public': is_public},
                                   mode='new', error=error)
        db = get_db()
        _insert_page(title, html_source, description, is_public, db)
        db.commit()
        # 动作2：游客创建成功后引导注册（高意向转化窗口）；注册用户直接回看板
        if _is_guest(db, session['user_id']):
            return redirect(url_for('dashboard', trial=1))
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
        error = _check_submission(title, html_source, description)
        if error:
            return render_template('editor.html', page=dict(page, title=title, html_source=html_source,
                                                            description=description, is_public=is_public),
                                   mode='edit', error=error)
        now = datetime.now().isoformat()
        # 内容变更后已重新扫描通过，恢复审核状态（举报导致的 rejected 也随新内容解除）
        db.execute(
            '''UPDATE pages SET title=?,html_source=?,description=?,is_public=?,review_status='approved',updated=?
               WHERE id=?''',
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

def _page_or_403(slug, handle=None):
    db = get_db()
    if handle is None:
        page = db.execute(
            'SELECT p.*, u.username, u.handle FROM pages p JOIN users u ON p.user_id=u.id WHERE p.slug=? ORDER BY p.id LIMIT 1',
            (slug,)
        ).fetchone()
    else:
        page = db.execute(
            'SELECT p.*, u.username, u.handle FROM pages p JOIN users u ON p.user_id=u.id WHERE u.handle=? AND p.slug=?',
            (handle, slug)
        ).fetchone()
    if not page:
        abort(404)
    is_owner = 'user_id' in session and session['user_id'] == page['user_id']
    is_admin = 'user_id' in session and _is_admin(session['user_id'])
    # 审核未通过/待复审：对外一律 404（不暴露存在），作者与管理员仍可见
    if page['review_status'] != 'approved' and not (is_owner or is_admin):
        abort(404)
    if not page['is_public']:
        if not (is_owner or is_admin):
            abort(403)
    # 分享有效期：到期后对外 410（数据不删），作者与管理员不受限
    if 'share_expires_at' in page.keys() and _is_expired(page) and not (is_owner or is_admin):
        abort(410)
    return page

@app.errorhandler(410)
def gone(error):
    return render_template('gone.html'), 410


@app.route('/p/<slug>')
def preview(slug):
    page = _page_or_403(slug)
    return render_template(
        'preview.html', page=page,
        html_source=page['html_source'],
        current_user_id=session.get('user_id')
    )

def _fission_ctx(db, page):
    """裂变入口上下文：非作者 + 作者未关闭时显示。"""
    current = session.get('user_id')
    is_owner = current == page['user_id']
    show = False
    if not is_owner:
        author = db.execute('SELECT show_fission FROM users WHERE id=?', (page['user_id'],)).fetchone()
        show = bool(author and author['show_fission'])
    return {'current_user_id': current, 'show_fission': show}


@app.route('/view/<slug>')
def view(slug):
    """纯净分享视图：只有作品，无顶栏无评论，iframe 独立视口保留作品自身 sticky。"""
    page = _page_or_403(slug)
    ctx = _fission_ctx(get_db(), page)
    return render_template('view.html', page=page, html_source=page['html_source'], **ctx)

# ── API: 从链接导入作品源码 ──────────────────────────────────────────────────
# 只抓取并审核后返回前端，不入库；真正发布仍走 /new，复用既有 3MB + 敏感词校验。

@app.route('/api/import', methods=['POST'])
def api_import():
    url = request.form.get('url', '')
    if not url:
        payload = request.get_json(silent=True) or {}
        url = payload.get('url', '')
    try:
        result = link_import.import_from_link(url)
    except link_import.LinkImportError as e:
        return jsonify(ok=False, error=str(e)), 400
    # 纵深防御：抓取内容立即过审核，避免把违规源码交到前端
    error = _check_submission(result['title'], result['html'], '')
    if error:
        return jsonify(ok=False, error=error), 400
    return jsonify(ok=True, html=result['html'], title=result['title'],
                   source_type=result['source_type'],
                   variants=result.get('variants'))


@app.route('/api/publish_batch', methods=['POST'])
@identity_required
def publish_batch():
    """一次发布多份作品（元宝链接：对话记录 + 代码作品）。复用审核与写入路径。"""
    payload = request.get_json(silent=True) or {}
    items = payload.get('items')
    if not isinstance(items, list) or not items or len(items) > 5:
        return jsonify(ok=False, error='发布数量无效'), 400
    # 先全部审核，任一不通过则整批拒绝（避免半批入库）
    prepared = []
    for item in items:
        if not isinstance(item, dict):
            return jsonify(ok=False, error='发布内容格式错误'), 400
        title = (item.get('title') or '').strip()[:120] or '未命名'
        html_source = item.get('html_source') or ''
        description = (item.get('description') or '').strip()[:500]
        is_public = 1 if item.get('is_public', True) else 0
        error = _check_submission(title, html_source, description)
        if error:
            return jsonify(ok=False, error=error), 400
        prepared.append((title, html_source, description, is_public))
    db = get_db()
    slugs = [_insert_page(t, h, d, p, db) for t, h, d, p in prepared]
    db.commit()
    return jsonify(ok=True, count=len(slugs), slugs=slugs,
                   is_guest=_is_guest(db, session['user_id']))


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

# ── Moderation: 举报 / 复审 / 激励 ──────────────────────────────────────────

GUARDIAN_BADGE_THRESHOLD = 5  # 有效举报达标自动授予「守护者」徽章


def _badges(user_id):
    """徽章由 trust_points 派生，不单独建表。"""
    db = get_db()
    row = db.execute('SELECT trust_points FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return []
    badges = []
    if row['trust_points'] >= GUARDIAN_BADGE_THRESHOLD:
        badges.append('守护者')
    return badges


@app.route('/api/report/<int:page_id>', methods=['POST'])
@login_required
def api_report(page_id):
    db = get_db()
    page = db.execute('SELECT id, user_id, review_status FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page:
        return jsonify({'error': '页面不存在'}), 404
    if page['user_id'] == session['user_id']:
        return jsonify({'error': '不能举报自己的作品'}), 400
    if page['review_status'] != 'approved':
        return jsonify({'error': '该作品已在复审中'}), 400
    reason = (request.json or {}).get('reason', '').strip()[:200]
    try:
        db.execute(
            'INSERT INTO reports (page_id,reporter_id,reason,status,created) VALUES (?,?,?,?,?)',
            (page_id, session['user_id'], reason, 'pending', datetime.now().isoformat())
        )
    except sqlite3.IntegrityError:
        return jsonify({'error': '你已举报过该作品'}), 400
    # 举报即将作品转入待复审：对外 404，作者和管理员仍可见
    db.execute("UPDATE pages SET review_status='pending_review' WHERE id=?", (page_id,))
    db.commit()
    return jsonify({'success': True, 'message': '举报已提交，作品进入复审'})


@app.route('/admin/review')
@login_required
def admin_review():
    if not _is_admin(session['user_id']):
        abort(403)
    db = get_db()
    rows = db.execute(
        '''SELECT r.*, p.title, p.slug, u.handle, u.username AS author,
                  ru.username AS reporter
           FROM reports r JOIN pages p ON r.page_id=p.id
           JOIN users u ON p.user_id=u.id JOIN users ru ON r.reporter_id=ru.id
           WHERE r.status='pending' ORDER BY r.created DESC'''
    ).fetchall()
    return render_template('review.html', reports=rows)


@app.route('/api/review/<int:report_id>/<action>', methods=['POST'])
@login_required
def api_review(report_id, action):
    if not _is_admin(session['user_id']):
        return jsonify({'error': '无权限'}), 403
    if action not in ('approve', 'reject'):
        return jsonify({'error': '无效操作'}), 400
    db = get_db()
    report = db.execute('SELECT * FROM reports WHERE id=? AND status=?', (report_id, 'pending')).fetchone()
    if not report:
        return jsonify({'error': '举报不存在或已处理'}), 404
    now = datetime.now().isoformat()
    if action == 'approve':
        # 举报不成立：恢复作品，举报人 -1（滥用举报惩罚）
        db.execute("UPDATE reports SET status='dismissed',reviewed=? WHERE id=?", (now, report_id))
        db.execute("UPDATE pages SET review_status='approved' WHERE id=?", (report['page_id'],))
        db.execute('UPDATE users SET trust_points=trust_points-1 WHERE id=?', (report['reporter_id'],))
        msg = '已驳回举报，作品恢复'
    else:
        # 举报成立：作品保持 rejected（对外 404），举报人 +1
        db.execute("UPDATE reports SET status='upheld',reviewed=? WHERE id=?", (now, report_id))
        db.execute("UPDATE pages SET review_status='rejected' WHERE id=?", (report['page_id'],))
        db.execute('UPDATE users SET trust_points=trust_points+1 WHERE id=?', (report['reporter_id'],))
        msg = '已确认违规，作品下架'
    db.commit()
    points = db.execute('SELECT trust_points FROM users WHERE id=?', (report['reporter_id'],)).fetchone()['trust_points']
    return jsonify({'success': True, 'message': msg, 'trust_points': points,
                    'badges': _badges(report['reporter_id'])})


# ── Public share: /<handle>/<slug> ──────────────────────────────────────────
# 放在所有静态路由之后：Flask 静态路由优先，两段动态路径不会吃掉 /login 等。
@app.route('/<handle>/<slug>')
def share(handle, slug):
    """公开分享链接：按公开标识 + 作品后缀定位，不含登录用户名。"""
    page = _page_or_403(slug, handle=handle)
    ctx = _fission_ctx(get_db(), page)
    return render_template('view.html', page=page, html_source=page['html_source'], **ctx)

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', port=5050)
