import json
import os
import re
from html import escape
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import jsonify, request


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
}


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
    recalc_customer_points,
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

    def refresh_daily_chain(row, cfg, new_order_id=0):
        """Create or update the girl's single daily chain message from current MCR orders."""
        target, thread_id = target_thread_id(row, cfg)
        day = str(row["reserve_date"])
        girl = str(row["girl_name"])
        with conn() as c:
            orders = [dict(x) for x in c.execute("""SELECT * FROM orders
                                                   WHERE order_date=? AND girl_name=?
                                                     AND COALESCE(order_status,'')!='取消'
                                                   ORDER BY id""", (day, girl)).fetchall()]
            registry = c.execute("""SELECT message_id FROM telegram_daily_chain_messages
                                    WHERE booking_date=? AND girl_name=? AND chat_id=?
                                      AND message_thread_id=?""",
                                 (day, girl, str(target), int(thread_id or 0))).fetchone()
            message_id = int(registry["message_id"] or 0) if registry else 0
            if not message_id:
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
        for index, order in enumerate(orders, 1):
            chain_line = escape(order_to_chain_line(order, index, False))
            if int(order.get("id") or 0) == int(new_order_id or 0):
                # Telegram 不支持自定义文字颜色，用彩色标记和粗体突出本次新增。
                lines.append(f"🟣 <b>NEW｜{chain_line}</b>")
            else:
                lines.append(f"▫️ {chain_line}")
        text = "\n".join(lines)

        sent_message_id = message_id
        if message_id:
            try:
                edit_message_text(target, message_id, text)
            except Exception:
                sent = send_message(target, text, thread_id=thread_id)
                sent_message_id = int(sent.get("message_id") or 0)
        else:
            sent = send_message(target, text, thread_id=thread_id)
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
                recalc_customer_points(c, customer_id, update_types=False)

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
                send_message(chat.get("id"), f"MCR 女孩表中没有找到“{girl}”。")
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
        girl_id = None
        with conn() as c:
            binding = c.execute("""SELECT b.girl_name,g.id AS girl_id FROM telegram_group_bindings b
                                  LEFT JOIN girls g ON g.name=b.girl_name
                                  WHERE b.chat_id=? AND b.enabled=1 ORDER BY b.updated_at DESC LIMIT 1""",
                                (str(chat.get("id")),)).fetchone()
            if binding:
                girl_id = binding["girl_id"]
        if not (is_manager(user.get("id"), chat.get("id")) or is_chat_admin(chat.get("id"), user.get("id"))):
            notify_internal(f"❌ 接龙导入被拒绝：{escape(display_name(user))} 不是店长、客服或该群管理员。")
            return
        try:
            source_message_id = reply.get("message_id") or message.get("message_id") or ""
            result = import_chain_text(
                chain_text, girl_id=girl_id, settlement_status="未结算",
                source_chat_id=chat.get("id"), source_message_id=source_message_id,
            )
            notify_internal(
                f"✅ MCR 接龙导入完成\n来源群：<b>{escape(chat.get('title') or str(chat.get('id')))}</b>"
                f"\n女孩：<b>{escape(result['girl_name'])}</b>\n日期：{escape(result['order_date'])}"
                f"\n新增：{int(result['inserted'])} 单｜修改：{int(result['updated'])} 单｜未变化：{int(result['unchanged'])} 单"
            )
        except Exception as exc:
            notify_internal(f"❌ 接龙导入失败\n来源群：<b>{escape(chat.get('title') or str(chat.get('id')))}</b>\n原因：{escape(str(exc))}")

    def handle_message(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "")
        if text.startswith("/绑定审核群"):
            bind_default_group(message)
            return
        if text.startswith("/绑定女孩"):
            bind_group(message)
            return
        if re.match(r"^/?导入(?:接龙)?(?:@\w+)?(?:\s|$)", text):
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
        return jsonify(ok=True)

    @app.route("/api/telegram/settings", methods=["GET", "POST"])
    def telegram_settings_api():
        ensure_db()
        if request.method == "POST":
            if request.headers.get("X-Alice-Role") not in ("boss", "admin"):
                return jsonify(ok=False, error="只有老板或管理员可以修改 Telegram 设置"), 403
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
        if request.headers.get("X-Alice-Role") not in ("boss", "admin"):
            return jsonify(ok=False, error="只有老板或管理员可以修改群绑定"), 403
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
        if request.headers.get("X-Alice-Role") not in ("boss", "admin"):
            return jsonify(ok=False, error="只有老板或管理员可以维护预约女孩"), 403
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
        if request.headers.get("X-Alice-Role") not in ("boss", "admin"):
            return jsonify(ok=False, error="只有老板或管理员可以设置 Webhook"), 403
        public_url = str((request.json or {}).get("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
        if not public_url.startswith("https://"):
            return jsonify(ok=False, error="请先配置 HTTPS PUBLIC_BASE_URL"), 400
        secret = str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        result = tg("setWebhook", {"url": public_url + "/telegram/webhook", "secret_token": secret or None,
                                   "allowed_updates": ["message", "callback_query"]})
        return jsonify(ok=True, result=result, webhook_url=public_url + "/telegram/webhook")

    ensure_db()
