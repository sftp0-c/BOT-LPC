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


def short_name(value, n: int = 0) -> str:
    """ФИО для кнопки: целиком, если влезает, иначе фамилия с инициалами.

    «Соколова Мария» кнопка вмещает целиком, а «Ковалевский Константин Юрьевич»
    пришлось бы обрезать многоточием - длинное ФИО сокращаем до «Ковалевский
    К. Ю.». Полное ФИО и должность сотрудника и так перечислены текстом над
    списком, так что в кнопке имени хватает.
    Предел по умолчанию - ширина кнопки MAX (max_api.BUTTON_TEXT).
    """
    if not n:
        # импорт локальный: на уровне модуля возникает круг
        # utils -> max_api -> config -> utils
        from max_api import BUTTON_TEXT
        n = BUTTON_TEXT
    parts = [part for part in as_str(value).split() if part]
    if not parts:
        # ФИО может быть не заполнено, а пустая подпись кнопки роняет всё
        # сообщение в MAX - поэтому подставляем слово, а не пустую строку
        return "Без имени"
    if len(" ".join(parts)) <= n:      # короткое ФИО показываем как есть
        return " ".join(parts)
    name = parts[0]
    for part in parts[1:]:
        with_initials = f"{name} {part[0].upper()}."
        if len(with_initials) > n:            # инициалы лишние - оставляем как есть
            break
        name = with_initials
    return cut_plain(name, n)



def person_label(name, user_id: str = "", n: int = 0) -> str:
    """Подпись человека в кнопке: «Ковалевский К. Ю.», а без ФИО - по MAX ID.

    Без ФИО человека не опознать, тем более что ник в профиле MAX бывает
    скрыт. MAX ID уникален и его видно в самой панели, поэтому он и идёт в
    кнопку - остаётся скопировать и открыть карточку.
    """
    label = short_name(name, n)
    if label == "Без имени":
        digits = "".join(ch for ch in as_str(user_id) if ch.isdigit())
        return f"ID {digits}" if digits else label
    return label

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


# ── справочник должностей сотрудников ───────────────────────────────────────
# Должность в проекте была свободным текстом: её набирали руками каждый раз,
# и «ПК» с «Приёмной комиссией» оказывались двумя разными людьми в списке.
# Теперь должности лежат здесь одним списком, и всё остальное берёт его отсюда:
# кнопки-подсказки в боте, выпадающий список в панели и группировка сотрудников
# в подменю «Обратная связь». Список лежит в utils, а не в handlers или store,
# потому что его берут и бот, и панель, а utils ни от кого из них не зависит -
# круг импортов иначе получился бы на ровном месте.
#
# ВНИМАНИЕ: список правый и его нужно менять под колледж. Слова здесь взяты
# общие, а не из приказа: ничего о конкретном колледже не выдумано, зато любой
# сис-админ может дописать свою строку одним словом. Три поля на должность:
#   код   - короткий латинский ключ (в базе это код роли, по нему и фильтры);
#   название - полное название, его видят студенты и панель;
#   кнопка - короткая подпись для кнопки бота, полное название туда не влезает.
POSITIONS: tuple[tuple[str, str, str], ...] = (
    ("director", "Директор", "👔 Директор"),
    ("deputy", "Заместитель директора", "👥 Зам директора"),
    ("deputy_uvr", "Заместитель директора по УВР", "👥 Зам УРП"),
    ("deputy_upr", "Заместитель директора по УПР", "👥 Зам УПР"),
    # УНР в названии и УМР в подписи кнопки - так было в проекте до этого
    # справочника (полное название брали из STAFF_ROLES, кнопку - из
    # FEEDBACK_ROLES). Расхождение оставлено как есть: что из этого верно -
    # знает только колледж, а менять это молча нельзя.
    ("deputy_unr", "Заместитель директора по УНР", "👥 Зам УМР"),
    ("chief_accountant", "Главный бухгалтер", "💼 Гл. бухгалтер"),
    ("secretary", "Секретарь", "📋 Секретарь"),
    ("admissions", "Приёмная комиссия", "🎓 Приёмная"),
    ("study_office", "Учебная часть", "🗓 Учебная"),
    ("accounting", "Бухгалтерия", "💰 Бухгалтерия"),
    ("psychologist", "Психолог", "🧠 Психолог"),
    ("medical", "Медпункт", "🩺 Медпункт"),
    ("dormitory", "Общежитие", "🏠 Общежитие"),
    ("security", "Охрана", "🛡 Охрана"),
    ("technical", "Техническая служба", "🔧 Техслужба"),
    ("library", "Библиотека", "📚 Библиотека"),
    ("canteen", "Столовая", "🍽 Столовая"),
    ("social_pedagogue", "Социальный педагог", "🧑‍🏫 Соцпедагог"),
)
"""Должности колледжа: (код, полное название, подпись кнопки)."""

