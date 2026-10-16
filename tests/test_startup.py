import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run
from classdiary.config import Config
from classdiary.db import Database
from classdiary.service import Diary
from classdiary.telegram import Telegram
from datetime import date


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_polling_startup_receives_start_and_sends_menu(self):
        """Exercise the actual runner, queue and message handler together, with Telegram simulated."""
        ready = asyncio.Event()
        calls = []

        class SimulatedTelegram:
            def __init__(self, token):
                self.polled = False

            async def call(self, method, **payload):
                calls.append((method, payload))
                if method == "getMe":
                    return {"id": 999, "username": "klassno_test_bot"}
                if method == "getUpdates":
                    if not self.polled:
                        self.polled = True
                        return [{"update_id": 10, "message": {
                            "message_id": 1, "date": 1792150000,
                            "from": {"id": 101, "first_name": "Тест"},
                            "chat": {"id": 101, "type": "private"}, "text": "/start"}}]
                    await asyncio.Event().wait()
                return True

            async def send(self, chat_id, text, keyboard=None, **extra):
                calls.append(("sendMessage", {"chat_id": chat_id, "text": text, "keyboard": keyboard}))
                if keyboard and "inline_keyboard" in keyboard:
                    ready.set()
                return {"message_id": len(calls)}

        with tempfile.TemporaryDirectory() as directory:
            config = Config("simulated-token", Path(directory) / "diary.sqlite3")
            with patch("run.Telegram", SimulatedTelegram):
                task = asyncio.create_task(run.serve(config))
                try:
                    await asyncio.wait_for(ready.wait(), timeout=5)
                    self.assertFalse(task.done())
                    menus = [data for method, data in calls if method == "sendMessage" and data["keyboard"] and "inline_keyboard" in data["keyboard"]]
                    self.assertEqual(menus[0]["chat_id"], 101)
                    self.assertIn("Создайте класс", menus[0]["text"])
                    self.assertTrue(any(method == "getUpdates" for method, _ in calls))
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def test_polling_group_commands_and_buttons_send_real_private_api_messages(self):
        """Run polling, permissions, rendering, panel state and the real transport together."""
        ready = asyncio.Event()
        wire = []
        updates = []

        class RecordingTelegram(Telegram):
            def __init__(self, token):
                super().__init__(token)
                self.polled = False
                self.ids = {}
                self.now = 1000.0
                self._clock = lambda: self.now

                async def sleep(delay):
                    self.now += delay
                    await asyncio.sleep(0)

                self._sleep = sleep

            async def call(self, method, **payload):
                if method == "getUpdates":
                    if not self.polled:
                        self.polled = True
                        return updates
                    ready.set()
                    await asyncio.Event().wait()
                return await super().call(method, **payload)

            def _request(self, method, payload):
                wire.append((method, payload))
                if method == "getMe":
                    return {"id": 999, "username": "klassno_test_bot"}
                if method == "sendMessage":
                    chat_id = payload["chat_id"]
                    self.ids[chat_id] = self.ids.get(chat_id, 0)+1
                    return {"message_id": self.ids[chat_id], "chat": {"id": chat_id}}
                if method == "editMessageText":
                    return {"message_id": payload["message_id"]}
                return True

        with tempfile.TemporaryDirectory() as directory:
            config = Config("simulated-token", Path(directory)/"diary.sqlite3")
            db = Database(config.database)
            diary = Diary(db)
            for uid in (100, 101):
                diary.touch_user({"id": uid, "first_name": f"Ученик {uid}"}, started=True)
            cid = diary.create_class(100, "9Б", "Europe/Moscow")
            diary.add_members(cid, 100, [{"user_id": 101}])
            diary.bind_group(cid, 100, -100, "9Б")
            diary.replace_weekday(cid, 100, 3, [{"position": 1, "subject": "Алгебра"}])
            diary.save_assignment(cid, 100, dict(owner_id=0, source_key="lesson:test", source_position=1,
                assigned_on="2026-10-14", subject="Алгебра", audience_id=0, due_on="2026-10-15", due_position=1,
                body="Решить № 777", attachments=[], no_homework=False, revision=0))
            db.close()
            user = {"id": 101, "first_name": "Ученик"}
            for index, text, group in ((20, "/start", False), (21, "/menu@klassno_test_bot", True), (22, "/hw@klassno_test_bot", True)):
                updates.append({"update_id": index, "message": {"message_id": index, "date": 1791975600,
                    "from": user, "chat": {"id": -100 if group else 101, "type": "supergroup" if group else "private"}, "text": text}})
            for index, text in ((23, "/stats@klassno_test_bot"), (24, "/pairs@klassno_test_bot")):
                updates.append({"update_id": index, "message": {"message_id": index, "date": 1791975600,
                    "from": user, "chat": {"id": -100, "type": "supergroup"}, "text": text}})
            updates.extend([
                {"update_id": 25, "callback_query": {"id": "group-button", "from": user,
                    "data": f"v:{cid}:hw:tomorrow", "message": {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}}}},
                {"update_id": 26, "callback_query": {"id": "private-back", "from": user,
                    "data": f"home:{cid}", "message": {"message_id": 3, "chat": {"id": 101, "type": "private"}}}}
            ])
            with patch("run.Telegram", RecordingTelegram), patch("classdiary.app.today_in", return_value=date(2026, 10, 14)), patch("classdiary.group_interactive.today_in", return_value=date(2026, 10, 14)):
                task = asyncio.create_task(run.serve(config))
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    private = [p for m, p in wire if m == "sendMessage" and p["chat_id"] == 101]
                    self.assertEqual(len(private), 3, "Initial menu, group command and group button each produce a new private message")
                    self.assertTrue(all("Решить № 777" in p["text"] for p in private[1:]))
                    self.assertEqual({p["message_id"] for m, p in wire if m == "editMessageText"}, {3})
                    self.assertTrue(all("Решить № 777" not in p["text"] for m, p in wire if m == "sendMessage" and p["chat_id"] == -100))
                    group_text = "\n".join(p["text"] for m, p in wire if m == "sendMessage" and p["chat_id"] == -100)
                    self.assertIn("Активность класса за 7 дней", group_text)
                    self.assertIn("Парные огоньки", group_text)
                    self.assertTrue(all("Активность класса" not in p["text"] and "Парные огоньки" not in p["text"] for p in private))
                    self.assertTrue(any(m == "answerCallbackQuery" and "Отправлено новым" in p.get("text", "") for m, p in wire))
                    saved = Database(config.database)
                    self.assertEqual(saved.scalar("SELECT message_id FROM ui_panels WHERE user_id=101"), 3)
                    self.assertEqual(saved.scalar("SELECT value FROM meta WHERE key='telegram_offset'"), "27")
                    saved.close()
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
