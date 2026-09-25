"""Мелкие общие утилиты: приведение значений, константы предметной области."""
import asyncio
import os
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


def tail_file(path, lines: int = 200, max_bytes: int = 262144) -> list[str]:
    """Последние строки файла (журнала). Читаем только хвост, а не файл целиком.

    Файл может быть большим, поэтому читаем с конца: seek + max_bytes.
    Нет файла или он недоступен — пустой список (журнал ещё не создан).
    """
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            chunk = f.read()
    except OSError:
        return []
    return chunk.decode("utf-8", "replace").splitlines()[-lines:]


def log_level_of(record: str) -> str:
    """Уровень записи журнала из строки формата «время УРОВЕНЬ logger: сообщение»."""
    parts = record.split(" ", 2)
    level = parts[1] if len(parts) > 2 else ""
    return level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else ""


# ── предметные константы ─────────────────────────────────────────────────────

STATUS = {
    "new": "🆕 Новое",
    "accepted": "👌 Принято",
    "in_progress": "🔧 В работе",
    "ready": "📄 Готово к выдаче",
    "completed": "✅ Завершено",
    "rejected": "❌ Отклонено",
}
OPEN_STATUSES = ("new", "accepted", "in_progress")
ACCEPT_ON_REPLY = ("new", "in_progress")
CATS = {
    "feedback": "💬 Обратная связь",
    "certificates": "📄 Справка",
    "academic": "🎓 Учебные вопросы",
    "accounting": "💰 Бухгалтерия",
}
STAFF_CATS = {
    "feedback": "💬 Обратная связь",
    "certificates": "📄 Справки",
    "academic": "🎓 Учебные вопросы",
    "accounting": "💰 Бухгалтерия",
    "all": "🔁 Всё",
}
TOPIC_CATS = {
    "academic": {
        "study": "Учёба и оценки",
        "period": "Сроки, сессии и пересдачи",
        "vacancies": "Вакансии и практика",
    },
    "accounting": {
        "scholarship": "Стипендия и выплаты",
    },
}


def topic_title(cat: str, code: str) -> str:
    return TOPIC_CATS.get(cat, {}).get(code, "")


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
    вместе с числом пользователей. Здесь лок удаляется после завершения всех
    вызовов, которые его получили.
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._interests: dict[str, int] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
            self._interests[key] = 0
        self._interests[key] += 1
        return lock

    def release(self, key: str) -> None:
        interests = self._interests.get(key)
        if interests is None:
            return
        if interests > 1:
            self._interests[key] = interests - 1
            return

        self._interests.pop(key, None)
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def clear(self) -> None:
        self._locks.clear()
        self._interests.clear()

    def __len__(self) -> int:
        return len(self._locks)
