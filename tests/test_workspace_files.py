"""Вложения: доверенная связь вместо маркера, отзыв прав, запись без перетирания."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

import pytest

import attachments
import database as db
from handlers import tickets as handler
from store import workspace as ws
from store.tickets import add_ticket_message, create_ticket


class FileStream:
    def __init__(self, content=b"%PDF-1.7 test", *, fail=False):
        self.content = content
        self.status_code = 200
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        yield self.content[:3]
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("тестовый обрыв сети")
        yield self.content[3:]


class FileClient:
    def __init__(self, content=b"%PDF-1.7 test", *, fail=False):
        self.content, self.fail = content, fail

    def stream(self, *args, **kwargs):
        return FileStream(self.content, fail=self.fail)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def session(actor="200", permissions=("attachments",)):
    token = hashlib.sha256(f"файлы-сессия-{actor}".encode()).hexdigest()
    await db.run("INSERT OR REPLACE INTO staff_web_access(user_id,enabled,permissions,auth_version) VALUES(?,1,?,1)",
                 (actor, json.dumps(list(permissions))))
    await db.run("INSERT OR REPLACE INTO staff_web_sessions(token_hash,user_id,auth_version,expires_at) VALUES(?,?,1,?)",
                 (token, actor, int(time.time()) + 3600))
    return token


@pytest.fixture
async def files_world(env, monkeypatch):
    await db.run("INSERT INTO admins(user_id,full_name,position) VALUES('200','Первый','Секретарь')")
    await db.run("INSERT INTO admins(user_id,full_name,position) VALUES('201','Второй','Преподаватель')")
    await db.run("INSERT INTO users(user_id,full_name,group_code) VALUES('300','Студент','24-23')")
    await db.run("INSERT INTO users(user_id,full_name,group_code) VALUES('301','Другой','24-24')")
    own = await create_ticket("300", "200", "certificates", "Нужна справка")
    foreign = await create_ticket("301", "201", "feedback", "Другой вопрос")
    monkeypatch.setattr(attachments.httpx, "AsyncClient", lambda **kwargs: FileClient())
    folder = Path(attachments.attachments_dir())
    folder.mkdir(exist_ok=True)
    return {"own": own, "foreign": foreign, "folder": folder, "token": await session()}


async def upload(ticket_id: int, actor="300", name="справка.pdf"):
    await db.set_state(actor, "attach_file", {"tid": ticket_id})
    return await handler.on_attachment(actor, [{"url": "https://max.example/file.pdf", "name": name, "size": 9000000}])


async def test_marker_is_not_trusted_file_or_link(files_world):
    w = files_world
    (w["folder"] / "чужой.pdf").write_bytes(b"%PDF-secret")
    await add_ticket_message(w["own"], "300", "student", "📎 Файл: чужой.pdf (1 КБ)")
    detail = await ws.workspace_ticket("200", w["own"], session_hash=w["token"])
    assert detail["files"] == [] and detail["legacy_files_restricted"]
    assert await handler.attached_names(w["own"]) == []
    response = await ws.workspace_attachment("200", w["own"], 1, session_hash=w["token"])
    assert response["result"] == "not_found" and "path" not in response


async def test_upload_binds_file_to_actual_message_and_hides_storage_metadata(files_world):
    w = files_world
    assert await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files WHERE ticket_id=?", (w["own"],))
    assert row["display_name"] == "справка.pdf" and row["storage_name"] != row["display_name"]
    assert row["size"] == len(b"%PDF-1.7 test")
    assert (await db.one("SELECT ticket_id FROM ticket_messages WHERE id=?", (row["message_id"],)))["ticket_id"] == w["own"]
    detail = await ws.workspace_ticket("200", w["own"], session_hash=w["token"])
    assert detail["files"][0]["downloadable"] and "storage_name" not in detail["files"][0]
    assert not detail["legacy_files_restricted"]
    allowed = await ws.workspace_attachment("200", w["own"], row["id"], session_hash=w["token"])
    assert allowed["result"] == "ok" and Path(allowed["path"]).read_bytes() == b"%PDF-1.7 test"
    for actor, ticket_id, token in [("200", w["foreign"], w["token"]), ("201", w["own"], await session("201"))]:
        denied = await ws.workspace_attachment(actor, ticket_id, row["id"], session_hash=token)
        assert denied["result"] == "not_found" and "file" not in denied and "path" not in denied


async def test_files_require_attachment_cap_and_current_assignment_even_after_card(files_world):
    w = files_world
    await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files")
    token = await session(permissions=[])
    detail = await ws.workspace_ticket("200", w["own"], session_hash=token)
    assert not detail["files"][0]["downloadable"]
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=token))["result"] == "denied"
    token = await session()
    await db.run("UPDATE tickets SET target_admin_id='201' WHERE ticket_id=?", (w["own"],))
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=token))["result"] == "not_found"
    token = await session(permissions=["attachments", "view_others"])
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=token))["result"] == "ok"
    await db.run("UPDATE staff_web_sessions SET revoked_at=1 WHERE user_id='200'")
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=token))["result"] == "denied"


async def test_download_checks_session_inside_transaction(files_world, monkeypatch):
    w = files_world
    await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files")
    original = db._conn
    statements = []

    @asynccontextmanager
    async def tracked():
        async with original() as conn:
            await conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "_conn", tracked)
    result = await ws.workspace_attachment("200", w["own"], row["id"], session_hash=w["token"])
    assert result["result"] == "ok"
    auth = next(i for i, sql in enumerate(statements) if "FROM staff_web_sessions" in sql)
    assert next(i for i, sql in enumerate(statements) if sql == "BEGIN") < auth


async def test_forged_pending_state_cannot_download_or_change_foreign_ticket(files_world, monkeypatch, api):
    calls = []

    async def forbidden_download(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Загрузка не должна начинаться")

    monkeypatch.setattr(attachments, "download_attachment", forbidden_download)
    w = files_world
    for actor in ("301", "200", "1"):
        assert await upload(w["own"], actor)
        assert "не найдено" in api.last(actor)[1]
    assert calls == []
    assert not await db.many("SELECT * FROM ticket_files") and list(w["folder"].iterdir()) == []


@pytest.mark.parametrize("change", ["ownership", "archive", "delete"])
async def test_owner_rechecked_after_download_and_new_file_removed(files_world, monkeypatch, api, change):
    w = files_world
    real_download = attachments.download_attachment
    paths = []

    async def racing_download(*args, **kwargs):
        path, name = await real_download(*args, **kwargs)
        paths.append(Path(path))
        if change == "ownership":
            await db.run("UPDATE tickets SET student_id='301' WHERE ticket_id=?", (w["own"],))
        elif change == "archive":
            await db.run("UPDATE tickets SET deleted_at='2026-11-05' WHERE ticket_id=?", (w["own"],))
        else:
            await db.run("DELETE FROM tickets WHERE ticket_id=?", (w["own"],))
        return path, name

    monkeypatch.setattr(attachments, "download_attachment", racing_download)
    assert await upload(w["own"])
    assert paths and all(not path.exists() for path in paths)
    assert not await db.many("SELECT * FROM ticket_files")
    assert "не прикреплён" in api.last("300")[1]
    assert not api.to("200")


async def test_concurrent_downloads_same_content_use_unique_names_and_limit_atomic(files_world, api):
    w = files_world
    results = await asyncio.gather(*[attachments.download_attachment("https://max.example/a", "одинаковый.pdf", unique=True)
                                     for _ in range(5)])
    assert len({name for _path, name in results}) == 5
    attached = await asyncio.gather(*[ws.workspace_attach_file("300", w["own"], storage_name=name,
        display_name="одинаковый.pdf", size=os.path.getsize(path)) for path, name in results])
    assert [item["result"] for item in attached].count("ok") == 3
    assert [item["result"] for item in attached].count("conflict") == 2
    rows = await db.many("SELECT * FROM ticket_files")
    assert len(rows) == 3 and len({row["message_id"] for row in rows}) == 3
    assert not api.sent


async def test_same_content_in_two_tickets_is_not_cross_linked(files_world):
    w = files_world
    await upload(w["own"])
    await upload(w["foreign"], "301")
    rows = await db.many("SELECT * FROM ticket_files ORDER BY id")
    assert len(rows) == 2 and rows[0]["storage_name"] != rows[1]["storage_name"]
    assert (await ws.workspace_attachment("200", w["own"], rows[1]["id"], session_hash=w["token"]))["result"] == "not_found"
    assert (await ws.workspace_attachment("200", w["foreign"], rows[1]["id"], session_hash=w["token"]))["result"] == "not_found"


async def test_reused_storage_name_rejected_without_second_message(files_world):
    w = files_world
    await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files")
    before = (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"]
    result = await ws.workspace_attach_file("301", w["foreign"], storage_name=row["storage_name"],
        display_name=row["display_name"], size=row["size"])
    assert result["result"] == "conflict"
    assert (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"] == before


async def test_db_refuses_message_from_another_ticket(files_world):
    w = files_world
    message_id = (await db.one("SELECT id FROM ticket_messages WHERE ticket_id=?", (w["foreign"],)))["id"]
    with pytest.raises(sqlite3.IntegrityError, match="Несовместимая связь файла"):
        await db.run("INSERT INTO ticket_files(ticket_id,message_id,storage_name,display_name,size) VALUES(?,?,?,?,?)",
                     (w["own"], message_id, "ложная.pdf", "ложная.pdf", 10))
    assert not await db.many("SELECT * FROM ticket_files")


async def test_db_update_cannot_rebind_file_to_unrelated_message(files_world):
    w = files_world
    await upload(w["own"])
    foreign_message = (await db.one("SELECT id FROM ticket_messages WHERE ticket_id=?", (w["foreign"],)))["id"]
    with pytest.raises(sqlite3.IntegrityError, match="Несовместимая связь файла"):
        await db.run("UPDATE ticket_files SET message_id=?", (foreign_message,))


async def test_failed_association_rolls_back_message_and_cleans_only_new_file(files_world, api):
    w = files_world
    legacy = w["folder"] / "справка.pdf"
    legacy.write_bytes(b"%PDF-legacy")
    await db.run("CREATE TRIGGER test_fail_file BEFORE INSERT ON ticket_files "
                 "BEGIN SELECT RAISE(ABORT,'тестовая ошибка связи'); END")
    before = (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"]
    await upload(w["own"])
    assert list(w["folder"].iterdir()) == [legacy] and legacy.read_bytes() == b"%PDF-legacy"
    assert (await db.one("SELECT COUNT(*) n FROM ticket_messages"))["n"] == before
    assert not await db.many("SELECT * FROM ticket_files")
    assert "не сохранился" in api.last("300")[1] and not api.to("200")


async def test_association_failure_never_removes_existing_legacy_file(files_world, monkeypatch):
    w = files_world
    legacy = w["folder"] / "старый.pdf"
    legacy.write_bytes(b"%PDF-legacy")

    async def existing_download(*args, **kwargs):
        await db.run("UPDATE tickets SET student_id='301' WHERE ticket_id=?", (w["own"],))
        return str(legacy), legacy.name

    monkeypatch.setattr(attachments, "download_attachment", existing_download)
    await upload(w["own"])
    assert legacy.read_bytes() == b"%PDF-legacy"


async def test_downloader_never_overwrites_original_and_cleans_interrupted_stream(files_world, monkeypatch):
    w = files_world
    legacy = w["folder"] / "справка.pdf"
    legacy.write_bytes(b"%PDF-original")
    await attachments.download_attachment("https://max.example/a", "справка.pdf", unique=True)
    assert legacy.read_bytes() == b"%PDF-original"
    before = set(w["folder"].iterdir())
    monkeypatch.setattr(attachments.httpx, "AsyncClient", lambda **kwargs: FileClient(fail=True))
    with pytest.raises(OSError, match="тестовый обрыв"):
        await attachments.download_attachment("https://max.example/a", "справка.pdf", unique=True)
    assert set(w["folder"].iterdir()) == before


async def test_max_storage_names_still_fit_legacy_admin_sanitizer(files_world):
    path, name = await attachments.download_attachment("https://max.example/a", "я" * 55 + ".pdf", unique=True)
    assert len(name) <= 60 and attachments.safe_name(name) == name
    assert Path(path).is_file()


async def test_missing_file_path_tricks_and_admin_flag_are_denied(files_world):
    w = files_world
    await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files")
    assert (await ws.workspace_attachment("200", w["own"], row["id"], admin=True))["result"] == "denied"
    assert (await ws.workspace_attachment("1", w["own"], row["id"], admin=True))["result"] == "ok"
    (w["folder"] / row["storage_name"]).unlink()
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=w["token"]))["result"] == "not_found"
    await db.run("UPDATE ticket_files SET storage_name='../test.db' WHERE id=?", (row["id"],))
    assert (await ws.workspace_attachment("1", w["own"], row["id"], admin=True))["result"] == "not_found"


async def test_file_link_cascades_and_revision_changes(files_world):
    w = files_world
    before = (await db.one("SELECT workspace_revision FROM tickets WHERE ticket_id=?", (w["own"],)))["workspace_revision"]
    await upload(w["own"])
    row = await db.one("SELECT * FROM ticket_files")
    assert (await db.one("SELECT workspace_revision FROM tickets WHERE ticket_id=?", (w["own"],)))["workspace_revision"] > before
    await db.run("DELETE FROM ticket_messages WHERE id=?", (row["message_id"],))
    assert not await db.many("SELECT * FROM ticket_files")
    assert (await ws.workspace_attachment("200", w["own"], row["id"], session_hash=w["token"]))["result"] == "not_found"


async def test_private_file_values_absent_from_sql_debug_and_audit(files_world, caplog):
    w = files_world
    path, name = await attachments.download_attachment("https://max.example/a", "СЕКРЕТ_ИМЯ.pdf", unique=True)
    caplog.set_level(logging.DEBUG, logger="aiosqlite")
    result = await ws.workspace_attach_file("300", w["own"], storage_name=name,
        display_name="СЕКРЕТ_ИМЯ.pdf", size=os.path.getsize(path))
    assert result["result"] == "ok" and name not in caplog.text and "СЕКРЕТ_ИМЯ.pdf" not in caplog.text
    assert not await db.many("SELECT * FROM admin_log")


async def test_cancelled_association_cleans_new_file_without_success(files_world, monkeypatch, api):
    w = files_world

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ws, "workspace_attach_file", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await upload(w["own"])
    assert list(w["folder"].iterdir()) == []
    assert not await db.many("SELECT * FROM ticket_files")
    assert not api.sent


async def test_schema_never_backfills_untrusted_marker(files_world):
    w = files_world
    await add_ticket_message(w["own"], "300", "student", "📎 Файл: legacy.pdf (1 КБ)")
    await db.init_db()
    assert not await db.many("SELECT * FROM ticket_files")
