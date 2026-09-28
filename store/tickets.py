"""Обращения: очередь, переписка, статусы, архив и массовые действия.

Очередь сотрудника и списки панели читаются одними и теми же запросами,
поэтому раздел отвечает и за то, что видит бот, и за то, что видит панель."""

from typing import Optional

import clock
import database as db
from utils import as_str, is_sysadmin_role, to_int

from .common import _row_dict
from .staff import get_admin


# ── обращения ─────────────────────────────────────────────────────────────────
async def get_ticket(ticket_id: int, include_archived: bool = False):
    where = "WHERE ticket_id=?" if include_archived else "WHERE ticket_id=? AND deleted_at=''"
    ticket = await db.one(f"SELECT * FROM tickets {where}", (ticket_id,))
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


async def admin_tickets(admin_id: Optional[str], limit: int = 20,
                        archived: bool = False) -> list:
    """Обращения сотрудника; None — все обращения (для сис-админа). Сначала открытые.

    По умолчанию отдаёт живые обращения: убранные в архив видны только
    отдельным запросом, иначе они засоряли бы очередь.
    """
    if admin_id:
        where = "WHERE t.deleted_at<>''" if archived else "WHERE t.target_admin_id=? AND t.deleted_at=''"
        params = (admin_id,)
    else:
        where = "WHERE t.deleted_at<>''" if archived else "WHERE t.deleted_at=''"
        params = ()
    # Студент и сотрудник джойнятся сразу: и боту, и панели нужны ФИО и группа в
    # каждой строке очереди, а запрос на строку здесь обернулся бы в N+1.
    return await db.many(
        "SELECT t.*, "
        "COALESCE(NULLIF(u.full_name, ''), '') student_name, "
        "COALESCE(u.group_code, '') student_group, "
        "COALESCE(a.full_name, '') staff_name, "
        "COALESCE(NULLIF(a.position, ''), a.role, '') staff_position "
        "FROM tickets t "
        "LEFT JOIN users u ON u.user_id = t.student_id "
        "LEFT JOIN admins a ON a.user_id = t.target_admin_id "
        f"{where} "
        "ORDER BY (t.status IN ('ready','completed','rejected')), "
        "CASE WHEN t.status='ready' THEN 0 ELSE 1 END, "
        "t.ticket_id DESC LIMIT ?",
        params + (limit,),
    )


async def ticket_thread(ticket_id: int, limit: int = 50) -> list:
    """Переписка с именами, должностями и временем — от свежих к старым."""
    return await db.many(
        "SELECT m.id, m.sender_id, m.sender_role, m.text, m.created_at, "
        "COALESCE(NULLIF(u.full_name, ''), a.full_name, '') sender_name, "
        "COALESCE(u.group_code, '') group_code, "
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
        "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) VALUES(?,?,?,?,?)",
        (int(ticket_id), as_str(actor_id).strip(), event, as_str(detail).strip()[:200], clock.stamp()),
    )


async def add_internal_note(ticket_id: int, actor_id: str, text: str) -> int:
    """Внутренняя заметка по обращению: её видит только сотрудник, студент - нет.

    Хранится обычным событием, поэтому отдельная таблица не нужна: в истории
    заметка видна как строка, но не отправляется в MAX.
    """
    return await db.run(
        "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) VALUES(?,?,'note',?,?)",
        (int(ticket_id), as_str(actor_id).strip(), as_str(text).strip()[:200], clock.stamp()),
    )


async def forward_ticket(ticket_id: int, new_admin_id: str, actor_id: str,
                         comment: str = "") -> tuple[bool, str]:
    """Передаёт обращение другому сотруднику. Возвращает (получилось, сообщение).

    Студенту отправитель не меняется - он по-прежнему автор, но его сотрудник
    теперь другой. Статус сохраняется: если обращение уже брали в работу,
    новый сотрудник видит, что оно в работе.
    """
    admin = await get_admin(new_admin_id)
    if not admin:
        return False, "Сотрудник не найден"
    if is_sysadmin_role(as_str(admin["role_type"])):
        return False, "Сис-админ не принимает обращения - выдайте ему роль сотрудника"
    changed = await db.run_count(   # run_count возвращает число строк, run - lastrowid
        "UPDATE tickets SET target_admin_id=?, updated_at=? WHERE ticket_id=?",
        (as_str(new_admin_id), clock.stamp(), int(ticket_id)),
    )
    if not changed:
        return False, f"Обращение №{ticket_id} не найдено"
    detail = f"{new_admin_id}" + (f": {comment}" if comment else "")
    await log_ticket_event(ticket_id, actor_id, "forward", detail)
    return True, f"Обращение №{ticket_id} теперь у {admin['full_name']}"


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
    """{status: количество}; admin_id=None — по всем обращениям. Архив не считается."""
    if admin_id:
        where, params = "WHERE target_admin_id=? AND deleted_at=''", (admin_id,)
    else:
        where, params = "WHERE deleted_at=''", ()
    rows = await db.many(f"SELECT status, COUNT(*) n FROM tickets {where} GROUP BY status", params)
    return {r["status"]: r["n"] for r in rows}


