from __future__ import annotations

import io
import asyncio
import json
import unittest
import urllib.error
from unittest.mock import patch

from classdiary.telegram import Telegram, TelegramError


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = Telegram("123456:FAKE_TOKEN_FOR_TESTS_ONLY")

    def test_post_request_serializes_unicode_json_and_keyboard(self):
        response = io.BytesIO(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())
        with patch("urllib.request.urlopen", return_value=response) as opened:
            result = self.client._request("sendMessage", {"chat_id": 100, "text": "Привет"})
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"chat_id": 100, "text": "Привет"})
        self.assertEqual(result, {"message_id": 1})

    def test_network_failure_does_not_expose_token(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("https://api.telegram.org/botSECRET/getMe")):
            with self.assertRaises(TelegramError) as caught:
                self.client._request("getMe", {})
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(caught.exception.code, 0)

    def test_rate_limit_parameters_are_parsed(self):
        body = json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 30}}).encode()
        error = urllib.error.HTTPError("https://api.telegram.org", 429, "Rate", {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                self.client._request("sendMessage", {})
        self.assertEqual(caught.exception.retry_after, 30)


class SendingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Telegram("123456:FAKE_TOKEN_FOR_TESTS_ONLY")
        self.now = 1000.0
        self.sleeps = []
        self.client._clock = lambda: self.now

        async def sleep(delay):
            self.sleeps.append(delay)
            self.now += delay
            await asyncio.sleep(0)

        self.client._sleep = sleep

    async def test_idle_chat_locks_are_released_after_sending(self):
        with patch.object(self.client, "_request", return_value={"message_id": 1}):
            for chat_id in range(100, 110):
                await self.client.send(chat_id, "Текст")
        self.assertEqual(len(self.client._chat_locks), 0)

    async def test_expired_chat_rate_timestamps_are_pruned(self):
        self.client._chat_last[99] = self.now - 400
        self.client._last_cleanup = self.now - 400
        with patch.object(self.client, "_request", return_value={"message_id": 1}):
            await self.client.send(101, "Текст")
        self.assertNotIn(99, self.client._chat_last)
        self.assertIn(101, self.client._chat_last)

    async def test_consecutive_outgoing_messages_respect_private_and_group_intervals(self):
        sent = []

        def request(method, payload):
            sent.append((payload["chat_id"], self.now))
            return {"message_id": len(sent)}

        with patch.object(self.client, "_request", side_effect=request):
            await self.client.send(101, "Первое")
            await self.client.send(101, "Второе")
            await self.client.send(-100, "Первое в группе")
            await self.client.send(-100, "Второе в группе")
        self.assertGreaterEqual(sent[1][1]-sent[0][1], 1.05-1e-8)
        self.assertGreaterEqual(sent[3][1]-sent[2][1], 3.05-1e-8)

    async def test_short_telegram_rate_limit_is_retried_once(self):
        with patch.object(self.client, "_request", side_effect=[TelegramError(429, "Rate", 2), {"message_id": 10}]) as request:
            result = await self.client.send(101, "Текст")
        self.assertEqual(result["message_id"], 10)
        self.assertEqual(request.call_count, 2)
        self.assertGreaterEqual(sum(self.sleeps), 2)

    async def test_long_rate_limit_is_reported_without_blocking_polling(self):
        with patch.object(self.client, "_request", side_effect=TelegramError(429, "Rate", 30)) as request:
            with self.assertRaises(TelegramError):
                await self.client.send(101, "Текст")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.sleeps, [])

    async def test_network_error_is_not_retried_to_avoid_duplicate_messages(self):
        with patch.object(self.client, "_request", side_effect=TelegramError(0, "Network")) as request:
            with self.assertRaises(TelegramError):
                await self.client.send(101, "Текст")
        self.assertEqual(request.call_count, 1)

    async def test_repeated_short_rate_limit_stops_after_second_attempt(self):
        with patch.object(self.client, "_request", side_effect=TelegramError(429, "Rate", 1)) as request:
            with self.assertRaises(TelegramError):
                await self.client.send(101, "Текст")
        self.assertEqual(request.call_count, 2)

    async def test_waiting_for_one_chat_does_not_hold_other_chats_global_lock(self):
        waiting, release = asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            if delay > 0.5:
                waiting.set()
                await release.wait()
            self.now += delay
            await asyncio.sleep(0)

        self.client._sleep = sleep
        with patch.object(self.client, "_request", return_value={"message_id": 1}):
            await self.client.send(101, "Первое")
            delayed = asyncio.create_task(self.client.send(101, "Второе"))
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                result = await asyncio.wait_for(self.client.send(202, "Другой пользователь"), 2)
                self.assertEqual(result["message_id"], 1)
                self.assertFalse(delayed.done())
            finally:
                release.set()
                await delayed
