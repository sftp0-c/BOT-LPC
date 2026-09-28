"""Расписания: ссылка на PDF, разобранные занятия и подписки на них.

Разбор PDF живёт в timetable.py, здесь - только хранение результата и всё,
что с ним делают: правка времени вручную, пересчёт под новые звонки, подписки."""

from datetime import timedelta

import clock
import database as db
from utils import as_str

from .common import _code
from .groups import upsert_group


# ── расписания ────────────────────────────────────────────────────────────────
async def get_schedule(group_code: str):
    code = _code(group_code)
    return await db.one("SELECT * FROM schedules WHERE group_code=?", (code,)) if code else None


async def schedule_groups(limit: int = 25) -> list:
    return await db.many("SELECT group_code FROM schedules ORDER BY group_code LIMIT ?", (limit,))


async def upsert_schedule(group_code: str, pdf_url: str) -> bool:
    """Сохраняет ссылку на PDF. True — ссылка изменилась (значит, разбор надо повторить)."""
    code = _code(group_code)
    if not code:
        return False
    await upsert_group(code)
    old = await db.one("SELECT pdf_url FROM schedules WHERE group_code=?", (code,))
    if old and as_str(old["pdf_url"]) == pdf_url:
        return False  # ссылка прежняя — разобранное расписание остаётся в силе
    await db.run(
        "INSERT INTO schedules(group_code, pdf_url, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(group_code) DO UPDATE SET pdf_url=excluded.pdf_url, updated_at=excluded.updated_at, "
        "parsed_at='', parsed_hash='', parse_error=''",
        (code, pdf_url, clock.stamp()),
    )
    return True


async def delete_schedule(group_code: str) -> None:
    code = _code(group_code)
    if code:
        await db.run("DELETE FROM schedules WHERE group_code=?", (code,))
        await db.run("DELETE FROM lessons WHERE group_code=?", (code,))


# ── разобранное расписание: занятия по дням недели ─────────────────────────────
def schedule_stamp(schedule) -> dict:
    """Состояние разбора из строки schedules: что разобрано, когда и с какой ошибкой."""
    return {
        "parsed_at": as_str(schedule["parsed_at"]) if schedule else "",
        "parsed_hash": as_str(schedule["parsed_hash"]) if schedule else "",
        "found_groups": [part for part in as_str(schedule["found_groups"] if schedule else "").split(",") if part],
        "parse_error": as_str(schedule["parse_error"]) if schedule else "",
    }


def stamp_is_fresh(stamp: dict, hours: int) -> bool:
    """Разбор считается актуальным, пока он свежее заданного числа часов.

    Отпечаток файла для этого не нужен: он решает другое — изменилось ли
    содержимое, когда PDF всё-таки скачали (и нужно ли уведомлять подписчиков).
    """
    if not stamp["parsed_hash"]:
        return False
    moment = clock.parse(stamp["parsed_at"])
    return bool(moment) and (clock.now() - moment) < timedelta(hours=hours)


def stamp_matches_file(stamp: dict, file_hash: str) -> bool:
    """Файл не изменился с прошлого разбора — переписывать занятия не нужно."""
    return bool(stamp["parsed_hash"]) and stamp["parsed_hash"] == file_hash


async def save_lessons(group_code: str, schedule, file_hash: str, found: list[str], error: str = "") -> int:
    """Заменяет занятия группы результатом разбора. schedule — GroupSchedule из timetable.py.

    Всё в одной транзакции: полупустое расписание не должно остаться в базе.
    """
    code = _code(group_code)
    if not code:
        return 0
    rows = [(code, int(day.weekday), int(lesson.number), lesson.subject, lesson.teacher,
             lesson.room, lesson.start, lesson.end)
            for _, day in sorted(schedule.days.items())
            for lesson in day.lessons]
    async with db._conn() as c:
        await c.execute("DELETE FROM lessons WHERE group_code=?", (code,))
        await c.executemany(
            "INSERT INTO lessons(group_code, weekday, lesson_num, subject, teacher, room, start, end) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        await c.execute(
            "UPDATE schedules SET parsed_at=?, parsed_hash=?, found_groups=?, parse_error=? "
            "WHERE group_code=?",
            (clock.stamp(), file_hash, ",".join(found), error, code),
        )
        await c.commit()
    return len(rows)


async def lessons_for_group(group_code: str) -> list:
    """Занятия группы по дням недели, в порядке показа."""
    code = _code(group_code)
    if not code:
        return []
    return await db.many(
        "SELECT weekday, lesson_num, subject, teacher, room, start, end FROM lessons "
        "WHERE group_code=? ORDER BY weekday, lesson_num",
        (code,),
    )