# ── обращения: создание и смена статуса ───────────────────────────────────────
async def create_ticket(student_id: str, admin_id: str, category: str, text: str, topic: str = "") -> int:
    """Создаёт обращение и его первое сообщение в одной транзакции."""
    async with db._conn() as c:
        stamp = clock.stamp()
        cur = await c.execute(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content, topic, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (student_id, admin_id, category, text, as_str(topic).strip(), stamp, stamp),
        )
        ticket_id = cur.lastrowid
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text, created_at) "
            "VALUES(?,?,?,?,?)",
            (ticket_id, student_id, "student", text, stamp),
        )
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) "
            "VALUES(?,?,'created',?,?)",
            (ticket_id, student_id, as_str(topic).strip()[:200] or category, stamp),
        )
        await c.commit()
    return ticket_id


async def add_ticket_message(ticket_id: int, sender_id: str, role: str, text: str, new_status: str | None = None) -> None:
    async with db._conn() as c:
        stamp = clock.stamp()
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text, created_at) VALUES(?,?,?,?,?)",
            (ticket_id, sender_id, role, text, stamp),
        )
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) VALUES(?,?,?,?,?)",
            (ticket_id, sender_id, "message_student" if role == "student" else "message_staff",
             as_str(text)[:200], stamp),
        )
        if new_status:
            await c.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE ticket_id=?", (new_status, stamp, ticket_id)
            )
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) VALUES(?,?,'status',?,?)",
                (ticket_id, sender_id, new_status, stamp),
            )
        else:
            await c.execute("UPDATE tickets SET updated_at=? WHERE ticket_id=?", (stamp, ticket_id))
        await c.commit()


async def set_ticket_status(ticket_id: int, status: str, actor_id: str = "") -> None:
    async with db._conn() as c:
        stamp = clock.stamp()
        await c.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE ticket_id=?", (status, stamp, ticket_id)
        )
        if actor_id:
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) "
                "VALUES(?,?,'status',?,?)",
                (ticket_id, as_str(actor_id), status, stamp),
            )
        await c.commit()


