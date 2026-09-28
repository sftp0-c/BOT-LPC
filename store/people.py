"""Люди: регистрация, реестр контактов, карточка человека и его удаление.

Здесь же согласие на обработку данных. Всё, что отвечает на вопрос «кто
этот человек и что о нём известно», собрано в одном месте, чтобы остальные
разделы брали готовый ответ, а не собирали его заново."""

import clock
import database as db
from utils import OPEN_STATUSES, as_str, is_sysadmin_role

from .common import _CONTACT_SELECT, _contact_filter, _row_dict, canonical_group


# ── регистрация ──────────────────────────────────────────────────────────────
async def get_user(user_id: str):
    return await db.one("SELECT * FROM users WHERE user_id=?", (user_id,))


async def is_registered(user_id: str) -> bool:
    return await db.one("SELECT 1 FROM users WHERE user_id=?", (user_id,)) is not None


async def upsert_user(user_id: str, full_name: str, group_code: str) -> None:
    # Код группы пишется в каноническом виде («24-23П»), а не как его набрали
    # («24-23 (П)»). Так его ищут расписания, справочник и все остальные
    # выборки проекта. Раньше здесь стоял norm_group, который скобки сохранял,
    # и группа, записанная со скобками, не находилась никогда: фильтр искал
    # каноническую форму, а в базе лежала другая.
    code = canonical_group(as_str(group_code).strip())
    await db.run(
        "INSERT INTO users(user_id, full_name, group_code, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, group_code=excluded.group_code",
        (user_id, full_name, code, clock.stamp()),
    )
    if code:
        # новая группа сразу попадает в справочник: иначе её не будет в списке панели
        # и переименовать её до перезапуска бота (когда сработает дозаполнение) нельзя
        await db.run("INSERT OR IGNORE INTO groups(group_code, created_at) VALUES(?,?)",
                     (code, clock.stamp()))


async def set_user_name(user_id: str, full_name: str) -> None:
    await db.run("UPDATE users SET full_name=? WHERE user_id=?", (full_name, user_id))


async def set_user_group(user_id: str, group_code: str) -> None:
    await db.run("UPDATE users SET group_code=? WHERE user_id=?",
                 (canonical_group(as_str(group_code).strip()), user_id))


# ── реестр пользователей: все, кто писал боту ─────────────────────────────────
CONTACT_KINDS = ("student", "staff", "request", "guest")


CONTACT_KIND_LABELS = {"": "👥 Все", "student": "🎓 Студенты", "staff": "🏫 Сотрудники",
                       "request": "📥 Заявки", "guest": "👤 Без регистрации"}


KIND_TITLES = {"student": "🎓 студент", "staff": "🏫 сотрудник", "request": "📥 заявка", "guest": "👤 без регистрации"}


async def touch_contact(user_id: str, username: str = "", display_name: str = "", last_text: str = "") -> None:
    """Запоминает, что человек писал боту: ник, имя из профиля MAX, время и текст.

    Вызывается на каждом событии, поэтому запрос один и идемпотентный: ник и имя
    обновляются только когда пришли непустыми (у MAX username бывает null).
    """
    if not user_id:
        return
    stamp = clock.stamp()
    await db.run(
        "INSERT INTO contacts(user_id, username, display_name, messages, last_text, first_seen, last_seen) "
        "VALUES(?,?,?,1,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE contacts.username END, "
        "display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE contacts.display_name END, "
        "messages=contacts.messages + CASE WHEN excluded.last_text<>'' THEN 1 ELSE 0 END, "
        "last_text=CASE WHEN excluded.last_text<>'' THEN excluded.last_text ELSE contacts.last_text END, "
        "last_seen=excluded.last_seen",
        (str(user_id), as_str(username).strip()[:64], as_str(display_name).strip()[:100],
         as_str(last_text).strip()[:200], stamp, stamp),
    )


def contact_kind(row) -> str:
    """Как человек выглядит в реестре: сотрудник, студент, заявка или гость."""
    if row["role_type"]:
        return "staff"
    if row["fio"]:
        return "student"
    return "request" if row["request_status"] else "guest"


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
                       f"LEFT JOIN admins a ON a.user_id=c.user_id {where}", params)
    return row["n"]


async def people_overview() -> dict:
    """Сколько всего людей знает бот и как они распределены."""
    row = await db.one(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END) students, "
        "SUM(CASE WHEN a.user_id IS NOT NULL THEN 1 ELSE 0 END) staff, "
        "SUM(CASE WHEN u.user_id IS NULL AND a.user_id IS NULL THEN 1 ELSE 0 END) guests, "
        "SUM(CASE WHEN c.last_seen >= ? THEN 1 ELSE 0 END) active_30 "
        "FROM contacts c "
        "LEFT JOIN users u ON u.user_id=c.user_id "
        "LEFT JOIN admins a ON a.user_id=c.user_id",
        (clock.stamp_at(-30 * 24 * 60),),
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


def contact_full_name(card) -> str:
    """ФИО для карточки человека: ФИО из профиля, иначе подпись из контактов."""
    if not card:
        return ""
    return as_str(card.get("fio")) or as_str(card.get("staff_name")) or as_str(card.get("display_name"))


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


# ── согласие на обработку данных ─────────────────────────────────────────────────
async def give_consent(user_id: str, version: str) -> str:
    """Фиксирует согласие на обработку данных: дату и редакцию текста.

    Хранится рядом с пользователем, а не «в настройках»: потом по записи можно
    доказать, что согласие было получено и на каких условиях.
    """
    await db.run("UPDATE users SET consent_at=?, consent_version=? WHERE user_id=?",
                 (clock.stamp(), as_str(version), as_str(user_id)))
    return clock.today().strftime("%d.%m.%Y")


async def consent_of(user_id: str) -> dict:
    """Когда и на каких условиях человек дал согласие."""
    row = await db.one("SELECT consent_at, consent_version FROM users WHERE user_id=?", (as_str(user_id),))
    return {"at": as_str(row["consent_at"]) if row else "",
            "version": as_str(row["consent_version"]) if row else ""}


async def users_without_consent(limit: int = 100) -> list:
    """Зарегистрировались, но согласия не дали - список для сис-админа."""
    rows = await db.many(
        "SELECT user_id, full_name, group_code, created_at FROM users "
        "WHERE consent_at='' ORDER BY created_at LIMIT ?", (int(limit),))
    return [dict(row) for row in rows]
