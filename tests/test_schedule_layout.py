"""Тесты: разделитель между парами в тексте расписания.

Пара — это два урока, поэтому день читается парами: перед каждым нечётным уроком
после первого (3, 5, 7) стоит пустая строка. Перед первым уроком и после
последнего пустой строки нет, нечётный хвост (урок 5) остаётся один, а между днями
недели разделитель прежний — ровно одна пустая строка.
"""
from datetime import date, timedelta

import timetable as tt
from timetable import WEEKDAYS_FULL

LESSONS = 11


def make_lesson(number: int) -> tt.Lesson:
    start, end = tt.LESSON_TIMES[number - 1]
    return tt.Lesson(number=number, subject=f"Предмет {number}", teacher="Иванова А. А.",
                     room=f"10{number}", start=start, end=end)


def make_day(count: int, weekday: int = 0) -> tt.DaySchedule:
    return tt.DaySchedule(weekday=weekday,
                          lessons=[make_lesson(number) for number in range(1, count + 1)])


def chunks(text: str) -> list[str]:
    """Куски текста между пустыми строками: так видно пары."""
    return [chunk for chunk in text.split("\n\n") if chunk.strip()]


def numbers_in(chunk: str) -> list[int]:
    found = []
    for line in chunk.splitlines():
        if " урок · " in line:
            found.append(int(line.split(" урок")[0].split()[-1]))
    return found


# ── внутри дня ────────────────────────────────────────────────────────────────
def test_no_gap_between_first_and_second_lesson():
    """Уроки 1 и 2 — одна пара, пустой строки между ними нет."""
    first = chunks(tt.format_day(make_day(2)))
    assert len(first) == 1
    assert numbers_in(first[0]) == [1, 2]


def test_gap_before_third_lesson():
    day = tt.format_day(make_day(3))
    assert len(chunks(day)) == 2
    assert numbers_in(chunks(day)[0]) == [1, 2]
    assert numbers_in(chunks(day)[1]) == [3]


def test_six_lessons_split_in_three_pairs():
    day = chunks(tt.format_day(make_day(6)))
    assert [numbers_in(chunk) for chunk in day] == [[1, 2], [3, 4], [5, 6]]


def test_gap_before_fifth_lesson():
    """Между 4 и 5 пустая строка стоит, как между 2 и 3."""
    day = chunks(tt.format_day(make_day(5)))
    assert [numbers_in(chunk) for chunk in day] == [[1, 2], [3, 4], [5]]


def test_odd_tail_stays_alone():
    """Пять уроков: хвост из одного урока остаётся, пустой строки после него нет."""
    text = tt.format_day(make_day(5))
    assert not text.endswith("\n")
    assert chunks(text)[-1].splitlines()[0].endswith("Предмет 5")


def test_no_gap_at_the_start_and_at_the_end_of_the_day():
    for count in (1, 2, 3, 6, LESSONS):
        text = tt.format_day(make_day(count))
        assert not text.startswith("\n")
        assert text.splitlines()[0].startswith("📅 Понедельник")
        assert text.splitlines()[1].strip().startswith("1 урок")
        assert not text.endswith("\n")


def test_gap_sits_before_the_odd_lesson_only():
    """Пустых строк ровно столько, сколько нечётных уроков после первого."""
    for count in range(1, LESSONS + 1):
        lines = tt.format_day(make_day(count)).splitlines()
        empty = [index for index, line in enumerate(lines) if not line.strip()]
        assert len(empty) == (count - 1) // 2
        for index in empty:
            # пустая строка стоит между хвостом предыдущей пары и началом нечётного урока
            assert "ауд." in lines[index - 1]
            assert "урок · " in lines[index + 1]
            assert int(lines[index + 1].split(" урок")[0].split()[-1]) % 2 == 1


def test_day_with_one_lesson_has_no_gap():
    assert chunks(tt.format_day(make_day(1))) == [tt.format_day(make_day(1))]


# ── между днями недели ────────────────────────────────────────────────────────
def test_days_in_a_week_are_separated_by_one_blank_line():
    schedule = tt.GroupSchedule(group="ИС-21", week=date(2026, 9, 28), days={
        0: make_day(4, 0),
        2: make_day(4, 2),
    })
    week = tt.format_schedule(schedule)
    second = tt.day_title(2, date(2026, 9, 28) + timedelta(days=2))
    assert f"\n\n📅 {second}" in week    # прежний разделитель: одна пустая строка
    assert "\n\n\n📅" not in week          # двустороннего разделителя не появилось
    assert week.count("📅") == 2
    # внутри дня пустые строки остались: пара — пара (по 4 урока = две пары)
    assert [numbers_in(chunk) for chunk in chunks(week) if "урок · " in chunk] == [
        [1, 2], [3, 4], [1, 2], [3, 4]]


def test_today_view_of_one_day_has_no_gap_before_the_title(frozen_college_clock):
    # дата за неделю от замороженного «сегодня»: зашитая 30.09 превращалась в
    # «завтра» на следующие же сутки, и проверка падала сама по себе
    day = frozen_college_clock.date() + timedelta(days=7)
    text = tt.format_day(make_day(6, day.weekday()), day, today=True)
    assert text.startswith(f"📅 {WEEKDAYS_FULL[day.weekday()].capitalize()} {day:%d.%m}  • сегодня"), text
    assert len(chunks(text)) == 3
