"""Explicit classroom requests in ordinary group messages; no external AI service."""
import re


VIEW_COMMANDS = {"/hw": ("hw", "tomorrow"), "/tomorrow": ("hw", "tomorrow"),
                 "/schedule": ("week", "today"), "/my": ("mine", "tomorrow"), "/stats": ("stats", "today"),
                 "/pairs": ("pairs", "today"), "/interactive": ("pairs", "today")}
GROUP_COMMANDS = {"/start", "/menu", "/help", "/groupcheck"} | VIEW_COMMANDS.keys()


def parse_command(text, username):
    parts = text.strip().split(maxsplit=1)
    command, payload = (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")
    if command.startswith("/"):
        command, separator, target = command.partition("@")
        if separator and target.casefold() != username.casefold():
            return None, ""
        return command.casefold(), payload
    if command.startswith("@") and command.rstrip(",!:")[1:].casefold() != username.casefold():
        return None, ""
    return command, payload


def parse_group_request(text):
    text = text.casefold().replace("ё", "е").strip()
    text = re.sub(r"^@[a-z0-9_]+\s*[,!:]?\s*", "", text)
    text = re.sub(r"[?!.,]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    if text in ("статистика", "моя статистика", "статистика сообщений", "моя серия"):
        return "stats", "today"
    if text in ("огоньки", "мои огоньки", "парные огоньки", "интерактив"):
        return "pairs", "today"
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
