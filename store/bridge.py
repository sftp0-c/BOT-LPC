"""store/bridge.py - очередь моста: приём, выдача, ответ.

Разделение по проекту: здесь только работа с базой, без MAX и без логики
владельца. Всё, что знает про то, КАК мост живёт, - в handlers/bridge.py и
web/bridge.py.

Состояния: new -> busy -> done | failed.
«busy» означает, что сообщение забрала программа на компьютере, но ответ ещё
не пришёл. Если программа упала, сообщение там и останется - поэтому
requeue_stale возвращает в новые те, по которым слишком долго нет ответа.
"""
import clock
import database as db
from utils import as_str

# Сколько ждать ответа, прежде чем считать программу упавшей. Час с запасом:
# задача бывает долгой, и вернуть сообщение в очередь на живом деле хуже,
# чем подождать.
BUSY_TIMEOUT_MIN = 90

STATUS_NEW, STATUS_BUSY, STATUS_DONE, STATUS_FAILED = "new", "busy", "done", "failed"


async def add_message(user_id: str, text: str) -> int:
    """Кладёт сообщение владельца в очередь. Возвращает id."""
    body = as_str(text).strip()
    if not body:
        return 0
    return await db.run(
        "INSERT INTO bridge_messages(user_id, text, status, created_at) VALUES(?,?,?,?)",
        (as_str(user_id), body, STATUS_NEW, clock.stamp()))


async def pending(limit: int = 5) -> list:
    """Самые старые необработанные сообщения: их порядок и есть порядок работы."""
    return await db.many(
        "SELECT id, user_id, text, created_at FROM bridge_messages WHERE status=? "
        "ORDER BY id LIMIT ?", (STATUS_NEW, max(1, int(limit))))


async def count_pending() -> int:
    row = await db.one("SELECT COUNT(*) n FROM bridge_messages WHERE status IN (?, ?)",
                       (STATUS_NEW, STATUS_BUSY))
    return int(row["n"] or 0)


async def take(message_id: int) -> bool:
    """Помечает сообщение взятым в работу. True - если взяли именно его.

    Проверка статуса в UPDATE, а не чтение с последующей записью: иначе два
    опроса подряд заберут одно сообщение и ответ придёт дважды.
    """
    changed = await db.run_count(
        "UPDATE bridge_messages SET status=?, taken_at=? WHERE id=? AND status=?",
        (STATUS_BUSY, clock.stamp(), int(message_id), STATUS_NEW))
    return bool(changed)


async def finish(message_id: int, answer: str) -> bool:
    """Записывает мой ответ и закрывает сообщение."""
    return bool(await db.run_count(
        "UPDATE bridge_messages SET status=?, answer=?, answered_at=?, error='' WHERE id=?",
        (STATUS_DONE, as_str(answer).strip()[:6000], clock.stamp(), int(message_id))))


async def fail(message_id: int, error: str) -> bool:
    """Помечает, что не вышло: мост сообщит об этом владельцу."""
    return bool(await db.run_count(
        "UPDATE bridge_messages SET status=?, error=?, answered_at=? WHERE id=?",
        (STATUS_FAILED, as_str(error).strip()[:500], clock.stamp(), int(message_id))))


async def requeue_stale(minutes: int = BUSY_TIMEOUT_MIN) -> int:
    """Возвращает в очередь сообщения, по которым слишком долго нет ответа.

    Нужно на случай, если программа на компьютере взяла задание и упала. Без
    этого сообщение осталось бы «в работе» навсегда, и владелец решил бы, что
    я завис.
    """
    minutes = max(1, int(minutes))
    return await db.run_count(
        "UPDATE bridge_messages SET status=?, taken_at='' "
        "WHERE status=? AND taken_at<>'' AND taken_at < ?",
        (STATUS_NEW, STATUS_BUSY, clock.stamp_at(-minutes)))


async def defer(message_id: int) -> bool:
    """Возвращает сообщение в очередь, не закрывая его.

    Нужно, когда программа взяла задание, но упёрлась в занятый проект. Задание
    не выполнено, но и не потеряно: срок жизни «в работе» и так вернул бы его
    через полтора часа, а ждать владельцу незачем.
    """
    return bool(await db.run_count(
        "UPDATE bridge_messages SET status=?, taken_at='' WHERE id=? AND status=?",
        (STATUS_NEW, int(message_id), STATUS_BUSY)))


async def last_answer(limit: int = 5) -> list:
    """Последние закрытые сообщения: для истории в панели и для разбора."""
    return await db.many(
        "SELECT id, user_id, text, answer, error, status, created_at, answered_at "
        "FROM bridge_messages WHERE status IN (?, ?) ORDER BY id DESC LIMIT ?",
        (STATUS_DONE, STATUS_FAILED, max(1, int(limit))))


async def get_message(message_id: int):
    return await db.one("SELECT * FROM bridge_messages WHERE id=?", (int(message_id),))
