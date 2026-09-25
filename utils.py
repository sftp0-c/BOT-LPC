"""Мелкие общие утилиты: приведение значений, константы предметной области."""
import asyncio
import re


def as_str(value) -> str:
    """Строка из любого значения; None → пустая строка."""
    return "" if value is None else str(value)


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def norm_group(text: str) -> str:
    """Нормализация кода группы: схлопываем пробелы, приводим к верхнему регистру."""
    return " ".join(text.split()).upper()


def short(text: str, n: int) -> str:
    """Однострока не длиннее n символов (с многоточием при обрезке)."""
    text = " ".join(as_str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ── предметные константы ─────────────────────────────────────────────────────

STATUS = {"new": "🆕 Новое", "in_progress": "🔧 В работе", "completed": "✅ Завершено", "rejected": "❌ Отклонено"}
OPEN_STATUSES = ("new", "in_progress")
CATS = {"feedback": "💬 Обратная связь", "certificates": "📄 Справка"}
STAFF_CATS = {"feedback": "💬 Обратная связь", "certificates": "📄 Справки", "all": "🔁 Всё"}


GROUP_RE = re.compile(r"^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9.-]{0,29}$")  # код группы: без пробелов, до 30 символов


def valid_group(group: str) -> bool:
    """Код группы после norm_group(): буквы/цифры, дефисы и точки (например, ИС-21)."""
    return bool(GROUP_RE.fullmatch(group))


ROLE_SYSADMIN = "sysadmin"      # сис-админ (старое имя в БД — superadmin)
LEGACY_SUPERADMIN = "superadmin"


def is_sysadmin_role(role_type: str) -> bool:
    return role_type in (ROLE_SYSADMIN, LEGACY_SUPERADMIN)


class UserLocks:
    """Лок на пользователя с очисткой неиспользуемых ключей.

    Обычный defaultdict(asyncio.Lock) никогда не отдаёт ключи — словарь растёт
    вместе с числом пользователей. Здесь лок удаляется, когда его никто не ждёт.
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def release(self, key: str) -> None:
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def clear(self) -> None:
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._locks)
