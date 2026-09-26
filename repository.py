"""Репозиторий: SQL-запросы к схемам из database.py.

bot.py не пишет SQL сам — все обращения к базе идут через функции этого модуля,
чтобы тексты запросов не дублировались и их было легко менять/тестировать точечно.
"""
from datetime import datetime, timedelta
from typing import Optional

import config
import database as db
from utils import (
    OPEN_STATUSES,
    as_str,
    is_owner_role,
    is_sysadmin_role,
    norm_code,
    norm_group,
    parse_db_time,
    parse_max_ids,
    parse_nicks,
    to_int,
    ttl_label,
    valid_group,
)

TOPIC_SOURCES = db.TOPIC_SOURCES
# Роли с доступом к панели. Владелец (owner) — тоже: в списках сотрудников и в
# статистике его быть не должно, поэтому фильтруем одной строкой.
ADMIN_ROLES_SQL = "'owner','sysadmin','superadmin'"
STAFF_ROLES_SQL = f"NOT IN ({ADMIN_ROLES_SQL})"


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


async def set_admin_profile(
    admin_id: str,
    role: str = "",
    office: str = "",
    position: str = "",
    department: str = "",
) -> None:
    values: list[tuple[str, str]] = []
    for name, raw in (("role", role), ("office", office), ("position", position), ("department", department)):
        value = as_str(raw).strip()[:100]
        if value:
            values.append((name, value))
    if not values:
        return
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name, _ in values)} WHERE user_id=?",
        tuple(value for _, value in values) + (admin_id,),
    )


async def clear_admin_fields(admin_id: str, *names: str) -> None:
    """Очищает указанные поля карточки сотрудника (set_admin_profile умеет только заполнять)."""
    fields = [name for name in names if name in ("role", "office", "position", "department")]
    if not fields:
        return
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name in fields)} WHERE user_id=?",
        tuple("" for _ in fields) + (admin_id,),
    )


async def list_staff() -> list:
    return await db.many(f"SELECT * FROM admins WHERE role_type {STAFF_ROLES_SQL} ORDER BY full_name")


async def staff_for_category(category: str, limit: int = 25) -> list:
    return await db.many(
        "SELECT user_id, full_name, role, position, department, office FROM admins "
        f"WHERE role_type {STAFF_ROLES_SQL} AND (ticket_category=? OR ticket_category='all') "
        "ORDER BY full_name LIMIT ?",
        (category, limit),
    )


async def add_staff(
    user_id: str,
    full_name: str,
    position: str = "",
    department: str = "",
    office: str = "",
    ticket_category: str = "all",
    can_broadcast: bool = False,
) -> None:
    """Добавляет сотрудника. Существующую строку не трогает — данные правит update_admin."""
    await db.run(
        "INSERT OR IGNORE INTO admins(user_id, full_name, role_type, position, department, office, "
        "ticket_category, can_broadcast) VALUES(?,?, 'staff', ?,?,?,?,?)",
        (str(user_id), as_str(full_name).strip()[:100], as_str(position).strip()[:100],
         as_str(department).strip()[:100], as_str(office).strip()[:100],
         ticket_category or "all", int(bool(can_broadcast))),
    )


async def delete_staff(user_id: str) -> None:
    await db.run("DELETE FROM admins WHERE user_id=?", (user_id,))


