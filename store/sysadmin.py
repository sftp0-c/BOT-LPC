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


_AUDIT_ROLES = frozenset({"owner", "sysadmin", "staff", "student", "bot", "unknown"})
_AUDIT_RESULTS = frozenset({"ok", "denied", "failed", "received"})
_AUDIT_SECRET_TARGETS = frozenset({"invite", "invite_code", "staff_invite"})


def _audit_code(value, fallback: str = "unknown", limit: int = 64) -> str:
    """Фиксированный код, не произвольная фраза с заменёнными пробелами."""
    text = as_str(value).strip()
    if not text or len(text) > limit or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in text):
        return fallback
    return text


def _audit_id(value, limit: int = 20) -> str:
    text = as_str(value).strip()
    return text if text.isascii() and text.isdigit() and len(text) <= limit else ""


def _audit_target(kind: str, value) -> str:
    if kind in ("ticket", "user", "template", "faq", "row", "public_feedback"):
        return _audit_id(value)
    if kind == "callback":
        return _audit_code(value, fallback="")
    if kind == "session":
        return as_str(value) if value in ("regular", "remember") else ""
    return ""


async def log_audit(user_id: str = "", role: str = "", action: str = "",
                    target_type: str = "", target_id: str = "",
                    result: str = "ok") -> None:
    """Структурированный аудит без переписки, значений полей и кодов приглашений."""
    safe_role = as_str(role).strip().lower()
    safe_role = safe_role if safe_role in _AUDIT_ROLES else "unknown"
    safe_target = target_type if target_type in ("ticket", "user", "template", "faq", "row", "callback", "session", "invite", "public_feedback") else ""
    safe_result = as_str(result).strip().lower()
    safe_result = safe_result if safe_result in _AUDIT_RESULTS else "failed"
    # Код приглашения является секретом вне зависимости от имени вызывающего кода.
    safe_target_id = _audit_target(safe_target, target_id)
    await db.run(
        "INSERT INTO audit_log(user_id, role, action, target_type, target_id, result, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (_audit_id(user_id), safe_role, _audit_code(action), safe_target,
         safe_target_id, safe_result, clock.stamp()),
    )


def _audit_where(user_id: str = "", role: str = "", action: str = "",
                 result: str = "", date_from: str = "", date_to: str = "") -> tuple[str, tuple]:
    clauses: list[str] = []
    params: list[str] = []
    if user_id:
        clauses.append("user_id=?")
        params.append(_audit_id(user_id))
    if role:
        clauses.append("role=?")
        params.append(as_str(role).strip().lower())
    if action:
        clauses.append("action=?")
        params.append(_audit_code(action))
    if result:
        clauses.append("result=?")
        params.append(as_str(result).strip().lower())
    if date_from:
        clauses.append("created_at>=?")
        params.append(as_str(date_from).strip()[:10] + " 00:00:00")
    if date_to:
        clauses.append("created_at<=?")
        params.append(as_str(date_to).strip()[:10] + " 23:59:59")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


async def audit_entries(limit: int = 100, offset: int = 0, **filters) -> list:
    """Записи полного аудита: свежие сверху, с комбинируемыми фильтрами."""
    where, params = _audit_where(**filters)
    return await db.many(
        "SELECT id, created_at, user_id, role, action, target_type, target_id, result "
        f"FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + (max(1, min(int(limit), 5000)), max(0, int(offset))),
    )


async def audit_count(**filters) -> int:
    """Число записей полного аудита с теми же фильтрами, что у страницы."""
    where, params = _audit_where(**filters)
    row = await db.one(f"SELECT COUNT(*) n FROM audit_log{where}", params)
    return int(row["n"]) if row else 0


async def audit_actions(limit: int = 100) -> list[str]:
    """Коды действий, встречавшиеся за срок хранения журнала."""
    rows = await db.many(
        "SELECT action FROM audit_log GROUP BY action ORDER BY MAX(id) DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    )
    return [as_str(row["action"]) for row in rows]


async def prune_audit(days: int = 30) -> int:
    """Удаляет полный аудит старше заданного срока хранения."""
    return await db.run_count(
        "DELETE FROM audit_log WHERE created_at < ?",
        (clock.stamp_at(-max(1, int(days)) * 24 * 60),),
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
