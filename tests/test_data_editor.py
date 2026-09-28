"""Раздел панели «Данные»: доступ, просмотр, правка и защита правил проекта.

Проверки поведенческие: раздел открыт владельцу бота и закрыт обычному
сис-админу, страница показывает настоящие данные базы (пагинация, сортировка и
фильтр проверяются на реальном содержимом), правка пишется в журнал действий,
пустое поле не затирает значение молча, а удаление человека упирается в правила
репозитория, а не в голый DELETE.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, register

import web.data as data_section
import webpanel


OWNER = "46010397"     # владелец бота: в тестах он же ROOT_IDS
SYS = "1"              # обычный сис-админ из SYSADMIN_IDS
STUDENT = "100"
STAFF = "200"
FIO = "Иванов Иван Иванович"


# ── хелперы страницы ─────────────────────────────────────────────────────────
def plain(cell: str) -> str:
    """Текст ячейки без разметки: значение интереснее, чем теги вокруг него."""
    return re.sub(r"<[^>]+>", "", cell).replace("&quot;", '"').strip()


def table_blocks(body: str) -> list:
    """Все таблицы раздела по очереди: на странице их может быть несколько."""
    return body.split('<table class="data-table">')[1:]


def block_rows(block: str) -> list:
    """Строки одной таблицы: список списков текстов ячеек."""
    rows = []
    for chunk in re.findall(r"<tr>(.*?)</tr>", block.split("</table>", 1)[0], re.S):
        cells = [plain(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)]
        if cells:
            rows.append(cells)
    return rows


def data_rows(body: str) -> list:
    """Строки первой таблицы страницы: список таблиц или строки просмотра."""
    return block_rows(table_blocks(body)[0])


def search_rows(body: str) -> list:
    """Строки результатов общего поиска: на /panel/data это вторая таблица."""
    return block_rows(table_blocks(body)[1])


def listed_row(body: str, name: str) -> str:
    """Строка списка таблиц (главная страница раздела) по имени таблицы."""
    match = re.search(r'<tr><td><a href="/panel/data/table/[^"]*"><code>' + re.escape(name)
                      + r"</code></a></td>.*?</tr>", body, re.S)
    assert match, f"в списке таблиц нет {name}"
    return match.group(0)


# ── фикстуры ─────────────────────────────────────────────────────────────────
@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    """Панель, вошедшая как владелец бота: у него максимальные права."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()                       # владелец попадает в admins как owner
    assert login_panel(panel_client, OWNER)
    return panel_client


async def seed():
    """Настоящее содержимое базы: студент, сотрудник, обращение, группы."""
    await register(STUDENT, FIO, "ис-21")
    await add_staff(STAFF, "Петрова Анна", "feedback", office="каб. 204")
    ticket = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    await repo.upsert_group("ИС-30", "Тридцатая")
    await repo.upsert_group("ИС-31", "Тридцать первая")
    return ticket


async def rowid_of(table: str, where: str, params: tuple) -> int:
    row = await db.one(f"SELECT rowid rid FROM {table} WHERE {where}", params)
    assert row, f"в таблице {table} нет строки {where}"
    return row["rid"]


