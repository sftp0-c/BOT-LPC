"""Веб-панель сис-админа: вход, права, правка данных, JSON-API.

Панель работает в том же процессе, что и бот, поэтому тесты бьют по ней через
TestClient с настоящей (временной) базой.
"""
import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import repository as repo
import webpanel
from conftest import click, press, register, say  # noqa: F401  — единый стиль остальных тестов

SYS = "1"        # сис-админ из SYSADMIN_IDS (см. conftest)
PASSWORD = "test-panel-pass"


@pytest.fixture(autouse=True)
def panel(monkeypatch, env):
    """Панель включена, пароль задан, сессии чистые.

    TestClient без `with` — lifespan не запускается: базу уже поднял conftest,
    а поллер и сетевые вызовы бота в этих тестах не нужны.
    """
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    webpanel._sessions.clear()
    webpanel._flash = ""
    return TestClient(bot.app)


def login(client, user_id: str = SYS) -> bool:
    return client.post("/panel/login", data={"user_id": user_id, "password": PASSWORD},
                       follow_redirects=False).status_code == 303


def csrf(client) -> str:
    return client.cookies.get(webpanel.COOKIE, "")


def post(client, path: str, data: dict | None = None):
    return client.post(path, data={**(data or {}), "csrf": csrf(client)}, follow_redirects=False)


# ── вход и доступ ─────────────────────────────────────────────────────────────
def test_panel_requires_login(panel):
    for path in ("/panel/", "/panel/staff", "/panel/logs", "/panel/api/stats"):
        assert panel.get(path, follow_redirects=False).status_code == 303, path


def test_login_rejects_wrong_password_and_stranger(panel):
    assert panel.post("/panel/login", data={"user_id": SYS, "password": "неверно"},
                      follow_redirects=False).status_code == 403
    # обычный сотрудник — не сис-админ
    assert panel.post("/panel/login", data={"user_id": "555", "password": PASSWORD},
                      follow_redirects=False).status_code == 403


def test_login_and_logout(panel):
    assert login(panel)
    assert panel.get("/panel/").status_code == 200
    assert panel.get("/panel/logout", follow_redirects=False).status_code == 303
    assert panel.get("/panel/", follow_redirects=False).status_code == 303


def test_panel_disabled_without_password(panel, monkeypatch):
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "")
    response = panel.get("/panel/", follow_redirects=False)
    assert response.status_code == 503


def test_dashboard_shows_counts(panel):
    assert login(panel)
    body = panel.get("/panel/").text
    assert "Обзор" in body and "студентов" in body and "событий обработано" in body


# ── правка данных ─────────────────────────────────────────────────────────────
async def test_panel_adds_and_edits_staff(panel):
    assert login(panel)
    assert post(panel, "/panel/staff/add", {
        "user_id": "700", "full_name": "Новиков Пётр", "role": "director",
        "office": "101", "ticket_category": "feedback", "can_broadcast": "1",
    }).status_code == 303
    row = await db.one("SELECT * FROM admins WHERE user_id='700'")
    assert (row["full_name"], row["role"], row["office"]) == ("Новиков Пётр", "director", "101")
    assert row["ticket_category"] == "feedback" and row["can_broadcast"] == 1

    assert post(panel, "/panel/staff/700", {
        "full_name": "Новиков Пётр Сергеевич", "role": "", "office": "202",
        "ticket_category": "all", "can_broadcast": "0",
    }).status_code == 303
    row = await db.one("SELECT * FROM admins WHERE user_id='700'")
    assert row["full_name"] == "Новиков Пётр Сергеевич" and row["office"] == "202"
    assert row["role"] == "" and row["can_broadcast"] == 0


async def test_panel_rejects_bad_staff_id(panel):
    assert login(panel)
    assert post(panel, "/panel/staff/add", {"user_id": "не цифры"}).status_code == 303
    assert await db.one("SELECT 1 FROM admins WHERE user_id='не цифры'") is None


async def test_panel_adds_sysadmin_without_env(panel):
    """Роль сис-админа из панели работает сразу — без правки SYSADMIN_IDS."""
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin", {"user_id": "701"}).status_code == 303
    assert await webpanel.is_sysadmin("701")
    # и второй сис-админ может войти под своим ID
    assert login(panel, "701")


