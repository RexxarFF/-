from datetime import datetime, timedelta, timezone
import unittest

from classdiary.db import Database
from classdiary.service import AccessDenied, Diary


class ClassInviteTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.diary = Diary(self.db)
        for uid in (100, 101, 200, 300):
            self.diary.touch_user({"id": uid, "first_name": str(uid)}, started=True)
        self.cid = self.diary.create_class(100, "9Б", "Europe/Moscow")
        self.other = self.diary.create_class(200, "Другой класс", "UTC")
        self.diary.add_members(self.cid, 100, [{"user_id": 101}])
        self.diary.bind_group(self.cid, 100, -100, "Группа класса")

    def tearDown(self):
        self.db.close()

    def test_invitation_expires_at_exact_seven_day_boundary(self):
        now = datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc)
        invite = self.diary.create_group_invite(self.cid, 100, now)
        self.assertEqual(invite["expires_at"], "2026-10-23 12:30:00")
        self.diary.group_invite(invite["token"], now+timedelta(days=7, seconds=-1))
        with self.assertRaises(ValueError):
            self.diary.group_invite(invite["token"], now+timedelta(days=7))

    def test_new_link_and_manual_close_invalidate_previous_links(self):
        first = self.diary.create_group_invite(self.cid, 100)
        second = self.diary.create_group_invite(self.cid, 100)
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaises(ValueError):
            self.diary.group_invite(first["token"])
        self.diary.group_invite(second["token"])
        with self.assertRaises(AccessDenied):
            self.diary.close_group_invites(self.cid, 101)
        self.diary.close_group_invites(self.cid, 100)
        with self.assertRaises(ValueError):
            self.diary.group_invite(second["token"])

    def test_rebinding_group_invalidates_link_but_same_binding_does_not(self):
        invite = self.diary.create_group_invite(self.cid, 100)
        self.diary.bind_group(self.cid, 100, -100, "Новое название той же группы")
        self.diary.group_invite(invite["token"])
        self.diary.bind_group(self.cid, 100, -200, "Другая группа")
        with self.assertRaises(ValueError):
            self.diary.group_invite(invite["token"])

    def test_creator_losing_members_permission_closes_own_link(self):
        role = self.diary.save_role(self.cid, 100, "Приглашения", ["members"])
        self.diary.toggle_role(self.cid, 100, 101, role)
        invite = self.diary.create_group_invite(self.cid, 101)
        self.diary.toggle_role(self.cid, 100, 101, role)
        with self.assertRaises(ValueError):
            self.diary.group_invite(invite["token"])

    def test_removing_member_closes_old_self_join_link(self):
        invite = self.diary.create_group_invite(self.cid, 100)
        self.diary.remove_member(self.cid, 100, 101)
        with self.assertRaises(ValueError):
            self.diary.join_group_invite(invite["token"], 101, -100)
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=101", (self.cid,)))

    def test_repeated_join_preserves_roles_and_personal_records(self):
        role = self.diary.save_role(self.cid, 100, "Куратор", ["homework"])
        self.diary.toggle_role(self.cid, 100, 101, role)
        aid = self.diary.save_assignment(self.cid, 101, dict(owner_id=101, source_key="own:1", source_position=0,
            assigned_on="2026-10-16", subject="Заметка", audience_id=0, due_on="2026-10-17", due_position=0,
            body="Личное", attachments=[], no_homework=False, revision=0))
        invite = self.diary.create_group_invite(self.cid, 100)
        for _ in range(2):
            self.assertEqual(self.diary.join_group_invite(invite["token"], 101, -100), self.cid)
        self.assertEqual(self.diary.permissions(self.cid, 101), {"homework"})
        self.assertEqual(self.db.scalar("SELECT body FROM assignments WHERE id=?", (aid,)), "Личное")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM members WHERE class_id=? AND user_id=101", (self.cid,)), 1)

    def test_verified_group_must_match_exact_class_invitation(self):
        invite = self.diary.create_group_invite(self.cid, 100)
        with self.assertRaises(AccessDenied):
            self.diary.join_group_invite(invite["token"], 300, -200)
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE user_id=300"))
        self.diary.join_group_invite(invite["token"], 300, -100)
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=300", (self.other,)))

    def test_unstarted_user_and_old_selected_only_token_cannot_self_join(self):
        self.diary.touch_user({"id": 400, "first_name": "Новичок"})
        invite = self.diary.create_group_invite(self.cid, 100)
        with self.assertRaises(AccessDenied):
            self.diary.join_group_invite(invite["token"], 400, -100)
        old_token = self.db.scalar("SELECT invite_token FROM classes WHERE id=?", (self.cid,))
        with self.assertRaises(ValueError):
            self.diary.group_invite(old_token)
        with self.assertRaises(AccessDenied):
            self.diary.select_class(300, self.cid)
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE user_id IN (300,400)"))

    def test_unbound_class_and_ordinary_member_cannot_create_invitation(self):
        with self.assertRaises(ValueError):
            self.diary.create_group_invite(self.other, 200)
        with self.assertRaises(AccessDenied):
            self.diary.create_group_invite(self.cid, 101)

    def test_join_rechecks_closed_invitation_inside_transaction(self):
        invite = self.diary.create_group_invite(self.cid, 100)
        # A membership lookup may finish after an administrator closes the invitation.
        self.diary.group_invite(invite["token"])
        self.diary.close_group_invites(self.cid, 100)
        with self.assertRaises(ValueError):
            self.diary.join_group_invite(invite["token"], 300, -100)
        self.assertFalse(self.db.scalar("SELECT 1 FROM members WHERE user_id=300"))
