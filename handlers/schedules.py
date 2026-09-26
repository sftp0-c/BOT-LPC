"""Расписание: загрузка PDF, разбор, кэш и выдача в чат.

Схема работы. Ссылку на PDF задаёт сис-админ (вкладка «Расписания»). Когда
студент смотрит расписание, бот лениво скачивает файл, разбирает его в занятия
и кладёт в таблицу lessons. Разбор кэшируется по отпечатку файла, поэтому повторное
открытие расписания не качает PDF заново; если файл на сервере обновили, отпечаток
изменится и разбор пройдёт сам.

Если разбор не удался (другой формат PDF, битый файл, нет pdfplumber), студент
получает ссылку как раньше, а сис-админ — запись с причиной в панели.
"""
import hashlib
import json
import logging
import os
import re

import httpx

import config
import database as db
import repository as repo
import timetable as tt
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
        await db.run("UPDATE schedules SET parsed_at=datetime('now') WHERE group_code=?", (code,))
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

    await repo.save_lessons(code, schedule, digest, found)
    await _prune_cache(digest)
    log.info("расписание %s разобрано: %s пар по %s дням", code, schedule.lessons_count, len(schedule.days))
    return ScheduleResult(group=code, schedule=schedule, url=url)


async def _remember_failure(code: str, reason: str, digest: str = "", found: list[str] | None = None) -> None:
    """Запоминает неудачу, чтобы панель показала причину, а бот не дёргал сайт по каждому клику."""
    await db.run(
        "UPDATE schedules SET parse_error=?, parsed_hash=?, found_groups=?, parsed_at=datetime('now') "
        "WHERE group_code=?",
        (reason[:200], digest, ",".join(found or []), code),
    )
    log.warning("расписание %s: %s", code, reason)


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
        before = await repo.lessons_count(code)
        before_text = await _signature(code)
        result = await parse_group(code, force=True)
        results[code] = result
        if not result.has_lessons:
            continue
        changed = await _signature(code) != before_text or (await repo.lessons_count(code)) != before
        if changed:
            await notify_changed(code, result)
    return results


async def _signature(group: str) -> str:
    """Отпечаток содержимого расписания: нужен, чтобы понять, что занятия поменялись."""
    parts = [f"{r['weekday']}:{r['lesson_num']}:{as_str(r['subject'])}:{as_str(r['room'])}"
             for r in await repo.lessons_for_group(group)]
    return "|".join(parts)


async def notify_changed(group: str, result: ScheduleResult) -> None:
    """Сообщает подписчикам группы, что расписание обновилось."""
    from handlers.admin import notify_schedule_subscribers  # импорт внутри: избегаем цикла

    head = f"🔔 Расписание группы {group} обновилось."
    upcoming = tt.format_upcoming(result.schedule) if result.has_lessons else ""
    await notify_schedule_subscribers(group, f"{head}\n\n{upcoming}" if upcoming else head)
