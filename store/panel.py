"""Данные сводки дня и списков веб-панели сис-админа.

Только чтение: панель показывает то, что уже есть в базе, поэтому раздел не
меняет состояние."""

import clock
import database as db
from utils import as_str, to_int

from .common import STAFF_ROLES_SQL, _code_forms, _row_value


# ── сводка дня для сис-админа ─────────────────────────────────────────────────
async def admin_today() -> dict:
    """Оперативная сводка и календарная статистика от полуночи колледжа."""
    day_start = f"{clock.today():%Y-%m-%d} 00:00:00"
    row = await db.one(
        "SELECT "
        "(SELECT COUNT(*) FROM tickets WHERE status='new' AND deleted_at='') no_answer, "
        "(SELECT COUNT(*) FROM tickets WHERE status='ready' AND doc_url='' AND deleted_at='') ready_not_picked, "
        "(SELECT COUNT(*) FROM staff_requests WHERE status='new') requests, "
        "(SELECT COUNT(*) FROM contacts c LEFT JOIN admins a ON a.user_id=c.user_id "
        " WHERE a.user_id IS NULL) no_staff, "
        "(SELECT COUNT(*) FROM staff_invites WHERE used_by='' AND "
        "(expires_at='' OR expires_at > ?)) codes_active, "
        "(SELECT COUNT(*) FROM user_states WHERE created_at='' OR created_at < ?) stuck_states, "
        "(SELECT COUNT(*) FROM tickets WHERE created_at >= ?) tickets_day, "
        "(SELECT COUNT(DISTINCT ticket_id) FROM ticket_events "
        " WHERE event='status' AND detail IN ('accepted','in_progress') AND created_at>=?) taken_day, "
        "(SELECT COUNT(DISTINCT ticket_id) FROM ticket_messages "
        " WHERE sender_role='staff' AND created_at>=?) answered_day, "
        "(SELECT COUNT(DISTINCT ticket_id) FROM ticket_events "
        " WHERE event='status' AND detail IN ('completed','rejected') AND created_at>=?) closed_day, "
        "(SELECT ROUND(AVG((julianday(r.first_reply) - julianday(t.created_at)) * 24 * 60)) "
        " FROM tickets t JOIN (SELECT ticket_id, MIN(created_at) first_reply FROM ticket_messages "
        " WHERE sender_role='staff' GROUP BY ticket_id) r ON r.ticket_id=t.ticket_id "
        " WHERE t.created_at>=?) avg_reply_minutes",
        (clock.stamp(), clock.stamp_at(-24 * 60), day_start, day_start,
         day_start, day_start, day_start),
    )
    by_hour = await db.many(
        "SELECT substr(created_at, 12, 2) hour, COUNT(*) count FROM tickets "
        "WHERE created_at>=? GROUP BY hour ORDER BY hour", (day_start,))
    avg_minutes = row["avg_reply_minutes"]
    return {
        "no_answer": row["no_answer"] or 0,
        "ready_not_picked": row["ready_not_picked"] or 0,
        "requests": row["requests"] or 0,
        "no_staff": row["no_staff"] or 0,
        "codes_active": row["codes_active"] or 0,
        "stuck_states": row["stuck_states"] or 0,
        "tickets_day": row["tickets_day"] or 0,
        "created_day": row["tickets_day"] or 0,
        "taken_day": row["taken_day"] or 0,
        "answered_day": row["answered_day"] or 0,
        "closed_day": row["closed_day"] or 0,
        "hourly": [{"hour": as_str(item["hour"]), "count": item["count"] or 0}
                   for item in by_hour],
        "avg_reply": _minutes_ru(avg_minutes),
    }


def _minutes_ru(value) -> str:
    """Минуты первого ответа в ту же краткую подпись сводки."""
    if value is None:
        return ""
    minutes = max(1, int(float(value)))
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes // (24 * 60)} дн"


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
        "created": "обращение создано", "status": "смена статуса", "ready": "документ готов",
        "message_student": "сообщение студента", "message_staff": "ответ сотрудника",
        "archived": "в архив", "restored": "возврат из архива", "forward": "передача сотруднику",
        "edited": "правка обращения", "archive": "в архив", "restore": "возврат из архива", "assign": "назначение",
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


def _ident(name: str) -> str:
    """Имя таблицы или колонки в кавычках: защита от пробелов и ключевых слов."""
    clean = as_str(name).replace('"', '""')
    return f'"{clean}"'


async def ping_database() -> bool:
    """Проверка доступности базы данных для диагностики панели."""
    res = await db.one("SELECT 1")
    return bool(res)


