"""Сис-админ смотрит расписание в боте: список, карточка, неделя/день/ближайшие."""
import pytest

import database as db
import timetable as tt
from conftest import press, say

SYS = "1"
GROUP = "24-23П"


@pytest.fixture
async def env_sysadmin(env):
    import repository as repo

    await repo.grant_sysadmin(SYS, "Иванов Иван Иванович")
    await repo.upsert_schedule(GROUP, "https://college.example/24-23.pdf")
    return env


@pytest.fixture
def parsed(monkeypatch):
    """Расписание разобрано: вместо скачивания PDF отдаём готовую неделю."""
    from handlers import schedules

    schedule = _full_week(GROUP)
    result = type("R", (), {"has_lessons": True, "schedule": schedule, "reason": ""})()
    monkeypatch.setattr(schedules, "parse_group", lambda code, force=False: _value(result))
    return result


def _full_week(group: str) -> tt.GroupSchedule:
    """Неделя на все семь дней: тест не должен зависеть от того, какой сегодня день."""
    return tt.GroupSchedule(group=group, days={
        weekday: tt.DaySchedule(weekday=weekday, lessons=[
            tt.Lesson(number=1, subject="Предмет 1", teacher="Иванов И. И.", room="101",
                      start="08:00", end="08:45"),
        ])
        for weekday in range(7)
    })


def _value(value):
    async def inner(*args, **kwargs):
        return value
    return inner()


async def test_list_has_eye_button_next_to_every_group(env_sysadmin, api):
    await press(SYS, "schedules")
    payloads = api.payloads(SYS)
    assert f"scview:{GROUP}" in payloads
    assert f"sc:{GROUP}" in payloads


async def test_card_offers_to_open_the_schedule(env_sysadmin, api):
    await press(SYS, f"sc:{GROUP}")
    assert f"scview:{GROUP}" in api.payloads(SYS)
    card = api.to(SYS)[-1][1]
    assert GROUP in card and "https://college.example/24-23.pdf" in card


async def test_admin_sees_the_same_week_as_students(env_sysadmin, parsed, api):
    await press(SYS, f"scview:{GROUP}")
    text = "\n".join(body for _, body, _ in api.to(SYS))
    assert "Расписание группы 24-23П" in text
    assert "Понедельник" in text and "Предмет 1" in text
    # переключение видов: день, ближайшие, неделя
    payloads = api.payloads(SYS)
    assert f"scview:{GROUP}:day" in payloads
    assert f"scview:{GROUP}:next" in payloads
    assert f"scview:{GROUP}" in payloads


async def test_admin_view_switches_to_day_and_back(env_sysadmin, parsed, api):
    await press(SYS, f"scview:{GROUP}:day")
    text = "\n".join(body for _, body, _ in api.to(SYS))
    assert "Предмет 1" in text
    assert "scview:24-23П" in api.payloads(SYS)   # кнопка «вся неделя»


async def test_admin_view_handles_sloppy_group_code(env_sysadmin, parsed, api):
    await press(SYS, "scview:24-23 (П)")
    assert "24-23П" in "\n".join(body for _, body, _ in api.to(SYS))


async def test_admin_view_falls_back_to_pdf_link(env_sysadmin, monkeypatch, api):
    """Если PDF не разобрался - ссылка, а не пустота."""
    from handlers import schedules

    monkeypatch.setattr(schedules, "parse_group",
                        lambda code, force=False: _value(type(
                            "R", (), {"has_lessons": False, "schedule": None, "reason": "не нашлось занятий"})()))
    await press(SYS, f"scview:{GROUP}")
    text = "\n".join(body for _, body, _ in api.to(SYS))
    assert "https://college.example/24-23.pdf" in text
    assert "не нашлось занятий" in text


async def test_admin_view_says_when_there_is_no_schedule(env_sysadmin, api):
    await press(SYS, "scview:99-99")
    assert "нет расписания" in "\n".join(body for _, body, _ in api.to(SYS))


async def test_student_cannot_open_admin_schedule_view(env_sysadmin, parsed, api):
    await say("300", "/start")
    await press("300", "scview:24-23П")
    assert not any("24-23П" in body and "Понедельник" in body
                   for _, body, _ in api.to("300"))


async def test_card_shows_parsed_state(env_sysadmin, api):
    """Карточка сразу говорит, разобран файл или нет - без открытия PDF."""
    await db.run("UPDATE schedules SET parsed_at=datetime('now'), found_groups=? WHERE group_code=?",
                 (GROUP, GROUP))
    await press(SYS, f"sc:{GROUP}")
    assert "Разобрано: 1 колонок" in api.to(SYS)[-1][1]
