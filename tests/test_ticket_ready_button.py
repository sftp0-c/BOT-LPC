"""Кнопка «Справка готова» в боте: кабинет 115 и уведомление студенту."""
import pytest

import repository as repo
from conftest import card_more, add_staff, press, register

STUDENT, STAFF = "300", "200"


@pytest.fixture
async def ticket_id(env):          # env из conftest даёт FakeAPI, его не перебиваем
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    return await repo.create_ticket(STUDENT, STAFF, "certificates", "Нужна справка", "Справка")


async def test_ready_button_present_in_staff_card(ticket_id, api):
    адреса, _ = await card_more(api, STAFF, ticket_id)
    assert f"tdready:{ticket_id}" in адреса


async def test_ready_button_sets_cabinet_115(ticket_id, api):
    await press(STAFF, f"tdready:{ticket_id}")
    row = await repo.get_ticket(ticket_id)
    assert row["status"] == "ready"
    assert row["pickup_place"] == "115"


async def test_student_gets_message_with_cabinet(ticket_id, api):
    await press(STAFF, f"tdready:{ticket_id}")
    text = "\n".join(body for uid, body, _ in api.to(STUDENT) if uid == STUDENT)
    assert "кабинете 115" in text


async def test_staff_card_shows_the_message(ticket_id, api):
    await press(STAFF, f"tdready:{ticket_id}")
    card = "\n".join(body for _, body, _ in api.to(STAFF))
    assert "Заберите в кабинете 115" in card


async def test_exception_cabinet_is_kept(ticket_id, api):
    await repo.update_ticket(ticket_id, STAFF, pickup_place="203")
    await press(STAFF, f"tdready:{ticket_id}")
    assert (await repo.get_ticket(ticket_id))["pickup_place"] == "203"
    text = "\n".join(body for uid, body, _ in api.to(STUDENT) if uid == STUDENT)
    assert "кабинете 203" in text


async def test_button_disappears_when_already_ready_with_default(ticket_id, api):
    await press(STAFF, f"tdready:{ticket_id}")
    await press(STAFF, f"t:{ticket_id}")
    assert f"tdready:{ticket_id}" not in api.payloads(STAFF)


async def test_student_has_no_ready_button(ticket_id, api):
    await press(STUDENT, f"t:{ticket_id}")
    assert f"tdready:{ticket_id}" not in api.payloads(STUDENT)


async def test_ready_for_missing_ticket(ticket_id, api):
    await press(STAFF, "tdready:999")
    assert "не найдено" in api.to(STAFF)[-1][1].lower()


async def test_ready_denied_for_student(ticket_id, api):
    await press(STUDENT, f"tdready:{ticket_id}")
    assert (await repo.get_ticket(ticket_id))["status"] == "new"
