"""Приглашение по ссылке: страница без входа, команда /join, одноразовость кода."""
import pytest
import config

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

import database as db
import repository as repo
from conftest import login_panel, say
from utils import as_str

NEWSTAFF = "700"
SYS = "1"


# ── страница приглашения ────────────────────────────────────────────────────
async def test_join_page_shows_the_code(panel_client):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    body = panel_client.get("/join/ABC123").text
    assert "ABC123" in body
    assert "Приглашение" in body


async def test_join_page_needs_no_login(panel_client):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    assert panel_client.get("/join/ABC123").status_code == 200


async def test_join_page_explains_unknown_code(panel_client):
    body = panel_client.get("/join/НЕТТАКОГО").text
    assert "Код не найден" in body


async def test_join_page_warns_about_used_code(panel_client):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await repo.use_invite("ABC123", NEWSTAFF)
    assert "уже использовали" in panel_client.get("/join/ABC123").text


async def test_join_page_shows_deadline(panel_client):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    body = panel_client.get("/join/ABC123").text
    assert "действует до" in body


async def test_join_page_has_bot_button_when_name_known(panel_client):
    await db.set_setting("bot_username", "se14445139_bot")
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    body = panel_client.get("/join/ABC123").text
    assert "Открыть бота в MAX" in body
    assert "se14445139_bot" in body


async def test_join_page_without_name_says_to_open_bot(panel_client):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    body = panel_client.get("/join/ABC123").text
    assert "/join" in body


# ── команда /join в боте ────────────────────────────────────────────────────
async def test_join_command_activates_code(api):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await say(NEWSTAFF, "/join ABC123")
    assert "Код принят" in api.to(NEWSTAFF)[-1][1]


async def test_join_command_accepts_colon_form(api):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await say(NEWSTAFF, "/join:ABC123")
    assert "Код принят" in api.to(NEWSTAFF)[-1][1]


async def test_join_command_ignores_case_and_spaces(api):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await say(NEWSTAFF, "/join  abc123 ")
    assert "Код принят" in api.to(NEWSTAFF)[-1][1]


async def test_join_command_without_code_asks_for_it(api):
    await say(NEWSTAFF, "/join")
    assert "Введите код" in api.to(NEWSTAFF)[-1][1]


async def test_join_command_reports_bad_code(api):
    await say(NEWSTAFF, "/join ZZZZ")
    assert "Код не найден" in api.to(NEWSTAFF)[-1][1]


async def test_code_is_one_time_only(api):
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await say("700", "/join ABC123")
    await say("701", "/join ABC123")
    assert "уже использовали" in api.to("701")[-1][1]


# ── панель: ссылка рядом с кодом ────────────────────────────────────────────
async def test_panel_shows_invite_link(panel_client):
    assert login_panel(panel_client)
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    assert "/join/ABC123" in panel_client.get("/panel/access").text


async def test_invite_link_uses_public_url(panel_client, monkeypatch):
    assert login_panel(panel_client)
    monkeypatch.setattr(config, "PUBLIC_URL", "http://192.168.0.102:8080/")
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    assert "http://192.168.0.102:8080/join/ABC123" in panel_client.get("/panel/access").text


async def test_invite_link_is_relative_without_public_url(panel_client, monkeypatch):
    assert login_panel(panel_client)
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    body = panel_client.get("/panel/access").text
    assert "/join/ABC123" in body
    assert "192.168.0.102" not in body          # адрес не подставлен, ссылка относительная


async def test_used_code_has_no_link(panel_client):
    assert login_panel(panel_client)
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await repo.use_invite("ABC123", NEWSTAFF)
    assert "/join/ABC123" not in panel_client.get("/panel/access").text


# ── вход по приглашению доводит до сотрудника ───────────────────────────────
async def test_join_leads_to_staff_registration(api):
    """Приглашение доводит человека до записи в сотрудники: код, ФИО, должность, кабинет."""
    await repo.create_invite("ABC123", created_by=SYS, ttl_hours=24)
    await say(NEWSTAFF, "/join ABC123")
    await say(NEWSTAFF, "Сидорова Мария Ивановна")
    await say(NEWSTAFF, "Преподаватель математики")
    await say(NEWSTAFF, "214")
    admin = await repo.get_admin(NEWSTAFF)
    assert admin is not None
    assert "Сидорова" in as_str(admin["full_name"])
    assert admin["position"] == "Преподаватель математики"
    assert admin["office"] == "214"
    assert await repo.invite_state("ABC123") == "used"
