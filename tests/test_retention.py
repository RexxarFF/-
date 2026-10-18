from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from classdiary.db import Database
from classdiary.service import Diary


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.diary = Diary(self.db)
        for uid in (100, 101, 200):
            self.diary.touch_user({"id": uid, "first_name": f"User {uid}"}, started=True)
        self.cid = self.diary.create_class(100, "9Б", "Europe/Moscow")
        self.diary.add_members(self.cid, 100, [{"user_id": 101}])

    def tearDown(self):
        self.db.close()

    def task(self, key, due="2026-10-27", created="2026-10-14 12:00:00", owner=0, cid=None, actor=None):
        cid = cid or self.cid
        actor = actor or owner or 100
        aid = self.diary.save_assignment(cid, actor, dict(owner_id=owner, source_key=key, source_position=1,
            assigned_on="2026-10-01", subject="Алгебра", audience_id=0, due_on=due, due_position=1,
            body="Задание", attachments=[{"type": "photo", "file_id": "file"}], no_homework=False, revision=0))
        self.db.run("UPDATE assignments SET created_at=? WHERE id=?", (created, aid))
        return aid

    def test_two_week_boundary_keeps_recent_today_and_future_homework(self):
        old = self.task("old")
        recent = self.task("recent", created="2026-10-14 12:00:01")
        today = self.task("today", due="2026-10-28")
        future = self.task("future", due="2026-11-05")
        private = self.task("private", owner=101)
        self.diary.toggle_done(self.cid, 101, old)
        self.diary.toggle_done(self.cid, 101, private)
        count = self.diary.purge_old_homework(now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(count, 2)
        self.assertEqual({r["id"] for r in self.db.all("SELECT id FROM assignments")}, {recent, today, future})
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM completions"), 0)
        self.assertEqual(self.diary.purge_old_homework(now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc)), 0)

    def test_past_due_is_calculated_in_each_class_timezone(self):
        moscow = self.task("moscow", due="2026-10-28")
        utc_class = self.diary.create_class(200, "Другой", "UTC")
        utc = self.task("utc", due="2026-10-28", cid=utc_class, actor=200)
        self.diary.purge_old_homework(now=datetime(2026, 10, 28, 22, 30, tzinfo=timezone.utc))
        self.assertIsNone(self.db.one("SELECT id FROM assignments WHERE id=?", (moscow,)))
        self.assertIsNotNone(self.db.one("SELECT id FROM assignments WHERE id=?", (utc,)))

    def test_editing_does_not_reset_creation_age(self):
        aid = self.task("edit")
        row = self.diary.assignment(self.cid, 100, aid)
        row["attachments"] = json.loads(row["attachments"])
        row["body"] = "Исправлено"
        self.diary.save_assignment(self.cid, 100, row)
        self.assertEqual(self.db.scalar("SELECT created_at FROM assignments WHERE id=?", (aid,)), "2026-10-14 12:00:00")
        self.assertEqual(self.diary.purge_old_homework(now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc)), 1)


