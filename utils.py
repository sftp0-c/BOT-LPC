"""Мелкие общие утилиты: приведение значений, константы предметной области."""
import asyncio
import os
import re

import clock
import secrets
from datetime import timedelta


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


# ── коды групп college-lan: люди пишут их по-разному ──────────────────────────
# На сайте колледжа группы выглядят как «24-23 (П)», «24-21(2С)», «26-29(П)».
# Студент может написать «24-23П», «24-23 п», «2423П» или «24 23 (п)» - поэтому
# приводим всё к одному виду: две цифры, дефис, цифры, суффикс без скобок.
_GROUP_TAIL_RE = re.compile(r"^(\d{2})\s*[-–—_]?\s*(\d{1,3})\s*[([{]?\s*([0-9А-ЯЁA-Z][0-9А-ЯЁA-Z]{0,3})\s*[)\]}]?$")


def group_code(text: str) -> str:
    """Единый вид кода группы: «24-23 (П)» → «24-23П», «24-21(2С)» → «24-21-2С».

    В коде остаются только цифры, дефис и буквы, поэтому он проходит GROUP_RE
    и его можно набрать как с кириллицей, так и латиницей. Суффикс из одной
    буквы приклеиваем без дефиса («24-23П»), из нескольких символов - через
    дефис («24-21-2С»).
    """
    raw = " ".join(as_str(text).split()).upper().replace("Ё", "Е")
    match = _GROUP_TAIL_RE.match(raw)
    if match:
        year, number, suffix = match.group(1), match.group(2), match.group(3)
        base = f"{year}-{number}"
        return base if not suffix else (f"{base}{suffix}" if len(suffix) == 1 else f"{base}-{suffix}")
    return re.sub(r"[^А-ЯЁA-Z0-9.-]", "", raw).strip("-.")


def group_digits(text: str) -> str:
    """Только цифры кода - по ним находим группу, когда человек написал её небрежно."""
    return re.sub(r"\D", "", as_str(text))


def same_group(left: str, right: str) -> bool:
    """Один и тот же ли код записан по-разному: сравниваем и буквы, и цифры."""
    a, b = group_code(left), group_code(right)
    if a and a == b:
        return True
    digits_a, digits_b = group_digits(a), group_digits(b)
    return bool(digits_a) and digits_a == digits_b and len(a) == len(b)


def short(text: str, n: int) -> str:
    """Однострока не длиннее n символов (с многоточием при обрезке)."""
    text = " ".join(as_str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def cut_plain(text: str, n: int) -> str:
    """Обрезка без многоточия: режем по границе слова, хвост убираем молча.

    short() оставляет «…», а в подписи кнопки многоточие читается как «имя
    обрезалось» и ничего не сообщает. Здесь подпись просто заканчивается.
    """
    text = " ".join(as_str(text).split())
    if len(text) <= n or n < 2:
        return text
    cut = text[:n].rstrip()
    space = cut.rfind(" ")
    if space >= n // 2:                       # не отбрасываем полслова ради пары букв
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") or text[:n]


def short_name(value, n: int = 26) -> str:
    """ФИО для кнопки: целиком, если влезает, иначе фамилия с инициалами.

    «Соколова Мария» кнопка вмещает целиком, а «Ковалевский Константин Юрьевич»
    пришлось бы обрезать многоточием - длинное ФИО сокращаем до «Ковалевский
    К. Ю.». Полное ФИО и должность сотрудника и так перечислены текстом над
    списком, так что в кнопке имени хватает.
    Предел по умолчанию - ширина кнопки MAX (max_api.BUTTON_TEXT).
    """
    parts = [part for part in as_str(value).split() if part]
    if not parts:
        return ""
    if len(" ".join(parts)) <= n:      # короткое ФИО показываем как есть
        return " ".join(parts)
    name = parts[0]
    for part in parts[1:]:
        with_initials = f"{name} {part[0].upper()}."
        if len(with_initials) > n:            # инициалы лишние - оставляем как есть
            break
        name = with_initials
    return cut_plain(name, n)


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
# Время в базе локальное, часовой пояс колледжа (см. clock). Раньше здесь стояло
# «голое время из базы считаем UTC и переводим в местное» — после перевода базы
# на локальное время это давало лишние +5 часов в каждой дате на панели.
def parse_db_time(value):
    """datetime из строки SQLite ('2026-09-25 14:32:05'); None — если разобрать нельзя."""
    return clock.parse(as_str(value).strip())


def local_time(value):
    """Момент в часовом поясе колледжа без зоны; None — если время не разобрано.

    Голое время из базы уже местное, поэтому оно возвращается как есть. Если в
    строке есть явный сдвиг ('Z', '+05:00') — приводится к колледжу.
    """
    return clock.parse(as_str(value).strip())


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
    current = clock.parse(now).date() if now else clock.today()
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
    delta = clock.now() - moment
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
# Подпись статуса в кнопке: «№1234 · 📄 Готово к выдаче» в одну строку не
# влезает, MAX обрезал бы её многоточием. В тексте статус остаётся полным.
STATUS_SHORT = {
    "new": "🆕 Новое",
    "accepted": "👌 Принято",
    "in_progress": "🔧 В работе",
    "ready": "📄 Готово",
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
