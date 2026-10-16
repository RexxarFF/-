from __future__ import annotations

import io
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
