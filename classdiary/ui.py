from __future__ import annotations

import html
import json
from datetime import date

from .schedule import DAYS


def e(value):
    return html.escape(str(value), quote=False)


def button(text, data=None, url=None):
    if data is not None:
        if len(data.encode()) > 64:
            raise ValueError("Callback exceeds Telegram limit")
        return {"text": text[:64], "callback_data": data}
    return {"text": text[:64], "url": url}


def keyboard(*rows):
    return {"inline_keyboard": [list(row) for row in rows if row]}


def chunks(text, maximum=3800):
    # We split between complete HTML lines, never in the middle of an escaped entity.
    current = []
    size = 0
    for line in text.splitlines():
        if len(line) > maximum:
            raise ValueError("Строка слишком длинная для Telegram.")
        if current and size + len(line) + 1 > maximum:
            yield "\n".join(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        yield "\n".join(current)


def text_lines(text, width=600):
    """Escape user text only after breaking it into bounded plain-text lines."""
    return "\n".join(e(line[i:i+width]) for line in text.splitlines() for i in range(0, max(1, len(line)), width))


def date_label(day: date):
    return f"{day:%d.%m.%Y} · {DAYS[day.weekday()]}"


def lesson_label(row):
    audience = f" · {row['audience']}" if row.get("audience") else ""
    return f"{row['position']}. {row['subject']}{audience}"


def timetable_text(classroom, day, lessons):
    lines = [f"📅 <b>{e(classroom['name'])}</b>", f"<b>{date_label(day)}</b>", ""]
    if not lessons:
        lines.append("По расписанию уроков нет.")
    for row in lessons:
        time = f"  {e(row['starts'])}–{e(row['ends'])}" if row.get("starts") else ""
        room = f" · каб. {e(row['room'])}" if row.get("room") else ""
        lines.append(f"<b>{e(lesson_label(row))}</b>{time}{room}")
    return "\n".join(lines)


def homework_text(classroom, day, assignments, lessons, personal):
    title = "📝 Мои записи" if personal else "📚 Общее ДЗ"
    lines = [f"<b>{title} · {e(classroom['name'])}</b>", f"<b>{date_label(day)}</b>", ""]
    if personal:
        if not assignments:
            lines += ["Личных записей на эту дату нет.", "Общие задания доступны отдельной кнопкой «Общее ДЗ»."]
        for row in assignments:
            lines += assignment_lines(row)
        return "\n".join(lines)
    used = set()
    for lesson in lessons:
        lines.append(f"<b>{e(lesson_label(lesson))}</b>")
        matches = [x for x in assignments if x["subject"].casefold() == lesson["subject"].casefold()
                   and x["due_position"] in (0, lesson["position"])
                   and (x["audience_id"] == lesson["audience_id"] or not x["audience_id"] or not lesson["audience_id"])
                   and x["id"] not in used]
        if not matches:
            lines.append("ДЗ пока не записано.")
        for row in matches:
            used.add(row["id"])
            lines += assignment_lines(row, include_subject=False)
        lines.append("")
    extras = [x for x in assignments if x["id"] not in used]
    if extras:
        lines.append("<b>Другие задания на эту дату</b>")
        for row in extras:
            lines += assignment_lines(row)
    if not lessons and not assignments:
        lines.append("На эту дату уроков и записанных заданий нет.")
    return "\n".join(lines)


def assignment_lines(row, include_subject=True):
    done = "✅ " if row.get("done") else ""
    lines = []
    if include_subject:
        audience = f" · {e(row['audience'])}" if row.get("audience") else ""
        lines.append(f"<b>{done}{e(row['subject'])}{audience}</b>")
    elif done:
        lines.append("✅ Отмечено выполненным")
    lines.append(text_lines(row["body"]) if row["body"] else "Задание во вложениях.")
    count = len(json.loads(row["attachments"]))
    if count:
        lines.append(f"📎 Вложений: {count} — откройте задание кнопкой ниже.")
    if row.get("source_position"):
        lines.append(f"<i>Записано {date.fromisoformat(row['assigned_on']):%d.%m}, урок № {row['source_position']} · запись #{row['id']}</i>")
    lines.append("")
    return lines
