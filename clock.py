"""Часы проекта: одна временная конвенция — локальное время колледжа.

Раньше «сейчас» бралось из двух источников: SQLite datetime('now') (это всегда
UTC) и Python datetime.now() (локальное время машины, а в контейнере с
python:3.12-slim — тоже UTC). В контейнере оба источника давали UTC, поэтому
все даты в базе и в панели отставали на 5 часов от екатеринбургских.

Теперь время в базе — локальное время колледжа, и берётся оно только отсюда:
clock.stamp() кладёт его в created_at, clock.parse() читает обратно,
clock.format_when() превращает в человеческую строку. Всё остальное в проекте
(бот, панель, аналитика) обязано считать «сейчас» через clock.

Зона — из переменной окружения TZ, по умолчанию Asia/Yekaterinburg: с
tzdata 2025+ город живёт в азиатском списке поясов, а Europe/Yekaterinburg
в Docker-образе — ссылка на него (см. Dockerfile). Если зона недоступна
(нет tzdata, опечатка в TZ), работаем на фиксированном UTC+5: перевода часов
в Екатеринбурге нет, так что это ровно то же время, только без пересчёта
летнего времени — и с предупреждением в журнале.

Даты в базе хранятся naive-строками «ГГГГ-ММ-ДД ЧЧ:ММ:СС», поэтому все
функции здесь возвращают naive-время: часовой пояс известен изнутри модуля и
никогда не теряется по дороге.
"""
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Екатеринбург. Имя по умолчанию — азиатское: в tzdata 2025+ именно оно
# основное, а европейское существует только как ссылка в Docker-образе.
DEFAULT_TZ = "Asia/Yekaterinburg"
# Запасной вариант, когда зона не читается: UTC+5 без перевода часов.
FALLBACK_OFFSET = timedelta(hours=5)
FALLBACK_NAME = "UTC+05:00"

STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Строка из базы в каноническом виде — так выглядит stamp().
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
# Тот же вид для отбора прямо в SQL: GLOB по символам, а не LIKE с '_'.
# Им пользуется миграция данных, чтобы не читать в Python лишние строки.
STAMP_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]"

log = logging.getLogger(__name__)

_zone = None
_zone_name = ""


# ── зона ──────────────────────────────────────────────────────────────────────
def zone():
    """Часовой пояс колледжа: ZoneInfo из TZ либо фиксированный UTC+5.

    Читается один раз и запоминается: пересчитывать зону на каждый вызов
    now() незачем, а часовой пояс за время работы процесса не меняется.
    Тестам, которые подменяют TZ, помогает reset().
    """
    global _zone, _zone_name
    if _zone is None:
        name = (os.environ.get("TZ") or "").strip() or DEFAULT_TZ
        try:
            _zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError, KeyError, ModuleNotFoundError):
            _zone = timezone(FALLBACK_OFFSET, FALLBACK_NAME)
            log.warning("Часовой пояс %s недоступен, работаем в фиксированном UTC+5", name)
        _zone_name = name
    return _zone


def tz_name() -> str:
    """Имя зоны, как её поняли: из TZ или значение по умолчанию."""
    zone()
    return _zone_name or DEFAULT_TZ


def is_exact() -> bool:
    """Читается ли настоящая зона, а не запасной фиксированный сдвиг."""
    return isinstance(zone(), ZoneInfo)


def reset() -> None:
    """Забыть загруженный пояс — чтобы он читался заново (нужно тестам)."""
    global _zone, _zone_name
    _zone = None
    _zone_name = ""


