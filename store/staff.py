"""Сотрудники: карточки, роли, отпуска, заместители, права на чужие
обращения и список занятых ФИО для приглашений по ссылке.

Права сис-админа (выдать, снять, .env) живут в store.sysadmin, а правка
полей карточки - здесь: это всё ещё про сотрудника, а не про доступ в панель."""

import clock
import database as db
from utils import as_str, norm_position, parse_max_ids, parse_nicks

from .common import (
    STAFF_ROLES_SQL, _CONTACT_SELECT, _contact_filter, _parse_day, _row_dict, _row_value,
)
from .people import contact_full_name, user_card


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
        # должность сводится к названию из справочника: иначе один и тот же отдел
        # в базе лежал бы как «ПК» и «Приёмная комиссия», и группировка по
        # должности разъезжалась бы на две подменю
        value = (norm_position(raw) if name == "position" else as_str(raw).strip())[:100]
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


async def staff_by_role(role: str) -> list:
    """Сотрудники с указанной должностью (ролем): директор, замы.

    Пусто, если должность ещё не назначена - тогда подменю обратной связи
    честно скажет об этом, а не покажет пустые кнопки.
    """
    return await db.many(
        "SELECT user_id, full_name, role, position, ticket_category FROM admins "
        "WHERE role=? AND role_type NOT IN ('sysadmin','owner','superadmin') "
        "ORDER BY full_name",
        (as_str(role),),
    )



# ── отпуска и заместители ───────────────────────────────────────────────────────
async def set_vacation(user_id: str, until: str = "", note: str = "") -> str:
    """Отметить отпуск сотрудника. Пустая дата - сотрудник на месте.

    Возвращает дату, на которую отпуск записан, чтобы вызывающий показал
    её человеку текстом, а не молча поменял данные.
    """
    day = _parse_day(until)
    if as_str(until).strip() and not day:
        return ""
    value = day.isoformat() if day else ""
    await db.run("UPDATE admins SET vacation_until=? WHERE user_id=?",
                 (value, as_str(user_id)))
    return value


async def on_vacation(user_id: str) -> bool:
    """Сотрудник в отпуске прямо сейчас."""
    row = await db.one("SELECT vacation_until FROM admins WHERE user_id=?", (as_str(user_id),))
    if not row or not as_str(row["vacation_until"]):
        return False
    day = _parse_day(row["vacation_until"])
    return bool(day) and clock.today() <= day         # отпуск начался или ещё не кончился


async def staff_on_vacation() -> list[dict]:
    """Кто сейчас в отпуске: для панели и подсказок."""
    rows = await db.many(
        "SELECT user_id, full_name, role, position, ticket_category, vacation_until "
        "FROM admins WHERE COALESCE(vacation_until, '')<>'' ORDER BY vacation_until")
    return [dict(row) for row in rows if await on_vacation(row["user_id"])]


def replacement_rule(staff, everyone, away) -> dict | None:
    """Кто принимает обращения отсутствующего сотрудника - без запросов.

    Правило одно и то же для поштучного и массового поиска: сначала человек с
    той же должностью, потом сосед по разделу обращений, минуя отпускников.
    Если таких нет - None: лучше честно сказать «заместитель не назначен»,
    чем отправлять обращение не тому.

    Работает по уже загруженным строкам, поэтому страница сотрудников делает
    ноль запросов вместо одного на каждого.
    """
    if not staff:
        return None
    role = as_str(_row_value(staff, "role", ""))
    category = as_str(_row_value(staff, "ticket_category", ""))
    staff_id = as_str(_row_value(staff, "user_id", ""))
    for field, value in (("role", role), ("ticket_category", category)):
        if not value:
            continue
        for row in everyone:
            if as_str(_row_value(row, "user_id", "")) == staff_id:
                continue
            if as_str(_row_value(row, "user_id", "")) in away:
                continue
            if as_str(_row_value(row, field, "")) == value:
                return row
    return None


