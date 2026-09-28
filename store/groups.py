"""Справочник групп и псевдонимы кода.

Люди пишут код группы по-разному, поэтому хранится один канонический вид,
а все прочие написания запоминаются псевдонимами и ищутся по обоим."""

import clock
import database as db
from utils import as_str, group_code, group_digits, same_group, valid_group

from .common import _code


# ── справочник групп ──────────────────────────────────────────────────────────
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


# ── единый справочник групп ───────────────────────────────────────────────────
# Люди пишут код группы по-разному: «24-23 (П)», «24-23П», «2423П», «24 23 п».
# Поэтому храним один канонический вид (utils.group_code) и запоминаем
# псевдонимы, а при регистрации ищем по обоим.
async def add_group_aliases(group_code_: str, aliases=()) -> int:
    """Запоминает, как ещё пишут код группы. Возвращает число новых псевдонимов."""
    canonical = group_code(group_code_)
    if not canonical:
        return 0
    added = 0
    for alias in aliases or ():
        normalized = group_code(alias)
        if not normalized or normalized == canonical:
            continue
        changed = await db.run(
            "INSERT INTO group_aliases(alias, group_code, created_at) VALUES(?,?,?) "
            "ON CONFLICT(alias) DO UPDATE SET group_code=excluded.group_code",
            (normalized, canonical, clock.stamp()),
        )
        added += int(changed > 0)
    return added


async def group_aliases(group_code_: str = "") -> list[str]:
    """Псевдонимы группы (или все псевдонимы, если код не задан)."""
    if group_code_:
        rows = await db.many("SELECT alias FROM group_aliases WHERE group_code=? ORDER BY alias",
                             (group_code(group_code_),))
    else:
        rows = await db.many("SELECT alias FROM group_aliases ORDER BY alias")
    return [as_str(row["alias"]) for row in rows]


async def find_group(text: str) -> dict | None:
    """Ищет группу по любому написанию кода. None - не нашли.

    Порядок: точное совпадение канонического кода, затем псевдоним, затем
    сравнение по цифрам (если код отличается одной лишь раскладкой букв).
    """
    wanted = group_code(text)
    if not wanted:
        return None
    row = await db.one("SELECT group_code, title, active FROM groups WHERE group_code=?", (wanted,))
    if row:
        return _group_dict(row)
    alias = await db.one("SELECT g.group_code, g.title, g.active FROM group_aliases a "
                         "JOIN groups g ON g.group_code=a.group_code WHERE a.alias=?", (wanted,))
    if alias:
        return _group_dict(alias)
    digits = group_digits(wanted)
    if not digits:
        return None
    for candidate in await db.many("SELECT group_code, title, active FROM groups"):
        if same_group(candidate["group_code"], wanted):
            return _group_dict(candidate)
    return None


def _group_dict(row) -> dict:
    return {"code": as_str(row["group_code"]), "title": as_str(row["title"]),
            "active": bool(row["active"])}


async def suggest_groups(text: str, limit: int = 8) -> list[dict]:
    """Подсказки для регистрации: что человек мог иметь в виду.

    Сначала группы, у которых совпадают цифры или начало кода, потом - по
    похожему началу. Пустой текст даёт первые группы справочника.
    """
    rows = await db.many("SELECT group_code, title, active FROM groups ORDER BY group_code")
    groups = [_group_dict(row) for row in rows]
    wanted = group_code(text)
    digits = group_digits(wanted)
    if not digits:
        return groups[:limit]

    def score(group: dict) -> tuple:
        code = group["code"]
        if code == wanted:
            return (0, 0, code)
        if group_digits(code) == digits:
            return (1, 0, code)
        if code.startswith(wanted) or wanted.startswith(code):
            return (2, 0, code)
        shared = len(set(code) & set(wanted))
        return (3, -shared, code)

    return sorted(groups, key=score)[:limit]


async def resolve_group(text: str) -> dict:
    """Готовит ответ для регистрации: нашли группу или нет, и что предложить.

    {"found": bool, "code": str, "title": str, "suggestions": [группы]}
    """
    group = await find_group(text)
    if group:
        return {"found": True, "code": group["code"], "title": group["title"], "suggestions": []}
    suggestions = await suggest_groups(text)
    return {"found": False, "code": group_code(text), "title": "", "suggestions": suggestions}


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
        await c.execute("INSERT OR IGNORE INTO groups(group_code, created_at) VALUES(?,?)",
                        (normalized, clock.stamp()))
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
