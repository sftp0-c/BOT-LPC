"""Рабочее место: SQL-изоляция, живая авторизация, атомарность и повторные команды."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import logging
import sqlite3
import time

import pytest

import clock
import config
import database as db
from store import workspace as ws
from store.tickets import add_ticket_message, archive_ticket, create_ticket


async def allow(actor: str = "200", permissions=(), *, enabled: int = 1, version: int = 1) -> str:
    """Синтетическая сессия для теста policy, без пути доставки MAX-кода."""
    token = hashlib.sha256(f"сессия-{actor}-{version}".encode()).hexdigest()
    await db.run("INSERT OR REPLACE INTO staff_web_access(user_id,enabled,permissions,auth_version) VALUES(?,?,?,?)",
                 (actor, enabled, json.dumps(list(permissions)), version))
    await db.run("INSERT OR REPLACE INTO staff_web_sessions(token_hash,user_id,auth_version,expires_at) VALUES(?,?,?,?)",
                 (token, actor, version, int(time.time()) + 3600))
    return token


async def revision(ticket_id: int) -> int:
    return (await db.one("SELECT workspace_revision FROM tickets WHERE ticket_id=?", (ticket_id,)))["workspace_revision"]


@pytest.fixture
async def world(env):
    async with db._conn() as conn:
        await conn.executemany("INSERT INTO admins(user_id,full_name,position,ticket_category) VALUES(?,?,?,?)", [
            ("200", "Сотрудник Первый", "Секретарь", "all"),
            ("201", "Сотрудник Второй", "Преподаватель", "all"),
            ("202", "Сотрудник Третий", "Бухгалтер", "accounting"),
            ("0200", "Похожий номер", "Секретарь", "all"),
        ])
        await conn.executemany("INSERT INTO users(user_id,full_name,group_code) VALUES(?,?,?)", [
            ("300", "Иванова Мария", "24-23"), ("301", "Другой Студент", "24-24")])
        await conn.commit()
    own = await create_ticket("300", "200", "certificates", "Нужна справка")
    foreign = await create_ticket("301", "201", "academic", "Чужая переписка и чужая тема")
    similar = await create_ticket("301", "0200", "feedback", "Похожий, но другой номер")
    return {"own": own, "foreign": foreign, "similar": similar}


async def apply(ticket_id: int, token: str, action: str = "reply", *, actor: str = "200", **kwargs):
    return await ws.workspace_apply(actor, ticket_id, action, session_hash=token,
        expected_revision=kwargs.pop("expected_revision", await revision(ticket_id)),
        operation_key=kwargs.pop("operation_key", "тестовая-операция"), **kwargs)


async def test_own_acl_before_pagination_count_and_export(world):
    token = await allow(permissions=["export"])
    for index in range(7):
        await create_ticket("301", "201", "certificates", f"Чужое {index}")
    result = await ws.workspace_list("200", session_hash=token, filters={"mine": False}, page_size=1)
    assert result["result"] == "ok"
    assert [row["ticket_id"] for row in result["rows"]] == [world["own"]]
    assert result["total"] == result["counts"]["total"] == result["counts"]["waiting"] == 1
    empty_page = await ws.workspace_list("200", session_hash=token, page=2, page_size=1)
    assert empty_page["rows"] == [] and empty_page["total"] == 1
    csv = await ws.workspace_list("200", session_hash=token, for_export=True, page_size=2000)
    assert csv["rows"] == result["rows"]
    assert "Чужая" not in repr(csv)
    assert (await ws.workspace_ticket("200", world["foreign"], session_hash=token)) == (
        await ws.workspace_ticket("200", 999999, session_hash=token))
    assert (await apply(world["foreign"], token, text="Попытка"))["result"] == "not_found"


async def test_view_others_is_read_only_and_mine_only_narrows(world):
    token = await allow(permissions=["view_others", "reply", "status", "note", "forward"])
    all_rows = await ws.workspace_list("200", session_hash=token)
    assert all_rows["total"] == 3
    mine = await ws.workspace_list("200", session_hash=token, filters={"mine": "1"})
    assert mine["total"] == 1 and mine["rows"][0]["is_own"]
    detail = await ws.workspace_ticket("200", world["foreign"], session_hash=token)
    assert detail["result"] == "ok" and not detail["is_own"]
    assert not set(detail["allowed_actions"]) & {"reply", "status", "note", "forward", "reply_close"}
    for action, values in [("reply", {"text": "Ответ"}), ("reply_close", {"text": "Закрыть"}),
                           ("status", {"status": "accepted"}), ("note", {"text": "Заметка"}),
                           ("forward", {"target_id": "200"})]:
        assert (await apply(world["foreign"], token, action, **values))["result"] == "denied"


async def test_manage_others_implies_view_but_needs_operation_permission(world):
    token = await allow(permissions=["manage_others"])
    assert (await ws.workspace_list("200", session_hash=token))["total"] == 3
    assert (await apply(world["foreign"], token, text="Ответ"))["result"] == "denied"
    token = await allow(permissions=["manage_others", "reply"])
    result = await apply(world["foreign"], token, text="Разрешённый ответ")
    assert result["result"] == "ok"
    assert result["notifications"][0]["recipient_id"] == "301"


@pytest.mark.parametrize("action,permissions,payload,expected", [
    ("reply", [], {"text": "Ответ"}, "denied"),
    ("reply", ["reply"], {"text": "Ответ"}, "ok"),
    ("reply_close", ["reply"], {"text": "Ответ"}, "denied"),
    ("reply_close", ["status"], {"text": "Ответ"}, "denied"),
    ("reply_close", ["reply", "status"], {"text": "Ответ"}, "ok"),
    ("status", ["status"], {"status": "in_progress"}, "ok"),
    ("status", ["reply"], {"status": "in_progress"}, "denied"),
    ("note", ["note"], {"text": "Внутренняя заметка"}, "ok"),
    ("note", ["reply"], {"text": "Внутренняя заметка"}, "denied"),
    ("forward", ["forward"], {"target_id": "201"}, "ok"),
    ("forward", ["view_others"], {"target_id": "201"}, "denied"),
])
async def test_every_action_permission_is_enforced(world, action, permissions, payload, expected):
    token = await allow(permissions=permissions)
    result = await apply(world["own"], token, action, **payload)
    assert result["result"] == expected
    if action == "note" and expected == "ok":
        assert result["notifications"] == [] and result["message_id"] == 0


@pytest.mark.parametrize("mutation", ["disabled", "revoked", "expired", "version", "deleted", "test", "role", "permissions"])
async def test_live_access_revocation_blocks_all_operations(world, mutation):
    token = await allow(permissions=ws.WEB_PERMISSIONS)
    assert (await ws.workspace_ticket("200", world["own"], session_hash=token))["result"] == "ok"
    if mutation == "disabled":
        await db.run("UPDATE staff_web_access SET enabled=0 WHERE user_id='200'")
    elif mutation == "revoked":
        await db.run("UPDATE staff_web_sessions SET revoked_at=1 WHERE user_id='200'")
    elif mutation == "expired":
        await db.run("UPDATE staff_web_sessions SET expires_at=? WHERE user_id='200'", (int(time.time()),))
    elif mutation == "version":
        await db.run("UPDATE staff_web_access SET auth_version=auth_version+1 WHERE user_id='200'")
    elif mutation == "deleted":
        await db.run("DELETE FROM admins WHERE user_id='200'")
    elif mutation == "test":
        await db.run("UPDATE admins SET is_test=1 WHERE user_id='200'")
    elif mutation == "role":
        await db.run("UPDATE admins SET role_type='sysadmin' WHERE user_id='200'")
    else:
        await db.run("UPDATE staff_web_access SET permissions='not-json' WHERE user_id='200'")
    for response in [await ws.workspace_list("200", session_hash=token),
                     await ws.workspace_list("200", session_hash=token, for_export=True),
                     await ws.workspace_ticket("200", world["own"], session_hash=token),
                     await apply(world["own"], token, text="Не отправлять")]:
        assert response["result"] == "denied"
        assert "ticket" not in response and "rows" not in response


async def test_session_belongs_to_exact_actor_and_legacy_see_all_is_not_web_permission(world):
    token = await allow(permissions=["reply"])
    await db.run("UPDATE admins SET see_all_tickets=1 WHERE user_id='200'")
    assert (await ws.workspace_list("201", session_hash=token))["result"] == "denied"
    assert (await ws.workspace_list("0200", session_hash=token))["result"] == "denied"
    assert (await ws.workspace_list("200"))["result"] == "denied"
    assert (await ws.workspace_list("200", session_hash=token))["total"] == 1


async def test_admin_flag_is_not_authority_and_root_is_live(world, monkeypatch):
    token = await allow(permissions=["reply"])
    assert (await ws.workspace_list("200", admin=True, session_hash=token))["result"] == "denied"
    assert (await ws.workspace_list("1", admin=True))["total"] == 3
    await db.run("UPDATE admins SET role_type='staff' WHERE user_id='1'")
    assert (await ws.workspace_list("1", admin=True))["result"] == "denied"
    monkeypatch.setattr(config, "ROOT_IDS", ["900"])
    assert (await ws.workspace_list("900", admin=True))["total"] == 3
    monkeypatch.setattr(config, "ROOT_IDS", [])
    assert (await ws.workspace_list("900", admin=True))["result"] == "denied"


async def test_staff_cannot_impersonate_answerer(world):
    token = await allow(permissions=["reply", "manage_others"])
    for key in ("answerer_id", "as_staff"):
        assert (await apply(world["own"], token, text="Ответ", **{key: "201"}))["result"] == "denied"
    result = await ws.workspace_apply("1", world["own"], "reply", admin=True,
        expected_revision=await revision(world["own"]), operation_key="за-сотрудника", text="Ответ", answerer_id="201")
    assert result["result"] == "ok"
    message = await db.one("SELECT sender_id FROM ticket_messages WHERE id=?", (result["message_id"],))
    assert message["sender_id"] == "201"


async def test_export_permission_rechecked_in_service(world):
    token = await allow()
    assert (await ws.workspace_list("200", session_hash=token, for_export=True))["result"] == "denied"
    token = await allow(permissions=["export"])
    assert (await ws.workspace_list("200", session_hash=token, for_export=True, page_size=5000))["page_size"] == 2000
    assert (await ws.workspace_list("200", session_hash=token, page_size=5000))["page_size"] == 100
    await db.run("UPDATE staff_web_access SET permissions='[]' WHERE user_id='200'")
    assert (await ws.workspace_list("200", session_hash=token, for_export=True))["result"] == "denied"
    assert (await ws.workspace_list("1", admin=True, for_export=True))["result"] == "ok"


async def test_archive_closed_ready_waiting_and_same_filters(world):
    token = await allow(permissions=["export"])
    ready = await create_ticket("300", "200", "certificates", "Готовая справка")
    closed = await create_ticket("300", "200", "certificates", "Закрытая справка")
    archived = await create_ticket("300", "200", "feedback", "В архиве")
    await db.run("UPDATE tickets SET status='ready' WHERE ticket_id=?", (ready,))
    await db.run("UPDATE tickets SET status='completed' WHERE ticket_id=?", (closed,))
    await archive_ticket(archived, "1")
    result = await ws.workspace_list("200", session_hash=token)
    assert {row["ticket_id"] for row in result["rows"]} == {world["own"], ready}
    assert {name: result["counts"][name] for name in ("open", "closed", "waiting", "archive")} == {
        "open": 2, "closed": 1, "waiting": 1, "archive": 1}
    for view, expected in [("waiting", {world["own"]}), ("closed", {closed}), ("archive", {archived})]:
        selected = await ws.workspace_list("200", session_hash=token, filters={"view": view})
        assert {row["ticket_id"] for row in selected["rows"]} == expected
        csv = await ws.workspace_list("200", session_hash=token, filters={"view": view}, for_export=True)
        assert csv["rows"] == selected["rows"]
    admin = await ws.workspace_list("1", admin=True, filters={"view": ""})
    assert admin["total"] == 5
    assert (await ws.workspace_ticket("200", archived, session_hash=token))["archived"]
    assert (await ws.workspace_apply("1", archived, "reply", admin=True,
        expected_revision=await revision(archived), operation_key="архив", text="Ответ"))["result"] == "conflict"


async def test_query_dates_literal_search_and_rejected_widening(world):
    token = await allow()
    await db.run("UPDATE tickets SET created_at=? WHERE ticket_id=?",
                 ((clock.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"), world["own"]))
    for filters in ({"period": "7"}, {"category": "academic"}, {"q": "Чужая"}, {"q": "%"},
                    {"q": "' OR 1=1 --"}, {"date_from": clock.now().date().isoformat()}):
        result = await ws.workspace_list("200", session_hash=token, filters=filters)
        assert result["total"] == 0 and result["counts"]["total"] == 0
    assert (await ws.workspace_list("200", session_hash=token, filters={"q": "ИВАНОВА"}))["total"] == 1
    for filters in ({"target_admin_id": "201"}, {"admin": True}, {"permissions": "view_others"},
                    {"status": "whatever"}, {"date_from": "2026-99-99"}, {"period": "-1"}):
        assert (await ws.workspace_list("200", session_hash=token, filters=filters))["result"] == "invalid"


async def test_transfer_removes_previous_assignment_access_and_returns_new_recipient(world):
    old = await allow(permissions=["forward", "reply"])
    new = await allow("201", ["reply"])
    before = await revision(world["own"])
    result = await apply(world["own"], old, "forward", target_id="201", text="Внутренний комментарий")
    assert result["result"] == "ok" and result["notifications"][0]["recipient_id"] == "201"
    assert (await ws.workspace_ticket("200", world["own"], session_hash=old))["result"] == "not_found"
    assert (await apply(world["own"], old, text="Старый экран", expected_revision=before))["result"] == "not_found"
    assert (await ws.workspace_ticket("201", world["own"], session_hash=new))["result"] == "ok"
    assert (await apply(world["own"], new, actor="201", text="Новый ответственный"))["result"] == "ok"


@pytest.mark.parametrize("target", ["999", "1", "202", "200"])
async def test_forward_requires_actual_staff_category_and_different_target(world, target):
    token = await allow(permissions=["forward"])
    assert (await apply(world["own"], token, "forward", target_id=target))["result"] == "invalid"


async def test_test_staff_cannot_be_forward_target(world):
    token = await allow(permissions=["forward"])
    await db.run("UPDATE admins SET is_test=1 WHERE user_id='201'")
    assert (await apply(world["own"], token, "forward", target_id="201"))["result"] == "invalid"


async def test_templates_checked_by_permission_and_category_used_once(world):
    token = await allow(permissions=["reply", "templates"])
    await db.run("DELETE FROM reply_templates")
    template = await db.run("INSERT INTO reply_templates(title,text,category) VALUES('Справка','Здравствуйте, {ФИО}','certificates')")
    wrong = await db.run("INSERT INTO reply_templates(title,text,category) VALUES('Учёба','Не тот раздел','academic')")
    preview = await ws.workspace_template_preview("200", world["own"], template, session_hash=token)
    assert preview["result"] == "ok" and "{ФИО}" in preview["template"]["text"]
    assert (await ws.workspace_template_preview("200", world["foreign"], wrong, session_hash=token))["result"] == "not_found"
    assert (await apply(world["own"], token, text="Ответ", template_id=wrong))["result"] == "invalid"
    before = await revision(world["own"])
    first = await apply(world["own"], token, text="Здравствуйте, Иванова Мария", template_id=template, expected_revision=before)
    second = await apply(world["own"], token, text="Здравствуйте, Иванова Мария", template_id=template, expected_revision=before)
    assert first["result"] == "ok" and second["result"] == "replay"
    assert (await db.one("SELECT used_count FROM reply_templates WHERE id=?", (template,)))["used_count"] == 1
    token = await allow(permissions=["reply"])
    assert (await ws.workspace_template_preview("200", world["own"], template, session_hash=token))["result"] == "denied"
    assert (await apply(world["own"], token, text="Ответ", template_id=template, operation_key="без-права"))["result"] == "denied"


async def test_reply_close_replay_mismatch_and_private_journal(world, caplog):
    token = await allow(permissions=["reply", "status"])
    before = await revision(world["own"])
    secret = "СЕКРЕТ_ПЕРЕПИСКИ_932810"
    caplog.set_level(logging.DEBUG, logger="aiosqlite")
    first = await apply(world["own"], token, "reply_close", text=secret, expected_revision=before)
    second = await apply(world["own"], token, "reply_close", text=secret, expected_revision=before)
    assert first["result"] == "ok" and second["result"] == "replay" and second["notifications"] == []
    assert first["message_id"] == second["message_id"]
    assert first["status"] == "completed" and first["revision"] > before
    mismatch = await apply(world["own"], token, "reply_close", text="Подмена текста", expected_revision=before)
    assert mismatch["result"] == "conflict"
    assert secret not in caplog.text and token not in caplog.text
    assert secret not in repr(await db.many("SELECT * FROM workspace_operations"))
    assert secret not in repr(await db.many("SELECT detail FROM ticket_events WHERE ticket_id=?", (world["own"],)))
    assert secret not in repr(await db.many("SELECT * FROM admin_log"))
    assert (await db.one("SELECT text FROM ticket_messages WHERE id=?", (first["message_id"],)))["text"] == secret


async def test_concurrent_same_operation_once_different_operation_conflicts(world):
    token = await allow(permissions=["reply", "status"])
    before = await revision(world["own"])
    same = await asyncio.gather(*[apply(world["own"], token, "reply_close", text="Один ответ", expected_revision=before)
                                  for _ in range(2)])
    assert sorted(item["result"] for item in same) == ["ok", "replay"]
    assert sum(len(item["notifications"]) for item in same) == 1
    before = await revision(world["foreign"])
    token = await allow(permissions=["reply", "manage_others"])
    different = await asyncio.gather(*[apply(world["foreign"], token, text="Ответ", expected_revision=before,
                                            operation_key=f"операция-{index}") for index in range(2)])
    assert sorted(item["result"] for item in different) == ["conflict", "ok"]


async def test_revocation_committed_while_write_waits_blocks_operation(world):
    token = await allow(permissions=["reply"])
    before = await revision(world["own"])
    async with db._conn() as lock:
        await lock.execute("BEGIN IMMEDIATE")
        await lock.execute("UPDATE staff_web_sessions SET revoked_at=1 WHERE user_id='200'")
        task = asyncio.create_task(apply(world["own"], token, text="Не писать", expected_revision=before))
        await asyncio.sleep(0.03)
        await lock.commit()
    result = await task
    assert result["result"] == "denied"
    assert await revision(world["own"]) == before


async def test_every_read_authorizes_inside_snapshot(world, monkeypatch):
    token = await allow(permissions=["export", "templates"])
    original = db._conn
    statements = []

    @asynccontextmanager
    async def tracked():
        async with original() as conn:
            await conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "_conn", tracked)
    for operation in (
        ws.workspace_list("200", session_hash=token),
        ws.workspace_list("200", session_hash=token, for_export=True),
        ws.workspace_ticket("200", world["own"], session_hash=token),
        ws.workspace_template_preview("200", world["own"], 9999, session_hash=token),
    ):
        statements.clear()
        await operation
        auth = next(i for i, sql in enumerate(statements) if "FROM staff_web_sessions" in sql)
        begin = next(i for i, sql in enumerate(statements) if sql.startswith("BEGIN"))
        assert begin < auth
        assert any(sql == "COMMIT" for sql in statements[auth:])


async def test_trigger_revision_detects_bot_and_other_channel_edits(world):
    token = await allow(permissions=["reply"])
    before = await revision(world["own"])
    await add_ticket_message(world["own"], "300", "student", "Новое сообщение студента")
    assert await revision(world["own"]) > before
    assert (await apply(world["own"], token, text="Устаревший ответ", expected_revision=before))["result"] == "conflict"


async def test_failure_rolls_back_message_status_event_usage_and_operation(world):
    token = await allow(permissions=["reply", "status", "templates"])
    template = await db.run("INSERT INTO reply_templates(title,text) VALUES('Шаблон','Ответ')")
    before = await revision(world["own"])
    messages_before = (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"]
    events_before = (await db.one("SELECT COUNT(*) n FROM ticket_events"))["n"]
    await db.run("CREATE TRIGGER test_abort_operation BEFORE INSERT ON workspace_operations "
                 "BEGIN SELECT RAISE(ABORT,'тестовый отказ'); END")
    with pytest.raises(sqlite3.IntegrityError, match="тестовый отказ"):
        await apply(world["own"], token, "reply_close", text="Ответ", template_id=template)
    assert await revision(world["own"]) == before
    assert (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"] == messages_before
    assert (await db.one("SELECT COUNT(*) n FROM ticket_events"))["n"] == events_before
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (world["own"],)))["status"] == "new"
    assert (await db.one("SELECT used_count FROM reply_templates WHERE id=?", (template,)))["used_count"] == 0
    assert not await db.many("SELECT * FROM workspace_operations")


@pytest.mark.parametrize("action,payload", [
    ("reply", {"text": " "}), ("reply", {"text": "я" * 4001}),
    ("note", {"text": "я" * 201}), ("note", {"text": "а\0б"}),
    ("status", {"status": "closed"}), ("status", {"status": "accepted", "text": "лишнее"}),
    ("forward", {"target_id": "201", "text": "я" * 201}),
    ("delete", {}), ("reply", {"text": "Ответ", "expected_revision": "2026-11-05"}),
    ("reply", {"text": "Ответ", "operation_key": ""}),
])
async def test_invalid_commands_do_not_fake_success(world, action, payload):
    token = await allow(permissions=ws.WEB_PERMISSIONS)
    before = await revision(world["own"])
    result = await apply(world["own"], token, action, **payload)
    assert result["result"] == "invalid"
    assert await revision(world["own"]) == before


async def test_transfer_committed_while_write_waits_prevents_old_owner_write(world):
    token = await allow(permissions=["reply"])
    before = await revision(world["own"])
    async with db._conn() as lock:
        await lock.execute("BEGIN IMMEDIATE")
        await lock.execute("UPDATE tickets SET target_admin_id='201' WHERE ticket_id=?", (world["own"],))
        task = asyncio.create_task(apply(world["own"], token, text="Не писать", expected_revision=before))
        await asyncio.sleep(0.03)
        await lock.commit()
    assert (await task)["result"] == "not_found"
    assert not await db.many("SELECT * FROM workspace_operations")


async def test_read_snapshot_keeps_count_and_rows_consistent_across_transfer(world, monkeypatch):
    token = await allow()
    async with db._conn() as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
    original = ws._many
    transferred = False

    async def between_count_and_rows(conn, sql, params=()):
        nonlocal transferred
        if "WITH matched AS" in sql and not transferred:
            transferred = True
            await db.run("UPDATE tickets SET target_admin_id='201' WHERE ticket_id=?", (world["own"],))
        return await original(conn, sql, params)

    monkeypatch.setattr(ws, "_many", between_count_and_rows)
    result = await ws.workspace_list("200", session_hash=token)
    assert transferred and result["total"] == 1
    assert [row["ticket_id"] for row in result["rows"]] == [world["own"]]
    following = await ws.workspace_list("200", session_hash=token)
    assert following["total"] == 0 and following["rows"] == []


@pytest.mark.parametrize("status", [[], {}, 1, None])
async def test_raw_status_wrong_type_is_invalid_not_exception(world, status):
    token = await allow(permissions=["status"])
    assert (await apply(world["own"], token, "status", status=status))["result"] == "invalid"


async def test_idempotency_is_scoped_to_actor_and_admin_audience(world):
    token = await allow(permissions=["reply"])
    other = await allow("201", ["reply", "manage_others"])
    result = await apply(world["own"], token, text="Первый")
    assert result["result"] == "ok"
    different_actor = await apply(world["own"], other, actor="201", text="Второй")
    assert different_actor["result"] == "ok"
    await db.run("UPDATE admins SET role_type='sysadmin' WHERE user_id='200'")
    admin = await ws.workspace_apply("200", world["own"], "reply", admin=True,
        expected_revision=await revision(world["own"]), operation_key="тестовая-операция", text="Сис-админ")
    assert admin["result"] == "ok"
    assert len(await db.many("SELECT * FROM workspace_operations")) == 3


async def test_replay_rechecks_revoked_permissions_and_assignment(world):
    token = await allow(permissions=["reply"])
    before = await revision(world["own"])
    assert (await apply(world["own"], token, text="Один", expected_revision=before))["result"] == "ok"
    await db.run("UPDATE staff_web_access SET permissions='[]' WHERE user_id='200'")
    assert (await apply(world["own"], token, text="Один", expected_revision=before))["result"] == "denied"
    await db.run("UPDATE staff_web_access SET permissions='[\"reply\"]' WHERE user_id='200'")
    await db.run("UPDATE tickets SET target_admin_id='201' WHERE ticket_id=?", (world["own"],))
    assert (await apply(world["own"], token, text="Один", expected_revision=before))["result"] == "not_found"


async def test_legacy_closed_status_visible_as_closed_but_cannot_be_written(world):
    token = await allow(permissions=["reply", "status"])
    await db.run("UPDATE tickets SET status='closed' WHERE ticket_id=?", (world["own"],))
    opened = await ws.workspace_list("200", session_hash=token)
    assert opened["total"] == 0 and opened["counts"]["closed"] == 1
    closed = await ws.workspace_list("200", session_hash=token, filters={"view": "closed"})
    assert [row["ticket_id"] for row in closed["rows"]] == [world["own"]]
    assert "reply_close" not in closed["rows"][0]["allowed_actions"]
    selected = await ws.workspace_list("1", admin=True, filters={"view": "", "status": "closed"})
    assert selected["total"] == 1
    assert (await apply(world["own"], token, "status", status="closed"))["result"] == "invalid"
    assert (await apply(world["own"], token, "reply_close", text="Ответ"))["result"] == "conflict"
