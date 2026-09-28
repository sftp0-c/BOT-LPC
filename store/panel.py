"""Данные сводки дня и списков веб-панели сис-админа.

Только чтение: панель показывает то, что уже есть в базе, поэтому раздел не
меняет состояние."""

import clock
import database as db
from utils import as_str, to_int

from .common import STAFF_ROLES_SQL, _code_forms, _row_value


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
        "(expires_at='' OR expires_at > ?)) codes_active, "
        "(SELECT COUNT(*) FROM user_states WHERE created_at='' OR created_at < ?) stuck_states, "
        "(SELECT COUNT(*) FROM tickets WHERE created_at >= ?) tickets_day, "
        "(SELECT ROUND(AVG(julianday(m.created_at) - julianday(t.created_at)), 2) FROM ticket_messages m "
        " JOIN tickets t ON t.ticket_id = m.ticket_id WHERE m.sender_role='staff') avg_reply_days",
        (clock.stamp(), clock.stamp_at(-24 * 60), clock.stamp_at(-24 * 60)),
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


# ── данные для веб-панели сис-админа ──────────────────────────────────────────
async def all_admins() -> list:
    """Все строки admins, включая сис-админов: сначала сис-админы, потом сотрудники."""
    return await db.many(
        f"SELECT * FROM admins ORDER BY (role_type {STAFF_ROLES_SQL}), full_name"
    )


async def list_users(limit: int = 200, group_code: str = "", offset: int = 0) -> list:
    """Студенты с числом обращений, ником и временем последнего обращения.

    offset добавлен для честных страниц в панели: раньше бралось окно «на
    страницу вперёд», и на середине списка переход врёт.
    """
    forms = _code_forms(group_code) if as_str(group_code).strip() else []
    if forms:
        placeholders = ",".join("?" * len(forms))
        where, params = f"WHERE u.group_code IN ({placeholders})", tuple(forms)
    else:
        where, params = "", ()
    return await db.many(
        "SELECT u.user_id, u.full_name, u.group_code, u.created_at, "
        "COALESCE(u.consent_at, '') consent_at, "
        "COALESCE(c.username, '') username, COALESCE(c.last_seen, '') last_seen, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.student_id=u.user_id) tickets "
        f"FROM users u LEFT JOIN contacts c ON c.user_id=u.user_id {where} "
        "ORDER BY u.full_name LIMIT ? OFFSET ?",
        params + (max(1, int(limit)), max(0, int(offset))),
    )


async def list_users_count(group_code: str = "") -> int:
    """Сколько всего студентов с учётом фильтра по группе."""
    forms = _code_forms(group_code) if as_str(group_code).strip() else []
    if forms:
        placeholders = ",".join("?" * len(forms))
        where, params = f"WHERE group_code IN ({placeholders})", tuple(forms)
    else:
        where, params = "", ()
    row = await db.one(f"SELECT COUNT(*) AS c FROM users {where}", params)
    return int(row["c"]) if row else 0


async def recent_ticket_events(limit: int = 30) -> list:
    """Сквозная лента событий по всем обращениям: кто что сделал и когда.

    Раньше события читались только по одному обращению, а ленты на главной
    странице панели не было вовсе - «кто чем занимался» приходилось искать
    вручную в журнале.
    """
    # имени автора в таблице нет - оно склеивается из users и admins,
    # как в ticket_events(), поэтому повторяем ту же склейку
    return await db.many(
        "SELECT e.id, e.ticket_id, e.event, e.detail, e.actor_id, e.created_at, "
        "COALESCE(au.full_name, aa.full_name, '') actor_name, "
        "COALESCE(aa.position, aa.role, '') actor_position, "
        "t.category, t.topic, t.student_id, "
        "COALESCE(u.full_name, '') student_name, COALESCE(u.group_code, '') student_group "
        "FROM ticket_events e "
        "LEFT JOIN tickets t ON t.ticket_id = e.ticket_id "
        "LEFT JOIN users u ON u.user_id = t.student_id "
        "LEFT JOIN users au ON au.user_id = e.actor_id "
        "LEFT JOIN admins aa ON aa.user_id = e.actor_id "
        "ORDER BY e.id DESC LIMIT ?", (int(limit),))


def event_feed_label(row) -> str:
    """Человеческая строка ленты: кто и что сделал с обращением."""
    who = as_str(_row_value(row, "actor_name")) or as_str(_row_value(row, "actor_id")) or "кто-то"
    event = as_str(_row_value(row, "event"))
    ticket_id = to_int(_row_value(row, "ticket_id"), 0)
    labels = {
        "created": "создал", "status": "сменил статус", "ready": "документ готов",
        "message_student": "ответил студенту", "message_staff": "ответил сотруднику",
        "archive": "в архив", "restore": "вернул из архива", "assign": "назначил",
    }
    tail = labels.get(event, event or "что-то сделал")
    return f"№{ticket_id} · {who} {tail}"


# ── состояние данных и настройки ───────────────────────────────────────────────
async def data_gaps() -> list[dict]:
    """Незаполненные данные, из-за которых бот работает хуже.

    Считаем одно запрос на раздел: панели нужны конкретные числа, чтобы
    показать, что стоит дозаполнить, а не «возможно, что-то не так».
    """
    staff = await db.one(
        "SELECT COUNT(*) n FROM admins WHERE role NOT IN ('superadmin', 'sysadmin') "
        "AND (COALESCE(position, '')='' OR COALESCE(role, '')='')")
    students = await db.one("SELECT COUNT(*) n FROM users WHERE COALESCE(group_code, '')=''")
    consent = await db.one("SELECT COUNT(*) n FROM users WHERE COALESCE(consent_at, '')=''")
    schedules = await db.one(
        "SELECT COUNT(*) n FROM groups g WHERE g.active=1 AND NOT EXISTS "
        "(SELECT 1 FROM lessons l WHERE l.group_code=g.group_code)")
    name = await db.one("SELECT COUNT(*) n FROM users WHERE COALESCE(full_name, '')=''")
    return [
        {"key": "staff", "count": int(staff["n"]), "title": "Сотрудники без должности или роли",
         "hint": "Подменю «Обратная связь» и адресные обращения не работают без ролей",
         "link": "/panel/staff"},
        {"key": "group", "count": int(students["n"]), "title": "Студенты без группы",
         "hint": "Без группы не показать расписание и справки",
         "link": "/panel/students"},
        {"key": "name", "count": int(name["n"]), "title": "Студенты без ФИО",
         "hint": "Обращения будут приходить с пустым именем",
         "link": "/panel/students"},
        {"key": "consent", "count": int(consent["n"]), "title": "Нет согласия на обработку данных",
         "hint": "Регистрация до 28.09.2026 шла без согласия — стоит получить",
         "link": "/panel/students?consent=0"},
        {"key": "schedule", "count": int(schedules["n"]), "title": "Активные группы без расписания",
         "hint": "Студенты не увидят пары, пока PDF не импортирован",
         "link": "/panel/schedules"},
    ]


async def all_settings() -> list:
    return await db.many("SELECT key, value FROM settings ORDER BY key")


async def dedupe_stats() -> dict:
    """Сколько событий уже обработано (таблица processed_updates) и какого возраста самое новое."""
    row = await db.one("SELECT COUNT(*) n, MIN(created_at) oldest, MAX(created_at) newest FROM processed_updates")
    return {"processed": row["n"], "oldest": row["oldest"], "newest": row["newest"]}
