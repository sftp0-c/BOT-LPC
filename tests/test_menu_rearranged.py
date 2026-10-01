"""Три недавних решения: жалоба, контакты и режимы просмотра."""
import database as db
import repository as repo
from conftest import add_staff, press, register

SYS = "1"
STAFF = "200"
STUDENT = "300"


# ── жалоба на ошибку живёт в «Обратной связи» ──────────────────────────────
async def test_error_report_only_in_feedback_submenu(api):
    await register(STUDENT)
    await press(STUDENT, "home")
    assert "bugreport" not in api.payloads(STUDENT)
    await press(STUDENT, "sub:fb")
    assert "bugreport" in api.payloads(STUDENT)


async def test_error_report_reachable_without_roles(api):
    """Должности могут быть не назначены - жалоба должна остаться доступной."""
    await register(STUDENT)
    await press(STUDENT, "sub:fb")
    assert "bugreport" in api.payloads(STUDENT)
    await press(STUDENT, "bugreport")
    assert "Опишите одним сообщением" in api.last(STUDENT)[1]


# ── контакты колледжа внутри частых вопросов ────────────────────────────────
async def test_contacts_button_inside_faq(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "college" in api.payloads(STUDENT)


async def test_contacts_button_when_faq_is_empty(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "college" in api.payloads(STUDENT)


async def test_contacts_still_open_the_reference(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    await press(STUDENT, "college")
    assert "Ленина" in api.last(STUDENT)[1]


# ── режим просмотра только у сис-админа ─────────────────────────────────────
async def test_staff_menu_has_no_view_switcher(api):
    await add_staff(STAFF, "Петрова Мария", category="all")
    await press(STAFF, "home")
    payloads = api.payloads(STAFF)
    assert not [p for p in payloads if p.startswith("view:")]


async def test_staff_cannot_switch_mode(api):
    await add_staff(STAFF, "Петрова Мария", category="all")
    await press(STAFF, "view:student")
    assert "Режим просмотра меняет только сис-админ" in api.last(STAFF)[1]
    assert "sub:cert" not in api.payloads(STAFF)
    assert await db.get_setting(f"menu_view:{STAFF}") != "student"


async def test_staff_view_command_is_refused(api):
    import bot
    from conftest import msg

    await add_staff(STAFF, "Петрова Мария", category="all")
    await bot.process(msg(STAFF, "/view:student"))
    assert "сис-админ" in api.last(STAFF)[1]


async def test_stale_student_mode_is_reset_for_staff(api):
    """Раньше сотрудник мог остаться в режиме студента - теперь это сбрасывается."""
    await add_staff(STAFF, "Петрова Мария", category="all")
    await db.set_setting(f"menu_view:{STAFF}", "student")
    await press(STAFF, "home")
    assert "staff" in api.payloads(STAFF)
    assert "sub:cert" not in api.payloads(STAFF)
    assert await db.get_setting(f"menu_view:{STAFF}") == "staff"


async def test_sysadmin_keeps_all_three_modes(api):
    await repo.grant_sysadmin(SYS, "Иванов Иван Иванович")
    await press(SYS, "home")
    assert "view:student" in api.payloads(SYS)
    await press(SYS, "view:student")
    assert "view:admin" in api.payloads(SYS)     # можно вернуться
    await press(SYS, "view:staff")
    assert "staff" in api.payloads(SYS)
    await press(SYS, "view:admin")
    # в режиме сис-админа открывается системное меню, а не переключатель
    assert "settings" in " ".join(api.payloads(SYS)) or "more" in api.payloads(SYS)
    assert await db.get_setting(f"menu_view:{SYS}") == "admin"


async def test_sysadmin_view_command_still_works(api):
    import bot
    from conftest import msg

    await repo.grant_sysadmin(SYS, "Иванов Иван Иванович")
    await bot.process(msg(SYS, "/view:student"))
    assert "ticket_menu" in api.payloads(SYS)
    assert await db.get_setting(f"menu_view:{SYS}") == "student"