# ── доступ: раздел только владельца ──────────────────────────────────────────
async def test_section_is_open_to_the_owner_and_closed_to_sysadmin(panel_client, monkeypatch, env):
    """Обычный сис-админ раздел не видит: 404, и никакая форма не проходит."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    await register(STUDENT, FIO, "ис-21")
    assert login_panel(panel_client, SYS)
    pages = ["/panel/data", "/panel/data/table/users", "/panel/data/table/users/row/1",
             "/panel/data/table.csv?name=users", "/panel/data/table/users/row/new"]
    for path in pages:
        assert panel_client.get(path, follow_redirects=False).status_code == 404, path
    assert panel_client.get("/panel/data", params={"q": FIO}).status_code == 404
    # формы раздела закрыты так же
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    for path, data in ((f"/panel/data/table/users/row/{rowid}", {"full_name": "Взлом"}),
                       ("/panel/data/table/users/row", {"user_id": "777"}),
                       (f"/panel/data/table/users/row/{rowid}/delete", {"word": "users"}),
                       (f"/panel/data/user/{STUDENT}/delete", {"word": "удалить"}),
                       (f"/panel/data/user/{STUDENT}/revoke", {"word": "удалить"}),
                       ("/panel/data/ticket/1/archive", {"word": "удалить"})):
        assert post_form(panel_client, path, data).status_code == 404, path
    assert (await db.one("SELECT full_name FROM users WHERE user_id=?", (STUDENT,)))["full_name"] == FIO
    assert await db.one("SELECT 1 FROM admins WHERE user_id=?", (OWNER,)) is not None

    # владелец входит - и все те же страницы открыты
    assert login_panel(panel_client, OWNER)
    for path in pages:
        assert panel_client.get(path).status_code == 200, path
    assert post_form(panel_client, f"/panel/data/table/users/row/{rowid}",
                     {"full_name": "Иванов Иван Андреевич"}).status_code == 303


def test_menu_has_the_section_next_to_the_database_tab():
    """Пункт «Данные» лежит в группе «Система» рядом с «База данных»."""
    system = [items for name, items in webpanel.NAV_GROUPS if name == "Система"]
    assert len(system) == 1
    paths = [path for path, _title, _icon in system[0]]
    assert "/data" in paths
    assert paths.index("/data") == paths.index("/database") + 1
    title = next(title for path, title, _icon in system[0] if path == "/data")
    assert title == "Данные"


async def test_menu_link_is_rendered(owner_client, env):
    assert 'href="/panel/data"' in owner_client.get("/panel/").text


# ── список таблиц ────────────────────────────────────────────────────────────
async def test_table_list_shows_every_table_with_counts(owner_client, env):
    await seed()
    body = owner_client.get("/panel/data").text
    names = {row["name"] for row in await db.many(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    for name in sorted(names):
        assert f"<code>{name}</code>" in body, f"в списке нет таблицы {name}"
    counts = dict(await db.table_counts())
    assert f"<td>{counts['users']}</td>" in listed_row(body, "users")
    assert f"<td>{counts['tickets']}</td>" in listed_row(body, "tickets")
    # объём данных посчитан, а не пусто
    assert re.search(r"<td class=\"small\">[^<]*[БКМГ]", listed_row(body, "users"))
    # служебные таблицы sqlite_* в список не попадают
    assert "<code>sqlite_sequence</code>" not in body


async def test_table_list_shows_when_the_table_changed(owner_client, env):
    """Дата последней строки - из created_at, в исходном виде и по-человечески."""
    await seed()
    last = (await db.one("SELECT MAX(created_at) t FROM groups"))["t"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", last), last
    row = listed_row(owner_client.get("/panel/data").text, "groups")
    assert f'title="{last}"' in row            # исходная метка времени - в подсказке
    assert re.search(r"[а-яА-Я]", row)         # и человеческая подпись рядом


# ── просмотр таблицы ─────────────────────────────────────────────────────────
async def test_table_view_pages_over_real_rows(owner_client, env):
    for index in range(data_section.DATA_PAGE + 5):
        await repo.upsert_user(str(3000 + index), f"Студент-{index:03d}", "ис-30")
    body = owner_client.get("/panel/data/table/users").text
    assert len(data_rows(body)) == data_section.DATA_PAGE
    assert "Страница 1 из 2" in body and "page_no=2" in body
    second = owner_client.get("/panel/data/table/users", params={"page_no": 2}).text
    assert len(data_rows(second)) == 5
    assert "Студент-000" in body and "Студент-000" not in second
    # фильтр переживает переход на следующую страницу
    paged = owner_client.get("/panel/data/table/users",
                             params={"col": "group_code", "eq": "ИС-30", "page_no": 2}).text
    assert "Страница 2 из 2" in paged and "eq=ИС-30" in paged
    # узкий фильтр умещается в одну страницу - перехода нет вовсе
    narrow = owner_client.get("/panel/data/table/users",
                              params={"col": "user_id", "eq": "3000"}).text
    assert len(data_rows(narrow)) == 1 and "page_no" not in narrow


async def test_table_view_sorts_by_column(owner_client, env):
    await seed()
    body = owner_client.get("/panel/data/table/groups").text
    first = [row[0] for row in data_rows(body)]
    assert first == sorted(first)
    desc = owner_client.get("/panel/data/table/groups",
                            params={"sort": "group_code", "dir": "desc"}).text
    assert [row[0] for row in data_rows(desc)] == sorted(first, reverse=True)
    # неизвестная колонка не ломает страницу и не становится запросом
    assert owner_client.get("/panel/data/table/groups", params={"sort": "1; DROP TABLE x"}
                            ).status_code == 200
    assert await db.one("SELECT 1 FROM sqlite_master WHERE name='groups'") is not None


async def test_table_view_filters_by_value_and_substring(owner_client, env):
    await seed()
    exact = owner_client.get("/panel/data/table/users",
                             params={"col": "user_id", "eq": STUDENT}).text
    rows = data_rows(exact)
    assert len(rows) == 1 and STUDENT in rows[0] and FIO in rows[0]
    # подстрока ищется по другой колонке
    found = owner_client.get("/panel/data/table/users",
                             params={"fcol": "full_name", "like": "Иванов"}).text
    assert len(data_rows(found)) == 1
    assert "Ничего не найдено" in owner_client.get(
        "/panel/data/table/users", params={"col": "user_id", "eq": "999999"}).text
    # «%» ищется как символ, а не как шаблон
    assert "Ничего не найдено" in owner_client.get(
        "/panel/data/table/users", params={"fcol": "full_name", "like": "%"}).text


async def test_table_view_shows_schema_and_column_types(owner_client, env):
    body = owner_client.get("/panel/data/table/lessons").text
    assert "PRAGMA table_info" in body and "CREATE TABLE" in body
    assert "INTEGER" in body and "PRIMARY KEY" in body
    for column in ("group_code", "weekday", "lesson_num", "subject", "start", "end"):
        assert f"<code>{column}</code>" in body, column
    assert "NOT NULL" in body


async def test_every_table_and_its_row_open(owner_client, env):
    """Дым по всем таблицам сразу: раздел не должен падать ни на одной из них.

    Таблицы в базе заводятся и меняются руками (а ещё и миграциями), поэтому
    проверка берёт список из sqlite_master, а не список из теста.
    """
    await seed()
    names = [row["name"] for row in await db.many(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]
    assert len(names) > 10
    for name in names:
        assert owner_client.get(f"/panel/data/table/{name}").status_code == 200, name
        assert owner_client.get("/panel/data/table.csv", params={"name": name}
                                ).status_code == 200, name
        assert owner_client.get(f"/panel/data/table/{name}/row/new").status_code == 200, name
        row = await db.one(f'SELECT rowid rid FROM "{name}" LIMIT 1')
        if row:
            page = owner_client.get(f"/panel/data/table/{name}/row/{row['rid']}")
            assert page.status_code == 200, name
            assert "Опасные действия" in page.text, name


async def test_unknown_table_is_404(owner_client, env):
    assert owner_client.get("/panel/data/table/нетакая").status_code == 404
    assert owner_client.get("/panel/data/table/users%22%3B%20DROP%20TABLE%20x").status_code == 404
    assert owner_client.get("/panel/data/table.csv", params={"name": "нетакая"}).status_code == 404


# ── правка строки ────────────────────────────────────────────────────────────
async def test_row_edit_saves_value_and_writes_the_log(owner_client, env):
    await seed()
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    body = owner_client.get(f"/panel/data/table/users/row/{rowid}").text
    assert FIO in body
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}",
                     {"full_name": "Иванов Иван Андреевич", "group_code": "ис-22"}
                     ).status_code == 303
    row = await db.one("SELECT full_name, group_code FROM users WHERE user_id=?", (STUDENT,))
    # значение пишется как ввели (это сырая правка), но сис-админу сказано,
    # что проект ждёт код группы канонически
    assert (row["full_name"], row["group_code"]) == ("Иванов Иван Андреевич", "ис-22")
    assert "канонически" in owner_client.get(f"/panel/data/table/users/row/{rowid}").text

    entry = next(item for item in await repo.admin_log(30)
                 if item["action"] == "правка строки данных")
    assert entry["actor_id"] == OWNER
    assert "users" in entry["details"] and str(rowid) in entry["details"]
    assert "full_name" in entry["details"]
    assert "Иванов Иван Андреевич" in entry["details"]     # что стало
    assert FIO in entry["details"]                         # и что было


async def test_row_form_shows_value_types_and_the_time_hint(owner_client, env):
    await seed()
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    body = owner_client.get(f"/panel/data/table/users/row/{rowid}").text
    assert f'value="{FIO}"' in body                       # текущее значение в поле
    assert 'name="created_at"' in body
    assert "ГГГГ-ММ-ДД ЧЧ:ММ:СС" in body
    assert "без часового пояса" in body
    assert "очистить поле" in body
    # число спрашивается числом, а не строкой
    lessons = owner_client.get("/panel/data/table/lessons/row/new").text
    assert 'name="lesson_num" type="number"' in lessons
    assert 'name="weekday" type="number"' in lessons


async def test_time_column_keeps_the_project_format(owner_client, env):
    await seed()
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    # мусор в дате не записывается
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}",
                     {"created_at": "31.12.2026"}).status_code == 303
    assert (await db.one("SELECT created_at FROM users WHERE user_id=?", (STUDENT,)))["created_at"] \
        != "31.12.2026"
    assert "не похоже на дату" in owner_client.get(f"/panel/data/table/users/row/{rowid}").text
    # канонический вид записывается как есть
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}",
                     {"created_at": "2026-01-31 08:05:00"}).status_code == 303
    assert (await db.one("SELECT created_at FROM users WHERE user_id=?", (STUDENT,)))["created_at"] \
        == "2026-01-31 08:05:00"


async def test_number_column_rejects_text(owner_client, env):
    await seed()
    rowid = await rowid_of("contacts", "user_id=?", (STUDENT,))
    assert post_form(owner_client, f"/panel/data/table/contacts/row/{rowid}",
                     {"messages": "не число"}).status_code == 303
    assert (await db.one("SELECT messages FROM contacts WHERE user_id=?",
                         (STUDENT,)))["messages"] != "не число"
    assert "нужно целое число" in owner_client.get(
        f"/panel/data/table/contacts/row/{rowid}").text
    # а число записывается числом
    assert post_form(owner_client, f"/panel/data/table/contacts/row/{rowid}",
                     {"messages": "42"}).status_code == 303
    assert (await db.one("SELECT messages FROM contacts WHERE user_id=?",
                         (STUDENT,)))["messages"] == 42


async def test_empty_field_does_not_wipe_value(owner_client, env):
    await seed()
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    # пустое поле - это «не трогать», а не «стереть»
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}",
                     {"full_name": "", "group_code": "ис-22"}).status_code == 303
    row = await db.one("SELECT full_name, group_code FROM users WHERE user_id=?", (STUDENT,))
    assert row["full_name"] == FIO
    assert row["group_code"] == "ис-22"
    # стереть можно только явной галочкой
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}",
                     {"clear__full_name": "1"}).status_code == 303
    assert (await db.one("SELECT full_name FROM users WHERE user_id=?", (STUDENT,)))["full_name"] == ""


async def test_row_create_and_delete_go_through_the_log(owner_client, env):
    await seed()
    # обязательное поле без значения по умолчанию - тоже обязательное
    assert post_form(owner_client, "/panel/data/table/groups/row", {"title": "Без кода"}
                     ).status_code == 303
    assert "обязательное поле" in owner_client.get("/panel/data/table/groups/row/new").text
    assert await db.one("SELECT 1 FROM groups WHERE title='Без кода'") is None

    assert post_form(owner_client, "/panel/data/table/groups/row",
                     {"group_code": "ис-40", "title": "Сороковая", "active": "0"}
                     ).status_code == 303
    row = await db.one("SELECT rowid rid, title, active FROM groups WHERE group_code='ис-40'")
    assert (row["title"], row["active"]) == ("Сороковая", 0)
    created = owner_client.get(f"/panel/data/table/groups/row/{row['rid']}").text
    assert "Строка создана" in created and "канонически" in created

    # удаление строки - только после слова-подтверждения
    assert post_form(owner_client, f"/panel/data/table/groups/row/{row['rid']}/delete",
                     {"word": "да"}).status_code == 303
    assert await db.one("SELECT 1 FROM groups WHERE group_code='ис-40'") is not None
    assert "не введено" in owner_client.get(f"/panel/data/table/groups/row/{row['rid']}").text
    assert post_form(owner_client, f"/panel/data/table/groups/row/{row['rid']}/delete",
                     {"word": "groups"}).status_code == 303
    assert await db.one("SELECT 1 FROM groups WHERE group_code='ис-40'") is None

    actions = [item["action"] for item in await repo.admin_log(30)]
    assert "добавлена строка данных" in actions
    assert "удалена строка данных" in actions
    assert all(item["actor_id"] == OWNER
               for item in await repo.admin_log(30) if item["action"].endswith("данных"))


# ── удаление человека и другие бизнес-действия ───────────────────────────────
async def test_person_is_deleted_through_repository_rules(owner_client, env):
    ticket = await seed()                      # обращение в статусе new
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    body = owner_client.get(f"/panel/data/table/users/row/{rowid}").text
    assert f"/panel/data/user/{STUDENT}/delete" in body
    assert "Удалить человека" in body

    # сырым DELETE строку users не удалить: у человека есть правила проекта
    assert post_form(owner_client, f"/panel/data/table/users/row/{rowid}/delete",
                     {"word": "users"}).status_code == 303
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is not None
    assert "запрещено" in owner_client.get(f"/panel/data/table/users/row/{rowid}").text

    # и через репозиторий удаление отклоняется: есть открытое обращение
    assert post_form(owner_client, f"/panel/data/user/{STUDENT}/delete",
                     {"word": "удалить"}).status_code == 303
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is not None
    page = owner_client.get(f"/panel/data/table/users/row/{rowid}").text
    assert "Удаление не выполнено" in page and "открытых обращений" in page

    # закрыли обращение - теперь удаление проходит, а обращения остаются
    await repo.set_ticket_status(ticket, "completed")
    assert post_form(owner_client, f"/panel/data/user/{STUDENT}/delete",
                     {"word": "удалить"}).status_code == 303
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None
    assert await db.one("SELECT 1 FROM contacts WHERE user_id=?", (STUDENT,)) is None
    assert (await repo.get_ticket(ticket))["student_id"] == STUDENT
    assert any(item["action"] == "удаление человека" for item in await repo.admin_log(30))


async def test_person_needs_the_confirm_word(owner_client, env):
    await seed()
    rowid = await rowid_of("users", "user_id=?", (STUDENT,))
    for path, data in ((f"/panel/data/user/{STUDENT}/delete", {"word": ""}),
                       (f"/panel/data/user/{STUDENT}/delete", {}),
                       (f"/panel/data/user/{STUDENT}/delete", {"word": "да"}),
                       (f"/panel/data/user/{STUDENT}/revoke", {"word": "нет"})):
        assert post_form(owner_client, path, data).status_code == 303
        assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is not None, path
    assert rowid


async def test_owner_cannot_be_deleted_or_demoted(owner_client, env):
    await seed()
    rowid = await rowid_of("admins", "user_id=?", (OWNER,))
    body = owner_client.get(f"/panel/data/table/admins/row/{rowid}").text
    assert "Удалить его нельзя" in body
    assert f"/panel/data/user/{OWNER}/delete" not in body
    assert f"/panel/data/user/{OWNER}/revoke" not in body

    # даже если слово введено - отказ, и он виден
    for path in (f"/panel/data/user/{OWNER}/delete", f"/panel/data/user/{OWNER}/revoke"):
        assert post_form(owner_client, path, {"word": "удалить"}).status_code == 303
        assert "нельзя" in owner_client.get("/panel/data").text
    assert (await repo.get_admin(OWNER))["role_type"] == "owner"
    assert await repo.is_owner(OWNER)
    # отказ тоже попал в журнал - видно, что кто-то пробовал
    actions = [item["action"] for item in await repo.admin_log(30)]
    assert "попытка удалить владельца" in actions
    assert "попытка снять права владельца" in actions


async def test_staff_rights_are_dropped_through_repository(owner_client, env):
    await seed()
    await repo.add_staff("777", "Сидоров Пётр")
    rowid = await rowid_of("admins", "user_id=?", ("777",))
    assert "/panel/data/user/777/revoke" in owner_client.get(
        f"/panel/data/table/admins/row/{rowid}").text
    assert post_form(owner_client, "/panel/data/user/777/revoke", {"word": "удалить"}
                     ).status_code == 303
    assert await repo.get_admin("777") is None
    assert any(item["action"] == "снятие прав сотрудника" for item in await repo.admin_log(30))


async def test_ticket_is_archived_not_wiped(owner_client, env):
    ticket = await seed()
    body = owner_client.get(f"/panel/data/table/tickets/row/{ticket}").text
    assert f"/panel/data/ticket/{ticket}/archive" in body
    # сырое удаление строки обращения запрещено
    assert post_form(owner_client, f"/panel/data/table/tickets/row/{ticket}/delete",
                     {"word": "tickets"}).status_code == 303
    assert await repo.get_ticket(ticket) is not None

    assert post_form(owner_client, f"/panel/data/ticket/{ticket}/archive", {"word": "удалить"}
                     ).status_code == 303
    assert (await repo.get_ticket(ticket, include_archived=True))["deleted_at"]
    assert await repo.get_ticket(ticket) is None      # из работы он ушёл
    assert post_form(owner_client, f"/panel/data/ticket/{ticket}/restore", {"word": "удалить"}
                     ).status_code == 303
    assert not (await repo.get_ticket(ticket))["deleted_at"]


# ── снимок базы перед опасным действием ──────────────────────────────────────
async def test_snapshot_is_made_before_a_dangerous_action(owner_client, env):
    await seed()
    assert db.list_backups() == []
    rowid = await rowid_of("groups", "group_code=?", ("ИС-30",))
    assert post_form(owner_client, f"/panel/data/table/groups/row/{rowid}/delete",
                     {"word": "groups"}).status_code == 303
    backups = db.list_backups()
    assert len(backups) == 1 and backups[0]["name"].startswith("bot-")
    # путь к снимку показан сис-админу
    assert "Снимок базы" in owner_client.get("/panel/data").text
    # и снимок действительно рабочий: в нём есть та же схема
    import aiosqlite

    async with aiosqlite.connect(db.backup_path(backups[0]["name"])) as connection:
        connection.row_factory = aiosqlite.Row
        rows = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    assert "users" in {row["name"] for row in rows}


async def test_snapshot_is_made_before_person_is_deleted(owner_client, env):
    await seed()
    assert db.list_backups() == []
    assert post_form(owner_client, f"/panel/data/user/{STUDENT}/delete",
                     {"word": "удалить"}).status_code == 303
    assert len(db.list_backups()) == 1


# ── поиск по всем таблицам ───────────────────────────────────────────────────
async def test_search_finds_value_in_every_table(owner_client, env):
    await seed()
    body = owner_client.get("/panel/data", params={"q": "Тридцать первая"}).text
    found = search_rows(body)
    assert [row[0] for row in found] == ["groups"]
    assert found[0][2] == "title" and "Тридцать первая" in found[0][3]
    # в ответе есть ключ строки и ссылка на неё
    assert re.search(r"/panel/data/table/groups/row/\d+", body)

    # MAX ID находится сразу в нескольких таблицах
    by_id = search_rows(owner_client.get("/panel/data", params={"q": STAFF}).text)
    tables = {row[0] for row in by_id}
    assert {"admins", "tickets"} <= tables, tables
    assert all(row[1].isdigit() for row in by_id)      # ключ строки рядом с таблицей
    # а ненайденное значение говорит об этом прямо
    assert "не нашлось" in owner_client.get("/panel/data", params={"q": "такого-нет-в-базе"}
                                           ).text


async def test_search_treats_like_wildcards_as_text(owner_client, env):
    await seed()
    assert "не нашлось" in owner_client.get("/panel/data", params={"q": "%"}).text
    assert "не нашлось" in owner_client.get("/panel/data", params={"q": "_"}).text


# ── выгрузка в CSV ───────────────────────────────────────────────────────────
async def test_csv_export_keeps_filters_and_opens_in_excel(owner_client, env):
    await seed()
    response = owner_client.get("/panel/data/table.csv",
                                params={"name": "users", "col": "user_id", "eq": STUDENT})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "users.csv" in response.headers["content-disposition"]
    assert response.content[:3] == b"\xef\xbb\xbf"            # BOM для Excel
    text = response.content.decode("utf-8-sig")
    head, line = text.splitlines()[0], text.splitlines()[1]
    assert "user_id" in head and "full_name" in head
    assert STUDENT in line and FIO in line
    assert len(text.strip().splitlines()) == 2        # заголовок и одна строка
    # без фильтра в выгрузке вся таблица
    whole = owner_client.get("/panel/data/table.csv", params={"name": "groups"}).text
    assert "ИС-30" in whole and "ИС-31" in whole


# ── CSRF ─────────────────────────────────────────────────────────────────────
async def test_every_form_of_the_section_requires_csrf(owner_client, env):
    ticket = await seed()
    groups_row = await rowid_of("groups", "group_code=?", ("ИС-30",))
    user_row = await rowid_of("users", "user_id=?", (STUDENT,))
    forms = (
        (f"/panel/data/table/users/row/{user_row}", {"full_name": "Без токена"}),
        ("/panel/data/table/groups/row", {"group_code": "ИС-99"}),
        (f"/panel/data/table/groups/row/{groups_row}/delete", {"word": "groups"}),
        (f"/panel/data/user/{STUDENT}/delete", {"word": "удалить"}),
        (f"/panel/data/user/{STAFF}/revoke", {"word": "удалить"}),
        (f"/panel/data/ticket/{ticket}/archive", {"word": "удалить"}),
    )
    for path, data in forms:
        assert owner_client.post(path, data=data, follow_redirects=False).status_code == 403, path
    # ничего не изменилось
    assert (await db.one("SELECT full_name FROM users WHERE user_id=?", (STUDENT,)))["full_name"] == FIO
    assert await db.one("SELECT 1 FROM groups WHERE group_code IN ('ИС-99','ИС-30')") is not None
    assert await repo.get_admin(STAFF) is not None
    assert not (await repo.get_ticket(ticket))["deleted_at"]
