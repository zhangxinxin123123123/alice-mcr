import importlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta


class FakeTelegramResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TelegramBookingFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="alice-telegram-test-")
        os.environ["ALICE_DB_PATH"] = os.path.join(cls.temp_dir, "test.db")
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["ALICE_DISABLE_CHAIN_SCHEDULER"] = "1"
        cls.app_module = importlib.import_module("app")
        cls.telegram_module = importlib.import_module("telegram_booking")
        cls.telegram_calls = []

        def fake_urlopen(req, timeout=20):
            method = req.full_url.rsplit("/", 1)[-1]
            body = (req.data or b"").decode("utf-8")
            cls.telegram_calls.append((method, body))
            if method == "getChatMember":
                result = {"status": "administrator"}
            elif method in ("sendMessage", "editMessageText", "sendPhoto", "sendDocument", "sendLocation"):
                result = {"message_id": len(cls.telegram_calls)}
            else:
                result = True
            return FakeTelegramResponse({"ok": True, "result": result})

        cls.telegram_module.urlopen = fake_urlopen
        cls.client = cls.app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        self.telegram_calls.clear()
        day = (self.app_module._tokyo_now().date() + timedelta(days=1)).isoformat()
        self.day = day
        with self.app_module.conn() as c:
            for table in ("orders", "customer_reservations", "customers", "pure_shifts", "girls",
                          "telegram_group_bindings", "telegram_managers", "telegram_booking_sessions",
                          "telegram_daily_girls", "telegram_customers", "chain_import_rows",
                          "telegram_customer_cancellations", "telegram_daily_chain_messages",
                          "telegram_chain_inbox", "telegram_attendance_inquiries"):
                c.execute(f"DELETE FROM {table}")
            for key, value in self.telegram_module.DEFAULT_SETTINGS.items():
                c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES(?,?)
                             ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                          (key, value))
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('娜娜子','在职',15000)")
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('有房女孩','在职',15000)")
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (day, "娜娜子", "19:00", "23:00", "", ""))
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (day, "有房女孩", "19:00", "23:00", "", "房间"))
            c.execute("INSERT INTO telegram_daily_girls(booking_date,girl_name,sort_order) VALUES(?,?,?)",
                      (day, "娜娜子", 0))

    def webhook(self, payload):
        response = self.client.post("/telegram/webhook", json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def test_complete_booking_approval_and_hotel_photo_flow(self):
        self.webhook({"message": {
            "message_id": 0,
            "chat": {"id": -90000, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9001, "first_name": "店长"},
            "text": "/绑定审核群",
        }})
        self.webhook({"message": {
            "message_id": 1,
            "chat": {"id": -10001, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9001, "first_name": "店长"},
            "text": "/绑定女孩 娜娜子",
        }})

        with self.app_module.conn() as c:
            binding = c.execute("SELECT * FROM telegram_group_bindings WHERE girl_name='娜娜子'").fetchone()
            self.assertEqual(binding["chat_title"], "Alice内部群")
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('0001','测试客人',1000)")
            customer_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO telegram_customers(telegram_user_id,customer_id) VALUES('7001',?)", (customer_id,))

        private_chat = {"id": 7001, "type": "private"}
        customer = {"id": 7001, "first_name": "测试客人", "username": "guest"}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
        self.webhook({"callback_query": {"id": "c1", "from": customer,
                                          "data": f"girl:{self.day}:{girl_id}", "message": {"chat": private_chat}}})
        self.webhook({"message": {"message_id": 2, "chat": private_chat, "from": customer, "text": "19-20"}})
        self.webhook({"callback_query": {"id": "c1-confirm", "from": customer,
                                          "data": "flow:confirm", "message": {"chat": private_chat}}})

        with self.app_module.conn() as c:
            reservation = dict(c.execute("SELECT * FROM customer_reservations").fetchone())
            self.assertEqual(reservation["status"], "待确认")
            self.assertEqual(reservation["telegram_group_chat_id"], "-10001")
            rid = reservation["id"]

        self.webhook({"callback_query": {
            "id": "c2", "from": {"id": 9001, "first_name": "店长"}, "data": f"approve:{rid}",
            "message": {"chat": {"id": -90000, "type": "supergroup", "title": "Alice内部群"}},
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            self.assertEqual(reservation["status"], "已确认")
            self.assertEqual(int(reservation["order_id"]), 0)

        self.webhook({"callback_query": {
            "id": "c2-points", "from": customer, "data": f"points:all:{rid}",
            "message": {"chat": private_chat},
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            self.assertGreater(int(reservation["order_id"]), 0)
            self.assertEqual(int(reservation["points_used"]), 1000)
            self.assertEqual(int(reservation["actual_payment"]), 14000)

        self.webhook({"callback_query": {
            "id": "c2-hotel", "from": customer, "data": f"hotel:{rid}",
            "message": {"chat": private_chat},
        }})
        self.webhook({"message": {
            "message_id": 3, "chat": private_chat, "from": customer,
            "photo": [{"file_id": "small"}, {"file_id": "hotel-photo"}], "caption": "酒店测试房间",
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            self.assertEqual(reservation["hotel_file_id"], "hotel-photo")
        self.assertTrue(any(method == "sendPhoto" for method, _body in self.telegram_calls))
        self.assertTrue(any(method == "sendMessage" and "chat_id=-10001" in body and "%E6%8E%A5%E9%BE%99" in body
                            for method, body in self.telegram_calls))

        with self.app_module.conn() as c:
            c.execute("UPDATE customers SET points=900 WHERE id=?", (customer_id,))
        self.webhook({"callback_query": {
            "id": "c2-cancel-prompt", "from": customer, "data": f"cancel_booking:{rid}",
            "message": {"chat": private_chat},
        }})
        self.webhook({"callback_query": {
            "id": "c2-cancel-confirm", "from": customer, "data": f"cancel_confirm:{rid}",
            "message": {"chat": private_chat},
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            order = c.execute("SELECT * FROM orders WHERE id=?", (reservation["order_id"],)).fetchone()
            linked_customer = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            cancellation = c.execute("SELECT * FROM telegram_customer_cancellations WHERE reservation_id=?", (rid,)).fetchone()
            self.assertEqual(reservation["status"], "取消")
            self.assertEqual(order["order_status"], "取消")
            self.assertEqual(linked_customer["points"], 0)
            self.assertEqual(cancellation["cancellation_no"], 1)
            self.assertEqual(cancellation["points_deducted"], 900)
        self.assertTrue(any(method == "editMessageText" and "chat_id=-10001" in body and "%E6%9A%82%E6%97%A0%E9%A2%84%E7%BA%A6" in body
                            for method, body in self.telegram_calls))
        # 酒店图片和说明仍会被删除；共享接龙本身只编辑、不删除。
        self.assertEqual(sum(1 for method, body in self.telegram_calls
                             if method == "deleteMessage" and "chat_id=-10001" in body), 1)

    def test_approved_booking_updates_one_daily_chain_and_marks_only_latest_as_new(self):
        manager = {"id": 9401, "first_name": "店长"}
        self.webhook({"message": {
            "message_id": 30, "chat": {"id": -40004, "type": "supergroup", "title": "Alice内部群"},
            "from": manager, "text": "/绑定审核群",
        }})
        self.webhook({"message": {
            "message_id": 31, "chat": {"id": -41004, "type": "supergroup", "title": "娜娜子群"},
            "from": manager, "text": "/绑定女孩 娜娜子",
        }})
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('0042','回头客',0)")
            returning_customer_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,
                                             customer_no,customer_name,received_amount,order_status)
                         VALUES('2026-01-01','19:00-20:00',?,?,?,?,?,15000,'已结束')""",
                      (girl_id, "娜娜子", returning_customer_id, "0042", "回头客"))
            c.execute("""INSERT INTO telegram_customers(telegram_user_id,customer_id,display_name)
                         VALUES('7402',?,'回头客')""", (returning_customer_id,))
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,received_amount,remark,order_status)
                         VALUES(?,?,?,?,?,?,?)""",
                      (self.day, "19:00-20:00", girl_id, "娜娜子", 15000, "人工旧接龙", "预约中"))
            for user_id, start, end in ((7401, "20:00", "21:00"), (7402, "21:00", "22:00")):
                c.execute("""INSERT INTO customer_reservations(
                                reserve_date,girl_name,start_time,end_time,username,status,price,
                                telegram_user_id,telegram_chat_id,telegram_group_chat_id)
                             VALUES(?,?,?,?,?,'待确认',15000,?,?,?)""",
                          (self.day, "娜娜子", start, end, f"客人{user_id}", str(user_id), str(user_id), "-41004"))
            reservation_ids = [r[0] for r in c.execute(
                "SELECT id FROM customer_reservations ORDER BY id").fetchall()]

        self.telegram_calls.clear()
        self.webhook({"callback_query": {
            "id": "daily-approve-1", "from": manager, "data": f"approve:{reservation_ids[0]}",
            "message": {"chat": {"id": -40004, "type": "supergroup", "title": "Alice内部群"}},
        }})
        first_group_messages = [body for method, body in self.telegram_calls
                                if method == "sendMessage" and "chat_id=-41004" in body]
        self.assertEqual(len(first_group_messages), 1)
        self.assertIn("%E4%BA%BA%E5%B7%A5%E6%97%A7%E6%8E%A5%E9%BE%99", first_group_messages[0])
        self.assertIn("%E5%AE%A2%E4%BA%BA7401", first_group_messages[0])
        self.assertIn("NEW%EF%BD%9C", first_group_messages[0])
        self.assertIn("%E8%81%94%E7%B3%BB%E7%AC%AC+2+%E4%BD%8D%E5%AE%A2%E6%88%B7", first_group_messages[0])
        self.assertIn("tg%3A%2F%2Fuser%3Fid%3D7401", first_group_messages[0])

        self.telegram_calls.clear()
        self.webhook({"callback_query": {
            "id": "daily-approve-2", "from": manager, "data": f"approve:{reservation_ids[1]}",
            "message": {"chat": {"id": -40004, "type": "supergroup", "title": "Alice内部群"}},
        }})
        edits = [body for method, body in self.telegram_calls
                 if method == "editMessageText" and "chat_id=-41004" in body]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].count("NEW%EF%BD%9C"), 1)
        self.assertIn("1.7-8", edits[0])
        self.assertIn("2.8-9", edits[0])
        self.assertIn("3.9-10", edits[0])
        self.assertIn("%2F0042", edits[0])
        self.assertIn("%E8%81%94%E7%B3%BB%E7%AC%AC+2+%E4%BD%8D%E5%AE%A2%E6%88%B7", edits[0])
        self.assertIn("%E8%81%94%E7%B3%BB%E7%AC%AC+3+%E4%BD%8D%E5%AE%A2%E6%88%B7", edits[0])
        self.assertIn("tg%3A%2F%2Fuser%3Fid%3D7402", edits[0])
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM telegram_daily_chain_messages").fetchone()[0], 1)

        self.telegram_calls.clear()
        self.webhook({"message": {
            "message_id": 32, "chat": {"id": -41004, "type": "supergroup", "title": "娜娜子群"},
            "from": manager, "text": f"/最新接龙 {self.day}",
        }})
        latest = [body for method, body in self.telegram_calls
                  if method == "sendMessage" and "chat_id=-41004" in body]
        self.assertEqual(len(latest), 1)
        self.assertIn("%E6%8E%A5%E9%BE%99", latest[0])
        self.assertIn("%2F0042", latest[0])
        self.assertNotIn("NEW%EF%BD%9C", latest[0])

    def test_sync_includes_room_and_no_room_girls_then_save_can_reduce(self):
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        response = self.client.post("/api/telegram/daily-girls", headers=headers,
                                    json={"action": "sync", "date": self.day})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual([x["girl_name"] for x in response.json["girls"]], ["娜娜子", "有房女孩"])
        self.webhook({"callback_query": {
            "id": "c3", "from": {"id": 7001, "first_name": "测试客人"},
            "data": f"date:{self.day}", "message": {"chat": {"id": 7001, "type": "private"}},
        }})
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("%E5%A8%9C%E5%A8%9C%E5%AD%90" in body for body in sent_bodies))
        self.assertTrue(any("%E6%9C%89%E6%88%BF%E5%A5%B3%E5%AD%A9" in body for body in sent_bodies))

        response = self.client.post("/api/telegram/daily-girls", headers=headers,
                                    json={"action": "save", "date": self.day, "girls": ["有房女孩"]})
        self.assertEqual([x["girl_name"] for x in response.json["girls"]], ["有房女孩"])

    def test_default_review_group_receives_unbound_girl_booking(self):
        self.webhook({"message": {
            "message_id": 10,
            "chat": {"id": -20002, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9002, "first_name": "店长"},
            "text": "/绑定审核群",
        }})
        with self.app_module.conn() as c:
            cfg = dict(c.execute("SELECT setting_key,setting_value FROM telegram_settings").fetchall())
            self.assertEqual(cfg["default_review_chat_id"], "-20002")

        private_chat = {"id": 7101, "type": "private"}
        customer = {"id": 7101, "first_name": "默认群客人"}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
        self.webhook({"callback_query": {"id": "c4", "from": customer,
                                          "data": f"girl:{self.day}:{girl_id}", "message": {"chat": private_chat}}})
        self.webhook({"message": {"message_id": 11, "chat": private_chat, "from": customer, "text": "20-21"}})
        self.webhook({"callback_query": {"id": "c4-confirm", "from": customer,
                                          "data": "flow:confirm", "message": {"chat": private_chat}}})
        with self.app_module.conn() as c:
            row = c.execute("SELECT * FROM customer_reservations").fetchone()
            self.assertEqual(row["telegram_group_chat_id"], "-20002")

    def test_new_customer_with_zero_points_skips_points_confirmation(self):
        self.webhook({"message": {
            "message_id": 12,
            "chat": {"id": -21002, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9102, "first_name": "店长"},
            "text": "/绑定审核群",
        }})
        private_chat = {"id": 7112, "type": "private"}
        customer = {"id": 7112, "first_name": "第一次预约客人", "username": "first_guest"}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
        self.webhook({"callback_query": {"id": "new-1", "from": customer,
                                          "data": f"girl:{self.day}:{girl_id}", "message": {"chat": private_chat}}})
        self.webhook({"message": {"message_id": 13, "chat": private_chat, "from": customer, "text": "20-21"}})
        self.webhook({"callback_query": {"id": "new-2", "from": customer,
                                          "data": "flow:confirm", "message": {"chat": private_chat}}})
        with self.app_module.conn() as c:
            rid = c.execute("SELECT id FROM customer_reservations WHERE telegram_user_id='7112'").fetchone()[0]

        self.telegram_calls.clear()
        self.webhook({"callback_query": {
            "id": "new-approve", "from": {"id": 9102, "first_name": "店长"}, "data": f"approve:{rid}",
            "message": {"chat": {"id": -21002, "type": "supergroup", "title": "Alice内部群"}},
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            session = c.execute("SELECT * FROM telegram_booking_sessions WHERE user_id='7112'").fetchone()
            self.assertGreater(int(reservation["order_id"]), 0)
            self.assertEqual(reservation["points_available"], 0)
            self.assertEqual(session["step"], "booked")
        sent = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertFalse(any("%E8%AF%B7%E9%80%89%E6%8B%A9%E6%9C%AC%E6%AC%A1%E7%A7%AF%E5%88%86" in body for body in sent), sent)
        self.assertTrue(any("%E6%96%B0%E5%AE%A2%E6%97%A0%E5%8F%AF%E7%94%A8%E7%A7%AF%E5%88%86" in body for body in sent), sent)

    def test_time_step_can_go_back_or_cancel_without_creating_reservation(self):
        private_chat = {"id": 7201, "type": "private"}
        customer = {"id": 7201, "first_name": "返回测试客人"}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
        self.webhook({"callback_query": {"id": "c5", "from": customer,
                                          "data": f"girl:{self.day}:{girl_id}",
                                          "message": {"chat": private_chat}}})
        with self.app_module.conn() as c:
            session = c.execute("SELECT * FROM telegram_booking_sessions WHERE user_id='7201'").fetchone()
            self.assertEqual(session["step"], "await_time")

        self.webhook({"callback_query": {"id": "c6", "from": customer,
                                          "data": f"flow:girls:{self.day}",
                                          "message": {"chat": private_chat}}})
        with self.app_module.conn() as c:
            session = c.execute("SELECT * FROM telegram_booking_sessions WHERE user_id='7201'").fetchone()
            self.assertEqual(session["step"], "choose_girl")

        self.webhook({"callback_query": {"id": "c7", "from": customer,
                                          "data": "flow:cancel", "message": {"chat": private_chat}}})
        with self.app_module.conn() as c:
            self.assertIsNone(c.execute("SELECT * FROM telegram_booking_sessions WHERE user_id='7201'").fetchone())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customer_reservations").fetchone()[0], 0)

    def test_chain_12_hour_orders_and_bot_24_hour_availability_use_same_mcr_ranges(self):
        with self.app_module.conn() as c:
            c.execute("UPDATE pure_shifts SET start_time='19:00',end_time='05:00' WHERE shift_date=? AND girl_name='娜娜子'", (self.day,))
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,order_status)
                         VALUES(?,?,?,?,?)""", (self.day, '7-8', girl_id, '娜娜子', '预约中'))

        self.assertEqual(self.app_module.service_range_minutes('7-8'), (19 * 60, 20 * 60))
        self.assertEqual(self.app_module.service_range_minutes('3-4'), (27 * 60, 28 * 60))

        response = self.client.post('/api/customer_available', json={
            'date': self.day,
            'girl_name': '娜娜子',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        slots = [slot['label'] for slot in response.json['girls'][0]['slots']]
        self.assertNotIn('19:00-19:30', slots)
        self.assertNotIn('19:30-20:00', slots)
        self.assertIn('03:00-03:30', slots)
        self.assertIn('03:30-04:00', slots)

        self.telegram_calls.clear()
        self.webhook({'callback_query': {
            'id': 'mcr-24h-display', 'from': {'id': 7210, 'first_name': '24小时测试'},
            'data': f'date:{self.day}', 'message': {'chat': {'id': 7210, 'type': 'private'}},
        }})
        sent = [body for method, body in self.telegram_calls if method == 'sendMessage']
        # 连续空档以 24 小时制显示；其中 03:00-04:00 已由上面的 slots 证明可约。
        self.assertTrue(any('20%3A00-05%3A00' in body for body in sent), sent)

    def test_chain_import_resolves_afternoon_short_times_to_24h_storage(self):
        with self.app_module.conn() as c:
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('四系乃','在职',28000)")
            girl_id = c.execute("SELECT id FROM girls WHERE name='四系乃'").fetchone()[0]
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (self.day, "四系乃", "14:00", "18:00", "", ""))

        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        response = self.client.post('/api/import_chain', json={
            'order_date': self.day,
            'girl_id': girl_id,
            'text': "\n".join([
                "1.2-3/28000/短写客人A",
                "2.3-4/28000/短写客人B",
                "3.4-5/28000/短写客人C",
                "4.5-6/28000/短写客人D",
            ]),
        }, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        with self.app_module.conn() as c:
            stored = [r["service_time"] for r in c.execute(
                "SELECT service_time FROM orders WHERE order_date=? AND girl_name='四系乃' ORDER BY id",
                (self.day,),
            ).fetchall()]
        self.assertEqual(stored, ["14:00-15:00", "15:00-16:00", "16:00-17:00", "17:00-18:00"])

        export = self.client.post('/api/chain_export', json={'date': self.day, 'girl_name': '四系乃'}, headers=headers)
        self.assertEqual(export.status_code, 200, export.get_data(as_text=True))
        self.assertIn("1.14:00-15:00/28000", export.json["full"])
        self.assertIn("四系乃满", export.json["free"]["text"])

    def test_legacy_short_chain_orders_are_shift_normalized_when_free_times_refresh(self):
        with self.app_module.conn() as c:
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('四系乃','在职',28000)")
            girl_id = c.execute("SELECT id FROM girls WHERE name='四系乃'").fetchone()[0]
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (self.day, "四系乃", "14:00", "18:00", "", ""))
            for t in ("2-3", "3-4", "4-5", "5-6"):
                c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,order_status)
                             VALUES(?,?,?,?,?)""", (self.day, t, girl_id, "四系乃", "预约中"))

        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        page = self.client.post('/api/chain_page', json={'date': self.day, 'girl_name': '四系乃'}, headers=headers)
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        self.assertIn("四系乃满", page.json["free"]["text"])

        with self.app_module.conn() as c:
            stored = [r["service_time"] for r in c.execute(
                "SELECT service_time FROM orders WHERE order_date=? AND girl_name='四系乃' ORDER BY id",
                (self.day,),
            ).fetchall()]
        self.assertEqual(stored, ["14:00-15:00", "15:00-16:00", "16:00-17:00", "17:00-18:00"])

    def test_full_girl_is_shown_as_full_instead_of_disappearing(self):
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,order_status)
                         VALUES(?,?,?,?,?)""", (self.day, '19:00-23:00', girl_id, '娜娜子', '预约中'))

        self.telegram_calls.clear()
        self.webhook({'callback_query': {
            'id': 'full-display', 'from': {'id': 7211, 'first_name': '满员测试'},
            'data': f'date:{self.day}', 'message': {'chat': {'id': 7211, 'type': 'private'}},
        }})
        sent = [body for method, body in self.telegram_calls if method == 'sendMessage']
        self.assertTrue(any('%E5%B7%B2%E6%BB%A1' in body for body in sent), sent)

    def test_group_import_incrementally_syncs_mcr_and_replies_only_in_internal_group(self):
        self.webhook({"message": {
            "message_id": 20,
            "chat": {"id": -30003, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "/绑定审核群",
        }})
        self.webhook({"message": {
            "message_id": 22,
            "chat": {"id": -39999, "type": "supergroup", "title": "娜娜子群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "导入",
            "reply_to_message": {"message_id": 21, "text": f"{self.day} 娜娜子\n1.19-20/15000/接龙测试客人"},
        }})
        with self.app_module.conn() as c:
            row = c.execute("SELECT * FROM orders WHERE girl_name='娜娜子'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["order_date"], self.day)
            self.assertEqual(row["order_status"], "已结束")
            first_order_id = row["id"]

        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("chat_id=-30003" in body and "%E6%96%B0%E5%A2%9E%EF%BC%9A1" in body for body in sent_bodies))
        self.assertFalse(any("chat_id=-39999" in body for body in sent_bodies))

        self.telegram_calls.clear()
        self.webhook({"message": {
            "message_id": 23,
            "chat": {"id": -39999, "type": "supergroup", "title": "娜娜子群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "/导入",
            "reply_to_message": {"message_id": 21, "text": f"{self.day} 娜娜子\n1.19:30-20:30/16000/接龙测试客人\n2.21-22/15000/新增测试客人"},
        }})
        with self.app_module.conn() as c:
            rows = c.execute("SELECT * FROM orders WHERE girl_name='娜娜子' ORDER BY id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["id"], first_order_id)
            self.assertEqual(rows[0]["service_time"], "19:30-20:30")
            self.assertEqual(rows[0]["received_amount"], 16000)
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("chat_id=-30003" in body and "%E6%96%B0%E5%A2%9E%EF%BC%9A1" in body and "%E4%BF%AE%E6%94%B9%EF%BC%9A1" in body for body in sent_bodies))
        self.assertFalse(any("chat_id=-39999" in body for body in sent_bodies))

        self.telegram_calls.clear()
        self.webhook({"message": {
            "message_id": 24,
            "chat": {"id": -39999, "type": "supergroup", "title": "娜娜子群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "/导入",
            "reply_to_message": {"message_id": 21, "text": f"{self.day} 娜娜子\n1.19:30-20:30/16000/接龙测试客人\n2.21-22/15000/新增测试客人"},
        }})
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders WHERE girl_name='娜娜子'").fetchone()[0], 2)
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("%E6%9C%AA%E5%8F%98%E5%8C%96%EF%BC%9A2" in body for body in sent_bodies))

    def test_automatic_chain_scan_imports_attending_girl_and_warns_internal_on_failure(self):
        auto_day = self.app_module._tokyo_now().date().isoformat()
        date_keyword = self.app_module._tokyo_now().strftime("%m%d")
        manager = {"id": 9300, "first_name": "客服"}
        with self.app_module.conn() as c:
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('未出勤女孩','在职',15000)")
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time)
                         VALUES(?,?,?,?)""", (auto_day, "娜娜子", "19:00", "23:00"))
        self.webhook({"message": {
            "message_id": 70,
            "chat": {"id": -30003, "type": "supergroup", "title": "Alice内部群"},
            "from": manager, "text": "/绑定审核群",
        }})
        self.webhook({"message": {
            "message_id": 71,
            "chat": {"id": -39999, "type": "supergroup", "title": "娜娜子群"},
            "from": manager, "text": "/绑定女孩 娜娜子",
        }})
        self.webhook({"message": {
            "message_id": 72,
            "chat": {"id": -39999, "type": "supergroup", "title": "娜娜子群"},
            "from": manager, "text": f"{date_keyword}\n1.19-20/15000/自动导入客人",
        }})
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        scanned = self.client.post("/api/telegram/chain-import/run", headers=headers)
        self.assertEqual(scanned.status_code, 200, scanned.get_data(as_text=True))
        self.assertEqual(scanned.json["imported"], 1)
        self.assertEqual(scanned.json["inserted"], 1)
        with self.app_module.conn() as c:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM orders WHERE order_date=? AND girl_name='娜娜子' AND customer_name='自动导入客人'",
                (auto_day,)).fetchone())

        self.telegram_calls.clear()
        self.webhook({"message": {
            "message_id": 73,
            "chat": {"id": -38888, "type": "supergroup", "title": "其他女孩群"},
            "from": manager, "text": "/绑定女孩 未出勤女孩",
        }})
        self.telegram_calls.clear()
        self.webhook({"message": {
            "message_id": 74,
            "chat": {"id": -38888, "type": "supergroup", "title": "其他女孩群"},
            "from": manager, "text": f"{date_keyword}\n1.20-21/15000/不应导入客人",
        }})
        failed = self.client.post("/api/telegram/chain-import/run", headers=headers)
        self.assertEqual(failed.json["failed"], 1)
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("chat_id=-30003" in body and "%E8%87%AA%E5%8A%A8%E6%8E%A5%E9%BE%99%E5%AF%BC%E5%85%A5%E5%A4%B1%E8%B4%A5" in body
                            for body in sent_bodies), sent_bodies)
        self.assertFalse(any("chat_id=-38888" in body for body in sent_bodies))

        with self.app_module.conn() as c:
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time)
                         VALUES(?,?,?,?)""", (auto_day, "未出勤女孩", "20:00", "23:00"))
        self.webhook({"edited_message": {
            "message_id": 74,
            "chat": {"id": -38888, "type": "supergroup", "title": "其他女孩群"},
            "from": manager, "text": f"{date_keyword}\n1.20-21/15000/修改后导入客人",
        }})
        retried = self.client.post("/api/telegram/chain-import/run", headers=headers)
        self.assertEqual(retried.json["imported"], 1)

    def test_auto_chain_date_only_uses_binding_empty_is_silent_and_internal_can_toggle(self):
        auto_day = self.app_module._tokyo_now().date().isoformat()
        date_keyword = self.app_module._tokyo_now().strftime("%m%d")
        manager = {"id": 9600, "first_name": "店长"}
        internal = {"id": -30003, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -39999, "type": "supergroup", "title": "娜娜子群"}
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time)
                         VALUES(?,?,?,?)""", (auto_day, "娜娜子", "19:00", "23:00"))
        self.webhook({"message": {"message_id": 80, "chat": internal, "from": manager, "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 81, "chat": girl_chat, "from": manager, "text": "/绑定女孩 娜娜子"}})
        old_keyword = (self.app_module._tokyo_now() - timedelta(days=1)).strftime("%m%d")
        self.webhook({"message": {"message_id": 810, "chat": girl_chat, "from": manager,
                                  "text": f"{old_keyword}\n1.19-20/15000/旧日期不导入"}})
        self.webhook({"message": {"message_id": 811, "chat": girl_chat, "from": manager,
                                  "text": f"{date_keyword} 娜娜子\n1.19-20/15000/非独行日期不导入"}})
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM telegram_chain_inbox").fetchone()[0], 0)
        self.webhook({"message": {
            "message_id": 82, "chat": girl_chat, "from": manager,
            "text": f"{date_keyword}\n1.19-20/15000/仅日期自动客人",
        }})
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        imported = self.client.post("/api/telegram/chain-import/run", headers=headers)
        self.assertEqual(imported.json["imported"], 1, imported.get_data(as_text=True))
        with self.app_module.conn() as c:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM orders WHERE order_date=? AND girl_name='娜娜子' AND customer_name='仅日期自动客人'",
                (auto_day,)).fetchone())

        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 83, "chat": girl_chat, "from": manager, "text": date_keyword}})
        empty = self.client.post("/api/telegram/chain-import/run", headers=headers)
        self.assertEqual(empty.json["empty"], 1, empty.get_data(as_text=True))
        self.assertFalse(any(method == "sendMessage" for method, _body in self.telegram_calls))

        self.webhook({"message": {"message_id": 84, "chat": internal, "from": manager, "text": "自动导入关闭"}})
        with self.app_module.conn() as c:
            value = c.execute("SELECT setting_value FROM telegram_settings WHERE setting_key='auto_chain_import_enabled'").fetchone()[0]
        self.assertEqual(value, "0")
        self.webhook({"message": {"message_id": 85, "chat": internal, "from": manager, "text": "自动导入开启"}})
        with self.app_module.conn() as c:
            value = c.execute("SELECT setting_value FROM telegram_settings WHERE setting_key='auto_chain_import_enabled'").fetchone()[0]
        self.assertEqual(value, "1")

    def test_attendance_inquiry_writes_mcr_shift_and_unanswered_expires(self):
        manager = {"id": 9700, "first_name": "店长"}
        girl_user = {"id": 9701, "first_name": "娜娜子"}
        internal = {"id": -30003, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -39999, "type": "supergroup", "title": "娜娜子群"}
        today = self.app_module._tokyo_now().date().isoformat()
        self.webhook({"message": {"message_id": 90, "chat": internal, "from": manager, "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 91, "chat": girl_chat, "from": manager, "text": "/绑定女孩 娜娜子"}})
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 92, "chat": internal, "from": manager, "text": "询问出勤"}})
        with self.app_module.conn() as c:
            inquiry = dict(c.execute("SELECT * FROM telegram_attendance_inquiries WHERE inquiry_date=? AND girl_name='娜娜子'",
                                     (today,)).fetchone())
        self.assertEqual(inquiry["status"], "pending")
        self.assertTrue(any(method == "sendMessage" and "chat_id=-39999" in body and "attendance%3Ayes%3A" in body
                            for method, body in self.telegram_calls))

        self.webhook({"callback_query": {"id": "att-yes", "from": girl_user,
                                          "message": {"message_id": inquiry["message_id"], "chat": girl_chat},
                                          "data": f"attendance:yes:{inquiry['id']}"}})
        self.webhook({"callback_query": {"id": "att-start", "from": girl_user,
                                          "message": {"message_id": inquiry["message_id"], "chat": girl_chat},
                                          "data": f"attendance:start:{inquiry['id']}:1230"}})
        self.webhook({"callback_query": {"id": "att-end", "from": girl_user,
                                          "message": {"message_id": inquiry["message_id"], "chat": girl_chat},
                                          "data": f"attendance:end:{inquiry['id']}:1440"}})
        with self.app_module.conn() as c:
            shift = c.execute("SELECT * FROM pure_shifts WHERE shift_date=? AND girl_name='娜娜子'", (today,)).fetchone()
            daily = c.execute("SELECT 1 FROM telegram_daily_girls WHERE booking_date=? AND girl_name='娜娜子'", (today,)).fetchone()
            status = c.execute("SELECT status FROM telegram_attendance_inquiries WHERE id=?", (inquiry["id"],)).fetchone()[0]
        self.assertEqual((shift["start_time"], shift["end_time"]), ("20:30", "00:00"))
        self.assertIsNotNone(daily)
        self.assertEqual(status, "attending")

        self.webhook({"message": {"message_id": 93, "chat": internal, "from": manager, "text": "询问出勤"}})
        with self.app_module.conn() as c:
            c.execute("UPDATE telegram_attendance_inquiries SET expires_at=datetime('now','-1 minute') WHERE id=?", (inquiry["id"],))
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 94, "chat": girl_chat, "from": girl_user, "text": "普通消息"}})
        with self.app_module.conn() as c:
            status = c.execute("SELECT status FROM telegram_attendance_inquiries WHERE id=?", (inquiry["id"],)).fetchone()[0]
            remaining_shift = c.execute("SELECT 1 FROM pure_shifts WHERE shift_date=? AND girl_name='娜娜子'", (today,)).fetchone()
            remaining_daily = c.execute("SELECT 1 FROM telegram_daily_girls WHERE booking_date=? AND girl_name='娜娜子'", (today,)).fetchone()
        self.assertEqual(status, "absent")
        self.assertIsNone(remaining_shift)
        self.assertIsNone(remaining_daily)
        self.assertTrue(any(method == "editMessageText" and "%E9%BB%98%E8%AE%A4%E4%B8%8D%E5%87%BA%E5%8B%A4" in body
                            for method, body in self.telegram_calls))

    def test_order_delete_returns_local_patch_and_cleans_import_mapping(self):
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points,total_points,total_spent) VALUES('0042','删除测试',750,750,15000)")
            customer_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,customer_name,received_amount,points)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (self.day, '19:00-20:00', girl_id, '娜娜子', customer_id, '0042', '删除测试', 15000, 750))
            order_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO chain_import_rows(order_date,girl_id,sequence_no,order_id,normalized_text)
                         VALUES(?,?,?,?,?)""", (self.day, girl_id, 1, order_id, '1.19-20/15000/0042'))
            c.execute("""INSERT INTO customer_reservations(reserve_date,girl_name,start_time,end_time,order_id)
                         VALUES(?,?,?,?,?)""", (self.day, '娜娜子', '19:00', '20:00', order_id))

        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        response = self.client.post(f"/api/delete/orders/{order_id}", headers=headers, json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["deleted"], 1)
        self.assertEqual(response.json["customers"][0]["total_orders"], 0)
        self.assertEqual(response.json["customers"][0]["points"], 0)
        with self.app_module.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM chain_import_rows WHERE order_id=?", (order_id,)).fetchone())
            reservation = c.execute("SELECT order_id FROM customer_reservations").fetchone()
            self.assertEqual(reservation["order_id"], 0)

    def test_second_customer_cancellation_blacklists_and_blocks_booking(self):
        with self.app_module.conn() as c:
            c.execute("INSERT INTO customers(customer_no,name,points,customer_status) VALUES('0088','二次取消测试',300,'正常')")
            customer_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO telegram_customers(telegram_user_id,customer_id,display_name) VALUES('7888',?,'二次取消测试')", (customer_id,))
            c.execute("""INSERT INTO telegram_customer_cancellations(
                            reservation_id,telegram_user_id,customer_id,cancellation_no,points_deducted)
                         VALUES(90001,'7888',?,1,500)""", (customer_id,))
            c.execute("""INSERT INTO customer_reservations(
                            reserve_date,girl_name,start_time,end_time,username,status,customer_id,
                            telegram_user_id,telegram_chat_id,telegram_group_chat_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (self.day, '娜娜子', '21:00', '22:00', '二次取消测试', '已确认', customer_id,
                       '7888', '7888', '-10001'))
            rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        private_chat = {"id": 7888, "type": "private"}
        customer = {"id": 7888, "first_name": "二次取消测试"}
        self.webhook({"callback_query": {
            "id": "second-cancel", "from": customer, "data": f"cancel_confirm:{rid}",
            "message": {"chat": private_chat},
        }})
        with self.app_module.conn() as c:
            linked = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            cancellation = c.execute("SELECT * FROM telegram_customer_cancellations WHERE reservation_id=?", (rid,)).fetchone()
            self.assertEqual(linked["customer_status"], "黑名单")
            self.assertEqual(cancellation["cancellation_no"], 2)

        self.telegram_calls.clear()
        self.webhook({"callback_query": {
            "id": "blocked-book", "from": customer, "data": "book",
            "message": {"chat": private_chat},
        }})
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("%E6%9A%82%E5%81%9C%E8%87%AA%E5%8A%A9%E9%A2%84%E7%BA%A6" in body for body in sent_bodies))

    def test_boss_can_create_account_with_module_permissions(self):
        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        self.assertEqual(boss.status_code, 200)
        headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"]}
        created = self.client.post("/api/system/users", headers=headers, json={
            "username": "permission_test", "password": "test1234", "label": "权限测试",
            "role": "user", "enabled": True, "permissions": ["pureShift"]
        })
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        login = self.client.post("/api/login", json={"username": "permission_test", "password": "test1234"})
        self.assertEqual(login.json["permissions"], ["pureShift"])
        user_headers = {"X-Alice-Role": "user", "X-Alice-Session": login.json["session_token"]}
        allowed = self.client.get(f"/api/pure_shifts?date={self.day}", headers=user_headers)
        denied = self.client.post("/api/customers", headers=user_headers, json={"name": "不应保存"})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        listing = self.client.get("/api/system/users", headers=headers).json["users"]
        uid = next(x["id"] for x in listing if x["username"] == "permission_test")
        self.client.post("/api/system/users", headers=headers, json={"action": "delete", "id": uid})

    def test_pure_shift_report_photo_sends_and_syncs_daily_girls(self):
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        with self.app_module.conn() as c:
            c.execute("INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_id','-90000') ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value")
            c.execute("INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_title','Alice内部群') ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value")
        response = self.client.post("/api/telegram/report-photo", headers=headers, json={
            "kind": "pure_shift", "date": self.day,
            "image_data_list": [
                "data:image/png;base64,ZmFrZS1wbmctMQ==",
                "data:image/png;base64,ZmFrZS1wbmctMg==",
            ]
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["synced"], 2)
        self.assertEqual(len(response.json["message_ids"]), 2)
        self.assertEqual(sum(method == "sendPhoto" for method, _body in self.telegram_calls), 2)
        self.assertTrue(any(method == "sendMessage" and "chat_id=-90000" in body
                            for method, body in self.telegram_calls))

    def test_pure_shift_remembers_normal_and_gold_tags(self):
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        saved = self.client.post("/api/pure_shifts", headers=headers, json={
            "date": self.day, "girl": "娜娜子", "start": "20:30", "end": "00:00",
            "tags": ["年纪小", "新人"], "goldTags": ["房间", "推荐"]
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        later_day = (self.app_module._tokyo_now().date() + timedelta(days=2)).isoformat()
        loaded = self.client.get(f"/api/pure_shifts?date={later_day}&autocopy=0", headers=headers)
        self.assertEqual(loaded.status_code, 200, loaded.get_data(as_text=True))
        self.assertEqual(loaded.json["girl_tags"]["娜娜子"], "年纪小 新人")
        self.assertEqual(loaded.json["girl_gold_tags"]["娜娜子"], "房间 推荐")

    def test_tonight_page_is_public_and_uses_mcr_price(self):
        page = self.client.get("/tonight")
        self.assertEqual(page.status_code, 200)
        self.assertIn("池袋今晚可约", page.get_data(as_text=True))
        available = self.client.post("/api/customer_available", json={
            "date": self.day, "client_now": f"{self.day}T10:00:00+09:00"
        })
        self.assertEqual(available.status_code, 200, available.get_data(as_text=True))
        nana = next(row for row in available.json["girls"] if row["girl"] == "娜娜子")
        self.assertEqual(nana["price"], 15000)
        self.assertEqual(nana["start"], "19:00")
        self.assertTrue(nana["slots"])

    def test_shift_order_combines_20_day_popularity_and_two_day_surge(self):
        target = self.app_module.datetime.strptime(self.day, "%Y-%m-%d").date()
        with self.app_module.conn() as c:
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('稳定女孩','在职',18000)")
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('爆发女孩','在职',18000)")
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,sort_order)
                         VALUES(?,?,?,?,?)""", (self.day, '稳定女孩', '19:00', '23:00', 1))
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,sort_order)
                         VALUES(?,?,?,?,?)""", (self.day, '爆发女孩', '19:00', '23:00', 99))
            for offset in range(3, 15):
                c.execute("INSERT INTO orders(order_date,girl_name,order_status) VALUES(?,?,'已结束')",
                          ((target - timedelta(days=offset)).isoformat(), '稳定女孩'))
            for offset in (0, 0, 1, 1):
                c.execute("INSERT INTO orders(order_date,girl_name,order_status) VALUES(?,?,'预约中')",
                          ((target - timedelta(days=offset)).isoformat(), '爆发女孩'))
            ranked = self.app_module.pure_shift_rows_for_date(c, self.day)
        names = [row['girl'] for row in ranked]
        self.assertLess(names.index('爆发女孩'), names.index('稳定女孩'))
        burst = next(row for row in ranked if row['girl'] == '爆发女孩')
        self.assertEqual(burst['orders_2d'], 4)
        self.assertGreater(burst['popularity_score'], 0)

    def test_wordpress_photo_gallery_field_name_uses_legacy_plugin_controls(self):
        html = '''<form id="post"><div class="acf-field" data-key="field_gallery">
        照片(可以添加多个照片)<input name="apg_nonce" value="nonce123">
        <input name="acf-photo-gallery-groups[]" value="girl_photos">
        <input name="acf-photo-gallery-field" value="field_gallery">
        <input name="girl_photos[]" value="456"></div></form>'''
        self.assertEqual(self.app_module._acf_gallery_field_key(html), "field_gallery")
        self.assertEqual(self.app_module._wordpress_photo_gallery_field_name(html, "field_gallery"),
                         "girl_photos")
        gallery_html = '<li class="acf-photo-gallery-mediabox acf-photo-gallery-mediabox-456"></li>'
        self.assertEqual(self.app_module._wordpress_photo_gallery_attachment_ids(gallery_html), [456])


if __name__ == "__main__":
    unittest.main()
