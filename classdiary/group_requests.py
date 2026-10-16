"""Explicit classroom requests in ordinary group messages; no external AI service."""
import re


def parse_group_request(text):
    text = text.casefold().replace("ё", "е").strip()
    text = re.sub(r"^@[a-z0-9_]+\s*[,!:]?\s*", "", text)
    text = re.sub(r"[?!.,]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    homework = r"(?:дз|д/з|д\.з\.|домашка|домашнее задание)"
    if re.fullmatch(rf"(?:(?:какое|что за) )?{homework}(?: (?:у нас|задано|задали))? на (завтра|сегодня)", text):
        return "hw", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"что (?:нам )?(?:задано|задали)(?: (?:у нас|нам))? на (?:завтра|сегодня)", text):
        return "hw", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"(?:какое )?расписание(?: уроков)? на (?:завтра|сегодня)", text):
        return "schedule", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"(?:какое )?расписание(?: на (?:всю )?неделю)?", text):
        return "week", "today"
    return None
