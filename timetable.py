"""Расписание: разбор PDF в занятия по дням недели и выдача текстом.

Почему так. Раньше бот только хранил ссылку на PDF и отдавал её студенту. PDF
колледжа — это таблицы «пара № | группа | предмет | вид | преподаватель | ауд.»,
и по ссылке студент не видит ни своего дня, ни времени. Здесь PDF скачивается,
разбирается в структуру «занятие», кладётся в базу и выдаётся текстом прямо в
MAX: 📅 Понедельник 28.09, «1. Математика», «09:00–09:45», «ауд. 204 · Крылова В. И.».

Устройство модуля:
* чистые функции `build_schedule`, `format_day`, `format_schedule`, `match_groups`
  работают с уже извлечёнными из PDF страницами — их легко тестировать;
* слой `parse_pdf` — единственное место, где нужен pdfplumber;
* слой загрузки (`ensure_schedule`) живёт в handlers/schedules.py.

Время пар берётся из звонков `LESSON_TIMES`: сами PDF времени не содержат.
Порядок нужен для точечных правок: номер 3 → 10:50.

Номер в PDF — это урок, а не пара: в паре уроков два. Поэтому в тексте между
парами стоит пустая строка, и день читается парами, а не простынёй.
"""
from __future__ import annotations

import re

import clock
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from utils import as_str

# Звонки колледжа — по урокам, в порядке следования за номером урока в PDF.
# Номер в PDF — это урок, а не пара: в паре уроков два, поэтому список плоский.
LESSON_TIMES: tuple[tuple[str, str], ...] = (
    ("08:00", "08:45"),  # 1 урок  (1 пара)
    ("08:50", "09:35"),  # 2 урок
    ("09:50", "10:35"),  # 3 урок  (2 пара)
    ("10:55", "11:40"),  # 4 урок
    ("12:00", "12:45"),  # 5 урок  (3 пара)
    ("13:05", "13:50"),  # 6 урок
    ("14:00", "14:45"),  # 7 урок  (4 пара)
    ("14:50", "15:35"),  # 8 урок
    ("15:45", "17:15"),  # 9 урок  (5 пара — один урок)
    ("17:25", "18:55"),  # 10 урок (6 пара — один урок)
    ("19:00", "20:30"),  # 11 урок (7 пара — один урок)
)
MAX_LESSONS_PER_DAY = len(LESSON_TIMES)

# Официальный график звонков: 7 пар, 11 уроков (скрин владельца, сверили с
# таблицей выше построчно - совпало всё). Первые четыре пары - по два урока,
# последние три - по одному длинному. Порядок уроков в паре: (первый, последний).
PAIR_SPANS: tuple[tuple[int, int], ...] = (
    (1, 2), (3, 4), (5, 6), (7, 8), (9, 9), (10, 10), (11, 11),
)
PAIR_COUNT = len(PAIR_SPANS)


def pair_of_lesson(number: int) -> int:
    """Номер пары по номеру урока: 1,2 -> 1; 3,4 -> 2; 9 -> 5."""
    for index, (first, last) in enumerate(PAIR_SPANS, start=1):
        if first <= number <= last:
            return index
    return 0


def pair_time(number: int) -> str:
    """Время пары одной строкой по номеру ПАРЫ: «08:00–09:35».

    Номер пары - это индекс в PAIR_SPANS, а не номер урока: если искать
    интервал по номеру урока, пара (1, 2) подходила и первому, и второму
    уроку, и обе пары выходили с одинаковым временем.
    """
    if not 1 <= number <= len(PAIR_SPANS):
        return ""
    first, last = PAIR_SPANS[number - 1]
    начало = LESSON_TIMES[first - 1][0] if first <= len(LESSON_TIMES) else ""
    конец = LESSON_TIMES[last - 1][1] if last <= len(LESSON_TIMES) else ""
    return f"{начало}–{конец}" if начало and конец else ""


WEEKDAYS_FULL = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)