async def vacation_replacement(staff) -> dict | None:
    """Кто принимает обращения отсутствующего сотрудника.

    Сначала - человек с той же должностью, потом - сосед по категории
    обращений. Если таких нет, возвращаем None: лучше честно сказать
    «заместитель не назначен», чем отправлять обращение не тому.
    """
    if not staff:
        return None
    role = as_str(_row_value(staff, "role", ""))
    category = as_str(_row_value(staff, "ticket_category", ""))
    for sql, param in (("SELECT * FROM admins WHERE role=? AND user_id<>? "
                        "AND role_type NOT IN ('sysadmin','owner','superadmin') "
                        "ORDER BY full_name", role),
                       ("SELECT * FROM admins WHERE ticket_category=? AND user_id<>? "
                        "AND role_type NOT IN ('sysadmin','owner','superadmin') "
                        "ORDER BY full_name", category)):
        if not param:
            continue
        rows = await db.many(sql, (param, as_str(_row_value(staff, "user_id", ""))))
        for row in rows:
            if not await on_vacation(row["user_id"]):
                return dict(row)
    return None


async def vacation_note(staff) -> str:
    """Человеческое объяснение вместо тишины: кто в отпуске и когда вернётся."""
    if not staff or not await on_vacation(_row_value(staff, "user_id", "")):
        return ""
    until = as_str(_row_value(staff, "vacation_until", ""))
    day = _parse_day(until)
    tail = f" до {day:%d.%m.%Y}" if day else ""
    replacement = await vacation_replacement(staff)
    if replacement:
        return (f"{as_str(_row_value(staff, 'full_name', 'Сотрудник'))} сейчас в отпуске{tail}. "
                f"Обращение получит {as_str(replacement['full_name'])}.")
    return (f"{as_str(_row_value(staff, 'full_name', 'Сотрудник'))} сейчас в отпуске{tail}. "
            f"Заместитель не назначен - обращение подождёт до его возвращения.")


async def vacations_bulk() -> dict[str, dict]:
    """Отпуска, которые ещё не кончились: {user_id: {...}}.

    Заменяет два запроса на каждого сотрудника при открытии списка. Закончившиеся
    отпуска отбрасываем: в панели это «кто сейчас в отпуске», а не архив.
    """
    today = clock.today()
    rows = await db.many("SELECT user_id, full_name, role, position, vacation_until "
                         "FROM admins WHERE COALESCE(vacation_until, '')<>''")
    current = {}
    for row in rows:
        until = _parse_day(row["vacation_until"])
        if until and today <= until:
            current[as_str(row["user_id"])] = {key: row[key] for key in row.keys()}
    return current


# ── подбор и правка сотрудников ───────────────────────────────────────────────
async def staff_for_category(category: str, limit: int = 25) -> list:
    return await db.many(
        "SELECT user_id, full_name, role, position, department, office, is_test "
        "FROM admins "
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
        "ticket_category, can_broadcast, created_at) VALUES(?,?, 'staff', ?,?,?,?,?,?)",
        (str(user_id), as_str(full_name).strip()[:100], norm_position(position)[:100],
         as_str(department).strip()[:100], as_str(office).strip()[:100],
         ticket_category or "all", int(bool(can_broadcast)), clock.stamp()),
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
        "WHERE t.created_at >= ? GROUP BY t.target_admin_id",
        (clock.stamp_at(-max(1, int(days)) * 24 * 60),),
    )
    return {as_str(row["uid"]): {"tickets": row["tickets"], "open": row["open_n"] or 0,
                                 "last_reply": as_str(row["last_reply"])} for row in rows}


async def set_staff_category(user_id: str, category: str) -> None:
    await db.run("UPDATE admins SET ticket_category=? WHERE user_id=?", (category, user_id))


async def set_staff_broadcast(user_id: str, allowed: bool) -> None:
    await db.run("UPDATE admins SET can_broadcast=? WHERE user_id=?", (int(allowed), user_id))


# ── карточка сотрудника: частичное обновление полей ──────────────────────────────
ADMIN_FIELDS = ("full_name", "role", "position", "department", "office", "ticket_category", "can_broadcast")


async def update_admin(user_id: str, **fields) -> None:
    """Частичное обновление строки admins. Неизвестные поля игнорируются, None — не меняем."""
    if fields.get("position") is not None:
        fields["position"] = norm_position(fields["position"])
    values = [(name, fields[name]) for name in ADMIN_FIELDS if name in fields and fields[name] is not None]
    if not values:
        return
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name, _ in values)} WHERE user_id=?",
        tuple(value for _, value in values) + (str(user_id),),
    )


