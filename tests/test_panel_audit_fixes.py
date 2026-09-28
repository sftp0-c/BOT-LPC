"""Четыре правки панели из разбора: ключи настроек, постраничность, шкала, N+1.

Проверяем поведение, а не наличие строк в коде:

* в редакторе настроек нет служебных ключей бота, а человеческие видны и
  сохраняются; подделанный ключ из формы не пишется в базу;
* длинные списки листаются постранично: страницы не повторяют и не теряют
  строки, и на второй странице видно своё содержимое;
* верх шкалы диаграммы округляется по правилу из докстринга nice_max();
* число SQL-запросов на странице сотрудников и на странице обращений не растёт
  вместе с числом сотрудников.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import database as db
import repository as repo
import webpanel
from conftest import add_staff, login_panel, post_form, register

# ключи, которые бот пишет сам: в панели им не место
SERVICE_KEYS = ("menu_view:1", db.TZ_MIGRATION_KEY, "groups_backfill_v1",
                db.SYSADMINS_REVOKED_KEY, "bot_username", "faq_enabled", "tpl:1:7")

KEY_INPUT = re.compile(r"name='key' value='([^']*)'")


def setting_keys(body: str) -> list[str]:
    """Ключи, которые панель отдаёт в редакторе настроек (из полей формы)."""
    return KEY_INPUT.findall(body)


def rows(body: str) -> int:
    """Сколько строк данных в таблице на странице."""
    return body.count("<tr data-hk")


def names(body: str, prefix: str) -> set[str]:
    """Все подписи с префиксом, которые встретились на странице."""
    return set(re.findall(prefix + r"-[0-9A-Za-zА-Яа-я]+", body))


# ── 1. в редакторе настроек только человеческие ключи ────────────────────────
def test_settings_editor_shows_exactly_the_known_keys(panel_client):
    assert login_panel(panel_client)
    assert setting_keys(panel_client.get("/panel/settings").text) == list(webpanel.HUMAN_SETTING_KEYS)


async def test_service_keys_are_hidden_and_human_ones_are_not(panel_client):
    """Главная проверка: служебные ключи в базе есть, но в панели их нет."""
    for key in SERVICE_KEYS:
        await db.set_setting(key, "1")
    await db.set_setting("welcome_text", "Здравствуйте!")
    await db.set_setting("tickets_enabled", "0")
    assert await db.get_setting("menu_view:1") == "1"      # ключ действительно записан

    assert login_panel(panel_client)
    body = panel_client.get("/panel/settings").text
    keys = setting_keys(body)
    assert "welcome_text" in keys and "tickets_enabled" in keys
    assert "Здравствуйте!" in body                          # значение показано
    for key in SERVICE_KEYS:
        assert key not in keys, f"служебный ключ {key} попал в панель"
        assert key not in body, f"служебный ключ {key} виден текстом на странице"


def test_human_settings_list_is_closed_and_explained():
    """Список ключей закрытый: новый ключ сам собой в панель не попадает."""
    assert "welcome_text" in webpanel.HUMAN_SETTING_KEYS
    assert "tickets_enabled" in webpanel.HUMAN_SETTING_KEYS
    for key in SERVICE_KEYS:
        assert key not in webpanel.HUMAN_SETTING_KEYS
    for key, about in webpanel.HUMAN_SETTINGS:   # у каждого ключа есть объяснение
        assert about.strip() and not about.strip().isdigit()


async def test_human_setting_saves_from_editor(panel_client):
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/settings/raw", {"key": "tunnel_enabled", "value": "1"})
    assert await db.get_setting("tunnel_enabled") == "1"


async def test_editor_refuses_to_write_service_key(panel_client):
    """Ключ не из списка не сохраняется: форма не должна править чужие настройки."""
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/settings/raw",
              {"key": "bot_username", "value": "подделка"})
    assert await db.get_setting("bot_username", "") == ""


# ── 2. постраничность длинных списков ───────────────────────────────────────
async def seed_students(count: int, group: str = "ис-21", prefix: str = "Студент",
                        first_id: int = 1000) -> None:
    for index in range(count):
        uid = str(first_id + index)
        await repo.upsert_user(uid, f"{prefix}-{index:03d}", group)
        await repo.touch_contact(uid, display_name=f"{prefix}-{index:03d}")


async def test_students_list_paginates(panel_client):
    await seed_students(webpanel.STUDENTS_PAGE + 7)
    assert login_panel(panel_client)
    first = panel_client.get("/panel/students").text
    second = panel_client.get("/panel/students", params={"page_no": 2}).text
    assert rows(first) == webpanel.STUDENTS_PAGE
    assert rows(second) == 7
    # список отсортирован по ФИО, поэтому страницы известны наперёд
    assert "Студент-000" in first and "Студент-000" not in second
    assert "Студент-049" in first and "Студент-050" in second
    assert "Страница 1 из 2" in first and "Страница 2 из 2" in second
    assert 'class="pager"' in first and "page_no=2" in first


async def test_students_beyond_the_old_two_hundred_limit(panel_client):
    """Раньше список обрывался на 200 строках: эти 250 должны быть видны все."""
    await seed_students(250)
    assert login_panel(panel_client)
    last = panel_client.get("/panel/students", params={"page_no": 5}).text
    assert "Студент-249" in last and "Студент-000" not in last
    assert "Страница 5 из 5" in last


async def test_students_pager_keeps_filters(panel_client):
    await seed_students(3, group="ис-22", prefix="Второкурсник", first_id=2000)
    await seed_students(webpanel.STUDENTS_PAGE + 3)
    assert login_panel(panel_client)
    body = panel_client.get("/panel/students", params={"group": "ис-22"}).text
    assert rows(body) == 3
    assert "Второкурсник-000" in body and "Студент-000" not in body
    assert "page_no" not in body                        # одна страница - перехода нет


async def test_people_registry_paginates(panel_client):
    """Реестр сортируется по последнему обращению, поэтому сравниваем наборы."""
    await seed_students(webpanel.PEOPLE_PAGE + 5)
    assert login_panel(panel_client)
    first = panel_client.get("/panel/people").text
    second = panel_client.get("/panel/people", params={"page_no": 2}).text
    assert rows(first) == webpanel.PEOPLE_PAGE
    assert rows(second) == 5
    first_names, second_names = names(first, "Студент"), names(second, "Студент")
    assert not first_names & second_names               # страницы не повторяются
    assert len(first_names | second_names) == webpanel.PEOPLE_PAGE + 5   # и не теряют
    assert "kind=" in first                              # фильтр перенесён в ссылки


async def test_templates_list_paginates(panel_client, clear_templates):
    assert clear_templates
    for index in range(webpanel.TEMPLATES_PAGE + 4):
        await repo.add_template(f"Шаблон-{index:03d}", "Текст ответа", "all", "1")
    assert login_panel(panel_client)
    first = panel_client.get("/panel/templates").text
    second = panel_client.get("/panel/templates", params={"page_no": 2}).text
    assert f"шаблонов: {webpanel.TEMPLATES_PAGE + 4}" in first   # счётчик честный
    assert rows(first) == webpanel.TEMPLATES_PAGE and rows(second) == 4
    # новые сверху, поэтому вторая страница - самые старые
    assert "Шаблон-053" in first and "Шаблон-000" not in first
    assert "Шаблон-000" in second


async def test_broadcasts_history_paginates(panel_client):
    for index in range(webpanel.BROADCASTS_PAGE + 3):
        await db.run("INSERT INTO broadcasts(sender_id, audience, text, sent, failed, created_at) "
                     "VALUES('1', 'all', ?, 1, 0, datetime('now'))", (f"Объявление-{index:03d}",))
    assert login_panel(panel_client)
    first = panel_client.get("/panel/broadcasts").text
    second = panel_client.get("/panel/broadcasts", params={"page_no": 2}).text
    assert "Объявление-027" in first and "Объявление-027" not in second
    assert "Объявление-000" in second and "Объявление-000" not in first


# ── 3. верх шкалы диаграммы ─────────────────────────────────────────────────
def test_nice_max_boundaries():
    """Правило из докстринга: вверх, по ряду 1-2-2,5-5 в каждом десятке."""
    expected = {0: 1, 1: 1, 5: 5, 9: 10, 10: 10, 11: 20,
                19: 20, 20: 20, 21: 25, 99: 100, 100: 100}
    for value, top in expected.items():
        assert webpanel.nice_max(value) == top, f"{value} должно давать {top}"


def test_nice_max_never_zero_and_never_too_high():
    for value in (0, 1, 3, 7, 33, 250, 4321, 20001):
        top = webpanel.nice_max(value)
        assert top > 0, f"{value} дало пустую шкалу"
        assert top >= value, f"{value} округлено вниз до {top}"
        assert top <= 2 * value or value <= 1, f"{value} завышено до {top}"
    assert webpanel.nice_max(-5) == 1
    assert webpanel.nice_max(20001) == 25000       # ряд не обрывается на 10000


def test_nice_max_is_monotonic():
    tops = [webpanel.nice_max(value) for value in range(1, 400)]
    assert tops == sorted(tops)


def test_charts_use_the_same_rule():
    """Диаграммы панели зовут ту же функцию, иначе правило изменится только тут."""
    import charts
    assert charts._nice_max is webpanel.nice_max


# ── 4. число запросов не растёт вместе с числом сотрудников ─────────────────
def count_queries(monkeypatch) -> list[str]:
    """Считает запросы к базе вместо того, чтобы их делать."""
    seen: list[str] = []
    real_many, real_one = db.many, db.one

    async def many(sql, params=()):
        seen.append(" ".join(str(sql).split()))
        return await real_many(sql, params)

    async def one(sql, params=()):
        seen.append(" ".join(str(sql).split()))
        return await real_one(sql, params)

    monkeypatch.setattr(db, "many", many)
    monkeypatch.setattr(db, "one", one)
    return seen


async def seed_staff(count: int) -> None:
    for index in range(count):
        await repo.add_staff(str(400 + index), f"Сотрудник-{index:02d}")


async def test_staff_page_queries_do_not_grow_with_staff(panel_client, monkeypatch):
    assert login_panel(panel_client)
    await seed_staff(2)
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/staff").status_code == 200
    small = len(seen)

    for index in range(2, 30):                 # список растёт с 2 до 30 человек
        await repo.add_staff(str(400 + index), f"Сотрудник-{index:02d}")
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/staff").status_code == 200
    assert len(seen) <= small, f"запросов стало {small} -> {len(seen)} при росте сотрудников"


async def test_staff_page_queries_do_not_grow_when_several_are_away(panel_client, monkeypatch):
    """Отпуск не возвращает N+1: список читается одним запросом и с отпусками."""
    assert login_panel(panel_client)
    await seed_staff(10)
    await repo.set_vacation("400", "05.10.2026")
    await repo.set_vacation("401", "05.10.2026")
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/staff").status_code == 200
    with_two = len(seen)

    for index in range(2, 20):
        await repo.add_staff(str(400 + index), f"Сотрудник-{index:02d}")
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/staff").status_code == 200
    assert len(seen) <= with_two, f"{with_two} -> {len(seen)}"


async def test_tickets_page_queries_do_not_grow_with_staff(panel_client, monkeypatch):
    """Страница обращений: галочки «видит чужие» больше не читаются поштучно."""
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login_panel(panel_client)
    await seed_staff(2)
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/tickets").status_code == 200
    small = len(seen)

    for index in range(2, 30):
        await repo.add_staff(str(400 + index), f"Сотрудник-{index:02d}")
    seen = count_queries(monkeypatch)
    assert panel_client.get("/panel/tickets").status_code == 200
    assert len(seen) <= small, f"запросов стало {small} -> {len(seen)} при росте сотрудников"


async def test_tickets_access_still_shows_every_flag(panel_client):
    """Массовая выборка не теряет галочки: право выдано - оно видно, и наоборот."""
    await add_staff("200", "Петрова Анна", position="Секретарь")
    await add_staff("201", "Сидоров Пётр", position="Кассир")
    await repo.set_staff_see_all("201", True)
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    block = body.split("Кто видит чужие обращения", 1)[1]
    forms = re.findall(r'name="user_id" value="(\d+)">(.*?)</form>', block, re.S)
    states = {uid: "checked" in rest for uid, rest in forms}
    assert states == {"200": False, "201": True}


async def test_staff_list_marks_vacation_and_replacement(panel_client):
    """Заместитель ищется по тому же правилу, что и раньше: должность, потом раздел."""
    await add_staff("500", "Петрова Анна", position="Секретарь")
    await add_staff("501", "Сидорова Мария", position="Секретарь")
    await repo.update_admin("500", role="secretary", ticket_category="certificates")
    await repo.update_admin("501", role="secretary", ticket_category="certificates")
    await repo.set_vacation("500", "05.10.2026")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff").text
    assert "в отпуске" in body
    assert "ведёт Сидорова Мария" in body
    # ровно так же считает и репозиторий: правило подмены не разошлось
    assert (await repo.vacation_replacement(await repo.get_admin("500")))["user_id"] == "501"


async def test_staff_list_vacation_without_replacement(panel_client):
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", "05.10.2026")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff").text
    assert "в отпуске" in body and "заместитель не назначен" in body