# «Понедельник» и его обрывки из-за переносов строк внутри PDF
_DAY_ALIASES: dict[str, int] = {}
for _index, _day in enumerate(WEEKDAYS_FULL[:6]):
    _cap = _day.capitalize()
    for _size in range(3, len(_cap) + 1):
        _DAY_ALIASES[_cap[:_size].lower()] = _index

# Коды групп встречаются в двух видах: «24-21(2С)» (курс-группа) и «ИС-21»
# (буквы-группа). Оба разбираем, остальное в шапке таблицы — не группа.
_GROUP_RE = re.compile(
    r"^(?:[А-ЯA-Zа-яa-z]{0,3}\d{1,2}|[А-ЯA-Zа-яa-z]{1,3})[-–/]\d{1,3}[А-Яа-яA-Za-z0-9]*(?:\s*\([^)]*\))?$"
)
_ROMAN_RE = re.compile(r"^[IVX]{1,4}$", re.IGNORECASE)
_TEACHER_RE = re.compile(
    r"(?:[А-ЯЁ][а-яё]{1,25}\s+){0,2}[А-ЯЁ]\.\s?[А-ЯЁ]?\.{0,2}(?:\s*,\s*(?:[А-ЯЁ][а-яё]{1,25}\s+){0,2}[А-ЯЁ]\.\s?[А-ЯЁ]?\.{0,2})*"
)
_SUBGROUP_RE = re.compile(r"^(\d)\s*[.:)]\s*\D")
_LESSON_NUM_RE = re.compile(r"^\s*(\d{1,2})\s*$")
_ROOM_RE = re.compile(r"^[А-Яа-яA-Za-z][\w/\-.]*(?:\s*\d+)?$")


