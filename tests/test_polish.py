from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from classdiary.app import Application
from classdiary.config import Config
from classdiary.db import Database
from classdiary.service import Diary
from classdiary.telegram import Telegram, TelegramError
from classdiary.ui import homework_text
from classdiary.ui import chunks, text_lines
from tests.test_application import FakeTelegram
from run import process_updates


class CalendarPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.diary = Diary(self.db)
        self.diary.touch_user({"id": 100, "first_name": "Ученик"}, started=True)
        self.cid = self.diary.create_class(100, "9Б", "Europe/Moscow")

    def tearDown(self):
        self.db.close()

    def test_empty_schedule_search_uses_constant_number_of_queries(self):
        queries = []
        self.db.conn.set_trace_callback(queries.append)
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 18)), date(2026, 10, 19))
        self.assertLessEqual(len(queries), 4)

    def test_absent_subject_search_uses_constant_number_of_queries(self):
        queries = []
        self.db.conn.set_trace_callback(queries.append)
        self.assertEqual(self.diary.next_lesson(self.cid, date(2026, 10, 18), "Алгебра", 0), (None, 0))
        self.assertLessEqual(len(queries), 4)

    def test_subject_search_normalizes_spacing_and_yo(self):
        self.diary.replace_weekday(self.cid, 100, 0, [{"position": 1, "subject": "Черчение ё"}])
        self.assertEqual(self.diary.next_lesson(self.cid, date(2026, 10, 18), " ЧЕРЧЕНИЕ   Е ", 0), (date(2026, 10, 19), 1))

    def test_calendar_search_sees_changes_on_next_call(self):
        self.diary.replace_weekday(self.cid, 100, 0, [{"position": 1, "subject": "Алгебра"}])
        self.diary.set_override(self.cid, 100, date(2026, 10, 19), [])
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 18)), date(2026, 10, 26))
        self.diary.clear_override(self.cid, 100, date(2026, 10, 19))
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 18)), date(2026, 10, 19))

    def test_unsupported_schema_is_rejected_without_creating_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
                conn.execute("INSERT INTO meta VALUES('schema_version','99')")
                conn.commit()
            with self.assertRaises(RuntimeError):
                Database(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("meta",)])

    def test_pair_request_is_idempotent_even_at_pending_limit(self):
        for uid in range(101, 111):
            self.diary.touch_user({"id": uid, "first_name": "Ученик"}, started=True)
            self.diary.add_members(self.cid, 100, [{"user_id": uid}])
            self.diary.request_pair(self.cid, 100, uid)
        pid = self.diary.request_pair(self.cid, 100, 101)
        self.assertTrue(pid)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 10)

    def test_old_decline_button_cannot_remove_accepted_pair(self):
        self.diary.touch_user({"id": 101, "first_name": "Ученик"}, started=True)
        self.diary.add_members(self.cid, 100, [{"user_id": 101}])
        pid = self.diary.request_pair(self.cid, 100, 101)
        self.diary.answer_pair(self.cid, 101, pid, True)
        with self.assertRaisesRegex(ValueError, "уже принято"):
            self.diary.answer_pair(self.cid, 101, pid, False)
        self.assertEqual(self.db.scalar("SELECT accepted FROM pairs WHERE id=?", (pid,)), 1)


class StudentExperienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        self.transport = FakeTelegram()
        self.app = Application(Config("test", Path(":memory:")), self.db, self.transport)
        self.app.username = "class_diary_test_bot"
        self.app.diary.touch_user({"id": 100, "first_name": "Ученик"}, started=True)
        self.cid = self.app.diary.create_class(100, "9Б", "Europe/Moscow")

    async def asyncTearDown(self):
        self.db.close()

    async def click(self, data):
        await self.app.on_update({"callback_query": {"id": "q", "from": {"id": 100, "first_name": "Ученик"},
            "data": data, "message": {"message_id": 1, "chat": {"id": 100, "type": "private"}, "text": "Меню"}}})

    def screen(self):
        return next(p for m, p in reversed(self.transport.calls) if m in {"sendMessage", "editMessageText"})

    async def test_large_keyboard_keeps_every_task_reachable(self):
        keyboard = {"inline_keyboard": [[{"text": str(i), "callback_data": f"task:{self.cid}:{i}"}] for i in range(150)]}
        token = await self.app.tg.begin(100)
        try:
            await self.app.tg.present(100, "Задания", keyboard)
            cache = self.app.tg.pages[100]
            found = set()
            for index in range(cache["total"]):
                await self.app.tg.show_page(100, cache["nonce"], index)
                rows = self.screen()["reply_markup"]["inline_keyboard"]
                self.assertLessEqual(sum(map(len, rows)), 100)
                found.update(b["callback_data"] for row in rows for b in row if b["callback_data"].startswith("task:"))
            self.assertEqual(found, {f"task:{self.cid}:{i}" for i in range(150)})
        finally:
            self.app.tg.end(token)

    async def test_page_history_is_bounded_for_one_active_user(self):
        token = await self.app.tg.begin(100)
        try:
            for _ in range(20):
                await self.app.tg.present(100, ("x" * 600 + "\n") * 8)
            self.assertLessEqual(len(self.app.tg.page_history), 12)
        finally:
            self.app.tg.end(token)

    async def test_old_cached_page_rechecks_role_permissions(self):
        self.app.diary.touch_user({"id": 101, "first_name": "Редактор"}, started=True)
        self.app.diary.add_members(self.cid, 100, [{"user_id": 101}])
        rid = self.app.diary.save_role(self.cid, 100, "Редактор", ["homework"])
        self.app.diary.toggle_role(self.cid, 100, 101, rid)
        token = await self.app.tg.begin(101)
        try:
            await self.app.tg.present(101, ("x" * 600 + "\n") * 8)
            nonce = self.app.tg.pages[101]["nonce"]
            self.app.diary.toggle_role(self.cid, 100, 101, rid)
            with self.assertRaisesRegex(ValueError, "Доступ к классу изменился"):
                await self.app.tg.show_page(101, nonce, 1)
        finally:
            self.app.tg.end(token)

    async def test_help_topics_are_short_and_return_to_active_class(self):
        await self.click("help")
        self.assertLess(len(self.screen()["text"]), 1000)
        self.assertIn("help:homework", str(self.screen()["reply_markup"]))
        await self.click("help:group")
        self.assertIn("не оценка дружбы", self.screen()["text"])
        await self.click("myhome")
        self.assertIn("Ближайший учебный день", self.screen()["text"])

    async def test_malformed_buttons_do_not_show_python_errors(self):
        for data in ("v", "v:1", "task:abc:1", "task:привет:1", "cal:1", "uipage:missing"):
            with self.subTest(data=data):
                self.transport.calls.clear()
                await self.click(data)
                replies = [p for m, p in self.transport.calls if m == "answerCallbackQuery"]
                self.assertTrue(replies)
                self.assertTrue(replies[-1]["show_alert"])
                self.assertNotIn("invalid literal", replies[-1]["text"])
                self.assertNotIn("list index", replies[-1]["text"])

    async def test_draft_can_choose_date_after_returning_without_due_date(self):
        self.app.new_state(100, "draft", self.cid, draft=dict(owner_id=100, due_on="", assigned_on="2026-10-18"))
        nonce = self.db.state(100)[1]["nonce"]
        await self.click(f"draft:{self.cid}:{nonce}:date")
        self.assertEqual(self.db.state(100)[0], "date_picker")

    async def test_new_content_replaces_old_nothing_assigned_status(self):
        draft = dict(owner_id=100, body="Ничего не задано", no_homework=True, attachments=[], subject="Алгебра",
                     audience_id=0, assigned_on="2026-10-18", due_on="2026-10-19")
        data = self.app.new_state(100, "draft", self.cid, draft=draft)
        await self.app.state_message(100, {"text": "Решить № 12"}, "draft", data)
        result = self.db.state(100)[1]["draft"]
        self.assertEqual(result["body"], "Решить № 12")
        self.assertFalse(result["no_homework"])

    async def test_private_failure_does_not_include_log_notes(self):
        _, message = self.app.private_error(TelegramError(400, "Bad Request"))
        self.assertNotIn("журнал", message)
        self.assertNotIn("400", message)

    async def test_repeated_pair_click_does_not_notify_classmate_again(self):
        self.app.diary.touch_user({"id": 101, "first_name": "Ученик"}, started=True)
        self.app.diary.add_members(self.cid, 100, [{"user_id": 101}])
        await self.click(f"pairrequest:{self.cid}:101")
        await self.click(f"pairrequest:{self.cid}:101")
        invitations = [p for m, p in self.transport.calls if m == "sendMessage" and p["chat_id"] == 101]
        self.assertEqual(len(invitations), 1)

    def test_completion_summary_excludes_nothing_assigned(self):
        rows = [dict(id=1, subject="Алгебра", audience_id=0, due_on="2026-10-19", due_position=1,
                     body="№ 12", attachments="[]", done=1, no_homework=0),
                dict(id=2, subject="История", audience_id=0, due_on="2026-10-19", due_position=2,
                     body="Ничего не задано", attachments="[]", done=0, no_homework=1)]
        text = homework_text({"name": "9Б"}, date(2026, 10, 19), rows, [], False)
        self.assertIn("Выполнено: 1 из 1", text)
        self.assertIn("Можно выдохнуть", text)

    def test_emoji_heavy_homework_has_bounded_utf16_pages(self):
        pages = list(chunks(text_lines("🦊" * 6000)))
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page.encode("utf-16-le")) // 2 <= 3800 for page in pages))


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def test_backup_works_without_bot_token_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "diary.sqlite3", Path(directory) / "backup.sqlite3"
            db = Database(source)
            db.run("INSERT INTO meta VALUES('test_value','kept')")
            db.close()
            env = {**os.environ, "BOT_TOKEN": "", "DATABASE_PATH": str(source), "PYTHONIOENCODING": "utf-8"}
            root = Path(__file__).resolve().parent.parent
            result = subprocess.run([sys.executable, "run.py", "--backup", str(target)], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with closing(sqlite3.connect(target)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='test_value'").fetchone(), ("kept",))

    def test_malformed_telegram_responses_do_not_leak_raw_errors(self):
        client = Telegram("123456:FAKE_TOKEN_FOR_TESTS_ONLY")
        for data in (b"not json", b"[]", b'{"ok":true}', b'{"ok":"yes"}', b'null'):
            with self.subTest(data=data), patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(TelegramError) as caught:
                    client._request("getMe", {})
                self.assertEqual(caught.exception.code, 0)

    async def test_slow_user_does_not_block_another_and_same_user_order_is_kept(self):
        slow_started, other_done, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        class App:
            async def on_update(self, update):
                number = update["update_id"]
                if number == 1:
                    slow_started.set()
                    await release.wait()
                if number == 2:
                    other_done.set()
                order.append(number)

        updates = [{"update_id": number, "message": {"from": {"id": uid}}} for number, uid in ((1, 100), (2, 200), (3, 100))]
        task = asyncio.create_task(process_updates(App(), updates, logging.getLogger("test")))
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            await asyncio.wait_for(other_done.wait(), 2)
            self.assertEqual(order, [2])
        finally:
            release.set()
            await task
        self.assertEqual(order, [2, 1, 3])

    async def test_one_handler_failure_does_not_skip_following_updates(self):
        received = []

        class App:
            async def on_update(self, update):
                received.append(update["update_id"])
                if update["update_id"] == 1:
                    raise RuntimeError("test")

        updates = [{"update_id": n, "message": {"from": {"id": 100}}} for n in (1, 2)]
        with self.assertLogs("polish-test", level="ERROR"):
            await process_updates(App(), updates, logging.getLogger("polish-test"))
        self.assertEqual(received, [1, 2])
