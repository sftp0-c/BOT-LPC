"""Разбор «сырых» обновлений MAX Bot API в удобные для бота значения."""
import hashlib

from utils import as_str


def sender_id(u: dict) -> str:
    """ID пользователя, который действовал.

    В message_callback поле message.sender — это сам бот (он отправил сообщение
    с кнопкой), настоящий пользователь лежит в callback.user.
    """
    kind = u.get("update_type")
    if kind == "message_callback":
        return as_str(((u.get("callback") or {}).get("user") or {}).get("user_id"))
    if kind == "message_created":
        sender = (u.get("message") or {}).get("sender") or {}
        return "" if sender.get("is_bot") else as_str(sender.get("user_id"))
    return as_str((u.get("user") or {}).get("user_id"))


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
