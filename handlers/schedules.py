"""Расписание: загрузка PDF, разбор, кэш и выдача в чат.

Схема работы. Ссылку на PDF задаёт сис-админ (вкладка «Расписания»). Когда
студент смотрит расписание, бот лениво скачивает файл, разбирает его в занятия
и кладёт в таблицу lessons. Разбор кэшируется по отпечатку файла, поэтому повторное
открытие расписания не качает PDF заново; если файл на сервере обновили, отпечаток
изменится и разбор пройдёт сам.

Если разбор не удался (другой формат PDF, битый файл, нет pdfplumber), студент
получает ссылку как раньше, а сис-админ — запись с причиной в панели.
"""
import clock
import hashlib
import json
import logging
import os
import re
from datetime import datetime, time

import httpx

import config
import database as db
import repository as repo
from schedule_import import remember_week, saved_week, week_is_fresh
import timetable as tt
from handlers.common import api
from handlers.registry import callback, state
from max_api import btn
from utils import as_str

log = logging.getLogger("bot")

PDF_MAGIC = b"%PDF"
MAX_PDF_BYTES = 40 * 1024 * 1024  # расписание колледжа — пара мегабайт, не больше


class ScheduleResult:
    """Что удалось узнать о расписании группы: текст, источник и что пошло не так."""

    def __init__(self, group: str, schedule: tt.GroupSchedule | None = None,
                 url: str = "", error: str = "", from_cache: bool = False):
        self.group = group
        self.schedule = schedule
        self.url = url
        self.error = error
        self.from_cache = from_cache

    @property
    def has_lessons(self) -> bool:
        return bool(self.schedule and self.schedule.days)

    @property
    def text(self) -> str:
        return tt.format_schedule(self.schedule) if self.has_lessons else ""

    @property
    def reason(self) -> str:
        """Почему показана ссылка, а не расписание (для сис-админа)."""
        if self.has_lessons:
            return ""
        if not self.url:
            return "для группы не задан PDF"
        if self.error:
            return self.error
        return "в PDF не нашлось занятий для этой группы"


def cache_folder() -> str:
    folder = os.path.join(os.path.dirname(db.database_file()), "schedules")
    os.makedirs(folder, exist_ok=True)
    return folder


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


