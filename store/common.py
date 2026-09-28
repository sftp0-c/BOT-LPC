"""Общие хелперы слоя доступа к базе.

Сюда попадает то, чем пользуются все разделы: чтение значений из строки
sqlite3, разбор дат из панелей и канонический вид кода группы. Раздел не
обращается к доменным таблицам и ничего не знает про обращения или
сотрудников."""

from datetime import date, datetime

import database as db
from utils import as_str, group_code, norm_group


# Параметр upsert_user тоже называется group_code и перекрывает импорт,
# поэтому для канонического вида используем отдельное имя.
canonical_group = group_code


TOPIC_SOURCES = db.TOPIC_SOURCES


# Роли с доступом к панели. Владелец (owner) — тоже: в списках сотрудников и в
# статистике его быть не должно, поэтому фильтруем одной строкой.
ADMIN_ROLES_SQL = "'owner','sysadmin','superadmin'"


STAFF_ROLES_SQL = f"NOT IN ({ADMIN_ROLES_SQL})"


# ── общие хелперы ────────────────────────────────────────────────────────────────
def _row_value(row, key: str, default=""):
    """Значение колонки у строки sqlite3.Row, словаря или None.

    Сотрудника в отпуск передают и словарём (из выборки), и объектом
    с __getitem__, поэтому читаем одинаково.
    """
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _row_dict(row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


# ── даты и канонический вид кода группы ───────────────────────────────────────────
def _parse_day(value) -> date | None:
    """Дата из строки панели или бота: «2026-10-01», «01.10.2026», «1 октября»."""
    raw = as_str(value).strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _code(group: str) -> str:
    """Единый вид кода группы (utils.group_code): «24-23 (П)» и «2423П» -> «24-23П».

    Через одну функцию нормализуются все, кто ищет группу: студенты, расписания,
    справочник, - иначе одна и та же группа находилась бы под разными ключами.
    """
    return group_code(as_str(group))


def _code_forms(group: str) -> list:
    """Все формы кода группы, в которых он может лежать в базе.

    Запись идёт через norm_group («24-23 (П)»), а канонический вид через
    utils.group_code («24-23П»). Обе формы встречаются в данных, поэтому
    фильтр должен ловить и ту, и другую - иначе группа, записанная со
    скобками, не находится никогда.
    """
    raw = as_str(group).strip()
    forms = [value for value in (raw, norm_group(raw), _code(raw)) if value]
    seen: list = []
    for value in forms:
        if value not in seen:
            seen.append(value)
    return seen or [""]


# ── общий SELECT реестра пользователей ──────────────────────────────────────────
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
