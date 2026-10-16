from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from classdiary.config import ROOT, get_timezone
from classdiary.db import Database
from classdiary.schedule import parse_date, parse_day, streak, validate_lessons
from classdiary.service import AccessDenied, Conflict, Diary
from classdiary.ui import chunks, homework_text, text_lines


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.diary = Diary(self.db)
        for uid in (100, 101, 102, 200):
            self.diary.touch_user({"id": uid, "first_name": f"User {uid}"}, started=True)
        self.cid = self.diary.create_class(100, "9Б", "Europe/Moscow")
        self.other = self.diary.create_class(200, "9Б", "UTC")
        self.diary.add_members(self.cid, 100, [{"user_id": 101}, {"user_id": 102}])

    def tearDown(self):
        self.db.close()

    def lesson(self, weekday, subject="Алгебра", position=1, audience=""):
        rows = self.db.all("SELECT l.*,COALESCE(a.name,'') AS audience FROM lessons l LEFT JOIN audiences a ON a.id=l.audience_id "
                           "WHERE l.class_id=? AND l.weekday=? AND l.archived=0 ORDER BY position", (self.cid, weekday))
        specs = [dict(position=r["position"], subject=r["subject"], audience=r["audience"]) for r in rows]
        specs.append(dict(position=position, subject=subject, audience=audience))
        self.diary.replace_weekday(self.cid, 100, weekday, specs)
        return next(r for r in self.diary.lessons_on(self.cid, date(2026, 10, 12)+timedelta(days=weekday))
                    if r["position"] == position and r["audience"] == audience)

    def draft(self, owner=0, key="l:test", subject="Алгебра", group=0, due="2026-10-16", position=1):
        return dict(owner_id=owner, source_key=key, source_position=position, assigned_on="2026-10-14", subject=subject,
                    audience_id=group, due_on=due, due_position=1, body="№ 125", attachments=[], no_homework=False, revision=0)

    def test_identical_class_names_are_isolated(self):
        self.assertNotEqual(self.cid, self.other)
        self.assertEqual(len(self.diary.classrooms(100)), 1)
        with self.assertRaises(AccessDenied):
            self.diary.require(self.other, 100)

    def test_member_cannot_add_people(self):
        with self.assertRaises(AccessDenied):
            self.diary.add_members(self.cid, 101, [{"user_id": 900}])

    def test_pending_member_is_activated_only_by_starting(self):
        self.diary.add_members(self.cid, 100, [{"user_id": 300, "first_name": "Новичок"}])
        with self.assertRaises(AccessDenied):
            self.diary.require(self.cid, 300)
        self.diary.touch_user({"id": 300, "first_name": "Новичок"}, started=True)
        self.assertEqual(self.diary.require(self.cid, 300)["id"], self.cid)

    def test_roles_union_and_revoke(self):
        r1 = self.diary.save_role(self.cid, 100, "ДЗ", ["homework"])
        r2 = self.diary.save_role(self.cid, 100, "Вести расписание", ["schedule"])
        self.diary.toggle_role(self.cid, 100, 101, r1)
        self.diary.toggle_role(self.cid, 100, 101, r2)
        self.assertEqual(self.diary.permissions(self.cid, 101), {"homework", "schedule"})
        self.diary.delete_role(self.cid, 100, r1)
        self.assertEqual(self.diary.permissions(self.cid, 101), {"schedule"})

    def test_cannot_assign_role_from_another_class(self):
        role = self.diary.save_role(self.other, 200, "Админ", ["members"])
        with self.assertRaises(AccessDenied):
            self.diary.toggle_role(self.cid, 100, 101, role)

    def test_removing_member_revokes_roles_and_private_records(self):
        role = self.diary.save_role(self.cid, 100, "ДЗ", ["homework"])
        self.diary.toggle_role(self.cid, 100, 101, role)
        self.diary.save_assignment(self.cid, 101, self.draft(owner=101))
        self.diary.remove_member(self.cid, 100, 101)
        self.assertEqual(self.diary.permissions(self.cid, 101), set())
        self.assertFalse(self.db.scalar("SELECT 1 FROM assignments WHERE owner_id=101"))
        self.diary.add_members(self.cid, 100, [{"user_id": 101}])
        self.assertEqual(self.diary.permissions(self.cid, 101), set())

    def test_owner_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            self.diary.remove_member(self.cid, 100, 100)

    def test_regular_student_cannot_write_common_homework(self):
        with self.assertRaises(AccessDenied):
            self.diary.save_assignment(self.cid, 101, self.draft())

    def test_personal_and_common_are_separate(self):
        common = self.diary.save_assignment(self.cid, 100, self.draft())
        private = self.diary.save_assignment(self.cid, 101, self.draft(owner=101))
        shared = self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))
        own = self.diary.assignments_on(self.cid, 101, date(2026, 10, 16), personal=True)
        self.assertEqual([r["id"] for r in shared], [common])
        self.assertEqual([r["id"] for r in own], [private])
        self.assertEqual(self.diary.assignments_on(self.cid, 102, date(2026, 10, 16), personal=True), [])

    def test_owner_cannot_read_other_students_private_note(self):
        aid = self.diary.save_assignment(self.cid, 101, self.draft(owner=101))
        with self.assertRaises(AccessDenied):
            self.diary.assignment(self.cid, 100, aid)

    def test_cross_class_assignment_ids_are_rejected(self):
        aid = self.diary.save_assignment(self.cid, 100, self.draft())
        with self.assertRaises(AccessDenied):
            self.diary.assignment(self.other, 200, aid)

    def test_forged_personal_owner_is_rejected(self):
        with self.assertRaises(AccessDenied):
            self.diary.save_assignment(self.cid, 101, self.draft(owner=102))

    def test_conflicting_curator_edits_do_not_overwrite(self):
        draft = self.draft()
        aid = self.diary.save_assignment(self.cid, 100, draft)
        with self.assertRaises(Conflict):
            self.diary.save_assignment(self.cid, 100, draft)
        current = self.diary.assignment(self.cid, 100, aid)
        draft.update(revision=current["revision"], body="Исправлено")
        self.diary.save_assignment(self.cid, 100, draft)
        with self.assertRaises(Conflict):
            self.diary.save_assignment(self.cid, 100, draft)

    def test_assignments_from_repeated_lessons_do_not_overwrite(self):
        a = self.diary.save_assignment(self.cid, 100, self.draft(key="lesson:2", position=2))
        b = self.diary.save_assignment(self.cid, 100, self.draft(key="lesson:5", position=5))
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))), 2)

    def test_next_lesson_uses_future_date_not_second_lesson_today(self):
        self.lesson(2, position=1)
        self.lesson(2, position=3)
        self.lesson(4, position=5)
        due, position = self.diary.next_lesson(self.cid, date(2026, 10, 14), "Алгебра", 0)
        self.assertEqual((due, position), (date(2026, 10, 16), 5))

    def test_next_lesson_obeys_subgroup(self):
        g1 = self.lesson(2, "Английский", 1, "Группа 1")["audience_id"]
        self.lesson(3, "Английский", 1, "Группа 2")
        self.lesson(5, "Английский", 1, "Группа 1")
        due, _ = self.diary.next_lesson(self.cid, date(2026, 10, 14), "Английский", g1)
        self.assertEqual(due, date(2026, 10, 17))

    def test_holiday_override_is_skipped(self):
        self.lesson(3)
        self.lesson(4)
        self.diary.set_override(self.cid, 100, date(2026, 10, 15), [])
        self.assertEqual(self.diary.next_lesson(self.cid, date(2026, 10, 14), "Алгебра", 0)[0], date(2026, 10, 16))

    def test_next_lesson_without_schedule_returns_none(self):
        self.assertEqual(self.diary.next_lesson(self.cid, date(2026, 10, 14), "Алгебра", 0), (None, 0))

    def test_replacing_schedule_preserves_unchanged_lesson_identity(self):
        lesson = self.lesson(2)
        self.diary.replace_weekday(self.cid, 100, 2, [{"position": 1, "subject": "Алгебра", "starts": "09:00", "ends": "09:45"}])
        self.assertEqual(self.diary.lessons_on(self.cid, date(2026, 10, 14))[0]["source_key"], lesson["source_key"])

    def test_changing_schedule_does_not_move_saved_due_dates(self):
        self.lesson(4)
        self.diary.save_assignment(self.cid, 100, self.draft())
        self.diary.replace_weekday(self.cid, 100, 4, [])
        self.assertEqual(len(self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))), 1)

    def test_subgroup_filter_does_not_hide_common_lessons(self):
        self.lesson(2, position=1)
        g1 = self.lesson(2, "Английский", 2, "Группа 1")["audience_id"]
        self.lesson(2, "Информатика", 2, "Группа 2")
        self.diary.toggle_audience(self.cid, 101, g1)
        visible = self.diary.visible_lessons(self.cid, 101, date(2026, 10, 14))
        self.assertEqual([r["subject"] for r in visible], ["Алгебра", "Английский"])

    def test_subgroup_homeworks_are_separate(self):
        g1 = self.lesson(2, "Английский", 1, "Группа 1")["audience_id"]
        g2 = self.lesson(2, "Английский", 1, "Группа 2")["audience_id"]
        a = self.diary.save_assignment(self.cid, 100, self.draft(key="g1", group=g1))
        self.diary.save_assignment(self.cid, 100, self.draft(key="g2", group=g2))
        self.diary.toggle_audience(self.cid, 101, g1)
        self.assertEqual([r["id"] for r in self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))], [a])

    def test_completion_is_personal_and_resets_on_edit(self):
        draft = self.draft()
        aid = self.diary.save_assignment(self.cid, 100, draft)
        self.diary.toggle_done(self.cid, 101, aid)
        self.assertEqual(self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))[0]["done"], 1)
        self.assertEqual(self.diary.assignments_on(self.cid, 102, date(2026, 10, 16))[0]["done"], 0)
        draft.update(revision=1, body="Другое задание")
        self.diary.save_assignment(self.cid, 100, draft)
        self.assertEqual(self.diary.assignments_on(self.cid, 101, date(2026, 10, 16))[0]["done"], 0)

    def test_nothing_assigned_is_explicit(self):
        draft = self.draft()
        draft.update(body="", no_homework=True)
        aid = self.diary.save_assignment(self.cid, 100, draft)
        self.assertEqual(self.diary.assignment(self.cid, 101, aid)["body"], "Ничего не задано")

    def test_one_group_cannot_be_stolen_by_another_class(self):
        self.diary.bind_group(self.cid, 100, -100, "Класс")
        with self.assertRaises(Conflict):
            self.diary.bind_group(self.other, 200, -100, "Класс")

    def test_message_duplicate_is_not_counted_twice(self):
        self.diary.bind_group(self.cid, 100, -100, "Класс")
        for _ in range(2):
            self.diary.count_message(-100, 10, 101, date(2026, 10, 16))
        self.assertEqual(self.diary.stats(self.cid, 101, date(2026, 10, 16))["total"], 1)

    def test_unlisted_group_user_is_not_counted(self):
        self.diary.bind_group(self.cid, 100, -100, "Класс")
        self.diary.count_message(-100, 10, 200, date(2026, 10, 16))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM message_events"), 0)

    def test_pair_requires_consent_of_other_person(self):
        pid = self.diary.request_pair(self.cid, 101, 102)
        with self.assertRaises(AccessDenied):
            self.diary.answer_pair(self.cid, 101, pid, True)
        self.diary.answer_pair(self.cid, 102, pid, True)
        self.assertEqual(self.diary.pairs(self.cid, 101, date(2026, 10, 16))[0]["accepted"], 1)

    def test_pair_streak_requires_messages_from_both(self):
        self.diary.bind_group(self.cid, 100, -100, "Класс")
        pid = self.diary.request_pair(self.cid, 101, 102)
        self.diary.answer_pair(self.cid, 102, pid, True)
        self.diary.count_message(-100, 1, 101, date(2026, 10, 15))
        self.diary.count_message(-100, 2, 102, date(2026, 10, 15))
        self.diary.count_message(-100, 3, 101, date(2026, 10, 16))
        self.assertEqual(self.diary.pairs(self.cid, 101, date(2026, 10, 16))[0]["streak"], 1)

    def test_announcement_delivery_is_queued_once(self):
        aid = self.diary.announce(self.cid, 100, "Завтра собрание", [])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM deliveries"), 3)
        self.diary.ack(self.cid, 101, aid)
        self.diary.ack(self.cid, 101, aid)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM announcement_reads"), 1)

    def test_cross_class_announcement_ack_is_rejected(self):
        aid = self.diary.announce(self.cid, 100, "Собрание", [])
        with self.assertRaises(AccessDenied):
            self.diary.ack(self.other, 200, aid)

    def test_backup_and_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "copy.sqlite3"
            self.db.set_state(101, "draft", {"test": "Привет"})
            self.db.backup(path)
            copy = Database(path)
            try:
                self.assertEqual(copy.state(101), ("draft", {"test": "Привет"}))
                self.assertEqual(copy.scalar("SELECT COUNT(*) FROM classes"), 2)
            finally:
                copy.close()


