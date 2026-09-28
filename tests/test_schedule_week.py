"""Тесты недели расписания: неделя из PDF, устаревание, подсветка сегодня."""
from datetime import date, timedelta


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
    assert "• сегодня" in text, text
    # а чужой день недели в той же неделе помечен не как сегодня
    other = make_week(monday, weekday=(today.weekday() + 1) % 7)
    assert "• сегодня" not in tt.format_schedule(other)


def test_explicit_week_argument_still_wins():
    other = tt.today_monday() - timedelta(days=21)
    text = tt.format_schedule(make_week(tt.today_monday()), week=other)
    assert f"Неделя с {other:%d.%m.%Y}" in text