class UpgradeTests(unittest.TestCase):
    def test_released_024_database_keeps_panels_invites_and_accepts_new_settings(self):
        schema = (Path(__file__).parent / "fixtures" / "schema_v024.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"diary.sqlite3"
            old = sqlite3.connect(path)
            old.executescript(schema)
            old.execute("INSERT INTO meta VALUES('schema_version','1')")
            old.execute("INSERT INTO users(id,name,started,active_class) VALUES(100,'Владелец',1,1)")
            old.execute("INSERT INTO classes(id,name,owner_id,timezone,invite_token,group_id) VALUES(1,'9Б',100,'Europe/Moscow','token',-100)")
            old.execute("INSERT INTO members VALUES(1,100,1)")
            old.execute("INSERT INTO ui_panels(user_id,message_id) VALUES(100,123)")
            old.execute("INSERT INTO group_panels(chat_id,message_id,class_id,owner_id) VALUES(-100,456,1,100)")
            old.execute("INSERT INTO class_invites(token,class_id,group_id,created_by,expires_at) VALUES('common',1,-100,100,'2099-01-01 00:00:00')")
            old.execute("INSERT INTO lessons(class_id,weekday,position,subject) VALUES(1,0,1,'Физкультура')")
            old.execute("INSERT INTO lessons(class_id,weekday,position,subject) VALUES(1,6,1,'Старый воскресный урок')")
            old.commit()
            old.close()
            db = Database(path)
            diary = Diary(db)
            diary.toggle_homework_exemption(1, 100, "Физкультура")
            diary.toggle_school_weekend(1, 100, 5)
            diary.add_school_break(1, 100, date(2026, 10, 19), date(2026, 10, 24), "Осенние каникулы")
            db.close()
            db = Database(path)
            diary = Diary(db)
            self.assertEqual(db.scalar("SELECT message_id FROM ui_panels WHERE user_id=100"), 123)
            self.assertEqual(db.scalar("SELECT owner_id FROM group_panels WHERE message_id=456"), 100)
            self.assertEqual(diary.group_invite("common")["class_id"], 1)
            self.assertEqual(diary.exempt_subjects(1), ["Физкультура"])
            self.assertEqual(diary.next_school_day(1, date(2026, 10, 17)), date(2026, 10, 26))
            self.assertEqual(diary.lessons_on(1, date(2026, 10, 18)), [])
            self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")
            self.assertEqual(db.all("PRAGMA foreign_key_check"), [])
            db.close()

    def test_released_011_database_upgrades_without_losing_class_or_records(self):
        schema = (Path(__file__).parent / "fixtures" / "schema_v011.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "diary.sqlite3"
            old = sqlite3.connect(path)
            old.executescript(schema)
            old.execute("INSERT INTO meta VALUES('schema_version','1')")
            old.execute("INSERT INTO users(id,name,started,active_class) VALUES(100,'Владелец',1,1)")
            old.execute("INSERT INTO classes(id,name,owner_id,timezone,invite_token,group_id) VALUES(1,'9Б',100,'Europe/Moscow','token',-100)")
            old.execute("INSERT INTO members VALUES(1,100,1)")
            old.execute("INSERT INTO roles(class_id,name,permissions) VALUES(1,'Куратор','[\"homework\"]')")
            old.execute("INSERT INTO assignments(class_id,owner_id,source_key,assigned_on,subject,due_on,body,editor_id,updated_at) "
                        "VALUES(1,0,'l:1','2026-10-14','Алгебра','2026-10-16','№ 125',100,'2026-10-14 15:00:00')")
            old.execute("INSERT INTO completions VALUES(1,100)")
            old.execute("INSERT INTO fsm(user_id,stage,data) VALUES(100,'draft',?)", (json.dumps({"cid": 1, "body": "Черновик"}),))
            old.execute("INSERT INTO deliveries(unique_key,class_id,recipient,kind,payload,state) VALUES('ann:1:100',1,100,'announcement','{}','sending')")
            old.commit()
            old.close()
            db = Database(path)
            diary = Diary(db)
            self.assertEqual(diary.require(1, 100)["group_id"], -100)
            self.assertEqual(db.scalar("SELECT name FROM roles"), "Куратор")
            self.assertEqual(db.scalar("SELECT body FROM assignments"), "№ 125")
            self.assertEqual(db.scalar("SELECT created_at FROM assignments"), "2026-10-14 15:00:00")
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM completions"), 1)
            self.assertEqual(db.state(100)[1]["body"], "Черновик")
            self.assertEqual(db.scalar("SELECT state FROM deliveries"), "pending")
            new = diary.save_assignment(1, 100, dict(owner_id=100, source_key="note:new", source_position=0,
                assigned_on="2026-10-16", subject="Личное", audience_id=0, due_on="2026-10-17", due_position=0,
                body="Запись после обновления", attachments=[], no_homework=False, revision=0))
            self.assertTrue(db.scalar("SELECT created_at FROM assignments WHERE id=?", (new,)))
            invite = diary.create_group_invite(1, 100)
            db.run("INSERT INTO group_panels(chat_id,message_id,class_id,owner_id) VALUES(-100,321,1,100)")
            db.close()
            # Applying the migration twice must be harmless.
            db = Database(path)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM assignments"), 2)
            self.assertEqual(Diary(db).group_invite(invite["token"])["class_id"], 1)
            self.assertEqual(db.scalar("SELECT owner_id FROM group_panels WHERE message_id=321"), 100)
            db.run("INSERT INTO ui_panels(user_id,message_id) VALUES(100,123)")
            self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")
            self.assertEqual(db.all("PRAGMA foreign_key_check"), [])
            db.close()
