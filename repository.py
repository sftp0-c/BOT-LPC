"""Репозиторий: SQL-запросы к схемам из database.py.

bot.py не пишет SQL сам — все обращения к базе идут через функции этого модуля,
чтобы тексты запросов не дублировались и их было легко менять/тестировать точечно.
"""
from typing import Optional

import database as db
from utils import as_str, norm_group, valid_group

TOPIC_SOURCES = db.TOPIC_SOURCES


def _row_dict(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


async def get_user(user_id: str):
    return await db.one("SELECT * FROM users WHERE user_id=?", (user_id,))


async def is_registered(user_id: str) -> bool:
    return await db.one("SELECT 1 FROM users WHERE user_id=?", (user_id,)) is not None


async def upsert_user(user_id: str, full_name: str, group_code: str) -> None:
    code = norm_group(group_code)
    await db.run(
        "INSERT INTO users(user_id, full_name, group_code) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, group_code=excluded.group_code",
        (user_id, full_name, code),
    )
    if code:
        # новая группа сразу попадает в справочник: иначе её не будет в списке панели
        # и переименовать её до перезапуска бота (когда сработает дозаполнение) нельзя
        await db.run("INSERT OR IGNORE INTO groups(group_code) VALUES(?)", (code,))


async def set_user_name(user_id: str, full_name: str) -> None:
    await db.run("UPDATE users SET full_name=? WHERE user_id=?", (full_name, user_id))


async def set_user_group(user_id: str, group_code: str) -> None:
    await db.run("UPDATE users SET group_code=? WHERE user_id=?", (norm_group(group_code), user_id))


# ── сотрудники ────────────────────────────────────────────────────────────────
async def get_admin(user_id: str):
    return await db.one("SELECT * FROM admins WHERE user_id=?", (user_id,))


async def _load_admins() -> dict[str, dict]:
    rows = await db.many("SELECT * FROM admins ORDER BY user_id")
    result = {}
    for row in rows:
        admin = _row_dict(row)
        admin.setdefault("id", str(row["user_id"]))
        admin.setdefault("role", "")
        admin.setdefault("office", "")
        result[str(row["user_id"])] = admin
    return result


async def admin_ids() -> list[str]:
    admins = await _load_admins()
    return list(admins.keys()) if hasattr(admins, "keys") else [str(value) for value in admins]


async def set_admin_profile(admin_id: str, role: str = "", office: str = "") -> None:
    values: list[tuple[str, str]] = []
    role_value = as_str(role).strip()
    office_value = as_str(office).strip()
    if role_value:
        values.append(("role", role_value))
    if office_value:
        values.append(("office", office_value))
    if not values:
        return
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name, _ in values)} WHERE user_id=?",
        tuple(value for _, value in values) + (admin_id,),
    )


async def list_staff() -> list:
    return await db.many("SELECT * FROM admins WHERE role_type NOT IN ('sysadmin', 'superadmin') ORDER BY full_name")


async def staff_for_category(category: str, limit: int = 25) -> list:
    return await db.many(
        "SELECT user_id, full_name, role, office FROM admins "
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
    ticket = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))
    if not ticket:
        return None
    result = _row_dict(ticket)
    student = await db.one("SELECT * FROM users WHERE user_id=?", (ticket["student_id"],))
    staff = await db.one("SELECT * FROM admins WHERE user_id=?", (ticket["target_admin_id"],))
    sender = await db.one(
        "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id LIMIT 1",
        (ticket_id,),
    )
    result["student"] = _row_dict(student)
    result["staff"] = _row_dict(staff)
    sender_data = _row_dict(sender)
    if sender_data:
        person = await db.one(
            "SELECT * FROM users WHERE user_id=?" if sender_data.get("sender_role") == "student"
            else "SELECT * FROM admins WHERE user_id=?",
            (sender_data.get("sender_id"),),
        )
        sender_data = {**(_row_dict(person) or {}), **sender_data}
    result["sender"] = sender_data or result["student"]
    return result


async def recent_student_tickets(user_id: str, limit: int = 15) -> list:
    return await db.many("SELECT * FROM tickets WHERE student_id=? ORDER BY ticket_id DESC LIMIT ?", (user_id, limit))


