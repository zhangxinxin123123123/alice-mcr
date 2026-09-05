import json
import os
import re
from html import escape
from datetime import timedelta
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

    def send_message(chat_id, text, keyboard=None, thread_id=0):
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            data["reply_markup"] = keyboard
        if int(thread_id or 0):
            data["message_thread_id"] = int(thread_id)
        return tg("sendMessage", data)

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
                "SELECT * FROM telegram_group_bindings WHERE enabled=1").fetchall()}
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
        start = time_to_min(shift.get("start") or shift.get("start_time"))
        end = time_to_min(shift.get("end") or shift.get("end_time"))
        if start is None or end is None:
            return []
        if end <= start:
            end += 24 * 60
        cutoff = current_business_minute_for_date(day, tokyo_now())
        if cutoff is not None:
            start = max(start, cutoff)
            if start >= end:
                return []
        busy = []
        for row in c.execute("SELECT service_time FROM orders WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'')!='取消'", (day, girl)).fetchall():
            period = service_range_minutes(row["service_time"])
            if period:
                busy.append(period)
        for row in c.execute("""SELECT id,start_time,end_time FROM customer_reservations
                              WHERE reserve_date=? AND girl_name=? AND status IN ('待确认','已确认')""", (day, girl)).fetchall():
            if int(row["id"]) == int(exclude_reservation_id or 0):
                continue
            a, b = time_to_min(row["start_time"]), time_to_min(row["end_time"])
            if a is not None and b is not None:
                if b <= a:
                    b += 24 * 60
                busy.append((a, b))
        free = [(start, end)]
        for ba, bb in sorted(busy):
            next_free = []
            for fa, fb in free:
                if bb <= fa or ba >= fb:
                    next_free.append((fa, fb))
                else:
                    if fa < ba:
                        next_free.append((fa, ba))
                    if bb < fb:
                        next_free.append((bb, fb))
            free = next_free
        return [(a, b) for a, b in free if b - a >= 30]

    def show_home(chat_id):
        cfg = settings()
        rows = [[callback_button("开始预约", "book")]]
        links = []
        if cfg.get("website_url"):
            links.append(url_button("官方网站", cfg["website_url"]))
        if cfg.get("hotel_url"):
            links.append(url_button("推荐酒店", cfg["hotel_url"]))
        if links:
            rows.append(links)
        send_message(chat_id, cfg.get("welcome_text") or DEFAULT_SETTINGS["welcome_text"], inline_keyboard(rows))

    def flow_keyboard(back_data=None, back_text="⬅️ 返回上一层"):
        rows = []
        if back_data:
            rows.append([callback_button(back_text, back_data)])
        rows.append([callback_button("❌ 取消预约", "flow:cancel")])
        return inline_keyboard(rows)

    def show_dates(chat_id, user_id):
        if settings().get("booking_enabled") != "1":
            send_message(chat_id, "目前预约功能暂时关闭，请稍后再试。")
            return
        now = tokyo_now()
        buttons = []
        for offset in range(0, 2):
            day = now.date() + timedelta(days=offset)
            label = "今天" if offset == 0 else "明天"
            buttons.append([callback_button(f"{label} {day.month}/{day.day}", f"date:{day.isoformat()}")])
        buttons.append([callback_button("⬅️ 返回首页", "flow:home"), callback_button("❌ 取消", "flow:cancel")])
        set_session(user_id, chat_id, "choose_date", {})
        send_message(chat_id, "请选择预约日期：", inline_keyboard(buttons))

    def show_girls(chat_id, user_id, day):
        girls = eligible_girls(day)
        if not girls:
            send_message(chat_id, "这一天暂时没有开放 Bot 预约的女孩。",
                         flow_keyboard("flow:dates", "⬅️ 重新选择日期"))
            return
        rows = [[callback_button(item["girl"], f"girl:{day}:{item['profile']['id']}")] for item in girls]
        rows.append([callback_button("⬅️ 返回选择日期", "flow:dates"), callback_button("❌ 取消", "flow:cancel")])
        set_session(user_id, chat_id, "choose_girl", {"date": day})
        send_message(chat_id, f"{escape(day)} 可预约女孩：", inline_keyboard(rows))

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
        send_message(chat_id, f"你选择了 <b>{escape(girl)}</b>。\n\n可预约：{escape(free_text)}\n\n请发送时间，例如：<code>19-20</code>、<code>19:30-21:00</code>。",
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
        if not item.get("binding"):
            send_message(chat.get("id"), "该女孩还没有可接收预约的群，请联系店长绑定默认审核群。",
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
                    [callback_button("✅ 确定预约", "flow:confirm")],
                    [callback_button("⬅️ 修改时间", f"flow:time:{day}:{item['profile']['id']}"),
                     callback_button("重新选女孩", f"flow:girls:{day}")],
                    [callback_button("❌ 取消预约", "flow:cancel")],
                ])
                send_message(chat.get("id"),
                             f"请确认预约：\n\n女孩：<b>{escape(girl)}</b>\n日期：{escape(day)}\n时间：<b>{escape(start_text)}-{escape(end_text)}</b>",
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
        try:
            sent = send_message(item["binding"]["chat_id"], group_text,
                                inline_keyboard([[callback_button("✅ 店长批准", f"approve:{rid}"), callback_button("❌ 拒绝", f"reject:{rid}")]]),
                                item["binding"].get("message_thread_id") or 0)
            with conn() as c:
                c.execute("UPDATE customer_reservations SET telegram_message_id=? WHERE id=?", (int(sent.get("message_id") or 0), rid))
        except Exception as exc:
            with conn() as c:
                c.execute("UPDATE customer_reservations SET status='发送失败',note=? WHERE id=?", (f"Telegram 群发送失败：{exc}", rid))
            send_message(chat.get("id"), "预约没有成功发送到女孩群，请联系人工客服。")
            clear_session(user.get("id"))
            return
        clear_session(user.get("id"))
        send_message(chat.get("id"), "预约已经交给店长审核，请稍等。",
                     inline_keyboard([[callback_button("🏠 返回首页", "flow:home")]]))

    def review_reservation(callback, approve):
        user = callback.get("from") or {}
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        rid = int((callback.get("data") or "0").split(":", 1)[1])
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
            order_id = int(row.get("order_id") or 0)
            if approve and not order_id:
                girl_row = c.execute("SELECT id FROM girls WHERE name=?", (row["girl_name"],)).fetchone()
                create_or_update_order(c, {
                    "order_date": row["reserve_date"], "girl_id": int(girl_row["id"]) if girl_row else 0,
                    "girl_name": row["girl_name"], "service_time": f"{row['start_time']}-{row['end_time']}",
                    "received_amount": int(row["price"] or 0), "customer_raw": row.get("username") or "Telegram客人",
                    "remark": "Telegram Bot 预约", "order_status": "预约中", "settlement_status": "未结算",
                })
                order_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
            c.execute("UPDATE customer_reservations SET status=?,order_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (new_status, order_id, rid))
        answer_callback(callback.get("id"), "已处理")
        reviewer = display_name(user)
        if approve:
            cfg = settings()
            rows = []
            if cfg.get("hotel_url"):
                rows.append([url_button("推荐酒店", cfg["hotel_url"])])
            send_message(row["telegram_chat_id"],
                         f"预约成功！{row['girl_name']} {row['reserve_date']} {row['start_time']}-{row['end_time']} 已经为你留好。\n\n开好酒店后，请直接发送酒店截图、地址或定位。",
                         inline_keyboard(rows) if rows else None)
            set_session(row["telegram_user_id"], row["telegram_chat_id"], "await_hotel", {"reservation_id": rid})
            send_message(chat.get("id"), f"✅ 预约 #{rid} 已由 {escape(reviewer)} 批准。")
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
        thread_id = 0
        with conn() as c:
            binding = c.execute("SELECT * FROM telegram_group_bindings WHERE girl_name=?", (row["girl_name"],)).fetchone()
            if binding:
                thread_id = int(binding["message_thread_id"] or 0)
        header = f"🏨 预约 #{rid} 的酒店资料\n女孩：{row['girl_name']}\n时间：{row['reserve_date']} {row['start_time']}-{row['end_time']}"
        try:
            if message.get("photo"):
                data = {"chat_id": target, "photo": file_id, "caption": f"{header}\n{caption}"}
                if thread_id:
                    data["message_thread_id"] = thread_id
                tg("sendPhoto", data)
            elif message.get("document"):
                data = {"chat_id": target, "document": file_id, "caption": f"{header}\n{caption}"}
                if thread_id:
                    data["message_thread_id"] = thread_id
                tg("sendDocument", data)
            elif message.get("location"):
                loc = message["location"]
                tg("sendLocation", {"chat_id": target, "latitude": loc["latitude"], "longitude": loc["longitude"],
                                    "message_thread_id": thread_id or None})
                send_message(target, header, thread_id=thread_id)
            elif message.get("text"):
                send_message(target, f"{escape(header)}\n地址：{escape(caption)}", thread_id=thread_id)
            else:
                send_message(chat.get("id"), "请发送酒店截图、地址文字或 Telegram 定位。")
                return
        except Exception:
            send_message(chat.get("id"), "酒店资料发送失败，请稍后重试或联系人工客服。")
            return
        with conn() as c:
            c.execute("UPDATE customer_reservations SET hotel_file_id=?,hotel_caption=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (file_id, caption, rid))
        clear_session(user.get("id"))
        send_message(chat.get("id"), "酒店资料已经发送给店长和女孩。")

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

    def handle_message(message):
        chat, user = message.get("chat") or {}, message.get("from") or {}
        text = str(message.get("text") or "")
        if text.startswith("/绑定审核群"):
            bind_default_group(message)
            return
        if text.startswith("/绑定女孩"):
            bind_group(message)
            return
        if chat.get("type") != "private":
            return
        if text.startswith("/start"):
            clear_session(user.get("id"))
            show_home(chat.get("id"))
            return
        if text.startswith("/cancel") or text == "取消":
            clear_session(user.get("id"))
            send_message(chat.get("id"), "本次操作已取消。")
            show_home(chat.get("id"))
            return
        session = get_session(user.get("id"))
        if not session:
            show_home(chat.get("id"))
        elif session["step"] == "await_time":
            submit_reservation(message, session)
        elif session["step"] == "await_hotel":
            receive_hotel(message, session)
        else:
            show_home(chat.get("id"))

    def handle_callback(callback):
        data = callback.get("data") or ""
        user = callback.get("from") or {}
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        answer_callback(callback.get("id"))
        if data == "book":
            show_dates(chat_id, user.get("id"))
        elif data == "flow:home":
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
            clear_session(user.get("id"))
            send_message(chat_id, "本次预约已取消。", inline_keyboard([[callback_button("重新开始预约", "book")]]))
        elif data.startswith("date:"):
            show_girls(chat_id, user.get("id"), data.split(":", 1)[1])
        elif data.startswith("girl:"):
            _, day, girl = data.split(":", 2)
            choose_girl(chat_id, user.get("id"), day, girl)
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
                "SELECT girl_name FROM telegram_group_bindings WHERE enabled=1").fetchall()}
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
