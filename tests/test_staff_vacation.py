"""Отпуск сотрудника: обращения уходят заместителю, студент знает об этом."""
import pytest
from datetime import date, timedelta

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
from conftest import add_staff, csrf_of, login_panel, press, register, say

STUDENT = "100"


async def staff_pair():
    """Два сотрудника одной должности и раздела - чтобы нашёлся заместитель."""
    await add_staff("500", "Петрова Анна", position="Секретарь", office="101")
    await add_staff("501", "Сидорова Мария", position="Секретарь", office="102")
    await repo.update_admin("500", role="secretary", ticket_category="certificates")
    await repo.update_admin("501", role="secretary", ticket_category="certificates")


# ── репозиторий ─────────────────────────────────────────────────────────────
async def test_set_vacation_saves_iso_date():
    await add_staff("500", "Петрова Анна")
    assert await repo.set_vacation("500", "05.10.2026") == "2026-10-05"
    assert await repo.on_vacation("500") is True


async def test_vacation_ends_by_itself():
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", (date.today() - timedelta(days=1)).isoformat())
    assert await repo.on_vacation("500") is False


async def test_broken_date_is_not_saved():
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", "завтра")
    assert await repo.on_vacation("500") is False


async def test_return_clears_vacation():
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", "05.10.2026")
    await repo.set_vacation("500", "")
    assert await repo.on_vacation("500") is False


async def test_staff_on_vacation_listed():
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", "05.10.2026")
    assert [row["user_id"] for row in await repo.staff_on_vacation()] == ["500"]


async def test_replacement_is_the_same_role():
    await staff_pair()
    assert (await repo.vacation_replacement(await repo.get_admin("500")))["user_id"] == "501"


async def test_no_replacement_when_alone():
    await add_staff("500", "Петрова Анна", position="Секретарь")
    await repo.update_admin("500", role="secretary", ticket_category="certificates")
    assert await repo.vacation_replacement(await repo.get_admin("500")) is None


async def test_replacement_skips_another_vacation():
    await staff_pair()
    await repo.set_vacation("500", "05.10.2026")
    await repo.set_vacation("501", "05.10.2026")
    assert await repo.vacation_replacement(await repo.get_admin("500")) is None


async def test_vacation_note_is_honest():
    await staff_pair()
    await repo.set_vacation("500", "05.10.2026")
    note = await repo.vacation_note(await repo.get_admin("500"))
    assert "в отпуске до 05.10.2026" in note and "Сидорова Мария" in note


async def test_vacation_note_without_replacement():
    await add_staff("500", "Петрова Анна", position="Секретарь")
    await repo.update_admin("500", role="secretary", ticket_category="certificates")
    await repo.set_vacation("500", "05.10.2026")
    note = await repo.vacation_note(await repo.get_admin("500"))
    assert "аместитель не назначен" in note


async def test_no_vacation_note_when_present():
    await staff_pair()
    assert await repo.vacation_note(await repo.get_admin("500")) == ""


# ── бот ─────────────────────────────────────────────────────────────────────
async def test_bot_tells_student_about_replacement(api):
    await register(STUDENT)
    await staff_pair()
    await repo.set_vacation("500", "05.10.2026")
    await press(STUDENT, "new:certificates")
    await press(STUDENT, "pick:certificates:500:")
    text = api.to(STUDENT)[-1][1]
    assert "в отпуске" in text and "Сидорова Мария" in text


async def test_ticket_goes_to_the_replacement(api):
    await register(STUDENT)
    await staff_pair()
    await repo.set_vacation("500", "05.10.2026")
    await press(STUDENT, "new:certificates")
    await press(STUDENT, "pick:certificates:500:")
    await say(STUDENT, "Нужна справка для военкомата")
    await press(STUDENT, "ticketsend")
    ticket = (await repo.admin_tickets(None, 10))[0]
    assert str(ticket["target_admin_id"]) == "501"
    assert "Ответит заместитель" in api.to(STUDENT)[-1][1]


async def test_without_vacation_staff_gets_the_ticket_itself(api):
    await register(STUDENT)
    await staff_pair()
    await press(STUDENT, "new:certificates")
    await press(STUDENT, "pick:certificates:500:")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    assert str((await repo.admin_tickets(None, 10))[0]["target_admin_id"]) == "500"


# ── панель ──────────────────────────────────────────────────────────────────
async def test_panel_staff_card_has_vacation_form(panel_client):
    assert login_panel(panel_client)
    await add_staff("500", "Петрова Анна")
    body = panel_client.get("/panel/staff/500").text
    assert "Отпуск" in body
    assert "/panel/staff/500/vacation" in body


async def test_panel_sets_and_clears_vacation(panel_client):
    assert login_panel(panel_client)
    await add_staff("500", "Петрова Анна")
    panel_client.post("/panel/staff/500/vacation",
                      data={"csrf": csrf_of(panel_client), "until": "2026-10-05"},
                      follow_redirects=False)
    assert await repo.on_vacation("500") is True
    assert "в отпуске" in panel_client.get("/panel/staff/500").text
    panel_client.post("/panel/staff/500/vacation",
                      data={"csrf": csrf_of(panel_client), "until": ""},
                      follow_redirects=False)
    assert await repo.on_vacation("500") is False


async def test_panel_rejects_broken_date(panel_client):
    assert login_panel(panel_client)
    await add_staff("500", "Петрова Анна")
    panel_client.post("/panel/staff/500/vacation",
                      data={"csrf": csrf_of(panel_client), "until": "послезавтра"},
                      follow_redirects=False)
    assert await repo.on_vacation("500") is False


async def test_staff_list_marks_vacation(panel_client):
    assert login_panel(panel_client)
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", "05.10.2026")
    body = panel_client.get("/panel/staff").text
    # в панели значки - это SVG-иконки, а не эмодзи: главное слово «в отпуске»
    assert "в отпуске" in body
    # панель честно говорит, что замещающего назначать некому
    assert "заместитель не назначен" in body
    assert "🏖" not in body, "в панели остался эмодзи отпуска"
