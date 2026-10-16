from __future__ import annotations

import asyncio
import json
import ssl
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
        self._last_send = 0.0

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
            async with self._send_lock:
                loop = asyncio.get_running_loop()
                delay = 0.055 - (loop.time() - self._last_send)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_send = loop.time()
        return await asyncio.to_thread(self._request, method, payload)

    async def send(self, chat_id, text, keyboard=None, **extra):
        payload = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                       link_preview_options={"is_disabled": True}, **extra)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return await self.call("sendMessage", **payload)

    async def answer_callback(self, callback_id, text="", alert=False):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200], show_alert=alert)
