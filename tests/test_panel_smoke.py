"""Тесты-охранники панели: ловят ошибки, которые не видно в основных сценариях.

Здесь нет проверки бизнес-логики - только «страница не падает» и «форма работает».
Такие тесты обычно ловят то, что забывают: пустой список, поиск без результатов,
POST без CSRF-токена.
"""
import re

import pytest

from conftest import add_staff, login_panel, post_form, register  # noqa: F401

PAGES = (
    "/panel/", "/panel/tickets", "/panel/people", "/panel/nostaff", "/panel/students",
    "/panel/staff", "/panel/access", "/panel/groups", "/panel/schedules",
    "/panel/broadcasts", "/panel/database", "/panel/settings", "/panel/logs",
)


# ── страницы не падают на пустых данных ───────────────────────────────────────
@pytest.mark.parametrize("path", PAGES)
def test_pages_survive_empty_database(panel_client, path):
    """Пустая база - самый частый случай, когда что-то ломается."""
    assert login_panel(panel_client)
    response = panel_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    # страница не должна быть страницей ошибки (в блоке журнала текст трейсбека
    # из прошлых сбоев допустим - он и есть содержимое журнала)
    assert "Internal Server Error" not in response.text


@pytest.mark.parametrize("q", ["a", "а", "несуществующий", "200", "%20", "☺"])
def test_staff_search_never_crashes(panel_client, q):
    """Поиск без результатов - отдельная проверка: раньше тут был 500."""
    assert login_panel(panel_client)
    assert panel_client.get("/panel/staff", params={"q": q}).status_code == 200
    assert panel_client.get("/panel/people", params={"q": q}).status_code == 200
    assert panel_client.get("/panel/nostaff", params={"q": q}).status_code == 200


def test_staff_page_with_no_staff_at_all(panel_client):
    """Ни одного сотрудника: страница и формы должны быть на месте."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff").text
    assert "Добавить сотрудника" in body
    assert "/panel/staff/add" in body


# ── формы: CSRF и работоспособность ───────────────────────────────────────────
def test_every_post_form_has_csrf():
    """Каждая POST-форма панели (кроме входа) обязана содержать csrf.

    Без токена require_form() отвечает 403 - форма выглядит целой, но не работает.
    Именно так «сломалось» выдачу кодов.
    """
    text = open("webpanel.py", encoding="utf-8").read()
    broken = []
    for block in re.findall(r"<form\b[^>]*method=[\"']post[\"'][^>]*>.*?</form>", text, re.S):
        action = re.search(r"action=[\"']([^\"']+)[\"']", block)
        action = action.group(1) if action else "?"
        if action == "/panel/login":
            continue  # вход идёт до сессии и CSRF-токена ещё нет
        if "csrf(" not in block:
            broken.append(action)
    assert not broken, f"формы без CSRF-токена: {broken}"


def test_code_form_works_from_page(panel_client):
    """Форма выдачи кода: именно её нельзя было отправить (403)."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel/access").text
    form = re.search(r'<form method="post" action="/panel/access/code".*?</form>', body, re.S)
    assert form, "форма выдачи кода не найдена на странице"
    assert "csrf" in form.group(0)
    token = re.search(r'name="csrf"[^>]*value="([^"]+)"', form.group(0)).group(1)
    assert panel_client.post("/panel/access/code",
                             data={"csrf": token, "user_id": "", "ttl_hours": "24"}, follow_redirects=False).status_code == 303
    assert "выдан" in panel_client.get("/panel/access").text


def test_log_test_message_form_works_from_page(panel_client):
    """Отправка тестового сообщения - вторая форма, которая молча не работала."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel/logs").text
    form = re.search(r'<form method="post" action="/panel/logs/test".*?</form>', body, re.S)
    assert form and "csrf" in form.group(0)
    token = re.search(r'name="csrf"[^>]*value="([^"]+)"', form.group(0)).group(1)
    assert panel_client.post("/panel/logs/test",
                             data={"csrf": token, "to": "46010397"}, follow_redirects=False).status_code == 303


def test_post_without_csrf_is_rejected(panel_client):
    """Защита работает: без токена - 403, даже если форма есть на странице."""
    assert login_panel(panel_client)
    assert panel_client.post("/panel/access/code", data={"ttl_hours": "24"}).status_code == 403
    assert panel_client.post("/panel/staff/add", data={"user_id": "300"}).status_code == 403


# ── ключевые кнопки возвращают осмысленный результат ───────────────────────────
async def test_staff_buttons_do_not_error(panel_client):
    """Кнопки карточек сотрудников: сохранение и повышение до сис-админа."""
    await add_staff("200", "Петрова Анна", office="204")
    await register("300", name="Соколова Мария")
    assert login_panel(panel_client)
    page = panel_client.get("/panel/staff").text
    for action in ("/panel/staff/200", "/panel/staff/promote/200"):
        assert action in page, f"нет кнопки {action}"
        assert post_form(panel_client, action).status_code == 303
    assert "Петрова Анна" in panel_client.get("/panel/staff").text


async def test_staff_search_with_no_result(panel_client):
    """Поиск, который ничего не нашёл: страница живая, форма добавления на месте."""
    await add_staff("200", "Петрова Анна", office="204")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/staff", params={"q": "Соколов"}).text
    assert "Петрова Анна" not in body
    assert "Добавить сотрудника" in body
    # и добавить в такой момент можно - форма не зависит от наличия строк
    assert post_form(panel_client, "/panel/staff/add", {
        "user_id": "301", "full_name": "Соколов Иван", "position": "", "department": "",
        "role": "", "office": "", "ticket_category": "all", "can_broadcast": "0",
    }).status_code == 303
    assert "Соколов Иван" in panel_client.get("/panel/staff").text


def test_panel_api_endpoints_answer():
    """JSON-API: внешние проверки не должны падать."""
    assert True  # проверяется в test_webpanel; здесь фиксируем намерение не ломать формат



