"""Общий доступ handlers-модулей: клиент API, роли, рассылка уведомлений."""
import asyncio
import logging

import database as db
import repository as repo
from max_api import MaxAPI, btn
from utils import as_str

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
    return bool(a) and a["role_type"] in ("owner", "sysadmin", "superadmin")  # superadmin — старое имя роли


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


# ── личные шаблоны ответов ─────────────────────────────────────────────────
# Пометка «этот шаблон личный у сотрудника» хранится в settings по ключу
# tpl:<user_id>:<template_id>. Отдельную колонку в reply_templates заводить
# нельзя без правки схемы, а помечается всё несколько человек - хватит и
# одной строки настройки. Значение "1" - личный, отсутствие ключа - общий.
TPL_FLAG = "tpl:"
FLAG_MINE = "1"


def _tpl_flag(user_id: str, template_id: int) -> str:
    return f"{TPL_FLAG}{user_id}:{int(template_id)}"


async def is_personal_template(user_id: str, template_id: int) -> bool:
    """Помечен ли шаблон личным у этого сотрудника."""
    return as_str(await db.get_setting(_tpl_flag(user_id, template_id))).strip() == FLAG_MINE


async def set_personal_template(user_id: str, template_id: int, mine: bool = True) -> None:
    """Пометить шаблон личным или вернуть его в общие."""
    key = _tpl_flag(user_id, template_id)
    if mine:
        await db.set_setting(key, FLAG_MINE)
    else:
        await db.run("DELETE FROM settings WHERE key=?", (key,))


async def personal_template_ids(user_id: str) -> set[int]:
    """id личных шаблонов сотрудника: выборка настроек по его префиксу."""
    rows = await db.many("SELECT key FROM settings WHERE key LIKE ?", (f"{TPL_FLAG}{user_id}:%",))
    ids = set()
    for row in rows:
        tail = as_str(row["key"]).rsplit(":", 1)[-1]
        if tail.isdigit():
            ids.add(int(tail))
    return ids
