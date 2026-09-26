"""Разбор «сырых» обновлений MAX Bot API в удобные для бота значения."""
import hashlib

from utils import as_str


def sender_id(u: dict) -> str:
    """ID пользователя, который действовал.

    В message_callback поле message.sender — это сам бот (он отправил сообщение
    с кнопкой), настоящий пользователь лежит в callback.user.
    """
    return as_str(user_of(u).get("user_id"))


def user_of(u: dict) -> dict:
    """Объект User того, кто действовал: оттуда берём имя, ник и время активности.

    В message_callback message.sender — сам бот, нажавший пользователь лежит
    в callback.user; в message_created отправитель — в message.sender; в
    bot_started — в user. В MAX это объект User: user_id, first_name,
    last_name, username (может быть null), is_bot, last_activity_time.
    """
    kind = u.get("update_type")
    if kind == "message_callback":
        return (u.get("callback") or {}).get("user") or {}
    if kind == "message_created":
        sender = (u.get("message") or {}).get("sender") or {}
        return {} if sender.get("is_bot") else sender
    return u.get("user") or {}


def profile_of(u: dict) -> dict:
    """Данные о пользователе из события: {'username', 'display_name'}.

    username у MAX nullable (если имя не задано или профиль закрыт) — тогда
    ссылки на профиль не будет, но ID и отображаемое имя знаем.
    """
    user = user_of(u)
    first = as_str(user.get("first_name")).strip()
    last = as_str(user.get("last_name")).strip()
    name = " ".join(part for part in (first, last) if part)
    if not name:  # у части пользователей заполнено только устаревшее поле name
        name = as_str(user.get("name")).strip()
    return {"username": as_str(user.get("username") or "").strip(), "display_name": name[:100]}


def update_key(u: dict):
    """Уникальный «отпечаток» события для защиты от дублей доставки.

    MAX доставляет события с гарантией «как минимум один раз»: long polling
    может переотдать события после обрыва соединения, webhook — повторить по
    своей политике ретраев, а два процесса бота могут забрать одно событие
    одновременно. Возвращает ключ, на котором бот делает дедупликацию, или
    None, если отпечаток построить нельзя (событие пропустит дедупликацию).
    """
    kind = u.get("update_type")
    if kind == "message_callback":
        cid = ((u.get("callback") or {}).get("callback_id"))
        return f"cb:{cid}" if cid else None
    if kind == "message_created":
        m = u.get("message") or {}
        ts = m.get("timestamp") or u.get("timestamp")
        chat = (m.get("recipient") or {}).get("chat_id")
        uid = (m.get("sender") or {}).get("user_id")
        text = (m.get("body") or {}).get("text")
        if ts is None or chat is None or uid is None:
            return None
        digest = hashlib.sha1(as_str(text).encode("utf-8", "ignore")).hexdigest()[:16]
        return f"mc:{chat}:{ts}:{uid}:{digest}"
    if kind == "bot_started":
        ts = u.get("timestamp")
        uid = (u.get("user") or {}).get("user_id")
        return f"bs:{uid}:{ts}" if ts is not None and uid is not None else None
    return None


def message_text(u: dict) -> str:
    return as_str(((u.get("message") or {}).get("body") or {}).get("text")).strip()


def is_dialog(u: dict) -> bool:
    """Сообщение из личного диалога? (группы и каналы бот не обслуживает)"""
    return ((u.get("message") or {}).get("recipient") or {}).get("chat_type", "dialog") == "dialog"


def callback_id(u: dict) -> str:
    return as_str((u.get("callback") or {}).get("callback_id"))


def callback_payload(u: dict) -> str:
    return as_str((u.get("callback") or {}).get("payload"))
