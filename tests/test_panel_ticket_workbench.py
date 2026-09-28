"""Панель: рабочее место с обращениями (очередь, карточка, массы, архив)."""
import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
from conftest import add_staff, login_panel, post_form, register

STUDENT, STAFF, OTHER = "300", "200", "201"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    await add_staff(OTHER, "Сидоров Пётр Петрович", position="Кассир")
    return env


async def ticket(admin=STAFF, text="Нужна справка") -> int:
    return await repo.create_ticket(STUDENT, admin, "feedback", text, "Справка")


# ── очередь и карточка ───────────────────────────────────────────────────────
async def test_queue_shows_tickets(panel_client, env):
    await ticket()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    assert "Очередь" in body and "Нужна справка" in body
    assert "workbench" in body


async def test_card_opens_next_to_queue(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert f"Обращение №{ticket_id}" in body
    assert "Быстрый ответ" in body
    assert "История изменений" in body
    assert "Иванов Иван Иванович" in body
    # очередь осталась на экране рядом с карточкой
    assert "workbench" in body and "Очередь" in body


async def test_old_card_link_redirects_to_workbench(panel_client, env):
    assert login_panel(panel_client)
    response = panel_client.get("/panel/tickets/1", follow_redirects=False)
    assert response.status_code == 303
    assert "/panel/tickets?t=1" in response.headers["location"]


async def test_filters_narrow_the_queue(panel_client, env):
    await ticket()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets?status=open&q=справка").text
    assert "Очередь" in body


async def test_archive_view_is_reachable(panel_client, env):
    await ticket()
    assert login_panel(panel_client)
    assert "Архив" in panel_client.get("/panel/tickets?view=archive").text


# ── редактирование ───────────────────────────────────────────────────────────
async def test_edit_saves_fields(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/edit", {
        "text_content": "Нужна справка для военной части",
        "target_admin_id": OTHER,
        "category": "certificates",
        "topic": "Справка",
        "pickup_place": "115",
        "ready_until": "к пятнице",
        "status": "in_progress",
    })
    row = await repo.get_ticket(ticket_id)
    assert row["text_content"] == "Нужна справка для военной части"
    assert row["target_admin_id"] == OTHER
    assert row["pickup_place"] == "115"
    assert row["ready_until"] == "к пятнице"
    assert row["status"] == "in_progress"


async def test_edit_does_not_erase_empty_fields(panel_client, env):
    ticket_id = await ticket()
    await repo.update_ticket(ticket_id, "1", topic="Справка", pickup_place="115")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/edit", {
        "text_content": "Нужна справка", "target_admin_id": STAFF, "category": "feedback",
        "topic": "", "pickup_place": "", "ready_until": "", "status": "new",
    })
    row = await repo.get_ticket(ticket_id)
    assert row["topic"] == "Справка"
    assert row["pickup_place"] == "115"


async def test_edits_are_logged_into_history(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/edit", {
        "text_content": "Изменённый текст", "target_admin_id": OTHER, "category": "feedback",
        "topic": "Справка", "pickup_place": "115", "ready_until": "", "status": "new"})
    details = " ".join(str(e["detail"]) for e in await repo.ticket_events(ticket_id))
    assert "текст" in details and OTHER in details


# ── кнопка «Справка готова» ──────────────────────────────────────────────────
async def test_ready_button_sets_cabinet_and_notifies(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/ready")
    row = await repo.get_ticket(ticket_id)
    assert row["status"] == "ready"
    assert row["pickup_place"] == "115"
    messages = await repo.ticket_thread(ticket_id)
    assert any("кабинете 115" in str(m["text"]) for m in messages)


async def test_ready_button_keeps_exception_cabinet(panel_client, env):
    ticket_id = await ticket()
    await repo.update_ticket(ticket_id, "1", pickup_place="203")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/ready")
    assert (await repo.get_ticket(ticket_id))["pickup_place"] == "203"


# ── архив ────────────────────────────────────────────────────────────────────
async def test_archive_hides_ticket_but_keeps_it(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/archive")
    assert await repo.get_ticket(ticket_id) is None
    assert await repo.archive_count() == 1
    body = panel_client.get("/panel/tickets").text
    assert "Нужна справка" not in body
    assert "Нужна справка" in panel_client.get("/panel/tickets?view=archive").text


async def test_restore_returns_ticket_to_queue(panel_client, env):
    ticket_id = await ticket()
    await repo.archive_ticket(ticket_id, "1")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/restore")
    assert [r["ticket_id"] for r in await repo.admin_tickets(None, 50)] == [ticket_id]


# ── массовые действия ────────────────────────────────────────────────────────
async def test_bulk_assign(panel_client, env):
    first, second = await ticket(), await ticket(admin=OTHER)
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/bulk", {
        "tids": f"{first},{second}", "action": "assign", "value": OTHER})
    for ticket_id in (first, second):
        assert (await repo.get_ticket(ticket_id))["target_admin_id"] == OTHER


async def test_bulk_status(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/bulk", {
        "tids": str(ticket_id), "action": "status", "value": "in_progress"})
    assert (await repo.get_ticket(ticket_id))["status"] == "in_progress"


async def test_bulk_archive(panel_client, env):
    first, second = await ticket(), await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/bulk", {
        "tids": f"{first},{second}", "action": "archive", "value": ""})
    assert await repo.admin_tickets(None, 50) == []


async def test_bulk_without_selection_warns(panel_client, env):
    await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/bulk", {"tids": "", "action": "archive"})
    assert "Отметьте" in panel_client.get("/panel/tickets").text


# ── создание из панели ───────────────────────────────────────────────────────
async def test_new_ticket_form(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets/new").text
    assert "Новое обращение" in body
    assert 'name="student_id"' in body and 'name="text_content"' in body


async def test_create_ticket_from_panel(panel_client, env):
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/new", {
        "student_id": STUDENT, "full_name": "Иванов Иван Иванович", "group_code": "24-23",
        "category": "feedback", "target_admin_id": STAFF,
        "text_content": "Обращение из панели", "pickup_place": "115"})
    rows = await repo.admin_tickets(None, 50)
    assert len(rows) == 1
    assert rows[0]["text_content"] == "Обращение из панели"
    assert rows[0]["pickup_place"] == "115"


async def test_create_ticket_requires_id_and_text(panel_client, env):
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/tickets/new", {"student_id": "", "text_content": ""})
    assert "Нужен MAX ID" in panel_client.get("/panel/tickets/new").text


# ── доступ ───────────────────────────────────────────────────────────────────
async def test_workbench_requires_login(panel_client, env):
    response = panel_client.get("/panel/tickets", follow_redirects=False)
    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]


async def test_edit_requires_csrf(panel_client, env):
    ticket_id = await ticket()
    assert login_panel(panel_client)
    response = panel_client.post(f"/panel/tickets/{ticket_id}/edit",
                                 data={"text_content": "взлом", "csrf": "wrong-token"})
    assert response.status_code == 403
    assert (await repo.get_ticket(ticket_id))["text_content"] == "Нужна справка"
