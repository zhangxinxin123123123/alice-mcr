
import re, math, sqlite3, webbrowser, threading, os, smtplib, json, hashlib, traceback, secrets, base64, gzip, unicodedata, socket, ipaddress, time
from datetime import date, datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib import robotparser
from flask import Flask, request, jsonify, send_from_directory, Response
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None
APP_DIR=Path(__file__).resolve().parent
DB_PATH=Path(os.environ.get('ALICE_DB_PATH') or ('/var/data/alice_academy_mcr.db' if Path('/var/data').exists() else str(APP_DIR/'alice_academy_mcr.db')))
BOSS_EMAIL=os.environ.get('ALICE_BOSS_EMAIL','xinxinzhang330@gmail.com')
NEKO_BASE_URL=os.environ.get('ALICE_NEKO_BASE_URL','https://neko-miaomiao.com').rstrip('/')
ALICE_BASE_URL=os.environ.get('ALICE_PUBLIC_BASE_URL','https://ailisi99.com').rstrip('/')
TOKYO_YY_BASE_URL=os.environ.get('TOKYO_YY_BASE_URL','https://tokyo-yy.com').rstrip('/')
TOKYO_ALICE_SHOP_ID=os.environ.get('TOKYO_ALICE_SHOP_ID','\u7231\u4e3d\u4e1d\u5b66\u56ed')
OPENAI_REVIEW_MODEL=os.environ.get('OPENAI_REVIEW_MODEL','gpt-5.6-luna').strip()
LEGACY_AVATAR_DIR=APP_DIR/'static'/'girl_avatars'
# Render 的程序目录会随部署重建；头像必须与 SQLite 一样放在持久磁盘。
AVATAR_DIR=Path(os.environ.get('ALICE_AVATAR_DIR') or
                (DB_PATH.parent/'girl_avatars' if str(DB_PATH).replace('\\','/').startswith('/var/data/') else LEGACY_AVATAR_DIR))
LEGACY_GIRL_PRAISE_DIR=APP_DIR/'static'/'girl_praises'
GIRL_PRAISE_DIR=Path(os.environ.get('ALICE_GIRL_PRAISE_DIR') or (DB_PATH.parent/'girl_praises'))
app=Flask(__name__, static_folder=str(APP_DIR/'static'), static_url_path='/static')

app.config['JSON_AS_ASCII'] = False
APP_VERSION = "v122_openai_review_writer"

@app.after_request
def compress_large_json(response):
    """Reduce transfer time for the large MCR snapshot on mobile connections."""
    if (response.status_code < 200 or response.status_code >= 300 or response.direct_passthrough
            or response.headers.get('Content-Encoding') or response.mimetype != 'application/json'
            or 'gzip' not in request.headers.get('Accept-Encoding', '').lower()):
        return response
    raw = response.get_data()
    if len(raw) < 1400:
        return response
    packed = gzip.compress(raw, compresslevel=5)
    if len(packed) >= len(raw):
        return response
    response.set_data(packed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(packed))
    response.headers['Vary'] = 'Accept-Encoding'
    return response

# 固定登录账号：需要改账号密码就在这里改
USERS = {
    "Star": {"password": "9941", "role": "boss", "label": "老板"},
    "admin": {"password": "admin123", "role": "admin", "label": "管理员"},
    "user": {"password": "user123", "role": "user", "label": "普通用户"},
}
SYSTEM_MODULES = [
    'home','orders','customers','girls','settlement','quickLinks','importer','telegramBooking',
    'chainReserve','pureShift','manual','advanceReserve','rooms','enums','reviewCrawler','stats','debug','loginAudit','operationAudit'
]
ROLE_DEFAULT_PERMISSIONS = {
    'boss': SYSTEM_MODULES,
    'admin': [x for x in SYSTEM_MODULES if x not in ('home','loginAudit','operationAudit')],
    'user': [x for x in SYSTEM_MODULES if x not in ('home','settlement','stats','loginAudit','operationAudit')],
}
ACTIVE_SESSIONS = {}
LAST_FULL_MAINTENANCE_DAY = None
PUBLIC_PATHS = {"/", "/reserve", "/tonight", "/api/login", "/api/health", "/api/db_info", "/api/customer_register", "/api/customer_login", "/api/customer_available", "/api/customer_reserve"}

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', str(password or '').encode(), salt.encode(), 180000).hex()
    return f"pbkdf2_sha256${salt}${digest}"

def password_matches(password, encoded):
    try:
        scheme, salt, expected = str(encoded or '').split('$', 2)
        if scheme != 'pbkdf2_sha256':
            return False
        actual = password_hash(password, salt).rsplit('$', 1)[-1]
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False

def normalize_permissions(value, role='user'):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = value.split(',')
    if not isinstance(value, (list, tuple, set)):
        value = ROLE_DEFAULT_PERMISSIONS.get(role, [])
    return [key for key in SYSTEM_MODULES if key in set(str(x) for x in value)]

@app.errorhandler(Exception)
def api_json_error(e):
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return jsonify(ok=False, error=f"服务器内部错误：{type(e).__name__}: {e}"), 500
    raise e

