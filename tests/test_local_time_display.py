"""Время на панели должно показываться таким, каким оно лежит в базе.

Здесь поймана реальная ошибка: `utils.local_time()` считал, что голое время
из базы записано в UTC, и приводил его к местному. Пока база писалась
через `datetime('now')` (это всегда UTC) - приведение было правильным. Но
после перевода базы на локальное время колледжа (см. `clock`) каждая дата
на панели уезжала вперёд на 5 часов: сохранённое 12:00 показывалось как
15:00. Теперь разбор времени один - `clock.parse`.
"""
from datetime import datetime, time, timedelta

import clock
import utils

# Момент, который точно не равен «сейчас», чтобы «сегодня/вчера» не путались
ПОЛДЕНЬ = "2026-09-28 12:00:00"


def test_stored_time_is_shown_as_is():
    assert utils.fmt_time(ПОЛДЕНЬ) == "28.09.2026 12:00"


def test_local_time_does_not_shift_stored_naive_value():
    moment = utils.local_time(ПОЛДЕНЬ)
    assert moment is not None
    assert (moment.hour, moment.minute) == (12, 0)
    assert moment.tzinfo is None, "время из базы хранится без зоны"


def test_explicit_offset_is_brought_to_college_zone():
    """Строка с явным сдвигом - единственный случай, когда время пересчитывается."""
    assert utils.fmt_time("2026-09-28T07:00:00Z") == "28.09.2026 12:00"
    assert utils.fmt_time("2026-09-28T12:00:00+05:00") == "28.09.2026 12:00"


def test_today_and_yesterday_follow_college_zone():
    assert "сегодня" in utils.fmt_when(clock.stamp())
    assert "вчера" in utils.fmt_when(clock.stamp_at(-60 * 24))


def test_days_ago_text_counts_from_college_now():
    assert utils.days_ago_text(clock.stamp(), 0) == "меньше часа назад"
    assert utils.days_ago_text(clock.stamp_at(-120), 0) == "2 ч назад"
    assert utils.days_ago_text(clock.stamp_at(-60 * 24), 1) == "вчера"
    assert utils.days_ago_text(clock.stamp_at(-60 * 24 * 3), 3) == "3 дн назад"


def test_unparsable_value_passes_through():
    assert utils.fmt_time("") == ""
    assert utils.fmt_when("не дата") == "не дата"
    assert utils.days_ago_text("не дата", 0) == "неизвестно"


def test_no_double_shift_between_stamp_and_display():
    """Сквозная проверка: что записали, то и показали - без сдвига."""
    for minutes in (0, 1, 90, 60 * 24 * 3):
        stamp = clock.stamp_at(-minutes)
        assert utils.local_time(stamp) == clock.parse(stamp)
        assert utils.fmt_time(stamp).endswith(clock.parse(stamp).strftime("%H:%M"))


def test_fmt_when_boundary_follows_given_now():
    """Граница «сегодня/вчера» считается от переданного now, а не от часов машины.

    Момент берём внутри суток, отсчитанных от начала дня, а не «сутки назад»:
    при «минус 24 часа» и сравнении «плюс два часа» тест попадал ровно на
    полночь и краснел после 22:00 - часы машины решали, зелёный тест или нет.
    """
    начало = datetime.combine(clock.today(), time(0, 0))
    вчера = начало - timedelta(days=1) + timedelta(hours=12)
    # один и тот же момент, разный now: метка считается от now, а не от часов машины
    assert utils.fmt_when(вчера, now=вчера + timedelta(hours=2)) == "сегодня 12:00"
    assert utils.fmt_when(вчера, now=начало + timedelta(hours=14)) == "вчера 12:00"
    # без переданного now «сейчас» - это настоящие сегодняшние сутки
    assert utils.fmt_when(вчера) == "вчера 12:00"


def test_timetable_today_uses_college_zone():
    """Расписание считает «сегодня» по часовому поясу колледжа, а не машины."""
    import timetable as tt
    сегодня = clock.today()
    assert tt.day_title(сегодня.weekday(), сегодня).endswith("сегодня")
    завтра = сегодня + timedelta(days=1)
    assert tt.day_title(завтра.weekday(), завтра).endswith("завтра")
    assert tt.today_monday().weekday() == 0


def test_panel_shows_the_same_time_as_the_bot():
    """Панель и бот обязаны показывать время одинаково - оба через utils."""
    from pathlib import Path
    source = Path("webpanel.py").read_text(encoding="utf-8-sig")
    assert "from utils import" in source
    # в панели нет собственного разбора времени в обход clock
    assert "datetime.now(" not in source
    assert "date.today(" not in source
    assert "fmt_when(" in source, "панель показывает время через fmt_when"