POSITION_CODES: tuple[str, ...] = tuple(code for code, _title, _btn in POSITIONS)
POSITION_TITLES: dict[str, str] = {code: title for code, title, _btn in POSITIONS}
POSITIONS_BTN: dict[str, str] = {code: label for code, _title, label in POSITIONS}

# Руководящие должности: именно им в подменю «Обратная связь» положены
# отдельные кнопки, потому что к ним пишут адресно. Порядок - от старшего к
# младшему, он же порядок кнопок на экране.
POSITION_LEADERS: tuple[str, ...] = ("director", "deputy_uvr", "deputy_upr", "deputy_unr")

# Синонимы, которые человек пишет вместо должности из справочника. Ключи
# сравниваются приведёнными (см. position_key), поэтому «Уч. часть» и
# «уч.часть» - одно и то же. Правка сюда ничего не ломает: должность, которой
# в списке нет, остаётся свободным текстом и просто не попадает в группу.
POSITION_ALIASES: dict[str, str] = {
    "директор колледжа": "director",
    "зам": "deputy",
    "зам дир": "deputy",
    "зам директора": "deputy",
    "заместитель": "deputy",
    "заместитель директора": "deputy",
    "зам по увр": "deputy_uvr",
    "зам ур": "deputy_uvr",
    "зам директора по увр": "deputy_uvr",
    "зам по упр": "deputy_upr",
    "зам упр": "deputy_upr",
    "зам директора по упр": "deputy_upr",
    "зам по унр": "deputy_unr",
    "зам унр": "deputy_unr",
    "зам директора по унр": "deputy_unr",
    "главбух": "chief_accountant",
    "гл бухгалтер": "chief_accountant",
    "главный бухгалтер": "chief_accountant",
    "секретариат": "secretary",
    "секретарь": "secretary",
    "прием": "admissions",
    "приём": "admissions",
    "приемная": "admissions",
    "приёмная": "admissions",
    "приемная комиссия": "admissions",
    "приёмная комиссия": "admissions",
    "пк": "admissions",
    "п к": "admissions",
    "пкк": "admissions",
    "учебная часть": "study_office",
    "учебный отдел": "study_office",
    "уч часть": "study_office",
    "учебно производственный отдел": "study_office",
    "бухгалтер": "accounting",
    "бухгалтерия": "accounting",
    "психологическая служба": "psychologist",
    "психолог": "psychologist",
    "мед пункт": "medical",
    "медицинский пункт": "medical",
    "медпункт": "medical",
    "медсестра": "medical",
    "общага": "dormitory",
    "общежитие": "dormitory",
    "охрана": "security",
    "техслужба": "technical",
    "техническая служба": "technical",
    "технический отдел": "technical",
    "библиотека": "library",
    "столовая": "canteen",
    "столовая буфет": "canteen",
    "соцпед": "social_pedagogue",
    "соцпедагог": "social_pedagogue",
    "социальный педагог": "social_pedagogue",
}

# Знаки, которые человек ставит от руки, а смысла в них нет: «Уч. часть» и
# «Уч часть» - одна и та же должность.
_POSITION_NOISE = re.compile(r"[.\-–—,;:()\[\]\"«»/]+")
_POSITION_LETTERS = re.compile(r"[0-9A-Za-z\u0400-\u04FF]")


def position_key(text) -> str:
    """Ключ для сравнения должностей: без регистра, «ё» и лишних знаков.

    Тот же приём, что в group_code для кодов групп: человек пишет «уч. часть»
    или «УЧ ЧАСТЬ», а это одна должность, а не три.
    """
    raw = " ".join(as_str(text).split()).lower().replace("ё", "е")
    return " ".join(_POSITION_NOISE.sub(" ", raw).split())


