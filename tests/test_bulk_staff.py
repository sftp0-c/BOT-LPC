"""Массовый выпуск сотрудников: разбор списка, предпросмотр и ссылки.

Ключевое обещание раздела - проверка ничего не создаёт. Сначала сис-админ
видит, что с каждой строкой не так, и только потом решает выпускать ссылки;
самого сотрудника заводит человек, нажав кнопку в боте.
"""
import re

import pytest

import database as db
import repository as repo
import utils
from conftest import login_panel, post_form

import bot
import web.staff_bulk as bulk
import webpanel

BOT_NAME = "se14445139_bot"
PATH = "/panel/invites"

import web.staff_bulk  # noqa: F401  — импорт и есть регистрация маршрутов

if not any(getattr(route, "path", "") == PATH for route in bot.app.routes):
    # раздел ещё не прописан в web/__init__.py, поэтому панель его не видит:
    # подключаем тот же роутер ещё раз - после добавления импорта шаг будет лишним
    bot.app.include_router(webpanel.router)


def text_of(body: str) -> str:
    """Текст страницы без разметки: в таблице интересуют слова, а не теги."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).replace("&nbsp;", " ")


def preview(rows: str):
    return bulk.parse_rows(rows)


# ── разбор строки ────────────────────────────────────────────────────────────
def test_semicolon_is_the_main_separator():
    row = preview("Иванов Иван Иванович; Секретарь; 214; Справки")[0]
    assert row["full_name"] == "Иванов Иван Иванович"
    assert row["position"] == "Секретарь"
    assert row["office"] == "214"
    assert row["category"] == "certificates"
    assert row["problems"] == []


def test_tab_is_accepted_too():
    """Список обычно копируют из таблицы, где разделителем был таб."""
    row = preview("Иванов Иван Иванович\tСекретарь\t214\tСправки")[0]
    assert (row["position"], row["office"], row["category"]) == ("Секретарь", "214", "certificates")


def test_comma_is_accepted_too():
    """Запятая - тоже разделитель: её ставят руками вместо точки с запятой."""
    row = preview("Иванов Иван Иванович, Бухгалтерия, 220, Деньги")[0]
    assert (row["position"], row["office"], row["category"]) == ("Бухгалтерия", "220", "accounting")


def test_category_may_be_left_out():
    """Без раздела сотрудник получает все обращения, а не ни одного."""
    assert preview("Иванов Иван Иванович; Секретарь; 214")[0]["category"] == "all"


def test_blank_line_inside_the_list_is_reported():
    """Пустая строка посреди списка - ошибка формата, её видно, а не теряем человека."""
    rows = preview("Иванов Иван Иванович; Секретарь; 214\n\nСидоров Пётр; Охрана; 1")
    assert len(rows) == 3
    assert rows[1]["problems"] == ["пустая строка"]


def test_trailing_empty_lines_are_not_people():
    """Хвостовые пустые строки - это не строки списка, а следы копирования."""
    assert len(preview("Иванов Иван Иванович; Секретарь; 214\n\n")) == 1


def test_unreadable_row_is_marked():
    """Строку, из которой не собралось четыре поля, надо поправить руками."""
    assert "не разобрали" in preview("; секретарь; 214")[0]["problems"][0]
    assert "не разобрали" in preview("Иванов; Секретарь; 214; Справки; Лишнее")[0]["problems"][0]


def test_position_is_taken_from_the_reference_book():
    """«ПК» и «приёмная комиссия» - одна должность, а не две разные строки."""
    first = preview("Иванов Иван Иванович; ПК; 115")[0]
    second = preview("Сидорова Анна; приёмная комиссия; 115")[0]
    assert first["position"] == second["position"] == "Приёмная комиссия"
    assert first["problems"] == []


def test_unknown_position_is_reported():
    """Должности вне справочника не выдумываем: строка идёт на правку."""
    assert "должность не в справочнике" in preview("Иванов; Мастер танцев; 1")[0]["problems"]


def test_missing_position_is_reported():
    """Без должности сотрудник не увидит своего раздела - такую строку правят."""
    assert "должность не указана" in preview("Иванов Иван Иванович")[0]["problems"][0]


# ── предпросмотр ничего не создаёт ──────────────────────────────────────────
@pytest.mark.panel
async def test_check_button_creates_nothing(panel_client):
    assert login_panel(panel_client)
    before = await db.many("SELECT COUNT(*) n FROM staff_invites")
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214; Справки",
        "action": "check"}).text
    after = await db.many("SELECT COUNT(*) n FROM staff_invites")
    assert before[0]["n"] == after[0]["n"] == 0
    assert await repo.get_admin("700") is None
    assert "Проверено строк: 1" in body


@pytest.mark.panel
async def test_check_shows_what_is_wrong(panel_client):
    """Предпросмотр объясняет проблему каждой строки, а не молчит про неё."""
    assert login_panel(panel_client)
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Мастер танцев; 1\n\nИванов; Секретарь; 214",
        "action": "check"}).text
    text = text_of(body)
    assert "должность не в справочнике" in text
    assert "пустая строка" in text
    assert "Проверено строк: 3" in body


# ── создание приглашений ─────────────────────────────────────────────────────
@pytest.mark.panel
async def test_create_gives_an_invite_to_every_good_row(panel_client):
    assert login_panel(panel_client)
    await db.set_setting("bot_username", BOT_NAME)
    body = post_form(panel_client, PATH, {
        "rows": ("Иванов Иван Иванович; Секретарь; 214; Справки\n"
                 "Сидорова Анна Петровна; Бухгалтерия; 220\n"
                 "Кузнецов Пётр; Мастер танцев; 1"),
        "action": "create"}).text
    issued = await db.many("SELECT * FROM staff_invites ORDER BY full_name")
    assert len(issued) == 2
    assert {row["full_name"] for row in issued} == {
        "Иванов Иван Иванович", "Сидорова Анна Петровна"}
    assert "Создано приглашений: 2" in text_of(body)
    assert "пропущено строк: 1" in text_of(body)


@pytest.mark.panel
async def test_created_link_is_shown_and_opens_the_bot(panel_client):
    """Ссылка собирается из ника бота и ведёт прямо в MAX с кодом приглашения."""
    assert login_panel(panel_client)
    await db.set_setting("bot_username", BOT_NAME)
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214; Справки",
        "action": "create"}).text
    row = await db.one("SELECT code FROM staff_invites")
    expected = utils.profile_url(BOT_NAME) + f"?start=inv_{row['code']}"
    assert expected in body
    assert "Скопировать всё списком" in body


@pytest.mark.panel
async def test_created_link_follows_the_profile_template(panel_client, monkeypatch):
    """Адрес ссылки берётся из настройки, а не написан руками в коде страницы."""
    assert login_panel(panel_client)
    monkeypatch.setattr(utils, "PROFILE_LINK", "https://example.org/bot/{username}")
    await db.set_setting("bot_username", BOT_NAME)
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214",
        "action": "create"}).text
    row = await db.one("SELECT code FROM staff_invites")
    assert f"https://example.org/bot/{BOT_NAME}?start=inv_{row['code']}" in body


@pytest.mark.panel
async def test_staff_appears_only_after_the_person_confirms(panel_client):
    """Кнопка «Создать приглашения» не заводит сотрудника: это делает человек."""
    assert login_panel(panel_client)
    post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214; Справки",
        "action": "create"})
    assert await db.many("SELECT * FROM admins WHERE position='Секретарь'") == []


@pytest.mark.panel
async def test_duplicate_person_lands_in_the_problems(panel_client):
    """Второе приглашение тому же человеку не выпускается молча."""
    assert login_panel(panel_client)
    await repo.add_staff("500", "Иванов Иван Иванович", position="Секретарь")
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214; Справки",
        "action": "create"}).text
    assert await db.many("SELECT * FROM staff_invites") == []
    assert "уже есть в базе" in text_of(body)


@pytest.mark.panel
async def test_repeated_row_in_one_list_is_a_duplicate(panel_client):
    """Повтор в самом списке - тоже дубль: две ссылки на одного человека лишние."""
    assert login_panel(panel_client)
    body = post_form(panel_client, PATH, {
        "rows": ("Иванов Иван Иванович; Секретарь; 214\n"
                 "Иванов  Иван Иванович; ПК; 115"),
        "action": "create"}).text
    issued = await db.many("SELECT * FROM staff_invites")
    assert len(issued) == 1
    assert "уже есть в базе" in text_of(body)


@pytest.mark.panel
async def test_link_is_empty_and_says_so_without_bot_name(panel_client):
    """Пока бот не назвал себя, страница говорит об этом, а не отдаёт битую ссылку."""
    assert login_panel(panel_client)
    body = post_form(panel_client, PATH, {
        "rows": "Иванов Иван Иванович; Секретарь; 214",
        "action": "create"}).text
    assert "Бот ещё не сообщил свой ник" in body