def norm(value) -> str:
    """Всё пробельное убрать, регистр поднять: для сравнения кодов групп."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@dataclass
class LessonPart:
    """Часть урока. Обычно одна; при подгруппах - по одной на подгруппу.

    В PDF в ячейке предмета подгруппы идут как «1.Предмет», «2.Предмет», и в
    ячейке аудитории напротив - столько же строк, по одной на подгруппу. Раньше
    разбор склеивал всё в одну строку и брал первый кабинет, поэтому кабинет
    второй подгруппы пропадал. Владелец отдельно просил их видеть.
    """

    subject: str
    teacher: str = ""
    room: str = ""


@dataclass
class Lesson:
    number: int                    # номер урока в дне
    subject: str                   # все части через « | » (так хранится)
    teacher: str = ""              # все преподаватели через «; »
    room: str = ""                 # все аудитории через « / »
    start: str = ""                # «09:00» из звонков
    end: str = ""                  # «09:45»
    parts: list[LessonPart] = field(default_factory=list)

    def time_str(self) -> str:
        return f"{self.start}–{self.end}" if self.start and self.end else ""

    def split_parts(self) -> list[LessonPart]:
        """Части урока; если parts нет - собираем одну из склеенных строк.

        Храним части и списком, и склейкой в subject/teacher/room: списком -
        для показа, склейкой - для базы и для всех прежних проверок.
        """
        if self.parts:
            return self.parts
        предметы = [x.strip() for x in str(self.subject or "").split("|") if x.strip()]
        преподаватели = [x.strip() for x in str(self.teacher or "").split(";") if x.strip()]
        аудитории = [x.strip() for x in str(self.room or "").split(" / ") if x.strip()]
        if not предметы:
            return []
        # Строка вида занятия - это не отдельный предмет, а продолжение того, что
        # над ним. Новый разбор так и склеивает, но в базе ещё лежат строки,
        # разобранные старым кодом: «Экономика отрас | (лекция)». Такая часть
        # приклеивается к предыдущей, иначе студент видит «(лекция)» отдельной
        # строкой - мусор, которого раньше не было.
        склеенные: list[str] = []
        for предмет in предметы:
            if предмет.startswith("(") and склеенные:
                склеенные[-1] = f"{склеенные[-1]} {предмет}"
            else:
                склеенные.append(предмет)
        предметы = склеенные
        # Метка «(подгруппы) » осталась от старого разбора. Номер подгруппы «1.»
        # и так всё говорит, а лишнее слово в тексте мешает читать.
        if предметы and предметы[0].startswith("(подгруппы) "):
            предметы[0] = предметы[0][len("(подгруппы) "):]
        части = []
        for index, предмет in enumerate(предметы):
            части.append(LessonPart(
                subject=предмет,
                teacher=преподаватели[index] if index < len(преподаватели) else "",
                room=аудитории[index] if index < len(аудитории) else "",
            ))
        # Преподаватели и кабинеты берутся по номеру, склеивать их в первую
        # часть нельзя: тогда второй преподаватель печатается дважды - и в первой
        # строке, и в своей.
        #
        # Кабинет при старом формате один на всех подгрупп, и печатается у первой:
        # кабинета второй подгруппы в базе нет, его туда никто не клал. Появится
        # после переразбора файлов на сервере. Врать не будем.
        return части

    def line(self) -> str:
        """Урок текстом: номер, время, предмет; ниже кабинет и преподаватель.

        Подгруппы идут отдельными строками, у каждой свой кабинет и свой
        преподаватель - так просил владелец. Раньше они склеивались в одну
        строку через «|», и кабинет второй подгруппы не показывался вовсе.
        """
        части = self.split_parts()
        if not части:
            return f"  {self.number}. урок"
        заголовок = f"  {self.number}."
        if self.time_str():
            заголовок += f" {self.time_str()}"
        строки = [f"{заголовок}  {части[0].subject}"]
        хвост = " · ".join(p for p in (аудитория(части[0].room), части[0].teacher) if p)
        if хвост:
            строки.append(f"     {хвост}")
        for часть in части[1:]:
            строка = f"     {часть.subject}"
            добавка = " · ".join(p for p in (аудитория(часть.room), часть.teacher) if p)
            if добавка:
                строка += f" · {добавка}"
            строки.append(строка)
        return "\n".join(строки)


def аудитория(значение: str) -> str:
    """«305-1» -> «ауд. 305-1». Без пометки число неотличимо от времени."""
    значение = str(значение or "").strip()
    return f"ауд. {значение}" if значение else ""


@dataclass
class DaySchedule:
    weekday: int                   # 0 — понедельник … 5 — суббота
    lessons: list[Lesson] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lessons


@dataclass
class GroupSchedule:
    group: str                     # канонический код группы: «24-21(2С)»
    days: dict[int, DaySchedule] = field(default_factory=dict)
    # понедельник недели, на которую составлен этот PDF. Без него шапка
    # «Неделя с …» считалась от сегодняшнего дня и на стыке недель врала.
    week: date | None = None

    @property
    def lessons_count(self) -> int:
        return sum(len(day.lessons) for day in self.days.values())

    def sorted_days(self) -> list[DaySchedule]:
        return [self.days[key] for key in sorted(self.days)]

    def day(self, weekday: int) -> DaySchedule | None:
        return self.days.get(weekday)

    def upcoming(self, now: datetime | None = None, limit: int = 3) -> list[tuple[DaySchedule, Lesson, date]]:
        """Ближайшие занятия: [(день, пара, дата)] начиная с текущего момента.

        Расписание недельное, поэтому урок «вторника 10:50» после обеда
        вторника показывается со следующего вторника.
        """
        moment = now or clock.now()
        today = moment.date()
        weekday_now = today.weekday()
        result: list[tuple[DaySchedule, Lesson, date]] = []
        for day in self.sorted_days():
            shift = (day.weekday - weekday_now) % 7
            day_date = today + timedelta(days=shift)
            for lesson in day.lessons:
                if not lesson.start:
                    continue
                lesson_dt = datetime.combine(day_date, _time(lesson.start))
                if lesson_dt <= moment:
                    lesson_dt += timedelta(days=7)
                result.append((day, lesson, lesson_dt.date()))
        result.sort(key=lambda item: item[2])
        return result[:limit]


def _time(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour or 0), minute=int(minute or 0))


def normalize_times(raw) -> tuple[tuple[str, str], ...]:
    """Звонки приводятся к плоскому списку по номерам уроков: [(начало, конец), ...].

    Принимает три вида настройки, потому что формат менялся:
    * по урокам  — {"1": ["08:00", "08:45"], "2": ["08:50", "09:35"]} (номер — урок);
    * по парам   — {"1": [["08:00", "08:45"], ["08:50", "09:35"]]} (у пары два урока,
      номера уроков проставляются подряд: пара 1 → уроки 1 и 2, пара 2 → 3 и 4);
    * старое     — {"1": ["09:00", "09:45"]} без списков: тоже номер урока.
    """
    by_lesson: dict[int, tuple[str, str]] = {}
    pairs_only: dict[int, list] = {}
    for number, value in (raw or {}).items():
        if not str(number).strip().isdigit() or not isinstance(value, (list, tuple)):
            continue
        if all(_looks_like_time(item) for item in value):  # {"N": ["09:00", "09:45"]}
            if len(value) == 2:
                by_lesson[int(number)] = (as_str(value[0]).strip(), as_str(value[1]).strip())
            continue
        lessons = []
        for lesson in value:
            if isinstance(lesson, (list, tuple)) and len(lesson) == 2 and all(_looks_like_time(item) for item in lesson):
                lessons.append((as_str(lesson[0]).strip(), as_str(lesson[1]).strip()))
        if lessons:
            pairs_only[int(number)] = lessons
    if not by_lesson:  # раскладка по парам: разворачиваем в номера уроков подряд
        index = 1
        for number in sorted(pairs_only):
            for lesson in pairs_only[number]:
                by_lesson[index] = lesson
                index += 1
    if not by_lesson:
        return LESSON_TIMES
    size = max(by_lesson)
    return tuple(by_lesson.get(number, ("", "")) for number in range(1, size + 1))


def _looks_like_time(value) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", as_str(value).strip()))


def apply_lesson_times(schedule: GroupSchedule, times=None) -> None:
    """Проставляет занятиям время из звонков по номеру урока.

    Номер в PDF — это урок (в паре их два), поэтому время берётся по номеру
    напрямую. Номера, для которых времени нет, остаются без времени (видно в панели).
    """
    table = tuple(times) if times else LESSON_TIMES
    for day in schedule.days.values():
        for lesson in day.lessons:
            index = lesson.number - 1
            lesson.start, lesson.end = table[index] if 0 <= index < len(table) and table[index][0] else ("", "")


# ── разбор страниц PDF ────────────────────────────────────────────────────────
def group_names(header: list) -> dict[int, str]:
    """Коды групп из строки шапки: {индекс колонки: «24-21(2С)»}."""
    groups: dict[int, str] = {}
    for index, cell in enumerate(header or []):
        text = clean(cell)
        if not text or text == "№" or "предмет" in text.lower():
            continue
        compact = norm(text)
        if _GROUP_RE.fullmatch(compact):
            groups[index] = text
    return groups


def room_columns(table: list) -> dict[int, int]:
    """Где в таблице лежит аудитория каждой группы: {колонка группы: колонка ауд.}

    У колледжа колонки идут парами «предмет | ауд.», поэтому аудитория группы
    стоит сразу справа от её предмета, а не после последней группы в файле.
    Сопоставляем по порядку: в шапке коды групп идут слева направо, и столько
    же раз в шапке встречается «Ауд.». Если «Ауд.» меньше, чем групп (старый
    файл или таблица с одной группой), берём единственную колонку справа.
    """
    groups = sorted(group_names(table[0] if table else []))
    auds = sorted({index for row in table[:2] for index, cell in enumerate(row)
                   if clean(cell).lower().startswith("ауд")})
    result: dict[int, int] = {}
    for position, column in enumerate(groups):
        result[column] = auds[position] if position < len(auds) else column + 1
    return result


def _is_teacher(value: str) -> bool:
    """Похоже ли значение на строку «Иванова А. А.» или «Ivanova A. A.»."""
    text = clean(value)
    if not text or len(text) > 60 or not re.search(r"\b[А-ЯЁA-Z]\.", text):
        return False
    words = text.split()
    return bool(words) and all(word[:1].isupper() for word in words)


def parse_cell_parts(text: str) -> list[tuple[str, str]]:
    """Ячейка предмета -> список (предмет, преподаватель) по подгруппам.

    Обычная ячейка: «МДК.03.01 ОПНиГ\n(лекция)\nКрылова В. И.» -> одна часть.
    Подгрупповая: «1.УстрЭкспСосудов (лаб)\nНуриева С. Р.\n2.…(лаб)\nКрылова В. И.»
    -> две части, у каждой свой преподаватель. Их склеивать нельзя: кабинеты
    приходят из соседней колонки отдельными строками и должны попасть в свою
    часть, иначе вторая подгруппа теряет и кабинет, и преподавателя.
    """
    lines = [clean(line) for line in str(text or "").splitlines() if clean(line)]
    if not lines:
        return []

    merged: list[str] = []
    for line in lines:
        previous_is_teacher = bool(merged and _is_teacher(merged[-1]))
        # Новый блок начинают только префикс подгруппы, строка преподавателя и
        # всё, что идёт после преподавателя. Строка вида занятия «(лекция)»
        # продолжает текущий блок: это часть того же предмета. Раньше «(»
        # тоже начинала блок, и подгрупповая ячейка рвалась на четыре части
        # («1.Предмет | (лаб) | 2.Предмет | (лаб)»), из-за чего кабинеты и
        # преподаватели попадали не к своей подгруппе.
        starts_block = bool(_SUBGROUP_RE.match(line)) or _is_teacher(line) or previous_is_teacher
        if merged and not starts_block:
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)

    blocks: list[list[str]] = []
    for line in merged:
        if _SUBGROUP_RE.match(line) and blocks:
            blocks.append([line])
        elif blocks and _is_teacher(line):
            blocks[-1].append(line)
        else:
            blocks.append([line])

    части: list[tuple[str, str]] = []
    for block in blocks:
        body = list(block)
        преподаватель = ""
        if body and _is_teacher(body[-1]):
            преподаватель = clean(" ".join(body.pop().split()))
        предмет = clean(" ".join(body))
        if предмет:
            части.append((предмет, преподаватель))
    return части


def parse_cell(text: str) -> tuple[str, str]:
    """Ячейка -> склеенные предмет и преподаватели. Прежняя форма, для проверок."""
    части = parse_cell_parts(text)
    if not части:
        return "", ""
    пометка = "(подгруппы) " if len(части) > 1 else ""
    предметы = [номер_подгруппы(предмет) for предмет, _ in части]
    преподаватели = [преподаватель for _, преподаватель in части if преподаватель]
    return пометка + " | ".join(предметы), "; ".join(преподаватели)


def номер_подгруппы(предмет: str) -> str:
    """«1.МДК.03.01 ТРТОУ» -> «1. МДК.03.01 ТРТОУ»: после номера ставим пробел."""
    match = re.match(r"^\s*([12])\s*[.)]\s*(\S.*)$", str(предмет or ""))
    return f"{match.group(1)}. {clean(match.group(2))}" if match else предмет

def parse_rooms(value: str) -> list[str]:
    """Аудитории из соседней ячейки: по одной на подгруппу.

    Обычная пара - одна строка: «204». Подгрупповая - по строке на подгруппу:
    «РММ» и «313-1» в одной ячейке. Раньше брался только первый кусок, и
    кабинет второй подгруппы пропадал.
    """
    text = clean(value).replace("\n", " ")
    if not text:
        return []
    куски = [кусок.strip() for кусок in str(value or "").split("\n")]
    аудитории: list[str] = []
    for кусок in куски:
        кусок = кусок.strip()
        if not кусок:
            continue
        # Перенос внутри названия кабинета. В PDF «Спорт зал» и «Гор. зал»
        # разбиты по строкам, и это один кабинет, а не два. Признак переноса -
        # кусок со строчной буквы: настоящий кабинет начинается с цифры или с
        # заглавной («РММ», «313-1», «Спорт»). Без этого правила вторая
        # подгруппа получала вместо кабинета слово «зал», а в базе 18 строк
        # с «Спорт» - то есть это не редкий случай.
        if аудитории and кусок[:1].islower():
            аудитории[-1] = f"{аудитории[-1]} {кусок}"
            continue
        если = _ROOM_RE.fullmatch(кусок) or re.match(r"^\d+[\w/\-.]*$", кусок)
        аудитории.append(кусок if если else (кусок.split()[0] if len(кусок.split()[0]) <= 12 else ""))
    return [аудитория for аудитория in аудитории if аудитория]


def parse_room(value: str) -> str:
    """Первая аудитория ячейки. Прежняя форма, от неё зависят проверки."""
    аудитории = parse_rooms(value)
    return аудитории[0] if аудитории else ""
def weekday_of_page(header_text: str) -> int | None:
    """День недели по заголовку страницы «День - Понедельник, 28.09.2026»."""
    first_line = clean(str(header_text or "").splitlines()[0] if header_text else "").lower()
    best: tuple[int, int] | None = None
    for alias, index in _DAY_ALIASES.items():
        position = first_line.find(alias)
        if position >= 0 and (best is None or position < best[0]):
            best = (position, index)
    return best[1] if best else None


def date_of_page(header_text: str) -> date | None:
    """Дата из заголовка страницы «День - Понедельник, 28.09.2026»."""
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", str(header_text or "")[:200])
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def week_of_pages(pages: list[dict]) -> date | None:
    """Понедельник недели PDF: берём самую раннюю дату из заголовков."""
    dates = [found for page in pages or []
             if (found := date_of_page(page.get("text", "")))]
    if not dates:
        return None
    monday = min(dates)
    return monday - timedelta(days=monday.weekday())


def build_schedule(pages: list[dict], group: str, times: dict | None = None) -> GroupSchedule:
    """Собирает расписание группы из страниц: pages = [{'text': str, 'tables': [[row, ...]]}].

    Страниц может быть сколько угодно: обычно одна страница = один день недели.
    Колонки ищутся в самой длинной таблице страницы, чтобы не разбирать
    «мусорные» таблицы вроде шапок и подвалов. times — звонки (см. LESSON_TIMES).
    """
    wanted = norm(group)
    result = GroupSchedule(group=group, week=week_of_pages(pages))
    for page in pages or []:
        weekday = weekday_of_page(page.get("text", ""))
        tables = [table for table in (page.get("tables") or []) if table]
        if weekday is None or not tables:
            continue
        table = max(tables, key=len)
        columns = [col for col, name in group_names(table[0] if table else []).items()
                   if wanted == norm(name) or wanted in norm(name)]
        if not columns:
            continue
        column = columns[0]
        # Аудитория — колонка «Ауд.» именно этой группы: в файле колледжа колонки
        # идут парами «предмет | ауд.», и аудитория соседней группы — это вовсе
        # не наша (так в базу попадала чужая аудитория, а своя терялась).
        room_column = room_columns(table).get(column, column + 1)
        day = DaySchedule(weekday=weekday)
        # первую строку пропускаем (это шапка с кодами групп). Строки, где номер
        # пары не читается, отсеются сами — так переживаются двухуровневые шапки.
        for row in table[1:]:
            if not row or column >= len(row):
                continue
            number = _lesson_number(row[0])
            if number is None:
                continue
            части = parse_cell_parts(row[column])
            if not части:
                continue
            if 0 <= room_column < len(row):
                аудитории = parse_rooms(row[room_column])
            else:
                аудитории = []
            # Кабинетов столько же, сколько подгрупп: ставим один к одному.
            # Один кабинет - он у всех. Кабинетов больше, чем подгрупп, - лишние
            # не наши (колонка соседней группы), их не показываем.
            if len(аудитории) == len(части):
                свои = аудитории
            elif len(аудитории) == 1:
                свои = аудитории * len(части)
            else:
                свои = list(аудитории)[:len(части)]
            собранные = [LessonPart(subject=предмет, teacher=преподаватель,
                                    room=свои[index] if index < len(свои) else "")
                        for index, (предмет, преподаватель) in enumerate(части)]
            day.lessons.append(Lesson(
                number=number,
                subject=" | ".join(p.subject for p in собранные),
                teacher="; ".join(p.teacher for p in собранные if p.teacher),
                room=" / ".join(p.room for p in собранные if p.room),
                parts=собранные))
            result.days[weekday] = day
    apply_lesson_times(result, times)
    return result


def _lesson_number(value) -> int | None:
    """Номер пары из ячейки: «1», «1 пара», «1.», «I» (римские)."""
    text = clean(value)
    if not text:
        return None
    match = _LESSON_NUM_RE.match(text)
    if match:
        number = int(match.group(1))
        return number if 1 <= number <= MAX_LESSONS_PER_DAY else None
    match = re.match(r"^(\d{1,2})\s*(?:пар[ауы]|пара)?\b", text, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        return number if 1 <= number <= MAX_LESSONS_PER_DAY else None
    if _ROMAN_RE.fullmatch(text):
        romans = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}
        return romans.get(text.upper())
    return None


def groups_in_pages(pages: list[dict]) -> list[str]:
    """Все коды групп, встречающиеся в шапках таблиц страниц."""
    found: list[str] = []
    for page in pages or []:
        for table in page.get("tables") or []:
            for name in group_names(table[0] if table else []).values():
                if norm(name) not in {norm(item) for item in found}:
                    found.append(name)
    return found


def match_groups(query: str, available: list[str]) -> list[str]:
    """Подходящие группы для запроса: «2421» → «24-21(2С)», «ис 21» → «ИС-21».

    Сравнение идёт по строке без пробелов, точек, скобок и дефисов, регистр не важен.
    """
    wanted = _compact(query)
    if not wanted:
        return []
    exact, partial = [], []
    for name in available:
        compact = _compact(name)
        if compact == wanted:
            exact.append(name)
        elif wanted in compact or compact.startswith(wanted):
            partial.append(name)
    return exact or partial


def _compact(value) -> str:
    """«ИС-21» → «ИС21», «24-21(2С)» → «24-212С»: слитно, без знаков препинания."""
    return re.sub(r"[\s_./()\-–]+", "", str(value or "")).upper()


# ── выдача текстом ───────────────────────────────────────────────────────────
def day_title(weekday: int, day_date: date | None = None) -> str:
    name = WEEKDAYS_FULL[weekday].capitalize()
    if day_date is None:
        return name
    today = clock.today()
    if day_date == today:
        return f"{name}, сегодня"
    if day_date == today + timedelta(days=1):
        return f"{name}, завтра"
    return f"{name} {day_date:%d.%m}"


def format_day(day: DaySchedule, day_date: date | None = None, today: bool = False) -> str:
    """День расписания текстом.

    Между парами встаёт пустая строка. Пара - это два урока, а последние три
    пары по официальному графику (уроки 9, 10 и 11) состоят из одного урока каждая,
    поэтому пустая строка ставится перед началом пары. Перед первым уроком и после
    последнего пустой строки нет.
    Сборка дня живёт здесь одной функцией, её зовёт бот (handlers/menus.py —
    «расписание на сегодня» и «на день», handlers/admin.py — день в карточке).
    """
    # Пометка «сегодня» приходит из day_title («Понедельник, сегодня»).
    # Раньше сюда же добавляли «• сегодня» - на экране выходило «Среда, сегодня
    # • сегодня», дважды. Одной пометки достаточно.
    lines = [f"📅 {day_title(day.weekday, day_date)}"]
    previous = 0
    for lesson in day.lessons:
        # Пустая строка - перед началом новой пары, а не перед нечётным уроком.
        # Уроки 9, 10 и 11 - это пары 5, 6 и 7 по официальному графику, так
        # что перед 10-м, который чётный, пустая строка тоже нужна: иначе 5-я
        # и 6-я пары шли слитно, хотя звонки у них разные.
        if previous and pair_of_lesson(lesson.number) != pair_of_lesson(previous):
            lines.append("")
        lines.append("  " + lesson.line())
        previous = lesson.number
    return "\n".join(lines)


def today_monday() -> date:
    """Понедельник текущей недели - точка отсчёта для «свежести» файла."""
    moment = clock.today()
    return moment - timedelta(days=moment.weekday())


def format_schedule(schedule: GroupSchedule, week: date | None = None, only: list[int] | None = None) -> str:
    """Расписание на неделю (или по дням only) в виде текста для MAX."""
    if not schedule.days:
        return ""
    # Неделя берётся из самого PDF: колледж выкладывает файл на конкретную
    # неделю, и шапка обязана совпадать с ним, а не с текущим днём.
    monday = week or schedule.week or today_monday()
    days = [schedule.days[key] for key in sorted(schedule.days) if only is None or key in only]
    lines = [f"📚 Расписание группы {schedule.group}"]
    if only is None:
        lines.append(f"Неделя с {monday:%d.%m.%Y}")
        stale = today_monday() - monday
        if stale.days >= 7:
            lines.append(f"⚠️ Файл от {monday:%d.%m.%Y} — свежее расписание колледж "
                         f"ещё не выложил ({stale.days} дн. назад).")
    for day in days:
        if day.is_empty:
            continue
        day_date = monday + timedelta(days=day.weekday)
        lines.append("")
        lines.append(format_day(day, day_date, today=day_date == clock.today()))
    lines.append("")
    звонки = ", ".join(f"{номер} — {pair_time(номер)}"
                      for номер in range(1, PAIR_COUNT + 1) if pair_time(номер))
    lines.append("")
    lines.append(f"Пары по звонкам: {звонки}")
    return "\n".join(lines)


def _bells_line(number: int, start: str = "", end: str = "") -> str:
    """Строка звонков одной пары: «1 — 08:00–09:35».

    Раньше сюда передавался номер УРОКА, а внутри считалось 2 * number - то есть
    уроки путались с парами, и строка выходила мусорной. Теперь номер пары
    приходит от звонков PAIR_SPANS, а время берётся из таблицы.
    """
    время = pair_time(number)
    return f"{number} — {время}" if время else ""

def format_upcoming(schedule: GroupSchedule, limit: int = 3) -> str:
    """Ближайшие занятия — отвечает на вопрос «а когда у меня следующая пара?»."""
    items = schedule.upcoming(limit=limit)
    if not items:
        return ""
    lines = ["⏰ Ближайшие занятия"]
    for day, lesson, day_date in items:
        lines.append(f"{day_date:%d.%m.%Y} · {WEEKDAYS_FULL[day.weekday][:2]} · {lesson.line()}")
    return "\n".join(lines)


# ── слой PDF (единственное место, где нужен pdfplumber) ──────────────────────
def extract_pages(data: bytes, max_pages: int = 14) -> list[dict]:
    """Текст и таблицы каждой страницы PDF. Ошибки pdfplumber не роняют разбор.

    Таблицы ищутся сначала по линиям рамок (так надёжнее разбираются настоящие
    файлы колледжа), а если рамок нет — по пробелам между колонками: часть
    расписаний выкладывают простым текстом.
    """
    import io

    import pdfplumber

    pages: list[dict] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 — битая страница не должна ломать весь файл
                text = ""
            pages.append({"text": text, "tables": _page_tables(page)})
    return pages


def _page_tables(page) -> list:
    """Таблицы страницы: сначала по рамкам, при неудаче — по колонкам пробелов.

    Стратегия «lines» на PDF без рамок возвращает вырожденную таблицу в одну
    колонку, поэтому результаты сравниваются: берём тот, где колонок больше.
    """

    best: list = []
    best_columns = 0
    for settings in (None, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
        try:
            tables = page.extract_tables(table_settings=settings) or []
        except Exception:  # noqa: BLE001
            tables = []
        columns = max((len(row) for row in tables[0] if row), default=0) if tables else 0
        if columns > best_columns:
            best, best_columns = tables, columns
        if best_columns >= 2:  # колонки нашлись — стратегия по рамкам годится
            break
    return best


def parse_pdf_bytes(data: bytes, group: str, times: dict | None = None) -> tuple[GroupSchedule, list[dict]]:
    """Разбирает PDF и возвращает расписание группы вместе с извлечёнными страницами."""
    pages = extract_pages(data)
    return build_schedule(pages, group, times), pages
