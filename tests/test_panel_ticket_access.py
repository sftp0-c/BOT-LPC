"""Тесты настройки «видит все обращения» в панели."""
import pytest

import repository as repo
from conftest import add_staff, login_panel, post_form, register

STUDENT, STAFF, OTHER = "300", "200", "201"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    await add_staff(OTHER, "Сидоров Пётр Петрович", position="Кассир")
    return env


async def test_toggle_is_visible_on_the_page(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    assert "Кто видит чужие обращения" in body
    assert STAFF in body and OTHER in body


async def test_toggle_grants_and_revokes_access(panel_client, env):
    assert login_panel(panel_client)
    assert not await repo.staff_sees_all(STAFF)
    post_form(panel_client, "/panel/tickets/see-all", {"user_id": STAFF, "value": "1"})
    assert await repo.staff_sees_all(STAFF)
    post_form(panel_client, "/panel/tickets/see-all", {"user_id": STAFF})
    assert not await repo.staff_sees_all(STAFF)


async def test_toggle_is_written_to_journal(panel_client, env):
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/see-all", {"user_id": OTHER, "value": "1"})
    actions = " ".join(item["action"] for item in await repo.admin_log(10))
    assert "доступ к чужим обращениям" in actions


async def test_toggle_for_unknown_staff_warns(panel_client, env):
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/see-all", {"user_id": "999", "value": "1"})
    assert "не найден" in panel_client.get("/panel/tickets").text


async def test_toggle_requires_csrf(panel_client, env):
    assert login_panel(panel_client)
    response = panel_client.post("/panel/tickets/see-all",
                                 data={"user_id": STAFF, "value": "1", "csrf": "wrong"})
    assert response.status_code == 403
    assert not await repo.staff_sees_all(STAFF)


async def test_bot_queue_follows_the_flag(api, env):
    """Право влияет и на очередь бота: сотрудник видит чужие только с галочкой."""
    from conftest import press

    ticket_id = await repo.create_ticket(STUDENT, OTHER, "feedback", "Обращение соседа", "Справка")
    await press(STAFF, "staff")
    assert f"t:{ticket_id}" not in api.payloads(STAFF)

    await repo.set_staff_see_all(STAFF, True)
    await press(STAFF, "staff")
    assert f"t:{ticket_id}" in api.payloads(STAFF)
