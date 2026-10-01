"""Справочник колледжа и частые вопросы: врезка в меню и панель."""
import pytest
import college

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

from conftest import csrf_of, login_panel, press, register
from handlers import faq

STUDENT = "100"


# ── бот: меню и экраны ──────────────────────────────────────────────────────
async def test_student_menu_has_faq_only(api):
    """В меню студента остались подменю, дела и частые вопросы - контакта там нет."""
    await register(STUDENT)
    await press(STUDENT, "home")
    payloads = api.payloads(STUDENT)
    # вопросы ушли с первого экрана внутрь «Создать обращение», но остались
    # достижимы - ровно два нажатия от главного экрана
    assert "faq" not in payloads, "вопросы снова висят на главном экране"
    assert "ticket_menu" in payloads
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    assert "faq" in api.payloads(STUDENT), "вопросы потерялись при переносе"
    assert "college" not in payloads


async def test_contacts_live_inside_faq(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "college" in api.payloads(STUDENT)


async def test_college_screen_shows_contacts(api):
    await register(STUDENT)
    await press(STUDENT, "college")
    text = api.to(STUDENT)[-1][1]
    assert "Лангепасский политехнический колледж" in text
    assert "Ленина" in text


async def test_profile_shows_college_phone(api):
    await register(STUDENT)
    await press(STUDENT, "profile")
    assert "Учебная часть" in api.to(STUDENT)[-1][1]
    assert "college" in api.payloads(STUDENT)


async def test_college_screen_offers_faq(api):
    await register(STUDENT)
    await press(STUDENT, "college")
    assert "faq" in api.payloads(STUDENT)


async def test_overridden_contact_reaches_the_bot(api):
    await college.override("телефон_приёмная", "+7 (000) 000-00-00")
    await register(STUDENT)
    await press(STUDENT, "college")
    assert "000-00-00" in api.to(STUDENT)[-1][1]


# ── панель ──────────────────────────────────────────────────────────────────
async def test_panel_college_page_shows_fields(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/college").text
    assert "Контакты колледжа" in body
    assert "Частые вопросы" in body
    assert college.setting_key("адрес") in body


async def test_panel_saves_contacts(panel_client):
    assert login_panel(panel_client)
    panel_client.get("/panel/college")
    data = {college.setting_key(key): "тест" for key in college.FIELDS}
    data["csrf"] = csrf_of(panel_client)
    panel_client.post("/panel/college", data=data, follow_redirects=False)
    assert await college.get("адрес") == "тест"


async def test_panel_empty_value_returns_to_site_data(panel_client):
    await college.override("адрес", "своё значение")
    assert login_panel(panel_client)
    panel_client.get("/panel/college")
    data = {college.setting_key(key): "" for key in college.FIELDS}
    data["csrf"] = csrf_of(panel_client)
    panel_client.post("/panel/college", data=data, follow_redirects=False)
    assert await college.is_overridden("адрес") is False
    assert await college.get("адрес") != ""


async def test_panel_seeds_questions_from_site(panel_client):
    assert login_panel(panel_client)
    panel_client.post("/panel/faq/seed",
                      data={"csrf": csrf_of(panel_client)}, follow_redirects=False)
    assert len(await faq.active_items()) == len(college.DEFAULT_FAQ)


async def test_panel_shows_questions_after_seed(panel_client):
    assert login_panel(panel_client)
    panel_client.post("/panel/faq/seed", data={"csrf": csrf_of(panel_client)},
                      follow_redirects=False)
    body = panel_client.get("/panel/college").text
    assert college.DEFAULT_FAQ[0]["question"] in body


async def test_panel_toggles_faq_answers(panel_client):
    assert login_panel(panel_client)
    panel_client.post("/panel/faq/toggle",
                      data={"csrf": csrf_of(panel_client), "enabled": "0"},
                      follow_redirects=False)
    assert await faq.ask_enabled() is False
    panel_client.post("/panel/faq/toggle",
                      data={"csrf": csrf_of(panel_client), "enabled": "1"},
                      follow_redirects=False)
    assert await faq.ask_enabled() is True


async def test_panel_toggles_single_question(panel_client):
    assert login_panel(panel_client)
    panel_client.post("/panel/faq/seed", data={"csrf": csrf_of(panel_client)},
                      follow_redirects=False)
    first = (await faq.active_items())[0]
    panel_client.post(f"/panel/faq/{first['id']}/toggle",
                      data={"csrf": csrf_of(panel_client)}, follow_redirects=False)
    assert first["question"] not in [row["question"] for row in await faq.active_items()]
