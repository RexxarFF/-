from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
SHORT_DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
SCHOOL_DAYS = DAYS[:6]


def subject_key(subject):
    return " ".join(subject.casefold().replace("ё", "е").split())


def homework_blocks(assignments, lessons, exemptions=()):
    """One status per scheduled lesson/subgroup; no automatic assignment records."""
    exempt = {subject_key(s) for s in exemptions}
    blocks = []
    for lesson in lessons:
        matches = [a for a in assignments if subject_key(a["subject"]) == subject_key(lesson["subject"])
                   and a["due_position"] in (0, lesson["position"])
                   and (a["audience_id"] == lesson["audience_id"] or not a["audience_id"] or not lesson["audience_id"])]
        status = "recorded" if matches else "exempt" if subject_key(lesson["subject"]) in exempt else "missing"
        blocks.append(dict(lesson=lesson, assignments=matches, status=status))
    return blocks


def parse_date(value: str, today: date) -> date:
    value = value.strip().lower()
    if value in ("сегодня", "today"):
        return today
    if value in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    if value in ("вчера", "yesterday"):
        return today - timedelta(days=1)
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", value):
        value = f"{value}.{today.year}"
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed
        except ValueError:
            pass
    raise ValueError("Дата: ДД.ММ.ГГГГ, например 21.10.2026.")


def validate_lesson(row: dict) -> dict:
    position = int(row["position"])
    subject = str(row["subject"]).strip()
    audience = str(row.get("audience", "")).strip()
    start, end = str(row.get("starts", "")), str(row.get("ends", ""))
    room = str(row.get("room", "")).strip()
    if not 1 <= position <= 16:
        raise ValueError("Номер урока должен быть от 1 до 16.")
    if not subject or len(subject) > 80 or len(audience) > 60 or len(room) > 30:
        raise ValueError("Предмет: 1–80 символов; подгруппа: до 60; кабинет: до 30.")
    if bool(start) != bool(end):
        raise ValueError("Укажи и начало, и конец урока.")
    for clock in (start, end):
        if clock and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise ValueError("Время указывается как 09:00–09:45.")
    if start and start >= end:
        raise ValueError("Конец урока должен быть позже начала.")
    return dict(position=position, subject=subject, audience=audience, starts=start, ends=end, room=room)


def validate_lessons(rows: list[dict]) -> list[dict]:
    if len(rows) > 64:
        raise ValueError("Слишком много строк: максимум 64 на день.")
    result = [validate_lesson(row) for row in rows]
    seen = set()
    for row in result:
        key = (row["position"], row["audience"].casefold())
        if key in seen:
            raise ValueError("Урок с таким номером и подгруппой уже есть.")
        if (row["position"], "") in seen or (not key[1] and any(p == key[0] for p, _ in seen)):
            raise ValueError("В одном уроке используй либо общую строку, либо отдельные подгруппы.")
        seen.add(key)
    return sorted(result, key=lambda row: (row["position"], row["audience"]))


def parse_day(text: str) -> list[dict]:
    """N; subject; audience; start-end; room. Blank day explicitly means a day off."""
    if text.strip().lower() in ("выходной", "нет уроков", "-"):
        return []
    rows = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = [x.strip() for x in raw.split(";")]
        if not 2 <= len(parts) <= 5:
            raise ValueError(f"Строка {number}: формат «номер; предмет; подгруппа; начало-конец; кабинет».")
        parts += [""] * (5 - len(parts))
        try:
            position = int(parts[0])
        except ValueError:
            raise ValueError(f"Строка {number}: сначала укажи номер урока.") from None
        time = re.split(r"\s*[-–—]\s*", parts[3]) if parts[3] else ["", ""]
        if len(time) != 2:
            raise ValueError(f"Строка {number}: время в формате 09:00-09:45.")
        rows.append(dict(position=position, subject=parts[1], audience=parts[2],
                         starts=time[0], ends=time[1], room=parts[4]))
    if not rows:
        raise ValueError("Пришли уроки или слово «выходной».")
    return validate_lessons(rows)


def streak(days: list[str], today: date) -> int:
    active = {date.fromisoformat(d) for d in days}
    cursor = today if today in active else today - timedelta(days=1)
    count = 0
    while cursor in active:
        count += 1
        cursor -= timedelta(days=1)
    return count