async def staff_targets(text: str) -> tuple[list[dict], list[str]]:
    """Кого имел в виду сис-админ: принимает ID, @ники, ссылку на профиль, список.

    Возвращает (найденные, ненайденные_ники). Найденные — [{user_id, full_name,
    exists}], где exists=True, если человек уже есть в admins: такие идут
    отдельным списком, чтобы сис-админ увидел, кого пропускаем.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for uid in parse_max_ids(text):
        if uid in seen:
            continue
        seen.add(uid)
        admin = await get_admin(uid)
        card = await user_card(uid)
        found.append({
            "user_id": uid,
            "full_name": (as_str(admin["full_name"]) if admin else "")
            or (contact_full_name(card) if card else ""),
            "exists": bool(admin),
        })
    missing: list[str] = []
    for nick in parse_nicks(text):
        row = await db.one(
            "SELECT c.user_id, COALESCE(a.full_name, u.full_name, c.display_name, '') name "
            "FROM contacts c LEFT JOIN users u ON u.user_id=c.user_id "
            "LEFT JOIN admins a ON a.user_id=c.user_id "
            "WHERE LOWER(c.username)=LOWER(?) LIMIT 1", (nick,),
        )
        if not row:
            missing.append(nick)
            continue
        uid = as_str(row["user_id"])
        if uid in seen:
            continue
        seen.add(uid)
        found.append({
            "user_id": uid,
            "full_name": as_str(row["name"]),
            "exists": bool(await get_admin(uid)),
        })
    return found, missing


def contact_full_name(card) -> str:
    """ФИО для карточки человека: ФИО из профиля, иначе подпись из контактов."""
    if not card:
        return ""
    return as_str(card.get("fio")) or as_str(card.get("staff_name")) or as_str(card.get("display_name"))


async def add_staff_many(entries: list[dict], **common) -> list[str]:
    """Добавляет сразу нескольких сотрудников с общими полями.

    Возвращает список ID, которые действительно добавились: те, кто уже был в
    списке, молча пропускаются — иначе одна опечатка тихо теряет человека.
    """
    added: list[str] = []
    for entry in entries:
        uid = as_str(entry.get("user_id")).strip()
        if not uid.isdigit() or await get_admin(uid):
            continue
        await add_staff(uid, entry.get("full_name") or f"Сотрудник {uid}")
        await update_admin(uid, **common)
        added.append(uid)
    return added


async def people_without_staff(limit: int = 50, q: str = "") -> list:
    """Люди, которые писали боту, но прав сотрудника не имеют: кому ещё стоит выдать."""
    where, params = _contact_filter("nostaff", q)
    return await db.many(
        f"{_CONTACT_SELECT}{where} ORDER BY c.last_seen DESC, c.user_id LIMIT ?",
        params + [max(1, int(limit))],
    )


async def people_without_staff_count(q: str = "") -> int:
    where, params = _contact_filter("nostaff", q)
    row = await db.one(
        "SELECT COUNT(*) n FROM contacts c "
        "LEFT JOIN users u ON u.user_id=c.user_id "
        "LEFT JOIN admins a ON a.user_id=c.user_id" + where, params,
    )
    return row["n"]


async def department_names() -> list:
    """Отделы, которые уже используются: чтобы подсказать их в форме, а не заставлять угадывать."""
    rows = await db.many(
        "SELECT DISTINCT department FROM admins WHERE department<>'' ORDER BY department LIMIT 100"
    )
    return [as_str(row["department"]) for row in rows]


async def staff_activity(days: int = 30) -> dict:
    """{user_id: {tickets, open, last_reply}} — кому сколько обращений и как давно отвечали.

    Один запрос на весь список сотрудников: в панели и боте это таблица, а не
    запрос на каждого человека.
    """
    rows = await db.many(
        "SELECT t.target_admin_id uid, COUNT(*) tickets, "
        "SUM(CASE WHEN t.status IN ('new','accepted','in_progress') THEN 1 ELSE 0 END) open_n, "
        "MAX(t.updated_at) last_reply "
        "FROM tickets t JOIN admins a ON a.user_id=t.target_admin_id "
        "WHERE t.created_at >= datetime('now', ?) GROUP BY t.target_admin_id",
        (f"-{max(1, int(days))} days",),
    )
    return {as_str(row["uid"]): {"tickets": row["tickets"], "open": row["open_n"] or 0,
                                 "last_reply": as_str(row["last_reply"])} for row in rows}


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


async def ticket_thread(ticket_id: int, limit: int = 50) -> list:
    """Переписка с именами, должностями и временем — от свежих к старым."""
    return await db.many(
        "SELECT m.id, m.sender_id, m.sender_role, m.text, m.created_at, "
        "COALESCE(u.full_name, '') sender_name, COALESCE(u.group_code, '') group_code, "
        "COALESCE(a.position, a.role, '') position, COALESCE(a.role_type, '') role_type "
        "FROM ticket_messages m "
        "LEFT JOIN users u ON u.user_id=m.sender_id AND m.sender_role='student' "
        "LEFT JOIN admins a ON a.user_id=m.sender_id AND m.sender_role='staff' "
        "WHERE m.ticket_id=? ORDER BY m.id DESC LIMIT ?",
        (ticket_id, limit),
    )


async def latest_message_roles(ticket_ids: list[int]) -> dict:
    """Роль автора последнего сообщения по каждому обращению: {ticket_id: 'student'|'staff'}.

    Один запрос на весь список — иначе карточка списка обращений делала бы запрос
    на каждое обращение. Отсутствующее обращение в словарь не попадает.
    """
    ids = [int(value) for value in ticket_ids if to_int(value, -1) >= 0]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = await db.many(
        f"SELECT ticket_id, sender_role FROM ticket_messages WHERE id IN "
        f"(SELECT MAX(id) FROM ticket_messages WHERE ticket_id IN ({placeholders}) GROUP BY ticket_id)",
        tuple(ids),
    )
    return {int(row["ticket_id"]): as_str(row["sender_role"]) for row in rows}


async def log_ticket_event(ticket_id: int, actor_id: str, event: str, detail: str = "") -> int:
    return await db.run(
        "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,?,?)",
        (int(ticket_id), as_str(actor_id).strip(), event, as_str(detail).strip()[:200]),
    )


async def ticket_events(ticket_id: int, limit: int = 50) -> list:
    """Лента событий обращения: кто создал, отвечал, менял статус — с именами."""
    return await db.many(
        "SELECT e.id, e.actor_id, e.event, e.detail, e.created_at, "
        "COALESCE(u.full_name, a.full_name, '') actor_name, COALESCE(a.position, a.role, '') actor_position "
        "FROM ticket_events e "
        "LEFT JOIN users u ON u.user_id=e.actor_id "
        "LEFT JOIN admins a ON a.user_id=e.actor_id "
        "WHERE e.ticket_id=? ORDER BY e.id DESC LIMIT ?",
        (ticket_id, limit),
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
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,'created',?)",
            (ticket_id, student_id, as_str(topic).strip()[:200] or category),
        )
        await c.commit()
    return ticket_id


async def add_ticket_message(ticket_id: int, sender_id: str, role: str, text: str, new_status: str | None = None) -> None:
    async with db._conn() as c:
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, sender_id, role, text),
        )
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,?,?)",
            (ticket_id, sender_id, "message_student" if role == "student" else "message_staff", as_str(text)[:200]),
        )
        if new_status:
            await c.execute(
                "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (new_status, ticket_id)
            )
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,'status',?)",
                (ticket_id, sender_id, new_status),
            )
        else:
            await c.execute("UPDATE tickets SET updated_at=datetime('now') WHERE ticket_id=?", (ticket_id,))
        await c.commit()


async def set_ticket_status(ticket_id: int, status: str, actor_id: str = "") -> None:
    async with db._conn() as c:
        await c.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (status, ticket_id)
        )
        if actor_id:
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,'status',?)",
                (ticket_id, as_str(actor_id), status),
            )
        await c.commit()


async def transition_ticket_status(ticket_id: int, current_status: str, status: str, actor_id: str = "") -> bool:
    async with db._conn() as c:
        cur = await c.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=? AND status=?",
            (status, ticket_id, current_status),
        )
        if cur.rowcount > 0 and actor_id:
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,'status',?)",
                (ticket_id, as_str(actor_id), status),
            )
        await c.commit()
        return cur.rowcount > 0


async def set_ticket_ready(
    ticket_id: int,
    ready_until: str,
    pickup_place: str = "",
    doc_url: str = "",
    actor_id: str = "",
) -> None:
    async with db._conn() as c:
        await c.execute(
            "UPDATE tickets SET status='ready', ready_until=?, pickup_place=?, doc_url=?, "
            "updated_at=datetime('now') WHERE ticket_id=?",
            (as_str(ready_until).strip(), as_str(pickup_place).strip(), as_str(doc_url).strip(), ticket_id),
        )
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail) VALUES(?,?,'ready',?)",
            (ticket_id, as_str(actor_id), f"{as_str(ready_until).strip()} · {as_str(pickup_place).strip()}"),
        )
        await c.commit()


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
        "INSERT INTO schedules(group_code, pdf_url, updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(group_code) DO UPDATE SET pdf_url=excluded.pdf_url, updated_at=excluded.updated_at, "
        "parsed_at='', parsed_hash='', parse_error=''",
        (code, pdf_url),
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
    moment = parse_db_time(stamp["parsed_at"])
    return bool(moment) and (datetime.now() - moment) < timedelta(hours=hours)


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
            "UPDATE schedules SET parsed_at=datetime('now'), parsed_hash=?, found_groups=?, parse_error=? "
            "WHERE group_code=?",
            (file_hash, ",".join(found), error, code),
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


# ── реестр пользователей: все, кто писал боту ─────────────────────────────────
CONTACT_KINDS = ("student", "staff", "request", "guest")
CONTACT_KIND_LABELS = {"": "👥 Все", "student": "🎓 Студенты", "staff": "🏫 Сотрудники",
                       "request": "📥 Заявки", "guest": "👤 Без регистрации"}
KIND_TITLES = {"student": "🎓 студент", "staff": "🏫 сотрудник", "request": "📥 заявка", "guest": "👤 без регистрации"}

_CONTACT_SELECT = (
    "SELECT c.user_id, c.username, c.display_name, c.messages, c.last_text, c.first_seen, c.last_seen, "
    "COALESCE(u.full_name, '') fio, COALESCE(u.group_code, '') group_code, "
    "COALESCE(a.full_name, '') staff_name, COALESCE(a.position, a.role, '') position, "
    "COALESCE(a.department, '') department, COALESCE(a.role_type, '') role_type, "
    "(SELECT COUNT(*) FROM tickets t WHERE t.student_id=c.user_id) tickets, "
    "(SELECT status FROM staff_requests r WHERE r.user_id=c.user_id) request_status "
    "FROM contacts c "
    "LEFT JOIN users u ON u.user_id=c.user_id "
    "LEFT JOIN admins a ON a.user_id=c.user_id"
)


async def touch_contact(user_id: str, username: str = "", display_name: str = "", last_text: str = "") -> None:
    """Запоминает, что человек писал боту: ник, имя из профиля MAX, время и текст.

    Вызывается на каждом событии, поэтому запрос один и идемпотентный: ник и имя
    обновляются только когда пришли непустыми (у MAX username бывает null).
    """
    if not user_id:
        return
    await db.run(
        "INSERT INTO contacts(user_id, username, display_name, messages, last_text) VALUES(?,?,?,1,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE contacts.username END, "
        "display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE contacts.display_name END, "
        "messages=contacts.messages + CASE WHEN excluded.last_text<>'' THEN 1 ELSE 0 END, "
        "last_text=CASE WHEN excluded.last_text<>'' THEN excluded.last_text ELSE contacts.last_text END, "
        "last_seen=datetime('now')",
        (str(user_id), as_str(username).strip()[:64], as_str(display_name).strip()[:100],
         as_str(last_text).strip()[:200]),
    )


def contact_kind(row) -> str:
    """Как человек выглядит в реестре: сотрудник, студент, заявка или гость."""
    if row["role_type"]:
        return "staff"
    if row["fio"]:
        return "student"
    return "request" if row["request_status"] else "guest"


def _contact_filter(kind: str = "", q: str = "") -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if kind == "student":
        where.append("u.user_id IS NOT NULL")
    elif kind == "staff":
        where.append("a.user_id IS NOT NULL")
    elif kind == "guest":
        where.append("u.user_id IS NULL AND a.user_id IS NULL")
    elif kind == "nostaff":
        where.append("a.user_id IS NULL")
    elif kind == "request":
        where.append("EXISTS (SELECT 1 FROM staff_requests r WHERE r.user_id=c.user_id)")
    needle = as_str(q).strip()
    if needle:
        like = f"%{needle}%"
        where.append(
            "(c.user_id LIKE ? OR c.username LIKE ? OR c.display_name LIKE ? "
            "OR u.full_name LIKE ? OR u.group_code LIKE ? OR a.full_name LIKE ? OR a.position LIKE ?)"
        )
        params = [like] * 7
    return (" WHERE " + " AND ".join(where) if where else ""), params


async def people(kind: str = "", q: str = "", limit: int = 100, offset: int = 0) -> list:
    """Реестр пользователей: студенты, сотрудники, гости и заявки — с именами и никами."""
    where, params = _contact_filter(kind, q)
    return await db.many(
        f"{_CONTACT_SELECT}{where} ORDER BY c.last_seen DESC, c.user_id LIMIT ? OFFSET ?",
        params + [max(1, int(limit)), max(0, int(offset))],
    )


async def people_count(kind: str = "", q: str = "") -> int:
    where, params = _contact_filter(kind, q)
    row = await db.one(f"SELECT COUNT(*) n FROM contacts c "
                       f"LEFT JOIN users u ON u.user_id=c.user_id "
                       f"LEFT JOIN admins a ON a.user_id=c.user_id{where}", params)
    return row["n"]


async def people_overview() -> dict:
    """Сколько всего людей знает бот и как они распределены."""
    row = await db.one(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END) students, "
        "SUM(CASE WHEN a.user_id IS NOT NULL THEN 1 ELSE 0 END) staff, "
        "SUM(CASE WHEN u.user_id IS NULL AND a.user_id IS NULL THEN 1 ELSE 0 END) guests, "
        "SUM(CASE WHEN c.last_seen >= datetime('now','-30 days') THEN 1 ELSE 0 END) active_30 "
        "FROM contacts c "
        "LEFT JOIN users u ON u.user_id=c.user_id "
        "LEFT JOIN admins a ON a.user_id=c.user_id"
    )
    requests = await db.one("SELECT COUNT(*) n FROM staff_requests WHERE status='new'")
    return {
        "total": row["total"] or 0,
        "students": row["students"] or 0,
        "staff": row["staff"] or 0,
        "guests": row["guests"] or 0,
        "active_30": row["active_30"] or 0,
        "requests": requests["n"] or 0,
    }


async def user_card(user_id: str) -> dict | None:
    """Всё известное об одном человеке: контакт, регистрация, сотрудник, обращения, заявка."""
    row = await db.one(f"{_CONTACT_SELECT} WHERE c.user_id=?", (str(user_id),))
    if not row:
        return None
    result = _row_dict(row)
    result["kind"] = contact_kind(row)
    result["tickets_list"] = await db.many(
        "SELECT ticket_id, category, status, created_at FROM tickets WHERE student_id=? ORDER BY ticket_id DESC LIMIT 10",
        (str(user_id),),
    )
    result["request"] = _row_dict(await db.one("SELECT * FROM staff_requests WHERE user_id=?", (str(user_id),)))
    return result


# ── удаление человека ─────────────────────────────────────────────────────────
async def student_open_tickets_count(user_id: str) -> int:
    """Сколько у человека не закрытых обращений, где он автор."""
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    row = await db.one(
        f"SELECT COUNT(*) n FROM tickets WHERE student_id=? AND status IN ({placeholders})",
        (str(user_id), *OPEN_STATUSES),
    )
    return row["n"]


async def delete_user(user_id: str, with_tickets: bool = False) -> tuple[bool, str]:
    """Удаляет человека: регистрацию, карточку сотрудника, контакт, состояние, подписки.

    Возвращает (получилось ли, сообщение для сис-админа). Обращения и переписка по
    умолчанию остаются: их видно в работе сотрудника, и удалять их — отдельное
    решение (with_tickets=True, тогда с обращениями уходят сообщения и события по
    каскаду). Сис-админа удалить нельзя: его роль настраивается во вкладке
    «Сотрудники».
    """
    uid = str(user_id)
    admin_row = await db.one("SELECT full_name, role_type FROM admins WHERE user_id=?", (uid,))
    if admin_row and is_sysadmin_role(as_str(admin_row["role_type"])):
        who = as_str(admin_row["full_name"]) or uid
        return False, f"{who} — сис-админ, его удаляют во вкладке «Сотрудники»"
    opened = await student_open_tickets_count(uid)
    if opened and not with_tickets:
        return False, f"У него {opened} открытых обращений — закройте их или удалите вместе с обращениями"
    user_row = await db.one("SELECT full_name FROM users WHERE user_id=?", (uid,))
    name = as_str(user_row["full_name"]) if user_row else ""
    tables = ("user_states", "schedule_subscriptions", "staff_requests", "login_attempts",
              "contacts", "users", "admins")
    async with db._conn() as c:
        for table in tables:
            await c.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        tickets = 0
        if with_tickets:
            cur = await c.execute("DELETE FROM tickets WHERE student_id=?", (uid,))
            tickets = cur.rowcount
        await c.commit()
    detail = f"удалено обращений: {tickets}" if with_tickets else "обращения оставлены"
    return True, f"{'Пользователь ' + name if name else 'Пользователь'} удалён ({detail})"


async def delete_ticket(ticket_id: int) -> tuple[bool, str]:
    """Удаляет обращение вместе с перепиской и историей.

    Возвращает (получилось ли, сообщение). Сообщения и события удаляются по
    каскаду внешних ключей, поэтому в базе не остаётся половины переписки.
    """
    row = await db.one("SELECT student_id FROM tickets WHERE ticket_id=?", (int(ticket_id),))
    if not row:
        return False, f"Обращение №{ticket_id} не найдено"
    await db.run_count("DELETE FROM tickets WHERE ticket_id=?", (int(ticket_id),))
    return True, f"Обращение №{ticket_id} удалено"


# ── сводка дня для сис-админа ─────────────────────────────────────────────────
async def admin_today() -> dict:
    """Один запрос на всё: что требует внимания сис-админа прямо сейчас.

    Считается на стороне SQLite, чтобы панель и бот показывали одно и то же.
    """
    row = await db.one(
        "SELECT "
        "(SELECT COUNT(*) FROM tickets WHERE status='new') no_answer, "
        "(SELECT COUNT(*) FROM tickets WHERE status='ready' AND doc_url='') ready_not_picked, "
        "(SELECT COUNT(*) FROM staff_requests WHERE status='new') requests, "
        "(SELECT COUNT(*) FROM contacts c LEFT JOIN admins a ON a.user_id=c.user_id "
        " WHERE a.user_id IS NULL) no_staff, "
        "(SELECT COUNT(*) FROM staff_invites WHERE used_by='' AND "
        "(expires_at='' OR expires_at > datetime('now'))) codes_active, "
        "(SELECT COUNT(*) FROM user_states WHERE created_at='' OR created_at < datetime('now','-1 day')) stuck_states, "
        "(SELECT COUNT(*) FROM tickets WHERE created_at >= datetime('now','-1 day')) tickets_day, "
        "(SELECT ROUND(AVG(julianday(m.created_at) - julianday(t.created_at)), 2) FROM ticket_messages m "
        " JOIN tickets t ON t.ticket_id = m.ticket_id WHERE m.sender_role='staff') avg_reply_days"
    )
    days = row["avg_reply_days"]
    return {
        "no_answer": row["no_answer"] or 0,
        "ready_not_picked": row["ready_not_picked"] or 0,
        "requests": row["requests"] or 0,
        "no_staff": row["no_staff"] or 0,
        "codes_active": row["codes_active"] or 0,
        "stuck_states": row["stuck_states"] or 0,
        "tickets_day": row["tickets_day"] or 0,
        "avg_reply": _duration_ru(days),
    }


def _duration_ru(days) -> str:
    """Сколько времени прошло: «2 ч 15 мин», «3 дн» — для сводки дня."""
    if days is None:
        return ""
    minutes = int(float(days) * 24 * 60)
    if minutes < 60:
        return f"{max(minutes, 1)} мин"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes // (24 * 60)} дн"


# ── коды и заявки на роль сотрудника ──────────────────────────────────────────
async def create_invite(
    code: str,
    user_id: str = "",
    full_name: str = "",
    created_by: str = "",
    ttl_hours: Optional[int] = None,
) -> str:
    """Создаёт код сотрудника. ttl_hours=0 — без срока. Возвращает код."""
    normalized = norm_code(code)
    hours = config.STAFF_CODE_TTL if ttl_hours is None else int(ttl_hours)
    await db.run(
        "INSERT INTO staff_invites(code, user_id, full_name, created_by, expires_at) "
        "VALUES(?,?,?,?, CASE WHEN ?>0 THEN datetime('now', ?) ELSE '' END) "
        "ON CONFLICT(code) DO UPDATE SET user_id=excluded.user_id, full_name=excluded.full_name, "
        "created_by=excluded.created_by, created_at=datetime('now'), expires_at=excluded.expires_at, "
        "used_by='', used_at=''",
        (normalized, as_str(user_id).strip(), as_str(full_name).strip()[:100], as_str(created_by).strip(),
         hours, f"+{hours} hours"),
    )
    return normalized


async def use_invite(code: str, user_id: str) -> tuple[bool, str]:
    """Пробует активировать код для пользователя. → (принят ли код, причина отказа).

    UPDATE с условием делает проверку и погашение атомарно: два человека с одним
    кодом не смогут пройти одновременно — победит первый, второй получит отказ.
    """
    normalized = norm_code(code)
    row = await db.one(
        "SELECT user_id, used_by, expires_at<>'' AND expires_at <= datetime('now') AS expired "
        "FROM staff_invites WHERE code=?",
        (normalized,),
    )
    if not row:
        return False, "Код не найден. Проверьте раскладку клавиатуры и пробелы."
    if row["used_by"]:
        return False, "Этот код уже использовали. Попросите сис-админа выдать новый."
    if row["expired"]:
        return False, "Срок действия кода истёк. Попросите новый код."
    if row["user_id"] and as_str(row["user_id"]) != str(user_id):
        return False, "Этот код выдан другому сотруднику."
    changed = await db.run_count(
        "UPDATE staff_invites SET used_by=?, used_at=datetime('now') "
        "WHERE code=? AND used_by='' AND (user_id='' OR user_id=?)",
        (str(user_id), normalized, str(user_id)),
    )
    return (True, "") if changed else (False, "Код уже использован.")


async def invite_state(code: str) -> str:
    """Состояние кода: active | used | expired | unknown."""
    row = await db.one(
        "SELECT used_by, expires_at<>'' AND expires_at <= datetime('now') AS expired "
        "FROM staff_invites WHERE code=?",
        (norm_code(code),),
    )
    if not row:
        return "unknown"
    if row["used_by"]:
        return "used"
    return "expired" if row["expired"] else "active"


async def list_invites(limit: int = 50) -> list:
    return await db.many(
        "SELECT i.*, COALESCE(u.full_name, '') fio, COALESCE(a.full_name, '') used_by_name "
        "FROM staff_invites i "
        "LEFT JOIN users u ON u.user_id=i.user_id "
        "LEFT JOIN admins a ON a.user_id=i.used_by "
        "ORDER BY i.created_at DESC, i.code LIMIT ?",
        (limit,),
    )


async def delete_invite(code: str) -> None:
    await db.run("DELETE FROM staff_invites WHERE code=?", (norm_code(code),))


async def set_invite_ttl(code: str, hours: int) -> tuple[bool, str]:
    """Меняет срок уже выданного кода: 0 — бессрочно. Возвращает (получилось, срок)."""
    normalized = norm_code(code)
    row = await db.one("SELECT used_by FROM staff_invites WHERE code=?", (normalized,))
    if not row:
        return False, "Код не найден"
    if row["used_by"]:
        return False, "Этот код уже использован"
    await db.run(
        "UPDATE staff_invites SET expires_at = CASE WHEN ?>0 THEN datetime('now', ?) ELSE '' END WHERE code=?",
        (int(hours), f"+{int(hours)} hours", normalized),
    )
    return True, ttl_label(hours)


async def create_staff_request(
    user_id: str,
    full_name: str = "",
    position: str = "",
    office: str = "",
    note: str = "",
) -> None:
    await db.run(
        "INSERT INTO staff_requests(user_id, full_name, position, office, note) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, position=excluded.position, "
        "office=excluded.office, note=excluded.note, status='new', updated_at=datetime('now')",
        (str(user_id), as_str(full_name).strip()[:100], as_str(position).strip()[:100],
         as_str(office).strip()[:100], as_str(note).strip()[:300]),
    )


async def staff_requests(status: str = "new", limit: int = 100) -> list:
    where = "WHERE r.status=?" if status else ""
    return await db.many(
        f"SELECT r.*, c.username, c.display_name FROM staff_requests r "
        f"LEFT JOIN contacts c ON c.user_id=r.user_id {where} ORDER BY r.updated_at DESC LIMIT ?",
        ([status] if status else []) + [limit],
    )


async def get_staff_request(user_id: str):
    return await db.one("SELECT * FROM staff_requests WHERE user_id=?", (str(user_id),))


async def set_staff_request_status(user_id: str, status: str) -> None:
    await db.run(
        "UPDATE staff_requests SET status=?, updated_at=datetime('now') WHERE user_id=?",
        (status, str(user_id)),
    )


# ── защита от подбора кода ────────────────────────────────────────────────────
async def note_attempt(user_id: str) -> None:
    await db.run("INSERT INTO login_attempts(user_id) VALUES(?)", (str(user_id),))


async def attempts_count(user_id: str, minutes: int = 60) -> int:
    row = await db.one(
        "SELECT COUNT(*) n FROM login_attempts WHERE user_id=? AND created_at >= datetime('now', ?)",
        (str(user_id), f"-{int(minutes)} minutes"),
    )
    return row["n"]


async def clear_attempts(user_id: str) -> None:
    await db.run("DELETE FROM login_attempts WHERE user_id=?", (str(user_id),))


async def attempts_log(limit: int = 30) -> list:
    """Кто и сколько раз пробовал коды за последний час — для панели."""
    return await db.many(
        "SELECT a.user_id, COUNT(*) tries, MAX(a.created_at) last_try, "
        "COALESCE(c.display_name, c.username, '') label, COALESCE(s.status, '') request_status "
        "FROM login_attempts a "
        "LEFT JOIN contacts c ON c.user_id=a.user_id "
        "LEFT JOIN staff_requests s ON s.user_id=a.user_id "
        "WHERE a.created_at >= datetime('now','-24 hours') "
        "GROUP BY a.user_id ORDER BY tries DESC, last_try DESC LIMIT ?",
        (limit,),
    )


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
        f"SELECT * FROM admins ORDER BY (role_type {STAFF_ROLES_SQL}), full_name"
    )


ADMIN_FIELDS = ("full_name", "role", "position", "department", "office", "ticket_category", "can_broadcast")


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
    """Назначает сис-админом навсегда (без правки .env и перезапуска)."""
    await db.run(
        "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?,?, 'sysadmin', 1) "
        "ON CONFLICT(user_id) DO UPDATE SET role_type='sysadmin', can_broadcast=1",
        (str(user_id), as_str(full_name).strip()[:100] or "Сис-админ"),
    )


# ── сис-админы: список живёт в базе, .env только заводит первых ────────────────
async def list_sysadmins() -> list:
    """Все, у кого есть доступ к панели: владелец и сис-админы, с ником из профиля MAX."""
    rows = await db.many(
        "SELECT a.user_id, a.full_name, a.role_type, a.created_at, "
        "COALESCE(c.username, '') username, COALESCE(c.last_seen, '') last_seen FROM admins a "
        "LEFT JOIN contacts c ON c.user_id=a.user_id "
        "WHERE a.role_type IN ('owner','sysadmin','superadmin') ORDER BY (a.role_type<>'owner'), a.created_at, a.user_id"
    )
    from_env = {str(value) for value in config.SYSADMIN_IDS}
    owners = {str(value) for value in config.ROOT_IDS or []}
    return [
        {**{key: row[key] for key in row.keys()},
         "in_env": as_str(row["user_id"]) in from_env,
         "is_owner": as_str(row["user_id"]) in owners or as_str(row["role_type"]) == "owner"}
        for row in rows
    ]


async def sysadmin_count() -> int:
    row = await db.one("SELECT COUNT(*) n FROM admins WHERE role_type IN ('owner','sysadmin','superadmin')")
    return row["n"] or 0


async def is_owner(user_id: str) -> bool:
    """Владелец бота: максимальные права, назначается при старте и не отзывается."""
    uid = str(user_id)
    if uid in {str(value) for value in config.ROOT_IDS or []}:
        return True
    row = await db.one("SELECT role_type FROM admins WHERE user_id=?", (uid,))
    return bool(row) and is_owner_role(as_str(row["role_type"]))


async def is_sysadmin(user_id: str) -> bool:
    """Есть ли у человека доступ к панели сис-админа (владелец, сис-админ)."""
    uid = str(user_id)
    if uid in {str(value) for value in config.ROOT_IDS or []}:
        return True
    row = await db.one("SELECT role_type FROM admins WHERE user_id=?", (uid,))
    return bool(row) and is_sysadmin_role(as_str(row["role_type"]))


async def grant_sysadmin(user_id: str, full_name: str = "") -> tuple[bool, str]:
    """Выдаёт права сис-админа и снимает возможный отзыв из .env."""
    uid = str(user_id).strip()
    if not uid.isdigit():
        return False, "MAX ID состоит только из цифр"
    if await is_owner(uid):
        return False, f"ID {uid} — владелец бота, его права постоянны"
    revoked = await revoked_sysadmins()
    if uid in revoked:
        await _save_revoked(revoked - {uid})
    await add_sysadmin(uid, full_name)
    return True, f"Сис-админ {full_name or uid} (ID {uid}) добавлен"


async def revoke_sysadmin(user_id: str) -> tuple[bool, str]:
    """Лишает прав сис-админа. Последнего и владельца снять нельзя."""
    uid = str(user_id)
    if await is_owner(uid):
        return False, "у этого ID максимальные права владельца, их нельзя снять"
    if (await sysadmin_count()) <= 1:
        return False, "Это последний сис-админ: сначала добавьте другого"
    if await get_admin(uid) is None:
        return False, "Пользователь не найден"
    await delete_staff(uid)
    revoked = await revoked_sysadmins()
    await _save_revoked(revoked | {uid})
    return True, f"Права сис-админа у ID {uid} сняты"


async def revoked_sysadmins() -> set:
    """ID, которым доступ отозван в панели: перезапуск бота их не вернёт."""
    value = as_str(await db.get_setting(db.SYSADMINS_REVOKED_KEY, ""))
    return {part.strip() for part in value.split(",") if part.strip()}


async def _save_revoked(ids: set) -> None:
    await db.set_setting(db.SYSADMINS_REVOKED_KEY, ",".join(sorted(ids)))


# ── журнал действий сис-админа ────────────────────────────────────────────────
async def log_action(actor_id: str, action: str, details: str = "") -> None:
    """Записывает, кто и что сделал. Подробности обрезаются, текст виден в панели."""
    await db.run(
        "INSERT INTO admin_log(actor_id, action, details) VALUES(?,?,?)",
        (as_str(actor_id).strip()[:64], as_str(action).strip()[:60], as_str(details).strip()[:300]),
    )


async def admin_log(limit: int = 100) -> list:
    """Последние действия сис-админов: новые сверху."""
    return await db.many(
        "SELECT l.id, l.actor_id, l.action, l.details, l.created_at, COALESCE(a.full_name, '') actor_name "
        "FROM admin_log l LEFT JOIN admins a ON a.user_id=l.actor_id "
        "ORDER BY l.id DESC LIMIT ?",
        (max(1, min(int(limit), 1000)),),
    )


async def admin_log_counts(days: int = 30) -> dict:
    """Сколько действий каждого вида за N дней — для обзора панели."""
    return {
        as_str(row["action"]): row["n"]
        for row in await db.many(
            "SELECT action, COUNT(*) n FROM admin_log WHERE created_at >= datetime('now', ?) "
            "GROUP BY action ORDER BY n DESC",
            (f"-{int(days)} days",),
        )
    }


async def list_users(limit: int = 200, group_code: str = "") -> list:
    """Студенты с числом обращений, ником и временем последнего обращения."""
    where, params = ("WHERE u.group_code=?", (_code(group_code),)) if _code(group_code) else ("", ())
    return await db.many(
        "SELECT u.user_id, u.full_name, u.group_code, u.created_at, "
        "COALESCE(c.username, '') username, COALESCE(c.last_seen, '') last_seen, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.student_id=u.user_id) tickets "
        f"FROM users u LEFT JOIN contacts c ON c.user_id=u.user_id{where} "
        "ORDER BY u.full_name LIMIT ?",
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
    staff = await db.one(f"SELECT COUNT(*) n FROM admins WHERE role_type {STAFF_ROLES_SQL}")
    week = await db.one("SELECT COUNT(*) n FROM tickets WHERE created_at >= datetime('now','-7 days')")
    total = await db.one("SELECT COUNT(*) n FROM tickets")
    return {"students": students["n"], "staff": staff["n"], "week": week["n"], "total": total["n"],
            "people": await people_overview()}
