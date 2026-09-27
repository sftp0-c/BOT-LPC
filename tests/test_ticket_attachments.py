"""Файлы в обращениях: приём вложения, показ в панели, защита доступа."""
import os

import pytest

import attachments
import repository as repo
from conftest import add_staff, login_panel, press, register

STUDENT, STAFF = "300", "200"
FILE_URL = "https://max.example/files/scan.pdf"


@pytest.fixture
async def ticket_id(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    return await repo.create_ticket(STUDENT, STAFF, "certificates", "Нужна справка", "Справка")


@pytest.fixture
def folder():
    """Папка вложений рядом с тестовой базой - путь задаёт сам conftest."""
    path = attachments.attachments_dir()
    os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture
def stored_file(monkeypatch, folder):
    async def fake_download(url, name=""):
        target = attachments.safe_name(name or "file.pdf")
        full = os.path.join(folder, target)
        with open(full, "wb") as handle:
            handle.write(b"%PDF-1.7 test")
        return full, target

    monkeypatch.setattr(attachments, "download_attachment", fake_download)
    return folder


# ── бот ──────────────────────────────────────────────────────────────────────
async def test_attach_button_waits_for_file(ticket_id, api, env):
    await press(STUDENT, f"tattach:{ticket_id}")
    assert "файл" in api.to(STUDENT)[-1][1].lower()
    assert f"tattach:{ticket_id}" in api.payloads(STUDENT)


async def test_file_is_attached_and_staff_notified(ticket_id, api, env, stored_file):
    from handlers import tickets

    await press(STUDENT, f"tattach:{ticket_id}")
    assert await tickets.on_attachment(STUDENT, [
        {"type": "file", "url": FILE_URL, "name": "справка.pdf", "size": 2048}]) is True

    names = await tickets.attached_names(ticket_id)
    assert names == ["справка.pdf"]
    staff_text = "\n".join(body for _, body, _ in api.to(STAFF))
    assert "справка.pdf" in staff_text
    assert "Файл прикреплён" in api.to(STUDENT)[-1][1]


async def test_text_instead_of_file_keeps_waiting(ticket_id, api, env):
    from conftest import say

    await press(STUDENT, f"tattach:{ticket_id}")
    await say(STUDENT, "а это не файл")
    assert "нужен файл" in api.to(STUDENT)[-1][1].lower()
    assert await tickets_state() == "attach_file"


async def tickets_state():
    import database as db

    st = await db.get_state(STUDENT)
    return st["state"] if st else None


async def test_file_without_offer_is_answered_politely(ticket_id, api, env):
    from handlers import tickets

    assert await tickets.on_attachment(STUDENT, [
        {"type": "file", "url": FILE_URL, "name": "справка.pdf"}]) is True
    assert "Прикрепить файл" in api.to(STUDENT)[-1][1]


async def test_too_many_files_are_refused(ticket_id, api, env, stored_file):
    from handlers import tickets

    await press(STUDENT, f"tattach:{ticket_id}")
    for index in range(attachments.ATTACH_MAX if hasattr(attachments, "ATTACH_MAX") else 3):
        await press(STUDENT, f"tattach:{ticket_id}")
        await tickets.on_attachment(STUDENT, [
            {"type": "file", "url": FILE_URL, "name": f"файл{index}.pdf", "size": 100}])
    await press(STUDENT, f"tattach:{ticket_id}")
    await tickets.on_attachment(STUDENT, [
        {"type": "file", "url": FILE_URL, "name": "лишний.pdf", "size": 100}])
    assert "уже прикреплено" in api.to(STUDENT)[-1][1]
    assert len(await tickets.attached_names(ticket_id)) == 3


async def test_cannot_attach_to_foreign_ticket(ticket_id, api, env):
    await press("301", f"tattach:{ticket_id}")
    assert f"tattach:{ticket_id}" not in api.payloads("301")


# ── панель ───────────────────────────────────────────────────────────────────
async def test_panel_shows_file_link(ticket_id, panel_client, env, stored_file):
    from handlers import tickets

    await press(STUDENT, f"tattach:{ticket_id}")
    await tickets.on_attachment(STUDENT, [
        {"type": "file", "url": FILE_URL, "name": "справка.pdf", "size": 100}])
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert "/panel/attachments/" in body
    assert "справка.pdf" in body


def test_attachment_download_requires_login(panel_client, env, stored_file):
    with open(os.path.join(stored_file, "справка.pdf"), "wb") as handle:
        handle.write(b"%PDF-1.7")
    response = panel_client.get("/panel/attachments/справка.pdf", follow_redirects=False)
    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]


def test_attachment_download_serves_file_for_admin(panel_client, env, stored_file):
    with open(os.path.join(stored_file, "справка.pdf"), "wb") as handle:
        handle.write(b"%PDF-1.7 data")
    assert login_panel(panel_client)
    response = panel_client.get("/panel/attachments/справка.pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_attachment_download_blocks_path_tricks(panel_client, env, stored_file):
    assert login_panel(panel_client)
    assert panel_client.get("/panel/attachments/..%2F..%2Fbot.db").status_code in (404, 400)
    assert panel_client.get("/panel/attachments/нет-такого.pdf").status_code == 404
