"""Кабинет 115 в боте: зашит только под справки, остальное выбирает сотрудник.

Проверяем поведение, а не наличие строки в коде: справка закрывается с 115,
не-справка без кабинета остаётся открытой и спрашивает кабинет, указанный
кабинет уважается всегда - и ни одна новая подпись не обрезается в MAX.
"""
import max_api
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register, say
from handlers import tickets

STUDENT = "300"
STAFF = "200"
OTHER = "201"
CERT_TEXT = "Нужна справка для поступления"
PLAIN_TEXT = "Когда сдавать зачёт по практике"


async def make_ticket(cat="feedback", text=PLAIN_TEXT, office="каб. 204", staff=STAFF) -> int:
    """Обращение так, как его пишет студент: раздел, сотрудник, текст, отправка."""
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await add_staff(staff, "Петрова Анна", cat)
    if office:
        await repo.set_admin_profile(staff, office=office)
    await press(STUDENT, f"new:{cat}")
    await press(STUDENT, f"pick:{cat}:{staff}")
    await say(STUDENT, text)
    await press(STUDENT, "ticketsend")
    return (await db.one("SELECT MAX(ticket_id) n FROM tickets"))["n"]


async def place_of(ticket_id: int) -> str:
    row = await db.one("SELECT status, pickup_place FROM tickets WHERE ticket_id=?", (ticket_id,))
    return f"{row['status']}/{row['pickup_place']}"


def labels(api, user) -> list[str]:
    return [b["text"] for row in (api.last(user)[2] or []) for b in row]


def assert_fits(api, user, where: str = "") -> None:
    """Ни одна подпись экрана не обрезается: ширина в ячейках против предела ряда."""
    for row in (api.last(user)[2] or []):
        limit = max_api.row_limit(len(row))
        for button in row:
            text = button["text"]
            assert max_api.display_width(text) <= limit, f"{where}: длинная подпись «{text}»"
            assert not text.endswith("…"), f"{where}: обрезанная подпись «{text}»"


# ── где живёт 115 ────────────────────────────────────────────────────────────
def test_hardcoded_room_is_only_the_certificate_one():
    """Зашит один кабинет, и он назван прямо: 115 - это справки.

    Прежнего «кабинета по умолчанию» больше нет: он подставлялся во все
    обращения подряд, и «готово» уходило студенту с чужим адресом.
    """
    assert tickets.CERT_PICKUP == "115"
    assert not hasattr(tickets, "PICKUP_PLACE")


@pytest.mark.parametrize("ticket,expected", [
    ({"category": "certificates", "topic": "", "text_content": ""}, True),
    ({"category": "documents", "topic": "", "text_content": ""}, True),
    ({"category": "справка", "topic": "", "text_content": ""}, True),
    ({"category": "feedback", "topic": "Справка для военкомата", "text_content": ""}, True),
    ({"category": "feedback", "topic": "", "text_content": "прошу выдать справку"}, True),
    ({"category": "feedback", "topic": "", "text_content": "Нужны справки"}, True),
    ({"category": "accounting", "topic": "Стипендия", "text_content": "Когда выплатят"}, False),
    ({"category": "feedback", "topic": "Когда сдавать зачётку", "text_content": "не сдал"}, False),
    ({"category": "academic", "topic": "", "text_content": "Снялся с курса"}, False),
    ({"ticket_id": 1}, False),          # обращение без раздела и текста
    (None, False),
])
def test_certificate_is_guessed_by_category_topic_and_text(ticket, expected):
    assert tickets.is_certificate(ticket) is expected


def test_pickup_hint_names_the_room_only_for_certificates():
    assert "115" in tickets.pickup_hint({"category": "certificates"})
    assert "115" not in tickets.pickup_hint({"category": "accounting"})
    assert tickets.pickup_hint({"category": "accounting"}) == "кабинет ответственного сотрудника"


# ── «Готово» под справку ─────────────────────────────────────────────────────
async def test_certificate_is_ready_in_room_115(api):
    tid = await make_ticket("certificates", CERT_TEXT)
    await press(STAFF, f"tdready:{tid}")
    assert await place_of(tid) == "ready/115"
    assert "кабинете 115" in "\n".join(text for _, text, _ in api.to(STUDENT))


async def test_certificate_in_another_category_still_gets_115(api):
    """Справку пишут и в разделе «Обратная связь» - раздел не спасение."""
    tid = await make_ticket("feedback", CERT_TEXT)
    await press(STAFF, f"tdready:{tid}")
    assert await place_of(tid) == "ready/115"


async def test_given_room_wins_even_for_certificate(api):
    """Указанный кабинет уважается всегда: исключение правится в панели."""
    tid = await make_ticket("certificates", CERT_TEXT)
    await repo.update_ticket(tid, STAFF, pickup_place="203")
    await press(STAFF, f"tdready:{tid}")
    assert await place_of(tid) == "ready/203"
    assert "кабинете 203" in "\n".join(text for _, text, _ in api.to(STUDENT))


