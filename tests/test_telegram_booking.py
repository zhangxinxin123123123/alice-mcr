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
            for table in ("orders", "customer_reservations", "pure_shifts", "girls",
                          "telegram_group_bindings", "telegram_managers", "telegram_booking_sessions"):
                c.execute(f"DELETE FROM {table}")
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('娜娜子','在职',15000)")
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('有房女孩','在职',15000)")
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (day, "娜娜子", "19:00", "23:00", "", ""))
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time,tags,gold_tags)
                         VALUES(?,?,?,?,?,?)""", (day, "有房女孩", "19:00", "23:00", "", "房间"))

    def webhook(self, payload):
        response = self.client.post("/telegram/webhook", json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def test_complete_booking_approval_and_hotel_photo_flow(self):
        self.webhook({"message": {
            "message_id": 1,
            "chat": {"id": -10001, "type": "supergroup", "title": "Alice内部群"},
            "from": {"id": 9001, "first_name": "店长"},
            "text": "/绑定女孩 娜娜子",
        }})

        with self.app_module.conn() as c:
            binding = c.execute("SELECT * FROM telegram_group_bindings WHERE girl_name='娜娜子'").fetchone()
            self.assertEqual(binding["chat_title"], "Alice内部群")

        private_chat = {"id": 7001, "type": "private"}
        customer = {"id": 7001, "first_name": "测试客人", "username": "guest"}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
        self.webhook({"callback_query": {"id": "c1", "from": customer,
                                          "data": f"girl:{self.day}:{girl_id}", "message": {"chat": private_chat}}})
        self.webhook({"message": {"message_id": 2, "chat": private_chat, "from": customer, "text": "19-20"}})

        with self.app_module.conn() as c:
            reservation = dict(c.execute("SELECT * FROM customer_reservations").fetchone())
            self.assertEqual(reservation["status"], "待确认")
            self.assertEqual(reservation["telegram_group_chat_id"], "-10001")
            rid = reservation["id"]

        self.webhook({"callback_query": {
            "id": "c2", "from": {"id": 9001, "first_name": "店长"}, "data": f"approve:{rid}",
            "message": {"chat": {"id": -10001, "type": "supergroup", "title": "Alice内部群"}},
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            self.assertEqual(reservation["status"], "已确认")
            self.assertGreater(int(reservation["order_id"]), 0)

        self.webhook({"message": {
            "message_id": 3, "chat": private_chat, "from": customer,
            "photo": [{"file_id": "small"}, {"file_id": "hotel-photo"}], "caption": "酒店测试房间",
        }})
        with self.app_module.conn() as c:
            reservation = c.execute("SELECT * FROM customer_reservations WHERE id=?", (rid,)).fetchone()
            self.assertEqual(reservation["hotel_file_id"], "hotel-photo")
        self.assertTrue(any(method == "sendPhoto" for method, _body in self.telegram_calls))

    def test_room_tagged_girl_is_not_eligible(self):
        self.webhook({"callback_query": {
            "id": "c3", "from": {"id": 7001, "first_name": "测试客人"},
            "data": f"date:{self.day}", "message": {"chat": {"id": 7001, "type": "private"}},
        }})
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("%E5%A8%9C%E5%A8%9C%E5%AD%90" in body for body in sent_bodies))
        self.assertFalse(any("%E6%9C%89%E6%88%BF%E5%A5%B3%E5%AD%A9" in body for body in sent_bodies))

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
        with self.app_module.conn() as c:
            row = c.execute("SELECT * FROM customer_reservations").fetchone()
            self.assertEqual(row["telegram_group_chat_id"], "-20002")


if __name__ == "__main__":
    unittest.main()
