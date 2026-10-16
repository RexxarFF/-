import unittest

from classdiary.group_requests import parse_command, parse_group_request


class GroupRequestTests(unittest.TestCase):
    def test_explicit_homework_questions(self):
        for text in ("Что задано на завтра?", "что нам задали на завтра", "ДЗ на завтра", "домашнее задание на завтра",
                     "@ClassDiaryBot, что задано на завтра?", "  Какое   дз на завтра?! ", "Д.З. на завтра"):
            with self.subTest(text=text):
                self.assertEqual(parse_group_request(text), ("hw", "tomorrow"))
        self.assertEqual(parse_group_request("что задано на сегодня"), ("hw", "today"))

    def test_schedule_day_and_week(self):
        self.assertEqual(parse_group_request("Расписание на завтра?"), ("schedule", "tomorrow"))
        self.assertEqual(parse_group_request("Какое расписание уроков на сегодня"), ("schedule", "today"))
        self.assertEqual(parse_group_request("расписание на всю неделю"), ("week", "today"))

    def test_interactive_phrases_are_recognized_explicitly(self):
        self.assertEqual(parse_group_request("Моя статистика?"), ("stats", "today"))
        self.assertEqual(parse_group_request("мои огоньки"), ("pairs", "today"))
        self.assertEqual(parse_group_request("Интерактив"), ("pairs", "today"))
        self.assertIsNone(parse_group_request("У нас хорошая статистика"))

    def test_unrelated_conversation_does_not_trigger_bot(self):
        for text in ("Завтра сделаю дз", "Никто не знает, что задано на завтра? Я потом уточню", "Привет", "", "@all"):
            with self.subTest(text=text):
                self.assertIsNone(parse_group_request(text))

    def test_only_own_addressed_commands_are_accepted(self):
        self.assertEqual(parse_command("/menu@Class_Bot", "class_bot"), ("/menu", ""))
        self.assertEqual(parse_command("/start@class_bot\nbind_123", "class_bot"), ("/start", "bind_123"))
        self.assertEqual(parse_command("/menu@other_bot", "class_bot"), (None, ""))
        self.assertEqual(parse_command("@other_bot, что задано на завтра", "class_bot"), (None, ""))