# ── «Готово» без кабинета: не молчим, а спрашиваем ──────────────────────────
async def test_plain_ticket_is_not_closed_without_room(api):
    """Зачётку не закрываем и не отправляем 115: сначала спросим кабинет."""
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STAFF, f"tdready:{tid}")
    assert await place_of(tid) == "new/"                    # дела осталось в работе
    assert not [m for m in api.to(STUDENT) if "кабинете" in m[1]]
    answer = api.last(STAFF)[1]
    assert tickets.PICKUP_ASK in answer
    assert "115" in answer and "справки" in answer


async def test_room_choices_are_offered_after_ready(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await add_staff(OTHER, "Козлов Иван", "feedback", office="каб. 208")
    await press(STAFF, f"tdready:{tid}")
    payloads = api.payloads(STAFF)
    assert f"tdplace:{tid}:115" in payloads                  # 115 есть в списке
    assert f"tdplace:{tid}:каб. 204" in payloads             # кабинеты из карточек
    assert f"tdplace:{tid}:каб. 208" in payloads
    assert f"tdplacex:{tid}" in payloads                     # и «ввести свой»
    assert "-" not in [b["text"] for b in api.last(STAFF)[2][0]]   # прочерк - это «кабинета нет»
    assert_fits(api, STAFF, "выбор кабинета")


async def test_room_list_has_no_empty_and_no_dash(api):
    await make_ticket("feedback", PLAIN_TEXT, office="")
    await add_staff(OTHER, "Козлов Иван", "feedback")          # кабинета не заведено
    rooms = await tickets.pickup_rooms()
    assert rooms == ["115"]
    assert all(room.strip() and room != "-" for room in rooms)


async def test_chosen_room_closes_and_notifies(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STAFF, f"tdready:{tid}")
    api.sent.clear()
    await press(STAFF, f"tdplace:{tid}:каб. 208")
    assert await place_of(tid) == "ready/каб. 208"
    notice = "\n".join(text for _, text, _ in api.to(STUDENT))
    assert "кабинете каб. 208" in notice
    assert "Заберите" in api.last(STAFF)[1]                   # и сотруднику в карточку


async def test_own_room_closes_and_notifies(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STAFF, f"tdready:{tid}")
    await press(STAFF, f"tdplacex:{tid}")
    assert (await db.get_state(STAFF))["state"] == "ticket_place"
    await say(STAFF, "  118  ")
    assert await place_of(tid) == "ready/118"
    assert "кабинете 118" in "\n".join(text for _, text, _ in api.to(STUDENT))


async def test_crafted_empty_room_keeps_ticket_open(api):
    """Пустой кабинет в подделанной кнопке - тоже не повод закрыть обращение."""
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STAFF, f"tdplace:{tid}:")
    assert await place_of(tid) == "new/"
    assert "не указан" in api.last(STAFF)[1]


# ── карточка: подпись про «готово» и подсказка про кабинет ────────────────────
async def test_card_shows_room_in_ready_button(api):
    tid = await make_ticket("certificates", CERT_TEXT)
    await press(STAFF, f"t:{tid}")
    assert "✅ Готово · 115" in labels(api, STAFF)
    assert "справка — 115" in api.last(STAFF)[1]
    assert_fits(api, STAFF, "карточка справки")


async def test_card_without_room_shows_no_number(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STAFF, f"t:{tid}")
    assert "✅ Готово" in labels(api, STAFF)
    assert "✅ Готово · 115" not in labels(api, STAFF)
    assert "кабинет ответственного сотрудника" in api.last(STAFF)[1]
    assert "115" not in api.last(STAFF)[1].split("Кабинет выдачи:")[1]
    assert_fits(api, STAFF, "карточка заявки")


async def test_ready_ticket_with_own_room_has_no_ready_button(api):
    """Готовое дело в своём кабинете больше не предлагает «Готово · 115»."""
    tid = await make_ticket("academic", PLAIN_TEXT, office="каб. 204")
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:today")
    assert await place_of(tid) == "ready/каб. 204"
    await press(STAFF, f"t:{tid}")
    assert not [p for p in api.payloads(STAFF) if p.startswith("tdready:")]
    assert_fits(api, STAFF, "готовое дело")


async def test_student_card_has_no_ready_button(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STUDENT, f"t:{tid}")
    assert not [p for p in api.payloads(STUDENT) if p.startswith(("tdready:", "tdplace"))]
    assert "115" not in api.last(STUDENT)[1]        # подсказка сотруднику студенту не нужна


# ── чужим «Готово» нажимать нельзя ───────────────────────────────────────────
async def test_foreign_staff_cannot_mark_ready(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await add_staff(OTHER, "Козлов Иван", "feedback")
    await press(OTHER, f"tdready:{tid}")
    assert await place_of(tid) == "new/"
    await press(OTHER, f"tdplace:{tid}:каб. 208")
    assert await place_of(tid) == "new/"


async def test_student_cannot_mark_ready(api):
    tid = await make_ticket("feedback", PLAIN_TEXT)
    await press(STUDENT, f"tdready:{tid}")
    assert await place_of(tid) == "new/"
