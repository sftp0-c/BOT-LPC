"""Сводка дня сис-админа, удаление обращений и максимальные права владельца."""
import pytest

import config
import database as db
import repository as repo
from conftest import add_staff, press, register, say

SYS, STUDENT, STAFF = "1", "100", "200"
OWNER = "46010397"


async def make_ticket(text="Нужна справка", status="new"):
    """Обращение студента к сотруднику через обычный сценарий бота."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "feedback", office="каб. 204")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, text)
    ticket_id = (await db.one("SELECT ticket_id FROM tickets ORDER BY ticket_id DESC"))["ticket_id"]
    if status != "new":
        await press(STAFF, f"st:{ticket_id}:{status}")
    return ticket_id


# ── сводка дня ────────────────────────────────────────────────────────────────
async def test_admin_today_counts_work(api):
    assert await make_ticket() == 1
    summary = await repo.admin_today()
    assert summary["no_answer"] == 1
    assert summary["tickets_day"] == 1
    assert summary["requests"] == 0
    assert summary["avg_reply"] == ""  # ответа ещё не было


async def test_admin_today_sees_answered_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"rp:{tid}")
    await say(STAFF, "Готовим справку")
    summary = await repo.admin_today()
    assert summary["no_answer"] == 0
    assert summary["avg_reply"] and "мин" in summary["avg_reply"]


async def test_admin_today_counts_requests_and_codes(api):
    await repo.create_staff_request("300", "Соколова", "Секретарь")
    await repo.create_invite("ABC234")
    summary = await repo.admin_today()
    assert summary["requests"] == 1 and summary["codes_active"] == 1


async def test_sysadmin_menu_has_today_button(api):
    await press(SYS, "sysadm")
    assert "today" in api.payloads(SYS)
    await press(SYS, "today")
    text = api.last(SYS)[1]
    assert "Что сделать сегодня" in text
    assert "Среднее время ответа" in text


async def test_today_is_closed_for_outsiders(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "today")
    assert not api.to(STUDENT)


async def test_clean_dialogs_removes_stuck_states(api):
    await register(STUDENT)
    await db.set_state(STUDENT, "reg_name")
    await db.run("UPDATE user_states SET created_at=''")
    await press(SYS, "cleandlg")
    assert "Удалено зависших состояний" in api.last(SYS)[1]
    assert await db.get_state(STUDENT) is None


# ── удаление обращения ────────────────────────────────────────────────────────
async def test_sysadmin_deletes_ticket_from_bot(api):
    tid = await make_ticket()
    await press(SYS, f"t:{tid}")
    assert f"tdel:{tid}" in api.payloads(SYS)

    await press(SYS, f"tdel:{tid}")
    assert "Удалить обращение" in api.last(SYS)[1]
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=?", (tid,)) is not None

    await press(SYS, f"tdely:{tid}")
    assert "удалено" in api.last(SYS)[1]
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=?", (tid,)) is None
    # переписка и история ушли по каскаду
    assert await db.one("SELECT 1 FROM ticket_messages WHERE ticket_id=?", (tid,)) is None
    assert await db.one("SELECT 1 FROM ticket_events WHERE ticket_id=?", (tid,)) is None
    # студент узнал об удалении
    assert any("удалено администратором" in text for _, text, _ in api.to(STUDENT))


async def test_staff_cannot_delete_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"t:{tid}")
    assert not [p for p in api.payloads(STAFF) if p.startswith("tdel")]
    api.sent.clear()
    await press(STAFF, f"tdely:{tid}")
    assert not api.to(STAFF)
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=?", (tid,)) is not None


async def test_delete_missing_ticket_is_graceful(api):
    await press(SYS, "tdel:999")
    assert "не найдено" in api.last(SYS)[1]


async def test_panel_deletes_ticket(panel, api):
    await make_ticket()
    assert login(panel)
    body = panel.get("/panel/tickets/1").text
    assert "Удалить обращение" in body

    api.sent.clear()
    assert post(panel, "/panel/tickets/1/delete").status_code == 303
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=1") is None
    assert "удалено администратором" in "\n".join(text for _, text, _ in api.to(STUDENT))
    assert "Обращение №1 удалено" in panel.get("/panel/tickets").text
    log_rows = await repo.admin_log(10)
    assert any("обращение удалено" in item["action"] for item in log_rows)


async def test_panel_delete_unknown_ticket_is_404(panel):
    assert login(panel)
    assert post(panel, "/panel/tickets/999/delete").status_code == 404


# ── максимальные права владельца на корневом уровне ───────────────────────────
async def test_root_owner_is_seeded_with_max_rights(api, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()  # инициализация базы — «корневой уровень»
    owner = await repo.get_admin(OWNER)
    assert owner["role_type"] == "owner"
    assert await repo.is_sysadmin(OWNER)
    assert await repo.is_owner(OWNER)
    # владелец не попадает в список сотрудников и в статистику сотрудников
    assert all(row["user_id"] != OWNER for row in await repo.list_staff())
    assert (await repo.stats_overview())["staff"] == 0


async def test_owner_rights_survive_restart_and_cannot_be_revoked(api, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    await repo.add_sysadmin("700")  # чтобы «последний сис-админ» не мешал

    done, message = await repo.revoke_sysadmin(OWNER)
    assert done is False and "владельц" in message
    await db.init_db()  # перезапуск
    assert (await repo.get_admin(OWNER))["role_type"] == "owner"


async def test_owner_is_not_grantable_as_regular_sysadmin(api, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    done, message = await repo.grant_sysadmin(OWNER, "Дубль")
    assert done is False and "владелец" in message
    assert (await repo.get_admin(OWNER))["role_type"] == "owner"


async def test_owner_sees_admin_panel_and_actions(api, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    await press(OWNER, "sysadm")
    assert "today" in api.payloads(OWNER)
    await press(OWNER, "people")
    assert "Пользователи бота" in api.last(OWNER)[1]


async def test_owner_row_marked_in_panel(panel, monkeypatch):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login(panel)
    body = panel.get("/panel/staff").text
    assert "владелец" in body
    assert f"/panel/staff/sysadmin/{OWNER}/revoke" not in body
    assert "права постоянны" in body


async def test_owner_can_log_into_panel(monkeypatch, env):
    from fastapi.testclient import TestClient

    import bot
    import webpanel

    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-panel-pass")
    await db.init_db()
    webpanel._sessions.clear()
    client = TestClient(bot.app)
    assert client.post("/panel/login", data={"user_id": OWNER, "password": "test-panel-pass"},
                       follow_redirects=False).status_code == 303
    assert "Что сделать сегодня" in client.get("/panel").text
    # и удалять обращения может тоже
    tid = await make_ticket()
    assert post(client, f"/panel/tickets/{tid}/delete").status_code == 303
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=?", (tid,)) is None


# ── вспомогательное ───────────────────────────────────────────────────────────
@pytest.fixture
def panel(monkeypatch, env):
    from fastapi.testclient import TestClient

    import bot
    import webpanel

    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-panel-pass")
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    webpanel._sessions.clear()
    webpanel._flash = ""
    client = TestClient(bot.app)
    client.post("/panel/login", data={"user_id": SYS, "password": "test-panel-pass"}, follow_redirects=False)
    return client


def post(client, path: str, data: dict | None = None):
    token = webpanel_token(client)
    return client.post(path, data={**(data or {}), "csrf": token}, follow_redirects=False)


def webpanel_token(client) -> str:
    import webpanel

    return webpanel._csrf.get(client.cookies.get(webpanel.COOKIE, ""), "")


def login(client) -> bool:
    return client.post("/panel/login", data={"user_id": SYS, "password": "test-panel-pass"},
                       follow_redirects=False).status_code == 303
