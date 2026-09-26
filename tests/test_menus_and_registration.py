"""Меню сис-админа: переключатель вида, «ещё» и новая регистрация с подтверждением."""

import database as db
import repository as repo
from conftest import add_staff, press, register, say

SYS, STAFF, STUDENT = "1", "200", "100"


# ── меню сис-админа ───────────────────────────────────────────────────────────
async def test_super_menu_is_compact_and_has_more(api):
    await press(SYS, "sysadm")
    payloads = api.payloads(SYS)
    # основное - короткое, служебное спрятано в «Ещё»
    assert {"today", "staff", "snew", "people", "admins", "more", "home"} <= set(payloads)
    for buried in ("diag", "settings", "groups", "broadcast", "stats", "schedules"):
        assert buried not in payloads, f"{buried} не должен быть на главном экране"


async def test_more_screen_hides_admin_tools(api):
    await press(SYS, "more")
    payloads = set(api.payloads(SYS))
    assert {"stats", "broadcast", "schedules", "groups", "settings", "diag", "sysadm"} <= payloads
    assert "view:admin" in payloads and "view:student" in payloads and "view:staff" in payloads


async def test_more_screen_is_closed_for_others(api):
    """Студент не должен попасть в служебный экран: вместо него - его меню."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "more")
    payloads = set(api.payloads(STUDENT))
    assert "settings" not in payloads and "diag" not in payloads
    assert "new:feedback" in payloads  # обычное меню студента


async def test_switch_to_student_view(api):
    await press(SYS, "sysadm")
    await press(SYS, "view:student")
    text = api.last(SYS)[1]
    assert "Режим студента" in text
    payloads = set(api.payloads(SYS))
    assert "new:feedback" in payloads and "view:admin" in payloads
    assert await db.get_setting(f"menu_view:{SYS}") == "student"


async def test_student_view_persists_after_restart(api):
    await press(SYS, "view:student")
    await db.init_db()  # перезапуск
    await press(SYS, "home")
    assert "Режим студента" in api.last(SYS)[1]


async def test_switch_to_staff_view(api):
    await add_staff(SYS, "Сис-админ")
    await press(SYS, "view:staff")
    text = api.last(SYS)[1]
    assert "Режим сотрудника" in text
    assert "staff" in api.payloads(SYS)


async def test_switch_back_to_admin_view(api):
    await press(SYS, "view:student")
    api.sent.clear()
    await press(SYS, "view:admin")
    assert "Панель сис-админа" in api.last(SYS)[1]
    assert "more" in api.payloads(SYS)


async def test_unknown_view_is_ignored(api):
    await press(SYS, "view:что-то")
    assert "Панель сис-админа" in api.last(SYS)[1] or "Кабинет сотрудника" in api.last(SYS)[1]
    assert await db.get_setting(f"menu_view:{SYS}", "admin") == "admin"


async def test_view_is_per_user(api):
    await add_staff(STAFF, "Петрова Анна")
    await press(SYS, "view:student")
    await press(STAFF, "home")
    assert "Режим студента" not in api.last(STAFF)[1]
    assert "Кабинет сотрудника" in api.last(STAFF)[1]


# ── регистрация ───────────────────────────────────────────────────────────────
async def test_registration_suggests_name_from_profile(api):
    await say(STUDENT, "/start")
    await repo.touch_contact(STUDENT, "ivanov", "Иванов Иван Иванович")
    await press(STUDENT, "who:student")
    text = api.last(STUDENT)[1]
    assert "Проверьте ФИО" in text and "Иванов Иван Иванович" in text
    assert "regname:Иванов Иван Иванович" in api.payloads(STUDENT)


async def test_registration_accepts_suggested_name(api):
    await say(STUDENT, "/start")
    await repo.touch_contact(STUDENT, "ivanov", "Иванов Иван Иванович")
    await press(STUDENT, "who:student")
    await press(STUDENT, "regname:Иванов Иван Иванович")
    assert "укажите код группы" in api.last(STUDENT)[1]
    await press(STUDENT, "regpick:ИС-21")
    await press(STUDENT, "regyes")
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["full_name"] == "Иванов Иван Иванович" and row["group_code"] == "ИС-21"


async def test_registration_ignores_non_fio_profile(api):
    await say(STUDENT, "/start")
    await repo.touch_contact(STUDENT, "student2000", "Студент")
    await press(STUDENT, "who:student")
    assert "Укажите ваши ФИО полностью" in api.last(STUDENT)[1]


async def test_registration_offers_group_buttons(api):
    await repo.upsert_group("ИС-21", "Информационные системы")
    await repo.upsert_group("БУХ-20", "Бухгалтерия")
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петрова Анна")
    payloads = api.payloads(STUDENT)
    assert "regpick:ИС-21" in payloads and "regpick:БУХ-20" in payloads


async def test_registration_group_pick_saves_after_confirm(api):
    await repo.upsert_group("ИС-21", "Информационные системы")
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петрова Анна")
    await press(STUDENT, "regpick:ИС-21")
    assert "Проверьте данные" in api.last(STUDENT)[1]
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None
    await press(STUDENT, "regyes")
    assert (await db.one("SELECT group_code FROM users WHERE user_id=?", (STUDENT,)))["group_code"] == "ИС-21"


async def test_registration_can_be_cancelled_and_edited(api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петрова Анна")
    await say(STUDENT, "ИС-21")
    assert "regname:" in api.payloads(STUDENT) and "regpick:" in api.payloads(STUDENT)
    await press(STUDENT, "regname:")
    assert "Укажите ваши ФИО" in api.last(STUDENT)[1] or "укажите код группы" in api.last(STUDENT)[1]


async def test_registration_text_instead_of_button_works(api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петрова Анна")
    await say(STUDENT, "ИС-21")
    await say(STUDENT, "Петрова Анна Петровна")  # исправил ФИО текстом
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["full_name"] == "Петрова Анна Петровна" and row["group_code"] == "ИС-21"


async def test_registration_without_confirmation_keeps_nothing(api):
    """Ключевое: без подтверждения пользователь не появляется в базе."""
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петрова Анна")
    await say(STUDENT, "ИС-21")
    assert await repo.is_registered(STUDENT) is False
