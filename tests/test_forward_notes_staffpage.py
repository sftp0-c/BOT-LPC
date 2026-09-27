"""Пересылка обращений, внутренние заметки, страница сотрудника и CSV."""

import database as db
import repository as repo
from conftest import add_staff, login_panel, press, register, say
from utils import as_str

SYS, STAFF, OTHER, STUDENT = "1", "200", "201", "100"


async def make_ticket(category: str = "feedback", staff: str = STAFF) -> int:
    await register(STUDENT, name="Иванов Иван")
    await add_staff(staff, "Петрова Анна", category="all")
    await press(STUDENT, f"new:{category}")
    await press(STUDENT, f"pick:{category}:{staff}")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    return (await db.one("SELECT ticket_id FROM tickets ORDER BY ticket_id DESC"))["ticket_id"]


# ── пересылка ─────────────────────────────────────────────────────────────────
async def test_forward_changes_executor_and_keeps_author(api):
    ticket = await make_ticket()
    await add_staff(OTHER, "Соколов Иван", category="all")
    await press(STAFF, f"fwd:{ticket}")
    assert f"fwdto:{ticket}:{OTHER}" in api.payloads(STAFF)

    await press(STAFF, f"fwdto:{ticket}:{OTHER}")
    assert f"fwdok:{ticket}:{OTHER}" in api.payloads(STAFF)
    await press(STAFF, f"fwdok:{ticket}:{OTHER}")

    row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket,))
    assert row["target_admin_id"] == OTHER and row["student_id"] == STUDENT
    assert "теперь у Соколов Иван" in api.last(STAFF)[1]
    assert any("Вам передали обращение" in text for _, text, _ in api.to(OTHER))
    # автор не должен получать сообщение о передаче - это внутреннее дело
    assert not any("передали" in text for _, text, _ in api.to(STUDENT))


async def test_forward_with_comment_saves_note(api):
    ticket = await make_ticket()
    await add_staff(OTHER, "Соколов Иван", category="all")
    await press(STAFF, f"fwdto:{ticket}:{OTHER}")
    await say(STAFF, "Тут нужна справка с печатью, передаю бухгалтерии")
    notice = "\n".join(text for _, text, _ in api.to(OTHER))
    assert "Комментарий: Тут нужна справка" in notice
    notes = [row for row in await db.many("SELECT * FROM ticket_events WHERE event='note'")]
    assert any("бухгалтерии" in row["detail"] for row in notes)


async def test_forward_keeps_status(api):
    ticket = await make_ticket()
    await add_staff(OTHER, "Соколов Иван", category="all")
    await press(STAFF, f"st:{ticket}:accepted")
    await press(STAFF, f"fwdok:{ticket}:{OTHER}")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (ticket,)))["status"] == "accepted"


async def test_forward_to_sysadmin_is_refused(api):
    ticket = await make_ticket()
    await repo.add_sysadmin("700")
    done, message = await repo.forward_ticket(ticket, "700", STAFF)
    assert done is False and "Сис-админ не принимает" in message
    assert (await db.one("SELECT target_admin_id FROM tickets WHERE ticket_id=?", (ticket,)))["target_admin_id"] == STAFF


async def test_forward_list_excludes_current_executor(api):
    ticket = await make_ticket()
    await add_staff(OTHER, "Соколов Иван", category="all")
    await press(STAFF, f"fwd:{ticket}")
    payloads = api.payloads(STAFF)
    assert f"fwdto:{ticket}:{OTHER}" in payloads
    assert f"fwdto:{ticket}:{STAFF}" not in payloads


async def test_forward_without_colleagues_explains(api):
    ticket = await make_ticket()
    await press(STAFF, f"fwd:{ticket}")
    assert "Передавать некому" in api.last(STAFF)[1]


async def test_student_cannot_forward(api):
    ticket = await make_ticket()
    api.sent.clear()
    await press(STUDENT, f"fwd:{ticket}")
    assert "Обращение не найдено" in api.last(STUDENT)[1] or not api.to(STUDENT)


