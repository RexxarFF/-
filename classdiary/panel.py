"""A private, per-user navigation panel; notifications use the original transport."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import secrets
import time

from .telegram import TelegramError
from .ui import button, chunks


@dataclass
class PanelContext:
    user_id: int
    adopt_message_id: int | None = None
    fresh_message: bool = False


class PanelTelegram:
    def __init__(self, transport, db):
        self.transport, self.db = transport, db
        self.context = ContextVar("private_panel", default=None)
        self.pages = {}

    async def call(self, method, **payload):
        result = await self.transport.call(method, **payload)
        context = self.context.get()
        if context and method in ("sendPhoto", "sendDocument", "sendVideo", "sendAudio", "sendVoice", "sendAnimation", "sendVideoNote"):
            if payload.get("chat_id") == context.user_id and isinstance(result, dict) and result.get("message_id"):
                row = self.row(context.user_id)
                media = json.loads(row["media_messages"]) if row else []
                media.append(result["message_id"])
                self.remember(context.user_id, media_messages=json.dumps(media))
        return result

    async def answer_callback(self, *args, **kwargs):
        return await self.transport.answer_callback(*args, **kwargs)

    def row(self, uid):
        return self.db.one("SELECT * FROM ui_panels WHERE user_id=?", (uid,))

    def remember(self, uid, **values):
        self.db.run("INSERT OR IGNORE INTO ui_panels(user_id) VALUES(?)", (uid,))
        for key, value in values.items():
            if key not in {"message_id", "media_messages", "temporary_messages"}:
                raise ValueError("Invalid panel field")
            self.db.run(f"UPDATE ui_panels SET {key}=? WHERE user_id=?", (value, uid))

    async def begin(self, uid, adopt_message_id=None, fresh_message=False):
        token = self.context.set(PanelContext(uid, adopt_message_id, fresh_message))
        # Only the bot's temporary file previews are removed. User messages and notifications are kept.
        row = self.row(uid)
        if row:
            for mid in json.loads(row["media_messages"]):
                try:
                    await self.transport.call("deleteMessage", chat_id=uid, message_id=mid)
                except TelegramError:
                    pass
            self.remember(uid, media_messages="[]")
        return token

    def end(self, token):
        self.context.reset(token)

    def interactive_for(self, chat_id):
        context = self.context.get()
        return bool(context and chat_id == context.user_id and chat_id > 0)

    async def send(self, chat_id, text, keyboard=None, **extra):
        if not self.interactive_for(chat_id):
            return await self.transport.send(chat_id, text, keyboard, **extra)
        if keyboard and ("keyboard" in keyboard or "remove_keyboard" in keyboard):
            result = await self.transport.send(chat_id, text, keyboard, **extra)
            if isinstance(result, dict) and result.get("message_id"):
                row = self.row(chat_id)
                ids = json.loads(row["temporary_messages"]) if row else []
                ids.append(result["message_id"])
                self.remember(chat_id, temporary_messages=json.dumps(ids))
            return result
        return await self.present(chat_id, text, keyboard, **extra)

    async def send_long(self, chat_id, text, keyboard=None):
        if self.interactive_for(chat_id):
            return await self.present(chat_id, text, keyboard)
        parts = list(chunks(text)) or ["Нет данных."]
        result = None
        for index, part in enumerate(parts):
            result = await self.transport.send(chat_id, part, keyboard if index == len(parts)-1 else None)
        return result

    async def present(self, uid, text, keyboard=None, **extra):
        parts = list(chunks(text)) or ["Нет данных."]
        if len(parts) > 1:
            nonce = secrets.token_hex(4)
            memberships = {row["class_id"] for row in self.db.all("SELECT class_id FROM members WHERE user_id=? AND active=1", (uid,))}
            self.pages[uid] = dict(nonce=nonce, pages=parts, keyboard=keyboard, created=time.monotonic(), memberships=memberships)
            return await self.show_page(uid, nonce, 0)
        self.pages.pop(uid, None)
        return await self.display(uid, parts[0], keyboard, **extra)

    async def show_page(self, uid, nonce, index):
        cache = self.pages.get(uid)
        if not cache or cache["nonce"] != nonce or time.monotonic()-cache["created"] > 3600:
            raise ValueError("Этот список устарел. Откройте раздел заново через /menu.")
        if not 0 <= index < len(cache["pages"]):
            raise ValueError("Страница не найдена.")
        memberships = {row["class_id"] for row in self.db.all("SELECT class_id FROM members WHERE user_id=? AND active=1", (uid,))}
        if not cache["memberships"].issubset(memberships):
            self.pages.pop(uid, None)
            raise ValueError("Доступ к классу изменился. Откройте /menu.")
        nav = []
        if index:
            nav.append(button("◀️", f"uipage:{nonce}:{index-1}"))
        nav.append(button(f"{index+1}/{len(cache['pages'])}", "noop"))
        if index+1 < len(cache["pages"]):
            nav.append(button("▶️", f"uipage:{nonce}:{index+1}"))
        rows = (cache["keyboard"] or {}).get("inline_keyboard", [])
        markup = {"inline_keyboard": [nav] + rows}
        return await self.display(uid, cache["pages"][index], markup)

    async def display(self, uid, text, keyboard=None, **extra):
        row = self.row(uid)
        context = self.context.get()
        mid = row["message_id"] if row and row["message_id"] else (context.adopt_message_id if context else None)
        fresh = bool(context and context.fresh_message)
        markup = keyboard if keyboard is not None else {"inline_keyboard": []}
        if mid and not fresh:
            try:
                result = await self.transport.call("editMessageText", chat_id=uid, message_id=mid, text=text,
                                                   parse_mode="HTML", reply_markup=markup,
                                                   link_preview_options={"is_disabled": True})
                self.remember(uid, message_id=mid)
                return result
            except TelegramError as exc:
                description = exc.description.casefold()
                if exc.code == 400 and "message is not modified" in description:
                    self.remember(uid, message_id=mid)
                    return {"message_id": mid}
                unavailable = any(x in description for x in ("message to edit not found", "message can't be edited", "message can not be edited",
                                                              "there is no text in the message", "message_id_invalid"))
                if exc.code != 400 or not unavailable:
                    raise
        result = await self.transport.send(uid, text, keyboard, **extra)
        if isinstance(result, dict) and result.get("message_id"):
            self.remember(uid, message_id=result["message_id"])
            if context:
                context.fresh_message = False
            # Replace the old navigation panel only after the new reply was accepted by Telegram.
            if fresh and mid and mid != result["message_id"]:
                try:
                    await self.transport.call("deleteMessage", chat_id=uid, message_id=mid)
                except TelegramError:
                    pass
        return result

    async def close_keyboard(self, uid):
        row = self.row(uid)
        if not row or not json.loads(row["temporary_messages"]):
            return
        ids = json.loads(row["temporary_messages"])
        result = await self.transport.send(uid, "Готово", {"remove_keyboard": True})
        if isinstance(result, dict) and result.get("message_id"):
            ids.append(result["message_id"])
        for mid in ids:
            try:
                await self.transport.call("deleteMessage", chat_id=uid, message_id=mid)
            except TelegramError:
                pass
        self.remember(uid, temporary_messages="[]")

    def cleanup(self):
        now = time.monotonic()
        self.pages = {uid: data for uid, data in self.pages.items() if now-data["created"] <= 3600}
