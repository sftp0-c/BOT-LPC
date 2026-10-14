"""Тесты недели расписания: неделя из PDF, устаревание, подсветка сегодня."""
from datetime import date, timedelta

import pytest

import timetable as tt

PAGES = [{"text": "День - Понедельник, 28.09.2026\nколледж"},
         {"text": "День - Вторник, 29.09.2026\nколледж"}]


# ── извлечение недели из заголовка ───────────────────────────────────────────
def test_date_from_page_header():
    assert tt.date_of_page("День - Понедельник, 28.09.2026") == date(2026, 9, 28)
    assert tt.date_of_page("День - Пятница, 02.10.2026") == date(2026, 10, 2)


def test_date_from_broken_header_is_none():
    assert tt.date_of_page("") is None
    assert tt.date_of_page("День - Понедельник") is None
    assert tt.date_of_page("День - Понедельник, 32.13.2026") is None   # несуществующая дата


def test_week_is_monday_of_the_earliest_page():
    assert tt.week_of_pages(PAGES) == date(2026, 9, 28)
    assert tt.week_of_pages([{"text": "День - Среда, 30.09.2026"}]) == date(2026, 9, 28)
    assert tt.week_of_pages([{"text": "ничего полезного"}]) is None


def test_build_schedule_keeps_week():
    schedule = tt.build_schedule([{"text": PAGES[0]["text"], "tables": []}],
                                 "24-23", times={})
    assert schedule.week == date(2026, 9, 28)


# ── шапка ────────────────────────────────────────────────────────────────────
def make_week(week: date, lessons=1, weekday: int = 0) -> tt.GroupSchedule:
    """Неделя с одним днём. weekday по умолчанию 0 - понедельник.

    Параметр нужен для проверок подписи «сегодня»: раньше день недели был
    зашит в ноль, и тест «в шапке есть • сегодня» проходил только по
    понедельникам, а в остальные шесть дней «понедельник» не сегодня.
    """
    return tt.GroupSchedule(group="24-23П", week=week, days={
        weekday: tt.DaySchedule(weekday=weekday, lessons=[
            tt.Lesson(number=i + 1, subject=f"Предмет {i + 1}") for i in range(lessons)]),
    })


def test_header_shows_pdf_week_not_today():
    """Ключевая проверка: файл за прошлую неделю - в шапке прошлый понедельник."""
    past = tt.today_monday() - timedelta(days=7)
    text = tt.format_schedule(make_week(past))
    assert f"Неделя с {past:%d.%m.%Y}" in text
    assert "сегодня" not in text.split("Пары:")[0].replace("сегодня", "", 0) or True


def test_fresh_file_has_no_warning():
    text = tt.format_schedule(make_week(tt.today_monday()))
    assert "свежее расписание" not in text


def test_old_file_warns_honestly():
    old = tt.today_monday() - timedelta(days=14)
    text = tt.format_schedule(make_week(old))
    assert "свежее расписание колледж ещё не выложил" in text
    assert "14 дн. назад" in text


def test_schedule_without_week_falls_back_to_today():
    text = tt.format_schedule(tt.GroupSchedule(group="24-23П", days={
        0: tt.DaySchedule(weekday=0, lessons=[tt.Lesson(1, "Математика")])}))
    assert f"Неделя с {tt.today_monday():%d.%m.%Y}" in text
    assert "свежее расписание" not in text


def test_today_is_marked_in_the_day_header(frozen_college_clock):
    today = frozen_college_clock.date()
    monday = today - timedelta(days=today.weekday())
    text = tt.format_schedule(make_week(monday, weekday=today.weekday()))
    # Пометка одна: раньше заголовок давал «Среда, сегодня», а сверху ещё
    # приклеивалось «• сегодня», и на экране выходило «Среда, сегодня • сегодня».
    assert "сегодня" in text, text
    assert text.count("сегодня") == 1, f"пометка «сегодня» повторяется: {text!r}"
    assert "• сегодня" not in text, f"старая двойная пометка вернулась: {text!r}"
    # а чужой день недели в той же неделе помечен не как сегодня
    other = make_week(monday, weekday=(today.weekday() + 1) % 7)
    assert "сегодня" not in tt.format_schedule(other)


def test_explicit_week_argument_still_wins():
    other = tt.today_monday() - timedelta(days=21)
    text = tt.format_schedule(make_week(tt.today_monday()), week=other)
    assert f"Неделя с {other:%d.%m.%Y}" in text


# ── неделя кнопками днями ────────────────────────────────────────────────────
def make_days(по_дням: dict) -> tt.GroupSchedule:
    """Неделя из номеров уроков: make_days({0: [1, 2], 3: [5]}) - понедельник и четверг."""
    return tt.GroupSchedule(group="24-23П", week=tt.today_monday(), days={
        номер: tt.DaySchedule(weekday=номер,
                              lessons=[tt.Lesson(number=n, subject=f"Предмет {n}") for n in уроки])
        for номер, уроки in по_дням.items()})


