from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, flash, make_response, current_app
from flask.wrappers import Request as FlaskRequest
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3, os, re, secrets, time, hashlib, html as _html
from datetime import datetime, timedelta
from functools import wraps
import moderation
import link_import

class GalleryRequest(FlaskRequest):
    """覆盖 Werkzeug 默认 500KB 表单内存限制，放行 3MB 作品上传。"""
    max_form_memory_size = 3 * 1024 * 1024 + 64 * 1024


app = Flask(__name__)
app.request_class = GalleryRequest
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-secret-change-in-prod'
if not os.environ.get('SECRET_KEY'):
    # 兜底值一公开就等于所有人都能伪造 session，生产必须配 .env
    print('[gallery] WARNING: SECRET_KEY 未配置，正在使用公开的默认值（仅限本地开发）', flush=True)
# 反向代理：应用只监听 127.0.0.1，nginx 是唯一入口且会带 X-Forwarded-For。
# 不信任这一跳的话 remote_addr 永远是 127.0.0.1，按 IP 的限流会把全站算成一个人。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# 会话保持 30 天：游客发完作品关掉浏览器，第二天回来仍能在看板看到（cookie 是签名的）
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# 线上由 nginx 强制 https，会话 cookie 加 Secure；本地/测试默认不加，否则 test_client 的 http 请求发不出 cookie
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('GALLERY_HTTPS') == '1'
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

# ── 作品短码 ──────────────────────────────────────────────────────────────────
# 中文标题直接进 URL 会被 percent-encode 成 %E4%B8%80... 一长串，粘到聊天里又长又
# 认不出。作品地址统一改成 7 位小写短码（剔除 0/1/i/l/o 易混字符），形如 /p/k7m2xq3。
SLUG_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
SLUG_LEN = 7
SLUG_RE = re.compile(r'[%s]{%d}' % (SLUG_ALPHABET, SLUG_LEN))


def _is_short_slug(slug):
    return bool(SLUG_RE.fullmatch(slug or ''))


def _new_slug(db):
    """生成全站唯一的 7 位短码。"""
    while True:
        code = ''.join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LEN))
        if not db.execute('SELECT 1 FROM pages WHERE slug=?', (code,)).fetchone():
            return code


