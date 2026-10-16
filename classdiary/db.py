from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT, started INTEGER NOT NULL DEFAULT 0,
 active_class INTEGER, reminder_time TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS classes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, owner_id INTEGER NOT NULL,
 timezone TEXT NOT NULL, invite_token TEXT NOT NULL UNIQUE, group_id INTEGER UNIQUE,
 group_title TEXT, interactive INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS members (
 class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id), active INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(class_id,user_id)
);
CREATE TABLE IF NOT EXISTS roles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 name TEXT NOT NULL, permissions TEXT NOT NULL DEFAULT '[]', UNIQUE(class_id,name)
);
CREATE TABLE IF NOT EXISTS member_roles (
 class_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
 PRIMARY KEY(class_id,user_id,role_id),
 FOREIGN KEY(class_id,user_id) REFERENCES members(class_id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audiences (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 name TEXT NOT NULL, UNIQUE(class_id,name)
);
CREATE TABLE IF NOT EXISTS member_audiences (
 class_id INTEGER NOT NULL, user_id INTEGER NOT NULL, audience_id INTEGER NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
 PRIMARY KEY(class_id,user_id,audience_id),
 FOREIGN KEY(class_id,user_id) REFERENCES members(class_id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS lessons (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6), position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 16),
 subject TEXT NOT NULL, audience_id INTEGER NOT NULL DEFAULT 0,
 starts TEXT NOT NULL DEFAULT '', ends TEXT NOT NULL DEFAULT '', room TEXT NOT NULL DEFAULT '',
 archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lesson_day ON lessons(class_id,weekday,archived);
CREATE TABLE IF NOT EXISTS day_overrides (
 class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE, day TEXT NOT NULL,
 lessons TEXT NOT NULL, PRIMARY KEY(class_id,day)
);
CREATE TABLE IF NOT EXISTS assignments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 owner_id INTEGER NOT NULL DEFAULT 0, source_key TEXT NOT NULL, source_position INTEGER NOT NULL DEFAULT 0,
 assigned_on TEXT NOT NULL, subject TEXT NOT NULL, audience_id INTEGER NOT NULL DEFAULT 0,
 due_on TEXT NOT NULL, due_position INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL,
 attachments TEXT NOT NULL DEFAULT '[]', no_homework INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, editor_id INTEGER NOT NULL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(class_id,owner_id,source_key,assigned_on)
);
CREATE INDEX IF NOT EXISTS assignment_due ON assignments(class_id,owner_id,due_on);
CREATE TABLE IF NOT EXISTS completions (
 assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(assignment_id,user_id)
);
CREATE TABLE IF NOT EXISTS announcements (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 body TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]', author_id INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS announcement_reads (
 announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(announcement_id,user_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, unique_key TEXT NOT NULL UNIQUE, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 recipient INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending', retry_after REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS message_events (
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, class_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY(chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS events_member ON message_events(class_id,user_id,day);
CREATE TABLE IF NOT EXISTS pairs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
 user_a INTEGER NOT NULL, user_b INTEGER NOT NULL, requester INTEGER NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(class_id,user_a,user_b)
);
CREATE TABLE IF NOT EXISTS fsm (
 user_id INTEGER PRIMARY KEY, stage TEXT NOT NULL, data TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ui_panels (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), message_id INTEGER,
 media_messages TEXT NOT NULL DEFAULT '[]', temporary_messages TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL, actor INTEGER NOT NULL,
 action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """Single-process SQLite repository. Transactions contain no await points."""

    def __init__(self, path: Path | str):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(assignments)")}
        if "created_at" not in columns:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE assignments SET created_at=updated_at WHERE created_at=''")
        self.conn.execute("UPDATE deliveries SET state='pending' WHERE state='sending'")
        version = self.scalar("SELECT value FROM meta WHERE key='schema_version'")
        if version is not None and version != "1":
            raise RuntimeError("Версия базы не поддерживается этим выпуском. Сделайте резервную копию.")
        self.run("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")

    def run(self, sql, params=()):
        return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        row = self.run(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, params=()):
        return [dict(row) for row in self.run(sql, params).fetchall()]

    def scalar(self, sql, params=()):
        row = self.run(sql, params).fetchone()
        return row[0] if row else None

    @contextmanager
    def transaction(self):
        self.run("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.run("ROLLBACK")
            raise
        else:
            self.run("COMMIT")

    def state(self, user_id):
        row = self.one("SELECT * FROM fsm WHERE user_id=? AND updated_at>datetime('now','-1 day')", (user_id,))
        return (row["stage"], json.loads(row["data"])) if row else (None, {})

    def set_state(self, user_id, stage, data):
        self.run("INSERT INTO fsm(user_id,stage,data) VALUES(?,?,?) ON CONFLICT(user_id) "
                 "DO UPDATE SET stage=excluded.stage,data=excluded.data,updated_at=CURRENT_TIMESTAMP",
                 (user_id, stage, json.dumps(data, ensure_ascii=False)))

    def clear_state(self, user_id):
        self.run("DELETE FROM fsm WHERE user_id=?", (user_id,))

    def audit(self, class_id, actor, action, detail=""):
        self.run("INSERT INTO audit(class_id,actor,action,detail) VALUES(?,?,?,?)",
                 (class_id, actor, action, detail))

    def backup(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        other = sqlite3.connect(path)
        try:
            self.conn.backup(other)
        finally:
            other.close()

    def close(self):
        self.conn.close()
