"""Независимый доступ сотрудников к сайту: права, одноразовые коды и отзывные сессии."""
import hashlib
import hmac
import json
import re
import secrets
import time

import clock
import config
import database as db

WEB_PERMISSIONS = (
    "reply", "status", "note", "forward", "templates", "attachments", "export",
    "view_others", "manage_others",
)
WEB_WORK_PERMISSIONS = WEB_PERMISSIONS[:6]
WEB_PERMISSION_PRESETS = {"view": (), "work": WEB_WORK_PERMISSIONS}
STAFF_WEB_SESSION_TTL = 12 * 3600
STAFF_WEB_CODE_TTL = 5 * 60
STAFF_WEB_CODE_ATTEMPTS = 5
STAFF_WEB_RATE_RULES = {
    "request": (("browser", 5, 600), ("id", 5, 600), ("peer", 30, 3600)),
    "verify": (("browser", 15, 600), ("id", 15, 600), ("peer", 90, 3600)),
}

__all__ = (
    "WEB_PERMISSIONS", "WEB_WORK_PERMISSIONS", "WEB_PERMISSION_PRESETS",
    "get_staff_web_access", "list_staff_web_access", "set_staff_web_access",
    "revoke_staff_web_sessions", "staff_web_principal", "staff_web_principal_in_tx",
    "normalize_staff_web_permissions", "normalize_staff_web_id",
    "staff_web_secret", "staff_web_hash", "staff_web_session_hash",
    "request_staff_web_code", "verify_staff_web_code", "cancel_staff_web_challenge",
    "revoke_staff_web_session",
)


class _PrivateText(str):
    """Параметры SQL и возвращаемые секреты не раскрываются через repr драйвера."""
    def __repr__(self):
        return "'<скрыто>'"


class _PrivateDict(dict):
    """Безопасен даже случайный отладочный repr результата авторизации."""
    def __repr__(self):
        return "<данные авторизации скрыты>"


def _now() -> int:
    return int(time.time())


def staff_web_secret() -> bytes | None:
    value = config.WEB_SESSION_SECRET
    if not isinstance(value, str) or len(value.strip()) < 32:
        return None
    try:
        return value.encode("utf-8")
    except UnicodeError:
        return None


def staff_web_hash(purpose: str, value: str) -> str:
    """Отдельные назначения HMAC исключают обмен cookie, кода и токена местами."""
    secret = staff_web_secret()
    if secret is None:
        raise RuntimeError("Не настроен секрет веб-доступа")
    return _PrivateText(hmac.new(secret, ("staff-web-v1\0" + purpose + "\0" + value).encode("utf-8"),
                                hashlib.sha256).hexdigest())


