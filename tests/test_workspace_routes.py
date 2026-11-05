"""Полные HTTP-сценарии кабинета: код MAX, права, формы, фильтры и возвраты."""
import csv
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import repository as repo
from conftest import add_staff, login_panel
from web import workspace, workspace_auth

ORIGIN = "https://workspace.example"


@pytest.fixture
async def data(env, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-panel-pass")
    await repo.upsert_group("ДЕМО-21", "Демонстрационная группа")
    await repo.upsert_user("100", "Студент Первый", "ДЕМО-21")
    await repo.upsert_user("101", "Студент Второй", "ДЕМО-21")
    await add_staff("200", "Сотрудник Первый", category="all", position="Куратор")
    await add_staff("201", "Сотрудник Второй", category="all", position="Секретарь")
    assert await repo.set_staff_web_access("200", True, ["reply", "status", "note", "forward", "templates", "attachments", "export"], "1")
    assert await repo.set_staff_web_access("201", True, [], "1")
    first = await repo.create_ticket("100", "200", "feedback", "Вопрос первого студента", "Учебный вопрос")
    second = await repo.create_ticket("101", "201", "feedback", "Чужой секретный вопрос", "Другой вопрос")
    return {"first": first, "second": second, "api": env}


@pytest.fixture
def client(data):
    return TestClient(bot.app, base_url=ORIGIN, follow_redirects=False)


def hidden(source, name):
    text = source.text if hasattr(source, "text") else source
    found = re.search(r'name="' + re.escape(name) + r'" value="([^"]*)"', text)
    assert found, name
    return html.unescape(found[1])


def login_staff(client, data, user="200"):
    page = client.get("/workspace/login")
    request = client.post("/workspace/login/request", data={"csrf": hidden(page, "csrf"), "user_id": user}, headers={"Origin": ORIGIN})
    code = re.search(r"\b[0-9]{8}\b", data["api"].last(user)[1])[0]
    response = client.post("/workspace/login/verify", data={"csrf": hidden(request, "csrf"),
                           "challenge": hidden(request, "challenge"), "code": code}, headers={"Origin": ORIGIN})
    assert response.status_code == 303
    return client.cookies.get(workspace_auth.STAFF_WEB_COOKIE)


def action_form(client, ticket_id, **values):
    page = client.get(f"/workspace/tickets/{ticket_id}")
    assert page.status_code == 200
    return {"csrf": hidden(page, "csrf"), "revision": hidden(page, "revision"),
            "operation_key": hidden(page, "operation_key"), **values}


def submit(client, ticket_id, values):
    return client.post(f"/workspace/tickets/{ticket_id}/action", data=values, headers={"Origin": ORIGIN})


async def test_workspace_contains_only_own_tickets_and_never_admin_navigation(client, data):
    login_staff(client, data)
    page = client.get("/workspace/tickets")
    assert page.status_code == 200
    assert "Вопрос первого студента" in page.text
    assert "Чужой секретный вопрос" not in page.text
    assert 'href="/panel/data"' not in page.text and 'href="/panel/staff-access"' not in page.text
    assert 'data-palette-open' not in page.text
    assert page.headers["cache-control"] == "no-store"
    for path in (f'/workspace/tickets/{data["second"]}', f'/workspace/tickets?t={data["second"]}'):
        response = client.get(path)
        assert response.status_code == 404 and "Чужой секретный вопрос" not in response.text
    for path in ("/panel/", "/panel/data", "/panel/database", "/panel/staff-access", "/panel/search?q=Студент", "/panel/tickets.csv"):
        response = client.get(path)
        assert response.status_code in (303, 403), path


async def test_reply_is_current_actor_and_replay_never_sends_twice(client, data):
    login_staff(client, data)
    tid = data["first"]
    values = action_form(client, tid, action="reply", text="Понятный ответ студенту")
    sent = len(data["api"].sent)
    response = submit(client, tid, values)
    assert response.status_code == 303
    row = await db.one("SELECT sender_id,sender_role,text FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC", (tid,))
    assert dict(row) == {"sender_id": "200", "sender_role": "staff", "text": "Понятный ответ студенту"}
    assert len(data["api"].sent) == sent + 1
    repeat = submit(client, tid, values)
    assert repeat.status_code == 303
    assert len(data["api"].sent) == sent + 1
    changed = submit(client, tid, {**values, "text": "Другой ответ"})
    assert changed.status_code == 409
    assert "Другой ответ" in changed.text
    audit = await db.one("SELECT * FROM audit_log WHERE action='workspace_reply' ORDER BY id DESC")
    assert audit["role"] == "staff" and audit["target_id"] == str(tid)
    assert "Понятный ответ" not in str(dict(audit))


@pytest.mark.parametrize("changed", [{"as_staff": "201"}, {"target_admin_id": "201"}, {"action": "delete"}, {"operation_key": "подделка"}, {"csrf": ""}])
async def test_forged_action_has_no_effect(client, data, changed):
    login_staff(client, data)
    tid = data["first"]
    values = action_form(client, tid, action="reply", text="Не должен уйти")
    before = len(data["api"].sent)
    response = submit(client, tid, {**values, **changed})
    assert response.status_code in (400, 403)
    assert len(data["api"].sent) == before
    assert not await db.one("SELECT id FROM ticket_messages WHERE text=?", ("Не должен уйти",))


async def test_operation_cannot_move_to_another_ticket_or_browser(client, data):
    login_staff(client, data)
    own2 = await repo.create_ticket("100", "200", "feedback", "Второе своё обращение")
    values = action_form(client, data["first"], action="reply", text="Не перенести в другой вопрос")
    assert submit(client, own2, values).status_code == 403
    other = TestClient(bot.app, base_url=ORIGIN, follow_redirects=False)
    login_staff(other, data)
    other_page = other.get(f'/workspace/tickets/{data["first"]}')
    values["csrf"] = hidden(other_page, "csrf")
    assert submit(other, data["first"], values).status_code == 403


async def test_revoked_access_cannot_use_open_form(client, data):
    login_staff(client, data)
    tid = data["first"]
    values = action_form(client, tid, action="reply", text="После снятия доступа")
    assert await repo.set_staff_web_access("200", False, [], "1")
    before = len(data["api"].sent)
    assert submit(client, tid, values).status_code == 303
    assert len(data["api"].sent) == before
    assert not await db.one("SELECT id FROM ticket_messages WHERE text=?", (values["text"],))
    assert await repo.set_staff_web_access("200", True, ["reply"], "1")
    assert client.get(f"/workspace/tickets/{tid}").status_code == 303


async def test_template_preview_and_note_are_not_student_messages(client, data):
    login_staff(client, data)
    tid = data["first"]
    template = await repo.add_template("Проверка", "Здравствуйте, {ФИО}! Группа {группа}.", "feedback", "1")
    values = action_form(client, tid, action="template_preview", template_id=str(template), text="")
    before = len(data["api"].sent)
    preview = client.post(f"/workspace/tickets/{tid}/action", data=values,
                          headers={"Origin": ORIGIN, "Accept": "text/plain, application/json"})
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("text/plain")
    assert "Студент Первый" in preview.text and "{ФИО}" not in preview.text
    assert len(data["api"].sent) == before
    values = action_form(client, tid, action="note", text="Только внутренняя заметка")
    assert submit(client, tid, values).status_code == 303
    assert len(data["api"].sent) == before
    assert await db.one("SELECT id FROM ticket_events WHERE event='note' AND detail=?", (values["text"],))
    assert not await db.one("SELECT id FROM ticket_messages WHERE text=?", (values["text"],))


async def test_delivery_failure_keeps_saved_reply_without_retry(client, data):
    login_staff(client, data)
    tid = data["first"]
    data["api"].blocked.add("100")
    values = action_form(client, tid, action="reply", text="Сохраняется даже без доставки")
    response = submit(client, tid, values)
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and "доставка уведомления в MAX не подтверждена" in page.text
    rows = await db.many("SELECT id FROM ticket_messages WHERE text=?", (values["text"],))
    assert len(rows) == 1
    assert submit(client, tid, values).status_code == 303
    assert len(await db.many("SELECT id FROM ticket_messages WHERE text=?", (values["text"],))) == 1


async def test_queue_pagination_selected_ticket_and_csv_share_scope(client, data):
    login_staff(client, data)
    for index in range(28):
        await repo.create_ticket("100", "200", "feedback", f"Своё обращение {index}")
    page = client.get("/workspace/tickets?page=2")
    assert page.status_code == 200 and "Страница" in page.text
    assert "Чужой секретный вопрос" not in page.text
    direct = client.get(f'/workspace/tickets?tab=open&page=1&t={data["first"]}')
    assert direct.status_code == 200 and "Вопрос первого студента" in direct.text
    exported = client.get("/workspace/tickets.csv?tab=open")
    rows = list(csv.reader(io.StringIO(exported.text.lstrip("\ufeff")), delimiter=";"))
    assert exported.status_code == 200 and len(rows) == 30
    assert all("Чужой секретный вопрос" not in row for row in rows)
    form = action_form(client, data["first"], action="reply", text="Фильтры сохранены",
                       **{"return": f'/workspace/tickets?tab=open&page=2&q=Своё&t={data["first"]}'})
    response = submit(client, data["first"], form)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/workspace/tickets"
    assert parse_qs(location.query)["page"] == ["2"]
    assert parse_qs(location.query)["q"] == ["Своё"]


async def test_read_only_has_no_mutating_actions_or_export(client, data):
    login_staff(client, data, "201")
    response = client.get(f'/workspace/tickets/{data["second"]}')
    assert response.status_code == 200
    assert 'data-ws-action-form' not in response.text
    assert client.get("/workspace/tickets.csv").status_code == 403
    assert 'href="/panel/' not in response.text


async def test_admin_workspace_still_has_all_rows_and_separate_admin_screen(client, data):
    assert login_panel(client)
    page = client.get(f'/panel/tickets?t={data["second"]}')
    assert page.status_code == 200 and "Чужой секретный вопрос" in page.text
    assert 'data-workspace' in page.text and 'class="wb-edit"' not in page.text
    assert f'/panel/tickets/{data["second"]}/admin' in page.text
    administration = client.get(f'/panel/tickets/{data["second"]}/admin')
    assert administration.status_code == 200 and 'name="as_staff"' in administration.text
    assert 'class="wb-edit"' in administration.text
    assert client.get(f'/panel/tickets/{data["second"]}').status_code == 200


@pytest.mark.parametrize("table", ["staff_web_access", "staff_web_sessions", "staff_web_login_challenges", "staff_web_rate_buckets", "workspace_operations", "ticket_files"])
async def test_raw_auth_data_never_available_even_to_owner(client, data, monkeypatch, table):
    monkeypatch.setattr(config, "ROOT_IDS", ["1"])
    assert login_panel(client)
    assert client.get("/panel/data/table/" + table).status_code == 404


def test_static_allowlist_and_preserved_landing(client):
    for name in ("workspace.css", "workspace.js"):
        response = client.get("/workspace/assets/" + name)
        assert response.status_code == 200 and response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/workspace/assets/.env").status_code == 404
    assert "Колледж ближе" in client.get("/").text
    assert "Сотрудник — войти через MAX" in client.get("/panel/login").text
    assert workspace.workspace_filters({"tab": "closed"})[0]["view"] == "closed"



def test_expired_login_form_has_readable_recovery(client):
    response = client.post("/workspace/login/verify", data={"code": "00000000"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Открыть новую форму входа" in response.text
    assert "application/json" not in response.headers["content-type"]


def test_access_success_is_not_claimed_by_query_parameter(client):
    assert login_panel(client)
    response = client.get("/panel/staff-access/200?done=1")
    assert response.status_code == 200
    assert "Изменения сохранены. Прежние сессии" not in response.text



def test_admin_bulk_form_has_csrf_checked_ids_and_full_return(client, data):
    assert login_panel(client)
    response = client.get("/panel/tickets", params={"q": "Студент", "category": "feedback"})
    assert response.status_code == 200
    form = re.search(r'<form id="ws-bulk-form".*?</form>', response.text, re.S)
    assert form and 'action="/panel/tickets/bulk"' in form[0]
    assert 'name="csrf"' in form[0] and 'name="return"' in form[0]
    value = html.unescape(re.search(r'name="return" value="([^"]+)"', form[0])[1])
    parsed = urlsplit(value)
    assert parsed.path == "/panel/tickets" and parse_qs(parsed.query)["q"] == ["Студент"]
    assert 'form="ws-bulk-form"' in response.text and 'name="all"' in form[0]
