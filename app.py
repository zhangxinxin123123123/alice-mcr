
import re, math, sqlite3, webbrowser, threading, os, smtplib, json, hashlib, traceback
from datetime import date, datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from flask import Flask, request, jsonify, send_from_directory
APP_DIR=Path(__file__).resolve().parent
DB_PATH=Path(os.environ.get('ALICE_DB_PATH') or ('/var/data/alice_academy_mcr.db' if Path('/var/data').exists() else str(APP_DIR/'alice_academy_mcr.db')))
BOSS_EMAIL=os.environ.get('ALICE_BOSS_EMAIL','xinxinzhang330@gmail.com')
NEKO_BASE_URL=os.environ.get('ALICE_NEKO_BASE_URL','https://neko-miaomiao.com').rstrip('/')
ALICE_BASE_URL=os.environ.get('ALICE_PUBLIC_BASE_URL','https://ailisi99.com').rstrip('/')
TOKYO_YY_BASE_URL=os.environ.get('TOKYO_YY_BASE_URL','https://tokyo-yy.com').rstrip('/')
TOKYO_ALICE_SHOP_ID=os.environ.get('TOKYO_ALICE_SHOP_ID','\u7231\u4e3d\u4e1d\u5b66\u56ed')
AVATAR_DIR=APP_DIR/'static'/'girl_avatars'
app=Flask(__name__, static_folder=str(APP_DIR/'static'), static_url_path='/static')

app.config['JSON_AS_ASCII'] = False

# 固定登录账号：需要改账号密码就在这里改
USERS = {
    "Star": {"password": "9941", "role": "boss", "label": "老板"},
    "admin": {"password": "admin123", "role": "admin", "label": "管理员"},
    "user": {"password": "user123", "role": "user", "label": "普通用户"},
}
PUBLIC_PATHS = {"/", "/reserve", "/api/login", "/api/health", "/api/db_info", "/api/customer_register", "/api/customer_login", "/api/customer_available", "/api/customer_reserve"}

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
    """识别接龙首行：0524小樱 / 1.0524小樱 / 2026-05-24 小樱。"""
    for line in lines or []:
        s = strip_chain_prefix(line) if 'strip_chain_prefix' in globals() else str(line or '').strip()
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s*([^\s/]+)?", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", (m.group(4) or '').strip()
        # 紧凑日期（0524小樱）只用于接龙首行；包含 7.30到8.30 / 7.30-8.30 的预约行不能误判成 0830 日期。
        if re.search(r"\d{1,2}(?:[:.]\d{1,2})?\s*(?:[-~ー～]|到|至)\s*\d{1,2}(?:[:.]\d{1,2})?", s):
            continue
        m = re.search(r"(?<!\d)(\d{1,2})(\d{2})\s*([^\s/]+)?", s)
        if m:
            y = date.today().year
            return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", (m.group(3) or '').strip()
    return None, ''

