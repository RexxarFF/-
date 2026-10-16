from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

from .config import ROOT, Config, get_timezone, today_in
from .db import Database
from .schedule import DAYS, SHORT_DAYS, parse_date, parse_day
from .service import AccessDenied, Conflict, Diary, PERMISSIONS
from .telegram import Telegram, TelegramError
from .ui import button as b, chunks, date_label, e, homework_text, keyboard as kb, lesson_label, text_lines, timetable_text

log = logging.getLogger(__name__)


class Application:
    def __init__(self, config: Config, db: Database, telegram: Telegram):
        self.config, self.db, self.tg = config, db, telegram
        self.diary = Diary(db)
        self.username = ""
        self.bot_id = 0

    def new_state(self, uid, stage, cid=None, **data):
        data.update(cid=cid, nonce=secrets.token_hex(3))
        self.db.set_state(uid, stage, data)
        return data

    def need_state(self, uid, cid, nonce, *stages):
        stage, data = self.db.state(uid)
        if stage not in stages or data.get("cid") != cid or data.get("nonce") != nonce:
            raise ValueError("Эта кнопка устарела. Откройте нужный раздел заново.")
        return stage, data

    async def send_long(self, uid, text, keyboard=None):
        parts = list(chunks(text)) or ["Нет данных."]
        result = None
        for index, part in enumerate(parts):
            result = await self.tg.send(uid, part, keyboard if index == len(parts)-1 else None)
        return result

    async def home(self, uid, cid=None):
        user = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        if cid is None:
            cid = user["active_class"] if user else None
        classes = self.diary.classrooms(uid)
        if not classes:
            return await self.tg.send(uid, "👋 <b>Классный дневник</b>\n\nСоздайте класс или попросите владельца добавить вас в список участников.\n"
                                      "Если вас уже добавили, нажмите /start.",
                                      kb([b("➕ Создать класс", "create")], [b("ℹ️ Помощь", "help")]))
        if not cid or cid not in {c["id"] for c in classes}:
            cid = classes[0]["id"]
            self.diary.select_class(uid, cid)
        classroom = self.diary.require(cid, uid)
        rights = self.diary.permissions(cid, uid)
        rows = [[b("📚 Общее ДЗ на завтра", f"v:{cid}:hw:tomorrow")],
                [b("📝 Мои записи", f"v:{cid}:mine:tomorrow"), b("➕ Записать себе", f"record:{cid}:mine")],
                [b("📅 Расписание на завтра", f"v:{cid}:schedule:tomorrow")],
                [b("📢 Объявления", f"news:{cid}"), b("👥 Мои подгруппы", f"audiences:{cid}")]]
        if "homework" in rights:
            rows.append([b("✏️ Записать общее ДЗ", f"record:{cid}:hw")])
        if classroom["interactive"]:
            rows.append([b("📊 Статистика", f"stats:{cid}"), b("🔥 Огоньки", f"pairs:{cid}")])
        if rights:
            rows.append([b("⚙️ Управление классом", f"manage:{cid}")])
        rows += [[b("🏫 Мои классы", "classes"), b("🔔 Напоминания", "reminders")], [b("ℹ️ Помощь", "help")]]
        await self.tg.send(uid, f"🏫 <b>{e(classroom['name'])}</b>\nВыберите действие.\n"
                          "Общая домашка и ваши личные записи открываются отдельно.", {"inline_keyboard": rows})

    async def on_update(self, update):
        try:
            if "callback_query" in update:
                await self.callback(update["callback_query"])
            elif "message" in update:
                await self.message(update["message"])
        except (ValueError, sqlite3.IntegrityError) as exc:
            callback = update.get("callback_query")
            if isinstance(exc, sqlite3.IntegrityError):
                message = "Такое название уже существует или данные изменились. Откройте раздел заново."
            else:
                message = str(exc)
            if callback:
                await self.tg.answer_callback(callback["id"], message, True)
            elif update.get("message", {}).get("chat", {}).get("type") == "private":
                await self.tg.send(update["message"]["chat"]["id"], e(message))
            elif update.get("message", {}).get("text", "").startswith("/"):
                await self.tg.send(update["message"]["chat"]["id"], e(message))
        except TelegramError as exc:
            log.warning("Telegram request failed: code=%s", exc.code)
            callback = update.get("callback_query")
            if callback:
                try:
                    await self.tg.answer_callback(callback["id"], "Не получилось отправить ответ. Откройте личку с ботом и нажмите /start.", True)
                except TelegramError:
                    pass

    async def message(self, message):
        user = message.get("from")
        if not user or user.get("is_bot"):
            return
        uid = int(user["id"])
        private = message["chat"]["type"] == "private"
        self.diary.touch_user(user, started=private)
        text = message.get("text", "").strip()
        command, _, payload = text.partition(" ")
        if "@" in command:
            command, mention = command.split("@", 1)
            if mention.casefold() != self.username.casefold():
                return
        if not private:
            return await self.group_message(message, command, payload)
        if command in ("/start", "/menu", "/cancel"):
            self.db.clear_state(uid)
            await self.tg.send(uid, "Меню открыто.", {"remove_keyboard": True})
            if command == "/start" and payload.startswith("c_"):
                classroom = self.db.one("SELECT * FROM classes WHERE invite_token=?", (payload[2:],))
                if not classroom:
                    raise ValueError("Ссылка на класс недействительна.")
                self.diary.select_class(uid, classroom["id"])
            elif command == "/start" and payload.startswith("v_"):
                parts = payload.split("_")
                if len(parts) != 4 or parts[2] not in ("hw", "mine", "schedule", "stats"):
                    raise ValueError("Неверная ссылка.")
                return await self.view(uid, int(parts[1]), parts[2], parts[3])
            return await self.home(uid)
        if command == "/help":
            return await self.help(uid)
        if "users_shared" in message:
            return await self.shared_users(uid, message["users_shared"])
        if "chat_shared" in message:
            return await self.shared_chat(uid, message["chat_shared"])
        shortcuts = {"/hw": "hw", "/tomorrow": "hw", "/schedule": "schedule", "/my": "mine", "/stats": "stats"}
        if command in shortcuts:
            userrow = self.db.one("SELECT active_class FROM users WHERE id=?", (uid,))
            if not userrow or not userrow["active_class"]:
                return await self.home(uid)
            return await self.view(uid, userrow["active_class"], shortcuts[command], "tomorrow")
        stage, data = self.db.state(uid)
        if stage:
            return await self.state_message(uid, message, stage, data)
        await self.home(uid)

    async def help(self, uid):
        await self.tg.send(uid, "<b>Как пользоваться</b>\n\n"
                           "📚 Общее ДЗ — задания, которые записали ответственные.\n"
                           "📝 Мои записи — только ваши задания и заметки.\n"
                           "✏️ При записи бот открывает предметы сегодняшнего дня и предлагает следующий день с этим предметом.\n"
                           "Каждый урок и каждая подгруппа записываются отдельно.\n"
                           "📅 Расписание — уроки на выбранную дату с учётом замен.\n"
                           "👥 В подгруппах можно выбрать свои занятия. Пока ничего не выбрано, видны все варианты.\n"
                           "📎 Откройте задание, чтобы получить фото и файлы.\n"
                           "🔥 Серия растёт за дни общения в привязанной группе.\n\n"
                           "/menu — меню; /cancel — отменить ввод; /hw — общее ДЗ; /my — личные записи; "
                           "/schedule — расписание; /stats — статистика.\n\n"
                           "Бот не отправляет домашку в группу автоматически. Важные объявления публикуются отдельно.")

    async def group_message(self, message, command, payload):
        chat_id, uid = message["chat"]["id"], message["from"]["id"]
        classroom = self.db.one("SELECT * FROM classes WHERE group_id=?", (chat_id,))
        if classroom and message.get("migrate_to_chat_id"):
            self.db.run("UPDATE classes SET group_id=? WHERE id=?", (message["migrate_to_chat_id"], classroom["id"]))
            return
        if classroom and not message.get("sender_chat") and not any(k in message for k in ("new_chat_members", "left_chat_member", "new_chat_title", "pinned_message")):
            timestamp = datetime.fromtimestamp(message["date"], timezone.utc).astimezone(get_timezone(classroom["timezone"])).date()
            self.diary.count_message(chat_id, message["message_id"], uid, timestamp)
        if command == "/start" and payload.startswith("bind_"):
            target = self.db.one("SELECT * FROM classes WHERE invite_token=?", (payload[5:],))
            if not target:
                return
            self.diary.require(target["id"], uid, "settings")
            member = await self.tg.call("getChatMember", chat_id=chat_id, user_id=uid)
            if member["status"] not in ("creator", "administrator"):
                raise AccessDenied("Привязать группу может её администратор.")
            self.diary.bind_group(target["id"], uid, chat_id, message["chat"].get("title", "Группа"))
            classroom = self.diary.require(target["id"], uid)
            await self.tg.send(chat_id, f"🏫 Группа привязана к классу <b>{e(classroom['name'])}</b>.\n"
                               "Для счётчика сообщений назначьте бота администратором или отключите Privacy Mode через @BotFather.")
        if not classroom:
            return
        shortcuts = {"/hw": "hw", "/tomorrow": "hw", "/schedule": "schedule", "/stats": "stats"}
        if command in shortcuts:
            try:
                self.diary.require(classroom["id"], uid)
                await self.view(uid, classroom["id"], shortcuts[command], "tomorrow")
            except (TelegramError, AccessDenied):
                await self.tg.send(chat_id, "Откройте бота в личке. Доступ получают участники, которых добавил владелец класса.",
                                   kb([b("Открыть бота", url=f"https://t.me/{self.username}?start=v_{classroom['id']}_{shortcuts[command]}_tomorrow")]))
            return
        if command in ("/menu", "/start"):
            await self.tg.send(chat_id, f"🏫 <b>{e(classroom['name'])}</b>\nОтвет придёт вам в личку.",
                               kb([b("📚 Общее ДЗ на завтра", f"v:{classroom['id']}:hw:tomorrow")],
                                  [b("📅 Расписание на завтра", f"v:{classroom['id']}:schedule:tomorrow")],
                                  [b("📊 Моя статистика", f"v:{classroom['id']}:stats:today")],
                                  [b("Открыть бота", url=f"https://t.me/{self.username}?start=c_{classroom['invite_token']}")]))

    async def callback(self, query):
        uid = query["from"]["id"]
        self.diary.touch_user(query["from"])
        parts = query.get("data", "").split(":")
        action = parts[0]
        message = query.get("message", {})
        private = message.get("chat", {}).get("type") == "private"
        if not private and action not in ("v", "ack"):
            raise AccessDenied("Откройте этот раздел в личке с ботом.")
        if action == "v":
            cid, kind, target = int(parts[1]), parts[2], parts[3]
            if not private:
                classroom = self.diary.require(cid, uid)
                if classroom["group_id"] != message.get("chat", {}).get("id"):
                    raise AccessDenied("Это меню предназначено другой группе.")
                try:
                    await self.view(uid, cid, kind, target)
                except TelegramError as exc:
                    if exc.code not in (400, 403):
                        raise
                    await self.tg.send(message["chat"]["id"], "Для получения ответа сначала запустите бота в личке.",
                                       kb([b("Открыть ответ", url=f"https://t.me/{self.username}?start=v_{cid}_{kind}_{target}")]))
                    return await self.tg.answer_callback(query["id"], "Нажмите «Открыть ответ» под сообщением.", True)
                return await self.tg.answer_callback(query["id"], "Отправлено вам в личку")
            await self.view(uid, cid, kind, target)
        elif action == "home":
            self.db.clear_state(uid)
            await self.home(uid, int(parts[1]))
        elif action == "help":
            await self.help(uid)
        elif action == "create":
            self.new_state(uid, "class_name")
            await self.tg.send(uid, "Введите название класса, например «9Б».\nОтменить: /cancel")
        elif action == "classes":
            rows = [[b(f"{c['name']} · #{c['id']}", f"select:{c['id']}")] for c in self.diary.classrooms(uid)]
            rows.append([b("➕ Создать класс", "create")])
            await self.tg.send(uid, "🏫 <b>Мои классы</b>", {"inline_keyboard": rows})
        elif action == "select":
            self.diary.select_class(uid, int(parts[1]))
            await self.home(uid, int(parts[1]))
        elif action == "manage":
            await self.manage(uid, int(parts[1]))
        elif action == "record":
            await self.record(uid, int(parts[1]), parts[2])
        elif action == "recorddate":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "record_pick")
            self.db.set_state(uid, "record_date", data)
            await self.tg.send(uid, "За какой день записываем? Введите дату ДД.ММ.ГГГГ. Это день, когда задание <b>задали</b>.")
        elif action == "lesson":
            cid, nonce, index = int(parts[1]), parts[2], int(parts[3])
            _, data = self.need_state(uid, cid, nonce, "record_pick")
            lessons = data["lessons"]
            if not 0 <= index < len(lessons):
                raise ValueError("Урок не найден.")
            await self.start_draft(uid, cid, data["scope"], date.fromisoformat(data["assigned_on"]), lessons[index])
        elif action == "custom":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "record_pick")
            if data["scope"] != "mine":
                raise ValueError("Общее задание привязывается к уроку расписания.")
            self.db.set_state(uid, "custom_title", data)
            await self.tg.send(uid, "Введите предмет или название личной заметки.")
        elif action == "draft":
            await self.draft_action(uid, int(parts[1]), parts[2], parts[3])
        elif action == "askdate":
            cid, kind = int(parts[1]), parts[2]
            self.diary.require(cid, uid)
            if kind not in ("hw", "mine", "schedule"):
                raise ValueError("Неизвестный раздел.")
            self.new_state(uid, "view_date", cid, kind=kind)
            await self.tg.send(uid, "Введите дату просмотра: ДД.ММ.ГГГГ.")
        elif action == "task":
            await self.task(uid, int(parts[1]), int(parts[2]))
        elif action == "taskdone":
            cid, aid = int(parts[1]), int(parts[2])
            self.diary.toggle_done(cid, uid, aid)
            await self.task(uid, cid, aid, media=False)
        elif action == "taskedit":
            cid, aid = int(parts[1]), int(parts[2])
            row = self.diary.assignment(cid, uid, aid)
            if row["owner_id"] == 0:
                self.diary.require(cid, uid, "homework")
            self.new_state(uid, "draft", cid, draft=self.to_draft(row))
            await self.draft_preview(uid)
        elif action == "taskdelete":
            cid, aid, revision = int(parts[1]), int(parts[2]), int(parts[3])
            row = self.diary.assignment(cid, uid, aid)
            self.diary.require(cid, uid, "homework" if row["owner_id"] == 0 else None)
            await self.tg.send(uid, f"Удалить задание <b>{e(row['subject'])}</b>?",
                               kb([b("Да, удалить", f"taskdeleteyes:{cid}:{aid}:{revision}")], [b("Отмена", f"task:{cid}:{aid}")]))
        elif action == "taskdeleteyes":
            cid, aid, revision = int(parts[1]), int(parts[2]), int(parts[3])
            self.diary.delete_assignment(cid, uid, aid, revision)
            await self.tg.send(uid, "Задание удалено.", kb([b("В меню", f"home:{cid}")]))
        elif action == "audiences":
            await self.audiences(uid, int(parts[1]))
        elif action == "audience":
            cid, aid = int(parts[1]), int(parts[2])
            self.diary.toggle_audience(cid, uid, aid)
            await self.audiences(uid, cid)
        elif action == "audienceall":
            cid = int(parts[1])
            self.diary.require(cid, uid)
            self.db.run("DELETE FROM member_audiences WHERE class_id=? AND user_id=?", (cid, uid))
            await self.audiences(uid, cid)
        elif action == "members":
            await self.members(uid, int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
        elif action == "member":
            await self.member(uid, int(parts[1]), int(parts[2]))
        elif action == "addmembers":
            await self.add_members(uid, int(parts[1]))
        elif action == "memberids":
            cid = int(parts[1])
            self.diary.require(cid, uid, "members")
            self.new_state(uid, "member_ids", cid)
            await self.tg.send(uid, "Пришлите Telegram ID участников через пробел или контакт с Telegram ID. Номера телефонов не нужны.")
        elif action == "remove":
            cid, target = int(parts[1]), int(parts[2])
            self.diary.require(cid, uid, "members")
            await self.tg.send(uid, "Исключить участника? Его личные записи в этом классе будут удалены.",
                               kb([b("Исключить", f"removeyes:{cid}:{target}")], [b("Отмена", f"member:{cid}:{target}")]))
        elif action == "removeyes":
            self.diary.remove_member(int(parts[1]), uid, int(parts[2]))
            await self.members(uid, int(parts[1]))
        elif action == "roles":
            self.db.clear_state(uid)
            await self.roles(uid, int(parts[1]))
        elif action == "role":
            await self.role_editor(uid, int(parts[1]), int(parts[2]))
        elif action == "roleperm":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "role_edit")
            self.diary.require(cid, uid, "roles")
            permission = parts[3]
            if permission not in PERMISSIONS:
                raise ValueError("Неизвестное право.")
            selected = set(data["permissions"])
            selected.symmetric_difference_update({permission})
            data["permissions"] = sorted(selected)
            self.db.set_state(uid, "role_edit", data)
            await self.role_preview(uid, data)
        elif action == "rolesave":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "role_edit")
            self.diary.save_role(cid, uid, data["name"], data["permissions"], data.get("role_id"))
            self.db.clear_state(uid)
            await self.roles(uid, cid)
        elif action == "rolerename":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "role_edit")
            self.diary.require(cid, uid, "roles")
            self.db.set_state(uid, "role_name", data)
            await self.tg.send(uid, "Введите название роли.")
        elif action == "roledelete":
            cid, rid = int(parts[1]), int(parts[2])
            self.diary.require(cid, uid, "roles")
            await self.tg.send(uid, "Удалить роль у всех участников?",
                               kb([b("Удалить", f"roledeleteyes:{cid}:{rid}")], [b("Отмена", f"roles:{cid}")]))
        elif action == "roledeleteyes":
            self.diary.delete_role(int(parts[1]), uid, int(parts[2]))
            await self.roles(uid, int(parts[1]))
        elif action == "grant":
            cid, target, rid = int(parts[1]), int(parts[2]), int(parts[3])
            self.diary.toggle_role(cid, uid, target, rid)
            await self.member(uid, cid, target)
        elif action == "scheduleedit":
            await self.schedule_menu(uid, int(parts[1]))
        elif action == "weekday":
            cid, weekday = int(parts[1]), int(parts[2])
            await self.schedule_input(uid, cid, weekday=weekday)
        elif action == "override":
            cid = int(parts[1])
            self.diary.require(cid, uid, "schedule")
            self.new_state(uid, "override_date", cid)
            await self.tg.send(uid, "На какую дату нужна замена? Введите ДД.ММ.ГГГГ.")
        elif action == "scheduleconfirm":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "schedule_confirm")
            if data.get("day"):
                self.diary.set_override(cid, uid, date.fromisoformat(data["day"]), data["rows"])
            else:
                self.diary.replace_weekday(cid, uid, data["weekday"], data["rows"])
            self.db.clear_state(uid)
            await self.tg.send(uid, "Расписание сохранено. Сроки уже записанных заданий остались прежними.", kb([b("В меню", f"home:{cid}")]))
        elif action == "clearoverride":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "schedule_input", "schedule_confirm")
            if not data.get("day"):
                raise ValueError("Это недельное расписание, а не замена.")
            self.diary.clear_override(cid, uid, date.fromisoformat(data["day"]))
            self.db.clear_state(uid)
            await self.tg.send(uid, "Замена отменена: действует недельное расписание.")
        elif action == "template":
            cid = int(parts[1])
            self.diary.require(cid, uid, "schedule")
            await self.tg.send(uid, "Загрузить расписание 9Б с фотографии?\n\n"
                               "Дни недели будут заменены. Время и предметы перенесены с фото.\n"
                               "В ячейках через / первый предмет предварительно отнесён к «Группе 1», второй — к «Группе 2». "
                               "Проверьте распределение в редакторе. ВУД внесены отдельными вариантами.\n"
                               "Ранее записанные задания не удаляются.",
                               kb([b("Загрузить расписание", f"templateyes:{cid}")], [b("Отмена", f"manage:{cid}")]))
        elif action == "templateyes":
            cid = int(parts[1])
            self.diary.require(cid, uid, "schedule")
            template = json.loads((ROOT / "templates" / "9b.json").read_text(encoding="utf-8"))
            for day, rows in template["week"].items():
                self.diary.replace_weekday(cid, uid, int(day), rows)
            await self.tg.send(uid, "Расписание 9Б загружено. Проверьте подгруппы через «Управление → Расписание».", kb([b("В меню", f"home:{cid}")]))
        elif action == "bind":
            await self.bind_menu(uid, int(parts[1]))
        elif action == "pickgroup":
            cid = int(parts[1])
            self.diary.require(cid, uid, "settings")
            request_id = secrets.randbelow(2_000_000_000)
            self.new_state(uid, "pick_group", cid, request_id=request_id)
            await self.tg.send(uid, "Выберите группу, в которую бот уже добавлен. Вы должны быть её администратором.",
                               {"keyboard": [[{"text": "Выбрать группу", "request_chat": {"request_id": request_id, "chat_is_channel": False,
                                                                                           "bot_is_member": True, "request_title": True}}]], "resize_keyboard": True})
        elif action == "zone":
            cid = int(parts[1])
            self.diary.require(cid, uid, "settings")
            self.new_state(uid, "timezone", cid)
            await self.tg.send(uid, "Введите часовой пояс: Europe/Moscow, UTC или смещение +03:00.")
        elif action == "interactive":
            cid = int(parts[1])
            self.diary.require(cid, uid, "settings")
            self.db.run("UPDATE classes SET interactive=1-interactive WHERE id=?", (cid,))
            await self.manage(uid, cid)
        elif action == "announcement":
            cid = int(parts[1])
            self.diary.require(cid, uid, "announcements")
            self.new_state(uid, "announcement", cid, body="", attachments=[])
            await self.tg.send(uid, "Пришлите текст и материалы объявления. Оно отправится участникам в личку и в привязанную группу только после подтверждения.",
                               kb([b("Предпросмотр", f"annpreview:{cid}")]))
        elif action == "annpreview":
            await self.announcement_preview(uid, int(parts[1]))
        elif action == "annpublish":
            cid = int(parts[1])
            _, data = self.need_state(uid, cid, parts[2], "announcement")
            aid = self.diary.announce(cid, uid, data["body"], data["attachments"])
            self.db.clear_state(uid)
            await self.tg.send(uid, f"Объявление #{aid} сохранено и поставлено в очередь отправки.", kb([b("В меню", f"home:{cid}")]))
        elif action == "news":
            await self.news(uid, int(parts[1]))
        elif action == "newsone":
            await self.news_one(uid, int(parts[1]), int(parts[2]))
        elif action == "ack":
            cid, aid = int(parts[1]), int(parts[2])
            if not private:
                classroom = self.diary.require(cid, uid)
                if classroom["group_id"] != message.get("chat", {}).get("id"):
                    raise AccessDenied("Это объявление другого класса.")
            self.diary.ack(cid, uid, aid)
            return await self.tg.answer_callback(query["id"], "Вы отметили, что ознакомились")
        elif action == "reads":
            await self.reads(uid, int(parts[1]), int(parts[2]))
        elif action == "reminders":
            await self.reminders(uid)
        elif action == "remindertime":
            self.new_state(uid, "reminder_time")
            await self.tg.send(uid, "Во сколько напоминать? Например 18:00. Время считается по часовому поясу каждого вашего класса.")
        elif action == "reminderoff":
            self.db.run("UPDATE users SET reminder_time=NULL WHERE id=?", (uid,))
            await self.reminders(uid)
        elif action == "stats":
            await self.stats(uid, int(parts[1]))
        elif action == "pairs":
            await self.pairs(uid, int(parts[1]))
        elif action == "pairchoose":
            await self.pair_choose(uid, int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
        elif action == "pairrequest":
            cid, target = int(parts[1]), int(parts[2])
            self.interactive_required(cid, uid)
            pid = self.diary.request_pair(cid, uid, target)
            name = self.db.scalar("SELECT name FROM users WHERE id=?", (uid,))
            try:
                await self.tg.send(target, f"🔥 {e(name)} предлагает вести парный огонёк. Оба участника должны писать в группе каждый день.",
                                   kb([b("Принять", f"pairanswer:{cid}:{pid}:yes"), b("Отклонить", f"pairanswer:{cid}:{pid}:no")]))
            except TelegramError:
                pass
            await self.pairs(uid, cid)
        elif action == "pairanswer":
            cid, pid = int(parts[1]), int(parts[2])
            self.interactive_required(cid, uid)
            self.diary.answer_pair(cid, uid, pid, parts[3] == "yes")
            await self.pairs(uid, cid)
        elif action == "pairremove":
            cid, pid = int(parts[1]), int(parts[2])
            self.diary.require(cid, uid)
            self.db.run("DELETE FROM pairs WHERE class_id=? AND id=? AND (user_a=? OR user_b=?)", (cid, pid, uid, uid))
            await self.pairs(uid, cid)
        else:
            raise ValueError("Неизвестная кнопка. Откройте /menu.")
        await self.tg.answer_callback(query["id"])

    async def manage(self, uid, cid):
        classroom = self.diary.require(cid, uid)
        rights = self.diary.permissions(cid, uid)
        rows = []
        if "members" in rights or "roles" in rights:
            rows.append([b("👥 Участники", f"members:{cid}:0")])
        if "roles" in rights:
            rows.append([b("🔑 Роли и права", f"roles:{cid}")])
        if "schedule" in rights:
            rows += [[b("📅 Редактор расписания", f"scheduleedit:{cid}")],
                     [b("📷 Загрузить расписание 9Б", f"template:{cid}")]]
        if "announcements" in rights:
            rows.append([b("📢 Новое объявление", f"announcement:{cid}")])
        if "settings" in rights:
            rows += [[b("💬 Привязать группу", f"bind:{cid}")], [b("🕒 Часовой пояс", f"zone:{cid}")],
                     [b(f"Интерактив: {'вкл' if classroom['interactive'] else 'выкл'}", f"interactive:{cid}")]]
        rows.append([b("В меню", f"home:{cid}")])
        await self.tg.send(uid, f"⚙️ <b>{e(classroom['name'])}</b>\nЧасовой пояс: {e(classroom['timezone'])}\n"
                           f"Группа: {e(classroom['group_title'] or 'не привязана')}", {"inline_keyboard": rows})

    async def record(self, uid, cid, scope, assigned=None):
        if scope not in ("hw", "mine"):
            raise ValueError("Неизвестный тип записи.")
        classroom = self.diary.require(cid, uid, "homework" if scope == "hw" else None)
        assigned = assigned or today_in(classroom["timezone"])
        lessons = self.diary.lessons_on(cid, assigned) if scope == "hw" else self.diary.visible_lessons(cid, uid, assigned)
        data = self.new_state(uid, "record_pick", cid, scope=scope, assigned_on=assigned.isoformat(), lessons=lessons)
        nonce = data["nonce"]
        owner = 0 if scope == "hw" else uid
        rows = []
        for index, lesson in enumerate(lessons):
            existing = self.diary.existing_assignment(cid, uid, owner, lesson["source_key"], assigned)
            rows.append([b(("✅ " if existing else "✏️ ") + lesson_label(lesson), f"lesson:{cid}:{nonce}:{index}")])
        rows.append([b("📆 Другой день записи", f"recorddate:{cid}:{nonce}")])
        if scope == "mine":
            rows.append([b("➕ Свой предмет / заметка", f"custom:{cid}:{nonce}")])
        rows.append([b("В меню", f"home:{cid}")])
        message = f"<b>{'Общее ДЗ' if scope == 'hw' else 'Личная запись'} · {date_label(assigned)}</b>\nВыберите урок, на котором задание задали."
        if not lessons:
            message += "\nУроков на этот день нет. Выберите другой день или заполните расписание."
        await self.tg.send(uid, message, {"inline_keyboard": rows})

    @staticmethod
    def to_draft(row):
        keys = ("owner_id", "source_key", "source_position", "assigned_on", "subject", "audience_id", "due_on",
                "due_position", "body", "no_homework", "revision")
        result = {k: row[k] for k in keys}
        result["attachments"] = json.loads(row["attachments"])
        return result

    async def start_draft(self, uid, cid, scope, assigned, lesson):
        owner = 0 if scope == "hw" else uid
        old = self.diary.existing_assignment(cid, uid, owner, lesson["source_key"], assigned)
        if old:
            draft = self.to_draft(old)
        else:
            due, position = self.diary.next_lesson(cid, assigned, lesson["subject"], lesson["audience_id"])
            draft = dict(owner_id=owner, source_key=lesson["source_key"], source_position=lesson["position"],
                         assigned_on=assigned.isoformat(), subject=lesson["subject"], audience_id=lesson["audience_id"],
                         due_on=due.isoformat() if due else "", due_position=position, body="", attachments=[], no_homework=False, revision=0)
        data = self.new_state(uid, "draft", cid, draft=draft)
        if not draft["due_on"]:
            self.db.set_state(uid, "draft_date", data)
            await self.tg.send(uid, "Следующего урока в расписании нет. Введите дату выполнения ДД.ММ.ГГГГ.")
        else:
            await self.draft_preview(uid)

    async def draft_preview(self, uid):
        stage, data = self.db.state(uid)
        if stage not in ("draft", "draft_date"):
            raise ValueError("Черновик не найден.")
        cid, nonce, draft = data["cid"], data["nonce"], data["draft"]
        self.diary.require(cid, uid, "homework" if draft["owner_id"] == 0 else None)
        audience = self.db.scalar("SELECT name FROM audiences WHERE id=? AND class_id=?", (draft["audience_id"], cid)) or "весь класс"
        due = date_label(date.fromisoformat(draft["due_on"])) if draft["due_on"] else "выберите дату"
        body = "Ничего не задано" if draft["no_homework"] else (text_lines(draft["body"]) or "<i>Текст пока не добавлен.</i>")
        text = f"<b>{'📚 Общее ДЗ' if draft['owner_id'] == 0 else '📝 Личная запись'} · {e(draft['subject'])}</b>\n"
        text += f"Подгруппа: {e(audience)}\nЗадали: {date.fromisoformat(draft['assigned_on']):%d.%m.%Y}, урок № {draft['source_position']}\n"
        text += f"Сделать к: <b>{due}</b>" + (f", урок № {draft['due_position']}" if draft["due_position"] else "")
        text += f"\n\n{body}\n\n📎 Вложений: {len(draft['attachments'])}\n"
        text += "Пришлите текст, фото или файлы — они добавятся в черновик. Сохранение только по кнопке."
        rows = [[b("💾 Сохранить", f"draft:{cid}:{nonce}:save")],
                [b("📆 Изменить срок", f"draft:{cid}:{nonce}:date"), b("№ урока к сдаче", f"draft:{cid}:{nonce}:position")],
                [b("Ничего не задано", f"draft:{cid}:{nonce}:none")],
                [b("Очистить текст", f"draft:{cid}:{nonce}:textclear"), b("Убрать вложения", f"draft:{cid}:{nonce}:mediaclear")],
                [b("Отмена", f"home:{cid}")]]
        await self.send_long(uid, text, {"inline_keyboard": rows})

    async def draft_action(self, uid, cid, nonce, action):
        _, data = self.need_state(uid, cid, nonce, "draft")
        draft = data["draft"]
        self.diary.require(cid, uid, "homework" if draft["owner_id"] == 0 else None)
        if action == "save":
            aid = self.diary.save_assignment(cid, uid, draft)
            scope = "hw" if draft["owner_id"] == 0 else "mine"
            assigned = date.fromisoformat(draft["assigned_on"])
            self.db.clear_state(uid)
            await self.tg.send(uid, f"✅ {'Общее ДЗ' if scope == 'hw' else 'Личная запись'} сохранено к {date.fromisoformat(draft['due_on']):%d.%m.%Y}. "
                               "В группу сообщение не отправлялось.", kb([b("Открыть задание", f"task:{cid}:{aid}")]))
            return await self.record(uid, cid, scope, assigned)
        if action == "preview":
            return await self.draft_preview(uid)
        if action in ("date", "position"):
            self.db.set_state(uid, "draft_date" if action == "date" else "draft_position", data)
            await self.tg.send(uid, "Введите дату выполнения ДД.ММ.ГГГГ." if action == "date" else "Введите номер урока, к которому нужно выполнить задание (1–16), или 0 — без привязки к номеру.")
            return
        if action == "none":
            draft.update(no_homework=True, body="", attachments=[])
        elif action == "textclear":
            draft.update(no_homework=False, body="")
        elif action == "mediaclear":
            draft["attachments"] = []
        else:
            raise ValueError("Неизвестное действие с черновиком.")
        self.db.set_state(uid, "draft", data)
        await self.draft_preview(uid)

    async def view(self, uid, cid, kind, target):
        classroom = self.diary.require(cid, uid)
        if kind == "stats":
            return await self.stats(uid, cid)
        if kind not in ("hw", "mine", "schedule"):
            raise ValueError("Неизвестный раздел.")
        day = parse_date(target, today_in(classroom["timezone"]))
        lessons = self.diary.visible_lessons(cid, uid, day)
        rows = []
        if kind == "schedule":
            text = timetable_text(classroom, day, lessons)
        else:
            assignments = self.diary.assignments_on(cid, uid, day, personal=kind == "mine")
            text = homework_text(classroom, day, assignments, lessons, kind == "mine")
            for row in assignments:
                label = f"{'✅' if row['done'] else '📎' if json.loads(row['attachments']) else '📖'} {row['subject']}"
                if row["audience"]:
                    label += f" · {row['audience']}"
                label += f" · #{row['id']}"
                rows.append([b(label, f"task:{cid}:{row['id']}")])
        rows += [[b("◀️", f"v:{cid}:{kind}:{day-timedelta(days=1)}"), b("📆 Дата", f"askdate:{cid}:{kind}"), b("▶️", f"v:{cid}:{kind}:{day+timedelta(days=1)}")],
                 [b("Сегодня", f"v:{cid}:{kind}:today"), b("Завтра", f"v:{cid}:{kind}:tomorrow")],
                 [b("🔄 Обновить", f"v:{cid}:{kind}:{day}")], [b("В меню", f"home:{cid}")]]
        # Telegram permits at most 100 inline buttons in a keyboard.
        if sum(map(len, rows)) > 95:
            rows = rows[:85] + rows[-4:]
        await self.send_long(uid, text, {"inline_keyboard": rows})

    async def send_media(self, recipient, media):
        methods = {"photo": ("sendPhoto", "photo"), "document": ("sendDocument", "document"), "video": ("sendVideo", "video"),
                   "audio": ("sendAudio", "audio"), "voice": ("sendVoice", "voice"), "animation": ("sendAnimation", "animation"),
                   "video_note": ("sendVideoNote", "video_note")}
        for item in media:
            method, field = methods[item["type"]]
            await self.tg.call(method, **{"chat_id": recipient, field: item["file_id"]})

    async def task(self, uid, cid, aid, media=True):
        row = self.diary.assignment(cid, uid, aid)
        done = bool(self.db.scalar("SELECT 1 FROM completions WHERE assignment_id=? AND user_id=?", (aid, uid)))
        title = "Общее ДЗ" if row["owner_id"] == 0 else "Личная запись"
        text = f"<b>{title} · {e(row['subject'])}</b>\n{e(row['audience'] or 'Весь класс')}\nК {date.fromisoformat(row['due_on']):%d.%m.%Y}"
        if row["due_position"]:
            text += f", урок № {row['due_position']}"
        text += f"\n\n{text_lines(row['body']) or 'Задание во вложениях.'}\n\nЗапись #{aid} · {'✅ Выполнено' if done else 'Не отмечено выполненным'}"
        if media:
            await self.send_media(uid, json.loads(row["attachments"]))
        rows = []
        if not row["no_homework"]:
            rows.append([b("↩️ Снять отметку" if done else "✅ Выполнено", f"taskdone:{cid}:{aid}")])
        if row["owner_id"] == uid or "homework" in self.diary.permissions(cid, uid):
            rows += [[b("✏️ Изменить", f"taskedit:{cid}:{aid}"), b("🗑 Удалить", f"taskdelete:{cid}:{aid}:{row['revision']}")]]
        rows += [[b("📎 Получить вложения", f"task:{cid}:{aid}")],
                 [b("К списку на дату", f"v:{cid}:{'hw' if row['owner_id']==0 else 'mine'}:{row['due_on']}")]]
        await self.send_long(uid, text, {"inline_keyboard": rows})

    async def audiences(self, uid, cid):
        self.diary.require(cid, uid)
        selected = {x["audience_id"] for x in self.db.all("SELECT audience_id FROM member_audiences WHERE class_id=? AND user_id=?", (cid, uid))}
        rows = [[b(("✅ " if x["id"] in selected else "▫️ ") + x["name"], f"audience:{cid}:{x['id']}")] for x in self.diary.audiences(cid, uid)]
        rows += [[b("Показывать все подгруппы", f"audienceall:{cid}")], [b("В меню", f"home:{cid}")]]
        await self.tg.send(uid, "👥 <b>Мои подгруппы</b>\nВыберите свои занятия, включая нужные ВУД. Общие уроки видны всегда.\n"
                           "Если ничего не выбрано, показываются все варианты. Выбор влияет только на ваш просмотр.", {"inline_keyboard": rows})

    async def members(self, uid, cid, page=0):
        self.diary.require(cid, uid)
        rights = self.diary.permissions(cid, uid)
        if not rights.intersection({"members", "roles"}):
            raise AccessDenied("Управление участниками недоступно.")
        members = self.diary.members(cid, uid)
        page = max(0, min(page, max(0, (len(members)-1)//20)))
        rows = [[b(("✅ " if m["active"] else "⏳ ") + m["name"], f"member:{cid}:{m['id']}")] for m in members[page*20:page*20+20]]
        navigation = []
        if page:
            navigation.append(b("◀️", f"members:{cid}:{page-1}"))
        if (page+1)*20 < len(members):
            navigation.append(b("▶️", f"members:{cid}:{page+1}"))
        if navigation:
            rows.append(navigation)
        if "members" in rights:
            rows.append([b("➕ Выбрать участников", f"addmembers:{cid}")])
        rows.append([b("Назад", f"manage:{cid}")])
        await self.tg.send(uid, f"👥 <b>Участники: {len(members)}</b>\n✅ запустил бота · ⏳ ещё не запускал", {"inline_keyboard": rows})

    async def member(self, uid, cid, target):
        classroom = self.diary.require(cid, uid)
        rights = self.diary.permissions(cid, uid)
        if not rights.intersection({"members", "roles"}):
            raise AccessDenied("Управление участниками недоступно.")
        member = next((m for m in self.diary.members(cid, uid) if m["id"] == target), None)
        if not member:
            raise ValueError("Участник не найден.")
        rows = []
        if "roles" in rights and target != classroom["owner_id"]:
            current = {r["role_id"] for r in self.db.all("SELECT role_id FROM member_roles WHERE class_id=? AND user_id=?", (cid, target))}
            for role in self.diary.roles(cid, uid):
                rows.append([b(("✅ " if role["id"] in current else "▫️ ") + role["name"], f"grant:{cid}:{target}:{role['id']}")])
        if "members" in rights and target != classroom["owner_id"]:
            rows.append([b("Исключить из класса", f"remove:{cid}:{target}")])
        rows.append([b("К участникам", f"members:{cid}:0")])
        await self.tg.send(uid, f"<b>{e(member['name'])}</b>\nTelegram ID: <code>{target}</code>\n"
                           f"{'Владелец класса' if target == classroom['owner_id'] else 'Нажмите роль для назначения или снятия.'}", {"inline_keyboard": rows})

    async def add_members(self, uid, cid):
        classroom = self.diary.require(cid, uid, "members")
        request_id = secrets.randbelow(2_000_000_000)
        self.new_state(uid, "pick_members", cid, request_id=request_id)
        await self.tg.send(uid, "Выбирайте участников кнопкой ниже — до 10 за один выбор. После добавления они должны открыть бота и нажать «Начать».\n"
                           f"Ссылка для выбранных участников:\nhttps://t.me/{self.username}?start=c_{classroom['invite_token']}",
                           {"keyboard": [[{"text": "Выбрать участников", "request_users": {"request_id": request_id, "user_is_bot": False,
                                                                                           "max_quantity": 10, "request_name": True, "request_username": True}}],
                                         [{"text": "/cancel"}]], "resize_keyboard": True})
        await self.tg.send(uid, "Если окно выбора недоступно:", kb([b("Добавить по Telegram ID", f"memberids:{cid}")]))

    async def shared_users(self, uid, shared):
        stage, data = self.db.state(uid)
        if stage != "pick_members" or shared.get("request_id") != data.get("request_id"):
            raise ValueError("Запрос выбора устарел. Откройте добавление участников заново.")
        users = shared.get("users", [])
        self.diary.add_members(data["cid"], uid, users)
        await self.tg.send(uid, f"Добавлено или уже было в классе: {len(users)}. Можно выбрать ещё участников или нажать /cancel.")

    async def roles(self, uid, cid):
        rows = [[b(role["name"], f"role:{cid}:{role['id']}")] for role in self.diary.roles(cid, uid)]
        rows += [[b("➕ Создать роль", f"role:{cid}:0")], [b("Назад", f"manage:{cid}")]]
        await self.tg.send(uid, "🔑 <b>Роли класса</b>\nУчастнику можно выдать несколько ролей: их права складываются. Владелец имеет все права.", {"inline_keyboard": rows})

    async def role_editor(self, uid, cid, rid):
        self.diary.require(cid, uid, "roles")
        row = self.db.one("SELECT * FROM roles WHERE class_id=? AND id=?", (cid, rid)) if rid else None
        if rid and not row:
            raise AccessDenied("Роль не найдена.")
        data = self.new_state(uid, "role_edit" if row else "role_name", cid,
                              role_id=rid if rid else None, name=row["name"] if row else "", permissions=json.loads(row["permissions"]) if row else [])
        if row:
            await self.role_preview(uid, data)
        else:
            await self.tg.send(uid, "Введите название новой роли.")

    async def role_preview(self, uid, data):
        cid, nonce = data["cid"], data["nonce"]
        self.diary.require(cid, uid, "roles")
        rows = [[b(("✅ " if key in data["permissions"] else "▫️ ") + label, f"roleperm:{cid}:{nonce}:{key}")] for key, label in PERMISSIONS.items()]
        rows += [[b("💾 Сохранить роль", f"rolesave:{cid}:{nonce}")], [b("Изменить название", f"rolerename:{cid}:{nonce}")]]
        if data.get("role_id"):
            rows.append([b("Удалить роль", f"roledelete:{cid}:{data['role_id']}")])
        rows.append([b("Отмена", f"roles:{cid}")])
        await self.tg.send(uid, f"<b>{e(data['name'])}</b>\nВыберите права и сохраните роль.", {"inline_keyboard": rows})

    async def schedule_menu(self, uid, cid):
        self.diary.require(cid, uid, "schedule")
        rows = [[b(DAYS[i], f"weekday:{cid}:{i}")] for i in range(7)]
        rows += [[b("🔄 Замена на дату", f"override:{cid}")], [b("Назад", f"manage:{cid}")]]
        await self.tg.send(uid, "📅 <b>Редактор расписания</b>\nВыберите день для изменения недельного расписания либо настройте замену на конкретную дату.", {"inline_keyboard": rows})

    async def schedule_input(self, uid, cid, weekday=None, day=None):
        self.diary.require(cid, uid, "schedule")
        data = self.new_state(uid, "schedule_input", cid, weekday=weekday, day=day.isoformat() if day else None)
        if day is not None:
            rows = self.diary.lessons_on(cid, day)
        else:
            rows = self.db.all("SELECT l.*,COALESCE(a.name,'') AS audience FROM lessons l LEFT JOIN audiences a ON a.id=l.audience_id "
                               "WHERE l.class_id=? AND l.weekday=? AND archived=0 ORDER BY position,audience_id", (cid, weekday))
        text = "\n".join("<code>" + e(f"{r['position']}; {r['subject']}; {r['audience']}; {r['starts']+'-'+r['ends'] if r['starts'] else ''}; {r['room']}") + "</code>" for r in rows)
        title = date_label(day) if day else DAYS[weekday]
        await self.send_long(uid, f"<b>{title}</b>\n\nТекущее расписание:\n{text if text else 'Уроков нет'}\n\n"
                             "Пришлите весь день одним сообщением, по уроку на строку:\n"
                             "<code>1; Алгебра; ; 09:00-09:45; 12</code>\n"
                             "<code>2; Английский язык; Группа 1; 10:05-10:50</code>\n"
                             "<code>2; Информатика; Группа 2; 10:05-10:50</code>\n\n"
                             "Подгруппа, время и кабинет необязательны. Без подгруппы урок общий. Для выходного пришлите «выходной».\n"
                             "Будет предпросмотр перед сохранением.",
                             kb([b("Убрать замену на дату", f"clearoverride:{cid}:{data['nonce']}")]) if day else None)

    async def bind_menu(self, uid, cid):
        classroom = self.diary.require(cid, uid, "settings")
        await self.tg.send(uid, "💬 <b>Привязка группы</b>\nНажмите «Добавить бота» и выберите группу. Вы должны быть её администратором.\n"
                           "Если бот уже добавлен, воспользуйтесь выбором существующей группы. Одна группа относится к одному классу.",
                           kb([b("➕ Добавить бота в группу", url=f"https://t.me/{self.username}?startgroup=bind_{classroom['invite_token']}")],
                              [b("Выбрать существующую группу", f"pickgroup:{cid}")], [b("Назад", f"manage:{cid}")]))

    async def shared_chat(self, uid, shared):
        stage, data = self.db.state(uid)
        if stage != "pick_group" or data.get("request_id") != shared.get("request_id"):
            raise ValueError("Запрос выбора группы устарел.")
        cid, chat_id = data["cid"], shared["chat_id"]
        self.diary.require(cid, uid, "settings")
        chat = await self.tg.call("getChat", chat_id=chat_id)
        if chat["type"] not in ("group", "supergroup"):
            raise ValueError("Выберите группу, а не канал.")
        person = await self.tg.call("getChatMember", chat_id=chat_id, user_id=uid)
        bot_member = await self.tg.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
        if person["status"] not in ("creator", "administrator") or bot_member["status"] in ("left", "kicked"):
            raise AccessDenied("Вы должны быть администратором, а бот — участником группы.")
        self.diary.bind_group(cid, uid, chat_id, chat.get("title", "Группа"))
        self.db.clear_state(uid)
        await self.tg.send(uid, "Группа привязана. В группе можно вызвать /menu. Для статистики сообщений боту нужен доступ к обычным сообщениям.", {"remove_keyboard": True})
        await self.manage(uid, cid)

    @staticmethod
    def extract_content(message):
        text = message.get("text", message.get("caption", ""))
        media = []
        if message.get("photo"):
            media.append({"type": "photo", "file_id": message["photo"][-1]["file_id"]})
        else:
            for kind in ("animation", "document", "video", "audio", "voice", "video_note"):
                if message.get(kind):
                    media.append({"type": kind, "file_id": message[kind]["file_id"]})
                    break
        if not text and not media:
            raise ValueError("Пришлите текст, фото, документ, видео или голосовое сообщение.")
        return text, media

    async def state_message(self, uid, message, stage, data):
        text = message.get("text", "").strip()
        cid = data.get("cid")
        if cid is not None:
            classroom = self.diary.require(cid, uid)
        if stage == "class_name":
            if not text:
                raise ValueError("Введите название текстом.")
            cid = self.diary.create_class(uid, text, self.config.default_timezone, self.config.max_classes_per_owner)
            self.db.clear_state(uid)
            await self.tg.send(uid, "Класс создан. Вы — его владелец. Можно загрузить расписание 9Б с фотографии либо составить своё.",
                               kb([b("Загрузить расписание 9Б", f"template:{cid}")], [b("Составить своё", f"scheduleedit:{cid}")]))
            await self.home(uid, cid)
        elif stage == "record_date":
            assigned = parse_date(text, today_in(classroom["timezone"]))
            await self.record(uid, cid, data["scope"], assigned)
        elif stage == "custom_title":
            if not 1 <= len(text) <= 80:
                raise ValueError("Название должно содержать 1–80 символов.")
            assigned = date.fromisoformat(data["assigned_on"])
            draft = dict(owner_id=uid, source_key=f"note:{secrets.token_hex(8)}", source_position=0,
                         assigned_on=assigned.isoformat(), subject=text, audience_id=0,
                         due_on=(assigned+timedelta(days=1)).isoformat(), due_position=0, body="", attachments=[], no_homework=False, revision=0)
            self.new_state(uid, "draft", cid, draft=draft)
            await self.draft_preview(uid)
        elif stage in ("draft_date", "draft_position"):
            draft = data["draft"]
            self.diary.require(cid, uid, "homework" if draft["owner_id"] == 0 else None)
            if stage == "draft_date":
                target = parse_date(text, today_in(classroom["timezone"]))
                if target < date.fromisoformat(draft["assigned_on"]):
                    raise ValueError("Срок выполнения не может быть раньше дня записи.")
                matches = [r for r in self.diary.lessons_on(cid, target) if r["subject"].casefold() == draft["subject"].casefold()
                           and (not draft["audience_id"] or r["audience_id"] in (0, draft["audience_id"]))]
                draft.update(due_on=target.isoformat(), due_position=matches[0]["position"] if matches else 0)
            else:
                position = int(text)
                if not 0 <= position <= 16:
                    raise ValueError("Номер урока должен быть от 0 до 16.")
                draft["due_position"] = position
            self.db.set_state(uid, "draft", data)
            await self.draft_preview(uid)
        elif stage == "draft":
            draft = data["draft"]
            self.diary.require(cid, uid, "homework" if draft["owner_id"] == 0 else None)
            body, media = self.extract_content(message)
            combined = "\n".join(x for x in (draft["body"], body) if x)
            if len(combined) > 6000 or len(draft["attachments"]) + len(media) > 20:
                raise ValueError("Максимум 6000 символов и 20 вложений. Черновик не изменён.")
            draft.update(body=combined, attachments=draft["attachments"]+media, no_homework=False)
            self.db.set_state(uid, "draft", data)
            # Albums arrive as multiple updates. One compact acknowledgement keeps the latest Save button available.
            await self.tg.send(uid, f"Добавлено. Текст: {len(combined)} символов · вложений: {len(draft['attachments'])}.",
                               kb([b("Предпросмотр", f"draft:{cid}:{data['nonce']}:preview"), b("💾 Сохранить", f"draft:{cid}:{data['nonce']}:save")]))
        elif stage == "view_date":
            target = parse_date(text, today_in(classroom["timezone"]))
            self.db.clear_state(uid)
            await self.view(uid, cid, data["kind"], target.isoformat())
        elif stage == "member_ids":
            self.diary.require(cid, uid, "members")
            if message.get("contact", {}).get("user_id"):
                contact = message["contact"]
                users = [{"user_id": contact["user_id"], "first_name": contact.get("first_name", ""), "last_name": contact.get("last_name", "")}]
            else:
                ids = text.replace(",", " ").split()
                if not ids or len(ids) > 50 or any(not x.isdecimal() for x in ids):
                    raise ValueError("Пришлите до 50 числовых Telegram ID через пробел.")
                users = [{"user_id": int(x)} for x in ids]
            self.diary.add_members(cid, uid, users)
            await self.tg.send(uid, f"Добавлено или уже было: {len(users)}. Можно отправить ещё ID или нажать /cancel.")
        elif stage == "role_name":
            self.diary.require(cid, uid, "roles")
            if not 1 <= len(text) <= 50:
                raise ValueError("Название роли: от 1 до 50 символов.")
            data["name"] = text
            self.db.set_state(uid, "role_edit", data)
            await self.role_preview(uid, data)
        elif stage == "override_date":
            self.diary.require(cid, uid, "schedule")
            target = parse_date(text, today_in(classroom["timezone"]))
            await self.schedule_input(uid, cid, day=target)
        elif stage == "schedule_input":
            self.diary.require(cid, uid, "schedule")
            data["rows"] = parse_day(text)
            self.db.set_state(uid, "schedule_confirm", data)
            title = data["day"] or DAYS[data["weekday"]]
            preview = "\n".join(f"{r['position']}. {r['subject']}" + (f" · {r['audience']}" if r["audience"] else "")
                                + (f" · {r['starts']}–{r['ends']}" if r["starts"] else "") for r in data["rows"])
            await self.send_long(uid, f"<b>Предпросмотр · {e(title)}</b>\n{text_lines(preview) if preview else 'Выходной'}\n\nСохранить изменения?",
                                 kb([b("💾 Сохранить расписание", f"scheduleconfirm:{cid}:{data['nonce']}")], [b("Отмена", f"home:{cid}")]))
        elif stage == "timezone":
            self.diary.require(cid, uid, "settings")
            get_timezone(text)
            self.db.run("UPDATE classes SET timezone=? WHERE id=?", (text, cid))
            self.db.audit(cid, uid, "timezone.set", text)
            self.db.clear_state(uid)
            await self.manage(uid, cid)
        elif stage == "announcement":
            self.diary.require(cid, uid, "announcements")
            body, media = self.extract_content(message)
            combined = "\n".join(x for x in (data["body"], body) if x)
            if len(combined) > 6000 or len(data["attachments"]) + len(media) > 20:
                raise ValueError("Максимум 6000 символов и 20 вложений.")
            data.update(body=combined, attachments=data["attachments"]+media)
            self.db.set_state(uid, "announcement", data)
            await self.tg.send(uid, f"Добавлено. Вложений: {len(data['attachments'])}.", kb([b("Предпросмотр", f"annpreview:{cid}")]))
        elif stage == "reminder_time":
            import re
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
                raise ValueError("Время должно быть в формате ЧЧ:ММ, например 18:00.")
            self.db.run("UPDATE users SET reminder_time=? WHERE id=?", (text, uid))
            self.db.clear_state(uid)
            await self.reminders(uid)
        else:
            await self.tg.send(uid, "Для следующего шага нажмите нужную кнопку выше. Для отмены: /cancel.")

    async def announcement_preview(self, uid, cid):
        self.diary.require(cid, uid, "announcements")
        stage, data = self.db.state(uid)
        if stage != "announcement" or data.get("cid") != cid:
            raise ValueError("Черновик объявления не найден.")
        await self.send_long(uid, f"📢 <b>Предпросмотр объявления</b>\n\n{text_lines(data['body']) or 'Текст не добавлен.'}\n\n"
                             f"Вложений: {len(data['attachments'])}\nПосле публикации участникам придёт уведомление.",
                             kb([b("📢 Опубликовать", f"annpublish:{cid}:{data['nonce']}")], [b("Отмена", f"home:{cid}")]))

    async def news(self, uid, cid):
        self.diary.require(cid, uid)
        news = self.db.all("SELECT * FROM announcements WHERE class_id=? ORDER BY id DESC LIMIT 30", (cid,))
        rows = [[b(f"#{row['id']} · {row['created_at'][:10]} · {row['body'][:35] or 'Материалы'}", f"newsone:{cid}:{row['id']}")] for row in news]
        rows.append([b("В меню", f"home:{cid}")])
        await self.tg.send(uid, "📢 <b>Последние объявления</b>" + ("\nОбъявлений пока нет." if not news else ""), {"inline_keyboard": rows})

    async def news_one(self, uid, cid, aid):
        self.diary.require(cid, uid)
        row = self.db.one("SELECT * FROM announcements WHERE id=? AND class_id=?", (aid, cid))
        if not row:
            raise ValueError("Объявление не найдено.")
        await self.send_media(uid, json.loads(row["attachments"]))
        rows = [[b("✅ Ознакомился", f"ack:{cid}:{aid}")]]
        if "announcements" in self.diary.permissions(cid, uid):
            rows.append([b("Кто ознакомился", f"reads:{cid}:{aid}")])
        rows.append([b("К объявлениям", f"news:{cid}")])
        await self.send_long(uid, f"📢 <b>Объявление #{aid}</b>\n\n{text_lines(row['body']) or 'Материалы во вложениях.'}", {"inline_keyboard": rows})

    async def reads(self, uid, cid, aid):
        self.diary.require(cid, uid, "announcements")
        if not self.db.scalar("SELECT 1 FROM announcements WHERE class_id=? AND id=?", (cid, aid)):
            raise ValueError("Объявление не найдено.")
        rows = self.db.all("SELECT u.name FROM announcement_reads ar JOIN users u ON u.id=ar.user_id "
                           "JOIN members m ON m.class_id=? AND m.user_id=ar.user_id WHERE ar.announcement_id=? ORDER BY u.name", (cid, aid))
        await self.send_long(uid, f"<b>Отметили «Ознакомился»: {len(rows)}</b>\n" + "\n".join(e(r["name"]) for r in rows))

    async def reminders(self, uid):
        clock = self.db.scalar("SELECT reminder_time FROM users WHERE id=?", (uid,))
        await self.tg.send(uid, f"🔔 <b>Личные напоминания</b>\nСейчас: {e(clock or 'выключены')}.\n"
                           "Бот напомнит об общей домашке и расписании на завтра. "
                           "Напоминание приходит в каждый учебный день накануне; в выходные без заданий не отправляется.",
                           kb([b("Выбрать время", "remindertime"), b("Выключить", "reminderoff")], [b("В меню", "classes")]))

    def interactive_required(self, cid, uid):
        classroom = self.diary.require(cid, uid)
        if not classroom["interactive"]:
            raise ValueError("Владелец выключил интерактив для этого класса.")
        return classroom

    async def stats(self, uid, cid):
        classroom = self.interactive_required(cid, uid)
        today = today_in(classroom["timezone"])
        stats = self.diary.stats(cid, uid, today)
        top = self.diary.top(cid, uid, today)
        text = f"📊 <b>{e(classroom['name'])} · ваши сообщения</b>\nСегодня: {stats['today']}\nЗа 7 дней: {stats['week']}\nВсего: {stats['total']}\n"
        text += f"🔥 Серия общения: {stats['streak']} дн.\n\n<b>Участники за 7 дней</b>\n"
        text += "\n".join(f"{i}. {e(r['name'])} — {r['n']}" for i, r in enumerate(top, 1)) or "Пока нет сообщений."
        text += "\n\nСчитаются сообщения участников с момента включения учёта. Одна отправка — одно сообщение; правки повторно не считаются. "
        text += "Для огонька достаточно одного сообщения за календарный день."
        await self.send_long(uid, text, kb([b("🔥 Парные огоньки", f"pairs:{cid}")], [b("В меню", f"home:{cid}")]))

    async def pairs(self, uid, cid):
        classroom = self.interactive_required(cid, uid)
        pairs = self.diary.pairs(cid, uid, today_in(classroom["timezone"]))
        lines = ["🔥 <b>Парные огоньки</b>", "Серия растёт за дни, в которые оба писали в привязанной группе. Требуется согласие второго участника.", ""]
        rows = []
        for pair in pairs:
            if pair["accepted"]:
                lines.append(f"{e(pair['name'])} — 🔥 {pair['streak']} дн.")
            elif pair["requester"] == uid:
                lines.append(f"{e(pair['name'])} — ожидаем согласия")
            else:
                lines.append(f"{e(pair['name'])} — приглашает вас")
                rows.append([b("Принять", f"pairanswer:{cid}:{pair['id']}:yes"), b("Отклонить", f"pairanswer:{cid}:{pair['id']}:no")])
            rows.append([b(f"Убрать: {pair['name']}", f"pairremove:{cid}:{pair['id']}")])
        rows += [[b("➕ Пригласить участника", f"pairchoose:{cid}:0")], [b("В меню", f"home:{cid}")]]
        await self.send_long(uid, "\n".join(lines), {"inline_keyboard": rows})

    async def pair_choose(self, uid, cid, page):
        self.interactive_required(cid, uid)
        members = [m for m in self.diary.members(cid, uid) if m["active"] and m["id"] != uid]
        page = max(0, min(page, max(0, (len(members)-1)//20)))
        rows = [[b(m["name"], f"pairrequest:{cid}:{m['id']}")] for m in members[page*20:page*20+20]]
        nav = []
        if page:
            nav.append(b("◀️", f"pairchoose:{cid}:{page-1}"))
        if (page+1)*20 < len(members):
            nav.append(b("▶️", f"pairchoose:{cid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([b("Назад", f"pairs:{cid}")])
        await self.tg.send(uid, "Выберите участника для парного огонька.", {"inline_keyboard": rows})

    def schedule_reminders(self, now=None):
        now = now or datetime.now(timezone.utc)
        classrooms = self.db.all("SELECT * FROM classes")
        for classroom in classrooms:
            local = now.astimezone(get_timezone(classroom["timezone"]))
            tomorrow = local.date() + timedelta(days=1)
            has_lessons = bool(self.diary.lessons_on(classroom["id"], tomorrow))
            users = self.db.all("SELECT u.id,u.reminder_time FROM users u JOIN members m ON m.user_id=u.id "
                                "WHERE m.class_id=? AND m.active=1 AND u.started=1 AND u.reminder_time IS NOT NULL", (classroom["id"],))
            for user in users:
                has_tasks = self.db.scalar("SELECT 1 FROM assignments WHERE class_id=? AND due_on=? AND owner_id IN (0,?) LIMIT 1",
                                           (classroom["id"], tomorrow.isoformat(), user["id"]))
                if not has_lessons and not has_tasks:
                    continue
                # Catch up within the same date after downtime; unique key prevents duplicate daily reminders.
                if local.strftime("%H:%M") >= user["reminder_time"]:
                    self.diary.queue(f"reminder:{classroom['id']}:{user['id']}:{local.date()}", classroom["id"], user["id"],
                                     "reminder", {"day": tomorrow.isoformat()})

    async def deliver_pending(self):
        deliveries = self.db.all("SELECT * FROM deliveries WHERE state='pending' AND retry_after<=? ORDER BY id LIMIT 20", (time.time(),))
        for delivery in deliveries:
            cid, recipient = delivery["class_id"], delivery["recipient"]
            classroom = self.db.one("SELECT * FROM classes WHERE id=?", (cid,))
            payload = json.loads(delivery["payload"])
            if not classroom:
                continue
            if recipient > 0:
                if not self.db.scalar("SELECT 1 FROM members m JOIN users u ON u.id=m.user_id WHERE m.class_id=? AND m.user_id=? AND m.active=1 AND u.started=1", (cid, recipient)):
                    self.db.run("UPDATE deliveries SET state='cancelled' WHERE id=?", (delivery["id"],))
                    continue
            elif classroom["group_id"] != recipient:
                self.db.run("UPDATE deliveries SET state='cancelled' WHERE id=?", (delivery["id"],))
                continue
            try:
                if delivery["kind"] == "announcement":
                    row = self.db.one("SELECT * FROM announcements WHERE id=? AND class_id=?", (payload["id"], cid))
                    if row:
                        await self.send_media(recipient, json.loads(row["attachments"]))
                        await self.send_long(recipient, f"📢 <b>{e(classroom['name'])} · важное объявление</b>\n\n{text_lines(row['body']) or 'Материалы во вложениях.'}",
                                             kb([b("✅ Ознакомился", f"ack:{cid}:{row['id']}")]))
                elif delivery["kind"] == "reminder":
                    user = self.db.one("SELECT reminder_time FROM users WHERE id=?", (recipient,))
                    tomorrow = date.fromisoformat(payload["day"])
                    if not user or not user["reminder_time"] or tomorrow <= today_in(classroom["timezone"]):
                        self.db.run("UPDATE deliveries SET state='cancelled' WHERE id=?", (delivery["id"],))
                        continue
                    await self.tg.send(recipient, f"🔔 <b>{e(classroom['name'])}</b>\nПроверьте задания и расписание на {tomorrow:%d.%m.%Y}.",
                                       kb([b("📚 Общее ДЗ", f"v:{cid}:hw:{tomorrow}")], [b("📝 Мои записи", f"v:{cid}:mine:{tomorrow}")],
                                          [b("📅 Расписание", f"v:{cid}:schedule:{tomorrow}")]))
                self.db.run("UPDATE deliveries SET state='sent',last_error='' WHERE id=?", (delivery["id"],))
            except TelegramError as exc:
                attempts = delivery["attempts"] + 1
                permanent = exc.code in (400, 401, 403) or attempts >= 5
                self.db.run("UPDATE deliveries SET attempts=?,state=?,retry_after=?,last_error=? WHERE id=?",
                            (attempts, "failed" if permanent else "pending", time.time()+max(exc.retry_after, min(300, 10*2**attempts)),
                             f"Telegram {exc.code}", delivery["id"]))
                if exc.code == 429:
                    break

    async def background(self):
        last_reminders = 0.0
        while True:
            try:
                if time.monotonic() - last_reminders > 30:
                    self.schedule_reminders()
                    last_reminders = time.monotonic()
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Background task failed: %s", type(exc).__name__)
            await asyncio.sleep(2)


if __name__ == "__main__":
    # Some hosts detect the package's app module as the entry point.
    # Delegate to the same CLI used by run.py instead of exiting after imports.
    from run import main

    raise SystemExit(main())
