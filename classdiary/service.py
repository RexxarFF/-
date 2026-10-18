from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone

from .config import get_timezone
from .db import Database
from .schedule import homework_blocks, parse_date, streak, subject_key, validate_lessons

PERMISSIONS = {
    "homework": "Общее ДЗ", "schedule": "Расписание и замены", "announcements": "Объявления",
    "members": "Участники", "roles": "Роли и права", "settings": "Настройки и привязка группы",
}


class AccessDenied(ValueError):
    pass


class Conflict(ValueError):
    pass


class Diary:
    def __init__(self, db: Database):
        self.db = db

    def touch_user(self, user: dict, started=False):
        user_id = int(user["id"])
        name = " ".join(x for x in (user.get("first_name", ""), user.get("last_name", "")) if x).strip()
        self.db.run("INSERT INTO users(id,name,username,started) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name,username=excluded.username,started=MAX(users.started,excluded.started),updated_at=CURRENT_TIMESTAMP",
                    (user_id, name[:150] or str(user_id), user.get("username"), int(started)))
        if started:
            self.db.run("UPDATE members SET active=1 WHERE user_id=?", (user_id,))

    def require(self, class_id: int, user_id: int, permission: str | None = None):
        classroom = self.db.one("SELECT c.* FROM classes c JOIN members m ON m.class_id=c.id "
                                "WHERE c.id=? AND m.user_id=? AND m.active=1", (class_id, user_id))
        if not classroom:
            raise AccessDenied("Нет доступа к этому классу. Попроси владельца добавить тебя и запусти бота в личке.")
        if permission and classroom["owner_id"] != user_id and permission not in self.permissions(class_id, user_id):
            raise AccessDenied("Для этого действия у тебя нет прав в этом классе.")
        return classroom

    def permissions(self, class_id, user_id):
        classroom = self.db.one("SELECT owner_id FROM classes WHERE id=?", (class_id,))
        member = self.db.scalar("SELECT active FROM members WHERE class_id=? AND user_id=?", (class_id, user_id))
        if not member:
            return set()
        if classroom and classroom["owner_id"] == user_id:
            return set(PERMISSIONS)
        rows = self.db.all("SELECT r.permissions FROM roles r JOIN member_roles mr ON mr.role_id=r.id "
                           "AND mr.class_id=r.class_id WHERE mr.class_id=? AND mr.user_id=?", (class_id, user_id))
        return {p for row in rows for p in json.loads(row["permissions"]) if p in PERMISSIONS}

    def classrooms(self, user_id):
        return self.db.all("SELECT c.* FROM classes c JOIN members m ON m.class_id=c.id "
                           "WHERE m.user_id=? AND m.active=1 ORDER BY c.name,c.id", (user_id,))

    def create_class(self, user_id, name, zone, maximum=10):
        name = name.strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("Название класса должно содержать от 1 до 60 символов.")
        get_timezone(zone)
        with self.db.transaction():
            if self.db.scalar("SELECT COUNT(*) FROM classes WHERE owner_id=?", (user_id,)) >= maximum:
                raise ValueError("Достигнут лимит созданных классов.")
            class_id = self.db.run("INSERT INTO classes(name,owner_id,timezone,invite_token) VALUES(?,?,?,?)",
                                   (name, user_id, zone, secrets.token_urlsafe(18))).lastrowid
            self.db.run("INSERT INTO members VALUES(?,?,1)", (class_id, user_id))
            self.db.run("UPDATE users SET active_class=? WHERE id=?", (class_id, user_id))
            self.db.run("INSERT INTO roles(class_id,name,permissions) VALUES(?,?,?)",
                        (class_id, "Ответственный за ДЗ", '["homework"]'))
            self.db.run("INSERT INTO roles(class_id,name,permissions) VALUES(?,?,?)",
                        (class_id, "Редактор расписания", '["schedule"]'))
            self.db.audit(class_id, user_id, "class.create", name)
        return class_id

    def select_class(self, user_id, class_id):
        self.require(class_id, user_id)
        self.db.run("UPDATE users SET active_class=? WHERE id=?", (class_id, user_id))
        self.db.clear_state(user_id)

    def add_members(self, class_id, actor, users):
        self.require(class_id, actor, "members")
        if len(users) > 50:
            raise ValueError("Добавляй до 50 участников за один раз.")
        with self.db.transaction():
            for user in users:
                uid = int(user.get("user_id", user.get("id", 0)))
                if not 0 < uid < 2**52 or user.get("is_bot"):
                    raise ValueError("Нужен Telegram ID обычного пользователя.")
                known = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
                if not known:
                    name = " ".join(str(user.get(x, "")) for x in ("first_name", "last_name")).strip() or str(uid)
                    self.db.run("INSERT INTO users(id,name,username) VALUES(?,?,?)", (uid, name[:150], user.get("username")))
                self.db.run("INSERT OR IGNORE INTO members VALUES(?,?,?)", (class_id, uid, int(bool(known and known["started"]))))
            self.db.audit(class_id, actor, "members.add", str(len(users)))

    def remove_member(self, class_id, actor, target):
        classroom = self.require(class_id, actor, "members")
        if target == classroom["owner_id"]:
            raise ValueError("Владельца класса исключить нельзя.")
        with self.db.transaction():
            self.db.run("DELETE FROM members WHERE class_id=? AND user_id=?", (class_id, target))
            self.db.run("DELETE FROM pairs WHERE class_id=? AND (user_a=? OR user_b=?)", (class_id, target, target))
            self.db.run("DELETE FROM assignments WHERE class_id=? AND owner_id=?", (class_id, target))
            self.db.run("UPDATE users SET active_class=NULL WHERE id=? AND active_class=?", (target, class_id))
            self.db.run("DELETE FROM fsm WHERE user_id=?", (target,))
            self.db.run("DELETE FROM deliveries WHERE class_id=? AND recipient=? AND state='pending'", (class_id, target))
            self.db.run("UPDATE class_invites SET active=0 WHERE class_id=?", (class_id,))
            self.db.audit(class_id, actor, "members.remove", str(target))

    def members(self, class_id, actor):
        self.require(class_id, actor)
        return self.db.all("SELECT u.id,u.name,u.username,m.active FROM members m JOIN users u ON u.id=m.user_id "
                           "WHERE m.class_id=? ORDER BY u.name,u.id", (class_id,))

    def roles(self, class_id, actor):
        self.require(class_id, actor, "roles")
        return self.db.all("SELECT * FROM roles WHERE class_id=? ORDER BY id", (class_id,))

    def save_role(self, class_id, actor, name, permissions, role_id=None):
        self.require(class_id, actor, "roles")
        name = name.strip()
        if not 1 <= len(name) <= 50 or not set(permissions).issubset(PERMISSIONS):
            raise ValueError("Проверь название роли и выбранные права.")
        with self.db.transaction():
            if role_id is None:
                role_id = self.db.run("INSERT INTO roles(class_id,name,permissions) VALUES(?,?,?)",
                                     (class_id, name, json.dumps(sorted(set(permissions))))).lastrowid
            elif self.db.run("UPDATE roles SET name=?,permissions=? WHERE id=? AND class_id=?",
                             (name, json.dumps(sorted(set(permissions))), role_id, class_id)).rowcount != 1:
                raise AccessDenied("Эта роль не принадлежит классу.")
            self.db.audit(class_id, actor, "role.save", str(role_id))
        return role_id

    def delete_role(self, class_id, actor, role_id):
        self.require(class_id, actor, "roles")
        self.db.run("DELETE FROM roles WHERE id=? AND class_id=?", (role_id, class_id))
        self.db.audit(class_id, actor, "role.delete", str(role_id))

    def toggle_role(self, class_id, actor, target, role_id):
        self.require(class_id, actor, "roles")
        if not self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=?", (class_id, target)):
            raise ValueError("Участник не найден.")
        if not self.db.scalar("SELECT 1 FROM roles WHERE id=? AND class_id=?", (role_id, class_id)):
            raise AccessDenied("Роль из другого класса.")
        with self.db.transaction():
            exists = self.db.scalar("SELECT 1 FROM member_roles WHERE class_id=? AND user_id=? AND role_id=?", (class_id, target, role_id))
            if exists:
                self.db.run("DELETE FROM member_roles WHERE class_id=? AND user_id=? AND role_id=?", (class_id, target, role_id))
            else:
                self.db.run("INSERT INTO member_roles VALUES(?,?,?)", (class_id, target, role_id))
            self.db.audit(class_id, actor, "role.toggle", f"{target}:{role_id}")

    def audience_id(self, class_id, name):
        if not name:
            return 0
        self.db.run("INSERT OR IGNORE INTO audiences(class_id,name) VALUES(?,?)", (class_id, name))
        return self.db.scalar("SELECT id FROM audiences WHERE class_id=? AND name=?", (class_id, name))

    def audiences(self, class_id, actor):
        self.require(class_id, actor)
        return self.db.all("SELECT * FROM audiences WHERE class_id=? ORDER BY id", (class_id,))

    def toggle_audience(self, class_id, actor, audience_id):
        self.require(class_id, actor)
        if not self.db.scalar("SELECT 1 FROM audiences WHERE class_id=? AND id=?", (class_id, audience_id)):
            raise AccessDenied("Подгруппа из другого класса.")
        existing = self.db.scalar("SELECT 1 FROM member_audiences WHERE class_id=? AND user_id=? AND audience_id=?", (class_id, actor, audience_id))
        if existing:
            self.db.run("DELETE FROM member_audiences WHERE class_id=? AND user_id=? AND audience_id=?", (class_id, actor, audience_id))
        else:
            self.db.run("INSERT INTO member_audiences VALUES(?,?,?)", (class_id, actor, audience_id))

    def replace_weekday(self, class_id, actor, weekday, rows):
        self.require(class_id, actor, "schedule")
        if not 0 <= weekday <= 5:
            raise ValueError("Выбери день с понедельника по субботу. Воскресенье — выходной.")
        rows = validate_lessons(rows)
        with self.db.transaction():
            old = self.db.all("SELECT * FROM lessons WHERE class_id=? AND weekday=? AND archived=0", (class_id, weekday))
            self.db.run("UPDATE lessons SET archived=1 WHERE class_id=? AND weekday=?", (class_id, weekday))
            for row in rows:
                audience = self.audience_id(class_id, row["audience"])
                same = next((x for x in old if x["position"] == row["position"] and x["subject"] == row["subject"] and x["audience_id"] == audience), None)
                if same:
                    self.db.run("UPDATE lessons SET archived=0,starts=?,ends=?,room=? WHERE id=?", (row["starts"], row["ends"], row["room"], same["id"]))
                else:
                    self.db.run("INSERT INTO lessons(class_id,weekday,position,subject,audience_id,starts,ends,room) VALUES(?,?,?,?,?,?,?,?)",
                                (class_id, weekday, row["position"], row["subject"], audience, row["starts"], row["ends"], row["room"]))
            self.db.audit(class_id, actor, "schedule.weekday", str(weekday))

    def set_override(self, class_id, actor, day: date, rows):
        self.require(class_id, actor, "schedule")
        if day.weekday() == 6:
            raise ValueError("На воскресенье расписание не создаётся.")
        rows = validate_lessons(rows)
        with self.db.transaction():
            for row in rows:
                row["audience_id"] = self.audience_id(class_id, row["audience"])
            self.db.run("INSERT INTO day_overrides VALUES(?,?,?) ON CONFLICT(class_id,day) DO UPDATE SET lessons=excluded.lessons",
                        (class_id, day.isoformat(), json.dumps(rows, ensure_ascii=False)))
            self.db.audit(class_id, actor, "schedule.override", day.isoformat())

    def clear_override(self, class_id, actor, day):
        self.require(class_id, actor, "schedule")
        self.db.run("DELETE FROM day_overrides WHERE class_id=? AND day=?", (class_id, day.isoformat()))

    def lessons_on(self, class_id, day: date):
        if self.day_off_reason(class_id, day):
            return []
        override = self.db.scalar("SELECT lessons FROM day_overrides WHERE class_id=? AND day=?", (class_id, day.isoformat()))
        if override is not None:
            rows = json.loads(override)
            for row in rows:
                digest = hashlib.sha256(row["subject"].encode()).hexdigest()[:10]
                row["source_key"] = f"d:{day}:{row['position']}:{row['audience_id']}:{digest}"
        else:
            rows = self.db.all("SELECT l.*,COALESCE(a.name,'') AS audience FROM lessons l LEFT JOIN audiences a ON a.id=l.audience_id "
                               "WHERE l.class_id=? AND l.weekday=? AND l.archived=0 ORDER BY position,audience_id", (class_id, day.weekday()))
            for row in rows:
                row["source_key"] = f"l:{row['id']}"
        return sorted(rows, key=lambda x: (x["position"], x["audience_id"]))

    def visible_lessons(self, class_id, actor, day):
        self.require(class_id, actor)
        rows = self.lessons_on(class_id, day)
        selected = {r["audience_id"] for r in self.db.all("SELECT audience_id FROM member_audiences WHERE class_id=? AND user_id=?", (class_id, actor))}
        return [r for r in rows if not selected or r["audience_id"] == 0 or r["audience_id"] in selected]

    def next_lesson(self, class_id, assigned: date, subject, audience_id):
        calendar = self._calendar_snapshot(class_id)
        key = subject_key(subject)

        def matches_subject(row):
            return subject_key(row["subject"]) == key and (not audience_id or row["audience_id"] in (0, audience_id))

        if not any(matches_subject(row) for days in calendar[:2] for rows in days.values() for row in rows):
            return None, 0
        for i in range(1, 741):
            day = assigned + timedelta(days=i)
            matches = [r for r in self._calendar_lessons(calendar, day) if matches_subject(r)]
            if matches:
                return day, matches[0]["position"]
        return None, 0

    def _calendar_snapshot(self, class_id):
        """Read once per search so holidays and empty schedules cost no SQL per day."""
        week = {}
        for row in self.db.all("SELECT * FROM lessons WHERE class_id=? AND archived=0 ORDER BY position,audience_id", (class_id,)):
            week.setdefault(row["weekday"], []).append(row)
        overrides = {r["day"]: json.loads(r["lessons"]) for r in self.db.all(
            "SELECT day,lessons FROM day_overrides WHERE class_id=?", (class_id,))}
        weekends = {6} | {r["weekday"] for r in self.db.all("SELECT weekday FROM school_weekends WHERE class_id=?", (class_id,))}
        breaks = self.db.all("SELECT start_on,end_on FROM school_breaks WHERE class_id=?", (class_id,))
        return week, overrides, weekends, breaks

    @staticmethod
    def _calendar_is_off(snapshot, day):
        _, _, weekends, breaks = snapshot
        key = day.isoformat()
        return day.weekday() in weekends or any(r["start_on"] <= key <= r["end_on"] for r in breaks)

    @classmethod
    def _calendar_lessons(cls, snapshot, day):
        if cls._calendar_is_off(snapshot, day):
            return []
        week, overrides, _, _ = snapshot
        return overrides.get(day.isoformat(), week.get(day.weekday(), []))

    def day_off_reason(self, class_id, day):
        if day.weekday() == 6:
            return "Выходной"
        if self.db.scalar("SELECT 1 FROM school_weekends WHERE class_id=? AND weekday=?", (class_id, day.weekday())):
            return "Выходной"
        return self.db.scalar("SELECT title FROM school_breaks WHERE class_id=? AND start_on<=? AND end_on>=? ORDER BY id DESC LIMIT 1",
                              (class_id, day.isoformat(), day.isoformat()))

    def next_school_day(self, class_id, after, direction=1):
        if direction not in (-1, 1):
            raise ValueError("Неверное направление даты.")
        calendar = self._calendar_snapshot(class_id)
        has_schedule = any(calendar[0].values()) or any(calendar[1].values())
        fallback = None
        for offset in range(1, 741):
            day = after+timedelta(days=direction*offset)
            if self._calendar_is_off(calendar, day):
                continue
            fallback = fallback or day
            if not has_schedule:
                return fallback
            if self._calendar_lessons(calendar, day):
                return day
        # An empty, newly created class still has a usable menu and calendar.
        if fallback:
            return fallback
        day = after+timedelta(days=direction)
        return day+timedelta(days=direction) if day.weekday() == 6 else day

    def resolve_school_date(self, class_id, value, today):
        value = value.strip().casefold()
        return self.next_school_day(class_id, today) if value in ("завтра", "tomorrow") else parse_date(value, today)

    def toggle_school_weekend(self, class_id, actor, weekday):
        self.require(class_id, actor, "schedule")
        if not 0 <= weekday <= 5:
            raise ValueError("Воскресенье всегда выходной. Выбери другой день.")
        if self.db.scalar("SELECT 1 FROM school_weekends WHERE class_id=? AND weekday=?", (class_id, weekday)):
            self.db.run("DELETE FROM school_weekends WHERE class_id=? AND weekday=?", (class_id, weekday))
        else:
            if self.db.scalar("SELECT COUNT(*) FROM school_weekends WHERE class_id=?", (class_id,)) >= 5:
                raise ValueError("Оставь хотя бы один учебный день в неделе.")
            self.db.run("INSERT INTO school_weekends VALUES(?,?)", (class_id, weekday))
        self.db.audit(class_id, actor, "calendar.weekend", str(weekday))

    def school_breaks(self, class_id, actor):
        self.require(class_id, actor, "schedule")
        return self.db.all("SELECT * FROM school_breaks WHERE class_id=? ORDER BY start_on DESC,id DESC", (class_id,))

    def add_school_break(self, class_id, actor, start, end, title="Каникулы"):
        self.require(class_id, actor, "schedule")
        title = title.strip()
        if end < start or (end-start).days > 365:
            raise ValueError("Конец периода должен быть не раньше начала. Максимум — один год.")
        if not 1 <= len(title) <= 80:
            raise ValueError("Название периода: 1–80 символов.")
        result = self.db.run("INSERT INTO school_breaks(class_id,start_on,end_on,title,created_by) VALUES(?,?,?,?,?)",
                             (class_id, start.isoformat(), end.isoformat(), title, actor)).lastrowid
        self.db.audit(class_id, actor, "calendar.break.add", str(result))
        return result

    def remove_school_break(self, class_id, actor, break_id):
        self.require(class_id, actor, "schedule")
        if self.db.run("DELETE FROM school_breaks WHERE id=? AND class_id=?", (break_id, class_id)).rowcount != 1:
            raise ValueError("Период уже удалён или относится к другому классу.")
        self.db.audit(class_id, actor, "calendar.break.remove", str(break_id))

    def exempt_subjects(self, class_id):
        return [r["subject"] for r in self.db.all("SELECT subject FROM homework_exemptions WHERE class_id=? ORDER BY subject", (class_id,))]

    def subject_catalog(self, class_id, actor):
        self.require(class_id, actor, "settings")
        subjects = self.exempt_subjects(class_id)
        subjects += [r["subject"] for r in self.db.all("SELECT DISTINCT subject FROM lessons WHERE class_id=? AND archived=0 AND weekday<6", (class_id,))]
        for override in self.db.all("SELECT lessons FROM day_overrides WHERE class_id=?", (class_id,)):
            subjects += [r["subject"] for r in json.loads(override["lessons"])]
        return sorted({subject_key(s): s for s in subjects}.values(), key=subject_key)

    def toggle_homework_exemption(self, class_id, actor, subject):
        self.require(class_id, actor, "settings")
        if subject_key(subject) not in {subject_key(s) for s in self.subject_catalog(class_id, actor)}:
            raise ValueError("Выбери предмет из расписания класса.")
        key = subject_key(subject)
        if self.db.scalar("SELECT 1 FROM homework_exemptions WHERE class_id=? AND subject_key=?", (class_id, key)):
            self.db.run("DELETE FROM homework_exemptions WHERE class_id=? AND subject_key=?", (class_id, key))
        else:
            self.db.run("INSERT INTO homework_exemptions VALUES(?,?,?)", (class_id, key, subject))
        self.db.audit(class_id, actor, "homework.exemption", subject)

    def homework_progress(self, class_id, actor, day):
        self.require(class_id, actor, "homework")
        assignments = self.db.all("SELECT * FROM assignments WHERE class_id=? AND owner_id=0 AND due_on=?", (class_id, day.isoformat()))
        blocks = homework_blocks(assignments, self.lessons_on(class_id, day), self.exempt_subjects(class_id))
        return dict(total=len(blocks), filled=sum(b["status"] != "missing" for b in blocks), blocks=blocks)

    def existing_assignment(self, class_id, actor, owner_id, source_key, assigned):
        self.require(class_id, actor, "homework" if owner_id == 0 else None)
        if owner_id not in (0, actor):
            raise AccessDenied("Чужие личные записи недоступны.")
        return self.db.one("SELECT * FROM assignments WHERE class_id=? AND owner_id=? AND source_key=? AND assigned_on=?",
                           (class_id, owner_id, source_key, assigned.isoformat()))

    def save_assignment(self, class_id, actor, draft):
        owner = int(draft["owner_id"])
        self.require(class_id, actor, "homework" if owner == 0 else None)
        if owner not in (0, actor):
            raise AccessDenied("Нельзя изменять чужую личную запись.")
        body = str(draft.get("body", "")).strip()
        media = draft.get("attachments", [])
        no_homework = bool(draft.get("no_homework"))
        if (not body and not media and not no_homework) or len(body) > 6000 or len(media) > 20:
            raise ValueError("Нужно задание или вложение. Максимум: 6000 символов и 20 вложений.")
        assigned = date.fromisoformat(draft["assigned_on"])
        due = date.fromisoformat(draft["due_on"])
        if due < assigned:
            raise ValueError("Срок выполнения не может быть раньше дня записи.")
        subject = str(draft["subject"]).strip()
        audience = int(draft.get("audience_id", 0))
        if not subject or len(subject) > 80:
            raise ValueError("Проверь название предмета.")
        if audience and not self.db.scalar("SELECT 1 FROM audiences WHERE class_id=? AND id=?", (class_id, audience)):
            raise AccessDenied("Подгруппа из другого класса.")
        if no_homework:
            body, media = "Ничего не задано", []
        for item in media:
            if item.get("type") not in {"photo", "document", "video", "audio", "voice", "animation", "video_note"} or not isinstance(item.get("file_id"), str):
                raise ValueError("Неподдерживаемое вложение.")
        key = (class_id, owner, draft["source_key"], assigned.isoformat())
        with self.db.transaction():
            old = self.db.one("SELECT * FROM assignments WHERE class_id=? AND owner_id=? AND source_key=? AND assigned_on=?", key)
            revision = int(draft.get("revision", 0))
            if (old and old["revision"] != revision) or (not old and revision):
                raise Conflict("Задание уже изменили или удалили. Открой урок заново, чтобы не затереть чужие правки.")
            values = (subject, audience, due.isoformat(), int(draft.get("due_position", 0)), body,
                      json.dumps(media, ensure_ascii=False), int(no_homework), actor)
            if old:
                self.db.run("UPDATE assignments SET subject=?,audience_id=?,due_on=?,due_position=?,body=?,attachments=?,no_homework=?,editor_id=?,"
                            "revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", values + (old["id"],))
                result = old["id"]
            else:
                result = self.db.run("INSERT INTO assignments(subject,audience_id,due_on,due_position,body,attachments,no_homework,editor_id,"
                                     "class_id,owner_id,source_key,assigned_on,source_position,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                                     values + key + (int(draft.get("source_position", 0)),)).lastrowid
            self.db.run("DELETE FROM completions WHERE assignment_id=?", (result,))
            # Audit contains identifiers only, never private homework text.
            self.db.audit(class_id, actor, "assignment.save", str(result))
        return result

    def assignment(self, class_id, actor, assignment_id):
        self.require(class_id, actor)
        row = self.db.one("SELECT x.*,COALESCE(a.name,'') AS audience FROM assignments x LEFT JOIN audiences a ON a.id=x.audience_id "
                          "WHERE x.id=? AND x.class_id=?", (assignment_id, class_id))
        if not row or row["owner_id"] not in (0, actor):
            raise AccessDenied("Запись не найдена или является личной записью другого человека.")
        return row

    def delete_assignment(self, class_id, actor, assignment_id, revision):
        row = self.assignment(class_id, actor, assignment_id)
        if row["owner_id"] == 0:
            self.require(class_id, actor, "homework")
        if self.db.run("DELETE FROM assignments WHERE id=? AND revision=?", (assignment_id, revision)).rowcount != 1:
            raise Conflict("Задание изменилось. Открой его заново.")
        self.db.audit(class_id, actor, "assignment.delete", str(assignment_id))

    def assignments_on(self, class_id, actor, day, personal=False):
        self.require(class_id, actor)
        owner = actor if personal else 0
        rows = self.db.all("SELECT x.*,COALESCE(a.name,'') AS audience,EXISTS(SELECT 1 FROM completions cp WHERE cp.assignment_id=x.id AND cp.user_id=?) AS done "
                           "FROM assignments x LEFT JOIN audiences a ON a.id=x.audience_id WHERE x.class_id=? AND x.owner_id=? AND x.due_on=? "
                           "ORDER BY due_position,subject,audience_id,assigned_on,source_position,id", (actor, class_id, owner, day.isoformat()))
        selected = {r["audience_id"] for r in self.db.all("SELECT audience_id FROM member_audiences WHERE class_id=? AND user_id=?", (class_id, actor))}
        return [r for r in rows if personal or not selected or r["audience_id"] == 0 or r["audience_id"] in selected]

    def toggle_done(self, class_id, actor, assignment_id):
        row = self.assignment(class_id, actor, assignment_id)
        if row["no_homework"]:
            return
        exists = self.db.scalar("SELECT 1 FROM completions WHERE assignment_id=? AND user_id=?", (assignment_id, actor))
        if exists:
            self.db.run("DELETE FROM completions WHERE assignment_id=? AND user_id=?", (assignment_id, actor))
        else:
            self.db.run("INSERT INTO completions VALUES(?,?)", (assignment_id, actor))

    def bind_group(self, class_id, actor, group_id, title):
        classroom = self.require(class_id, actor, "settings")
        if int(group_id) >= 0:
            raise ValueError("Нужна группа, а не личный чат.")
        other = self.db.scalar("SELECT id FROM classes WHERE group_id=? AND id<>?", (group_id, class_id))
        if other:
            raise Conflict("Эта группа уже привязана к другому классу.")
        self.db.run("UPDATE classes SET group_id=?,group_title=? WHERE id=?", (group_id, title[:150], class_id))
        if classroom["group_id"] != group_id:
            self.db.run("UPDATE class_invites SET active=0 WHERE class_id=?", (class_id,))
        self.db.audit(class_id, actor, "group.bind", str(group_id))

    def create_group_invite(self, class_id, actor, now=None):
        classroom = self.require(class_id, actor, "members")
        if not classroom["group_id"]:
            raise ValueError("Сначала привяжите группу в управлении классом.")
        now = now or datetime.now(timezone.utc)
        expires = (now.astimezone(timezone.utc)+timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        token = secrets.token_urlsafe(18)
        with self.db.transaction():
            self.db.run("UPDATE class_invites SET active=0 WHERE class_id=?", (class_id,))
            self.db.run("INSERT INTO class_invites(token,class_id,group_id,created_by,expires_at) VALUES(?,?,?,?,?)",
                        (token, class_id, classroom["group_id"], actor, expires))
            self.db.audit(class_id, actor, "class_invite.create")
        return self.group_invite(token, now)

    def group_invite(self, token, now=None):
        now = now or datetime.now(timezone.utc)
        row = self.db.one("SELECT i.*,c.name,c.group_id AS current_group_id,c.group_title FROM class_invites i "
                          "JOIN classes c ON c.id=i.class_id WHERE i.token=? AND i.active=1 AND i.expires_at>?",
                          (token, now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        if not row or row["group_id"] != row["current_group_id"]:
            raise ValueError("Приглашение закрыто или истекло. Попроси ответственного опубликовать новое.")
        try:
            self.require(row["class_id"], row["created_by"], "members")
        except AccessDenied:
            raise ValueError("Приглашение закрыто. Попроси владельца класса опубликовать новое.") from None
        return row

    def close_group_invites(self, class_id, actor):
        self.require(class_id, actor, "members")
        self.db.run("UPDATE class_invites SET active=0 WHERE class_id=?", (class_id,))
        self.db.audit(class_id, actor, "class_invite.close")

    def join_group_invite(self, token, actor, verified_group_id):
        with self.db.transaction():
            invite = self.group_invite(token)
            if invite["group_id"] != verified_group_id:
                raise AccessDenied("Участие в нужной группе не подтверждено.")
            if not self.db.scalar("SELECT started FROM users WHERE id=?", (actor,)):
                raise AccessDenied("Сначала запустите бота в личке.")
            self.db.run("INSERT INTO members(class_id,user_id,active) VALUES(?,?,1) ON CONFLICT(class_id,user_id) DO UPDATE SET active=1",
                        (invite["class_id"], actor))
            self.db.run("UPDATE users SET active_class=? WHERE id=?", (invite["class_id"], actor))
            self.db.clear_state(actor)
            self.db.audit(invite["class_id"], actor, "members.join_group_invite")
        return invite["class_id"]

    def announce(self, class_id, actor, body, attachments):
        classroom = self.require(class_id, actor, "announcements")
        if not body.strip() and not attachments:
            raise ValueError("Объявление пустое.")
        if len(body) > 6000 or len(attachments) > 20:
            raise ValueError("Слишком длинное объявление.")
        with self.db.transaction():
            aid = self.db.run("INSERT INTO announcements(class_id,body,attachments,author_id) VALUES(?,?,?,?)",
                               (class_id, body, json.dumps(attachments, ensure_ascii=False), actor)).lastrowid
            recipients = [m["id"] for m in self.members(class_id, actor) if m["active"]]
            if classroom["group_id"]:
                recipients.insert(0, classroom["group_id"])
            for recipient in recipients:
                self.queue(f"ann:{aid}:{recipient}", class_id, recipient, "announcement", {"id": aid})
            self.db.audit(class_id, actor, "announcement.publish", str(aid))
        return aid

    def ack(self, class_id, actor, announcement_id):
        self.require(class_id, actor)
        if not self.db.scalar("SELECT 1 FROM announcements WHERE id=? AND class_id=?", (announcement_id, class_id)):
            raise AccessDenied("Объявление не найдено.")
        self.db.run("INSERT OR IGNORE INTO announcement_reads VALUES(?,?)", (announcement_id, actor))

    def queue(self, key, class_id, recipient, kind, payload):
        self.db.run("INSERT OR IGNORE INTO deliveries(unique_key,class_id,recipient,kind,payload) VALUES(?,?,?,?,?)",
                    (key, class_id, recipient, kind, json.dumps(payload)))

    def purge_old_homework(self, retention_days=14, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if retention_days < 1:
            raise ValueError("Срок хранения должен быть положительным.")
        cutoff = (now.astimezone(timezone.utc)-timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        deleted = 0
        with self.db.transaction():
            for classroom in self.db.all("SELECT id,timezone FROM classes"):
                today = now.astimezone(get_timezone(classroom["timezone"])).date().isoformat()
                count = self.db.run("DELETE FROM assignments WHERE class_id=? AND created_at<=? AND due_on<?",
                                    (classroom["id"], cutoff, today)).rowcount
                deleted += count
                if count:
                    self.db.audit(classroom["id"], 0, "assignment.cleanup", str(count))
            self.db.run("DELETE FROM fsm WHERE updated_at<=datetime('now','-1 day')")
        return deleted

    def count_message(self, chat_id, message_id, user_id, day: date):
        classroom = self.db.one("SELECT * FROM classes WHERE group_id=? AND interactive=1", (chat_id,))
        if not classroom or not self.db.scalar("SELECT 1 FROM members WHERE class_id=? AND user_id=?", (classroom["id"], user_id)):
            return
        self.db.run("INSERT OR IGNORE INTO message_events VALUES(?,?,?,?,?)", (chat_id, message_id, classroom["id"], user_id, day.isoformat()))

    def stats(self, class_id, actor, today):
        self.require(class_id, actor)
        rows = self.db.all("SELECT day,COUNT(*) n FROM message_events WHERE class_id=? AND user_id=? GROUP BY day", (class_id, actor))
        counts = {r["day"]: r["n"] for r in rows}
        return dict(today=counts.get(today.isoformat(), 0),
                    week=sum(n for d, n in counts.items() if today - timedelta(days=6) <= date.fromisoformat(d) <= today),
                    total=sum(counts.values()), streak=streak(list(counts), today))

    def top(self, class_id, actor, today):
        self.require(class_id, actor)
        return self.db.all("SELECT u.name,e.user_id,COUNT(*) n FROM message_events e JOIN users u ON u.id=e.user_id "
                           "JOIN members m ON m.class_id=e.class_id AND m.user_id=e.user_id WHERE e.class_id=? AND e.day>=? AND e.day<=? "
                           "GROUP BY e.user_id,u.name ORDER BY n DESC,e.user_id LIMIT 10",
                           (class_id, (today-timedelta(days=6)).isoformat(), today.isoformat()))

    def request_pair(self, class_id, actor, target):
        self.require(class_id, actor)
        self.require(class_id, target)
        if actor == target:
            raise ValueError("Выбери другого участника.")
        a, b = sorted((actor, target))
        existing = self.db.scalar("SELECT id FROM pairs WHERE class_id=? AND user_a=? AND user_b=?", (class_id, a, b))
        if existing:
            return existing
        if self.db.scalar("SELECT COUNT(*) FROM pairs WHERE class_id=? AND requester=? AND accepted=0", (class_id, actor)) >= 10:
            raise ValueError("Сначала дождись ответов на отправленные приглашения.")
        self.db.run("INSERT OR IGNORE INTO pairs(class_id,user_a,user_b,requester) VALUES(?,?,?,?)", (class_id, a, b, actor))
        return self.db.scalar("SELECT id FROM pairs WHERE class_id=? AND user_a=? AND user_b=?", (class_id, a, b))

    def answer_pair(self, class_id, actor, pair_id, accept):
        self.require(class_id, actor)
        pair = self.db.one("SELECT * FROM pairs WHERE class_id=? AND id=?", (class_id, pair_id))
        if not pair or actor not in (pair["user_a"], pair["user_b"]) or actor == pair["requester"]:
            raise AccessDenied("Это приглашение предназначено другому участнику.")
        if pair["accepted"]:
            raise ValueError("Приглашение уже принято. Управлять огоньком можно в разделе «Огоньки».")
        other = pair["user_b"] if actor == pair["user_a"] else pair["user_a"]
        self.require(class_id, other)
        if accept:
            self.db.run("UPDATE pairs SET accepted=1 WHERE id=?", (pair_id,))
        else:
            self.db.run("DELETE FROM pairs WHERE id=?", (pair_id,))

    def pairs(self, class_id, actor, today):
        self.require(class_id, actor)
        pairs = self.db.all("SELECT * FROM pairs WHERE class_id=? AND (user_a=? OR user_b=?)", (class_id, actor, actor))
        for pair in pairs:
            target = pair["user_a"] if actor == pair["user_b"] else pair["user_b"]
            pair["name"] = self.db.scalar("SELECT name FROM users WHERE id=?", (target,))
            days = self.db.all("SELECT day FROM message_events WHERE class_id=? AND user_id IN (?,?) GROUP BY day HAVING COUNT(DISTINCT user_id)=2",
                               (class_id, actor, target))
            pair["streak"] = streak([r["day"] for r in days], today) if pair["accepted"] else 0
        return pairs