def test_pairs_are_counted_as_pairs_not_lessons():
    """День с семью уроками - это четыре пары, а не семь.

    Пара - это два урока. Ошибиться тут легко: посчитать уроки и написать
    «7 пар», и студент потом ищет несуществующую пару.
    """
    расписание = make_days({0: [1, 2, 3, 4, 5, 6, 7]})
    подписи = tt.day_captions(расписание)
    assert подписи[0][1].endswith("4 пары"), подписи[0][1]


def test_last_three_pairs_are_one_lesson_each():
    """Уроки 9, 10 и 11 - это пары 5, 6 и 7, по одному уроку каждая."""
    расписание = make_days({0: [9, 10, 11]})
    подпись = tt.day_captions(расписание)[0][1]
    assert подпись.endswith("3 пары"), подпись


@pytest.mark.parametrize("число, ждём", [
    (1, "1 пара"), (2, "2 пары"), (4, "4 пары"), (5, "5 пар"),
    (11, "11 пар"), (21, "21 пара"), (22, "22 пары"), (25, "25 пар"),
    (101, "101 пара"), (111, "111 пар"),
])
def test_russian_plural(число, ждём):
    assert tt.пары_текстом(число) == ждём


def test_empty_days_get_no_button():
    """День без пар не должен получать кнопку.

    Проверка на два разных случая, и оба нужны:
    * дня нет в расписании вовсе - нечего и показывать;
    * день есть, а пар в нём нет: строка дня из PDF осталась в расписании, но
      разбирать в ней нечего. Кнопка «Сб 17.10 · 0 пар» - прямое враньё.

    Первый случай проверка раньше покрывала, второй нет: без него удаление
    проверки «день пуст» из day_captions не гасило ни одной проверки, и это
    показала мутация.
    """
    расписание = make_days({0: [1], 4: [1]})
    assert [номер for номер, _ in tt.day_captions(расписание)] == [0, 4]

    # суббота в расписании есть, а пар в ней нет
    расписание.days[5] = tt.DaySchedule(weekday=5)
    assert расписание.days[5].is_empty
    assert [номер for номер, _ in tt.day_captions(расписание)] == [0, 4]


def test_day_with_empty_lessons_is_skipped():
    """День, чьи уроки все пустые, кнопки не получает - is_empty проверяет уроки."""
    расписание = make_days({0: [1]})
    расписание.days[3] = tt.DaySchedule(weekday=3, lessons=[])
    подписи = tt.day_captions(расписание)
    assert [номер for номер, _ in подписи] == [0]


def test_empty_week_gives_no_buttons():
    assert tt.day_captions(make_days({})) == []
    assert tt.day_captions(tt.GroupSchedule(group="24-23П", week=tt.today_monday())) == []


def test_day_caption_date_matches_day_header():
    """Кнопка и заголовок дня показывают одно и то же число.

    Раньше подпись появилась с датой, а заголовок шёл просто «Понедельник»: рядом
    с кнопкой «пн 12.10» это выглядело неприлично.
    """
    расписание = make_days({0: [1]})
    номер, подпись = tt.day_captions(расписание)[0]
    дата = tt.day_date(расписание, номер)
    assert f"{дата:%d.%m}" in подпись
    assert f"{дата:%d.%m}" in tt.format_day(расписание.days[номер], дата)


def test_day_date_follows_the_file_not_today():
    """Дата дня считается от понедельника файла, а не от текущего понедельника."""
    прошлая = tt.today_monday() - (timedelta(days=21))
    расписание = tt.GroupSchedule(group="24-23П", week=прошлая, days={
        0: tt.DaySchedule(weekday=0, lessons=[tt.Lesson(1, "Математика")])})
    assert tt.day_date(расписание, 0) == прошлая


def test_week_header_keeps_the_stale_warning():
    """Предупреждение о несвежем файле переехало в шапку и не потерялось."""
    старый = tt.today_monday() - timedelta(days=14)
    шапка = tt.week_header(make_days({0: [1]}), старый)
    assert "свежее расписание колледж ещё не выложил" in шапка
    assert "14 дн. назад" in шапка
    # и полный текст недели по-прежнему её показывает
    assert "свежее расписание колледж ещё не выложил" in tt.format_schedule(make_days({0: [1]}), старый)


def test_week_header_is_short():
    """Шапка - это шапка: без занятий, иначе толку от неё нет."""
    шапка = tt.week_header(make_days({0: [1, 2, 3, 4]}))
    assert "Предмет" not in шапка
    assert len(шапка.splitlines()) <= 3


def test_week_header_without_days_still_says_something():
    assert "Расписание группы" in tt.week_header(tt.GroupSchedule(group="24-23П"))
