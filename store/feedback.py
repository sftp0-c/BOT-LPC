"""Анонимные сообщения о сервисе: отдельная очередь и временные лимиты без авторов."""
import hashlib
import time

import clock
import database as db

PUBLIC_FEEDBACK_KINDS = ("bug", "suggestion")
PUBLIC_FEEDBACK_STATUSES = ("new", "in_progress", "closed")
PUBLIC_FEEDBACK_COOLDOWN = 30
PUBLIC_FEEDBACK_TTL = 2 * 3600


class _PrivateText(str):
    """Даже отладочный журнал драйвера SQLite не получает текст и токены в repr."""
    def __repr__(self):
        return "'<скрыто>'"


def _check_status(status: str, *, empty: bool = False) -> None:
    if status not in PUBLIC_FEEDBACK_STATUSES and not (empty and status == ""):
        raise ValueError("Неизвестный статус обратной связи")


def _now() -> int:
    return int(time.time())


def _nonce_digest(nonce: str) -> str:
    return _PrivateText(hashlib.sha256(("public-feedback-deleted-v1\0" + nonce).encode("utf-8")).hexdigest())


async def _prune_in_tx(conn, now: int) -> int:
    rates = await conn.execute("DELETE FROM public_feedback_rate_buckets WHERE expires_at <= ?", (now,))
    deleted = await conn.execute("DELETE FROM public_feedback_deleted_nonces WHERE expires_at <= ?", (now,))
    return rates.rowcount + deleted.rowcount


async def prune_public_feedback_rates(now: int) -> int:
    """Удаление истёкших лимитов и отпечатков без текста удалённых отзывов."""
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        count = await _prune_in_tx(conn, now)
        await conn.commit()
        return count


async def _rate_wait(conn, rules, now: int) -> int:
    retry_after = 0
    for key, limit, _window in rules:
        row = await (await conn.execute(
            "SELECT COALESCE(SUM(attempts), 0) AS total, MIN(expires_at) AS first_expiry "
            "FROM public_feedback_rate_buckets WHERE bucket_key = ?", (_PrivateText(key),))).fetchone()
        if row["total"] >= limit:
            retry_after = max(retry_after, row["first_expiry"] - now)
    return retry_after


async def submit_public_feedback(*, peer_key: str, cookie_key: str = "", now: int | None = None,
                                 shared_peer: bool = False, kind: str = "", body: str = "",
                                 submission_nonce: str = "", expires_at: int = 0) -> tuple[str, int]:
    """Попытка, квоты, пауза и вставка атомарны; повтор не меняет текст и срок паузы.

    Явные часы оставлены для детерминированных тестов. HTTP их не передаёт:
    реальные время и срок формы проверяются после получения блокировки SQLite.
    """
    for key in (peer_key, cookie_key):
        if key and (len(key) != 64 or any(char not in "0123456789abcdef" for char in key)):
            raise ValueError("Для лимита требуется HMAC-ключ")
    if not peer_key or (submission_nonce and not cookie_key):
        raise ValueError("Для лимита требуется HMAC-ключ")
    body = body.strip()
    if submission_nonce and (kind not in PUBLIC_FEEDBACK_KINDS or not 10 <= len(body) <= 4000
                             or "\x00" in body or len(submission_nonce) > 200):
        raise ValueError("Некорректная обратная связь")
    # shared_peer — совместимый аргумент; глобальная квота действует для всех.
    rules = [("peer:" + peer_key, 30, 3600), ("global", 120, 3600)]
    if cookie_key:
        rules.append(("cookie:" + cookie_key, 5, 600))
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now() if now is None else now
            await _prune_in_tx(conn, now)
            retry_after = await _rate_wait(conn, rules, now)
            if retry_after:
                await conn.commit()
                return "limited", retry_after
            for key, _limit, window in rules:
                await conn.execute(
                    "INSERT INTO public_feedback_rate_buckets(bucket_key, expires_at, attempts) VALUES(?, ?, 1) "
                    "ON CONFLICT(bucket_key, expires_at) DO UPDATE SET attempts = attempts + 1",
                    (_PrivateText(key), now + window))
            if not submission_nonce:
                await conn.commit()
                return "ignored", 0
            if expires_at and expires_at <= now:
                await conn.commit()
                return "expired", 0
            nonce = _PrivateText(submission_nonce)
            existing = await (await conn.execute(
                "SELECT id FROM public_feedback WHERE submission_nonce = ?", (nonce,))).fetchone()
            deleted = await (await conn.execute(
                "SELECT 1 FROM public_feedback_deleted_nonces WHERE nonce_hash = ?",
                (_nonce_digest(submission_nonce),))).fetchone()
            if existing or deleted:
                await conn.commit()
                return "replay", existing["id"] if existing else 0
            cooldown_key = _PrivateText("cooldown:" + cookie_key)
            cooldown = await (await conn.execute(
                "SELECT MAX(expires_at) AS until FROM public_feedback_rate_buckets WHERE bucket_key = ?",
                (cooldown_key,))).fetchone()
            if cooldown["until"] and cooldown["until"] > now:
                wait = max(cooldown["until"] - now, await _rate_wait(conn, rules, now))
                await conn.commit()
                return "limited", wait
            stamp = clock.stamp()
            result = await conn.execute(
                "INSERT INTO public_feedback(kind, body, created_at, updated_at, submission_nonce) "
                "VALUES(?, ?, ?, ?, ?)", (kind, _PrivateText(body), stamp, stamp, nonce))
            await conn.execute(
                "INSERT INTO public_feedback_rate_buckets(bucket_key, expires_at, attempts) VALUES(?, ?, 1)",
                (cooldown_key, now + PUBLIC_FEEDBACK_COOLDOWN))
            await conn.commit()
            return "created", result.lastrowid
        except BaseException:
            await conn.rollback()
            raise


