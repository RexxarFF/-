import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run
from classdiary.config import Config


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
