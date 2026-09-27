"""Режим просмотра: сис-админ, сотрудник, студент."""
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register, say

SYS, STAFF, STUDENT = "1", "200", "300"


@pytest.fixture
async def env(env):
    await repo.grant_sysadmin(SYS, "Иванов Иван Иванович")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    return env


def labels(api, user=SYS) -> str:
    return " ".join(b["text"] for row in (api.to(user)[-1][2] or []) for b in row)


# ── переключатель ────────────────────────────────────────────────────────────
async def test_switcher_visible_for_sysadmin(api, env):
    await press(SYS, "home")
    text = labels(api)
    for view in ("⚙️ Сис-админ", "🏫 Сотрудник", "🎓 Студент"):
        assert view in text


async def test_switcher_marks_current_mode(api, env):
    await press(SYS, "home")
    assert "✅ ⚙️ Сис-админ" in labels(api)


async def test_switch_to_student_shows_student_menu(api, env):
    await press(SYS, "view:student")
    payloads = api.payloads(SYS)
    assert "sub:cert" in payloads and "sched" in payloads
    assert "Режим студента" in api.to(SYS)[-1][1]


async def test_switch_to_staff_shows_queue(api, env):
    await press(SYS, "view:staff")
    assert "staff" in api.payloads(SYS)
    assert "Режим сотрудника" in api.to(SYS)[-1][1]


async def test_switch_back_to_admin(api, env):
    """Возврат в свой кабинет: служебные кнопки сис-админа на месте."""
    await press(SYS, "view:student")
    await press(SYS, "view:admin")
    payloads = api.payloads(SYS)
    assert {"people", "admins", "codes"} <= set(payloads)
    assert "Панель сис-админа" in api.to(SYS)[-1][1]


async def test_view_command_explains_modes(api, env):
    await say(SYS, "/view")
    body = api.to(SYS)[-1][1]
    assert "Режим просмотра" in body
    for view in ("Сис-админ", "Сотрудник", "Студент"):
        assert view in body
    assert "view:student" in api.payloads(SYS)


async def test_view_command_switches(api, env):
    await say(SYS, "/view:student")
    assert "sub:cert" in api.payloads(SYS)


# ── режим студента без профиля не ломается ───────────────────────────────────
async def test_student_mode_schedule_never_starts_registration(api, env):
    """Сис-админ без профиля студента жмёт «Моё расписание» и не попадает в регистрацию."""
    await repo.upsert_schedule("24-23П", "https://college.example/24-23.pdf")
    await press(SYS, "view:student")
    await press(SYS, "sched")
    assert await db.get_state(SYS) is None
    assert "sched:24-23П" in api.payloads(SYS)


async def test_empty_registry_does_not_loop(api, env):
    """Пустой справочник расписаний: честное сообщение вместо вечного перехода."""
    await press(SYS, "view:student")
    await press(SYS, "sched")
    assert "Расписаний пока нет" in api.to(SYS)[-1][1]
    assert await db.get_state(SYS) is None


async def test_student_mode_profile_is_honest(api, env):
    await press(SYS, "view:student")
    await press(SYS, "profile")
    assert "Профиля студента у вас нет" in api.to(SYS)[-1][1]


async def test_student_mode_tickets_do_not_start_registration(api, env):
    await press(SYS, "view:student")
    await press(SYS, "tickets")
    assert await db.get_state(SYS) is None


async def test_student_with_profile_sees_own_schedule(api, env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await repo.grant_sysadmin(STUDENT, "Иванов Иван Иванович")
    await repo.upsert_schedule("24-23", "https://college.example/24-23.pdf")
    await press(STUDENT, "view:student")
    await press(STUDENT, "sched")
    assert "24-23" in "\n".join(body for _, body, _ in api.to(STUDENT))


async def test_mode_persists_between_presses(api, env):
    await press(SYS, "view:student")
    assert await db.get_setting(f"menu_view:{SYS}") == "student"
    await press(SYS, "home")
    assert "sub:cert" in api.payloads(SYS)
