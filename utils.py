"""Мелкие общие утилиты: приведение значений, константы предметной области."""
import asyncio
import os
import re
import secrets
from datetime import datetime, timedelta, timezone


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


# ── ссылки на профили MAX ──────────────────────────────────────────────────────
# Публичный профиль пользователя MAX открывается по адресу https://max.ru/<username>.
# Формат вынесен в настройку MAX_PROFILE_LINK: если MAX его поменяет, правка в .env.
PROFILE_LINK = os.getenv("MAX_PROFILE_LINK", "https://max.ru/{username}").strip() or "https://max.ru/{username}"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def profile_url(username) -> str:
    """Ссылка на публичный профиль MAX; '' — если username неизвестен или подозрителен.

    Ник приходит из события MAX, поэтому в адрес подставляется только то, что
    похоже на настоящий username: иначе в href мог бы попасть посторонний адрес.
    """
    name = as_str(username).strip().lstrip("@")
    if not USERNAME_RE.fullmatch(name):
        return ""
    return PROFILE_LINK.format(username=name)


# ── коды доступа для сотрудников ──────────────────────────────────────────────
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I — их путают при переписке
CODE_RE = re.compile(r"^[A-Z2-9]{4,32}$")


def gen_code(length: int = 6) -> str:
    """Случайный код сотрудника из заглавных букв и цифр без неоднозначных символов."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(max(4, min(length, 12))))


def norm_code(value) -> str:
    """Код в виде, в котором он хранится: заглавные буквы и цифры, без пробелов и дефисов."""
    return re.sub(r"[^A-Z0-9]", "", as_str(value).upper())[:32]


# ── время ──────────────────────────────────────────────────────────────────────
# SQLite пишет created_at через datetime('now') — это UTC. Показываем местное время.
def parse_db_time(value):
    """datetime из строки SQLite ('2026-09-25 14:32:05'); None — если разобрать нельзя."""
    text = as_str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def local_time(value):
    """Тот же момент, но в местном часовом поясе; None — если время не разобрано."""
    moment = parse_db_time(value)
    if moment is None:
        return None
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment).astimezone()


def fmt_time(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Дата и время в местном поясе; неразобранное значение возвращается как есть."""
    moment = local_time(value)
    return moment.strftime(fmt) if moment else as_str(value).strip()


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def fmt_when(value, now=None) -> str:
    """Человеческая метка времени: «сегодня 14:32», «вчера 15:00» или «25.09.2026 15:00»."""
    moment = local_time(value)
    if moment is None:
        return as_str(value).strip()
    current = (now or datetime.now().astimezone()).astimezone().date()
    day = (current - moment.date()).days
    if day == 0:
        return moment.strftime("сегодня %H:%M")
    if day == 1:
        return moment.strftime("вчера %H:%M")
    if 1 < day < 7:
        return f"{WEEKDAYS[moment.weekday()]} {moment:%H:%M}"
    return moment.strftime("%d.%m.%Y %H:%M")


def days_ago_text(value, days: int) -> str:
    """«5 дней назад», «вчера», «сегодня» — по строке времени из базы."""
    moment = local_time(value)
    if moment is None:
        return "неизвестно"
    delta = datetime.now().astimezone() - moment
    if delta < timedelta(hours=1):
        return "меньше часа назад"
    if delta < timedelta(hours=24):
        hours = max(1, int(delta.total_seconds() // 3600))
        return f"{hours} ч назад"
    day = delta.days
    if day == 1:
        return "вчера"
    return f"{day} дн назад"


# ── разбор ввода ID и подсказки для быстрой выдачи прав ───────────────────────
# Чтобы выдать права, не приходилось выяснять ID заранее: принимаем и цифры,
# и ссылку на профиль, и @ник, и сразу несколько человек через запятую.
MAX_ID_RE = re.compile(r"\d{3,15}")
NICK_RE = re.compile(r"@([A-Za-z0-9._-]{1,64})")


def parse_max_ids(text) -> list[str]:
    """ID из свободного ввода: «123», «123, 456», «@ivanova 789», столбик — по порядку, без повторов."""
    found: list[str] = []
    for candidate in MAX_ID_RE.findall(as_str(text)):
        if candidate not in found:
            found.append(candidate)
    return found


def parse_nicks(text) -> list[str]:
    """@ники из того же ввода — чтобы можно было написать «@ivanova» вместо цифр."""
    found: list[str] = []
    for nick in NICK_RE.findall(as_str(text)):
        if nick not in found:
            found.append(nick)
    return found


POSITION_HINTS = (
    "Секретарь",
    "Специалист",
    "Преподаватель",
    "Методист",
    "Начальник отдела",
    "Заведующий отделением",
)

CODE_TTL_CHOICES = ((1, "1 час"), (24, "24 часа"), (168, "7 дней"), (720, "30 дней"), (0, "бессрочно"))


def ttl_label(hours: int) -> str:
    """Человеческая подпись срока кода: 24 → «1 дн», 168 → «7 дн», 0 → «бессрочно»."""
    hours = to_int(hours, 0)
    if hours <= 0:
        return "бессрочно"
    if hours % 24 == 0:
        return f"{hours // 24} дн"
    return f"{hours} ч"


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
ROLE_OWNER = "owner"            # владелец бота: максимальные права, назначается на старте
ADMIN_ROLES = (ROLE_OWNER, ROLE_SYSADMIN, LEGACY_SUPERADMIN)
STAFF_ROLES_DB = (ROLE_SYSADMIN, LEGACY_SUPERADMIN, ROLE_OWNER)


def is_sysadmin_role(role_type: str) -> bool:
    """Любая роль с доступом к панели сис-админа."""
    return role_type in ADMIN_ROLES


def is_owner_role(role_type: str) -> bool:
    """Владелец: максимальные права, его нельзя снять."""
    return role_type == ROLE_OWNER


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