async def admin_tickets(admin_id: Optional[str], limit: int = 20) -> list:
    """Обращения сотрудника; None — все обращения (для сис-админа). Сначала открытые."""
    where, params = ("WHERE target_admin_id=?", (admin_id,)) if admin_id else ("", ())
    return await db.many(
        f"SELECT * FROM tickets {where} "
        "ORDER BY (status IN ('ready','completed','rejected')), "
        "CASE WHEN status='ready' THEN 0 ELSE 1 END, "
        "ticket_id DESC LIMIT ?",
        params + (limit,),
    )


async def ticket_messages(ticket_id: int, limit: int = 10) -> list:
    return await db.many(
        "SELECT sender_id, sender_role, text FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (ticket_id, limit)
    )


async def open_tickets_count(admin_id: str) -> int:
    row = await db.one(
        "SELECT COUNT(*) n FROM tickets WHERE target_admin_id=? AND status IN ('new','accepted','in_progress')", (admin_id,)
    )
    return row["n"]


async def status_counts(admin_id: Optional[str] = None) -> dict:
    """{status: количество}; admin_id=None — по всем обращениям."""
    where, params = ("WHERE target_admin_id=?", (admin_id,)) if admin_id else ("", ())
    rows = await db.many(f"SELECT status, COUNT(*) n FROM tickets {where} GROUP BY status", params)
    return {r["status"]: r["n"] for r in rows}


# ── обращения ─────────────────────────────────────────────────────────────────
async def create_ticket(student_id: str, admin_id: str, category: str, text: str, topic: str = "") -> int:
    """Создаёт обращение и его первое сообщение в одной транзакции."""
    async with db._conn() as c:
        cur = await c.execute(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content, topic) VALUES(?,?,?,?,?)",
            (student_id, admin_id, category, text, as_str(topic).strip()),
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


async def transition_ticket_status(ticket_id: int, current_status: str, status: str) -> bool:
    async with db._conn() as c:
        cur = await c.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=? AND status=?",
            (status, ticket_id, current_status),
        )
        await c.commit()
        return cur.rowcount > 0


async def set_ticket_ready(
    ticket_id: int,
    ready_until: str,
    pickup_place: str = "",
    doc_url: str = "",
) -> None:
    await db.run(
        "UPDATE tickets SET status='ready', ready_until=?, pickup_place=?, doc_url=?, updated_at=datetime('now') "
        "WHERE ticket_id=?",
        (as_str(ready_until).strip(), as_str(pickup_place).strip(), as_str(doc_url).strip(), ticket_id),
    )


# ── расписания ────────────────────────────────────────────────────────────────
async def get_schedule(group_code: str):
    code = _code(group_code)
    return await db.one("SELECT * FROM schedules WHERE group_code=?", (code,)) if code else None


async def schedule_groups(limit: int = 25) -> list:
    return await db.many("SELECT group_code FROM schedules ORDER BY group_code LIMIT ?", (limit,))


async def upsert_schedule(group_code: str, pdf_url: str) -> None:
    code = _code(group_code)
    if not code:
        return
    await upsert_group(code)
    await db.run(
        "INSERT INTO schedules(group_code, pdf_url, updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(group_code) DO UPDATE SET pdf_url=excluded.pdf_url, updated_at=excluded.updated_at",
        (code, pdf_url),
    )


async def delete_schedule(group_code: str) -> None:
    code = _code(group_code)
    if code:
        await db.run("DELETE FROM schedules WHERE group_code=?", (code,))


