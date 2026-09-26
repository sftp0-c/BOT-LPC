"""Упрощение выдачи прав: умный ввод ID, массы, подсказки, «кто без прав», срок кода."""
import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import repository as repo
import webpanel
from conftest import (PANEL_PASSWORD, add_staff, login_panel, post_form, press,  # noqa: F401
                      register, say)
from utils import parse_max_ids, parse_nicks, ttl_label

SYS, STAFF, STUDENT = "1", "200", "100"
NEW1, NEW2, NEW3 = "300", "301", "302"


@pytest.fixture
def panel_client(monkeypatch, env):
    """Панель с паролем и чистыми сессиями; вход — login_panel."""
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PANEL_PASSWORD)
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    webpanel._sessions.clear()
    webpanel._flash = ""
    return TestClient(bot.app)


# ── разбор ввода ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("12345", ["12345"]),
    ("12345, 67890", ["12345", "67890"]),
    ("12345\n67890\n12345", ["12345", "67890"]),
    ("вот список: 300; 301", ["300", "301"]),
    ("@ivanova привет", []),
])
def test_parse_max_ids(text, expected):
    assert parse_max_ids(text) == expected


def test_parse_nicks():
    assert parse_nicks("@ivanova и @petrov, 300") == ["ivanova", "petrov"]


@pytest.mark.parametrize("hours,label", [(0, "бессрочно"), (24, "1 дн"), (168, "7 дн"), (3, "3 ч")])
def test_ttl_label(hours, label):
    assert ttl_label(hours) == label


async def test_staff_targets_resolves_nick_and_digits(api):
    await register(STUDENT)
    await db.run("UPDATE contacts SET username='ivanov' WHERE user_id=?", (STUDENT,))
    found, missing = await repo.staff_targets(f"{NEW1} @ivanov")
    assert [item["user_id"] for item in found] == [NEW1, STUDENT]
    assert all(item["exists"] is False for item in found)
    assert missing == []

    found, missing = await repo.staff_targets("@nikols")
    assert not found and missing == ["nikols"]


async def test_staff_targets_marks_existing(api):
    await add_staff(STAFF, "Петрова Анна")
    (item,) = (await repo.staff_targets(str(STAFF)))[0]
    assert item["exists"] is True and item["full_name"] == "Петрова Анна"


# ── бот: выдача прав пачкой ───────────────────────────────────────────────────
async def test_bulk_add_staff_with_hints(api):
    await press(SYS, "sfadd")
    await say(SYS, f"{NEW1}, {NEW2}")
    assert "Добавлю 2 сотрудников" in api.last(SYS)[1]

    api.sent.clear()
    await press(SYS, "mph:Секретарь")
    assert "Кому они будут отвечать" in api.last(SYS)[1]

    await press(SYS, "sfbc:feedback")
    text = api.last(SYS)[1]
    assert "Добавлено сотрудников: 2" in text
    for uid in (NEW1, NEW2):
        staff = await repo.get_admin(uid)
        assert staff["role_type"] == "staff"
        assert staff["position"] == "Секретарь"
        assert staff["ticket_category"] == "feedback"
    assert any("назначили сотрудником" in note for _, note, _ in api.to(NEW1))


async def test_bulk_add_staff_with_typed_position(api):
    await press(SYS, "sfadd")
    await say(SYS, f"{NEW1} {NEW2} {NEW3}")
    await say(SYS, "Преподаватель")
    await press(SYS, "sfbc:all")
    assert "Добавлено сотрудников: 3" in api.last(SYS)[1]
    assert (await repo.get_admin(NEW3))["position"] == "Преподаватель"


async def test_bulk_add_skips_existing_and_says_who(api):
    await add_staff(STAFF, "Петрова Анна")
    await press(SYS, "sfadd")
    await say(SYS, f"{STAFF}, {NEW1}, {NEW2}")
    assert "Уже в списке" in api.last(SYS)[1]
    await press(SYS, "mph:Секретарь")
    await press(SYS, "sfbc:all")
    text = api.last(SYS)[1]
    assert "Добавлено сотрудников: 2" in text
    assert await repo.get_admin(NEW2)


