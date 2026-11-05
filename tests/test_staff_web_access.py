"""Назначение веб-прав, транзакционная актуальность, триггеры и старые копии БД."""
import asyncio
import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import database as db
from store import web_access as access
from web import common as panel_common
from web import staff_access
from web.router import router


@pytest.fixture
async def staff_rows(env):
    await db.run("INSERT INTO admins(user_id,full_name,role_type,position,see_all_tickets) "
                 "VALUES('7555666777','Мария Сотрудник','staff','Куратор',1)")
    await db.run("INSERT INTO admins(user_id,full_name,role_type,position) "
                 "VALUES('7666777888','Другой Сотрудник','staff','Методист')")
    return env


async def enabled_session(user_id="7555666777"):
    assert await access.set_staff_web_access(user_id, True, access.WEB_WORK_PERMISSIONS, "1")
    now = access._now()
    browser = access.staff_web_hash("test-browser", user_id)
    peer = access.staff_web_hash("test-peer", user_id)
    delivery = await access.request_staff_web_code(user_id, browser, peer, now=now, browser_expires=now + 600)
    token = await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now)
    assert token
    pending = await access.request_staff_web_code(user_id, browser, peer, now=now, browser_expires=now + 600)
    return token, pending, browser, peer, now


@pytest.fixture
def access_client(staff_rows, monkeypatch):
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-pass")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url="https://panel.example", follow_redirects=False)
    token = panel_common.start_session("1")
    client.cookies.set(panel_common.COOKIE, token, domain="panel.example", path="/")
    return client


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


async def test_existing_staff_never_auto_enabled_or_inherits_see_all(staff_rows):
    await db.init_db()
    assert await access.get_staff_web_access("7555666777") is None
    rows = await access.list_staff_web_access()
    assert len(rows) == 2 and all(not row["enabled"] for row in rows)
    assert await access.set_staff_web_access("7555666777", True, [], "1")
    assert (await access.get_staff_web_access("7555666777"))["permissions"] == []


async def test_work_preset_and_explicit_management_implication(staff_rows):
    assert access.WEB_PERMISSIONS == ("reply", "status", "note", "forward", "templates", "attachments", "export", "view_others", "manage_others")
    assert set(access.WEB_WORK_PERMISSIONS) == {"reply", "status", "note", "forward", "templates", "attachments"}
    assert await access.set_staff_web_access("7555666777", True, ["manage_others", "reply"], "1")
    assert (await access.get_staff_web_access("7555666777"))["permissions"] == ["reply", "view_others", "manage_others"]
    for bad in (["sysadmin"], "reply", {"reply": True}, [None]):
        with pytest.raises(ValueError):
            await access.set_staff_web_access("7555666777", True, bad, "1")


@pytest.mark.parametrize("actor", ["7555666777", "7666777888", "missing"])
async def test_staff_cannot_grant_revoke_or_self_escalate(staff_rows, actor):
    assert not await access.set_staff_web_access("7555666777", True, access.WEB_PERMISSIONS, actor)
    assert await access.get_staff_web_access("7555666777") is None
    assert not await access.revoke_staff_web_sessions("7555666777", actor)


@pytest.mark.parametrize("target", ["1", "missing", "0", "fake-test"])
async def test_only_valid_current_non_test_staff_can_be_enabled(staff_rows, target):
    if target in ("0", "fake-test"):
        await db.run("INSERT INTO admins(user_id,full_name,role_type,is_test) VALUES(?,'Тест','staff',1)", (target,))
    assert not await access.set_staff_web_access(target, True, [], "1")


async def test_permissions_update_revokes_sessions_and_pending_codes(staff_rows):
    token, pending, browser, peer, now = await enabled_session()
    session_hash = access.staff_web_session_hash(token)
    before = await access.get_staff_web_access("7555666777")
    assert await access.set_staff_web_access("7555666777", True, ["status"], "1")
    after = await access.get_staff_web_access("7555666777")
    assert after["auth_version"] > before["auth_version"]
    assert after["permissions"] == ["status"]
    assert await access.staff_web_principal(session_hash) is None
    assert await access.verify_staff_web_code(pending["challenge"], pending["code"], browser, peer, now=now) is None
    session = await db.one("SELECT revoked_at FROM staff_web_sessions")
    assert session["revoked_at"] > 0


