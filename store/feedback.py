"""Анонимные сообщения о сервисе: отдельная очередь и временные лимиты без авторов."""
import clock
import database as db

PUBLIC_FEEDBACK_KINDS = ("bug", "suggestion")
PUBLIC_FEEDBACK_STATUSES = ("new", "in_progress", "closed")


class _PrivateText(str):
    """Даже отладочный журнал драйвера SQLite не получает текст и токены в repr."""
    def __repr__(self):
        return "'<скрыто>'"


def _check_status(status: str, *, empty: bool = False) -> None:
    if status not in PUBLIC_FEEDBACK_STATUSES and not (empty and status == ""):
        raise ValueError("Неизвестный статус обратной связи")


async def prune_public_feedback_rates(now: int) -> int:
    """Удаление истёкших HMAC-бакетов; можно вызывать и из обслуживания бота."""
    return await db.run_count(
        "DELETE FROM public_feedback_rate_buckets WHERE expires_at <= ?", (now,))


async def submit_public_feedback(*, now: int, peer_key: str, cookie_key: str = "",
                                 shared_peer: bool = False, kind: str = "", body: str = "",
                                 submission_nonce: str = "") -> tuple[str, int]:
    """Одна транзакция: попытка, лимиты и идемпотентная вставка.

    Ключи уже посчитаны веб-слоем через HMAC. Пустой nonce означает невалидную
    попытку: её тоже считаем, но не создаём обращение. Возвращаем код и ID либо
    время ожидания для limited. Повторный nonce никогда не меняет старый текст.
    """
    for key in (peer_key, cookie_key):
        if key and (len(key) != 64 or any(char not in "0123456789abcdef" for char in key)):
            raise ValueError("Для лимита требуется HMAC-ключ")
    if not peer_key:
        raise ValueError("Для лимита требуется HMAC-ключ")
    body = body.strip()
    if submission_nonce and (kind not in PUBLIC_FEEDBACK_KINDS or not 10 <= len(body) <= 4000
                             or "\x00" in body or len(submission_nonce) > 200):
        raise ValueError("Некорректная обратная связь")
    rules = [("peer:" + peer_key, 30, 3600)]
    if cookie_key:
        rules.append(("cookie:" + cookie_key, 5, 600))
    if shared_peer:
        rules.append(("global", 120, 3600))
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("DELETE FROM public_feedback_rate_buckets WHERE expires_at <= ?", (now,))
            retry_after = 0
            for key, limit, _window in rules:
                row = await (await conn.execute(
                    "SELECT COALESCE(SUM(attempts), 0) AS total, MIN(expires_at) AS first_expiry "
                    "FROM public_feedback_rate_buckets WHERE bucket_key = ?", (key,))).fetchone()
                if row["total"] >= limit:
                    retry_after = max(retry_after, row["first_expiry"] - now)
            if retry_after:
                await conn.commit()
                return "limited", retry_after
            # Окно скользящее: каждая секунда попыток истекает отдельно, не целым часом.
            for key, _limit, window in rules:
                await conn.execute(
                    "INSERT INTO public_feedback_rate_buckets(bucket_key, expires_at, attempts) VALUES(?, ?, 1) "
                    "ON CONFLICT(bucket_key, expires_at) DO UPDATE SET attempts = attempts + 1",
                    (key, now + window))
            if not submission_nonce:
                await conn.commit()
                return "ignored", 0
            nonce = _PrivateText(submission_nonce)
            existing = await (await conn.execute(
                "SELECT id FROM public_feedback WHERE submission_nonce = ?", (nonce,))).fetchone()
            if existing:
                await conn.commit()
                return "replay", existing["id"]
            stamp = clock.stamp()
            result = await conn.execute(
                "INSERT INTO public_feedback(kind, body, created_at, updated_at, submission_nonce) "
                "VALUES(?, ?, ?, ?, ?)", (kind, _PrivateText(body), stamp, stamp, nonce))
            await conn.commit()
            return "created", result.lastrowid
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