# ── права сотрудника ──────────────────────────────────────────────────────────
async def set_staff_see_all(user_id: str, value: bool) -> bool:
    """Доступ сотрудника к чужим обращениям: очередь отдела или только свои."""
    await db.run("UPDATE admins SET see_all_tickets=? WHERE user_id=?",
                 (1 if value else 0, as_str(user_id)))
    return True


async def staff_sees_all(user_id: str) -> bool:
    """Видит ли сотрудник чужие обращения: выданное право или роль сис-админа."""
    row = await db.one("SELECT see_all_tickets, role_type FROM admins WHERE user_id=?", (as_str(user_id),))
    if not row:
        return False
    return bool(row["see_all_tickets"]) or as_str(row["role_type"]) in ("owner", "sysadmin", "superadmin")


async def staff_sees_all_bulk() -> dict[str, bool]:
    """Кто из сотрудников видит чужие обращения - одним запросом.

    Панель спрашивала каждого сотрудника по отдельности, то есть делала
    столько же запросов, сколько сотрудников, на самой посещаемой странице.
    """
    rows = await db.many("SELECT user_id, see_all_tickets FROM admins "
                         "WHERE role_type NOT IN ('sysadmin','owner','superadmin')")
    return {as_str(row["user_id"]): bool(row["see_all_tickets"]) for row in rows}


# ── приглашения по ссылке: чем ФИО уже занят ──────────────────────────────────
async def staff_names() -> set:
    """ФИО всех сотрудников одним запросом.

    Предпросмотр пачки спрашивает не по одному человеку, а забирает весь
    список разом: иначе страница на двадцать строк сделала бы двадцать
    запросов только чтобы честно сказать «такой уже есть».
    """
    rows = await db.many("SELECT full_name FROM admins WHERE full_name<>''")
    return {as_str(row["full_name"]) for row in rows}


# ── тестовые сотрудники: стенд для сквозной проверки пути ────────────────────
# Тестовый сотрудник нужен для одной вещи: проверить путь «студент → панель →
# MAX» целиком, не занимая живого человека. Он не пишет в MAX, не читает
# обращений и не отвечает - он цель, которую студент выбирает в боте.
#
# Настоящего MAX ID у него нет и быть не может: идентификатор выдаёт мессенджер.
# Поэтому ID синтетический - «test-1», «test-2», … Числом он быть не должен, и
# проект его числом и не считает: admins.user_id - это TEXT, max_api.send сам
# оставляет строку, когда int() не получился, а handlers.common.notify гасит
# неудачную отправку. Всё остальное (списки сотрудников, выбор сотрудника в
# боте, карточка, права) работает с той же строкой admins - отдельной таблицы
# для тестовых нет и не нужно.
TEST_ID_PREFIX = "test-"
TEST_ID_TRIES = 20                 # сколько раз ищем свободный номер, прежде чем сдаться
TEST_NAME = "Тестовый сотрудник"   # подпись, если ФИО не заполнили


async def next_test_staff_id() -> str:
    """Ближайший свободный синтетический ID: «test-1», «test-2», …

    Номер считается по занятым, а не по числу строк: удалённый «test-2» снова
    свободен, и следующим его и выдадут. Строки, не подходящие под шаблон,
    пропускаются - они не занимают номер.
    """
    rows = await db.many("SELECT user_id FROM admins WHERE user_id LIKE ?",
                         (f"{TEST_ID_PREFIX}%",))
    taken = set()
    for row in rows:
        tail = as_str(row["user_id"])[len(TEST_ID_PREFIX):]
        if tail.isdigit():
            taken.add(int(tail))
    number = 1
    while number in taken:
        number += 1
    return f"{TEST_ID_PREFIX}{number}"


