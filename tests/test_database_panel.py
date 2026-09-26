"""Управление базой данных из панели: состояние, сжатие, копии, восстановление, чистка."""
import os

import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import webpanel
from conftest import register, say  # noqa: F401

SYS = "1"
PASSWORD = "test-panel-pass"


@pytest.fixture(autouse=True)
def panel(monkeypatch, env, tmp_path):
    """Панель включена, копии складываются во временную папку теста."""
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    webpanel._sessions.clear()
    webpanel._flash = ""
    return TestClient(bot.app)


def login(client) -> bool:
    return client.post("/panel/login", data={"user_id": SYS, "password": PASSWORD},
                       follow_redirects=False).status_code == 303


def csrf(client) -> str:
    """CSRF-токен формы: он отдельный от cookie сессии."""
    return webpanel._csrf.get(client.cookies.get(webpanel.COOKIE, ""), "")


def post(client, path: str, data: dict | None = None):
    return client.post(path, data={**(data or {}), "csrf": csrf(client)}, follow_redirects=False)


# ── состояние ─────────────────────────────────────────────────────────────────
def test_panel_shows_database_state(panel):
    assert login(panel)
    body = panel.get("/panel/database").text
    assert "Состояние базы" in body
    assert "Проверка целостности" in body and "✅ цела" in body
    assert "WAL" in body and "Данные по таблицам" in body
    assert "<code>tickets</code>" in body


def test_database_tab_requires_login(panel):
    assert panel.get("/panel/database", follow_redirects=False).status_code == 303


