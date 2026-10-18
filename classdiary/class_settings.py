"""School calendar, automatic no-homework subjects and curator completeness."""
from datetime import date

from .config import today_in
from .schedule import SHORT_DAYS, subject_key
from .ui import button as b, date_label, e, keyboard as kb, lesson_label


class ClassSettings:
    ACTIONS = {"nohw", "nohwpage", "nohwtoggle", "schoolcal", "weekend", "breakadd", "breaksave",
               "breakshow", "breakdelete", "hwprogress", "fillhw"}

    def __init__(self, app):
        self.app, self.db, self.diary = app, app.db, app.diary

    async def no_homework(self, uid, cid, page=0, data=None):
        self.diary.require(cid, uid, "settings")
        data = data or self.app.new_state(uid, "nohw_settings", cid, subjects=self.diary.subject_catalog(cid, uid))
        subjects, nonce = data["subjects"], data["nonce"]
        page = max(0, min(page, max(0, (len(subjects)-1)//15)))
        data["page"] = page
        self.db.set_state(uid, "nohw_settings", data)
        selected = {subject_key(s) for s in self.diary.exempt_subjects(cid)}
        rows = [[b(("✅ " if subject_key(s) in selected else "▫️ ")+s, f"nohwtoggle:{cid}:{nonce}:{index}")]
                for index, s in enumerate(subjects) if page*15 <= index < (page+1)*15]
        nav = []
        if page:
            nav.append(b("◀️", f"nohwpage:{cid}:{nonce}:{page-1}"))
        if (page+1)*15 < len(subjects):
            nav.append(b("▶️", f"nohwpage:{cid}:{nonce}:{page+1}"))
        rows += [nav, [b("Назад", f"manage:{cid}")]]
        await self.app.tg.send(uid, "📚 <b>Предметы без домашки</b>\n\n"
            "Отметьте предметы, по которым обычно ничего не задают. Изменения сохраняются сразу.\n"
            "В общем ДЗ будет автоматически написано «ДЗ не задают». Если ответственный запишет задание, оно появится вместо этой подписи.\n"
            "Настройка действует для всех подгрупп выбранного предмета.\n\n"+
            (f"Выбрано: {len(selected)}." if subjects else "Сначала добавь предметы в расписание."),
            {"inline_keyboard": [row for row in rows if row]})

    async def calendar(self, uid, cid, page=0):
        self.diary.require(cid, uid, "schedule")
        self.db.clear_state(uid)
        weekends = {r["weekday"] for r in self.db.all("SELECT weekday FROM school_weekends WHERE class_id=?", (cid,))}
        rows = [[b(("🌙 " if i in weekends else "📚 ")+SHORT_DAYS[i], f"weekend:{cid}:{i}") for i in range(3)],
                [b(("🌙 " if i in weekends else "📚 ")+SHORT_DAYS[i], f"weekend:{cid}:{i}") for i in range(3, 6)]]
        periods = self.diary.school_breaks(cid, uid)
        page = max(0, min(page, max(0, (len(periods)-1)//8)))
        for period in periods[page*8:(page+1)*8]:
            start, end = date.fromisoformat(period["start_on"]), date.fromisoformat(period["end_on"])
            rows.append([b(f"{start:%d.%m.%y}–{end:%d.%m.%y} · {period['title']}", f"breakshow:{cid}:{period['id']}")])
        nav = []
        if page:
            nav.append(b("◀️ Периоды", f"schoolcal:{cid}:{page-1}"))
        if (page+1)*8 < len(periods):
            nav.append(b("Периоды ▶️", f"schoolcal:{cid}:{page+1}"))
        rows += [nav, [b("➕ Каникулы / выходной", f"breakadd:{cid}")], [b("Назад", f"manage:{cid}")]]
        await self.app.tg.send(uid, "📆 <b>Учебный календарь</b>\n\n"
            "🌙 — постоянный выходной; 📚 — день разрешён для уроков. Нажми день, чтобы переключить. Воскресенье всегда выходной.\n\n"
            "Каникулы и выбранные выходные скрывают уроки, но сохраняют недельное расписание. После окончания периода оно возвращается. "
            "Чтобы провести урок в такой день, сначала уберите соответствующий выходной.\n\n"
            "Кнопка следующего учебного дня пропускает даты без уроков. Уже записанное ДЗ автоматически не переносится.\n\n"
            f"Периодов отдыха: {len(periods)}.", {"inline_keyboard": [r for r in rows if r]})

    async def break_preview(self, uid, data):
        cid, nonce = data["cid"], data["nonce"]
        self.diary.require(cid, uid, "schedule")
        start, end = date.fromisoformat(data["start_on"]), date.fromisoformat(data["end_on"])
        await self.app.tg.send(uid, f"📆 <b>{e(data['title'])}</b>\n{date_label(start)}\n— {date_label(end)}\n\n"
            "Включая обе даты. В эти дни уроков не будет. Можно прислать другое название или сохранить период.",
            kb([b("💾 Сохранить период", f"breaksave:{cid}:{nonce}")], [b("Отмена", f"schoolcal:{cid}")]))

    async def progress(self, uid, cid, target="tomorrow"):
        classroom = self.diary.require(cid, uid, "homework")
        day = self.diary.resolve_school_date(cid, target, today_in(classroom["timezone"]))
        progress = self.diary.homework_progress(cid, uid, day)
        data = self.app.new_state(uid, "hw_progress", cid, day=day.isoformat(), lessons=[x["lesson"] for x in progress["blocks"]])
        rows, missing = [], []
        for index, block in enumerate(progress["blocks"]):
            lesson, status = block["lesson"], block["status"]
            label = lesson_label(lesson)
            if status == "missing":
                missing.append(label)
                rows.append([b("✏️ "+label, f"fillhw:{cid}:{data['nonce']}:{index}")])
        previous, following = (self.diary.next_school_day(cid, day, d) for d in (-1, 1))
        rows += [[b("◀️ День", f"hwprogress:{cid}:{previous}"), b("День ▶️", f"hwprogress:{cid}:{following}")],
                 [b("📚 Посмотреть ДЗ", f"v:{cid}:hw:{day}"), b("🔄 Обновить", f"hwprogress:{cid}:{day}")],
                 [b("В меню", f"home:{cid}")]]
        await self.app.send_long(uid, f"✏️ <b>Заполненность общего ДЗ</b>\n{date_label(day)}\n\n"
            f"<b>Заполнено {progress['filled']} из {progress['total']} занятий.</b>\n"
            "Подгруппы считаются отдельно. «Ничего не задано» и предметы без домашки считаются заполненными.\n\n"+
            ("⏳ Осталось записать:\n"+"\n".join(e(s) for s in missing)+"\n\nНажми предмет — дата выполнения уже будет выбрана."
             if missing else "✅ Все занятия заполнены." if progress["total"] else "На эту дату уроков нет."),
            {"inline_keyboard": rows})

    async def callback(self, uid, parts):
        action, cid = parts[0], int(parts[1])
        if action == "nohw":
            return await self.no_homework(uid, cid)
        if action in ("nohwpage", "nohwtoggle"):
            _, data = self.app.need_state(uid, cid, parts[2], "nohw_settings")
            self.diary.require(cid, uid, "settings")
            if action == "nohwtoggle":
                index = int(parts[3])
                if not 0 <= index < len(data["subjects"]):
                    raise ValueError("Список предметов изменился. Открой настройки заново.")
                self.diary.toggle_homework_exemption(cid, uid, data["subjects"][index])
            return await self.no_homework(uid, cid, int(parts[3]) if action == "nohwpage" else data["page"], data)
        if action == "schoolcal":
            return await self.calendar(uid, cid, int(parts[2]) if len(parts) > 2 else 0)
        if action == "weekend":
            self.diary.toggle_school_weekend(cid, uid, int(parts[2]))
            return await self.calendar(uid, cid)
        if action == "breakadd":
            classroom = self.diary.require(cid, uid, "schedule")
            return await self.app.open_calendar(uid, cid, "break_start", today_in(classroom["timezone"]))
        if action == "breaksave":
            _, data = self.app.need_state(uid, cid, parts[2], "break_confirm")
            self.diary.add_school_break(cid, uid, date.fromisoformat(data["start_on"]), date.fromisoformat(data["end_on"]), data["title"])
            return await self.calendar(uid, cid)
        if action in ("breakshow", "breakdelete"):
            self.diary.require(cid, uid, "schedule")
            period = self.db.one("SELECT * FROM school_breaks WHERE id=? AND class_id=?", (int(parts[2]), cid))
            if not period:
                raise ValueError("Период уже удалён или относится к другому классу.")
            if action == "breakdelete":
                self.diary.remove_school_break(cid, uid, period["id"])
                return await self.calendar(uid, cid)
            return await self.app.tg.send(uid, f"<b>{e(period['title'])}</b>\n{period['start_on']} — {period['end_on']}\n\nУбрать период отдыха?",
                kb([b("Убрать период", f"breakdelete:{cid}:{period['id']}")], [b("Назад", f"schoolcal:{cid}")]))
        if action == "hwprogress":
            return await self.progress(uid, cid, parts[2] if len(parts) > 2 else "tomorrow")
        if action == "fillhw":
            _, data = self.app.need_state(uid, cid, parts[2], "hw_progress")
            classroom = self.diary.require(cid, uid, "homework")
            index = int(parts[3])
            if not 0 <= index < len(data["lessons"]):
                raise ValueError("Открой заполненность заново.")
            lesson, day = data["lessons"][index], date.fromisoformat(data["day"])
            if not any(x["source_key"] == lesson["source_key"] for x in self.diary.lessons_on(cid, day)):
                raise ValueError("Расписание изменилось. Открой заполненность заново.")
            assigned = min(day, today_in(classroom["timezone"]))
            source = f"direct:{day}:{lesson['source_key']}"
            old = self.db.one("SELECT * FROM assignments WHERE class_id=? AND owner_id=0 AND source_key=? AND due_on=? ORDER BY id DESC LIMIT 1",
                              (cid, source, day.isoformat()))
            draft = self.app.to_draft(old) if old else dict(owner_id=0, source_key=source, source_position=lesson["position"],
                assigned_on=assigned.isoformat(), subject=lesson["subject"], audience_id=lesson["audience_id"],
                due_on=day.isoformat(), due_position=lesson["position"], body="", attachments=[], no_homework=False, revision=0)
            self.app.new_state(uid, "draft", cid, draft=draft, progress_day=day.isoformat())
            return await self.app.draft_preview(uid)
