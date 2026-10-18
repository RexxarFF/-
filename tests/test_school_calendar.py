from datetime import date
import unittest

from classdiary.db import Database
from classdiary.schedule import homework_blocks
from classdiary.service import AccessDenied, Diary
from classdiary.ui import homework_text


class SchoolCalendarTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.diary = Diary(self.db)
        for uid in (100, 101, 200):
            self.diary.touch_user({"id": uid, "first_name": str(uid)}, started=True)
        self.cid = self.diary.create_class(100, "9Б", "Europe/Moscow")
        self.other = self.diary.create_class(200, "Другой", "UTC")
        self.diary.add_members(self.cid, 100, [{"user_id": 101}])
        for day in (0, 2, 4, 5):
            self.diary.replace_weekday(self.cid, 100, day, [{"position": 1, "subject": "Алгебра"}])

    def tearDown(self):
        self.db.close()

    def test_saturday_and_sunday_target_monday_and_skip_empty_weekdays(self):
        for today in (date(2026, 10, 17), date(2026, 10, 18)):
            self.assertEqual(self.diary.resolve_school_date(self.cid, "tomorrow", today), date(2026, 10, 19))
        self.assertEqual(self.diary.resolve_school_date(self.cid, "Завтра", date(2026, 10, 19)), date(2026, 10, 21))
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 19), -1), date(2026, 10, 17))

    def test_sunday_rows_in_legacy_database_are_ignored_and_cannot_be_edited(self):
        self.db.run("INSERT INTO lessons(class_id,weekday,position,subject) VALUES(?,6,1,'Старый воскресный урок')", (self.cid,))
        self.db.run("INSERT INTO day_overrides VALUES(?,?,?)", (self.cid, "2026-10-18", '[{"subject":"Воскресный урок"}]'))
        self.assertEqual(self.diary.lessons_on(self.cid, date(2026, 10, 18)), [])
        with self.assertRaises(ValueError):
            self.diary.replace_weekday(self.cid, 100, 6, [])
        with self.assertRaises(ValueError):
            self.diary.set_override(self.cid, 100, date(2026, 10, 18), [])

    def test_weekend_setting_preserves_template_and_skips_saturday(self):
        self.diary.toggle_school_weekend(self.cid, 100, 5)
        self.assertEqual(self.diary.lessons_on(self.cid, date(2026, 10, 17)), [])
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 16)), date(2026, 10, 19))
        self.diary.toggle_school_weekend(self.cid, 100, 5)
        self.assertEqual(self.diary.lessons_on(self.cid, date(2026, 10, 17))[0]["subject"], "Алгебра")

    def test_break_is_inclusive_and_next_lesson_skips_it(self):
        start, end = date(2026, 10, 19), date(2026, 10, 24)
        bid = self.diary.add_school_break(self.cid, 100, start, end, "Осенние каникулы")
        for day in (start, date(2026, 10, 21), end):
            self.assertEqual(self.diary.lessons_on(self.cid, day), [])
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 17)), date(2026, 10, 26))
        self.assertEqual(self.diary.next_lesson(self.cid, date(2026, 10, 17), "Алгебра", 0), (date(2026, 10, 26), 1))
        self.diary.remove_school_break(self.cid, 100, bid)
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 17)), start)

    def test_overlapping_breaks_and_single_day_off(self):
        self.diary.add_school_break(self.cid, 100, date(2026, 10, 19), date(2026, 10, 21))
        self.diary.add_school_break(self.cid, 100, date(2026, 10, 21), date(2026, 10, 24))
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 17)), date(2026, 10, 26))
        self.diary.add_school_break(self.cid, 100, date(2026, 10, 26), date(2026, 10, 26), "Праздник")
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 17)), date(2026, 10, 28))

    def test_specific_replacement_is_included_and_empty_replacement_is_skipped(self):
        self.diary.set_override(self.cid, 100, date(2026, 10, 19), [])
        self.diary.set_override(self.cid, 100, date(2026, 10, 20), [{"position": 1, "subject": "Химия"}])
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 10, 17)), date(2026, 10, 20))

    def test_year_boundary_and_empty_class_still_have_no_sunday_target(self):
        self.diary.replace_weekday(self.cid, 100, 3, [{"position": 1, "subject": "Алгебра"}])
        self.diary.add_school_break(self.cid, 100, date(2026, 12, 31), date(2027, 1, 9))
        self.assertEqual(self.diary.next_school_day(self.cid, date(2026, 12, 30)), date(2027, 1, 11))
        self.assertEqual(self.diary.next_school_day(self.other, date(2026, 10, 17)), date(2026, 10, 19))

    def test_calendar_permissions_class_isolation_and_invalid_ranges(self):
        with self.assertRaises(AccessDenied):
            self.diary.toggle_school_weekend(self.cid, 101, 5)
        with self.assertRaises(AccessDenied):
            self.diary.add_school_break(self.cid, 101, date(2026, 10, 19), date(2026, 10, 20))
        bid = self.diary.add_school_break(self.cid, 100, date(2026, 10, 19), date(2026, 10, 20))
        with self.assertRaises(ValueError):
            self.diary.remove_school_break(self.other, 200, bid)
        for start, end in ((date(2026, 10, 20), date(2026, 10, 19)), (date(2026, 1, 1), date(2028, 1, 1))):
            with self.assertRaises(ValueError):
                self.diary.add_school_break(self.cid, 100, start, end)
        for i in range(5):
            self.diary.toggle_school_weekend(self.cid, 100, i)
        with self.assertRaises(ValueError):
            self.diary.toggle_school_weekend(self.cid, 100, 5)

    def test_exemptions_are_class_scoped_and_apply_to_both_subgroups(self):
        self.diary.replace_weekday(self.cid, 100, 0, [{"position": 1, "subject": "Английский", "audience": "Группа 1"},
                                                   {"position": 1, "subject": "Английский", "audience": "Группа 2"}])
        self.diary.toggle_homework_exemption(self.cid, 100, "Английский")
        progress = self.diary.homework_progress(self.cid, 100, date(2026, 10, 19))
        self.assertEqual((progress["filled"], progress["total"]), (2, 2))
        self.assertEqual(self.diary.exempt_subjects(self.other), [])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM assignments"), 0)
        with self.assertRaises(AccessDenied):
            self.diary.toggle_homework_exemption(self.cid, 101, "Английский")

    def test_manual_homework_wins_over_exemption_and_policy_can_be_removed(self):
        self.diary.toggle_homework_exemption(self.cid, 100, "Алгебра")
        self.diary.save_assignment(self.cid, 100, dict(owner_id=0, source_key="l:test", source_position=1,
            assigned_on="2026-10-17", subject="Алгебра", audience_id=0, due_on="2026-10-19", due_position=1,
            body="Решить № 25", attachments=[], no_homework=False, revision=0))
        rows = self.diary.assignments_on(self.cid, 101, date(2026, 10, 19))
        text = homework_text({"name": "9Б"}, date(2026, 10, 19), rows,
            self.diary.lessons_on(self.cid, date(2026, 10, 19)), False, self.diary.exempt_subjects(self.cid))
        self.assertIn("Решить № 25", text)
        self.assertNotIn("ДЗ не задают", text)
        self.diary.toggle_homework_exemption(self.cid, 100, "АЛГЕБРА")
        self.assertEqual(self.diary.exempt_subjects(self.cid), [])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM assignments"), 1)

    def test_three_states_and_reference_format_have_no_internal_ids_or_lesson_numbers(self):
        day = date(2026, 10, 19)
        self.diary.replace_weekday(self.cid, 100, 0, [{"position": 1, "subject": "История"},
            {"position": 2, "subject": "Биология"}, {"position": 3, "subject": "Физкультура"}])
        self.diary.toggle_homework_exemption(self.cid, 100, "Физкультура")
        aid = self.diary.save_assignment(self.cid, 100, dict(owner_id=0, source_key="l:test", source_position=1,
            assigned_on="2026-10-17", subject="История", audience_id=0, due_on="2026-10-19", due_position=1,
            body="Стр. 139–141 <прочитать>", attachments=[], no_homework=False, revision=0))
        rows, lessons = self.diary.assignments_on(self.cid, 101, day), self.diary.lessons_on(self.cid, day)
        text = homework_text({"name": "9Б"}, day, rows, lessons, False, self.diary.exempt_subjects(self.cid))
        self.assertIn("<blockquote><b>История</b>", text)
        self.assertIn("(19.10, Пн)", text)
        self.assertIn("&lt;прочитать&gt;", text)
        self.assertIn("ДЗ не задают", text)
        self.assertIn("ДЗ ещё не записано", text)
        self.assertNotIn(f"запись #{aid}", text)
        self.assertNotIn("урок №", text)
        progress = self.diary.homework_progress(self.cid, 100, day)
        self.assertEqual((progress["filled"], progress["total"]), (2, 3))

    def test_repeated_lesson_with_unspecified_slot_does_not_duplicate_assignment_body(self):
        lessons = [dict(position=i, subject="Алгебра", audience_id=0, audience="") for i in (1, 2)]
        assignment = dict(id=1, subject="Алгебра", audience_id=0, due_position=0, due_on="2026-10-19", body="Уникальный текст", attachments="[]")
        blocks = homework_blocks([assignment], lessons)
        self.assertTrue(all(b["status"] == "recorded" for b in blocks))
        text = homework_text({"name": "9Б"}, date(2026, 10, 19), [assignment], lessons, False)
        self.assertEqual(text.count("Уникальный текст"), 1)

    def test_adding_break_preserves_existing_homework_dates(self):
        self.diary.save_assignment(self.cid, 100, dict(owner_id=0, source_key="l:test", source_position=1,
            assigned_on="2026-10-17", subject="Алгебра", audience_id=0, due_on="2026-10-19", due_position=1,
            body="№ 25", attachments=[], no_homework=False, revision=0))
        self.diary.add_school_break(self.cid, 100, date(2026, 10, 19), date(2026, 10, 24))
        self.assertEqual(self.db.scalar("SELECT due_on FROM assignments"), "2026-10-19")
        self.assertEqual(len(self.diary.assignments_on(self.cid, 101, date(2026, 10, 19))), 1)