async def search_teachers(needle: str, limit: int = 10) -> list[str]:
    """Преподаватели, чьё имя похоже на запрос. Пустой запрос - самые ходовые."""
    text = f"%{as_str(needle).strip()}%"
    rows = await db.many(
        "SELECT teacher, COUNT(*) n FROM lessons "
        "WHERE teacher<>'' AND teacher LIKE ? "
        "GROUP BY teacher ORDER BY n DESC, teacher LIMIT ?",
        (text, int(limit)),
    )
    return [as_str(row["teacher"]) for row in rows]


async def teacher_groups(teacher: str, limit: int = 20) -> list[str]:
    """Группы, где у преподавателя есть занятия."""
    rows = await db.many(
        "SELECT DISTINCT group_code FROM lessons WHERE teacher=? ORDER BY group_code LIMIT ?",
        (as_str(teacher), int(limit)),
    )
    return [as_str(row["group_code"]) for row in rows]


async def lessons_for_teacher(teacher: str) -> dict:
    """Занятия преподавателя по дням: {weekday: [(номер, предмет, группа, аудитория)]}."""
    rows = await db.many(
        "SELECT weekday, lesson_num, subject, group_code, room FROM lessons "
        "WHERE teacher=? ORDER BY weekday, lesson_num, group_code",
        (as_str(teacher),),
    )
    days: dict[int, list] = {}
    for row in rows:
        days.setdefault(int(row["weekday"]), []).append(
            (int(row["lesson_num"]), as_str(row["subject"]), as_str(row["group_code"]),
             as_str(row["room"])))
    return days


async def lessons_count(group_code: str) -> int:
    code = _code(group_code)
    if not code:
        return 0
    row = await db.one("SELECT COUNT(*) n FROM lessons WHERE group_code=?", (code,))
    return row["n"] or 0


async def all_parsed_groups() -> list:
    """Группы, у которых есть разобранное расписание (для подсказок и статистики)."""
    return [as_str(row["group_code"]) for row in await db.many(
        "SELECT DISTINCT group_code FROM lessons ORDER BY group_code")]


async def set_lesson_time(group_code: str, weekday: int, lesson_num: int, start: str, end: str) -> int:
    """Правка времени одной пары вручную — звонки у колледжа меняются."""
    return await db.run_count(
        "UPDATE lessons SET start=?, end=? WHERE group_code=? AND weekday=? AND lesson_num=?",
        (start, end, _code(group_code), int(weekday), int(lesson_num)),
    )


async def reapply_lesson_times(times) -> int:
    """Пересчитывает время всех сохранённых занятий под новые звонки.

    Нужно после правки звонков: занятия уже разобраны и лежат в базе, а время
    в них проставлялось по старым правилам. Время берётся по номеру урока из
    PDF. Возвращает, сколько строк обновлено.
    """
    table = tuple(times) if times else ()
    if not table:
        return 0
    rows = await db.many("SELECT group_code, weekday, lesson_num FROM lessons")
    updates = []
    for row in rows:
        index = int(row["lesson_num"]) - 1
        start, end = table[index] if 0 <= index < len(table) and table[index][0] else ("", "")
        updates.append((start, end, as_str(row["group_code"]), int(row["weekday"]), int(row["lesson_num"])))
    if not updates:
        return 0
    async with db._conn() as c:
        await c.executemany(
            "UPDATE lessons SET start=?, end=? WHERE group_code=? AND weekday=? AND lesson_num=?",
            updates,
        )
        await c.commit()
    return len(updates)


async def group_has_lessons(group_code: str) -> bool:
    return (await lessons_count(group_code)) > 0


# ── подписки на расписание ───────────────────────────────────────────────────
async def set_schedule_subscription(user_id: str, group_code: str) -> None:
    code = _code(group_code)
    if code:
        await db.run(
            "INSERT INTO schedule_subscriptions(user_id, group_code, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET group_code=excluded.group_code",
            (str(user_id), code, clock.stamp()),
        )


async def delete_schedule_subscription(user_id: str) -> None:
    await db.run("DELETE FROM schedule_subscriptions WHERE user_id=?", (str(user_id),))


async def is_schedule_subscribed(user_id: str, group_code: str) -> bool:
    code = _code(group_code)
    if not code:
        return False
    rows = await db.many(
        "SELECT group_code FROM schedule_subscriptions WHERE user_id=?",
        (str(user_id),),
    )
    return any(_code(row["group_code"]) == code for row in rows)


async def schedule_subscribers(group_code: str) -> list[str]:
    code = _code(group_code)
    if not code:
        return []
    rows = await db.many("SELECT user_id, group_code FROM schedule_subscriptions")
    return [str(row["user_id"]) for row in rows if _code(row["group_code"]) == code]
