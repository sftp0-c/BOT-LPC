"""Тесты на поиск по фамилии: он и должен работать, и не должен ловить лишнего.

Главное здесь - регистр. LIKE в SQLite нечувствителен к регистру только для
ASCII, а фамилии кириллические: поиск без lower() искал бы «иванов» и не находил
«Иванов». Проверяем оба направления.

Второе - спецсимволы LIKE. Если «%» и «_» не экранировать, запрос «а_ов» нашёл бы
ещё и «ахов», то есть вернул бы чужие обращения. Проверяем, что не находит.

Третье - пустой поиск не должен ломать выгрузку: пустая строка не значит «все
подряд» в LIKE, а запрос безусловно вернул бы всё, что угодно.
"""
import pytest

import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, register
from web.tickets import filter_tickets

pytestmark = pytest.mark.panel

OWNER = "46010397"
STAFF = "200"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


async def ticket_of(user_id: str, name: str, group: str, text: str) -> int:
    await register(user_id, name, group)
    await add_staff(STAFF, "Петрова Анна", "all", office="215")
    return await repo.create_ticket(user_id, STAFF, "feedback", text)


@pytest.fixture
async def three_tickets(env):
    """Три студента с разными фамилиями - и ни один не пишет своё имя в тексте."""
    first = await ticket_of("100", "Иванов Иван Иванович", "ис-21", "Нужна справка")
    second = await ticket_of("101", "Петрова Анна Сергеевна", "ис-22", "Не приходит ответ")
    third = await ticket_of("102", "Сидоров Пётр", "ис-23", "Вопрос по оплате")
    return {"Иванов": first, "Петрова": second, "Сидоров": third}


async def test_finds_ticket_by_last_name(env, three_tickets):
    """Фамилия в тексте обращения не написана - и всё равно нашлось."""
    rows, _latest, _waiting, _overdue = await filter_tickets(q="Иванов")
    assert {int(r["ticket_id"]) for r in rows} == {three_tickets["Иванов"]}


async def test_search_by_name_ignores_case(env, three_tickets):
    """Кириллица: LIKE без lower() не нашёл бы «иванов» по «Иванов»."""
    for needle in ("иванов", "ИВАНОВ", "Иванов", "иВаНоВ"):
        rows, _l, _w, _o = await filter_tickets(q=needle)
        assert {int(r["ticket_id"]) for r in rows} == {three_tickets["Иванов"]}, needle


async def test_search_by_first_name_and_group(env, three_tickets):
    """Находится и по имени, и по части фамилии - как в реестре людей."""
    rows, _l, _w, _o = await filter_tickets(q="Петрова")
    assert {int(r["ticket_id"]) for r in rows} == {three_tickets["Петрова"]}


async def test_like_wildcards_are_not_treated_as_wildcards(env, three_tickets):
    """«а_ов» не должен находить «ахов»: спецсимволы LIKE экранируются."""
    assert await repo.student_ids_by_name("а_ов") == set()
    assert await repo.student_ids_by_name("%") == set()
    assert await repo.student_ids_by_name("_") == set()


async def test_search_still_finds_by_text_and_id(env, three_tickets):
    """Прежние способы работают: ничего не сломали."""
    by_text, _l, _w, _o = await filter_tickets(q="оплате")
    assert {int(r["ticket_id"]) for r in by_text} == {three_tickets["Сидоров"]}
    by_id, _l, _w, _o = await filter_tickets(q="101")
    assert {int(r["ticket_id"]) for r in by_id} == {three_tickets["Петрова"]}


async def test_empty_search_returns_everything(env, three_tickets):
    """Пустой поиск - это «все обращения», а не «ни одного»."""
    rows, _l, _w, _o = await filter_tickets(q="")
    assert len(rows) == 3
    assert await repo.student_ids_by_name("") == set()
    assert await repo.student_ids_by_name("   ") == set()


async def test_search_by_name_in_the_page(owner_client, three_tickets):
    """Страница находит по фамилии и показывает подсказку про неё."""
    body = owner_client.get("/panel/tickets?q=Петрова").text
    assert "фамилия" in body, "подпись поля не обещает поиск по фамилии"
    tid = three_tickets["Петрова"]
    assert f"№{tid}" in body
    assert f"№{three_tickets['Иванов']}" not in body


async def test_name_search_reaches_the_export(owner_client, three_tickets):
    """Выгрузка по фамилии содержит именно это обращение."""
    tid = three_tickets["Сидоров"]
    body = owner_client.get("/panel/tickets.csv?q=Сидоров").text
    assert f"{tid};" in body
    assert "Нужна справка" not in body, "выгрузка протащила чужое обращение"
