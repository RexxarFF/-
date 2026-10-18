"""Group-only screens for statistics and pair streaks, with actor-scoped controls."""
from __future__ import annotations

from .config import today_in
from .service import AccessDenied
from .telegram import TelegramError
from .ui import button as b, e, keyboard as kb


PUBLIC_VIEWS = {"stats", "pairs"}


class GroupInteractive:
    def __init__(self, app):
        self.app, self.db, self.diary = app, app.db, app.diary

    @staticmethod
    def data(cid, uid, action, *values):
        return ":".join(map(str, ("g", cid, uid, action, *values)))

    def require(self, cid, uid, message):
        classroom = self.app.interactive_required(cid, uid)
        if message.get("chat", {}).get("type") not in ("group", "supergroup") or classroom["group_id"] != message["chat"]["id"]:
            raise AccessDenied("Это меню предназначено другой группе.")
        return classroom

    def name(self, uid):
        return e((self.db.scalar("SELECT name FROM users WHERE id=?", (uid,)) or str(uid))[:50])

    async def screen(self, message, text, keyboard, edit=False, cid=None, uid=None):
        if edit and message.get("message_id"):
            self.app.require_group_owner(message, uid, cid, uid)
            try:
                return await self.app.tg.call("editMessageText", chat_id=message["chat"]["id"],
                                              message_id=message["message_id"], text=text, parse_mode="HTML",
                                              reply_markup=keyboard, link_preview_options={"is_disabled": True})
            except TelegramError as exc:
                description = exc.description.casefold()
                if exc.code == 400 and "message is not modified" in description:
                    return
                if exc.code != 400 or not any(x in description for x in ("message to edit not found", "message can't be edited", "message can not be edited")):
                    raise
        return await self.app.send_owned_group(message, text, keyboard, cid, uid)

    async def show(self, uid, cid, kind, message, edit=False, page=0):
        classroom = self.require(cid, uid, message)
        if kind == "stats":
            return await self.stats(uid, cid, classroom, message, edit)
        if kind == "pairs":
            return await self.pairs(uid, cid, classroom, message, edit, page)
        if kind == "choose":
            return await self.choose(uid, cid, message, edit, page)
        raise ValueError("Неизвестный раздел интерактива.")

    async def stats(self, uid, cid, classroom, message, edit):
        today = today_in(classroom["timezone"])
        stats, top = self.diary.stats(cid, uid, today), self.diary.top(cid, uid, today)
        text = f"📊 <b>{e(classroom['name'])} · статистика</b>\nУчастник: <b>{self.name(uid)}</b>\n\n"
        text += f"Сегодня: {stats['today']}\nЗа 7 дней: {stats['week']}\nВсего: {stats['total']}\n🔥 Серия общения: {stats['streak']} дн.\n\n"
        text += "<b>Активность класса за 7 дней</b>\n"
        text += "\n".join(f"{i}. {e(row['name'][:50])} — {row['n']}" for i, row in enumerate(top, 1)) or "Пока нет сообщений."
        text += "\n\nСчитаются сообщения участников класса с момента включения учёта."
        return await self.screen(message, text, kb([b("🔥 Мои огоньки", self.data(cid, uid, "p", 0))],
                                                  [b("🔄 Обновить", self.data(cid, uid, "s"))],
                                                  [b("В меню", self.data(cid, uid, "m"))]), edit, cid, uid)

    async def pairs(self, uid, cid, classroom, message, edit, page):
        pairs = sorted(self.diary.pairs(cid, uid, today_in(classroom["timezone"])), key=lambda row: row["id"])
        page = max(0, min(page, max(0, (len(pairs)-1)//8)))
        lines = [f"🔥 <b>Парные огоньки · {self.name(uid)}</b>",
                 "Серия растёт за дни, когда оба писали в группе. Второй участник должен принять приглашение.", ""]
        rows = []
        for pair in pairs[page*8:page*8+8]:
            name = e(pair["name"][:60])
            if pair["accepted"]:
                lines.append(f"{name} — 🔥 {pair['streak']} дн.")
            elif pair["requester"] == uid:
                lines.append(f"{name} — ожидаем согласия")
            else:
                lines.append(f"{name} — приглашает тебя")
                rows.append([b("Принять", self.data(cid, uid, "a", pair["id"], 1)),
                             b("Отклонить", self.data(cid, uid, "a", pair["id"], 0))])
            rows.append([b(f"Убрать: {pair['name']}", self.data(cid, uid, "x", pair["id"]))])
        if not pairs:
            lines.append("Огоньков пока нет. Пригласи участника класса.")
        navigation = []
        if page:
            navigation.append(b("◀️", self.data(cid, uid, "p", page-1)))
        if (page+1)*8 < len(pairs):
            navigation.append(b("▶️", self.data(cid, uid, "p", page+1)))
        if navigation:
            rows.append(navigation)
        rows.extend([[b("➕ Пригласить участника", self.data(cid, uid, "c", 0))],
                     [b("📊 Моя статистика", self.data(cid, uid, "s"))], [b("В меню", self.data(cid, uid, "m"))]])
        return await self.screen(message, "\n".join(lines), {"inline_keyboard": rows}, edit, cid, uid)

    async def choose(self, uid, cid, message, edit, page):
        members = [row for row in self.diary.members(cid, uid) if row["active"] and row["id"] != uid]
        page = max(0, min(page, max(0, (len(members)-1)//20)))
        rows = [[b(row["name"], self.data(cid, uid, "r", row["id"]))] for row in members[page*20:page*20+20]]
        navigation = []
        if page:
            navigation.append(b("◀️", self.data(cid, uid, "c", page-1)))
        if (page+1)*20 < len(members):
            navigation.append(b("▶️", self.data(cid, uid, "c", page+1)))
        if navigation:
            rows.append(navigation)
        rows.append([b("Назад", self.data(cid, uid, "p", 0))])
        return await self.screen(message, f"🔥 <b>{self.name(uid)}</b>\nВыбери участника для парного огонька.", {"inline_keyboard": rows}, edit, cid, uid)

    async def callback(self, query, parts):
        if len(parts) < 4:
            raise ValueError("Кнопка интерактива устарела.")
        cid, owner, action = int(parts[1]), int(parts[2]), parts[3]
        lengths = {"s": 4, "p": 5, "c": 5, "r": 5, "a": 6, "x": 5, "m": 4}
        if action not in lengths or len(parts) != lengths[action] or (action == "a" and parts[5] not in ("0", "1")):
            raise ValueError("Кнопка интерактива устарела. Открой /pairs заново.")
        uid, message = query["from"]["id"], query.get("message", {})
        self.require(cid, uid, message)
        if owner != uid:
            raise AccessDenied("Это кнопки другого участника. Открой /stats или /pairs для себя.")
        self.app.require_group_owner(message, uid, cid, owner)
        if action == "m":
            await self.app.group_menu(uid, cid, message, edit=True)
        elif action == "s":
            await self.show(uid, cid, "stats", message, edit=True)
        elif action in ("p", "c"):
            await self.show(uid, cid, "pairs" if action == "p" else "choose", message, edit=True,
                            page=int(parts[4]) if len(parts) > 4 else 0)
        elif action == "r":
            target = int(parts[4])
            a, other = sorted((uid, target))
            existing = self.db.one("SELECT id FROM pairs WHERE class_id=? AND user_a=? AND user_b=?", (cid, a, other))
            pid = self.diary.request_pair(cid, uid, target)
            if not existing:
                invitation = f'🔥 <a href="tg://user?id={uid}">{self.name(uid)}</a> предлагает '
                invitation += f'<a href="tg://user?id={target}">{self.name(target)}</a> вести парный огонёк.\n'
                invitation += "Серия растёт за дни, когда оба общаются в группе."
                await self.app.send_owned_group(message, invitation, kb([b("Принять", self.data(cid, target, "a", pid, 1)),
                                                                        b("Отклонить", self.data(cid, target, "a", pid, 0))]), cid, target)
            await self.show(uid, cid, "pairs", message, edit=True)
        elif action == "a":
            pid, accept = int(parts[4]), parts[5] == "1"
            pair = self.db.one("SELECT * FROM pairs WHERE class_id=? AND id=?", (cid, pid))
            if not pair or pair["accepted"]:
                raise ValueError("Приглашение уже обработано. Открой свои огоньки заново.")
            self.diary.answer_pair(cid, uid, pid, accept)
            await self.show(uid, cid, "pairs", message, edit=True)
        elif action == "x":
            pid = int(parts[4])
            removed = self.db.run("DELETE FROM pairs WHERE class_id=? AND id=? AND (user_a=? OR user_b=?)", (cid, pid, uid, uid))
            if not removed.rowcount:
                raise AccessDenied("Этот огонёк не найден или принадлежит другим участникам.")
            await self.show(uid, cid, "pairs", message, edit=True)
        else:
            raise ValueError("Неизвестная кнопка интерактива.")
        await self.app.tg.answer_callback(query["id"])
