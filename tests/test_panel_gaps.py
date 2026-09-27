"""Панель: пробелы в данных и согласие в списке студентов."""
import database as db
import repository as repo
from conftest import add_staff, csrf_of, login_panel, register


async def gaps() -> dict:
    return {row["key"]: row["count"] for row in await repo.data_gaps()}


# ── подсчёт пробелов ────────────────────────────────────────────────────────
async def test_gaps_cover_all_known_gaps():
    assert set(await gaps()) == {"staff", "group", "name", "consent", "schedule"}


async def test_gaps_count_students_without_group_and_consent():
    await register("100", "Иванов Иван Иванович", "ис-21")
    before = await gaps()
    await repo.upsert_user("101", "", "")
    await repo.upsert_user("102", "Сидоров С.С.", "ис-21")
    after = await gaps()
    assert after["group"] - before["group"] == 1
    assert after["name"] - before["name"] == 1
    # согласие есть только у тех, кто дал его явно
    await repo.give_consent("102", "1.0")
    assert (await gaps())["consent"] == before["consent"] + 1


async def test_gaps_count_staff_without_role():
    before = await gaps()
    await add_staff("500", "Петрова Анна", position="", office="-")
    assert (await gaps())["staff"] == before["staff"] + 1


async def test_gaps_count_groups_without_schedule():
    before = await gaps()
    await repo.upsert_group("99-99", title="Без расписания")
    assert (await gaps())["schedule"] == before["schedule"] + 1


async def test_every_gap_has_hint_and_link():
    for row in await repo.data_gaps():
        assert row["title"] and row["hint"] and row["link"].startswith("/panel/")


# ── главная страница панели ─────────────────────────────────────────────────
async def test_panel_shows_gaps_card(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert "Пробелы в данных" in body


async def test_panel_says_all_filled_when_clean(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    if not [row for row in await repo.data_gaps() if row["count"]]:
        assert "Все данные заполнены" in body
    else:
        assert "Студенты" in body


# ── список студентов ────────────────────────────────────────────────────────
async def test_students_page_shows_consent_column(panel_client):
    assert login_panel(panel_client)
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.upsert_user("101", "Петров Пётр", "ис-21")
    body = panel_client.get("/panel/students").text
    assert "Согласие" in body
    assert "✅" in body


async def test_students_filter_by_missing_consent(panel_client):
    assert login_panel(panel_client)
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.upsert_user("101", "Петров Пётр", "ис-21")
    body = panel_client.get("/panel/students?consent=0").text
    assert "Петров Пётр" in body
    assert "Иванов Иван Иванович" not in body


async def test_students_page_survives_empty_fio(panel_client):
    assert login_panel(panel_client)
    await repo.upsert_user("102", "", "ис-21")
    body = panel_client.get("/panel/students").text
    assert "без ФИО" in body


# ── текст согласия в настройках ─────────────────────────────────────────────
async def test_settings_shows_consent_form(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/settings").text
    assert "Согласие на обработку данных" in body
    assert "/panel/settings/consent" in body


async def test_consent_text_saved_from_panel(panel_client):
    assert login_panel(panel_client)
    response = panel_client.post("/panel/settings/consent",
                                 data={"csrf": csrf_of(panel_client), "value": "Текст для ЛПК"},
                                 follow_redirects=False)
    assert response.status_code in (302, 303)
    assert await db.get_setting("consent_text", "") == "Текст для ЛПК"
