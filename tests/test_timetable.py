"""Расписание: разбор PDF в занятия и выдача текстом.

Разбор проверяется на двух уровнях. Первый — настоящий PDF, собранный
tests/pdf_helper.py: из него pdfplumber реально извлекает текст и таблицы.
Второй — страницы с кириллицей, подставленные напрямую в build_schedule():
тестовый PDF собирается шрифтом без кириллицы, а весь разбор текста живёт именно
в build_schedule().
"""
from datetime import date, datetime, timedelta

import pytest

import timetable as tt
from pdf_helper import make_grid_pdf, sample_week_latin

# ── настоящий PDF ─────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def latin_pages() -> list:
    return tt.extract_pages(make_grid_pdf(sample_week_latin()))


def test_real_pdf_gives_text_and_table(latin_pages):
    assert len(latin_pages) == 2
    assert "Monday" in (latin_pages[0]["text"] or "")
    rows = latin_pages[0]["tables"][0]
    assert rows[0][:2] == ["#", "IS-21"]
    assert rows[1][0] == "1" and "Math" in rows[1][1] and rows[1][2] == "204"


def test_real_pdf_is_parsed_into_lessons(latin_pages):
    pages = [
        {"text": "День - Понедельник, 28.09.2026", "tables": latin_pages[0]["tables"]},
        {"text": "День - Среда, 30.09.2026", "tables": latin_pages[1]["tables"]},
    ]
    schedule = tt.build_schedule(pages, "IS-21")
    assert sorted(schedule.days) == [0, 2]
    monday = schedule.day(0)
    assert [lesson.number for lesson in monday.lessons] == [1, 3]
    assert monday.lessons[0].subject == "Math | (lec)"
    assert monday.lessons[0].teacher == "Ivanova A. A."
    assert monday.lessons[0].room == "204"
    assert monday.lessons[0].time_str() == "08:00–08:45"
    assert monday.lessons[1].time_str() == "09:50–10:35"  # 3-й урок = 1-й урок 2-й пары


def test_groups_are_found_in_real_pdf(latin_pages):
    assert tt.groups_in_pages(latin_pages) == ["IS-21"]


# ── разбор страниц с кириллицей ───────────────────────────────────────────────
def cyrillic_week() -> list:
    """Страницы в том же виде, что отдаёт pdfplumber: tables — список таблиц."""
    return [
        {"text": "День - Понедельник, 28.09.2026", "tables": [[
            ["№", "ИС-21", "ИС-22", "ауд."],
            ["1", "Математика\n(лекция)\nИванова А. А.", "Физика\n(лаб)\nПетров П. П.", "204", "217"],
            ["3", "Физика\n(лаб)\nПетров П. П.", "Информатика\n(практ)\nСидоров С. С.", "217", "101"],
        ]]},
        {"text": "День - Четверг, 01.10.2026", "tables": [[
            ["№", "ИС-21", "ИС-22", "ауд."],
            ["2", "История\n(лекция)\nСидорова М. М.", "Математика", "101", "204"],
        ]]},
    ]


def test_build_schedule_reads_cyrillic_table():
    schedule = tt.build_schedule(cyrillic_week(), "ИС-21")
    assert sorted(schedule.days) == [0, 3]
    monday = schedule.day(0)
    assert monday.lessons[0].subject == "Математика | (лекция)"
    assert monday.lessons[0].teacher == "Иванова А. А."
    assert monday.lessons[0].room == "204"
    assert monday.lessons[1].number == 3 and monday.lessons[1].room == "217"
    thursday = schedule.day(3)
    assert thursday.lessons[0].subject == "История | (лекция)"


def test_build_schedule_ignores_day_without_our_group():
    pages = [{"text": "День - Пятница, 03.10.2026", "tables": [
        ["№", "БУХ-20", "ауд."],
        ["1", "Бухучёт", "301"],
    ]}]
    assert tt.build_schedule(pages, "ИС-21").days == {}


def test_subgroups_are_marked():
    pages = [{"text": "День - Понедельник, 28.09.2026", "tables": [[
        ["№", "ИС-21", "ауд."],
        ["4", "1.Практика\nСидоров С. С.\n2.Практика\nПетров П. П.", "101"],
    ]]}]
    lesson = tt.build_schedule(pages, "ИС-21").day(0).lessons[0]
    assert lesson.subject.startswith("(подгруппы)")
    assert "Сидоров" in lesson.teacher and "Петров" in lesson.teacher


def test_room_without_russian_lookalike():
    assert tt.parse_room("204") == "204"
    assert tt.parse_room("214/215") == "214/215"
    assert tt.parse_room("спортзал") == "спортзал"
    assert tt.parse_room("") == ""
    assert tt.parse_room("каф. 3, ауд. 204") == "каф."  # берём первое слово, лишнее не показываем


def test_weekday_detects_day_from_header():
    assert tt.weekday_of_page("День - Понедельник, 28.09.2026") == 0
    assert tt.weekday_of_page("День - Среда, 30.09.2026") == 2  # обрывок слова
    assert tt.weekday_of_page("Расписание") is None
    assert tt.weekday_of_page("") is None


def test_group_names_skip_non_groups():
    header = ["№", "ИС-21", "24-21(2С)", "Предмет", "", "24-30"]
    assert tt.group_names(header) == {1: "ИС-21", 2: "24-21(2С)", 5: "24-30"}


