from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        self.code = int(code)
        self.description = str(description)
        self.retry_after = int(retry_after)
        super().__init__(f"Telegram {self.code}: {self.description}")


class Telegram:
    """Small Bot API client with no external dependencies or token-bearing logs."""

    def __init__(self, token):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._context = ssl.create_default_context()
        self._send_lock = asyncio.Lock()
        self._last_send = float("-inf")
        self._chat_locks = {}
        self._chat_last = {}
        self._clock = time.monotonic
        self._sleep = asyncio.sleep

    def _request(self, method, payload):
        request = urllib.request.Request(self._base + method,
                                         data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45, context=self._context) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise TelegramError(exc.code, "Ошибка ответа Telegram") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # urllib exceptions may contain the URL, including the bot token.
            raise TelegramError(0, "Нет соединения с Telegram. Проверьте доступ к api.telegram.org.") from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0), result.get("description", "Ошибка Telegram"),
                                result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    async def call(self, method, **payload):
        if method.startswith(("send", "edit", "copy")):
            chat_id = payload.get("chat_id")
            if chat_id is not None:
                lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
                async with lock:
                    return await self._send_request(method, payload, chat_id)
            return await self._send_request(method, payload)
        return await asyncio.to_thread(self._request, method, payload)

    async def _send_request(self, method, payload, chat_id=None):
        for attempt in range(2):
            if chat_id is not None:
                interval = 3.05 if isinstance(chat_id, int) and chat_id < 0 else 1.05
                delay = self._chat_last.get(chat_id, float("-inf"))+interval-self._clock()
                if delay > 0:
                    await self._sleep(delay)
            # Waiting for this chat does not hold the global lock or block another chat.
            async with self._send_lock:
                delay = self._last_send+0.055-self._clock()
                if delay > 0:
                    await self._sleep(delay)
                self._last_send = self._clock()
                if chat_id is not None:
                    self._chat_last[chat_id] = self._last_send
            try:
                return await asyncio.to_thread(self._request, method, payload)
            except TelegramError as exc:
                # Retry only an explicit short rate limit. Network failures can mean a message
                # was delivered without confirmation, so retrying them could create duplicates.
                if exc.code != 429 or exc.retry_after > 5 or attempt:
                    raise
                await self._sleep(max(1, exc.retry_after))

    async def send(self, chat_id, text, keyboard=None, **extra):
        payload = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                       link_preview_options={"is_disabled": True}, **extra)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return await self.call("sendMessage", **payload)

    async def answer_callback(self, callback_id, text="", alert=False):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200], show_alert=alert)