def offset_minutes() -> int:
    """Сдвиг зоны относительно UTC в минутах: у Екатеринбурга +300."""
    moment = datetime.now(zone())
    delta = moment.utcoffset() or timedelta(0)
    return int(delta.total_seconds() // 60)


# ── сейчас ────────────────────────────────────────────────────────────────────
def now() -> datetime:
    """Локальное время колледжа без часового пояса — ровно то, что лежит в базе."""
    return datetime.now(zone()).replace(tzinfo=None)


def today() -> date:
    return now().date()


def stamp() -> str:
    """«ГГГГ-ММ-ДД ЧЧ:ММ:СС» локального времени — формат хранения в базе."""
    return now().strftime(STAMP_FORMAT)


def stamp_at(minutes: float) -> str:
    """Тот же вид строки, но на minutes минут вперёд (минус — назад).

    Заменяет SQL вида datetime('now', '+6 hours') и datetime('now', '-1 day'):
    там сдвиг считался в UTC, здесь — в локальном времени.
    """
    return (now() + timedelta(minutes=minutes)).strftime(STAMP_FORMAT)


def date_ago(days: int) -> str:
    """Дата «days дней назад» в виде «ГГГГ-ММ-ДД» — для фильтров по дням."""
    return (today() - timedelta(days=days)).strftime(DATE_FORMAT)


# ── разбор и сдвиг строк из базы ───────────────────────────────────────────────
def parse(value) -> datetime | None:
    """datetime из строки базы; None — разобрать нельзя.

    Понимает «ГГГГ-ММ-ДД ЧЧ:ММ:СС», ISO с «T» и с «Z»/сдвигом. Момент с
    часовым поясом приводится к зоне колледжа, результат — без пояса:
    в базе всё лежит naive, иначе сравнение строк ломалось бы.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text[-1] in "Zz":                     # 3.12 понимает Z сам, но не «z»
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        for pattern in (STAMP_FORMAT, "%Y-%m-%d %H:%M", DATE_FORMAT):
            try:
                moment = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(zone()).replace(tzinfo=None)
    return moment


def shift(value, minutes: float) -> str:
    """Строка времени, сдвинутая на minutes минут (миграция данных).

    Неразобранное значение возвращается как есть: сдвигать то, что не
    похоже на дату, нельзя — лучше оставить, чем испортить.
    """
    moment = parse(value)
    if moment is None:
        return str(value or "")
    return (moment + timedelta(minutes=minutes)).strftime(STAMP_FORMAT)


def looks_like_stamp(value) -> bool:
    """Похоже ли значение на дату из базы (ровно 19 символов нужного вида)."""
    return bool(_STAMP_RE.fullmatch(str(value or "").strip()))


# ── человеческие подписи ───────────────────────────────────────────────────────
def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 минуту, 2 минуты, 5 минут (и 11/12/14 -> many)."""
    tail = abs(int(number)) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def minutes_ago(value: int) -> str:
    return f"{value} {plural(value, 'минуту', 'минуты', 'минут')} назад"


def hours_ago(value: int) -> str:
    return f"{value} {plural(value, 'час', 'часа', 'часов')} назад"


def is_today(value, reference: datetime | None = None) -> bool:
    moment = parse(value)
    return bool(moment) and moment.date() == (reference or now()).date()


def is_yesterday(value, reference: datetime | None = None) -> bool:
    moment = parse(value)
    if not moment:
        return False
    return moment.date() == (reference or now()).date() - timedelta(days=1)


def format_when(value, reference: datetime | None = None) -> str:
    """Человеческая метка времени: «5 минут назад», «сегодня в 14:30», «28.09.2026 09:15».

    Значение из базы — локальное, поэтому «сейчас» тоже берётся локальное.
    Неразобранное значение возвращается как есть: панель покажет его, и
    это лучше, чем молча показать пустоту.
    """
    moment = parse(value)
    if moment is None:
        return str(value or "").strip()
    current = reference or now()
    minutes = int((current - moment).total_seconds()) // 60
    if -1 < minutes < 1:
        return "только что"
    if minutes < 0:                     # метка из будущего: часы спешат или строка не сдвинулась
        ahead = -minutes
        if ahead < 60:
            return f"через {ahead} {plural(ahead, 'минуту', 'минуты', 'минут')}"
        return f"через {ahead // 60} {plural(ahead // 60, 'час', 'часа', 'часов')}"
    if minutes < 60:
        return minutes_ago(minutes)
    if is_today(moment, reference=current):
        return moment.strftime("сегодня в %H:%M")
    if is_yesterday(moment, reference=current):
        return moment.strftime("вчера в %H:%M")
    days = (current.date() - moment.date()).days
    if 1 < days < 7:
        return f"{WEEKDAYS[moment.weekday()]} в {moment:%H:%M}"
    return moment.strftime("%d.%m.%Y %H:%M")
