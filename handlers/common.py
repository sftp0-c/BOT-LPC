"""Общий доступ handlers-модулей: клиент API, роли, рассылка уведомлений."""
import asyncio
import logging

import repository as repo
from max_api import MaxAPI, btn

log = logging.getLogger("bot")

api = MaxAPI()

DEFAULT_WELCOME = "🏫 Бот колледжа. Выберите действие:"
BACK = [[btn("↩️ В меню", "home")]]

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Фоновая задача, которая не будет собрана GC до завершения."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def pending_tasks() -> list[asyncio.Task]:
    """Ещё не завершённые фоновые задачи (нужны для корректной остановки приложения)."""
    return [t for t in _tasks if not t.done()]


# ── роли ──────────────────────────────────────────────────────────────────────
async def admin_of(user_id: str):
    return await repo.get_admin(user_id)


def is_super(a) -> bool:
    return bool(a) and a["role_type"] in ("sysadmin", "superadmin")  # superadmin — старое имя роли


def can_broadcast(a) -> bool:
    return bool(a) and (is_super(a) or bool(a["can_broadcast"]))


async def need_super(user_id: str):
    """Строка admins, если пользователь — сис-админ, иначе None."""
    return await admin_of(user_id) if is_super(await admin_of(user_id)) else None


async def notify(user_id, text, keyboard=None) -> bool:
    """Отправка без падения: пользователь мог не запускать бота или заблокировать его."""
    try:
        await api.send(user_id, text, keyboard)
        return True
    except Exception as exc:
        log.warning("не удалось отправить сообщение %s: %s", user_id, exc)
        return False
