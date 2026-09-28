"""Нижнее меню MAX: это команды бота, а не кнопки в сообщении.

По документации MAX бот умеет отправлять только inline-кнопки, а нижнее меню
чата собирается из команд, зарегистрированных через PATCH /me/commands. Поэтому
здесь список команд, которые MAX показывает в меню, и их отображение на уже
существующие действия бота: нажатие команды не должно вести в пустоту.
"""
from handlers.registry import CALLBACKS

# (имя команды, описание для меню MAX) -> payload существующего обработчика
BOT_COMMANDS: list[tuple[str, str, str]] = [
    ("menu", "Главное меню", "home"),
    ("schedule", "Моё расписание", "view_schedules"),
    ("today", "Пары на сегодня", "today"),
    ("tickets", "Мои обращения", "tickets"),
    ("new_request", "Новое обращение", "new:feedback"),
    ("profile", "Профиль и группа", "profile"),
    ("teacher", "Расписание преподавателя", "teacher"),
    ("queue", "Очередь обращений", "staff"),
    ("stats", "Статистика", "staffstats"),
    ("help", "Что умеет бот и помощь", "help"),
    ("view", "Режим: сис-админ / сотрудник / студент", "view"),
]

# только сис-админам: MAX показывает меню всем, поэтому команды закрыты проверкой
ADMIN_COMMANDS: list[tuple[str, str, str]] = [
    ("admin", "Панель сис-админа", "sysadm"),
]

MAX_COMMAND_NAME = 32      # ограничение MAX
MAX_COMMANDS = 32          # ограничение MAX


def command_name(value: str) -> str:
    """Имя команды в том виде, в каком его понимает MAX (латиница, цифры, _)."""
    out = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in value.lower())
    if not out or not out[0].isalpha():
        out = "cmd_" + out
    return out[:MAX_COMMAND_NAME]


def bot_command_list() -> list[dict]:
    """Тело для PATCH /me/commands."""
    commands = [{"name": command_name(name), "description": desc[:256]}
                for name, desc, _ in BOT_COMMANDS[:MAX_COMMANDS]]
    return commands


def command_payload(text: str) -> str | None:
    """«/today», «today», «/today@bot» -> payload обработчика."""
    word = (text or "").strip().split()[0] if (text or "").strip() else ""
    if not word:
        return None
    word = word.split("@")[0].lstrip("/").lower()
    word = word.split(":")[0].split()[0]      # /view:student -> view, аргумент разбирает вызывающий
    for name, _desc, payload in BOT_COMMANDS + ADMIN_COMMANDS:
        if command_name(name) == word:
            return payload
    return None


def command_labels() -> str:
    """Подсказка «Команды: …» для /help и панели."""
    return ", ".join(f"/{command_name(name)}" for name, _, _ in BOT_COMMANDS)


def unknown_payloads(known: set[str] | None = None) -> list[str]:
    """Команды без обработчика - проверяется тестом, а не на импорте."""

    return [payload for _n, _d, payload in BOT_COMMANDS + ADMIN_COMMANDS
            if CALLBACKS.get(payload.split(":")[0]) is None]
