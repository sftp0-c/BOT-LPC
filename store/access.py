"""Коды сотрудников, заявки на роль и защита от подбора кода.

Счётчик неудачных попыток живёт рядом с кодами, потому что он и защищает их."""

from typing import Optional

import clock
import config
import database as db
from utils import as_str, norm_code, ttl_label


# ── коды и заявки на роль сотрудника ──────────────────────────────────────────
async def create_invite(
    code: str,
    user_id: str = "",
    full_name: str = "",
    created_by: str = "",
    ttl_hours: Optional[int] = None,
) -> str:
    """Создаёт код сотрудника. ttl_hours=0 — без срока. Возвращает код."""
    normalized = norm_code(code)
    hours = config.STAFF_CODE_TTL if ttl_hours is None else int(ttl_hours)
    stamp = clock.stamp()
    await db.run(
        "INSERT INTO staff_invites(code, user_id, full_name, created_by, created_at, expires_at) "
        "VALUES(?,?,?,?,?, CASE WHEN ?>0 THEN ? ELSE '' END) "
        "ON CONFLICT(code) DO UPDATE SET user_id=excluded.user_id, full_name=excluded.full_name, "
        "created_by=excluded.created_by, created_at=excluded.created_at, expires_at=excluded.expires_at, "
        "used_by='', used_at=''",
        (normalized, as_str(user_id).strip(), as_str(full_name).strip()[:100], as_str(created_by).strip(),
         stamp, hours, clock.stamp_at(hours * 60)),
    )
    return normalized


async def use_invite(code: str, user_id: str) -> tuple[bool, str]:
    """Пробует активировать код для пользователя. → (принят ли код, причина отказа).

    UPDATE с условием делает проверку и погашение атомарно: два человека с одним
    кодом не смогут пройти одновременно — победит первый, второй получит отказ.
    """
    normalized = norm_code(code)
    row = await db.one(
        "SELECT user_id, used_by, expires_at<>'' AND expires_at <= ? AS expired "
        "FROM staff_invites WHERE code=?",
        (clock.stamp(), normalized),
    )
    if not row:
        return False, "Код не найден. Проверьте раскладку клавиатуры и пробелы."
    if row["used_by"]:
        return False, "Этот код уже использовали. Попросите сис-админа выдать новый."
    if row["expired"]:
        return False, "Срок действия кода истёк. Попросите новый код."
    if row["user_id"] and as_str(row["user_id"]) != str(user_id):
        return False, "Этот код выдан другому сотруднику."
    changed = await db.run_count(
        "UPDATE staff_invites SET used_by=?, used_at=? "
        "WHERE code=? AND used_by='' AND (user_id='' OR user_id=?)",
        (str(user_id), clock.stamp(), normalized, str(user_id)),
    )
    return (True, "") if changed else (False, "Код уже использован.")


async def invite_state(code: str) -> str:
    """Состояние кода: active | used | expired | unknown."""
    row = await db.one(
        "SELECT used_by, expires_at<>'' AND expires_at <= ? AS expired "
        "FROM staff_invites WHERE code=?",
        (clock.stamp(), norm_code(code)),
    )
    if not row:
        return "unknown"
    if row["used_by"]:
        return "used"
    return "expired" if row["expired"] else "active"


async def list_invites(limit: int = 50) -> list:
    return await db.many(
        "SELECT i.*, COALESCE(u.full_name, '') fio, COALESCE(a.full_name, '') used_by_name "
        "FROM staff_invites i "
        "LEFT JOIN users u ON u.user_id=i.user_id "
        "LEFT JOIN admins a ON a.user_id=i.used_by "
        "ORDER BY i.created_at DESC, i.code LIMIT ?",
        (limit,),
    )


async def delete_invite(code: str) -> None:
    await db.run("DELETE FROM staff_invites WHERE code=?", (norm_code(code),))


async def set_invite_ttl(code: str, hours: int) -> tuple[bool, str]:
    """Меняет срок уже выданного кода: 0 — бессрочно. Возвращает (получилось, срок)."""
    normalized = norm_code(code)
    row = await db.one("SELECT used_by FROM staff_invites WHERE code=?", (normalized,))
    if not row:
        return False, "Код не найден"
    if row["used_by"]:
        return False, "Этот код уже использован"
    await db.run(
        "UPDATE staff_invites SET expires_at = CASE WHEN ?>0 THEN ? ELSE '' END WHERE code=?",
        (int(hours), clock.stamp_at(int(hours) * 60), normalized),
    )
    return True, ttl_label(hours)


async def create_staff_request(
    user_id: str,
    full_name: str = "",
    position: str = "",
    office: str = "",
    note: str = "",
) -> None:
    stamp = clock.stamp()
    await db.run(
        "INSERT INTO staff_requests(user_id, full_name, position, office, note, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, position=excluded.position, "
        "office=excluded.office, note=excluded.note, status='new', updated_at=excluded.updated_at",
        (str(user_id), as_str(full_name).strip()[:100], as_str(position).strip()[:100],
         as_str(office).strip()[:100], as_str(note).strip()[:300], stamp, stamp),
    )


async def staff_requests(status: str = "new", limit: int = 100) -> list:
    where = "WHERE r.status=?" if status else ""
    return await db.many(
        f"SELECT r.*, c.username, c.display_name FROM staff_requests r "
        f"LEFT JOIN contacts c ON c.user_id=r.user_id {where} ORDER BY r.updated_at DESC LIMIT ?",
        ([status] if status else []) + [limit],
    )


async def get_staff_request(user_id: str):
    return await db.one("SELECT * FROM staff_requests WHERE user_id=?", (str(user_id),))


async def set_staff_request_status(user_id: str, status: str) -> None:
    await db.run(
        "UPDATE staff_requests SET status=?, updated_at=? WHERE user_id=?",
        (status, clock.stamp(), str(user_id)),
    )


# ── защита от подбора кода ────────────────────────────────────────────────────
async def note_attempt(user_id: str) -> None:
    await db.run("INSERT INTO login_attempts(user_id, created_at) VALUES(?,?)",
                 (str(user_id), clock.stamp()))


async def attempts_count(user_id: str, minutes: int = 60) -> int:
    row = await db.one(
        "SELECT COUNT(*) n FROM login_attempts WHERE user_id=? AND created_at >= ?",
        (str(user_id), clock.stamp_at(-int(minutes))),
    )
    return row["n"]


async def clear_attempts(user_id: str) -> None:
    await db.run("DELETE FROM login_attempts WHERE user_id=?", (str(user_id),))


async def attempts_log(limit: int = 30) -> list:
    """Кто и сколько раз пробовал коды за последний час — для панели."""
    return await db.many(
        "SELECT a.user_id, COUNT(*) tries, MAX(a.created_at) last_try, "
        "COALESCE(c.display_name, c.username, '') label, COALESCE(s.status, '') request_status "
        "FROM login_attempts a "
        "LEFT JOIN contacts c ON c.user_id=a.user_id "
        "LEFT JOIN staff_requests s ON s.user_id=a.user_id "
        "WHERE a.created_at >= ? "
        "GROUP BY a.user_id ORDER BY tries DESC, last_try DESC LIMIT ?",
        (clock.stamp_at(-24 * 60), limit),
    )
