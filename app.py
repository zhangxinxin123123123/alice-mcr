
import re, math, sqlite3, webbrowser, threading
from datetime import date, datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
APP_DIR=Path(__file__).resolve().parent; DB_PATH=APP_DIR/'alice_academy_mcr.db'
app=Flask(__name__, static_folder=str(APP_DIR/'static'), static_url_path='/static')

app.config['JSON_AS_ASCII'] = False

# 固定登录账号：需要改账号密码就在这里改
USERS = {
    "admin": {"password": "admin123", "role": "admin", "label": "管理员"},
    "user": {"password": "user123", "role": "user", "label": "普通用户"},
}
PUBLIC_PATHS = {"/", "/api/login", "/api/health", "/api/db_info"}

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
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS girls(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, girl_alias TEXT DEFAULT '', girl_type TEXT DEFAULT '普通', girl_status TEXT DEFAULT '在职',
            take_home_per_hour INTEGER DEFAULT 0, take_home_per_order INTEGER DEFAULT 0, list_price INTEGER DEFAULT 0, contact TEXT DEFAULT '', tags TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT, service_time TEXT, hours REAL DEFAULT 1,
            girl_id INTEGER, girl_name TEXT, customer_id INTEGER, customer_no TEXT, customer_name TEXT,
            received_amount INTEGER DEFAULT 0, girl_take_home INTEGER DEFAULT 0, store_profit INTEGER DEFAULT 0, points INTEGER DEFAULT 0,
            order_status TEXT DEFAULT '已结束', settlement_status TEXT DEFAULT '未结算', payment_method TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', raw_text TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS recharge_records(id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, customer_no TEXT, amount INTEGER, payment_method TEXT DEFAULT '', remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', order_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
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
        defaults=[('customer_type','新客',1),('customer_type','常客',2),('girl_type','普通',1),('girl_status','在职',1),('order_status','已结束',1),('settlement_status','未结算',1),('settlement_status','已结算',2),('schedule_status','出勤',1),('schedule_status','休息',2),('customer_preference_tag','酒量好',1),('customer_preference_tag','喜欢聊天',2),('customer_preference_tag','喜欢新人',3),('customer_preference_tag','安静型',4)]
        for et,v,so in defaults:
            c.execute('INSERT OR IGNORE INTO enum_values(enum_type,value,sort_order) VALUES(?,?,?)',(et,v,so))
        # v16.4: existing databases get the new girl list price column automatically
        girl_cols = [r[1] for r in c.execute('PRAGMA table_info(girls)').fetchall()]
        if 'list_price' not in girl_cols:
            c.execute('ALTER TABLE girls ADD COLUMN list_price INTEGER DEFAULT 0')
        if 'girl_alias' not in girl_cols:
            c.execute("ALTER TABLE girls ADD COLUMN girl_alias TEXT DEFAULT ''")

def current_role():
    return request.headers.get('X-Alice-Role') or request.args.get('role') or ''

@app.before_request
def require_login_for_api():
    path = request.path
    if path.startswith('/static/') or path in PUBLIC_PATHS:
        return None
    if path.startswith('/api/') and current_role() not in ('admin','user'):
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
    c=sqlite3.connect("/var/data/alice_academy_mcr.db"); c.row_factory=sqlite3.Row; return c
def rows(rs): return [dict(r) for r in rs]
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
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*[-~ー～]\s*(\d{1,2})(?:[:.](\d{1,2}))?", text)
    if not m:
        return 1.0
    a = parse_time_part(m.group(1), m.group(2))
    b = parse_time_part(m.group(3), m.group(4))
    if b < a:
        b += 24
    minutes = int(round((b - a) * 60))
    return billable_hours_from_minutes(minutes)








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
    c.execute('INSERT INTO girls(name,remark) VALUES(?,?)',(name,'接龙/订单自动生成'))
    return c.execute('SELECT * FROM girls WHERE name=?',(name,)).fetchone()
def take_home(g,hours):
    if not g: return 0
    per=int(g['take_home_per_order'] or 0); hr=int(g['take_home_per_hour'] or 0)
    return per if per>0 else int(round(hr*float(hours or 1)))
def recalc_girl(c,gid):
    g=c.execute('SELECT * FROM girls WHERE id=?',(gid,)).fetchone()
    if not g: return
    for o in c.execute('SELECT * FROM orders WHERE girl_id=?',(gid,)).fetchall():
        h=float(o['hours'] or calc_hours(o['service_time'])); th=take_home(g,h); prof=round_yen_1000_half_up(int(o['received_amount'] or 0)-th)
        c.execute('UPDATE orders SET girl_name=?, girl_take_home=?, store_profit=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',(g['name'],th,prof,o['id']))

def recalc_customer_points(c, customer_id=None):
    if customer_id:
        ids = [customer_id]
    else:
        ids = [r["id"] for r in c.execute("SELECT id FROM customers").fetchall()]
    for cid in ids:
        row = c.execute("SELECT COALESCE(SUM(points),0) AS pts, COALESCE(SUM(received_amount),0) AS spent FROM orders WHERE customer_id=?", (cid,)).fetchone()
        pts = int(row["pts"] or 0)
        spent = int(row["spent"] or 0)
        c.execute("UPDATE customers SET points=?, total_points=?, total_spent=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (pts, pts, spent, cid))

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

    if d.get('id'):
        c.execute("""UPDATE orders SET order_date=?,service_time=?,hours=?,girl_id=?,girl_name=?,customer_id=?,customer_no=?,customer_name=?,received_amount=?,girl_take_home=?,store_profit=?,points=?,order_status=?,settlement_status=?,payment_method=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),d.get('payment_method',''),d.get('remark',''),d.get('remark2',''),d.get('id')))
        if old_customer_id and old_customer_id != cust['id']:
            recalc_customer_points(c, old_customer_id)
        recalc_customer_points(c, cust['id'])
    else:
        c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,customer_no,customer_name,received_amount,girl_take_home,store_profit,points,order_status,settlement_status,payment_method,remark,remark2,raw_text)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),d.get('payment_method',''),d.get('remark',''),d.get('remark2',''),d.get('raw_text','')))
        recalc_customer_points(c, cust['id'])


@app.route('/')
def index(): return send_from_directory(APP_DIR/'static','index.html')
@app.route('/api/all')
def all_data():
    init_db()
    with conn() as c:
        return jsonify({
            'customers':rows(c.execute('SELECT * FROM customers ORDER BY id DESC').fetchall()),
            'girls':rows(c.execute('SELECT * FROM girls ORDER BY id DESC').fetchall()),
            'orders':rows(c.execute('SELECT * FROM orders ORDER BY order_date DESC, id DESC').fetchall()),
            'recharges':rows(c.execute('SELECT * FROM recharge_records ORDER BY id DESC').fetchall()),
            'points':rows(c.execute('SELECT * FROM points_records ORDER BY id DESC').fetchall()),
            'enums':rows(c.execute('SELECT * FROM enum_values ORDER BY enum_type,sort_order,id').fetchall()),
            'schedules':rows(c.execute('SELECT * FROM girl_schedules ORDER BY schedule_date DESC,id DESC').fetchall()),
            'hotel_rooms':rows(c.execute('SELECT * FROM hotel_rooms ORDER BY hotel_name, room_no').fetchall()),
            'room_assignments':rows(c.execute('SELECT * FROM room_assignments ORDER BY assignment_date DESC, hotel_name, room_no').fetchall())})
@app.route('/api/customers',methods=['POST'])
def customers():
    d=request.json or {}
    with conn() as c:
        no=d.get('customer_no') or next_customer_no(c)
        if str(no).isdigit(): no=f'{int(no):04d}'
        vals=(no,d.get('name') or f'客户{no}',d.get('customer_type','普通'),d.get('customer_status','正常'),int(d.get('recharge_balance') or 0),int(d.get('total_recharge') or 0),int(d.get('total_spent') or 0),int(d.get('points') or 0),int(d.get('total_points') or 0),d.get('source',''),d.get('contact',''),d.get('grade',''),d.get('tags',''),d.get('member_level',''),d.get('remark',''),d.get('remark2',''))
        if d.get('id'):
            c.execute('''UPDATE customers SET customer_no=?,name=?,customer_type=?,customer_status=?,recharge_balance=?,total_recharge=?,total_spent=?,points=?,total_points=?,source=?,contact=?,grade=?,tags=?,member_level=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',vals+(d.get('id'),))
        else:
            c.execute('''INSERT INTO customers(customer_no,name,customer_type,customer_status,recharge_balance,total_recharge,total_spent,points,total_points,source,contact,grade,tags,member_level,remark,remark2) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',vals)
    return jsonify(ok=True)
@app.route('/api/girls',methods=['POST'])
def girls():
    d=request.json or {}
    with conn() as c:
        if d.get('id'):
            c.execute('''UPDATE girls SET name=?,girl_alias=?,girl_type=?,girl_status=?,take_home_per_hour=?,take_home_per_order=?,list_price=?,contact=?,tags=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type'),d.get('girl_status'),int(d.get('take_home_per_hour') or 0),int(d.get('take_home_per_order') or 0),int(d.get('list_price') or 0),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2',''),d.get('id')))
            recalc_girl(c,int(d['id']))
        else:
            c.execute('''INSERT OR IGNORE INTO girls(name,girl_alias,girl_type,girl_status,take_home_per_hour,take_home_per_order,list_price,contact,tags,remark,remark2) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type','普通'),d.get('girl_status','在职'),int(d.get('take_home_per_hour') or 0),int(d.get('take_home_per_order') or 0),int(d.get('list_price') or 0),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2','')))
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
    with conn() as c: c.execute(f'DELETE FROM {allowed[table]} WHERE id=?',(item_id,))
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
    - 23.30-0.30 会保存为 23.30-24.30
    - 0.30-1.30 会保存为 24.30-25.30
    这样凌晨 0 点以后不会被看成当天上午。
    """
    token = re.sub(r"\s+", "", str(token or ""))
    m = re.match(r"^(\d{1,2})([:.](\d{1,2}))?([-~ー～])(\d{1,2})([:.](\d{1,2}))?$", token)
    if not m:
        return token

    sh, ssep, sm, dash, eh, esep, em = m.groups()
    sh_i, eh_i = int(sh), int(eh)

    # 用户输入 0.30 时，夜场业务里按 24.30 处理。
    if sh_i == 0:
        sh_i += 24
    if eh_i == 0:
        eh_i += 24
    if eh_i < sh_i:
        eh_i += 24

    ssep = ssep or ''
    esep = esep or ''
    sm = sm or ''
    em = em or ''
    return f"{sh_i}{ssep}{sm}-{eh_i}{esep}{em}"

def parse_chain_service_time(line):
    body = strip_chain_prefix(line)
    if "包夜" in body:
        return "包夜 12.00-5.00", body
    # support 5.30-6.30, 10-12, 6.40-7.40, 8:45-9:45, 23.30-0.30
    m = re.search(r"(\d{1,2}(?:[:.]\d{1,2})?\s*[-~ー～]\s*\d{1,2}(?:[:.]\d{1,2})?)(.*)$", body)
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


@app.route("/api/db_info")
def api_db_info():
    init_db()
    with conn() as c:
        return jsonify({
            "db_path": str(DB_PATH),
            "customers_count": c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "girls_count": c.execute("SELECT COUNT(*) FROM girls").fetchone()[0],
            "orders_count": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "version": "v16_5_user_permission_stats_daily_only",
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
            "version": "v16_5_user_permission_stats_daily_only",
            "port": 5057,
            "db_path": str(DB_PATH),
            "customers_count": c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "girls_count": c.execute("SELECT COUNT(*) FROM girls").fetchone()[0],
            "orders_count": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        })

@app.route("/api/db_info")
def api_db_info_v10():
    init_db()
    with conn() as c:
        return jsonify({
            "ok": True,
            "version": "v16_5_user_permission_stats_daily_only",
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
    ids = [int(x) for x in (d.get('ids') or [])]
    status = d.get('settlement_status') or '已结算'
    if not ids:
        return jsonify(ok=False, error='没有选择订单'), 400
    q = ','.join(['?'] * len(ids))
    with conn() as c:
        c.execute(f"UPDATE orders SET settlement_status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN ({q})", [status] + ids)
    return jsonify(ok=True, updated=len(ids), settlement_status=status)



@app.route('/api/customers/recalc_points', methods=['POST'])
def api_customers_recalc_points():
    with conn() as c:
        recalc_customer_points(c, None)
    return jsonify(ok=True)



def sync_room_assignment_to_schedule(c, assignment_date, girl_id, hotel_name, room_no):
    if not girl_id:
        return
    girl = c.execute("SELECT * FROM girls WHERE id=?", (int(girl_id),)).fetchone()
    if not girl:
        return
    note = f"房间安排自动生成：{hotel_name}-{room_no}"
    old = c.execute("SELECT id FROM girl_schedules WHERE schedule_date=? AND girl_id=? AND note=?", (assignment_date, int(girl_id), note)).fetchone()
    price = int(girl['list_price'] or 0)
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

def open_browser(): webbrowser.open('http://127.0.0.1:5057')
import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port)
