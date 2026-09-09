import importlib
import base64
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta


class FakeTelegramResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
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
            body = (req.data or b"").decode("utf-8", errors="replace")
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
                          "telegram_chain_inbox", "telegram_attendance_inquiries",
                          "telegram_closing_confirmations", "telegram_full_sync_days", "telegram_customer_digests",
                          "telegram_point_alert_digests", "scraped_reviews", "girl_praises", "operation_logs", "customer_ledger",
                          "points_records", "recharge_records", "girl_tag_memory", "telegram_customer_name_reviews",
                          "telegram_ai_sessions", "telegram_ai_interactions", "telegram_ai_teachings",
                          "telegram_ai_usage", "telegram_ai_budget_alerts"):
                c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM customer_membership_history")
            c.execute("DELETE FROM financial_settings WHERE setting_key='membership_retention_v2_initialized'")
            c.execute("UPDATE financial_settings SET setting_value='1' WHERE setting_key='ledger_enabled'")
            c.execute("UPDATE financial_settings SET setting_value='500' WHERE setting_key='point_rate_bps'")
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

    def test_closing_business_date_uses_previous_day_until_four(self):
        closing_day = self.telegram_module.closing_business_date
        self.assertEqual(closing_day(datetime(2026, 9, 9, 0, 0)), '2026-09-08')
        self.assertEqual(closing_day(datetime(2026, 9, 9, 3, 59)), '2026-09-08')
        self.assertEqual(closing_day(datetime(2026, 9, 9, 4, 0)), '2026-09-09')

    def test_auto_import_accepts_previous_and_current_date_until_four(self):
        candidate_days = self.telegram_module.auto_import_candidate_dates
        self.assertEqual([str(day) for day in candidate_days(datetime(2026, 9, 9, 0, 0))],
                         ['2026-09-09', '2026-09-08'])
        self.assertEqual([str(day) for day in candidate_days(datetime(2026, 9, 9, 3, 59))],
                         ['2026-09-09', '2026-09-08'])
        self.assertEqual([str(day) for day in candidate_days(datetime(2026, 9, 9, 4, 0))],
                         ['2026-09-09'])

    def test_internal_group_can_generate_shift_image_from_copy(self):
        internal = {"id": -90000, "type": "supergroup", "title": "Alice内部群"}
        manager = {"id": 9001, "first_name": "店长"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                   "text": "/绑定审核群"}})
        day = self.day.replace('-', '')[4:]
        copy_text = (f"{day}周三出勤\n\n【推荐】娜娜子（新人）\n"
                     "20:30到00:00             15000/h\n\nhttps://ailisi99.com/")
        self.webhook({"message": {"message_id": 2, "chat": internal, "from": manager,
                                   "text": "文案生成", "reply_to_message": {"message_id": 1, "text": copy_text}}})
        self.assertTrue(any(method == 'sendPhoto' and '文案生成出勤表' in body
                            for method, body in self.telegram_calls))
        self.assertTrue(any(method == 'sendMessage' and '%E6%96%87%E6%A1%88%E7%94%9F%E6%88%90%E5%AE%8C%E6%88%90' in body
                            for method, body in self.telegram_calls))

    def test_internal_group_alice_ai_uses_read_only_mcr_snapshot(self):
        internal = {"id": -90123, "type": "supergroup", "title": "Alice内部群"}
        manager = {"id": 9123, "first_name": "店长"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                   "text": "/绑定审核群"}})
        captured = {}
        old_urlopen = self.telegram_module.urlopen
        def fake_ai_urlopen(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                captured["body"] = json.loads(req.data.decode("utf-8"))
                return FakeTelegramResponse({"output": [{"content": [{
                    "type": "output_text", "text": "今天有 1 位女孩出勤，建议先确认晚间空档哦～"
                }]}]})
            return old_urlopen(req, timeout=timeout)
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = fake_ai_urlopen
        try:
            self.webhook({"message": {"message_id": 2, "chat": internal, "from": manager,
                                       "text": "艾莉兔 今天经营怎么样？"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)
        self.assertFalse(captured["body"]["store"])
        self.assertIn("只有只读权限", captured["body"]["instructions"])
        self.assertIn("当前internal权限摘要", captured["body"]["input"][-1]["content"])
        self.assertIn("称呼提问者为“主人”", captured["body"]["instructions"])
        self.assertTrue(any(method == "editMessageText" and "%E5%BB%BA%E8%AE%AE" in body
                            for method, body in self.telegram_calls))
        with self.app_module.conn() as c:
            history = c.execute("SELECT history_json FROM telegram_ai_sessions WHERE chat_id=? AND user_id=?",
                                (str(internal["id"]), str(manager["id"]))).fetchone()
        self.assertIsNotNone(history)

    def test_alice_ai_customer_and_girl_modes_are_scoped(self):
        internal = {"id": -90124, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -90125, "type": "supergroup", "title": "娜娜子群"}
        manager = {"id": 9124, "first_name": "店长"}
        customer = {"id": 9125, "first_name": "客人"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                  "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 2, "chat": girl_chat, "from": manager,
                                  "text": "/绑定女孩 娜娜子"}})
        captured = []
        old_urlopen = self.telegram_module.urlopen

        def fake_ai_urlopen(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                captured.append(json.loads(req.data.decode("utf-8")))
                return FakeTelegramResponse({"output_text": "好的，我来帮你看看～"})
            return old_urlopen(req, timeout=timeout)

        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = fake_ai_urlopen
        try:
            self.webhook({"message": {"message_id": 3, "chat": {"id": 9125, "type": "private"},
                                      "from": customer, "text": "明天怎么预约？"}})
            self.webhook({"message": {"message_id": 4, "chat": girl_chat, "from": manager,
                                      "text": "艾莉兔 我明天几点出勤？"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)

        self.assertEqual(len(captured), 2)
        customer_body, girl_body = captured
        self.assertIn("客人哥哥", customer_body["instructions"])
        self.assertIn("只能协助TEL预约", customer_body["instructions"])
        self.assertNotIn("店铺收益", customer_body["input"][-1]["content"])
        self.assertIn("称呼对方为“姐姐”", girl_body["instructions"])
        self.assertIn("本群绑定女孩", girl_body["input"][-1]["content"])
        self.assertNotIn("客户身份", girl_body["input"][-1]["content"])

    def test_alice_ai_feedback_teaching_usage_and_customer_continuation(self):
        internal = {"id": -90126, "type": "supergroup", "title": "Alice内部群"}
        manager = {"id": 9126, "first_name": "主人"}
        customer = {"id": 9127, "first_name": "客人"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                  "text": "/绑定审核群"}})
        captured = []
        old_urlopen = self.telegram_module.urlopen

        def fake_ai_urlopen(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                captured.append(json.loads(req.data.decode("utf-8")))
                return FakeTelegramResponse({"model": "gpt-5-mini", "output_text": "先看今日空档。",
                                             "usage": {"input_tokens": 100, "output_tokens": 50,
                                                       "total_tokens": 150}})
            return old_urlopen(req, timeout=timeout)

        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = fake_ai_urlopen
        try:
            self.webhook({"message": {"message_id": 2, "chat": internal, "from": manager,
                                      "text": "艾莉兔 今天怎么安排？"}})
            with self.app_module.conn() as c:
                interaction = dict(c.execute("SELECT * FROM telegram_ai_interactions ORDER BY id DESC LIMIT 1").fetchone())
            self.webhook({"callback_query": {"id": "ai-ok", "from": manager,
                                              "data": f"ai_feedback:approve:{interaction['id']}",
                                              "message": {"message_id": interaction["response_message_id"],
                                                          "chat": internal, "text": "艾莉兔回答"}}})
            self.webhook({"message": {"message_id": 3, "chat": internal, "from": manager,
                                      "text": "艾莉兔 今天怎么安排？"}})

            private_chat = {"id": customer["id"], "type": "private"}
            self.webhook({"callback_query": {"id": "book-ai", "from": customer, "data": "book",
                                              "message": {"chat": private_chat}}})
            self.webhook({"message": {"message_id": 4, "chat": private_chat, "from": customer,
                                      "text": "艾莉兔 怎么预约？"}})
            self.webhook({"message": {"message_id": 5, "chat": private_chat, "from": customer,
                                      "text": "那明天呢？"}})
            self.webhook({"message": {"message_id": 6, "chat": private_chat, "from": customer,
                                      "text": "继续预约"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)

        self.assertEqual(len(captured), 4)
        self.assertIn("主人确认的教学", captured[1]["input"][-1]["content"])
        with self.app_module.conn() as c:
            teaching = c.execute("SELECT * FROM telegram_ai_teachings WHERE source_interaction_id=?",
                                 (interaction["id"],)).fetchone()
            usage = c.execute("SELECT SUM(input_tokens),SUM(output_tokens),SUM(estimated_cost_usd) FROM telegram_ai_usage").fetchone()
            active = c.execute("SELECT active FROM telegram_ai_sessions WHERE chat_id=? AND user_id=?",
                               (str(customer["id"]), str(customer["id"]))).fetchone()[0]
        self.assertIsNotNone(teaching)
        self.assertEqual((usage[0], usage[1]), (400, 200))
        self.assertAlmostEqual(float(usage[2]), 0.0005, places=8)
        self.assertEqual(active, 0)

        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"], "X-Alice-User": "Star"}
        listing = self.client.get("/api/telegram/ai-learning", headers=headers)
        self.assertEqual(listing.status_code, 200, listing.get_data(as_text=True))
        self.assertTrue(listing.json["teachings"])

    def test_customer_ai_failure_is_cute_and_never_exposes_backend_details(self):
        customer = {"id": 9130, "first_name": "客人"}
        old_urlopen = self.telegram_module.urlopen

        def fake_empty_ai(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                return FakeTelegramResponse({"model": "gpt-5-mini", "output": []})
            return old_urlopen(req, timeout=timeout)

        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = fake_empty_ai
        try:
            self.webhook({"message": {"message_id": 1, "chat": {"id": 9130, "type": "private"},
                                      "from": customer, "text": "明天还能预约吗？"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)

        edits = [body for method, body in self.telegram_calls if method == "editMessageText"]
        self.assertTrue(edits)
        self.assertIn("%E5%85%94%E5%85%94", edits[-1])
        self.assertNotIn("OPENAI_API_KEY", edits[-1])
        self.assertNotIn("Render", edits[-1])
        self.assertNotIn("AI+%E6%B2%A1%E6%9C%89%E8%BF%94%E5%9B%9E", edits[-1])

    def test_out_of_scope_ai_question_is_logged_alerted_and_costs_no_api_call(self):
        internal = {"id": -90131, "type": "supergroup", "title": "Alice内部群"}
        manager = {"id": 9131, "first_name": "店长"}
        customer = {"id": 9132, "first_name": "客人"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                  "text": "/绑定审核群"}})
        api_calls = []
        old_urlopen = self.telegram_module.urlopen

        def reject_ai_call(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                api_calls.append(req.full_url)
                raise AssertionError("无关问题不应调用 OpenAI")
            return old_urlopen(req, timeout=timeout)

        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = reject_ai_call
        self.telegram_calls.clear()
        try:
            self.webhook({"message": {"message_id": 2, "chat": {"id": 9132, "type": "private"},
                                      "from": customer, "text": "东京天气怎么样"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)

        self.assertEqual(api_calls, [])
        self.assertTrue(any(method == "sendMessage" and "%E5%85%94%E5%85%94" in body
                            for method, body in self.telegram_calls))
        self.assertTrue(any(method == "sendMessage" and "-90131" in body and "%E5%B7%B2%E6%8B%A6%E6%88%AA" in body
                            for method, body in self.telegram_calls))
        with self.app_module.conn() as c:
            row = dict(c.execute("SELECT * FROM operation_logs ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(row["action_name"], "AI无关询问")
        self.assertEqual(row["log_level"], "WARN")
        self.assertIn("东京天气怎么样", row["detail"])
        self.assertIn('"openai_called": false', row["detail"])

    def test_tutu_alias_works_in_bound_girl_group(self):
        internal = {"id": -90133, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -90134, "type": "supergroup", "title": "娜娜子群"}
        manager = {"id": 9133, "first_name": "店长"}
        self.webhook({"message": {"message_id": 1, "chat": internal, "from": manager,
                                  "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 2, "chat": girl_chat, "from": manager,
                                  "text": "/绑定女孩 娜娜子"}})
        calls = []
        old_urlopen = self.telegram_module.urlopen

        def fake_ai(req, timeout=20):
            if req.full_url == "https://api.openai.com/v1/responses":
                calls.append(json.loads(req.data.decode("utf-8")))
                return FakeTelegramResponse({"model": "gpt-5-mini", "output_text": "姐姐今天19点出勤哦～"})
            return old_urlopen(req, timeout=timeout)

        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        os.environ["ALICE_AI_ASSISTANT_SYNC"] = "1"
        self.telegram_module.urlopen = fake_ai
        try:
            self.webhook({"message": {"message_id": 3, "chat": girl_chat, "from": manager,
                                      "text": "兔兔 我今天几点出勤？"}})
        finally:
            self.telegram_module.urlopen = old_urlopen
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ALICE_AI_ASSISTANT_SYNC", None)
        self.assertEqual(len(calls), 1)
        self.assertIn("称呼对方为“姐姐”", calls[0]["instructions"])

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

    def test_bound_group_reply_import_and_date_prefix_accept_compact_chain(self):
        manager = {"id": 9750, "first_name": "客服"}
        member = {"id": 9751, "first_name": "女孩"}
        internal = {"id": -30003, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -39999, "type": "supergroup", "title": "娜娜子群"}
        keyword = self.app_module.datetime.strptime(self.day, "%Y-%m-%d").strftime("%m%d")
        chain = f"{keyword}\n1.7-8（1.6）1547\n2.8-10 3.3 0016 积分折扣"
        with self.app_module.conn() as c:
            c.execute("INSERT INTO customers(customer_no,name) VALUES('1547','编号1547')")
            c.execute("INSERT INTO customers(customer_no,name) VALUES('0016','编号0016')")
        self.webhook({"message": {"message_id": 100, "chat": internal, "from": manager, "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 101, "chat": girl_chat, "from": manager, "text": "/绑定女孩 娜娜子"}})
        self.webhook({"message": {
            "message_id": 102, "chat": girl_chat, "from": member, "text": "/导入",
            "reply_to_message": {"message_id": 99, "text": chain},
        }})
        with self.app_module.conn() as c:
            rows = c.execute("SELECT service_time,received_amount,customer_no FROM orders WHERE order_date=? AND girl_name='娜娜子' ORDER BY service_time",
                             (self.day,)).fetchall()
        self.assertEqual([(row["service_time"], row["received_amount"], row["customer_no"]) for row in rows],
                         [("19:00-20:00", 16000, "1547"), ("20:00-22:00", 33000, "0016")])

        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 103, "chat": girl_chat, "from": member, "text": "导入" + chain}})
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders WHERE order_date=? AND girl_name='娜娜子'", (self.day,)).fetchone()[0], 2)
        sent_bodies = [body for method, body in self.telegram_calls if method == "sendMessage"]
        self.assertTrue(any("chat_id=-30003" in body and "%E6%9C%AA%E5%8F%98%E5%8C%96%EF%BC%9A2" in body
                            for body in sent_bodies), sent_bodies)
        self.assertFalse(any("chat_id=-39999" in body for body in sent_bodies))

    def test_points_discount_clears_old_points_adds_500_and_bot_queries_customer(self):
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name) VALUES('0420','积分测试客人')")
            customer_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,customer_name,
                         received_amount,points,points_used,order_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (self.day, "18:00-19:00", girl_id, "娜娜子", customer_id, "0420", "积分测试客人",
                       30000, 1500, 0, "已结束"))
            c.execute("UPDATE customers SET points=1500 WHERE id=?", (customer_id,))
            discount_id = self.app_module.create_or_update_order(c, {
                "order_date": self.day, "service_time": "19:00-20:00", "girl_id": girl_id,
                "received_amount": 12345, "customer_raw": "0420", "remark": "积分折扣",
                "order_status": "预约中", "settlement_status": "未结算",
            })
            discount = c.execute("SELECT * FROM orders WHERE id=?", (discount_id,)).fetchone()
            customer = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        self.assertEqual(discount["received_amount"], 12345)
        self.assertEqual(discount["points_used"], 1500)
        self.assertEqual(discount["points"], 500)
        self.assertIn("积分抵扣金额：¥1,500", discount["remark"])
        self.assertEqual(customer["points"], 500)

        with self.app_module.conn() as c:
            self.app_module.create_or_update_order(c, {
                "id": discount_id, "order_date": self.day, "service_time": "19:00-20:00", "girl_id": girl_id,
                "received_amount": 12345, "customer_raw": "0420", "remark": "积分折扣",
                "order_status": "预约中", "settlement_status": "未结算",
            })
            customer = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        self.assertEqual(customer["points"], 500)

        manager = {"id": 9760, "first_name": "客服"}
        internal = {"id": -30003, "type": "supergroup", "title": "Alice内部群"}
        self.webhook({"message": {"message_id": 110, "chat": internal, "from": manager, "text": "/绑定审核群"}})
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 111, "chat": internal, "from": manager, "text": "积分查询+0420"}})
        self.assertTrue(any(method == "sendMessage" and "500" in body and "0420" in body for method, body in self.telegram_calls))
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 112, "chat": internal, "from": manager, "text": "编号查询+积分测试客人"}})
        self.assertTrue(any(method == "sendMessage" and "0420" in body for method, body in self.telegram_calls))

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
        with self.app_module.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM orders WHERE order_date=? AND girl_name='未出勤女孩'",
                                        (auto_day,)).fetchone())
        self.webhook({"message": {
            "message_id": 75,
            "chat": {"id": -38888, "type": "supergroup", "title": "其他女孩群"},
            "from": manager, "text": f"{date_keyword}\n1.20-21/15000/修改后导入客人",
        }})
        with self.app_module.conn() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM orders WHERE order_date=? AND girl_name='未出勤女孩'",
                                           (auto_day,)).fetchone())

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
        old_keyword = "1332"
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
        with self.app_module.conn() as c:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM orders WHERE order_date=? AND girl_name='娜娜子' AND customer_name='仅日期自动客人'",
                (auto_day,)).fetchone())

        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 83, "chat": girl_chat, "from": manager, "text": date_keyword}})
        with self.app_module.conn() as c:
            status = c.execute("SELECT status FROM telegram_chain_inbox WHERE chat_id=? AND message_id=83",
                               (str(girl_chat["id"]),)).fetchone()[0]
        self.assertEqual(status, "empty")
        self.assertFalse(any(method == "sendMessage" for method, _body in self.telegram_calls))

        self.webhook({"message": {"message_id": 84, "chat": internal, "from": manager, "text": "自动导入关闭"}})
        with self.app_module.conn() as c:
            value = c.execute("SELECT setting_value FROM telegram_settings WHERE setting_key='auto_chain_import_enabled'").fetchone()[0]
        self.assertEqual(value, "0")
        self.telegram_calls.clear()
        changed_chain = f"{date_keyword}\n1.19:30-20:30/15000/仅日期自动客人"
        self.webhook({"message": {"message_id": 841, "chat": girl_chat, "from": manager, "text": changed_chain}})
        changed_notices = [body for method, body in self.telegram_calls if method == "sendMessage" and "chat_id=-30003" in body]
        self.assertTrue(any("%E4%BF%AE%E6%94%B9%EF%BC%9A1" in body for body in changed_notices), changed_notices)
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 842, "chat": girl_chat, "from": manager, "text": changed_chain}})
        self.assertFalse(any(method == "sendMessage" and "chat_id=-30003" in body for method, body in self.telegram_calls))
        self.webhook({"message": {"message_id": 85, "chat": internal, "from": manager, "text": "自动导入开启"}})
        with self.app_module.conn() as c:
            value = c.execute("SELECT setting_value FROM telegram_settings WHERE setting_key='auto_chain_import_enabled'").fetchone()[0]
        self.assertEqual(value, "1")
        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 86, "chat": internal, "from": manager, "text": "自动导入状态"}})
        self.assertTrue(any(method == "sendMessage" and "%E4%B8%8B%E6%AC%A1%E8%87%AA%E5%8A%A8%E5%AF%BC%E5%85%A5" in body
                            for method, body in self.telegram_calls))

    def test_bound_group_accepts_far_future_chain_and_keeps_one_bot_table(self):
        future = self.app_module._tokyo_now().date() + timedelta(days=45)
        future_day = future.isoformat()
        keyword = future.strftime("%m%d")
        manager = {"id": 9610, "first_name": "店长"}
        internal = {"id": -30103, "type": "supergroup", "title": "Alice内部群"}
        girl_chat = {"id": -39103, "type": "supergroup", "title": "娜娜子群"}
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO pure_shifts(shift_date,girl_name,start_time,end_time)
                         VALUES(?,?,?,?)""", (future_day, "娜娜子", "18:00", "23:30"))
        self.webhook({"message": {"message_id": 900, "chat": internal, "from": manager,
                                  "text": "/绑定审核群"}})
        self.webhook({"message": {"message_id": 901, "chat": girl_chat, "from": manager,
                                  "text": "/绑定女孩 娜娜子"}})

        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 902, "chat": girl_chat, "from": manager,
                                  "text": f"{keyword}\n1.18-19/15000/人工未来客人"}})
        with self.app_module.conn() as c:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM orders WHERE order_date=? AND girl_name='娜娜子' AND customer_name='人工未来客人'",
                (future_day,)).fetchone())
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM telegram_daily_chain_messages WHERE booking_date=? AND girl_name='娜娜子'",
                (future_day,)).fetchone()[0], 1)
        self.assertTrue(any(method == "sendMessage" and "chat_id=-39103" in body
                            for method, body in self.telegram_calls))
        self.assertTrue(any(method == "deleteMessage" and "message_id=902" in body
                            for method, body in self.telegram_calls))

        self.telegram_calls.clear()
        self.webhook({"message": {"message_id": 903, "chat": girl_chat, "from": manager,
                                  "text": f"{keyword}\n1.18-19/15000/人工未来客人\n2.20-21/15000/新增未来客人"}})
        self.assertTrue(any(method == "editMessageText" and "chat_id=-39103" in body
                            for method, body in self.telegram_calls))
        self.assertTrue(any(method == "deleteMessage" and "message_id=903" in body
                            for method, body in self.telegram_calls))
        with self.app_module.conn() as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM telegram_daily_chain_messages WHERE booking_date=? AND girl_name='娜娜子'",
                (future_day,)).fetchone()[0], 1)

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
        self.assertEqual(response.json["customers"][0]["points"], 750)
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

    def test_login_survives_process_memory_reset_and_second_same_account_login(self):
        first = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(first.status_code, 200)
        first_headers = {"X-Alice-Session": first.json["session_token"]}
        self.app_module.ACTIVE_SESSIONS.clear()
        restored = self.client.get("/api/all", headers=first_headers)
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertTrue(restored.json["ok"])

        second = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(second.status_code, 200)
        still_valid = self.client.get("/api/all", headers=first_headers)
        self.assertEqual(still_valid.status_code, 200, still_valid.get_data(as_text=True))

    def test_boss_can_query_per_admin_operation_logs(self):
        login = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        headers = {"X-Alice-Role": "boss", "X-Alice-Session": login.json["session_token"]}
        saved = self.client.post("/api/pure_shifts", headers=headers, json={
            "date": self.day, "girl": "娜娜子", "start": "20:00", "end": "23:00"
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        today = self.app_module._tokyo_now().date().isoformat()
        logs = self.client.get(f"/api/operation_logs?date={today}", headers=headers)
        self.assertEqual(logs.status_code, 200, logs.get_data(as_text=True))
        saved_log = next(row for row in logs.json["logs"]
                         if row["actor_name"] == "Star" and row["target"] == "/api/pure_shifts")
        self.assertEqual(saved_log['log_level'], 'INFO')
        self.assertIn(saved_log['action_name'], ('新增或提交数据', '修改数据'))
        self.assertIn('娜娜子', saved_log['detail'])
        self.assertIn('20:00', saved_log['detail'])

    def test_frontend_button_click_is_logged_and_searchable(self):
        admin = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        admin_headers = {"X-Alice-Role": "admin", "X-Alice-Session": admin.json["session_token"]}
        clicked = self.client.post("/api/operation_logs/frontend", headers=admin_headers, json={
            "label": "全面同步", "module": "pureShift", "handler": "sendPureShiftReport()"
        })
        self.assertEqual(clicked.status_code, 200, clicked.get_data(as_text=True))
        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        boss_headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"]}
        today = self.app_module._tokyo_now().date().isoformat()
        logs = self.client.get(f"/api/operation_logs?date={today}&actor=admin&q=全面同步", headers=boss_headers)
        self.assertEqual(logs.status_code, 200, logs.get_data(as_text=True))
        self.assertTrue(any(row["method"] == "CLICK" and row["target"] == "pureShift"
                            for row in logs.json["logs"]), logs.get_data(as_text=True))
        click_log = next(row for row in logs.json['logs'] if row['method'] == 'CLICK')
        self.assertEqual(click_log['log_level'], 'DEBUG')
        self.assertEqual(click_log['action_name'], '点击按钮')

    def test_management_log_permission_is_separate_from_login_audit(self):
        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        boss_headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"]}
        created = self.client.post('/api/system/users', headers=boss_headers, json={
            'username': 'audit_viewer', 'password': 'audit1234', 'label': '日志查看',
            'role': 'user', 'enabled': True, 'permissions': ['operationAudit']
        })
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        login = self.client.post('/api/login', json={'username': 'audit_viewer', 'password': 'audit1234'})
        headers = {"X-Alice-Role": "user", "X-Alice-Session": login.json["session_token"]}
        today = self.app_module._tokyo_now().date().isoformat()
        self.assertEqual(self.client.get(f'/api/operation_logs?date={today}', headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f'/api/login_audit?date={today}', headers=headers).status_code, 403)

    def test_management_log_records_get_query_and_warn_level(self):
        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"]}
        queried = self.client.get(f'/api/pure_shifts?date={self.day}', headers=headers)
        self.assertEqual(queried.status_code, 200)
        today = self.app_module._tokyo_now().date().isoformat()
        logs = self.client.get(f'/api/operation_logs?date={today}&level=DEBUG&method=GET&q=pure_shifts', headers=headers)
        self.assertTrue(any(x['target'] == '/api/pure_shifts' and self.day in x['detail']
                            for x in logs.json['logs']), logs.get_data(as_text=True))
        user = self.client.post('/api/login', json={'username': 'user', 'password': 'user123'})
        user_headers = {"X-Alice-Role": "user", "X-Alice-Session": user.json["session_token"]}
        self.assertEqual(self.client.get('/api/system/users', headers=user_headers).status_code, 403)
        warnings = self.client.get(f'/api/operation_logs?date={today}&level=WARN&q=system/users', headers=headers)
        self.assertTrue(any(x['actor_name'] == 'user' and x['response_status'] == 403
                            for x in warnings.json['logs']), warnings.get_data(as_text=True))

    def test_zero_order_customer_cleanup_is_batched_and_archived(self):
        boss = self.client.post("/api/login", json={"username": "Star", "password": "9941"})
        headers = {"X-Alice-Role": "boss", "X-Alice-Session": boss.json["session_token"]}
        with self.app_module.conn() as c:
            cur = c.execute("""INSERT INTO customers(customer_no,name,recharge_balance,points)
                               VALUES('8888','零单清理测试',3000,200)""")
            zero_id = int(cur.lastrowid)
            c.execute("INSERT INTO recharge_records(customer_id,customer_no,amount) VALUES(?,?,?)",
                      (zero_id, '8888', 3000))
            c.execute("""INSERT INTO customers(customer_no,name) VALUES('8889','有单保留测试')""")
            keep_id = int(c.execute("SELECT id FROM customers WHERE customer_no='8889'").fetchone()[0])
            c.execute("INSERT INTO orders(order_date,customer_id,customer_no,customer_name) VALUES(?,?,?,?)",
                      (self.day, keep_id, '8889', '有单保留测试'))
        preview = self.client.post('/api/customers/cleanup_zero_orders', headers=headers, json={"execute": False})
        self.assertGreaterEqual(preview.json['count'], 1)
        cleaned = self.client.post('/api/customers/cleanup_zero_orders', headers=headers, json={"execute": True})
        self.assertEqual(cleaned.status_code, 200, cleaned.get_data(as_text=True))
        with self.app_module.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM customers WHERE id=?", (zero_id,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM customers WHERE id=?", (keep_id,)).fetchone())
            archive = c.execute("SELECT payload_json FROM customer_cleanup_archives WHERE batch_id=?",
                                (cleaned.json['backup_batch_id'],)).fetchone()
            self.assertIn('零单清理测试', archive['payload_json'])

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

    def test_girl_closing_keyword_confirms_payment_and_next_attendance(self):
        today = self.telegram_module.closing_business_date(self.app_module._tokyo_now())
        tomorrow = (self.app_module._tokyo_now().date() + timedelta(days=1)).isoformat()
        girl_chat = {"id": -51001, "type": "supergroup", "title": "娜娜子专属群"}
        girl = {"id": 510, "first_name": "娜娜子"}
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,enabled)
                         VALUES('娜娜子','-51001','娜娜子专属群',1)""")
            c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_id','-90000')
                         ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""")
            c.execute("""INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,received_amount,payment_method,order_status)
                         VALUES(?,?,?,?,?,?,?)""", (today, '娜娜子', 10000, 5000, 15000, '现金', '已结束'))
            c.execute("""INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,received_amount,payment_method,order_status)
                         VALUES(?,?,?,?,?,?,?)""", (today, '娜娜子', 10000, 5000, 15000, '转账', '已结束'))
        self.webhook({"message": {"message_id": 301, "chat": girl_chat, "from": girl, "text": "下班"}})
        with self.app_module.conn() as c:
            closing = dict(c.execute("SELECT * FROM telegram_closing_confirmations").fetchone())
        self.assertEqual(closing['order_count'], 2)
        self.assertEqual(closing['girl_earnings'], 20000)
        self.assertEqual(closing['store_profit'], 10000)
        self.assertEqual(closing['non_cash_received'], 15000)
        self.webhook({"callback_query": {"id": "close-pay", "from": girl,
                      "data": f"closing:pay:{closing['id']}:transfer", "message": {"chat": girl_chat}}})
        with self.app_module.conn() as c:
            closing = dict(c.execute("SELECT * FROM telegram_closing_confirmations WHERE id=?", (closing['id'],)).fetchone())
        self.assertEqual(closing['status'], 'await_attendance')
        self.webhook({"message": {"message_id": 302, "chat": girl_chat, "from": girl,
                      "reply_to_message": {"message_id": closing['attendance_prompt_message_id']},
                      "text": "明天 18-23"}})
        with self.app_module.conn() as c:
            closing = c.execute("SELECT * FROM telegram_closing_confirmations WHERE id=?", (closing['id'],)).fetchone()
            shift = c.execute("SELECT * FROM pure_shifts WHERE shift_date=? AND girl_name='娜娜子'", (tomorrow,)).fetchone()
        self.assertEqual(closing['status'], 'completed')
        self.assertEqual(closing['settlement_method'], '线上转账')
        self.assertEqual((shift['start_time'], shift['end_time']), ('18:00', '23:00'))

    def test_full_time_girl_closing_skips_next_attendance_question(self):
        today = self.telegram_module.closing_business_date(self.app_module._tokyo_now())
        girl_chat = {"id": -51002, "type": "supergroup", "title": "全职女孩专属群"}
        girl = {"id": 511, "first_name": "娜娜子"}
        with self.app_module.conn() as c:
            c.execute("UPDATE girls SET girl_type='全职' WHERE name='娜娜子'")
            c.execute("""INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,enabled)
                         VALUES('娜娜子','-51002','全职女孩专属群',1)""")
            c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_id','-90000')
                         ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""")
            c.execute("""INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,received_amount,payment_method,order_status)
                         VALUES(?,?,?,?,?,?,?)""", (today, '娜娜子', 10000, 5000, 15000, '现金', '已结束'))
        self.webhook({"message": {"message_id": 311, "chat": girl_chat, "from": girl, "text": "下班"}})
        with self.app_module.conn() as c:
            closing_id = int(c.execute("SELECT id FROM telegram_closing_confirmations").fetchone()[0])
        self.webhook({"callback_query": {"id": "fulltime-pay", "from": girl,
                      "data": f"closing:pay:{closing_id}:cash", "message": {"chat": girl_chat}}})
        with self.app_module.conn() as c:
            closing = c.execute("SELECT * FROM telegram_closing_confirmations WHERE id=?", (closing_id,)).fetchone()
        self.assertEqual(closing['status'], 'completed')
        self.assertEqual(closing['attendance_prompt_message_id'], 0)
        self.assertEqual(closing['next_attendance_text'], '全职，无需填写下次出勤')

    def test_internal_closing_keyword_sends_each_bound_girl_but_down_does_not(self):
        today = self.telegram_module.closing_business_date(self.app_module._tokyo_now())
        internal = {"id": -90000, "type": "supergroup", "title": "Alice内部群"}
        manager = {"id": 9001, "first_name": "店长"}
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_id','-90000')
                         ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""")
            c.execute("INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,enabled) VALUES('娜娜子','-53001','娜娜子专属群',1)")
            c.execute("INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,enabled) VALUES('有房女孩','-53002','有房女孩专属群',1)")
            c.execute("INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,order_status) VALUES(?,?,?,?,?)",
                      (today, '娜娜子', 10000, 5000, '已结束'))
            c.execute("INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,order_status) VALUES(?,?,?,?,?)",
                      (today, '有房女孩', 10000, 5000, '已结束'))
        self.webhook({"message": {"message_id": 320, "chat": internal, "from": manager, "text": "下班"}})
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM telegram_closing_confirmations").fetchone()[0], 0)
        self.webhook({"message": {"message_id": 321, "chat": internal, "from": manager, "text": "闭店"}})
        with self.app_module.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM telegram_closing_confirmations").fetchone()[0], 2)
        self.assertTrue(any(method == 'sendMessage' and 'chat_id=-53001' in body for method, body in self.telegram_calls))
        self.assertTrue(any(method == 'sendMessage' and 'chat_id=-53002' in body for method, body in self.telegram_calls))

    def test_settlement_screenshot_only_sends_screenshot(self):
        today = self.app_module._tokyo_now().date().isoformat()
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO telegram_group_bindings(girl_name,chat_id,chat_title,enabled)
                         VALUES('娜娜子','-52001','娜娜子专属群',1)""")
            c.execute("""INSERT INTO telegram_settings(setting_key,setting_value) VALUES('default_review_chat_id','-90000')
                         ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""")
            c.execute("INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,order_status) VALUES(?,?,?,?,?)",
                      (today, '娜娜子', 10000, 5000, '已结束'))
            c.execute("INSERT INTO orders(order_date,girl_name,girl_take_home,store_profit,order_status) VALUES(?,?,?,?,?)",
                      (today, '有房女孩', 10000, 5000, '已结束'))
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        response = self.client.post('/api/telegram/report-photo', headers=headers, json={
            'kind': 'settlement', 'date': today, 'image_data': 'data:image/png;base64,ZmFrZS1wbmc='
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json['closing'], {})
        self.assertTrue(any(method == 'sendDocument' and 'alice-settlement-' in body
                            for method, body in self.telegram_calls))
        self.assertFalse(any(method == 'sendMessage' and 'chat_id=-52001' in body for method, body in self.telegram_calls))

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
        self.assertEqual(loaded.json["girl_tags"]["娜娜子"], "年纪小 新人 普通")
        self.assertEqual(loaded.json["girl_gold_tags"]["娜娜子"], "房间 推荐")

    def test_after_22_full_sync_new_attendance_auto_adds_tel_once(self):
        login = self.client.post("/api/login", json={"username": "admin", "password": "admin123"})
        headers = {"X-Alice-Role": "admin", "X-Alice-Session": login.json["session_token"]}
        original_now = self.app_module._tokyo_now
        late_now = original_now().replace(hour=22, minute=30, second=0, microsecond=0)
        day = late_now.date().isoformat()
        with self.app_module.conn() as c:
            c.execute("INSERT INTO girls(name,girl_status,list_price) VALUES('夜间新增女孩','在职',15000)")
            c.execute("INSERT INTO telegram_full_sync_days(sync_date,late_auto_enabled) VALUES(?,1)", (day,))
        self.app_module._tokyo_now = lambda: late_now
        try:
            first = self.client.post('/api/pure_shifts', headers=headers, json={
                'date': day, 'girl': '夜间新增女孩', 'start': '23:00', 'end': '02:00'
            })
            self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
            self.assertTrue(first.json['tel_auto_synced'])
            second = self.client.post('/api/pure_shifts', headers=headers, json={
                'id': first.json['id'], 'date': day, 'girl': '夜间新增女孩',
                'start': '23:30', 'end': '02:00'
            })
            self.assertFalse(second.json['tel_auto_synced'])
        finally:
            self.app_module._tokyo_now = original_now
        with self.app_module.conn() as c:
            rows = c.execute("""SELECT * FROM telegram_daily_girls
                                WHERE booking_date=? AND girl_name='夜间新增女孩'""", (day,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'late_attendance_auto')

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

    def test_neko_service_field_and_attendance_page_use_alias_only(self):
        html = '''<form id="post"><div class="acf-field" data-key="field_service">
        <label>服务(详细说明,可以写多行)</label>
        <textarea name="acf[field_service]">旧文案</textarea></div></form>'''
        self.assertEqual(self.app_module._wordpress_service_field_name(html), 'acf[field_service]')
        self.assertEqual(self.app_module._wordpress_attendance_first_line(
            '旧服务说明第一行\n第二行内容', '今日出勤：19:00-23:00'),
            '今日出勤：19:00-23:00\n旧服务说明第一行\n第二行内容')
        self.assertEqual(self.app_module._wordpress_attendance_first_line(
            '今日出勤：18:00-22:00\n原有内容', '今日出勤：20:00-24:00'),
            '今日出勤：20:00-24:00\n原有内容')
        page = (self.app_module.APP_DIR / 'static' / 'neko_dona_shift.html').read_text(encoding='utf-8')
        self.assertIn('const PAGE_SIZE=6', page)
        self.assertIn('name:r.alias,start:r.start,end:r.end', page)
        self.assertIn('不再修改官网“今日出勤”汇总页', page)

    def test_neko_attendance_sync_reports_missing_render_credentials(self):
        login = self.client.post('/api/login', json={'username':'admin','password':'admin123'})
        headers = {'X-Alice-Session': login.json['session_token']}
        keys = ('ALICE_NEKO_ADMIN_USER','ALICE_NEKO_ADMIN_PASSWORD','NEKO_ADMIN_USER',
                'NEKO_ADMIN_PASS','NEKO_ADMIN_PASSWORD')
        old = {key: os.environ.pop(key, None) for key in keys}
        try:
            response = self.client.post('/api/neko/attendance-sync', headers=headers, json={
                'date': self.day,
                'attendance': [{'name':'喵马甲','start':'19:00','end':'23:00'}]
            })
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(response.status_code, 502, response.get_data(as_text=True))
        self.assertFalse(response.json['configured'])
        self.assertIn('ALICE_NEKO_ADMIN_USER', response.json['warning'])

    def test_neko_sync_updates_profiles_privates_absent_and_protects_nanami(self):
        originals = (self.app_module.neko_admin_login, self.app_module._wordpress_model_posts,
                     self.app_module._wordpress_update_attendance_line,
                     self.app_module._wordpress_inline_model_status)
        old_user = os.environ.get('ALICE_NEKO_ADMIN_USER')
        old_password = os.environ.get('ALICE_NEKO_ADMIN_PASSWORD')
        text_calls, status_calls = [], []
        os.environ['ALICE_NEKO_ADMIN_USER'] = 'test-user'
        os.environ['ALICE_NEKO_ADMIN_PASSWORD'] = 'test-password'
        self.app_module.neko_admin_login = lambda *_args: object()
        self.app_module._wordpress_model_posts = lambda _opener, _base: ([
            {'id':30,'title':'喵马甲A','status':'private'},
            {'id':29,'title':'喵马甲B','status':'publish'},
            {'id':28,'title':'七海莉莉','status':'publish'},
            {'id':27,'title':'今日出勤','status':'publish'},
        ], 'nonce')
        self.app_module._wordpress_update_attendance_line = lambda _opener, post, line, _base: (
            text_calls.append((post['id'], line)) or {'changed':True})
        self.app_module._wordpress_inline_model_status = lambda _opener, post, status, _nonce, **_kw: (
            status_calls.append((post['id'], status)))
        try:
            result = self.app_module.sync_neko_wordpress_attendance(
                self.day, [{'name':'喵马甲A','start':'19:00','end':'23:00'}])
        finally:
            (self.app_module.neko_admin_login, self.app_module._wordpress_model_posts,
             self.app_module._wordpress_update_attendance_line,
             self.app_module._wordpress_inline_model_status) = originals
            if old_user is None: os.environ.pop('ALICE_NEKO_ADMIN_USER', None)
            else: os.environ['ALICE_NEKO_ADMIN_USER'] = old_user
            if old_password is None: os.environ.pop('ALICE_NEKO_ADMIN_PASSWORD', None)
            else: os.environ['ALICE_NEKO_ADMIN_PASSWORD'] = old_password
        self.assertEqual(text_calls, [(30, '今日出勤：19:00-23:00')])
        self.assertIn((30, 'publish'), status_calls)
        self.assertIn((29, 'private'), status_calls)
        self.assertFalse(any(post_id in (27,28) for post_id, _status in status_calls))
        self.assertEqual(result['protected'], ['七海莉莉'])
        self.assertTrue(result['synced'])

    def test_wordpress_visibility_privates_every_non_attending_model(self):
        original_posts = self.app_module._wordpress_model_posts
        original_update = self.app_module._wordpress_inline_model_status
        changed = []
        self.app_module._wordpress_model_posts = lambda _opener: ([
            {'id':10,'title':'娜娜子','status':'private'},
            {'id':9,'title':'娜娜子','status':'publish'},
            {'id':8,'title':'官网旧女孩','status':'publish'},
            {'id':7,'title':'今日出勤','status':'publish'},
            {'id':6,'title':'招聘优秀女生','status':'private'},
            {'id':5,'title':'价格和玩法','status':'publish'},
            {'id':4,'title':'爱丽丝积分活动','status':'publish'},
            {'id':3,'title':'商务陪酒','status':'private'},
        ], 'nonce')
        self.app_module._wordpress_inline_model_status = lambda _opener, post, status, _nonce: changed.append((post['id'],status))
        try:
            result = self.app_module.sync_alice_wordpress_girl_visibility(object(), ['娜娜子'], ['娜娜子'])
        finally:
            self.app_module._wordpress_model_posts = original_posts
            self.app_module._wordpress_inline_model_status = original_update
        self.assertIn((10,'publish'), changed)
        self.assertIn((9,'private'), changed)
        self.assertIn((8,'private'), changed)
        self.assertFalse(any(post_id in {3,4,5,6,7} for post_id, _status in changed))
        self.assertEqual(result['protected'], 5)
        self.assertTrue(result['synced'])

    def test_points_audit_reports_unstructured_note_and_manual_use_is_clamped(self):
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('0888','积分审计客人',100)")
            customer_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,customer_name,
                         received_amount,points,points_used,order_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (self.day,'17:00-18:00',girl_id,'娜娜子',customer_id,'0888','积分审计客人',20000,1,0,'已结束'))
            self.app_module.create_or_update_order(c, {'order_date':self.day,'service_time':'18:00-19:00','girl_id':girl_id,
                'customer_raw':'0888','received_amount':10000,'points_used':999999,'order_status':'已结束'})
            latest = c.execute('SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT 1',(customer_id,)).fetchone()
            c.execute("""INSERT INTO orders(order_date,customer_id,customer_no,customer_name,points,points_used,remark,order_status)
                         VALUES(?,?,?,?,1000,0,'积分减免0.2','已结束')""",
                      (self.day,customer_id,'0888','积分审计客人'))
        self.assertEqual(latest['points_used'], 100)
        login = self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        result = self.client.get('/api/customers/points-audit',headers={'X-Alice-Session':login.json['session_token']})
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        self.assertTrue(any(item['kind']=='积分使用备注未登记' and item.get('customer_no')=='0888'
                            for item in result.json['anomalies']))

    def test_new_orders_increment_current_points_and_all_discount_words_trigger(self):
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('0890','增量客人',100)")
            customer_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            self.app_module.create_or_update_order(c, {'order_date':self.day,'service_time':'17:00-18:00',
                'girl_id':girl_id,'customer_raw':'0890','received_amount':20000,'order_status':'已结束'})
            customer = c.execute('SELECT points FROM customers WHERE id=?',(customer_id,)).fetchone()
            self.assertEqual(customer['points'],1100)
            normal_id = c.execute("SELECT id FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT 1",(customer_id,)).fetchone()['id']
            self.app_module.create_or_update_order(c, {'id':normal_id,'order_date':self.day,'service_time':'17:30-18:30',
                'girl_id':girl_id,'customer_raw':'0890','received_amount':40000,'order_status':'已结束'})
            self.assertEqual(c.execute('SELECT points FROM customers WHERE id=?',(customer_id,)).fetchone()['points'],1100)
            for index, keyword in enumerate(('积分减免0.2','积分抵扣2000','积分折扣','积分全扣'),start=1):
                c.execute('UPDATE customers SET points=1200 WHERE id=?',(customer_id,))
                self.app_module.create_or_update_order(c, {'order_date':self.day,'service_time':'19:00-20:00',
                    'girl_id':girl_id,'customer_raw':'0890','received_amount':18000,'remark':keyword,'order_status':'已结束'})
                order = c.execute('SELECT points,points_used,remark FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT 1',(customer_id,)).fetchone()
                self.assertEqual((order['points'],order['points_used']),(500,1200),keyword)
                self.assertEqual(c.execute('SELECT points FROM customers WHERE id=?',(customer_id,)).fetchone()['points'],500)

    def test_customer_ledger_preserves_opening_balance_and_rate_is_future_only(self):
        login = self.client.post('/api/login', json={'username':'Star','password':'9941'})
        headers = {'X-Alice-Session':login.json['session_token']}
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points,recharge_balance) VALUES('1227','账本客人',7200,5000)")
            customer_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,
                         customer_name,received_amount,points,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (self.day,'17:00-18:00',girl_id,'娜娜子',customer_id,'1227','账本客人',20000,1000,'已结束'))
        opened = self.client.get(f'/api/customer_ledger?customer_id={customer_id}', headers=headers)
        self.assertEqual(opened.status_code, 200, opened.get_data(as_text=True))
        self.assertEqual(opened.json['customer']['points'], 7200)
        self.assertEqual(opened.json['customer']['recharge_balance'], 5000)
        self.assertEqual({x['transaction_type'] for x in opened.json['entries']}, {'期初余额'})
        changed = self.client.post('/api/loyalty/settings', headers=headers,
                                   json={'enabled':True,'point_rate_percent':3})
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        with self.app_module.conn() as c:
            rule_log = c.execute("SELECT * FROM operation_logs WHERE action_name='修改全局积分规则' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(rule_log)
        self.assertIn('旧积分比例', rule_log['detail'])
        self.assertIn('新积分比例', rule_log['detail'])
        with self.app_module.conn() as c:
            self.app_module.create_or_update_order(c, {'order_date':self.day,'service_time':'18:00-19:00',
                'girl_id':girl_id,'customer_raw':'1227','received_amount':20000,'order_status':'已结束'})
            customer = c.execute('SELECT points FROM customers WHERE id=?',(customer_id,)).fetchone()
            old_order = c.execute('SELECT points FROM orders WHERE customer_id=? ORDER BY id LIMIT 1',(customer_id,)).fetchone()
        self.assertEqual(customer['points'], 7800)
        self.assertEqual(old_order['points'], 1000)
        adjusted = self.client.post('/api/customer_ledger', headers=headers, json={
            'customer_id':customer_id,'account_type':'points','action':'deduct','amount':300,
            'transaction_type':'积分扣除','reason':'测试核对','request_id':'ledger-test-1'})
        self.assertEqual(adjusted.status_code, 200, adjusted.get_data(as_text=True))
        self.assertEqual(adjusted.json['customer']['points'], 7500)

    def test_customer_ledger_rollback_switch_restores_legacy_rate_without_new_rows(self):
        login = self.client.post('/api/login', json={'username':'Star','password':'9941'})
        headers = {'X-Alice-Session':login.json['session_token']}
        disabled = self.client.post('/api/loyalty/settings', headers=headers,
                                    json={'enabled':False,'point_rate_percent':3})
        self.assertEqual(disabled.status_code, 200, disabled.get_data(as_text=True))
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('1228','回滚客人',100)")
            customer_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            self.app_module.create_or_update_order(c, {'order_date':self.day,'service_time':'18:00-19:00',
                'girl_id':girl_id,'customer_raw':'1228','received_amount':20000,'order_status':'已结束'})
            self.assertEqual(c.execute('SELECT points FROM customers WHERE id=?',(customer_id,)).fetchone()['points'],1100)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customer_ledger WHERE idempotency_key LIKE 'order:%' ").fetchone()[0],0)

    def test_daily_point_expiry_alert_excludes_recharge_customers(self):
        alert_day = datetime.strptime(self.day, '%Y-%m-%d').date()
        last_day = (alert_day - timedelta(days=25)).isoformat()
        with self.app_module.conn() as c:
            c.execute("UPDATE telegram_settings SET setting_value='-90000' WHERE setting_key='default_review_chat_id'")
            c.execute("INSERT INTO customers(customer_no,name,points) VALUES('1301','普通高积分',1800)")
            normal_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,points,total_recharge) VALUES('1302','充值高积分',2200,50000)")
            recharge_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            for customer_id, customer_no, customer_name in (
                    (normal_id,'1301','普通高积分'), (recharge_id,'1302','充值高积分')):
                c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,
                             customer_name,received_amount,points,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (last_day,'18:00-19:00',girl_id,'娜娜子',customer_id,customer_no,
                           customer_name,20000,1000,'已结束'))
        login = self.client.post('/api/login', json={'username':'admin','password':'admin123'})
        result = self.client.post('/api/telegram/point-expiry-alert/run',
                                  headers={'X-Alice-Session':login.json['session_token']},
                                  json={'date':self.day,'force':True})
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertTrue(result.json['sent'])
        self.assertEqual(result.json['count'], 1)
        bodies = [body for method, body in self.telegram_calls if method == 'sendMessage']
        self.assertTrue(any('%E6%99%AE%E9%80%9A%E9%AB%98%E7%A7%AF%E5%88%86' in body for body in bodies))
        self.assertFalse(any('%E5%85%85%E5%80%BC%E9%AB%98%E7%A7%AF%E5%88%86' in body for body in bodies))

    def test_point_expiry_alert_only_final_five_days_and_every_other_day(self):
        alert_day = datetime.strptime(self.day, '%Y-%m-%d').date()
        with self.app_module.conn() as c:
            c.execute("UPDATE telegram_settings SET setting_value='-90000' WHERE setting_key='default_review_chat_id'")
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            for no, name, days_left in (('1311','五天客户',5),('1312','六天客户',6),('1313','过期客户',-1)):
                c.execute("INSERT INTO customers(customer_no,name,points) VALUES(?,?,1800)",(no,name))
                cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                last_day=(alert_day + timedelta(days=days_left-30)).isoformat()
                c.execute("""INSERT INTO orders(order_date,service_time,girl_id,girl_name,customer_id,customer_no,
                             customer_name,received_amount,points,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (last_day,'18:00-19:00',girl_id,'娜娜子',cid,no,name,20000,1000,'已结束'))
        login=self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        headers={'X-Alice-Session':login.json['session_token']}
        first=self.client.post('/api/telegram/point-expiry-alert/run',headers=headers,json={'date':self.day}).json
        self.assertTrue(first['sent']);self.assertEqual(first['count'],1)
        next_day=(alert_day+timedelta(days=1)).isoformat()
        skipped=self.client.post('/api/telegram/point-expiry-alert/run',headers=headers,json={'date':next_day}).json
        self.assertFalse(skipped['sent']);self.assertEqual(skipped['reason'],'隔日提醒间隔未到')
        third_day=(alert_day+timedelta(days=2)).isoformat()
        sent_again=self.client.post('/api/telegram/point-expiry-alert/run',headers=headers,json={'date':third_day}).json
        self.assertTrue(sent_again['sent']);self.assertEqual(sent_again['count'],2)

    def test_monthly_vip_and_svip_rankings_can_overlap_and_explain_reason(self):
        month_day = self.app_module._tokyo_now().date().replace(day=1).isoformat()
        with self.app_module.conn() as c:
            girl_id = c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            customer_ids = []
            amounts = [70000,60000,50000,40000,30000,20000,10000]
            hours = [0.5,7,6,5,4,3,2]
            for index in range(7):
                no = f'14{index:02d}'
                c.execute('INSERT INTO customers(customer_no,name) VALUES(?,?)', (no,f'排名客人{index+1}'))
                cid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
                customer_ids.append(cid)
                c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,
                             customer_no,customer_name,received_amount,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (month_day,'18:00-19:00',hours[index],girl_id,'娜娜子',cid,no,
                           f'排名客人{index+1}',amounts[index],'已结束'))
            self.app_module.update_customer_type_by_history(c)
            first = dict(c.execute('SELECT * FROM customers WHERE id=?',(customer_ids[0],)).fetchone())
            second = dict(c.execute('SELECT * FROM customers WHERE id=?',(customer_ids[1],)).fetchone())
            seventh = dict(c.execute('SELECT * FROM customers WHERE id=?',(customer_ids[6],)).fetchone())
        self.assertEqual((first['vip_active'],first['svip_active']),(0,0))
        self.assertEqual((first['vip_current'],first['svip_current']),(1,0))
        self.assertIn('动态消费第1名',first['vip_current_reason'])
        self.assertEqual((second['vip_current'],second['svip_current']),(1,1))
        self.assertEqual(second['customer_type'],'VIP / SVIP')
        self.assertIn('动态时长第1名',second['svip_current_reason'])
        self.assertEqual((seventh['vip_current'],seventh['svip_current']),(0,0))

    def test_previous_month_winners_are_retained_until_manual_cancel(self):
        today=self.app_module._tokyo_now().date()
        previous=(today.replace(day=1)-timedelta(days=1)).replace(day=15).isoformat()
        with self.app_module.conn() as c:
            girl_id=c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name) VALUES('1499','月底冠军')")
            cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,
                         customer_no,customer_name,received_amount,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (previous,'18:00-22:00',4,girl_id,'娜娜子',cid,'1499','月底冠军',88000,'已结束'))
            self.app_module.update_customer_type_by_history(c)
            customer=dict(c.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone())
            history=c.execute("SELECT COUNT(*) FROM customer_membership_history WHERE customer_id=? AND action='获得'",(cid,)).fetchone()[0]
        self.assertEqual((customer['vip_active'],customer['svip_active']),(1,1))
        self.assertEqual(history,2)
        login=self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        result=self.client.post('/api/customer_membership',headers={'X-Alice-Session':login.json['session_token']},
                                json={'customer_id':cid,'membership_type':'VIP','action':'cancel','reason':'长期未消费人工取消'})
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        self.assertEqual(result.json['customer']['vip_active'],0)
        self.assertEqual(result.json['customer']['svip_active'],1)

    def test_customer_auto_tags_order_and_girl_type_tag_memory(self):
        with self.app_module.conn() as c:
            girl_id=c.execute("SELECT id FROM girls WHERE name='娜娜子'").fetchone()[0]
            c.execute("UPDATE girls SET girl_type='萝莉系',tags='清纯 温柔' WHERE id=?",(girl_id,))
            c.execute("INSERT INTO customers(customer_no,name) VALUES('1488','标签客户')")
            cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            for index in range(3):
                c.execute("""INSERT INTO orders(order_date,service_time,hours,girl_id,girl_name,customer_id,
                             customer_no,customer_name,received_amount,order_status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (self.day,f'{18+index}:00-{19+index}:00',1,girl_id,'娜娜子',cid,'1488','标签客户',30000,'已结束'))
            self.app_module.update_customer_type_by_history(c,cid)
            customer=dict(c.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone())
        tags=json.loads(customer['auto_tags'])
        self.assertEqual(tags[:4],['萝莉控','高端消费','积分普通','稳定复购'])
        login=self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        result=self.client.get('/api/pure_shifts?date='+self.day,headers={'X-Alice-Session':login.json['session_token']})
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        self.assertIn('萝莉系',result.json['girl_tags']['娜娜子'])
        self.assertIn('清纯',result.json['girl_tags']['娜娜子'])
    def test_review_drafts_are_labeled_non_customer_quotes(self):
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO scraped_reviews(source_url,source_page,girl_name,review_text,tags,review_hash)
                         VALUES('https://example.com/reviews','https://example.com/reviews','娜娜子',
                         '公开评论素材内容足够长，提到服务很温柔也非常细心。','温柔,服务细心','hash-review-1')""")
        login = self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        result = self.client.post('/api/review_crawler',headers={'X-Alice-Session':login.json['session_token']},
                                  json={'action':'drafts','date':self.day})
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        nana = next(item for item in result.json['drafts'] if item['girl_name']=='娜娜子')
        self.assertIn('非真实客评',nana['label'])
        self.assertEqual(len(nana['short_drafts']),5)

    def test_tokyo_public_report_payload_decoder(self):
        expected = {'report': {'post_id': 99, 'title': '测试长评', 'preview': '公开预览内容'}}
        clear = json.dumps(expected, ensure_ascii=False).encode('utf-8')
        key = self.app_module.TOKYO_REPORT_XOR_KEY
        encoded = base64.b64encode(bytes(value ^ key[index % len(key)] for index, value in enumerate(clear))).decode()
        decoded = self.app_module._decode_tokyo_report_payload(encoded)
        self.assertEqual(decoded['post_id'], 99)
        self.assertEqual(decoded['preview'], '公开预览内容')

    def test_manual_unlocked_review_can_be_archived(self):
        login = self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        headers={'X-Alice-Session':login.json['session_token']}
        result = self.client.post('/api/review_crawler', headers=headers, json={
            'action':'archive', 'girl_name':'娜娜子', 'material_type':'登录后长评',
            'source_url':'https://tokyo-yy.com/精华帖/12345_测试/', 'source_title':'测试长评',
            'author_name':'测试作者', 'review_date':self.day,
            'review_text':'这是本人登录并按网站规则解锁后手动归档的完整评论正文，内容足够长，也提到了服务温柔和细心。' * 10
        })
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        self.assertTrue(result.json['inserted'])
        with self.app_module.conn() as c:
            c.execute("""INSERT INTO scraped_reviews(source_url,source_page,girl_name,review_text,tags,review_hash,material_type)
                         VALUES(?,?,?,?,?,?,?)""", ('https://tokyo-yy.com/精华帖/华人出张店/',
                         'https://tokyo-yy.com/精华帖/12345_测试/', '娜娜子', '这是同一篇长评公开显示的预览内容。',
                         '温柔', 'hash-review-preview-12345', '公开长评预览'))
            c.execute("""INSERT INTO scraped_reviews(source_url,source_page,girl_name,review_text,tags,review_hash,material_type)
                         VALUES(?,?,?,?,?,?,?)""", ('https://tokyo-yy.com/精华帖/华人出张店/',
                         'https://tokyo-yy.com/精华帖/98765_未解锁/', '娜娜子', '这是一段公开预览。您需要回帖后查看隐藏内容',
                         '', 'hash-review-preview-98765', '公开长评预览'))
        listing = self.client.get('/api/review_crawler',headers=headers)
        saved = next(item for item in listing.json['reviews'] if item.get('source_title')=='测试长评')
        self.assertEqual(saved['material_type'],'登录后长评')
        self.assertEqual(saved['author_name'],'测试作者')
        self.assertIn('温柔',saved['tags'])
        polished = self.client.post('/api/review_crawler',headers=headers,json={
            'action':'polish','review_id':saved['id']})
        self.assertEqual(polished.status_code,200,polished.get_data(as_text=True))
        self.assertIn('未新增事实',polished.json['polished']['label'])
        self.assertNotIn('预约',polished.json['polished']['polished_text'])
        preview = next(item for item in listing.json['reviews'] if item.get('review_hash')=='hash-review-preview-12345')
        self.assertEqual(preview['unlock_status'],'已解锁')
        self.assertEqual(preview['status_color'],'green')
        self.assertFalse(preview['needs_unlock'])
        locked = next(item for item in listing.json['reviews'] if item.get('review_hash')=='hash-review-preview-98765')
        self.assertEqual(locked['status_color'],'red')
        self.assertTrue(locked['needs_unlock'])
        self.assertEqual(locked['unlock_status'],'待解锁')
        generated = self.client.post('/api/review_crawler',headers=headers,json={
            'action':'drafts','date':'2099-01-01','girl_name':'娜娜子','custom_description':'笑容甜美，聊天自然，第一次见面也不会尴尬'})
        self.assertEqual(generated.status_code,200,generated.get_data(as_text=True))
        self.assertEqual(generated.json['drafts'][0]['girl_name'],'娜娜子')
        self.assertIn('完整长评',generated.json['drafts'][0]['evidence_summary'])
        self.assertIn('你的补充',generated.json['drafts'][0]['evidence_summary'])
        self.assertIn('笑容甜美',generated.json['drafts'][0]['long_draft'])
        assisted = self.client.post('/api/review_crawler',headers=headers,json={
            'action':'assist_real','girl_name':'娜娜子','original_text':'本人比照片好看，聊天很自然',
            'confirmed_details':'见面不会尴尬，态度温柔'})
        self.assertEqual(assisted.status_code,200,assisted.get_data(as_text=True))
        assisted_text = assisted.json['polished']['polished_text']
        self.assertIn('本人比照片好看',assisted_text)
        self.assertIn('见面不会尴尬',assisted_text)
        self.assertNotIn('按摩',assisted_text)
        self.assertGreaterEqual(assisted.json['polished']['character_count'],70)
        old_urlopen = self.app_module.urlopen
        captured = {}
        def fake_openai(req, timeout=75):
            captured['url'] = req.full_url
            captured['body'] = json.loads(req.data.decode('utf-8'))
            return FakeTelegramResponse({'output':[{'content':[{'type':'output_text','text':json.dumps({
                'text':'本人提供的真实内容经过自然扩写后的测试正文。','style_summary':'短句分段、口语总结','warning':''},ensure_ascii=False)}]}]})
        os.environ['OPENAI_API_KEY'] = 'test-openai-key'
        self.app_module.urlopen = fake_openai
        try:
            ai_result = self.client.post('/api/review_crawler',headers=headers,json={
                'action':'assist_real_ai','girl_name':'娜娜子','original_text':'本人比照片好看',
                'confirmed_details':'聊天自然','author_name':'测试作者','style_strength':'high','target_length':220})
        finally:
            self.app_module.urlopen = old_urlopen
            os.environ.pop('OPENAI_API_KEY',None)
        self.assertEqual(ai_result.status_code,200,ai_result.get_data(as_text=True))
        self.assertEqual(ai_result.json['polished']['author_name'],'测试作者')
        self.assertEqual(ai_result.json['polished']['target_length'],220)
        self.assertEqual(captured['url'],'https://api.openai.com/v1/responses')
        self.assertIn('JSON',captured['body']['instructions'])
        self.assertIn('warning',captured['body']['instructions'])

    def test_new_customer_midnight_digest_is_idempotent(self):
        report_day = (self.app_module._tokyo_now().date()-timedelta(days=1)).isoformat()
        with self.app_module.conn() as c:
            c.execute("UPDATE telegram_settings SET setting_value='-30003' WHERE setting_key='default_review_chat_id'")
            c.execute("INSERT INTO customers(customer_no,name,created_at) VALUES('0777','需要改编号的名字',?)",
                      (report_day+' 10:00:00',))
        login = self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        headers={'X-Alice-Session':login.json['session_token']}
        first=self.client.post('/api/telegram/new-customer-digest/run',headers=headers,json={'date':report_day})
        second=self.client.post('/api/telegram/new-customer-digest/run',headers=headers,json={'date':report_day})
        self.assertTrue(first.json['sent'])
        self.assertFalse(second.json['sent'])
        self.assertEqual(second.json['reason'],'已发送')
        self.assertTrue(any(method=='sendMessage' and '0777' in body for method,body in self.telegram_calls))

    def test_new_customer_name_review_requires_internal_confirmation(self):
        report_day = self.app_module._tokyo_now().date().isoformat()
        internal = {'id':-30003,'type':'supergroup','title':'Alice内部群'}
        manager = {'id':9901,'first_name':'客服'}
        self.webhook({'message':{'message_id':700,'chat':internal,'from':manager,'text':'/绑定审核群'}})
        with self.app_module.conn() as c:
            c.execute("INSERT INTO customers(customer_no,name,source,created_at) VALUES('1701','QQ小王','',?)",(report_day+' 03:00:00',))
            customer_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO customers(customer_no,name,source,created_at) VALUES('1702','20822.6-7','',?)",(report_day+' 03:05:00',))
            c.execute("INSERT INTO customers(customer_no,name,source,created_at) VALUES('1703','池袋2-16-18','',?)",(report_day+' 03:10:00',))
        login=self.client.post('/api/login',json={'username':'admin','password':'admin123'})
        headers={'X-Alice-Session':login.json['session_token']}
        review=self.client.post('/api/telegram/new-customer-name-review/run',headers=headers,json={'date':report_day})
        self.assertEqual(review.status_code,200,review.get_data(as_text=True))
        self.assertEqual(review.json['sent'],3)
        with self.app_module.conn() as c:
            before=dict(c.execute('SELECT name,source FROM customers WHERE id=?',(customer_id,)).fetchone())
            prompt=c.execute("SELECT prompt_message_id FROM telegram_customer_name_reviews WHERE customer_id=? AND issue_type='source_prefix'",(customer_id,)).fetchone()[0]
            suspicious=c.execute("SELECT COUNT(*) FROM telegram_customer_name_reviews WHERE issue_type='suspicious_name'").fetchone()[0]
        self.assertEqual(before,{'name':'QQ小王','source':''})
        self.assertEqual(suspicious,2)
        self.webhook({'message':{'message_id':710,'chat':internal,'from':manager,'text':'OK',
                                 'reply_to_message':{'message_id':prompt,'text':'新客户名称检查'}}})
        with self.app_module.conn() as c:
            after=dict(c.execute('SELECT name,source FROM customers WHERE id=?',(customer_id,)).fetchone())
        self.assertEqual(after,{'name':'小王','source':'QQ'})
        duplicate=self.client.post('/api/telegram/new-customer-name-review/run',headers=headers,json={'date':report_day})
        self.assertEqual(duplicate.json['sent'],0)


if __name__ == "__main__":
    unittest.main()
