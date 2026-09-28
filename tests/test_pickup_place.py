"""Тесты: кабинет 115 зашит только под справки, для остального его выбирают."""
import pytest

import repository as repo
import webpanel as wp
from conftest import add_staff, login_panel, post_form, register

STUDENT, STAFF = "300", "200"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь", office="каб. 204")
    return env


def _row(**over) -> dict:
    base = {"category": "", "topic": "", "text_content": "", "pickup_place": ""}
    base.update(over)
    return base


async def ticket(category="", topic="", text="Прошу помочь", pickup_place="") -> int:
    тикет = await repo.create_ticket(STUDENT, STAFF, category or "feedback", text, topic)
    if pickup_place:
        await repo.update_ticket(тикет, STAFF, pickup_place=pickup_place)
    return тикет


# ── где живёт 115 ─────────────────────────────────────────────────────────────
def test_115_остаётся_под_справки():
    assert wp.CERT_PICKUP == "115"


def test_справка_узнаётся_по_разделу_теме_и_тексту():
    assert wp.is_certificate(_row(category="certificates")) is True
    assert wp.is_certificate(_row(category="documents")) is True
    assert wp.is_certificate(_row(topic="Справка для военкомата")) is True
    assert wp.is_certificate(_row(text_content="прошу выдать справку")) is True


def test_остальное_не_считается_справкой():
    assert wp.is_certificate(_row(category="session")) is False
    assert wp.is_certificate(_row(topic="Когда сдавать зачётку")) is False
    assert wp.is_certificate(_row(text_content="не сдал зачёт")) is False
    assert wp.is_certificate(_row()) is False


def test_подсказка_разная_для_справок_и_остального():
    assert "115" in wp.pickup_hint(_row(category="certificates"))
    assert "115" not in wp.pickup_hint(_row(category="session"))


async def test_список_кабинетов_берётся_у_сотрудников(panel_client, env):
    assert login_panel(panel_client)
    rooms = await wp.pickup_options()
    assert 'value="115"' in rooms
    assert 'value="каб. 204"' in rooms


# ── кнопка «Готово» ───────────────────────────────────────────────────────────
async def test_справка_сама_получает_115(panel_client, env):
    assert login_panel(panel_client)
    тикет = await ticket(category="certificates", topic="Справка")
    post_form(panel_client, f"/panel/tickets/{тикет}/ready", {})
    строка = await repo.get_ticket(тикет)
    assert строка["pickup_place"] == "115"
    assert строка["status"] == "ready"


async def test_без_кабинета_обращение_не_закрывается(panel_client, env):
    """Документ готов, а сказать, где его забрать, нечем - молчать нельзя."""
    assert login_panel(panel_client)
    тикет = await ticket(category="session", topic="Когда сдавать зачётку")
    post_form(panel_client, f"/panel/tickets/{тикет}/ready", {})
    строка = await repo.get_ticket(тикет)
    assert строка["status"] != "ready", "обращение закрылось без кабинета"
    assert строка["pickup_place"] == ""


async def test_указанный_кабинет_уважается(panel_client, env):
    assert login_panel(panel_client)
    тикет = await ticket(category="session", topic="Когда сдавать зачётку",
                         pickup_place="каб. 204")
    post_form(panel_client, f"/panel/tickets/{тикет}/ready", {})
    строка = await repo.get_ticket(тикет)
    assert строка["pickup_place"] == "каб. 204"
    assert строка["status"] == "ready"


async def test_115_не_перетирает_указанный_кабинет(panel_client, env):
    assert login_panel(panel_client)
    тикет = await ticket(category="certificates", topic="Справка", pickup_place="каб. 7")
    post_form(panel_client, f"/panel/tickets/{тикет}/ready", {})
    строка = await repo.get_ticket(тикет)
    assert строка["pickup_place"] == "каб. 7"


# ── интерфейс ─────────────────────────────────────────────────────────────────
async def test_в_карточке_подсказка_разная(panel_client, env):
    # сравниваем сам текст подсказки: «115» встречается ещё и в градиентах CSS
    assert login_panel(panel_client)
    справка = await ticket(category="certificates", topic="Справка", text="Нужна справка")
    assert wp.pickup_hint(_row(category="certificates")) in panel_client.get(
        f"/panel/tickets?t={справка}").text
    зачёт = await ticket(category="session", topic="Когда сдавать зачётку")
    body = panel_client.get(f"/panel/tickets?t={зачёт}").text
    assert "кабинет ответственного сотрудника" in body
    assert "справка - 115" not in body


async def test_в_форме_есть_список_кабинетов(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets/new").text
    assert "pickup-rooms" in body
    assert 'value="каб. 204"' in body