def test_match_groups_tolerates_spelling():
    available = ["ИС-21", "24-21(2С)", "БУХ-20"]
    assert tt.match_groups("ис-21", available) == ["ИС-21"]
    assert tt.match_groups("ИС 21", available) == ["ИС-21"]
    assert tt.match_groups("ис21", available) == ["ИС-21"]
    assert tt.match_groups("2421", available) == ["24-21(2С)"]
    assert tt.match_groups("242", available) == ["24-21(2С)"]
    assert tt.match_groups("нетакой", available) == []


def test_lesson_numbers_accept_roman_and_words():
    pages = [{"text": "День - Понедельник, 28.09.2026", "tables": [[
        ["№", "ИС-21", "ауд."],
        ["1 пара", "Физика", "101"],
        ["II", "Химия", "102"],
        ["3.", "Математика", "103"],
    ]]}]
    numbers = [lesson.number for lesson in tt.build_schedule(pages, "ИС-21").day(0).lessons]
    assert numbers == [1, 2, 3]


# ── выдача текстом ────────────────────────────────────────────────────────────
def test_format_day_and_week():
    schedule = tt.build_schedule(cyrillic_week(), "ИС-21")
    monday = tt.format_day(schedule.day(0))
    assert monday.startswith("📅 Понедельник")
    assert "1 урок · 🕐 08:00–08:45 · Математика | (лекция)" in monday
    assert "ауд. 204 · Иванова А. А." in monday

    week = tt.format_schedule(schedule)
    assert "📚 Расписание группы ИС-21" in week
    assert "Понедельник" in week and "Четверг" in week
    assert week.index("Понедельник") < week.index("Четверг")


def test_format_marks_today_and_tomorrow():
    today = date.today()
    schedule = tt.GroupSchedule(group="ИС-21", days={
        today.weekday(): tt.DaySchedule(weekday=today.weekday(), lessons=[tt.Lesson(1, "Математика")]),
        (today.weekday() + 1) % 7: tt.DaySchedule(
            weekday=(today.weekday() + 1) % 7, lessons=[tt.Lesson(2, "Физика")]),
    })
    text = tt.format_schedule(schedule, week=today - timedelta(days=today.weekday()))
    assert "сегодня" in text and "завтра" in text


def test_upcoming_rolls_past_lesson_to_next_week():
    # понедельник, сейчас 10:00: первая пара прошла и должна показаться в следующий понедельник
    now = datetime(2026, 9, 28, 10, 0)
    schedule = tt.GroupSchedule(group="ИС-21", days={
        0: tt.DaySchedule(weekday=0, lessons=[
            tt.Lesson(1, "Математика", start="08:00", end="08:45"),
            tt.Lesson(3, "Физика", start="12:00", end="12:45"),
        ]),
    })
    items = schedule.upcoming(now)
    assert [(lesson.subject, day_date) for _, lesson, day_date in items] == [
        ("Физика", date(2026, 9, 28)),
        ("Математика", date(2026, 10, 5)),
    ]


def test_format_upcoming_mentions_date():
    schedule = tt.GroupSchedule(group="ИС-21", days={
        0: tt.DaySchedule(weekday=0, lessons=[tt.Lesson(1, "Математика", start="08:00", end="08:45")]),
    })
    text = tt.format_upcoming(schedule)
    assert "Ближайшие занятия" in text and "Математика" in text
    assert "08:00–08:45" in text


def test_lesson_numbers_get_times_in_order():
    """Номер в PDF — это урок: 1 и 2 уроки — первая пара, 3 и 4 — вторая."""
    pages = [{"text": "День - Понедельник, 28.09.2026", "tables": [[
        ["№", "ИС-21", "ауд."],
        ["1", "Математика", "204"],
        ["2", "Физика", "217"],
        ["3", "История", "101"],
        ["4", "Информатика", "102"],
    ]]}]
    monday = tt.build_schedule(pages, "ИС-21").day(0)
    assert [lesson.time_str() for lesson in monday.lessons] == [
        "08:00–08:45", "08:50–09:35", "09:50–10:35", "10:55–11:40",
    ]


def test_lesson_times_setting_replaces_defaults():
    """Явно переданные звонки полностью заменяют умолчания для разбора."""
    schedule = tt.build_schedule(cyrillic_week(), "ИС-21")
    tt.apply_lesson_times(schedule, (("07:00", "07:45"),))
    assert schedule.day(0).lessons[0].time_str() == "07:00–07:45"
    assert schedule.day(0).lessons[1].time_str() == ""  # урока 3 в переданных звонках нет


def test_normalize_times_expands_pair_format():
    assert tt.normalize_times({"1": [["08:00", "08:45"], ["08:50", "09:35"]]}) == (
        ("08:00", "08:45"), ("08:50", "09:35"),
    )
    assert tt.normalize_times({"1": ["08:00", "08:45"], "3": ["09:50", "10:35"]}) == (
        ("08:00", "08:45"), ("", ""), ("09:50", "10:35"),
    )
    assert tt.normalize_times({"мусор": 1}) == tt.LESSON_TIMES


def test_default_bells_match_college_schedule():
    assert tt.LESSON_TIMES[0] == ("08:00", "08:45")
    assert tt.LESSON_TIMES[1] == ("08:50", "09:35")
    assert tt.LESSON_TIMES[2] == ("09:50", "10:35")
    assert tt.LESSON_TIMES[4] == ("12:00", "12:45")
    assert tt.LESSON_TIMES[8] == ("15:45", "17:15")
    assert len(tt.LESSON_TIMES) == 11
