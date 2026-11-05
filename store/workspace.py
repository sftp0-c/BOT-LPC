"""Рабочее место: права и сессии проверяются в одной транзакции с данными.

Веб-слой передаёт только ID пользователя и отпечаток серверной сессии, а не
готовое разрешение. История обращения хранит переписку и внутренние заметки;
журнал операций — только хеш запроса и числовые ссылки, без текста.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
import re
import time

import clock
import config
import database as db
from utils import ACCEPT_ON_REPLY, CATS, OPEN_STATUSES, STATUS, is_sysadmin_role

from .tickets import _ticket_time_ms, ticket_filter_dates

WEB_PERMISSIONS = (
    "reply", "status", "note", "forward", "templates", "attachments", "export",
    "view_others", "manage_others",
)
WORKSPACE_FILE_LIMIT = 3
WORKSPACE_FILE_BYTES = 20 * 1024 * 1024
_CLOSED = ("completed", "rejected", "closed")
_ACTIONS = ("reply", "reply_close", "status", "note", "forward")
_FILTERS = {"view", "status", "category", "q", "period", "mine", "date_from", "date_to", "scope"}
_STORAGE_NAME = re.compile(r"[A-Za-zА-Яа-яЁё0-9_-][A-Za-zА-Яа-яЁё0-9._-]{0,59}\Z")
_FILE_SUFFIXES = (".jpg", ".jpeg", ".png", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt")
_MARKER = "📎 Файл: "


class _PrivateText(str):
    """aiosqlite DEBUG не должен писать содержимое, поиск и токены через repr."""

    def __repr__(self):
        return "'<скрыто>'"


def _error(result: str, message: str = "") -> dict:
    messages = {"denied": "Доступ запрещён.", "not_found": "Обращение или файл недоступны.",
                "conflict": "Обращение изменилось. Обновите карточку.",
                "invalid": "Некорректные данные операции."}
    return {"result": result, "message": message or messages[result]}


def _integer(value, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("Ожидается целое число.")
    number = int(value)
    if not minimum <= number <= 9223372036854775807:
        raise ValueError("Число вне допустимого диапазона.")
    return number


@asynccontextmanager
async def _transaction(*, write: bool = False):
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


async def _one(conn, sql: str, params: tuple = ()):
    return await (await conn.execute(sql, params)).fetchone()


async def _many(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in await (await conn.execute(sql, params)).fetchall()]


async def _principal(conn, actor_id, session_hash: str, admin: bool):
    if isinstance(actor_id, bool) or not isinstance(actor_id, (str, int)) or not str(actor_id):
        return None
    actor_id = str(actor_id)
    if admin:
        row = await _one(conn, "SELECT * FROM admins WHERE user_id=?", (actor_id,))
        root = actor_id in {str(value) for value in (config.ROOT_IDS or [])}
        if not root and (not row or not is_sysadmin_role(row["role_type"]) or row["is_test"]):
            return None
        return {"user_id": actor_id, "full_name": row["full_name"] if row else "Владелец",
                "position": row["position"] if row else "", "permissions": list(WEB_PERMISSIONS),
                "is_admin": True}
    # Этот helper НЕ открывает новое соединение: проверка привязана к снимку/записи.
    from .web_access import staff_web_principal_in_tx

    principal = await staff_web_principal_in_tx(conn, _PrivateText(session_hash or ""), now=int(time.time()))
    if not principal or principal.get("user_id") != actor_id:
        return None
    permissions = principal.get("permissions")
    if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in WEB_PERMISSIONS
                                               for p in permissions):
        return None
    permissions = set(permissions)
    if "manage_others" in permissions:
        permissions.add("view_others")
    return {**principal, "permissions": sorted(permissions), "is_admin": False}


def _has(principal: dict, permission: str) -> bool:
    return principal["is_admin"] or permission in principal["permissions"]


def _acl(principal: dict, *, mine: bool = False) -> tuple[str, tuple]:
    if mine or not _has(principal, "view_others"):
        return "t.target_admin_id=? COLLATE BINARY", (principal["user_id"],)
    return "1=1", ()


def _may_change(principal: dict, ticket: dict, action: str) -> bool:
    if ticket["target_admin_id"] != principal["user_id"] and not _has(principal, "manage_others"):
        return False
    needed = ("reply", "status") if action == "reply_close" else (action,)
    return all(_has(principal, permission) for permission in needed)


def _allowed_actions(principal: dict, ticket: dict) -> list[str]:
    allowed = [permission for permission in ("templates", "attachments") if _has(principal, permission)]
    if not ticket["deleted_at"]:
        allowed += [action for action in _ACTIONS if _may_change(principal, ticket, action)
                    and not (action == "reply_close" and ticket["status"] in _CLOSED)]
    return allowed


async def _query_setup(conn):
    await conn.create_function("ticket_time_ms", 1, _ticket_time_ms, deterministic=True)


def _base_select() -> str:
    open_sql = ",".join("'" + value + "'" for value in OPEN_STATUSES)
    return (
        "SELECT t.*, COALESCE(u.full_name,'') student_name, COALESCE(u.group_code,'') student_group, "
        "COALESCE(a.full_name,'') staff_name, COALESCE(NULLIF(a.position,''),a.role,'') staff_position, "
        "COALESCE(m.sender_role,'') latest_role, COALESCE(m.created_at,'') latest_message_at, "
        "CASE WHEN t.status='in_progress' AND e.event='status' "
        "AND (e.detail='in_progress' OR e.detail LIKE 'статус: % → in_progress') "
        "THEN e.created_at ELSE '' END in_progress_at, "
        f"COALESCE(t.status IN ({open_sql}) AND m.sender_role='student',0) is_waiting, "
        f"COALESCE(t.status IN ({open_sql}) AND m.sender_role='student' "
        "AND ticket_time_ms(m.created_at)<=?,0) is_overdue "
        "FROM tickets t LEFT JOIN users u ON u.user_id=t.student_id "
        "LEFT JOIN admins a ON a.user_id=t.target_admin_id "
        "LEFT JOIN ticket_messages m ON m.id=(SELECT id FROM ticket_messages "
        "WHERE ticket_id=t.ticket_id ORDER BY id DESC LIMIT 1) "
        "LEFT JOIN ticket_events e ON e.id=(SELECT id FROM ticket_events "
        "WHERE ticket_id=t.ticket_id AND event IN ('status','ready') ORDER BY id DESC LIMIT 1) "
    )


def _cutoff() -> int:
    return _ticket_time_ms((clock.now() - timedelta(hours=24)).isoformat())


def _row_flags(row: dict, principal: dict) -> dict:
    return {**row, "is_own": row["target_admin_id"] == principal["user_id"],
            "revision": row["workspace_revision"], "allowed_actions": _allowed_actions(principal, row)}


async def _ticket_row(conn, principal: dict, ticket_id: int):
    clause, params = _acl(principal)
    await _query_setup(conn)
    row = await _one(conn, _base_select() + "WHERE t.ticket_id=? AND " + clause,
                     (_cutoff(), ticket_id, *params))
    return _row_flags(dict(row), principal) if row else None


def _normal_filters(filters, principal: dict) -> dict:
    if filters is None:
        filters = {}
    if not isinstance(filters, dict) or filters.keys() - _FILTERS:
        raise ValueError("Неизвестный фильтр очереди.")
    result = {key: "" for key in _FILTERS}
    for key, value in filters.items():
        if not isinstance(value, (str, int, bool)):
            raise ValueError("Некорректный фильтр очереди.")
        result[key] = str(value).strip()
    mine = result["mine"].lower()
    if mine not in ("", "0", "1", "false", "true", "no", "yes", "off", "on"):
        raise ValueError("Некорректный фильтр «Только мои».")
    result["mine"] = mine in ("1", "true", "yes", "on")
    if not result["view"] and not principal["is_admin"]:
        result["view"] = "open"
    if result["view"] not in ("", "open", "waiting", "closed", "archive"):
        raise ValueError("Неизвестный вид очереди.")
    if result["status"] not in ("", "open", "closed", *STATUS):
        raise ValueError("Неизвестный статус обращения.")
    if result["category"] not in ("", *CATS):
        raise ValueError("Неизвестная категория обращения.")
    if len(result["q"]) > 200 or "\x00" in result["q"]:
        raise ValueError("Поиск должен быть не длиннее 200 символов.")
    if result["period"] not in ("", "all", "today", "7", "30", "90"):
        raise ValueError("Неизвестный период.")
    if result["scope"] not in ("", "waiting", "overdue"):
        raise ValueError("Неизвестный фильтр ожидания.")
    result["date_from"], result["date_to"] = ticket_filter_dates(result["date_from"], result["date_to"])
    return result


def _filter_where(principal: dict, filters: dict) -> tuple[str, tuple]:
    acl, args = _acl(principal, mine=filters["mine"])
    clauses, params = [acl], list(args)
    if filters["category"]:
        clauses.append("t.category=?")
        params.append(filters["category"])
    if filters["status"] == "open":
        clauses.append("t.status IN ('new','accepted','in_progress','ready')")
    elif filters["status"]:
        clauses.append("t.status=?")
        params.append(filters["status"])
    if filters["q"]:
        fields = ("CAST(t.ticket_id AS TEXT)", "t.student_id", "t.target_admin_id", "u.full_name",
                  "t.text_content", "t.topic")
        search = [f"instr(lower({field}),?)>0" for field in fields]
        params.extend([_PrivateText(filters["q"].lower())] * len(fields))
        number = filters["q"].lstrip("№#")
        if filters["q"].startswith(("№", "#")) and number.isascii() and number.isdecimal():
            search.append("CAST(t.ticket_id AS TEXT)=?")
            params.append(_PrivateText(str(int(number))))
        clauses.append("(" + " OR ".join(search) + ")")
    if filters["date_from"]:
        clauses.append("ticket_time_ms(t.created_at)>=?")
        params.append(_ticket_time_ms(filters["date_from"]))
    if filters["date_to"]:
        clauses.append("ticket_time_ms(t.created_at)<=?")
        params.append(_ticket_time_ms(filters["date_to"] + " 23:59:59.999"))
    if filters["period"] not in ("", "all"):
        days = 1 if filters["period"] == "today" else int(filters["period"])
        start = clock.now().date() - timedelta(days=days - 1)
        clauses.append("ticket_time_ms(t.created_at)>=?")
        params.append(_ticket_time_ms(start.isoformat()))
    return " AND ".join(clauses), tuple(params)


async def workspace_list(actor_id, *, session_hash: str = "", admin: bool = False,
                         filters=None, page: int = 1, page_size: int = 25,
                         for_export: bool = False) -> dict:
    """Одинаковая SQL-область для карточек, счётчиков, поиска и CSV; ACL до LIMIT."""
    async with _transaction() as conn:
        principal = await _principal(conn, actor_id, session_hash, admin)
        if not principal or (for_export and not _has(principal, "export")):
            return _error("denied")
        try:
            filters = _normal_filters(filters, principal)
            page = max(1, _integer(page, minimum=1))
            page_size = min(2000 if for_export else 100, _integer(page_size, minimum=1))
            if (page - 1) * page_size > 9223372036854775807:
                raise ValueError("Номер страницы слишком велик.")
        except ValueError as exc:
            return _error("invalid", str(exc))
        await _query_setup(conn)
        where, params = _filter_where(principal, filters)
        base = "WITH matched AS (" + _base_select() + "WHERE " + where + ") "
        scope = {"waiting": "is_waiting=1", "overdue": "is_overdue=1"}.get(filters["scope"], "1=1")
        args = (_cutoff(), *params)
        count_parts = {
            "open": "deleted_at='' AND status NOT IN ('completed','rejected','closed')",
            "waiting": "deleted_at='' AND is_waiting=1",
            "closed": "deleted_at='' AND status IN ('completed','rejected','closed')",
            "archive": "deleted_at<>''", "overdue": "deleted_at='' AND is_overdue=1",
            **{status: f"deleted_at='' AND status='{status}'" for status in STATUS},
        }
        counters = ", ".join(f"COALESCE(SUM({clause}),0) {name}" for name, clause in count_parts.items())
        counts = dict(await _one(conn, base + f"SELECT COUNT(*) total,{counters} FROM matched WHERE {scope}", args))
        view = {"": "deleted_at=''", "open": count_parts["open"], "waiting": count_parts["waiting"],
                "closed": count_parts["closed"], "archive": count_parts["archive"]}[filters["view"]]
        selected = f"FROM matched WHERE ({view}) AND ({scope})"
        total = (await _one(conn, base + "SELECT COUNT(*) total " + selected, args))["total"]
        rows = await _many(conn, base + "SELECT * " + selected + " ORDER BY ticket_id DESC LIMIT ? OFFSET ?",
                           (*args, page_size, (page - 1) * page_size))
        return {"result": "ok", "rows": [_row_flags(row, principal) for row in rows], "total": total,
                "counts": counts, "page": page, "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size), "filters": filters,
                "permissions": principal["permissions"], "can_export": _has(principal, "export")}


async def _ticket_people(conn, ticket: dict) -> dict:
    student = await _one(conn, "SELECT user_id,full_name,group_code FROM users WHERE user_id=?", (ticket["student_id"],))
    staff = await _one(conn, "SELECT user_id,full_name,role,position,office,role_type,ticket_category "
                            "FROM admins WHERE user_id=?", (ticket["target_admin_id"],))
    sender = await _one(conn, "SELECT sender_id,sender_role FROM ticket_messages WHERE ticket_id=? ORDER BY id LIMIT 1",
                        (ticket["ticket_id"],))
    person = student
    if sender:
        table = "users" if sender["sender_role"] == "student" else "admins"
        person = await _one(conn, f"SELECT user_id,full_name FROM {table} WHERE user_id=?", (sender["sender_id"],))
    return {**ticket, "student": dict(student or {}), "staff": dict(staff or {}),
            "sender": {**dict(person or {}), **dict(sender or {})}}


async def workspace_ticket(actor_id, ticket_id, *, session_hash: str = "", admin: bool = False) -> dict:
    """Полная карточка, без дополнительных незащищённых чтений в веб-маршруте."""
    async with _transaction() as conn:
        principal = await _principal(conn, actor_id, session_hash, admin)
        if not principal:
            return _error("denied")
        try:
            ticket_id = _integer(ticket_id, minimum=1)
        except ValueError:
            return _error("not_found")
        ticket = await _ticket_row(conn, principal, ticket_id)
        if not ticket:
            return _error("not_found")
        ticket = await _ticket_people(conn, ticket)
        thread = await _many(conn,
            "SELECT m.*,COALESCE(NULLIF(u.full_name,''),a.full_name,'') sender_name,"
            "COALESCE(u.group_code,'') group_code,COALESCE(NULLIF(a.position,''),a.role,'') position,"
            "COALESCE(a.role_type,'') role_type FROM ticket_messages m "
            "LEFT JOIN users u ON u.user_id=m.sender_id AND m.sender_role='student' "
            "LEFT JOIN admins a ON a.user_id=m.sender_id AND m.sender_role='staff' "
            "WHERE m.ticket_id=? ORDER BY m.id", (ticket_id,))
        events = await _many(conn,
            "SELECT e.*,COALESCE(NULLIF(u.full_name,''),a.full_name,'') actor_name,"
            "COALESCE(NULLIF(a.position,''),a.role,'') actor_position FROM ticket_events e "
            "LEFT JOIN users u ON u.user_id=e.actor_id LEFT JOIN admins a ON a.user_id=e.actor_id "
            "WHERE e.ticket_id=? ORDER BY e.id DESC", (ticket_id,))
        templates = []
        if _has(principal, "templates"):
            templates = await _many(conn, "SELECT id,title,text,category,used_count FROM reply_templates "
                "WHERE category IN (?, 'all') ORDER BY (category='all'),used_count DESC,id DESC", (ticket["category"],))
        recipients = []
        if "forward" in ticket["allowed_actions"]:
            recipients = await _many(conn,
                "SELECT user_id,full_name,position,office,ticket_category FROM admins "
                "WHERE role_type='staff' AND is_test=0 AND user_id<>? AND ticket_category IN (?, 'all') "
                "ORDER BY full_name,user_id", (ticket["target_admin_id"], ticket["category"]))
        files = await _many(conn, "SELECT f.id,f.ticket_id,f.message_id,f.display_name,f.size,f.created_at "
            "FROM ticket_files f JOIN ticket_messages m ON m.id=f.message_id AND m.ticket_id=f.ticket_id "
            "WHERE f.ticket_id=? ORDER BY f.id", (ticket_id,))
        for item in files:
            item["downloadable"] = _has(principal, "attachments")
        trusted_messages = {item["message_id"] for item in files}
        legacy = any(message["text"].startswith(_MARKER) and message["id"] not in trusted_messages for message in thread)
        return {"result": "ok", "ticket": ticket, "thread": thread, "events": events, "templates": templates,
                "recipients": recipients, "files": files, "allowed_actions": ticket["allowed_actions"],
                "revision": ticket["revision"], "is_own": ticket["is_own"], "archived": bool(ticket["deleted_at"]),
                "permissions": principal["permissions"], "legacy_files_restricted": legacy}


async def workspace_template_preview(actor_id, ticket_id, template_id, *, session_hash: str = "",
                                     admin: bool = False) -> dict:
    """Даёт разрешённый шаблон для чистого форматирования, но не отправляет его."""
    async with _transaction() as conn:
        principal = await _principal(conn, actor_id, session_hash, admin)
        if not principal or not _has(principal, "templates"):
            return _error("denied")
        try:
            ticket_id, template_id = _integer(ticket_id, minimum=1), _integer(template_id, minimum=1)
        except ValueError:
            return _error("not_found")
        ticket = await _ticket_row(conn, principal, ticket_id)
        if not ticket:
            return _error("not_found")
        template = await _one(conn, "SELECT id,title,text,category,used_count FROM reply_templates "
                             "WHERE id=? AND category IN (?, 'all')", (template_id, ticket["category"]))
        if not template:
            return _error("not_found")
        return {"result": "ok", "ticket": await _ticket_people(conn, ticket),
                "template": dict(template), "revision": ticket["revision"]}


async def _event(conn, ticket_id: int, actor_id: str, event: str, detail: str, stamp: str) -> int:
    cursor = await conn.execute("INSERT INTO ticket_events(ticket_id,actor_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                                (ticket_id, actor_id, event, _PrivateText(detail), stamp))
    return cursor.lastrowid


def _valid_text(value, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Текст должен быть строкой.")
    value = value.strip()
    if len(value) > maximum or "\x00" in value or (required and not value):
        raise ValueError(f"Текст должен содержать {'от 1 до' if required else 'не более'} {maximum} символов.")
    return value


async def workspace_apply(actor_id, ticket_id, action: str, *, session_hash: str = "", admin: bool = False,
                          expected_revision, operation_key, text: str = "", status: str = "", target_id: str = "",
                          template_id: int = 0, answerer_id: str = "", as_staff: str = "") -> dict:
    """Проверка сессии, ACL, ревизии, повторов и все записи — один BEGIN IMMEDIATE."""
    async with _transaction(write=True) as conn:
        principal = await _principal(conn, actor_id, session_hash, admin)
        if not principal:
            return _error("denied")
        try:
            ticket_id = _integer(ticket_id, minimum=1)
        except ValueError:
            return _error("not_found")
        ticket = await _ticket_row(conn, principal, ticket_id)
        if not ticket:
            return _error("not_found")
        if not isinstance(action, str) or action not in _ACTIONS:
            return _error("invalid", "Неизвестное действие.")
        if not _may_change(principal, ticket, action):
            return _error("denied")
        if not principal["is_admin"] and (answerer_id or as_staff):
            return _error("denied")
        try:
            revision = _integer(expected_revision)
            template_id = _integer(template_id)
            operation_key = _valid_text(operation_key, 200, required=True)
            target_id = _valid_text(target_id, 100)
            answerer_id = _valid_text(answerer_id or as_staff, 100)
            text = _valid_text(text, 4000 if action in ("reply", "reply_close") else 200,
                               required=action in ("reply", "reply_close", "note"))
            if not isinstance(status, str) or (action == "status" and status not in STATUS):
                raise ValueError("Неизвестный статус обращения.")
            if action != "status" and status:
                raise ValueError("Статус не относится к этому действию.")
            if action != "forward" and target_id:
                raise ValueError("Получатель не относится к этому действию.")
            if action == "status" and text:
                raise ValueError("Текст не относится к смене статуса.")
            if template_id and action not in ("reply", "reply_close"):
                raise ValueError("Шаблон можно применить только к ответу.")
            if answerer_id and action not in ("reply", "reply_close"):
                raise ValueError("Другой автор допустим только для ответа сис-админа.")
        except (ValueError, TypeError) as exc:
            return _error("invalid", str(exc) if isinstance(exc, ValueError) else "Некорректные данные операции.")
        if template_id and not _has(principal, "templates"):
            return _error("denied")
        fingerprint = hashlib.sha256(json.dumps(
            [ticket_id, action, revision, text, status, target_id, template_id, answerer_id],
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        key = _PrivateText(hashlib.sha256(operation_key.encode("utf-8")).hexdigest())
        audience = "admin" if principal["is_admin"] else "staff"
        previous = await _one(conn, "SELECT * FROM workspace_operations WHERE actor_id=? AND audience=? AND operation_key=?",
                              (principal["user_id"], audience, key))
        if previous:
            if previous["request_hash"] != fingerprint:
                return _error("conflict", "Ключ операции уже использован для другого запроса.")
            return {"result": "replay", "ticket_id": previous["ticket_id"], "revision": previous["revision"],
                    "message_id": previous["message_id"], "event_ids": json.loads(previous["event_ids"]),
                    "notifications": []}
        if ticket["workspace_revision"] != revision:
            return {**_error("conflict"), "revision": ticket["workspace_revision"]}
        if ticket["deleted_at"]:
            return _error("conflict", "Обращение в архиве. Сначала восстановите его.")
        if action == "reply_close" and ticket["status"] in _CLOSED:
            return _error("conflict", "Обращение уже закрыто.")
        if action == "status" and ticket["status"] == status:
            return _error("conflict", "Этот статус уже установлен.")
        if template_id:
            template = await _one(conn, "SELECT id FROM reply_templates WHERE id=? AND category IN (?, 'all')",
                                 (template_id, ticket["category"]))
            if not template:
                return _error("invalid", "Шаблон недоступен для этого обращения.")
        target = None
        if action == "forward":
            target = await _one(conn, "SELECT user_id,full_name FROM admins WHERE user_id=? AND role_type='staff' "
                               "AND is_test=0 AND ticket_category IN (?, 'all')", (target_id, ticket["category"]))
            if not target or target_id == ticket["target_admin_id"]:
                return _error("invalid", "Выберите другого действующего сотрудника нужного раздела.")
        author = principal
        if answerer_id:
            author = await _one(conn, "SELECT user_id,full_name,position FROM admins WHERE user_id=? "
                               "AND role_type='staff' AND is_test=0", (answerer_id,))
            if not author:
                return _error("invalid", "Автор ответа не является действующим сотрудником.")
        stamp = clock.stamp()
        new_status = status if action == "status" else ticket["status"]
        if action == "reply_close":
            new_status = "completed"
        elif action == "reply" and ticket["status"] in ACCEPT_ON_REPLY:
            new_status = "accepted"
        new_target = target_id if target else ticket["target_admin_id"]
        acl, acl_params = _acl(principal)
        changed = await conn.execute(
            "UPDATE tickets AS t SET status=?,target_admin_id=?,updated_at=?,workspace_revision=workspace_revision+1 "
            "WHERE ticket_id=? AND workspace_revision=? AND deleted_at='' AND " + acl,
            (new_status, new_target, stamp, ticket_id, revision, *acl_params))
        if changed.rowcount != 1:
            return _error("conflict")
        message_id, event_ids = 0, []
        if action in ("reply", "reply_close"):
            message = await conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) "
                "VALUES(?,?,'staff',?,?)", (ticket_id, author["user_id"], _PrivateText(text), stamp))
            message_id = message.lastrowid
            event_ids.append(await _event(conn, ticket_id, author["user_id"], "message_staff", "", stamp))
            if template_id:
                await conn.execute("UPDATE reply_templates SET used_count=used_count+1 WHERE id=?", (template_id,))
        if new_status != ticket["status"]:
            event_ids.append(await _event(conn, ticket_id, author["user_id"], "status", new_status, stamp))
        if action == "note":
            event_ids.append(await _event(conn, ticket_id, principal["user_id"], "note", text, stamp))
        if action == "forward":
            event_ids.append(await _event(conn, ticket_id, principal["user_id"], "forward", target_id, stamp))
            if text:
                event_ids.append(await _event(conn, ticket_id, principal["user_id"], "note", text, stamp))
        fresh_revision = (await _one(conn, "SELECT workspace_revision FROM tickets WHERE ticket_id=?", (ticket_id,)))["workspace_revision"]
        await conn.execute(
            "INSERT INTO workspace_operations(actor_id,audience,operation_key,request_hash,ticket_id,action,revision,"
            "message_id,event_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (principal["user_id"], audience, key, _PrivateText(fingerprint), ticket_id, action, fresh_revision,
             message_id, json.dumps(event_ids), stamp))
        notifications = []
        if action != "note":
            notify_text = text
            if action == "forward":
                notify_text = (text + "\n\n" if text else "") + ticket["text_content"][:700]
            notifications.append({"kind": action, "recipient_id": target_id if target else ticket["student_id"],
                "ticket_id": ticket_id, "message_id": message_id, "event_id": event_ids[0],
                "event_ids": event_ids, "text": _PrivateText(notify_text), "status": new_status,
                "actor_id": author["user_id"], "actor_name": author["full_name"], "actor_position": author["position"]})
        return {"result": "ok", "ticket_id": ticket_id, "revision": fresh_revision, "message_id": message_id,
                "event_ids": event_ids, "notifications": notifications, "status": new_status, "target_id": new_target}


def _file_path(storage_name: str) -> str:
    if not isinstance(storage_name, str) or not _STORAGE_NAME.fullmatch(storage_name) or not storage_name.lower().endswith(_FILE_SUFFIXES):
        return ""
    folder = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(config.DATABASE_PATH)), "attachments"))
    path = os.path.join(folder, storage_name)
    if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) != folder:
        return ""
    return path


async def workspace_attachment(actor_id, ticket_id, file_id, *, session_hash: str = "", admin: bool = False) -> dict:
    """Доверенная связь файла, действующая сессия, право вложений и текущая ACL."""
    async with _transaction() as conn:
        principal = await _principal(conn, actor_id, session_hash, admin)
        if not principal or not _has(principal, "attachments"):
            return _error("denied")
        try:
            ticket_id, file_id = _integer(ticket_id, minimum=1), _integer(file_id, minimum=1)
        except ValueError:
            return _error("not_found")
        acl, args = _acl(principal)
        row = await _one(conn, "SELECT f.* FROM ticket_files f JOIN tickets t ON t.ticket_id=f.ticket_id "
            "JOIN ticket_messages m ON m.id=f.message_id AND m.ticket_id=f.ticket_id "
            "WHERE t.ticket_id=? AND f.id=? AND " + acl, (ticket_id, file_id, *args))
        if not row:
            return _error("not_found")
        path = _file_path(row["storage_name"])
        if not path or not os.path.isfile(path):
            return _error("not_found")
        return {"result": "ok", "file": dict(row), "path": path}


async def _student_attachment_ticket(conn, actor_id, ticket_id: int):
    # Только существующий MAX-сценарий исходного автора. Это не веб-auth обход.
    ticket = await _one(conn, "SELECT * FROM tickets WHERE ticket_id=? AND student_id=? COLLATE BINARY AND deleted_at=''",
                        (ticket_id, str(actor_id)))
    return dict(ticket) if ticket else None


async def workspace_can_attach(actor_id, ticket_id) -> dict:
    """Предварительная проверка MAX до сети и записи файла; после скачивания повторяется."""
    async with _transaction() as conn:
        try:
            ticket_id = _integer(ticket_id, minimum=1)
        except ValueError:
            return _error("not_found")
        ticket = await _student_attachment_ticket(conn, actor_id, ticket_id)
        if not ticket:
            return _error("not_found")
        count = (await _one(conn, "SELECT COUNT(*) n FROM ticket_files WHERE ticket_id=?", (ticket_id,)))["n"]
        if count >= WORKSPACE_FILE_LIMIT:
            return _error("conflict", f"К обращению уже прикреплено {WORKSPACE_FILE_LIMIT} файла.")
        return {"result": "ok", "ticket": ticket}


async def workspace_attach_file(actor_id, ticket_id, *, storage_name: str, display_name: str, size: int) -> dict:
    """MAX: ассоциация, сообщение и событие атомарны; текстовый маркер не источник доверия."""
    async with _transaction(write=True) as conn:
        try:
            ticket_id = _integer(ticket_id, minimum=1)
        except ValueError:
            return _error("not_found")
        ticket = await _student_attachment_ticket(conn, actor_id, ticket_id)
        if not ticket:
            return _error("not_found")
        path = _file_path(storage_name)
        try:
            size = _integer(size, minimum=1)
            display_name = _valid_text(display_name, 200, required=True)
        except ValueError as exc:
            return _error("invalid", str(exc))
        if (not path or not os.path.isfile(path) or os.path.getsize(path) != size or size > WORKSPACE_FILE_BYTES
                or any(char in display_name for char in ("/", "\\", "\n", "\r"))):
            return _error("invalid", "Файл или его имя недопустимы.")
        count = (await _one(conn, "SELECT COUNT(*) n FROM ticket_files WHERE ticket_id=?", (ticket_id,)))["n"]
        if count >= WORKSPACE_FILE_LIMIT:
            return _error("conflict", f"К обращению уже прикреплено {WORKSPACE_FILE_LIMIT} файла.")
        if await _one(conn, "SELECT id FROM ticket_files WHERE storage_name=?", (_PrivateText(storage_name),)):
            return _error("conflict", "Этот файл уже прикреплён.")
        stamp = clock.stamp()
        message = await conn.execute("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text,created_at) "
            "VALUES(?,?,'student',?,?)", (ticket_id, str(actor_id), _PrivateText(f"{_MARKER}{storage_name} ({size // 1024} КБ)"), stamp))
        message_id = message.lastrowid
        cursor = await conn.execute("INSERT INTO ticket_files(ticket_id,message_id,storage_name,display_name,size,created_at) "
            "VALUES(?,?,?,?,?,?)", (ticket_id, message_id, _PrivateText(storage_name), _PrivateText(display_name), size, stamp))
        event_id = await _event(conn, ticket_id, str(actor_id), "message_student", "", stamp)
        await conn.execute("UPDATE tickets SET updated_at=? WHERE ticket_id=?", (stamp, ticket_id))
        return {"result": "ok", "file_id": cursor.lastrowid, "message_id": message_id, "event_ids": [event_id],
                "ticket": ticket, "display_name": display_name}
