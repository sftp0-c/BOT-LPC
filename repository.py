"""Репозиторий: SQL-запросы к схемам из database.py.

bot.py не пишет SQL сам — все обращения к базе идут через функции этого модуля,
чтобы тексты запросов не дублировались и их было легко менять/тестировать точечно.
"""
from typing import Optional

import database as db
from utils import norm_group


async def get_user(user_id: str):
    return await db.one("SELECT * FROM users WHERE user_id=?", (user_id,))


async def is_registered(user_id: str) -> bool:
    return await db.one("SELECT 1 FROM users WHERE user_id=?", (user_id,)) is not None


async def upsert_user(user_id: str, full_name: str, group_code: str) -> None:
    await db.run(
        "INSERT INTO users(user_id, full_name, group_code) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, group_code=excluded.group_code",
        (user_id, full_name, norm_group(group_code)),
    )


async def set_user_name(user_id: str, full_name: str) -> None:
    await db.run("UPDATE users SET full_name=? WHERE user_id=?", (full_name, user_id))


async def set_user_group(user_id: str, group_code: str) -> None:
    await db.run("UPDATE users SET group_code=? WHERE user_id=?", (norm_group(group_code), user_id))


# ── сотрудники ────────────────────────────────────────────────────────────────
async def get_admin(user_id: str):
    return await db.one("SELECT * FROM admins WHERE user_id=?", (user_id,))


async def list_staff() -> list:
    return await db.many("SELECT * FROM admins WHERE role_type NOT IN ('sysadmin', 'superadmin') ORDER BY full_name")


async def staff_for_category(category: str, limit: int = 25) -> list:
    return await db.many(
        "SELECT user_id, full_name FROM admins "
        "WHERE role_type NOT IN ('sysadmin', 'superadmin') AND (ticket_category=? OR ticket_category='all') "
        "ORDER BY full_name LIMIT ?",
        (category, limit),
    )


async def add_staff(user_id: str, full_name: str) -> None:
    await db.run("INSERT OR IGNORE INTO admins(user_id, full_name, role_type) VALUES(?,?, 'staff')", (user_id, full_name))


async def delete_staff(user_id: str) -> None:
    await db.run("DELETE FROM admins WHERE user_id=?", (user_id,))


async def set_staff_category(user_id: str, category: str) -> None:
    await db.run("UPDATE admins SET ticket_category=? WHERE user_id=?", (category, user_id))


async def set_staff_broadcast(user_id: str, allowed: bool) -> None:
    await db.run("UPDATE admins SET can_broadcast=? WHERE user_id=?", (int(allowed), user_id))


# ── обращения ─────────────────────────────────────────────────────────────────
async def get_ticket(ticket_id: int):
    return await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))


async def recent_student_tickets(user_id: str, limit: int = 15) -> list:
    return await db.many("SELECT * FROM tickets WHERE student_id=? ORDER BY ticket_id DESC LIMIT ?", (user_id, limit))


async def admin_tickets(admin_id: Optional[str], limit: int = 20) -> list:
    """Обращения сотрудника; None — все обращения (для сис-админа). Сначала открытые."""
    where, params = ("WHERE target_admin_id=?", (admin_id,)) if admin_id else ("", ())
    return await db.many(
        f"SELECT * FROM tickets {where} ORDER BY (status IN ('completed','rejected')), ticket_id DESC LIMIT ?",
        params + (limit,),
    )


async def ticket_messages(ticket_id: int, limit: int = 10) -> list:
    return await db.many(
        "SELECT sender_role, text FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (ticket_id, limit)
    )


async def open_tickets_count(admin_id: str) -> int:
    row = await db.one(
        "SELECT COUNT(*) n FROM tickets WHERE target_admin_id=? AND status IN ('new','in_progress')", (admin_id,)
    )
    return row["n"]


async def status_counts(admin_id: Optional[str] = None) -> dict:
    """{status: количество}; admin_id=None — по всем обращениям."""
    where, params = ("WHERE target_admin_id=?", (admin_id,)) if admin_id else ("", ())
    rows = await db.many(f"SELECT status, COUNT(*) n FROM tickets {where} GROUP BY status", params)
    return {r["status"]: r["n"] for r in rows}


# ── обращения ─────────────────────────────────────────────────────────────────
async def create_ticket(student_id: str, admin_id: str, category: str, text: str) -> int:
    """Создаёт обращение и его первое сообщение в одной транзакции."""
    async with db._conn() as c:
        cur = await c.execute(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content) VALUES(?,?,?,?)",
            (student_id, admin_id, category, text),
        )
        ticket_id = cur.lastrowid
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, student_id, "student", text),
        )
        await c.commit()
    return ticket_id


async def add_ticket_message(ticket_id: int, sender_id: str, role: str, text: str, new_status: str | None = None) -> None:
    async with db._conn() as c:
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, sender_id, role, text),
        )
        if new_status:
            await c.execute(
                "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (new_status, ticket_id)
            )
        else:
            await c.execute("UPDATE tickets SET updated_at=datetime('now') WHERE ticket_id=?", (ticket_id,))
        await c.commit()


async def set_ticket_status(ticket_id: int, status: str) -> None:
    await db.run("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (status, ticket_id))


# ── расписания ────────────────────────────────────────────────────────────────
async def get_schedule(group_code: str):
    return await db.one("SELECT * FROM schedules WHERE group_code=?", (group_code,))


async def schedule_groups(limit: int = 25) -> list:
    return await db.many("SELECT group_code FROM schedules ORDER BY group_code LIMIT ?", (limit,))


async def upsert_schedule(group_code: str, pdf_url: str) -> None:
    await db.run(
        "INSERT INTO schedules(group_code, pdf_url, updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(group_code) DO UPDATE SET pdf_url=excluded.pdf_url, updated_at=excluded.updated_at",
        (norm_group(group_code), pdf_url),
    )


async def delete_schedule(group_code: str) -> None:
    await db.run("DELETE FROM schedules WHERE group_code=?", (group_code,))


# ── рассылки и статистика ─────────────────────────────────────────────────────
async def audience_ids(audience: str) -> list:
    """Список user_id получателей: 'all' или код группы."""
    sql = "SELECT user_id FROM users"
    params: tuple = ()
    if audience != "all":
        sql += " WHERE group_code=?"
        params = (norm_group(audience),)
    return [r["user_id"] for r in await db.many(sql, params)]


async def log_broadcast(sender_id: str, audience: str, text: str, sent: int, failed: int) -> None:
    await db.run(
        "INSERT INTO broadcasts(sender_id, audience, text, sent, failed) VALUES(?,?,?,?,?)",
        (sender_id, audience, text, sent, failed),
    )


async def stats_overview() -> dict:
    students = await db.one("SELECT COUNT(*) n FROM users")
    staff = await db.one("SELECT COUNT(*) n FROM admins WHERE role_type NOT IN ('sysadmin', 'superadmin')")
    week = await db.one("SELECT COUNT(*) n FROM tickets WHERE created_at >= datetime('now','-7 days')")
    total = await db.one("SELECT COUNT(*) n FROM tickets")
    return {"students": students["n"], "staff": staff["n"], "week": week["n"], "total": total["n"]}
