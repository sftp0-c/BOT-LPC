"""Коды сотрудников, заявки на роль, приглашения по ссылке и защита от подбора кода.

Счётчик неудачных попыток живёт рядом с кодами, потому что он и защищает их."""

from typing import Optional

import clock
import config
import database as db
from utils import as_str, gen_code, norm_code, profile_url, ttl_label


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


# ── приглашения по ссылке (диплинк MAX) ──────────────────────────────────────
# Ссылка выглядит как https://max.ru/<ник бота>?start=inv_<КОД>: код едет в
# payload, потому что по переходу MAX присылает событие bot_started с этим полем
# (см. updates.start_payload). Ник бота бот узнаёт о себе при старте и кладёт в
# настройку bot_username, поэтому адрес здесь не зашит.
INVITE_PREFIX = "inv_"


def invite_payload(code: str) -> str:
    """Что кладём в ?start= ссылки: inv_<КОД>. Без кода - и payload пустой."""
    normalized = norm_code(code)
    return f"{INVITE_PREFIX}{normalized}" if normalized else ""


def invite_code(payload) -> str:
    """Код приглашения из payload ссылки; '' - если это не наше приглашение."""
    text = as_str(payload).strip()
    if not text.startswith(INVITE_PREFIX):
        return ""
    return norm_code(text[len(INVITE_PREFIX):])


def invite_link(username, code: str) -> str:
    """Готовая ссылка-приглашение: профиль бота и ?start=inv_<КОД>.

    Адрес берётся из utils.PROFILE_LINK - тем же шаблоном, что и все прочие
    ссылки на профили MAX. Если ни ника бота, ни кода нет, возвращается '':
    выдумывать адрес опасно, а пустая ссылка в панели видна сразу.
    """
    base = profile_url(username)
    payload = invite_payload(code)
    return f"{base}?start={payload}" if base and payload else ""


def invite_status(invite) -> str:
    """Состояние приглашения по строке get_invite: unknown | used | expired | active."""
    if not invite:
        return "unknown"
    if as_str(invite["used_by"]):
        return "used"
    return "expired" if invite["expired"] else "active"


async def get_invite(code: str):
    """Приглашение по коду: строка со всеми полями и признаком «срок истёк».

    Один запрос вместо двух: срок и признак его истечения читает сама база,
    поэтому состояние приглашения не может разойтись с тем, что в ней лежит.
    """
    return await db.one(
        "SELECT *, expires_at<>'' AND expires_at <= ? AS expired FROM staff_invites WHERE code=?",
        (clock.stamp(), norm_code(code)),
    )


async def claim_invite(code: str, user_id: str) -> tuple[bool, str]:
    """Забирает приглашение себе: (получилось ли, причина отказа).

    Проверка и погашение - один UPDATE с условием, ровно как в use_invite: по
    одной ссылке нельзя войти дважды. Кто первый изменил строку, тот и вошёл;
    второму UPDATE уже ничего не меняет и возвращает ноль строк.
    """
    normalized = norm_code(code)
    row = await get_invite(normalized)
    if not row:
        return False, "Приглашение не найдено. Попросите сис-админа выдать новую ссылку."
    if as_str(row["used_by"]):
        return False, "Эта ссылка уже сработала. Попросите сис-админа выдать новую."
    if row["expired"]:
        return False, "Срок приглашения истёк. Попросите сис-админа выдать новую ссылку."
    if as_str(row["user_id"]) and as_str(row["user_id"]) != str(user_id):
        return False, "Это приглашение выдано другому сотруднику."
    stamp = clock.stamp()
    changed = await db.run_count(
        "UPDATE staff_invites SET used_by=?, used_at=? "
        "WHERE code=? AND used_by='' AND (user_id='' OR user_id=?) "
        "AND (expires_at='' OR expires_at > ?)",
        (str(user_id), stamp, normalized, str(user_id), stamp),
    )
    return (True, "") if changed else (False, "Эта ссылка уже сработала.")


async def active_invite_names() -> set:
    """ФИО, по которым уже есть живое приглашение.

    Нужен предпросмотру пачки: вторую ссылку на того же человека выпускать
    незачем, иначе по ссылке войдёт не тот, кого приглашали.
    """
    rows = await db.many(
        "SELECT full_name FROM staff_invites "
        "WHERE full_name<>'' AND used_by='' AND (expires_at='' OR expires_at > ?)",
        (clock.stamp(),),
    )
    return {as_str(row["full_name"]) for row in rows}


async def create_invites_bulk(entries: list, created_by: str = "",
                              ttl_hours: Optional[int] = None) -> list[dict]:
    """Выдаёт сразу несколько приглашений по ссылке. Возвращает созданные.

    У каждой строки свой код, а срок берётся из настройки, если не задан явно.
    Сотрудника здесь не создаётся: он появится, когда человек откроет ссылку
    и подтвердит вход кнопкой (см. handlers.invites).
    """
    hours = config.STAFF_CODE_TTL if ttl_hours is None else int(ttl_hours)
    stamp = clock.stamp()
    until = clock.stamp_at(hours * 60) if hours > 0 else ""
    issued: list[dict] = []
    for entry in entries or []:
        name = as_str(entry.get("full_name")).strip()[:100]
        if not name:
            continue
        code = gen_code(8)
        while await get_invite(code):        # код занят - берём следующий
            code = gen_code(8)
        issued.append({
            "code": code,
            "full_name": name,
            "position": as_str(entry.get("position")).strip()[:100],
            "office": as_str(entry.get("office")).strip()[:100],
            "category": as_str(entry.get("category")).strip()[:40] or "all",
            "expires_at": until,
        })
        await db.run(
            "INSERT INTO staff_invites(code, full_name, position, office, category, "
            "created_by, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (code, name, issued[-1]["position"], issued[-1]["office"], issued[-1]["category"],
             as_str(created_by).strip(), stamp, until),
        )
    return issued
