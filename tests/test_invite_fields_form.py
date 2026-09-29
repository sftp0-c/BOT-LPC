"""Форма с полями: ввёл данные - получил ссылки.

Раньше в разделе была только вставка списка строкой «ФИО; должность; кабинет;
раздел». Человек попросил обычные поля: заполнил, нажал «создать», получил
ссылки. Обе формы сходятся в один список строк, поэтому проверяем и то, что
поля попадают в разбор, и то, что ссылки на выходе настоящие.
"""
import re

import database as db
import pytest
from conftest import login_panel, post_form

@pytest.fixture
async def sysadmin():
    """Сис-админ в базе: раздел открыт не всем подряд."""
    import repository as repo

    await repo.grant_sysadmin("1", "Иванов Иван Иванович")
    return "1"


def fields_count(html: str) -> int:
    """Сколько строк с полями нарисовано на странице."""
    return len(re.findall(r'name="f\d+_full_name"', html))


async def test_page_starts_with_input_fields(panel_client):
    """Главное на странице - поля, а не textarea."""
    assert login_panel(panel_client, "1")
    html = panel_client.get("/panel/invites").text
    assert fields_count(html) >= 6, "на странице нет строк с полями"
    assert 'name="f0_full_name"' in html
    assert 'name="f0_position"' in html
    assert 'name="f0_office"' in html
    assert 'name="f0_category"' in html
    assert "Создать ссылки" in html


async def test_position_hints_come_from_the_registry(panel_client):
    """Должность подсказывается из справочника, а не вписывается руками."""
    import utils

    assert login_panel(panel_client, "1")
    html = panel_client.get("/panel/invites").text
    assert '<datalist id="invite-positions">' in html
    assert utils.POSITIONS[0][1] in html, "первая должность справочника не в подсказках"
    assert 'list="invite-positions"' in html


async def test_more_rows_can_be_added(panel_client):
    """Кнопка «Ещё строк» добавляет строки, но не до бесконечности."""
    assert login_panel(panel_client, "1")
    assert fields_count(panel_client.get("/panel/invites").text) == 6
    assert fields_count(panel_client.get("/panel/invites?more=6").text) == 12
    assert fields_count(panel_client.get("/panel/invites?more=999").text) == 36


async def test_typed_fields_produce_links(panel_client, sysadmin):
    """Заполнил поля, нажал «создать» - приглашение и ссылка на месте."""

    # Ссылка собирается из ника бота, который он узнаёт о себе при старте.
    # Без него страница честно пишет, что собрать ссылку не из чего.
    await db.set_setting("bot_username", "lpc_navigator")

    assert login_panel(panel_client, "1")
    answer = post_form(panel_client, "/panel/invites", {
        "action": "create", "n_rows": "6",
        "f0_full_name": "Иванова Мария Петровна", "f0_position": "Секретарь",
        "f0_office": "214", "f0_category": "certificates", "ttl_hours": "24",
    })
    # Ссылки показываются на той же странице, поэтому ответ 200, а не редирект
    assert answer.status_code == 200
    body = answer.text

    assert "Иванова Мария Петровна" in body
    assert "Секретарь" in body
    assert "214" in body
    assert "https://max.ru/lpc_navigator?start=inv_" in body, "нет ссылки для MAX"

    row = await db.one("SELECT * FROM staff_invites")
    assert row["full_name"] == "Иванова Мария Петровна"
    assert row["position"] == "Секретарь"
    assert row["office"] == "214"
    assert row["category"] == "certificates"
    assert row["used_by"] == "", "приглашение создано уже использованным"


async def test_several_rows_at_once(panel_client, sysadmin):
    """Несколько строк за один раз - столько же приглашений."""

    assert login_panel(panel_client, "1")
    post_form(panel_client, "/panel/invites", {
        "action": "create", "n_rows": "6",
        "f0_full_name": "Петров Пётр", "f0_position": "Учебная часть", "f0_office": "208",
        "f1_full_name": "Сидорова Анна", "f1_position": "Приёмная комиссия", "f1_office": "106",
    })
    rows = await db.many("SELECT * FROM staff_invites ORDER BY full_name")
    assert [row["full_name"] for row in rows] == ["Петров Пётр", "Сидорова Анна"]
    # синонимы сводятся к справочнику
    assert {row["position"] for row in rows} == {"Учебная часть", "Приёмная комиссия"}


async def test_empty_rows_are_skipped(panel_client, sysadmin):
    """Заполнять все строки не нужно: пустые молча пропускаются."""

    assert login_panel(panel_client, "1")
    post_form(panel_client, "/panel/invites", {
        "action": "create", "n_rows": "6",
        "f0_full_name": "Кузнецова Мария", "f0_position": "Библиотека",
        "f2_full_name": "", "f3_full_name": "   ", "f4_office": "210",
    })
    rows = await db.many("SELECT * FROM staff_invites")
    # строка с одним только кабинетом - это не человек, приглашение из неё не выходит
    assert len(rows) == 1, "пустые или безымянные строки попали в приглашения"
    assert rows[0]["full_name"] == "Кузнецова Мария"


async def test_mistyped_data_comes_back_to_the_form(panel_client, sysadmin):
    """Ошибка не съедает ввод: человек видит, что он написал."""
    assert login_panel(panel_client, "1")
    # Смотрим ответ именно этой отправки: по новому адресу полей уже нет
    answer = post_form(panel_client, "/panel/invites", {
        "action": "create", "n_rows": "6",
        "f0_full_name": "Романов Пётр", "f0_position": "Завведомо несуществующая",
        "f0_office": "111",
    })
    body = answer.text
    assert "Романов Пётр" in body, "введённое ФИО пропало"
    assert "111" in body, "введённый кабинет пропал"


async def test_field_and_textarea_agree(panel_client, sysadmin):
    """Поля и вставка списком дают одно и то же - разбор у них общий."""

    assert login_panel(panel_client, "1")
    post_form(panel_client, "/panel/invites", {
        "action": "create", "n_rows": "6", "rows": "Семёнов Олег; Секретарь; 210; всё",
    })
    rows = await db.many("SELECT * FROM staff_invites")
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Семёнов Олег"
    assert rows[0]["position"] == "Секретарь"


async def test_nothing_is_created_without_press(panel_client, sysadmin):
    """Одна кнопка - одно нажатие: пока не нажали, приглашений нет."""

    assert login_panel(panel_client, "1")
    panel_client.get("/panel/invites")
    assert await db.many("SELECT * FROM staff_invites") == []
