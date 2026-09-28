"""Часы проекта: разбор дат, склонения и устойчивость к отсутствующей зоне.

Время в базе — локальное время колледжа (clock), поэтому проверяем именно
его: склонения в подписях, границы суток при сдвиге и то, что при
недоступной зоне модуль не падает, а считает UTC+5.
"""
from datetime import datetime, timedelta

import pytest

import clock

# Опорный момент для всех подписей: 28.09.2026, 14:00 локального времени.
NOW = datetime(2026, 9, 28, 14, 0, 0)


def ago(**kwargs) -> str:
    """Момент в базе на N минут/часов/дней раньше NOW."""
    return clock.shift(NOW.strftime(clock.STAMP_FORMAT),
                       -timedelta(**kwargs).total_seconds() / 60)


# ── разбор строк ──────────────────────────────────────────────────────────────
def test_parse_plain_db_stamp():
    assert clock.parse("2026-09-28 14:30:05") == datetime(2026, 9, 28, 14, 30, 5)


def test_parse_accepts_iso_t_and_z():
    assert clock.parse("2026-09-28T14:30:05") == datetime(2026, 9, 28, 14, 30, 5)
    # Z — это UTC: 19:30Z в Екатеринбурге становится 00:30 следующего дня
    assert clock.parse("2026-09-28T19:30:05Z") == datetime(2026, 9, 29, 0, 30, 5)
    assert clock.parse("2026-09-28T14:30:05+05:00") == datetime(2026, 9, 28, 14, 30, 5)


def test_parse_returns_none_for_junk():
    for value in ("", "   ", None, "bad value", "28.09.2026 14:30", "сегодня до 18:00"):
        assert clock.parse(value) is None, value


def test_looks_like_stamp_separates_dates_from_free_text():
    assert clock.looks_like_stamp("2026-09-28 14:30:05")
    for value in ("", "18:00", "28.09.2026", "2026-09-28", "2026-09-28 14:30", "сегодня до 18:00"):
        assert not clock.looks_like_stamp(value), value


# ── сейчас, отпечаток, сдвиг ──────────────────────────────────────────────────
def test_stamp_has_db_format_and_is_local():
    stamp = clock.stamp()
    moment = clock.parse(stamp)
    assert moment is not None
    assert stamp == moment.strftime("%Y-%m-%d %H:%M:%S")
    assert abs((clock.now() - moment).total_seconds()) < 5
    assert clock.today() == moment.date()


def test_stamp_at_moves_forward_and_back():
    assert clock.stamp_at(0)[:10] == clock.stamp()[:10]
    forward = clock.parse(clock.stamp_at(60))
    assert timedelta(minutes=59) <= forward - clock.now() <= timedelta(minutes=61)
    back = clock.parse(clock.stamp_at(-60))
    assert -timedelta(minutes=61) <= back - clock.now() <= -timedelta(minutes=59)


def test_date_ago_counts_days():
    assert clock.date_ago(0) == clock.today().strftime("%Y-%m-%d")
    assert clock.date_ago(7) == (clock.today() - timedelta(days=7)).strftime("%Y-%m-%d")


def test_shift_moves_across_midnight():
    assert clock.shift("2026-09-28 23:50:00", 20) == "2026-09-29 00:10:00"
    assert clock.shift("2026-09-01 00:10:00", -20) == "2026-08-31 23:50:00"
    # через новый год
    assert clock.shift("2026-12-31 23:59:59", 1) == "2027-01-01 00:00:59"


def test_shift_keeps_anything_it_cannot_read():
    for value in ("", "сегодня до 18:00", "18:00", "bad value"):
        assert clock.shift(value, 300) == value


# ── склонения ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("count", "word"), [
    (1, "минуту"), (2, "минуты"), (5, "минут"),
    (11, "минут"), (12, "минут"), (14, "минут"), (21, "минуту"), (22, "минуты"),
    (25, "минут"), (101, "минуту"), (111, "минут"),
])
def test_minutes_are_declined(count: int, word: str):
    assert clock.minutes_ago(count) == f"{count} {word} назад"
    if count < 60:      # дальше часа подпись уже не «минуты назад», а «сегодня в …»
        assert clock.format_when(NOW - timedelta(minutes=count), reference=NOW) == f"{count} {word} назад"


def test_hours_and_days_phrases():
    assert clock.hours_ago(1) == "1 час назад"
    assert clock.hours_ago(3) == "3 часа назад"
    assert clock.hours_ago(21) == "21 час назад"
    # сегодня подпись всегда с часами: «3 часа назад» рядом с «вчера в 14:30»
    # читается хуже, чем точное время того же дня
    assert clock.format_when(NOW - timedelta(hours=3), reference=NOW) == "сегодня в 11:00"
    assert clock.format_when(NOW - timedelta(hours=8), reference=NOW) == "сегодня в 06:00"


# ── сегодня / вчера ───────────────────────────────────────────────────────────
def test_today_and_yesterday():
    assert clock.is_today("2026-09-28 09:15:00", reference=NOW)
    assert not clock.is_today("2026-09-27 23:50:00", reference=NOW)
    assert clock.is_yesterday("2026-09-27 23:50:00", reference=NOW)
    assert not clock.is_yesterday("2026-09-28 09:15:00", reference=NOW)
    assert not clock.is_today("не дата")


def test_format_when_today_yesterday_and_old_day():
    assert clock.format_when("2026-09-28 09:15:00", reference=NOW) == "сегодня в 09:15"
    assert clock.format_when("2026-09-27 14:30:00", reference=NOW) == "вчера в 14:30"
    assert clock.format_when("2026-09-25 14:30:00", reference=NOW) == "пт в 14:30"
    assert clock.format_when("2026-09-01 09:15:00", reference=NOW) == "01.09.2026 09:15"
    assert clock.format_when("2026-01-05 09:15:00", reference=NOW) == "05.01.2026 09:15"


def test_format_when_keeps_unreadable_value():
    assert clock.format_when("bad value") == "bad value"
    assert clock.format_when("") == ""
    assert clock.format_when(None) == ""


def test_format_when_on_future_value():
    assert clock.format_when(NOW + timedelta(minutes=5), reference=NOW) == "через 5 минут"
    assert clock.format_when(NOW + timedelta(minutes=1), reference=NOW) == "через 1 минуту"
    assert clock.format_when(NOW + timedelta(hours=2), reference=NOW) == "через 2 часа"


# ── зона ──────────────────────────────────────────────────────────────────────
def test_zone_is_the_college_one():
    assert clock.tz_name() == "Asia/Yekaterinburg"
    # и в контейнере (Europe/Yekaterinburg — ссылка), и на машине без tzdata
    # должно получаться одно и то же время: перевода часов в Екатеринбурге нет
    assert clock.offset_minutes() == 300


def test_missing_zone_falls_back_to_utc5(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Nonexistent-City")
    clock.reset()
    try:
        assert not clock.is_exact()
        assert clock.offset_minutes() == 300
        first = clock.now()
        second = clock.now()
        assert second >= first                       # монотонно, без прыжков назад
        assert clock.parse(clock.stamp()) is not None
        assert clock.format_when(clock.stamp_at(-5)) == "5 минут назад"
    finally:
        clock.reset()


def test_empty_tz_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TZ", "")
    clock.reset()
    try:
        assert clock.tz_name() == "Asia/Yekaterinburg"
        assert clock.offset_minutes() == 300
    finally:
        clock.reset()