async def test_bulk_add_by_nickname(api):
    await register(STUDENT, name="Иванов Иван")
    await db.run("UPDATE contacts SET username='ivanov' WHERE user_id=?", (STUDENT,))
    await press(SYS, "sfadd")
    await say(SYS, "@ivanov")
    assert "Добавляю Иванов Иван" in api.last(SYS)[1]
    await say(SYS, "Иванов Иван")
    await say(SYS, "Преподаватель")
    await say(SYS, "-")
    assert (await repo.get_admin(STUDENT))["position"] == "Преподаватель"


async def test_add_unknown_nickname_explains(api):
    await press(SYS, "sfadd")
    await say(SYS, "@nikols")
    assert "мне не знаком" in api.last(SYS)[1]


async def test_add_without_ids_asks_again(api):
    await press(SYS, "sfadd")
    await say(SYS, "просто текст")
    assert "Не нашёл ни одного ID" in api.last(SYS)[1]


async def test_position_hint_updates_card(api):
    await add_staff(STAFF, "Петрова Анна")
    await press(SYS, f"sfph:{STAFF}:Методист")
    text = api.last(SYS)[1]
    assert "Методист" in text
    assert (await repo.get_admin(STAFF))["position"] == "Методист"
    assert f"sfr:{STAFF}" in api.payloads(SYS)  # вернулись в карточку сотрудника