def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_no TEXT NOT NULL UNIQUE, name TEXT,
            customer_type TEXT DEFAULT '新客', customer_status TEXT DEFAULT '正常', recharge_balance INTEGER DEFAULT 0,
            total_recharge INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0, points INTEGER DEFAULT 0, total_points INTEGER DEFAULT 0,
            source TEXT DEFAULT '', contact TEXT DEFAULT '', grade TEXT DEFAULT '', tags TEXT DEFAULT '', member_level TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', customer_type_locked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girls(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, girl_alias TEXT DEFAULT '', girl_type TEXT DEFAULT '普通', girl_status TEXT DEFAULT '在职',
            take_home_per_hour INTEGER DEFAULT 10000, list_price INTEGER DEFAULT 15000, contact TEXT DEFAULT '', tags TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT, service_time TEXT, hours REAL DEFAULT 1,
            girl_id INTEGER, girl_name TEXT, customer_id INTEGER, customer_no TEXT, customer_name TEXT,
            received_amount INTEGER DEFAULT 0, girl_take_home INTEGER DEFAULT 0, store_profit INTEGER DEFAULT 0, points INTEGER DEFAULT 0,
            order_status TEXT DEFAULT '已结束', settlement_status TEXT DEFAULT '未结算', payment_method TEXT DEFAULT '现金',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', raw_text TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settlement_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TEXT NOT NULL, girl_name TEXT NOT NULL,
            theoretical_amount INTEGER DEFAULT 0, actual_settlement INTEGER DEFAULT 0, formula_text TEXT DEFAULT '',
            order_ids TEXT DEFAULT '', boss_email TEXT DEFAULT '', girl_email TEXT DEFAULT '',
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
            girl_name TEXT PRIMARY KEY, tags TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girl_avatar_cache(
            girl_name TEXT PRIMARY KEY, neko_name TEXT DEFAULT '', avatar_url TEXT DEFAULT '',
            source_url TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
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
        for qg, qt, qc, so in [('网址','网址','',1),('常用短语','常用短语','',2)]:
            c.execute('INSERT OR IGNORE INTO quick_links(group_name,title,content,sort_order) SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM quick_links WHERE group_name=? AND title=?)', (qg, qt, qc, so, qg, qt))
        defaults=[('customer_type','新客',1),('customer_type','回头客',2),('customer_type','老客',3),('customer_type','VIP',4),('customer_type','SVIP',5),('customer_type','常客',6),('girl_type','普通',1),('girl_status','在职',1),('order_status','预约中',0),('order_status','已结束',1),('order_status','取消',2),('settlement_status','未结算',1),('settlement_status','已结算',2),('schedule_status','出勤',1),('schedule_status','休息',2),('customer_preference_tag','酒量好',1),('customer_preference_tag','喜欢聊天',2),('customer_preference_tag','喜欢新人',3),('customer_preference_tag','安静型',4)]
        for et,v,so in defaults:
            c.execute('INSERT OR IGNORE INTO enum_values(enum_type,value,sort_order) VALUES(?,?,?)',(et,v,so))
        # v17.1: 默认新增女孩定价 15000，到手 10000；旧库自动补列和迁移快捷分组名称。
        customer_cols = [r[1] for r in c.execute('PRAGMA table_info(customers)').fetchall()]
        if 'customer_type_locked' not in customer_cols:
            c.execute("ALTER TABLE customers ADD COLUMN customer_type_locked INTEGER DEFAULT 0")
        girl_cols = [r[1] for r in c.execute('PRAGMA table_info(girls)').fetchall()]
        if 'list_price' not in girl_cols:
            c.execute('ALTER TABLE girls ADD COLUMN list_price INTEGER DEFAULT 15000')
        if 'girl_alias' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN girl_alias TEXT DEFAULT ''")
        if 'email' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN email TEXT DEFAULT ''")
        c.execute("UPDATE quick_links SET group_name='网址' WHERE group_name='排班表'")
        c.execute("UPDATE quick_links SET title='网址' WHERE title='排班表'")
        c.execute("UPDATE quick_links SET group_name='常用短语' WHERE group_name='固定短语'")
        c.execute("UPDATE quick_links SET title='常用短语' WHERE title='固定短语'")

def current_role():
    return request.headers.get('X-Alice-Role') or request.args.get('role') or ''

@app.before_request
def require_login_for_api():
    path = request.path
    if path.startswith('/static/') or path in PUBLIC_PATHS:
        return None
    if path.startswith('/api/') and current_role() not in ('boss','admin','user'):
        return jsonify(ok=False, error='请先登录'), 401
    return None

@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.json or {}
    u = str(d.get('username') or '').strip()
    p = str(d.get('password') or '').strip()
    info = USERS.get(u)
    if info and info['password'] == p:
        return jsonify(ok=True, username=u, role=info['role'], label=info['label'])
    return jsonify(ok=False, error='用户名或密码错误'), 401


def conn():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
def rows(rs): return [dict(r) for r in rs]

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
    rel = '/static/girl_avatars/' + path.name
    with conn() as c:
        c.execute("""INSERT INTO girl_avatar_cache(girl_name,neko_name,avatar_url,source_url,updated_at)
                     VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(girl_name) DO UPDATE SET
                     neko_name=excluded.neko_name, avatar_url=excluded.avatar_url,
                     source_url=excluded.source_url, updated_at=CURRENT_TIMESTAMP""",
                  (girl_name, neko_name, rel, src_url))
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
            local_path = APP_DIR / str(row['avatar_url']).lstrip('/').replace('/', os.sep)
            if local_path.exists():
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




def parse_service_end_datetime(order_date, service_time):
    """把 23.30-0.30 / 20:00-21:00 这类预约时间转换为结束 datetime；凌晨自动按次日处理。"""
    try:
        base = datetime.strptime(str(order_date), "%Y-%m-%d")
    except Exception:
        return None
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
    now = datetime.now()
    for o in c.execute("SELECT id,order_date,service_time,order_status FROM orders WHERE COALESCE(order_status,'')='预约中'").fetchall():
        end_dt = parse_service_end_datetime(o['order_date'], o['service_time'])
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

def recalc_customer_points(c, customer_id=None, update_types=True):
    if customer_id:
        ids = [customer_id]
    else:
        ids = [r["id"] for r in c.execute("SELECT id FROM customers").fetchall()]
    for cid in ids:
        row = c.execute("SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(received_amount),0) AS spent FROM orders WHERE customer_id=?", (cid,)).fetchone()
        pts = int(row["pts"] or 0)
        spent = int(row["spent"] or 0)
        c.execute("UPDATE customers SET points=?, total_points=?, total_spent=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (pts, pts, spent, cid))
    if update_types:
        update_customer_type_by_history(c, None)


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

def create_or_update_order(c,d):
    old_customer_id = None
    if d.get('id'):
        old = c.execute("SELECT customer_id FROM orders WHERE id=?", (int(d['id']),)).fetchone()
        if old:
            old_customer_id = old["customer_id"]

    g = None
    if d.get('girl_id'):
        g = c.execute('SELECT * FROM girls WHERE id=?',(int(d['girl_id']),)).fetchone()
    if not g:
        g = ensure_girl(c,d.get('girl_name',''))
    if not g:
        raise ValueError('缺少女孩')

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
    pts = math.floor(rec/20)
    auto_payment = detect_payment_method_from_note(d.get('remark',''), d.get('remark2',''), d.get('raw_text',''))
    payment_method = auto_payment or (d.get('payment_method') or '现金')

    if d.get('id'):
        c.execute("""UPDATE orders SET order_date=?,service_time=?,hours=?,girl_id=?,girl_name=?,customer_id=?,customer_no=?,customer_name=?,received_amount=?,girl_take_home=?,store_profit=?,points=?,order_status=?,settlement_status=?,payment_method=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),payment_method,d.get('remark',''),d.get('remark2',''),d.get('id')))
        if old_customer_id and old_customer_id != cust['id']:
            recalc_customer_points(c, old_customer_id)
        recalc_customer_points(c, cust['id'])
    else:
        c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,customer_no,customer_name,received_amount,girl_take_home,store_profit,points,order_status,settlement_status,payment_method,remark,remark2,raw_text)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),payment_method,d.get('remark',''),d.get('remark2',''),d.get('raw_text','')))
        recalc_customer_points(c, cust['id'])


@app.route('/')
def index(): return send_from_directory(APP_DIR/'static','index.html')
@app.route('/reserve')
def reserve_page(): return send_from_directory(APP_DIR/'static','reserve.html')
@app.route('/api/all')
def all_data():
    init_db()
    with conn() as c:
        auto_finish_reservations(c)
        update_customer_type_by_history(c, None)
        return jsonify({
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
            'quick_links':rows(c.execute('SELECT * FROM quick_links ORDER BY sort_order, id').fetchall())})
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
@app.route('/api/girls',methods=['POST'])
def girls():
    d=request.json or {}
    with conn() as c:
        if d.get('id'):
            c.execute('''UPDATE girls SET name=?,girl_alias=?,girl_type=?,girl_status=?,take_home_per_hour=?,list_price=?,contact=?,tags=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type'),d.get('girl_status'),int(d.get('take_home_per_hour') or 10000),int(d.get('list_price') or 15000),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2',''),d.get('id')))
            recalc_girl(c,int(d['id']))
        else:
            c.execute('''INSERT OR IGNORE INTO girls(name,girl_alias,girl_type,girl_status,take_home_per_hour,list_price,contact,tags,remark,remark2) VALUES(?,?,?,?,?,?,?,?,?,?)''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type','普通'),d.get('girl_status','在职'),int(d.get('take_home_per_hour') or 10000),int(d.get('list_price') or 15000),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2','')))
            row=c.execute('SELECT id FROM girls WHERE name=?',(d.get('name'),)).fetchone()
            if row: recalc_girl(c,row['id'])
    return jsonify(ok=True)
@app.route('/api/orders',methods=['POST'])
def orders():
    with conn() as c: create_or_update_order(c, request.json or {})
    return jsonify(ok=True)
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
        c.execute(f'DELETE FROM {allowed[table]} WHERE id=?',(item_id,))
        if table == 'orders' and affected_customer_id:
            recalc_customer_points(c, affected_customer_id)
        elif table in ('customers', 'recharges', 'points'):
            update_customer_type_by_history(c, None)
    return jsonify(ok=True)
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

def normalize_chain_time_token(token):
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
    if ':' in token:
        return f"{sh % 24:02d}:{sm:02d}-{eh % 24:02d}:{em:02d}"

    # 12.30-1.30 是同日 12.30-13.30，不允许被标准化成 25.30。
    display_eh = eh
    if eh * 60 + em < sh * 60 + sm and sh >= 12 and eh < 12:
        same_day_pm_end = (eh + 12) * 60 + em
        if same_day_pm_end > sh * 60 + sm:
            display_eh = eh + 12

    start = _time_label(sh, sm)
    end = _time_label(display_eh, em)
    return f"{start}-{end}"

def parse_chain_service_time(line):
    body = strip_chain_prefix(line)
    if "包夜" in body:
        return "包夜 12.00-5.00", body
    # support 5.30-6.30, 10-12, 6.40-7.40, 8:45-9:45, 23.30-0.30
    m = re.search(r"(\d{1,2}(?:[:.]\d{1,2})?\s*(?:[-~ー～]|到|至)\s*\d{1,2}(?:[:.]\d{1,2})?)(.*)$", body)
    if not m:
        return None, body
    return normalize_chain_time_token(m.group(1)), m.group(2)

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


@app.route('/api/import_chain',methods=['POST'])
def import_chain():
    try:
        d = request.json or {}
        lines = [x.strip() for x in d.get('text','').splitlines() if x.strip()]
        hd, hg = parse_header(lines)
        od = d.get('order_date') or hd or str(date.today())
        count = 0
        with conn() as c:
            g = None
            if d.get('girl_id'):
                g = c.execute('SELECT * FROM girls WHERE id=?', (int(d['girl_id']),)).fetchone()
            if not g:
                g = ensure_girl(c, hg)
            if not g:
                return jsonify(ok=False, error='无法识别女孩名。请确认首行类似：0524小樱'), 400

            for line in lines:
                st, rest_raw = parse_chain_service_time(line)
                if not st:
                    continue

                parts = split_chain_fields(rest_raw)

                if parts and '包夜' in parts[0][0]:
                    parts.pop(0)
                if not parts:
                    continue

                rec = yen_to_int(parts.pop(0)[0])
                if not parts:
                    raise ValueError(f'接龙行缺少客人字段：{line}。格式：时间/价格/客人用户名 或 时间/价格/客人ID。')
                cust_token, force_name = parts.pop(0)
                cust = ('__NAME__:' + cust_token) if force_name else cust_token
                assert_no_duplicate_customer_name_for_chain(c, cust)
                remark_parts = [p[0] for p in parts]

                create_or_update_order(c, {
                    'order_date': od,
                    'service_time': st,
                    'girl_id': g['id'],
                    'received_amount': rec,
                    'customer_raw': cust,
                    'remark': ' '.join(remark_parts),
                    'settlement_status': d.get('settlement_status','未结算'),
                    'raw_text': line
                })
                count += 1
        return jsonify(ok=True, count=count, girl_name=g['name'])
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
        busy = []
        for o in c.execute("""SELECT service_time FROM orders
                            WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'""", (date_str, girl)).fetchall():
            itv = _parse_interval_text(o['service_time'])
            if itv:
                busy.append(itv)
        free = _subtract_intervals(base, busy)
        if cutoff is not None:
            free = [(max(a, cutoff), b) for a, b in free if max(a, cutoff) < b]
        if not free:
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
        auto_finish_reservations(c)
        shifts = pure_shift_rows_for_date(c, date_str)
        # 给每个纯出勤女孩补上女孩表价格/ID，接龙预约用这个自动定价。
        out_shifts = []
        for sft in shifts:
            g = c.execute('SELECT * FROM girls WHERE name=?', (sft.get('girl') or '',)).fetchone()
            row = dict(sft)
            row['girl_id'] = g['id'] if g else 0
            row['price'] = int((g['list_price'] if g else 15000) or 15000)
            out_shifts.append(row)
        if not girl_name and out_shifts:
            girl_name = out_shifts[0]['girl']
        orders = rows(c.execute("""SELECT * FROM orders
            WHERE order_date=? AND girl_name=?
            ORDER BY service_time ASC, id ASC""", (date_str, girl_name)).fetchall()) if girl_name else []
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
        service_time = normalize_chain_time_token(d.get('service_time') or '')
        base_price = int((g['list_price'] or 15000) or 15000)
        raw_amount = d.get('received_amount')
        amount = int(raw_amount) if str(raw_amount or '').strip() else int(round(base_price * calc_hours(service_time)))
        assert_no_duplicate_customer_name_for_chain(c, d.get('customer_raw') or '', d.get('id') or None)
        create_or_update_order(c, {
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
        })
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
            "version": "v29_basic_time_no_pm",
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
            "version": "v29_basic_time_no_pm",
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
    ids = [int(x) for x in (d.get('ids') or [])]
    if not ids:
        return jsonify(ok=False, error='没有选择订单'), 400
    q = ','.join(['?'] * len(ids))
    with conn() as c:
        affected_customer_ids_for_bulk_delete = [r['customer_id'] for r in c.execute(f'SELECT DISTINCT customer_id FROM orders WHERE id IN ({q})', ids).fetchall() if r['customer_id']]
        c.execute(f'DELETE FROM orders WHERE id IN ({q})', ids)
        for cid in affected_customer_ids_for_bulk_delete:
            recalc_customer_points(c, cid)
    return jsonify(ok=True, deleted=len(ids))

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
        grouped[girl]['theoretical_amount'] += amount
        if str(o['payment_method'] or '现金') != '现金':
            grouped[girl]['non_cash'] += amount
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
            c.execute("""INSERT INTO settlement_reports(report_date,girl_name,theoretical_amount,actual_settlement,formula_text,order_ids,boss_email,girl_email,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(report_date,girl_name) DO UPDATE SET
                           theoretical_amount=excluded.theoretical_amount,
                           actual_settlement=excluded.actual_settlement,
                           formula_text=excluded.formula_text,
                           order_ids=excluded.order_ids,
                           boss_email=excluded.boss_email,
                           girl_email=excluded.girl_email,
                           updated_at=CURRENT_TIMESTAMP""",
                      (report_date, girl_name, theoretical, actual, formula, order_ids, BOSS_EMAIL, girl_email))
            saved += 1
    return jsonify(ok=True, saved=saved)

def send_plain_email(to_addrs, subject, body, display_name='Alice MCR', smtp=None):
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
            boss_lines.append(f"{r['girl_name']}：今日理论 {int(r['theoretical_amount'] or 0)}，实给 {int(r['actual_settlement'] or 0)}。公式：{r['formula_text'] or ''}")
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

@app.route('/api/customers/recalc_points', methods=['POST'])
def api_customers_recalc_points():
    with conn() as c:
        recalc_customer_points(c, None, update_types=False)
    return jsonify(ok=True)



def sync_room_assignment_to_schedule(c, assignment_date, girl_id, hotel_name, room_no):
    if not girl_id:
        return
    girl = c.execute("SELECT * FROM girls WHERE id=?", (int(girl_id),)).fetchone()
    if not girl:
        return
    note = f"房间安排自动生成：{hotel_name}-{room_no}"
    old = c.execute("SELECT id FROM girl_schedules WHERE schedule_date=? AND girl_id=? AND note=?", (assignment_date, int(girl_id), note)).fetchone()
    price = int(girl['list_price'] or 15000)
    if old:
        c.execute("""UPDATE girl_schedules SET girl_name=?, start_time='00:00', end_time='04:00', price=?, status='出勤', updated_at=CURRENT_TIMESTAMP WHERE id=?""", (girl['name'], price, old['id']))
    else:
        c.execute("""INSERT INTO girl_schedules(schedule_date,girl_id,girl_name,start_time,end_time,price,status,note) VALUES(?,?,?,?,?,?,?,?)""", (assignment_date, int(girl_id), girl['name'], '00:00', '04:00', price, '出勤', note))

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
        sync_room_assignment_to_schedule(c, assignment_date, girl_id, hotel, room)
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
                hotel = str(rr.get('hotel_name') or '').strip(); room = str(rr.get('room_no') or '').strip()
                if not hotel or not room: continue
                r = c.execute('SELECT daily_cost FROM hotel_rooms WHERE hotel_name=? AND room_no=?', (hotel, room)).fetchone()
                cost = int(rr.get('daily_cost') or (r['daily_cost'] if r else 0) or 0)
                gid = int(girls[idx] if idx < len(girls) and girls[idx] else 0)
                gname = ''
                if gid:
                    g = c.execute('SELECT * FROM girls WHERE id=?', (gid,)).fetchone(); gname = g['name'] if g else ''
                c.execute("""INSERT OR IGNORE INTO hotel_rooms(hotel_name,room_no,daily_cost) VALUES(?,?,?)""", (hotel, room, cost))
                c.execute("""INSERT OR REPLACE INTO room_assignments(assignment_date,hotel_name,room_no,girl_id,girl_name,daily_cost,note,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", (str(day), hotel, room, gid, gname, cost, note))
                sync_room_assignment_to_schedule(c, str(day), gid, hotel, room)
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
        mem = c.execute("SELECT tags FROM girl_tag_memory WHERE girl_name=?", (r['girl_name'],)).fetchone()
        g = c.execute("SELECT tags FROM girls WHERE name=?", (r['girl_name'],)).fetchone()
        tag_text = normalize_tag_text((mem['tags'] if mem else '') or (g['tags'] if g else ''))
        schedules.append({
            'id': f"schedule_{r['id']}", 'raw_id': r['id'], 'date': r['schedule_date'], 'girl': r['girl_name'],
            'start': r['start_time'] or '00:00', 'end': r['end_time'] or '04:00', 'tags': tag_text,
            'goldTags': '房间' if '房间安排自动生成' in str(r['note'] or '') else '', 'source': 'schedule', 'sort_order': 10000 + int(r['id'] or 0)
        })
    return pure + schedules

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
        tags = {r['girl_name']: normalize_tag_text(r['tags']) for r in c.execute('SELECT * FROM girl_tag_memory').fetchall()}
        return jsonify(ok=True, shifts=shifts, girl_tags=tags, copied=copied)

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
        c.execute("""INSERT INTO girl_tag_memory(girl_name,tags,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(girl_name) DO UPDATE SET tags=excluded.tags, updated_at=CURRENT_TIMESTAMP""", (girl, tags))
        if raw_id.startswith('pure_'):
            sid = int(raw_id.split('_',1)[1])
            c.execute("""UPDATE pure_shifts SET shift_date=?,girl_name=?,start_time=?,end_time=?,tags=?,gold_tags=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (shift_date, girl, start, end, tags, gold_tags, sid))
            return jsonify(ok=True, id=f'pure_{sid}')
        if raw_id.startswith('schedule_'):
            sid = int(raw_id.split('_',1)[1])
            g = c.execute('SELECT id FROM girls WHERE name=?', (girl,)).fetchone()
            c.execute("""UPDATE girl_schedules SET schedule_date=?, girl_id=?, girl_name=?, start_time=?, end_time=?, price=?, status='出勤', updated_at=CURRENT_TIMESTAMP WHERE id=?""", (shift_date, int(g['id']) if g else 0, girl, start, end, 0, sid))
            return jsonify(ok=True, id=f'schedule_{sid}')
        max_sort = c.execute('SELECT COALESCE(MAX(sort_order),0) AS m FROM pure_shifts WHERE shift_date=?', (shift_date,)).fetchone()['m']
        cur = c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags,sort_order,source)
                           VALUES(?,?,?,?,?,?,?,?)""", (shift_date, girl, start, end, tags, gold_tags, int(max_sort or 0)+1, 'manual'))
        return jsonify(ok=True, id=f"pure_{cur.lastrowid}")

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
            data = rows(c.execute(f"SELECT * FROM girl_avatar_cache WHERE girl_name IN ({placeholders})", names).fetchall())
        else:
            data = rows(c.execute("SELECT * FROM girl_avatar_cache ORDER BY updated_at DESC").fetchall())
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
    if '包夜' in str(text or ''):
        return 24*60, 29*60
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*(?:[-~ー～]|到|至)\s*(\d{1,2})(?:[:.](\d{1,2}))?", str(text or ''))
    if not m: return None
    a = int(m.group(1))*60 + int(m.group(2) or 0)
    b = int(m.group(3))*60 + int(m.group(4) or 0)
    if int(m.group(1)) < 6: a += 24*60
    if int(m.group(3)) < 6: b += 24*60
    if b <= a: b += 24*60
    return a,b

def ranges_overlap(a,b,c,d):
    return max(a,c) < min(b,d)

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
        shifts=pure_shift_rows_for_date(c, day)
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
            busy=[]
            for o in c.execute("SELECT service_time FROM orders WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'", (day,girl)).fetchall():
                r=service_range_minutes(o['service_time'])
                if r: busy.append(r)
            for rsv in c.execute("SELECT start_time,end_time FROM customer_reservations WHERE reserve_date=? AND girl_name=? AND status IN ('待确认','已确认')", (day,girl)).fetchall():
                a=time_to_min(rsv['start_time']); b=time_to_min(rsv['end_time'])
                if a is not None and b is not None:
                    if b <= a: b += 24*60
                    busy.append((a,b))
            slots=[]
            x=st
            while x+30 <= en:
                if not any(ranges_overlap(x,x+30,a,b) for a,b in busy):
                    slots.append({'start':min_to_time(x),'end':min_to_time(x+30),'label':f"{min_to_time(x)}-{min_to_time(x+30)}"})
                x += 30
            out.append({'girl':girl,'start':min_to_time(st),'end':min_to_time(en),'price':sft.get('price') or 0,'slots':slots})
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
        for o in c.execute("SELECT service_time FROM orders WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'", (day,girl)).fetchall():
            r=service_range_minutes(o['service_time'])
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
    return jsonify(ok=True)

def open_browser(): webbrowser.open('http://127.0.0.1:5057')
import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port)