def _migrate_slugs(db):
    """把含中文/非 ASCII（或过长）的旧 slug 换成短码，旧 slug 记入别名表以便 301。

    幂等：短码直接跳过；全 ASCII 且不超过 20 字符的旧 slug 原样保留。
    """
    db.execute('''CREATE TABLE IF NOT EXISTS page_aliases (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id INTEGER NOT NULL,
                    slug    TEXT    NOT NULL,
                    UNIQUE(page_id, slug))''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_alias_slug ON page_aliases(slug)')
    for row in db.execute('SELECT id, slug FROM pages').fetchall():
        old = row['slug'] or ''
        if not old or _is_short_slug(old):
            continue
        if all(ord(c) < 128 for c in old) and len(old) <= 20:
            continue
        db.execute('UPDATE pages SET slug=? WHERE id=?', (_new_slug(db), row['id']))
        db.execute('INSERT OR IGNORE INTO page_aliases (page_id, slug) VALUES (?,?)',
                   (row['id'], old))


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

        # 找回：游客的一次性身份凭据（只存 sha256，明文仅生成时展示一次）
        if 'recovery_code' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN recovery_code TEXT')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_recovery ON users(recovery_code)')

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
        # 可见性三态：public 进画廊 / unlisted 仅持链接可见 / private 仅作者可见
        # is_public 继续保留并与之同步（=public 时置 1），首页与卡片查询不必改
        if 'visibility' not in page_cols:
            db.execute("ALTER TABLE pages ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'")
            db.execute("UPDATE pages SET visibility = CASE WHEN is_public=1 THEN 'public' ELSE 'private' END")
        else:
            # 兜底：写入过 is_public 但未同步 visibility 的历史行
            db.execute("""UPDATE pages SET visibility = CASE WHEN is_public=1 THEN 'public' ELSE 'private' END
                          WHERE visibility IS NULL OR visibility NOT IN ('public','unlisted','private')""")
        # 续期次数：游客作品可续期次数有限，避免 3 天试用被无限续等于永久
        if 'renew_count' not in page_cols:
            db.execute('ALTER TABLE pages ADD COLUMN renew_count INTEGER NOT NULL DEFAULT 0')
        # 作品地址短码化：中文 slug 迁移为 7 位短码，旧链接经别名表 301
        _migrate_slugs(db)

init_db()

# ── Auth decorator ───────────────────────────────────────────────────────────

@app.before_request
def _persist_session():
    """已有身份的会话转成持久 cookie（只转一次，避免每个响应都写 Set-Cookie）。"""
    if 'user_id' in session and not session.permanent:
        session.permanent = True


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# ── 游客身份（阶段2）─────────────────────────────────────────────────────────
TRIAL_DAYS = 3  # 匿名作品分享有效期
RENEW_MAX_TIMES = 6  # 匿名作品最多续期次数：每次 3 天，累计约 3 周后需注册才继续保留


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
    session.permanent = True  # 关掉浏览器再回来仍是同一个临时身份
    return uid


def identity_required(f):
    """游客或注册用户都可访问；未登录自动创建游客身份。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        _ensure_identity()
        return f(*args, **kwargs)
    return decorated


def _delete_pages_cascade(db, page_ids):
    """删除作品并清掉其点赞/评论/举报，避免留下孤儿行。调用方负责 commit。"""
    ids = [int(i) for i in page_ids]
    if not ids:
        return 0
    marks = ','.join('?' * len(ids))
    db.execute('DELETE FROM likes WHERE page_id IN (%s)' % marks, ids)
    db.execute('DELETE FROM comments WHERE page_id IN (%s)' % marks, ids)
    db.execute('DELETE FROM reports WHERE page_id IN (%s)' % marks, ids)
    return db.execute('DELETE FROM pages WHERE id IN (%s)' % marks, ids).rowcount


def _expiry_for(user_id, db):
    """游客作品 3 天到期；注册用户与管理员永久（NULL）。"""
    row = db.execute('SELECT is_guest, is_admin FROM users WHERE id=?', (user_id,)).fetchone()
    if row and row['is_guest'] and not row['is_admin']:
        return (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
    return None


# ── 游客找回码 ────────────────────────────────────────────────────────────────
# 问题：游客身份只存在 session cookie 里，清 Cookie / 换设备后作品就「没人认领」。
# 做法：给游客一个 16 位找回码（80 bit，剔除 0O1I 易混字符），**库里只存 sha256**；
#       明文只在生成/重置那一次展示，之后无法再读出（重新生成会让旧码立即作废）。
# 边界：只对 is_guest=1 的账号有效；注册原地升级时清空，避免旧码接管正式账号。
RECOVERY_LEN = 16
RECOVERY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
RECOVER_MAX_TRIES = 8      # 单 IP 10 分钟内最多尝试次数
RECOVER_WINDOW = 600
_recover_hits = {}         # ip -> [尝试时间戳]（单进程内存限流，重启即清）


def _recovery_hash(code):
    """归一化（去分隔符与空格、转大写）后取 sha256；格式不合法返回 None。"""
    raw = re.sub(r'[^A-Za-z0-9]', '', code or '').upper()
    if len(raw) != RECOVERY_LEN or any(c not in RECOVERY_ALPHABET for c in raw):
        return None
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_recovery_code(db, user_id):
    """生成并写入新码，返回带分隔符的明文（形如 ABCD-EFGH-JKLM-NPQR）。"""
    while True:
        raw = ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LEN))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if not db.execute('SELECT 1 FROM users WHERE recovery_code=?', (digest,)).fetchone():
            break
    db.execute('UPDATE users SET recovery_code=? WHERE id=?', (digest, user_id))
    db.commit()
    return '-'.join(raw[i:i + 4] for i in range(0, RECOVERY_LEN, 4))


def _recover_rate_ok(ip):
    now = time.time()
    hits = [t for t in _recover_hits.get(ip, []) if now - t < RECOVER_WINDOW]
    _recover_hits[ip] = hits
    return len(hits) < RECOVER_MAX_TRIES


def _recover_hit(ip):
    """记一次失败尝试；异常 IP 过多时顺手清掉过期桶，避免内存无上限增长。"""
    now = time.time()
    if len(_recover_hits) > 2000:
        for k in [k for k, v in _recover_hits.items()
                  if not v or now - v[-1] > RECOVER_WINDOW]:
            _recover_hits.pop(k, None)
    _recover_hits.setdefault(ip, []).append(now)


# ── 上传频率限制（P2，2026-11-27）────────────────────────────────────────────
# 只挡「同一 IP 短时间反复发作品」的刷量/灌水，不影响正常试用：
#   每分钟最多 6 次，每小时最多 40 次。计数按真实 IP（ProxyFix 已解 X-Forwarded-For）。
# 与找回码限流一样是单进程内存桶，重启即清；gunicorn 保持 1 worker 以维持语义。
PUBLISH_MINUTE_MAX = int(os.environ.get('GALLERY_PUBLISH_PER_MIN', '6'))
PUBLISH_HOUR_MAX = int(os.environ.get('GALLERY_PUBLISH_PER_HOUR', '40'))
PUBLISH_RATE_ENABLED = os.environ.get('GALLERY_PUBLISH_RATE', '1') != '0'
_publish_hits = {}  # ip -> [时间戳]


def _publish_rate_ok(ip):
    """检查限流并记录一次成功发布；返回 False 表示应拒绝（429）。"""
    # 测试环境跳过（所有用例同 IP，不该互相挤占额度）；生产可用环境变量整体关闭
    if app.config.get('TESTING') or not PUBLISH_RATE_ENABLED:
        return True
    now = time.time()
    hits = [t for t in _publish_hits.get(ip, []) if now - t < 3600]
    if sum(1 for t in hits if now - t < 60) >= PUBLISH_MINUTE_MAX:
        _publish_hits[ip] = hits
        return False
    if len(hits) >= PUBLISH_HOUR_MAX:
        _publish_hits[ip] = hits
        return False
    hits.append(now)
    _publish_hits[ip] = hits
    # 桶太多时清掉长时间不活跃的 IP，避免内存无上限增长
    if len(_publish_hits) > 2000:
        for k in [k for k, v in _publish_hits.items() if not v or now - v[-1] > 3600]:
            _publish_hits.pop(k, None)
    return True


# ── 链接导入频率限制（2026-11-27）────────────────────────────────────────────
# /api/import 会同步抓取外部 URL（单次最长 12s，还会内联图片）。gunicorn 单 worker，
# 匿名并发慢请求即可占满 worker 造成全站不可用。独立计数，不与发布额度互相挤占。
IMPORT_MINUTE_MAX = int(os.environ.get('GALLERY_IMPORT_PER_MIN', '10'))
IMPORT_HOUR_MAX = int(os.environ.get('GALLERY_IMPORT_PER_HOUR', '60'))
IMPORT_RATE_ENABLED = os.environ.get('GALLERY_IMPORT_RATE', '1') != '0'
_import_hits = {}  # ip -> [时间戳]


def _import_rate_ok(ip):
    """检查导入限流并记录一次成功调用；返回 False 表示应拒绝（429）。"""
    if app.config.get('TESTING') or not IMPORT_RATE_ENABLED:
        return True
    now = time.time()
    hits = [t for t in _import_hits.get(ip, []) if now - t < 3600]
    if sum(1 for t in hits if now - t < 60) >= IMPORT_MINUTE_MAX:
        _import_hits[ip] = hits
        return False
    if len(hits) >= IMPORT_HOUR_MAX:
        _import_hits[ip] = hits
        return False
    hits.append(now)
    _import_hits[ip] = hits
    if len(_import_hits) > 2000:
        for k in [k for k, v in _import_hits.items() if not v or now - v[-1] > 3600]:
            _import_hits.pop(k, None)
    return True


# ── 过期策略（2026-11-26 更新）────────────────────────────────────────────────
# 过期只影响「对外可见性」（访客 410），**仍不做任何自动删除**：
#   1) 过期作品进入「回收站」（惰性判定：share_expires_at 已过，不落额外状态字段）；
#   2) 由管理员在 /admin/trash 人工确认：保留（转永久）或删除（级联清点赞/评论/举报）；
#   3) 作者续期、注册升级、管理员保留后，作品自动离开回收站；
#   4) 因此仍禁止新增基于 share_expires_at 的定时 DELETE / 归档 / 软删任务——
#      删除只能由作者主动操作或管理员在回收站逐条确认。
def _is_expired(page):
    """到期判断：NULL 视为永久；坏数据不误伤（视为未到期）。到期≠删除，见上方策略。"""
    raw = page['share_expires_at']
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) <= datetime.now()
    except (ValueError, TypeError):
        return False


# ── 可见性三态 ────────────────────────────────────────────────────────────────
VISIBILITIES = ('public', 'unlisted', 'private')
VIS_LABELS = {'public': '公开', 'unlisted': '仅链接', 'private': '私密'}
EXPIRY_WARN_HOURS = 24  # 到期前提醒窗口


def _visibility(page):
    """读取可见性，兼容没有该字段的行：is_public 回退为 public/private。"""
    try:
        val = page['visibility']
    except (KeyError, IndexError):
        val = None
    if val in VISIBILITIES:
        return val
    return 'public' if page['is_public'] else 'private'


def _vis_from_form(form):
    """表单解析：优先 visibility 三态；老表单只有 is_public 复选框。"""
    vis = (form.get('visibility') or '').strip()
    if vis in VISIBILITIES:
        return vis
    return 'public' if form.get('is_public') else 'private'


def _hours_left(page):
    """距到期的小时数（向上取整，4h59m 显示 5 小时）；永久/坏数据返回 None。"""
    raw = page['share_expires_at']
    if not raw:
        return None
    try:
        secs = (datetime.fromisoformat(raw) - datetime.now()).total_seconds()
    except (ValueError, TypeError):
        return None
    return int(-(-secs // 3600))

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

# 保留用户名：禁止注册，防抢注提权（tojoevan 会被 migrate_db 自动置为管理员）
# 与功能/系统账号混淆（admin、support 等）。大小写不敏感比对。
RESERVED_USERNAMES = {
    'tojoevan',                                             # 项目所有者（自动管理员）
    'admin', 'administrator', 'root', 'superuser', 'sysadmin',
    'system', 'sys', 'support', 'service', 'webmaster', 'postmaster',
    'noreply', 'no-reply', 'contact', 'info', 'help', 'team',
    'api', 'www', 'web', 'mail', 'bot', 'gallery', 'official',
    'guest', 'anonymous', 'test', 'demo', 'null', 'undefined',
}


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
        elif username.lower() in RESERVED_USERNAMES:
            error = '该用户名是系统保留字，请换一个'
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
                        '''UPDATE users SET username=?, email=?, password=?, handle=?, is_guest=0,
                           recovery_code=NULL WHERE id=?''',
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
                session.permanent = True
                return redirect(url_for('dashboard'))
        return render_template('auth.html', mode='register', error=error)
    return render_template('auth.html', mode='register')

def _safe_next(url, default):
    """只接受站内相对路径；协议相对地址与反斜杠变体一律回退默认。

    不能用 urlparse 判定：浏览器会把 ///evil.com、/\\evil.com 等按协议相对地址
    解析，但 urlparse 视其为 path（netloc 为空）。改用严格白名单，按浏览器的
    解析规则从源头拒绝。
    """
    if not url or not isinstance(url, str):
        return default
    if not url.startswith('/'):
        return default
    if url.startswith('//') or url.startswith('/\\'):
        return default
    if '\\' in url or any(ord(c) < 0x20 for c in url):  # 反斜杠/控制字符会被浏览器规范化
        return default
    return url


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
            session.permanent = True
            next_url = _safe_next(request.args.get('next'), url_for('dashboard'))
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
    user = db.execute('SELECT id, username, email, handle, password, show_fission, is_guest FROM users WHERE id=?', (session['user_id'],)).fetchone()
    is_guest = bool(user['is_guest']) if user else False

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
                return render_template('settings.html', user=user, error=error, is_guest=is_guest)
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
                return render_template('settings.html', user=user, error=error, is_guest=is_guest)
            
            if email and '@' not in email:
                error = '邮箱格式不正确'
            if not error:
                db.execute('UPDATE users SET email=? WHERE id=?', (email, session['user_id']))
                db.commit()
                flash('邮箱已更新')
                return redirect(url_for('settings'))
            return render_template('settings.html', user=user, error=error, is_guest=is_guest)
        
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
            return render_template('settings.html', user=user, error=error, is_guest=is_guest)
    
    return render_template('settings.html', user=user, is_guest=is_guest)

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    rows = db.execute(
        'SELECT p.*, u.handle FROM pages p JOIN users u ON u.id=p.user_id'
        ' WHERE p.user_id=? ORDER BY p.updated DESC',
        (session['user_id'],)
    ).fetchall()
    pages = []
    for row in rows:
        item = dict(row)
        item['days_left'] = None
        item['hours_left'] = None
        item['visibility'] = _visibility(row)
        if item.get('share_expires_at'):
            try:
                delta = datetime.fromisoformat(item['share_expires_at']) - datetime.now()
                item['days_left'] = max(0, delta.days + (1 if delta.total_seconds() > 0 else 0))
            except (ValueError, TypeError):
                item['days_left'] = None
            item['hours_left'] = _hours_left(row)
        item['renew_left'] = _renew_left(row) if item.get('share_expires_at') else None
        pages.append(item)
    # 到期提醒窗口：24 小时内到期；已过期的也纳入（提示是「已失效」而不是「将到期」）
    expiring = [p for p in pages
                if p['hours_left'] is not None and p['hours_left'] <= EXPIRY_WARN_HOURS]
    expired_count = sum(1 for p in expiring if p['hours_left'] <= 0)
    me = db.execute('SELECT recovery_code FROM users WHERE id=?', (session['user_id'],)).fetchone()
    is_admin = _is_admin(session['user_id'])
    return render_template('dashboard.html', pages=pages, expiring=expiring,
                           expired_count=expired_count,
                           expiring_hours=EXPIRY_WARN_HOURS,
                           is_admin=is_admin,
                           trash_count=_trash_count(db) if is_admin else 0,
                           is_guest=_is_guest(db, session['user_id']),
                           has_recovery=bool(me and me['recovery_code']),
                           recovered=bool(request.args.get('recovered')),
                           switched=bool(request.args.get('switched')),
                           trial=bool(request.args.get('trial')),
                           badges=_badges(session['user_id']))


@app.route('/recovery-code', methods=['POST'])
@identity_required
def recovery_code():
    """为当前游客生成/重置找回码；明文只在本页展示一次。"""
    db = get_db()
    uid = session['user_id']
    if not _is_guest(db, uid):
        return redirect(url_for('dashboard'))
    resp = make_response(render_template('recovery_code.html',
                                         code=_new_recovery_code(db, uid)))
    resp.headers['Cache-Control'] = 'no-store'  # 找回码不留在浏览器缓存/历史里的副本
    return resp


@app.route('/recover', methods=['GET', 'POST'])
def recover():
    """用找回码恢复游客身份（换设备 / 清 Cookie 后）。"""
    error = None
    switched = False
    if request.method == 'POST':
        ip = request.remote_addr or '-'
        if not _recover_rate_ok(ip):
            error = '尝试次数过多，请 10 分钟后再试'
        else:
            digest = _recovery_hash(request.form.get('code', ''))
            db = get_db()
            row = db.execute('SELECT id, username, is_guest FROM users WHERE recovery_code=?',
                             (digest,)).fetchone() if digest else None
            if not row or not row['is_guest']:
                _recover_hit(ip)  # 只有失败才计数，成功一次不会占掉别人的额度
                error = '找回码无效，或对应的临时身份已被注册升级'
            else:
                _recover_hits.pop(ip, None)
                # 当前浏览器若是另一个游客且名下有作品，切换后那些作品就不再显示，先告知
                cur = session.get('user_id')
                if cur and cur != row['id'] and _is_guest(db, cur):
                    switched = bool(db.execute('SELECT 1 FROM pages WHERE user_id=? LIMIT 1',
                                               (cur,)).fetchone())
                session['user_id'] = row['id']
                session['username'] = row['username']
                session.permanent = True
                url = url_for('dashboard') + '?recovered=1' + ('&switched=1' if switched else '')
                return redirect(url)
    return render_template('recover.html', error=error)


def _extend_expiry(user_id, db, page_id):
    """续期：从现在与现有到期时间中较晚者起算 3 天，避免提前续期反而缩短有效期。"""
    row = db.execute('SELECT share_expires_at FROM pages WHERE id=?', (page_id,)).fetchone()
    now = datetime.now()
    base = now
    if row and row['share_expires_at']:
        try:
            current = datetime.fromisoformat(row['share_expires_at'])
            if current > base:
                base = current
        except (ValueError, TypeError):
            pass
    expiry = (base + timedelta(days=TRIAL_DAYS)).isoformat()
    db.execute('UPDATE pages SET share_expires_at=?, updated=?, renew_count=COALESCE(renew_count,0)+1'
               ' WHERE id=?',
               (expiry, now.isoformat(), page_id))
    return expiry


def _renew_left(page):
    """该作品还剩几次续期额度；非游客作品（永久）返回 None。"""
    if not page['share_expires_at']:
        return None
    used = page['renew_count'] if 'renew_count' in page.keys() else 0
    return max(0, RENEW_MAX_TIMES - (used or 0))


@app.route('/renew/<int:page_id>', methods=['POST'])
@login_required
def renew_page(page_id):
    """匿名作品续期 3 天；注册/管理员作品永久无需续期，游客续期有次数上限。"""
    db = get_db()
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or page['user_id'] != session['user_id']:
        abort(403)
    if not page['share_expires_at']:
        return redirect(url_for('dashboard'))
    if _renew_left(page) == 0:
        flash('这个作品的免登录续期次数已用完（共 %d 次）。注册账号后作品永久保留。'
              % RENEW_MAX_TIMES)
        return redirect(url_for('dashboard'))
    if _expiry_for(session['user_id'], db):
        _extend_expiry(session['user_id'], db, page_id)
        db.commit()
    return redirect(url_for('dashboard'))


@app.route('/renew_all', methods=['POST'])
@login_required
def renew_all_expiring():
    """一键续期：把 24 小时内到期的作品统一延后 3 天（受续期额度限制）。"""
    db = get_db()
    if _expiry_for(session['user_id'], db):
        rows = db.execute(
            'SELECT * FROM pages WHERE user_id=? AND share_expires_at IS NOT NULL',
            (session['user_id'],)
        ).fetchall()
        renewed = used_up = 0
        for row in rows:
            if _hours_left(row) is None or _hours_left(row) > EXPIRY_WARN_HOURS:
                continue
            if _renew_left(row) == 0:
                used_up += 1
                continue
            _extend_expiry(session['user_id'], db, row['id'])
            renewed += 1
        db.commit()
        if renewed:
            flash('已为 %d 个作品各延长 %d 天。' % (renewed, TRIAL_DAYS))
        if used_up:
            flash('%d 个作品已达免登录续期上限，注册账号后可永久保留。' % used_up)
    return redirect(url_for('dashboard'))

# ── Create / Edit ─────────────────────────────────────────────────────────────

MAX_HTML_BYTES = 3 * 1024 * 1024  # 3MB 后端硬限制（前端已有校验，此处防绕过）

# ── 标题规范化（SEO P2，2026-11-27）──────────────────────────────────────────
# 导入源常带来全角 ASCII、连续空白与控制字符，进同一作品页会显得杂乱、
# 也不利于标题作为 og:title / 搜索结果摘要。统一在写入前规范化，不改用户可见语义。
# str.translate 的 key 必须是码位整数，不是字符
_FULLWIDTH_ASCII = {0xFF01 + i: chr(0x21 + i) for i in range(0x5E)}
_FULLWIDTH_ASCII[0x3000] = ' '  # 全角空格


def _normalize_title(title):
    if not title:
        return '未命名'
    s = str(title).translate(_FULLWIDTH_ASCII)
    s = ''.join(ch if ch >= ' ' else ' ' for ch in s)  # 控制字符转空格
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120] or '未命名'


def _check_submission(title, html_source, description):
    """统一校验：3MB 上限 + 敏感词扫描。返回错误文案或 None。"""
    if len(html_source.encode('utf-8')) > MAX_HTML_BYTES:
        return 'HTML 文件不能超过 3MB'
    hit, word, field = moderation.scan(title, description, html_source)
    if hit:
        return f'内容未通过审核：{field}含敏感词「{word}」，请修改后重试'
    return None


def _insert_page(title, html_source, description, visibility, db):
    """创建作品的唯一写入口：/new 与批量发布共用，避免逻辑漂移。

    visibility 为三态 public / unlisted / private。
    """
    now = datetime.now().isoformat()
    slug = _new_slug(db)
    expiry = _expiry_for(session['user_id'], db)
    title = _normalize_title(title)
    # is_public 与 visibility 同步写：前者维持「进画廊」旧语义，首页查询无需改动
    db.execute(
        '''INSERT INTO pages (user_id,title,slug,html_source,description,is_public,visibility,
                              review_status,share_expires_at,created,updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (session['user_id'], title, slug, html_source, description,
         1 if visibility == 'public' else 0, visibility, 'approved',
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
        visibility = _vis_from_form(request.form)
        error = _check_submission(title, html_source, description)
        if error:
            return render_template('editor.html', page={'title': title, 'html_source': html_source,
                                                          'description': description,
                                                          'visibility': visibility,
                                                          'is_public': 1 if visibility == 'public' else 0},
                                   mode='new', error=error)
        if not _publish_rate_ok(request.remote_addr or 'unknown'):
            return render_template('editor.html', page={'title': title, 'html_source': html_source,
                                                          'description': description,
                                                          'visibility': visibility,
                                                          'is_public': 1 if visibility == 'public' else 0},
                                   mode='new', error='发布太频繁，请稍后再试（每分钟最多 6 次）。'), 429
        db = get_db()
        _insert_page(title, html_source, description, visibility, db)
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
        visibility = _vis_from_form(request.form)
        error = _check_submission(title, html_source, description)
        if error:
            return render_template('editor.html', page=dict(page, title=title, html_source=html_source,
                                                            description=description,
                                                            visibility=visibility,
                                                            is_public=1 if visibility == 'public' else 0),
                                   mode='edit', error=error)
        now = datetime.now().isoformat()
        title = _normalize_title(title)
        # 内容变更后已重新扫描通过，恢复审核状态（举报导致的 rejected 也随新内容解除）
        db.execute(
            '''UPDATE pages SET title=?,html_source=?,description=?,is_public=?,visibility=?,
                               review_status='approved',updated=? WHERE id=?''',
            (title, html_source, description, 1 if visibility == 'public' else 0,
             visibility, now, page_id)
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
    _delete_pages_cascade(db, [page_id])
    db.commit()
    return redirect(url_for('dashboard'))


@app.route('/privacy')
def privacy():
    """隐私与数据说明：存什么、谁能看、保留多久、怎么删。"""
    return render_template('privacy.html')


@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    """注销账号：连同作品、点赞、评论、举报一并删除，不可恢复。

    注册用户需验证密码；游客账号没有可用密码（注册时随机生成、用户并不知晓），
    因此只要求键入 DELETE 确认，避免临时身份的数据无法删除。
    """
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        session.clear()
        return redirect(url_for('index'))
    if user['is_admin']:
        flash('管理员账号不能在此自助删除')
        return redirect(url_for('settings'))
    if (request.form.get('confirm') or '').strip().upper() != 'DELETE':
        flash('请在确认框内输入 DELETE 后再提交')
        return redirect(url_for('settings'))
    if not user['is_guest'] and not check_password_hash(
            user['password'], request.form.get('password') or ''):
        flash('密码错误，账号未删除')
        return redirect(url_for('settings'))

    page_ids = [r['id'] for r in db.execute('SELECT id FROM pages WHERE user_id=?', (uid,))]
    if page_ids:
        marks = ','.join('?' * len(page_ids))
        db.execute('DELETE FROM likes WHERE page_id IN (%s)' % marks, page_ids)
        db.execute('DELETE FROM comments WHERE page_id IN (%s)' % marks, page_ids)
        db.execute('DELETE FROM reports WHERE page_id IN (%s)' % marks, page_ids)
    db.execute('DELETE FROM likes WHERE user_id=?', (uid,))
    db.execute('DELETE FROM comments WHERE user_id=?', (uid,))
    db.execute('DELETE FROM reports WHERE reporter_id=?', (uid,))
    db.execute('DELETE FROM pages WHERE user_id=?', (uid,))
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    session.clear()
    flash('账号与全部作品已删除')
    return redirect(url_for('index'))

# ── Preview ───────────────────────────────────────────────────────────────────

def _can_access(page):
    """与 _page_or_403 同口径的访问判断，供 API 等非渲染路径复用。

    审核未通过 / 私密 / 已过期：作者与管理员不受限，其余一律拒绝。
    """
    is_owner = 'user_id' in session and session['user_id'] == page['user_id']
    is_admin = 'user_id' in session and _is_admin(session['user_id'])
    if is_owner or is_admin:
        return True
    if page['review_status'] != 'approved':
        return False
    if _visibility(page) == 'private':
        return False
    if 'share_expires_at' in page.keys() and _is_expired(page):
        return False
    return True


AMBIGUOUS_SLUG = 'ambiguous-slug'


def _page_or_403(slug, handle=None):
    """按短码（或旧 slug 别名）定位作品。

    新作品一律 7 位短码、全站唯一，裸 /p/<code> 即可定位。旧的中文 slug 保留在
    page_aliases 中，旧链接照样能打开（视图层再 301 到规范地址）。历史 ASCII slug
    仍是「用户内唯一」，同名时按下面的优先级消歧：
      1. 显式 handle（/<handle>/<slug>）
      2. 查询串 ?u=<handle>（旧卡片链接自带，最常用）
      3. 当前登录者自己的作品
      4. 全站唯一
      5. 仍有多条 → 明确报歧义，交给错误页提示改用完整链接
    """
    db = get_db()
    if handle is None:
        handle = (request.args.get('u') or '').strip().lower() or None
    alias = ' OR p.id IN (SELECT page_id FROM page_aliases WHERE slug=?)'
    if handle:
        page = db.execute(
            'SELECT p.*, u.username, u.handle FROM pages p JOIN users u ON p.user_id=u.id'
            ' WHERE u.handle=? AND (p.slug=?' + alias + ')',
            (handle, slug, slug)
        ).fetchone()
        if not page:
            abort(404)
    else:
        rows = db.execute(
            'SELECT p.*, u.username, u.handle FROM pages p JOIN users u ON p.user_id=u.id'
            ' WHERE p.slug=?' + alias + ' ORDER BY p.id',
            (slug, slug)
        ).fetchall()
        if not rows:
            abort(404)
        if len(rows) > 1:
            me = session.get('user_id')
            mine = [r for r in rows if r['user_id'] == me] if me else []
            rows = mine or rows
        if len(rows) > 1:
            abort(404, description=AMBIGUOUS_SLUG)
        page = rows[0]
    is_owner = 'user_id' in session and session['user_id'] == page['user_id']
    is_admin = 'user_id' in session and _is_admin(session['user_id'])
    # 审核未通过/待复审：对外一律 404（不暴露存在），作者与管理员仍可见
    if page['review_status'] != 'approved' and not (is_owner or is_admin):
        abort(404)
    # 可见性：private 仅作者/管理员；unlisted 与 public 对外都可访问，
    # 区别只在是否出现在首页画廊（由 is_public=1 控制，unlisted 时为 0）
    if _visibility(page) == 'private' and not (is_owner or is_admin):
        abort(403)
    # 分享有效期：到期后对外 410（数据不删），作者与管理员不受限
    if 'share_expires_at' in page.keys() and _is_expired(page) and not (is_owner or is_admin):
        abort(410)
    return page

@app.errorhandler(410)
def gone(error):
    return render_template('gone.html'), 410


@app.errorhandler(403)
def forbidden(error):
    """私密作品被他人打开：给出可读说明，而不是 Werkzeug 的英文默认页。"""
    return render_template(
        'error.html', code=403, title='这是一个私密作品',
        message='只有作者本人登录后可以查看。如果你是作者，请用发布作品的账号登录。',
        show_login=True), 403


@app.errorhandler(404)
def not_found(error):
    """404 统一落地到品牌化页面；slug 撞名时给出「链接有歧义」的专门说明。"""
    if getattr(error, 'description', '') == AMBIGUOUS_SLUG:
        return render_template(
            'error.html', code=404, title='链接有歧义',
            message='这个地址对应多份同名作品，无法确定是哪一份。'
                    '请使用完整的分享链接（形如 /作者标识/作品名称），或从首页重新点入。',
            show_login=False), 404
    return render_template(
        'error.html', code=404, title='页面不存在',
        message='链接可能已删除、拼写有误，或作品尚未通过审核。',
        show_login=False), 404


@app.context_processor
def inject_nav_ctx():
    """统一导航所需上下文：是否管理员、回收站待处理数（仅管理员才查）。"""
    uid = session.get('user_id')
    is_admin = bool(uid and _is_admin(uid))
    count = 0
    if is_admin:
        try:
            count = _trash_count(get_db())
        except Exception:
            count = 0
    return dict(nav_is_admin=is_admin, nav_trash_count=count)


@app.template_global()
def page_url(page, **kwargs):
    """作品页面链接：新作品是全局唯一短码，裸 /p/<code> 即可定位（最短）；
    历史 ASCII slug 仍是用户内唯一，保留 ?u= 消歧。"""
    try:
        handle = page['handle']
    except (KeyError, IndexError):
        handle = None
    slug = page['slug']
    if _is_short_slug(slug):
        return url_for('preview', slug=slug, **kwargs)
    return url_for('preview', slug=slug, u=handle, **kwargs)


def _owner_expiry_ctx(page):
    """渲染上下文：可见性 + 仅作者可见的到期提醒。

    /p、/view、/<handle>/<slug> 三个入口共用，避免只有 /p 有提醒、
    而用户实际分享出去的是 /<handle>/<slug>（纯净分享）那种情况。
    """
    is_owner = session.get('user_id') == page['user_id']
    hours_left = _hours_left(page)
    expired = _is_expired(page)
    return {
        'is_owner': is_owner,
        'visibility': _visibility(page),
        'expiry_warn': (hours_left if (is_owner and hours_left is not None
                                       and hours_left <= EXPIRY_WARN_HOURS) else None),
        'expiry_expired': is_owner and expired,
        # 过期作品仅作者可见：作者需要更醒目的提示，访客根本打不开
    }


# ── SEO / GEO 基础设施（2026-11-27）──────────────────────────────────────────
# 作品正文在 iframe 里，搜索引擎与 AI 引擎看不到。给「公开 + 已审核 + 未过期」
# 的作品生成一份纯文本摘要留在主文档，爬虫才读得到；私密/仅链接/待审/过期一律不生成。
def _indexable_pages(db):
    """可被搜索引擎收录的作品行：public + approved + 未过期。"""
    rows = db.execute(
        'SELECT p.*, u.handle FROM pages p JOIN users u ON u.id=p.user_id '
        "WHERE p.visibility='public' AND p.review_status='approved' "
        'ORDER BY p.id'
    ).fetchall()
    return [r for r in rows if not _is_expired(r)]


def _is_indexable(page):
    return (_visibility(page) == 'public'
            and page['review_status'] == 'approved'
            and not _is_expired(page))


def _plain_text(source, limit=1500):
    """从 HTML 里剥出可读文本：去掉 script/style/svg 内容与所有标签，
    还原实体、压缩空白，超长截断（尽量断在句末）。data URI 图片不产生文本。"""
    if not source:
        return ''
    s = re.sub(r'<(script|style|noscript|svg)\b[^>]*>.*?</\1>', ' ', source,
               flags=re.I | re.S)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = _html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > limit:
        cut = s[:limit]
        p = max(cut.rfind('。'), cut.rfind('！'), cut.rfind('？'), cut.rfind('. '))
        s = (cut[:p + 1] if p > limit * 0.6 else cut).rstrip() + '…'
    return s


def _html_with_alt(source, title):
    """展示层补全图片 alt（SEO P2）：只给缺 alt 的 <img> 补，不改库中源码。
    优先取 title 属性，其次 src 文件名，最后用作品标题兜底。"""
    if not source:
        return source
    fallback = _html.escape((title or '图片').strip()[:80] or '图片', quote=True)

    def _fix(m):
        tag = m.group(0)
        if re.search(r'\balt\s*=', tag, re.I):  # 已有 alt（含 alt=""）则尊重原样
            return tag
        tm = re.search(r'\btitle\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if tm and tm.group(1).strip():
            alt = _html.escape(tm.group(1).strip()[:120], quote=True)
        else:
            sm = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            name = ''
            if sm:
                name = os.path.basename(_html.unescape(sm.group(1)).split('?')[0])
                name = os.path.splitext(name)[0]
            alt = _html.escape(name.strip()[:120], quote=True) if name.strip() else fallback
        # 插在 <img 之后，保留其余属性与顺序
        return re.sub(r'^<img', '<img alt="%s"' % alt, tag, count=1, flags=re.I)

    return re.sub(r'<img\b[^>]*>', _fix, source, flags=re.I)


def _seo_ctx(page):
    """作品页的 SEO 上下文：只有可索引作品才给文本层与 CreativeWork 数据。"""
    if not _is_indexable(page):
        return {'page_text': '', 'seo_ld': '', 'canonical': SITE_URL + url_for('preview', slug=page['slug']),
                'og_image': SITE_URL + '/static/og-default.png'}
    text = _plain_text(page['html_source'])
    canonical = SITE_URL + url_for('preview', slug=page['slug'])
    desc = (page['description'] or '').strip()
    if not desc or desc.startswith('从链接导入'):
        desc = (page['title'] + ' · 一个可交互的 HTML 作品。' + text[:110])
    import json
    ld = {
        '@context': 'https://schema.org',
        '@type': 'CreativeWork',
        'name': page['title'],
        'description': desc[:300],
        'author': {'@type': 'Person', 'name': page['handle'] or '匿名作者'},
        'datePublished': page['created'],
        'dateModified': page['updated'] or page['created'],
        'url': canonical,
    }
    # 模板以 |safe 输出 JSON-LD，必须把 < > & 转成 JSON 的 \uXXXX 转义：
    # 否则标题里的 </script> 会闭合 script 标签形成 XSS（转义后 JSON 仍合法、浏览器不会截断）。
    ld_json = json.dumps(ld, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return {
        'page_text': text,
        'seo_ld': ld_json,
        'canonical': canonical,
        'og_image': SITE_URL + url_for('og_image', slug=page['slug']),
    }


@app.route('/og/<slug>.png')
def og_image(slug):
    """动态分享封面：仅对可索引作品生成，其余 404 落到静态默认图。"""
    db = get_db()
    page = db.execute('SELECT p.*, u.handle FROM pages p JOIN users u ON p.user_id=u.id'
                      ' WHERE p.slug=?', (slug,)).fetchone()
    if not page or not _is_indexable(page):
        abort(404)
    try:
        import og_image as _og
        png = _og.generate(page['title'], page['handle'] or '')
    except Exception:
        # 字体/Pillow 异常不应影响作品页，回退到静态图由模板处理
        current_app.logger.warning('OG 封面生成失败 slug=%s', slug, exc_info=True)
        abort(404)
    resp = make_response(png)
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/robots.txt')
def robots_txt():
    body = (
        'User-agent: *\n'
        'Allow: /\n'
        'Disallow: /admin\n'
        'Disallow: /dashboard\n'
        'Disallow: /settings\n'
        'Disallow: /edit/\n'
        'Disallow: /new\n'
        'Disallow: /recover\n'
        'Disallow: /recovery-code\n'
        'Disallow: /delete\n'
        'Disallow: /renew\n'
        f'\nSitemap: {SITE_URL}/sitemap.xml\n'
    )
    resp = make_response(body)
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/sitemap.xml')
def sitemap_xml():
    from xml.sax.saxutils import escape as _xml_escape
    db = get_db()
    entries = []
    for p in _indexable_pages(db):
        loc = SITE_URL + url_for('preview', slug=p['slug'])
        lastmod = (p['updated'] or p['created'] or '')[:10]
        entries.append(
            '  <url><loc>%s</loc>%s</url>' % (
                _xml_escape(loc),
                '<lastmod>%s</lastmod>' % _xml_escape(lastmod) if lastmod else '',
            )
        )
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + '\n'.join(entries) + '\n</urlset>\n')
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/xml; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/llms.txt')
def llms_txt():
    db = get_db()
    count = len(_indexable_pages(db))
    body = f"""# HTML Gallery

> 一个把 HTML 片段、AI 对话生成的网页作品发布成可分享链接的平台。访客可在浏览器里直接打开并交互，无需登录。

本站收录 {count} 个公开作品。作品正文通过 iframe 隔离运行以保证安全；每个作品页提供纯文本摘要，便于阅读与引用。

## 站点用途

- 发布：用户粘贴一段 HTML 源码，或一条 AI 对话分享链接，即可生成一个永久或临时的作品页。
- 浏览：公开作品进入首页画廊，访客可直接查看、点赞、评论。
- 分享：每个作品有短链接（/p/短码）与作者专属链接（/作者标识/短码）。

## 支持的来源

- 直接粘贴 HTML 源码。
- 支持从主流 AI 对话工具的分享链接导入（对话记录会转排为可阅读的图文页）。

## 典型用法

- 想看某个作品的内容：打开作品页的「文本内容」区域，可复制文字。
- 想发布作品：首页点「+ 新建」，粘贴源码或链接，选择可见性后发布。
- 想找回临时作品：登录页点「用找回码恢复」，输入发布时生成的找回码。

## 索引说明

- 仅「公开 + 已审核 + 未过期」的作品可被收录；仅持链接与私密作品不收录。
- 完整作品清单见 {SITE_URL}/sitemap.xml。
"""
    resp = make_response(body)
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/p/<slug>')
def preview(slug):
    page = _page_or_403(slug)
    if page['slug'] != slug:  # 旧链接 / 别名：301 到短码，规范地址只此一份
        extra = {'embed': 1} if request.args.get('embed') else {}
        return redirect(url_for('preview', slug=page['slug'], **extra), 301)
    if request.args.get('embed'):
        # 首页缩略图专用：只吐作品源码本身，不带站点外壳。
        # 卡片用 <iframe src="?embed=1"> 而不是内联 srcdoc，首页 HTML 不会随作品体积膨胀，
        # 也就没有「图文内容较大」的占位卡了。lazy 加载 + 短缓存，重复访问不吃流量。
        #
        # 安全：这个端点吐的是原始用户 HTML，若被当顶层页面打开即与站点同源，脚本可
        # fetch 站内 API（带 SameSite=Lax cookie）。两层防护：
        #   1) CSP sandbox allow-scripts：无论顶层/嵌入都把文档降为 opaque origin，
        #      脚本能跑（缩略动画不受影响），但读不到站点 cookie / API。
        #   2) Sec-Fetch-Dest=document（浏览器顶层导航）直接 403；iframe 请求或无此头
        #      （老浏览器/爬虫）放行，靠第 1 层兜底。
        if request.headers.get('Sec-Fetch-Dest') == 'document':
            abort(403)
        resp = make_response(page['html_source'] or '')
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'public, max-age=600'
        resp.headers['X-Robots-Tag'] = 'noindex'
        resp.headers['Content-Security-Policy'] = 'sandbox allow-scripts'
        # 缓存须按 Sec-Fetch-Dest 区分：否则 iframe 的 200 会被 document 请求复用，
        # 绕过上面的 403 分支（CSP 仍在缓存里兜底，但两层防护应各自生效）。
        resp.headers['Vary'] = 'Sec-Fetch-Dest'
        return resp
    return render_template(
        'preview.html', page=page,
        html_source=_html_with_alt(page['html_source'], page['title']),
        current_user_id=session.get('user_id'),
        **_owner_expiry_ctx(page), **_seo_ctx(page)
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
    if page['slug'] != slug:
        return redirect(url_for('view', slug=page['slug']), 301)
    ctx = _fission_ctx(get_db(), page)
    return render_template('view.html', page=page, html_source=_html_with_alt(page['html_source'], page['title']),
                           **_owner_expiry_ctx(page), **ctx, **_seo_ctx(page))

# ── API: 从链接导入作品源码 ──────────────────────────────────────────────────
# 只抓取并审核后返回前端，不入库；真正发布仍走 /new，复用既有 3MB + 敏感词校验。

@app.route('/api/import', methods=['POST'])
@identity_required
def api_import():
    # 限流在抓取之前，避免慢请求堆积占满单 worker；身份由 identity_required 建立。
    if not _import_rate_ok(request.remote_addr or ''):
        return jsonify(ok=False, error='导入太频繁，请稍后再试'), 429
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
        # 三态可见性；老客户端只传 is_public 时按 public/private 回退
        vis = (item.get('visibility') or '').strip()
        if vis not in VISIBILITIES:
            vis = 'public' if item.get('is_public', True) else 'private'
        error = _check_submission(title, html_source, description)
        if error:
            return jsonify(ok=False, error=error), 400
        prepared.append((title, html_source, description, vis))
    if not _publish_rate_ok(request.remote_addr or 'unknown'):
        return jsonify(ok=False, error='发布太频繁，请稍后再试（每分钟最多 6 次）。'), 429
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
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or not _can_access(page):
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
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or not _can_access(page):
        return jsonify({'error': '页面不存在'}), 404
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
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or not _can_access(page):
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
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    if not page or not _can_access(page):
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
    page = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
    # 可见性/过期先拦（不可见即 404）；复审状态单独判断，重复举报要给出可读提示
    if not page or (_visibility(page) == 'private' and page['user_id'] != session.get('user_id')) \
            or (_is_expired(page) and page['user_id'] != session.get('user_id')):
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


# ── 回收站：过期作品待管理员确认保留或删除 ─────────────────────────────────────
# 入站是惰性判定（share_expires_at 已过即入），不新增状态字段：
# 作者续期、注册升级、管理员「保留」或「删除」后，作品自然离开列表。
# 只支持逐条处置，没有定时批量删除——删除必须有人按下去。

def _trash_rows(db):
    """回收站候选：所有带到期时间的作品（数量很少，逐行用 _is_expired 判定）。"""
    return db.execute(
        '''SELECT p.id, p.title, p.slug, p.visibility, p.is_public, p.user_id,
                  p.share_expires_at, p.created, p.updated, p.renew_count,
                  length(CAST(p.html_source AS BLOB)) AS size_bytes,
                  u.handle, u.username, u.is_guest,
                  (SELECT COUNT(*) FROM likes l WHERE l.page_id=p.id) AS like_count,
                  (SELECT COUNT(*) FROM comments c WHERE c.page_id=p.id) AS comment_count
           FROM pages p JOIN users u ON u.id=p.user_id
           WHERE p.share_expires_at IS NOT NULL
           ORDER BY p.share_expires_at ASC'''
    ).fetchall()


def _trash_pages(db):
    """已过期、待在回收站处理的作品，附带过期时长与体积信息。"""
    now = datetime.now()
    items = []
    for row in _trash_rows(db):
        if not _is_expired(row):
            continue
        item = dict(row)
        item['visibility'] = _visibility(row)
        try:
            hours = int((now - datetime.fromisoformat(row['share_expires_at'])).total_seconds() // 3600)
        except (ValueError, TypeError):
            hours = None
        item['expired_hours'] = hours
        item['expired_days'] = None if hours is None else hours // 24
        items.append(item)
    return items


def _trash_count(db):
    """回收站待处理数量（看板入口用，不读 html_source，避免每次看板搬大字段）。"""
    rows = db.execute('SELECT share_expires_at FROM pages WHERE share_expires_at IS NOT NULL')
    return sum(1 for r in rows if _is_expired(r))


@app.route('/admin/trash')
@login_required
def admin_trash():
    if not _is_admin(session['user_id']):
        abort(403)
    return render_template('trash.html', items=_trash_pages(get_db()))


@app.route('/admin/trash/<int:page_id>/<action>', methods=['POST'])
@login_required
def admin_trash_action(page_id, action):
    """回收站处置：keep=保留并转永久；delete=级联删除（点赞/评论/举报一并清）。"""
    if not _is_admin(session['user_id']):
        return jsonify({'error': '无权限'}), 403
    if action not in ('keep', 'delete'):
        return jsonify({'error': '无效操作'}), 400
    db = get_db()
    page = db.execute('SELECT id, title, share_expires_at FROM pages WHERE id=?',
                      (page_id,)).fetchone()
    if not page:
        return jsonify({'error': '作品不存在，可能已被作者删除'}), 404
    if action == 'keep':
        # 保留 = 转为永久：清空到期时间，作品恢复对外访问（可见性/审核状态不变）
        db.execute('UPDATE pages SET share_expires_at=NULL, updated=? WHERE id=?',
                   (datetime.now().isoformat(), page_id))
        db.commit()
        app.logger.info('回收站：%s 保留作品 %s《%s》并转永久',
                        session.get('username'), page_id, page['title'])
        return jsonify({'success': True, 'message': '已保留，作品转为永久有效'})
    # 删除前复核：作者可能刚续期，作品已离开回收站，此时不应再删
    if not _is_expired(page):
        return jsonify({'error': '该作品已不在回收站（可能已被作者续期），刷新后再看'}), 409
    _delete_pages_cascade(db, [page_id])
    db.commit()
    app.logger.info('回收站：%s 删除作品 %s《%s》',
                    session.get('username'), page_id, page['title'])
    return jsonify({'success': True, 'message': '已删除，不可恢复'})


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
    """公开分享链接：按公开标识 + 作品短码定位，不含登录用户名。"""
    page = _page_or_403(slug, handle=handle)
    if page['slug'] != slug or page['handle'] != handle:
        return redirect(url_for('share', handle=page['handle'], slug=page['slug']), 301)
    ctx = _fission_ctx(get_db(), page)
    return render_template('view.html', page=page, html_source=_html_with_alt(page['html_source'], page['title']),
                           **_owner_expiry_ctx(page), **ctx, **_seo_ctx(page))

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', port=5050)
