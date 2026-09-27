"""Черновик обращения в боте: несколько сообщений, отмена, отправка."""
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register, say

STUDENT, STAFF = "300", "200"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    return env


async def start_draft(text="Нужна справка для военной части"):
    """Путь студента: «Справки» → выбор сотрудника → первое сообщение."""
    await press(STUDENT, "new:certificates")
    await press(STUDENT, f"pick:certificates:{STAFF}")
    await say(STUDENT, text)
    return await db.get_state(STUDENT)


async def test_first_message_does_not_create_ticket(env):
    await start_draft()
    assert await repo.recent_student_tickets(STUDENT) == []


async def test_draft_offers_send_clear_and_menu(env, api):
    await start_draft()
    payloads = api.payloads(STUDENT)
    assert "ticketsend" in payloads
    assert "draftclr" in payloads
    assert "home" in payloads
    assert "Сообщение сохранено" in api.to(STUDENT)[-1][1]


async def test_second_message_is_appended(env, api):
    await start_draft("Нужна справка")
    await say(STUDENT, "Срочно, до пятницы")
    await press(STUDENT, "ticketsend")
    rows = await repo.recent_student_tickets(STUDENT)
    assert len(rows) == 1
    assert "Нужна справка" in rows[0]["text_content"]
    assert "Срочно, до пятницы" in rows[0]["text_content"]


async def test_send_creates_one_ticket_with_combined_text(env, api):
    await start_draft("Первая часть")
    await say(STUDENT, "Вторая часть")
    await say(STUDENT, "Третья часть")
    await press(STUDENT, "ticketsend")
    rows = await repo.recent_student_tickets(STUDENT)
    assert len(rows) == 1
    text = rows[0]["text_content"]
    assert text.count("часть") == 3
    assert await db.get_state(STUDENT) is None      # состояние закрыто


async def test_clear_drops_the_draft(env, api):
    await start_draft("Черновик")
    await press(STUDENT, "draftclr")
    assert "очищен" in api.to(STUDENT)[-1][1]
    await press(STUDENT, "ticketsend")
    assert await repo.recent_student_tickets(STUDENT) == []


async def test_menu_button_cancels_draft_and_returns_home(env, api):
    """«В меню» снимает черновик и возвращает обычное меню бота."""
    await start_draft("Черновик")
    api.sent.clear()
    await press(STUDENT, "home")
    assert await db.get_state(STUDENT) is None
    assert await repo.recent_student_tickets(STUDENT) == []
    # клавиатура последнего сообщения - это меню бота, а не кнопки черновика
    payloads = api.payloads(STUDENT)
    assert {"sub:cert", "sub:acc", "sub:fb", "sched", "tickets", "profile"} <= set(payloads)
    assert "ticketsend" not in payloads and "draftclr" not in payloads


async def test_send_without_draft_is_graceful(env, api):
    await press(STUDENT, "ticketsend")
    assert "Черновик пуст" in api.to(STUDENT)[-1][1]


async def test_draft_respects_who_answers(env, api):
    """Без выбранного сотрудника обращение не создаётся: кнопка честно отказывает.

    Черновик можно набрать раньше, чем известен адресат, поэтому «Отправить»
    не должна создавать обращение — она просит начать заново.
    """
    await press(STUDENT, "new:academic")
    await press(STUDENT, "topic:academic:study")          # тема выбрана, сотрудник — нет
    await say(STUDENT, "Текст без выбора сотрудника")
    state = await db.get_state(STUDENT)
    assert state and state["state"] == "ticket"
    assert "ticketsend" in api.payloads(STUDENT)

    await press(STUDENT, "ticketsend")
    assert await repo.recent_student_tickets(STUDENT) == []
    answer = api.last(STUDENT)[1]
    assert "Черновик пуст" in answer
    assert "заново" in answer