async def test_revoke_all_keeps_enabled_but_rejects_old_tokens(staff_rows):
    token, pending, browser, peer, now = await enabled_session()
    assert await access.revoke_staff_web_sessions("7555666777", "1")
    assert (await access.get_staff_web_access("7555666777"))["enabled"] == 1
    assert await access.staff_web_principal(access.staff_web_session_hash(token)) is None
    assert await access.verify_staff_web_code(pending["challenge"], pending["code"], browser, peer, now=now) is None
    delivery = await access.request_staff_web_code("7555666777", browser, peer, now=now, browser_expires=now + 600)
    assert await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now)


@pytest.mark.parametrize("change", ["role", "test", "delete", "replace", "id"])
async def test_raw_role_paths_revoke_and_tombstone_prevents_revival(staff_rows, change):
    token, pending, browser, peer, now = await enabled_session()
    before = (await access.get_staff_web_access("7555666777"))["auth_version"]
    if change == "role":
        await db.run("UPDATE admins SET role_type='sysadmin' WHERE user_id='7555666777'")
        await db.run("UPDATE admins SET role_type='staff' WHERE user_id='7555666777'")
    elif change == "test":
        await db.run("UPDATE admins SET is_test=1 WHERE user_id='7555666777'")
        await db.run("UPDATE admins SET is_test=0 WHERE user_id='7555666777'")
    elif change == "id":
        await db.run("UPDATE admins SET user_id='7999999999' WHERE user_id='7555666777'")
        await db.run("INSERT INTO admins(user_id,full_name,role_type) VALUES('7555666777','Возвращён','staff')")
    else:
        if change == "delete":
            await db.run("DELETE FROM admins WHERE user_id='7555666777'")
        await db.run("INSERT OR REPLACE INTO admins(user_id,full_name,role_type) VALUES('7555666777','Возвращён','staff')")
    after = await access.get_staff_web_access("7555666777")
    assert after["enabled"] == 0 and after["auth_version"] > before
    assert await access.staff_web_principal(access.staff_web_session_hash(token)) is None
    assert await access.set_staff_web_access("7555666777", True, access.WEB_WORK_PERMISSIONS, "1")
    assert await access.staff_web_principal(access.staff_web_session_hash(token)) is None
    assert await access.verify_staff_web_code(pending["challenge"], pending["code"], browser, peer, now=now) is None


async def test_non_auth_profile_change_keeps_session_and_principal_is_fresh(staff_rows):
    token, _pending, _browser, _peer, _now = await enabled_session()
    await db.run("UPDATE admins SET full_name='Новое ФИО',position='Новая должность',see_all_tickets=1 WHERE user_id='7555666777'")
    principal = await access.staff_web_principal(access.staff_web_session_hash(token))
    assert principal["full_name"] == "Новое ФИО" and principal["position"] == "Новая должность"
    assert "view_others" not in principal["permissions"]


@pytest.mark.parametrize("column,value", [("enabled", 0), ("auth_version", 900), ("permissions", '"broken"')])
async def test_principal_rechecks_access_row_each_time(staff_rows, column, value):
    token, _pending, _browser, _peer, _now = await enabled_session()
    session_hash = access.staff_web_session_hash(token)
    assert await access.staff_web_principal(session_hash)
    await db.run(f"UPDATE staff_web_access SET {column}=? WHERE user_id='7555666777'", (value,))
    assert await access.staff_web_principal(session_hash) is None
    async with db._conn() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        assert await access.staff_web_principal_in_tx(connection, session_hash) is None
        await connection.rollback()


async def test_atomic_actor_recheck_after_write_lock(staff_rows):
    async with db._conn() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(access.set_staff_web_access("7555666777", True, ["reply"], "1"))
        await asyncio.sleep(0.05)
        await connection.execute("UPDATE admins SET role_type='staff' WHERE user_id='1'")
        await connection.commit()
    assert not await pending
    assert await access.get_staff_web_access("7555666777") is None


