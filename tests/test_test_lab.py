"""Раздел панели «Тест»: тестовые сотрудники, доступ и сквозной путь.

Проверки поведенческие, как и остальные проверки панели:

* тестовый сотрудник получает синтетический ID следующего свободного номера и
  метку ``is_test``;
* он попадает в ``list_staff()`` и в ``staff_for_category()`` - без этого
  студент не увидит его в боте и не сможет написать обращение (это главная
  проверка раздела, а не украшение);
* обычный сотрудник в тестовые не попадает, а тестовый виден в общем списке;
* правка не стирает молча пустое поле, удаление требует слова, снимок базы
  делается после слова, а создание и удаление пишутся в журнал;
* раздел открыт владельцу бота и закрыт обычному сис-админу (404, не 403), и
  попытка попадает в журнал;
* CSRF обязателен на каждой форме.

Отдельно проверяется, что синтетический ID безопасен: колонка ``user_id`` - это
TEXT, а неудачная отправка по нему гасится и не роняет ответ студенту.
"""
import logging
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form
from handlers.common import notify
from store import staff as staff_repo

import webpanel


OWNER = "46010397"     # владелец бота: в тестах он же ROOT_IDS
SYS = "1"              # обычный сис-админ из SYSADMIN_IDS
STAFF = "200"
NAME = "Тестовый Иван Иванович"


# ── хелперы страницы ────────────────────────────────────────────────────────
def plain(cell: str) -> str:
    """Текст ячейки без разметки: значение интереснее, чем теги вокруг него."""
    return re.sub(r"<[^>]+>", "", cell).replace("&quot;", '"').strip()


def staff_rows(body: str) -> list:
    """Строки таблицы «Кто заведён»: список списков текстов ячеек."""
    block = body.split('<table class="data-table">')[1].split("</table>", 1)[0]
    rows = []
    for chunk in re.findall(r"<tr>(.*?)</tr>", block, re.S):
        cells = [plain(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)]
        if cells:
            rows.append(cells)
    return rows


# ── фикстуры ────────────────────────────────────────────────────────────────
@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    """Панель, вошедшая как владелец бота: у него максимальные права."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()                       # владелец попадает в admins как owner
    assert login_panel(panel_client, OWNER)
    return panel_client


@pytest.fixture
async def sys_client(panel_client, monkeypatch, env):
    """Панель, вошедшая как обычный сис-админ: раздел ему не положен."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, SYS)
    return panel_client


async def seed():
    """Настоящий сотрудник: тест не должен задевать живых людей."""
    await add_staff(STAFF, "Петрова Анна", "feedback", office="каб. 204")


# ── синтетический ID: создание ──────────────────────────────────────────────
async def test_new_test_staff_gets_the_next_free_synthetic_id(env):
    """Номер выдаёт раздел, ID не число, метка is_test обязана стоять."""
    first = await staff_repo.add_test_staff(NAME, "Секретарь", "учебная часть", "каб. 000")
    second = await staff_repo.add_test_staff("Тестовый второй")
    assert (first, second) == ("test-1", "test-2")
    assert not first.isdigit(), "синтетический ID не должен быть числом"
    assert await staff_repo.next_test_staff_id() == "test-3"
    row = await staff_repo.get_test_staff(first)
    assert row["is_test"] == 1
    assert (row["full_name"], row["position"], row["office"]) == (NAME, "Секретарь", "каб. 000")
    assert (row["ticket_category"], row["see_all_tickets"]) == ("all", 0)
    assert row["can_broadcast"] == 0, "тестовый сотрудник не должен попадать в рассылку"
    assert row["role_type"] == "staff"
    assert await staff_repo.test_staff_count() == 2