def _valid_hash(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_token(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def staff_web_session_hash(token: str) -> str:
    if not _valid_token(token):
        return ""
    return staff_web_hash("session", token)


def normalize_staff_web_id(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 9223372036854775807:
        return ""
    return value


def normalize_staff_web_permissions(permissions) -> list[str]:
    if not isinstance(permissions, (list, tuple, set, frozenset)) or any(
            not isinstance(item, str) or item not in WEB_PERMISSIONS for item in permissions):
        raise ValueError("Неизвестные права веб-доступа")
    selected = set(permissions)
    if "manage_others" in selected:
        selected.add("view_others")
    return [name for name in WEB_PERMISSIONS if name in selected]


def _read_permissions(raw: str) -> list[str]:
    try:
        return normalize_staff_web_permissions(json.loads(raw))
    except (ValueError, TypeError):
        raise ValueError("Повреждены права веб-доступа") from None


async def get_staff_web_access(user_id: str) -> dict | None:
    row = await db.one("SELECT * FROM staff_web_access WHERE user_id=?", (_PrivateText(str(user_id)),))
    if row is None:
        return None
    result = dict(row)
    try:
        result["permissions"] = _read_permissions(result["permissions"])
    except ValueError:
        result["permissions"], result["enabled"] = [], 0
    return result


async def list_staff_web_access(search: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    needle = "%" + search[:100].strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = await db.many(
        "SELECT a.user_id,a.full_name,a.position,COALESCE(w.enabled,0) AS enabled,"
        "COALESCE(w.permissions,'[]') AS permissions,COALESCE(w.auth_version,0) AS auth_version "
        "FROM admins a LEFT JOIN staff_web_access w ON w.user_id=a.user_id "
        "WHERE a.role_type='staff' AND a.is_test=0 "
        "AND (lower(a.full_name) LIKE ? ESCAPE '\\' OR lower(a.position) LIKE ? ESCAPE '\\' "
        "OR a.user_id LIKE ? ESCAPE '\\') ORDER BY lower(a.full_name),a.user_id LIMIT ? OFFSET ?",
        (_PrivateText(needle), _PrivateText(needle), _PrivateText(needle),
         max(1, min(100, int(limit))), max(0, int(offset))))
    result = []
    for row in rows:
        if not normalize_staff_web_id(row["user_id"]):
            continue
        item = dict(row)
        try:
            item["permissions"] = _read_permissions(item["permissions"])
        except ValueError:
            item["permissions"], item["enabled"] = [], 0
        result.append(item)
    return result


async def _actor_allowed(conn, actor_id: str) -> bool:
    row = await (await conn.execute("SELECT role_type,is_test FROM admins WHERE user_id=?",
                                   (_PrivateText(actor_id),))).fetchone()
    return bool(row and row["is_test"] == 0 and row["role_type"] in ("sysadmin", "superadmin", "owner"))


async def _target_allowed(conn, user_id: str) -> bool:
    row = await (await conn.execute("SELECT role_type,is_test FROM admins WHERE user_id=?",
                                   (_PrivateText(user_id),))).fetchone()
    return bool(normalize_staff_web_id(user_id) and row and row["role_type"] == "staff" and row["is_test"] == 0)


async def _revoke_in_tx(conn, user_id: str, now: int) -> None:
    await conn.execute("UPDATE staff_web_sessions SET revoked_at=? WHERE user_id=? AND revoked_at=0",
                       (max(1, now), _PrivateText(user_id)))
    await conn.execute("UPDATE staff_web_login_challenges SET consumed_at=? WHERE user_id=? AND consumed_at=0",
                       (max(1, now), _PrivateText(user_id)))


async def _access_audit(conn, actor_id: str, action: str, user_id: str, fields: str) -> None:
    # Только имена полей и ID, никогда значения прав, коды или содержимое форм.
    await conn.execute("INSERT INTO admin_log(actor_id,action,details,created_at) VALUES(?,?,?,?)",
                       (_PrivateText(actor_id), action, _PrivateText(f"user_id={user_id}; {fields}"), clock.stamp()))


async def set_staff_web_access(user_id: str, enabled: bool, permissions, actor_id: str) -> bool:
    """Выдача только действующим сис-админом; изменение и отзыв атомарны."""
    if type(enabled) not in (bool, int) or enabled not in (0, 1):
        raise ValueError("Некорректное состояние веб-доступа")
    selected = normalize_staff_web_permissions(permissions)
    user_id, actor_id = str(user_id), str(actor_id)
    now = _now()
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if not await _actor_allowed(conn, actor_id) or not await _target_allowed(conn, user_id):
            await conn.rollback()
            return False
        await conn.execute(
            "INSERT INTO staff_web_access(user_id,enabled,permissions,auth_version,created_at,updated_at,updated_by) "
            "VALUES(?,?,?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,"
            "permissions=excluded.permissions,auth_version=staff_web_access.auth_version+1,"
            "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (_PrivateText(user_id), int(enabled), _PrivateText(json.dumps(selected, separators=(",", ":"))),
             now, now, _PrivateText(actor_id)))
        await _revoke_in_tx(conn, user_id, now)
        await _access_audit(conn, actor_id, "staff_web_access", user_id, "enabled,permissions")
        await conn.commit()
        return True


async def revoke_staff_web_sessions(user_id: str, actor_id: str) -> bool:
    """Отзыв всех сессий и кодов без отключения разрешённого нового входа."""
    user_id, actor_id = str(user_id), str(actor_id)
    now = _now()
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if not await _actor_allowed(conn, actor_id):
            await conn.rollback()
            return False
        cursor = await conn.execute("UPDATE staff_web_access SET auth_version=auth_version+1,"
                                    "updated_at=?,updated_by=? WHERE user_id=?",
                                    (now, _PrivateText(actor_id), _PrivateText(user_id)))
        if not cursor.rowcount:
            await conn.rollback()
            return False
        await _revoke_in_tx(conn, user_id, now)
        await _access_audit(conn, actor_id, "staff_web_revoke", user_id, "auth_version,sessions,challenges")
        await conn.commit()
        return True


async def staff_web_principal_in_tx(conn, session_hash: str, *, now: int | None = None) -> dict | None:
    """Тот же контроль в соединении операции, без окна между чтением и записью."""
    if staff_web_secret() is None or not _valid_hash(session_hash):
        return None
    now = _now() if now is None else now
    row = await (await conn.execute(
        "SELECT a.user_id,a.full_name,a.position,w.permissions,w.auth_version "
        "FROM staff_web_sessions s JOIN staff_web_access w ON w.user_id=s.user_id "
        "JOIN admins a ON a.user_id=s.user_id WHERE s.token_hash=? AND s.revoked_at=0 "
        "AND s.expires_at>? AND s.auth_version=w.auth_version AND w.enabled=1 "
        "AND a.role_type='staff' AND a.is_test=0", (_PrivateText(session_hash), now))).fetchone()
    if not row or not normalize_staff_web_id(row["user_id"]):
        return None
    try:
        permissions = _read_permissions(row["permissions"])
    except ValueError:
        return None
    return _PrivateDict(user_id=_PrivateText(row["user_id"]), full_name=_PrivateText(row["full_name"]),
                        position=_PrivateText(row["position"]), permissions=permissions,
                        auth_version=row["auth_version"], session_hash=_PrivateText(session_hash))


async def staff_web_principal(session_hash: str) -> dict | None:
    async with db._conn() as conn:
        return await staff_web_principal_in_tx(conn, session_hash)


async def _prune_in_tx(conn, now: int) -> None:
    await conn.execute("DELETE FROM staff_web_rate_buckets WHERE expires_at<=?", (now,))
    await conn.execute("DELETE FROM staff_web_login_challenges WHERE expires_at<=?", (now,))
    await conn.execute("DELETE FROM staff_web_sessions WHERE expires_at<=?", (now,))


async def _take_rates(conn, phase: str, now: int, browser_hash: str, id_hash: str, peer_hash: str) -> bool:
    keys = {"browser": browser_hash, "id": id_hash, "peer": peer_hash}
    rules = [(f"{phase}:{kind}:{keys[kind]}", maximum, window)
             for kind, maximum, window in STAFF_WEB_RATE_RULES[phase] if keys[kind]]
    for key, maximum, _window in rules:
        row = await (await conn.execute("SELECT COALESCE(SUM(attempts),0) AS total "
                                       "FROM staff_web_rate_buckets WHERE bucket_key=?",
                                       (_PrivateText(key),))).fetchone()
        if row["total"] >= maximum:
            return False
    for key, _maximum, window in rules:
        await conn.execute("INSERT INTO staff_web_rate_buckets(bucket_key,expires_at,attempts) VALUES(?,?,1) "
                           "ON CONFLICT(bucket_key,expires_at) DO UPDATE SET attempts=attempts+1",
                           (_PrivateText(key), now + window))
    return True


async def request_staff_web_code(user_id: str, browser_hash: str, peer_hash: str, *,
                                 browser_expires: int, now: int | None = None) -> dict:
    """Результат для HTTP одинаков; код/адресат доступны лишь внутреннему отправителю."""
    live_clock = now is None
    now = _now() if now is None else now
    if not _valid_hash(browser_hash) or not _valid_hash(peer_hash) or not now < browser_expires <= now + 600:
        raise ValueError("Некорректный контекст входа")
    identifier = normalize_staff_web_id(user_id)
    id_hash = staff_web_hash("login-id", identifier or user_id[:100])
    challenge = _PrivateText(secrets.token_urlsafe(32))
    challenge_hash = staff_web_hash("challenge", challenge)
    code = _PrivateText(f"{secrets.randbelow(100_000_000):08d}")
    code_hash = staff_web_hash("code", f"{challenge_hash}:{browser_hash}:{id_hash}:{code}")
    result = _PrivateDict(challenge=challenge, user_id="", code="")
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        # Блокировка могла ждать: срок cookie проверяем по времени после её получения.
        if live_clock:
            now = _now()
        if browser_expires <= now:
            await conn.rollback()
            return result
        await _prune_in_tx(conn, now)
        if not await _take_rates(conn, "request", now, browser_hash, id_hash, peer_hash):
            await conn.commit()
            return result
        row = await (await conn.execute(
            "SELECT w.auth_version,w.permissions FROM staff_web_access w JOIN admins a ON a.user_id=w.user_id "
            "WHERE a.user_id=? AND a.role_type='staff' AND a.is_test=0 AND w.enabled=1",
            (_PrivateText(identifier),))).fetchone()
        if row:
            try:
                _read_permissions(row["permissions"])
            except ValueError:
                row = None
        # Новый код погашает старый лишь у того же браузера, не у чужого сотрудника.
        await conn.execute("UPDATE staff_web_login_challenges SET consumed_at=? "
                           "WHERE browser_hash=? AND consumed_at=0", (max(1, now), _PrivateText(browser_hash)))
        await conn.execute(
            "INSERT INTO staff_web_login_challenges(challenge_hash,browser_hash,id_hash,user_id,code_hash,"
            "auth_version,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (_PrivateText(challenge_hash), _PrivateText(browser_hash), _PrivateText(id_hash),
             _PrivateText(identifier if row else ""), _PrivateText(code_hash), row["auth_version"] if row else 0,
             min(now + STAFF_WEB_CODE_TTL, browser_expires), now))
        await conn.commit()
    if row:
        result.update(user_id=_PrivateText(identifier), code=code)
    return result


async def cancel_staff_web_challenge(challenge: str) -> None:
    if _valid_token(challenge):
        await db.run_count("UPDATE staff_web_login_challenges SET consumed_at=? WHERE challenge_hash=?",
                           (max(1, _now()), staff_web_hash("challenge", challenge)))


async def verify_staff_web_code(challenge: str, code: str, browser_hash: str, peer_hash: str, *,
                                now: int | None = None) -> str | None:
    """Лимиты, попытка кода, погашение и новая сессия — одна BEGIN IMMEDIATE."""
    if not _valid_hash(browser_hash) or not _valid_hash(peer_hash):
        raise ValueError("Некорректный контекст входа")
    live_clock = now is None
    now = _now() if now is None else now
    challenge_hash = staff_web_hash("challenge", challenge[:200])
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        # OTP не продлевается на время ожидания другой записи в SQLite.
        if live_clock:
            now = _now()
        await _prune_in_tx(conn, now)
        row = await (await conn.execute(
            "SELECT * FROM staff_web_login_challenges WHERE challenge_hash=? AND browser_hash=?",
            (_PrivateText(challenge_hash), _PrivateText(browser_hash)))).fetchone()
        id_hash = row["id_hash"] if row else ""
        allowed = await _take_rates(conn, "verify", now, browser_hash, id_hash, peer_hash)
        if not allowed or not row or row["consumed_at"] or row["expires_at"] <= now \
                or row["attempts"] >= STAFF_WEB_CODE_ATTEMPTS:
            await conn.commit()
            return None
        await conn.execute("UPDATE staff_web_login_challenges SET attempts=attempts+1 WHERE challenge_hash=?",
                           (_PrivateText(challenge_hash),))
        expected = staff_web_hash("code", f"{challenge_hash}:{browser_hash}:{id_hash}:{code[:100]}")
        matches = bool(re.fullmatch(r"[0-9]{8}", code)) and hmac.compare_digest(expected, row["code_hash"])
        if not matches or not row["user_id"]:
            if row["attempts"] + 1 >= STAFF_WEB_CODE_ATTEMPTS:
                await conn.execute("UPDATE staff_web_login_challenges SET consumed_at=? WHERE challenge_hash=?",
                                   (max(1, now), _PrivateText(challenge_hash)))
            await conn.commit()
            return None
        access = await (await conn.execute(
            "SELECT w.auth_version,w.permissions FROM staff_web_access w JOIN admins a ON a.user_id=w.user_id "
            "WHERE w.user_id=? AND w.enabled=1 AND w.auth_version=? AND a.role_type='staff' AND a.is_test=0",
            (_PrivateText(row["user_id"]), row["auth_version"]))).fetchone()
        try:
            valid = bool(access and normalize_staff_web_id(row["user_id"]))
            if valid:
                _read_permissions(access["permissions"])
        except ValueError:
            valid = False
        await conn.execute("UPDATE staff_web_login_challenges SET consumed_at=? WHERE challenge_hash=?",
                           (max(1, now), _PrivateText(challenge_hash)))
        if not valid:
            await conn.commit()
            return None
        token = _PrivateText(secrets.token_urlsafe(32))
        await conn.execute("INSERT INTO staff_web_sessions(token_hash,user_id,auth_version,expires_at,created_at) "
                           "VALUES(?,?,?,?,?)", (staff_web_session_hash(token), _PrivateText(row["user_id"]),
                                                row["auth_version"], now + STAFF_WEB_SESSION_TTL, now))
        await conn.commit()
        return token


async def revoke_staff_web_session(session_hash: str) -> bool:
    if not _valid_hash(session_hash):
        return False
    return bool(await db.run_count("UPDATE staff_web_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at=0",
                                   (max(1, _now()), _PrivateText(session_hash))))