async def test_access_admin_page_search_escapes_and_preset_server_validation(access_client):
    page = access_client.get("/panel/staff-access?q=" + "Мария")
    assert page.status_code == 200 and 'Мария Сотрудник' in page.text and 'Другой Сотрудник' not in page.text
    edit = access_client.get("/panel/staff-access/7555666777")
    assert edit.status_code == 200 and 'method="post"' in edit.text
    token = csrf(edit)
    response = access_client.post("/panel/staff-access/7555666777",
                                  data={"csrf": token, "enabled": "1", "preset": "work", "permissions": "manage_others"})
    assert response.status_code == 303
    result = await access.get_staff_web_access("7555666777")
    assert result["permissions"] == list(access.WEB_WORK_PERMISSIONS)
    response = access_client.post("/panel/staff-access/7555666777",
                                  data={"csrf": token, "enabled": "1", "preset": "root"})
    assert response.status_code == 400
    response = access_client.post("/panel/staff-access/7555666777",
                                  data={"csrf": token, "enabled": "1", "preset": "work", "permissions": "sysadmin"})
    assert response.status_code == 400
    rows = await db.many("SELECT action,details FROM admin_log WHERE action='staff_web_access'")
    assert rows and rows[-1]["details"] == "user_id=7555666777; enabled,permissions"
    assert "manage_others" not in rows[-1]["details"]
    assert edit.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("operation", ["save", "revoke"])
async def test_access_admin_posts_require_csrf(access_client, operation):
    assert await access.set_staff_web_access("7555666777", True, [], "1")
    before = await access.get_staff_web_access("7555666777")
    suffix = "/revoke" if operation == "revoke" else ""
    result = access_client.post("/panel/staff-access/7555666777" + suffix,
                                data={"enabled": "1", "preset": "work"})
    assert result.status_code == 403
    assert await access.get_staff_web_access("7555666777") == before


async def test_access_admin_revoke_and_staff_cannot_use_panel(access_client):
    token, _pending, _browser, _peer, _now = await enabled_session()
    edit = access_client.get("/panel/staff-access/7555666777")
    assert access_client.post("/panel/staff-access/7555666777/revoke", data={"csrf": csrf(edit)}).status_code == 303
    assert await access.staff_web_principal(access.staff_web_session_hash(token)) is None
    forged_panel = panel_common.start_session("7555666777")
    access_client.cookies.set(panel_common.COOKIE, forged_panel, domain="panel.example", path="/")
    assert access_client.get("/panel/staff-access").status_code == 403
    assert access_client.get("/panel/staff-access/7666777888").status_code == 403
    assert access_client.post("/panel/staff-access/7666777888",
                              data={"enabled": "1", "preset": "work"}).status_code == 403


async def test_access_list_escapes_html_and_unknown_profile_not_editable(access_client):
    await db.run("UPDATE admins SET full_name=?,position=? WHERE user_id='7555666777'", ('<script>alert(7)</script>', '" onfocus="bad'))
    page = access_client.get("/panel/staff-access/7555666777")
    assert '&lt;script&gt;alert(7)&lt;/script&gt;' in page.text and '<script>alert(7)</script>' not in page.text
    assert access_client.get("/panel/staff-access/1").status_code == 404
    assert access_client.get("/panel/staff-access/unknown").status_code == 404


async def test_access_sql_parameters_and_results_have_private_repr(staff_rows, caplog):
    caplog.set_level(logging.DEBUG)
    assert await access.set_staff_web_access("7555666777", True, access.WEB_PERMISSIONS, "1")
    now = access._now()
    browser, peer = access.staff_web_hash("private", "browser"), access.staff_web_hash("private", "peer")
    delivery = await access.request_staff_web_code("7555666777", browser, peer, now=now, browser_expires=now + 600)
    token = await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now)
    session_hash = access.staff_web_session_hash(token)
    principal = await access.staff_web_principal(session_hash)
    assert "7555666777" not in repr(principal) + repr(delivery)
    for secret in ("7555666777", browser, peer, delivery["challenge"], delivery["code"], token, session_hash):
        assert secret not in caplog.text