async def test_removed_number_is_free_again(env):
    """Номер считается по занятым: удалённый «test-2» свободен, и его выдадут снова."""
    first = await staff_repo.add_test_staff("Тестовый первый")
    second = await staff_repo.add_test_staff("Тестовый второй")
    assert await staff_repo.delete_test_staff(second)
    assert await staff_repo.add_test_staff("Тестовый второй") == "test-2"
    assert await staff_repo.get_test_staff(first) is not None
    # занятый номер не выдаётся, даже если строка подправлена руками
    await db.run("UPDATE admins SET user_id='test-9' WHERE user_id='test-1'")
    assert await staff_repo.add_test_staff("Тестовый четвёртый") == "test-1"


# ── синтетический ID: почему это безопасно ──────────────────────────────────
async def test_synthetic_id_breaks_nothing_on_the_way_to_the_student(env):
    """Проверка на живом коде проекта, а не на обещании.

    Три места, где нечисловой ID мог бы сломать путь, и все три проверены:
    тип колонки, подпись человека и отправка сообщения по адресу, которого в MAX
    нет.
    """
    made = await staff_repo.add_test_staff(NAME)
    kind = await db.one("SELECT type FROM pragma_table_info('admins') WHERE name='user_id'")
    assert kind["type"].upper() == "TEXT", "user_id обязан быть текстом, иначе ID не приметится"
    # подпись человека в боте строится по ФИО, а не по ID
    assert NAME in [row["full_name"] for row in await staff_repo.list_test_staff()]
    # отправка по несуществующему адресу не должна ронять ответ студенту:
    # MAX отвечает ошибкой, notify() её гасит и пишет в журнал
    env.blocked.add(made)
    assert await notify(made, "проверка отправки") is False
    assert env.to(made) == [], "сообщение не ушло: адреса в MAX не существует"


# ── тестовый виден студенту ─────────────────────────────────────────────────
async def test_test_staff_is_offered_to_the_student_in_the_bot(env):
    """Главная проверка раздела: тестового должно быть видно там же, где обычных."""
    await seed()
    made = await staff_repo.add_test_staff(NAME, "Секретарь", "учебная часть", "каб. 000")

    everyone = {row["user_id"]: row for row in await staff_repo.list_staff()}
    assert STAFF in everyone and made in everyone
    assert everyone[made]["is_test"] == 1, "метка обязана попасть в list_staff()"
    assert everyone[STAFF]["is_test"] == 0

    chosen = await staff_repo.staff_for_category("feedback")
    assert {"is_test"} <= set(chosen[0].keys()), "метка обязана попасть в staff_for_category()"
    picked = {row["user_id"]: row for row in chosen}
    assert STAFF in picked and made in picked
    assert picked[made]["is_test"] == 1
    assert picked[made]["position"] == "Секретарь"
    # выбор студента идёт по той же функции, что и подменю бота: предел мал,
    # и тестовый сотрудник попадает в выдачу наравне с обычным
    assert len(await staff_repo.staff_for_category("feedback", limit=1)) == 1
    assert {made, STAFF} <= {row["user_id"] for row in await staff_repo.staff_for_category("feedback")}


async def test_only_test_staff_is_in_the_test_list(env):
    await seed()
    made = await staff_repo.add_test_staff(NAME)
    assert [row["user_id"] for row in await staff_repo.list_test_staff()] == [made]
    assert STAFF not in [row["user_id"] for row in await staff_repo.list_test_staff()]
    assert await staff_repo.get_test_staff(STAFF) is None
    assert await staff_repo.test_staff_count() == 1


# ── правка, удаление, переключение ─────────────────────────────────────────
async def test_edit_changes_fields_and_empty_means_do_not_touch(env):
    made = await staff_repo.add_test_staff(NAME, "Секретарь", "учебная часть", "каб. 000")
    assert await staff_repo.update_test_staff(made, full_name="Тестовый Иван Петрович",
                                              office="", department="", position="")
    row = await staff_repo.get_test_staff(made)
    assert row["full_name"] == "Тестовый Иван Петрович"
    assert row["office"] == "каб. 000"          # пустое поле - «не трогать», а не «стереть»
    assert row["department"] == "учебная часть"
    assert row["position"] == "Секретарь"
    # права переключаются явно, и «0» - это тоже решение
    assert await staff_repo.update_test_staff(made, see_all="1")
    assert (await staff_repo.get_test_staff(made))["see_all_tickets"] == 1
    assert await staff_repo.update_test_staff(made, see_all="0")
    assert (await staff_repo.get_test_staff(made))["see_all_tickets"] == 0
    # ничего не передали - строка не тронута
    assert await staff_repo.update_test_staff(made) is False
    # править и удалять можно только тестового
    assert await staff_repo.update_test_staff(STAFF, full_name="Взлом") is False
    assert await staff_repo.delete_test_staff(STAFF) is False


