from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Read simple KEY=value lines; never evaluate shell expressions."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def get_timezone(name: str):
    if name in ("UTC", "Etc/UTC"):
        return timezone.utc
    if re.fullmatch(r"[+-]\d{2}:\d{2}", name):
        hours, minutes = map(int, name[1:].split(":"))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError("Смещение должно быть от −14:00 до +14:00.")
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(offset if name[0] == "+" else -offset)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows does not ship the IANA database. Moscow has a fixed UTC+3.
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), "Europe/Moscow")
        raise ValueError("Неизвестный пояс. Используйте, например, +03:00.") from None


def today_in(name: str):
    return datetime.now(get_timezone(name)).date()


@dataclass(frozen=True)
class Config:
    token: str
    database: Path
    default_timezone: str = "Europe/Moscow"
    max_classes_per_owner: int = 10
    homework_retention_days: int = 14

    @classmethod
    def from_env(cls, require_token=True) -> "Config":
        load_env(ROOT / ".env")
        token = os.environ.get("BOT_TOKEN", "").strip()
        if require_token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажите настоящий BOT_TOKEN в файле .env. Токен выдаёт @BotFather.")
        db = Path(os.environ.get("DATABASE_PATH", "data/diary.sqlite3"))
        if not db.is_absolute():
            db = ROOT / db
        zone = os.environ.get("DEFAULT_TIMEZONE", "Europe/Moscow")
        get_timezone(zone)
        maximum = int(os.environ.get("MAX_CLASSES_PER_OWNER", "10"))
        if maximum < 1:
            raise ValueError("MAX_CLASSES_PER_OWNER должен быть положительным.")
        retention = int(os.environ.get("HOMEWORK_RETENTION_DAYS", "14"))
        if not 1 <= retention <= 365:
            raise ValueError("HOMEWORK_RETENTION_DAYS должен быть от 1 до 365.")
        return cls(token, db, zone, maximum, retention)
