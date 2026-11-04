"""Безопасный полный аудит: схема, приватность, фильтры и срок хранения."""
from pathlib import Path

import pytest

import clock
import config
import database as db
import repository as repo
from conftest import click, login_panel, msg

SECRET_MESSAGE = "СЕКРЕТ-ПЕРЕПИСКИ-9f07"
SECRET_CODE = "INV777"
OWNER = "46010397"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


def joined(rows) -> str:
    return " ".join(" ".join(str(value or "") for value in tuple(row)) for row in rows)


async def test_audit_schema_and_facade_are_complete(env):
    columns = {row["name"] for row in await db.many("PRAGMA table_info(audit_log)")}
    assert {"id", "created_at", "user_id", "role", "action",
            "target_type", "target_id", "result"} <= columns
    indexes = {row["name"] for row in await db.many("PRAGMA index_list(audit_log)")}
    assert {"idx_audit_log_created", "idx_audit_log_user", "idx_audit_log_action"} <= indexes
    assert repo.log_audit is not None and repo.audit_entries is not None


async def test_audit_write_normalizes_codes_and_hides_invites(env):
    await repo.log_audit(" 100 ", "student", "message_received", "ticket", "42", "ok")
    await repo.log_audit("1", "owner", "invite_open", "invite", SECRET_CODE, "ok")
    rows = await repo.audit_entries(10)
    assert rows[1]["action"] == "message_received"
    assert rows[1]["target_type"] == "ticket" and rows[1]["target_id"] == "42"
    assert rows[0]["target_type"] == "invite" and rows[0]["target_id"] == ""
    assert SECRET_CODE not in joined(rows)


async def test_bot_audit_never_writes_message_payload_or_callback_argument(env):
    await db.set_setting("tickets_enabled", "0")
    await __import__("bot").process(msg("901", SECRET_MESSAGE))
    await __import__("bot").process(click("901", f"home:{SECRET_CODE}"))
    rows = await db.many("SELECT * FROM audit_log ORDER BY id")
    body = joined(rows)
    assert SECRET_MESSAGE not in body
    assert SECRET_CODE not in body
    assert "message_received" in body and "callback" in body


async def test_failed_audit_is_visible_in_technical_log(env, monkeypatch, caplog):
    import bot

    async def broken(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(repo, "log_audit", broken)
    with caplog.at_level("WARNING"):
        await bot.audit_event("902", "message_received")
    assert "аудит события message_received" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "audit unavailable" not in caplog.text


async def test_filters_count_csv_and_page(panel_client, env):
    await repo.log_audit("100", "student", "message_received", "ticket", "8", "ok")
    await repo.log_audit("1", "sysadmin", "ticket_edit", "ticket", "8", "failed")
    assert login_panel(panel_client)
    page = panel_client.get("/panel/audit", params={"user_id": "100"})
    assert page.status_code == 200
    table = page.text.split("<table>", 1)[1].split("</table>", 1)[0]
    assert "message_received" in table and "ticket_edit" not in table
    csv = panel_client.get("/panel/audit.csv", params={"result": "failed"})
    assert csv.status_code == 200 and "ticket_edit" in csv.text and "message_received" not in csv.text
    assert csv.headers["cache-control"] == "no-store"
    assert await repo.audit_count(role="student") == 1


async def test_audit_prunes_only_older_than_thirty_days(env):
    old = clock.stamp_at(-31 * 24 * 60)
    keep = clock.stamp_at(-29 * 24 * 60)
    await db.run("INSERT INTO audit_log(user_id, action, created_at) VALUES('1','old',?)", (old,))
    await db.run("INSERT INTO audit_log(user_id, action, created_at) VALUES('1','keep',?)", (keep,))
    assert await repo.prune_audit(30) == 1
    assert [row["action"] for row in await repo.audit_entries(10)] == ["keep"]


async def test_admin_log_does_not_receive_invite_codes_or_raw_field_values(panel_client, env):
    monkeypatch_value = SECRET_CODE
    await repo.log_action("1", "контроль", "только имя поля")
    assert login_panel(panel_client)
    panel_client.post("/panel/access/code", data={
        "csrf": __import__("webpanel")._csrf[panel_client.cookies.get(__import__("webpanel").COOKIE)],
        "user_id": "", "ttl_hours": "24",
    }, follow_redirects=False)
    rows = await repo.admin_log(50)
    body = joined(rows)
    assert monkeypatch_value not in body
    assert SECRET_MESSAGE not in body


async def test_raw_audit_tables_are_read_only(owner_client, env):
    for table in ("admin_log", "audit_log"):
        assert owner_client.get(f"/panel/data/table/{table}").status_code == 200
        assert owner_client.get(f"/panel/data/table/{table}/row/new").status_code == 404
        assert owner_client.post(f"/panel/data/table/{table}/row", data={}).status_code in (403, 404)


def test_log_file_does_not_contain_privacy_canaries(env):
    text = Path(config.LOG_FILE).read_text(encoding="utf-8") if Path(config.LOG_FILE).exists() else ""
    assert SECRET_MESSAGE not in text and SECRET_CODE not in text