# ── бот: «кто без прав» и выдача из карточки человека ─────────────────────────
async def test_nostaff_lists_people_without_rights(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    await press(SYS, "nostaff")
    text = api.last(SYS)[1]
    assert f"make:{STUDENT}" in api.payloads(SYS)
    assert "Писали боту, но без прав" in text
    assert f"make:{STAFF}" not in api.payloads(SYS)


async def test_make_staff_from_person_card(api):
    await register(STUDENT, name="Соколова Мария")
    await press(SYS, f"make:{STUDENT}")
    assert "должность" in api.last(SYS)[1].lower()
    await press(SYS, "mph:Специалист")
    assert "Кому он отвечает" in api.last(SYS)[1]
    await press(SYS, "mkc:certificates")
    staff = await repo.get_admin(STUDENT)
    assert staff["position"] == "Специалист" and staff["ticket_category"] == "certificates"
    assert any("назначили роль сотрудника" in note for _, note, _ in api.to(STUDENT))
    assert f"sfr:{STUDENT}" in api.payloads(SYS)  # показали карточку нового сотрудника


async def test_make_staff_reports_duplicate(api):
    await register(NEW2, name="Петрова Анна")
    await add_staff(NEW2, "Петрова Анна")
    await press(SYS, f"make:{NEW2}")
    text = api.last(SYS)[1]
    assert "уже сотрудник" in text
    assert f"sf:{NEW2}" in api.payloads(SYS)


async def test_today_mentions_people_without_rights(api):
    await register(STUDENT)
    await press(SYS, "today")
    assert "без прав сотрудника: 1" in api.last(SYS)[1]


# ── бот: массовые права сис-админа ─────────────────────────────────────────────
async def test_grant_sysadmin_to_several(api):
    await press(SYS, "sysadd")
    await say(SYS, f"{NEW1}, {NEW2}")
    text = api.last(SYS)[1]
    assert "выданы: 2" in text
    for uid in (NEW1, NEW2):
        assert (await repo.get_admin(uid))["role_type"] == "sysadmin"
        assert any("права сис-админа" in note for _, note, _ in api.to(uid))


async def test_grant_sysadmin_list_skips_existing(api):
    await repo.add_sysadmin(NEW1)
    await press(SYS, "sysadd")
    await say(SYS, f"{NEW1}, {NEW2}")
    text = api.last(SYS)[1]
    assert "Уже сис-админ, пропускаю: 300" in text
    await say(SYS, "-")
    assert "выданы: 1" in api.last(SYS)[1]
    assert (await repo.get_admin(NEW2))["role_type"] == "sysadmin"

    await press(SYS, "sysadd")
    await say(SYS, f"{NEW1}, {NEW2}")
    assert "Уже сис-админ" in api.last(SYS)[1]


async def test_revoke_sysadmin_from_list(api):
    await repo.add_sysadmin(NEW1)
    await repo.add_sysadmin(NEW2)
    await press(SYS, f"sysdel:{NEW1},{NEW2}")
    assert "Всего 2" in api.last(SYS)[1]
    await press(SYS, f"sysdely:{NEW1},{NEW2}")
    assert "Права сняты: 2" in api.last(SYS)[1]
    assert await repo.get_admin(NEW1) is None and await repo.get_admin(NEW2) is None


async def test_promote_staff_to_sysadmin_from_card(api):
    await add_staff(STAFF, "Петрова Анна")
    await press(SYS, f"sf:{STAFF}")
    assert f"sfsa:{STAFF}" in api.payloads(SYS)
    await press(SYS, f"sfsa:{STAFF}")
    assert (await repo.get_admin(STAFF))["role_type"] == "sysadmin"
    assert any("права сис-админа" in note for _, note, _ in api.to(STAFF))


# ── бот: срок кода кнопкой ────────────────────────────────────────────────────
async def test_code_ttl_can_be_changed(api):
    await press(SYS, "codegen")
    code = api.last(SYS)[1].split("Код: ")[1].split("\n")[0].strip()
    await press(SYS, f"codettl:{code}:168")
    assert "срок теперь 7 дн" in api.last(SYS)[1]
    row = await db.one("SELECT expires_at FROM staff_invites WHERE code=?", (code,))
    assert row["expires_at"]


async def test_code_ttl_becomes_forever(api):
    await press(SYS, "codegen")
    code = api.last(SYS)[1].split("Код: ")[1].split("\n")[0].strip()
    await press(SYS, f"codettl:{code}:0")
    assert "бессрочно" in api.last(SYS)[1]
    row = await db.one("SELECT expires_at FROM staff_invites WHERE code=?", (code,))
    assert row["expires_at"] == ""


async def test_code_ttl_of_used_code_is_refused(api):
    await press(SYS, "codegen")
    code = api.last(SYS)[1].split("Код: ")[1].split("\n")[0].strip()
    await db.run("UPDATE staff_invites SET used_by=? WHERE code=?", (NEW1, code))
    await press(SYS, f"codettl:{code}:24")
    assert "уже использован" in api.last(SYS)[1]


# ── панель ────────────────────────────────────────────────────────────────────
async def test_panel_adds_several_staff_at_once(panel_client, api):
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/staff/add", {
        "user_id": f"{NEW1}, {NEW2}", "full_name": "", "position": "Специалист",
        "department": "", "office": "", "role": "", "ticket_category": "feedback",
        "can_broadcast": "0",
    }).status_code == 303
    body = panel_client.get("/panel/staff").text
    assert "Добавлено сотрудников: 2" in body
    for uid in (NEW1, NEW2):
        assert (await repo.get_admin(uid))["position"] == "Специалист"


async def test_panel_add_reports_bad_input(panel_client):
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/staff/add", {"user_id": "не-цифры"}).status_code == 303
    assert "только из цифр" in panel_client.get("/panel/staff").text


async def test_panel_grants_and_revokes_several_sysadmins(panel_client):
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/staff/sysadmin", {"user_id": f"{NEW1}, {NEW2}"}).status_code == 303
    assert "выданы: 2" in panel_client.get("/panel/staff").text
    assert post_form(panel_client, "/panel/staff/sysadmin/revoke", {"user_id": f"{NEW1}, {NEW2}"}).status_code == 303
    assert "Права сняты: 2" in panel_client.get("/panel/staff").text
    assert await repo.get_admin(NEW1) is None


async def test_panel_promotes_staff_by_button(panel_client):
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff").text
    assert f"/panel/staff/promote/{STAFF}" in body
    assert post_form(panel_client, f"/panel/staff/promote/{STAFF}").status_code == 303
    assert (await repo.get_admin(STAFF))["role_type"] == "sysadmin"


