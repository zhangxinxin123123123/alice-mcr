import base64
import json
import os
import re
import secrets
import threading
import time
from io import BytesIO
from pathlib import Path
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import jsonify, request
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


DEFAULT_SETTINGS = {
    "bot_display_name": "爱丽丝预约 Bot",
    "welcome_text": "欢迎来到爱丽丝！\n\n点击下方按钮开始预约。",
    "website_url": "https://ailisi99.com/",
    "hotel_url": "",
    "booking_enabled": "1",
    "hotel_upload_timeout_seconds": "120",
    "default_review_chat_id": "",
    "default_review_chat_title": "",
    "default_review_thread_id": "0",
    "support_username": "",
    "button_start": "开始预约",
    "button_support": "人工客服",
    "button_website": "官方网站",
    "button_hotel": "推荐酒店",
    "button_today": "今天",
    "button_tomorrow": "明天",
    "button_home": "🏠 返回首页",
    "button_restart": "重新开始预约",
    "button_back": "⬅️ 返回上一层",
    "button_cancel": "❌ 取消预约",
    "button_confirm": "✅ 确定预约",
    "button_change_time": "⬅️ 修改时间",
    "button_reselect_girl": "重新选女孩",
    "text_choose_date": "请选择预约日期：",
    "text_choose_girl": "{date} 可预约女孩：",
    "text_time_prompt": "你选择了 <b>{girl}</b>。\n\n可预约：{free_time}\n\n请发送时间，例如：<code>19-20</code>、<code>19:30-21:00</code>。",
    "text_confirm": "请确认预约：\n\n女孩：<b>{girl}</b>\n日期：{date}\n时间：<b>{start_time}-{end_time}</b>",
    "text_submitted": "预约已经交给店长审核，请稍等。",
    "text_booking_success": "🎀 太好啦，{girl} 已经为你留好啦～\n\n预约时间：{date} {start_time}-{end_time}\n\n开好酒店后，请点击下方“发送酒店信息”，把酒店名称、地址、房号、截图或定位发给我们。",
    "text_cancel_policy": "🌸 温馨提醒\n为了把珍贵的预约时间留给真正有需要的客人：第一次取消预约，会清空当前累计积分；第二次取消预约，将暂停后续预约资格并加入黑名单。\n\n如果行程可能有变化，请尽早联系人工客服，我们会温柔地帮你协调改期。谢谢你的理解与珍惜～",
    "button_send_hotel": "🏨 发送酒店信息",
    "button_reschedule": "📅 申请改期",
    "points_yen_per_point": "1",
    "auto_chain_import_enabled": "1",
    "auto_chain_import_interval_minutes": "30",
}


def closing_business_date(now):
    """深夜营业跨日：00:00–03:59 的下班结算仍属于前一个营业日。"""
    current = now or datetime.now()
    return (current.date() - timedelta(days=1) if current.hour < 4 else current.date()).isoformat()


def auto_import_candidate_dates(now):
    """00:00–03:59 同时接收当天和前一天接龙；其余时间只接收当天。"""
    current = now or datetime.now()
    days = [current.date()]
    if current.hour < 4:
        days.append(current.date() - timedelta(days=1))
    return days


