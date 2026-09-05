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
        cls.app_module = importlib.import_module("app")
        cls.telegram_module = importlib.import_module("telegram_booking")
        cls.telegram_calls = []

        def fake_urlopen(req, timeout=20):
            method = req.full_url.rsplit("/", 1)[-1]
            body = (req.data or b"").decode("utf-8")
            cls.telegram_calls.append((method, body))
            if method == "getChatMember":
                result = {"status": "administrator"}
            elif method == "sendMessage":
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
                          "telegram_daily_girls", "telegram_customers"):
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

    def test_group_reply_chain_command_imports_into_mcr(self):
        self.webhook({"message": {
            "message_id": 20,
            "chat": {"id": -30003, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "/绑定审核群",
        }})
        self.webhook({"message": {
            "message_id": 22,
            "chat": {"id": -30003, "type": "supergroup", "title": "娜娜子群"},
            "from": {"id": 9300, "first_name": "客服"}, "text": "/导入接龙",
            "reply_to_message": {"message_id": 21, "text": f"{self.day} 娜娜子\n1.19-20/15000/接龙测试客人"},
        }})
        with self.app_module.conn() as c:
            row = c.execute("SELECT * FROM orders WHERE girl_name='娜娜子'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["order_date"], self.day)
            self.assertEqual(row["order_status"], "已结束")


if __name__ == "__main__":
    unittest.main()