async def test_delete_removes_the_row_and_only_a_test_one(env):
    await seed()
    made = await staff_repo.add_test_staff(NAME)
    assert await staff_repo.delete_test_staff(made) is True
    assert await staff_repo.get_test_staff(made) is None
    assert await staff_repo.get_admin(made) is None
    # живого сотрудника репозиторий не отдаёт на удаление даже под чужим предлогом
    assert await staff_repo.delete_test_staff(STAFF) is False
    assert await staff_repo.get_admin(STAFF) is not None
    assert await staff_repo.delete_test_staff("такого-нет") is False


async def test_staff_can_be_switched_into_test_and_back(env):
    await seed()
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 0
    assert STAFF not in [row["user_id"] for row in await staff_repo.list_test_staff()]
    assert await staff_repo.set_staff_test(STAFF, True) is True
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 1
    assert STAFF in [row["user_id"] for row in await staff_repo.list_test_staff()]
    assert STAFF in {row["user_id"] for row in await staff_repo.list_staff()}
    # и обратно: карточка та же, метка снята
    assert await staff_repo.set_staff_test(STAFF, False) is True
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 0
    assert await staff_repo.list_test_staff() == []
    assert await staff_repo.set_staff_test("такого-нет") is False


# ── доступ: раздел только владельца ─────────────────────────────────────────
async def test_section_is_open_to_the_owner_and_closed_to_sysadmin(panel_client, sys_client,
                                                                   caplog, env):
    """Обычный сис-админ раздел не видит: 404, и никакая форма не проходит."""
    await seed()
    made = await staff_repo.add_test_staff(NAME)
    with caplog.at_level(logging.WARNING, logger="panel"):
        for path in ("/panel/test", f"/panel/test/{made}", f"/panel/test/{made}/delete"):
            assert sys_client.get(path, follow_redirects=False).status_code == 404, path
    assert any("«Тест» попытался открыть не владелец" in record.getMessage()
               for record in caplog.records), "попытка не-владельца должна попасть в журнал"
    forms = (("/panel/test/add", {"full_name": "Взлом"}),
             (f"/panel/test/{made}/edit", {"full_name": "Взлом"}),
             (f"/panel/test/{made}/delete", {"word": "удалить"}),
             (f"/panel/test/{made}/switch", {}))
    for path, data in forms:
        assert post_form(sys_client, path, data).status_code == 404, path
    assert await staff_repo.get_test_staff(made) is not None
    assert (await staff_repo.get_admin(made))["full_name"] == NAME

    # владелец входит - и все те же страницы открыты
    assert login_panel(panel_client, OWNER)
    for path in ("/panel/test", f"/panel/test/{made}", f"/panel/test/{made}/delete"):
        assert panel_client.get(path).status_code == 200, path
    assert post_form(panel_client, "/panel/test/add",
                     {"full_name": "Тестовый четвёртый", "ticket_category": "all",
                      "see_all": "0"}).status_code == 303
    assert await staff_repo.test_staff_count() == 2


async def test_plain_staff_is_not_reachable_through_the_test_pages(owner_client, env):
    """Обычный сотрудник не должен открываться, правиться и удаляться как тестовый."""
    await seed()
    assert owner_client.get(f"/panel/test/{STAFF}", follow_redirects=False).status_code == 404
    assert owner_client.get(f"/panel/test/{STAFF}/delete",
                            follow_redirects=False).status_code == 404
    assert post_form(owner_client, f"/panel/test/{STAFF}/edit",
                     {"full_name": "Взлом"}).status_code == 404
    assert post_form(owner_client, f"/panel/test/{STAFF}/delete",
                     {"word": "удалить"}).status_code == 404
    assert (await staff_repo.get_admin(STAFF))["full_name"] == "Петрова Анна"
    # переключение - только POST: обычным GET по нему ничего не сделать
    assert owner_client.get(f"/panel/test/{STAFF}/switch").status_code == 405


