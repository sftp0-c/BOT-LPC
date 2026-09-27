"""Кто ответил на обращение: имя сотрудника должно быть видно в боте и в панели."""
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register
from handlers import tickets

STUDENT, STAFF = "300", "200"
TID = 1


@pytest.fixture
async def ticket(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Здравствуйте, нужно справку", "Справка")
    await repo.add_ticket_message(ticket_id, STAFF, "staff", "Справку подготовим к среде")
    return ticket_id


async def test_staff_message_shows_name(ticket):
    rows = await repo.ticket_thread(ticket)
    assert rows[0]["sender_name"] == "Петрова Мария Сергеевна"


async def test_card_shows_staff_name_not_generic_word(ticket, api):
    await press(STAFF, f"t:{ticket}")
    text = "\n".join(body for _, body, _ in api.to(STAFF))
    assert "Петрова Мария Сергеевна" in text
    assert "🏫 Сотрудник" not in text
    assert "Секретарь" in text          # должность по-прежнему полезна


async def test_student_sees_responder_name(ticket, api):
    await press(STUDENT, f"t:{ticket}")
    text = "\n".join(body for _, body, _ in api.to(STUDENT))
    assert "Петрова Мария Сергеевна" in text
    assert "Ответственный: Петрова Мария Сергеевна" in text


async def test_student_message_keeps_own_name(ticket):
    await repo.add_ticket_message(ticket, STUDENT, "student", "Спасибо!")
    rows = await repo.ticket_thread(ticket)
    student = [row for row in rows if row["sender_role"] == "student"]
    assert student and student[0]["sender_name"] == "Иванов Иван Иванович"
    assert student[0]["group_code"] == "24-23"


async def test_panel_thread_shows_name(ticket, panel_client):
    from conftest import login_panel

    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets/{ticket}").text
    assert "Петрова Мария Сергеевна" in body
    assert "🏫 Сотрудник" not in body


async def test_message_author_falls_back_when_staff_row_deleted(ticket):
    """Сотрудник удалён из справочника - подпись должна остаться внятной."""
    await db.run("DELETE FROM admins WHERE user_id=?", (STAFF,))
    rows = await repo.ticket_thread(ticket)
    author = tickets.message_author(rows[0])
    assert author.startswith("🏫")
    assert db is not None


async def test_position_used_when_name_missing(ticket):
    await db.run("UPDATE admins SET full_name='' WHERE user_id=?", (STAFF,))
    rows = await repo.ticket_thread(ticket)
    author = tickets.message_author(rows[0])
    assert "Секретарь" in author