async def test_panel_deletes_staff_only_without_open_tickets(panel):
    await register("100")
    assert login(panel)
    await press(SYS, "sfadd")
    await say(SYS, "800")
    await say(SYS, "Сидоров Пётр")
    assert post(panel, "/panel/staff/800/delete").status_code == 303
    assert await repo.get_admin("800") is None


async def test_panel_group_rename_moves_students(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    assert post(panel, "/panel/groups/rename", {"old": "ИС-21", "code": "ИС-22"}).status_code == 303
    assert (await db.one("SELECT group_code FROM users WHERE user_id='100'"))["group_code"] == "ИС-22"


async def test_panel_group_toggle_and_add(panel):
    assert login(panel)
    assert post(panel, "/panel/groups/add", {"code": "ис-30", "title": "ИС-30", "active": "1"}).status_code == 303
    assert (await repo.get_group("ИС-30"))["active"] == 1
    assert post(panel, "/panel/groups/toggle", {"code": "ИС-30"}).status_code == 303
    assert (await repo.get_group("ИС-30"))["active"] == 0


async def test_panel_ticket_status_notifies_student(panel, api):
    await register("100")
    await add_ticket()
    assert login(panel)
    api.sent.clear()
    assert post(panel, "/panel/tickets/1/status", {"status": "accepted"}).status_code == 303
    assert (await repo.get_ticket(1))["status"] == "accepted"
    assert any("Статус обращения" in text for _, text, _ in api.to("100"))


async def test_panel_broadcast_starts_with_right_audience(panel, monkeypatch):
    """Панель запускает рассылку в фоне: проверяем переданные данные, а не сам запуск."""
    await register("100")
    started = []

    async def fake_run(sender, audience, text):
        started.append((sender, audience, text))

    monkeypatch.setattr(webpanel, "run_broadcast", fake_run)
    assert login(panel)
    assert post(panel, "/panel/broadcasts/send", {"audience": "all", "text": "Проверка панели"}).status_code == 303
    await bot.asyncio.sleep(0)  # дать фоновой задаче стартовать
    assert started == [(SYS, "all", "Проверка панели")]


async def test_panel_broadcast_needs_text(panel, monkeypatch):
    started = []

    async def fake_run(sender, audience, text):
        started.append((sender, audience, text))

    monkeypatch.setattr(webpanel, "run_broadcast", fake_run)
    assert login(panel)
    assert post(panel, "/panel/broadcasts/send", {"audience": "all", "text": "  "}).status_code == 303
    assert not started and "Введите текст объявления" in panel.get("/panel/broadcasts").text


async def test_panel_settings_toggle(panel):
    assert login(panel)
    assert post(panel, "/panel/settings/tickets", {"enabled": "1"}).status_code == 303
    assert await db.get_setting("tickets_enabled") == "0"
    assert post(panel, "/panel/settings/welcome", {"value": "Здравствуйте"}).status_code == 303
    assert await db.get_setting("welcome_text") == "Здравствуйте"


async def test_panel_logs_and_test_actions(panel, api):
    assert login(panel)
    assert post(panel, "/panel/logs/test", {"action": "ping"}).status_code == 303
    assert "тестовая запись" in panel.get("/panel/logs").text
    assert post(panel, "/panel/logs/test", {"to": SYS}).status_code == 303
    assert any("Тестовое сообщение" in text for _, text, _ in api.to(SYS))


# ── защита форм и API ─────────────────────────────────────────────────────────
def test_post_without_csrf_is_rejected(panel):
    assert login(panel)
    response = panel.post("/panel/groups/add", data={"code": "ИС-40"}, follow_redirects=False)
    assert response.status_code == 403
    assert webpanel._flash == ""


def test_api_stats_and_health(panel):
    assert login(panel)
    stats = panel.get("/panel/api/stats").json()
    assert stats["overview"]["students"] == 0 and "statuses" in stats
    assert panel.get("/panel/api/logs").json()["file"] == config.LOG_FILE
    assert panel.get("/panel/api/health").json()["ok"] is True
    assert panel.get("/panel/api/health").json()["panel_enabled"] is True


# ── вспомогательное ───────────────────────────────────────────────────────────
async def add_ticket():
    """Обращение студента 100 к сотруднику 200 через бота."""
    await repo.add_staff("200", "Петрова Анна")
    await repo.create_ticket("100", "200", "feedback", "Не открывается журнал")
    return 1