async def transition_ticket_status(ticket_id: int, current_status: str, status: str, actor_id: str = "") -> bool:
    async with db._conn() as c:
        stamp = clock.stamp()
        cur = await c.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE ticket_id=? AND status=?",
            (status, stamp, ticket_id, current_status),
        )
        if cur.rowcount > 0 and actor_id:
            await c.execute(
                "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) "
                "VALUES(?,?,'status',?,?)",
                (ticket_id, as_str(actor_id), status, stamp),
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
        stamp = clock.stamp()
        await c.execute(
            "UPDATE tickets SET status='ready', ready_until=?, pickup_place=?, doc_url=?, "
            "updated_at=? WHERE ticket_id=?",
            (as_str(ready_until).strip(), as_str(pickup_place).strip(), as_str(doc_url).strip(),
             stamp, ticket_id),
        )
        await c.execute(
            "INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) VALUES(?,?,'ready',?,?)",
            (ticket_id, as_str(actor_id), f"{as_str(ready_until).strip()} · {as_str(pickup_place).strip()}",
             stamp),
        )
        await c.commit()


# ── архив и правка обращений ─────────────────────────────────────────────────────
async def archive_ticket(ticket_id: int, actor_id: str = "") -> tuple[bool, str]:
    """Мягкое удаление: обращение уходит в архив, переписка и история остаются.

    Возвращается одним действием (restore_ticket), поэтому спорный вопрос можно
    вернуть в работу, а не искать в резервной копии базы.
    """
    ticket_id = int(ticket_id)
    row = await db.one("SELECT deleted_at FROM tickets WHERE ticket_id=?", (ticket_id,))
    if not row:
        return False, f"Обращение №{ticket_id} не найдено"
    if as_str(row["deleted_at"]):
        return False, f"Обращение №{ticket_id} уже в архиве"
    stamp = clock.stamp()
    await db.run("UPDATE tickets SET deleted_at=?, deleted_by=?, updated_at=? WHERE ticket_id=?",
                 (stamp, as_str(actor_id), stamp, ticket_id))
    await log_ticket_event(ticket_id, as_str(actor_id) or "сис-админ", "archived", "")
    return True, f"Обращение №{ticket_id} в архиве"


async def restore_ticket(ticket_id: int, actor_id: str = "") -> tuple[bool, str]:
    """Возвращает обращение из архива в работу."""
    ticket_id = int(ticket_id)
    row = await db.one("SELECT deleted_at FROM tickets WHERE ticket_id=?", (ticket_id,))
    if not row:
        return False, f"Обращение №{ticket_id} не найдено"
    if not as_str(row["deleted_at"]):
        return False, f"Обращение №{ticket_id} и так в работе"
    await db.run("UPDATE tickets SET deleted_at='', deleted_by='', "
                 "updated_at=? WHERE ticket_id=?", (clock.stamp(), ticket_id))
    await log_ticket_event(ticket_id, as_str(actor_id) or "сис-админ", "restored", "")
    return True, f"Обращение №{ticket_id} снова в работе"


async def archive_count() -> int:
    row = await db.one("SELECT COUNT(*) n FROM tickets WHERE deleted_at<>''")
    return int(row["n"]) if row else 0


async def update_ticket(ticket_id: int, actor_id: str = "", **fields) -> tuple[bool, str]:
    """Правка полей обращения из панели: каждое изменение попадает в историю.

    Пустое значение поле не затирает: такие поля просто не трогаются, иначе один
    невнимательный клик стирал бы текст обращения.
    """
    ticket_id = int(ticket_id)
    row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))
    if not row:
        return False, f"Обращение №{ticket_id} не найдено"
    if as_str(row["deleted_at"]):
        return False, f"Обращение №{ticket_id} в архиве - сначала восстановите"
    columns = {
        "text_content": "текст", "category": "категория", "topic": "тема",
        "target_admin_id": "ответственный", "status": "статус",
        "pickup_place": "кабинет выдачи", "ready_until": "срок готовности",
        "doc_url": "документ",
    }
    changes = []
    for field, label in columns.items():
        if field not in fields or fields[field] is None:
            continue
        value = as_str(fields[field]).strip()
        if not value or value == as_str(row[field]):
            continue
        changes.append(f"{label}: {as_str(row[field]) or '—'} → {value}")
        await db.run(f"UPDATE tickets SET {field}=?, updated_at=? WHERE ticket_id=?",
                     (value, clock.stamp(), ticket_id))
    if not changes:
        return True, "Изменений не было"
    detail = "; ".join(changes)[:300]
    event = "status" if len(changes) == 1 and changes[0].startswith("статус") else "edited"
    await log_ticket_event(ticket_id, as_str(actor_id) or "сис-админ", event, detail)
    return True, f"Обращение №{ticket_id} сохранено: {detail}"


async def bulk_update(ticket_ids: list, action: str, value: str = "",
                      actor_id: str = "") -> tuple[int, str]:
    """Массовые действия: назначить, сменить статус, поставить кабинет, убрать в архив."""
    done = 0
    for raw in ticket_ids or []:
        try:
            ticket_id = int(raw)
        except (TypeError, ValueError):
            continue
        if action == "assign":
            ok, _ = await update_ticket(ticket_id, actor_id, target_admin_id=as_str(value))
        elif action == "status":
            ok, _ = await update_ticket(ticket_id, actor_id, status=as_str(value))
        elif action == "pickup":
            ok, _ = await update_ticket(ticket_id, actor_id, pickup_place=as_str(value))
        elif action == "archive":
            ok, _ = await archive_ticket(ticket_id, actor_id)
        else:
            return done, f"Неизвестное действие: {action}"
        done += int(bool(ok))
    labels = {"assign": "назначено", "status": "статус изменён",
              "pickup": "кабинет обновлён", "archive": "в архиве"}
    return done, f"Обращений обработано: {done} ({labels.get(action, action)})"


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
