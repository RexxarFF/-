#!/usr/bin/env python3
"""Start the bot, validate the bundle, or make a consistent SQLite backup."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from classdiary.app import Application
from classdiary.config import ROOT, Config
from classdiary.db import Database
from classdiary.schedule import validate_lessons
from classdiary.telegram import Telegram, TelegramError


def check():
    template = json.loads((ROOT / "templates" / "9b.json").read_text(encoding="utf-8"))
    for weekday, lessons in template["week"].items():
        if not 0 <= int(weekday) <= 6:
            raise ValueError("Неверный день в шаблоне.")
        validate_lessons(lessons)
    db = Database(":memory:")
    db.close()
    print("OK: Python, база данных и расписание корректны. BOT_TOKEN и сеть для этой проверки не нужны.")


async def serve(config):
    log = logging.getLogger("classdiary")
    log.info("Открываем SQLite и проверяем возможность записи...")
    db = Database(config.database)
    telegram = Telegram(config.token)
    app = Application(config, db, telegram)
    background = None
    try:
        log.info("Подключаемся к Telegram: проверка токена (getMe)...")
        me = await telegram.call("getMe")
        app.username, app.bot_id = me["username"], me["id"]
        log.info("Токен принят: @%s. Подготавливаем получение сообщений...", app.username)
        # Keep queued updates: homework messages sent while offline must not be discarded.
        await telegram.call("deleteWebhook", drop_pending_updates=False)
        await telegram.call("setMyCommands", commands=[
            {"command": "menu", "description": "Главное меню"}, {"command": "hw", "description": "Общее ДЗ на завтра"},
            {"command": "my", "description": "Личные записи на завтра"}, {"command": "schedule", "description": "Расписание на завтра"},
            {"command": "stats", "description": "Статистика сообщений"}, {"command": "cancel", "description": "Отменить ввод"},
            {"command": "help", "description": "Как пользоваться"}])
        log.info("Started @%s. Polling. Press Ctrl+C to stop.", app.username)
        background = asyncio.create_task(app.background())
        offset = int(db.scalar("SELECT value FROM meta WHERE key='telegram_offset'") or 0)
        failures = 0
        while True:
            try:
                updates = await telegram.call("getUpdates", offset=offset, timeout=30, limit=50,
                                              allowed_updates=["message", "callback_query"])
                for update in updates:
                    if update["update_id"] < offset:
                        continue
                    try:
                        await app.on_update(update)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Never print raw update payloads, private text or token-bearing URLs.
                        log.error("Update %s failed: %s", update["update_id"], type(exc).__name__)
                    offset = update["update_id"] + 1
                    db.run("INSERT INTO meta VALUES('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                failures = 0
            except TelegramError as exc:
                if exc.code in (401, 409):
                    reason = "Токен неверен или отозван." if exc.code == 401 else "С этим токеном уже запущен другой экземпляр бота. Остановите его."
                    raise RuntimeError(reason) from None
                failures += 1
                delay = max(exc.retry_after, min(30, 2**min(failures, 5)))
                log.warning("Telegram unavailable (code %s), retry in %ss", exc.code, delay)
                await asyncio.sleep(delay)
    finally:
        if background:
            background.cancel()
            try:
                await background
            except asyncio.CancelledError:
                pass
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Классный дневник — Telegram-бот")
    parser.add_argument("--check", action="store_true", help="Проверить проект без токена и сети")
    parser.add_argument("--backup", metavar="PATH", help="Сохранить резервную копию базы")
    args = parser.parse_args()
    print(f"[ClassDiary 0.1.1] CLI started; Python {sys.version.split()[0]}", flush=True)
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее.", file=sys.stderr)
        return 1
    try:
        if args.check:
            check()
            return 0
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.getLogger("classdiary").info("Читаем BOT_TOKEN и настройки запуска...")
        config = Config.from_env()
        if args.backup:
            path = Path(args.backup).resolve()
            if path == config.database.resolve():
                raise ValueError("Резервная копия должна быть отдельным файлом.")
            db = Database(config.database)
            try:
                db.backup(path)
            finally:
                db.close()
            print(f"Резервная копия: {path}")
            return 0
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except (ValueError, RuntimeError, TelegramError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
