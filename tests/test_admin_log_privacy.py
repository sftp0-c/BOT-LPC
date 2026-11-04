"""Канарейки в настоящих обработчиках: данные сохраняются, журналы — без значений."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import attachments
import config
import database as db
import repository as repo
from handlers import admin, menus, tickets
from tests.conftest import login_panel, post_form
from web import access, data as data_web, schedules as schedule_web

FIO = "СекретФИО42"
POSITION = "СекретДолжность42"
OFFICE = "СекретКабинет42"
MESSAGE = "СекретТекст42"
TITLE = "СекретНазвание42"
CODE = "K7X9Q2"
URL = "https://example.org/s.pdf?token=canary-url-42"
SECRETS = (FIO, POSITION, OFFICE, MESSAGE, TITLE, CODE, URL, "canary-url-42")


@pytest.fixture
async def privacy_log(env, monkeypatch, caplog):
    """Перехватываем вход настоящего API, не полагаясь на фильтрацию ниже."""
    calls = []
    original = repo.log_action

    async def capture(actor_id, action, details=""):
        calls.append((str(actor_id), action, details))
        await original(actor_id, action, details)

    monkeypatch.setattr(repo, "log_action", capture)
    caplog.set_level(logging.INFO)
    return calls


@pytest.fixture
async def owner_panel(panel_client, env, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", ["1"])
    assert login_panel(panel_client)
    return panel_client


async def assert_safe(calls, caplog, *extra):
    stored = await db.many("SELECT * FROM admin_log ORDER BY id DESC LIMIT 500")
    text = (repr(calls) + caplog.text + repr([dict(row) for row in stored])).casefold()
    for secret in (*SECRETS, *extra):
        assert secret.casefold() not in text, f"Значение попало в журнал: {secret}"


def entry(calls, action, target):
    matched = [details for actor, event, details in calls if event == action and actor == "1"]
    assert matched, f"Не записано событие: {action}"
    assert str(target) in matched[-1], "Потерян идентификатор сущности"
    return matched[-1]


@pytest.mark.parametrize("operation", ["position", "promote", "create"])
async def test_bot_staff_fields_are_private(operation, env, monkeypatch, privacy_log, caplog):
    if operation == "create":
        await db.set_state("1", "make_staff_category", {"id": "20", "name": FIO, "position": POSITION})
        await admin.cb_make_staff_category("1", "feedback")
        details = entry(privacy_log, "сотрудник добавлен", "20")
        assert "full_name" in details and "position" in details
        assert (await repo.get_admin("20"))["full_name"] == FIO
    else:
        await repo.add_staff("20", FIO)
        if operation == "position":
            monkeypatch.setattr(admin, "resolve_position", lambda code: POSITION)
            await admin.cb_staff_position_hint("1", "20:academic")
            assert "position" in entry(privacy_log, "должность изменена", "20")
            assert (await repo.get_admin("20"))["position"] == POSITION
        else:
            await admin.cb_staff_promote("1", "20")
            entry(privacy_log, "права сис-админа выданы", "20")
            assert await repo.is_sysadmin("20")
    await assert_safe(privacy_log, caplog)


@pytest.mark.parametrize("operation", ["position", "vacation", "promote", "delete", "make", "sysadmin", "delete_person", "update"])
async def test_panel_staff_fields_are_private(operation, owner_panel, env, privacy_log, caplog):
    if operation in ("make", "delete_person"):
        await repo.touch_contact("20", display_name=FIO)
        await repo.upsert_user("20", FIO, "ИС-21")
    else:
        await repo.add_staff("20", FIO)
    cases = {
        "position": ("/panel/staff/20/position", {"position_pick": POSITION}, "должность изменена"),
        "vacation": ("/panel/staff/20/vacation", {"until": "2099-12-19"}, "отпуск сотрудника"),
        "promote": ("/panel/staff/promote/20", {}, "права сис-админа выданы"),
        "delete": ("/panel/staff/20/delete", {}, "сотрудник удалён"),
        "make": ("/panel/access/make/20", {"full_name": FIO, "position": POSITION, "office": OFFICE}, "сделан сотрудником из реестра"),
        "sysadmin": ("/panel/access/sysadmin/20", {"full_name": FIO}, "права сис-админа выданы"),
        "delete_person": ("/panel/people/20/delete", {}, ""),
        "update": ("/panel/staff/20", {"full_name": FIO, "position": POSITION, "office": OFFICE, "can_broadcast": "1"}, "карточка сотрудника изменена"),
    }
    path, payload, action = cases[operation]
    assert post_form(owner_panel, path, payload).status_code == 303
    if action:
        details = entry(privacy_log, action, "20")
        if operation == "update":
            assert {"position", "office", "can_broadcast"} <= set(details.split(": ", 1)[1].split(", "))
    if operation in ("position", "make", "update"):
        assert (await repo.get_admin("20"))["position"] == POSITION
    elif operation in ("promote", "sysadmin"):
        assert await repo.is_sysadmin("20")
    elif operation == "vacation":
        assert (await repo.get_admin("20"))["vacation_until"] == "2099-12-19"
    elif operation == "delete":
        assert await repo.get_admin("20") is None
    else:
        assert await repo.get_user("20") is None
        assert "удалён пользователь 20" in caplog.text
    await assert_safe(privacy_log, caplog, "2099-12-19")


async def test_report_is_delivered_but_not_logged(env, privacy_log, caplog):
    await repo.upsert_user("30", FIO, "ИС-21")
    await menus.st_bug_report("30", MESSAGE, {})
    assert any(MESSAGE in text for _, text, _ in env.to("1"))
    assert ("30", "ошибка в боте", "описание получено") in privacy_log
    await assert_safe(privacy_log, caplog)


async def test_real_invite_code_never_reaches_logs(owner_panel, env, monkeypatch, privacy_log, caplog):
    monkeypatch.setattr(access, "gen_code", lambda length: CODE)
    assert post_form(owner_panel, "/panel/access/code", {"user_id": "20", "full_name": FIO}).status_code == 303
    assert await db.one("SELECT 1 FROM staff_invites WHERE code=?", (CODE,))
    await admin.cb_code_ttl("1", f"{CODE}:0")
    assert (await db.one("SELECT expires_at FROM staff_invites WHERE code=?", (CODE,)))["expires_at"] == ""
    assert post_form(owner_panel, "/panel/access/code/delete", {"code": CODE}).status_code == 303
    assert await db.one("SELECT 1 FROM staff_invites WHERE code=?", (CODE,)) is None
    assert {"код сотрудника выдан", "срок кода изменён", "код сотрудника отозван"} <= {action for _, action, _ in privacy_log}
    await assert_safe(privacy_log, caplog)


async def test_ready_preserves_conversation_not_audit_values(env, privacy_log, caplog):
    await repo.upsert_user("30", FIO, "ИС-21")
    await repo.add_staff("20", FIO)
    tid = await repo.create_ticket("30", "20", "feedback", MESSAGE)
    await repo.update_ticket(tid, "20", pickup_place=OFFICE)
    await tickets.cb_ticket_ready("20", str(tid))
    saved = await repo.get_ticket(tid)
    assert saved["status"] == "ready" and saved["pickup_place"] == OFFICE
    thread = await repo.ticket_thread(tid, 100)
    assert any(row["text"] == MESSAGE for row in thread)
    assert any(OFFICE in row["text"] for row in thread)
    assert any(OFFICE in text for _, text, _ in env.to("30"))
    assert any(action == "документ готов" and str(tid) in details and "pickup_place" in details
               for _, action, details in privacy_log)
    await assert_safe(privacy_log, caplog)


async def test_attachment_failure_logs_id_and_error_type(env, monkeypatch, privacy_log, caplog):
    tid = await repo.create_ticket("30", "20", "feedback", MESSAGE)
    await db.set_state("30", "attach_file", {"tid": tid})
    monkeypatch.setattr(attachments, "download_attachment", AsyncMock(side_effect=ValueError(URL)))
    await tickets.on_attachment("30", [{"url": URL, "name": TITLE}])
    assert str(tid) in caplog.text and "ValueError" in caplog.text
    await assert_safe(privacy_log, caplog)


@pytest.mark.parametrize("operation", ["save", "parse"])
async def test_schedule_url_and_reason_are_not_logged(operation, owner_panel, monkeypatch, privacy_log, caplog):
    monkeypatch.setattr(schedule_web, "probe_pdf_url", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(schedule_web.schedules, "parse_group", AsyncMock(return_value=SimpleNamespace(has_lessons=False, reason=URL)))
    await repo.upsert_schedule("ИС-21", URL)
    assert post_form(owner_panel, f"/panel/schedules/{operation}", {"group_code": "ИС-21", "pdf_url": URL}).status_code == 303
    action = "ссылка на расписание сохранена" if operation == "save" else "разбор расписания не удался"
    details = entry(privacy_log, action, "ИС-21")
    if operation == "save":
        assert "pdf_url" in details
    assert (await repo.get_schedule("ИС-21"))["pdf_url"] == URL
    await assert_safe(privacy_log, caplog)


async def test_lesson_time_logs_field_names(owner_panel, privacy_log, caplog):
    await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject) VALUES('ИС-21',0,1,?)", (TITLE,))
    assert post_form(owner_panel, "/panel/schedules/lesson-time", {"group_code": "ИС-21", "weekday": "0", "lesson_num": "1", "start": "08:13", "end": "09:17"}).status_code == 303
    details = entry(privacy_log, "время пары исправлено", "ИС-21")
    assert "start" in details and "end" in details and "пара 1" in details
    row = await db.one("SELECT start, end FROM lessons WHERE group_code='ИС-21'")
    assert (row["start"], row["end"]) == ("08:13", "09:17")
    await assert_safe(privacy_log, caplog, "08:13", "09:17")


@pytest.mark.parametrize("operation", ["probe", "bot_import", "panel_import", "api", "snapshot"])
async def test_technical_failures_keep_type_not_error_text(operation, owner_panel, env, monkeypatch, privacy_log, caplog):
    failure = ValueError(f"{URL} {MESSAGE}")
    if operation == "probe":
        monkeypatch.setattr(admin, "probe_client", Mock(side_effect=failure))
        assert not (await admin.probe_pdf_url(URL))[0]
    elif operation in ("bot_import", "panel_import"):
        monkeypatch.setattr(schedule_web.schedule_import, "import_sources", AsyncMock(side_effect=failure))
        if operation == "bot_import":
            await admin._run_import("1")
        else:
            assert post_form(owner_panel, "/panel/schedules/import", {"source": URL}).status_code == 303
    elif operation == "api":
        monkeypatch.setattr(admin.api, "me", AsyncMock(side_effect=failure), raising=False)
        await admin.check_api("1")
    else:
        monkeypatch.setattr(db, "backup_to", AsyncMock(side_effect=failure))
        assert await data_web.snapshot("проверка") == ""
    assert "ValueError" in caplog.text
    await assert_safe(privacy_log, caplog)


@pytest.mark.parametrize("table,payload,field", [
    ("reply_templates", {"title": TITLE, "text": MESSAGE}, "text"),
    ("staff_invites", {"code": CODE, "full_name": FIO}, "full_name"),
    ("settings", {"key": "privacy_probe", "value": MESSAGE}, "value"),
])
async def test_raw_row_lifecycle_logs_only_fields(table, payload, field, owner_panel, privacy_log, caplog):
    response = post_form(owner_panel, f"/panel/data/table/{table}/row", payload)
    assert response.status_code == 303
    rowid = response.headers["location"].rsplit("/", 1)[-1]
    assert rowid.isdigit()
    row = await db.one(f'SELECT * FROM "{table}" WHERE rowid=?', (int(rowid),))
    assert row[field] == payload[field]
    changed = payload[field] + " исправлено"
    assert post_form(owner_panel, f"/panel/data/table/{table}/row/{rowid}", {field: changed}).status_code == 303
    assert (await db.one(f'SELECT * FROM "{table}" WHERE rowid=?', (int(rowid),)))[field] == changed
    assert post_form(owner_panel, f"/panel/data/table/{table}/row/{rowid}/delete", {"word": table}).status_code == 303
    assert await db.one(f'SELECT 1 FROM "{table}" WHERE rowid=?', (int(rowid),)) is None
    for action in ("добавлена строка данных", "правка строки данных", "удалена строка данных"):
        details = entry(privacy_log, action, rowid)
        assert table in details and field in details
    await assert_safe(privacy_log, caplog)


@pytest.mark.parametrize("operation", ["create", "save"])
async def test_raw_row_errors_do_not_log_values(operation, owner_panel, monkeypatch, privacy_log, caplog):
    rowid = await db.run("INSERT INTO settings(key, value) VALUES('privacy_failure', 'до')")
    original = db.run

    async def fail_write(sql, params=()):
        if "settings" in sql and sql.lstrip().startswith(("INSERT", "UPDATE")):
            raise ValueError(f"{MESSAGE} {URL}")
        return await original(sql, params)

    monkeypatch.setattr(db, "run", fail_write)
    monkeypatch.setattr(data_web, "snapshot", AsyncMock(return_value=""))
    path = "/panel/data/table/settings/row" + (f"/{rowid}" if operation == "save" else "")
    assert post_form(owner_panel, path, {"key": "privacy_new", "value": MESSAGE}).status_code == 303
    assert (await db.one("SELECT value FROM settings WHERE rowid=?", (rowid,)))["value"] == "до"
    assert "ValueError" in caplog.text
    await assert_safe(privacy_log, caplog)


async def test_legacy_audit_columns_hidden_without_deletion(owner_panel):
    legacy = ("target", "details", "ip", "ua")
    present = {row["name"] for row in await db.many("PRAGMA table_info(audit_log)")}
    for name in legacy:
        if name not in present:
            await db.run(f'ALTER TABLE audit_log ADD COLUMN "{name}" TEXT')
    rowid = await db.run("INSERT INTO audit_log(user_id, action, target, details, ip, ua) VALUES('1', 'legacy_probe', ?, ?, ?, ?)", (URL, MESSAGE, FIO, TITLE))
    safe = {"id", "created_at", "user_id", "role", "action", "target_type", "target_id", "result"}
    columns = await data_web.table_columns("audit_log")
    assert {column["name"] for column in columns} == safe
    assert {column["name"] for column in await data_web.table_columns("settings")} == {"key", "value"}
    assert data_web.filters_of(columns, "details", MESSAGE, "ip", FIO) == ("", ())
    page = owner_panel.get("/panel/data/table/audit_log")
    csv = owner_panel.get("/panel/data/table.csv", params={"name": "audit_log"})
    assert page.status_code == csv.status_code == 200
    assert "legacy_probe" in page.text and "legacy_probe" in csv.text
    for secret in (URL, MESSAGE, FIO, TITLE):
        assert secret not in page.text and secret not in csv.text
    original = await db.one("SELECT target, details, ip, ua FROM audit_log WHERE rowid=?", (rowid,))
    assert tuple(original) == (URL, MESSAGE, FIO, TITLE)