def round_yen_1000_half_up(n):
    """店铺收益按 1000 円单位四舍五入：7500 -> 8000，末尾 500 自动进位。"""
    n = int(round(float(n or 0)))
    if n <= 0:
        return 0
    return ((n + 500) // 1000) * 1000

def parse_header(lines):
    """识别接龙首行：0524小樱 / 9月8日 小樱 / 2026-05-24 小樱。"""
    for line in lines or []:
        s = strip_chain_prefix(line) if 'strip_chain_prefix' in globals() else str(line or '').strip()
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s*([^\s/]+)?", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", (m.group(4) or '').strip()
        m = re.search(r"(?:\[|【)?(\d{1,2})月(\d{1,2})日?(?:\]|】)?\s*([^\s/]+)?", s)
        if m:
            y = date.today().year
            return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", (m.group(3) or '').strip()
        # 紧凑日期（0524小樱）只用于接龙首行；包含 7.30到8.30 / 7.30-8.30 的预约行不能误判成 0830 日期。
        if re.search(r"\d{1,2}(?:[:.]\d{1,2})?\s*(?:[-~ー～]|到|至)\s*\d{1,2}(?:[:.]\d{1,2})?", s):
            continue
        m = re.search(r"(?<!\d)(\d{1,2})(\d{2})\s*([^\s/]+)?", s)
        if m:
            y = date.today().year
            return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", (m.group(3) or '').strip()
    return None, ''

_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED_PATHS = set()

def _init_db_schema():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_no TEXT NOT NULL UNIQUE, name TEXT,
            customer_type TEXT DEFAULT '新客', customer_status TEXT DEFAULT '正常', recharge_balance INTEGER DEFAULT 0,
            total_recharge INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0, points INTEGER DEFAULT 0, total_points INTEGER DEFAULT 0,
            source TEXT DEFAULT '', contact TEXT DEFAULT '', grade TEXT DEFAULT '', tags TEXT DEFAULT '', member_level TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', customer_type_locked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS system_users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user', label TEXT DEFAULT '', permissions TEXT DEFAULT '[]', enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS login_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT DEFAULT '', role TEXT DEFAULT '', role_label TEXT DEFAULT '',
            ip TEXT DEFAULT '', user_agent TEXT DEFAULT '', session_token TEXT DEFAULT '', login_at TEXT DEFAULT '',
            last_seen_at TEXT DEFAULT '', logout_at TEXT DEFAULT '', status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_sessions_token ON login_sessions(session_token)")
        c.execute("""CREATE TABLE IF NOT EXISTS operation_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_name TEXT NOT NULL, actor_role TEXT DEFAULT '',
            method TEXT NOT NULL, target TEXT NOT NULL, detail TEXT DEFAULT '', response_status INTEGER DEFAULT 0,
            log_level TEXT DEFAULT 'INFO', action_name TEXT DEFAULT '',
            ip TEXT DEFAULT '', user_agent TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        operation_cols = [r[1] for r in c.execute('PRAGMA table_info(operation_logs)').fetchall()]
        if 'log_level' not in operation_cols:
            c.execute("ALTER TABLE operation_logs ADD COLUMN log_level TEXT DEFAULT 'INFO'")
        if 'action_name' not in operation_cols:
            c.execute("ALTER TABLE operation_logs ADD COLUMN action_name TEXT DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_created ON operation_logs(created_at,id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_actor ON operation_logs(actor_name,created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_level ON operation_logs(log_level,created_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS customer_cleanup_archives(
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE,
            actor_name TEXT DEFAULT '', reason TEXT DEFAULT '', customer_count INTEGER DEFAULT 0,
            payload_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        if not c.execute("SELECT 1 FROM system_users LIMIT 1").fetchone():
            seed_users = []
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_users'").fetchone():
                seed_users = [dict(x) for x in c.execute("SELECT username,password,role,label,is_active FROM app_users ORDER BY rowid").fetchall()]
            if not seed_users:
                seed_users = [dict(username=username, password=info.get('password'), role=info.get('role'),
                                   label=info.get('label'), is_active=1) for username, info in USERS.items()]
            for info in seed_users:
                username = str(info.get('username') or '').strip()
                role = info.get('role') or 'user'
                if not username or role not in ROLE_DEFAULT_PERMISSIONS:
                    continue
                c.execute("""INSERT INTO system_users(username,password_hash,role,label,permissions,enabled)
                             VALUES(?,?,?,?,?,?)""", (username, password_hash(info.get('password')), role,
                             info.get('label') or username, json.dumps(ROLE_DEFAULT_PERMISSIONS.get(role, []), ensure_ascii=False),
                             1 if info.get('is_active', 1) else 0))
        c.execute("""CREATE TABLE IF NOT EXISTS girls(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, girl_alias TEXT DEFAULT '', girl_type TEXT DEFAULT '普通', girl_status TEXT DEFAULT '在职', enrollment TEXT DEFAULT '',
            take_home_per_hour INTEGER DEFAULT 10000, list_price INTEGER DEFAULT 15000, contact TEXT DEFAULT '', tags TEXT DEFAULT '',
            avatar_url TEXT DEFAULT '', avatar_source_url TEXT DEFAULT '', avatar_updated_at TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT, service_time TEXT, hours REAL DEFAULT 1,
            girl_id INTEGER, girl_name TEXT, customer_id INTEGER, customer_no TEXT, customer_name TEXT,
            received_amount INTEGER DEFAULT 0, girl_take_home INTEGER DEFAULT 0, store_profit INTEGER DEFAULT 0, points INTEGER DEFAULT 0,
            order_status TEXT DEFAULT '已结束', settlement_status TEXT DEFAULT '未结算', payment_method TEXT DEFAULT '现金',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', raw_text TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_date_id ON orders(customer_id, order_date, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_date_id ON orders(order_date, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS settlement_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TEXT NOT NULL, girl_name TEXT NOT NULL,
            theoretical_amount INTEGER DEFAULT 0, actual_settlement INTEGER DEFAULT 0, formula_text TEXT DEFAULT '',
            order_ids TEXT DEFAULT '', signed_order_ids TEXT DEFAULT '', boss_email TEXT DEFAULT '', girl_email TEXT DEFAULT '',
            sent_to_boss_at TEXT DEFAULT '', sent_to_girl_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(report_date, girl_name))""")
        c.execute("""CREATE TABLE IF NOT EXISTS recharge_records(id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, customer_no TEXT, amount INTEGER, payment_method TEXT DEFAULT '现金', remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', order_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS points_records(id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, customer_no TEXT, change_points INTEGER, reason TEXT DEFAULT '', remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', order_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS enum_values(id INTEGER PRIMARY KEY AUTOINCREMENT, enum_type TEXT NOT NULL, value TEXT NOT NULL, sort_order INTEGER DEFAULT 0, UNIQUE(enum_type,value))""")
        c.execute("""CREATE TABLE IF NOT EXISTS girl_schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_date TEXT, girl_id INTEGER, girl_name TEXT, start_time TEXT, end_time TEXT, price INTEGER DEFAULT 0, status TEXT DEFAULT '出勤', note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS hotel_rooms(
            id INTEGER PRIMARY KEY AUTOINCREMENT, hotel_name TEXT NOT NULL, room_no TEXT NOT NULL, daily_cost INTEGER DEFAULT 0, remark TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(hotel_name, room_no))""")
        c.execute("""CREATE TABLE IF NOT EXISTS room_assignments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_date TEXT NOT NULL, hotel_name TEXT NOT NULL, room_no TEXT NOT NULL,
            girl_id INTEGER DEFAULT 0, girl_name TEXT DEFAULT '', daily_cost INTEGER DEFAULT 0, note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(assignment_date, hotel_name, room_no))""")
        c.execute("""CREATE TABLE IF NOT EXISTS pure_shifts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, shift_date TEXT NOT NULL, girl_name TEXT NOT NULL,
            start_time TEXT DEFAULT '19:00', end_time TEXT DEFAULT '23:00', tags TEXT DEFAULT '', gold_tags TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0, source TEXT DEFAULT 'manual', note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girl_tag_memory(
            girl_name TEXT PRIMARY KEY, tags TEXT DEFAULT '', gold_tags TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girl_avatar_cache(
            girl_name TEXT PRIMARY KEY, neko_name TEXT DEFAULT '', avatar_url TEXT DEFAULT '',
            source_url TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girl_praises(
            id INTEGER PRIMARY KEY AUTOINCREMENT, girl_id INTEGER DEFAULT 0, girl_name TEXT NOT NULL,
            source_name TEXT DEFAULT '', image_path TEXT NOT NULL, image_mime TEXT DEFAULT 'image/png',
            image_blob BLOB, publish_status TEXT DEFAULT '未上架', wp_post_id INTEGER DEFAULT 0,
            wp_attachment_id INTEGER DEFAULT 0, published_at TEXT DEFAULT '', publish_error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_girl_praises_girl ON girl_praises(girl_id, girl_name, created_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS scraped_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL, source_page TEXT DEFAULT '',
            girl_name TEXT DEFAULT '', review_text TEXT NOT NULL, rating REAL DEFAULT 0,
            review_date TEXT DEFAULT '', tags TEXT DEFAULT '', review_hash TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_scraped_reviews_girl ON scraped_reviews(girl_name, created_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS customer_accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, line_name TEXT NOT NULL, phone TEXT NOT NULL,
            status TEXT DEFAULT '待审核', member_level TEXT DEFAULT 'svip', customer_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS customer_reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, reserve_date TEXT NOT NULL, girl_name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
            customer_account_id INTEGER DEFAULT 0, username TEXT DEFAULT '', line_name TEXT DEFAULT '', phone TEXT DEFAULT '',
            status TEXT DEFAULT '待确认', price INTEGER DEFAULT 0, note TEXT DEFAULT '', order_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS quick_links(
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', content TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_quick_links_group ON quick_links(group_name, sort_order, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS chain_import_rows(
            order_date TEXT NOT NULL, girl_id INTEGER NOT NULL, sequence_no INTEGER NOT NULL, order_id INTEGER NOT NULL,
            normalized_text TEXT DEFAULT '', source_chat_id TEXT DEFAULT '', source_message_id TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(order_date, girl_id, sequence_no))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_chain_import_order ON chain_import_rows(order_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS telegram_customer_cancellations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id INTEGER NOT NULL UNIQUE,
            telegram_user_id TEXT DEFAULT '', customer_id INTEGER DEFAULT 0,
            cancellation_no INTEGER DEFAULT 1, points_deducted INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tg_cancel_customer ON telegram_customer_cancellations(customer_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tg_cancel_user ON telegram_customer_cancellations(telegram_user_id, created_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS telegram_full_sync_days(
            sync_date TEXT PRIMARY KEY, full_synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
            late_auto_enabled INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for qg, qt, qc, so in [('网址','网址','',1),('常用短语','常用短语','',2)]:
            c.execute('INSERT OR IGNORE INTO quick_links(group_name,title,content,sort_order) SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM quick_links WHERE group_name=? AND title=?)', (qg, qt, qc, so, qg, qt))
        defaults=[('customer_type','新客',1),('customer_type','回头客',2),('customer_type','老客',3),('customer_type','VIP',4),('customer_type','SVIP',5),('customer_type','常客',6),('girl_type','普通',1),('girl_status','在职',1),('order_status','预约中',0),('order_status','已结束',1),('order_status','取消',2),('settlement_status','未结算',1),('settlement_status','已结算',2),('schedule_status','出勤',1),('schedule_status','休息',2),('customer_preference_tag','酒量好',1),('customer_preference_tag','喜欢聊天',2),('customer_preference_tag','喜欢新人',3),('customer_preference_tag','安静型',4)]
        for et,v,so in defaults:
            c.execute('INSERT OR IGNORE INTO enum_values(enum_type,value,sort_order) VALUES(?,?,?)',(et,v,so))
        # v17.1: 默认新增女孩定价 15000，到手 10000；旧库自动补列和迁移快捷分组名称。
        customer_cols = [r[1] for r in c.execute('PRAGMA table_info(customers)').fetchall()]
        if 'customer_type_locked' not in customer_cols:
            c.execute("ALTER TABLE customers ADD COLUMN customer_type_locked INTEGER DEFAULT 0")
        report_cols = [r[1] for r in c.execute('PRAGMA table_info(settlement_reports)').fetchall()]
        if 'signed_order_ids' not in report_cols:
            c.execute("ALTER TABLE settlement_reports ADD COLUMN signed_order_ids TEXT DEFAULT ''")
        girl_cols = [r[1] for r in c.execute('PRAGMA table_info(girls)').fetchall()]
        if 'list_price' not in girl_cols:
            c.execute('ALTER TABLE girls ADD COLUMN list_price INTEGER DEFAULT 15000')
        if 'girl_alias' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN girl_alias TEXT DEFAULT ''")
        if 'email' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN email TEXT DEFAULT ''")
        if 'enrollment' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN enrollment TEXT DEFAULT ''")
        if 'avatar_url' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN avatar_url TEXT DEFAULT ''")
        if 'avatar_source_url' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN avatar_source_url TEXT DEFAULT ''")
        if 'avatar_updated_at' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN avatar_updated_at TEXT DEFAULT ''")
        order_cols = [r[1] for r in c.execute('PRAGMA table_info(orders)').fetchall()]
        if 'points_used' not in order_cols:
            c.execute("ALTER TABLE orders ADD COLUMN points_used INTEGER DEFAULT 0")
        review_cols = [r[1] for r in c.execute('PRAGMA table_info(scraped_reviews)').fetchall()]
        for column, definition in (
            ('material_type', "TEXT DEFAULT '公开素材'"), ('author_name', "TEXT DEFAULT ''"),
            ('source_title', "TEXT DEFAULT ''"), ('access_scope', "TEXT DEFAULT 'public'")):
            if column not in review_cols:
                c.execute(f"ALTER TABLE scraped_reviews ADD COLUMN {column} {definition}")
        praise_cols = [r[1] for r in c.execute('PRAGMA table_info(girl_praises)').fetchall()]
        if 'image_mime' not in praise_cols:
            c.execute("ALTER TABLE girl_praises ADD COLUMN image_mime TEXT DEFAULT 'image/png'")
        if 'image_blob' not in praise_cols:
            c.execute("ALTER TABLE girl_praises ADD COLUMN image_blob BLOB")
        for column, definition in (
            ('publish_status', "TEXT DEFAULT '未上架'"), ('wp_post_id', 'INTEGER DEFAULT 0'),
            ('wp_attachment_id', 'INTEGER DEFAULT 0'), ('published_at', "TEXT DEFAULT ''"),
            ('publish_error', "TEXT DEFAULT ''")):
            if column not in praise_cols:
                c.execute(f"ALTER TABLE girl_praises ADD COLUMN {column} {definition}")
        c.execute("UPDATE girl_praises SET publish_status='未上架' WHERE COALESCE(publish_status,'')='' ")
        c.execute("""UPDATE girl_praises
                     SET image_path='/girl_praises/' || substr(image_path, length('/static/girl_praises/') + 1),
                         updated_at=CURRENT_TIMESTAMP
                     WHERE image_path LIKE '/static/girl_praises/%'""")
        c.execute("UPDATE quick_links SET group_name='网址' WHERE group_name='排班表'")
        c.execute("UPDATE quick_links SET title='网址' WHERE title='排班表'")
        c.execute("UPDATE quick_links SET group_name='常用短语' WHERE group_name='固定短语'")
        c.execute("UPDATE quick_links SET title='常用短语' WHERE title='固定短语'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_recharges_customer_created ON recharge_records(customer_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_points_customer_created ON points_records(customer_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_schedules_date_id ON girl_schedules(schedule_date, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_room_assignments_date ON room_assignments(assignment_date, hotel_name, room_no)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_customer_reservations_date ON customer_reservations(reserve_date, start_time, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pure_shifts_date_sort ON pure_shifts(shift_date, sort_order, id)")
        tag_memory_cols = [r[1] for r in c.execute('PRAGMA table_info(girl_tag_memory)').fetchall()]
        if 'gold_tags' not in tag_memory_cols:
            c.execute("ALTER TABLE girl_tag_memory ADD COLUMN gold_tags TEXT DEFAULT ''")
        c.execute("PRAGMA optimize")

def init_db():
    key = str(DB_PATH.resolve())
    if key in _DB_INITIALIZED_PATHS:
        return
    with _DB_INIT_LOCK:
        if key in _DB_INITIALIZED_PATHS:
            return
        _init_db_schema()
        _DB_INITIALIZED_PATHS.add(key)

    try:
        GIRL_PRAISE_DIR.mkdir(parents=True, exist_ok=True)
        if LEGACY_GIRL_PRAISE_DIR.exists():
            for old in LEGACY_GIRL_PRAISE_DIR.iterdir():
                if old.is_file():
                    new = GIRL_PRAISE_DIR / old.name
                    if not new.exists():
                        new.write_bytes(old.read_bytes())
    except Exception:
        traceback.print_exc()

def current_role():
    info = ACTIVE_SESSIONS.get(current_session_token()) or {}
    return info.get('role') or ''

def current_session_token():
    return request.headers.get('X-Alice-Session') or request.args.get('session_token') or ''

def current_session_info():
    token = current_session_token()
    if not token:
        return {}
    # Render 重启或切换实例会清空进程内存；有效登录必须能从持久数据库恢复。
    try:
        with conn() as c:
            row = c.execute("""SELECT ls.username,ls.role,ls.status,ls.logout_at,
                                      su.label,su.permissions,su.enabled
                               FROM login_sessions ls
                               LEFT JOIN system_users su ON su.username=ls.username
                               WHERE ls.session_token=? ORDER BY ls.id DESC LIMIT 1""", (token,)).fetchone()
        if not row or row['status'] != 'active' or row['logout_at'] or not int(row['enabled'] or 0):
            ACTIVE_SESSIONS.pop(token, None)
            return {}
        role = str(row['role'] or '')
        info = {'username': str(row['username'] or ''), 'role': role,
                'label': str(row['label'] or row['username'] or ''),
                'permissions': normalize_permissions(row['permissions'], role)}
        ACTIVE_SESSIONS[token] = info
        return info
    except sqlite3.OperationalError:
        # 首次建库、登录记录表尚未创建时仅使用本进程刚签发的令牌。
        return ACTIVE_SESSIONS.get(token) or {}

def required_module_for_api(path):
    if path == '/api/telegram/report-photo':
        kind = str((request.get_json(silent=True) or {}).get('kind') or '')
        return 'settlement' if kind == 'settlement' else 'pureShift'
    if path == '/api/operation_logs/frontend':
        return None
    rules = [
        ('/api/system/users', 'loginAudit'), ('/api/login_audit', 'loginAudit'), ('/api/operation_logs', 'operationAudit'), ('/api/telegram/', 'telegramBooking'),
        ('/api/settlements', 'settlement'), ('/api/orders/bulk_settle', 'settlement'),
        ('/api/customers', 'customers'), ('/api/girls', 'girls'), ('/api/girl_', 'girls'),
        ('/api/orders', 'orders'), ('/api/import_chain', 'importer'), ('/api/chain_', 'chainReserve'),
        ('/api/pure_shifts', 'pureShift'), ('/api/schedules', 'pureShift'),
        ('/api/room', 'rooms'), ('/api/hotel_room', 'rooms'), ('/api/delete_room', 'rooms'),
        ('/api/enums', 'enums'), ('/api/quick_links', 'quickLinks'),
        ('/api/review_crawler', 'reviewCrawler'),
        ('/api/customer_accounts', 'advanceReserve'), ('/api/customer_reservations', 'advanceReserve'),
    ]
    if path.startswith('/api/delete/'):
        table = path.split('/')[3] if len(path.split('/')) > 3 else ''
        return {'orders':'orders','customers':'customers','girls':'girls','recharges':'customers','points':'customers'}.get(table)
    for prefix, module in rules:
        if path.startswith(prefix):
            return module
    return None

@app.before_request
def require_login_for_api():
    path = request.path
    if path.startswith('/static/') or path in PUBLIC_PATHS:
        return None
    if path.startswith('/api/'):
        session = current_session_info()
        role = session.get('role') or ''
        if role not in ('boss','admin','user'):
            return jsonify(ok=False, error='请先登录'), 401
        if not current_session_token() or not session:
            return jsonify(ok=False, error='登录已失效，请重新登录'), 401
        module = required_module_for_api(path)
        if role != 'boss' and module and module not in session.get('permissions', []):
            return jsonify(ok=False, error='当前账号没有这个模块的权限'), 403
    return None

@app.route('/api/login', methods=['POST'])
def api_login():
    init_db()
    d = request.json or {}
    u = str(d.get('username') or '').strip()
    p = str(d.get('password') or '').strip()
    with conn() as c:
        row = c.execute("SELECT * FROM system_users WHERE username=? AND enabled=1", (u,)).fetchone()
    info = dict(row) if row else None
    if info and password_matches(p, info.get('password_hash')):
        permissions = normalize_permissions(info.get('permissions'), info.get('role'))
        token = secrets.token_urlsafe(24)
        session_info = {'username': u, 'role': info['role'], 'label': info['label'], 'permissions': permissions}
        ACTIVE_SESSIONS[token] = session_info
        now = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
        with conn() as c:
            c.execute("""INSERT INTO login_sessions(username,role,role_label,ip,user_agent,session_token,login_at,last_seen_at,status)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (u, info['role'], info['label'], str(request.remote_addr or ''),
                       str(request.headers.get('User-Agent') or ''), token, now, now, 'active'))
        return jsonify(ok=True, username=u, role=info['role'], label=info['label'], permissions=permissions, session_token=token)
    return jsonify(ok=False, error='用户名或密码错误'), 401

@app.route('/api/login/logout', methods=['POST'])
def api_login_logout():
    d = request.json or {}
    token = str(d.get('session_token') or current_session_token() or '').strip()
    if token:
        ACTIVE_SESSIONS.pop(token, None)
        now = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
        with conn() as c:
            c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='logout',updated_at=CURRENT_TIMESTAMP
                         WHERE session_token=? AND COALESCE(logout_at,'')=''""", (now, now, token))
    return jsonify(ok=True)

@app.route('/api/system/users', methods=['GET', 'POST'])
def api_system_users():
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以管理登录权限'), 403
    init_db()
    actor = current_session_info().get('username') or ''
    if request.method == 'GET':
        with conn() as c:
            user_rows = rows(c.execute("""SELECT id,username,role,label,permissions,enabled,created_at,updated_at
                                         FROM system_users ORDER BY CASE role WHEN 'boss' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,id""").fetchall())
        active_counts = {}
        for session in ACTIVE_SESSIONS.values():
            name = session.get('username') or ''
            active_counts[name] = active_counts.get(name, 0) + 1
        try:
            with conn() as c:
                db_active = c.execute("""SELECT username,COUNT(*) AS n FROM login_sessions
                                         WHERE status='active' AND COALESCE(logout_at,'')='' GROUP BY username""").fetchall()
            for row in db_active:
                active_counts[row['username']] = max(active_counts.get(row['username'], 0), int(row['n'] or 0))
        except sqlite3.OperationalError:
            pass
        for item in user_rows:
            item['permissions'] = normalize_permissions(item.get('permissions'), item.get('role'))
            item['active_sessions'] = active_counts.get(item.get('username'), 0)
        return jsonify(ok=True, users=user_rows, modules=SYSTEM_MODULES)
    d = request.json or {}
    action = str(d.get('action') or 'save')
    uid = int(d.get('id') or 0)
    with conn() as c:
        if action == 'delete':
            row = c.execute("SELECT * FROM system_users WHERE id=?", (uid,)).fetchone()
            if not row:
                return jsonify(ok=False, error='账号不存在'), 404
            if row['username'] == actor:
                return jsonify(ok=False, error='不能删除当前正在使用的老板账号'), 400
            if row['role'] == 'boss' and c.execute("SELECT COUNT(*) FROM system_users WHERE role='boss' AND enabled=1").fetchone()[0] <= 1:
                return jsonify(ok=False, error='系统必须至少保留一个启用的老板账号'), 400
            c.execute("DELETE FROM system_users WHERE id=?", (uid,))
            for token, session in list(ACTIVE_SESSIONS.items()):
                if session.get('username') == row['username']:
                    ACTIVE_SESSIONS.pop(token, None)
            return jsonify(ok=True)
        username = str(d.get('username') or '').strip()
        label = str(d.get('label') or username).strip()
        role = str(d.get('role') or 'user').strip()
        enabled = 1 if d.get('enabled', True) else 0
        if not username or role not in ROLE_DEFAULT_PERMISSIONS:
            return jsonify(ok=False, error='账号名或角色不正确'), 400
        permissions = SYSTEM_MODULES if role == 'boss' else normalize_permissions(d.get('permissions'), role)
        existing = c.execute("SELECT * FROM system_users WHERE id=?", (uid,)).fetchone() if uid else None
        if existing and existing['username'] == actor and (role != 'boss' or not enabled):
            return jsonify(ok=False, error='不能停用当前老板账号或取消自己的老板角色'), 400
        duplicate = c.execute("SELECT id FROM system_users WHERE username=? AND id<>?", (username, uid or 0)).fetchone()
        if duplicate:
            return jsonify(ok=False, error='这个登录账号已经存在'), 400
        password = str(d.get('password') or '')
        if existing:
            encoded = password_hash(password) if password else existing['password_hash']
            c.execute("""UPDATE system_users SET username=?,password_hash=?,role=?,label=?,permissions=?,enabled=?,updated_at=CURRENT_TIMESTAMP
                         WHERE id=?""", (username, encoded, role, label, json.dumps(permissions, ensure_ascii=False), enabled, uid))
            old_username = existing['username']
        else:
            if len(password) < 4:
                return jsonify(ok=False, error='新账号密码至少需要4位'), 400
            c.execute("""INSERT INTO system_users(username,password_hash,role,label,permissions,enabled)
                         VALUES(?,?,?,?,?,?)""", (username, password_hash(password), role, label,
                         json.dumps(permissions, ensure_ascii=False), enabled))
            old_username = username
        for token, session in list(ACTIVE_SESSIONS.items()):
            if session.get('username') == old_username:
                if not enabled:
                    ACTIVE_SESSIONS.pop(token, None)
                else:
                    session.update(username=username, role=role, permissions=permissions)
    return jsonify(ok=True)

@app.route('/api/operation_logs', methods=['GET'])
def api_operation_logs():
    session = current_session_info()
    if current_role() != 'boss' and 'operationAudit' not in set(session.get('permissions') or []):
        return jsonify(ok=False, error='当前账号没有管理日志权限'), 403
    init_db()
    selected_date = str(request.args.get('date') or tokyo_today_date())[:10]
    limit = min(500, max(20, int(request.args.get('limit') or 200)))
    actor = str(request.args.get('actor') or '').strip()
    query = str(request.args.get('q') or '').strip()
    method = str(request.args.get('method') or '').strip().upper()
    level = str(request.args.get('level') or '').strip().upper()
    conditions = ["date(datetime(created_at,'+9 hours'))=?"]
    values = [selected_date]
    if actor:
        conditions.append("actor_name LIKE ?")
        values.append(f"%{actor}%")
    if query:
        conditions.append("(method LIKE ? OR target LIKE ? OR action_name LIKE ? OR detail LIKE ?)")
        values.extend([f"%{query}%"] * 4)
    if method:
        conditions.append("method=?")
        values.append(method)
    if level in ('DEBUG', 'INFO', 'WARN', 'ERROR'):
        conditions.append("log_level=?")
        values.append(level)
    values.append(limit)
    with conn() as c:
        items = rows(c.execute(f"""SELECT id,actor_name,actor_role,method,target,detail,response_status,
                                          COALESCE(log_level,'INFO') AS log_level,COALESCE(action_name,'') AS action_name,ip,user_agent,
                                          datetime(created_at,'+9 hours') AS created_at
                                   FROM operation_logs
                                   WHERE {' AND '.join(conditions)}
                                   ORDER BY id DESC LIMIT ?""", values).fetchall())
    return jsonify(ok=True, date=selected_date, logs=items)


@app.route('/api/operation_logs/frontend', methods=['POST'])
def api_frontend_operation_log():
    """记录后台页面按钮点击，便于把用户动作和随后调用的接口对照排查。"""
    if current_role() not in ('boss', 'admin', 'user'):
        return jsonify(ok=False, error='请先登录'), 401
    d = request.get_json(silent=True) or {}
    session = current_session_info()
    label = re.sub(r'\s+', ' ', str(d.get('label') or '')).strip()[:120]
    module = re.sub(r'[^A-Za-z0-9_-]', '', str(d.get('module') or ''))[:80]
    handler = str(d.get('handler') or '').strip()[:300]
    event_type = str(d.get('event_type') or 'CLICK').strip().upper()
    if event_type not in ('CLICK', 'QUERY'):
        event_type = 'CLICK'
    field = re.sub(r'\s+', ' ', str(d.get('field') or '')).strip()[:120]
    value = re.sub(r'\s+', ' ', str(d.get('value') or '')).strip()[:300]
    detail = json.dumps({'button': label, 'module': module, 'handler': handler,
                         'field': field, 'value': value},
                        ensure_ascii=False, separators=(',', ':'))
    forwarded = str(request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    with conn() as c:
        action_name = '筛选查询' if event_type == 'QUERY' else '点击按钮'
        c.execute("""INSERT INTO operation_logs(actor_name,actor_role,method,target,detail,response_status,log_level,action_name,ip,user_agent)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (str(session.get('username') or ''), str(session.get('role') or ''), event_type,
                   module or 'unknown', detail, 200, 'DEBUG', action_name, forwarded or str(request.remote_addr or ''),
                   str(request.headers.get('User-Agent') or '')[:500]))
    return jsonify(ok=True)


def conn():
    c=sqlite3.connect(DB_PATH, timeout=20); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=20000')
    return c
def rows(rs): return [dict(r) for r in rs]

def safe_audit_value(value, depth=0):
    if depth > 3:
        return '…'
    if isinstance(value, dict):
        result = {}
        for raw_key, raw_value in list(value.items())[:60]:
            key = str(raw_key)
            lowered = key.lower()
            if any(secret in lowered for secret in ('password', 'passwd', 'token', 'secret', 'image_data',
                                                      'image_blob', 'file_data', 'authorization')):
                result[key] = '[已隐藏]'
            else:
                result[key] = safe_audit_value(raw_value, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = [safe_audit_value(x, depth + 1) for x in list(value)[:30]]
        if len(value) > 30:
            items.append(f'…其余{len(value)-30}项')
        return items
    if isinstance(value, str):
        compact = re.sub(r'\s+', ' ', value).strip()
        return compact[:300] + ('…' if len(compact) > 300 else '')
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:300]


def audit_action_label(method, path, payload):
    if method == 'GET':
        return '读取全部数据' if path == '/api/all' else '查询数据'
    if method == 'DELETE' or '/delete' in path or str(payload.get('action') or '') == 'delete' or payload.get('delete_id'):
        return '删除数据'
    if method in ('PUT', 'PATCH') or payload.get('id'):
        return '修改数据'
    return '新增或提交数据'


@app.after_request
def record_admin_operation(response):
    """记录已登录后台账号的查询和写入；密码、Token、图片及超长正文不会进入日志。"""
    try:
        if request.method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') or not request.path.startswith('/api/'):
            return response
        if request.path in ('/api/login', '/api/login/logout', '/api/health', '/api/db_info',
                            '/api/operation_logs', '/api/operation_logs/frontend'):
            return response
        session = current_session_info()
        actor = str(session.get('username') or '')
        if not actor:
            return response
        payload = request.get_json(silent=True) or {}
        query = {k: v for k, v in request.args.items() if k not in ('v', 'session_token')}
        safe = safe_audit_value(payload)
        detail_obj = {'action': audit_action_label(request.method, request.path, payload)}
        if query:
            detail_obj['query'] = safe_audit_value(query)
        if safe:
            detail_obj['data'] = safe
        detail = json.dumps(detail_obj, ensure_ascii=False, separators=(',', ':'))[:5000]
        status = int(response.status_code or 0)
        level = 'ERROR' if status >= 500 else ('WARN' if status >= 400 else ('DEBUG' if request.method == 'GET' else 'INFO'))
        action_name = str(detail_obj.get('action') or '')
        forwarded = str(request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
        with conn() as c:
            c.execute("""INSERT INTO operation_logs(actor_name,actor_role,method,target,detail,response_status,log_level,action_name,ip,user_agent)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (actor, str(session.get('role') or ''), request.method, request.path, detail,
                       status, level, action_name, forwarded or str(request.remote_addr or ''),
                       str(request.headers.get('User-Agent') or '')[:500]))
    except Exception:
        pass
    return response

NEKO_SEED_GIRLS = [
    {"name":"新人女孩 夏織（かおり）性感日妹","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2026/04/20260423_WechatIMG14-1.thumb.jpg"},
    {"name":"七海莉莉（童颜巨乳萝莉）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2026/03/20260325_%E5%9B%BE%E7%89%87_20260325183102.thumb.jpg"},
    {"name":"紅莉（べにり）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2026/01/20260129_%E5%9B%BE%E7%89%87_20260129142059_646_58.thumb.jpg"},
    {"name":"綾瀬（傲娇地雷系）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2025/12/20251220_line_oa_chat_251220_142928.thumb.jpg"},
    {"name":"新人女孩淼淼（模特瘦身美女）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2025/10/20251024_IMG_1200.thumb.jpg"},
    {"name":"绚（04年长腿巨瘦嫩妹妹）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2025/10/20251004_%E5%9B%BE%E7%89%87_20251004142526_271_58.thumb.jpg"},
    {"name":"芙莲（模特双马尾妹妹）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2025/09/20250929_photo_2025-09-29_12-38-49.thumb.jpg"},
    {"name":"琴烟（三点粉纯欲校花）","thumbnail":"https://neko-miaomiao.com/wp-content/uploads/2025/08/20250813_GUrU-ZNbEAA0aQd-1.thumb.jpg"},
]

ALICE_SEED_GIRLS = [
    {"name":"新人女孩葵（aoi）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2026/06/20260605_%E5%9B%BE%E7%89%87_20260606055218.thumb.jpg"},
    {"name":"新人女孩德莉莎（少女系-模特系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2026/05/20260531_997.thumb.jpg"},
    {"name":"小野猫（服务系-巨乳系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2025/11/20251107_IMAGE-2025-11-07-100825.thumb.jpg"},
    {"name":"新人女孩妮卡（萝莉系-嫩系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2025/08/20250825_images-1.thumb.jpg"},
    {"name":"新人女孩瑞贝卡（模特系-嫩系-女神系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2025/07/20250731_GwqhSc6bgAALaaa.thumb.jpg"},
    {"name":"新人女孩夏弥（嫩系-身材系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2025/06/20250628_%E5%9B%BE%E7%89%87_20250628001448.thumb.jpg"},
    {"name":"新人女孩绘梨衣（颜值系-可爱系）", "thumbnail":"https://ailisi99.com/wp-content/uploads/2025/04/20250702_GulmDEcXoAAzTL5.thumb.jpg"},
]

def http_text(url, timeout=18):
    req = Request(url, headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
        'Accept':'application/json,text/html,*/*',
        'Referer':NEKO_BASE_URL + '/',
    })
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or 'utf-8'
        return raw.decode(enc, 'replace'), r.headers.get_content_type()

def opener_text(opener, url, timeout=25):
    with opener.open(url, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or 'utf-8'
        return raw.decode(enc, 'replace'), r.geturl()

def http_bytes(url, timeout=25, referer=None):
    req = Request(url, headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
        'Accept':'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer':referer or (NEKO_BASE_URL + '/'),
    })
    with urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type() or ''

def normalize_avatar_name(s):
    s = str(s or '').strip().lower()
    table = str.maketrans({'織':'织','紅':'红','綾':'绫','蓮':'莲','煙':'烟'})
    s = s.translate(table)
    s = s.translate(str.maketrans({
        '\u611b':'\u7231', '\u9e97':'\u4e3d', '\u7d72':'\u4e1d',
        '\u5b78':'\u5b66', '\u5712':'\u56ed', '\u65af':'\u4e1d',
    }))
    s = re.sub(r'（[^）]*）|\([^)]*\)', '', s)
    s = re.sub(r'新人女孩|本人照片|超年轻嫩妹|自带房间|带房间|免费房间|回归|上班前预约立减\d+|性感日妹|童颜|巨乳|萝莉|模特|美女|妹妹|校花|傲娇|地雷系|长腿|纯欲|三点粉|双马尾|ss级|ss|s级|a级|伴游|top', '', s, flags=re.I)
    s = re.sub(r'[\s·・,，。/\\|:：;；!！?？~～\-—_]+', '', s)
    return s

def first_neko_image(item):
    pics = item.get('model_pics') or item.get('pics') or item.get('images') or item.get('photos') or []
    if isinstance(pics, list) and pics:
        first = pics[0]
        if isinstance(first, dict):
            for k in ('url','src','full','large','medium','thumbnail','thumb'):
                if first.get(k): return str(first.get(k))
        elif first:
            return str(first)
    for key in ('thumbnail','thumb','image','cover','avatar','photo','pic','featured_image','model_avatar','model_photo'):
        value = item.get(key)
        if isinstance(value, dict):
            for k in ('url','src','full','large','medium','thumbnail','thumb'):
                if value.get(k): return str(value.get(k))
        elif value:
            return str(value)
    embedded = item.get('_embedded') if isinstance(item.get('_embedded'), dict) else {}
    media = embedded.get('wp:featuredmedia') if embedded else None
    if isinstance(media, list) and media:
        first = media[0] if isinstance(media[0], dict) else {}
        for key in ('source_url','link'):
            if first.get(key): return str(first.get(key))
    return ''

def strip_html_text(value):
    value = re.sub(r'<[^>]+>', ' ', str(value or ''))
    value = re.sub(r'&nbsp;|&#160;', ' ', value)
    value = re.sub(r'&amp;', '&', value)
    value = re.sub(r'&lt;', '<', value)
    value = re.sub(r'&gt;', '>', value)
    return re.sub(r'\s+', ' ', value).strip()

def html_unescape(value):
    import html
    return html.unescape(str(value or '')).replace('\\/', '/')

def first_nonempty(*values):
    for value in values:
        text = strip_html_text(value)
        if text:
            return text
    return ''

def split_neko_name_remark(value):
    raw = strip_html_text(value)
    notes = []

    def take_note(match):
        note = strip_html_text(match.group(1) or match.group(2))
        if note:
            notes.append(note)
        return ' '

    name = re.sub(r'（([^）]+)）|\(([^)]+)\)', take_note, raw)
    name = re.sub(r'\s+', ' ', name).strip(' -/|｜')
    unique_notes = []
    for note in notes:
        if note and note not in unique_notes:
            unique_notes.append(note)
    return name or raw, ' / '.join(unique_notes)

def join_neko_notes(*values):
    notes = []
    for value in values:
        text = strip_html_text(value)
        if text and text not in notes:
            notes.append(text)
    return ' / '.join(notes)

def absolute_neko_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.startswith('//'):
        return 'https:' + value
    if value.startswith('/'):
        return urljoin(NEKO_BASE_URL + '/', value.lstrip('/'))
    return value

def absolute_alice_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.startswith('//'):
        return 'https:' + value
    if value.startswith('/'):
        return urljoin(ALICE_BASE_URL + '/', value.lstrip('/'))
    return value

def absolute_tokyo_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.startswith('//'):
        return 'https:' + value
    if value.startswith('/'):
        return urljoin(TOKYO_YY_BASE_URL + '/', value.lstrip('/'))
    return value

def tokyo_shop_url(shop_id=None):
    shop_id = str(shop_id or TOKYO_ALICE_SHOP_ID).strip()
    return TOKYO_YY_BASE_URL + '/%E5%8D%8E%E4%BA%BA%E5%87%BA%E5%BC%A0%E5%BA%97/' + quote(shop_id, safe='') + '/'

def tokyo_shop_api_url(shop_id=None):
    shop_id = str(shop_id or TOKYO_ALICE_SHOP_ID).strip()
    return TOKYO_YY_BASE_URL + '/api/shop/' + quote(shop_id, safe='')

def tokyo_chuqin_image_url(shop_id, filename):
    shop_id = str(shop_id or TOKYO_ALICE_SHOP_ID).strip()
    filename = str(filename or '').strip()
    if not filename:
        return ''
    if filename.startswith(('http://','https://','//')):
        return absolute_tokyo_url(filename)
    return TOKYO_YY_BASE_URL + '/data/chuqin/' + quote(shop_id, safe='') + '/' + quote(filename, safe='')

def clean_tokyo_girl_name(text):
    text = strip_html_text(text)
    if not text:
        return ''
    if re.search(r'今日出勤|招聘|价格|玩法|SYSTEM|制度|积分|充值|活动|盲盒|拍卖|已经结束|客服|公告', text, re.I):
        return ''
    return text

def fetch_tokyo_alice_girls():
    shop_id = TOKYO_ALICE_SHOP_ID
    url = tokyo_shop_api_url(shop_id)
    req = Request(url, headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
        'Accept':'application/json,text/plain,*/*',
        'Referer':tokyo_shop_url(shop_id),
    })
    with urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode(r.headers.get_content_charset() or 'utf-8', 'replace'))
    shop = data.get('shop') if isinstance(data, dict) else {}
    shop_id = str((shop or {}).get('shopId') or shop_id)
    items = []
    seen = set()
    for girl in (data.get('girls') or []):
        if not isinstance(girl, dict):
            continue
        name = clean_tokyo_girl_name(first_nonempty(girl.get('name'), girl.get('seo_name')))
        if not name:
            continue
        media = girl.get('media') or []
        if not isinstance(media, list):
            media = []
        media = [str(x or '').strip() for x in media if str(x or '').strip() and not re.search(r'\.(mp4|mov|avi|webm)(?:\?|$)', str(x), re.I)]
        if not media:
            continue
        first_img = next((x for x in media if '.480.' in x), media[0])
        img = tokyo_chuqin_image_url(shop_id, first_img)
        key = normalize_avatar_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        post_id = str(girl.get('post_id') or '')
        link = tokyo_shop_url(shop_id)
        if post_id:
            link = link + post_id + '-' + quote(str(girl.get('seo_name') or name), safe='') + '/'
        items.append({
            'name': name,
            'post_title': name,
            'thumbnail': img,
            'link': link,
            'referer': tokyo_shop_url(shop_id),
            'source': 'tokyo-yy-api',
            'status': girl.get('status') or '',
        })
    return items, 'tokyo-yy:' + shop_id

def clean_alice_card_name(text):
    text = strip_html_text(text)
    if not text:
        return ''
    if re.search(r'今日出勤|招聘|活动|制度|价格与玩法|SYSTEM|LINE|電話|电话|OPEN|Copyright|店家公告|联系方式|推荐酒店', text, re.I):
        return ''
    text = re.sub(r'^(?:sss级|ss级|s级|a级)\s*', '', text, flags=re.I)
    text = re.sub(r'^(?:一|一个)?小时\s*\d{4,6}\s*', '', text)
    text = re.sub(r'^\d{4,6}\s*(?:/h|円|日元)?\s*', '', text, flags=re.I)
    text = re.split(r'\s+\d{1,3}\s*歳|\s+\d{1,3}\s*岁', text, maxsplit=1)[0]
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def extract_alice_items_from_html(page_html):
    items = []
    seen = set()
    for block in re.findall(r'<a\b[^>]*class=["\'][^"\']*(?:cbox|__lazyloop_0)[^"\']*["\'][^>]*>.*?</a>', page_html, re.S | re.I):
        text = strip_html_text(html_unescape(block))
        name = clean_alice_card_name(text)
        if not name:
            continue
        img = ''
        im = re.search(r'<img\b[^>]*(?:data-original|data-src|src)=(["\'])(.*?)\1', block, re.S | re.I)
        if im:
            img = absolute_alice_url(html_unescape(im.group(2)))
        if not img or img.startswith('data:') or '/wp-content/uploads/' not in img:
            continue
        key = normalize_avatar_name(name)
        if key in seen:
            continue
        seen.add(key)
        items.append({'name': name, 'post_title': name, 'thumbnail': img, 'link': ALICE_BASE_URL + '/', 'source': 'alice-html'})
    return items

def fetch_alice_girls():
    errors = []
    try:
        items, source = fetch_tokyo_alice_girls()
        if items:
            fetch_alice_girls.last_errors = []
            return items, source
        errors.append({'url': tokyo_shop_api_url(), 'error': 'empty'})
    except Exception as e:
        errors.append({'url': tokyo_shop_api_url(), 'error': str(e)})
    urls = [
        ALICE_BASE_URL + '/',
        ALICE_BASE_URL + '/?rest_route=/wp/v2/model&per_page=100&_embed=1',
        ALICE_BASE_URL + '/wp-json/wp/v2/model?per_page=100&_embed=1',
    ]
    for url in urls:
        try:
            req = Request(url, headers={
                'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
                'Accept':'application/json,text/html,*/*',
                'Referer':ALICE_BASE_URL + '/',
            })
            with urlopen(req, timeout=25) as r:
                raw = r.read()
                ctype = r.headers.get_content_type() or ''
                text = raw.decode(r.headers.get_content_charset() or 'utf-8', 'replace')
            items = []
            if 'json' in ctype or text.lstrip().startswith(('[','{')):
                try:
                    data = json.loads(text)
                    for idx, item in enumerate(extract_neko_items(data)):
                        name = first_nonempty(item.get('post_title'), item.get('name'), item.get('model_name'))
                        img = absolute_alice_url(first_neko_image(item))
                        if name and img:
                            items.append({'name': name, 'post_title': name, 'thumbnail': img, 'link': absolute_alice_url(item.get('link') or item.get('url') or ''), 'source': 'alice-json'})
                except Exception as e:
                    errors.append({'url': url, 'error': 'json parse: ' + str(e)})
            else:
                items = extract_alice_items_from_html(text)
            if items:
                fetch_alice_girls.last_errors = []
                return items, url.replace(ALICE_BASE_URL, '').strip('/') or 'home'
            errors.append({'url': url, 'error': 'empty or protected'})
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})
    fetch_alice_girls.last_errors = errors[-5:]
    return ALICE_SEED_GIRLS, 'seed'
fetch_alice_girls.last_errors = []

def match_alice_girl(girl_name, alias, alice_items):
    wants = [normalize_avatar_name(girl_name), normalize_avatar_name(alias)]
    wants = [w for w in wants if w]
    best = None
    best_score = 0
    for item in alice_items:
        source_name = item.get('post_title') or item.get('name') or item.get('model_name') or ''
        n = normalize_avatar_name(source_name)
        if not n:
            continue
        score = 0
        for w in wants:
            if w == n:
                score = max(score, 110)
            elif len(w) >= 2 and (w in n or n in w):
                score = max(score, 85 + min(len(w), len(n)))
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 85 else None

def price_from_text(value, loose=False):
    text = strip_html_text(value)
    if not text:
        return 0
    has_hint = bool(re.search(r'¥|￥|円|日元|料金|価格|金額|定价|價格|价|費|费|price|fee|course|コース|小时|時間|hour|/h|每小时|万|w', text, re.I))
    if not loose and not has_hint:
        return 0
    course = re.search(r'(?:\d{2,3}\s*(?:/|分|min|分钟|m)\s*)[^\d¥￥]{0,12}(?:[¥￥]\s*)?(\d{4,6})(?:\s*(?:円|日元|JPY))?', text, re.I)
    if course:
        return int(course.group(1))
    candidates = re.findall(r'(?:[¥￥]\s*)?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s*(?:万|w|W))?(?:\s*(?:円|日元|JPY|/h|/H|/hour|每小时|小时|時間|h))?', text)
    for candidate in candidates:
        raw_num = re.search(r'\d[\d,]*(?:\.\d+)?', candidate)
        if not raw_num:
            continue
        numeric = float(raw_num.group(0).replace(',', ''))
        if re.search(r'万|w', candidate, re.I):
            price = int(round(numeric * 10000))
        elif numeric < 10:
            continue
        elif numeric < 100:
            if not loose:
                continue
            price = int(round(numeric * 1000))
        elif numeric < 1000:
            continue
        else:
            price = int(round(numeric))
        if price >= 1000 and (has_hint or price >= 10000):
            return price
    return 0

def neko_price(item):
    keys = (
        'price', 'model_price', 'service_price', 'list_price', 'fee', 'model_fee',
        'course_price', 'hour_price', 'hourly_price', 'per_hour', 'price_text',
        'model_price_text', 'model_cat', 'cat', 'category', 'model_category',
        'girl_category', 'girl_type', 'type', 'rank', 'class'
    )
    for key in keys:
        price = price_from_text(item.get(key), loose=True)
        if price:
            return price
    for key, value in item.items():
        if re.search(r'price|fee|料金|価格|金額|定价|價格|category|cat|type|rank|class', str(key), re.I):
            price = price_from_text(value, loose=True)
            if price:
                return price
    for key in ('model_brief', 'model_detail', 'post_excerpt', 'excerpt', 'description', 'remark', 'post_content', 'content'):
        price = price_from_text(item.get(key))
        if price:
            return price
    return 0

def neko_profile_from_item(item, index=0):
    raw_name = first_nonempty(item.get('post_title'), item.get('name'), item.get('model_name'))
    name, title_remark = split_neko_name_remark(raw_name)
    remark = join_neko_notes(title_remark, first_nonempty(
        item.get('model_brief'), item.get('model_detail'), item.get('post_excerpt'),
        item.get('excerpt'), item.get('description'), item.get('remark'),
        item.get('post_content'), item.get('content')))
    image = absolute_neko_url(first_neko_image(item))
    link = absolute_neko_url(item.get('link') or item.get('url') or '')
    if not link and item.get('post_name'):
        link = NEKO_BASE_URL + '/model/' + str(item.get('post_name')).strip('/').strip() + '/'
    return {
        'id': str(index + 1).zfill(3),
        'name': name,
        'remark': remark,
        'price': neko_price(item),
        'image': image,
        'link': link,
    }

def extract_neko_items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ('girls', 'models', 'items', 'posts', 'list', 'rows', 'results'):
        value = data.get(key)
        if isinstance(value, list):
            return value
    nested = data.get('data')
    if isinstance(nested, list):
        return nested
    if isinstance(nested, dict):
        return extract_neko_items(nested)
    return []

def neko_admin_credentials(username=None, password=None):
    user = str(username or os.environ.get('ALICE_NEKO_ADMIN_USER') or os.environ.get('NEKO_ADMIN_USER') or '').strip()
    pwd = str(password or os.environ.get('ALICE_NEKO_ADMIN_PASSWORD') or os.environ.get('NEKO_ADMIN_PASS') or os.environ.get('NEKO_ADMIN_PASSWORD') or '').strip()
    return user, pwd

def alice_wordpress_credentials():
    user = str(os.environ.get('ALICE_WP_ADMIN_USER') or '').strip()
    pwd = str(os.environ.get('ALICE_WP_ADMIN_PASSWORD') or '').strip()
    return user, pwd

def leading_zero_bits(data):
    n = 0
    for b in data:
        if b == 0:
            n += 8
            continue
        if b < 0x02: n += 7
        elif b < 0x04: n += 6
        elif b < 0x08: n += 5
        elif b < 0x10: n += 4
        elif b < 0x20: n += 3
        elif b < 0x40: n += 2
        elif b < 0x80: n += 1
        break
    return n

def solve_neko_pow(nonce, bits):
    counter = 0
    while True:
        digest = hashlib.sha256((str(nonce) + ':' + str(counter)).encode('utf-8')).digest()
        if leading_zero_bits(digest) >= int(bits or 0):
            return str(counter)
        counter += 1

def parse_input_attrs(tag):
    attrs = {}
    for m in re.finditer(r'([A-Za-z0-9_\-\[\]]+)\s*=\s*([\'"])(.*?)\2', str(tag or ''), re.S):
        attrs[m.group(1)] = html_unescape(m.group(3))
    return attrs

def neko_admin_login(username=None, password=None):
    import base64
    import http.cookiejar
    from urllib.request import build_opener, HTTPCookieProcessor
    user, pwd = neko_admin_credentials(username, password)
    if not user or not pwd:
        raise ValueError('未配置喵喵后台账号密码')
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.addheaders = [
        ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'),
        ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
        ('Referer', NEKO_BASE_URL + '/wp-login.php'),
    ]
    login_url = NEKO_BASE_URL + '/wp-login.php?redirect_to=' + quote(NEKO_BASE_URL + '/wp-admin/') + '&reauth=1'
    login_html, _ = opener_text(opener, login_url, timeout=30)
    data = {
        'log': user,
        'pwd': pwd,
        'wp-submit': '登录',
        'redirect_to': NEKO_BASE_URL + '/wp-admin/',
        'testcookie': '1',
        'rememberme': 'forever',
    }
    for tag in re.findall(r'<input\b[^>]*>', login_html, re.I | re.S):
        attrs = parse_input_attrs(tag)
        name = attrs.get('name') or ''
        if name.startswith('pow_challenge['):
            idx = re.search(r'\[(\d+)\]', name)
            challenge = attrs.get('value') or ''
            if not idx or not challenge:
                continue
            prefix = challenge.split('.', 1)[0]
            padded = prefix + ('=' * ((4 - len(prefix) % 4) % 4))
            decoded = base64.urlsafe_b64decode(padded.encode('utf-8')).decode('utf-8', 'ignore')
            parts = decoded.split(':')
            if len(parts) >= 3:
                data[name] = challenge
                data['pow_solution[' + idx.group(1) + ']'] = solve_neko_pow(parts[0], int(parts[-1]))
    body = urlencode(data).encode('utf-8')
    req = Request(NEKO_BASE_URL + '/wp-login.php', data=body, method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': login_url,
    })
    opener.open(req, timeout=30).read()
    admin_html, admin_url = opener_text(opener, NEKO_BASE_URL + '/wp-admin/', timeout=30)
    if 'wpbody-content' not in admin_html and 'wp-admin-bar' not in admin_html:
        err = ''
        m = re.search(r'<div[^>]+id=["\']login_error["\'][^>]*>(.*?)</div>', admin_html, re.S | re.I)
        if m:
            err = strip_html_text(html_unescape(m.group(1)))
        raise ValueError(err or '喵喵后台登录失败')
    return opener

def alice_wordpress_login(username=None, password=None):
    import base64
    import http.cookiejar
    from urllib.request import build_opener, HTTPCookieProcessor
    user = str(username or '').strip()
    pwd = str(password or '').strip()
    if not user or not pwd:
        raise ValueError('官网同步尚未配置后台账号密码')
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.addheaders = [
        ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'),
        ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
        ('Referer', ALICE_BASE_URL + '/wp-login.php'),
    ]
    login_url = ALICE_BASE_URL + '/wp-login.php?redirect_to=' + quote(ALICE_BASE_URL + '/wp-admin/') + '&reauth=1'
    login_html, _ = opener_text(opener, login_url, timeout=30)
    data = {'log': user, 'pwd': pwd, 'wp-submit': '登录', 'redirect_to': ALICE_BASE_URL + '/wp-admin/',
            'testcookie': '1', 'rememberme': 'forever'}
    for tag in re.findall(r'<input\b[^>]*>', login_html, re.I | re.S):
        attrs = parse_input_attrs(tag)
        name = attrs.get('name') or ''
        if not name.startswith('pow_challenge['):
            continue
        idx = re.search(r'\[(\d+)\]', name)
        challenge = attrs.get('value') or ''
        if not idx or not challenge:
            continue
        prefix = challenge.split('.', 1)[0]
        padded = prefix + ('=' * ((4 - len(prefix) % 4) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode('utf-8')).decode('utf-8', 'ignore')
        parts = decoded.split(':')
        if len(parts) >= 3:
            data[name] = challenge
            data['pow_solution[' + idx.group(1) + ']'] = solve_neko_pow(parts[0], int(parts[-1]))
    req = Request(ALICE_BASE_URL + '/wp-login.php', data=urlencode(data).encode('utf-8'), method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'Referer': login_url})
    opener.open(req, timeout=30).read()
    admin_html, _ = opener_text(opener, ALICE_BASE_URL + '/wp-admin/', timeout=30)
    if 'wpbody-content' not in admin_html and 'wp-admin-bar' not in admin_html:
        raise ValueError('官网后台登录失败，请检查 Render 中的账号密码')
    return opener

def _wordpress_form_pairs(edit_html):
    form = re.search(r'<form\b[^>]*(?:id|name)=["\']post["\'][^>]*>(.*?)</form>', edit_html, re.S | re.I)
    body = form.group(1) if form else edit_html
    pairs = []
    for tag in re.findall(r'<input\b[^>]*>', body, re.I | re.S):
        attrs = parse_input_attrs(tag)
        name = attrs.get('name') or ''
        kind = str(attrs.get('type') or 'text').lower()
        if not name or kind in ('submit', 'button', 'file', 'image', 'reset'):
            continue
        if kind in ('checkbox', 'radio') and not re.search(r'\bchecked\b', tag, re.I):
            continue
        pairs.append((name, attrs.get('value') or ''))
    for match in re.finditer(r'<textarea\b([^>]*)>(.*?)</textarea>', body, re.S | re.I):
        attrs = parse_input_attrs('<textarea ' + match.group(1) + '>')
        if attrs.get('name'):
            pairs.append((attrs['name'], html_unescape(match.group(2))))
    for match in re.finditer(r'<select\b([^>]*)>(.*?)</select>', body, re.S | re.I):
        attrs = parse_input_attrs('<select ' + match.group(1) + '>')
        name = attrs.get('name') or ''
        if not name:
            continue
        options = re.findall(r'<option\b([^>]*)>(.*?)</option>', match.group(2), re.S | re.I)
        selected = [x for x in options if re.search(r'\bselected\b', x[0], re.I)] or options[:1]
        for option_attrs, option_text in selected:
            oa = parse_input_attrs('<option ' + option_attrs + '>')
            pairs.append((name, oa.get('value') if 'value' in oa else strip_html_text(option_text)))
    return pairs

def _acf_gallery_field_key(edit_html):
    starts = list(re.finditer(r'<div\b[^>]*class=["\'][^"\']*acf-field[^"\']*["\'][^>]*>', edit_html, re.S | re.I))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else min(len(edit_html), match.end() + 12000)
        block = edit_html[match.start():end]
        if '照片(可以添加多个照片)' not in block and 'acf-photo-gallery' not in block:
            continue
        attrs = parse_input_attrs(match.group(0))
        key = attrs.get('data-key') or ''
        if key.startswith('field_'):
            return key
        name_match = re.search(r'name=["\']acf\[(field_[^\]]+)\]["\']', block, re.I)
        if name_match:
            return name_match.group(1)
    return ''

def _wordpress_photo_gallery_field_name(edit_html, gallery_key):
    pairs = _wordpress_form_pairs(edit_html)
    declared_key = next((value for key, value in pairs if key == 'acf-photo-gallery-field'), '')
    groups = [str(value or '').strip() for key, value in pairs
              if key == 'acf-photo-gallery-groups[]' and str(value or '').strip()]
    if declared_key and declared_key != gallery_key:
        return ''
    return groups[0] if groups else ''

def _wordpress_photo_gallery_attachment_ids(edit_html):
    return [int(value) for value in re.findall(r'acf-photo-gallery-mediabox-(\d+)', str(edit_html or ''), re.I)]

def _wordpress_rest_nonce(edit_html):
    patterns = [
        r'wpApiSettings\s*=\s*\{.*?["\']nonce["\']\s*:\s*["\']([^"\']+)',
        r'["\']wpApiSettings["\']\s*:\s*\{.*?["\']nonce["\']\s*:\s*["\']([^"\']+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, str(edit_html or ''), re.I | re.S)
        if match:
            return html_unescape(match.group(1)).replace('\\/', '/')
    return ''

def _wordpress_rest_upload_image(opener, edit_html, edit_url, day, image_bytes, page_index=0, page_count=1,
                                 filename='', content_type='image/png'):
    nonce = _wordpress_rest_nonce(edit_html)
    if not nonce:
        return 0
    suffix = f'-{page_index + 1}' if page_count > 1 else ''
    filename = filename or f'alice-attendance-{day}{suffix}.png'
    req = Request(ALICE_BASE_URL + '/wp-json/wp/v2/media', data=image_bytes, method='POST', headers={
        'Content-Type': content_type, 'Content-Disposition': f'attachment; filename="{filename}"',
        'X-WP-Nonce': nonce, 'Referer': edit_url, 'Accept': 'application/json'})
    with opener.open(req, timeout=90) as response:
        payload = json.loads(response.read().decode(response.headers.get_content_charset() or 'utf-8', 'replace'))
    return int(payload.get('id') or 0)

def _multipart_request(url, fields, filename, image_bytes, opener, referer, content_type='image/png'):
    boundary = 'AliceWpBoundary' + secrets.token_hex(12)
    parts = []
    for key, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="async-upload"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode('utf-8'))
    parts.append(image_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    req = Request(url, data=b''.join(parts), method='POST', headers={
        'Content-Type': f'multipart/form-data; boundary={boundary}', 'Referer': referer,
        'Accept': 'application/json,text/plain,*/*'})
    with opener.open(req, timeout=60) as response:
        return response.read().decode('utf-8', 'replace')

def _wordpress_girl_key(value):
    text = unicodedata.normalize('NFKC', str(value or '')).lower()
    text = re.sub(r'[（(【\[].*?[）)】\]]', '', text)
    text = re.sub(r'(?:新人女孩|新人女优|新人|女孩|回归|復帰)', '', text)
    return re.sub(r'[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+', '', text)

def _wordpress_model_posts(opener):
    base = ALICE_BASE_URL + '/wp-admin/edit.php?post_type=model'
    posts, seen, inline_nonce = [], set(), ''
    page = 1
    max_page = 1
    while page <= max_page and page <= 30:
        list_url = base + '&paged=' + str(page)
        html, _ = opener_text(opener, list_url, timeout=40)
        if page == 1:
            page_numbers = [int(x) for x in re.findall(r'(?:[?&]|&amp;|&#038;)paged=(\d+)', html)]
            max_page = max(page_numbers or [1])
        if not inline_nonce:
            for tag in re.findall(r'<input\b[^>]*>', html, re.I | re.S):
                attrs = parse_input_attrs(tag)
                if attrs.get('name') == '_inline_edit' or attrs.get('id') == '_inline_edit':
                    inline_nonce = attrs.get('value') or ''
                    break
        found = 0
        for match in re.finditer(r'<tr\b([^>]*)\bid=["\']post-(\d+)["\']([^>]*)>(.*?)</tr>', html, re.I | re.S):
            post_id = int(match.group(2))
            if post_id in seen:
                continue
            row_attrs = (match.group(1) or '') + ' ' + (match.group(3) or '')
            row_html = match.group(4) or ''
            title_match = re.search(r'<a\b[^>]*class=["\'][^"\']*row-title[^"\']*["\'][^>]*>(.*?)</a>', row_html, re.I | re.S)
            if not title_match:
                title_match = re.search(r'<a\b[^>]*href=["\'][^"\']*post=' + str(post_id) + r'[^"\']*["\'][^>]*>(.*?)</a>', row_html, re.I | re.S)
            title = strip_html_text(html_unescape(title_match.group(1))) if title_match else ''
            status_match = re.search(r'\bstatus-([a-z_-]+)', row_attrs, re.I)
            status = (status_match.group(1).lower() if status_match else
                      ('private' if re.search(r'(?:—|&mdash;)\s*私密', row_html, re.I) else
                       ('draft' if re.search(r'(?:—|&mdash;)\s*草稿', row_html, re.I) else 'publish')))
            category_html_match = re.search(
                r'<td\b[^>]*class=["\'][^"\']*taxonomy-model_category[^"\']*["\'][^>]*>(.*?)</td>',
                row_html, re.I | re.S)
            category_text = strip_html_text(html_unescape(category_html_match.group(1))) if category_html_match else ''
            category_prices = {int(x) for x in re.findall(r'(?<!\d)([1-9][0-9]{4,5})(?!\d)', category_text)}
            if title:
                posts.append({'id': post_id, 'title': title, 'status': status, 'list_url': list_url,
                              'category_text': category_text, 'category_prices': sorted(category_prices)})
                seen.add(post_id)
                found += 1
        if not found and page > 1:
            break
        page += 1
    if not inline_nonce:
        raise ValueError('找不到官网女孩列表的快速编辑授权码')
    return posts, inline_nonce

def _wordpress_inline_model_status(opener, post, desired_status, inline_nonce, category_term_id=None):
    data = {
        'action': 'inline-save', '_inline_edit': inline_nonce, 'post_type': 'model',
        'post_ID': str(post['id']), 'post_title': post['title'],
        '_status': 'publish' if desired_status in ('publish', 'private') else desired_status,
        'screen': 'edit-model', 'post_view': 'list', 'edit_date': 'true',
    }
    if desired_status == 'private':
        data['keep_private'] = 'private'
    if category_term_id:
        data['tax_input[model_category][]'] = str(int(category_term_id))
    req = Request(ALICE_BASE_URL + '/wp-admin/admin-ajax.php', data=urlencode(data).encode('utf-8'),
                  method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded',
                                          'Referer': post.get('list_url') or ALICE_BASE_URL + '/wp-admin/edit.php?post_type=model'})
    with opener.open(req, timeout=45) as response:
        result = response.read().decode(response.headers.get_content_charset() or 'utf-8', 'replace').strip()
    if result in ('', '0', '-1') or ('post-' + str(post['id'])) not in result:
        raise ValueError('官网没有确认女孩状态更新成功')
    is_private = bool(re.search(r'(?:status-private|(?:—|&mdash;)\s*私密)', result, re.I))
    if (desired_status == 'private') != is_private:
        raise ValueError('官网返回文章行，但公开/私密状态没有改变')

def _wordpress_model_price_terms(opener, create_prices=None):
    terms_url = ALICE_BASE_URL + '/wp-admin/edit-tags.php?taxonomy=model_category&post_type=model'
    html, _ = opener_text(opener, terms_url, timeout=40)

    def parse_terms(source):
        found = {}
        for match in re.finditer(r'<tr\b[^>]*\bid=["\']tag-(\d+)["\'][^>]*>(.*?)</tr>', source, re.I | re.S):
            title_match = re.search(r'class=["\'][^"\']*row-title[^"\']*["\'][^>]*>(.*?)</a>', match.group(2), re.I | re.S)
            label = strip_html_text(html_unescape(title_match.group(1))) if title_match else ''
            price_match = re.search(r'(?<!\d)([1-9][0-9]{4,5})(?!\d)', label)
            if price_match:
                found[int(price_match.group(1))] = {'id': int(match.group(1)), 'label': label}
        # 父类别下拉包含全部分页项目，用它补齐第二页及以后价格分类。
        for match in re.finditer(r'<option\b[^>]*value=["\'](\d+)["\'][^>]*>(.*?)</option>', source, re.I | re.S):
            label = strip_html_text(html_unescape(match.group(2)))
            price_match = re.search(r'(?<!\d)([1-9][0-9]{4,5})(?!\d)', label)
            if price_match:
                found[int(price_match.group(1))] = {'id': int(match.group(1)), 'label': label}
        return found

    terms = parse_terms(html)
    missing = sorted({int(x) for x in (create_prices or []) if int(x or 0) > 0} - set(terms))
    if missing:
        nonce_match = re.search(r'name=["\']_wpnonce_add-tag["\'][^>]*value=["\']([^"\']+)', html, re.I)
        if not nonce_match:
            raise ValueError('找不到官网新增价格分类的授权码')
        nonce = html_unescape(nonce_match.group(1))
        for price in missing:
            label = f'一小时{price}'
            payload = [('action', 'add-tag'), ('screen', 'edit-model_category'),
                       ('taxonomy', 'model_category'), ('post_type', 'model'),
                       ('tag-name', label), ('slug', ''), ('description', ''),
                       ('_wpnonce_add-tag', nonce), ('_wp_http_referer', '/wp-admin/edit-tags.php?taxonomy=model_category&post_type=model')]
            req = Request(ALICE_BASE_URL + '/wp-admin/edit-tags.php', data=urlencode(payload).encode('utf-8'),
                          method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': terms_url})
            with opener.open(req, timeout=40) as response:
                response.read()
        html, _ = opener_text(opener, terms_url + '&alice_refresh=1', timeout=40)
        terms = parse_terms(html)
    return terms

def sync_alice_wordpress_girl_prices(opener, girl_prices):
    managed = []
    managed_keys = {}
    for name, value in (girl_prices or {}).items():
        detail = value if isinstance(value, dict) else {'price': value}
        price = int(detail.get('price') or 0)
        info = {'name': str(name).strip(), 'price': price, 'keys': []}
        aliases = [detail.get('alias') or '']
        aliases.extend(re.split(r'[,，、/]+', str(detail.get('alias') or '')))
        for candidate in [name] + aliases:
            key = _wordpress_girl_key(candidate)
            if key and key not in info['keys']:
                info['keys'].append(key)
                managed_keys[key] = info
        if info['keys'] and price > 0:
            managed.append(info)
    posts, nonce = _wordpress_model_posts(opener)
    terms = _wordpress_model_price_terms(opener, [x['price'] for x in managed])
    matches = {}
    for post in posts:
        info = managed_keys.get(_wordpress_girl_key(post['title']))
        if info:
            matches.setdefault(info['name'], []).append(post)
    result = {'synced': True, 'matched': 0, 'updated': 0, 'unchanged': 0,
              'failed': [], 'unmatched': [], 'missing_terms': []}
    for info in managed:
        candidates = sorted(matches.get(info['name'], []), key=lambda x: x['id'], reverse=True)
        if not candidates:
            result['unmatched'].append(info['name'])
            continue
        term = terms.get(info['price'])
        if not term:
            result['missing_terms'].append(info['price'])
            continue
        result['matched'] += 1
        for post in candidates:
            if post.get('category_prices') == [info['price']]:
                result['unchanged'] += 1
                continue
            try:
                _wordpress_inline_model_status(opener, post, post.get('status') or 'publish', nonce, term['id'])
                result['updated'] += 1
            except Exception as exc:
                result['failed'].append({'girl': info['name'], 'post_id': post['id'], 'error': str(exc)})
    result['synced'] = not result['failed'] and not result['missing_terms']
    result['missing_terms'] = sorted(set(result['missing_terms']))
    return result

def sync_alice_wordpress_girl_visibility(opener, attendance_names, all_girl_names):
    attendance_keys = {_wordpress_girl_key(x) for x in (attendance_names or []) if _wordpress_girl_key(x)}
    posts, nonce = _wordpress_model_posts(opener)
    matches = {}
    for post in posts:
        key = _wordpress_girl_key(post['title'])
        if key:
            matches.setdefault(key, []).append(post)
    result = {'synced': True, 'matched': 0, 'published': 0, 'privated': 0, 'unchanged': 0,
              'failed': [], 'unmatched_attendance': []}
    # 官网女孩管理里的全部 model 都由当天出勤控制：出勤者仅保留最新的一篇公开，
    # 其余女孩及同名旧文章全部私密，避免 MCR 名单之外的旧资料继续公开。
    for key, candidates in matches.items():
        candidates = sorted(candidates, key=lambda x: x['id'], reverse=True)
        result['matched'] += 1
        for index, post in enumerate(candidates):
            desired = 'publish' if key in attendance_keys and index == 0 else 'private'
            if post['status'] == desired:
                result['unchanged'] += 1
                continue
            try:
                _wordpress_inline_model_status(opener, post, desired, nonce)
                if desired == 'publish':
                    result['published'] += 1
                else:
                    result['privated'] += 1
            except Exception as exc:
                result['failed'].append({'girl': post.get('title') or key, 'post_id': post['id'], 'error': str(exc)})
    for name in attendance_names or []:
        if _wordpress_girl_key(name) not in matches:
            result['unmatched_attendance'].append(str(name or '').strip())
    result['synced'] = not result['failed']
    return result

def publish_girl_praise_to_wordpress(praise_id):
    user, pwd = alice_wordpress_credentials()
    if not user or not pwd:
        raise ValueError('Render 尚未设置官网账号密码')
    with conn() as c:
        praise = c.execute('SELECT * FROM girl_praises WHERE id=?', (int(praise_id),)).fetchone()
    if not praise:
        raise ValueError('好评图片不存在')
    praise = dict(praise)
    image_bytes = bytes(praise.get('image_blob') or b'')
    if not image_bytes:
        filename = Path(str(praise.get('image_path') or '')).name
        for folder in (GIRL_PRAISE_DIR, LEGACY_GIRL_PRAISE_DIR):
            candidate = folder / filename
            if candidate.is_file():
                image_bytes = candidate.read_bytes()
                break
    if not image_bytes:
        raise ValueError('好评图片文件已丢失')
    opener = alice_wordpress_login(user, pwd)
    posts, _inline_nonce = _wordpress_model_posts(opener)
    girl_key = _wordpress_girl_key(praise.get('girl_name'))
    candidates = [post for post in posts if _wordpress_girl_key(post.get('title')) == girl_key]
    if not candidates:
        raise ValueError(f"官网女孩管理中找不到“{praise.get('girl_name')}”")
    post = sorted(candidates, key=lambda item: item['id'], reverse=True)[0]
    edit_url = f"{ALICE_BASE_URL}/wp-admin/post.php?post={post['id']}&action=edit"
    edit_html, _ = opener_text(opener, edit_url, timeout=40)
    gallery_key = _acf_gallery_field_key(edit_html)
    gallery_name = _wordpress_photo_gallery_field_name(edit_html, gallery_key) if gallery_key else ''
    if not gallery_key or not gallery_name:
        raise ValueError('官网女孩页面找不到照片相册字段')
    mime = str(praise.get('image_mime') or 'image/png')
    extension = {'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}.get(mime, '.png')
    upload_name = f"alice-praise-{post['id']}-{praise['id']}{extension}"
    try:
        attachment_id = _wordpress_rest_upload_image(
            opener, edit_html, edit_url, '', image_bytes, filename=upload_name, content_type=mime)
    except Exception:
        # 部分 WordPress 主机关闭 REST 媒体写入，下面自动改走后台原生上传接口。
        attachment_id = 0
    if not attachment_id:
        media_html, _ = opener_text(opener, ALICE_BASE_URL + '/wp-admin/media-new.php', timeout=35)
        nonce_match = re.search(r'<input\b[^>]*name=["\']_wpnonce["\'][^>]*value=["\']([^"\']+)', media_html, re.I)
        if not nonce_match:
            raise ValueError('找不到官网图片上传授权码')
        upload_raw = _multipart_request(ALICE_BASE_URL + '/wp-admin/async-upload.php', [
            ('name', upload_name), ('action', 'upload-attachment'),
            ('_wpnonce', html_unescape(nonce_match.group(1)))
        ], upload_name, image_bytes, opener, edit_url, mime)
        upload_data = json.loads(upload_raw)
        attachment_id = int(((upload_data.get('data') or {}).get('id')) or 0)
        if not upload_data.get('success') or not attachment_id:
            raise ValueError(((upload_data.get('data') or {}).get('message')) or '官网图片上传失败')
    existing_ids = _wordpress_photo_gallery_attachment_ids(edit_html)
    gallery_ids = list(dict.fromkeys(existing_ids + [attachment_id]))
    pairs = _wordpress_form_pairs(edit_html)
    replace_names = {f'acf[{gallery_key}]', gallery_name, gallery_name + '[]', 'action', 'post_ID'}
    pairs = [(key, value) for key, value in pairs if key not in replace_names]
    pairs.extend([('action', 'editpost'), ('post_ID', str(post['id']))])
    pairs.extend((gallery_name + '[]', str(media_id)) for media_id in gallery_ids)
    pairs.append(('save', '更新'))
    req = Request(ALICE_BASE_URL + '/wp-admin/post.php', data=urlencode(pairs, doseq=True).encode('utf-8'),
                  method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': edit_url})
    with opener.open(req, timeout=60) as response:
        response.read()
    verify_html, _ = opener_text(opener, edit_url + '&alice_praise_verify=1', timeout=40)
    if attachment_id not in _wordpress_photo_gallery_attachment_ids(verify_html):
        raise ValueError('官网没有确认好评图片已加入女孩相册')
    with conn() as c:
        c.execute("""UPDATE girl_praises SET publish_status='已上架',wp_post_id=?,wp_attachment_id=?,
                     published_at=CURRENT_TIMESTAMP,publish_error='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (post['id'], attachment_id, int(praise_id)))
    return {'praise_id': int(praise_id), 'girl_name': praise.get('girl_name'),
            'wp_post_id': int(post['id']), 'wp_attachment_id': int(attachment_id), 'status': '已上架'}

REVIEW_FEATURE_WORDS = {
    '温柔': ('温柔','温和','優しい','やさしい'), '聊天自然': ('聊天','健谈','会話','話しやす'),
    '服务细心': ('细心','贴心','周到','丁寧','気遣い'), '可爱': ('可爱','可愛い','かわいい'),
    '颜值出众': ('漂亮','美女','颜值','綺麗','美人'), '活泼': ('活泼','元气','明るい','元気'),
    '放松感': ('放松','舒服','治愈','癒し','リラックス'), '按摩': ('按摩','マッサージ'),
}

def _safe_public_url(value):
    url = str(value or '').strip()
    parsed = urlparse(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname:
        raise ValueError('请输入公开网站的 http/https 地址')
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f'网站地址无法解析：{exc}')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError('只能采集公开网站，不能访问本机或内网地址')
    return url

def _review_tags(text):
    return [label for label, words in REVIEW_FEATURE_WORDS.items() if any(word.lower() in text.lower() for word in words)]

def _review_source_identity(value):
    url = str(value or '').strip()
    parsed = urlparse(url)
    match = re.search(r'/(?:精华帖/)?(\d{4,})(?:[_/-]|$)', unquote(parsed.path))
    if match and parsed.hostname and 'tokyo-yy.com' in parsed.hostname.lower():
        return 'tokyo-report:' + match.group(1)
    return (parsed.scheme.lower()+'://'+parsed.netloc.lower()+parsed.path.rstrip('/')) if parsed.netloc else url

TOKYO_REPORT_XOR_KEY = b'Yk9zQ2h0RjdtVnBMejRYZ1R1RjhNMXZSYmdBcWVIeXJEdE5uV3VCc1BkVUk='

def _decode_tokyo_report_payload(payload):
    """Decode the public report API payload in the same way as Tokyo YY's browser JavaScript."""
    encoded = payload.get('data') if isinstance(payload, dict) else payload
    raw = base64.b64decode(str(encoded or ''), validate=True)
    clear = bytes(value ^ TOKYO_REPORT_XOR_KEY[index % len(TOKYO_REPORT_XOR_KEY)]
                  for index, value in enumerate(raw))
    result = json.loads(clear.decode('utf-8'))
    if not isinstance(result, dict):
        raise ValueError('东京夜游网评论数据格式异常')
    report = result.get('report')
    return report if isinstance(report, dict) else result

def _review_girl_name(text_value, girl_rows):
    normalized = unicodedata.normalize('NFKC', str(text_value or '')).lower().replace(' ', '')
    for girl in girl_rows:
        aliases = [girl.get('name'), girl.get('girl_alias')]
        if any(_wordpress_girl_key(alias) and _wordpress_girl_key(alias) in normalized for alias in aliases):
            return girl.get('name') or ''
    return ''

def _store_scraped_reviews(collected):
    inserted = updated = 0
    with conn() as c:
        for item in collected:
            old = c.execute('SELECT id FROM scraped_reviews WHERE review_hash=?', (item['review_hash'],)).fetchone()
            values = (item.get('girl_name',''), item.get('tags',''), item.get('source_page',''),
                      item.get('review_date',''), item.get('material_type','公开素材'),
                      item.get('author_name',''), item.get('source_title',''), item.get('access_scope','public'))
            if old:
                c.execute("""UPDATE scraped_reviews SET girl_name=?,tags=?,source_page=?,review_date=?,material_type=?,
                             author_name=?,source_title=?,access_scope=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                          values + (old['id'],))
                updated += 1
            else:
                c.execute("""INSERT INTO scraped_reviews(source_url,source_page,girl_name,review_text,rating,review_date,
                             tags,review_hash,material_type,author_name,source_title,access_scope)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (item.get('source_url',''), item.get('source_page',''), item.get('girl_name',''),
                           item.get('review_text',''), float(item.get('rating') or 0), item.get('review_date',''),
                           item.get('tags',''), item['review_hash'], item.get('material_type','公开素材'),
                           item.get('author_name',''), item.get('source_title',''), item.get('access_scope','public')))
                inserted += 1
    return inserted, updated

def _tokyo_yy_report_materials(start_url, max_pages, girl_rows):
    """Collect only previews exposed by Tokyo YY's public report API; full member text stays manual."""
    parsed = urlparse(start_url)
    if parsed.hostname not in ('tokyo-yy.com', 'www.tokyo-yy.com'):
        return None
    decoded_path = unquote(parsed.path)
    if '/精华帖/' not in decoded_path and '/report' not in decoded_path.lower():
        return None
    report_match = re.search(r'/精华帖/(\d+)', decoded_path)
    report_type = 'jp' if ('日本' in decoded_path or 'type=jp' in parsed.query) else 'cn'
    api_pages = []
    if report_match:
        report_batches = [[{'post_id': int(report_match.group(1))}]]
    else:
        list_url = f'{TOKYO_YY_BASE_URL}/api/report?type={report_type}'
        req = Request(_safe_public_url(list_url), headers={
            'User-Agent':'AliceReviewResearchBot/1.0 (+public review research; low frequency)',
            'Accept':'application/json'})
        with urlopen(req, timeout=25) as response:
            listing = json.loads(response.read(2_000_000).decode('utf-8'))
        latest_page = int(listing.get('page') or 0)
        report_batches = [listing.get('reports') or []]
        api_pages.append(latest_page)
        for offset in range(1, max_pages):
            page_no = latest_page - offset
            if page_no < 0:
                break
            page_url = f'{TOKYO_YY_BASE_URL}/api/report?type={report_type}&page={page_no}'
            req = Request(_safe_public_url(page_url), headers={
                'User-Agent':'AliceReviewResearchBot/1.0 (+public review research; low frequency)',
                'Accept':'application/json'})
            with urlopen(req, timeout=25) as response:
                page_data = json.loads(response.read(2_000_000).decode('utf-8'))
            report_batches.append(page_data.get('reports') or [])
            api_pages.append(page_no)
    collected = []
    for batch in report_batches:
        for summary in batch:
            post_id = int(summary.get('post_id') or 0)
            if not post_id:
                continue
            detail_url = f'{TOKYO_YY_BASE_URL}/api/report/{post_id}?v=3'
            req = Request(_safe_public_url(detail_url), headers={
                'User-Agent':'AliceReviewResearchBot/1.0 (+public review research; low frequency)',
                'Accept':'application/json'})
            with urlopen(req, timeout=25) as response:
                detail_raw = response.read(2_000_000).decode('utf-8').strip()
            try:
                detail_payload = json.loads(detail_raw)
            except json.JSONDecodeError:
                # This endpoint currently returns the encoded value as text/plain.
                detail_payload = detail_raw
            detail = _decode_tokyo_report_payload(detail_payload)
            preview = re.sub(r'\s+', ' ', str(detail.get('preview') or '')).strip()
            title = re.sub(r'\s+', ' ', str(detail.get('title') or summary.get('title') or '')).strip()
            # The public API deliberately separates preview from gated full content. Never archive detail.content here.
            if len(preview) < 12:
                continue
            source_page = f'{TOKYO_YY_BASE_URL}/精华帖/{post_id}_{quote(title, safe="")}/'
            girl_name = _review_girl_name(title + ' ' + preview, girl_rows)
            digest = hashlib.sha256((source_page+'\n'+preview).encode('utf-8')).hexdigest()
            collected.append({
                'source_url':start_url, 'source_page':source_page, 'source_title':title,
                'girl_name':girl_name, 'review_text':preview, 'tags':','.join(_review_tags(title+' '+preview)),
                'review_hash':digest, 'review_date':str(detail.get('report_date') or summary.get('report_date') or ''),
                'author_name':str(detail.get('author_name') or summary.get('author_name') or ''),
                'material_type':'公开长评预览', 'access_scope':'public'
            })
            time.sleep(.08)
    inserted, updated = _store_scraped_reviews(collected)
    return {'pages': max(1, len(api_pages)), 'found':len(collected), 'inserted':inserted,
            'updated':updated, 'mode':'东京夜游网公开数据接口'}

def _tokyo_yy_home_materials(start_url, max_pages, girl_rows):
    """Archive published short comments shown below Alice girls on Tokyo YY's public home/detail pages."""
    parsed = urlparse(start_url)
    if parsed.hostname not in ('tokyo-yy.com', 'www.tokyo-yy.com') or parsed.path.rstrip('/'):
        return None
    headers = {'User-Agent':'AliceReviewResearchBot/1.0 (+public review research; low frequency)',
               'Accept':'application/json'}
    list_url = f'{TOKYO_YY_BASE_URL}/api/homepage/all-girls'
    with urlopen(Request(_safe_public_url(list_url), headers=headers), timeout=30) as response:
        listing = json.loads(response.read(5_000_000).decode('utf-8'))
    target_shop = _wordpress_girl_key(TOKYO_ALICE_SHOP_ID)
    external_girls = [item for item in (listing.get('girls') or [])
                      if _wordpress_girl_key(item.get('shopId')) == target_shop and int(item.get('comment_count') or 0) > 0]
    collected = []
    visited_pages = 1
    for external in external_girls:
        post_id = int(external.get('post_id') or 0)
        if not post_id:
            continue
        meta_url = f'{TOKYO_YY_BASE_URL}/api/comment/{post_id}/meta'
        with urlopen(Request(_safe_public_url(meta_url), headers=headers), timeout=20) as response:
            meta = json.loads(response.read(200_000).decode('utf-8'))
        total_pages = min(max_pages, max(1, int(meta.get('totalPages') or 1)))
        title = re.sub(r'\s+', ' ', str(external.get('name') or external.get('seo_name') or '')).strip()
        shop_id = quote(str(external.get('shopId') or TOKYO_ALICE_SHOP_ID), safe='')
        source_page = f'{TOKYO_YY_BASE_URL}/华人出张店/{shop_id}/{post_id}-{quote(title, safe="")}/'
        girl_name = _review_girl_name(title, girl_rows)
        for page_no in range(total_pages):
            comments_url = f'{TOKYO_YY_BASE_URL}/api/comment/{post_id}/{page_no}'
            with urlopen(Request(_safe_public_url(comments_url), headers=headers), timeout=20) as response:
                raw = response.read(2_000_000).decode('utf-8').strip()
            comments = (_decode_tokyo_report_payload(raw).get('comments') or [])
            visited_pages += 1
            for comment in comments:
                # Only use comments the site's public data marks as published.
                if str(comment.get('status') or '').lower() != 'publish':
                    continue
                content = re.sub(r'\s+', ' ', str(comment.get('content') or '')).strip()
                if len(content) < 8:
                    continue
                digest = hashlib.sha256((source_page+'\ncomment:'+str(comment.get('id') or '')+'\n'+content).encode('utf-8')).hexdigest()
                collected.append({
                    'source_url':start_url, 'source_page':source_page, 'source_title':title,
                    'girl_name':girl_name, 'review_text':content,
                    'tags':','.join(_review_tags(title+' '+content)), 'review_hash':digest,
                    'review_date':str(comment.get('date') or ''), 'author_name':str(comment.get('username') or ''),
                    'material_type':'公开短评', 'access_scope':'public'
                })
            time.sleep(.08)
    inserted, updated = _store_scraped_reviews(collected)
    return {'pages':visited_pages, 'girls':len(external_girls), 'found':len(collected),
            'inserted':inserted, 'updated':updated, 'mode':'东京夜游网首页公开短评'}

def crawl_public_reviews(start_url, max_pages=3):
    start_url = _safe_public_url(start_url)
    max_pages = min(10, max(1, int(max_pages or 3)))
    root = urlparse(start_url)
    with conn() as c:
        girl_rows = rows(c.execute("SELECT name,girl_alias,tags,remark,remark2 FROM girls ORDER BY id").fetchall())
    tokyo_home_result = _tokyo_yy_home_materials(start_url, max_pages, girl_rows)
    if tokyo_home_result is not None:
        return tokyo_home_result
    tokyo_result = _tokyo_yy_report_materials(start_url, max_pages, girl_rows)
    if tokyo_result is not None:
        return tokyo_result
    if BeautifulSoup is None:
        raise RuntimeError('服务器尚未安装评论采集解析组件，请等待本次部署完成')
    robots_url = f'{root.scheme}://{root.netloc}/robots.txt'
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        if not rp.can_fetch('AliceReviewResearchBot/1.0', start_url):
            raise ValueError('该网站 robots.txt 不允许采集这个页面')
    except ValueError:
        raise
    except Exception:
        # robots.txt 不可用时仍保持低频、少页面，并在结果中说明。
        pass
    queue = [start_url]
    visited = set()
    collected = []
    while queue and len(visited) < max_pages:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        _safe_public_url(page_url)
        req = Request(page_url, headers={'User-Agent':'AliceReviewResearchBot/1.0 (+public review research; low frequency)'})
        with urlopen(req, timeout=20) as response:
            final_url = _safe_public_url(response.geturl())
            if urlparse(final_url).netloc.lower() != root.netloc.lower():
                raise ValueError('页面跳转到了其他网站，已停止采集')
            content_type = str(response.headers.get('Content-Type') or '')
            if 'html' not in content_type.lower():
                raise ValueError('目标地址不是 HTML 页面')
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('单页超过 2MB，已停止采集')
        visited.add(page_url)
        soup = BeautifulSoup(raw, 'html.parser')
        for unwanted in soup(['script','style','nav','footer','header','form']):
            unwanted.decompose()
        candidates = soup.select('article,[class*="review" i],[class*="comment" i],[class*="kuchikomi" i],[class*="voice" i]')
        seen_page = set()
        for node in candidates[:250]:
            text_value = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()
            if len(text_value) < 20 or len(text_value) > 2000 or text_value in seen_page:
                continue
            seen_page.add(text_value)
            girl_name = _review_girl_name(text_value, girl_rows)
            digest = hashlib.sha256((final_url+'\n'+text_value).encode('utf-8')).hexdigest()
            collected.append({'source_url':start_url,'source_page':final_url,'girl_name':girl_name,
                              'review_text':text_value,'tags':','.join(_review_tags(text_value)),'review_hash':digest,
                              'material_type':'公开网页素材','access_scope':'public'})
        if len(visited) < max_pages:
            links = []
            for anchor in soup.select('a[href]'):
                href = urljoin(final_url, anchor.get('href') or '')
                parsed = urlparse(href)
                hint = (href+' '+anchor.get_text(' ', strip=True)).lower()
                if parsed.netloc.lower() == root.netloc.lower() and any(x in hint for x in ('review','comment','kuchikomi','口コミ','体験','お客様','声')):
                    links.append(href.split('#')[0])
            queue.extend(link for link in dict.fromkeys(links) if link not in visited and link not in queue)
        time.sleep(.2)
    inserted, updated = _store_scraped_reviews(collected)
    return {'pages':len(visited),'found':len(collected),'inserted':inserted,'updated':updated,'mode':'公开网页'}

def review_marketing_drafts(day, selected_name='', custom_description=''):
    custom_description = re.sub(r'\s+', ' ', str(custom_description or '')).strip()[:1200]
    with conn() as c:
        names = ([str(selected_name).strip()] if str(selected_name or '').strip() else
                 [r['girl_name'] for r in c.execute("SELECT girl_name FROM pure_shifts WHERE shift_date=? ORDER BY sort_order,id", (day,)).fetchall()])
        girls = {r['name']:dict(r) for r in c.execute("SELECT name,girl_alias,tags,remark,remark2 FROM girls").fetchall()}
        review_rows = rows(c.execute("""SELECT girl_name,review_text,tags,material_type,author_name,source_title
                                      FROM scraped_reviews ORDER BY updated_at DESC,id DESC""").fetchall())
    global_long_rows = [r for r in review_rows if r.get('material_type') == '登录后长评']
    result = []
    for name in dict.fromkeys(names):
        if name not in girls:
            continue
        sources = [r for r in review_rows if _wordpress_girl_key(r.get('girl_name')) == _wordpress_girl_key(name)]
        short_sources = [r for r in sources if r.get('material_type') == '公开短评']
        long_sources = [r for r in sources if r.get('material_type') == '登录后长评']
        feature_counts = {}
        for source in sources:
            for tag in str(source.get('tags') or '').split(','):
                if tag:
                    feature_counts[tag] = feature_counts.get(tag,0)+1
        for tag in _review_tags(custom_description):
            feature_counts[tag] = feature_counts.get(tag,0) + 100
        girl = girls.get(name) or {}
        if not feature_counts:
            for tag in _review_tags(' '.join(str(girl.get(x) or '') for x in ('tags','remark','remark2'))):
                feature_counts[tag] = 1
        descriptors = []
        custom_descriptors = []
        for token in re.split(r'[,，、/|；;。\n]+', custom_description):
            token = token.strip()
            if 1 < len(token) <= 32 and token not in custom_descriptors:
                custom_descriptors.append(token)
        for value in (custom_description,girl.get('tags'),girl.get('remark'),girl.get('remark2')):
            for token in re.split(r'[,，、/|；;。\n]+', str(value or '')):
                token = token.strip()
                if 1 < len(token) <= 24 and token not in descriptors:
                    descriptors.append(token)
        features = [x[0] for x in sorted(feature_counts.items(), key=lambda x:(-x[1],x[0]))[:4]]
        for descriptor in descriptors:
            if len(features) >= 4:
                break
            if descriptor not in features:
                features.append(descriptor)
        features = features or ['自然亲切','轻松陪伴']
        f1, f2 = features[0], features[min(1,len(features)-1)]
        style_sources = long_sources or global_long_rows
        style_text = '\n'.join(str(r.get('review_text') or '') for r in style_sources[:20])
        style_parts = []
        if re.search(r'评分|\d(?:\.\d)?分|颜值.{0,8}身材', style_text):
            style_parts.append('先概括重点、再分项评价')
        if re.search(r'见面|进门|开始|后来|最后|结束', style_text):
            style_parts.append('按见面过程推进')
        if re.search(r'兄弟|大家|推荐|总结', style_text):
            style_parts.append('口语化总结')
        style_profile = '＋'.join(style_parts[:3]) or '自然叙述＋重点总结'
        evidence = f'你的补充 {len(custom_descriptors)} 项、女孩表/描述 {len(descriptors)} 项、公开短评 {len(short_sources)} 条、完整长评 {len(long_sources)} 条'
        shorts = [
            f'{name}的公开反馈关键词是{f1}和{f2}，想了解她可以先查看实时空档。',
            f'今天想找偏{f1}类型的女孩，可以留意{name}，资料特点是{f2}。',
            f'{name}｜{f1} × {f2}，结合女孩资料与公开短评整理，预约前可先咨询。',
            f'如果你在意{f1}和相处氛围，{name}是今天值得进一步了解的选择。',
            f'今日女孩介绍：{name}，资料与公开反馈较集中在{f1}、{f2}。'
        ]
        custom_sentence = (f'你补充的女孩特点包括：{"、".join(custom_descriptors[:4])}。' if custom_descriptors else '')
        long_text = (f'【综合评价宣传稿｜非真实客评】\n\n{name}给人的第一组关键词，是{f1}和{f2}。{custom_sentence}这不是只看一句介绍得出的结论，而是把你的描述、女孩表资料、已经公开的短评以及素材库中的长评结构放在一起整理后的方向。'
                     f'从现有资料来看，她更适合重视相处氛围、沟通感受和细节匹配的客人。与其只用一个标签概括，不如预约前告诉客服你喜欢的节奏、在意的部分和希望避免的情况，再结合当天状态判断是否合适。\n\n'
                     f'这篇草稿采用“{style_profile}”的写法：先把{name}最明确的特点说清楚，再补充适合的客人类型和预约建议。现有素材里较稳定的共同点是{f1}、{f2}；其他尚未被多条资料互相印证的描述，不在这里写成确定事实。\n\n'
                     f'如果你正在比较今天的女孩，可以把{name}放进候选名单，再让客服根据实时空档与最新状态确认。预约时间以 MCR 显示为准。本文由系统根据归档资料生成，属于宣传创作草稿，不是任何客人的原话，也不代表真实体验已经发生；发布前请再次核对女孩资料。')
        result.append({'girl_name':name,'features':features,'source_count':len(sources),'short_drafts':shorts,
                       'long_draft':long_text,'label':'综合评价宣传稿（非真实客评）',
                       'evidence_summary':evidence,'style_profile':style_profile,
                       'short_source_count':len(short_sources),'long_source_count':len(long_sources)})
    return result

def polish_archived_customer_review(review_id):
    """Lightly clean one real archived review without adding any new claim or experience."""
    with conn() as c:
        row = c.execute("""SELECT id,girl_name,review_text,material_type,author_name,review_date,
                            source_title,source_page,source_url FROM scraped_reviews WHERE id=?""",
                        (int(review_id or 0),)).fetchone()
    if not row:
        raise ValueError('找不到这条评价素材')
    item = dict(row)
    if item.get('material_type') not in ('公开短评','登录后长评','公开长评预览'):
        raise ValueError('这条素材不是可润色的真实客户评价')
    original = unicodedata.normalize('NFKC', str(item.get('review_text') or '')).strip()
    if not original:
        raise ValueError('评价正文为空')
    lines = []
    for line in re.split(r'[\r\n]+', original):
        line = re.sub(r'[ \t\u3000]+', ' ', line).strip()
        line = re.sub(r'([，。！？!?])\1{2,}', r'\1\1', line)
        if line:
            lines.append(line)
    if len(lines) <= 1 and len(original) > 120:
        sentences = [x.strip() for x in re.split(r'(?<=[。！？!?])\s*', lines[0] if lines else original) if x.strip()]
        if len(sentences) > 3:
            lines = [''.join(sentences[index:index+3]) for index in range(0, len(sentences), 3)]
    polished = '\n\n'.join(lines)
    return {'review_id':item['id'],'girl_name':item.get('girl_name') or '',
            'material_type':item.get('material_type') or '', 'author_name':item.get('author_name') or '',
            'review_date':item.get('review_date') or '', 'source_title':item.get('source_title') or '',
            'source_url':item.get('source_page') or item.get('source_url') or '',
            'original_text':original, 'polished_text':polished,
            'label':'真实客户评价润色（仅整理标点与段落，未新增事实）'}

def assist_real_customer_review(girl_name, original_text, confirmed_details=''):
    """Expand a customer's own short note using only facts and feelings they explicitly supplied."""
    girl_name = str(girl_name or '').strip()
    original = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(original_text or ''))).strip()[:1000]
    details = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(confirmed_details or ''))).strip()[:1500]
    if not girl_name:
        raise ValueError('请选择对应女孩')
    if len(original) < 4:
        raise ValueError('请至少填写 4 个字的客户原话')
    with conn() as c:
        if not c.execute('SELECT 1 FROM girls WHERE name=?', (girl_name,)).fetchone():
            raise ValueError('女孩表中没有这个女孩')
        style_rows = c.execute("""SELECT review_text FROM scraped_reviews
                                  WHERE material_type IN ('公开短评','登录后长评')
                                  ORDER BY updated_at DESC,id DESC LIMIT 60""").fetchall()
    style_text = '\n'.join(str(row['review_text'] or '') for row in style_rows)
    style_profile = ('论坛口语、短句分段' if re.search(r'哈哈|兄弟|真的|总体|总的来说|推荐', style_text)
                     else '自然口语、重点总结')
    original_clean = original.rstrip('。.!！?？')
    detail_clean = details.rstrip('。.!！?？')
    paragraphs = [f'这次见到{girl_name}，我最直接的感受就是：{original_clean}。']
    if detail_clean:
        paragraphs.append(f'具体一点说，我自己确认过的感受还有：{detail_clean}。')
    first_point = re.split(r'[，。；;、]', original_clean)[0].strip() or original_clean
    summary_parts = [first_point]
    if detail_clean:
        detail_point = re.split(r'[，。；;、]', detail_clean)[0].strip()
        if detail_point and detail_point not in summary_parts:
            summary_parts.append(detail_point)
    paragraphs.append(f'回头想想，比较让我记住的就是{"、".join(summary_parts[:2])}。以上都是我本人这次实际感受到、能够确认的部分，其他没有体验或不能确定的内容就不多写了。')
    expanded = '\n\n'.join(paragraphs)
    return {'girl_name':girl_name,'original_text':original,'confirmed_details':details,
            'polished_text':expanded,'style_profile':style_profile,
            'label':'真实客户协助润色（依据客户原话与确认感受，未添加他人经历）',
            'character_count':len(expanded)}

def _openai_response_text(payload):
    direct = str(payload.get('output_text') or '').strip() if isinstance(payload, dict) else ''
    if direct:
        return direct
    chunks = []
    for item in (payload.get('output') or []) if isinstance(payload, dict) else []:
        for content in item.get('content') or []:
            if content.get('type') in ('output_text','text') and content.get('text'):
                chunks.append(str(content['text']))
    return '\n'.join(chunks).strip()

def ai_assist_real_customer_review(girl_name, original_text, confirmed_details='', author_name='',
                                   style_strength='medium', target_length=180):
    api_key = str(os.environ.get('OPENAI_API_KEY') or '').strip()
    if not api_key:
        raise RuntimeError('尚未配置 OPENAI_API_KEY，请先在 Render 环境变量中添加后再生成')
    girl_name = str(girl_name or '').strip()
    original = re.sub(r'\s+', ' ', str(original_text or '')).strip()[:1500]
    details = re.sub(r'\s+', ' ', str(confirmed_details or '')).strip()[:2500]
    author_name = str(author_name or '').strip()[:80]
    strength = str(style_strength or 'medium').lower()
    if strength not in ('light','medium','high'):
        strength = 'medium'
    target_length = min(1000, max(80, int(target_length or 180)))
    if not girl_name or len(original) < 4:
        raise ValueError('请选择女孩，并填写至少 4 个字的客户原话')
    with conn() as c:
        if not c.execute('SELECT 1 FROM girls WHERE name=?', (girl_name,)).fetchone():
            raise ValueError('女孩表中没有这个女孩')
        samples = []
        if author_name:
            samples = [r['review_text'] for r in c.execute("""SELECT review_text FROM scraped_reviews
                       WHERE author_name=? AND material_type IN ('公开短评','登录后长评','公开长评预览')
                       ORDER BY updated_at DESC,id DESC LIMIT 12""", (author_name,)).fetchall()]
    if author_name and not samples:
        raise ValueError('这个作者还没有可用的归档样本')
    strength_note = {
        'light':'只参考句长、分段和标点习惯，不参考个人惯用表达。',
        'medium':'参考句长、分段、叙述顺序和一般口语习惯，不复制独特句子。',
        'high':'较强参考可观察的节奏、口语程度和组织方式，但不得冒充作者或复用其独特句子。'
    }[strength]
    sample_text = '\n\n---样本分隔---\n\n'.join(str(x or '')[:1800] for x in samples[:8])
    instructions = (
        '你是中文评价编辑。任务是协助真实客户把自己的短评扩写得自然、具体、紧凑。'
        '事实只能来自“客户原话”和“客户确认的真实感受”；作者样本只用于分析抽象语言特征，绝不能把样本中的人物、服务、地点、动作、评分或经历写入新稿。'
        '不得声称是样本作者本人，不得复制样本中的独特句子。避免空话、重复总结和营销口号。'
        '若真实信息不足以达到目标字数，应宁可短一些，并在 warning 说明“真实素材不足”，绝不编造。'
        '返回严格 JSON：{"text":"润色正文","style_summary":"不超过30字","warning":""}，不要 Markdown。')
    user_input = (f'女孩：{girl_name}\n目标字数：约{target_length}个汉字\n参考强度：{strength_note}\n'
                  f'客户原话：{original}\n客户确认的真实感受：{details or "未补充"}\n'
                  f'参考作者：{author_name or "不指定，使用通用自然口语"}\n作者样本：\n{sample_text or "无"}')
    body = json.dumps({'model':OPENAI_REVIEW_MODEL,'instructions':instructions,'input':user_input,
                       'reasoning':{'effort':'low'},'store':False,'max_output_tokens':1600},
                      ensure_ascii=False).encode('utf-8')
    req = Request('https://api.openai.com/v1/responses', data=body, method='POST', headers={
        'Authorization':'Bearer '+api_key, 'Content-Type':'application/json',
        'User-Agent':'AliceMCR/1.0'})
    try:
        with urlopen(req, timeout=75) as response:
            response_payload = json.loads(response.read(2_000_000).decode('utf-8'))
    except HTTPError as exc:
        message = exc.read(2000).decode('utf-8', errors='replace')
        raise RuntimeError(f'大语言模型调用失败（HTTP {exc.code}）：{message[:300]}')
    output = _openai_response_text(response_payload)
    if not output:
        raise RuntimeError('大语言模型没有返回文字')
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', output.strip(), flags=re.I)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {'text':output.strip(),'style_summary':'作者语气参考','warning':''}
    text_value = str(result.get('text') or '').strip()
    if not text_value:
        raise RuntimeError('大语言模型返回的正文为空')
    return {'girl_name':girl_name,'author_name':author_name,'style_strength':strength,
            'target_length':target_length,'character_count':len(text_value),'polished_text':text_value,
            'style_profile':str(result.get('style_summary') or '作者语气参考')[:80],
            'warning':str(result.get('warning') or '')[:200],
            'model':OPENAI_REVIEW_MODEL,
            'label':'AI真实客户协助润色（事实仅来自客户本人提供内容）'}

def sync_alice_wordpress_attendance(day, image_bytes, service_text, attendance_names=None, all_girl_names=None,
                                    girl_prices=None):
    user, pwd = alice_wordpress_credentials()
    if not user or not pwd:
        return {'configured': False, 'synced': False, 'warning': 'Render 尚未设置 ALICE_WP_ADMIN_USER 和 ALICE_WP_ADMIN_PASSWORD'}
    post_id = int(os.environ.get('ALICE_WP_ATTENDANCE_POST_ID') or 9744)
    stage = '登录官网后台'
    try:
        opener = alice_wordpress_login(user, pwd)
        stage = '读取“今日出勤”编辑页'
        edit_url = f'{ALICE_BASE_URL}/wp-admin/post.php?post={post_id}&action=edit'
        edit_html, _ = opener_text(opener, edit_url, timeout=40)
        gallery_key = _acf_gallery_field_key(edit_html)
        if not gallery_key:
            raise ValueError('找不到“照片(可以添加多个照片)”字段')
        gallery_name = _wordpress_photo_gallery_field_name(edit_html, gallery_key)
        if not gallery_name:
            raise ValueError('找不到旧版相册插件的图片字段名')
        image_bytes_list = ([bytes(image_bytes)] if isinstance(image_bytes, (bytes, bytearray))
                            else [bytes(item) for item in (image_bytes or []) if item])
        if not image_bytes_list:
            raise ValueError('没有可上传的今日出勤图片')
        attachment_ids = []
        stage = '上传今日出勤图片'
        try:
            for page_index, page_bytes in enumerate(image_bytes_list):
                attachment_id = _wordpress_rest_upload_image(
                    opener, edit_html, edit_url, day, page_bytes, page_index, len(image_bytes_list))
                if not attachment_id:
                    stage = '读取官网媒体上传页'
                    media_html, _ = opener_text(opener, ALICE_BASE_URL + '/wp-admin/media-new.php', timeout=35)
                    nonce_match = re.search(r'<input\b[^>]*name=["\']_wpnonce["\'][^>]*value=["\']([^"\']+)', media_html, re.I)
                    if not nonce_match:
                        raise ValueError('找不到官网图片上传授权码')
                    suffix = f'-{page_index + 1}' if len(image_bytes_list) > 1 else ''
                    filename = f'alice-attendance-{day}{suffix}.png'
                    stage = '上传今日出勤图片'
                    upload_raw = _multipart_request(ALICE_BASE_URL + '/wp-admin/async-upload.php', [
                        ('name', filename), ('action', 'upload-attachment'),
                        ('_wpnonce', html_unescape(nonce_match.group(1)))
                    ], filename, page_bytes, opener, edit_url)
                    upload_data = json.loads(upload_raw)
                    attachment_id = int(((upload_data.get('data') or {}).get('id')) or 0)
                    if not upload_data.get('success') or not attachment_id:
                        raise ValueError(((upload_data.get('data') or {}).get('message')) or '官网图片上传失败')
                attachment_ids.append(attachment_id)
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace').strip()[:300]
            raise ValueError(f'官网图片上传失败（HTTP {exc.code}）' + (f'：{strip_html_text(detail)}' if detail else '')) from exc
        stage = '整理“今日出勤”表单'
        pairs = _wordpress_form_pairs(edit_html)
        replace_names = {'acf[field_5d253ca06ccc7]', f'acf[{gallery_key}]', gallery_name,
                         gallery_name + '[]', 'action', 'post_ID'}
        pairs = [(k, v) for k, v in pairs if k not in replace_names]
        pairs.extend([
            ('action', 'editpost'), ('post_ID', str(post_id)),
            ('acf[field_5d253ca06ccc7]', str(service_text or '').strip()),
        ])
        # 按生成顺序写入；第一张就是相册列表默认显示的图片。
        pairs.extend((gallery_name + '[]', str(attachment_id)) for attachment_id in attachment_ids)
        pairs.append(('save', '更新'))
        stage = '保存“今日出勤”图片和文案'
        req = Request(ALICE_BASE_URL + '/wp-admin/post.php', data=urlencode(pairs, doseq=True).encode('utf-8'),
                      method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': edit_url})
        try:
            with opener.open(req, timeout=60) as response:
                final_url = response.geturl()
                result_html = response.read().decode(response.headers.get_content_charset() or 'utf-8', 'replace')
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace').strip()[:300]
            raise ValueError(f'官网“今日出勤”保存失败（HTTP {exc.code}）' + (f'：{strip_html_text(detail)}' if detail else '')) from exc
        if 'post.php' not in final_url and 'post.php' not in result_html:
            raise ValueError('官网没有确认保存成功')
        stage = '确认“今日出勤”相册已替换'
        verify_html, _ = opener_text(opener, edit_url + '&alice_verify=1', timeout=40)
        verified_ids = _wordpress_photo_gallery_attachment_ids(verify_html)
        if verified_ids != attachment_ids:
            raise ValueError(f'旧版相册插件没有保存新图片（期望 {attachment_ids}，实际 {verified_ids or "空"}）')
        stage = '同步女孩公开/私密状态'
        try:
            visibility = sync_alice_wordpress_girl_visibility(opener, attendance_names or [], all_girl_names or [])
        except Exception as exc:
            visibility = {'synced': False, 'warning': str(exc), 'matched': 0, 'published': 0, 'privated': 0}
        stage = '同步女孩每小时价格分类'
        try:
            price_categories = sync_alice_wordpress_girl_prices(opener, girl_prices or {})
        except Exception as exc:
            price_categories = {'synced': False, 'warning': str(exc), 'matched': 0, 'updated': 0}
        return {'configured': True, 'synced': True, 'post_id': post_id,
                'attachment_id': attachment_ids[0], 'attachment_ids': attachment_ids,
                'visibility': visibility, 'price_categories': price_categories}
    except Exception as exc:
        warning = str(exc)
        if not warning.startswith(('官网图片上传失败', '官网“今日出勤”保存失败')):
            warning = f'{stage}失败：{warning}'
        return {'configured': True, 'synced': False, 'stage': stage, 'warning': warning}

@app.route('/api/wordpress/diagnose', methods=['GET'])
def api_wordpress_diagnose():
    """Read-only checks for every WordPress page needed before an attendance update."""
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以运行官网诊断'), 403
    user, pwd = alice_wordpress_credentials()
    if not user or not pwd:
        return jsonify(ok=False, stage='读取配置', error='Render 尚未设置官网账号密码'), 400
    post_id = int(os.environ.get('ALICE_WP_ATTENDANCE_POST_ID') or 9744)
    checks = []
    stage = '登录官网后台'
    try:
        opener = alice_wordpress_login(user, pwd)
        checks.append({'stage': stage, 'ok': True})
        stage = '读取“今日出勤”编辑页'
        edit_url = f'{ALICE_BASE_URL}/wp-admin/post.php?post={post_id}&action=edit'
        edit_html, final_url = opener_text(opener, edit_url, timeout=40)
        gallery_key = _acf_gallery_field_key(edit_html)
        if not gallery_key:
            raise ValueError('找不到“照片(可以添加多个照片)”字段')
        gallery_name = _wordpress_photo_gallery_field_name(edit_html, gallery_key)
        if not gallery_name:
            raise ValueError('找不到旧版相册插件的图片字段名')
        checks.append({'stage': stage, 'ok': True, 'post_id': post_id, 'gallery_key': gallery_key,
                       'gallery_name': gallery_name,
                       'gallery_attachment_ids': _wordpress_photo_gallery_attachment_ids(edit_html),
                       'final_url': final_url,
                       'gallery_inputs': [
                           {'name': key, 'value': str(value)[:100]}
                           for key, value in _wordpress_form_pairs(edit_html)
                           if key in ('apg_nonce', 'acf-photo-gallery-field', 'acf-photo-gallery-groups[]',
                                      gallery_name, gallery_name + '[]')
                       ]})
        rest_nonce = _wordpress_rest_nonce(edit_html)
        if rest_nonce:
            stage = '检查官网 REST 媒体接口'
            req = Request(ALICE_BASE_URL + '/wp-json/wp/v2/media?per_page=1&context=edit', headers={
                'X-WP-Nonce': rest_nonce, 'Referer': edit_url, 'Accept': 'application/json'})
            with opener.open(req, timeout=40) as response:
                json.loads(response.read().decode(response.headers.get_content_charset() or 'utf-8', 'replace'))
            checks.append({'stage': stage, 'ok': True})
            return jsonify(ok=True, checks=checks, upload_method='wordpress_rest_api',
                           next_stage='上传图片（诊断未执行写入）')
        stage = '读取官网媒体上传页'
        media_html, media_url = opener_text(opener, ALICE_BASE_URL + '/wp-admin/media-new.php', timeout=35)
        nonce_match = re.search(r'<input\b[^>]*name=["\']_wpnonce["\'][^>]*value=["\']([^"\']+)', media_html, re.I)
        if not nonce_match:
            raise ValueError('找不到官网图片上传授权码')
        checks.append({'stage': stage, 'ok': True, 'final_url': media_url})
        return jsonify(ok=True, checks=checks, upload_method='legacy_async_upload',
                       next_stage='上传图片（诊断未执行写入）')
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace').strip()[:300]
        return jsonify(ok=False, stage=stage, http_status=exc.code, checks=checks,
                       error=strip_html_text(detail) or str(exc)), 502
    except Exception as exc:
        return jsonify(ok=False, stage=stage, checks=checks, error=str(exc)), 502

@app.route('/api/wordpress/visibility-diagnose', methods=['GET'])
def api_wordpress_visibility_diagnose():
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以运行官网诊断'), 403
    day = request.args.get('date') or tokyo_today_date().isoformat()
    user, pwd = alice_wordpress_credentials()
    try:
        opener = alice_wordpress_login(user, pwd)
        posts, nonce = _wordpress_model_posts(opener)
        with conn() as c:
            attendance = [str(row['girl'] or '').strip() for row in pure_shift_rows_for_date(c, day)]
            managed = [str(row['name'] or '').strip() for row in c.execute(
                "SELECT name FROM girls WHERE COALESCE(name,'')!='' ORDER BY id DESC").fetchall()]
        post_map = {}
        for post in posts:
            post_map.setdefault(_wordpress_girl_key(post['title']), []).append(post)
        rows_out = []
        attendance_keys = {_wordpress_girl_key(name) for name in attendance}
        for name in managed:
            key = _wordpress_girl_key(name)
            matches = sorted(post_map.get(key, []), key=lambda item: item['id'], reverse=True)
            desired = 'publish' if key in attendance_keys else 'private'
            rows_out.append({'girl': name, 'attendance': key in attendance_keys, 'desired': desired,
                             'matches': [{'id': item['id'], 'title': item['title'], 'status': item['status']}
                                         for item in matches]})
        return jsonify(ok=True, date=day, inline_nonce_found=bool(nonce), wordpress_posts=len(posts),
                       attendance=attendance, girls=rows_out)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502

@app.route('/api/wordpress/visibility-sync', methods=['POST'])
def api_wordpress_visibility_sync():
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以同步官网状态'), 403
    data = request.get_json(silent=True) or {}
    day = str(data.get('date') or tokyo_today_date().isoformat())
    user, pwd = alice_wordpress_credentials()
    try:
        opener = alice_wordpress_login(user, pwd)
        with conn() as c:
            attendance = [str(row['girl'] or '').strip() for row in pure_shift_rows_for_date(c, day)]
            managed = [str(row['name'] or '').strip() for row in c.execute(
                "SELECT name FROM girls WHERE COALESCE(name,'')!='' ORDER BY id DESC").fetchall()]
        result = sync_alice_wordpress_girl_visibility(opener, attendance, managed)
        return jsonify(ok=bool(result.get('synced')), date=day, attendance=attendance, **result)
    except Exception as exc:
        return jsonify(ok=False, date=day, error=str(exc)), 502

@app.route('/api/wordpress/price-category-sync', methods=['POST'])
def api_wordpress_price_category_sync():
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以同步官网价格分类'), 403
    user, pwd = alice_wordpress_credentials()
    try:
        opener = alice_wordpress_login(user, pwd)
        with conn() as c:
            girl_prices = {str(row['name'] or '').strip(): {'price': int(row['list_price'] or 0),
                                                            'alias': str(row['girl_alias'] or '').strip()}
                           for row in c.execute("SELECT name,girl_alias,list_price FROM girls WHERE COALESCE(name,'')!=''").fetchall()}
        result = sync_alice_wordpress_girl_prices(opener, girl_prices)
        return jsonify(ok=bool(result.get('synced')), **result)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502

def acf_value_from_edit(edit_html, data_name):
    m = re.search(r'<div[^>]+class=["\'][^"\']*acf-field[^"\']*["\'][^>]+data-name=["\']' + re.escape(data_name) + r'["\'][^>]*>', edit_html, re.S | re.I)
    if not m:
        return ''
    next_m = re.search(r'<div[^>]+class=["\'][^"\']*acf-field[^"\']*["\']', edit_html[m.end():], re.S | re.I)
    block = edit_html[m.start():m.end() + (next_m.start() if next_m else 5000)]
    ta = re.search(r'<textarea\b[^>]*>(.*?)</textarea>', block, re.S | re.I)
    if ta:
        return html_unescape(ta.group(1)).strip()
    inp = re.search(r'<input\b[^>]*\bvalue=(["\'])(.*?)\1', block, re.S | re.I)
    if inp:
        return html_unescape(inp.group(2)).strip()
    return ''

def neko_admin_image_urls(edit_html):
    urls = []
    seen = set()
    pattern = r'https?:\\?/\\?/[^"\'<>\s]+?(?:\.jpg|\.jpeg|\.png|\.webp|\.gif)(?:\?[^"\'<>\s]*)?'
    for raw in re.findall(pattern, edit_html, re.I):
        url = html_unescape(raw).replace('\\/', '/')
        if '/wp-content/uploads/' not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls

def neko_admin_item_from_edit(opener, post_id, title, edit_url):
    edit_html, final_url = opener_text(opener, edit_url, timeout=35)
    title_value = ''
    m = re.search(r'<input\b[^>]+id=["\']title["\'][^>]*\bvalue=(["\'])(.*?)\1', edit_html, re.S | re.I)
    if m:
        title_value = html_unescape(m.group(2)).strip()
    images = neko_admin_image_urls(edit_html)
    model_brief = acf_value_from_edit(edit_html, 'model_brief')
    model_detail = acf_value_from_edit(edit_html, 'model_detail')
    link = ''
    pm = re.search(r'<span[^>]+id=["\']sample-permalink["\'][^>]*>.*?<a[^>]+href=(["\'])(.*?)\1', edit_html, re.S | re.I)
    if pm:
        link = html_unescape(pm.group(2))
    return {
        'id': str(post_id),
        'post_title': title_value or title,
        'name': title_value or title,
        'model_brief': model_brief,
        'model_detail': model_detail,
        'thumbnail': images[0] if images else '',
        'model_pics': images,
        'link': link,
        'source': 'wp-admin',
    }

def fetch_neko_admin_girls(username=None, password=None, max_pages=8):
    opener = neko_admin_login(username, password)
    items, seen = [], set()
    for page in range(1, max_pages + 1):
        url = NEKO_BASE_URL + '/wp-admin/edit.php?post_type=model&post_status=publish&posts_per_page=100&paged=' + str(page)
        html, _ = opener_text(opener, url, timeout=35)
        page_rows = []
        for m in re.finditer(r'<tr[^>]+id=["\']post-(\d+)["\'][^>]*>(.*?)</tr>', html, re.S | re.I):
            post_id = m.group(1)
            if post_id in seen:
                continue
            row = m.group(2)
            tm = re.search(r'class=["\']row-title["\'][^>]*>(.*?)</a>', row, re.S | re.I)
            title = strip_html_text(html_unescape(tm.group(1))) if tm else ''
            if not title or title == '今日出勤' or '出勤' in title:
                continue
            em = re.search(r'href=(["\'])([^"\']*post\.php\?post=' + re.escape(post_id) + r'[^"\']*)\1', row, re.S | re.I)
            edit_url = html_unescape(em.group(2)).replace('&amp;', '&') if em else (NEKO_BASE_URL + '/wp-admin/post.php?post=' + post_id + '&action=edit')
            edit_url = urljoin(NEKO_BASE_URL + '/wp-admin/', edit_url)
            seen.add(post_id)
            page_rows.append((post_id, title, edit_url))
        if not page_rows:
            break
        for post_id, title, edit_url in page_rows:
            try:
                item = neko_admin_item_from_edit(opener, post_id, title, edit_url)
                if item.get('post_title') and first_neko_image(item):
                    items.append(item)
            except Exception:
                items.append({'id': post_id, 'post_title': title, 'name': title, 'link': edit_url, 'source': 'wp-admin'})
    if not items:
        raise ValueError('喵喵后台已发布女孩列表为空')
    return items

def fetch_neko_girls(username=None, password=None):
    errors = []
    admin_user, admin_pass = neko_admin_credentials(username, password)
    if admin_user and admin_pass:
        try:
            items = fetch_neko_admin_girls(admin_user, admin_pass)
            if items:
                fetch_neko_girls.last_errors = []
                return items, 'wp-admin:model'
            errors.append({'url': 'wp-admin:model', 'error': 'empty list'})
        except Exception as e:
            errors.append({'url': 'wp-admin:model', 'error': str(e)})
    urls = [
        NEKO_BASE_URL + '/simple-api/girls?_cb=' + str(int(datetime.now().timestamp())),
        NEKO_BASE_URL + '/simple-api/girls',
        NEKO_BASE_URL + '/simple-api/models?_cb=' + str(int(datetime.now().timestamp())),
        NEKO_BASE_URL + '/simple-api/models',
        NEKO_BASE_URL + '/simple-api/home?_cb=' + str(int(datetime.now().timestamp())),
        NEKO_BASE_URL + '/simple-api/home',
        NEKO_BASE_URL + '/wp-json/wp/v2/posts?per_page=100&_embed=1',
    ]
    for url in urls:
        try:
            text, ctype = http_text(url)
            if 'json' not in ctype and not text.lstrip().startswith(('[','{')):
                errors.append({'url': url, 'error': 'not json: ' + ctype})
                continue
            data = json.loads(text)
            items = extract_neko_items(data)
            if items:
                fetch_neko_girls.last_errors = []
                return items, url.replace(NEKO_BASE_URL, '').split('?')[0].strip('/') or 'neko-api'
            errors.append({'url': url, 'error': 'empty list'})
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})
            continue
    fetch_neko_girls.last_errors = errors[-5:]
    return NEKO_SEED_GIRLS, 'seed'
fetch_neko_girls.last_errors = []

def match_neko_girl(girl_name, alias, neko_items):
    wants = [normalize_avatar_name(girl_name), normalize_avatar_name(alias)]
    wants = [w for w in wants if w]
    best = None
    best_score = 0
    for item in neko_items:
        neko_name = item.get('post_title') or item.get('name') or item.get('model_name') or ''
        n = normalize_avatar_name(neko_name)
        if not n:
            continue
        score = 0
        for w in wants:
            if w == n:
                score = max(score, 100)
            elif len(w) >= 2 and (w in n or n in w):
                score = max(score, 80 + min(len(w), len(n)))
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 80 else None

def avatar_file_for(girl_name, src_url, content_type=''):
    ext = '.jpg'
    parsed_ext = Path(urlparse(src_url).path).suffix.lower()
    if parsed_ext in ('.jpg','.jpeg','.png','.webp','.gif'):
        ext = parsed_ext
    elif 'png' in content_type:
        ext = '.png'
    elif 'webp' in content_type:
        ext = '.webp'
    key = hashlib.sha1((girl_name + '|' + src_url).encode('utf-8')).hexdigest()[:16]
    return AVATAR_DIR / (key + ext)

def avatar_local_path(avatar_url):
    value = str(avatar_url or '')
    if value.startswith('/girl_avatars/'):
        return AVATAR_DIR / Path(value).name
    if value.startswith('/static/girl_avatars/'):
        return LEGACY_AVATAR_DIR / Path(value).name
    return None

@app.route('/girl_avatars/<path:filename>')
def saved_girl_avatar(filename):
    # 只允许 cache_avatar 生成的哈希文件名。
    if not re.fullmatch(r'[0-9a-f]{16}\.(?:jpg|jpeg|png|webp|gif)', filename, re.I):
        return Response('Not found', status=404)
    return send_from_directory(str(AVATAR_DIR), filename)

def cache_avatar(girl_name, neko_name, src_url, referer=None):
    if not src_url:
        return ''
    if src_url.startswith('//'):
        src_url = 'https:' + src_url
    elif src_url.startswith('/'):
        src_url = urljoin(NEKO_BASE_URL + '/', src_url.lstrip('/'))
    data, content_type = http_bytes(src_url, referer=referer)
    if not data or len(data) < 500:
        raise ValueError('image is empty')
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = avatar_file_for(girl_name, src_url, content_type)
    path.write_bytes(data)
    rel = '/girl_avatars/' + path.name
    with conn() as c:
        c.execute("""INSERT INTO girl_avatar_cache(girl_name,neko_name,avatar_url,source_url,updated_at)
                     VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(girl_name) DO UPDATE SET
                     neko_name=excluded.neko_name, avatar_url=excluded.avatar_url,
                     source_url=excluded.source_url, updated_at=CURRENT_TIMESTAMP""",
                  (girl_name, neko_name, rel, src_url))
        c.execute("""UPDATE girls SET avatar_url=?, avatar_source_url=?,
                     avatar_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                     WHERE name=?""", (rel, src_url, girl_name))
    return rel

def cached_neko_image(girl_name, neko_name, src_url):
    src_url = absolute_neko_url(src_url)
    if not src_url:
        return ''
    try:
        init_db()
        with conn() as c:
            row = c.execute("SELECT avatar_url,source_url FROM girl_avatar_cache WHERE girl_name=?", (girl_name,)).fetchone()
        if row and row['avatar_url'] and row['source_url'] == src_url:
            local_path = avatar_local_path(row['avatar_url'])
            if local_path and local_path.exists():
                return row['avatar_url']
    except Exception:
        pass
    try:
        return cache_avatar(girl_name, neko_name or girl_name, src_url)
    except Exception:
        return src_url

def yen_to_int(s):
    s=str(s or '').strip().replace('（','').replace('）','').replace('(','').replace(')','').replace(',','').replace('¥','').replace('円','')
    if not s: return 0
    m=re.search(r'([\d.]+)\s*万',s)
    if m: return int(float(m.group(1))*10000)
    m=re.search(r'[\d.]+',s)
    if not m: return 0
    n=float(m.group(0)); return int(round(n*10000)) if 0<n<100 else int(round(n))
def parse_time_part(h, m=None):
    hour = int(h)
    minute = int(m or 0)
    if minute >= 60:
        minute = 59
    return hour + minute / 60

def _time_label(hour, minute):
    return f"{hour}.{minute:02d}" if minute else str(hour)

def _parse_time_groups(m):
    sh = int(m.group(1)); sm = int(m.group(2) or 0)
    eh = int(m.group(3)); em = int(m.group(4) or 0)
    if sm >= 60: sm = 59
    if em >= 60: em = 59
    return sh, sm, eh, em

def _chain_interval_minutes(sh, sm, eh, em):
    """
    接龙时间时长判断。
    - 12.30-1.30 视为 12.30-13.30，不再变成 25.30。
    - 8.30-1.30 这类夜场简写视为 20.30-次日1.30。
    - 23.30-0.30 仍按跨凌晨 1 小时计算，但显示不写成 24.30。
    """
    start = sh * 60 + sm
    end = eh * 60 + em

    if end < start:
        # 中午/下午时间：12.30-1.30 应该是 12.30-13.30。
        if sh >= 12 and eh < 12:
            same_day_pm_end = (eh + 12) * 60 + em
            if same_day_pm_end > start:
                end = same_day_pm_end
            else:
                end += 24 * 60
        # 夜场简写：8.30-1.30 表示 20.30-次日1.30。
        elif sh < 12 and eh < sh:
            start += 12 * 60
            end += 24 * 60
        else:
            end += 24 * 60

    return end - start

def _business_clock_minutes_24h(h, mi):
    if h >= 24:
        return h * 60 + mi
    if h < 6:
        return (24 + h) * 60 + mi
    return h * 60 + mi

def _strict_24h_interval_minutes(sh, sm, eh, em):
    start = _business_clock_minutes_24h(sh, sm)
    end = _business_clock_minutes_24h(eh, em)
    if end <= start:
        end += 24 * 60
    return end - start

def service_duration_minutes(t):
    text = str(t or "").strip()
    if not text or "包夜" in text:
        return None
    strict = re.search(r"(\d{1,2}):(\d{2})\s*(?:[-~ー～]|到|至)\s*(\d{1,2}):(\d{2})", text)
    if strict:
        return _strict_24h_interval_minutes(*_parse_time_groups(strict))
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*(?:[-~ー～]|到|至)\s*(\d{1,2})(?:[:.](\d{1,2}))?", text)
    if not m:
        return None
    return _chain_interval_minutes(*_parse_time_groups(m))

def validate_service_time(t):
    minutes = service_duration_minutes(t)
    if minutes is not None and minutes > 7 * 60:
        raise ValueError(f'预约时长超过7小时，请检查时间：{t}')

def billable_hours_from_minutes(minutes):
    if minutes <= 0:
        return 1.0
    if minutes <= 60:
        return 1.0
    return math.ceil(minutes / 30) * 0.5

def calc_hours(t):
    text = str(t or "").strip()
    if "包夜" in text:
        return 3.0
    minutes = service_duration_minutes(text)
    if minutes is None:
        return 1.0
    return billable_hours_from_minutes(minutes)




def parse_service_end_datetime(order_date, service_time, shift_intervals=None):
    """把 23.30-0.30 / 20:00-21:00 这类预约时间转换为结束 datetime；凌晨自动按次日处理。"""
    try:
        base = datetime.strptime(str(order_date), "%Y-%m-%d")
    except Exception:
        return None
    try:
        interval = _parse_interval_text_for_shift(service_time, shift_intervals)
        if interval:
            end = int(interval[1])
            return base + timedelta(days=end // (24 * 60), hours=(end // 60) % 24, minutes=end % 60)
    except Exception:
        pass
    text = str(service_time or "")
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*(?:[-~ー～]|到|至)\s*(\d{1,2})(?:[:.](\d{1,2}))?", text)
    if not m:
        return None
    sh = int(m.group(1)); eh = int(m.group(3)); em = int(m.group(4) or 0)
    day_add = 0
    if eh >= 24:
        day_add = eh // 24
        eh = eh % 24
    elif eh < sh or sh >= 24:
        day_add = 1
    if sh >= 24 and day_add == 0:
        day_add = 1
    try:
        return base + timedelta(days=day_add, hours=eh, minutes=em)
    except Exception:
        return None

def auto_finish_reservations(c):
    """预约结束时间已经超过当前时间时，自动把预约中改成已结束；取消不动。"""
    now = _tokyo_now()
    for o in c.execute("SELECT id,order_date,service_time,girl_name,order_status FROM orders WHERE COALESCE(order_status,'')='预约中'").fetchall():
        end_dt = parse_service_end_datetime(
            o['order_date'],
            o['service_time'],
            _shift_intervals_for_girl(c, o['order_date'], o['girl_name'])
        )
        if end_dt and end_dt < now:
            c.execute("UPDATE orders SET order_status='已结束', updated_at=CURRENT_TIMESTAMP WHERE id=?", (o['id'],))


def next_customer_no(c):
    rows = c.execute("SELECT customer_no FROM customers").fetchall()
    max_no = 0
    for row in rows:
        try:
            val = row["customer_no"]
        except Exception:
            try:
                val = row[0]
            except Exception:
                val = ""
        m = re.search(r"\d+", str(val or ""))
        if m:
            try:
                n = int(m.group(0))
                if n > max_no:
                    max_no = n
            except Exception:
                pass
    return f"{max_no + 1:04d}"

def next_no(c):
    rows = c.execute("SELECT customer_no FROM customers").fetchall()
    max_no = 0
    for row in rows:
        try:
            val = row["customer_no"]
        except Exception:
            try:
                val = row[0]
            except Exception:
                val = ""
        m = re.search(r"\d+", str(val or ""))
        if m:
            try:
                n = int(m.group(0))
                if n > max_no:
                    max_no = n
            except Exception:
                pass
    return f"{max_no + 1:04d}"


def normalize_customer_name_for_duplicate(name):
    """客户名重复判断：忽略首尾/中间空格并做大小写统一。"""
    return re.sub(r"\s+", "", str(name or '')).lower()

def customer_name_duplicate_row(c, raw='', current_order_id=None):
    """接龙录入时使用：如果输入的是客户名且客户库已存在同名客户，返回该客户。
    数字客户ID仍按老逻辑使用，不作为重复客户名拦截。
    """
    raw = str(raw or '').strip()
    force_name = False
    if raw.startswith('__NAME__:'):
        force_name = True
        raw = raw[len('__NAME__:'):].strip()
    if not raw:
        return None
    if raw.isdigit() and not force_name:
        return None
    target = normalize_customer_name_for_duplicate(raw)
    if not target:
        return None
    allowed_customer_id = None
    if current_order_id:
        old = c.execute('SELECT customer_id FROM orders WHERE id=?', (int(current_order_id),)).fetchone()
        if old:
            allowed_customer_id = old['customer_id']
    for row in c.execute("SELECT * FROM customers WHERE COALESCE(name,'')!=''").fetchall():
        if allowed_customer_id and int(row['id']) == int(allowed_customer_id):
            continue
        if normalize_customer_name_for_duplicate(row['name']) == target:
            return row
    return None

def assert_no_duplicate_customer_name_for_chain(c, raw='', current_order_id=None):
    dup = customer_name_duplicate_row(c, raw, current_order_id)
    if dup:
        raise ValueError(f"客户名重复：{dup['name']} 已存在（客户ID {dup['customer_no']}）。请修改客户名后再导入。")

def ensure_customer(c, raw='', remark=''):
    raw = str(raw or '').strip()
    force_name = False
    if raw.startswith('__NAME__:'):
        force_name = True
        raw = raw[len('__NAME__:'):].strip()

    def make_new_customer_no():
        rows = c.execute("SELECT customer_no FROM customers").fetchall()
        max_no = 0
        for row in rows:
            try:
                val = row["customer_no"]
            except Exception:
                try:
                    val = row[0]
                except Exception:
                    val = ""
            m = re.search(r"\d+", str(val or ""))
            if m:
                try:
                    n = int(m.group(0))
                    if n > max_no:
                        max_no = n
                except Exception:
                    pass
        return f"{max_no + 1:04d}"

    if raw and raw.isdigit() and not force_name:
        no = f'{int(raw):04d}'
        row = c.execute('SELECT * FROM customers WHERE customer_no=?', (no,)).fetchone()
        if row:
            return row
        raise ValueError(f'客人ID {no} 不存在。第一次预约请在价格后填写客人用户名；如果用户名本身是数字，请写成 //{raw}。')
    elif raw:
        row = c.execute('SELECT * FROM customers WHERE name=?', (raw,)).fetchone()
        if row:
            return row
        no = make_new_customer_no()
        c.execute('INSERT INTO customers(customer_no,name,remark) VALUES(?,?,?)', (no, raw, remark))
    else:
        no = make_new_customer_no()
        c.execute('INSERT INTO customers(customer_no,name,remark) VALUES(?,?,?)', (no, f'自动客户{no}', remark))
    return c.execute('SELECT * FROM customers WHERE customer_no=?', (no,)).fetchone()


def ensure_girl(c,name):
    name=str(name or '').strip()
    if not name: return None
    row=c.execute('SELECT * FROM girls WHERE name=?',(name,)).fetchone()
    if row: return row
    c.execute('INSERT INTO girls(name,take_home_per_hour,list_price,remark) VALUES(?,?,?,?)',(name,10000,15000,'接龙/订单自动生成'))
    return c.execute('SELECT * FROM girls WHERE name=?',(name,)).fetchone()
def take_home(g,hours):
    if not g: return 0
    # 统一按“小时数 × 女生表中的每小时到手”计算女孩真实到手。
    hr=int(g['take_home_per_hour'] or 0)
    return int(round(hr*float(hours or 1)))
def recalc_girl(c,gid):
    g=c.execute('SELECT * FROM girls WHERE id=?',(gid,)).fetchone()
    if not g: return
    for o in c.execute('SELECT * FROM orders WHERE girl_id=?',(gid,)).fetchall():
        h=float(o['hours'] or calc_hours(o['service_time'])); th=take_home(g,h); prof=round_yen_1000_half_up(int(o['received_amount'] or 0)-th)
        c.execute('UPDATE orders SET girl_name=?, girl_take_home=?, store_profit=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',(g['name'],th,prof,o['id']))

def tokyo_today_date():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo("Asia/Tokyo")).date()
        except Exception:
            pass
    return (datetime.utcnow() + timedelta(hours=9)).date()

def refresh_customer_totals(c, customer_id=None, update_types=True):
    """刷新消费/累计统计，绝不重算或覆盖当前积分余额。"""
    if customer_id:
        ids = [int(customer_id)]
    else:
        ids = [int(r["id"]) for r in c.execute("SELECT id FROM customers").fetchall()]
    for cid in ids:
        totals = c.execute("""SELECT COALESCE(SUM(points),0) AS total_points,
                                     COALESCE(SUM(received_amount),0) AS total_spent
                              FROM orders WHERE customer_id=? AND COALESCE(order_status,'') NOT LIKE '%取消%'""", (cid,)).fetchone()
        c.execute("UPDATE customers SET total_points=?,total_spent=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (int(totals['total_points'] or 0), int(totals['total_spent'] or 0), cid))
    if update_types:
        update_customer_type_by_history(c, None)

def audit_customer_points(c):
    """只检查积分备注是否结构化入账，绝不从历史订单反推客户余额。"""
    anomalies = []
    customers = {int(row['id']):dict(row) for row in c.execute("SELECT id,customer_no,name FROM customers").fetchall()}
    checked = 0
    order_rows = rows(c.execute("""SELECT id,customer_id,customer_no,customer_name,order_date,points,points_used,
                                          remark,remark2,raw_text,order_status
                                   FROM orders
                                   WHERE (COALESCE(remark,'')||' '||COALESCE(remark2,'')||' '||COALESCE(raw_text,'')) LIKE '%积分%'
                                   ORDER BY order_date,id""").fetchall())
    for order in order_rows:
        if '取消' in str(order.get('order_status') or ''):
            continue
        checked += 1
        text = ' '.join(str(order.get(key) or '') for key in ('remark','remark2','raw_text'))
        customer = customers.get(int(order.get('customer_id') or 0), {})
        base = {'customer_id':int(order.get('customer_id') or 0),
                'customer_no':order.get('customer_no') or customer.get('customer_no'),
                'customer_name':order.get('customer_name') or customer.get('name'),
                'order_id':int(order['id']),'order_date':order.get('order_date'),
                'points':int(order.get('points') or 0),'points_used':int(order.get('points_used') or 0),
                'note':str(order.get('remark') or order.get('remark2') or order.get('raw_text') or '')}
        if point_use_note_triggered(text):
            if int(order.get('points_used') or 0) <= 0:
                anomalies.append({'kind':'积分使用备注未登记', **base})
        else:
            anomalies.append({'kind':'其他积分备注待确认', **base})
        if int(order.get('points_used') or 0) < 0 or int(order.get('points') or 0) < 0:
            anomalies.append({'kind':'积分字段出现负数', **base})
    counts = {}
    for item in anomalies:
        counts[item['kind']] = counts.get(item['kind'], 0) + 1
    return {'customers_checked': len(customers), 'orders_with_point_notes': checked,
            'anomaly_count': len(anomalies), 'counts': counts, 'anomalies': anomalies}


def update_customer_type_by_history(c, customer_id=None):
    """自动维护客户类型：充值为 SVIP；月消费前5/30天高定价复购为 VIP。"""
    month = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m')
    top_vip_ids = {
        int(r['customer_id'])
        for r in c.execute("""
            SELECT customer_id
            FROM orders
            WHERE customer_id IS NOT NULL
              AND COALESCE(order_status,'') NOT IN ('取消','鍙栨秷')
              AND COALESCE(received_amount,0) > 0
              AND substr(COALESCE(order_date,''),1,7)=?
            GROUP BY customer_id
            ORDER BY SUM(COALESCE(received_amount,0)) DESC, COUNT(*) DESC, MAX(id) DESC
            LIMIT 5
        """, (month,)).fetchall()
        if r['customer_id']
    }
    cutoff = (datetime.utcnow() + timedelta(hours=9) - timedelta(days=30)).strftime('%Y-%m-%d')
    high_price_stats = {}
    for r in c.execute("""
        SELECT o.customer_id, o.girl_id, o.girl_name, o.hours, o.service_time, COALESCE(g.list_price,0) AS list_price
        FROM orders o
        LEFT JOIN girls g ON g.id=o.girl_id OR (COALESCE(o.girl_id,0)=0 AND g.name=o.girl_name)
        WHERE o.customer_id IS NOT NULL
          AND COALESCE(o.order_status,'') NOT IN ('取消','鍙栨秷')
          AND COALESCE(o.order_date,'') >= ?
          AND COALESCE(g.list_price,0) >= 25000
    """, (cutoff,)).fetchall():
        cid = int(r['customer_id'])
        girl_key = str(r['girl_id'] or '').strip() or str(r['girl_name'] or '').strip()
        if not girl_key:
            continue
        info = high_price_stats.setdefault(cid, {'girls': set(), 'has_long': False})
        info['girls'].add(girl_key)
        try:
            hours = float(r['hours'] or calc_hours(r['service_time']))
        except Exception:
            hours = 1.0
        if hours > 1.0:
            info['has_long'] = True
    high_price_vip_ids = {
        cid for cid, info in high_price_stats.items()
        if len(info['girls']) >= 2 and info['has_long']
    }
    recharged_ids = {
        int(r['customer_id'])
        for r in c.execute("""
            SELECT DISTINCT customer_id
            FROM recharge_records
            WHERE customer_id IS NOT NULL AND COALESCE(amount,0) > 0
        """).fetchall()
        if r['customer_id']
    }
    for r in c.execute("SELECT id FROM customers WHERE COALESCE(total_recharge,0)>0 OR COALESCE(recharge_balance,0)>0").fetchall():
        recharged_ids.add(int(r['id']))

    params = []
    where = ""
    if customer_id:
        where = "WHERE c.id=?"
        params.append(int(customer_id))
    customer_rows = c.execute(f"""
        SELECT c.id, c.customer_type, COALESCE(c.customer_type_locked,0) AS customer_type_locked, COALESCE(o.total_orders,0) AS total_orders
        FROM customers c
        LEFT JOIN (
            SELECT customer_id, COUNT(*) AS total_orders
            FROM orders
            WHERE customer_id IS NOT NULL
            GROUP BY customer_id
        ) o ON o.customer_id=c.id
        {where}
    """, params).fetchall()

    for row in customer_rows:
        if int(row['customer_type_locked'] or 0):
            continue
        cid = int(row['id'])
        total_orders = int(row['total_orders'] or 0)
        if cid in recharged_ids:
            new_type = 'SVIP'
        elif cid in top_vip_ids or cid in high_price_vip_ids:
            new_type = 'VIP'
        elif total_orders >= 3:
            new_type = '老客'
        elif total_orders >= 2:
            new_type = '回头客'
        else:
            new_type = '新客'
        if (row['customer_type'] or '') != new_type:
            c.execute("UPDATE customers SET customer_type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_type, cid))



def detect_payment_method_from_note(*texts):
    """根据备注关键词自动识别支付方式。命中后覆盖传入的默认支付方式。"""
    text = ' '.join(str(t or '') for t in texts).lower().replace(' ', '')
    if not text:
        return None
    rules = [
        ('PayPay', ['paypay', 'ペイペイ', 'pay pay']),
        ('微信支付', ['微信', 'wechat', 'weixin', 'wx']),
        ('支付宝', ['支付宝', 'alipay', 'ali pay']),
        ('楽天Pay', ['楽天pay', '楽天ペイ', 'rakutenpay']),
        ('LINE Pay', ['linepay', 'lineペイ']),
        ('d払い', ['d払い', 'dbarai']),
        ('现金', ['现金', '現金', 'cash']),
        ('人民币', ['人民币', 'rmb', '人民元']),
    ]
    for method, keys in rules:
        if any(k.lower().replace(' ', '') in text for k in keys):
            return method
    return None

def point_use_note_triggered(*texts):
    text = re.sub(r'\s+', '', ' '.join(str(value or '') for value in texts))
    return '积分' in text and any(word in text for word in ('减免','抵扣','折扣','全扣'))

def create_or_update_order(c,d):
    old_customer_id = None
    old_points_used = 0
    old_order_points = 0
    old_order_remark = ''
    old_raw_text = ''
    if d.get('id'):
        old = c.execute("""SELECT customer_id,COALESCE(points,0) AS points,
                                  COALESCE(points_used,0) AS points_used,COALESCE(remark,'') AS remark,
                                  COALESCE(raw_text,'') AS raw_text FROM orders WHERE id=?""", (int(d['id']),)).fetchone()
        if old:
            old_customer_id = old["customer_id"]
            old_points_used = int(old["points_used"] or 0)
            old_order_points = int(old["points"] or 0)
            old_order_remark = str(old["remark"] or '')
            old_raw_text = str(old["raw_text"] or '')

    g = None
    if d.get('girl_id'):
        g = c.execute('SELECT * FROM girls WHERE id=?',(int(d['girl_id']),)).fetchone()
    if not g:
        g = ensure_girl(c,d.get('girl_name',''))
    if not g:
        raise ValueError('缺少女孩')

    d = dict(d)
    d['service_time'] = normalize_chain_time_token(
        d.get('service_time', ''),
        _shift_intervals_for_girl(c, d.get('order_date'), g['name'])
    )
    validate_service_time(d.get('service_time',''))
    h = float(d.get('hours') or calc_hours(d.get('service_time','')))
    rec = int(d.get('received_amount') or 0)
    # 订单编辑时允许单独修改“女孩到手”，不反写女孩表。
    # 未传 girl_take_home 时，才按女孩表默认规则计算。
    if 'girl_take_home' in d and str(d.get('girl_take_home') or '').strip() != '':
        th = int(d.get('girl_take_home') or 0)
    else:
        th = take_home(g,h)
    prof = round_yen_1000_half_up(rec - th)
    cust = ensure_customer(c,d.get('customer_raw',''),d.get('remark',''))
    remark = str(d.get('remark') or '')
    discount_requested = point_use_note_triggered(remark, d.get('remark2'), d.get('raw_text'))
    if d.get('id'):
        # 编辑旧订单只改订单资料，不再触碰客户当前积分，也不重复赠送/扣除。
        pts = old_order_points
        points_used = old_points_used
    elif discount_requested:
        available = max(0, int(cust['points'] or 0))
        pts = 500
        points_used = available
        remark = re.sub(r'\s*[｜|]?\s*积分抵扣金额[：:]\s*¥?[\d,]+', '', remark).strip()
        remark = f"{remark}｜积分抵扣金额：¥{available:,}"
    else:
        pts = max(0, math.floor(rec/20))
        requested_points = max(0, int(d.get('points_used') or 0))
        available = max(0, int(cust['points'] or 0))
        points_used = min(requested_points, available)
    auto_payment = detect_payment_method_from_note(d.get('remark',''), d.get('remark2',''), d.get('raw_text',''))
    payment_method = auto_payment or (d.get('payment_method') or '现金')

    if d.get('id'):
        c.execute("""UPDATE orders SET order_date=?,service_time=?,hours=?,girl_id=?,girl_name=?,customer_id=?,customer_no=?,customer_name=?,received_amount=?,girl_take_home=?,store_profit=?,points=?,points_used=?,order_status=?,settlement_status=?,payment_method=?,remark=?,remark2=?,raw_text=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,points_used,d.get('order_status','已结束'),d.get('settlement_status','未结算'),payment_method,remark,d.get('remark2',''),d.get('raw_text',old_raw_text),d.get('id')))
        if old_customer_id and old_customer_id != cust['id']:
            refresh_customer_totals(c, old_customer_id)
        refresh_customer_totals(c, cust['id'])
    else:
        if '取消' in str(d.get('order_status') or ''):
            pts = points_used = 0
        cur = c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,customer_no,customer_name,received_amount,girl_take_home,store_profit,points,points_used,order_status,settlement_status,payment_method,remark,remark2,raw_text)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,points_used,d.get('order_status','已结束'),d.get('settlement_status','未结算'),payment_method,remark,d.get('remark2',''),d.get('raw_text','')))
        new_balance = max(0, int(cust['points'] or 0) + int(pts or 0) - int(points_used or 0))
        c.execute("UPDATE customers SET points=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_balance,cust['id']))
        refresh_customer_totals(c, cust['id'])


@app.route('/')
def index(): return send_from_directory(APP_DIR/'static','index.html')
@app.route('/reserve')
def reserve_page(): return send_from_directory(APP_DIR/'static','reserve.html')
@app.route('/tonight')
def tonight_page(): return send_from_directory(APP_DIR/'static','tonight.html')
@app.route('/girl_praises/<path:filename>')
def girl_praise_file(filename):
    name = Path(str(filename or '')).name
    if name != filename or not re.fullmatch(r'[0-9a-f]{16,64}\.(?:png|jpg|jpeg|webp|gif)', name, re.I):
        return 'Not found', 404
    for folder in (GIRL_PRAISE_DIR, LEGACY_GIRL_PRAISE_DIR):
        path = folder / name
        if path.exists() and path.is_file():
            return send_from_directory(folder, name)
    try:
        init_db()
        with conn() as c:
            row = c.execute("""SELECT image_blob, image_mime FROM girl_praises
                               WHERE image_path IN (?,?)
                               ORDER BY updated_at DESC, id DESC LIMIT 1""",
                            ('/girl_praises/' + name, '/static/girl_praises/' + name)).fetchone()
        if row and row['image_blob']:
            return Response(bytes(row['image_blob']), mimetype=row['image_mime'] or 'image/png')
    except Exception:
        traceback.print_exc()
    return 'Not found', 404

def remove_girl_praise_file(image_path):
    name = Path(str(image_path or '')).name
    if not re.fullmatch(r'[0-9a-f]{16,64}\.(?:png|jpg|jpeg|webp|gif)', name, re.I):
        return
    for folder in (GIRL_PRAISE_DIR, LEGACY_GIRL_PRAISE_DIR):
        try:
            base = folder.resolve()
            path = (folder / name).resolve()
            if path.parent == base and path.exists() and path.is_file():
                path.unlink()
        except Exception:
            traceback.print_exc()
@app.route('/api/all')
def all_data():
    global LAST_FULL_MAINTENANCE_DAY
    init_db()
    with conn() as c:
        today_for_mcr = tokyo_today_date()
        normalize_chain_order_times_for_date(c, (today_for_mcr - timedelta(days=1)).isoformat())
        normalize_chain_order_times_for_date(c, today_for_mcr.isoformat())
        auto_finish_reservations(c)
        maintenance_day = today_for_mcr.isoformat()
        if LAST_FULL_MAINTENANCE_DAY != maintenance_day:
            update_customer_type_by_history(c, None)
            LAST_FULL_MAINTENANCE_DAY = maintenance_day
        payload = {
            'ok': True,
            'version': APP_VERSION,
            'customers':rows(c.execute('''SELECT c.*, COALESCE(o.total_orders,0) AS total_orders, COALESCE(o.total_spent, c.total_spent, 0) AS total_spent FROM customers c LEFT JOIN (SELECT customer_id, COUNT(*) AS total_orders, SUM(received_amount) AS total_spent FROM orders GROUP BY customer_id) o ON o.customer_id=c.id ORDER BY c.id DESC''').fetchall()),
            'girls':rows(c.execute('SELECT * FROM girls ORDER BY id DESC').fetchall()),
            'orders':rows(c.execute('''SELECT o.*, COALESCE(c.customer_type,'新客') AS customer_type,
                                             COALESCE(oc.customer_total_orders,0) AS customer_total_orders
                                      FROM orders o
                                      LEFT JOIN customers c ON c.id=o.customer_id
                                      LEFT JOIN (SELECT customer_id, COUNT(*) AS customer_total_orders FROM orders GROUP BY customer_id) oc ON oc.customer_id=o.customer_id
                                      ORDER BY o.order_date DESC, o.id DESC''').fetchall()),
            'recharges':rows(c.execute('SELECT * FROM recharge_records ORDER BY id DESC').fetchall()),
            'points':rows(c.execute('SELECT * FROM points_records ORDER BY id DESC').fetchall()),
            'enums':rows(c.execute('SELECT * FROM enum_values ORDER BY enum_type,sort_order,id').fetchall()),
            'schedules':rows(c.execute('SELECT * FROM girl_schedules ORDER BY schedule_date DESC,id DESC').fetchall()),
            'hotel_rooms':rows(c.execute('SELECT * FROM hotel_rooms ORDER BY hotel_name, room_no').fetchall()),
            'room_assignments':rows(c.execute('SELECT * FROM room_assignments ORDER BY assignment_date DESC, hotel_name, room_no').fetchall()),
            'customer_accounts':rows(c.execute('SELECT * FROM customer_accounts ORDER BY id DESC').fetchall()),
            'customer_reservations':rows(c.execute('SELECT * FROM customer_reservations ORDER BY reserve_date DESC, start_time DESC, id DESC').fetchall()),
            'quick_links':rows(c.execute('SELECT * FROM quick_links ORDER BY sort_order, id').fetchall()),
            'girl_praises':rows(c.execute('''SELECT gp.id, gp.girl_id, gp.girl_name, gp.source_name, gp.image_path,
                                                    gp.image_mime, gp.publish_status, gp.wp_post_id, gp.wp_attachment_id,
                                                    gp.published_at, gp.publish_error, gp.created_at, gp.updated_at,
                                                    CASE WHEN gp.image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image_blob,
                                                    COALESCE(g.name, gp.girl_name) AS display_girl_name
                                             FROM girl_praises gp
                                             LEFT JOIN girls g ON g.id=gp.girl_id
                                             ORDER BY gp.created_at DESC, gp.id DESC''').fetchall())}
        session = current_session_info()
        if session.get('role') != 'boss':
            allowed = set(session.get('permissions') or [])
            if 'customers' not in allowed:
                payload['customers'] = []; payload['recharges'] = []; payload['points'] = []
            if not ({'girls','pureShift','telegramBooking','chainReserve','rooms'} & allowed):
                payload['girls'] = []; payload['girl_praises'] = []
            if not ({'orders','home','stats','settlement','chainReserve'} & allowed):
                payload['orders'] = []
            if 'pureShift' not in allowed:
                payload['schedules'] = []
            if 'rooms' not in allowed:
                payload['hotel_rooms'] = []; payload['room_assignments'] = []
            if 'advanceReserve' not in allowed:
                payload['customer_accounts'] = []; payload['customer_reservations'] = []
            if 'quickLinks' not in allowed:
                payload['quick_links'] = []
            if 'enums' not in allowed:
                payload['enums'] = []
        return jsonify(payload)
@app.route('/api/customers',methods=['POST'])
def customers():
    d=request.json or {}
    with conn() as c:
        no=d.get('customer_no') or next_customer_no(c)
        if str(no).isdigit(): no=f'{int(no):04d}'
        manual_type = str(d.get('customer_type','新客') or '新客').strip()
        type_locked = 1 if manual_type.upper() in ('VIP','SVIP') else 0
        vals=(no,d.get('name') or f'客户{no}',manual_type,d.get('customer_status','正常'),int(d.get('recharge_balance') or 0),int(d.get('total_recharge') or 0),int(d.get('total_spent') or 0),int(d.get('points') or 0),int(d.get('total_points') or 0),d.get('source',''),d.get('contact',''),d.get('grade',''),d.get('tags',''),d.get('member_level',''),d.get('remark',''),d.get('remark2',''),type_locked)
        if d.get('id'):
            c.execute('''UPDATE customers SET customer_no=?,name=?,customer_type=?,customer_status=?,recharge_balance=?,total_recharge=?,total_spent=?,points=?,total_points=?,source=?,contact=?,grade=?,tags=?,member_level=?,remark=?,remark2=?,customer_type_locked=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',vals+(d.get('id'),))
        else:
            c.execute('''INSERT INTO customers(customer_no,name,customer_type,customer_status,recharge_balance,total_recharge,total_spent,points,total_points,source,contact,grade,tags,member_level,remark,remark2,customer_type_locked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',vals)
        update_customer_type_by_history(c, None)
    return jsonify(ok=True)


@app.route('/api/customers/cleanup_zero_orders', methods=['POST'])
def cleanup_zero_order_customers():
    """批量清除没有任何订单的客户，并在同一数据库中保存可恢复快照。"""
    if current_role() != 'boss':
        return jsonify(ok=False, error='只有老板账号可以批量清理客户'), 403
    d = request.get_json(silent=True) or {}
    execute = bool(d.get('execute'))
    init_db()
    with conn() as c:
        candidates = rows(c.execute("""SELECT c.* FROM customers c
                                       WHERE NOT EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.id)
                                       ORDER BY c.id""").fetchall())
        summary = {
            'count': len(candidates),
            'with_points': sum(1 for x in candidates if int(x.get('points') or 0) > 0),
            'with_balance': sum(1 for x in candidates if int(x.get('recharge_balance') or 0) > 0),
        }
        if not execute or not candidates:
            return jsonify(ok=True, preview=True, **summary)
        ids = [int(x['id']) for x in candidates]
        placeholders = ','.join('?' for _ in ids)
        snapshot = {'customers': candidates}
        related_tables = {
            'recharge_records': 'customer_id', 'points_records': 'customer_id',
            'customer_accounts': 'customer_id', 'customer_reservations': 'customer_id',
            'telegram_customer_cancellations': 'customer_id', 'telegram_customers': 'customer_id',
        }
        existing_tables = {str(x[0]) for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, column in related_tables.items():
            if table in existing_tables:
                snapshot[table] = rows(c.execute(
                    f"SELECT * FROM {table} WHERE {column} IN ({placeholders})", ids).fetchall())
        batch_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + secrets.token_hex(4)
        c.execute("""INSERT INTO customer_cleanup_archives(batch_id,actor_name,reason,customer_count,payload_json)
                     VALUES(?,?,?,?,?)""", (batch_id, str(current_session_info().get('username') or ''),
                     '清理预约单数为0的客户', len(candidates), json.dumps(snapshot, ensure_ascii=False)))
        for table in ('recharge_records', 'points_records', 'telegram_customer_cancellations', 'telegram_customers'):
            if table in existing_tables:
                c.execute(f"DELETE FROM {table} WHERE customer_id IN ({placeholders})", ids)
        for table in ('customer_accounts', 'customer_reservations'):
            if table in existing_tables:
                c.execute(f"UPDATE {table} SET customer_id=0 WHERE customer_id IN ({placeholders})", ids)
        deleted = int(c.execute(f"DELETE FROM customers WHERE id IN ({placeholders})", ids).rowcount or 0)
    return jsonify(ok=True, deleted=deleted, backup_batch_id=batch_id, **summary)
@app.route('/api/girls',methods=['POST'])
def girls():
    d=request.json or {}
    name = str(d.get('name') or '').strip()
    if not name:
        return jsonify(ok=False, error='女孩名不能为空'), 400
    init_db()
    with conn() as c:
        if d.get('id'):
            c.execute('''UPDATE girls SET name=?,girl_alias=?,girl_type=?,girl_status=?,enrollment=?,take_home_per_hour=?,list_price=?,contact=?,tags=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(name,d.get('girl_alias',''),d.get('girl_type'),d.get('girl_status'),d.get('enrollment',''),int(d.get('take_home_per_hour') or 10000),int(d.get('list_price') or 15000),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2',''),d.get('id')))
            recalc_girl(c,int(d['id']))
        else:
            c.execute('''INSERT OR IGNORE INTO girls(name,girl_alias,girl_type,girl_status,enrollment,take_home_per_hour,list_price,contact,tags,remark,remark2) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(name,d.get('girl_alias',''),d.get('girl_type','普通'),d.get('girl_status','在职'),d.get('enrollment',''),int(d.get('take_home_per_hour') or 10000),int(d.get('list_price') or 15000),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2','')))
            row=c.execute('SELECT id FROM girls WHERE name=?',(name,)).fetchone()
            if row: recalc_girl(c,row['id'])
    return jsonify(ok=True)

@app.route('/api/girl_praises', methods=['GET', 'POST'])
def api_girl_praises():
    init_db()
    if request.method == 'GET':
        girl_name = str(request.args.get('girl_name') or '').strip()
        with conn() as c:
            if girl_name:
                g = c.execute('SELECT id,name FROM girls WHERE name=?', (girl_name,)).fetchone()
                if g:
                    data = rows(c.execute('''SELECT gp.id, gp.girl_id, gp.girl_name, gp.source_name, gp.image_path,
                                                    gp.image_mime, gp.publish_status, gp.wp_post_id, gp.wp_attachment_id,
                                                    gp.published_at, gp.publish_error, gp.created_at, gp.updated_at,
                                                    CASE WHEN gp.image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image_blob,
                                                    COALESCE(g.name, gp.girl_name) AS display_girl_name
                                             FROM girl_praises gp
                                             LEFT JOIN girls g ON g.id=gp.girl_id
                                             WHERE gp.girl_id=? OR gp.girl_name=?
                                             ORDER BY gp.created_at DESC, gp.id DESC''',
                                          (g['id'], girl_name)).fetchall())
                else:
                    data = rows(c.execute('''SELECT gp.id, gp.girl_id, gp.girl_name, gp.source_name, gp.image_path,
                                                    gp.image_mime, gp.publish_status, gp.wp_post_id, gp.wp_attachment_id,
                                                    gp.published_at, gp.publish_error, gp.created_at, gp.updated_at,
                                                    CASE WHEN gp.image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image_blob,
                                                    gp.girl_name AS display_girl_name
                                             FROM girl_praises gp
                                             WHERE gp.girl_name=?
                                             ORDER BY gp.created_at DESC, gp.id DESC''',
                                          (girl_name,)).fetchall())
            else:
                data = rows(c.execute('''SELECT gp.id, gp.girl_id, gp.girl_name, gp.source_name, gp.image_path,
                                                gp.image_mime, gp.publish_status, gp.wp_post_id, gp.wp_attachment_id,
                                                gp.published_at, gp.publish_error, gp.created_at, gp.updated_at,
                                                CASE WHEN gp.image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image_blob,
                                                COALESCE(g.name, gp.girl_name) AS display_girl_name
                                         FROM girl_praises gp
                                         LEFT JOIN girls g ON g.id=gp.girl_id
                                         ORDER BY gp.created_at DESC, gp.id DESC''').fetchall())
        return jsonify(ok=True, praises=data)

    d = request.json or {}
    publish_id = d.get('publish_id')
    if publish_id:
        try:
            return jsonify(ok=True, publication=publish_girl_praise_to_wordpress(int(publish_id)))
        except Exception as exc:
            with conn() as c:
                c.execute("""UPDATE girl_praises SET publish_status='未上架',publish_error=?,
                             updated_at=CURRENT_TIMESTAMP WHERE id=?""", (str(exc)[:1000], int(publish_id)))
            return jsonify(ok=False, error=str(exc)), 502
    delete_id = d.get('delete_id')
    if delete_id:
        with conn() as c:
            row = c.execute('SELECT id,image_path FROM girl_praises WHERE id=?', (delete_id,)).fetchone()
            if not row:
                return jsonify(ok=False, error='图片不存在或已删除'), 404
            image_path = row['image_path']
            c.execute('DELETE FROM girl_praises WHERE id=?', (delete_id,))
        remove_girl_praise_file(image_path)
        return jsonify(ok=True, deleted_id=int(delete_id))

    girl_name = str(d.get('girl_name') or '').strip()
    image_data = str(d.get('image_data') or '').strip()
    source_name = str(d.get('source_name') or '').strip()
    if not girl_name:
        return jsonify(ok=False, error='女孩名不能为空'), 400
    if not image_data.startswith('data:image/'):
        return jsonify(ok=False, error='图片数据格式不正确'), 400
    try:
        header, b64 = image_data.split(',', 1)
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return jsonify(ok=False, error='图片数据解析失败'), 400
    if len(raw) < 100:
        return jsonify(ok=False, error='图片数据太小，保存失败'), 400
    if len(raw) > 20 * 1024 * 1024:
        return jsonify(ok=False, error='图片超过20MB，保存失败'), 400
    ext = '.png'
    mime = 'image/png'
    header_l = header.lower()
    if 'jpeg' in header_l or 'jpg' in header_l:
        ext = '.jpg'
        mime = 'image/jpeg'
    elif 'webp' in header_l:
        ext = '.webp'
        mime = 'image/webp'
    elif 'gif' in header_l:
        ext = '.gif'
        mime = 'image/gif'
    GIRL_PRAISE_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        g = c.execute('SELECT id,name FROM girls WHERE name=?', (girl_name,)).fetchone()
        if not g:
            return jsonify(ok=False, error=f'女孩不存在：{girl_name}'), 400
        key_src = f"{g['id']}|{g['name']}|{datetime.now(timezone.utc).isoformat()}|{secrets.token_hex(8)}"
        filename = hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:24] + ext
        path = GIRL_PRAISE_DIR / filename
        path.write_bytes(raw)
        rel = '/girl_praises/' + filename
        cur = c.execute('''INSERT INTO girl_praises(girl_id,girl_name,source_name,image_path,image_mime,image_blob,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
                        (g['id'], g['name'], source_name or '客人好评', rel, mime, sqlite3.Binary(raw)))
        row = c.execute('''SELECT gp.id, gp.girl_id, gp.girl_name, gp.source_name, gp.image_path,
                                  gp.image_mime, gp.publish_status, gp.wp_post_id, gp.wp_attachment_id,
                                  gp.published_at, gp.publish_error, gp.created_at, gp.updated_at,
                                  CASE WHEN gp.image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image_blob,
                                  COALESCE(g.name, gp.girl_name) AS display_girl_name
                           FROM girl_praises gp
                           LEFT JOIN girls g ON g.id=gp.girl_id
                           WHERE gp.id=?''', (cur.lastrowid,)).fetchone()
    return jsonify(ok=True, praise=dict(row))

@app.route('/api/orders',methods=['POST'])
def orders():
    with conn() as c: create_or_update_order(c, request.json or {})
    return jsonify(ok=True)


def customer_refresh_rows(c, customer_ids):
    """Return only customers affected by a local order mutation; avoids reloading /api/all."""
    ids = sorted({int(x) for x in customer_ids if x})
    result = []
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        q = ','.join(['?'] * len(batch))
        result.extend(dict(r) for r in c.execute(f'''SELECT c.*,
                    COALESCE(o.total_orders,0) AS total_orders,
                    COALESCE(o.total_spent,c.total_spent,0) AS total_spent
                FROM customers c
                LEFT JOIN (
                    SELECT customer_id,COUNT(*) AS total_orders,SUM(received_amount) AS total_spent
                    FROM orders WHERE customer_id IN ({q}) GROUP BY customer_id
                ) o ON o.customer_id=c.id
                WHERE c.id IN ({q})''', batch + batch).fetchall())
    return result


@app.route('/api/delete/<table>/<int:item_id>',methods=['POST'])
def delete(table,item_id):
    allowed={'customers':'customers','girls':'girls','orders':'orders','recharges':'recharge_records','points':'points_records'}
    if table not in allowed: return jsonify(ok=False),400
    with conn() as c:
        affected_customer_id = None
        if table == 'orders':
            old = c.execute('SELECT customer_id FROM orders WHERE id=?', (item_id,)).fetchone()
            affected_customer_id = old['customer_id'] if old else None
        elif table in ('recharges', 'points'):
            old = c.execute(f'SELECT customer_id FROM {allowed[table]} WHERE id=?', (item_id,)).fetchone()
            affected_customer_id = old['customer_id'] if old else None
        if table == 'orders':
            c.execute('DELETE FROM chain_import_rows WHERE order_id=?', (item_id,))
            c.execute('UPDATE customer_reservations SET order_id=0, updated_at=CURRENT_TIMESTAMP WHERE order_id=?', (item_id,))
        cur = c.execute(f'DELETE FROM {allowed[table]} WHERE id=?',(item_id,))
        if table == 'orders' and affected_customer_id:
            refresh_customer_totals(c, affected_customer_id, update_types=False)
        elif table in ('customers', 'recharges', 'points'):
            update_customer_type_by_history(c, None)
        changed_customers = customer_refresh_rows(c, [affected_customer_id] if affected_customer_id else [])
    return jsonify(ok=True, deleted=int(cur.rowcount or 0), customers=changed_customers)
@app.route('/api/delete_by_date',methods=['POST'])
def delete_by_date():
    d=request.json or {}; start=d.get('start'); end=d.get('end'); table=d.get('table','orders')
    if table!='orders': return jsonify(ok=False,error='现在只支持按日期删除订单'),400
    with conn() as c:
        cur=c.execute('DELETE FROM orders WHERE order_date>=? AND order_date<=?',(start,end))
        return jsonify(ok=True,deleted=cur.rowcount)
@app.route('/api/enums',methods=['POST'])
def enums():
    d=request.json or {}
    with conn() as c:
        if d.get('delete_id'): c.execute('DELETE FROM enum_values WHERE id=?',(d['delete_id'],))
        elif d.get('enum_type') and d.get('value'): c.execute('INSERT OR IGNORE INTO enum_values(enum_type,value,sort_order) VALUES(?,?,?)',(d['enum_type'],d['value'],999))
    return jsonify(ok=True)




def strip_chain_prefix(line):
    s = str(line or "").strip()
    s = re.sub(r"^#?\s*接龙\s*", "", s)
    # remove only leading list number like "1." or "2、"
    s = re.sub(r"^\s*\d+\s*[.、]\s*", "", s)
    return s.strip()

def _storage_time_label(minute):
    h = (int(minute) // 60) % 24
    mi = int(minute) % 60
    return f"{h:02d}:{mi:02d}"

def _chain_time_candidates(hour, minute):
    if hour >= 24:
        return [hour * 60 + minute]
    if hour >= 13:
        return [hour * 60 + minute]
    if hour == 12:
        return [12 * 60 + minute, 24 * 60 + minute]
    if hour == 0:
        return [24 * 60 + minute]
    return [(hour + 12) * 60 + minute, (hour + 24) * 60 + minute]

def _chain_interval_candidates(sh, sm, eh, em):
    seen = set()
    candidates = []
    for start in _chain_time_candidates(sh, sm):
        for base_end in _chain_time_candidates(eh, em):
            end = base_end
            while end <= start:
                end += 12 * 60
            duration = end - start
            if 0 < duration <= 7 * 60 and (start, end) not in seen:
                seen.add((start, end))
                candidates.append((start, end))
    return candidates

def _choose_interval_for_shifts(candidates, shift_intervals=None):
    if not candidates:
        return None
    shifts = [s for s in (shift_intervals or []) if s and s[0] is not None and s[1] is not None]
    if not shifts:
        return candidates[0]

    def score(interval):
        a, b = interval
        best = None
        for sa, sb in shifts:
            overlap = max(0, min(b, sb) - max(a, sa))
            inside = sa <= a and b <= sb
            distance = abs(a - sa) + abs(b - sb)
            item = (0 if inside else 1, -overlap, distance, a)
            if best is None or item < best:
                best = item
        return best or (1, 0, 0, a)

    return sorted(candidates, key=score)[0]

def _shift_intervals_for_girl(c, date_str, girl_name):
    if not c or not date_str or not girl_name:
        return []
    out = []
    try:
        for sft in pure_shift_rows_for_date(c, date_str):
            if str(sft.get('girl') or '').strip() != str(girl_name or '').strip():
                continue
            if _is_package_time(sft.get('start')) or _is_package_time(sft.get('end')):
                out.append((24 * 60, 29 * 60))
            else:
                interval = _interval_minutes(sft.get('start'), sft.get('end'))
                if interval:
                    out.append(interval)
    except Exception:
        traceback.print_exc()
    return out

def _explicit_24h_chain_token(token, sh, eh):
    raw = str(token or '')
    if ':' not in raw:
        return False
    parts = re.split(r"(?:[-~ー～]|到|至)", re.sub(r"\s+", "", raw), maxsplit=1)
    if len(parts) != 2:
        return True
    return (
        parts[0].startswith('0') or parts[1].startswith('0')
        or sh == 0 or eh == 0 or sh >= 13 or eh >= 13
    )

def normalize_chain_time_token(token, shift_intervals=None):
    """
    接龙时间显示标准化。
    支持 7.30到8.30 / 7.30-8.30 / 23.30-0.30。
    注意：7.30 表示 7点30分，不会被拼成 7.3030。
    """
    token = re.sub(r"\s+", "", str(token or ""))
    m = re.match(r"^(\d{1,2})(?:[:.](\d{1,2}))?(?:[-~ー～]|到|至)(\d{1,2})(?:[:.](\d{1,2}))?$", token)
    if not m:
        return token

    sh, sm, eh, em = _parse_time_groups(m)
    if _explicit_24h_chain_token(token, sh, eh):
        start = _business_clock_minutes_24h(sh, sm)
        end = _business_clock_minutes_24h(eh, em)
        if end <= start:
            end += 24 * 60
        return f"{_storage_time_label(start)}-{_storage_time_label(end)}"

    interval = _choose_interval_for_shifts(_chain_interval_candidates(sh, sm, eh, em), shift_intervals)
    if not interval:
        return token
    return f"{_storage_time_label(interval[0])}-{_storage_time_label(interval[1])}"

def parse_chain_service_time(line, shift_intervals=None):
    body = strip_chain_prefix(line)
    if "包夜" in body:
        return "包夜 12.00-5.00", body
    # support 5.30-6.30, 10-12, 6.40-7.40, 8:45-9:45, 23.30-0.30
    m = re.search(r"(\d{1,2}(?:[:.]\d{1,2})?\s*(?:[-~ー～]|到|至)\s*\d{1,2}(?:[:.]\d{1,2})?)(.*)$", body)
    if not m:
        return None, body
    return normalize_chain_time_token(m.group(1), shift_intervals), m.group(2)

def split_chain_fields(rest_raw):
    """
    接龙字段解析：时间之后按 / 或空白分字段。
    规则：价格后的字段必须是客人。纯数字=既有客人ID；字符串=第一次预约用户名。
    如果用户名本身是纯数字，用双斜杠写法强制当用户名：8-9/10000//123/备注。
    """
    text = str(rest_raw or '').strip().replace('（', ' ').replace('）', ' ').replace('(', ' ').replace(')', ' ')
    fields, buf, force_next_name = [], [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '/':
            if i + 1 < len(text) and text[i + 1] == '/':
                token = ''.join(buf).strip()
                if token:
                    fields.append((token, False))
                buf = []
                force_next_name = True
                i += 2
                continue
            token = ''.join(buf).strip()
            if token:
                fields.append((token, force_next_name))
            buf = []
            force_next_name = False
            i += 1
            continue
        if ch.isspace():
            token = ''.join(buf).strip()
            if token:
                fields.append((token, force_next_name))
                buf = []
                force_next_name = False
            i += 1
            continue
        buf.append(ch)
        i += 1
    token = ''.join(buf).strip()
    if token:
        fields.append((token, force_next_name))
    return fields


def chain_sequence_no(line, fallback):
    s = re.sub(r"^#?\s*接龙\s*", "", str(line or "").strip())
    m = re.match(r"^\s*(\d+)\s*[.、]", s)
    return int(m.group(1)) if m else int(fallback)


def normalize_chain_import_line(line):
    return re.sub(r"\s+", "", str(line or "")).replace('：', ':').replace('／', '/')


def import_chain_text(text, order_date='', girl_id=None, settlement_status='未结算',
                      source_chat_id='', source_message_id=''):
    lines = [x.strip() for x in str(text or '').splitlines() if x.strip()]
    hd, hg = parse_header(lines)
    od = order_date or hd or str(date.today())
    count = 0
    inserted = 0
    updated = 0
    unchanged = 0
    with conn() as c:
        g = None
        if girl_id:
            g = c.execute('SELECT * FROM girls WHERE id=?', (int(girl_id),)).fetchone()
        if not g:
            g = ensure_girl(c, hg)
        if not g:
            raise ValueError('无法识别女孩名。请确认首行类似：0524小樱')

        shift_intervals = _shift_intervals_for_girl(c, od, g['name'])
        parsed = []
        seen_sequences = set()
        fallback_sequence = 0
        for line in lines:
            line_date, line_girl = parse_header([line])
            if line_date and line_date == hd and (not line_girl or line_girl == hg):
                continue
            st, rest_raw = parse_chain_service_time(line, shift_intervals)
            if not st:
                continue

            fallback_sequence += 1
            sequence_no = chain_sequence_no(line, fallback_sequence)
            if sequence_no in seen_sequences:
                raise ValueError(f'接龙序号 {sequence_no} 重复，请检查后重新导入。')
            seen_sequences.add(sequence_no)

            parts = split_chain_fields(rest_raw)

            if parts and '包夜' in parts[0][0]:
                parts.pop(0)
            if not parts:
                raise ValueError(f'接龙行缺少价格和客人字段：{line}。格式：时间/价格/客人用户名 或 时间/价格/客人ID。')

            price_token = parts.pop(0)[0]
            rec = yen_to_int(price_token)
            if rec <= 0:
                raise ValueError(f'接龙行价格无法识别：{line}。请填写例如 15000。')
            if not parts:
                raise ValueError(f'接龙行缺少客人字段：{line}。格式：时间/价格/客人用户名 或 时间/价格/客人ID。')
            cust_token, force_name = parts.pop(0)
            cust = ('__NAME__:' + cust_token) if force_name else cust_token
            remark_parts = [p[0] for p in parts]
            parsed.append((sequence_no, normalize_chain_import_line(line), {
                'order_date': od,
                'service_time': st,
                'girl_id': g['id'],
                'received_amount': rec,
                'customer_raw': cust,
                'remark': ' '.join(remark_parts),
                'settlement_status': settlement_status,
                'raw_text': line
            }))

        legacy_orders = c.execute("""SELECT id,raw_text FROM orders
                                     WHERE order_date=? AND girl_id=? AND COALESCE(raw_text,'')<>''
                                     ORDER BY id""", (od, g['id'])).fetchall()
        legacy_by_sequence = {}
        for legacy in legacy_orders:
            raw = str(legacy['raw_text'] or '')
            seq = chain_sequence_no(raw, 0)
            if seq > 0 and seq not in legacy_by_sequence:
                legacy_by_sequence[seq] = legacy

        for sequence_no, normalized, order_data in parsed:
            mapping = c.execute("""SELECT * FROM chain_import_rows
                                   WHERE order_date=? AND girl_id=? AND sequence_no=?""",
                                (od, g['id'], sequence_no)).fetchone()
            order_id = int(mapping['order_id']) if mapping else 0
            existing_order = c.execute("SELECT id,raw_text FROM orders WHERE id=?", (order_id,)).fetchone() if order_id else None
            if not existing_order and sequence_no in legacy_by_sequence:
                existing_order = legacy_by_sequence[sequence_no]
                order_id = int(existing_order['id'])

            previous_normalized = normalize_chain_import_line(existing_order['raw_text']) if existing_order else ''
            if existing_order and previous_normalized == normalized:
                unchanged += 1
            elif existing_order:
                assert_no_duplicate_customer_name_for_chain(c, order_data['customer_raw'], current_order_id=order_id)
                order_data['id'] = order_id
                create_or_update_order(c, order_data)
                updated += 1
            else:
                assert_no_duplicate_customer_name_for_chain(c, order_data['customer_raw'])
                create_or_update_order(c, order_data)
                order_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
                inserted += 1

            c.execute("""INSERT INTO chain_import_rows(
                            order_date,girl_id,sequence_no,order_id,normalized_text,source_chat_id,source_message_id,updated_at)
                         VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(order_date,girl_id,sequence_no) DO UPDATE SET
                            order_id=excluded.order_id,normalized_text=excluded.normalized_text,
                            source_chat_id=excluded.source_chat_id,source_message_id=excluded.source_message_id,
                            updated_at=CURRENT_TIMESTAMP""",
                      (od, g['id'], sequence_no, order_id, normalized,
                       str(source_chat_id or ''), str(source_message_id or '')))
            count += 1
    return {'count': count, 'inserted': inserted, 'updated': updated, 'unchanged': unchanged,
            'girl_name': g['name'], 'order_date': od}

@app.route('/api/import_chain',methods=['POST'])
def import_chain():
    try:
        d = request.json or {}
        result = import_chain_text(d.get('text',''), d.get('order_date') or '', d.get('girl_id'),
                                   d.get('settlement_status','未结算'))
        return jsonify(ok=True, **result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500


def _basic_12h_minute_label(m):
    h = (int(m) // 60) % 24
    mi = int(m) % 60
    dh = h % 12 or 12
    return f"{dh}:{mi:02d}" if mi else str(dh)

def format_chain_basic_service_time(service_time):
    raw = str(service_time or '').strip()
    if not raw:
        return raw
    interval = _parse_interval_text(raw)
    if not interval:
        return raw
    start, end = interval
    return f"{_basic_12h_minute_label(start)}-{_basic_12h_minute_label(end)}"

def order_to_chain_line(o, idx, full=True):
    service_time = str(o['service_time'] or '').strip()
    price = int(o['received_amount'] or 0)
    remark = str(o['remark'] or '').strip()
    if full:
        # 完整版：时间/价格/客户ID/客户名/备注
        parts = [service_time, str(price), str(o['customer_no'] or '').strip(), str(o['customer_name'] or '').strip()]
        if remark:
            parts.append(remark)
    else:
        # 普通版：只给女孩/群里看的时间、价格、备注，不暴露客户ID和名字
        parts = [format_chain_basic_service_time(service_time), str(price)]
        if remark:
            parts.append(remark)
    return f"{idx}.{ '/'.join([p for p in parts if p != '']) }"



def _clock_to_minutes(v, default_end=False):
    """夜场时间转分钟：19:00=1140，0:30=1470，包夜=1740。"""
    raw = str(v or '').strip()
    if not raw:
        return None
    if '包夜' in raw:
        return 29 * 60
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", raw)
    if not m:
        return None
    h = int(m.group(1)); mi = int(m.group(2) or 0)
    # 纯出勤表常用 19:00-23:00；接龙常用 7.30-9.30 表示晚上。
    if h <= 5:
        h += 24
    elif h < 12:
        h += 12
    return h * 60 + mi

def _parse_interval_text(text):
    t = str(text or '').strip()
    if not t:
        return None
    if '包夜' in t:
        m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", t)
        if not m:
            return None
        return (_clock_to_minutes(m.group(0)), 29 * 60)
    m = re.search(r"(\d{1,2}(?:[:.]\d{1,2})?)\s*(?:[-~ー～]|到|至)\s*(\d{1,2}(?:[:.]\d{1,2})?)", t)
    if not m:
        return None
    a, b = _clock_to_minutes(m.group(1)), _clock_to_minutes(m.group(2))
    if a is None or b is None:
        return None
    if b <= a:
        b += 24 * 60
    return (a, b)

def _fmt_free_minute(m, is_end=False):
    if is_end and m >= 24 * 60:
        return '包夜'
    h = (m // 60) % 24
    mi = m % 60
    # 晚上 19-23 按用户习惯显示 7-11.30。
    dh = h - 12 if 13 <= h <= 23 else h
    return f"{dh}.{mi:02d}" if mi else str(dh)

def _subtract_intervals(base, busy):
    free = [base]
    for bs, be in sorted(busy):
        nxt = []
        for fs, fe in free:
            if be <= fs or bs >= fe:
                nxt.append((fs, fe)); continue
            if bs > fs:
                nxt.append((fs, min(bs, fe)))
            if be < fe:
                nxt.append((max(be, fs), fe))
        free = [(a,b) for a,b in nxt if b-a >= 1]
    return free

def _is_package_time(v):
    s = str(v or '')
    return '包夜' in s or '鍖呭' in s

def _clock_parts(v):
    raw = str(v or '').strip()
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", raw)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if mi >= 60:
        mi = 59
    return h, mi

def _strict_24h_clock_parts(v):
    raw = str(v or '').strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    if mi >= 60:
        mi = 59
    return h, mi

def _strict_24h_interval_values(start_value, end_value):
    sp = _strict_24h_clock_parts(start_value)
    ep = _strict_24h_clock_parts(end_value)
    if not sp or not ep:
        return None
    sh, sm = sp
    eh, em = ep
    start = _business_clock_minutes_24h(sh, sm)
    end = _business_clock_minutes_24h(eh, em)
    if end <= start:
        end += 24 * 60
    return start, end

def _interval_minutes(start_value, end_value):
    strict = _strict_24h_interval_values(start_value, end_value)
    if strict:
        return strict
    sp = _clock_parts(start_value)
    ep = _clock_parts(end_value)
    if not sp or not ep:
        return None
    sh, sm = sp
    eh, em = ep

    def start_minute():
        if sh >= 24:
            return sh * 60 + sm
        if sh >= 12:
            return sh * 60 + sm
        if sh == 0:
            return 24 * 60 + sm
        if sh <= 3 and eh <= 5:
            return (24 + sh) * 60 + sm
        return (12 + sh) * 60 + sm

    start = start_minute()
    if eh >= 24:
        end = eh * 60 + em
    elif eh >= 12:
        end = eh * 60 + em
    elif eh == 0:
        end = 24 * 60 + em
    elif eh <= 5 and (sh >= 6 or sh >= 12 or sh <= 3):
        end = (24 + eh) * 60 + em
    else:
        end = (12 + eh) * 60 + em
    if end <= start:
        end += 24 * 60
    return start, end

def _clock_to_minutes(v, default_end=False):
    raw = str(v or '').strip()
    if not raw:
        return None
    if _is_package_time(raw):
        return 29 * 60 if default_end else 24 * 60
    parts = _clock_parts(raw)
    if not parts:
        return None
    h, mi = parts
    if h == 0:
        h = 24
    elif h <= 3:
        h += 24
    elif h < 12:
        h += 12
    return h * 60 + mi

def _parse_interval_text(text):
    t = str(text or '').strip()
    if not t:
        return None
    if _is_package_time(t):
        return (24 * 60, 29 * 60)
    m = re.search(r"(\d{1,2}(?:[:.]\d{1,2})?)\s*(?:[-~ー～]|到|至)\s*(\d{1,2}(?:[:.]\d{1,2})?)", t)
    if not m:
        return None
    return _interval_minutes(m.group(1), m.group(2))

def _parse_interval_text_for_shift(text, shift_intervals=None):
    raw = str(text or '').strip()
    if not raw or _is_package_time(raw):
        return _parse_interval_text(raw)
    return _parse_interval_text(normalize_chain_time_token(raw, shift_intervals))

def normalize_chain_order_times_for_date(c, date_str, girl_name=''):
    if not date_str:
        return 0
    params = [date_str]
    where = "order_date=?"
    if girl_name:
        where += " AND girl_name=?"
        params.append(girl_name)
    changed = 0
    for o in c.execute(f"SELECT id,service_time,girl_name FROM orders WHERE {where}", params).fetchall():
        raw = str(o['service_time'] or '').strip()
        if not raw or _is_package_time(raw):
            continue
        normalized = normalize_chain_time_token(raw, _shift_intervals_for_girl(c, date_str, o['girl_name']))
        if normalized and normalized != raw and _parse_interval_text(normalized):
            c.execute("""UPDATE orders
                         SET service_time=?, hours=?, updated_at=CURRENT_TIMESTAMP
                         WHERE id=?""", (normalized, calc_hours(normalized), o['id']))
            changed += 1
    return changed

def _fmt_free_minute(m, is_end=False):
    h = (m // 60) % 24
    mi = m % 60
    return f"{h:02d}:{mi:02d}"

def _round_up_to_slot(m, step=30):
    return ((int(m) + step - 1) // step) * step

def _tokyo_now():
    if ZoneInfo:
        return datetime.now(ZoneInfo('Asia/Tokyo')).replace(tzinfo=None)
    jst = timezone(timedelta(hours=9))
    return datetime.now(timezone.utc).astimezone(jst).replace(tzinfo=None)

def _to_tokyo_naive(dt):
    if dt.tzinfo is None:
        return dt
    if ZoneInfo:
        return dt.astimezone(ZoneInfo('Asia/Tokyo')).replace(tzinfo=None)
    jst = timezone(timedelta(hours=9))
    return dt.astimezone(jst).replace(tzinfo=None)

def _parse_client_now(value):
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return _to_tokyo_naive(datetime.fromisoformat(raw.replace('Z', '+00:00')))
    except Exception:
        return None

def _request_client_now(payload=None):
    if payload is None:
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
    else:
        payload = payload or {}
    return _parse_client_now(
        request.headers.get('X-Client-Now')
        or payload.get('client_now')
        or payload.get('clientNow')
        or payload.get('now')
    )

def _parse_chain_date(value, now=None):
    raw = str(value or '').strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            pass
    compact = re.sub(r'\D', '', raw)
    if len(compact) == 4:
        base = now or _tokyo_now()
        try:
            return date(base.year, int(compact[:2]), int(compact[2:]))
        except Exception:
            return None
    return None

def _current_business_minute_for_date(date_str, now=None):
    now = now or _tokyo_now()
    target = _parse_chain_date(date_str, now)
    if not target:
        return None
    today = now.date()
    if target > today:
        return None
    current = _round_up_to_slot(now.hour * 60 + now.minute)
    if target == today:
        return current if now.hour >= 6 else None
    if target == today - timedelta(days=1) and now.hour < 12:
        return _round_up_to_slot((24 + now.hour) * 60 + now.minute)
    return 48 * 60

def _normalize_today_free_base_for_cutoff(base, cutoff):
    if cutoff is None or cutoff >= 24 * 60:
        return base
    a, b = base
    da, db = a % (24 * 60), b % (24 * 60)
    if a >= 24 * 60 and b >= 24 * 60 and da < cutoff < db:
        return (da, db)
    return base

def build_chain_free_rows(c, date_str):
    """出勤时间减去当天接龙预约时间，返回全部女孩空闲文本。"""
    result = []
    try:
        now = _request_client_now()
    except Exception:
        now = None
    cutoff = _current_business_minute_for_date(date_str, now)
    for sft in pure_shift_rows_for_date(c, date_str):
        girl = sft.get('girl') or ''
        if _is_package_time(sft.get('start')) or _is_package_time(sft.get('end')):
            base = (24 * 60, 29 * 60)
        else:
            base = _interval_minutes(sft.get('start'), sft.get('end'))
        if not base or base[0] is None or base[1] is None:
            continue
        base = _normalize_today_free_base_for_cutoff(base, cutoff)
        if cutoff is not None:
            base = (max(base[0], cutoff), base[1])
            if base[0] >= base[1]:
                continue
        shift_intervals = [base]
        busy = []
        for o in c.execute("""SELECT service_time FROM orders
                            WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'""", (date_str, girl)).fetchall():
            itv = _parse_interval_text_for_shift(o['service_time'], shift_intervals)
            if itv:
                busy.append(itv)
        free = _subtract_intervals(base, busy)
        if cutoff is not None:
            free = [(max(a, cutoff), b) for a, b in free if max(a, cutoff) < b]
        if not free:
            result.append({'girl': girl, 'segments': '满', 'text': f"{girl}满", 'full': True})
            continue
        segments = ''.join([f"{_fmt_free_minute(a)}-{_fmt_free_minute(b, True)}空" for a,b in free])
        result.append({'girl': girl, 'segments': segments, 'text': f"{girl}{segments}"})
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        header = f"{dt.month:02d}{dt.day:02d}出勤"
    except Exception:
        header = f"{date_str}出勤"
    return {'header': header, 'lines': result, 'text': header + ('\n' + '\n'.join(x['text'] for x in result) if result else '') + '\n\nhttps://ailisi99.com/'}

@app.route('/api/chain_page', methods=['POST'])
def api_chain_page():
    init_db()
    d = request.json or {}
    date_str = d.get('date') or str(date.today())
    girl_name = str(d.get('girl_name') or '').strip()
    client_now = _request_client_now(d)
    with conn() as c:
        normalize_chain_order_times_for_date(c, date_str)
        auto_finish_reservations(c)
        shifts = pure_shift_rows_for_date(c, date_str)
        # 给每个纯出勤女孩补上女孩表价格/ID，接龙预约用这个自动定价。
        out_shifts = []
        seen_shift_girls = set()
        for sft in shifts:
            shift_girl = str(sft.get('girl') or '').strip()
            if shift_girl:
                seen_shift_girls.add(shift_girl)
            g = c.execute('SELECT * FROM girls WHERE name=?', (shift_girl,)).fetchone()
            row = dict(sft)
            row['girl_id'] = g['id'] if g else 0
            row['price'] = int((g['list_price'] if g else 15000) or 15000)
            row['girl_alias'] = (g['girl_alias'] if g else '') or ''
            row['take_home_per_hour'] = int((g['take_home_per_hour'] if g else 10000) or 10000)
            out_shifts.append(row)
        order_girls = rows(c.execute("""SELECT girl_name, MIN(service_time) AS service_time, COUNT(*) AS order_count
                                         FROM orders
                                         WHERE order_date=? AND COALESCE(girl_name,'')<>''
                                         GROUP BY girl_name
                                         ORDER BY MIN(id) ASC""", (date_str,)).fetchall())
        for og in order_girls:
            order_girl = str(og.get('girl_name') or '').strip()
            if not order_girl or order_girl in seen_shift_girls:
                continue
            g = c.execute('SELECT * FROM girls WHERE name=?', (order_girl,)).fetchone()
            interval = _parse_interval_text(og.get('service_time') or '')
            start_label = _fmt_free_minute(interval[0]) if interval else '订单'
            end_label = _fmt_free_minute(interval[1], True) if interval else '订单'
            out_shifts.append({
                'id': f"orders_{order_girl}",
                'raw_id': 0,
                'date': date_str,
                'girl': order_girl,
                'start': start_label,
                'end': end_label,
                'tags': '订单',
                'goldTags': '订单',
                'source': 'orders',
                'sort_order': 20000,
                'order_count': int(og.get('order_count') or 0),
                'girl_id': g['id'] if g else 0,
                'price': int((g['list_price'] if g else 15000) or 15000),
                'girl_alias': (g['girl_alias'] if g else '') or '',
                'take_home_per_hour': int((g['take_home_per_hour'] if g else 10000) or 10000),
            })
            seen_shift_girls.add(order_girl)
        if not girl_name and out_shifts:
            girl_name = out_shifts[0]['girl']
        orders = rows(c.execute("""SELECT o.*, COALESCE(c.remark,'') AS customer_remark
            FROM orders o
            LEFT JOIN customers c ON c.id=o.customer_id
            WHERE o.order_date=? AND o.girl_name=?
            ORDER BY o.service_time ASC, o.id ASC""", (date_str, girl_name)).fetchall()) if girl_name else []
        free = build_chain_free_rows(c, date_str)
        return jsonify(ok=True, date=date_str, girl_name=girl_name, shifts=out_shifts, orders=orders, free=free)

@app.route('/api/chain_order', methods=['POST'])
def api_chain_order():
    init_db()
    d = request.json or {}
    date_str = d.get('order_date') or str(date.today())
    girl_id = d.get('girl_id')
    girl_name = d.get('girl_name') or ''
    with conn() as c:
        g = None
        if girl_id:
            g = c.execute('SELECT * FROM girls WHERE id=?', (int(girl_id),)).fetchone()
        if not g:
            g = ensure_girl(c, girl_name)
        if not g:
            return jsonify(ok=False, error='缺少女孩'), 400
        service_time = normalize_chain_time_token(
            d.get('service_time') or '',
            _shift_intervals_for_girl(c, date_str, g['name'])
        )
        base_price = int((g['list_price'] or 15000) or 15000)
        raw_amount = d.get('received_amount')
        amount = int(raw_amount) if str(raw_amount or '').strip() else int(round(base_price * calc_hours(service_time)))
        assert_no_duplicate_customer_name_for_chain(c, d.get('customer_raw') or '', d.get('id') or None)
        payload = {
            'id': d.get('id') or None,
            'order_date': date_str,
            'service_time': service_time,
            'girl_id': g['id'],
            'received_amount': amount,
            'customer_raw': d.get('customer_raw') or '',
            'remark': d.get('remark') or '',
            'order_status': d.get('order_status') or '预约中',
            'settlement_status': d.get('settlement_status') or '未结算',
            'payment_method': d.get('payment_method') or '现金',
            'raw_text': d.get('raw_text') or ''
        }
        if 'girl_take_home' in d and str(d.get('girl_take_home') or '').strip() != '':
            payload['girl_take_home'] = int(d.get('girl_take_home') or 0)
        create_or_update_order(c, payload)
        auto_finish_reservations(c)
    return jsonify(ok=True)

@app.route('/api/orders/status', methods=['POST'])
def api_order_status():
    init_db()
    d = request.json or {}
    oid = int(d.get('id') or 0)
    status = str(d.get('order_status') or '').strip()
    if not oid or not status:
        return jsonify(ok=False, error='缺少订单ID或状态'), 400
    with conn() as c:
        c.execute("UPDATE orders SET order_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, oid))
    return jsonify(ok=True)

@app.route('/api/chain_export', methods=['POST'])
def api_chain_export():
    init_db()
    d = request.json or {}
    date_str = d.get('date') or str(date.today())
    girl_name = str(d.get('girl_name') or '').strip()
    client_now = _request_client_now(d)
    with conn() as c:
        normalize_chain_order_times_for_date(c, date_str, girl_name)
        auto_finish_reservations(c)
        orders = c.execute("""SELECT * FROM orders
            WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'
            ORDER BY service_time ASC, id ASC""", (date_str, girl_name)).fetchall()
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            header = f"{dt.month:02d}{dt.day:02d}{girl_name}"
            display_header = f"{dt.month}月{dt.day}日 {girl_name} 接龙"
        except Exception:
            header = f"{date_str} {girl_name}"
            display_header = f"{date_str} {girl_name} 接龙"
        full_lines = [header] + [order_to_chain_line(o, i, True) for i, o in enumerate(orders, start=1)]
        basic_lines = [display_header] + [order_to_chain_line(o, i, False) for i, o in enumerate(orders, start=1)]
        free = build_chain_free_rows(c, date_str)
        return jsonify(ok=True, full='\n'.join(full_lines), basic='\n'.join(basic_lines), count=len(orders), free=free)


@app.route("/api/db_info")
def api_db_info():
    init_db()
    with conn() as c:
        return jsonify({
            "db_path": str(DB_PATH),
            "customers_count": c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "girls_count": c.execute("SELECT COUNT(*) FROM girls").fetchone()[0],
            "orders_count": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "version": APP_VERSION,
            "port": 5057,
        })

@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp



@app.route("/api/health")
def api_health():
    init_db()
    with conn() as c:
        return jsonify({
            "ok": True,
            "version": APP_VERSION,
            "port": 5057,
            "db_path": str(DB_PATH),
            "customers_count": c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "girls_count": c.execute("SELECT COUNT(*) FROM girls").fetchone()[0],
            "orders_count": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        })


@app.route("/api/enums", methods=["POST"])
def api_enums():
    d = request.json or {}
    with conn() as c:
        if d.get("delete_id"):
            c.execute("DELETE FROM enum_values WHERE id=?", (d["delete_id"],))
        elif d.get("id"):
            exists = c.execute("SELECT id FROM enum_values WHERE enum_type=? AND value=? AND id<>?", (d.get("enum_type"), d.get("value"), d.get("id"))).fetchone()
            if exists:
                return jsonify({"ok": False, "error": "这个枚举值已经存在，不能重复"}), 400
            c.execute("UPDATE enum_values SET enum_type=?, value=?, sort_order=? WHERE id=?", (d.get("enum_type"), d.get("value"), int(d.get("sort_order") or 999), d.get("id")))
        elif d.get("enum_type") and d.get("value"):
            c.execute("INSERT OR IGNORE INTO enum_values(enum_type,value,sort_order) VALUES(?,?,?)", (d["enum_type"], d["value"], int(d.get("sort_order") or 999)))
    return jsonify({"ok": True})


@app.route("/api/schedules", methods=["POST"])
def api_schedules():
    d = request.json or {}
    with conn() as c:
        girl_id = int(d.get("girl_id") or 0)
        girl = c.execute("SELECT * FROM girls WHERE id=?", (girl_id,)).fetchone()
        if not girl:
            return jsonify({"ok": False, "error": "请选择女孩"}), 400
        if d.get("id"):
            c.execute("""UPDATE girl_schedules SET schedule_date=?, girl_id=?, girl_name=?, start_time=?, end_time=?, price=?, status=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (d.get("schedule_date"), girl["id"], girl["name"], d.get("start_time",""), d.get("end_time",""), int(d.get("price") or 0), d.get("status","出勤"), d.get("note",""), int(d.get("id"))))
        else:
            c.execute("""INSERT INTO girl_schedules(schedule_date,girl_id,girl_name,start_time,end_time,price,status,note) VALUES(?,?,?,?,?,?,?,?)""",
                      (d.get("schedule_date"), girl["id"], girl["name"], d.get("start_time",""), d.get("end_time",""), int(d.get("price") or 0), d.get("status","出勤"), d.get("note","")))
    return jsonify({"ok": True})


@app.route('/api/orders/bulk_delete', methods=['POST'])
def api_orders_bulk_delete():
    d = request.json or {}
    ids = list(dict.fromkeys(int(x) for x in (d.get('ids') or []) if str(x).strip().isdigit() and int(x) > 0))
    if not ids:
        return jsonify(ok=False, error='没有选择订单'), 400
    with conn() as c:
        affected_customer_ids_for_bulk_delete = set()
        deleted = 0
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            q = ','.join(['?'] * len(batch))
            affected_customer_ids_for_bulk_delete.update(
                int(r['customer_id']) for r in c.execute(
                    f'SELECT DISTINCT customer_id FROM orders WHERE id IN ({q})', batch
                ).fetchall() if r['customer_id']
            )
            c.execute(f'DELETE FROM chain_import_rows WHERE order_id IN ({q})', batch)
            c.execute(f'UPDATE customer_reservations SET order_id=0, updated_at=CURRENT_TIMESTAMP WHERE order_id IN ({q})', batch)
            deleted += int(c.execute(f'DELETE FROM orders WHERE id IN ({q})', batch).rowcount or 0)
        for cid in affected_customer_ids_for_bulk_delete:
            refresh_customer_totals(c, cid, update_types=False)
        changed_customers = customer_refresh_rows(c, affected_customer_ids_for_bulk_delete)
    return jsonify(ok=True, deleted=deleted, customers=changed_customers)

@app.route('/api/orders/bulk_settle', methods=['POST'])
def api_orders_bulk_settle():
    d = request.json or {}
    ids = []
    seen_ids = set()
    for x in (d.get('ids') or []):
        try:
            oid = int(x)
        except Exception:
            continue
        if oid > 0 and oid not in seen_ids:
            seen_ids.add(oid)
            ids.append(oid)
    status = d.get('settlement_status') or '已结算'
    if not ids:
        return jsonify(ok=False, error='没有选择订单'), 400
    q = ','.join(['?'] * len(ids))
    with conn() as c:
        before = rows(c.execute(f"SELECT id,order_date,settlement_status FROM orders WHERE id IN ({q})", ids).fetchall())
        existing_ids = [int(r['id']) for r in before]
        if existing_ids:
            q2 = ','.join(['?'] * len(existing_ids))
            cur = c.execute(f"UPDATE orders SET settlement_status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN ({q2})", [status] + existing_ids)
            if status == '已结算':
                dates = sorted(set(str(r['order_date'] or '') for r in before if r.get('order_date')))
                selected = set(existing_ids)
                for report_date in dates:
                    for r in rows(c.execute("SELECT id,order_ids FROM settlement_reports WHERE report_date=?", (report_date,)).fetchall()):
                        report_ids = set()
                        for part in str(r.get('order_ids') or '').split(','):
                            try:
                                report_ids.add(int(part))
                            except Exception:
                                pass
                        if report_ids & selected:
                            c.execute("DELETE FROM settlement_reports WHERE id=?", (r['id'],))
            updated = cur.rowcount if cur.rowcount is not None else len(existing_ids)
        else:
            updated = 0
    return jsonify(ok=True, updated=updated, updated_ids=existing_ids, settlement_status=status)





def settlement_source_rows(c, report_date):
    rows = c.execute("""SELECT * FROM orders
                         WHERE order_date=? AND COALESCE(settlement_status,'')<>'已结算'
                         ORDER BY girl_name, id""", (report_date,)).fetchall()
    grouped = {}
    for o in rows:
        girl = o['girl_name'] or '未填写女孩'
        grouped.setdefault(girl, {'girl_name': girl, 'theoretical_amount': 0, 'non_cash': 0, 'order_ids': []})
        amount = int(o['store_profit'] or 0)
        paid = int(o['received_amount'] or 0)
        grouped[girl]['theoretical_amount'] += amount
        if str(o['payment_method'] or '现金') != '现金':
            grouped[girl]['non_cash'] += paid
        grouped[girl]['order_ids'].append(str(o['id']))
    return list(grouped.values())

def settlement_formula_text(theoretical, non_cash):
    return f"{int(theoretical or 0)} - {int(non_cash or 0)} = {int(theoretical or 0) - int(non_cash or 0)}"

def saved_settlement_map(c, report_date):
    return {r['girl_name']: dict(r) for r in c.execute('SELECT * FROM settlement_reports WHERE report_date=?', (report_date,)).fetchall()}

@app.route('/api/settlements', methods=['GET'])
def api_settlements_get():
    init_db()
    report_date = request.args.get('date') or ''
    with conn() as c:
        if report_date:
            reports = rows(c.execute('SELECT * FROM settlement_reports WHERE report_date=? ORDER BY girl_name', (report_date,)).fetchall())
        else:
            reports = rows(c.execute('SELECT * FROM settlement_reports ORDER BY report_date DESC, girl_name').fetchall())
    return jsonify(ok=True, settlements=reports, boss_email=BOSS_EMAIL)

@app.route('/api/settlements/save', methods=['POST'])
def api_settlements_save():
    init_db()
    d = request.json or {}
    report_date = str(d.get('date') or date.today()).strip()
    items = d.get('items') or []
    if not report_date:
        return jsonify(ok=False, error='缺少结算日期'), 400
    with conn() as c:
        saved = 0
        for item in items:
            girl_name = str(item.get('girl_name') or '').strip()
            if not girl_name:
                continue
            g = c.execute('SELECT email FROM girls WHERE name=?', (girl_name,)).fetchone()
            girl_email = str(item.get('girl_email') or (g['email'] if g and 'email' in g.keys() else '') or '').strip()
            theoretical = int(item.get('theoretical_amount') or 0)
            actual = int(item.get('actual_settlement') if item.get('actual_settlement') is not None else theoretical)
            formula = str(item.get('formula_text') or '').strip()
            order_ids = ','.join(str(x) for x in (item.get('order_ids') or []))
            signed_order_ids = ','.join(str(x) for x in (item.get('signed_order_ids') or []))
            c.execute("""INSERT INTO settlement_reports(report_date,girl_name,theoretical_amount,actual_settlement,formula_text,order_ids,signed_order_ids,boss_email,girl_email,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(report_date,girl_name) DO UPDATE SET
                           theoretical_amount=excluded.theoretical_amount,
                           actual_settlement=excluded.actual_settlement,
                           formula_text=excluded.formula_text,
                           order_ids=excluded.order_ids,
                           signed_order_ids=excluded.signed_order_ids,
                           boss_email=excluded.boss_email,
                           girl_email=excluded.girl_email,
                           updated_at=CURRENT_TIMESTAMP""",
                      (report_date, girl_name, theoretical, actual, formula, order_ids, signed_order_ids, BOSS_EMAIL, girl_email))
            saved += 1
    return jsonify(ok=True, saved=saved)

def parse_id_csv(value):
    ids = set()
    for part in str(value or '').split(','):
        try:
            v = int(part)
        except Exception:
            continue
        if v > 0:
            ids.add(v)
    return ids

def settlement_formula_for_group(theoretical, non_cash):
    return settlement_formula_text(theoretical, non_cash)

def is_cash_payment_method(value):
    text = str(value or '').strip().lower()
    if not text:
        return True
    non_cash_keys = (
        'paypay', 'pay pay', 'wechat', 'weixin', 'alipay', 'linepay', 'line pay',
        'rakuten', 'rmb', '\u5fae\u4fe1', '\u652f\u4ed8\u5b9d', '\u4eba\u6c11\u5e01'
    )
    return not any(k in text for k in non_cash_keys)

@app.route('/api/settlements/sign', methods=['POST'])
def api_settlements_sign():
    init_db()
    d = request.json or {}
    checked = bool(d.get('checked'))
    ids = []
    for x in d.get('ids') or []:
        try:
            v = int(x)
        except Exception:
            continue
        if v > 0 and v not in ids:
            ids.append(v)
    if not ids:
        return jsonify(ok=True, saved=0)
    with conn() as c:
        q = ','.join('?' for _ in ids)
        order_rows = rows(c.execute(f"SELECT id,order_date,girl_name,store_profit,received_amount,payment_method FROM orders WHERE id IN ({q})", ids).fetchall())
        grouped = {}
        for o in order_rows:
            day = str(o.get('order_date') or '').strip()
            girl = str(o.get('girl_name') or '').strip() or '未填写女孩'
            if not day or not girl:
                continue
            key = (day, girl)
            g = grouped.setdefault(key, {'ids': [], 'theoretical': 0, 'non_cash': 0})
            g['ids'].append(int(o['id']))
            g['theoretical'] += int(o.get('store_profit') or 0)
            if not is_cash_payment_method(o.get('payment_method')):
                g['non_cash'] += int(o.get('received_amount') or 0)
        saved = 0
        for (day, girl), g in grouped.items():
            old = c.execute("SELECT * FROM settlement_reports WHERE report_date=? AND girl_name=?", (day, girl)).fetchone()
            signed = parse_id_csv(old['signed_order_ids'] if old and 'signed_order_ids' in old.keys() else '')
            target = set(g['ids'])
            if checked:
                signed.update(target)
            else:
                signed.difference_update(target)
            order_ids = str(old['order_ids']) if old and old['order_ids'] else ','.join(str(x) for x in g['ids'])
            signed_ids = ','.join(str(x) for x in sorted(signed))
            theoretical = int(old['theoretical_amount']) if old and old['theoretical_amount'] is not None else int(g['theoretical'])
            default_actual = int(g['theoretical']) - int(g['non_cash'])
            actual = int(old['actual_settlement']) if old and old['actual_settlement'] is not None else default_actual
            formula = str(old['formula_text']) if old and old['formula_text'] else settlement_formula_for_group(g['theoretical'], g['non_cash'])
            g_row = c.execute('SELECT email FROM girls WHERE name=?', (girl,)).fetchone()
            girl_email = (old['girl_email'] if old and 'girl_email' in old.keys() else '') or (g_row['email'] if g_row and 'email' in g_row.keys() else '') or ''
            c.execute("""INSERT INTO settlement_reports(report_date,girl_name,theoretical_amount,actual_settlement,formula_text,order_ids,signed_order_ids,boss_email,girl_email,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(report_date,girl_name) DO UPDATE SET
                           theoretical_amount=excluded.theoretical_amount,
                           actual_settlement=excluded.actual_settlement,
                           formula_text=excluded.formula_text,
                           order_ids=excluded.order_ids,
                           signed_order_ids=excluded.signed_order_ids,
                           boss_email=excluded.boss_email,
                           girl_email=excluded.girl_email,
                           updated_at=CURRENT_TIMESTAMP""",
                      (day, girl, theoretical, actual, formula, order_ids, signed_ids, BOSS_EMAIL, girl_email))
            saved += 1
    return jsonify(ok=True, saved=saved, checked=checked)

def send_plain_email(to_addrs, subject, body, display_name='Alice Management', smtp=None):
    to_addrs = [x for x in to_addrs if x]
    if not to_addrs:
        return {'sent': False, 'reason': 'no recipients'}
    smtp = smtp if isinstance(smtp, dict) else {}
    host = str(smtp.get('host') or os.environ.get('SMTP_HOST') or os.environ.get('ALICE_SMTP_HOST') or '').strip()
    user = str(smtp.get('user') or os.environ.get('SMTP_USER') or os.environ.get('ALICE_SMTP_USER') or '').strip()
    password = str(smtp.get('password') or os.environ.get('SMTP_PASSWORD') or os.environ.get('ALICE_SMTP_PASSWORD') or '')
    sender = str(smtp.get('from') or os.environ.get('SMTP_FROM') or os.environ.get('ALICE_SMTP_FROM') or user or '').strip()
    try:
        port = int(str(smtp.get('port') or os.environ.get('SMTP_PORT') or os.environ.get('ALICE_SMTP_PORT') or 587).strip())
    except Exception:
        port = 587
    if not host or not sender or not password:
        return {'sent': False, 'reason': 'SMTP not configured', 'to': to_addrs, 'subject': subject, 'body': body}
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr((display_name, sender))
    msg['To'] = ', '.join(to_addrs)
    try:
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(host, port, timeout=20) as mailer:
            if port != 465:
                mailer.starttls()
            if user:
                mailer.login(user, password)
            mailer.sendmail(sender, to_addrs, msg.as_string())
        return {'sent': True, 'to': to_addrs}
    except Exception as exc:
        return {'sent': False, 'reason': str(exc), 'to': to_addrs, 'subject': subject}

@app.route('/api/settlements/notify', methods=['POST'])
def api_settlements_notify():
    init_db()
    d = request.json or {}
    report_date = str(d.get('date') or date.today()).strip()
    smtp = d.get('smtp') if isinstance(d.get('smtp'), dict) else None
    with conn() as c:
        source = settlement_source_rows(c, report_date)
        saved = saved_settlement_map(c, report_date)
        for row in source:
            if row['girl_name'] not in saved:
                formula = settlement_formula_text(row['theoretical_amount'], row['non_cash'])
                actual = row['theoretical_amount'] - row['non_cash']
                g = c.execute('SELECT email FROM girls WHERE name=?', (row['girl_name'],)).fetchone()
                c.execute("""INSERT OR IGNORE INTO settlement_reports(report_date,girl_name,theoretical_amount,actual_settlement,formula_text,order_ids,boss_email,girl_email)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (report_date, row['girl_name'], row['theoretical_amount'], actual, formula, ','.join(row['order_ids']), BOSS_EMAIL, (g['email'] if g and 'email' in g.keys() else '') or ''))
        reports = rows(c.execute('SELECT * FROM settlement_reports WHERE report_date=? ORDER BY girl_name', (report_date,)).fetchall())
        if not reports:
            return jsonify(ok=False, error='当天没有可通报的结算记录'), 400
        boss_lines = [f"当日结算通报 {report_date}", ""]
        for r in reports:
            boss_lines.append(f"{r['girl_name']}：今日收益 {int(r['theoretical_amount'] or 0)}，实给 {int(r['actual_settlement'] or 0)}。公式：{r['formula_text'] or ''}")
        boss_result = send_plain_email([BOSS_EMAIL], f"当日结算通报 {report_date}", "\n".join(boss_lines), smtp=smtp)
        girl_results = []
        now = datetime.now().isoformat(timespec='seconds')
        if boss_result.get('sent'):
            c.execute('UPDATE settlement_reports SET sent_to_boss_at=? WHERE report_date=?', (now, report_date))
        for r in reports:
            girl_email = str(r.get('girl_email') or '').strip()
            if not girl_email:
                girl_results.append({'girl_name': r['girl_name'], 'sent': False, 'reason': 'no email'})
                continue
            body = f"{r['girl_name']}，今日家教费：理论 {int(r['theoretical_amount'] or 0)}，实给 {int(r['actual_settlement'] or 0)}。"
            result = send_plain_email([girl_email], f"家教费结算 {report_date}", body, '家教费结算', smtp)
            result['girl_name'] = r['girl_name']
            girl_results.append(result)
            if result.get('sent'):
                c.execute('UPDATE settlement_reports SET sent_to_girl_at=? WHERE report_date=? AND girl_name=?', (now, report_date, r['girl_name']))
    return jsonify(ok=True, boss=boss_result, girls=girl_results, reports=reports)

@app.route('/api/girls/email', methods=['POST'])
def api_girl_email_save():
    init_db()
    d = request.json or {}
    girl_id = int(d.get('id') or 0)
    email = str(d.get('email') or '').strip()
    if not girl_id:
        return jsonify(ok=False, error='缺少女孩ID'), 400
    with conn() as c:
        c.execute('UPDATE girls SET email=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (email, girl_id))
    return jsonify(ok=True)

@app.route('/api/quick_links', methods=['POST'])
def api_quick_links():
    d = request.json or {}
    with conn() as c:
        if d.get('delete_id'):
            c.execute('DELETE FROM quick_links WHERE id=?', (int(d['delete_id']),))
            return jsonify(ok=True)
        group_name = str(d.get('group_name') or '常用短语').strip()
        title = str(d.get('title') or '').strip()
        content = str(d.get('content') or '').strip()
        sort_order = int(d.get('sort_order') or 0)
        if not group_name or not title:
            return jsonify(ok=False, error='分组和名字不能为空'), 400
        if d.get('id'):
            c.execute('UPDATE quick_links SET group_name=?, title=?, content=?, sort_order=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (group_name, title, content, sort_order, int(d['id'])))
        else:
            c.execute('INSERT INTO quick_links(group_name,title,content,sort_order) VALUES(?,?,?,?)', (group_name, title, content, sort_order))
    return jsonify(ok=True)

@app.route('/api/customers/points-audit', methods=['GET'])
def api_customers_points_audit():
    init_db()
    with conn() as c:
        result = audit_customer_points(c)
    return jsonify(ok=True, **result)

@app.route('/api/review_crawler', methods=['GET', 'POST'])
def api_review_crawler():
    init_db()
    if request.method == 'GET':
        with conn() as c:
            recent = rows(c.execute("""SELECT id,source_url,source_page,girl_name,review_text,tags,review_hash,review_date,
                                      material_type,author_name,source_title,access_scope,created_at,updated_at
                                      FROM scraped_reviews ORDER BY updated_at DESC,id DESC LIMIT 100""").fetchall())
            total = int(c.execute('SELECT COUNT(*) FROM scraped_reviews').fetchone()[0])
            girl_names = [r[0] for r in c.execute("SELECT name FROM girls WHERE girl_status!='离职' ORDER BY name").fetchall()]
            author_rows = c.execute("""SELECT author_name,COUNT(*) sample_count,CAST(AVG(LENGTH(review_text)) AS INTEGER) avg_length
                                       FROM scraped_reviews WHERE COALESCE(author_name,'')!=''
                                       AND material_type IN ('公开短评','登录后长评','公开长评预览')
                                       GROUP BY author_name ORDER BY sample_count DESC,author_name LIMIT 200""").fetchall()
            unlocked_rows = c.execute("SELECT source_page,source_url,review_text FROM scraped_reviews WHERE material_type='登录后长评'").fetchall()
        unlocked_lengths = {}
        for row in unlocked_rows:
            key = _review_source_identity(row['source_page'] or row['source_url'])
            unlocked_lengths[key] = max(unlocked_lengths.get(key,0), len(str(row['review_text'] or '').strip()))
        for item in recent:
            source_key = _review_source_identity(item.get('source_page') or item.get('source_url'))
            own_length = len(str(item.get('review_text') or '').strip())
            full_length = own_length if item.get('material_type') == '登录后长评' else unlocked_lengths.get(source_key,0)
            item['content_length'] = own_length
            if item.get('material_type') in ('登录后长评','公开长评预览'):
                if full_length >= 350:
                    item['unlock_status'], item['status_color'], item['needs_unlock'] = '已完整归档', 'green', False
                elif full_length > 0:
                    item['unlock_status'], item['status_color'], item['needs_unlock'] = '疑似未复制完整', 'orange', True
                else:
                    item['unlock_status'], item['status_color'], item['needs_unlock'] = '只有预览・待解锁', 'red', True
            else:
                item['unlock_status'], item['status_color'], item['needs_unlock'] = '公开内容', 'blue', False
        return jsonify(ok=True,total=total,reviews=recent,girls=girl_names,authors=rows(author_rows),
                       ai_configured=bool(str(os.environ.get('OPENAI_API_KEY') or '').strip()),
                       ai_model=OPENAI_REVIEW_MODEL)
    d = request.json or {}
    action = str(d.get('action') or 'crawl')
    if action == 'drafts':
        day = str(d.get('date') or tokyo_today_date().isoformat())[:10]
        girl_name = str(d.get('girl_name') or '').strip()
        custom_description = str(d.get('custom_description') or '').strip()
        if custom_description and not girl_name:
            return jsonify(ok=False,error='填写女孩特点后，请先选择对应女孩'), 400
        return jsonify(ok=True,date=day,girl_name=girl_name,
                       drafts=review_marketing_drafts(day, girl_name, custom_description))
    if action == 'polish':
        return jsonify(ok=True,polished=polish_archived_customer_review(d.get('review_id')))
    if action == 'assist_real':
        return jsonify(ok=True,polished=assist_real_customer_review(
            d.get('girl_name'), d.get('original_text'), d.get('confirmed_details')))
    if action == 'assist_real_ai':
        return jsonify(ok=True,polished=ai_assist_real_customer_review(
            d.get('girl_name'), d.get('original_text'), d.get('confirmed_details'),
            d.get('author_name'), d.get('style_strength'), d.get('target_length')))
    if action == 'archive':
        review_text = re.sub(r'\s+\n', '\n', str(d.get('review_text') or '').strip())
        if len(review_text) < 20:
            return jsonify(ok=False,error='长评正文至少需要 20 个字'), 400
        source_page = _safe_public_url(d.get('source_url'))
        girl_name = str(d.get('girl_name') or '').strip()
        if not girl_name:
            return jsonify(ok=False,error='请选择对应女孩'), 400
        with conn() as c:
            if not c.execute('SELECT 1 FROM girls WHERE name=?', (girl_name,)).fetchone():
                return jsonify(ok=False,error='女孩表中没有这个女孩'), 400
        material_type = str(d.get('material_type') or '登录后长评').strip()
        if material_type not in ('登录后长评','公开短评','公开网页素材'):
            material_type = '登录后长评'
        digest = hashlib.sha256((source_page+'\n'+review_text).encode('utf-8')).hexdigest()
        item = {'source_url':source_page,'source_page':source_page,'source_title':str(d.get('source_title') or '').strip(),
                'girl_name':girl_name,'review_text':review_text,'tags':','.join(_review_tags(review_text)),
                'review_hash':digest,'review_date':str(d.get('review_date') or '').strip()[:30],
                'author_name':str(d.get('author_name') or '').strip()[:80],'material_type':material_type,
                'access_scope':'manual_authorized'}
        inserted, updated = _store_scraped_reviews([item])
        return jsonify(ok=True,inserted=inserted,updated=updated,girl_name=girl_name)
    result = crawl_public_reviews(d.get('url'), d.get('max_pages') or 3)
    return jsonify(ok=True, **result)


NO_ROOM_HOTEL = '无房间'
SINGLE_ROOM_NO = '-'

def _room_label(hotel_name, room_no):
    hotel = str(hotel_name or '').strip()
    room = str(room_no or '').strip()
    if hotel == NO_ROOM_HOTEL:
        return NO_ROOM_HOTEL
    if room == SINGLE_ROOM_NO:
        return hotel
    return f"{hotel}-{room}" if hotel and room else (hotel or room or NO_ROOM_HOTEL)

def _normalize_assignment_room(hotel_name='', room_no='', room_name='', girl_name='', idx=0):
    room_name = str(room_name or '').strip()
    hotel = str(hotel_name or '').strip()
    room = str(room_no or '').strip()
    no_terms = {'无', '无房间', '没有房间', 'なし', 'none', 'no'}
    if room_name:
        if room_name.lower() in no_terms:
            room_name = ''
        else:
            return room_name, SINGLE_ROOM_NO, False
    if not hotel or not room or hotel.lower() in no_terms or room.lower() in no_terms:
        key = re.sub(r'\s+', '', str(girl_name or '').strip())[:40] or f"未安排{int(idx or 0) + 1}"
        return NO_ROOM_HOTEL, key, True
    return hotel, room, False

def _room_shift_clock(value, fallback):
    raw = str(value or '').strip().lower()
    m = re.match(r'^(\d{1,2})(?:[:.](\d{1,2}))?\s*(am|pm)?$', raw)
    if not m:
        return fallback
    h = int(m.group(1)); minute = int(m.group(2) or 0)
    suffix = m.group(3)
    if suffix == 'pm' and h < 12:
        h += 12
    if suffix == 'am' and h == 12:
        h = 0
    minute = max(0, min(59, minute))
    return f"{h % 24:02d}:{minute:02d}"

def parse_room_shift_time(value):
    raw = str(value or '').strip()
    if not raw:
        return '00:00', '04:00'
    if _is_package_time(raw):
        return '00:00', '05:00'
    m = re.search(r'(\d{1,2}(?:[:.]\d{1,2})?\s*(?:am|pm)?)\s*(?:[-~ー～]|到|至)\s*(\d{1,2}(?:[:.]\d{1,2})?\s*(?:am|pm)?)', raw, re.I)
    if not m:
        return '00:00', '04:00'
    return _room_shift_clock(m.group(1), '00:00'), _room_shift_clock(m.group(2), '04:00')

def sync_room_assignment_to_schedule(c, assignment_date, girl_id, hotel_name, room_no, start_time=None, end_time=None):
    if not girl_id:
        return
    girl = c.execute("SELECT * FROM girls WHERE id=?", (int(girl_id),)).fetchone()
    if not girl:
        return
    start_time = start_time or '00:00'
    end_time = end_time or '04:00'
    note = f"房间安排自动生成：{_room_label(hotel_name, room_no)}"
    old = c.execute("SELECT id FROM girl_schedules WHERE schedule_date=? AND girl_id=? AND note=?", (assignment_date, int(girl_id), note)).fetchone()
    price = int(girl['list_price'] or 15000)
    if old:
        c.execute("""UPDATE girl_schedules SET girl_name=?, start_time=?, end_time=?, price=?, status='出勤', updated_at=CURRENT_TIMESTAMP WHERE id=?""", (girl['name'], start_time, end_time, price, old['id']))
    else:
        c.execute("""INSERT INTO girl_schedules(schedule_date,girl_id,girl_name,start_time,end_time,price,status,note) VALUES(?,?,?,?,?,?,?,?)""", (assignment_date, int(girl_id), girl['name'], start_time, end_time, price, '出勤', note))

@app.route('/api/hotel_rooms', methods=['POST'])
def api_hotel_rooms():
    init_db()
    d = request.json or {}
    hotel = str(d.get('hotel_name') or '').strip()
    room = str(d.get('room_no') or '').strip()
    if not hotel or not room:
        return jsonify(ok=False, error='酒店名和房间号不能为空'), 400
    with conn() as c:
        if d.get('id'):
            c.execute("""UPDATE hotel_rooms SET hotel_name=?, room_no=?, daily_cost=?, remark=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (hotel, room, int(d.get('daily_cost') or 0), d.get('remark',''), int(d['id'])))
        else:
            c.execute("""INSERT OR REPLACE INTO hotel_rooms(hotel_name,room_no,daily_cost,remark,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)""", (hotel, room, int(d.get('daily_cost') or 0), d.get('remark','')))
    return jsonify(ok=True)

@app.route('/api/room_assignments', methods=['POST'])
def api_room_assignments():
    init_db()
    d = request.json or {}
    assignment_date = d.get('assignment_date') or str(date.today())
    hotel = str(d.get('hotel_name') or '').strip()
    room = str(d.get('room_no') or '').strip()
    if not hotel or not room:
        return jsonify(ok=False, error='请选择或填写房间'), 400
    girl_id = int(d.get('girl_id') or 0)
    girl_name = ''
    with conn() as c:
        if girl_id:
            g = c.execute('SELECT * FROM girls WHERE id=?', (girl_id,)).fetchone()
            if not g:
                return jsonify(ok=False, error='女孩不存在'), 400
            girl_name = g['name']
        if not d.get('daily_cost'):
            r = c.execute('SELECT daily_cost FROM hotel_rooms WHERE hotel_name=? AND room_no=?', (hotel, room)).fetchone()
            cost = int((r['daily_cost'] if r else 0) or 0)
        else:
            cost = int(d.get('daily_cost') or 0)
        c.execute("""INSERT OR IGNORE INTO hotel_rooms(hotel_name,room_no,daily_cost) VALUES(?,?,?)""", (hotel, room, cost))
        if d.get('id'):
            c.execute("""UPDATE room_assignments SET assignment_date=?, hotel_name=?, room_no=?, girl_id=?, girl_name=?, daily_cost=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (assignment_date, hotel, room, girl_id, girl_name, cost, d.get('note',''), int(d['id'])))
        else:
            c.execute("""INSERT OR REPLACE INTO room_assignments(assignment_date,hotel_name,room_no,girl_id,girl_name,daily_cost,note,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", (assignment_date, hotel, room, girl_id, girl_name, cost, d.get('note','')))
        start_time, end_time = parse_room_shift_time(d.get('shift_time') or f"{d.get('start_time','')}-{d.get('end_time','')}")
        sync_room_assignment_to_schedule(c, assignment_date, girl_id, hotel, room, start_time, end_time)
    return jsonify(ok=True)

@app.route('/api/room_assignments/bulk', methods=['POST'])
def api_room_assignments_bulk():
    init_db()
    d = request.json or {}
    start = d.get('start_date') or str(date.today())
    end = d.get('end_date') or start
    rooms = d.get('rooms') or []
    girls = d.get('girl_ids') or []
    note = d.get('note','')
    try:
        ds = datetime.strptime(start, '%Y-%m-%d').date(); de = datetime.strptime(end, '%Y-%m-%d').date()
    except Exception:
        return jsonify(ok=False, error='日期格式错误'), 400
    if de < ds: ds, de = de, ds
    count = 0
    with conn() as c:
        day = ds
        while day <= de:
            for idx, rr in enumerate(rooms):
                gname = str(rr.get('girl_name') or '').strip()
                gid = int(rr.get('girl_id') or (girls[idx] if idx < len(girls) and girls[idx] else 0) or 0)
                if gid:
                    g = c.execute('SELECT * FROM girls WHERE id=?', (gid,)).fetchone()
                    gname = g['name'] if g else gname
                elif gname:
                    g = ensure_girl(c, gname)
                    gid = int(g['id']) if g else 0
                    gname = g['name'] if g else gname
                hotel, room, no_room = _normalize_assignment_room(rr.get('hotel_name'), rr.get('room_no'), rr.get('room_name'), gname, idx)
                r = None if no_room else c.execute('SELECT daily_cost FROM hotel_rooms WHERE hotel_name=? AND room_no=?', (hotel, room)).fetchone()
                cost = 0 if no_room else int(rr.get('daily_cost') or (r['daily_cost'] if r else 0) or 0)
                start_time, end_time = parse_room_shift_time(rr.get('shift_time') or f"{rr.get('start_time','')}-{rr.get('end_time','')}")
                line_note = str(note or '').strip()
                if gid:
                    time_note = f"出勤 {start_time}-{end_time}"
                    line_note = f"{line_note}；{time_note}" if line_note else time_note
                if not no_room:
                    c.execute("""INSERT OR IGNORE INTO hotel_rooms(hotel_name,room_no,daily_cost) VALUES(?,?,?)""", (hotel, room, cost))
                c.execute("""INSERT OR REPLACE INTO room_assignments(assignment_date,hotel_name,room_no,girl_id,girl_name,daily_cost,note,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", (str(day), hotel, room, gid, gname, cost, line_note))
                sync_room_assignment_to_schedule(c, str(day), gid, hotel, room, start_time, end_time)
                count += 1
            day += timedelta(days=1)
    return jsonify(ok=True, count=count)

@app.route('/api/delete_room_assignment/<int:item_id>', methods=['POST'])
def api_delete_room_assignment(item_id):
    with conn() as c:
        c.execute('DELETE FROM room_assignments WHERE id=?', (item_id,))
    return jsonify(ok=True)

@app.route('/api/delete_hotel_room/<int:item_id>', methods=['POST'])
def api_delete_hotel_room(item_id):
    with conn() as c:
        c.execute('DELETE FROM hotel_rooms WHERE id=?', (item_id,))
    return jsonify(ok=True)


def normalize_tag_text(text):
    """普通TAG统一为空格分隔；兼容旧分号输入。"""
    return " ".join(re.sub(r"[;；]+", " ", str(text or "")).split())

def normalize_gold_tag_text(text):
    return " ".join(re.sub(r"[;；]+", " ", str(text or "")).split())

def auto_sync_late_attendance_to_tel(c, shift_date, girl):
    """22:00 后已全面同步的当天，新出现的出勤女孩只补入 TEL 一次。"""
    now = _tokyo_now()
    day = str(shift_date or '')[:10]
    name = str(girl or '').strip()
    if not name or day != now.date().isoformat() or now.hour < 22:
        return False
    state = c.execute("SELECT late_auto_enabled FROM telegram_full_sync_days WHERE sync_date=?", (day,)).fetchone()
    if not state or int(state['late_auto_enabled'] or 0) != 1:
        return False
    if c.execute("SELECT 1 FROM telegram_daily_girls WHERE booking_date=? AND girl_name=?", (day, name)).fetchone():
        return False
    sort_order = int(c.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM telegram_daily_girls WHERE booking_date=?",
                               (day,)).fetchone()[0] or 0)
    c.execute("""INSERT OR IGNORE INTO telegram_daily_girls(booking_date,girl_name,sort_order,source,updated_at)
                 VALUES(?,?,?,'late_attendance_auto',CURRENT_TIMESTAMP)""", (day, name, sort_order))
    return bool(c.execute("SELECT changes()").fetchone()[0])

def pure_shift_rows_for_date(c, date_str):
    pure = []
    for r in c.execute("SELECT * FROM pure_shifts WHERE shift_date=? ORDER BY sort_order ASC,id ASC", (date_str,)).fetchall():
        pure.append({
            'id': f"pure_{r['id']}", 'raw_id': r['id'], 'date': r['shift_date'], 'girl': r['girl_name'],
            'start': r['start_time'], 'end': r['end_time'], 'tags': normalize_tag_text(r['tags']),
            'goldTags': normalize_gold_tag_text(r['gold_tags']), 'source': 'manual', 'sort_order': r['sort_order'] or 0
        })
    schedules = []
    for r in c.execute("SELECT * FROM girl_schedules WHERE schedule_date=? AND COALESCE(status,'出勤')='出勤' ORDER BY id ASC", (date_str,)).fetchall():
        mem = c.execute("SELECT tags,gold_tags FROM girl_tag_memory WHERE girl_name=?", (r['girl_name'],)).fetchone()
        g = c.execute("SELECT tags FROM girls WHERE name=?", (r['girl_name'],)).fetchone()
        tag_text = normalize_tag_text((mem['tags'] if mem else '') or (g['tags'] if g else ''))
        gold_text = normalize_gold_tag_text(mem['gold_tags'] if mem else '')
        if '房间安排自动生成' in str(r['note'] or '') and '房间' not in gold_text.split():
            gold_text = normalize_gold_tag_text((gold_text + ' 房间').strip())
        schedules.append({
            'id': f"schedule_{r['id']}", 'raw_id': r['id'], 'date': r['schedule_date'], 'girl': r['girl_name'],
            'start': r['start_time'] or '00:00', 'end': r['end_time'] or '04:00', 'tags': tag_text,
            'goldTags': gold_text, 'source': 'schedule', 'sort_order': 10000 + int(r['id'] or 0)
        })
    shifts = pure + schedules
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
        start20 = (target - timedelta(days=19)).isoformat()
        start2 = (target - timedelta(days=1)).isoformat()
        stats = {}
        for row in c.execute("""SELECT girl_name,
                    COUNT(*) AS orders_20d,
                    SUM(CASE WHEN order_date>=? THEN 1 ELSE 0 END) AS orders_2d
                FROM orders
                WHERE order_date BETWEEN ? AND ? AND COALESCE(order_status,'')!='取消'
                  AND COALESCE(girl_name,'')!=''
                GROUP BY girl_name""", (start2, start20, date_str)).fetchall():
            total = int(row['orders_20d'] or 0)
            recent = int(row['orders_2d'] or 0)
            prior = max(0, total - recent)
            # 20天稳定人气 + 近2天权重 + 超出前18天平均速度的爆发奖励。
            surge = max(0, recent * 9 - prior)
            stats[str(row['girl_name'] or '').strip()] = (total * 10 + recent * 30 + surge * 6,
                                                          total, recent, surge)
        for shift in shifts:
            score, total, recent, surge = stats.get(str(shift.get('girl') or '').strip(), (0, 0, 0, 0))
            shift.update(popularity_score=score, orders_20d=total, orders_2d=recent, surge_score=surge)
        shifts.sort(key=lambda row: (-int(row.get('popularity_score') or 0),
                                     int(row.get('sort_order') or 0), str(row.get('girl') or '')))
    except Exception:
        pass
    return shifts

def copy_yesterday_pure_if_empty(c, date_str):
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        return 0
    today_count = len(pure_shift_rows_for_date(c, date_str))
    if today_count > 0:
        return 0
    yday = str(d - timedelta(days=1))
    yrows = pure_shift_rows_for_date(c, yday)
    for idx, r in enumerate(yrows, start=1):
        c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags,sort_order,source,note)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (date_str, r['girl'], r['start'], r['end'], normalize_tag_text(r.get('tags','')), normalize_gold_tag_text(r.get('goldTags','')), idx, 'manual', '从昨日纯出勤自动复制'))
    return len(yrows)

@app.route('/api/pure_shifts', methods=['GET'])
def api_pure_shifts_get():
    init_db()
    date_str = request.args.get('date') or str(date.today())
    autocopy = str(request.args.get('autocopy') or '') in ('1','true','yes')
    with conn() as c:
        copied = copy_yesterday_pure_if_empty(c, date_str) if autocopy else 0
        shifts = pure_shift_rows_for_date(c, date_str)
        memory_rows = c.execute('SELECT * FROM girl_tag_memory').fetchall()
        tags = {r['girl_name']: normalize_tag_text(r['tags']) for r in memory_rows}
        gold_tags = {r['girl_name']: normalize_gold_tag_text(r['gold_tags']) for r in memory_rows}
        return jsonify(ok=True, shifts=shifts, girl_tags=tags, girl_gold_tags=gold_tags, copied=copied)

@app.route('/api/pure_shifts', methods=['POST'])
def api_pure_shifts_save():
    init_db()
    d = request.json or {}
    shift_date = d.get('date') or d.get('shift_date') or str(date.today())
    girl = str(d.get('girl') or d.get('girl_name') or '').strip()
    if not girl:
        return jsonify(ok=False, error='女孩名不能为空'), 400
    start = str(d.get('start') or d.get('start_time') or '19:00').strip()
    end = str(d.get('end') or d.get('end_time') or '23:00').strip()
    tags = normalize_tag_text(d.get('tags') if not isinstance(d.get('tags'), list) else ' '.join(d.get('tags')))
    gold_tags = normalize_gold_tag_text(d.get('goldTags') if not isinstance(d.get('goldTags'), list) else ' '.join(d.get('goldTags')))
    raw_id = str(d.get('id') or '')
    with conn() as c:
        c.execute("""INSERT INTO girl_tag_memory(girl_name,tags,gold_tags,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(girl_name) DO UPDATE SET tags=excluded.tags,gold_tags=excluded.gold_tags,
                     updated_at=CURRENT_TIMESTAMP""", (girl, tags, gold_tags))
        if raw_id.startswith('pure_'):
            sid = int(raw_id.split('_',1)[1])
            c.execute("""UPDATE pure_shifts SET shift_date=?,girl_name=?,start_time=?,end_time=?,tags=?,gold_tags=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (shift_date, girl, start, end, tags, gold_tags, sid))
            tel_auto_synced = auto_sync_late_attendance_to_tel(c, shift_date, girl)
            return jsonify(ok=True, id=f'pure_{sid}', tel_auto_synced=tel_auto_synced)
        if raw_id.startswith('schedule_'):
            sid = int(raw_id.split('_',1)[1])
            g = c.execute('SELECT id FROM girls WHERE name=?', (girl,)).fetchone()
            c.execute("""UPDATE girl_schedules SET schedule_date=?, girl_id=?, girl_name=?, start_time=?, end_time=?, price=?, status='出勤', updated_at=CURRENT_TIMESTAMP WHERE id=?""", (shift_date, int(g['id']) if g else 0, girl, start, end, 0, sid))
            tel_auto_synced = auto_sync_late_attendance_to_tel(c, shift_date, girl)
            return jsonify(ok=True, id=f'schedule_{sid}', tel_auto_synced=tel_auto_synced)
        max_sort = c.execute('SELECT COALESCE(MAX(sort_order),0) AS m FROM pure_shifts WHERE shift_date=?', (shift_date,)).fetchone()['m']
        cur = c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags,sort_order,source)
                           VALUES(?,?,?,?,?,?,?,?)""", (shift_date, girl, start, end, tags, gold_tags, int(max_sort or 0)+1, 'manual'))
        tel_auto_synced = auto_sync_late_attendance_to_tel(c, shift_date, girl)
        return jsonify(ok=True, id=f"pure_{cur.lastrowid}", tel_auto_synced=tel_auto_synced)

@app.route('/api/pure_shifts/delete', methods=['POST'])
def api_pure_shifts_delete():
    init_db()
    d = request.json or {}
    raw_id = str(d.get('id') or '')
    with conn() as c:
        if raw_id.startswith('pure_'):
            c.execute('DELETE FROM pure_shifts WHERE id=?', (int(raw_id.split('_',1)[1]),))
        elif raw_id.startswith('schedule_'):
            c.execute('DELETE FROM girl_schedules WHERE id=?', (int(raw_id.split('_',1)[1]),))
        else:
            return jsonify(ok=False, error='id错误'), 400
    return jsonify(ok=True)

@app.route('/api/pure_shifts/clear', methods=['POST'])
def api_pure_shifts_clear():
    init_db()
    d = request.json or {}
    date_str = d.get('date') or str(date.today())
    with conn() as c:
        c.execute('DELETE FROM pure_shifts WHERE shift_date=?', (date_str,))
        c.execute("DELETE FROM girl_schedules WHERE schedule_date=?", (date_str,))
    return jsonify(ok=True)


# ===== 客人提前预约 / 后台审核 =====
@app.route('/api/girl_avatars', methods=['GET'])
def api_girl_avatars():
    init_db()
    raw = request.args.get('names') or ''
    names = [x.strip() for x in raw.split(',') if x.strip()]
    with conn() as c:
        if names:
            placeholders = ','.join('?' for _ in names)
            data = rows(c.execute(f"""SELECT g.name AS girl_name, COALESCE(NULLIF(g.girl_alias,''),g.name) AS neko_name,
                g.avatar_url, g.avatar_source_url AS source_url, g.avatar_updated_at AS updated_at
                FROM girls g WHERE g.name IN ({placeholders}) AND COALESCE(g.avatar_url,'')!=''""", names).fetchall())
        else:
            data = rows(c.execute("""SELECT g.name AS girl_name, COALESCE(NULLIF(g.girl_alias,''),g.name) AS neko_name,
                g.avatar_url, g.avatar_source_url AS source_url, g.avatar_updated_at AS updated_at
                FROM girls g WHERE COALESCE(g.avatar_url,'')!='' ORDER BY g.avatar_updated_at DESC""").fetchall())
    return jsonify(ok=True, avatars={r['girl_name']:r for r in data}, rows=data)

@app.route('/api/alice_avatars/sync', methods=['POST'])
def api_alice_avatars_sync():
    init_db()
    d = request.json or {}
    names = [str(x or '').strip() for x in (d.get('names') or []) if str(x or '').strip()]
    date_str = d.get('date') or str(date.today())
    with conn() as c:
        if not names:
            names = []
            seen = set()
            for r in pure_shift_rows_for_date(c, date_str):
                n = str(r.get('girl') or '').strip()
                if n and n not in seen:
                    seen.add(n); names.append(n)
        if not names:
            names = [r['name'] for r in c.execute("SELECT name FROM girls ORDER BY id DESC").fetchall()]
        alias_map = {r['name']:(r['girl_alias'] if 'girl_alias' in r.keys() else '') for r in c.execute("SELECT name,girl_alias FROM girls").fetchall()}
    try:
        alice_items, source = fetch_alice_girls()
    except Exception as e:
        return jsonify(ok=False, error=str(e), results=[], missing=names), 502
    results, missing, errors = [], [], []
    for name in names:
        try:
            item = match_alice_girl(name, alias_map.get(name,''), alice_items)
            if not item:
                missing.append(name); continue
            alice_name = item.get('post_title') or item.get('name') or item.get('model_name') or ''
            img = absolute_alice_url(first_neko_image(item))
            if not img:
                missing.append(name); continue
            avatar_url = cache_avatar(name, alice_name, img, referer=item.get('referer') or item.get('link') or (ALICE_BASE_URL + '/'))
            results.append({'girl_name':name, 'alice_name':alice_name, 'avatar_url':avatar_url, 'source_url':img})
        except Exception as e:
            errors.append({'girl_name':name, 'error':str(e)})
    return jsonify(ok=True, source=source, source_count=len(alice_items), results=results,
                   missing=missing, errors=errors, source_errors=fetch_alice_girls.last_errors,
                   avatars={r['girl_name']:r for r in results})

@app.route('/api/neko_avatars/sync', methods=['POST'])
def api_neko_avatars_sync():
    init_db()
    d = request.json or {}
    names = [str(x or '').strip() for x in (d.get('names') or []) if str(x or '').strip()]
    date_str = d.get('date') or str(date.today())
    with conn() as c:
        if not names:
            names = []
            seen = set()
            for r in pure_shift_rows_for_date(c, date_str):
                n = str(r.get('girl') or '').strip()
                if n and n not in seen:
                    seen.add(n); names.append(n)
        if not names:
            names = [r['name'] for r in c.execute("SELECT name FROM girls ORDER BY id DESC").fetchall()]
        alias_map = {r['name']:(r['girl_alias'] if 'girl_alias' in r.keys() else '') for r in c.execute("SELECT name,girl_alias FROM girls").fetchall()}
    try:
        neko_items, source = fetch_neko_girls(d.get('neko_user'), d.get('neko_password'))
    except Exception as e:
        return jsonify(ok=False, error=str(e), results=[], missing=names), 502
    results, missing, errors = [], [], []
    for name in names:
        try:
            item = match_neko_girl(name, alias_map.get(name,''), neko_items)
            if not item:
                missing.append(name); continue
            neko_name = item.get('post_title') or item.get('name') or item.get('model_name') or ''
            img = first_neko_image(item)
            if not img:
                missing.append(name); continue
            avatar_url = cache_avatar(name, neko_name, img)
            results.append({'girl_name':name, 'neko_name':neko_name, 'avatar_url':avatar_url, 'source_url':img})
        except Exception as e:
            errors.append({'girl_name':name, 'error':str(e)})
    return jsonify(ok=True, source=source, source_count=len(neko_items), results=results,
                   missing=missing, errors=errors,
                   avatars={r['girl_name']:r for r in results})

@app.route('/api/neko_profiles', methods=['GET','POST'])
def api_neko_profiles():
    init_db()
    d = (request.json or {}) if request.method == 'POST' else {}
    try:
        neko_items, source = fetch_neko_girls(d.get('neko_user'), d.get('neko_password'))
    except Exception as e:
        return jsonify(ok=False, error=str(e), source='', profiles=[]), 502
    profiles = []
    for idx, item in enumerate(neko_items):
        p = neko_profile_from_item(item, idx)
        if p.get('name'):
            source_image = p.get('image') or ''
            local_image = cached_neko_image(p['name'], p['name'], source_image)
            if local_image:
                p['source_image'] = source_image
                p['image'] = local_image
            profiles.append(p)
    notice = ''
    errors = getattr(fetch_neko_girls, 'last_errors', []) or []
    if source == 'seed':
        notice = '喵喵实时接口暂时读取失败，当前显示内置旧名单；新女孩需要等喵喵接口恢复或提供新的公开接口。'
    return jsonify(ok=True, source=source, source_count=len(neko_items), profiles=profiles, notice=notice, errors=errors)

def time_to_min(t):
    m = re.match(r"^(\d{1,2})(?::|\.)(\d{2})$", str(t or '').strip()) or re.match(r"^(\d{1,2})$", str(t or '').strip())
    if not m: return None
    h = int(m.group(1)); mi = int(m.group(2) or 0) if len(m.groups()) > 1 else 0
    if h < 6: h += 24
    return h * 60 + mi

def min_to_time(m):
    h = (int(m) // 60) % 24
    mi = int(m) % 60
    return f"{h:02d}:{mi:02d}"

def service_range_minutes(text):
    """把 MCR 订单时间转换成夜场业务分钟。

    接龙允许 12 小时简写（7-8 表示 19:00-20:00），带冒号的时间按
    24 小时制处理（07:00-08:00 才表示上午）。统一复用出勤/接龙解析器，
    避免 Bot 与 MCR 对同一条订单得出不同的占用时间。
    """
    return _parse_interval_text(text)

def ranges_overlap(a,b,c,d):
    return max(a,c) < min(b,d)


def mcr_girl_free_ranges(c, day, girl, shift, exclude_reservation_id=0, client_now=None):
    """MCR 唯一的女孩空闲时间计算入口，返回业务分钟区间。

    出勤来自 MCR 出勤表，占用来自 MCR 订单表；尚未生成订单的 Bot
    待审核/已批准预约也作为临时占用，防止审核期间被重复预约。
    """
    start = time_to_min(shift.get('start') or shift.get('start_time'))
    end = time_to_min(shift.get('end') or shift.get('end_time'))
    if start is None or end is None:
        return []
    if end <= start:
        end += 24 * 60
    shift_intervals = [(start, end)]

    cutoff = _current_business_minute_for_date(day, client_now)
    if cutoff is not None:
        start = max(start, cutoff)
        if start >= end:
            return []

    busy = []
    for order in c.execute("""SELECT service_time FROM orders
                              WHERE order_date=? AND girl_name=?
                                AND COALESCE(order_status,'')!='取消'""", (day, girl)).fetchall():
        period = _parse_interval_text_for_shift(order['service_time'], shift_intervals)
        if period:
            busy.append(period)

    for reservation in c.execute("""SELECT id,start_time,end_time,COALESCE(order_id,0) AS order_id
                                    FROM customer_reservations
                                    WHERE reserve_date=? AND girl_name=?
                                      AND status IN ('待确认','已确认')""", (day, girl)).fetchall():
        if int(reservation['id']) == int(exclude_reservation_id or 0):
            continue
        # 已经生成 MCR 订单的预约由 orders 统一占用，不重复建立第二套来源。
        if int(reservation['order_id'] or 0) > 0:
            continue
        a = time_to_min(reservation['start_time'])
        b = time_to_min(reservation['end_time'])
        if a is not None and b is not None:
            if b <= a:
                b += 24 * 60
            busy.append((a, b))

    free = [(start, end)]
    for busy_start, busy_end in sorted(busy):
        next_free = []
        for free_start, free_end in free:
            if busy_end <= free_start or busy_start >= free_end:
                next_free.append((free_start, free_end))
                continue
            if free_start < busy_start:
                next_free.append((free_start, busy_start))
            if busy_end < free_end:
                next_free.append((busy_end, free_end))
        free = next_free
    return [(a, b) for a, b in free if b - a >= 30]

def customer_by_phone_or_username(c, phone, username):
    return c.execute("SELECT * FROM customer_accounts WHERE phone=? OR username=? ORDER BY id DESC LIMIT 1", (phone, username)).fetchone()

@app.route('/api/customer_register', methods=['POST'])
def api_customer_register():
    init_db()
    d=request.json or {}
    username=str(d.get('username') or '').strip()
    line_name=str(d.get('line_name') or '').strip()
    phone=str(d.get('phone') or '').strip()
    if not username or not line_name or not phone:
        return jsonify(ok=False,error='用户名、LINE名、手机号都要填写'),400
    with conn() as c:
        old=customer_by_phone_or_username(c, phone, username)
        if old:
            return jsonify(ok=True, status=old['status'], account=dict(old), message='已经注册过，请等待审核或直接登录')
        c.execute("INSERT INTO customer_accounts(username,line_name,phone,status,member_level) VALUES(?,?,?,?,?)", (username,line_name,phone,'待审核','svip'))
        return jsonify(ok=True,status='待审核',message='注册成功，等待管理员审核')

@app.route('/api/customer_login', methods=['POST'])
def api_customer_login():
    init_db()
    d=request.json or {}
    username=str(d.get('username') or '').strip()
    phone=str(d.get('phone') or '').strip()
    with conn() as c:
        row=customer_by_phone_or_username(c, phone, username)
        if not row: return jsonify(ok=False,error='没有找到注册信息，请先注册'),404
        return jsonify(ok=True, account=dict(row), approved=(row['status']=='已通过'))

@app.route('/api/customer_available', methods=['POST'])
def api_customer_available():
    init_db()
    d=request.json or {}
    day=d.get('date') or str(date.today())
    girl_filter=str(d.get('girl_name') or '').strip()
    client_now=_request_client_now(d)
    with conn() as c:
        normalize_chain_order_times_for_date(c, day, girl_filter)
        shifts=pure_shift_rows_for_date(c, day)
        profiles={str(row['name'] or '').strip(): row for row in c.execute(
            """SELECT name,list_price,tags,avatar_url,avatar_updated_at FROM girls
               WHERE COALESCE(girl_status,'')<>'离职'"""
        ).fetchall()}
        cutoff=_current_business_minute_for_date(day, client_now)
        out=[]
        for sft in shifts:
            girl=sft['girl']
            if girl_filter and girl != girl_filter: continue
            st=time_to_min(sft.get('start') or sft.get('start_time'))
            en=time_to_min(sft.get('end') or sft.get('end_time'))
            if st is None or en is None: continue
            if en <= st: en += 24*60
            if cutoff is not None:
                st=max(st, cutoff)
                if st >= en: continue
            free=mcr_girl_free_ranges(c, day, girl, sft, client_now=client_now)
            slots=[]
            for free_start, free_end in free:
                x=free_start
                while x+30 <= free_end:
                    slots.append({'start':min_to_time(x),'end':min_to_time(x+30),'label':f"{min_to_time(x)}-{min_to_time(x+30)}"})
                    x += 30
            profile=profiles.get(girl)
            out.append({'girl':girl,'start':min_to_time(st),'end':min_to_time(en),
                        'price':int(profile['list_price'] or 0) if profile else 0,
                        'tags':normalize_tag_text((sft.get('tags') or '') or (profile['tags'] if profile else '')),
                        'avatar_url':str(profile['avatar_url'] or '') if profile else '',
                        'avatar_updated_at':str(profile['avatar_updated_at'] or '') if profile else '',
                        'orders_20d':int(sft.get('orders_20d') or 0),
                        'orders_2d':int(sft.get('orders_2d') or 0),
                        'trending':bool(int(sft.get('surge_score') or 0) > 0),
                        'slots':slots})
        return jsonify(ok=True,date=day,girls=out)

@app.route('/api/customer_reserve', methods=['POST'])
def api_customer_reserve():
    init_db()
    d=request.json or {}
    acc_id=int(d.get('account_id') or 0)
    day=d.get('date') or str(date.today())
    girl=str(d.get('girl_name') or '').strip()
    start=str(d.get('start_time') or '').strip()
    end=str(d.get('end_time') or '').strip()
    note=str(d.get('note') or '').strip()
    if not acc_id or not girl or not start or not end: return jsonify(ok=False,error='预约信息不完整'),400
    with conn() as c:
        normalize_chain_order_times_for_date(c, day, girl)
        acc=c.execute('SELECT * FROM customer_accounts WHERE id=?',(acc_id,)).fetchone()
        if not acc: return jsonify(ok=False,error='请先注册'),404
        if acc['status']!='已通过': return jsonify(ok=False,error='管理员审核通过后才可以预约'),403
        # 再检查一次空档，避免重复预约
        avail=api_customer_available().json if False else None
        a=time_to_min(start); b=time_to_min(end)
        if a is None or b is None: return jsonify(ok=False,error='时间格式错误'),400
        if b <= a: b += 24*60
        cutoff=_current_business_minute_for_date(day, _request_client_now(d))
        if cutoff is not None and a < cutoff:
            return jsonify(ok=False,error='不能预约已经过去的时间'),400
        shift_intervals = _shift_intervals_for_girl(c, day, girl)
        for o in c.execute("SELECT service_time FROM orders WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'", (day,girl)).fetchall():
            r=_parse_interval_text_for_shift(o['service_time'], shift_intervals)
            if r and ranges_overlap(a,b,r[0],r[1]): return jsonify(ok=False,error='这个时间已经被预约'),409
        for rsv in c.execute("SELECT start_time,end_time FROM customer_reservations WHERE reserve_date=? AND girl_name=? AND status IN ('待确认','已确认')", (day,girl)).fetchall():
            c1=time_to_min(rsv['start_time']); d1=time_to_min(rsv['end_time'])
            if c1 is not None and d1 is not None:
                if d1 <= c1: d1 += 24*60
                if ranges_overlap(a,b,c1,d1): return jsonify(ok=False,error='这个时间已经被预约'),409
        price_row=c.execute('SELECT list_price FROM girls WHERE name=?',(girl,)).fetchone()
        price=int(price_row['list_price'] or 15000) if price_row else 15000
        c.execute("""INSERT INTO customer_reservations(reserve_date,girl_name,start_time,end_time,customer_account_id,username,line_name,phone,status,price,note)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (day,girl,start,end,acc_id,acc['username'],acc['line_name'],acc['phone'],'待确认',price,note))
        return jsonify(ok=True,message='预约已提交，等待管理员确认')

@app.route('/api/customer_accounts/status', methods=['POST'])
def api_customer_account_status():
    init_db()
    d=request.json or {}
    acc_id=int(d.get('id') or 0); status=d.get('status') or '已通过'
    with conn() as c:
        acc=c.execute('SELECT * FROM customer_accounts WHERE id=?',(acc_id,)).fetchone()
        if not acc: return jsonify(ok=False,error='账号不存在'),404
        customer_id=int(acc['customer_id'] or 0)
        if status=='已通过' and not customer_id:
            cust=ensure_customer(c, acc['username'], f"LINE:{acc['line_name']} 手机:{acc['phone']}")
            customer_id=cust['id']
        c.execute("UPDATE customer_accounts SET status=?, member_level='svip', customer_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status,customer_id,acc_id))
    return jsonify(ok=True)

@app.route('/api/customer_reservations/status', methods=['POST'])
def api_customer_reservation_status():
    init_db()
    d=request.json or {}
    rid=int(d.get('id') or 0); status=d.get('status') or '已确认'
    with conn() as c:
        r=c.execute('SELECT * FROM customer_reservations WHERE id=?',(rid,)).fetchone()
        if not r: return jsonify(ok=False,error='预约不存在'),404
        order_id=int(r['order_id'] or 0)
        if status=='已确认' and not order_id:
            acc=c.execute('SELECT * FROM customer_accounts WHERE id=?',(r['customer_account_id'],)).fetchone()
            customer_raw = acc['username'] if acc else r['username']
            g=c.execute('SELECT id FROM girls WHERE name=?',(r['girl_name'],)).fetchone()
            create_or_update_order(c, {'order_date':r['reserve_date'], 'girl_id': int(g['id']) if g else 0, 'girl_name':r['girl_name'], 'service_time':f"{r['start_time']}-{r['end_time']}", 'received_amount':int(r['price'] or 0), 'customer_raw':customer_raw, 'remark': '客人网站提前预约 '+(r['note'] or ''), 'order_status':'预约中', 'settlement_status':'未结算'})
            order_id=c.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        c.execute("UPDATE customer_reservations SET status=?, order_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status,order_id,rid))
        updated = c.execute('SELECT * FROM customer_reservations WHERE id=?', (rid,)).fetchone()
    return jsonify(ok=True, reservation=dict(updated))

def open_browser(): webbrowser.open('http://127.0.0.1:5057')

# Telegram 预约系统：与 MCR 共用出勤、房间、预约和订单数据库。
from telegram_booking import register_telegram_booking
register_telegram_booking(
    app=app,
    conn=conn,
    init_main_db=init_db,
    pure_shift_rows_for_date=pure_shift_rows_for_date,
    create_or_update_order=create_or_update_order,
    time_to_min=time_to_min,
    min_to_time=min_to_time,
    service_range_minutes=service_range_minutes,
    ranges_overlap=ranges_overlap,
    tokyo_now=_tokyo_now,
    current_business_minute_for_date=_current_business_minute_for_date,
    mcr_girl_free_ranges=mcr_girl_free_ranges,
    import_chain_text=import_chain_text,
    order_to_chain_line=order_to_chain_line,
    ensure_customer=ensure_customer,
    refresh_customer_totals=refresh_customer_totals,
    sync_wordpress_attendance=sync_alice_wordpress_attendance,
    parse_chain_header=parse_header,
)

import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port)
