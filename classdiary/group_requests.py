"""Explicit classroom requests in ordinary group messages; no external AI service."""
import re


VIEW_COMMANDS = {"/hw": ("hw", "tomorrow"), "/tomorrow": ("hw", "tomorrow"),
                 "/schedule": ("week", "today"), "/my": ("mine", "tomorrow"), "/stats": ("stats", "today"),
                 "/pairs": ("pairs", "today"), "/interactive": ("pairs", "today"), "/progress": ("progress", "tomorrow")}
GROUP_COMMANDS = {"/start", "/menu", "/cancel", "/help", "/groupcheck", "/invite"} | VIEW_COMMANDS.keys()
WORD_COMMANDS = {
    "меню": "/menu", "бот": "/menu", "главное меню": "/menu", "начать": "/menu",
    "отмена": "/cancel", "назад": "/cancel",
    "дз": "/hw", "домашка": "/hw", "домашнее задание": "/hw",
    "мои записи": "/my", "мое дз": "/my", "личное дз": "/my", "личные записи": "/my",
    "расп": "/schedule", "расписание": "/schedule", "уроки": "/schedule",
    "стата": "/stats", "стат": "/stats", "статистика": "/stats", "моя статистика": "/stats", "мои сообщения": "/stats",
    "огоньки": "/pairs", "огонек": "/pairs", "мои огоньки": "/pairs", "интерактив": "/pairs",
    "пригласить": "/invite", "пригласить класс": "/invite", "пригласить всех": "/invite",
    "приглашение": "/invite", "приглашение в класс": "/invite", "инвайт": "/invite",
    "помощь": "/help", "команды": "/help", "проверка группы": "/groupcheck",
    "заполненность": "/progress", "заполненность дз": "/progress", "проверить дз": "/progress",
}


def parse_command(text, username, allow_words=True):
    parts = text.strip().split(maxsplit=1)
    command, payload = (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")
    if command.startswith("/"):
        command, separator, target = command.partition("@")
        if separator and target.casefold() != username.casefold():
            return None, ""
        return command.casefold(), payload
    if command.startswith("@") and command.rstrip(",!:")[1:].casefold() != username.casefold():
        return None, ""
    words = re.sub(r"^@[a-z0-9_]+\s*[,!:]?\s*", "", text.strip(), flags=re.IGNORECASE)
    words = re.sub(r"\s+", " ", words.casefold().replace("ё", "е")).strip().rstrip("?!. ")
    if words in WORD_COMMANDS and (allow_words or words in {"меню", "отмена", "назад"}):
        return WORD_COMMANDS[words], ""
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
    if re.fullmatch(rf"(?:(?:какое|что за) )?{homework}(?: (?:у нас|задано|задали))? (?:на )?(завтра|сегодня)", text):
        return "hw", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"что (?:нам )?(?:задано|задали)(?: (?:у нас|нам))? на (?:завтра|сегодня)", text):
        return "hw", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"(?:какое )?(?:расписание(?: уроков)?|расп|уроки) (?:на )?(?:завтра|сегодня)", text):
        return "schedule", "tomorrow" if text.endswith("завтра") else "today"
    if re.fullmatch(r"(?:какое )?расписание(?: на (?:всю )?неделю)?", text):
        return "week", "today"
    return None
