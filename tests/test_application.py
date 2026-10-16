from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from classdiary.app import Application
from classdiary.config import Config, ROOT
from classdiary.db import Database
from classdiary.telegram import TelegramError


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.blocked = set()
        self.admins = {100, 999}
        self.fail_delivery = False
        self.edit_error = None
        self.privacy_disabled = False
        self.message_ids = {}
        self.send_errors = {}

    def next_message_id(self, chat_id):
        self.message_ids[chat_id] = self.message_ids.get(chat_id, 0)+1
        return self.message_ids[chat_id]

    def validate_text(self, text, keyboard):
        if len(text) > 4096:
            raise AssertionError(f"Oversized Telegram text: {len(text)}")
        if keyboard and "inline_keyboard" in keyboard:
            buttons = [b for row in keyboard["inline_keyboard"] for b in row]
            assert len(buttons) <= 100
            assert all(len(b.get("callback_data", "").encode()) <= 64 for b in buttons)

    async def send(self, chat_id, text, keyboard=None, **extra):
        if chat_id in self.send_errors:
            raise self.send_errors[chat_id]
        if chat_id in self.blocked:
            raise TelegramError(403, "Forbidden")
        if self.fail_delivery:
            raise TelegramError(429, "Retry later", 15)
        self.validate_text(text, keyboard)
        self.calls.append(("sendMessage", dict(chat_id=chat_id, text=text, reply_markup=keyboard, **extra)))
        return {"message_id": self.next_message_id(chat_id), "chat": {"id": chat_id}}

    async def answer_callback(self, callback_id, text="", alert=False):
        self.calls.append(("answerCallbackQuery", dict(callback_query_id=callback_id, text=text, show_alert=alert)))

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "editMessageText":
            self.validate_text(payload["text"], payload.get("reply_markup"))
            if payload["chat_id"] in self.blocked:
                raise TelegramError(403, "Forbidden")
            if self.edit_error:
                raise self.edit_error
            return {"message_id": payload["message_id"]}
        if method in {"sendPhoto", "sendDocument", "sendVideo", "sendAudio", "sendVoice", "sendAnimation", "sendVideoNote"}:
            return {"message_id": self.next_message_id(payload["chat_id"])}
        if method == "getChatMember":
            return {"status": "administrator" if payload["user_id"] in self.admins else "member"}
        if method == "getMe":
            return {"id": 999, "username": "class_diary_test_bot", "can_read_all_group_messages": self.privacy_disabled}
        if method == "getChat":
            return {"id": payload["chat_id"], "type": "supergroup", "title": "Класс"}
        return True


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        self.tg = FakeTelegram()
        self.app = Application(Config("test", Path(":memory:")), self.db, self.tg)
        self.app.username, self.app.bot_id = "class_diary_test_bot", 999
        self.datepatch = patch("classdiary.app.today_in", return_value=date(2026, 10, 14))
        self.datepatch.start()
        self.groupdatepatch = patch("classdiary.group_interactive.today_in", return_value=date(2026, 10, 14))
        self.groupdatepatch.start()
        for uid in (100, 101, 102, 200):
            self.app.diary.touch_user({"id": uid, "first_name": f"Ученик {uid}"}, started=True)
        self.cid = self.app.diary.create_class(100, "9Б", "Europe/Moscow")
        self.app.diary.add_members(self.cid, 100, [{"user_id": 101}, {"user_id": 102}])
        template = json.loads((ROOT / "templates" / "9b.json").read_text(encoding="utf-8"))
        for day, rows in template["week"].items():
            self.app.diary.replace_weekday(self.cid, 100, int(day), rows)

    async def asyncTearDown(self):
        self.datepatch.stop()
        self.groupdatepatch.stop()
        self.db.close()

    async def callback(self, data, uid=100, group=False, message_id=1):
        chat = {"id": -100 if group else uid, "type": "supergroup" if group else "private"}
        query = {"id": "callback-test", "from": {"id": uid, "first_name": f"Ученик {uid}"}, "data": data,
                 "message": {"message_id": message_id, "chat": chat}}
        await self.app.on_update({"callback_query": query})

    async def message(self, text=None, uid=100, group=False, **extra):
        msg = {"message_id": len(self.tg.calls)+100, "date": 1791975600,
               "from": {"id": uid, "first_name": f"Ученик {uid}"},
               "chat": {"id": -100 if group else uid, "type": "supergroup" if group else "private"}, **extra}
        if text is not None:
            msg["text"] = text
        await self.app.on_update({"message": msg})

    def texts(self, uid=None):
        return "\n".join(payload["text"] for method, payload in self.tg.calls if method in ("sendMessage", "editMessageText") and (uid is None or payload["chat_id"] == uid))

    async def start_record(self, uid=100, scope="hw", subject="Английский язык", audience="Группа 1"):
        await self.callback(f"record:{self.cid}:{scope}", uid)
        stage, state = self.db.state(uid)
        self.assertEqual(stage, "record_pick")
        index = next(i for i, r in enumerate(state["lessons"]) if r["subject"] == subject and r["audience"] == audience)
        await self.callback(f"lesson:{self.cid}:{state['nonce']}:{index}", uid)
        stage, data = self.db.state(uid)
        self.assertEqual(stage, "draft")
        return data

    async def test_full_curator_flow_records_next_subgroup_lesson(self):
        state = await self.start_record()
        self.assertEqual(state["draft"]["due_on"], "2026-10-16")
        await self.message("Workbook, page 25")
        await self.message(photo=[{"file_id": "small"}, {"file_id": "full-resolution"}])
        state = self.db.state(100)[1]
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save")
        rows = self.app.diary.assignments_on(self.cid, 101, date(2026, 10, 16))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "Workbook, page 25")
        self.assertEqual(json.loads(rows[0]["attachments"])[0]["file_id"], "full-resolution")
        self.assertEqual(rows[0]["due_position"], 3)
        self.assertFalse(any(payload.get("chat_id", 0) < 0 for method, payload in self.tg.calls if method.startswith("send")))

    async def test_personal_note_does_not_appear_in_common_view(self):
        state = await self.start_record(uid=101, scope="mine")
        await self.message("МОЯ СЕКРЕТНАЯ ПОМЕТКА", uid=101)
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save", uid=101)
        self.tg.calls.clear()
        await self.callback(f"v:{self.cid}:hw:2026-10-16", uid=100)
        self.assertNotIn("СЕКРЕТНАЯ", self.texts())
        self.tg.calls.clear()
        await self.callback(f"v:{self.cid}:mine:2026-10-16", uid=101)
        self.assertIn("МОЯ СЕКРЕТНАЯ ПОМЕТКА", self.texts(101))

    async def test_new_curator_can_write_only_after_role_grant(self):
        await self.callback(f"record:{self.cid}:hw", uid=101)
        self.assertTrue(any(p.get("show_alert") for m, p in self.tg.calls if m == "answerCallbackQuery"))
        role = self.db.scalar("SELECT id FROM roles WHERE class_id=? AND name='Ответственный за ДЗ'", (self.cid,))
        self.app.diary.toggle_role(self.cid, 100, 101, role)
        await self.start_record(uid=101)

    async def test_revoked_curator_cannot_save_open_draft(self):
        role = self.db.scalar("SELECT id FROM roles WHERE class_id=? AND name='Ответственный за ДЗ'", (self.cid,))
        self.app.diary.toggle_role(self.cid, 100, 101, role)
        state = await self.start_record(uid=101)
        await self.message("ДЗ", uid=101)
        self.app.diary.toggle_role(self.cid, 100, 101, role)
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save", uid=101)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM assignments"), 0)

    async def test_stale_lesson_buttons_do_not_open_another_draft(self):
        await self.callback(f"record:{self.cid}:hw")
        first = self.db.state(100)[1]
        await self.callback(f"record:{self.cid}:mine")
        await self.callback(f"lesson:{self.cid}:{first['nonce']}:0")
        self.assertEqual(self.db.state(100)[0], "record_pick")
        self.assertTrue(any("устарела" in p.get("text", "") for m, p in self.tg.calls if m == "answerCallbackQuery"))

    async def test_group_button_returns_timetable_to_private_chat(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"v:{self.cid}:schedule:tomorrow", uid=101, group=True)
        messages = [p for m, p in self.tg.calls if m == "sendMessage"]
        self.assertTrue(messages)
        self.assertTrue(all(p["chat_id"] == 101 for p in messages))
        self.assertIn("Профориентация", self.texts(101))
        self.assertIn("Геометрия", self.texts(101))

    async def test_bot_blocked_in_private_offers_deep_link_in_group(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.tg.blocked.add(101)
        await self.callback(f"v:{self.cid}:hw:tomorrow", uid=101, group=True)
        messages = [p for m, p in self.tg.calls if m == "sendMessage"]
        self.assertEqual(messages[-1]["chat_id"], -100)
        self.assertIn("https://t.me/", str(messages[-1]["reply_markup"]))

    async def test_forwarded_group_menu_cannot_access_other_class(self):
        other = self.app.diary.create_class(200, "Другой класс", "UTC")
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"v:{other}:hw:tomorrow", uid=200, group=True)
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))

    async def test_group_cannot_run_private_mutation_callbacks(self):
        await self.callback(f"record:{self.cid}:hw", uid=100, group=True)
        self.assertIsNone(self.db.state(100)[0])

    async def test_native_user_selection_is_scoped_to_request(self):
        await self.callback(f"addmembers:{self.cid}")
        state = self.db.state(100)[1]
        await self.message(users_shared={"request_id": state["request_id"], "users": [{"user_id": 300, "first_name": "Друг"}]})
        self.assertTrue(self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=300", (self.cid,)))
        await self.message(users_shared={"request_id": state["request_id"]+1, "users": [{"user_id": 301}]})
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=301", (self.cid,)))

    async def test_template_can_be_loaded_from_setup_button(self):
        cid = self.app.diary.create_class(100, "Новый", "Europe/Moscow")
        await self.callback(f"templateyes:{cid}")
        self.assertEqual(len(self.app.diary.lessons_on(cid, date(2026, 10, 14))), 9)

    async def test_draft_preview_button_works(self):
        state = await self.start_record()
        await self.message("Текст")
        await self.callback(f"draft:{self.cid}:{state['nonce']}:preview")
        self.assertIn("Сделать к:", self.texts())
        self.assertFalse(any("Неизвестное действие" in p.get("text", "") for m, p in self.tg.calls if m == "answerCallbackQuery"))

    async def test_schedule_replacement_requires_confirmation(self):
        await self.callback(f"weekday:{self.cid}:2")
        await self.message("1; Тестовый урок; ; 08:10-08:55")
        self.assertNotIn("Тестовый урок", [r["subject"] for r in self.app.diary.lessons_on(self.cid, date(2026, 10, 14))])
        state = self.db.state(100)[1]
        await self.callback(f"scheduleconfirm:{self.cid}:{state['nonce']}")
        self.assertEqual([r["subject"] for r in self.app.diary.lessons_on(self.cid, date(2026, 10, 14))], ["Тестовый урок"])

    async def test_reminders_are_deduplicated_across_scheduler_runs(self):
        self.db.run("UPDATE users SET reminder_time='18:00' WHERE id=101")
        now = datetime(2026, 10, 14, 15, 5, tzinfo=timezone.utc)
        self.app.schedule_reminders(now)
        self.app.schedule_reminders(now)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM deliveries WHERE kind='reminder'"), 1)

    async def test_deliveries_recheck_membership_after_removal(self):
        self.app.diary.announce(self.cid, 100, "Объявление", [])
        self.app.diary.remove_member(self.cid, 100, 101)
        await self.app.deliver_pending()
        self.assertNotIn(101, [p["chat_id"] for m, p in self.tg.calls if m == "sendMessage"])

    async def test_retry_after_is_saved_without_duplicate_new_queue_items(self):
        self.app.diary.announce(self.cid, 100, "Объявление", [])
        self.tg.fail_delivery = True
        await self.app.deliver_pending()
        first = self.db.one("SELECT * FROM deliveries ORDER BY id LIMIT 1")
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["attempts"], 1)
        self.assertGreater(first["retry_after"], 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM deliveries"), 3)

    async def test_announcement_publish_needs_permission_and_confirmation(self):
        await self.callback(f"announcement:{self.cid}")
        await self.message("Завтра собрание")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM announcements"), 0)
        state = self.db.state(100)[1]
        await self.callback(f"annpublish:{self.cid}:{state['nonce']}")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM announcements"), 1)
        await self.app.deliver_pending()
        self.assertIn("Завтра собрание", self.texts(101))

    async def test_bot_commands_for_other_bot_are_ignored(self):
        await self.message("/start@another_bot")
        self.assertFalse(self.tg.calls)

    async def test_long_homework_does_not_exceed_telegram_message_limit(self):
        state = await self.start_record()
        await self.message("<&>"*1800)
        await self.callback(f"draft:{self.cid}:{state['nonce']}:preview")
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save")
        await self.callback(f"v:{self.cid}:hw:2026-10-16", uid=101)
        self.assertTrue(all(len(p["text"]) <= 4096 for m, p in self.tg.calls if m == "sendMessage"))

    async def test_shortcut_during_draft_does_not_append_command_as_homework(self):
        await self.start_record()
        await self.message("Текст задания")
        await self.message("/hw")
        self.assertEqual(self.db.state(100)[1]["draft"]["body"], "Текст задания")

    async def test_private_weekend_note_only_triggers_its_owners_reminder(self):
        self.db.run("UPDATE users SET reminder_time='18:00' WHERE id IN (101,102)")
        self.app.diary.save_assignment(self.cid, 101, dict(owner_id=101, source_key="personal", source_position=0,
                                                       assigned_on="2026-10-14", subject="Личное", audience_id=0,
                                                       due_on="2026-10-18", due_position=0, body="Заметка", attachments=[]))
        self.app.schedule_reminders(datetime(2026, 10, 17, 15, 5, tzinfo=timezone.utc))
        self.assertEqual([r["recipient"] for r in self.db.all("SELECT recipient FROM deliveries")], [101])

    async def test_group_migration_preserves_class_binding(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message(group=True, migrate_to_chat_id=-100999)
        self.assertEqual(self.db.scalar("SELECT group_id FROM classes WHERE id=?", (self.cid,)), -100999)

    async def test_nonadmin_cannot_bind_a_group(self):
        self.tg.admins.remove(100)
        token = self.db.scalar("SELECT invite_token FROM classes WHERE id=?", (self.cid,))
        await self.message(f"/start bind_{token}", group=True)
        self.assertIsNone(self.db.scalar("SELECT group_id FROM classes WHERE id=?", (self.cid,)))
        self.assertIn("администратор", self.texts(-100))

    async def test_navigation_edits_one_persistent_message(self):
        await self.message("/start")
        mid = self.app.tg.row(100)["message_id"]
        await self.callback(f"v:{self.cid}:hw:tomorrow")
        await self.callback(f"home:{self.cid}")
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage"]), 1)
        self.assertEqual({p["message_id"] for m, p in self.tg.calls if m == "editMessageText"}, {mid})
        # Restarting the handler must continue editing the same saved panel.
        self.app = Application(self.app.config, self.db, self.tg)
        await self.message("/menu")
        self.assertEqual(self.app.tg.row(100)["message_id"], mid)
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage"]), 1)

    async def test_deleted_panel_is_replaced_once(self):
        await self.message("/start")
        old = self.app.tg.row(100)["message_id"]
        self.tg.edit_error = TelegramError(400, "Bad Request: message to edit not found")
        await self.callback(f"home:{self.cid}")
        replacement = self.app.tg.row(100)["message_id"]
        self.assertNotEqual(old, replacement)
        self.tg.edit_error = None
        await self.callback(f"v:{self.cid}:hw:tomorrow")
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage"]), 2)
        self.assertEqual(self.app.tg.row(100)["message_id"], replacement)

    async def test_unchanged_panel_and_parse_errors_do_not_create_new_messages(self):
        await self.message("/start")
        for error in ("Bad Request: message is not modified", "Bad Request: can't parse entities"):
            self.tg.edit_error = TelegramError(400, error)
            await self.callback(f"home:{self.cid}")
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage"]), 1)

    async def test_old_private_callback_adopts_its_message_on_upgrade(self):
        await self.app.on_update({"callback_query": {"id": "upgrade", "from": {"id": 100},
            "data": f"home:{self.cid}", "message": {"message_id": 543, "text": "Старое меню",
                                                       "chat": {"id": 100, "type": "private"}}}})
        self.assertEqual(self.app.tg.row(100)["message_id"], 543)
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))

    async def test_media_preview_is_removed_on_back_without_deleting_user_upload(self):
        state = await self.start_record()
        await self.message(photo=[{"file_id": "photo"}])
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save")
        aid = self.db.scalar("SELECT id FROM assignments")
        await self.callback(f"task:{self.cid}:{aid}")
        photo_id = json.loads(self.app.tg.row(100)["media_messages"])[0]
        await self.callback(f"v:{self.cid}:hw:2026-10-16")
        deletions = [p["message_id"] for m, p in self.tg.calls if m == "deleteMessage"]
        self.assertEqual(deletions, [photo_id])

    async def test_long_view_pages_reuse_panel_and_recheck_access(self):
        state = await self.start_record()
        await self.message("Очень длинное задание. "*260)
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save")
        await self.callback(f"v:{self.cid}:hw:2026-10-16", uid=101)
        cache = self.app.tg.pages[101]
        self.assertGreater(len(cache["pages"]), 1)
        self.tg.calls.clear()
        await self.callback(f"uipage:{cache['nonce']}:1", uid=101)
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "editMessageText"]), 1)
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))
        self.tg.calls.clear()
        await self.callback(f"uipage:{cache['nonce']}:0", uid=102)
        self.assertFalse(any(m == "editMessageText" for m, p in self.tg.calls))
        self.app.diary.remove_member(self.cid, 100, 101)
        self.tg.calls.clear()
        await self.callback(f"uipage:{cache['nonce']}:0", uid=101)
        self.assertFalse(any(m == "editMessageText" for m, p in self.tg.calls))

    async def test_calendar_selects_dates_across_year_boundary(self):
        await self.callback(f"record:{self.cid}:hw")
        nonce = self.db.state(100)[1]["nonce"]
        await self.callback(f"recorddate:{self.cid}:{nonce}")
        state = self.db.state(100)[1]
        await self.callback(f"cal:{self.cid}:{state['nonce']}:month:2026-12-01")
        self.assertIn("2027-01-01", str(self.tg.calls[-2]))
        await self.callback(f"cal:{self.cid}:{state['nonce']}:month:2027-01-01")
        await self.callback(f"cal:{self.cid}:{state['nonce']}:pick:2027-01-04")
        stage, data = self.db.state(100)
        self.assertEqual(stage, "record_pick")
        self.assertEqual(data["assigned_on"], "2027-01-04")
        self.assertIn("04.01.2027 · Понедельник", self.texts())

    async def test_due_calendar_keeps_draft_and_rejects_date_before_assignment(self):
        await self.start_record()
        await self.message("Текст задания")
        await self.message(document={"file_id": "doc"})
        nonce = self.db.state(100)[1]["nonce"]
        await self.callback(f"draft:{self.cid}:{nonce}:date")
        state = self.db.state(100)[1]
        await self.callback(f"cal:{self.cid}:{state['nonce']}:pick:2026-10-13")
        self.assertEqual(self.db.state(100)[0], "date_picker")
        await self.callback(f"cal:{self.cid}:{state['nonce']}:pick:2026-10-23")
        stage, data = self.db.state(100)
        self.assertEqual(stage, "draft")
        self.assertEqual(data["draft"]["due_on"], "2026-10-23")
        self.assertEqual(data["draft"]["body"], "Текст задания")
        self.assertEqual(data["draft"]["attachments"][0]["file_id"], "doc")
        self.assertEqual(data["draft"]["due_position"], 3)

    async def test_date_calendar_back_returns_to_original_view_date(self):
        await self.callback(f"askdate:{self.cid}:hw:2026-11-03")
        nonce = self.db.state(100)[1]["nonce"]
        await self.callback(f"cal:{self.cid}:{nonce}:back")
        self.assertIn("03.11.2026", self.texts())
        self.assertIsNone(self.db.state(100)[0])

    async def test_week_view_and_day_details_use_selected_week(self):
        await self.callback(f"week:{self.cid}:today")
        self.assertIn("12.10–18.10.2026", self.texts())
        self.assertIn("Воскресенье · 18.10", self.texts())
        await self.callback(f"week:{self.cid}:2026-10-19")
        self.assertIn("19.10–25.10.2026", self.texts())
        await self.callback(f"v:{self.cid}:schedule:2026-10-22")
        self.assertIn("22.10.2026 · Четверг", self.texts())
        self.assertIn(f"week:{self.cid}:2026-10-22", str(self.tg.calls[-2]))

    async def test_natural_group_request_sends_fresh_private_answer(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/start", uid=101)
        mid = self.app.tg.row(101)["message_id"]
        self.tg.calls.clear()
        await self.message("Что задано на завтра?", uid=101, group=True)
        messages = [p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101]
        self.assertEqual(len(messages), 1, "A group request must create a visible new private reply")
        self.assertNotEqual(self.app.tg.row(101)["message_id"], mid)
        self.assertFalse(any(m == "editMessageText" for m, p in self.tg.calls))
        self.assertIn("Общее ДЗ", self.texts(101))
        self.assertIn("Отправил ответ", self.texts(-100))
        self.assertNotIn("Общее ДЗ", self.texts(-100))
        self.tg.calls.clear()
        await self.message("У меня завтра тренировка", uid=101, group=True)
        self.assertFalse(self.tg.calls)

    async def test_group_button_creates_new_private_panel_then_back_edits_it(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/start", uid=101)
        old = self.app.tg.row(101)["message_id"]
        self.tg.calls.clear()
        await self.callback(f"v:{self.cid}:hw:tomorrow", uid=101, group=True)
        private_messages = [p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101]
        self.assertEqual(len(private_messages), 1)
        fresh = self.app.tg.row(101)["message_id"]
        self.assertNotEqual(old, fresh)
        self.assertTrue(any(m == "deleteMessage" and p["chat_id"] == 101 and p["message_id"] == old for m, p in self.tg.calls))
        self.tg.calls.clear()
        await self.callback(f"home:{self.cid}", uid=101)
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))
        self.assertEqual({p["message_id"] for m, p in self.tg.calls if m == "editMessageText"}, {fresh})

    async def test_failed_private_delivery_keeps_previous_panel(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/start", uid=101)
        old = self.app.tg.row(101)["message_id"]
        self.tg.blocked.add(101)
        self.tg.calls.clear()
        await self.callback(f"v:{self.cid}:hw:tomorrow", uid=101, group=True)
        self.assertEqual(self.app.tg.row(101)["message_id"], old)
        self.assertFalse(any(m == "deleteMessage" for m, p in self.tg.calls))
        self.assertIn("Не могу отправить ответ в личку", self.texts(-100))
        self.assertTrue(any(m == "answerCallbackQuery" and p.get("show_alert") for m, p in self.tg.calls))

    async def test_group_private_failure_explains_actual_error_category(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        for error, expected in ((TelegramError(429, "Rate limit", 30), "через 30 сек"),
                                (TelegramError(0, "No network"), "не подтвердил доставку"),
                                (TelegramError(400, "Can't parse entities"), "сформировать или отправить")):
            with self.subTest(code=error.code):
                self.tg.calls.clear()
                self.tg.send_errors[101] = error
                await self.message("/hw", uid=101, group=True)
                self.assertIn(expected, self.texts(-100))
                self.assertNotIn("нажмите /start", self.texts(-100))
                self.assertNotIn("Отправил ответ", self.texts(-100))

    async def test_my_command_in_group_delivers_only_requesters_private_notes(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.app.diary.save_assignment(self.cid, 101, dict(owner_id=101, source_key="note:mine", source_position=0,
            assigned_on="2026-10-14", subject="Моё", audience_id=0, due_on="2026-10-15", due_position=0,
            body="ЛИЧНЫЙ ТЕКСТ", attachments=[], no_homework=False, revision=0))
        await self.message("/my@class_diary_test_bot", uid=101, group=True)
        self.assertIn("ЛИЧНЫЙ ТЕКСТ", self.texts(101))
        self.assertNotIn("ЛИЧНЫЙ ТЕКСТ", self.texts(-100))
        self.assertFalse(any(p.get("chat_id") == 102 for m, p in self.tg.calls if m.startswith("send")))

    async def test_unlisted_group_button_reports_access_error_without_private_delivery(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"v:{self.cid}:hw:tomorrow", uid=200, group=True)
        self.assertIn("Нет доступа к этому классу", self.texts(-100))
        self.assertFalse(any(p.get("chat_id") == 200 for m, p in self.tg.calls if m.startswith("send")))
        self.assertFalse(any("Отправлено новым" in p.get("text", "") for m, p in self.tg.calls if m == "answerCallbackQuery"))

    async def test_group_receipt_failure_does_not_repeat_successful_private_delivery(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.tg.send_errors[-100] = TelegramError(429, "Rate", 30)
        await self.message("/hw", uid=101, group=True)
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101]), 1)
        self.assertIsNotNone(self.app.tg.row(101)["message_id"])

    async def test_group_forum_reply_stays_in_source_topic_and_private_reply_has_no_group_thread(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/menu", group=True, message_thread_id=77, is_topic_message=True)
        menu = next(p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == -100)
        self.assertEqual(menu["message_thread_id"], 77)
        self.assertTrue(menu["reply_parameters"]["allow_sending_without_reply"])
        self.tg.calls.clear()
        await self.message("/hw", uid=101, group=True, message_thread_id=77, is_topic_message=True)
        receipt = next(p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == -100)
        private = next(p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101)
        self.assertEqual(receipt["message_thread_id"], 77)
        self.assertNotIn("message_thread_id", private)

    async def test_long_group_answer_sends_one_private_message_then_pages_edit_it(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        state = await self.start_record()
        await self.message("Очень длинное задание. "*260)
        await self.callback(f"draft:{self.cid}:{state['nonce']}:save")
        await self.message("/start", uid=101)
        old = self.app.tg.row(101)["message_id"]
        self.tg.calls.clear()
        await self.callback(f"v:{self.cid}:hw:2026-10-16", uid=101, group=True)
        panel = self.app.tg.row(101)["message_id"]
        self.assertNotEqual(panel, old)
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101]), 1)
        cache = self.app.tg.pages[101]
        self.assertGreater(len(cache["pages"]), 1)
        self.tg.calls.clear()
        await self.callback(f"uipage:{cache['nonce']}:1", uid=101)
        self.assertEqual({p["message_id"] for m, p in self.tg.calls if m == "editMessageText"}, {panel})
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))

    async def test_old_panel_deletion_failure_does_not_hide_new_private_reply(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/start", uid=101)
        old = self.app.tg.row(101)["message_id"]
        original_call = self.tg.call

        async def call(method, **payload):
            if method == "deleteMessage":
                raise TelegramError(400, "Message can't be deleted")
            return await original_call(method, **payload)

        self.tg.calls.clear()
        with patch.object(self.tg, "call", side_effect=call):
            await self.callback(f"v:{self.cid}:hw:tomorrow", uid=101, group=True)
        self.assertNotEqual(self.app.tg.row(101)["message_id"], old)
        self.assertEqual(len([1 for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == 101]), 1)
        self.assertTrue(any("Отправлено новым" in p.get("text", "") for m, p in self.tg.calls if m == "answerCallbackQuery"))

    async def test_temporary_keyboard_cleanup_error_does_not_fail_successful_request(self):
        with patch.object(self.app.tg, "close_keyboard", side_effect=TelegramError(429, "Rate", 30)):
            await self.message("/start", uid=101)
        self.assertIsNotNone(self.app.tg.row(101)["message_id"])
        self.assertIn("Выберите действие", self.texts(101))
        self.assertIsNone(self.app.tg.context.get())

    async def test_unlisted_group_request_does_not_expose_homework(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("Что задано на завтра?", uid=200, group=True)
        self.assertNotIn("Общее ДЗ", self.texts(200))
        self.assertIn("Открыть бота", str(self.tg.calls))

    async def test_announcement_posts_immediately_with_class_mentions_once(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"announcement:{self.cid}")
        await self.message("Завтра собрание")
        self.db.run("UPDATE users SET name=? WHERE id=101", ("Ира <&>",))
        state = self.db.state(100)[1]
        self.tg.calls.clear()
        await self.callback(f"annpublish:{self.cid}:{state['nonce']}")
        group = [p for m, p in self.tg.calls if m == "sendMessage" and p["chat_id"] == -100]
        self.assertEqual(len(group), 1)
        self.assertIn("Завтра собрание", group[0]["text"])
        self.assertIn('tg://user?id=101">Ира &lt;&amp;&gt;</a>', group[0]["text"])
        self.assertIn("tg://user?id=102", group[0]["text"])
        self.assertNotIn("tg://user?id=200", group[0]["text"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM deliveries WHERE state='pending'"), 3)
        panel = self.app.tg.row(100)["message_id"]
        self.tg.calls.clear()
        await self.app.deliver_pending()
        self.assertFalse(any(m == "editMessageText" for m, p in self.tg.calls))
        self.assertFalse(any(p.get("chat_id") == -100 for m, p in self.tg.calls if m.startswith("send")))
        self.assertEqual(self.app.tg.row(100)["message_id"], panel)

    async def test_back_from_input_cancels_it_without_appending_later_text(self):
        await self.start_record()
        await self.callback(f"manage:{self.cid}")
        self.assertIsNone(self.db.state(100)[0])
        await self.message("Случайный текст")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM assignments"), 0)

    async def test_class_creation_preserves_setup_buttons_in_panel(self):
        await self.callback("create")
        await self.message("10А")
        screens = [p for m, p in self.tg.calls if m in ("sendMessage", "editMessageText")]
        self.assertIn("Класс создан", screens[-1]["text"])
        self.assertIn("Составить своё расписание", str(screens[-1]["reply_markup"]))

    async def test_unbound_group_menu_explains_binding_instead_of_silence(self):
        await self.message("/menu@class_diary_test_bot", group=True)
        self.assertIn("Бот получил сообщение", self.texts(-100))
        self.assertIn("Группа пока не привязана", self.texts(-100))
        self.assertIsNone(self.db.scalar("SELECT group_id FROM classes WHERE id=?", (self.cid,)))

    async def test_unbound_group_natural_request_gets_setup_hint(self):
        await self.message("что задано на завтра", group=True)
        self.assertIn("Привязать группу", self.texts(-100))
        self.assertNotIn("Общее ДЗ", self.texts(-100))

    async def test_unbound_group_chatter_is_ignored(self):
        await self.message("Всем привет", group=True)
        self.assertFalse(self.tg.calls)

    async def test_anonymous_group_command_gets_identity_hint_without_class_access(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/hw@class_diary_test_bot", group=True,
                           sender_chat={"id": -100, "type": "supergroup"},
                           **{"from": {"id": 1087968824, "is_bot": True, "first_name": "Group"}})
        self.assertIn("Переключитесь на свой личный аккаунт", self.texts(-100))
        self.assertFalse(self.db.scalar("SELECT 1 FROM users WHERE id=1087968824"))
        self.assertFalse(any(p.get("chat_id", 0) > 0 for m, p in self.tg.calls if m.startswith("send")))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM message_events"), 0)

    async def test_anonymous_foreign_bot_command_is_ignored(self):
        await self.message("/hw@another_bot", group=True,
                           sender_chat={"id": -100, "type": "supergroup"},
                           **{"from": {"id": 1087968824, "is_bot": True}})
        self.assertFalse(self.tg.calls)

    async def test_real_bot_messages_are_ignored(self):
        await self.message("/hw", group=True, **{"from": {"id": 1234, "is_bot": True}})
        self.assertFalse(self.tg.calls)

    async def test_groupcheck_reports_binding_membership_and_admin_status(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/groupcheck@class_diary_test_bot", uid=101, group=True)
        text = self.texts(-100)
        self.assertIn("Эта команда получена ботом", text)
        self.assertIn("Привязан класс: <b>9Б</b>", text)
        self.assertIn("Вы добавлены в класс", text)
        self.assertIn("Статус бота: администратор", text)
        self.assertIn("Privacy Mode: включён", text)

    async def test_groupcheck_warns_when_privacy_blocks_normal_messages(self):
        self.tg.admins.discard(999)
        await self.message("/groupcheck@class_diary_test_bot", group=True)
        text = self.texts(-100)
        self.assertIn("Группа не привязана", text)
        self.assertIn("Для обычных фраз назначьте бота администратором", text)
        self.assertIn("Privacy Mode: включён", text)

    async def test_groupcheck_disabled_privacy_explains_readding(self):
        self.tg.admins.discard(999)
        self.tg.privacy_disabled = True
        await self.message("/groupcheck@class_diary_test_bot", group=True)
        self.assertIn("Privacy Mode: выключен", self.texts(-100))
        self.assertIn("удалите и снова добавьте", self.texts(-100))

    async def test_group_request_with_bot_prefix_and_comma_is_handled(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("@class_diary_test_bot, что задано на завтра?", uid=101, group=True)
        self.assertIn("Общее ДЗ", self.texts(101))

    async def test_group_command_accepts_newline_and_case_insensitive_bot_name(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/MENU@CLASS_DIARY_TEST_BOT\n", group=True)
        self.assertIn("ДЗ и расписание — в личку", self.texts(-100))

    async def test_group_logs_receipt_without_recording_homework_text(self):
        with self.assertLogs("classdiary.app", level="INFO") as capture:
            await self.message("Конфиденциальное сообщение", group=True)
        self.assertIn("Group message received: chat_id=-100", "\n".join(capture.output))
        self.assertNotIn("Конфиденциальное", "\n".join(capture.output))

    async def test_statistics_and_interactive_commands_reply_in_group_even_if_private_chat_blocked(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.tg.blocked.add(101)
        for command, title in (("/stats", "статистика"), ("/pairs", "Парные огоньки"),
                               ("/interactive", "Парные огоньки"), ("моя статистика", "статистика"), ("огоньки", "Парные огоньки")):
            with self.subTest(command=command):
                self.tg.calls.clear()
                await self.message(command, uid=101, group=True)
                messages = [p for m, p in self.tg.calls if m == "sendMessage"]
                self.assertTrue(messages)
                self.assertTrue(all(p["chat_id"] == -100 for p in messages))
                self.assertIn(title, self.texts(-100))
                self.assertNotIn("Отправил ответ вам в личку", self.texts(-100))

    async def test_group_menu_statistics_and_pair_buttons_send_no_private_messages(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        for kind in ("stats", "pairs"):
            with self.subTest(kind=kind):
                self.tg.calls.clear()
                await self.callback(f"v:{self.cid}:{kind}:today", uid=101, group=True)
                self.assertTrue(any(m == "sendMessage" and p["chat_id"] == -100 for m, p in self.tg.calls))
                self.assertFalse(any(p.get("chat_id", -1) > 0 for m, p in self.tg.calls if m.startswith(("send", "edit", "delete"))))

    async def test_group_interactive_keeps_private_draft_and_panel_untouched(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.app.tg.remember(101, message_id=111, media_messages="[112]", temporary_messages="[113]")
        draft = {"cid": self.cid, "draft": {"body": "ЛИЧНЫЙ ЧЕРНОВИК"}}
        self.db.set_state(101, "draft", draft)
        before = self.app.tg.row(101)
        await self.message("/stats", uid=101, group=True)
        await self.callback(f"g:{self.cid}:101:p:0", uid=101, group=True)
        self.assertEqual(self.app.tg.row(101), before)
        self.assertEqual(self.db.state(101), ("draft", draft))
        self.assertFalse(any(p.get("chat_id", -1) > 0 for m, p in self.tg.calls if m.startswith(("send", "edit", "delete"))))
        self.assertNotIn("ЛИЧНЫЙ ЧЕРНОВИК", self.texts(-100))

    async def test_group_interactive_refresh_edits_its_group_message(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/stats", uid=101, group=True)
        self.tg.calls.clear()
        await self.callback(f"g:{self.cid}:101:s", uid=101, group=True, message_id=45)
        edits = [p for m, p in self.tg.calls if m == "editMessageText"]
        self.assertEqual({(p["chat_id"], p["message_id"]) for p in edits}, {(-100, 45)})
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))
        self.assertIn("Участник: <b>Ученик 101</b>", self.texts(-100))

    async def test_group_interactive_buttons_cannot_be_used_by_another_actor(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"g:{self.cid}:101:c:0", uid=102, group=True)
        self.assertFalse(any(m in ("sendMessage", "editMessageText") for m, p in self.tg.calls))
        self.assertTrue(any("другого участника" in p.get("text", "") for m, p in self.tg.calls if m == "answerCallbackQuery"))

    async def test_pair_invitation_and_consent_happen_entirely_in_group(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.message("/pairs", uid=101, group=True)
        await self.callback(f"g:{self.cid}:101:c:0", uid=101, group=True)
        self.tg.calls.clear()
        await self.callback(f"g:{self.cid}:101:r:102", uid=101, group=True)
        pair = self.db.one("SELECT * FROM pairs")
        self.assertEqual(pair["accepted"], 0)
        invitation = next(p for m, p in self.tg.calls if m == "sendMessage")
        self.assertEqual(invitation["chat_id"], -100)
        self.assertIn("tg://user?id=102", invitation["text"])
        accept = f"g:{self.cid}:102:a:{pair['id']}:1"
        self.assertIn(accept, str(invitation["reply_markup"]))
        self.assertFalse(any(p.get("chat_id", -1) > 0 for m, p in self.tg.calls if m.startswith(("send", "edit"))))
        await self.callback(accept, uid=100, group=True, message_id=2)
        self.assertEqual(self.db.scalar("SELECT accepted FROM pairs"), 0)
        self.tg.calls.clear()
        await self.callback(accept, uid=102, group=True, message_id=2)
        self.assertEqual(self.db.scalar("SELECT accepted FROM pairs"), 1)
        self.assertTrue(any(m == "editMessageText" and p["chat_id"] == -100 and p["message_id"] == 2 for m, p in self.tg.calls))
        self.assertFalse(any(p.get("chat_id", -1) > 0 for m, p in self.tg.calls if m.startswith(("send", "edit"))))

    async def test_repeated_group_pair_request_does_not_duplicate_invitation(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        await self.callback(f"g:{self.cid}:101:r:102", uid=101, group=True)
        self.tg.calls.clear()
        await self.callback(f"g:{self.cid}:101:r:102", uid=101, group=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 1)
        self.assertFalse(any(m == "sendMessage" for m, p in self.tg.calls))

    async def test_group_pair_decline_and_remove_are_authorized(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        pid = self.app.diary.request_pair(self.cid, 101, 102)
        await self.callback(f"g:{self.cid}:102:a:{pid}:0", uid=102, group=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 0)
        pid = self.app.diary.request_pair(self.cid, 101, 102)
        self.app.diary.answer_pair(self.cid, 102, pid, True)
        await self.callback(f"g:{self.cid}:100:x:{pid}", uid=100, group=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 1)
        await self.callback(f"g:{self.cid}:101:x:{pid}", uid=101, group=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 0)

    async def test_foreign_or_unlisted_group_interactive_requests_are_rejected(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        other = self.app.diary.create_class(200, "Другой класс", "UTC")
        await self.callback(f"v:{other}:stats:today", uid=200, group=True)
        await self.callback(f"g:{other}:200:s", uid=200, group=True)
        await self.callback(f"v:{self.cid}:stats:today", uid=200, group=True)
        self.assertFalse(any(m in ("sendMessage", "editMessageText") for m, p in self.tg.calls))

    async def test_disabled_interactive_rejects_old_group_buttons_and_hides_menu_entries(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        self.db.run("UPDATE classes SET interactive=0 WHERE id=?", (self.cid,))
        await self.message("/menu", group=True)
        menu = next(p for m, p in self.tg.calls if m == "sendMessage")
        self.assertNotIn(f"v:{self.cid}:stats", str(menu["reply_markup"]))
        self.assertNotIn(f"v:{self.cid}:pairs", str(menu["reply_markup"]))
        self.tg.calls.clear()
        await self.callback(f"g:{self.cid}:101:r:102", uid=101, group=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM pairs"), 0)
        self.assertFalse(any(m in ("sendMessage", "editMessageText") for m, p in self.tg.calls))

    async def test_pair_group_pages_remain_bounded_and_in_source_topic(self):
        self.app.diary.bind_group(self.cid, 100, -100, "9Б")
        for uid in range(300, 309):
            self.app.diary.touch_user({"id": uid, "first_name": "&"*150}, started=True)
            self.app.diary.add_members(self.cid, 100, [{"user_id": uid}])
            self.app.diary.request_pair(self.cid, 101, uid)
        await self.message("/pairs", uid=101, group=True, message_thread_id=77)
        screen = next(p for m, p in self.tg.calls if m == "sendMessage")
        self.assertEqual(screen["message_thread_id"], 77)
        self.assertLessEqual(len(screen["text"]), 3800)
        self.assertIn(f"g:{self.cid}:101:p:1", str(screen["reply_markup"]))
        self.tg.calls.clear()
        await self.callback(f"g:{self.cid}:101:p:1", uid=101, group=True)
        self.assertTrue(any(m == "editMessageText" and p["chat_id"] == -100 for m, p in self.tg.calls))

    async def test_interactive_control_data_fits_telegram_limit_for_large_ids(self):
        from classdiary.group_interactive import GroupInteractive
        data = GroupInteractive.data(2**63-1, 2**52-1, "a", 2**63-1, 1)
        self.assertLessEqual(len(data.encode()), 64)