# ── меню ────────────────────────────────────────────────────────────────────
def test_menu_has_the_section_in_the_system_group():
    """Пункт «Тест» лежит в группе «Система» рядом с разделом «Данные»."""
    system = [items for name, items in webpanel.NAV_GROUPS if name == "Система"]
    assert len(system) == 1
    paths = [path for path, _title, _icon in system[0]]
    assert "/test" in paths
    assert paths.index("/test") == paths.index("/data") + 1
    title = next(title for path, title, _icon in system[0] if path == "/test")
    assert title == "Тест"


# ── страница ───────────────────────────────────────────────────────────────
async def test_page_shows_the_counter_the_hint_and_the_path_memo(owner_client, env):
    body = owner_client.get("/panel/test").text
    assert "Тестовых сотрудников заведено: 0" in body
    assert "Настоящего MAX ID у тестового сотрудника нет" in body
    assert "тестовый" in body and "test-1" in body
    # кнопка «Путь целиком» ведёт на памятку, которая есть на той же странице
    assert "Путь целиком" in body and 'href="#put"' in body and 'id="put"' in body
    for step in ("Студент откроет бота в MAX", "выберет его",
                 "Рабочее место", "ответить за этого сотрудника"):
        assert step in body, step


async def test_page_lists_the_test_staff_with_buttons(owner_client, env):
    await seed()
    assert post_form(owner_client, "/panel/test/add", {
        "full_name": NAME, "position": "Секретарь", "office": "каб. 000",
        "department": "учебная часть", "ticket_category": "feedback", "see_all": "1",
    }).status_code == 303
    row = await db.one("SELECT * FROM admins WHERE is_test=1")
    assert row["user_id"] == "test-1"
    assert (row["full_name"], row["position"], row["office"], row["department"]) == (
        NAME, "Секретарь", "каб. 000", "учебная часть")
    assert (row["ticket_category"], row["see_all_tickets"]) == ("feedback", 1)

    body = owner_client.get("/panel/test").text
    assert "Тестовых сотрудников заведено: 1" in body
    table = staff_rows(body)
    assert len(table) == 1
    fio, position, office, department, category, mark, rights, _actions = table[0]
    assert fio.startswith(NAME) and "test-1" in fio
    assert (position, office, department) == ("Секретарь", "каб. 000", "учебная часть")
    assert category == "Обратная связь"
    assert mark == "тестовый"
    assert rights == "видит чужие"
    assert 'href="/panel/test/test-1"' in body
    assert 'href="/panel/test/test-1/delete"' in body
    # живой сотрудник - в своей таблице и в тестовые не попал
    assert "Петрова Анна" in body
    assert len(staff_rows(body)) == 1
    assert "заведён тестовый сотрудник" in [item["action"] for item in await repo.admin_log(20)]


async def test_add_without_a_name_changes_nothing(owner_client, env):
    assert post_form(owner_client, "/panel/test/add",
                     {"full_name": "  ", "ticket_category": "all", "see_all": "0"}
                     ).status_code == 303
    assert await staff_repo.list_test_staff() == []
    assert "нужно ФИО" in owner_client.get("/panel/test").text
    assert "заведён тестовый сотрудник" not in [item["action"] for item in await repo.admin_log(20)]