async def add_test_staff(full_name: str = "", position: str = "", department: str = "",
                         office: str = "", ticket_category: str = "all",
                         see_all: bool = False) -> str:
    """Завести тестового сотрудника; возвращает его синтетический user_id.

    Пустая строка - завести не удалось: свободного номера не нашлось. Случается
    только если занято TEST_ID_TRIES номеров подряд, но молча «создать» строку
    без ID было бы хуже: её потом не нашёл бы никто.
    """
    user_id = ""
    for _ in range(TEST_ID_TRIES):
        wanted = await next_test_staff_id()
        if not await get_admin(wanted):
            user_id = wanted
            break
    if not user_id:
        return ""
    # can_broadcast всегда 0: тестовый сотрудник не должен попасть в рассылку.
    await db.run(
        "INSERT INTO admins(user_id, full_name, role_type, position, department, office, "
        "ticket_category, can_broadcast, see_all_tickets, is_test, created_at) "
        "VALUES(?,?, 'staff', ?,?,?,?, 0, ?, 1, ?)",
        (user_id, as_str(full_name).strip()[:100] or TEST_NAME, norm_position(position)[:100],
         as_str(department).strip()[:100], as_str(office).strip()[:100],
         as_str(ticket_category).strip()[:40] or "all", int(bool(see_all)), clock.stamp()),
    )
    return user_id


async def get_test_staff(user_id: str) -> dict | None:
    """Карточка тестового сотрудника; None - если такой строки нет или она обычная."""
    return _row_dict(await db.one("SELECT * FROM admins WHERE user_id=? AND is_test=1",
                                  (as_str(user_id),)))


async def list_test_staff() -> list:
    """Тестовые сотрудники: ровно те, у кого is_test=1.

    Метка лежит в самой строке admins, поэтому и этот список, и список всех
    сотрудников, и выбор сотрудника в боте читают одни и те же строки - расходиться
    им негде.
    """
    return await db.many("SELECT * FROM admins WHERE is_test=1 ORDER BY full_name")


async def test_staff_count() -> int:
    """Сколько тестовых сотрудников заведено: счётчик на странице раздела."""
    return int((await db.one("SELECT COUNT(*) n FROM admins WHERE is_test=1"))["n"])


async def update_test_staff(user_id: str, full_name: str = "", position: str = "",
                            department: str = "", office: str = "",
                            ticket_category: str = "", see_all: str = "") -> bool:
    """Правка тестового сотрудника. Пустое поле - «не трогать», а не «стереть».

    Так же, как при правке сотрудника: очищенное поле не должно молча забрать
    кабинет у того, кого правят на бегу. ``see_all`` приходит строкой «0» или «1»
    из выпадающего списка, и пустое значение (его не бывает) ничего бы не изменило.
    """
    if not await get_test_staff(user_id):
        return False
    values: list[tuple[str, object]] = []
    for name, raw in (("full_name", full_name), ("position", position),
                      ("department", department), ("office", office),
                      ("ticket_category", ticket_category)):
        # должность сводится к справочнику, как и при обычном добавлении
        text = (norm_position(raw) if name == "position" else as_str(raw)).strip()
        if text:
            values.append((name, text[:100]))
    right = as_str(see_all).strip()
    if right in ("0", "1"):
        values.append(("see_all_tickets", int(right)))
    if not values:
        return False
    await db.run(
        f"UPDATE admins SET {', '.join(f'{name}=?' for name, _ in values)} WHERE user_id=?",
        tuple(value for _, value in values) + (as_str(user_id),),
    )
    return True


async def delete_test_staff(user_id: str) -> bool:
    """Удалить тестового сотрудника. Обычного - нет, False.

    Строку с is_test=0 удаляют через delete_staff - осознанно и по правилам
    проекта. Здесь такая попытка - ошибка выбора в панели, и она обязана быть
    отказом, а не тихим исчезновением живого сотрудника.
    """
    if not await get_test_staff(user_id):
        return False
    await db.run("DELETE FROM admins WHERE user_id=?", (as_str(user_id),))
    return True


async def set_staff_test(user_id: str, value: bool = True) -> bool:
    """Переключить сотрудника: обычный → тестовый и обратно.

    Следа в других колонках не остаётся - тот же человек и те же обращения, меняется
    только метка. Поэтому вернуть можно без потерь, и администратору не нужно
    заводить дубля, чтобы проверить путь на живом сотруднике.
    """
    row = await db.one("SELECT is_test FROM admins WHERE user_id=?", (as_str(user_id),))
    if not row:
        return False
    await db.run("UPDATE admins SET is_test=? WHERE user_id=?", (1 if value else 0, as_str(user_id),))
    return True