async def delete_public_feedback(feedback_id: int, actor_id: str) -> str:
    """Удаляется один отзыв; отпечаток не даёт старой форме создать его заново."""
    if type(feedback_id) is not int or not 0 < feedback_id <= 9223372036854775807:
        return "not_found"
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            actor = await (await conn.execute(
                "SELECT role_type,is_test FROM admins WHERE user_id=?", (_PrivateText(str(actor_id)),))).fetchone()
            if not actor or actor["is_test"] or actor["role_type"] not in ("owner", "sysadmin", "superadmin"):
                await conn.rollback()
                return "denied"
            row = await (await conn.execute(
                "SELECT submission_nonce FROM public_feedback WHERE id=?", (feedback_id,))).fetchone()
            if not row:
                await conn.rollback()
                return "not_found"
            now = _now()
            await _prune_in_tx(conn, now)
            # Дольше максимального срока публичной формы хранить отпечаток не нужно.
            await conn.execute(
                "INSERT INTO public_feedback_deleted_nonces(nonce_hash,expires_at) VALUES(?,?) "
                "ON CONFLICT(nonce_hash) DO UPDATE SET expires_at=MAX(expires_at,excluded.expires_at)",
                (_nonce_digest(row["submission_nonce"]), now + PUBLIC_FEEDBACK_TTL))
            removed = await conn.execute("DELETE FROM public_feedback WHERE id=?", (feedback_id,))
            if removed.rowcount != 1:
                await conn.rollback()
                return "not_found"
            await conn.commit()
            return "deleted"
        except BaseException:
            await conn.rollback()
            raise


async def list_public_feedback(status: str = "", limit: int = 50, offset: int = 0):
    _check_status(status, empty=True)
    where, args = ("WHERE status = ?", (status,)) if status else ("", ())
    return await db.many(
        "SELECT id, kind, status, created_at, updated_at FROM public_feedback "
        f"{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, max(1, min(100, limit)), max(0, offset)))


async def count_public_feedback(status: str = "") -> int:
    _check_status(status, empty=True)
    where, args = ("WHERE status = ?", (status,)) if status else ("", ())
    row = await db.one(f"SELECT COUNT(*) AS total FROM public_feedback {where}", args)
    return row["total"]


async def get_public_feedback(feedback_id: int):
    # Токен отправки не нужен даже странице сис-админа.
    return await db.one(
        "SELECT id, kind, body, status, created_at, updated_at, handled_by "
        "FROM public_feedback WHERE id = ?", (feedback_id,))


async def set_public_feedback_status(feedback_id: int, status: str, handled_by: str) -> bool:
    _check_status(status)
    if not handled_by:
        raise ValueError("Не указан обработавший сис-админ")
    return bool(await db.run_count(
        "UPDATE public_feedback SET status = ?, updated_at = ?, handled_by = ? WHERE id = ?",
        (status, clock.stamp(), handled_by, feedback_id)))
