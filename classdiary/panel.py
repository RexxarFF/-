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
        self.page_history = {}

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

    def access_stamp(self, uid):
        members = self.db.all("SELECT m.class_id,c.owner_id,c.interactive FROM members m JOIN classes c ON c.id=m.class_id "
                              "WHERE m.user_id=? AND m.active=1 ORDER BY m.class_id", (uid,))
        roles = self.db.all("SELECT mr.class_id,r.id,r.permissions FROM member_roles mr JOIN roles r ON r.id=mr.role_id "
                            "AND r.class_id=mr.class_id WHERE mr.user_id=? ORDER BY mr.class_id,r.id", (uid,))
        audiences = self.db.all("SELECT class_id,audience_id FROM member_audiences WHERE user_id=? ORDER BY class_id,audience_id", (uid,))
        return members, roles, audiences

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
        self.cleanup()
        parts = list(chunks(text)) or ["Нет данных."]
        rows = (keyboard or {}).get("inline_keyboard", [])
        footer = rows[-1:] if sum(map(len, rows)) > 88 else []
        if footer:
            rows = rows[:-1]
        limit = 88 - sum(map(len, footer))
        keyboard_pages, page, count = [], [], 0
        for row in rows:
            if page and count + len(row) > limit:
                keyboard_pages.append(page + footer)
                page, count = [], 0
            page.append(row)
            count += len(row)
        keyboard_pages.append(page + footer)
        total = max(len(parts), len(keyboard_pages))
        if total > 1:
            nonce = secrets.token_hex(4)
            memberships = {row["class_id"] for row in self.db.all("SELECT class_id FROM members WHERE user_id=? AND active=1", (uid,))}
            # Keep every control reachable, even when the list has over 100 tasks.
            self.pages[uid] = dict(nonce=nonce, pages=parts, keyboard=keyboard, total=total,
                                   keyboards=keyboard_pages, created=time.monotonic(), memberships=memberships,
                                   access=self.access_stamp(uid))
            self.page_history[uid, nonce] = self.pages[uid]
            own = [key for key in self.page_history if key[0] == uid]
            for key in own[:-12]:
                del self.page_history[key]
            while len(self.page_history) > 500:
                del self.page_history[next(iter(self.page_history))]
            return await self.show_page(uid, nonce, 0)
        self.pages.pop(uid, None)
        return await self.display(uid, parts[0], keyboard, **extra)

    async def show_page(self, uid, nonce, index):
        cache = self.page_history.get((uid, nonce))
        if not cache or cache["nonce"] != nonce or time.monotonic()-cache["created"] > 3600:
            raise ValueError("Этот список устарел. Открой раздел заново через /menu.")
        if not 0 <= index < cache["total"]:
            raise ValueError("Страница не найдена.")
        memberships = {row["class_id"] for row in self.db.all("SELECT class_id FROM members WHERE user_id=? AND active=1", (uid,))}
        if not cache["memberships"].issubset(memberships) or cache["access"] != self.access_stamp(uid):
            self.pages.pop(uid, None)
            self.page_history.pop((uid, nonce), None)
            raise ValueError("Доступ к классу изменился. Открой /menu.")
        nav = []
        if index:
            nav.append(button("◀️", f"uipage:{nonce}:{index-1}"))
        nav.append(button(f"{index+1}/{cache['total']}", "noop"))
        if index+1 < cache["total"]:
            nav.append(button("▶️", f"uipage:{nonce}:{index+1}"))
        rows = cache["keyboards"][min(index, len(cache["keyboards"])-1)]
        markup = {"inline_keyboard": [nav] + rows}
        return await self.display(uid, cache["pages"][min(index, len(cache["pages"])-1)], markup)

    async def display(self, uid, text, keyboard=None, **extra):
        row = self.row(uid)
        context = self.context.get()
        mid = (context.adopt_message_id if context else None) or (row["message_id"] if row else None)
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
                context.adopt_message_id = result["message_id"]
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
        self.page_history = {key: data for key, data in self.page_history.items() if now-data["created"] <= 3600}
