"""Демо-стенд: переключатель в панели и работа в боте."""
import bot
from conftest import csrf_of, login_panel, msg, press, register, say
from handlers import demo

SYS = "1"
STUDENT = "100"


# ── панель ──────────────────────────────────────────────────────────────────
async def test_panel_shows_demo_toggle(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/settings").text
    assert "Демо-стенд" in body
    assert "/panel/settings/demo" in body


async def test_panel_switches_demo_off_and_on(panel_client):
    assert login_panel(panel_client)
    panel_client.post("/panel/settings/demo",
                      data={"csrf": csrf_of(panel_client), "enabled": "0"},
                      follow_redirects=False)
    assert await demo.is_demo() is False
    panel_client.post("/panel/settings/demo",
                      data={"csrf": csrf_of(panel_client), "enabled": "1"},
                      follow_redirects=False)
    assert await demo.is_demo() is True


async def test_panel_toggle_needs_csrf(panel_client):
    assert login_panel(panel_client)
    assert panel_client.post("/panel/settings/demo", data={"enabled": "1"}).status_code == 403
    assert await demo.is_demo() is False


# ── бот ─────────────────────────────────────────────────────────────────────
async def test_sysadmin_switches_demo_from_menu(api):
    """Кнопка «Демо-стенд» - экран переключателя, а не включение сразу."""
    await press(SYS, "demo")
    assert await demo.is_demo() is False
    assert "demoon" in api.payloads(SYS)
    await press(SYS, "demoon")
    assert await demo.is_demo() is True


async def test_exit_button_turns_it_off(api):
    await press(SYS, "demoon")
    await press(SYS, "demooff")
    assert await demo.is_demo() is False


async def test_home_button_leaves_demo(api):
    await press(SYS, "demoon")
    await press(SYS, "home")
    assert await demo.is_demo() is False


async def test_demo_command_turns_it_off(api):
    await press(SYS, "demoon")
    assert await demo.is_demo() is True
    await bot.process(msg(SYS, "/demo_off"))
    assert await demo.is_demo() is False
    assert "выключен" in api.to(SYS)[-1][1]


async def test_student_cannot_enter_demo(api):
    await register(STUDENT)
    await press(STUDENT, "demo")
    assert await demo.is_demo() is False
    assert "сис-админ" in api.to(STUDENT)[-1][1]


async def test_commands_still_work_in_demo(api):
    await press(SYS, "demoon")
    api.sent.clear()
    await say(SYS, "/start")
    assert api.to(SYS), "в демо команды-команды должны работать"