async def faq_counts() -> tuple[int, int]:
    """Счётчики частых вопросов: (всего, включено)."""
    row = await db.one("SELECT COUNT(*) AS total, COALESCE(SUM(active),0) AS enabled FROM faq")
    return int(row["total"] or 0), int(row["enabled"] or 0)


async def toggle_faq_item(faq_id: int) -> tuple[bool, int]:
    """Переключает статус вопроса FAQ (активен/выключен).

    Возвращает (найден, старый_статус_active).
    """
    row = await db.one("SELECT active FROM faq WHERE id=?", (int(faq_id),))
    if not row:
        return False, 0
    was_active = int(row["active"] or 0)
    await db.run("UPDATE faq SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (int(faq_id),))
    return True, was_active


async def backup_database(target: str | None = None) -> str:
    """Создаёт резервную копию базы данных."""
    return await db.backup_to(target)


async def data_table_names() -> list[str]:
    """Все таблицы базы, кроме служебных sqlite_* и закрытых."""
    rows = await db.many(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [
        as_str(row["name"]) for row in rows
        if not as_str(row["name"]).startswith("staff_web_")
        and row["name"] not in ("workspace_operations", "ticket_files", "public_feedback_deleted_nonces")
    ]


async def data_table_columns(name: str) -> list[dict]:
    """PRAGMA table_info: имя, тип, NOT NULL, значение по умолчанию, ключ."""
    rows = await db.many(f"PRAGMA table_info({_ident(name)})")
    if name == "audit_log":
        safe = {"id", "created_at", "user_id", "role", "action", "target_type", "target_id", "result"}
        rows = [row for row in rows if row["name"] in safe]
    elif name == "public_feedback":
        rows = [row for row in rows if row["name"] != "submission_nonce"]
    elif name == "tickets":
        rows = [row for row in rows if row["name"] != "workspace_revision"]
    return [
        {"name": as_str(row["name"]), "type": as_str(row["type"]),
         "notnull": bool(row["notnull"]), "default": as_str(row["dflt_value"]),
         "pk": bool(row["pk"]),
         "time": as_str(row["name"]).endswith("_at") or row["name"] in ("created", "updated")}
        for row in rows
    ]


async def data_table_sql(name: str) -> str | None:
    """Текст CREATE TABLE таблицы."""
    row = await db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return as_str(row["sql"]) if row else None


async def data_row_count(name: str) -> int:
    """Сколько строк в таблице."""
    row = await db.one(f"SELECT COUNT(*) n FROM {_ident(name)}")
    return to_int(row["n"]) if row else 0


async def data_table_stats_raw(name: str, parts: list[str]):
    """Выполняет вычисление статистики таблицы."""
    return await db.one(f"SELECT {', '.join(parts)} FROM {_ident(name)}")


async def data_fetch_rows(name: str, prefix: str, where: str, order: str, params: tuple, limit: int, offset: int = 0) -> list:
    """Строки таблицы с сортировкой и фильтрами."""
    return await db.many(
        f"SELECT {prefix}* FROM {_ident(name)}{where}{order} LIMIT ? OFFSET ?",
        params + (max(1, int(limit)), max(0, int(offset)))
    )


async def data_search_query(sql: str, args: tuple) -> list:
    """Результаты поиска по колонке таблицы."""
    return await db.many(sql, args)


async def data_filtered_count(name: str, where: str, params: tuple) -> int:
    """Количество строк с учётом фильтра WHERE."""
    row = await db.one(f"SELECT COUNT(*) n FROM {_ident(name)}{where}", params)
    return to_int(row["n"]) if row else 0


async def data_get_row(name: str, rowid: int, prefix_rowid: bool = False):
    """Строка таблицы по rowid."""
    prefix = "rowid AS rid, " if prefix_rowid else ""
    return await db.one(f"SELECT {prefix}* FROM {_ident(name)} WHERE rowid=?", (int(rowid),))


async def data_update_row(name: str, rowid: int, sets: list[str], params: tuple) -> None:
    """Обновление строки таблицы по rowid."""
    await db.run(f"UPDATE {_ident(name)} SET {', '.join(sets)} WHERE rowid=?", params + (int(rowid),))


async def data_insert_row(name: str, keys: list[str], params: tuple) -> int:
    """Вставка строки в таблицу."""
    return await db.run(
        f"INSERT INTO {_ident(name)}({', '.join(keys)}) VALUES({', '.join('?' * len(keys))})",
        params
    )


async def data_delete_row(name: str, rowid: int) -> None:
    """Удаление строки таблицы по rowid."""
    await db.run(f"DELETE FROM {_ident(name)} WHERE rowid=?", (int(rowid),))