def register_telegram_booking(
    app,
    conn,
    init_main_db,
    pure_shift_rows_for_date,
    create_or_update_order,
    time_to_min,
    min_to_time,
    service_range_minutes,
    ranges_overlap,
    tokyo_now,
    current_business_minute_for_date,
    mcr_girl_free_ranges,
    import_chain_text,
    order_to_chain_line,
    ensure_customer,
    refresh_customer_totals,
    sync_wordpress_attendance=None,
    parse_chain_header=None,
):
    def ensure_db():
        init_main_db()
        with conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_settings(
                setting_key TEXT PRIMARY KEY, setting_value TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_group_bindings(
                girl_name TEXT PRIMARY KEY, chat_id TEXT NOT NULL, chat_title TEXT DEFAULT '',
                message_thread_id INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_managers(
                user_id TEXT PRIMARY KEY, username TEXT DEFAULT '', display_name TEXT DEFAULT '', active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_booking_sessions(
                user_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, step TEXT DEFAULT '', payload TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_daily_girls(
                booking_date TEXT NOT NULL, girl_name TEXT NOT NULL, sort_order INTEGER DEFAULT 0,
                source TEXT DEFAULT 'manual', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(booking_date,girl_name))""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_daily_chain_messages(
                booking_date TEXT NOT NULL, girl_name TEXT NOT NULL, chat_id TEXT NOT NULL,
                message_thread_id INTEGER DEFAULT 0, message_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(booking_date,girl_name,chat_id,message_thread_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_customers(
                telegram_user_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL,
                telegram_username TEXT DEFAULT '', display_name TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_chain_inbox(
                chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, chat_title TEXT DEFAULT '',
                message_thread_id INTEGER DEFAULT 0, chain_text TEXT NOT NULL,
                order_date TEXT DEFAULT '', girl_name TEXT DEFAULT '', status TEXT DEFAULT 'pending',
                last_error TEXT DEFAULT '', received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chat_id,message_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tg_chain_inbox_pending ON telegram_chain_inbox(status,order_date,girl_name)")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_attendance_inquiries(
                id INTEGER PRIMARY KEY AUTOINCREMENT, inquiry_date TEXT NOT NULL, girl_name TEXT NOT NULL,
                chat_id TEXT NOT NULL, chat_title TEXT DEFAULT '', message_thread_id INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '', responder_user_id TEXT DEFAULT '', responder_name TEXT DEFAULT '',
                expires_at TEXT NOT NULL, requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
                responded_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(inquiry_date,girl_name))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tg_attendance_expiry ON telegram_attendance_inquiries(status,expires_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_closing_confirmations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, close_date TEXT NOT NULL, girl_name TEXT NOT NULL,
                chat_id TEXT NOT NULL, chat_title TEXT DEFAULT '', message_thread_id INTEGER DEFAULT 0,
                trigger_source TEXT DEFAULT 'keyword', order_count INTEGER DEFAULT 0,
                girl_earnings INTEGER DEFAULT 0, store_profit INTEGER DEFAULT 0,
                non_cash_received INTEGER DEFAULT 0, amount_due INTEGER DEFAULT 0,
                settlement_method TEXT DEFAULT '', status TEXT DEFAULT 'await_payment',
                summary_message_id INTEGER DEFAULT 0, attendance_prompt_message_id INTEGER DEFAULT 0,
                confirmed_user_id TEXT DEFAULT '', confirmed_name TEXT DEFAULT '',
                next_attendance_text TEXT DEFAULT '', next_attendance_date TEXT DEFAULT '',
                next_start_time TEXT DEFAULT '', next_end_time TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, confirmed_at TEXT, completed_at TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(close_date,girl_name))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tg_closing_status ON telegram_closing_confirmations(close_date,status)")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_customer_digests(
                digest_date TEXT PRIMARY KEY, customer_count INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0, sent_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_full_sync_days(
                sync_date TEXT PRIMARY KEY, full_synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                late_auto_enabled INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS telegram_chain_sync_state(
                id INTEGER PRIMARY KEY CHECK(id=1), last_started_at TEXT, last_completed_at TEXT,
                last_result TEXT DEFAULT '')""")
            c.execute("INSERT OR IGNORE INTO telegram_chain_sync_state(id) VALUES(1)")
            for key, value in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO telegram_settings(setting_key,setting_value) VALUES(?,?)", (key, value))
            cols = [r[1] for r in c.execute("PRAGMA table_info(customer_reservations)").fetchall()]
            additions = {
                "telegram_user_id": "TEXT DEFAULT ''",
                "telegram_chat_id": "TEXT DEFAULT ''",
                "telegram_username": "TEXT DEFAULT ''",
                "telegram_group_chat_id": "TEXT DEFAULT ''",
                "telegram_message_id": "INTEGER DEFAULT 0",
                "hotel_file_id": "TEXT DEFAULT ''",
                "hotel_caption": "TEXT DEFAULT ''",
                "customer_id": "INTEGER DEFAULT 0",
                "points_available": "INTEGER DEFAULT 0",
                "points_used": "INTEGER DEFAULT 0",
                "actual_payment": "INTEGER DEFAULT 0",
                "telegram_chain_chat_id": "TEXT DEFAULT ''",
                "telegram_chain_message_id": "INTEGER DEFAULT 0",
                "telegram_hotel_chat_id": "TEXT DEFAULT ''",
                "telegram_hotel_message_ids": "TEXT DEFAULT ''",
            }
            for name, sql_type in additions.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE customer_reservations ADD COLUMN {name} {sql_type}")

    def settings(c=None):
        own = c is None
        c = c or conn()
        try:
            result = dict(DEFAULT_SETTINGS)
            for row in c.execute("SELECT setting_key,setting_value FROM telegram_settings").fetchall():
                result[row["setting_key"]] = row["setting_value"]
            return result
        finally:
            if own:
                c.close()

    def telegram_token():
        return str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

    def tg(method, data=None, timeout=20):
        token = telegram_token()
        if not token:
            raise RuntimeError("服务器尚未配置 TELEGRAM_BOT_TOKEN")
        payload = urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in (data or {}).items() if v is not None}).encode("utf-8")
        req = Request(f"https://api.telegram.org/bot{token}/{method}", data=payload,
                      headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        with urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or f"Telegram {method} 调用失败")
        return result.get("result")

    def send_photo_bytes(chat_id, image_bytes, caption="", thread_id=0):
        token = telegram_token()
        if not token:
            raise RuntimeError("服务器尚未配置 TELEGRAM_BOT_TOKEN")
        boundary = "AliceBoundary" + secrets.token_hex(12)
        chunks = []
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if int(thread_id or 0):
            fields["message_thread_id"] = str(int(thread_id))
        for key, value in fields.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8"))
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"alice-report.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
        chunks.append(image_bytes)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        req = Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=b"".join(chunks),
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=40) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or "Telegram 图片发送失败")
        return result.get("result")

    def send_document_bytes(chat_id, image_bytes, caption="", thread_id=0, filename="alice-report.png"):
        """以 PNG 文件发送，保留原始像素，避免 Telegram 照片压缩导致文字发白或模糊。"""
        token = telegram_token()
        if not token:
            raise RuntimeError("服务器尚未配置 TELEGRAM_BOT_TOKEN")
        boundary = "AliceBoundary" + secrets.token_hex(12)
        chunks = []
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if int(thread_id or 0):
            fields["message_thread_id"] = str(int(thread_id))
        for key, value in fields.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8"))
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "-", str(filename or "alice-report.png"))
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{safe_filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
        chunks.append(image_bytes)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        req = Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(chunks),
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=40) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("description") or "Telegram 高清原图发送失败")
        return result.get("result")

    def inline_keyboard(rows):
        return {"inline_keyboard": rows}

    def url_button(text, url):
        return {"text": text, "url": url}

    def callback_button(text, data):
        return {"text": text, "callback_data": data}

    def render_text(template, **values):
        result = str(template or "")
        for key, value in values.items():
            result = result.replace("{" + key + "}", str(value))
        return result

    def valid_group_chat_id(value):
        return bool(re.fullmatch(r"-\d+", str(value or "").strip()))

    def support_url_button(cfg):
        username = str(cfg.get("support_username") or "").strip().lstrip("@")
        return url_button(cfg.get("button_support") or "人工客服", f"https://t.me/{username}") if username else None

    def send_message(chat_id, text, keyboard=None, thread_id=0):
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            data["reply_markup"] = keyboard
        if int(thread_id or 0):
            data["message_thread_id"] = int(thread_id)
        return tg("sendMessage", data)

    def edit_message_text(chat_id, message_id, text, keyboard=None):
        data = {"chat_id": chat_id, "message_id": int(message_id), "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            data["reply_markup"] = keyboard
        return tg("editMessageText", data)

    def answer_callback(callback_id, text="", alert=False):
        try:
            tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": alert})
        except Exception:
            pass

    def set_session(user_id, chat_id, step, payload=None):
        with conn() as c:
            c.execute("""INSERT INTO telegram_booking_sessions(user_id,chat_id,step,payload,updated_at)
                         VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,step=excluded.step,
                         payload=excluded.payload,updated_at=CURRENT_TIMESTAMP""",
                      (str(user_id), str(chat_id), step, json.dumps(payload or {}, ensure_ascii=False)))

    def get_session(user_id):
        with conn() as c:
            row = c.execute("SELECT * FROM telegram_booking_sessions WHERE user_id=?", (str(user_id),)).fetchone()
            if not row:
                return None
            out = dict(row)
            try:
                out["payload"] = json.loads(out.get("payload") or "{}")
            except Exception:
                out["payload"] = {}
            return out

    def clear_session(user_id):
        with conn() as c:
            c.execute("DELETE FROM telegram_booking_sessions WHERE user_id=?", (str(user_id),))

    def display_name(user):
        return " ".join(x for x in [user.get("first_name", ""), user.get("last_name", "")] if x).strip() or user.get("username") or str(user.get("id"))

    def is_chat_admin(chat_id, user_id):
        try:
            member = tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})
            return member.get("status") in ("creator", "administrator")
        except Exception:
            return False

    def is_manager(user_id, chat_id=None):
        with conn() as c:
            count = c.execute("SELECT COUNT(*) FROM telegram_managers WHERE active=1").fetchone()[0]
            row = c.execute("SELECT 1 FROM telegram_managers WHERE user_id=? AND active=1", (str(user_id),)).fetchone()
        if row:
            return True
        return count == 0 and chat_id is not None and is_chat_admin(chat_id, user_id)

    def save_manager(user):
        with conn() as c:
            c.execute("""INSERT INTO telegram_managers(user_id,username,display_name,active,updated_at)
                         VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,
                         active=1,updated_at=CURRENT_TIMESTAMP""",
                      (str(user.get("id")), user.get("username") or "", display_name(user), 1))

    def eligible_girls(day):
        with conn() as c:
            cfg = settings(c)
            bindings = {r["girl_name"]: dict(r) for r in c.execute(
                "SELECT * FROM telegram_group_bindings WHERE enabled=1").fetchall()
                        if valid_group_chat_id(r["chat_id"])}
            girls = {r["name"]: dict(r) for r in c.execute(
                "SELECT * FROM girls WHERE COALESCE(girl_status,'在职')='在职'").fetchall()}
            selected = [r["girl_name"] for r in c.execute(
                "SELECT girl_name FROM telegram_daily_girls WHERE booking_date=? ORDER BY sort_order,girl_name",
                (day,)).fetchall()]
            shifts = {}
            for shift in pure_shift_rows_for_date(c, day):
                name = str(shift.get("girl") or "").strip()
                if name and name not in shifts:
                    shifts[name] = shift
            result = []
            for name in selected:
                shift = shifts.get(name)
                if not shift or name not in girls:
                    continue
                binding = bindings.get(name)
                if not binding and cfg.get("default_review_chat_id"):
                    binding = {
                        "girl_name": "*", "chat_id": cfg["default_review_chat_id"],
                        "chat_title": cfg.get("default_review_chat_title") or "默认审核群",
                        "message_thread_id": int(cfg.get("default_review_thread_id") or 0), "enabled": 1,
                    }
                result.append({"girl": name, "shift": shift, "binding": binding, "profile": girls[name]})
            return result

    def free_ranges(c, day, girl, shift, exclude_reservation_id=0):
        return mcr_girl_free_ranges(
            c, day, girl, shift,
            exclude_reservation_id=exclude_reservation_id,
            client_now=tokyo_now(),
        )

    def show_home(chat_id):
        cfg = settings()
        rows = [[callback_button(cfg.get("button_start") or "开始预约", "book")]]
        links = []
        if cfg.get("website_url"):
            links.append(url_button(cfg.get("button_website") or "官方网站", cfg["website_url"]))
        if cfg.get("hotel_url"):
            links.append(url_button(cfg.get("button_hotel") or "推荐酒店", cfg["hotel_url"]))
        if links:
            rows.append(links)
        support = support_url_button(cfg)
        if support:
            rows.append([support])
        send_message(chat_id, cfg.get("welcome_text") or DEFAULT_SETTINGS["welcome_text"], inline_keyboard(rows))

    def flow_keyboard(back_data=None, back_text="⬅️ 返回上一层"):
        cfg = settings()
        rows = []
        if back_data:
            rows.append([callback_button(back_text or cfg.get("button_back") or "⬅️ 返回上一层", back_data)])
        rows.append([callback_button(cfg.get("button_cancel") or "❌ 取消预约", "flow:cancel")])
        support = support_url_button(cfg)
        if support:
            rows.append([support])
        return inline_keyboard(rows)

    def show_dates(chat_id, user_id):
        cfg = settings()
        if cfg.get("booking_enabled") != "1":
            send_message(chat_id, "目前预约功能暂时关闭，请稍后再试。")
            return
        with conn() as c:
            blocked = c.execute("""SELECT COUNT(*) AS n FROM telegram_customer_cancellations
                                   WHERE telegram_user_id=?""", (str(user_id),)).fetchone()
            linked = c.execute("""SELECT c.customer_status FROM telegram_customers t
                                  JOIN customers c ON c.id=t.customer_id WHERE t.telegram_user_id=?""",
                               (str(user_id),)).fetchone()
        if int(blocked['n'] or 0) >= 2 or (linked and linked['customer_status'] == '黑名单'):
            rows = []
            support = support_url_button(cfg)
            if support:
                rows.append([support])
            send_message(chat_id, "这个账号目前已暂停自助预约。如需协助，请联系人工客服。",
                         inline_keyboard(rows) if rows else None)
            return
        now = tokyo_now()
        buttons = []
        for offset in range(0, 2):
            day = now.date() + timedelta(days=offset)
            label = (cfg.get("button_today") or "今天") if offset == 0 else (cfg.get("button_tomorrow") or "明天")
            buttons.append([callback_button(f"{label} {day.month}/{day.day}", f"date:{day.isoformat()}")])
        buttons.append([callback_button(cfg.get("button_back") or "⬅️ 返回上一层", "flow:home"),
                        callback_button(cfg.get("button_cancel") or "❌ 取消预约", "flow:cancel")])
        support = support_url_button(cfg)
        if support:
            buttons.append([support])
        set_session(user_id, chat_id, "choose_date", {})
        send_message(chat_id, cfg.get("text_choose_date") or "请选择预约日期：", inline_keyboard(buttons))

    def show_girls(chat_id, user_id, day):
        cfg = settings()
        girls = eligible_girls(day)
        girl_rows = []
        with conn() as c:
            for item in girls:
                free = free_ranges(c, day, item['girl'], item['shift'])
                girl_rows.append((item, free))
        if not girl_rows:
            send_message(chat_id, "这一天暂时没有开放 Bot 预约的女孩。",
                         flow_keyboard("flow:dates", "⬅️ 重新选择日期"))
            return
        rows = []
        for item, free in girl_rows:
            if free:
                free_text = '、'.join(f"{min_to_time(a)}-{min_to_time(b)}" for a, b in free)
                callback_data = f"girl:{day}:{item['profile']['id']}"
            else:
                free_text = "已满"
                callback_data = f"full:{day}:{item['profile']['id']}"
            rows.append([callback_button(f"{item['girl']}｜{free_text}", callback_data)])
        rows.append([callback_button(cfg.get("button_back") or "⬅️ 返回上一层", "flow:dates"),
                     callback_button(cfg.get("button_cancel") or "❌ 取消预约", "flow:cancel")])
        support = support_url_button(cfg)
        if support:
            rows.append([support])
        set_session(user_id, chat_id, "choose_girl", {"date": day})
        send_message(chat_id, render_text(cfg.get("text_choose_girl"), date=escape(day)), inline_keyboard(rows))

    def choose_girl(chat_id, user_id, day, girl_ref):
        item = next((x for x in eligible_girls(day)
                     if str(x["profile"].get("id")) == str(girl_ref) or x["girl"] == girl_ref), None)
        if not item:
            send_message(chat_id, "该女孩当前没有开放预约，请重新选择。")
            show_girls(chat_id, user_id, day)
            return
        girl = item["girl"]
        with conn() as c:
            free = free_ranges(c, day, girl, item["shift"])
        if not free:
            send_message(chat_id, f"{girl} 当天已经没有空闲时间。",
                         flow_keyboard(f"flow:girls:{day}", "⬅️ 重新选择女孩"))
            return
        free_text = "、".join(f"{min_to_time(a)}-{min_to_time(b)}" for a, b in free)
        set_session(user_id, chat_id, "await_time", {"date": day, "girl": girl})
        cfg = settings()
        send_message(chat_id, render_text(cfg.get("text_time_prompt"), girl=escape(girl), free_time=escape(free_text)),
                     flow_keyboard(f"flow:girls:{day}", "⬅️ 重新选择女孩"))

    def parse_time_text(text):
        if "包夜" in str(text or ""):
            return "00:00", "05:00"
        m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?\s*(?:-|~|～|—|到|至)\s*(\d{1,2})(?:[:.](\d{1,2}))?", str(text or ""))
        if not m:
            return None
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
        if h1 > 29 or h2 > 29 or m1 > 59 or m2 > 59:
            return None
        return f"{h1 % 24:02d}:{m1:02d}", f"{h2 % 24:02d}:{m2:02d}"

    def submit_reservation(message, session, confirmed=False):
        user, chat = message.get("from") or {}, message.get("chat") or {}
        parsed = parse_time_text(message.get("text") or "")
        if not parsed:
            day = session["payload"].get("date")
            send_message(chat.get("id"), "时间格式没有看懂，请按 <code>19-20</code> 或 <code>19:30-21:00</code> 发送。",
                         flow_keyboard(f"flow:girls:{day}", "⬅️ 重新选择女孩"))
            return
        start_text, end_text = parsed
        day, girl = session["payload"].get("date"), session["payload"].get("girl")
        item = next((x for x in eligible_girls(day) if x["girl"] == girl), None)
        if not item:
            clear_session(user.get("id"))
            send_message(chat.get("id"), "该女孩已经停止开放预约，请重新选择。")
            return
        cfg = settings()
        if not valid_group_chat_id(cfg.get("default_review_chat_id")):
            send_message(chat.get("id"), "内部审核群尚未正确绑定，请联系店长。",
                         flow_keyboard(f"flow:girls:{day}", "⬅️ 重新选择女孩"))
            return
        a, b = time_to_min(start_text), time_to_min(end_text)
        if a is None or b is None:
            send_message(chat.get("id"), "时间格式错误，请重新发送。")
            return
        if b <= a:
            b += 24 * 60
        with conn() as c:
            free = free_ranges(c, day, girl, item["shift"])
            if not any(a >= fa and b <= fb for fa, fb in free):
                free_text = "、".join(f"{min_to_time(fa)}-{min_to_time(fb)}" for fa, fb in free) or "无"
                send_message(chat.get("id"), f"这个时间当前不可预约。可预约时间：{escape(free_text)}",
                             flow_keyboard(f"flow:girls:{day}", "⬅️ 重新选择女孩"))
                return
            if not confirmed:
                set_session(user.get("id"), chat.get("id"), "confirm_time", {
                    "date": day, "girl": girl, "start_time": start_text, "end_time": end_text,
                })
                keyboard = inline_keyboard([
                    [callback_button(cfg.get("button_confirm") or "✅ 确定预约", "flow:confirm")],
                    [callback_button(cfg.get("button_change_time") or "⬅️ 修改时间", f"flow:time:{day}:{item['profile']['id']}"),
                     callback_button(cfg.get("button_reselect_girl") or "重新选女孩", f"flow:girls:{day}")],
                    [callback_button(cfg.get("button_cancel") or "❌ 取消预约", "flow:cancel")],
                ] + ([[support_url_button(cfg)]] if support_url_button(cfg) else []))
                send_message(chat.get("id"),
                             render_text(cfg.get("text_confirm"), girl=escape(girl), date=escape(day),
                                         start_time=escape(start_text), end_time=escape(end_text)),
                             keyboard)
                return
            price = int(item["profile"].get("list_price") or 15000)
            cur = c.execute("""INSERT INTO customer_reservations(
                reserve_date,girl_name,start_time,end_time,username,status,price,note,
                telegram_user_id,telegram_chat_id,telegram_username,telegram_group_chat_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (day, girl, start_text, end_text, display_name(user), "待确认", price, "Telegram Bot 预约",
                 str(user.get("id")), str(chat.get("id")), user.get("username") or "", str(item["binding"]["chat_id"])))
            rid = int(cur.lastrowid)
        group_text = (f"📋 <b>新的预约申请 #{rid}</b>\n\n"
                      f"女孩：<b>{escape(girl)}</b>\n日期：{escape(day)}\n时间：{escape(start_text)}-{escape(end_text)}\n"
                      f"客人：{escape(display_name(user))}\nTelegram ID：<code>{user.get('id')}</code>")
        customer_username = str(user.get("username") or "").strip().lstrip("@")
        customer_url = f"https://t.me/{customer_username}" if customer_username else f"tg://user?id={user.get('id')}"
        try:
            sent = send_message(cfg["default_review_chat_id"], group_text,
                                inline_keyboard([
                                    [callback_button("✅ 店长批准", f"approve:{rid}"), callback_button("❌ 拒绝", f"reject:{rid}")],
                                    [url_button("💬 一键联系客户", customer_url)],
                                ]),
                                int(cfg.get("default_review_thread_id") or 0))
            with conn() as c:
                c.execute("UPDATE customer_reservations SET telegram_message_id=? WHERE id=?", (int(sent.get("message_id") or 0), rid))
        except Exception as exc:
            with conn() as c:
                c.execute("UPDATE customer_reservations SET status='发送失败',note=? WHERE id=?", (f"Telegram 内部审核群发送失败：{exc}", rid))
            send_message(chat.get("id"), "预约没有成功发送到内部审核群，请联系人工客服。")
            clear_session(user.get("id"))
            return
        clear_session(user.get("id"))
        submitted_rows = [[callback_button(cfg.get("button_home") or "🏠 返回首页", "flow:home")]]
        support = support_url_button(cfg)
        if support:
            submitted_rows.append([support])
        send_message(chat.get("id"), cfg.get("text_submitted") or "预约已经交给店长审核，请稍等。",
                     inline_keyboard(submitted_rows))

    def target_thread_id(row, cfg):
        target = str(row.get("telegram_group_chat_id") or cfg.get("default_review_chat_id") or "")
        thread_id = int(cfg.get("default_review_thread_id") or 0) if target == str(cfg.get("default_review_chat_id") or "") else 0
        with conn() as c:
            binding = c.execute("""SELECT * FROM telegram_group_bindings
                                  WHERE girl_name=? AND enabled=1 AND chat_id=?""",
                                (row["girl_name"], target)).fetchone()
            if binding:
                thread_id = int(binding["message_thread_id"] or 0)
        return target, thread_id

    def refresh_daily_chain(row, cfg, new_order_id=0, force_new=False):
        """Create or update the girl's single daily chain message from current MCR orders."""
        target, thread_id = target_thread_id(row, cfg)
        day = str(row["reserve_date"])
        girl = str(row["girl_name"])
        with conn() as c:
            orders = [dict(x) for x in c.execute("""SELECT * FROM orders
                                                   WHERE order_date=? AND girl_name=?
                                                     AND COALESCE(order_status,'')!='取消'
                                                   ORDER BY id""", (day, girl)).fetchall()]
            contacts = {int(x["order_id"]): dict(x) for x in c.execute("""SELECT order_id,telegram_user_id,telegram_username
                                                                           FROM customer_reservations
                                                                           WHERE reserve_date=? AND girl_name=?
                                                                             AND status='已确认'
                                                                             AND COALESCE(order_id,0)>0""",
                                                                        (day, girl)).fetchall()}
            first_order_ids = {int(x["customer_id"]): int(x["first_order_id"])
                               for x in c.execute("""SELECT customer_id,MIN(id) AS first_order_id
                                                      FROM orders
                                                      WHERE COALESCE(customer_id,0)>0
                                                        AND COALESCE(order_status,'')!='取消'
                                                      GROUP BY customer_id""").fetchall()}
            registry = c.execute("""SELECT message_id FROM telegram_daily_chain_messages
                                    WHERE booking_date=? AND girl_name=? AND chat_id=?
                                      AND message_thread_id=?""",
                                 (day, girl, str(target), int(thread_id or 0))).fetchone()
            message_id = int(registry["message_id"] or 0) if registry else 0
            if force_new:
                message_id = 0
            if not message_id and not force_new:
                previous = c.execute("""SELECT telegram_chain_message_id FROM customer_reservations
                                        WHERE reserve_date=? AND girl_name=? AND telegram_chain_chat_id=?
                                          AND COALESCE(telegram_chain_message_id,0)>0
                                        ORDER BY id DESC LIMIT 1""",
                                     (day, girl, str(target))).fetchone()
                message_id = int(previous["telegram_chain_message_id"] or 0) if previous else 0

        def order_sort_key(order):
            period = service_range_minutes(order.get("service_time"))
            return (period[0] if period else 99 * 60, int(order.get("id") or 0))

        orders.sort(key=order_sort_key)
        dt = datetime.strptime(day, "%Y-%m-%d")
        lines = [f"<b>{escape(f'{dt.month}月{dt.day}日 {girl} 接龙')}</b>"]
        if not orders:
            lines.append("暂无预约")
        contact_rows = []
        for index, order in enumerate(orders, 1):
            display_order = dict(order)
            remark = str(display_order.get("remark") or "").strip()
            display_order["remark"] = ""
            chain_line = order_to_chain_line(display_order, index, False)
            customer_id = int(order.get("customer_id") or 0)
            customer_name = str(order.get("customer_name") or "").strip()
            customer_no = str(order.get("customer_no") or "").strip()
            if customer_id:
                first_order_id = first_order_ids.get(customer_id)
                customer_label = customer_name if int(order.get("id") or 0) == int(first_order_id or 0) else customer_no
                if customer_label:
                    chain_line += "/" + customer_label
                if remark and not remark.startswith("Telegram Bot 预约") and remark not in (customer_name, customer_no):
                    chain_line += "/" + remark
            elif remark:
                chain_line += "/" + remark
            chain_line = escape(chain_line)
            if int(order.get("id") or 0) == int(new_order_id or 0):
                # Telegram 不支持自定义文字颜色，用彩色标记和粗体突出本次新增。
                lines.append(f"🟣 <b>NEW｜{chain_line}</b>")
            else:
                lines.append(f"▫️ {chain_line}")
            contact = contacts.get(int(order.get("id") or 0))
            if contact:
                telegram_username = str(contact.get("telegram_username") or "").strip().lstrip("@")
                telegram_user_id = str(contact.get("telegram_user_id") or "").strip()
                if telegram_username:
                    contact_url = f"https://t.me/{telegram_username}"
                elif telegram_user_id.isdigit():
                    contact_url = f"tg://user?id={telegram_user_id}"
                else:
                    contact_url = ""
                if contact_url:
                    contact_rows.append([url_button(f"💬 联系第 {index} 位客户", contact_url)])
        text = "\n".join(lines)
        keyboard = inline_keyboard(contact_rows) if contact_rows else None

        sent_message_id = message_id
        if message_id:
            try:
                edit_message_text(target, message_id, text, keyboard)
            except Exception:
                sent = send_message(target, text, keyboard, thread_id=thread_id)
                sent_message_id = int(sent.get("message_id") or 0)
        else:
            sent = send_message(target, text, keyboard, thread_id=thread_id)
            sent_message_id = int(sent.get("message_id") or 0)

        with conn() as c:
            c.execute("""INSERT INTO telegram_daily_chain_messages(
                            booking_date,girl_name,chat_id,message_thread_id,message_id,updated_at)
                         VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(booking_date,girl_name,chat_id,message_thread_id) DO UPDATE SET
                            message_id=excluded.message_id,updated_at=CURRENT_TIMESTAMP""",
                      (day, girl, str(target), int(thread_id or 0), int(sent_message_id)))
            c.execute("""UPDATE customer_reservations
                         SET telegram_chain_chat_id=?,telegram_chain_message_id=?,updated_at=CURRENT_TIMESTAMP
                         WHERE reserve_date=? AND girl_name=? AND telegram_group_chat_id=?""",
                      (str(target), int(sent_message_id), day, girl, str(target)))
        return sent_message_id

    def send_approved_chain(row, order_row, cfg, review_chat_id):
        try:
            refresh_daily_chain(row, cfg, int(order_row.get("id") or 0))
        except Exception as exc:
            send_message(review_chat_id, f"⚠️ 订单已建立，但接龙发送失败：{escape(str(exc))}")

    def booking_action_keyboard(row, cfg):
        rid = int(row['id'])
        rows = [[callback_button(cfg.get('button_send_hotel') or '🏨 发送酒店信息', f'hotel:{rid}')]]
        support = support_url_button(cfg)
        if support:
            rows.append([url_button(cfg.get('button_reschedule') or '📅 申请改期', support['url']), support])
        rows.append([callback_button(cfg.get('button_cancel') or '❌ 取消预约', f'cancel_booking:{rid}')])
        return inline_keyboard(rows)

    def link_telegram_customer(c, row):
        linked = c.execute("""SELECT c.* FROM telegram_customers t
                            JOIN customers c ON c.id=t.customer_id WHERE t.telegram_user_id=?""",
                           (str(row.get("telegram_user_id") or ""),)).fetchone()
        if linked:
            return linked
        customer = ensure_customer(c, row.get("username") or row.get("telegram_username") or "Telegram客人",
                                   f"Telegram ID:{row.get('telegram_user_id')}")
        c.execute("""INSERT INTO telegram_customers(telegram_user_id,customer_id,telegram_username,display_name,updated_at)
                     VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(telegram_user_id) DO UPDATE SET customer_id=excluded.customer_id,
                     telegram_username=excluded.telegram_username,display_name=excluded.display_name,
                     updated_at=CURRENT_TIMESTAMP""",
                  (str(row.get("telegram_user_id") or ""), int(customer["id"]),
                   row.get("telegram_username") or "", row.get("username") or ""))
        return c.execute("SELECT * FROM customers WHERE id=?", (int(customer["id"]),)).fetchone()

    def finalize_points(user, chat_id, rid, requested_points):
        cfg = settings()
        rate = max(1, int(cfg.get("points_yen_per_point") or 1))
        with conn() as c:
            found = c.execute("""SELECT * FROM customer_reservations
                               WHERE id=? AND telegram_user_id=? AND status='已确认'""",
                              (int(rid), str(user.get("id")))).fetchone()
            if not found:
                clear_session(user.get("id"))
                send_message(chat_id, "没有找到等待积分选择的预约。")
                return
            row = dict(found)
            if int(row.get("order_id") or 0):
                clear_session(user.get("id"))
                send_message(chat_id, "这笔预约已经完成积分确认。")
                return
            customer = link_telegram_customer(c, row)
            available = max(0, min(int(row.get("points_available") or 0), int(customer["points"] or 0)))
            max_usable = min(available, int(row["price"] or 0) // rate)
            used = max(0, min(int(requested_points or 0), max_usable))
            actual = max(0, int(row["price"] or 0) - used * rate)
            girl_row = c.execute("SELECT id FROM girls WHERE name=?", (row["girl_name"],)).fetchone()
            create_or_update_order(c, {
                "order_date": row["reserve_date"], "girl_id": int(girl_row["id"]) if girl_row else 0,
                "girl_name": row["girl_name"], "service_time": f"{row['start_time']}-{row['end_time']}",
                "received_amount": actual, "points_used": used, "customer_raw": customer["customer_no"],
                "remark": f"Telegram Bot 预约｜使用积分 {used}", "order_status": "预约中", "settlement_status": "未结算",
            })
            order_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
            c.execute("""UPDATE customer_reservations SET order_id=?,points_used=?,actual_payment=?,
                         updated_at=CURRENT_TIMESTAMP WHERE id=?""", (order_id, used, actual, int(rid)))
            order_row = dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
            row.update({"order_id": order_id, "points_used": used, "actual_payment": actual})
        success_text = render_text(
            cfg.get('text_booking_success') or DEFAULT_SETTINGS['text_booking_success'],
            girl=escape(row['girl_name']), date=escape(row['reserve_date']),
            start_time=escape(row['start_time']), end_time=escape(row['end_time']),
        )
        policy_text = cfg.get('text_cancel_policy') or DEFAULT_SETTINGS['text_cancel_policy']
        if available > 0:
            points_text = (f"当前可用积分：{available}\n本次使用积分：{used}\n"
                           f"积分抵扣：¥{used * rate:,}\n<b>客人实际支付：¥{actual:,}</b>")
        else:
            # 新客没有积分，不向客人展示无意义的积分确认/使用明细。
            points_text = f"<b>客人实际支付：¥{actual:,}</b>"
        send_message(chat_id, f"{success_text}\n\n{points_text}\n\n{policy_text}", booking_action_keyboard(row, cfg))
        send_approved_chain(row, order_row, cfg, cfg.get("default_review_chat_id"))
        set_session(user.get("id"), chat_id, "booked", {"reservation_id": int(rid)})

    def review_reservation(callback, approve):
        user = callback.get("from") or {}
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        rid = int((callback.get("data") or "0").split(":", 1)[1])
        cfg = settings()
        if str(chat.get("id")) != str(cfg.get("default_review_chat_id")):
            answer_callback(callback.get("id"), "所有预约只能在内部审核群批准", True)
            return
        if not is_manager(user.get("id"), chat.get("id")):
            answer_callback(callback.get("id"), "只有店长可以审核", True)
            return
        save_manager(user)
        with conn() as c:
            row = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            if not row:
                answer_callback(callback.get("id"), "预约不存在", True)
                return
            row = dict(row)
            if row["status"] not in ("待确认", "发送失败"):
                answer_callback(callback.get("id"), f"该预约已是：{row['status']}", True)
                return
            new_status = "已确认" if approve else "已拒绝"
            customer = link_telegram_customer(c, row) if approve else None
            available_points = int(customer["points"] or 0) if customer else 0
            customer_id = int(customer["id"]) if customer else int(row.get("customer_id") or 0)
            c.execute("""UPDATE customer_reservations SET status=?,customer_id=?,points_available=?,
                         updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (new_status, customer_id, available_points, rid))
        answer_callback(callback.get("id"), "已处理")
        reviewer = display_name(user)
        if approve:
            if available_points <= 0:
                # 新客默认积分为 0：批准后直接生成 MCR 订单和女孩群接龙。
                finalize_points({"id": row["telegram_user_id"]}, row["telegram_chat_id"], rid, 0)
                send_message(chat.get("id"), f"✅ 预约 #{rid} 已由 {escape(reviewer)} 批准。新客无可用积分，已直接生成接龙。")
            else:
                send_message(row["telegram_chat_id"],
                             f"预约成功！{row['girl_name']} {row['reserve_date']} {row['start_time']}-{row['end_time']} 已经为你留好。\n\n"
                             f"当前可用积分：<b>{available_points}</b>\n请选择本次积分使用方式（1 积分抵 ¥{int(cfg.get('points_yen_per_point') or 1)}）：",
                             inline_keyboard([
                                 [callback_button("不使用积分", f"points:0:{rid}"), callback_button("全部使用", f"points:all:{rid}")],
                                 [callback_button("输入使用积分", f"points:custom:{rid}")],
                             ]))
                set_session(row["telegram_user_id"], row["telegram_chat_id"], "choose_points",
                            {"reservation_id": rid, "available_points": available_points})
                send_message(chat.get("id"), f"✅ 预约 #{rid} 已由 {escape(reviewer)} 批准。等待客人确认积分后生成接龙。")
        else:
            send_message(row["telegram_chat_id"], f"很抱歉，预约 #{rid} 未通过。请重新选择时间或联系人工客服。")
            send_message(chat.get("id"), f"❌ 预约 #{rid} 已由 {escape(reviewer)} 拒绝。")

    def receive_hotel(message, session):
        user, chat = message.get("from") or {}, message.get("chat") or {}
        rid = int(session["payload"].get("reservation_id") or 0)
        with conn() as c:
            row = c.execute("SELECT * FROM customer_reservations WHERE id=? AND telegram_user_id=?", (rid, str(user.get("id")))).fetchone()
            if not row or row["status"] != "已确认":
                clear_session(user.get("id"))
                send_message(chat.get("id"), "没有找到等待酒店资料的已确认预约。")
                return
            row = dict(row)
        caption = message.get("caption") or message.get("text") or "酒店资料"
        file_id = ""
        if message.get("photo"):
            file_id = message["photo"][-1].get("file_id") or ""
        elif message.get("document"):
            file_id = message["document"].get("file_id") or ""
        target = row["telegram_group_chat_id"]
        cfg = settings()
        thread_id = int(cfg.get("default_review_thread_id") or 0) if str(target) == str(cfg.get("default_review_chat_id") or "") else 0
        with conn() as c:
            binding = c.execute("""SELECT * FROM telegram_group_bindings
                                  WHERE girl_name=? AND chat_id=? AND enabled=1""",
                                (row["girl_name"], str(target))).fetchone()
            if binding:
                thread_id = int(binding["message_thread_id"] or 0)
        header = f"🏨 预约 #{rid} 的酒店资料\n女孩：{row['girl_name']}\n时间：{row['reserve_date']} {row['start_time']}-{row['end_time']}"
        sent_message_ids = []
        try:
            if message.get("photo"):
                data = {"chat_id": target, "photo": file_id, "caption": f"{header}\n{caption}"}
                if thread_id:
                    data["message_thread_id"] = thread_id
                sent = tg("sendPhoto", data)
                sent_message_ids.append(int(sent.get('message_id') or 0))
            elif message.get("document"):
                data = {"chat_id": target, "document": file_id, "caption": f"{header}\n{caption}"}
                if thread_id:
                    data["message_thread_id"] = thread_id
                sent = tg("sendDocument", data)
                sent_message_ids.append(int(sent.get('message_id') or 0))
            elif message.get("location"):
                loc = message["location"]
                sent = tg("sendLocation", {"chat_id": target, "latitude": loc["latitude"], "longitude": loc["longitude"],
                                           "message_thread_id": thread_id or None})
                sent_message_ids.append(int(sent.get('message_id') or 0))
                sent = send_message(target, header, thread_id=thread_id)
                sent_message_ids.append(int(sent.get('message_id') or 0))
            elif message.get("text"):
                sent = send_message(target, f"{escape(header)}\n地址/信息：{escape(caption)}", thread_id=thread_id)
                sent_message_ids.append(int(sent.get('message_id') or 0))
            else:
                send_message(chat.get("id"), "请发送酒店截图、地址文字或 Telegram 定位。")
                return
        except Exception:
            send_message(chat.get("id"), "酒店资料发送失败，请稍后重试或联系人工客服。")
            return
        with conn() as c:
            c.execute("""UPDATE customer_reservations SET hotel_file_id=?,hotel_caption=?,telegram_hotel_chat_id=?,
                         telegram_hotel_message_ids=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (file_id, caption, str(target), json.dumps([x for x in sent_message_ids if x]), rid))
        clear_session(user.get("id"))
        send_message(chat.get("id"), "酒店地址和资料已经发送成功啦～如有变化，可以再次点击“发送酒店信息”更新。",
                     booking_action_keyboard(row, cfg))

    def delete_bot_group_message(chat_id, message_id):
        if not chat_id or not int(message_id or 0):
            return
        try:
            tg('deleteMessage', {'chat_id': chat_id, 'message_id': int(message_id)})
        except Exception:
            pass

    def cancel_reservation_by_customer(user, chat_id, rid):
        cfg = settings()
        with conn() as c:
            found = c.execute("""SELECT * FROM customer_reservations
                               WHERE id=? AND telegram_user_id=?""",
                              (int(rid), str(user.get('id')))).fetchone()
            if not found:
                send_message(chat_id, '没有找到这笔预约，请联系人工客服。')
                return
            row = dict(found)
            if row['status'] == '取消':
                send_message(chat_id, '这笔预约已经取消过了。')
                return
            if row['status'] != '已确认':
                send_message(chat_id, '当前预约状态不能自助取消，请联系人工客服。')
                return
            customer_id = int(row.get('customer_id') or 0)
            customer = c.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone() if customer_id else None
            previous_count = int(c.execute("""SELECT COUNT(*) FROM telegram_customer_cancellations
                                             WHERE telegram_user_id=?""",
                                           (str(user.get('id')),)).fetchone()[0] or 0)
            cancellation_no = previous_count + 1
            points_deducted = int(customer['points'] or 0) if customer and cancellation_no == 1 else 0
            c.execute("""INSERT INTO telegram_customer_cancellations(
                            reservation_id,telegram_user_id,customer_id,cancellation_no,points_deducted)
                         VALUES(?,?,?,?,?)""",
                      (int(rid), str(user.get('id')), customer_id, cancellation_no, points_deducted))
            order_id = int(row.get('order_id') or 0)
            if order_id:
                c.execute("UPDATE orders SET order_status='取消',updated_at=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
            c.execute("UPDATE customer_reservations SET status='取消',updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(rid),))
            if customer:
                if cancellation_no == 1:
                    c.execute("UPDATE customers SET points=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (customer_id,))
                    if points_deducted:
                        c.execute("""INSERT INTO points_records(customer_id,customer_no,change_points,reason,remark,order_id)
                                     VALUES(?,?,?,?,?,?)""",
                                  (customer_id, customer['customer_no'], -points_deducted,
                                   'Telegram 首次取消预约', f'预约 #{rid} 首次取消，清空累计积分', order_id or None))
                if cancellation_no >= 2:
                    c.execute("UPDATE customers SET customer_status='黑名单',updated_at=CURRENT_TIMESTAMP WHERE id=?", (customer_id,))
                refresh_customer_totals(c, customer_id, update_types=False)

        # 每位女孩每天只有一张接龙总表；取消时重建总表，不能删除共享消息。
        try:
            refresh_daily_chain(row, cfg)
        except Exception as exc:
            internal_id = str(cfg.get('default_review_chat_id') or '')
            if internal_id:
                send_message(internal_id, f"⚠️ 预约已取消，但接龙总表更新失败：{escape(str(exc))}",
                             thread_id=int(cfg.get('default_review_thread_id') or 0))
        hotel_ids = []
        try:
            hotel_ids = json.loads(row.get('telegram_hotel_message_ids') or '[]')
        except Exception:
            hotel_ids = []
        for message_id in hotel_ids:
            delete_bot_group_message(row.get('telegram_hotel_chat_id'), message_id)

        group_notice = (f"❌ <b>预约已取消 #{rid}</b>\n女孩：{escape(row['girl_name'])}\n"
                        f"时间：{escape(row['reserve_date'])} {escape(row['start_time'])}-{escape(row['end_time'])}")
        internal_id = str(cfg.get('default_review_chat_id') or '')
        if internal_id:
            consequence = f"扣除累计积分 {points_deducted}" if cancellation_no == 1 else '已加入黑名单'
            send_message(internal_id,
                         f"{group_notice}\n客人：{escape(row.get('username') or row.get('telegram_username') or str(user.get('id')))}"
                         f"\n第 {cancellation_no} 次取消｜{consequence}",
                         thread_id=int(cfg.get('default_review_thread_id') or 0))
        clear_session(user.get('id'))
        if cancellation_no == 1:
            result_text = (f"预约已经取消。按照预约守护规则，本次为第一次取消，当前累计积分 "
                           f"{points_deducted} 已清空。下次预约前，请确认好行程哦～")
        else:
            result_text = "预约已经取消。本次为第二次取消，账号已暂停自助预约并加入黑名单。如有特殊情况，请联系人工客服说明。"
        rows = []
        support = support_url_button(cfg)
        if support:
            rows.append([support])
        send_message(chat_id, result_text, inline_keyboard(rows) if rows else None)

    def bind_group(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "")
        girl = re.sub(r"^/绑定女孩(?:@\w+)?\s*", "", text).strip()
        if chat.get("type") not in ("group", "supergroup"):
            send_message(chat.get("id"), "请在女孩群内使用这个命令。")
            return
        if not is_chat_admin(chat.get("id"), user.get("id")):
            send_message(chat.get("id"), "只有群管理员可以绑定女孩。")
            return
        if not girl:
            send_message(chat.get("id"), "格式：<code>/绑定女孩 娜娜子</code>")
            return
        with conn() as c:
            exists = c.execute("SELECT 1 FROM girls WHERE name=?", (girl,)).fetchone()
            if not exists:
                send_message(chat.get("id"), f"管理系统女孩表中没有找到“{girl}”。")
                return
            c.execute("""INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,message_thread_id,enabled,updated_at)
                         VALUES(?,?,?,?,1,CURRENT_TIMESTAMP)
                         ON CONFLICT(girl_name) DO UPDATE SET chat_id=excluded.chat_id,chat_title=excluded.chat_title,
                         message_thread_id=excluded.message_thread_id,enabled=1,updated_at=CURRENT_TIMESTAMP""",
                      (girl, str(chat.get("id")), chat.get("title") or "", int(message.get("message_thread_id") or 0)))
        save_manager(user)
        send_message(chat.get("id"), f"✅ 已把本群绑定给 <b>{escape(girl)}</b>。\n该女孩加入当天自动预约名单后就会出现在 Bot 中。",
                     thread_id=message.get("message_thread_id") or 0)

    def bind_default_group(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        if chat.get("type") not in ("group", "supergroup"):
            send_message(chat.get("id"), "请在 Alice 内部审核群内使用这个命令。")
            return
        if not is_chat_admin(chat.get("id"), user.get("id")):
            send_message(chat.get("id"), "只有群管理员可以绑定默认审核群。")
            return
        values = {
            "default_review_chat_id": str(chat.get("id")),
            "default_review_chat_title": chat.get("title") or "Alice内部群",
            "default_review_thread_id": str(int(message.get("message_thread_id") or 0)),
        }
        with conn() as c:
            for key, value in values.items():
                c.execute("""INSERT INTO telegram_settings(setting_key,setting_value,updated_at)
                             VALUES(?,?,CURRENT_TIMESTAMP)
                             ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,
                             updated_at=CURRENT_TIMESTAMP""", (key, value))
        save_manager(user)
        send_message(chat.get("id"), "✅ 已将本群设为默认预约审核群。\n没有绑定专属群的女孩，预约都会发送到这里。",
                     thread_id=message.get("message_thread_id") or 0)

    def send_latest_chain_from_group(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "")
        if chat.get("type") not in ("group", "supergroup"):
            send_message(chat.get("id"), "请在已经绑定女孩的群内使用 <code>/最新接龙</code>。")
            return
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            send_message(chat.get("id"), "只有店长、客服或群管理员可以发送最新接龙。",
                         thread_id=message.get("message_thread_id") or 0)
            return
        with conn() as c:
            binding = c.execute("""SELECT girl_name FROM telegram_group_bindings
                                  WHERE chat_id=? AND enabled=1
                                  ORDER BY updated_at DESC LIMIT 1""",
                                (str(chat.get("id")),)).fetchone()
        if not binding:
            send_message(chat.get("id"), "本群还没有绑定女孩，请先发送 <code>/绑定女孩 女孩名</code>。",
                         thread_id=message.get("message_thread_id") or 0)
            return
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        day = date_match.group(1) if date_match else tokyo_now().date().isoformat()
        row = {"reserve_date": day, "girl_name": binding["girl_name"],
               "telegram_group_chat_id": str(chat.get("id"))}
        try:
            # 强制发一张新的总表，让“最新接龙”出现在群聊底部；后续批准继续编辑这张。
            refresh_daily_chain(row, settings(), force_new=True)
        except Exception as exc:
            send_message(chat.get("id"), f"最新接龙发送失败：{escape(str(exc))}",
                         thread_id=message.get("message_thread_id") or 0)

    def import_chain_from_group(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        if chat.get("type") not in ("group", "supergroup"):
            return
        cfg = settings()
        internal_chat_id = str(cfg.get("default_review_chat_id") or "")
        internal_thread_id = int(cfg.get("default_review_thread_id") or 0)

        def notify_internal(text):
            if internal_chat_id:
                send_message(internal_chat_id, text, thread_id=internal_thread_id)

        reply = message.get("reply_to_message") or {}
        chain_text = str(reply.get("text") or reply.get("caption") or "").strip()
        if not chain_text:
            chain_text = re.sub(r"^/?导入(?:接龙)?(?:@\w+)?\s*", "", str(message.get("text") or "")).strip()
        if not chain_text:
            notify_internal("❌ 接龙导入失败：请回复一条接龙消息并发送 <code>导入</code>，也可以把接龙文字直接写在“导入”后面。")
            return
        if chain_is_empty(chain_text):
            return
        girl_id = None
        binding = None
        with conn() as c:
            binding = bound_girl(chat.get("id"), c)
            if binding:
                girl_id = binding["girl_id"]
        # 女孩专属群已经通过群 ID 绑定，群内成员可直接导入；未绑定群仍需客服或管理员权限。
        if not binding and not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            notify_internal(f"❌ 接龙导入被拒绝：{escape(display_name(user))} 不是店长、客服或该群管理员。")
            return
        try:
            source_message_id = reply.get("message_id") or message.get("message_id") or ""
            order_date, _detected_girl, girl_id = validate_attending_chain(chain_text, girl_id)
            result = import_chain_text(
                chain_text, order_date=order_date, girl_id=girl_id, settlement_status="未结算",
                source_chat_id=chat.get("id"), source_message_id=source_message_id,
            )
            if int(result.get("count") or 0) == 0:
                raise ValueError("没有识别到有效预约行，请检查时间/价格/客人字段。")
            notify_internal(
                f"✅ 管理系统接龙导入完成\n来源群：<b>{escape(chat.get('title') or str(chat.get('id')))}</b>"
                f"\n女孩：<b>{escape(result['girl_name'])}</b>\n日期：{escape(result['order_date'])}"
                f"\n新增：{int(result['inserted'])} 单｜修改：{int(result['updated'])} 单｜未变化：{int(result['unchanged'])} 单"
            )
        except Exception as exc:
            notify_internal(f"❌ 接龙导入失败\n来源群：<b>{escape(chat.get('title') or str(chat.get('id')))}</b>\n原因：{escape(str(exc))}")

    def chain_header(chain_text):
        lines = [line.strip() for line in str(chain_text or "").splitlines() if line.strip()]
        if not lines or not callable(parse_chain_header):
            return "", ""
        first = lines[0]
        # 只收集首行以日期开头的消息，避免把普通聊天里的日期误当成接龙。
        if not re.match(r"^(?:#?接龙\s*)?(?:\d+[.、]\s*)?(?:\[|【)?(?:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}日?|\d{3,4})(?:\]|】)?(?:\s|[^\d]|$)", first):
            return "", ""
        day, girl = parse_chain_header([first])
        return str(day or ""), str(girl or "").strip()

    def automatic_chain_header(chain_text):
        """自动扫描认首行 MMDD；凌晨四点前同时接受当天与前一天。"""
        lines = [line.strip() for line in str(chain_text or "").splitlines() if line.strip()]
        if not lines:
            return "", ""
        now = tokyo_now()
        if not re.fullmatch(r"\d{4}", lines[0]):
            return "", ""
        for candidate in auto_import_candidate_dates(now):
            if lines[0] == candidate.strftime("%m%d"):
                return candidate.isoformat(), ""
        return "", ""

    def chain_is_empty(chain_text):
        lines = [line.strip() for line in str(chain_text or "").splitlines() if line.strip()]
        if len(lines) <= 1:
            return True
        empty_markers = re.compile(r"^(?:暂无(?:预约|接龙)?|没有预约|无预约|空|なし|予約なし)[。.!！]?$", re.I)
        return all(empty_markers.match(re.sub(r"^\d+[.、]\s*", "", line).strip()) for line in lines[1:])

    def bound_girl(chat_id, c=None):
        own = c is None
        c = c or conn()
        try:
            row = c.execute("""SELECT b.girl_name,g.id AS girl_id FROM telegram_group_bindings b
                               LEFT JOIN girls g ON g.name=b.girl_name
                               WHERE b.chat_id=? AND b.enabled=1 ORDER BY b.updated_at DESC LIMIT 1""",
                            (str(chat_id),)).fetchone()
            return dict(row) if row else None
        finally:
            if own:
                c.close()

    def validate_attending_chain(chain_text, preferred_girl_id=None):
        day, header_girl = chain_header(chain_text)
        if not day:
            raise ValueError("接龙首行必须以日期开头，例如：0908娜娜子 或 2026-09-08 娜娜子。")
        with conn() as c:
            preferred = c.execute("SELECT id,name FROM girls WHERE id=?", (int(preferred_girl_id),)).fetchone() if preferred_girl_id else None
            girl_name = str(header_girl or (preferred["name"] if preferred else "")).strip()
            if preferred and header_girl and header_girl != preferred["name"]:
                raise ValueError(f"接龙女孩“{header_girl}”与本群绑定女孩“{preferred['name']}”不一致。")
            attendance = {str(row.get("girl") or "").strip() for row in pure_shift_rows_for_date(c, day)}
            if not girl_name:
                raise ValueError("接龙首行缺少女孩名。")
            if girl_name not in attendance:
                raise ValueError(f"{day} 出勤表中没有“{girl_name}”，本次不会导入。")
            girl = preferred or c.execute("SELECT id,name FROM girls WHERE name=? LIMIT 1", (girl_name,)).fetchone()
            if not girl:
                raise ValueError(f"女孩表中没有“{girl_name}”。")
            return day, girl_name, int(girl["id"])

    def cache_chain_message(message):
        chat = message.get("chat") or {}
        if chat.get("type") not in ("group", "supergroup"):
            return False
        chain_text = str(message.get("text") or message.get("caption") or "").strip()
        day, girl = automatic_chain_header(chain_text)
        message_id = int(message.get("message_id") or 0)
        if not day or not message_id:
            return False
        with conn() as c:
            if not girl:
                binding = bound_girl(chat.get("id"), c)
                if binding:
                    girl = str(binding.get("girl_name") or "")
            c.execute("""INSERT INTO telegram_chain_inbox(
                            chat_id,message_id,chat_title,message_thread_id,chain_text,order_date,girl_name,status,last_error,updated_at)
                         VALUES(?,?,?,?,?,?,?,'pending','',CURRENT_TIMESTAMP)
                         ON CONFLICT(chat_id,message_id) DO UPDATE SET
                            chat_title=excluded.chat_title,message_thread_id=excluded.message_thread_id,
                            chain_text=excluded.chain_text,order_date=excluded.order_date,girl_name=excluded.girl_name,
                            status='pending',last_error='',processed_at=NULL,updated_at=CURRENT_TIMESTAMP""",
                      (str(chat.get("id")), message_id, str(chat.get("title") or ""),
                       int(message.get("message_thread_id") or 0), chain_text, day, girl))
        return True

    def notify_chain_failure(row, error):
        cfg = settings()
        internal_chat_id = str(cfg.get("default_review_chat_id") or "")
        if not valid_group_chat_id(internal_chat_id):
            return
        text = (f"⚠️ <b>自动接龙导入失败</b>\n来源群：<b>{escape(row.get('chat_title') or row.get('chat_id'))}</b>"
                f"\n日期：{escape(row.get('order_date') or '未识别')}｜女孩：{escape(row.get('girl_name') or '未识别')}"
                f"\n原因：{escape(str(error))}\n\n请修改原接龙消息；Bot 收到编辑后会在下一轮重新检查。也可以回复接龙发送 <code>导入</code>。")
        try:
            send_message(internal_chat_id, text[:3900], thread_id=int(cfg.get("default_review_thread_id") or 0))
        except Exception:
            pass

    def import_bound_chain_immediately(message):
        """绑定女孩群新发当天 MMDD 接龙时立即同步，不受定时开关影响。"""
        chat = message.get("chat") or {}
        chain_text = str(message.get("text") or message.get("caption") or "").strip()
        day, _unused = automatic_chain_header(chain_text)
        if not day:
            return False
        with conn() as c:
            binding = bound_girl(chat.get("id"), c)
        if not binding or not int(binding.get("girl_id") or 0):
            return False
        row = {
            "chat_id": str(chat.get("id")), "message_id": int(message.get("message_id") or 0),
            "chat_title": str(chat.get("title") or ""), "order_date": day,
            "girl_name": str(binding.get("girl_name") or ""), "chain_text": chain_text,
        }
        try:
            if chain_is_empty(chain_text):
                with conn() as c:
                    c.execute("""UPDATE telegram_chain_inbox SET status='empty',last_error='',processed_at=CURRENT_TIMESTAMP
                                 WHERE chat_id=? AND message_id=?""", (row["chat_id"], row["message_id"]))
                return True
            order_date, girl_name, girl_id = validate_attending_chain(chain_text, binding["girl_id"])
            imported = import_chain_text(
                chain_text, order_date=order_date, girl_id=girl_id, settlement_status="未结算",
                source_chat_id=row["chat_id"], source_message_id=row["message_id"])
            if int(imported.get("count") or 0) == 0:
                raise ValueError("没有识别到有效预约行，请检查时间/价格/客人字段。")
            with conn() as c:
                c.execute("""UPDATE telegram_chain_inbox SET status='imported',last_error='',processed_at=CURRENT_TIMESTAMP,
                             order_date=?,girl_name=? WHERE chat_id=? AND message_id=?""",
                          (order_date, girl_name, row["chat_id"], row["message_id"]))
            changed = int(imported.get("inserted") or 0) + int(imported.get("updated") or 0)
            if changed:
                cfg = settings()
                internal_id = str(cfg.get("default_review_chat_id") or "")
                if valid_group_chat_id(internal_id):
                    send_message(
                        internal_id,
                        f"✅ <b>接龙即时同步完成</b>\n来源群：{escape(row['chat_title'] or row['chat_id'])}"
                        f"\n女孩：{escape(girl_name)}｜日期：{escape(order_date)}"
                        f"\n新增：{int(imported.get('inserted') or 0)} 单｜修改：{int(imported.get('updated') or 0)} 单",
                        thread_id=int(cfg.get("default_review_thread_id") or 0),
                    )
            return True
        except Exception as exc:
            with conn() as c:
                c.execute("""UPDATE telegram_chain_inbox SET status='failed',last_error=?,processed_at=CURRENT_TIMESTAMP
                             WHERE chat_id=? AND message_id=?""", (str(exc)[:1000], row["chat_id"], row["message_id"]))
            notify_chain_failure(row, exc)
            return True

    def run_pending_chain_imports(force=False):
        ensure_db()
        cfg = settings()
        if not force and str(cfg.get("auto_chain_import_enabled") or "1") != "1":
            return {"skipped": True, "reason": "disabled", "imported": 0, "failed": 0}
        interval = max(5, int(cfg.get("auto_chain_import_interval_minutes") or 30))
        with conn() as c:
            if not force:
                claimed = c.execute("""UPDATE telegram_chain_sync_state SET last_started_at=CURRENT_TIMESTAMP
                                       WHERE id=1 AND (last_started_at IS NULL OR
                                       last_started_at<=datetime('now',?))""", (f"-{interval} minutes",))
                if not claimed.rowcount:
                    return {"skipped": True, "reason": "not_due", "imported": 0, "failed": 0}
            else:
                c.execute("UPDATE telegram_chain_sync_state SET last_started_at=CURRENT_TIMESTAMP WHERE id=1")
            pending = [dict(row) for row in c.execute(
                "SELECT * FROM telegram_chain_inbox WHERE status='pending' ORDER BY message_id DESC").fetchall()]
            for row in pending:
                binding = bound_girl(row["chat_id"], c)
                row["preferred_girl_id"] = int(binding["girl_id"] or 0) if binding else 0
                if binding and not row.get("girl_name"):
                    row["girl_name"] = str(binding.get("girl_name") or "")
        latest, superseded = [], []
        seen = set()
        for row in pending:
            key = (row["chat_id"], row["order_date"], row["girl_name"])
            (latest if key not in seen else superseded).append(row)
            seen.add(key)
        if superseded:
            with conn() as c:
                c.executemany("UPDATE telegram_chain_inbox SET status='superseded',processed_at=CURRENT_TIMESTAMP WHERE chat_id=? AND message_id=?",
                              [(row["chat_id"], row["message_id"]) for row in superseded])
        result = {"skipped": False, "checked": len(latest), "imported": 0, "failed": 0,
                  "empty": 0, "expired": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        allowed_days = {day.isoformat() for day in auto_import_candidate_dates(tokyo_now())}
        for row in latest:
            try:
                if str(row.get("order_date") or "") not in allowed_days:
                    with conn() as c:
                        c.execute("""UPDATE telegram_chain_inbox SET status='expired',last_error='',processed_at=CURRENT_TIMESTAMP
                                     WHERE chat_id=? AND message_id=?""", (row["chat_id"], row["message_id"]))
                    result["expired"] += 1
                    continue
                if chain_is_empty(row["chain_text"]):
                    with conn() as c:
                        c.execute("""UPDATE telegram_chain_inbox SET status='empty',last_error='',processed_at=CURRENT_TIMESTAMP
                                     WHERE chat_id=? AND message_id=?""", (row["chat_id"], row["message_id"]))
                    result["empty"] += 1
                    continue
                day, girl_name, girl_id = validate_attending_chain(
                    row["chain_text"], row.get("preferred_girl_id") or None)
                imported = import_chain_text(
                    row["chain_text"], order_date=day, girl_id=girl_id, settlement_status="未结算",
                    source_chat_id=row["chat_id"], source_message_id=row["message_id"])
                if int(imported.get("count") or 0) == 0:
                    raise ValueError("没有识别到有效预约行，请检查时间/价格/客人字段。")
                with conn() as c:
                    c.execute("""UPDATE telegram_chain_inbox SET status='imported',last_error='',processed_at=CURRENT_TIMESTAMP,
                                 order_date=?,girl_name=? WHERE chat_id=? AND message_id=?""",
                              (day, girl_name, row["chat_id"], row["message_id"]))
                result["imported"] += 1
                for key in ("inserted", "updated", "unchanged"):
                    result[key] += int(imported.get(key) or 0)
            except Exception as exc:
                with conn() as c:
                    c.execute("""UPDATE telegram_chain_inbox SET status='failed',last_error=?,processed_at=CURRENT_TIMESTAMP
                                 WHERE chat_id=? AND message_id=?""", (str(exc)[:1000], row["chat_id"], row["message_id"]))
                result["failed"] += 1
                notify_chain_failure(row, exc)
        with conn() as c:
            c.execute("""UPDATE telegram_chain_sync_state SET last_completed_at=CURRENT_TIMESTAMP,last_result=? WHERE id=1""",
                      (json.dumps(result, ensure_ascii=False),))
        return result

    def handle_auto_import_control(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        raw = re.sub(r"^/", "", str(message.get("text") or "").strip())
        compact = re.sub(r"\s+", "", raw)
        match = re.fullmatch(r"(?:自动导入(?:接龙)?|接龙自动导入)(开启|打开|开|关闭|停止|关|状态)", compact)
        if not match:
            return False
        cfg = settings()
        if str(chat.get("id")) != str(cfg.get("default_review_chat_id") or ""):
            return True
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            send_message(chat.get("id"), "❌ 只有店长、客服或群管理员可以修改自动导入设置。")
            return True
        action = match.group(1)
        if action != "状态":
            enabled = "0" if action in ("关闭", "停止", "关") else "1"
            with conn() as c:
                c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES('auto_chain_import_enabled',?)
                             ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""", (enabled,))
        else:
            enabled = str(cfg.get("auto_chain_import_enabled") or "1")
        label = "已开启" if enabled == "1" else "已关闭"
        interval = max(5, int(cfg.get("auto_chain_import_interval_minutes") or 30))
        if enabled == "1" and action == "状态":
            with conn() as c:
                state = c.execute("SELECT last_started_at FROM telegram_chain_sync_state WHERE id=1").fetchone()
            last_started = str(state["last_started_at"] or "") if state else ""
            next_text = "预计 1 分钟内"
            if last_started:
                try:
                    due_utc = datetime.strptime(last_started, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=interval)
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    if due_utc > now_utc:
                        next_text = (due_utc + timedelta(hours=9)).strftime("%m月%d日 %H:%M（东京时间）")
                except ValueError:
                    pass
            suffix = (f"每 {interval} 分钟检查一次。\n下次自动导入：<b>{next_text}</b>。"
                      "\n日期窗口：00:00–03:59 同时接受前一天和当天；04:00 后只接受当天。")
        elif enabled == "1":
            suffix = f"每 {interval} 分钟检查一次；凌晨 4 点前同时接受前一天和当天。"
        else:
            suffix = "定时扫描已停止；绑定女孩群重发当天接龙仍会即时导入，也可回复接龙发送“导入”。"
        send_message(chat.get("id"), f"✅ 接龙自动导入{label}。{suffix}")
        return True

    def parse_shift_copy_text(raw_text):
        lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError("没有收到出勤文案。请回复出勤文案发送“文案生成”，或把文案写在关键字下一行。")
        header = lines.pop(0)
        now = tokyo_now()
        day = ""
        full = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", header)
        short = re.search(r"(?<!\d)(\d{2})(\d{2})(?!\d)", header)
        try:
            if full:
                day = datetime(int(full.group(1)), int(full.group(2)), int(full.group(3))).date().isoformat()
            elif short:
                month, date_no = int(short.group(1)), int(short.group(2))
                candidates = [datetime(now.year + offset, month, date_no).date() for offset in (-1, 0, 1)]
                day = min(candidates, key=lambda d: abs((d - now.date()).days)).isoformat()
        except ValueError:
            day = ""
        if not day:
            raise ValueError("文案第一行缺少有效日期，例如“0909周三出勤”。")
        entries = []
        index = 0
        time_line = re.compile(r"(\d{1,2}(?::\d{2})?)\s*(?:到|至|[-~～—])\s*(\d{1,2}(?::\d{2})?).*?(?:¥|￥)?\s*([\d,]+)\s*(?:円|/h|/H|每小时)?")
        while index < len(lines):
            name_line = lines[index]
            if re.match(r"https?://", name_line, re.I):
                break
            if index + 1 >= len(lines):
                raise ValueError(f"“{name_line}”后面缺少时间和价格。")
            match = time_line.search(lines[index + 1])
            if not match:
                raise ValueError(f"无法识别“{name_line}”的时间/价格行：{lines[index + 1]}")
            gold_tags = re.findall(r"【([^】]+)】", name_line)
            tags = re.findall(r"[（(]([^）)]+)[）)]", name_line)
            name = re.sub(r"【[^】]+】|[（(][^）)]+[）)]", "", name_line).strip()
            if not name:
                raise ValueError(f"无法识别女孩名：{name_line}")
            start, end = match.group(1), match.group(2)
            start = start if ":" in start else start + ":00"
            end = end if ":" in end else end + ":00"
            entries.append({"girl": name, "start": start.zfill(5), "end": end.zfill(5),
                            "price": int(match.group(3).replace(",", "")),
                            "tags": tags, "gold_tags": gold_tags})
            index += 2
        if not entries:
            raise ValueError("没有识别到女孩资料。每位女孩需使用两行：名字与TAG一行，时间和价格一行。")
        return day, entries

    _report_font_cache = {}

    def report_font(size):
        size = int(size)
        if size in _report_font_cache:
            return _report_font_cache[size]
        candidates = [
            os.environ.get("ALICE_REPORT_FONT", ""),
            "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/meiryo.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/tmp/NotoSansCJKsc-Regular.otf",
        ]
        download_path = Path("/tmp/NotoSansCJKsc-Regular.otf")
        if os.name == "nt":
            download_path = Path(os.environ.get("TEMP") or ".") / "NotoSansCJKsc-Regular.otf"
        if not any(Path(path).is_file() for path in candidates if path):
            try:
                font_url = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
                with urlopen(Request(font_url, headers={"User-Agent": "Alice-MCR/1.0"}), timeout=45) as response:
                    font_bytes = response.read()
                if 5_000_000 < len(font_bytes) < 30_000_000:
                    download_path.write_bytes(font_bytes)
            except Exception:
                pass
        candidates.append(str(download_path))
        for path in candidates:
            try:
                if path and Path(path).is_file():
                    font = ImageFont.truetype(path, size=size)
                    _report_font_cache[size] = font
                    return font
            except Exception:
                continue
        font = ImageFont.load_default(size=max(10, size))
        _report_font_cache[size] = font
        return font

    def load_shift_avatar(avatar_url, size=132):
        if not avatar_url:
            return None
        try:
            url = str(avatar_url)
            payload = b""
            if url.startswith("/girl_avatars/"):
                filename = Path(url).name
                db_path = Path(os.environ.get("ALICE_DB_PATH") or "")
                local_dirs = [Path(os.environ.get("ALICE_AVATAR_DIR") or ""),
                              db_path.parent / "girl_avatars" if str(db_path) else Path(),
                              Path(__file__).resolve().parent / "static" / "girl_avatars"]
                for directory in local_dirs:
                    candidate = directory / filename
                    if str(directory) not in ("", ".") and candidate.is_file():
                        payload = candidate.read_bytes()
                        break
            if url.startswith("/"):
                base = str(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
                if not base and not payload:
                    return None
                url = base + url if base else url
            if not payload:
                with urlopen(Request(url, headers={"User-Agent": "Alice-MCR/1.0"}), timeout=12) as response:
                    payload = response.read(5 * 1024 * 1024)
            image = Image.open(BytesIO(payload)).convert("RGB")
            return ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
        except Exception:
            return None

    def render_shift_copy_images(day, entries):
        with conn() as c:
            placeholders = ",".join("?" for _ in entries)
            avatars = {str(row["name"]): str(row["avatar_url"] or "") for row in c.execute(
                f"SELECT name,avatar_url FROM girls WHERE name IN ({placeholders})", [entry["girl"] for entry in entries]).fetchall()}
        pages = []
        weekday = "周" + "一二三四五六日"[datetime.fromisoformat(day).weekday()]
        for page_index in range(0, len(entries), 6):
            chunk = entries[page_index:page_index + 6]
            width, row_height = 1280, 196
            height = 245 + row_height * len(chunk) + 90
            base = Image.new("RGB", (width, height), "#08050e")
            glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            glow_draw.rounded_rectangle((18, 18, width - 18, height - 18), 32, outline="#ff38a4", width=12)
            glow = glow.filter(ImageFilter.GaussianBlur(16))
            base = Image.alpha_composite(base.convert("RGBA"), glow)
            draw = ImageDraw.Draw(base)
            draw.rounded_rectangle((20, 20, width - 20, height - 20), 30, fill="#0b0712", outline="#ff55b4", width=4)
            title = f"{datetime.fromisoformat(day).strftime('%m%d')} {weekday} 出勤"
            draw.text((width // 2, 74), title, font=report_font(64), fill="#ffffff", anchor="mm",
                      stroke_width=3, stroke_fill="#ff2f9a")
            draw.text((width // 2, 150), f"出勤人数：{len(entries)}", font=report_font(30), fill="#ffd447", anchor="mm")
            y = 205
            for entry in chunk:
                draw.rounded_rectangle((45, y, width - 45, y + row_height - 18), 26,
                                       fill="#100b1c", outline="#ff3f9f", width=3)
                avatar = load_shift_avatar(avatars.get(entry["girl"], ""))
                if avatar:
                    mask = Image.new("L", avatar.size, 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, avatar.width - 1, avatar.height - 1), fill=255)
                    base.paste(avatar, (74, y + 23), mask)
                    draw.ellipse((72, y + 21, 208, y + 157), outline="#ff80c6", width=5)
                else:
                    draw.ellipse((72, y + 21, 208, y + 157), fill="#321337", outline="#ff80c6", width=5)
                    draw.text((140, y + 89), entry["girl"][:1], font=report_font(54), fill="#ffffff", anchor="mm")
                combined = " ".join(entry["tags"] + entry["gold_tags"])
                name_color = "#49bfff" if re.search(r"大美女|绝色|颜值", combined) else ("#ff9a4d" if "服务" in combined else ("#ff63b7" if re.search(r"年纪小|少女|新人", combined) else "#d999ff"))
                draw.text((238, y + 31), entry["girl"], font=report_font(42), fill=name_color,
                          stroke_width=1, stroke_fill="#36142f")
                tag_text = "  ".join([f"【{tag}】" for tag in entry["gold_tags"]] + entry["tags"]) or "ALICE"
                draw.text((238, y + 91), tag_text[:34], font=report_font(27), fill="#f5c8e4")
                draw.text((width - 55, y + 35), f"{entry['start']} 到 {entry['end']}", font=report_font(34), fill="#dfffff", anchor="ra")
                draw.text((width - 55, y + 100), f"¥{entry['price']:,}/h", font=report_font(38), fill="#ffd447", anchor="ra")
                y += row_height
            footer = f"ALICE ACADEMY  {page_index // 6 + 1}/{(len(entries) + 5) // 6}"
            draw.text((width // 2, height - 50), footer, font=report_font(24), fill="#ff8fc7", anchor="mm")
            output = BytesIO()
            base.convert("RGB").save(output, format="PNG", optimize=True)
            pages.append(output.getvalue())
        return pages

    def handle_shift_copy_generation(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "").strip()
        if not re.match(r"^/?文案生成(?:@\w+)?(?:\s|$)", text):
            return False
        cfg = settings()
        if str(chat.get("id")) != str(cfg.get("default_review_chat_id") or ""):
            return True
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            send_message(chat.get("id"), "❌ 只有店长、客服或群管理员可以生成出勤图。")
            return True
        reply = message.get("reply_to_message") or {}
        inline_text = re.sub(r"^/?文案生成(?:@\w+)?", "", text, count=1).strip()
        source_text = inline_text or str(reply.get("text") or reply.get("caption") or "").strip()
        try:
            day, entries = parse_shift_copy_text(source_text)
            images = render_shift_copy_images(day, entries)
            for index, image_bytes in enumerate(images):
                caption = f"📋 <b>{escape(day)} 文案生成出勤表</b>\n识别女孩：{len(entries)} 人"
                if len(images) > 1:
                    caption += f"\n图片 {index + 1}/{len(images)}"
                send_photo_bytes(chat.get("id"), image_bytes, caption,
                                 int(cfg.get("default_review_thread_id") or 0))
            send_message(chat.get("id"), f"✅ 文案生成完成：{len(entries)} 位女孩，共 {len(images)} 张图。")
        except Exception as exc:
            send_message(chat.get("id"), f"❌ 文案生成失败：{escape(str(exc))}")
        return True

    def attendance_time_label(minutes, storage=False):
        minutes = int(minutes)
        if minutes == 24 * 60:
            return "00:00" if storage else "24:00"
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def attendance_time_keyboard(inquiry_id, mode, start_minutes=0):
        if mode == "start":
            values = range(12 * 60, 24 * 60, 30)
        else:
            values = range(int(start_minutes) + 30, 24 * 60 + 1, 30)
        buttons = [callback_button(attendance_time_label(value),
                                   f"attendance:{mode}:{int(inquiry_id)}:{value}") for value in values]
        return inline_keyboard([buttons[i:i + 4] for i in range(0, len(buttons), 4)])

    def start_attendance_inquiry(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        cfg = settings()
        if str(chat.get("id")) != str(cfg.get("default_review_chat_id") or ""):
            return True
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            send_message(chat.get("id"), "❌ 只有店长、客服或群管理员可以发起出勤询问。")
            return True
        inquiry_date = tokyo_now().date().isoformat()
        display_date = tokyo_now().strftime("%m月%d日")
        with conn() as c:
            bindings = [dict(row) for row in c.execute("""SELECT b.girl_name,b.chat_id,b.chat_title,b.message_thread_id
                                                            FROM telegram_group_bindings b
                                                            JOIN girls g ON g.name=b.girl_name
                                                            WHERE b.enabled=1 AND COALESCE(g.girl_status,'在职')<>'离职'
                                                            ORDER BY b.girl_name""").fetchall()]
        sent_count, failed = 0, []
        for binding in bindings:
            try:
                with conn() as c:
                    c.execute("""INSERT INTO telegram_attendance_inquiries(
                                    inquiry_date,girl_name,chat_id,chat_title,message_thread_id,message_id,status,
                                    start_time,end_time,responder_user_id,responder_name,expires_at,requested_at,responded_at,updated_at)
                                 VALUES(?,?,?,?,?,0,'pending','','','','',datetime('now','+30 minutes'),CURRENT_TIMESTAMP,NULL,CURRENT_TIMESTAMP)
                                 ON CONFLICT(inquiry_date,girl_name) DO UPDATE SET
                                    chat_id=excluded.chat_id,chat_title=excluded.chat_title,
                                    message_thread_id=excluded.message_thread_id,message_id=0,
                                    status='pending',start_time='',end_time='',responder_user_id='',responder_name='',
                                    expires_at=datetime('now','+30 minutes'),requested_at=CURRENT_TIMESTAMP,
                                    responded_at=NULL,updated_at=CURRENT_TIMESTAMP""",
                              (inquiry_date, binding["girl_name"], str(binding["chat_id"]),
                               str(binding.get("chat_title") or ""), int(binding.get("message_thread_id") or 0)))
                    inquiry_id = int(c.execute("SELECT id FROM telegram_attendance_inquiries WHERE inquiry_date=? AND girl_name=?",
                                               (inquiry_date, binding["girl_name"])).fetchone()[0])
                sent = send_message(
                    binding["chat_id"],
                    f"🌙 <b>{display_date} 出勤确认</b>\n\n{escape(binding['girl_name'])}，今天是否出勤？\n"
                    "请在 <b>30 分钟内</b>点击下方按钮；没有点击将默认今天不出勤。",
                    inline_keyboard([[callback_button("✅ 出勤", f"attendance:yes:{inquiry_id}")]]),
                    thread_id=int(binding.get("message_thread_id") or 0),
                )
                message_id = int((sent or {}).get("message_id") or 0)
                with conn() as c:
                    c.execute("UPDATE telegram_attendance_inquiries SET message_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                              (message_id, inquiry_id))
                sent_count += 1
            except Exception as exc:
                with conn() as c:
                    c.execute("""UPDATE telegram_attendance_inquiries SET status='send_failed',updated_at=CURRENT_TIMESTAMP
                                 WHERE inquiry_date=? AND girl_name=?""", (inquiry_date, binding["girl_name"]))
                failed.append(f"{binding['girl_name']}（{str(exc)[:80]}）")
        summary = f"✅ 已向 {sent_count} 个女孩专属群发送出勤询问，30 分钟未点击将默认不出勤。"
        if failed:
            summary += "\n⚠️ 发送失败：" + "、".join(escape(x) for x in failed[:20])
        send_message(chat.get("id"), summary, thread_id=int(cfg.get("default_review_thread_id") or 0))
        return True

    def expire_attendance_inquiries():
        with conn() as c:
            expired = [dict(row) for row in c.execute("""SELECT * FROM telegram_attendance_inquiries
                                                           WHERE status='pending' AND expires_at<=CURRENT_TIMESTAMP""").fetchall()]
            if expired:
                c.executemany("""UPDATE telegram_attendance_inquiries SET status='absent',updated_at=CURRENT_TIMESTAMP
                                   WHERE id=? AND status='pending'""", [(row["id"],) for row in expired])
                c.executemany("""DELETE FROM pure_shifts WHERE shift_date=? AND girl_name=?
                                   AND source='telegram_attendance'""",
                              [(row["inquiry_date"], row["girl_name"]) for row in expired])
                c.executemany("""DELETE FROM telegram_daily_girls WHERE booking_date=? AND girl_name=?
                                   AND source='attendance_inquiry'""",
                              [(row["inquiry_date"], row["girl_name"]) for row in expired])
        for row in expired:
            try:
                edit_message_text(row["chat_id"], row["message_id"],
                                  f"🌙 <b>{escape(row['girl_name'])} 出勤确认已结束</b>\n\n30 分钟内未确认，今天默认不出勤。")
            except Exception:
                pass
        return len(expired)

    def handle_attendance_callback(callback):
        data = str(callback.get("data") or "")
        user = callback.get("from") or {}
        msg = callback.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        parts = data.split(":")
        if len(parts) < 3:
            answer_callback(callback.get("id"), "这个按钮已失效", True)
            return
        action, inquiry_id = parts[1], int(parts[2] or 0)
        with conn() as c:
            found = c.execute("SELECT *,expires_at<=CURRENT_TIMESTAMP AS expired FROM telegram_attendance_inquiries WHERE id=?",
                              (inquiry_id,)).fetchone()
            row = dict(found) if found else None
        if not row or chat_id != str(row["chat_id"]):
            answer_callback(callback.get("id"), "这个出勤确认不属于本群", True)
            return
        if action == "yes":
            if row["status"] != "pending" or int(row.get("expired") or 0):
                if row["status"] == "pending":
                    with conn() as c:
                        c.execute("UPDATE telegram_attendance_inquiries SET status='absent',updated_at=CURRENT_TIMESTAMP WHERE id=?", (inquiry_id,))
                answer_callback(callback.get("id"), "确认已超过 30 分钟，请联系内部客服", True)
                return
            with conn() as c:
                c.execute("""UPDATE telegram_attendance_inquiries SET status='selecting',responder_user_id=?,responder_name=?,
                             responded_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                          (str(user.get("id") or ""), display_name(user), inquiry_id))
            answer_callback(callback.get("id"), "请选择开始时间")
            edit_message_text(chat_id, row["message_id"],
                              f"✅ <b>{escape(row['girl_name'])} 今天出勤</b>\n\n请选择开始时间（12:00–24:00）：",
                              attendance_time_keyboard(inquiry_id, "start"))
            return
        if row["status"] != "selecting":
            answer_callback(callback.get("id"), "这个时间选择已经失效", True)
            return
        if action == "start" and len(parts) == 4:
            start_minutes = int(parts[3])
            start_time = attendance_time_label(start_minutes, storage=True)
            with conn() as c:
                c.execute("UPDATE telegram_attendance_inquiries SET start_time=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                          (start_time, inquiry_id))
            answer_callback(callback.get("id"), "请选择结束时间")
            edit_message_text(chat_id, row["message_id"],
                              f"✅ <b>{escape(row['girl_name'])} 今天出勤</b>\n\n开始：<b>{attendance_time_label(start_minutes)}</b>\n请选择结束时间：",
                              attendance_time_keyboard(inquiry_id, "end", start_minutes))
            return
        if action != "end" or len(parts) != 4 or not row.get("start_time"):
            answer_callback(callback.get("id"), "请先选择开始时间", True)
            return
        end_minutes = int(parts[3])
        end_time = attendance_time_label(end_minutes, storage=True)
        inquiry_date, girl = row["inquiry_date"], row["girl_name"]
        with conn() as c:
            existing = c.execute("SELECT id FROM pure_shifts WHERE shift_date=? AND girl_name=? ORDER BY id LIMIT 1",
                                 (inquiry_date, girl)).fetchone()
            if existing:
                c.execute("""UPDATE pure_shifts SET start_time=?,end_time=?,source='telegram_attendance',
                             note='女孩群自助出勤',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                          (row["start_time"], end_time, int(existing["id"])))
            else:
                memory = c.execute("SELECT tags,gold_tags FROM girl_tag_memory WHERE girl_name=?", (girl,)).fetchone()
                max_sort = int(c.execute("SELECT COALESCE(MAX(sort_order),0) FROM pure_shifts WHERE shift_date=?",
                                         (inquiry_date,)).fetchone()[0] or 0)
                c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags,sort_order,source,note)
                             VALUES(?,?,?,?,?,?,?,'telegram_attendance','女孩群自助出勤')""",
                          (inquiry_date, girl, row["start_time"], end_time,
                           str(memory["tags"] or "") if memory else "",
                           str(memory["gold_tags"] or "") if memory else "", max_sort + 1))
            c.execute("""INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order,source,updated_at)
                         VALUES(?,?,9999,'attendance_inquiry',CURRENT_TIMESTAMP)
                         ON CONFLICT(booking_date,girl_name) DO UPDATE SET source='attendance_inquiry',updated_at=CURRENT_TIMESTAMP""",
                      (inquiry_date, girl))
            c.execute("""UPDATE telegram_attendance_inquiries SET status='attending',end_time=?,responded_at=CURRENT_TIMESTAMP,
                         updated_at=CURRENT_TIMESTAMP WHERE id=?""", (end_time, inquiry_id))
        answer_callback(callback.get("id"), "出勤时间已保存")
        shown_end = "24:00" if end_time == "00:00" else end_time
        edit_message_text(chat_id, row["message_id"],
                          f"✅ <b>出勤登记完成</b>\n\n女孩：{escape(girl)}\n时间：<b>{row['start_time']}–{shown_end}</b>\n已自动写入 MCR 今日出勤表。")
        cfg = settings()
        internal_id = str(cfg.get("default_review_chat_id") or "")
        if internal_id:
            send_message(internal_id,
                         f"✅ <b>女孩已确认出勤</b>\n女孩：{escape(girl)}\n时间：{row['start_time']}–{shown_end}\n已写入 MCR 今日出勤表。",
                         thread_id=int(cfg.get("default_review_thread_id") or 0))

    def handle_customer_lookup(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "").strip()
        points_match = re.fullmatch(r"/?积分查询(?:\s*[+＋:：]?\s*)(\d+)", text)
        number_match = re.fullmatch(r"/?编号查询(?:\s*[+＋:：]?\s*)(.+)", text)
        if not points_match and not number_match:
            return False
        cfg = settings()
        if str(chat.get("id")) != str(cfg.get("default_review_chat_id") or ""):
            return True
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            send_message(chat.get("id"), "❌ 只有店长、客服或群管理员可以查询客户资料。")
            return True
        if points_match:
            customer_no = f"{int(points_match.group(1)):04d}"
            with conn() as c:
                customer = c.execute("SELECT * FROM customers WHERE customer_no=?", (customer_no,)).fetchone()
                if customer:
                    customer = c.execute("SELECT * FROM customers WHERE id=?", (int(customer["id"]),)).fetchone()
            if not customer:
                send_message(chat.get("id"), f"没有找到客户编号 <b>{escape(customer_no)}</b>。")
            else:
                send_message(chat.get("id"),
                             f"🎀 <b>客户积分查询</b>\n编号：<b>{escape(customer['customer_no'])}</b>\n"
                             f"客户名：{escape(customer['name'] or '未填写')}\n当前积分：<b>{int(customer['points'] or 0):,}</b>\n"
                             f"客户类型：{escape(customer['customer_type'] or '新客')}")
            return True
        name = number_match.group(1).strip()
        escaped_like = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with conn() as c:
            matches = c.execute("""SELECT customer_no,name,points,customer_type FROM customers
                                   WHERE name=? OR name LIKE ? ESCAPE '\\'
                                   ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END,id DESC LIMIT 10""",
                                (name, f"%{escaped_like}%", name)).fetchall()
        if not matches:
            send_message(chat.get("id"), f"没有找到客户名“<b>{escape(name)}</b>”。")
        else:
            lines = ["🔎 <b>客户编号查询</b>"]
            for item in matches:
                lines.append(f"{escape(item['name'] or '未填写')}：<b>{escape(item['customer_no'])}</b>｜积分 {int(item['points'] or 0):,}")
            if len(matches) == 10:
                lines.append("结果较多，请输入更完整的客户名。")
            send_message(chat.get("id"), "\n".join(lines))
        return True

    def closing_money(day, girl, c):
        row = c.execute("""SELECT COUNT(*) AS order_count,
                                  COALESCE(SUM(girl_take_home),0) AS girl_earnings,
                                  COALESCE(SUM(store_profit),0) AS store_profit,
                                  COALESCE(SUM(CASE WHEN COALESCE(payment_method,'现金')<>'现金'
                                                    THEN received_amount ELSE 0 END),0) AS non_cash_received
                           FROM orders WHERE order_date=? AND girl_name=?
                             AND COALESCE(order_status,'')<>'取消'""", (day, girl)).fetchone()
        result = dict(row) if row else {'order_count': 0, 'girl_earnings': 0, 'store_profit': 0, 'non_cash_received': 0}
        result['amount_due'] = int(result.get('store_profit') or 0) - int(result.get('non_cash_received') or 0)
        return result

    def closing_summary_text(day, girl, totals):
        due = int(totals.get('amount_due') or 0)
        settlement = (f"女孩需交店里：<b>¥{due:,}</b>" if due >= 0
                      else f"店里需转给女孩：<b>¥{abs(due):,}</b>")
        return (f"🌙 <b>{escape(day)} 下班结算确认</b>\n\n"
                f"女孩：<b>{escape(girl)}</b>\n"
                f"今天预约：<b>{int(totals.get('order_count') or 0)} 单</b>\n"
                f"女孩今天到手：<b>¥{int(totals.get('girl_earnings') or 0):,}</b>\n"
                f"店铺收益：<b>¥{int(totals.get('store_profit') or 0):,}</b>\n"
                f"客人线上支付：<b>¥{int(totals.get('non_cash_received') or 0):,}</b>\n"
                f"{settlement}\n\n请女孩核对金额，并选择本次与店里的结算方式：")

    def send_closing_prompt(girl, binding, trigger_source='keyword', user=None, notify_if_empty=True, day=None):
        day = day or closing_business_date(tokyo_now())
        with conn() as c:
            totals = closing_money(day, girl, c)
            existing = c.execute("SELECT * FROM telegram_closing_confirmations WHERE close_date=? AND girl_name=?",
                                 (day, girl)).fetchone()
        if int(totals.get('order_count') or 0) <= 0:
            if notify_if_empty:
                send_message(binding['chat_id'], f"🌙 {escape(girl)} 今天没有需要结算的预约。",
                             thread_id=int(binding.get('message_thread_id') or 0))
            return {'sent': False, 'empty': True}
        if existing:
            status_label = {'await_payment': '等待确认结算方式', 'await_attendance': '等待回复下次出勤',
                            'completed': '已完成', 'amount_issue': '金额有误，等待客服处理'}.get(existing['status'], existing['status'])
            if trigger_source == 'keyword':
                send_message(binding['chat_id'], f"🌙 今天的下班确认已经发过了，当前状态：<b>{escape(status_label)}</b>。",
                             thread_id=int(binding.get('message_thread_id') or 0))
            return {'sent': False, 'existing': True, 'status': existing['status']}
        user = user or {}
        with conn() as c:
            cur = c.execute("""INSERT INTO telegram_closing_confirmations(
                close_date,girl_name,chat_id,chat_title,message_thread_id,trigger_source,
                order_count,girl_earnings,store_profit,non_cash_received,amount_due,
                confirmed_user_id,confirmed_name)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (day, girl, str(binding['chat_id']), str(binding.get('chat_title') or ''),
                 int(binding.get('message_thread_id') or 0), trigger_source,
                 int(totals['order_count'] or 0), int(totals['girl_earnings'] or 0),
                 int(totals['store_profit'] or 0), int(totals['non_cash_received'] or 0),
                 int(totals['amount_due'] or 0), str(user.get('id') or ''), display_name(user) if user else ''))
            closing_id = int(cur.lastrowid)
        sent = send_message(
            binding['chat_id'], closing_summary_text(day, girl, totals),
            inline_keyboard([
                [callback_button("✅ 线上转账", f"closing:pay:{closing_id}:transfer"),
                 callback_button("✅ 线下现金", f"closing:pay:{closing_id}:cash")],
                [callback_button("⚠️ 金额有误", f"closing:pay:{closing_id}:issue")],
            ]), thread_id=int(binding.get('message_thread_id') or 0))
        with conn() as c:
            c.execute("UPDATE telegram_closing_confirmations SET summary_message_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (int((sent or {}).get('message_id') or 0), closing_id))
        return {'sent': True, 'id': closing_id}

    def send_unclosed_prompts(day=None):
        day = day or closing_business_date(tokyo_now())
        with conn() as c:
            girls = [str(r['girl_name']) for r in c.execute("""SELECT DISTINCT girl_name FROM orders
                         WHERE order_date=? AND COALESCE(order_status,'')<>'取消' AND COALESCE(girl_name,'')<>''
                         ORDER BY girl_name""", (day,)).fetchall()]
            bindings = {str(r['girl_name']): dict(r) for r in c.execute(
                "SELECT * FROM telegram_group_bindings WHERE enabled=1").fetchall()}
        sent, existing, unbound, failed = 0, 0, 0, []
        for girl in girls:
            binding = bindings.get(girl)
            if not binding or not valid_group_chat_id(binding.get('chat_id')):
                unbound += 1
                continue
            try:
                result = send_closing_prompt(girl, binding, 'settlement_report', notify_if_empty=False, day=day)
                sent += 1 if result.get('sent') else 0
                existing += 1 if result.get('existing') else 0
            except Exception as exc:
                failed.append(f"{girl}：{str(exc)[:100]}")
        if failed:
            cfg = settings()
            internal = str(cfg.get('default_review_chat_id') or '')
            if internal:
                send_message(internal, "⚠️ <b>闭店确认发送失败</b>\n" + "\n".join(escape(x) for x in failed[:20]),
                             thread_id=int(cfg.get('default_review_thread_id') or 0))
        return {'sent': sent, 'already_sent': existing, 'unbound': unbound, 'failed': failed}

    def start_closing_from_keyword(message, keyword):
        chat, user = message.get('chat') or {}, message.get('from') or {}
        with conn() as c:
            binding = c.execute("""SELECT * FROM telegram_group_bindings WHERE chat_id=? AND enabled=1
                                   ORDER BY updated_at DESC LIMIT 1""", (str(chat.get('id')),)).fetchone()
        # “下班”只处理当前女孩专属群；“闭店”只允许在内部群批量发送。
        if keyword == '下班' and binding:
            send_closing_prompt(binding['girl_name'], dict(binding), 'keyword', user=user)
            return True
        cfg = settings()
        if keyword == '闭店' and str(chat.get('id')) == str(cfg.get('default_review_chat_id') or ''):
            if not (is_manager(user.get('id'), chat.get('id')) or is_chat_admin(chat.get('id'), user.get('id'))):
                send_message(chat.get('id'), "❌ 只有店长、客服或群管理员可以执行闭店。")
                return True
            result = send_unclosed_prompts()
            send_message(chat.get('id'), f"🌙 闭店确认已发送 {result['sent']} 个女孩群；"
                         f"已发过 {result['already_sent']} 个；未绑定跳过 {result['unbound']} 个。",
                         thread_id=int(cfg.get('default_review_thread_id') or 0))
            return True
        return False

    def parse_next_attendance(text):
        raw = str(text or '').strip()
        now = tokyo_now()
        day = None
        if re.search(r"明天", raw):
            day = (now.date() + timedelta(days=1)).isoformat()
        elif re.search(r"后天", raw):
            day = (now.date() + timedelta(days=2)).isoformat()
        else:
            match = re.search(r"(?:(20\d{2})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?", raw)
            explicit_year = False
            if match:
                year, month, day_num = int(match.group(1) or now.year), int(match.group(2)), int(match.group(3))
                explicit_year = bool(match.group(1))
            else:
                compact = re.search(r"(?<!\d)(\d{2})(\d{2})(?!\d)", raw)
                if compact:
                    year, month, day_num = now.year, int(compact.group(1)), int(compact.group(2))
                    match = compact
            if match:
                try:
                    parsed = datetime(year, month, day_num).date()
                    if not explicit_year and parsed < now.date():
                        parsed = parsed.replace(year=parsed.year + 1)
                    day = parsed.isoformat()
                except (ValueError, TypeError):
                    day = None
        time_match = re.search(r"(?<!\d)(\d{1,2})(?:[:.](\d{1,2}))?\s*(?:[-~ー～]|到|至)\s*(\d{1,2})(?:[:.](\d{1,2}))?(?!\d)", raw)
        if not day or not time_match:
            return None
        sh, sm, eh, em = int(time_match.group(1)), int(time_match.group(2) or 0), int(time_match.group(3)), int(time_match.group(4) or 0)
        if sh > 23 or eh > 24 or sm > 59 or em > 59 or (eh == 24 and em):
            return None
        return day, f"{sh:02d}:{sm:02d}", ("00:00" if eh == 24 else f"{eh:02d}:{em:02d}")

    def complete_closing_attendance(row, user, text, parsed=None):
        next_date = start_time = end_time = ''
        if parsed:
            next_date, start_time, end_time = parsed
        with conn() as c:
            if parsed:
                existing = c.execute("SELECT id FROM pure_shifts WHERE shift_date=? AND girl_name=? ORDER BY id LIMIT 1",
                                     (next_date, row['girl_name'])).fetchone()
                memory = c.execute("SELECT tags,gold_tags FROM girl_tag_memory WHERE girl_name=?", (row['girl_name'],)).fetchone()
                if existing:
                    c.execute("""UPDATE pure_shifts SET start_time=?,end_time=?,source='telegram_closing',
                                 note='下班确认填写',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                              (start_time, end_time, int(existing['id'])))
                else:
                    sort_order = int(c.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM pure_shifts WHERE shift_date=?",
                                               (next_date,)).fetchone()[0] or 1)
                    c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags,sort_order,source,note)
                                 VALUES(?,?,?,?,?,?,?,'telegram_closing','下班确认填写')""",
                              (next_date, row['girl_name'], start_time, end_time,
                               str(memory['tags'] or '') if memory else '', str(memory['gold_tags'] or '') if memory else '', sort_order))
                c.execute("""INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order,source,updated_at)
                             VALUES(?,?,9999,'closing_attendance',CURRENT_TIMESTAMP)
                             ON CONFLICT(booking_date,girl_name) DO UPDATE SET source='closing_attendance',updated_at=CURRENT_TIMESTAMP""",
                          (next_date, row['girl_name']))
            c.execute("""UPDATE telegram_closing_confirmations SET status='completed',next_attendance_text=?,
                         next_attendance_date=?,next_start_time=?,next_end_time=?,confirmed_user_id=?,confirmed_name=?,
                         completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (text, next_date, start_time, end_time, str(user.get('id') or ''), display_name(user), int(row['id'])))
        shown = (f"{next_date} {start_time}–{'24:00' if end_time == '00:00' else end_time}"
                 if parsed else (str(text or '').strip() or "暂未确定"))
        send_message(row['chat_id'], f"✅ 下班确认完成。\n结算方式：<b>{escape(row['settlement_method'])}</b>\n下次出勤：<b>{escape(shown)}</b>",
                     thread_id=int(row.get('message_thread_id') or 0))
        cfg = settings()
        internal = str(cfg.get('default_review_chat_id') or '')
        if internal:
            send_message(internal, f"🌙 <b>女孩下班确认完成</b>\n女孩：{escape(row['girl_name'])}\n"
                         f"日期：{escape(row['close_date'])}\n单数：{int(row['order_count'] or 0)}\n"
                         f"女孩到手：¥{int(row['girl_earnings'] or 0):,}\n店铺收益：¥{int(row['store_profit'] or 0):,}\n"
                         f"结算方式：{escape(row['settlement_method'])}\n下次出勤：{escape(shown)}",
                         thread_id=int(cfg.get('default_review_thread_id') or 0))

    def handle_closing_attendance_reply(message):
        chat, user = message.get('chat') or {}, message.get('from') or {}
        text = str(message.get('text') or '').strip()
        reply_id = int(((message.get('reply_to_message') or {}).get('message_id') or 0))
        with conn() as c:
            found = c.execute("""SELECT * FROM telegram_closing_confirmations
                                 WHERE chat_id=? AND status='await_attendance'
                                 ORDER BY id DESC LIMIT 1""", (str(chat.get('id')),)).fetchone()
        row = dict(found) if found else None
        if not row or not (reply_id == int(row.get('attendance_prompt_message_id') or 0) or re.match(r"^下次出勤", text)):
            return False
        answer = re.sub(r"^下次出勤\s*[:：+＋]?\s*", "", text).strip()
        if answer in ('未定', '不确定', '暂未确定', '不知道'):
            complete_closing_attendance(row, user, '暂未确定', None)
            return True
        parsed = parse_next_attendance(answer)
        if not parsed:
            send_message(chat.get('id'), "时间格式没有看懂，请回复本消息，例如：<code>0910 18-23</code>、<code>明天 19:30-24</code>；还没确定可点“暂未确定”。",
                         inline_keyboard([[callback_button("暂未确定", f"closing:attendance:{row['id']}:unknown")]]),
                         thread_id=int(row.get('message_thread_id') or 0))
            return True
        complete_closing_attendance(row, user, answer, parsed)
        return True

    def handle_message(message, edited=False):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "")
        expire_attendance_inquiries()
        if not edited and handle_closing_attendance_reply(message):
            return
        closing_match = None if edited else re.fullmatch(r"/?(下班|闭店)(?:@\w+)?", text.strip())
        if closing_match:
            if start_closing_from_keyword(message, closing_match.group(1)):
                return
        # 编辑旧消息不触发导入；必须重新发送一张带当天 MMDD 标题的完整接龙。
        cached_chain = False if edited else cache_chain_message(message)
        if cached_chain and import_bound_chain_immediately(message):
            return
        if handle_auto_import_control(message):
            return
        if handle_shift_copy_generation(message):
            return
        if handle_customer_lookup(message):
            return
        if re.fullmatch(r"/?询问出勤(?:@\w+)?", text.strip()):
            start_attendance_inquiry(message)
            return
        if text.startswith("/绑定审核群"):
            bind_default_group(message)
            return
        if text.startswith("/绑定女孩"):
            bind_group(message)
            return
        if re.match(r"^/?最新接龙(?:@\w+)?(?:\s|$)", text):
            send_latest_chain_from_group(message)
            return
        if re.match(r"^/?导入(?:接龙)?(?:@\w+)?(?=\s|\d|$)", text):
            import_chain_from_group(message)
            return
        if chat.get("type") != "private":
            return
        session = get_session(user.get("id"))
        if text.startswith("/start"):
            if session and session.get("step") in ("choose_points", "await_points"):
                send_message(chat.get("id"), "请先完成本次积分选择；如不使用积分，请点击“不使用积分”。")
                return
            clear_session(user.get("id"))
            show_home(chat.get("id"))
            return
        if text.startswith("/cancel") or text == "取消":
            if session and session.get("step") in ("choose_points", "await_points"):
                finalize_points(user, chat.get("id"), int(session["payload"].get("reservation_id") or 0), 0)
                return
            clear_session(user.get("id"))
            send_message(chat.get("id"), "本次操作已取消。")
            show_home(chat.get("id"))
            return
        if not session:
            show_home(chat.get("id"))
        elif session["step"] == "await_time":
            submit_reservation(message, session)
        elif session["step"] == "await_hotel":
            receive_hotel(message, session)
        elif session["step"] == "booked":
            rid = int(session["payload"].get("reservation_id") or 0)
            cfg = settings()
            with conn() as c:
                row = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            if row:
                send_message(chat.get("id"), "请点击下方按钮发送酒店资料、申请改期或取消预约。",
                             booking_action_keyboard(dict(row), cfg))
            else:
                clear_session(user.get("id"))
                show_home(chat.get("id"))
        elif session["step"] == "await_points":
            raw = re.sub(r"[^0-9]", "", text)
            if not raw:
                send_message(chat.get("id"), "请输入要使用的积分数字，例如：<code>1000</code>。")
            else:
                finalize_points(user, chat.get("id"), int(session["payload"].get("reservation_id") or 0), int(raw))
        elif session["step"] == "choose_points":
            send_message(chat.get("id"), "请点击上方按钮选择积分使用方式。")
        else:
            show_home(chat.get("id"))

    def handle_callback(callback):
        data = callback.get("data") or ""
        user = callback.get("from") or {}
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if data.startswith("closing:"):
            parts = data.split(':')
            action, closing_id = (parts[1], int(parts[2])) if len(parts) >= 3 else ('', 0)
            with conn() as c:
                found = c.execute("SELECT * FROM telegram_closing_confirmations WHERE id=?", (closing_id,)).fetchone()
            row = dict(found) if found else None
            if not row or str(chat_id) != str(row.get('chat_id')):
                answer_callback(callback.get('id'), "这个下班确认已失效", True)
                return
            if action == 'pay' and len(parts) == 4:
                choice = parts[3]
                if row['status'] != 'await_payment':
                    answer_callback(callback.get('id'), "结算方式已经确认过了", True)
                    return
                if choice == 'issue':
                    with conn() as c:
                        c.execute("""UPDATE telegram_closing_confirmations SET status='amount_issue',confirmed_user_id=?,
                                     confirmed_name=?,confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                                  (str(user.get('id') or ''), display_name(user), closing_id))
                    answer_callback(callback.get('id'), "已通知内部客服")
                    edit_message_text(chat_id, row['summary_message_id'], "⚠️ <b>金额有误</b>，已经通知内部客服核对，请暂时不要交款。")
                    cfg = settings(); internal = str(cfg.get('default_review_chat_id') or '')
                    if internal:
                        send_message(internal, f"⚠️ <b>女孩下班结算金额有误</b>\n女孩：{escape(row['girl_name'])}\n日期：{escape(row['close_date'])}\n请客服核对。",
                                     thread_id=int(cfg.get('default_review_thread_id') or 0))
                    return
                method = '线上转账' if choice == 'transfer' else '线下现金'
                with conn() as c:
                    girl_row = c.execute("SELECT girl_type FROM girls WHERE name=?", (row['girl_name'],)).fetchone()
                    is_full_time = bool(girl_row and '全职' in str(girl_row['girl_type'] or ''))
                    c.execute("""UPDATE telegram_closing_confirmations SET settlement_method=?,status='await_attendance',
                                 confirmed_user_id=?,confirmed_name=?,confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                              (method, str(user.get('id') or ''), display_name(user), closing_id))
                answer_callback(callback.get('id'), f"已确认{method}")
                edit_message_text(chat_id, row['summary_message_id'], closing_summary_text(row['close_date'], row['girl_name'], row) + f"\n\n✅ 已确认：<b>{method}</b>")
                row['settlement_method'] = method
                if is_full_time:
                    complete_closing_attendance(row, user, '全职，无需填写下次出勤', None)
                    return
                prompt = send_message(chat_id, "📅 <b>请告诉我们下次出勤时间</b>\n请直接回复本消息，例如：<code>0910 18-23</code>、<code>明天 19:30-24</code>。",
                                      inline_keyboard([[callback_button("暂未确定", f"closing:attendance:{closing_id}:unknown")]]),
                                      thread_id=int(row.get('message_thread_id') or 0))
                with conn() as c:
                    c.execute("UPDATE telegram_closing_confirmations SET attendance_prompt_message_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                              (int((prompt or {}).get('message_id') or 0), closing_id))
                return
            if action == 'attendance' and len(parts) == 4 and parts[3] == 'unknown':
                if row['status'] != 'await_attendance':
                    answer_callback(callback.get('id'), "这次下班确认已经完成", True)
                    return
                answer_callback(callback.get('id'), "已记录为暂未确定")
                complete_closing_attendance(row, user, '暂未确定', None)
                return
            answer_callback(callback.get('id'), "按钮已失效", True)
            return
        if data.startswith("attendance:"):
            handle_attendance_callback(callback)
            return
        if not data.startswith("full:"):
            answer_callback(callback.get("id"))
        if data == "book":
            show_dates(chat_id, user.get("id"))
        elif data == "flow:home":
            session = get_session(user.get("id"))
            if session and session.get("step") in ("choose_points", "await_points"):
                send_message(chat_id, "请先完成本次积分选择。")
            else:
                clear_session(user.get("id"))
                show_home(chat_id)
        elif data == "flow:dates":
            show_dates(chat_id, user.get("id"))
        elif data.startswith("flow:girls:"):
            show_girls(chat_id, user.get("id"), data.split(":", 2)[2])
        elif data.startswith("flow:time:"):
            _, _, day, girl_ref = data.split(":", 3)
            choose_girl(chat_id, user.get("id"), day, girl_ref)
        elif data == "flow:confirm":
            session = get_session(user.get("id"))
            if not session or session.get("step") != "confirm_time":
                send_message(chat_id, "这个预约确认已经失效，请重新开始。",
                             inline_keyboard([[callback_button("重新开始预约", "book")]]))
            else:
                payload = session.get("payload") or {}
                submit_reservation({"from": user, "chat": msg.get("chat") or {},
                                    "text": f"{payload.get('start_time')}-{payload.get('end_time')}"},
                                   session, confirmed=True)
        elif data == "flow:cancel":
            session = get_session(user.get("id"))
            if session and session.get("step") in ("choose_points", "await_points"):
                finalize_points(user, chat_id, int(session["payload"].get("reservation_id") or 0), 0)
            else:
                clear_session(user.get("id"))
                cfg = settings()
                send_message(chat_id, "本次预约已取消。",
                             inline_keyboard([[callback_button(cfg.get("button_restart") or "重新开始预约", "book")]]))
        elif data.startswith("points:"):
            _, choice, rid_text = data.split(":", 2)
            session = get_session(user.get("id"))
            if not session or session.get("step") not in ("choose_points", "await_points") or int(session["payload"].get("reservation_id") or 0) != int(rid_text):
                send_message(chat_id, "这个积分选择已经失效。")
            elif choice == "custom":
                set_session(user.get("id"), chat_id, "await_points", session["payload"])
                send_message(chat_id, f"当前可用积分：{int(session['payload'].get('available_points') or 0)}\n请输入要使用的积分数字：")
            else:
                requested = int(session["payload"].get("available_points") or 0) if choice == "all" else 0
                finalize_points(user, chat_id, int(rid_text), requested)
        elif data.startswith("booking_actions:"):
            rid = int(data.split(":", 1)[1])
            cfg = settings()
            with conn() as c:
                row = c.execute("""SELECT * FROM customer_reservations
                                   WHERE id=? AND telegram_user_id=? AND status='已确认'""",
                                (rid, str(user.get('id')))).fetchone()
            if row:
                set_session(user.get('id'), chat_id, 'booked', {'reservation_id': rid})
                send_message(chat_id, "预约仍然为你保留着，请选择需要的操作。",
                             booking_action_keyboard(dict(row), cfg))
        elif data.startswith("hotel:"):
            rid = int(data.split(":", 1)[1])
            with conn() as c:
                row = c.execute("""SELECT * FROM customer_reservations
                                   WHERE id=? AND telegram_user_id=? AND status='已确认'""",
                                (rid, str(user.get('id')))).fetchone()
            if not row:
                send_message(chat_id, "没有找到可以发送酒店资料的预约。")
            else:
                set_session(user.get('id'), chat_id, 'await_hotel', {'reservation_id': rid})
                send_message(chat_id,
                             "请在下一条消息发送酒店资料，可以发送：\n• 酒店名称、地址和房号文字\n• 酒店订单截图\n• Telegram 定位\n\n资料会自动转发到对应女孩专属群。",
                             flow_keyboard("flow:home", "暂时不发送"))
        elif data.startswith("cancel_booking:"):
            rid = int(data.split(":", 1)[1])
            cfg = settings()
            with conn() as c:
                count = int(c.execute("SELECT COUNT(*) FROM telegram_customer_cancellations WHERE telegram_user_id=?",
                                      (str(user.get('id')),)).fetchone()[0] or 0)
                row = c.execute("""SELECT id FROM customer_reservations
                                   WHERE id=? AND telegram_user_id=? AND status='已确认'""",
                                (rid, str(user.get('id')))).fetchone()
            if not row:
                send_message(chat_id, "这笔预约当前无法自助取消，请联系人工客服。")
            else:
                consequence = "本次将清空当前累计积分" if count == 0 else "本次将暂停预约资格并加入黑名单"
                send_message(chat_id,
                             f"{cfg.get('text_cancel_policy') or DEFAULT_SETTINGS['text_cancel_policy']}\n\n<b>{escape(consequence)}</b>\n确定要取消这笔预约吗？",
                             inline_keyboard([
                                 [callback_button("确认取消预约", f"cancel_confirm:{rid}")],
                                 [callback_button("我再想想", f"booking_actions:{rid}")],
                             ]))
        elif data.startswith("cancel_confirm:"):
            cancel_reservation_by_customer(user, chat_id, int(data.split(":", 1)[1]))
        elif data.startswith("date:"):
            show_girls(chat_id, user.get("id"), data.split(":", 1)[1])
        elif data.startswith("girl:"):
            _, day, girl = data.split(":", 2)
            choose_girl(chat_id, user.get("id"), day, girl)
        elif data.startswith("full:"):
            answer_callback(callback.get("id"), "该女孩当天已经约满，请选择其他女孩。", True)
        elif data.startswith("approve:"):
            review_reservation(callback, True)
        elif data.startswith("reject:"):
            review_reservation(callback, False)

    @app.route("/telegram/webhook", methods=["POST"])
    def telegram_webhook():
        configured_secret = str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        if configured_secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != configured_secret:
            return jsonify(ok=False, error="webhook secret 错误"), 403
        ensure_db()
        update = request.json or {}
        if update.get("callback_query"):
            handle_callback(update["callback_query"])
        elif update.get("message"):
            handle_message(update["message"])
        elif update.get("edited_message"):
            handle_message(update["edited_message"], edited=True)
        return jsonify(ok=True)

    @app.route("/api/telegram/settings", methods=["GET", "POST"])
    def telegram_settings_api():
        ensure_db()
        if request.method == "POST":
            data = request.json or {}
            allowed = set(DEFAULT_SETTINGS)
            with conn() as c:
                for key in allowed:
                    if key in data:
                        value = "1" if key == "booking_enabled" and bool(data[key]) else ("0" if key == "booking_enabled" else str(data[key] or ""))
                        c.execute("""INSERT INTO telegram_settings(setting_key,setting_value,updated_at)
                                     VALUES(?,?,CURRENT_TIMESTAMP)
                                     ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=CURRENT_TIMESTAMP""", (key, value))
        with conn() as c:
            bindings = [dict(r) for r in c.execute("SELECT * FROM telegram_group_bindings ORDER BY girl_name").fetchall()]
            managers = [dict(r) for r in c.execute("SELECT * FROM telegram_managers ORDER BY updated_at DESC").fetchall()]
        return jsonify(ok=True, settings=settings(), bindings=bindings, managers=managers,
                       token_configured=bool(telegram_token()), bot_username="alice_booking_test_bot")

    @app.route("/api/telegram/bindings", methods=["POST"])
    def telegram_bindings_api():
        ensure_db()
        data = request.json or {}
        girl = str(data.get("girl_name") or "").strip()
        chat_id = str(data.get("chat_id") or "").strip()
        if not girl or not chat_id:
            return jsonify(ok=False, error="女孩和群 ID 不能为空"), 400
        if not valid_group_chat_id(chat_id):
            return jsonify(ok=False, error="群 ID 必须是 Telegram 自动生成的负数，不能填写自定义编号"), 400
        with conn() as c:
            c.execute("""INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,message_thread_id,enabled,updated_at)
                         VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(girl_name) DO UPDATE SET chat_id=excluded.chat_id,chat_title=excluded.chat_title,
                         message_thread_id=excluded.message_thread_id,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
                      (girl, chat_id, str(data.get("chat_title") or ""), int(data.get("message_thread_id") or 0), 1 if data.get("enabled", True) else 0))
        return jsonify(ok=True)

    @app.route("/api/telegram/daily-girls", methods=["POST"])
    def telegram_daily_girls_api():
        ensure_db()
        data = request.json or {}
        day = str(data.get("date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return jsonify(ok=False, error="预约日期格式错误"), 400
        action = str(data.get("action") or "get")
        with conn() as c:
            attendance = []
            seen = set()
            for shift in pure_shift_rows_for_date(c, day):
                name = str(shift.get("girl") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    attendance.append(name)
            if action == "sync":
                c.execute("DELETE FROM telegram_daily_girls WHERE booking_date=?", (day,))
                for index, name in enumerate(attendance):
                    c.execute("""INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order,source)
                                 VALUES(?,?,?,'attendance')""", (day, name, index))
            elif action == "save":
                names = []
                known = {r["name"] for r in c.execute("SELECT name FROM girls").fetchall()}
                for raw_name in data.get("girls") or []:
                    name = str(raw_name or "").strip()
                    if name and name in known and name not in names:
                        names.append(name)
                c.execute("DELETE FROM telegram_daily_girls WHERE booking_date=?", (day,))
                for index, name in enumerate(names):
                    c.execute("""INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order,source)
                                 VALUES(?,?,?,'manual')""", (day, name, index))
            elif action != "get":
                return jsonify(ok=False, error="不支持的操作"), 400
            selected = [r["girl_name"] for r in c.execute(
                "SELECT girl_name FROM telegram_daily_girls WHERE booking_date=? ORDER BY sort_order,girl_name",
                (day,)).fetchall()]
            bound = {r["girl_name"] for r in c.execute(
                "SELECT girl_name,chat_id FROM telegram_group_bindings WHERE enabled=1").fetchall()
                     if valid_group_chat_id(r["chat_id"])}
        rows = [{"girl_name": name, "bound": name in bound} for name in selected]
        return jsonify(ok=True, date=day, girls=rows, attendance=attendance)

    @app.route("/api/telegram/webhook/setup", methods=["POST"])
    def telegram_webhook_setup_api():
        ensure_db()
        public_url = str((request.json or {}).get("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
        if not public_url.startswith("https://"):
            return jsonify(ok=False, error="请先配置 HTTPS PUBLIC_BASE_URL"), 400
        secret = str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        result = tg("setWebhook", {"url": public_url + "/telegram/webhook", "secret_token": secret or None,
                                   "allowed_updates": ["message", "edited_message", "callback_query"]})
        return jsonify(ok=True, result=result, webhook_url=public_url + "/telegram/webhook")

    @app.route("/api/telegram/report-photo", methods=["POST"])
    def telegram_report_photo_api():
        ensure_db()
        data = request.json or {}
        kind = str(data.get("kind") or "").strip()
        day = str(data.get("date") or "").strip()
        raw_items = data.get("image_data_list") or [data.get("image_data")]
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        if kind not in ("pure_shift", "settlement") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return jsonify(ok=False, error="报表类型或日期不正确"), 400
        if not raw_items or len(raw_items) > 6 or any(not str(raw or "").startswith("data:image/png;base64,") for raw in raw_items):
            return jsonify(ok=False, error="请先生成 PNG 截图"), 400
        try:
            image_bytes_list = [base64.b64decode(str(raw).split(",", 1)[1], validate=True) for raw in raw_items]
        except Exception:
            return jsonify(ok=False, error="截图数据损坏，请重新生成"), 400
        if any(not image_bytes or len(image_bytes) > 10 * 1024 * 1024 for image_bytes in image_bytes_list):
            return jsonify(ok=False, error="截图为空或超过10MB"), 400
        image_bytes = image_bytes_list[0]
        # 官网直接使用与 Telegram 完全相同的原始 PNG；多页时全部保存，避免二次渲染造成偏色和清晰度下降。
        wordpress_image_bytes_list = image_bytes_list
        cfg = settings()
        chat_id = str(cfg.get("default_review_chat_id") or "")
        if not valid_group_chat_id(chat_id):
            return jsonify(ok=False, error="请先在内部群发送 /绑定审核群"), 400
        thread_id = int(cfg.get("default_review_thread_id") or 0)
        synced = 0
        names = []
        all_girl_names = []
        girl_prices = {}
        wordpress_result = {"configured": False, "synced": False}
        if kind == "pure_shift":
            with conn() as c:
                girl_rows = c.execute(
                    "SELECT name,girl_alias,list_price FROM girls WHERE COALESCE(name,'')!='' ORDER BY id DESC").fetchall()
                all_girl_names = [str(r["name"] or "").strip() for r in girl_rows]
                girl_prices = {str(r["name"] or "").strip(): {
                    "price": int(r["list_price"] or 0), "alias": str(r["girl_alias"] or "").strip()
                } for r in girl_rows}
                for shift in pure_shift_rows_for_date(c, day):
                    name = str(shift.get("girl") or "").strip()
                    if name and name not in names:
                        names.append(name)
            synced = len(names)
            caption = f"📋 <b>{escape(day)} 爱丽丝出勤表</b>\n已同步 TEL预约女孩：{synced}人"
            if callable(sync_wordpress_attendance):
                wordpress_result = sync_wordpress_attendance(
                    day, wordpress_image_bytes_list, str(data.get("service_text") or ""), names, all_girl_names,
                    girl_prices)
        else:
            caption = f"💴 <b>{escape(day)} 今日金额结算</b>"
        result = None
        message_ids = []
        for page_index, page_bytes in enumerate(image_bytes_list):
            page_caption = caption
            if len(image_bytes_list) > 1:
                page_caption += f"\n图片 {page_index + 1}/{len(image_bytes_list)}"
            if kind == "settlement":
                page_result = send_document_bytes(
                    chat_id, page_bytes, page_caption, thread_id,
                    filename=f"alice-settlement-{day}-{page_index + 1}.png")
            else:
                page_result = send_photo_bytes(chat_id, page_bytes, page_caption, thread_id)
            if result is None:
                result = page_result
            message_ids.append(int((page_result or {}).get("message_id") or 0))
        sync_warnings = []
        if kind == "pure_shift":
            avatar_sync = data.get("avatar_sync") if isinstance(data.get("avatar_sync"), dict) else {}
            avatar_missing = [str(x).strip() for x in avatar_sync.get("missing") or [] if str(x).strip()]
            avatar_errors = [str((x or {}).get("girl_name") or "").strip()
                             for x in avatar_sync.get("errors") or [] if isinstance(x, dict)]
            avatar_unrecognized = list(dict.fromkeys([x for x in avatar_missing + avatar_errors if x]))
            if avatar_unrecognized:
                sync_warnings.append("东京YY头像未识别（已沿用女孩表已保存头像）：" + "、".join(avatar_unrecognized[:30]))
            if not wordpress_result.get("synced"):
                sync_warnings.append("官网同步失败：" + str(wordpress_result.get("warning") or "官网账号尚未配置"))
            else:
                visibility = wordpress_result.get("visibility") or {}
                unmatched = [str(x) for x in visibility.get("unmatched_attendance") or [] if str(x).strip()]
                if unmatched:
                    sync_warnings.append("官网女孩管理未识别出勤女孩：" + "、".join(unmatched[:30]))
                failed = visibility.get("failed") or []
                if failed:
                    failed_names = []
                    for item in failed:
                        name = str((item or {}).get("girl") or "未知女孩")
                        if name not in failed_names:
                            failed_names.append(name)
                    sync_warnings.append("官网公开/私密状态更新失败：" + "、".join(failed_names[:30]))
                if not visibility.get("synced") and visibility.get("warning"):
                    sync_warnings.append("官网女孩状态同步失败：" + str(visibility.get("warning")))
                price_categories = wordpress_result.get("price_categories") or {}
                price_unmatched = [str(x) for x in price_categories.get("unmatched") or [] if str(x).strip()]
                if price_unmatched:
                    sync_warnings.append("官网价格分类未匹配女孩（请在女孩表填写官网马甲）：" + "、".join(price_unmatched[:30]))
                if not price_categories.get("synced"):
                    detail = price_categories.get("warning")
                    if not detail and price_categories.get("missing_terms"):
                        detail = "缺少价格分类 " + "、".join(str(x) for x in price_categories.get("missing_terms") or [])
                    if not detail and price_categories.get("failed"):
                        detail = str((price_categories.get("failed") or [{}])[0].get("error") or "部分女孩更新失败")
                    sync_warnings.append("官网每小时价格分类同步失败：" + str(detail or "请检查女孩名称和定价"))
            if sync_warnings:
                warning_text = "⚠️ <b>全面同步检查</b>\n" + "\n".join("• " + escape(x) for x in sync_warnings)
                try:
                    send_message(chat_id, warning_text[:3900], thread_id=thread_id)
                except Exception:
                    pass
        if kind == "pure_shift":
            with conn() as c:
                c.execute("DELETE FROM telegram_daily_girls WHERE booking_date=?", (day,))
                for index, name in enumerate(names):
                    c.execute("""INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order,source)
                                 VALUES(?,?,?,'attendance_send')""", (day, name, index))
                late_enabled = 1 if day == tokyo_now().date().isoformat() and tokyo_now().hour >= 22 else 0
                c.execute("""INSERT INTO telegram_full_sync_days(sync_date,full_synced_at,late_auto_enabled,updated_at)
                             VALUES(?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP)
                             ON CONFLICT(sync_date) DO UPDATE SET full_synced_at=CURRENT_TIMESTAMP,
                             late_auto_enabled=excluded.late_auto_enabled,updated_at=CURRENT_TIMESTAMP""",
                          (day, late_enabled))
        # 结算截图只发送截图；女孩下班确认只能由群内“闭店”关键字触发。
        closing_result = {}
        return jsonify(ok=True, message_id=int((result or {}).get("message_id") or 0), message_ids=message_ids, synced=synced,
                       wordpress=wordpress_result,
                       sync_warnings=sync_warnings,
                       closing=closing_result,
                       chat_title=cfg.get("default_review_chat_title") or "Alice内部群")

    @app.route("/api/telegram/chain-import/run", methods=["POST"])
    def telegram_chain_import_run_api():
        return jsonify(ok=True, **run_pending_chain_imports(force=True))

    def send_new_customer_digest(report_day=None, force=False):
        report_day = str(report_day or (tokyo_now().date() - timedelta(days=1)).isoformat())[:10]
        cfg = settings()
        chat_id = str(cfg.get('default_review_chat_id') or '')
        if not valid_group_chat_id(chat_id):
            return {'sent':False,'date':report_day,'count':0,'reason':'未绑定内部群'}
        with conn() as c:
            existing = c.execute('SELECT * FROM telegram_customer_digests WHERE digest_date=?', (report_day,)).fetchone()
            if existing and not force:
                return {'sent':False,'date':report_day,'count':int(existing['customer_count'] or 0),'reason':'已发送'}
            customer_rows = c.execute("""SELECT customer_no,name,created_at FROM customers
                                         WHERE date(datetime(created_at,'+9 hours'))=?
                                         ORDER BY created_at,id""", (report_day,)).fetchall()
        if not customer_rows:
            with conn() as c:
                c.execute("""INSERT INTO telegram_customer_digests(digest_date,customer_count,message_id,sent_at)
                             VALUES(?,0,0,CURRENT_TIMESTAMP)
                             ON CONFLICT(digest_date) DO UPDATE SET customer_count=0,sent_at=CURRENT_TIMESTAMP""", (report_day,))
            return {'sent':False,'date':report_day,'count':0,'reason':'没有新客户'}
        lines = [f"🐰 <b>{escape(report_day)} 新增客户：{len(customer_rows)} 人</b>"]
        for row in customer_rows[:80]:
            created = str(row['created_at'] or '')[11:16]
            lines.append(f"• <b>{escape(row['customer_no'] or '未编号')}</b>｜{escape(row['name'] or '未填写')}｜{escape(created)}")
        if len(customer_rows) > 80:
            lines.append(f"• 其余 {len(customer_rows)-80} 人请在 MCR 客户表查看")
        lines.extend(['', '请客服把 Telegram／通讯录里的客人名字改成客户编号，完成后再核对一次。'])
        message = send_message(chat_id, '\n'.join(lines)[:3900], thread_id=int(cfg.get('default_review_thread_id') or 0))
        message_id = int((message or {}).get('message_id') or 0)
        with conn() as c:
            c.execute("""INSERT INTO telegram_customer_digests(digest_date,customer_count,message_id,sent_at)
                         VALUES(?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(digest_date) DO UPDATE SET customer_count=excluded.customer_count,
                         message_id=excluded.message_id,sent_at=CURRENT_TIMESTAMP""",
                      (report_day,len(customer_rows),message_id))
        return {'sent':True,'date':report_day,'count':len(customer_rows),'message_id':message_id}

    @app.route("/api/telegram/new-customer-digest/run", methods=["POST"])
    def telegram_new_customer_digest_run_api():
        payload = request.get_json(silent=True) or {}
        return jsonify(ok=True, **send_new_customer_digest(payload.get('date'), bool(payload.get('force'))))

    def auto_chain_import_loop():
        public_url = str(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
        render_host = str(os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        if not public_url and render_host:
            public_url = "https://" + render_host
        if public_url.startswith("https://"):
            try:
                tg("setWebhook", {"url": public_url + "/telegram/webhook",
                                   "secret_token": str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip() or None,
                                   "allowed_updates": ["message", "edited_message", "callback_query"]})
            except Exception:
                pass
        while True:
            time.sleep(60)
            try:
                expire_attendance_inquiries()
                send_new_customer_digest()
                run_pending_chain_imports(force=False)
            except Exception:
                pass

    ensure_db()
    if str(os.environ.get("ALICE_DISABLE_CHAIN_SCHEDULER") or "") != "1":
        threading.Thread(target=auto_chain_import_loop, name="alice-chain-import", daemon=True).start()