async def download(url: str) -> bytes:
    """Скачивает PDF с проверкой сигнатуры и размера (иначе в кэш попадёт HTML 404)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(url)
        response.raise_for_status()
    data = response.content
    if not data.startswith(PDF_MAGIC):
        raise ValueError("по ссылке не PDF (возможно, страница с ошибкой)")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("файл больше 40 МБ — похоже, это не расписание")
    return data


async def parse_group(group: str, force: bool = False) -> ScheduleResult:
    """Возвращает расписание группы, при необходимости скачав и разобрав PDF.

    force=True — перечитать файл, даже если разбор свежий (кнопка «Обновить»
    и проверка в панели).
    """
    code = repo._code(group)  # noqa: SLF001 — нормализация кода группы одна на весь проект
    row = await repo.get_schedule(code)
    if not row:
        return ScheduleResult(group=code)
    url = as_str(row["pdf_url"])
    stamp = repo.schedule_stamp(row)
    if not url:
        return ScheduleResult(group=code, error="для группы не задан PDF")

    # 1. Разбор в базе ещё свежий — отдаём его, не качая файл. Через
    #    SCHEDULE_CACHE_HOURS PDF скачивается заново: так подхватывается новая
    #    версия расписания, выложенная на сайте, без лишних запросов на каждый клик.
    if not force and repo.stamp_is_fresh(stamp, config.SCHEDULE_CACHE_HOURS):
        schedule = await schedule_from_db(code)
        if schedule.days:
            return ScheduleResult(group=code, schedule=schedule, url=url, from_cache=True)

    try:
        data = await download(url)
    except Exception as exc:  # noqa: BLE001 — сеть и формат файла: причина уходит в панель
        reason = f"не скачался PDF: {exc}"
        await _remember_failure(code, reason)
        return ScheduleResult(group=code, url=url, error=reason)

    digest = file_hash(data)
    if not force and repo.stamp_matches_file(stamp, digest):
        # файл не изменился — только освежаем время разбора
        # время разбора - локальное, как и всё остальное в базе
        await db.run("UPDATE schedules SET parsed_at=? WHERE group_code=?", (clock.stamp(), code))
        schedule = await schedule_from_db(code)
        if schedule.days:
            return ScheduleResult(group=code, schedule=schedule, url=url, from_cache=True)

    folder = cache_folder()
    os.makedirs(folder, exist_ok=True)  # папка могла быть удалена вручную
    path = os.path.join(folder, f"{code}_{digest}.pdf")
    if not os.path.exists(path):  # кэш на диске: повторный разбор не качает файл заново
        with open(path, "wb") as handle:
            handle.write(data)

    times = await lesson_times()
    try:
        schedule, pages = tt.parse_pdf_bytes(data, code, times)
    except Exception as exc:  # noqa: BLE001 — pdfplumber падает на кривых PDF
        reason = f"не разобрался PDF: {exc}"
        await _remember_failure(code, reason)
        return ScheduleResult(group=code, url=url, error=reason)

    found = tt.groups_in_pages(pages)
    if not schedule.days:
        # Возможно, код группы в справочнике отличается от написания в PDF:
        # тогда ищем ближайший по совпадению и пробуем ещё раз.
        matches = tt.match_groups(code, found)
        if matches:
            schedule, _ = tt.parse_pdf_bytes(data, matches[0], times)
    if not schedule.days:
        reason = f"в PDF не нашлось занятий группы {code}" + (f" (найдены: {', '.join(found[:8])})" if found else "")
        await _remember_failure(code, reason, digest, found)
        return ScheduleResult(group=code, url=url, error=reason)

    # Тот же фильтр недели, что и при импорте со страницы: файл постарше уже
    # сохранённого не должен откатывать свежую неделю. Базу не трогаем и
    # показываем то, что в ней уже лежит, - иначе студент увидит либо пустоту,
    # либо устаревшие пары.
    if not await week_is_fresh(code, schedule.week, url):
        stored = await schedule_from_db(code)
        if stored.days:
            # Файл мы посмотрели, и он не подходит: запоминаем его отпечаток,
            # чтобы следующий круг не качал и не разбирал его заново. Занятия в
            # базе остаются от свежей недели - см. schedule_import.week_is_fresh.
            await _remember_seen(code, digest)
            return ScheduleResult(group=code, schedule=stored, url=url)

    await repo.save_lessons(code, schedule, digest, found)
    if schedule.week:
        # Неделя, за которую теперь лежит расписание группы. Из неё потом
        # берётся и защита от отката старым файлом, и ответ в уведомлении
        # «с какой даты это действует» - в самой таблице lessons недели нет.
        await remember_week(code, schedule.week)
    await _prune_cache(digest)
    log.info("расписание %s разобрано: %s пар по %s дням", code, schedule.lessons_count, len(schedule.days))
    return ScheduleResult(group=code, schedule=schedule, url=url)


async def _remember_failure(code: str, reason: str, digest: str = "", found: list[str] | None = None) -> None:
    """Запоминает неудачу, чтобы панель показала причину, а бот не дёргал сайт по каждому клику."""
    await db.run(
        "UPDATE schedules SET parse_error=?, parsed_hash=?, found_groups=?, parsed_at=? "
        "WHERE group_code=?",
        (reason[:200], digest, ",".join(found or []), clock.stamp(), code),
    )
    log.warning("расписание %s: %s", code, reason)


async def _remember_seen(code: str, digest: str) -> None:
    """Файл посмотрен, но его неделя старше уже сохранённой: запоминаем отпечаток.

    Занятия не трогаем. Без этого бот качал бы и разбирал один и тот же старый
    файл на каждом круге слежения, ничего не записывая и никого не уведомляя.
    """
    await db.run("UPDATE schedules SET parsed_hash=?, parsed_at=? WHERE group_code=?",
                 (digest, clock.stamp(), code))


async def _prune_cache(keep: str) -> None:
    """Оставляет в кэше на диске последние файлы: старые PDF больше не нужны."""
    folder = cache_folder()
    files = [name for name in os.listdir(folder) if name.endswith(".pdf")]
    files.sort(key=lambda name: os.path.getmtime(os.path.join(folder, name)), reverse=True)
    for name in files[config.SCHEDULE_CACHE_FILES:]:
        if digest_of(name) == keep:
            continue
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            continue


def digest_of(filename: str) -> str:
    return filename.split("_", 1)[1].rsplit(".", 1)[0] if "_" in filename else ""


def _looks_like_time(value) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", as_str(value).strip()))


async def lesson_times() -> tuple:
    """Звонки по номерам уроков: [(начало, конец), ...], индекс = номер урока − 1.

    Настройка lesson_times может быть записана по урокам ({"1": ["08:00", "08:45"]})
    или по парам ({"1": [["08:00", "08:45"], ["08:50", "09:35"]]}) — timetable
    переводит оба вида в плоский список. Битое значение игнорируем.
    """
    raw = as_str(await db.get_setting("lesson_times", ""))
    if not raw:
        return tt.LESSON_TIMES
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("настройка lesson_times повреждена, беру значения по умолчанию")
        return tt.LESSON_TIMES
    return tt.normalize_times(data) if isinstance(data, dict) else tt.LESSON_TIMES


def times_to_json(times) -> str:
    """Обратное преобразование для сохранения настройки из панели."""
    return json.dumps({str(number): [start, end] for number, (start, end) in enumerate(times, start=1)
                       if start}, ensure_ascii=False)


async def schedule_from_db(group: str) -> tt.GroupSchedule:
    """Собирает GroupSchedule из строк lessons — то, что уже разобрано."""
    code = repo._code(group)  # noqa: SLF001
    schedule = tt.GroupSchedule(group=code)
    for row in await repo.lessons_for_group(code):
        weekday = int(row["weekday"])
        day = schedule.days.setdefault(weekday, tt.DaySchedule(weekday=weekday))
        day.lessons.append(tt.Lesson(
            number=int(row["lesson_num"]), subject=as_str(row["subject"]),
            teacher=as_str(row["teacher"]), room=as_str(row["room"]),
            start=as_str(row["start"]), end=as_str(row["end"]),
        ))
    for day in schedule.days.values():
        day.lessons.sort(key=lambda lesson: lesson.number)
    return schedule


async def refresh_all(limit: int = 50) -> dict:
    """Перечитывает расписания всех групп. Возвращает {группа: результат}.

    Используется кнопкой в панели и после смены ссылки на PDF: подписчикам уходит
    уведомление, если занятия действительно изменились.
    """
    results: dict[str, ScheduleResult] = {}
    for row in await repo.schedule_groups(limit):
        code = as_str(row["group_code"])
        before = await lessons_snapshot(code)
        result = await parse_group(code, force=True)
        results[code] = result
        if not result.has_lessons:
            continue
        if await lessons_snapshot(code) != before:
            await notify_changed(code, result, before)
    return results


# ── уведомление об изменениях ─────────────────────────────────────────────────
# Текст собирается здесь, а не в боте и не в панели: сообщение о перемене пишут
# два пути - фоновое слежение (schedule_watch) и кнопка «Обновить всё» в панели, -
# и студент должен получать одно и то же.


async def lessons_snapshot(group: str) -> dict:
    """Снимок занятий группы: (день недели, номер урока) -> (предмет, кабинет,
    преподаватель, начало, конец).

    Сравнение снимков «до» и «после» разбора отвечает сразу на два вопроса:
    менялись ли занятия вообще (иначе уведомлять не о чем) и что именно
    поменялось (иначе студент не поймёт, зачем ему это сообщение).
    """
    snapshot: dict = {}
    for row in await repo.lessons_for_group(group):
        snapshot[(int(row["weekday"]), int(row["lesson_num"]))] = (
            as_str(row["subject"]), as_str(row["room"]), as_str(row["teacher"]),
            as_str(row["start"]), as_str(row["end"]))
    return snapshot


def changes_lines(before: dict, after: dict, limit: int = 6) -> list[str]:
    """Что поменялось в расписании - строки для уведомления."""
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        weekday, number = key
        was, now = before.get(key), after.get(key)
        place = f"{clock.WEEKDAYS[weekday % 7]}, {number} урок"
        if was is None:
            lines.append(f"• {place}: добавили {now[0]}")
        elif now is None:
            lines.append(f"• {place}: убрали {was[0]}")
        else:
            parts = []
            if was[0] != now[0]:
                parts.append(f"«{was[0]}» → «{now[0]}»")
            if was[1] != now[1]:
                parts.append(f"аудитория {was[1] or '—'} → {now[1] or '—'}")
            if was[3:] != now[3:]:
                parts.append(f"время {was[3] or '—'}–{was[4] or '—'}"
                             f" → {now[3] or '—'}–{now[4] or '—'}")
            if parts:
                lines.append(f"• {place}: " + ", ".join(parts))
    if len(lines) > limit:
        lines = lines[:limit] + [f"• …и ещё {len(lines) - limit}."]
    return lines


def week_line(week) -> str:
    """С какой даты действует новое расписание."""
    if not week:
        return ""
    monday = tt.today_monday()
    if week == monday:
        return "Действует с этой недели."
    if week > monday:
        return f"Действует с недели {week:%d.%m.%Y}."
    return f"Файл за неделю с {week:%d.%m.%Y} — свежее расписание колледж ещё не выложил."


def nearest_lines(schedule, limit: int = 3) -> str:
    """Ближайшие занятия с датами недели самого файла.

    tt.format_upcoming считает даты от сегодняшнего дня, а колледж выкладывает
    файл на конкретную неделю: для следующей недели он показал бы сегодняшние
    даты. Поэтому опорный момент - понедельник недели из PDF.
    """
    if not schedule or not schedule.days:
        return ""
    if schedule.week and schedule.week != tt.today_monday():
        items = schedule.upcoming(now=datetime.combine(schedule.week, time.min), limit=limit)
    else:
        items = schedule.upcoming(limit=limit)
    if not items:
        return ""
    lines = ["⏰ Ближайшие занятия"]
    for day, lesson, day_date in items:
        lines.append(f"{day_date:%d.%m.%Y} · {clock.WEEKDAYS[day.weekday]} · {lesson.line()}")
    return "\n".join(lines)


async def change_notice(group: str, schedule=None, before: dict | None = None,
                        intro: str = "обновилось") -> str:
    """Текст уведомления подписчику: что изменилось, с какой даты и что делать.

    schedule - разбор файла, в нём есть неделя PDF (в базе недели нет, она лежит
    в настройках). before - снимок занятий ДО разбора: без него в сообщении не
    будет строки «что именно поменялось».
    """
    code = repo._code(group)  # noqa: SLF001 - нормализация кода группы одна на весь проект
    if schedule is None or not getattr(schedule, "days", None):
        schedule = await schedule_from_db(code)
    parts = [f"🔔 Расписание группы {code} {intro}."]
    week = getattr(schedule, "week", None) or await saved_week(code)
    if week:
        parts.append(week_line(week))
    if before:
        lines = changes_lines(before, await lessons_snapshot(code))
        if lines:
            parts.append("\n".join(lines))
    parts.append("Ничего делать не нужно — посмотрите расписание и приходите к своим парам.")
    nearest = nearest_lines(schedule)
    if nearest:
        parts.append(nearest)
    return "\n\n".join(part for part in parts if part)


async def notify_changed(group: str, result: ScheduleResult, before: dict | None = None) -> None:
    """Сообщает подписчикам группы, что расписание обновилось."""
    from handlers.admin import notify_schedule_subscribers  # импорт внутри: избегаем цикла

    code = repo._code(group)  # noqa: SLF001
    await notify_schedule_subscribers(code, await change_notice(code, result.schedule, before))


# ── расписание преподавателя ─────────────────────────────────────────────────
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def format_teacher_week(days: dict) -> str:
    """Неделя преподавателя текстом: день, урок, предмет, группа, аудитория."""
    if not days:
        return "📅 Занятий не найдено"
    lines = []
    for weekday in sorted(days):
        lines.append(f"\n📅 {WEEKDAYS[weekday % 7]}")
        for number, subject, group, room in days[weekday]:
            where = f" · {room}" if room else ""
            lines.append(f"   {number} урок · {subject} · {group}{where}")
    return "\n".join(lines)


@callback("teacher")
async def cb_teacher_search(x, arg):
    """Кто ведёт: ищем преподавателя по фамилии и показываем его неделю."""
    found = await repo.search_teachers(as_str(arg))
    if not found:
        return await api.send(
            x, "🔍 Никого не нашлось. Напишите фамилию преподавателя — например, часть фамилии.",
            [[btn("🔍 Искать ещё", "teacherask")], [btn("↩️ В меню", "home")]])
    if len(found) == 1:
        return await show_teacher(x, found[0])
    keyboard = [[btn(name, f"teacher:{name}")] for name in found[:8]]
    keyboard += [[btn("🔍 Искать ещё", "teacherask")], [btn("↩️ В меню", "home")]]
    return await api.send(x, "🔍 Кого показать?", keyboard)


async def show_teacher(x: str, teacher: str) -> None:
    """Неделя преподавателя: группы, где он ведёт, и его занятия по дням."""
    days = await repo.lessons_for_teacher(teacher)
    groups = await repo.teacher_groups(teacher)
    head = f"👨‍🏫 {teacher}"
    if groups:
        head += f"\nГруппы: {', '.join(groups[:12])}"
    await api.send(x, f"{head}\n\n{format_teacher_week(days)}",
                   [[btn("🔍 Искать ещё", "teacherask")], [btn("↩️ В меню", "home")]])


@callback("teacherask")
async def cb_teacher_ask(x, arg):
    """Запрашиваем фамилию для поиска по расписанию."""
    await db.set_state(x, "teacher_search", {})
    await api.send(x, "Напишите фамилию или часть фамилии преподавателя:")


@state("teacher_search")
async def st_teacher_search(x, text, p):
    """Фамилия из сообщения -> подходящие преподаватели или сразу неделя."""
    await db.clear_state(x)
    needle = as_str(text).strip()
    if not needle:
        return await cb_teacher_search(x, "")
    return await cb_teacher_search(x, needle)