async def test_additive_idempotent_migration_and_trigger_health(staff_rows):
    assert not await db.missing_objects()
    async with db._conn() as connection:
        for name in db.EXPECTED_TRIGGERS:
            await connection.execute(f'DROP TRIGGER "{name}"')
        for name in ("workspace_operations", "ticket_files", "staff_web_rate_buckets",
                     "staff_web_login_challenges", "staff_web_sessions", "staff_web_access"):
            await connection.execute(f'DROP TABLE "{name}"')
        await connection.execute("ALTER TABLE tickets DROP COLUMN workspace_revision")
        await connection.commit()
    assert "tickets.workspace_revision" in await db.missing_objects()
    await db.init_db()
    await db.init_db()
    assert not await db.missing_objects()
    assert await access.get_staff_web_access("7555666777") is None
    await db.run("DROP TRIGGER staff_web_admin_deleted")
    assert "триггер staff_web_admin_deleted" in await db.missing_objects()
    await db.repair_schema()
    assert not await db.missing_objects()


async def test_restore_invalidates_sessions_codes_but_keeps_original_backup(staff_rows):
    token, pending, browser, peer, now = await enabled_session()
    path = await db.backup_to()
    version = (await access.get_staff_web_access("7555666777"))["auth_version"]
    assert await access.revoke_staff_web_sessions("7555666777", "1")
    done, safety = await db.restore_from(Path(path).name)
    assert done and "before-restore" in safety
    assert await access.staff_web_principal(access.staff_web_session_hash(token)) is None
    assert await access.verify_staff_web_code(pending["challenge"], pending["code"], browser, peer, now=now) is None
    assert not await db.many("SELECT * FROM staff_web_sessions")
    assert (await access.get_staff_web_access("7555666777"))["auth_version"] > version
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM staff_web_sessions").fetchone()[0] == 1
    assert await db.integrity_check() == "ok"


async def test_restore_old_schema_recovers_and_does_not_auto_enable(staff_rows):
    backup = Path(db.backups_folder()) / "bot-old-workspace.db"
    await db.backup_to(str(backup))
    with sqlite3.connect(backup) as connection:
        for name in db.EXPECTED_TRIGGERS:
            connection.execute(f'DROP TRIGGER "{name}"')
        for name in ("workspace_operations", "ticket_files", "staff_web_rate_buckets",
                     "staff_web_login_challenges", "staff_web_sessions", "staff_web_access"):
            connection.execute(f'DROP TABLE "{name}"')
        connection.execute("ALTER TABLE tickets DROP COLUMN workspace_revision")
        connection.commit()
    assert (await db.restore_from(backup.name))[0]
    assert not await db.missing_objects()
    assert await access.get_staff_web_access("7555666777") is None


async def test_failed_restore_keeps_live_sessions(staff_rows):
    token, _pending, _browser, _peer, _now = await enabled_session()
    assert not (await db.restore_from("bot-missing.db"))[0]
    assert await access.staff_web_principal(access.staff_web_session_hash(token))