def _position_index() -> dict[str, str]:
    """Приведённое написание -> код должности: названия, кнопки, коды, синонимы."""
    index: dict[str, str] = {}
    for code in POSITION_CODES:
        for form in (code, POSITION_TITLES[code], POSITIONS_BTN[code]):
            key = position_key(form)
            if key:
                index.setdefault(key, code)
    for alias, code in POSITION_ALIASES.items():
        index.setdefault(position_key(alias), code)
    return index


_POSITION_INDEX = _position_index()


def norm_position(value) -> str:
    """Должность в одном виде: пробелы, регистр и синонины сведены к справочнику.

    «ПК», «пк», «Приёмная комиссия» и «приемная  комиссия» дают один и тот же
    результат, поэтому в подменю попадают в одну группу. Должность, которой в
    справочнике нет, не выдумывается: она остаётся текстом, только с
    заглавной буквы и без лишних пробелов.
    """
    raw = " ".join(as_str(value).split())
    # «-», «—» и прочая пунктуация - это не должность: в боте ими чистят поле,
    # и оставлять их в базе нельзя, иначе у человека появится раздел с тире
    # вместо названия. Должность обязана хоть что-то говорить буквами.
    if not raw or not _POSITION_LETTERS.search(raw):
        return ""
    code = _POSITION_INDEX.get(position_key(raw), "")
    return POSITION_TITLES[code] if code else raw[0].upper() + raw[1:]


def position_code(value) -> str:
    """Код должности из справочника; '' - должность своя или не заполнена."""
    return _POSITION_INDEX.get(position_key(value), "")


def position_label(code) -> str:
    """Короткая подпись должности для кнопки; неизвестный код показываем как есть."""
    return POSITIONS_BTN.get(as_str(code), as_str(code))


def position_group(position, role: str = "") -> str:
    """Код группы, в которую попадает сотрудник: должность, иначе тип роли.

    Пусто - значит, человек есть, а должность не заполнена или своя: тогда он не
    попадает ни в одну группу, и это лучше, чем подставить чужую.
    """
    return position_code(position) or (as_str(role) if as_str(role) in POSITION_TITLES else "")


def has_position(position) -> bool:
    """Заполнена ли должность: для пустого состояния раздела сотрудника.

    Проверяется не текст, а нормированное значение: «-» и «—» в боте означают
    «должности нет», и открывать такой сотруднику нечего.
    """
    return bool(norm_position(position))


def row_value(row, key: str, default: str = "") -> str:
    """Поле строки сотрудника: строки SQLite не умеют .get, а словари умеют."""
    if isinstance(row, dict):
        return as_str(row.get(key, default))
    try:
        return as_str(row[key])
    except (KeyError, IndexError, TypeError):
        return default


def group_by_position(rows) -> dict[str, list]:
    """Сотрудники, разложенные по должностям справочника: {код: [сотрудник]}.

    Группировка идёт по position_group, а не по написанному тексту, поэтому
    «ПК» и «Приёмная комиссия» попадают в одну группу. Сотрудник без должности и
    без кода типа роли в словарь не попадает вовсе: для него честнее сказать
    «должность не заполнена», чем показать чужое подменю.
    """
    groups: dict[str, list] = {}
    for row in rows or []:
        code = position_group(row_value(row, "position"), row_value(row, "role"))
        if code:
            groups.setdefault(code, []).append(row)
    return groups

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
# Подписи тех же разделов для кнопок. Фильтр раздела всегда идёт по две кнопки
# в ряду, а предел MAX в таком ряду - 16 символов: «🎓 Учебные вопросы» (19)
# обрезался многоточием в каждой карточке сотрудника. Полное название при
# этом остаётся в тексте над списком («Обращения: 🎓 Учебные вопросы»).
STAFF_CATS_BTN = {
    "feedback": "💬 Связь",
    "certificates": "📄 Справки",
    "academic": "🎓 Учёба",
    "accounting": "💰 Деньги",
    "all": "🔁 Всё",
}
# Полные названия тем. Они попадают в текст обращения, в уведомление
# сотруднику и в карточку дела - там места хватает и смысл важен.
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
# Короткие подписи тех же тем для кнопок: предел в ряду из одной кнопки -
# 20 ячеек, а «Сроки, сессии и пересдачи» это 25. Полное название остаётся
# в заголовке и в тексте обращения.
TOPIC_CATS_BTN = {
    "academic": {
        "study": "Учёба и оценки",
        "period": "Сроки и сессии",
        "vacancies": "Вакансии",
    },
    "accounting": {
        "scholarship": "Стипендия",
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