async def set_schedule_subscription(user_id: str, group_code: str) -> None:
    code = _code(group_code)
    if code:
        await db.run(
            "INSERT INTO schedule_subscriptions(user_id, group_code) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET group_code=excluded.group_code",
            (str(user_id), code),
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


# ── справочник групп ──────────────────────────────────────────────────────────
def _code(group_code) -> str:
    """Нормализованный код группы; '' — если значения нет (None, пустая строка)."""
    return norm_group(as_str(group_code))


async def get_group(group_code: str):
    """Строка справочника по коду группы; None — группа неизвестна или код пуст."""
    code = _code(group_code)
    return await db.one("SELECT * FROM groups WHERE group_code=?", (code,)) if code else None


async def groups(active_only: bool = True, limit: int = 50) -> list:
    """Группы справочника по алфавиту: активные — рабочие, все — включая скрытые."""
    where = " WHERE active=1" if active_only else ""
    return await db.many(f"SELECT * FROM groups{where} ORDER BY group_code LIMIT ?", (limit,))


async def list_groups(active_only: bool = True) -> list[dict]:
    where = " WHERE active=1" if active_only else ""
    rows = await db.many(f"SELECT group_code, active FROM groups{where} ORDER BY group_code")
    return [{"code": _code(row["group_code"]), "active": row["active"]} for row in rows]


async def known_groups(limit: int = 50) -> list[str]:
    """Коды всех групп, о которых знает бот, — по алфавиту.

    Справочник groups наполняется из users и schedules (см. database.GROUP_BACKFILL),
    но и после этого он может разойтись с данными: группу скрыли или удалили из
    справочника, а студенты и расписание остались. Поэтому берём объединение.
    UNION убирает повторы прямо в SQL, а регистр и пробелы приводит norm_group:
    верхний регистр в SQLite работает только с ASCII, а коды групп кириллические.
    """
    rows = await db.many(
        "SELECT group_code FROM groups "
        "UNION SELECT group_code FROM users "
        "UNION SELECT group_code FROM schedules"
    )
    return sorted({_code(r["group_code"]) for r in rows} - {""})[:limit]


async def upsert_group(
    code: str = "",
    title: str | bool | None = None,
    active: bool | None = None,
    **kwargs,
) -> bool:
    """Добавляет группу в справочник и/или меняет её поля.

    title и active, равные None, не трогаются — так можно завести группу, не затирая
    её название, и переключать активность, не передавая лишнего. Возвращает False,
    если код группы пустой: случайный пустой код в справочнике не нужен.
    """
    if not code:
        code = kwargs.pop("group_code", "")
    if kwargs:
        unexpected = next(iter(kwargs))
        raise TypeError(f"Unexpected keyword argument: {unexpected}")
    if isinstance(title, (bool, int)):
        if active is None:
            active = title
        title = None
    normalized = _code(code)
    if not normalized:
        return False
    values: list[tuple[str, object]] = []
    if title is not None:
        values.append(("title", as_str(title).strip()[:100]))
    if active is not None:
        values.append(("active", int(active)))
    async with db._conn() as c:
        await c.execute("INSERT OR IGNORE INTO groups(group_code) VALUES(?)", (normalized,))
        if values:
            await c.execute(
                f"UPDATE groups SET {', '.join(f'{n}=?' for n, _ in values)} WHERE group_code=?",
                tuple(v for _, v in values) + (normalized,),
            )
        await c.commit()
    return True


async def set_group_active(code: str, active: bool) -> None:
    normalized = _code(code)
    if normalized:
        await db.run("UPDATE groups SET active=? WHERE group_code=?", (int(active), normalized))


async def delete_group(code: str = "", **kwargs) -> None:
    """Убирает группу из справочника. Студенты и расписание группы не трогаем."""
    if not code:
        code = kwargs.pop("group_code", "")
    if kwargs:
        unexpected = next(iter(kwargs))
        raise TypeError(f"Unexpected keyword argument: {unexpected}")
    normalized = _code(code)
    if normalized:
        await db.run("DELETE FROM groups WHERE group_code=?", (normalized,))


async def rename_group(old: str, new: str) -> bool:
    old_code = _code(old)
    new_code = _code(new)
    if not old_code or not new_code or not valid_group(old_code) or not valid_group(new_code):
        return False
    if old_code == new_code:
        return False
    async with db._conn() as c:
        try:
            old_row = await c.execute("SELECT 1 FROM groups WHERE group_code=?", (old_code,))
            if await old_row.fetchone() is None:
                return False
            new_row = await c.execute("SELECT 1 FROM groups WHERE group_code=?", (new_code,))
            if await new_row.fetchone() is not None:
                return False
            await c.execute("UPDATE groups SET group_code=? WHERE group_code=?", (new_code, old_code))
            await c.execute("UPDATE users SET group_code=? WHERE group_code=?", (new_code, old_code))
            await c.execute("UPDATE schedules SET group_code=? WHERE group_code=?", (new_code, old_code))
            await c.execute(
                "UPDATE schedule_subscriptions SET group_code=? WHERE group_code=?",
                (new_code, old_code),
            )
            await c.commit()
            return True
        except Exception:
            await c.rollback()
            raise


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
        "INSERT INTO broadcasts(sender_id, sender_name, sender_role, audience, text, sent, failed) "
        "VALUES(?,?,?,?,?,?,?)",
        (sender_id, as_str(sender_name).strip()[:100], as_str(sender_role).strip()[:100],
         audience, text, sent, failed),
    )