# ── заметки ───────────────────────────────────────────────────────────────────
async def test_internal_note_is_saved_and_not_shown_to_student(api):
    ticket = await make_ticket()
    await press(STAFF, f"note:{ticket}")
    assert "увидит только команда" in api.last(STAFF)[1]
    await say(STAFF, "Позвонить в 214, справку с печатью")

    events = await db.many("SELECT * FROM ticket_events WHERE ticket_id=? AND event='note'", (ticket,))
    assert len(events) == 1 and "Позвонить в 214" in events[0]["detail"]
    # студент не должен увидеть заметку ни в ответе, ни в уведомлении
    assert not any("Позвонить в 214" in text for _, text, _ in api.to(STUDENT))
    # но сотрудник видит её в истории обращения
    await press(STAFF, f"t:{ticket}")
    assert "Позвонить в 214" in api.last(STAFF)[1]


async def test_note_button_in_staff_card(api):
    ticket = await make_ticket()
    await press(STAFF, f"t:{ticket}")
    assert f"note:{ticket}" in api.payloads(STAFF)


async def test_student_never_sees_internal_notes(api):
    """Заметка не должна всплыть у студента ни в карточке, ни в истории."""
    ticket = await make_ticket()
    await repo.add_internal_note(ticket, STAFF, "тайная пометка для команды")
    await press(STUDENT, f"t:{ticket}")
    text = "\n".join(body for _, body, _ in api.to(STUDENT))
    assert "тайная пометка" not in text
    assert "Нужна справка" in text  # само обращение при этом видно


async def test_notes_do_not_appear_in_messages(api):
    """Заметка - событие, а не сообщение: она не должна попасть в ленту переписки."""
    ticket = await make_ticket()
    await repo.add_internal_note(ticket, STAFF, "служебная пометка")
    await press(STAFF, f"t:{ticket}")
    messages = await db.many("SELECT * FROM ticket_messages WHERE ticket_id=?", (ticket,))
    assert not any("служебная пометка" in as_str(row["text"]) for row in messages)


# ── панель ────────────────────────────────────────────────────────────────────
async def test_panel_staff_page(panel_client):
    await add_staff(STAFF, "Петрова Анна", position="Секретарь", office="204")
    await make_ticket()
    assert login_panel(panel_client)
    response = panel_client.get(f"/panel/staff/{STAFF}")
    assert response.status_code == 200
    text = response.text
    assert "Петрова Анна" in text and "Секретарь" in text and "204" in text
    assert "обращений за 90 дней" in text and "среднее время ответа" in text
    assert "Последние обращения" in text


async def test_panel_staff_page_of_stranger(panel_client):
    assert login_panel(panel_client)
    assert panel_client.get("/panel/staff/999").status_code == 200
    assert "не найден" in panel_client.get("/panel/staff/999").text


async def test_panel_staff_page_on_empty(panel_client):
    """Страница сотрудника не должна падать, пока обращений нет."""
    assert login_panel(panel_client)
    assert panel_client.get("/panel/staff/200").status_code == 200


async def test_tickets_csv_export(panel_client):
    ticket = await make_ticket()
    assert login_panel(panel_client)
    response = panel_client.get("/panel/tickets.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert "№;Создано;Статус" in text
    assert str(ticket) in text and "Иванов Иван" in text


async def test_tickets_csv_respects_filters(panel_client):
    first = await make_ticket("feedback")
    second = await make_ticket("certificates")
    await db.run("UPDATE tickets SET status='completed' WHERE ticket_id=?", (second,))
    assert login_panel(panel_client)
    done = panel_client.get("/panel/tickets.csv", params={"status": "completed"}).content.decode("utf-8")
    # сверяем по номеру обращения в начале строки, а не по вхождению цифры «1»
    numbers = [line.split(";")[0] for line in done.splitlines()[1:] if line.strip()]
    assert numbers == [str(second)]
    assert str(first) not in numbers


async def test_tickets_csv_link_on_page(panel_client):
    assert login_panel(panel_client)
    assert "/panel/tickets.csv" in panel_client.get("/panel/tickets").text

