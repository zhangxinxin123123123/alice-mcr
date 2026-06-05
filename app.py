
import re, math, sqlite3, webbrowser, threading, os
from datetime import date, datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
APP_DIR=Path(__file__).resolve().parent
DB_PATH=Path(os.environ.get('ALICE_DB_PATH') or ('/var/data/alice_academy_mcr.db' if Path('/var/data').exists() else str(APP_DIR/'alice_academy_mcr.db')))
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
            take_home_per_hour INTEGER DEFAULT 0, list_price INTEGER DEFAULT 0, contact TEXT DEFAULT '', tags TEXT DEFAULT '',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT, service_time TEXT, hours REAL DEFAULT 1,
            girl_id INTEGER, girl_name TEXT, customer_id INTEGER, customer_no TEXT, customer_name TEXT,
            received_amount INTEGER DEFAULT 0, girl_take_home INTEGER DEFAULT 0, store_profit INTEGER DEFAULT 0, points INTEGER DEFAULT 0,
            order_status TEXT DEFAULT '已结束', settlement_status TEXT DEFAULT '未结算', payment_method TEXT DEFAULT '现金',
            remark TEXT DEFAULT '', remark2 TEXT DEFAULT '', raw_text TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
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
        defaults=[('customer_type','新客',1),('customer_type','常客',2),('girl_type','普通',1),('girl_status','在职',1),('order_status','预约中',0),('order_status','已结束',1),('order_status','取消',2),('settlement_status','未结算',1),('settlement_status','已结算',2),('schedule_status','出勤',1),('schedule_status','休息',2),('customer_preference_tag','酒量好',1),('customer_preference_tag','喜欢聊天',2),('customer_preference_tag','喜欢新人',3),('customer_preference_tag','安静型',4)]
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
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
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




def parse_service_end_datetime(order_date, service_time):
    """把 23.30-0.30 / 20:00-21:00 这类预约时间转换为结束 datetime；凌晨自动按次日处理。"""
    try:
        base = datetime.strptime(str(order_date), "%Y-%m-%d")
    except Exception:
        return None
    text = str(service_time or "")
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*[-~ー～]\s*(\d{1,2})(?:[:.](\d{1,2}))?", text)
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
    # 统一按“小时数 × 女生表中的每小时到手”计算女孩真实到手。
    hr=int(g['take_home_per_hour'] or 0)
    return int(round(hr*float(hours or 1)))
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
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),(d.get('payment_method') or '现金'),d.get('remark',''),d.get('remark2',''),d.get('id')))
        if old_customer_id and old_customer_id != cust['id']:
            recalc_customer_points(c, old_customer_id)
        recalc_customer_points(c, cust['id'])
    else:
        c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,customer_no,customer_name,received_amount,girl_take_home,store_profit,points,order_status,settlement_status,payment_method,remark,remark2,raw_text)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (d.get('order_date'),d.get('service_time'),h,g['id'],g['name'],cust['id'],cust['customer_no'],cust['name'],rec,th,prof,pts,d.get('order_status','已结束'),d.get('settlement_status','未结算'),(d.get('payment_method') or '现金'),d.get('remark',''),d.get('remark2',''),d.get('raw_text','')))
        recalc_customer_points(c, cust['id'])


@app.route('/')
def index(): return send_from_directory(APP_DIR/'static','index.html')
@app.route('/api/all')
def all_data():
    init_db()
    with conn() as c:
        auto_finish_reservations(c)
        return jsonify({
            'customers':rows(c.execute('''SELECT c.*, COALESCE(o.total_orders,0) AS total_orders, COALESCE(o.total_spent, c.total_spent, 0) AS total_spent FROM customers c LEFT JOIN (SELECT customer_id, COUNT(*) AS total_orders, SUM(received_amount) AS total_spent FROM orders GROUP BY customer_id) o ON o.customer_id=c.id ORDER BY c.id DESC''').fetchall()),
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
            c.execute('''UPDATE girls SET name=?,girl_alias=?,girl_type=?,girl_status=?,take_home_per_hour=?,list_price=?,contact=?,tags=?,remark=?,remark2=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type'),d.get('girl_status'),int(d.get('take_home_per_hour') or 0),int(d.get('list_price') or 0),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2',''),d.get('id')))
            recalc_girl(c,int(d['id']))
        else:
            c.execute('''INSERT OR IGNORE INTO girls(name,girl_alias,girl_type,girl_status,take_home_per_hour,list_price,contact,tags,remark,remark2) VALUES(?,?,?,?,?,?,?,?,?,?)''',(d.get('name'),d.get('girl_alias',''),d.get('girl_type','普通'),d.get('girl_status','在职'),int(d.get('take_home_per_hour') or 0),int(d.get('list_price') or 0),d.get('contact',''),d.get('tags',''),d.get('remark',''),d.get('remark2','')))
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
        parts = [service_time, str(price)]
        if remark:
            parts.append(remark)
    return f"{idx}.{ '/'.join([p for p in parts if p != '']) }"

@app.route('/api/chain_page', methods=['POST'])
def api_chain_page():
    init_db()
    d = request.json or {}
    date_str = d.get('date') or str(date.today())
    girl_name = str(d.get('girl_name') or '').strip()
    with conn() as c:
        auto_finish_reservations(c)
        shifts = pure_shift_rows_for_date(c, date_str)
        # 给每个纯出勤女孩补上女孩表价格/ID，接龙预约用这个自动定价。
        out_shifts = []
        for sft in shifts:
            g = c.execute('SELECT * FROM girls WHERE name=?', (sft.get('girl') or '',)).fetchone()
            row = dict(sft)
            row['girl_id'] = g['id'] if g else 0
            row['price'] = int((g['list_price'] if g else 0) or 0)
            out_shifts.append(row)
        if not girl_name and out_shifts:
            girl_name = out_shifts[0]['girl']
        orders = rows(c.execute("""SELECT * FROM orders
            WHERE order_date=? AND girl_name=?
            ORDER BY service_time ASC, id ASC""", (date_str, girl_name)).fetchall()) if girl_name else []
        return jsonify(ok=True, date=date_str, girl_name=girl_name, shifts=out_shifts, orders=orders)

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
        amount = int(d.get('received_amount') or (g['list_price'] or 0) or 0)
        create_or_update_order(c, {
            'id': d.get('id') or None,
            'order_date': date_str,
            'service_time': normalize_chain_time_token(d.get('service_time') or ''),
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
        return jsonify(ok=True, full='\n'.join(full_lines), basic='\n'.join(basic_lines), count=len(orders))


@app.route("/api/db_info")
def api_db_info():
    init_db()
    with conn() as c:
        return jsonify({
            "db_path": str(DB_PATH),
            "customers_count": c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "girls_count": c.execute("SELECT COUNT(*) FROM girls").fetchone()[0],
            "orders_count": c.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "version": "v17_chain_reservation",
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
            "version": "v17_chain_reservation",
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

def open_browser(): webbrowser.open('http://127.0.0.1:5057')
import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port)