async def test_panel_search_filters_staff(panel_client):
    await add_staff(STAFF, "Петрова Анна", office="204")
    await add_staff(NEW1, "Соколов Иван", office="101")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff?q=Соколов").text
    assert "Соколов Иван" in body and "Петрова Анна" not in body
    assert "Соколов Иван" in panel_client.get("/panel/staff?q=101").text


async def test_panel_shows_staff_activity(panel_client):
    await add_staff(STAFF, "Петрова Анна", category="feedback")
    await register(STUDENT)
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Нужна справка")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff").text
    assert "За 90 дней / открытых" in body
    assert "1 / 1" in body


async def test_panel_nostaff_page_and_make(panel_client, api):
    await register(STUDENT, name="Соколова Мария")
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/nostaff").text
    assert "Без прав" in body
    assert f"/panel/access/make/{STUDENT}" in body
    assert f"/panel/access/make/{STAFF}" not in body

    assert post_form(panel_client, f"/panel/access/make/{STUDENT}").status_code == 303
    staff = await repo.get_admin(STUDENT)
    assert staff["full_name"] == "Соколова Мария" and staff["ticket_category"] == "all"


async def test_panel_person_card_grants_sysadmin(panel_client):
    await register(STUDENT, name="Соколова Мария")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/people/{STUDENT}").text
    assert f"/panel/access/sysadmin/{STUDENT}" in body
    assert post_form(panel_client, f"/panel/access/sysadmin/{STUDENT}").status_code == 303
    assert (await repo.get_admin(STUDENT))["role_type"] == "sysadmin"


async def test_panel_code_uses_selected_ttl(panel_client):
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/access/code", {"user_id": "", "ttl_hours": "168"}).status_code == 303
    body = panel_client.get("/panel/access").text
    assert "Срок: 7 дн" in body
    row = await db.one("SELECT expires_at FROM staff_invites ORDER BY created_at DESC LIMIT 1")
    assert row["expires_at"]


# ── бот: доступ сис-админа к обращениям и расписаниям ──────────────────────────
async def test_sysadmin_menu_has_ticket_and_schedule_buttons(api):
    await press(SYS, "sysadm")
    assert {"snew", "view_schedules"} <= set(api.payloads(SYS))


async def test_sysadmin_creates_ticket_to_colleague(api):
    await add_staff(NEW1, "Соколов Иван", category="feedback")
    await press(SYS, "snew")
    assert "Выберите раздел" in api.last(SYS)[1]
    await press(SYS, "new:feedback")
    await press(SYS, f"pick:feedback:{NEW1}")
    assert "Кому: Соколов Иван" in api.last(SYS)[1]
    await say(SYS, "Нужна подпись на приказ")
    text = api.last(SYS)[1]
    assert "Обращение №1 отправлено" in text

    ticket = await db.one("SELECT * FROM tickets WHERE ticket_id=1")
    assert ticket["student_id"] == SYS and ticket["target_admin_id"] == NEW1
    notice = "\n".join(body for _, body, _ in api.to(NEW1))
    assert "Сис-админ · сис-админ" in notice and "Нужна подпись на приказ" in notice


async def test_sysadmin_sees_own_tickets(api):
    await add_staff(NEW1, "Соколов Иван", category="feedback")
    await press(SYS, "snew")
    await press(SYS, "new:feedback")
    await press(SYS, f"pick:feedback:{NEW1}")
    await say(SYS, "Вопрос по расписанию")
    await press(SYS, "tickets")
    assert "t:1" in api.payloads(SYS)


async def test_sysadmin_opens_schedule_of_group(api):
    await repo.upsert_group("ИС-21", "Информационные системы")
    await repo.upsert_schedule("ИС-21", "https://college.example/is-21.pdf")
    await press(SYS, "view_schedules")
    assert "sched:ИС-21" in api.payloads(SYS)


async def test_stranger_cannot_use_sysadmin_ticket_start(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "snew")
    assert not api.to(STUDENT)
