"""Расписание преподавателя: поиск по фамилии и неделя занятий."""
import pytest

import database as db
import repository as repo
from conftest import press, register, say
from handlers.schedules import format_teacher_week

STUDENT = "300"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject, teacher, room, start, end) "
                 "VALUES(?,?,?,?,?,?,?,?)", ("24-23П", 0, 1, "Математика", "Шкуропатов П. И.", "314-1",
                                            "08:00", "08:45"))
    await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject, teacher, room, start, end) "
                 "VALUES(?,?,?,?,?,?,?,?)", ("24-21-2С", 2, 3, "Физика", "Шкуропатов П. И.", "222-1",
                                            "09:50", "10:35"))
    await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject, teacher, room, start, end) "
                 "VALUES(?,?,?,?,?,?,?,?)", ("24-25", 0, 1, "История", "Бикбаева Л. М.", "229-1",
                                            "08:00", "08:45"))
    # вторая преподавательница с той же фамилией - чтобы поиск давал список
    await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject, teacher, room, start, end) "
                 "VALUES(?,?,?,?,?,?,?,?)", ("25-29П", 1, 2, "Химия", "Шкуропатова А. А.", "220-1",
                                            "08:50", "09:35"))
    return env


# ── репозиторий ──────────────────────────────────────────────────────────────
async def test_search_finds_teacher_by_part_of_name(env):
    assert "Шкуропатов П. И." in await repo.search_teachers("Шкур")
    assert await repo.search_teachers("икбаева") == ["Бикбаева Л. М."]


async def test_search_is_case_insensitive_and_limits(env):
    assert len(await repo.search_teachers("", 2)) == 2
    assert await repo.search_teachers("нетакого") == []


async def test_teacher_groups_and_days(env):
    assert await repo.teacher_groups("Шкуропатов П. И.") == ["24-21-2С", "24-23П"]
    days = await repo.lessons_for_teacher("Шкуропатов П. И.")
    assert set(days) == {0, 2}
    assert days[0][0][1] == "Математика"
    assert days[0][0][2] == "24-23П"


# ── форматирование ───────────────────────────────────────────────────────────
def test_week_is_readable():
    text = format_teacher_week({0: [(1, "Математика", "24-23П", "314-1")]})
    assert "Понедельник" in text
    assert "Математика" in text and "24-23П" in text and "314-1" in text


def test_empty_week_says_so():
    assert "не найдено" in format_teacher_week({})


# ── бот ──────────────────────────────────────────────────────────────────────
async def test_teacher_search_lists_matches(api, env):
    await press(STUDENT, "teacherask")
    assert "фамилию" in api.to(STUDENT)[-1][1].lower()
    await say(STUDENT, "Шкур")
    payloads = api.payloads(STUDENT)
    assert any(p == "teacher:Шкуропатов П. И." for p in payloads)
    assert any(p == "teacher:Шкуропатова А. А." for p in payloads)
    assert "Кого показать" in api.to(STUDENT)[-1][1]


async def test_teacher_search_shows_week_directly(api, env):
    await press(STUDENT, "teacherask")
    await say(STUDENT, "Бикбаева")
    text = "\n".join(body for _, body, _ in api.to(STUDENT))
    assert "Бикбаева Л. М." in text
    assert "24-25" in text
    assert "Понедельник" in text


async def test_teacher_search_reports_nothing_found(api, env):
    await press(STUDENT, "teacherask")
    await say(STUDENT, "Такого нет")
    assert "не нашлось" in api.to(STUDENT)[-1][1].lower()


async def test_teacher_search_cancels_state(api, env):
    import database as database

    await press(STUDENT, "teacherask")
    assert await database.get_state(STUDENT) is not None
    await say(STUDENT, "Бикбаева")
    assert await database.get_state(STUDENT) is None


async def test_teacher_button_works_from_command(api, env):
    await say(STUDENT, "/teacher")
    assert "Кого показать" in api.to(STUDENT)[-1][1]