async def broadcast_history(limit: int = 50) -> list:
    return await db.many("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,))


# ── данные для веб-панели сис-админа ──────────────────────────────────────────
async def all_admins() -> list:
    """Все строки admins, включая сис-админов: сначала сис-админы, потом сотрудники."""
    return await db.many(
        "SELECT * FROM admins ORDER BY (role_type NOT IN ('sysadmin','superadmin')), full_name"
    )


ADMIN_FIELDS = ("full_name", "role", "office", "ticket_category", "can_broadcast")


async def update_admin(user_id: str, **fields) -> None:
    """Частичное обновление строки admins. Неизвестные поля игнорируются, None — не меняем."""
    values = [(name, fields[name]) for name in ADMIN_FIELDS if name in fields and fields[name] is not None]
    if not values:
        return
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name, _ in values)} WHERE user_id=?",
        tuple(value for _, value in values) + (str(user_id),),
    )


async def add_sysadmin(user_id: str, full_name: str = "") -> None:
    """Добавляет сис-админа прямо из панели (без правки .env и перезапуска)."""
    await db.run(
        "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?,?, 'sysadmin', 1) "
        "ON CONFLICT(user_id) DO UPDATE SET role_type='sysadmin', can_broadcast=1",
        (str(user_id), as_str(full_name).strip()[:100] or "Сис-админ"),
    )


async def list_users(limit: int = 200, group_code: str = "") -> list:
    """Студенты (или сотрудники) с числом обращений — для таблиц панели."""
    where, params = ("WHERE u.group_code=?", (_code(group_code),)) if _code(group_code) else ("", ())
    return await db.many(
        "SELECT u.user_id, u.full_name, u.group_code, u.created_at, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.student_id=u.user_id) tickets "
        f"FROM users u{where} ORDER BY u.full_name LIMIT ?",
        params + (limit,),
    )


async def all_settings() -> list:
    return await db.many("SELECT key, value FROM settings ORDER BY key")


async def dedupe_stats() -> dict:
    """Сколько событий уже обработано (таблица processed_updates) и какого возраста самое новое."""
    row = await db.one("SELECT COUNT(*) n, MIN(created_at) oldest, MAX(created_at) newest FROM processed_updates")
    return {"processed": row["n"], "oldest": row["oldest"], "newest": row["newest"]}


async def top_groups(limit: int = 20) -> list:
    """Группы по числу зарегистрированных студентов и открытых обращений."""
    return await db.many(
        "SELECT group_code, COUNT(*) students FROM users WHERE TRIM(group_code) <> '' "
        "GROUP BY group_code ORDER BY students DESC, group_code LIMIT ?",
        (limit,),
    )


async def stats_overview() -> dict:
    students = await db.one("SELECT COUNT(*) n FROM users")
    staff = await db.one("SELECT COUNT(*) n FROM admins WHERE role_type NOT IN ('sysadmin', 'superadmin')")
    week = await db.one("SELECT COUNT(*) n FROM tickets WHERE created_at >= datetime('now','-7 days')")
    total = await db.one("SELECT COUNT(*) n FROM tickets")
    return {"students": students["n"], "staff": staff["n"], "week": week["n"], "total": total["n"]}