class ParsingTests(unittest.TestCase):
    def test_schedule_parser_keeps_duplicate_subjects(self):
        rows = parse_day("2; Алгебра; ; 09:00-09:45\n5; Алгебра; ; 12:15-13:00")
        self.assertEqual([r["position"] for r in rows], [2, 5])

    def test_schedule_rejects_duplicate_slot_and_audience(self):
        with self.assertRaises(ValueError):
            parse_day("1; Алгебра\n1; Геометрия")

    def test_schedule_rejects_all_class_and_subgroup_collision(self):
        with self.assertRaises(ValueError):
            parse_day("1; Английский\n1; Информатика; Группа 2")

    def test_schedule_accepts_two_subgroups_in_one_slot(self):
        self.assertEqual(len(parse_day("1; Английский; Группа 1\n1; Информатика; Группа 2")), 2)

    def test_times_are_validated(self):
        for text in ("1; Алгебра; ; 25:00-26:00", "1; Алгебра; ; 11:00-10:00"):
            with self.assertRaises(ValueError):
                parse_day(text)

    def test_date_formats(self):
        today = date(2026, 10, 16)
        for text in ("16.10.2026", "2026-10-16", "16.10", "сегодня"):
            self.assertEqual(parse_date(text, today), today)
        self.assertEqual(parse_date("завтра", today), date(2026, 10, 17))

    def test_short_leap_day_uses_current_year(self):
        self.assertEqual(parse_date("29.02", date(2028, 1, 1)), date(2028, 2, 29))

    def test_timezone_offsets(self):
        self.assertEqual(get_timezone("+03:00").utcoffset(None), timedelta(hours=3))
        with self.assertRaises(ValueError):
            get_timezone("+14:30")

    def test_streak_grace_until_end_of_today(self):
        self.assertEqual(streak(["2026-10-14", "2026-10-15"], date(2026, 10, 16)), 2)
        self.assertEqual(streak(["2026-10-14"], date(2026, 10, 16)), 0)

    def test_html_is_escaped_and_long_lines_are_safe(self):
        text = text_lines("<&>"*2000)
        self.assertNotIn("<&>", text)
        self.assertTrue(all(len(part) <= 3800 for part in chunks(text)))

    def test_photo_template_transcription(self):
        template = json.loads((ROOT / "templates" / "9b.json").read_text(encoding="utf-8"))
        self.assertTrue(template["group_mapping_needs_review"])
        for rows in template["week"].values():
            validate_lessons(rows)
        tuesday = template["week"]["1"]
        self.assertEqual([r["position"] for r in tuesday if r["subject"] == "Алгебра"], [2, 5])
        self.assertEqual([r["position"] for r in template["week"]["3"] if r["subject"] == "Геометрия"], [5, 6])
        self.assertEqual(min(r["position"] for r in template["week"]["4"]), 2)
        self.assertEqual(min(r["position"] for r in template["week"]["5"]), 2)
        self.assertEqual(template["week"]["0"][0]["starts"], "08:10")
        self.assertEqual(template["week"]["6"], [])