async def test_revision_triggers_and_trusted_ticket_file_links(staff_rows):
    ticket = await db.run("INSERT INTO tickets(student_id,target_admin_id,category,text_content) VALUES('80','7555666777','feedback','Тест')")
    other = await db.run("INSERT INTO tickets(student_id,target_admin_id,category,text_content) VALUES('80','7555666777','feedback','Другой')")

    async def revision():
        return (await db.one("SELECT workspace_revision FROM tickets WHERE ticket_id=?", (ticket,)))["workspace_revision"]

    assert await revision() == 0
    await db.run("UPDATE tickets SET status='in_progress' WHERE ticket_id=?", (ticket,))
    assert await revision() == 1
    message = await db.run("INSERT INTO ticket_messages(ticket_id,sender_id,sender_role,text) VALUES(?,'80','student','Тест')", (ticket,))
    assert await revision() == 2
    event = await db.run("INSERT INTO ticket_events(ticket_id,event) VALUES(?,'created')", (ticket,))
    assert await revision() == 3
    file_id = await db.run("INSERT INTO ticket_files(ticket_id,message_id,storage_name,display_name,size) VALUES(?,?,'safe-file.pdf','Файл.pdf',12)", (ticket, message))
    assert await revision() == 4
    with pytest.raises(sqlite3.IntegrityError):
        await db.run("INSERT INTO ticket_files(ticket_id,message_id,storage_name,display_name,size) VALUES(?,?,'wrong.pdf','Файл.pdf',12)", (other, message))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run("UPDATE ticket_files SET ticket_id=? WHERE id=?", (other, file_id))
    await db.run("UPDATE ticket_messages SET text='Правка' WHERE id=?", (message,))
    assert await revision() == 5
    await db.run("UPDATE ticket_events SET detail='Правка' WHERE id=?", (event,))
    assert await revision() == 6
    await db.run("UPDATE ticket_files SET display_name='Имя.pdf' WHERE id=?", (file_id,))
    assert await revision() == 7
    await db.run("DELETE FROM ticket_files WHERE id=?", (file_id,))
    await db.run("DELETE FROM ticket_events WHERE id=?", (event,))
    await db.run("DELETE FROM ticket_messages WHERE id=?", (message,))
    assert await revision() == 10
    await db.run("DELETE FROM tickets WHERE ticket_id=?", (ticket,))


async def test_panel_duplicate_fields_are_rejected(access_client):
    token = csrf(access_client.get("/panel/staff-access/7555666777"))
    encoded = urlencode({"csrf": token, "enabled": "1", "preset": "work"}) + "&enabled=0"
    result = access_client.post("/panel/staff-access/7555666777", content=encoded,
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert result.status_code == 400
    assert await access.get_staff_web_access("7555666777") is None


def test_staff_access_module_uses_only_declared_permissions():
    assert set(staff_access.PERMISSION_LABELS) == set(access.WEB_PERMISSIONS)


async def test_staff_access_has_clear_status_and_no_empty_pager(access_client):
    response = access_client.get("/panel/staff-access")
    assert response.status_code == 200
    assert response.text.count('<nav ') == 1
    assert 'aria-label="Страницы сотрудников"' not in response.text
    assert 'class="staff-web-list"' in response.text
    assert 'class="pill pill-off">Выключен</span>' in response.text
    assert "Код не отправляется, пока доступ выключен" in response.text
    assert "Существующие сотрудники не получают веб-доступ автоматически" in response.text
    assert "Если сотруднику не приходит код" in response.text
    assert 'href="/workspace/login"' in response.text
    assert await access.get_staff_web_access("7555666777") is None


@pytest.mark.parametrize("number,count,label", [(1, 51, "Далее"), (2, 1, "Назад")])
async def test_staff_access_pager_only_when_needed(access_client, monkeypatch, number, count, label):
    sample = (await access.list_staff_web_access())[0]
    observed = []

    async def rows(search, limit, offset):
        observed.append((search, limit, offset))
        return [sample] * count

    monkeypatch.setattr(staff_access, "list_staff_web_access", rows)
    response = access_client.get(f"/panel/staff-access?q=проверка&page={number}")
    pager = re.search(r'<nav class="pager"[^>]*>(.*?)</nav>', response.text, re.S)
    assert pager and label in pager[1] and f"Страница {number}" in pager[1]
    assert "q=%D0%BF" in pager[1] and "&amp;page=" in pager[1]
    assert observed == [("проверка", 51, (number - 1) * 50)]


async def test_staff_access_empty_search_and_form_are_explicit(access_client):
    empty = access_client.get("/panel/staff-access?q=ничего-не-найдено")
    assert "Подходящих сотрудников нет." in empty.text and "Сбросить поиск" in empty.text
    assert 'aria-label="Страницы сотрудников"' not in empty.text
    edit = access_client.get("/panel/staff-access/7555666777")
    assert edit.text.count('class="staff-web-check"') == len(access.WEB_PERMISSIONS)
    assert "Сохранить доступ" in edit.text and "К списку сотрудников" in edit.text
    assert "прежние входы и коды будут отозваны" in edit.text