async def test_counts_grow_with_data(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    counts = dict(await db.table_counts())
    assert counts["users"] == 1
    assert counts["contacts"] >= 1
    storage = await db.storage_info()
    assert storage["page_count"] > 0 and storage["journal_mode"] == "wal"
    assert await db.integrity_check() == "ok"


# ── обслуживание ──────────────────────────────────────────────────────────────
async def test_vacuum_and_checkpoint_keep_data(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    assert post(panel, "/panel/database/vacuum").status_code == 303
    assert "База сжата" in panel.get("/panel/database").text
    assert post(panel, "/panel/database/checkpoint").status_code == 303
    assert "WAL слит" in panel.get("/panel/database").text
    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == 1


# ── резервные копии ───────────────────────────────────────────────────────────
async def test_backup_create_download_and_list(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    assert post(panel, "/panel/database/backup").status_code == 303
    body = panel.get("/panel/database").text
    assert "Копия создана" in body

    backups = db.list_backups()
    assert len(backups) == 1 and backups[0]["name"].startswith("bot-")
    assert os.path.getsize(db.backup_path(backups[0]["name"])) > 0

    response = panel.get(f"/panel/database/backup/{backups[0]['name']}")
    assert response.status_code == 200
    assert response.content[:15] == b"SQLite format 3"


async def test_download_current_database(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    response = panel.get("/panel/database/download")
    assert response.status_code == 200
    assert response.content[:15] == b"SQLite format 3"
    # временный файл выгрузки не остаётся в папке копий
    assert [item for item in db.list_backups()] == []


async def test_backup_restore_returns_data_to_previous_state(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    first = await db.backup_to()

    await register("200", "Петрова Анна", "ис-21")
    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == 2
    name = os.path.basename(first)

    assert post(panel, "/panel/database/restore", {"name": name}).status_code == 303
    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == 1
    body = panel.get("/panel/database").text
    assert "База восстановлена" in body
    # прежнее состояние сохранилось отдельной копией
    assert any("before-restore" in item["name"] for item in db.list_backups())


async def test_restore_rejects_foreign_and_missing_files(panel):
    assert login(panel)
    assert await db.check_backup("bot-нет-такой.db") == "Файл копии не найден."
    assert await db.check_backup("../database.db") == "Файл копии не найден."

    # файл не из базы бота
    import sqlite3
    path = os.path.join(db.backups_folder(), "bot-чужая.db")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(x)")
    assert "это не база бота" in await db.check_backup("bot-чужая.db")

    # нечитаемый файл
    with open(path, "wb") as handle:
        handle.write(b"not a database at all")
    verdict = await db.check_backup("bot-чужая.db")
    assert "не читается" in verdict or "повреждена" in verdict

    done, message = await db.restore_from("bot-чужая.db")
    assert done is False and message
    assert post(panel, "/panel/database/restore", {"name": "bot-чужая.db"}).status_code == 303
    assert "Восстановление не выполнено" in panel.get("/panel/database").text
    # данные на месте — неудачное восстановление ничего не тронуло
    assert await db.integrity_check() == "ok"


def test_backup_path_blocks_directory_traversal():
    assert db.backup_path("../../secret.db") == ""
    assert db.backup_path("bot-../x.db") == ""
    assert db.backup_path("notabot.db") == ""


# ── проверка схемы и починка ───────────────────────────────────────────────────
async def test_missing_table_is_reported_and_repaired(panel):
    """Удалённая вручную таблица: страницы не падают, а предлагают починку."""
    await register("100", "Иванов Иван Иванович", "ис-21")
    await db.run("DROP TABLE users")
    assert await db.missing_objects() == ["таблица users"]

    assert login(panel)
    for path in ("/panel/", "/panel/students", "/panel/people"):
        body = panel.get(path).text
        assert "Восстановить схему" in body, path
        assert "таблица users" in body, path

    # API отвечает 503 и перечисляет, чего не хватает
    api = panel.get("/panel/api/stats")
    assert api.status_code == 503 and api.json()["missing"] == ["таблица users"]

    # вкладка «База данных» показывает то же и чинит одной кнопкой
    tab = panel.get("/panel/database").text
    assert "Схема базы неполна" in tab and "таблица users" in tab
    assert post(panel, "/panel/database/repair").status_code == 303

    assert await db.missing_objects() == []
    after = panel.get("/panel/database").text
    assert "Схема базы неполна" not in after and "соответствует коду" in after
    assert "Создано: таблица users" in after  # подсказка, что таблица пустая
    assert panel.get("/panel/").status_code == 200
    assert (await db.one("SELECT COUNT(*) n FROM groups"))["n"] == 1  # данные не тронуты


async def test_repair_keeps_existing_data(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    assert post(panel, "/panel/database/repair").status_code == 303
    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == 1
    assert "ничего дописывать не пришлось" in panel.get("/panel/database").text


async def test_missing_column_is_detected_and_repaired(panel):
    await db.run("ALTER TABLE user_states DROP COLUMN created_at")
    assert "user_states.created_at" in await db.missing_objects()
    assert login(panel)
    assert "user_states.created_at" in panel.get("/panel/database").text
    assert post(panel, "/panel/database/repair").status_code == 303
    assert await db.missing_objects() == []


async def test_bot_asks_user_to_contact_admin_when_schema_is_broken(panel, api):
    await db.run("DROP TABLE users")
    assert login(panel)
    await say("100", "привет")
    assert "не хватает" in "\n".join(text for _, text, _ in api.to("100"))


async def test_health_reports_schema_state(panel):
    health = panel.get("/panel/api/health").json()
    assert health["ok"] is True and health["missing_schema"] == []
    await db.run("DROP TABLE tickets")
    broken = panel.get("/panel/api/health").json()
    # индексы таблицы удаляются вместе с ней — их тоже перечисляем
    assert broken["ok"] is False
    assert "таблица tickets" in broken["missing_schema"]


async def test_health_endpoint_of_the_bot_reports_schema(panel):
    body = panel.get("/health").json()
    assert body["ok"] is True and body["missing_schema"] == []
    await db.run("DROP TABLE tickets")
    broken = panel.get("/health").json()
    assert broken["ok"] is False and broken["platform"] == "MAX"
    assert "таблица tickets" in broken["missing_schema"]


async def test_only_last_backups_are_kept(panel):
    assert login(panel)
    for _ in range(5):
        await db.backup_to()
    assert len(db.list_backups()) == 3  # config.BACKUP_KEEP


async def test_delete_backup(panel):
    assert login(panel)
    path = await db.backup_to()
    name = os.path.basename(path)
    assert post(panel, "/panel/database/backup/delete", {"name": name}).status_code == 303
    assert db.list_backups() == []
    assert panel.post("/panel/database/backup/delete",
                      data={"name": name, "csrf": csrf(panel)}, follow_redirects=False).status_code == 404


# ── чистка служебных таблиц ───────────────────────────────────────────────────
async def test_prune_removes_old_service_rows(panel):
    await db.run("INSERT INTO processed_updates(key, created_at) VALUES('old', datetime('now','-10 days'))")
    await db.run("INSERT INTO processed_updates(key) VALUES('fresh')")
    await db.run("INSERT INTO login_attempts(user_id, created_at) VALUES('500', datetime('now','-40 days'))")
    await db.set_state("100", "reg_name")
    await db.run("UPDATE user_states SET created_at=datetime('now','-10 days') WHERE user_id='100'")

    assert login(panel)
    assert post(panel, "/panel/database/prune", {"table": "processed_updates", "days": "7"}).status_code == 303
    assert post(panel, "/panel/database/prune", {"table": "login_attempts", "days": "30"}).status_code == 303
    assert post(panel, "/panel/database/prune", {"table": "user_states", "days": "7"}).status_code == 303

    assert [row["key"] for row in await db.many("SELECT key FROM processed_updates")] == ["fresh"]
    assert await db.many("SELECT * FROM login_attempts") == []
    assert await db.get_state("100") is None
    assert "Удалено записей" in panel.get("/panel/database").text


async def test_prune_keeps_business_data_untouched(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    for table in ("processed_updates", "login_attempts", "user_states"):
        post(panel, "/panel/database/prune", {"table": table, "days": "1"})
    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == 1
    assert (await db.one("SELECT COUNT(*) n FROM contacts"))["n"] == 1


async def test_prune_rejects_unknown_table(panel):
    assert login(panel)
    assert post(panel, "/panel/database/prune", {"table": "users", "days": "1"}).status_code == 400
    assert await db.prune("users", 1) == -1


def test_database_actions_require_csrf(panel):
    assert login(panel)
    for path in ("/panel/database/vacuum", "/panel/database/checkpoint", "/panel/database/backup"):
        assert panel.post(path, follow_redirects=False).status_code == 403, path
