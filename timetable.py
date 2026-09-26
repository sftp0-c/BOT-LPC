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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from utils import as_str, short

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
class Lesson:
    number: int                    # номер пары в дне
    subject: str                   # предмет и вид занятия
    teacher: str = ""              # преподаватель
    room: str = ""                 # аудитория
    start: str = ""                # «09:00» из звонков
    end: str = ""                  # «09:45»

    def time_str(self) -> str:
        return f"{self.start}–{self.end}" if self.start and self.end else ""

    def line(self) -> str:
        """Строка урока: «2 урок · 08:50–09:35 · Математика (лекция)», ниже — аудитория и преподаватель."""
        head = f"{self.number} урок"
        if self.time_str():
            head += f" · 🕐 {self.time_str()}"
        head += f" · {self.subject}"
        tail = " · ".join(part for part in (f"ауд. {self.room}" if self.room else "", self.teacher) if part)
        return f"  {head}\n     {tail}" if tail else f"  {head}"


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
        moment = now or datetime.now()
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


def _is_teacher(value: str) -> bool:
    """Похоже ли значение на строку «Иванова А. А.» или «Ivanova A. A.»."""
    text = clean(value)
    if not text or len(text) > 60 or not re.search(r"\b[А-ЯЁA-Z]\.", text):
        return False
    words = text.split()
    return bool(words) and all(word[:1].isupper() for word in words)


def parse_cell(text: str) -> tuple[str, str]:
    """Ячейка предмета -> (предмет вместе с видом занятия, преподаватели).

    Обычная ячейка: «МДК.03.01 ОПНиГ\\n(лекция)\\nКрылова В. И.».
    Подгрупповая: «1.УстрЭкспСосудов (лаб)\\nНуриева С. Р.\\n2.…(лаб)\\nКрылова В. И.» —
    подпары склеиваются через «|», в тем�� идёт пометка про подгруппы.
    """
    lines = [clean(line) for line in str(text or "").splitlines() if clean(line)]
    if not lines:
        return "", ""

    merged: list[str] = []
    for line in lines:
        previous_is_teacher = bool(merged and _is_teacher(merged[-1]))
        starts_block = bool(_SUBGROUP_RE.match(line)) or line.startswith("(") or _is_teacher(line) or previous_is_teacher
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

    subjects: list[str] = []
    teachers: list[str] = []
    for block in blocks:
        body = list(block)
        if body and _is_teacher(body[-1]):
            teachers.append(body.pop())
        subject = clean(" ".join(body))
        if subject:
            subjects.append(subject)
    has_subgroups = any(_SUBGROUP_RE.match(block[0]) for block in blocks if block)
    prefix = "(подгруппы) " if has_subgroups else ""
    return prefix + " | ".join(subjects), "; ".join(teachers)


def parse_room(value: str) -> str:
    """Аудитория из соседней ячейки: «204», «214/215», «спортзал»."""
    text = clean(value).replace("\n", " ")
    if not text:
        return ""
    if _ROOM_RE.fullmatch(text) or re.match(r"^\d+[\w/\-.]*$", text):
        return text
    first = text.split()[0]
    return first if len(first) <= 12 else ""


def weekday_of_page(header_text: str) -> int | None:
    """День недели по заголовку страницы «День - Понедельник, 28.09.2026»."""
    first_line = clean(str(header_text or "").splitlines()[0] if header_text else "").lower()
    best: tuple[int, int] | None = None
    for alias, index in _DAY_ALIASES.items():
        position = first_line.find(alias)
        if position >= 0 and (best is None or position < best[0]):
            best = (position, index)
    return best[1] if best else None


def build_schedule(pages: list[dict], group: str, times: dict | None = None) -> GroupSchedule:
    """Собирает расписание группы из страниц: pages = [{'text': str, 'tables': [[row, ...]]}].

    Страниц может быть сколько угодно: обычно одна страница = один день недели.
    Колонки ищутся в самой длинной таблице страницы, чтобы не разбирать
    «мусорные» таблицы вроде шапок и подвалов. times — звонки (см. LESSON_TIMES).
    """
    wanted = norm(group)
    result = GroupSchedule(group=group)
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
        # Аудитория — колонка сразу после последней группы: когда групп несколько,
        # брать «ячейку справа» от своей группы нельзя — там чужая колонка.
        group_columns = list(group_names(table[0] if table else []))
        room_column = (max(group_columns) + 1) if group_columns else column + 1
        day = DaySchedule(weekday=weekday)
        # первую строку пропускаем (это шапка с кодами групп). Строки, где номер
        # пары не читается, отсеются сами — так переживаются двухуровневые шапки.
        for row in table[1:]:
            if not row or column >= len(row):
                continue
            number = _lesson_number(row[0])
            if number is None:
                continue
            subject, teacher = parse_cell(row[column])
            if not subject:
                continue
            room = parse_room(row[room_column]) if 0 <= room_column < len(row) else ""
            day.lessons.append(Lesson(number=number, subject=subject, teacher=teacher, room=room))
        day.lessons.sort(key=lambda lesson: lesson.number)
        if day.lessons and not day.is_empty:
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
    if day_date == date.today():
        return f"{name}, сегодня"
    if day_date == date.today() + timedelta(days=1):
        return f"{name}, завтра"
    return f"{name} {day_date:%d.%m}"


def format_day(day: DaySchedule, day_date: date | None = None) -> str:
    lines = [f"📅 {day_title(day.weekday, day_date)}"]
    lines += ["  " + lesson.line() for lesson in day.lessons]
    return "\n".join(lines)


def format_schedule(schedule: GroupSchedule, week: date | None = None, only: list[int] | None = None) -> str:
    """Расписание на неделю (или по дням only) в виде текста для MAX."""
    if not schedule.days:
        return ""
    monday = week or (date.today() - timedelta(days=date.today().weekday()))
    days = [schedule.days[key] for key in sorted(schedule.days) if only is None or key in only]
    lines = [f"📚 Расписание группы {schedule.group}"]
    if only is None:
        lines.append(f"Неделя с {monday:%d.%m.%Y}")
    for day in days:
        if day.is_empty:
            continue
        day_date = monday + timedelta(days=day.weekday)
        lines.append("")
        lines.append(format_day(day, day_date))
    lines.append("")
    lines.append(f"Пары: {short(', '.join(_bells_line(number, start, end) for number, (start, end) in enumerate(LESSON_TIMES, start=1)), 150)}")
    return "\n".join(lines)


def _bells_line(number: int, start: str, end: str) -> str:
    """Строка звонков: пара и её уроки. Уроки нумеруются подряд, пара — через каждые два."""
    second = 2 * number
    times = f"{start}–{end}"
    if second <= len(LESSON_TIMES):
        times += f" / {LESSON_TIMES[second - 1][0]}–{LESSON_TIMES[second - 1][1]}"
    return f"{number} — {times}"


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