async def test_edit_page_saves_and_keeps_the_untouched_fields(owner_client, env):
    made = await staff_repo.add_test_staff(NAME, "Секретарь", "учебная часть", "каб. 000")
    card = owner_client.get(f"/panel/test/{made}").text
    assert NAME in card and "каб. 000" in card and "тестовый" in card
    assert post_form(owner_client, f"/panel/test/{made}/edit", {
        "full_name": "Тестовый Иван Петрович", "office": "", "position": "",
        "department": "", "ticket_category": "certificates", "see_all": "0"}).status_code == 303
    row = await staff_repo.get_test_staff(made)
    assert row["full_name"] == "Тестовый Иван Петрович"
    assert row["office"] == "каб. 000"                 # пустое поле не стёрло кабинет
    assert row["department"] == "учебная часть"
    assert (row["ticket_category"], row["see_all_tickets"]) == ("certificates", 0)
    entry = next(item for item in await repo.admin_log(20)
                 if item["action"] == "правка тестового сотрудника")
    assert entry["actor_id"] == OWNER and "test-1" in entry["details"]
    assert NAME in entry["details"] and "Тестовый Иван Петрович" in entry["details"]


async def test_delete_needs_the_word_and_leaves_a_snapshot(owner_client, env):
    made = await staff_repo.add_test_staff(NAME)
    page = owner_client.get(f"/panel/test/{made}/delete").text
    assert "удалить" in page and 'name="word"' in page
    assert db.list_backups() == []
    for word in ("", "да", "нет", "удалитьть"):
        assert post_form(owner_client, f"/panel/test/{made}/delete", {"word": word}
                         ).status_code == 303
        assert await staff_repo.get_test_staff(made) is not None, word
        assert db.list_backups() == [], "до слова снимок делать рано"
    assert "не введено" in owner_client.get("/panel/test").text
    assert post_form(owner_client, f"/panel/test/{made}/delete",
                     {"word": "УДАЛИТЬ "}).status_code == 303
    assert await staff_repo.get_test_staff(made) is None
    assert len(db.list_backups()) == 1
    assert "Снимок базы" in owner_client.get("/panel/test").text
    entry = next(item for item in await repo.admin_log(20)
                 if item["action"] == "удалён тестовый сотрудник")
    assert entry["actor_id"] == OWNER and "test-1" in entry["details"] and NAME in entry["details"]


async def test_switch_button_marks_and_unmarks_a_live_staff(owner_client, env):
    await seed()
    body = owner_client.get("/panel/test").text
    assert f'action="/panel/test/{STAFF}/switch"' in body
    assert post_form(owner_client, f"/panel/test/{STAFF}/switch").status_code == 303
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 1
    assert STAFF in [row["user_id"] for row in await staff_repo.list_test_staff()]
    assert "Тестовых сотрудников заведено: 1" in owner_client.get("/panel/test").text
    entry = next(item for item in await repo.admin_log(20)
                 if item["action"] == "тестовый сотрудник")
    assert entry["actor_id"] == OWNER and STAFF in entry["details"]
    # обратно: метка снимается, карточка остаётся
    assert post_form(owner_client, f"/panel/test/{STAFF}/switch").status_code == 303
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 0
    assert await staff_repo.list_test_staff() == []
    assert "снята метка тестового" in [item["action"] for item in await repo.admin_log(20)]


# ── CSRF ────────────────────────────────────────────────────────────────────
async def test_every_form_of_the_section_requires_csrf(owner_client, env):
    await seed()
    made = await staff_repo.add_test_staff(NAME)
    forms = (("/panel/test/add", {"full_name": "Без токена"}),
             (f"/panel/test/{made}/edit", {"full_name": "Без токена"}),
             (f"/panel/test/{made}/delete", {"word": "удалить"}),
             (f"/panel/test/{STAFF}/switch", {}))
    for path, data in forms:
        assert owner_client.post(path, data=data, follow_redirects=False).status_code == 403, path
    # ничего не изменилось
    row = await staff_repo.get_test_staff(made)
    assert row["full_name"] == NAME
    assert await staff_repo.test_staff_count() == 1
    assert (await staff_repo.get_admin(STAFF))["is_test"] == 0
    assert db.list_backups() == []


# ── меню в собранном приложении ────────────────────────────────────────────
async def test_menu_link_is_rendered(owner_client, env):
    assert 'href="/panel/test"' in owner_client.get("/panel/").text
