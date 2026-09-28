"""Список сис-админов и журнал их действий.

Список живёт в базе, а .env только заводит первых. Права проверяются тут же,
по той же таблице admins, поэтому панель и бот всегда сходятся во мнении о
том, кому доступно."""

import clock
import config
import database as db
from utils import as_str, is_owner_role, is_sysadmin_role

from .staff import delete_staff, get_admin


# ── сис-админы: список живёт в базе, .env только заводит первых ────────────────
async def add_sysadmin(user_id: str, full_name: str = "") -> None:
    """Назначает сис-админом навсегда (без правки .env и перезапуска)."""
    await db.run(
        "INSERT INTO admins(user_id, full_name, role_type, can_broadcast, created_at) "
        "VALUES(?,?, 'sysadmin', 1, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET role_type='sysadmin', can_broadcast=1",
        (as_str(user_id), as_str(full_name).strip()[:100] or "Сис-админ", clock.stamp()),
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
        "INSERT INTO admin_log(actor_id, action, details, created_at) VALUES(?,?,?,?)",
        (as_str(actor_id).strip()[:64], as_str(action).strip()[:60], as_str(details).strip()[:300],
         clock.stamp()),
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
            "SELECT action, COUNT(*) n FROM admin_log WHERE created_at >= ? "
            "GROUP BY action ORDER BY n DESC",
            (clock.stamp_at(-int(days) * 24 * 60),),
        )
    }
