"""Рассылки, сводная статистика и числа для диаграмм панели.

Все запросы считают в SQLite: панель показывает картинку по этим числам, а
не собирает строки в Python - иначе объём данных упирается в память."""

import clock
import database as db
from utils import as_str

from .common import STAFF_ROLES_SQL, _code
from .people import people_overview


# ── рассылки и статистика ─────────────────────────────────────────────────────
async def audience_ids(audience: str) -> list:
    """Список user_id получателей: 'all' или код группы."""
    rows = await db.many("SELECT user_id, group_code FROM users")
    if audience == "all":
        return [str(row["user_id"]) for row in rows]
    code = _code(audience)
    if not code:
        return []
    return [str(row["user_id"]) for row in rows if _code(row["group_code"]) == code]


async def log_broadcast(
    sender_id: str,
    audience: str,
    text: str,
    sent: int,
    failed: int,
    sender_name: str = "",
    sender_role: str = "",
) -> None:
    await db.run(
        "INSERT INTO broadcasts(sender_id, sender_name, sender_role, audience, text, sent, failed, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sender_id, as_str(sender_name).strip()[:100], as_str(sender_role).strip()[:100],
         audience, text, sent, failed, clock.stamp()),
    )


async def broadcast_history(limit: int = 50) -> list:
    return await db.many("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,))


async def top_groups(limit: int = 20) -> list:
    """Группы по числу зарегистрированных студентов и открытых обращений."""
    return await db.many(
        "SELECT group_code, COUNT(*) students FROM users WHERE TRIM(group_code) <> '' "
        "GROUP BY group_code ORDER BY students DESC, group_code LIMIT ?",
        (limit,),
    )


async def stats_overview() -> dict:
    students = await db.one("SELECT COUNT(*) n FROM users")
    staff = await db.one(f"SELECT COUNT(*) n FROM admins WHERE role_type {STAFF_ROLES_SQL}")
    week = await db.one("SELECT COUNT(*) n FROM tickets WHERE created_at >= ?",
                         (clock.stamp_at(-7 * 24 * 60),))
    total = await db.one("SELECT COUNT(*) n FROM tickets")
    return {"students": students["n"], "staff": staff["n"], "week": week["n"], "total": total["n"],
            "people": await people_overview()}


# ── аналитика для диаграмм в панели ───────────────────────────────────────────
# Все запросы здесь считают в SQLite: панель показывает картинку по этим числам,
# а не собирает строки в Python - иначе объём данных упирается в память.
async def tickets_by_day(days: int = 30) -> list:
    """Обращения по дням: [{day, count, done}] - столбики и доля завершённых."""
    rows = await db.many(
        "SELECT substr(created_at, 1, 10) day, COUNT(*) count, "
        "SUM(CASE WHEN status IN ('completed','rejected') THEN 1 ELSE 0 END) done "
        "FROM tickets WHERE created_at >= ? "
        "GROUP BY day ORDER BY day", (clock.date_ago(max(1, int(days))),),
    )
    return [{"day": as_str(row["day"]), "count": row["count"], "done": row["done"] or 0} for row in rows]


async def tickets_by_status() -> list:
    """Сколько обращений в каждом статусе: [{status, count}]."""
    rows = await db.many("SELECT status, COUNT(*) count FROM tickets GROUP BY status ORDER BY count DESC")
    return [{"status": as_str(row["status"]), "count": row["count"]} for row in rows]


async def tickets_by_category() -> list:
    """Сколько обращений по разделам: [{category, count}]."""
    rows = await db.many(
        "SELECT category, COUNT(*) count FROM tickets GROUP BY category ORDER BY count DESC")
    return [{"category": as_str(row["category"]), "count": row["count"]} for row in rows]


async def staff_load(days: int = 30) -> list:
    """Нагрузка на сотрудников: сколько обращений, сколько открыто, среднее время ответа.

    avg_minutes считается по первой ответной реплике сотрудника - это честная мера
    скорости: время до «взял в работу» может быть другим, но ответ виден студенту.
    """
    rows = await db.many(
        "SELECT a.user_id, COALESCE(a.full_name, a.user_id) full_name, "
        "COUNT(t.ticket_id) tickets, "
        "SUM(CASE WHEN t.status IN ('new','accepted','in_progress') THEN 1 ELSE 0 END) open_n, "
        "ROUND(AVG(CASE WHEN r.first_reply IS NOT NULL THEN "
        "  (julianday(r.first_reply) - julianday(t.created_at)) * 24 * 60 END)) avg_minutes, "
        "MAX(r.first_reply) last_reply "
        "FROM admins a "
        "LEFT JOIN tickets t ON t.target_admin_id = a.user_id "
        "  AND t.created_at >= ? "
        "LEFT JOIN (SELECT ticket_id, MIN(created_at) first_reply FROM ticket_messages "
        "           WHERE sender_role='staff' GROUP BY ticket_id) r ON r.ticket_id = t.ticket_id "
        f"WHERE a.role_type {STAFF_ROLES_SQL} "
        "GROUP BY a.user_id ORDER BY tickets DESC, full_name LIMIT 25",
        (clock.stamp_at(-max(1, int(days)) * 24 * 60),),
    )
    return [{"user_id": as_str(row["user_id"]), "full_name": as_str(row["full_name"]),
             "tickets": row["tickets"] or 0, "open": row["open_n"] or 0,
             "avg_minutes": row["avg_minutes"], "last_reply": as_str(row["last_reply"])}
            for row in rows]


async def response_speed(days: int = 30) -> dict:
    """Скорость ответа по обращениям, созданным за N дней: сколько ответили, среднее и худшее время."""
    row = await db.one(
        "SELECT COUNT(t.ticket_id) total, "
        "SUM(CASE WHEN r.first_reply IS NOT NULL THEN 1 ELSE 0 END) answered, "
        "ROUND(AVG(CASE WHEN r.first_reply IS NOT NULL THEN "
        "  (julianday(r.first_reply) - julianday(t.created_at)) * 24 * 60 END)) avg_minutes, "
        "ROUND(MAX(CASE WHEN r.first_reply IS NOT NULL THEN "
        "  (julianday(r.first_reply) - julianday(t.created_at)) * 24 * 60 END)) worst_minutes "
        "FROM tickets t "
        "LEFT JOIN (SELECT ticket_id, MIN(created_at) first_reply FROM ticket_messages "
        "           WHERE sender_role='staff' GROUP BY ticket_id) r ON r.ticket_id = t.ticket_id "
        "WHERE t.created_at >= ?",
        (clock.stamp_at(-max(1, int(days)) * 24 * 60),),
    )
    total = row["total"] or 0
    return {"total": total, "answered": row["answered"] or 0,
            "avg_minutes": row["avg_minutes"], "worst_minutes": row["worst_minutes"],
            "share": round(100 * (row["answered"] or 0) / total) if total else 0}


async def students_by_group(limit: int = 12) -> list:
    """Студенты по группам - понятная картинка «сколько у нас групп»."""
    rows = await db.many(
        "SELECT group_code, COUNT(*) count FROM users WHERE TRIM(group_code) <> '' "
        "GROUP BY group_code ORDER BY count DESC, group_code LIMIT ?", (limit,),
    )
    return [{"group": as_str(row["group_code"]), "count": row["count"]} for row in rows]
