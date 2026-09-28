"""Шаблоны ответов с подстановками, «Ответить и закрыть» и личные шаблоны.

Проверяем три вещи: в шаблон подставляются данные обращения вместо «{ФИО}» в
письме студенту; кнопка «Ответить и закрыть» отвечает и закрывает дело за два
нажатия; личные шаблоны сотрудника идут первыми и помечены «мои».
"""
from datetime import datetime

import max_api
import pytest

import college
import database as db
import repository as repo
from conftest import add_staff, press, register, say
from handlers import common, tickets

STUDENT, STAFF, OTHER = "100", "200", "201"
CATEGORY = "certificates"


class FrozenClock:
    """Подменяем «сейчас», чтобы дата и время в подстановках были известны."""

    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """28 сентября 2026, 14:32 - как в бумажном письме."""
    monkeypatch.setattr(tickets, "clock", FrozenClock(datetime(2026, 9, 28, 14, 32)))


async def make_ticket(topic: str = "Место обучения", category: str = CATEGORY,
                      name: str = "Иванов Иван Иванович", group: str = "ИС-21",
                      office: str = "215", position: str = "Методист",
                      staff_id: str = STAFF) -> int:
    """Обращение от студента к сотруднику: как его создаёт живой человек."""
    await register(STUDENT, name, group)
    await add_staff(staff_id, "Петрова Анна Сергеевна", category="all",
                    position=position, office=office)
    return await repo.create_ticket(STUDENT, staff_id, category, "Нужна справка", topic=topic)


def lines_of(api, user: str) -> list:
    return api.last(user)[1].splitlines()


# ── подстановки: каждое имя ─────────────────────────────────────────────────
async def test_every_placeholder_is_filled():
    tid = await make_ticket()
    text = await tickets.render_template(
        "Уважаемый(ая) {ФИО}!\n"
        "Ваша группа: {группа}. Обращение №{номер}, тема: {тема}.\n"
        "С вами работает {преподаватель}, {должность}, кабинет {кабинет}.\n"
        "Забрать можно в {кабинет_выдачи}.\n"
        "Сегодня {дата} в {время}. Учебная часть: {учебная_часть}, "
        "колледж: {колледж}, директор: {директор}.\n"
        "{фамилия} {имя} {отчество}",
        await repo.get_ticket(tid))
    assert "Уважаемый(ая) Иванов Иван Иванович!" in text
    assert "Ваша группа: ИС-21." in text
    assert "Обращение №1, тема: Место обучения." in text
    assert "С вами работает Петрова Анна Сергеевна, Методист, кабинет 215." in text
    assert "Забрать можно в 215." in text
    assert "Сегодня 28 сентября 2026 в 14:32." in text
    assert "Учебная часть: +7 (34669) 2-71-33" in text
    assert "колледж: +7 (34669) 2-71-33" in text
    assert "директор: +7 (34669) 2-26-18" in text
    assert text.endswith("Иванов Иван Иванович")
    assert "{" not in text        # ни одной неподставленной скобки


async def test_pickup_place_of_the_ticket_wins():
    tid = await make_ticket()
    await repo.update_ticket(tid, STAFF, pickup_place="203")
    text = await tickets.render_template(
        "{фамилия}|{имя}|{отчество}|{кабинет_выдачи}", await repo.get_ticket(tid))
    assert text == "Иванов|Иван|Иванович|203"


async def test_patronymic_is_empty_when_absent():
    tid = await make_ticket(name="Иванов Иван")
    text = await tickets.render_template("{ФИО}|{отчество}|", await repo.get_ticket(tid))
    assert text == "Иванов Иван||"       # отчество пустое, а не «None»


async def test_missing_data_gives_words_not_holes():
    """Нет ФИО, кабинета и должности - письмо остаётся связным, без пустот."""
    tid = await make_ticket(office="-", position="-")
    await db.run("UPDATE users SET full_name='' WHERE user_id=?", (STUDENT,))
    text = await tickets.render_template(
        "Здравствуйте, {ФИО}! Группа {группа}. Кабинет {кабинет}. "
        "Должность {должность}. Работает с вами {преподаватель}.",
        await repo.get_ticket(tid))
    assert text == ("Здравствуйте, студент! Группа ИС-21. Кабинет не указан. "
                    "Должность не назначена. Работает с вами Петрова Анна Сергеевна.")


async def test_missing_group_and_place_are_named():
    tid = await make_ticket(office="-")
    await db.run("UPDATE users SET group_code='' WHERE user_id=?", (STUDENT,))
    text = await tickets.render_template(
        "{группа}|{кабинет_выдачи}", await repo.get_ticket(tid))
    assert text == "не указана|кабинет не указан"


async def test_placeholder_case_does_not_matter():
    tid = await make_ticket()
    text = await tickets.render_template("{ФИО} {фио} {Фио} {фИО}", await repo.get_ticket(tid))
    assert text == "Иванов Иван Иванович " * 3 + "Иванов Иван Иванович"


async def test_unknown_placeholder_stays_and_is_reported(api, clear_templates):
    """Опечатка в шаблоне не ломает ответ, но сотруднику список имён показан."""
    tid = await make_ticket()
    await repo.add_template("С опечаткой", "Здравствуйте, {ФИО}, ваш {студент}.",
                            CATEGORY, "1")
    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]
    await press(STAFF, f"t:{tid}")
    await press(STAFF, f"tpl:{tid}")
    api.sent.clear()
    await press(STAFF, f"tplu:{template_id}:{tid}")
    text = api.last(STAFF)[1]
    assert "Здравствуйте, Иванов Иван Иванович, ваш {студент}." in text
    assert "Непонятно: {студент}" in text          # какое именно имя не понято
    assert "{ФИО}" in text and "{кабинет_выдачи}" in text   # и какие понятны
    assert tickets.unknown_placeholders("{ФИО} {студент}") == ["студент"]
    assert tickets.unknown_placeholders("{ФИО} {группа}") == []
    assert tickets.unknown_placeholders("") == []


async def test_unknown_placeholder_never_breaks_sending(api, clear_templates):
    tid = await make_ticket()
    await repo.add_template("С опечаткой", "Здравствуйте, {ФИО}, {студент}!", CATEGORY, "1")
    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]
    await press(STAFF, f"tplu:{template_id}:{tid}")
    await press(STAFF, f"tplsend:{tid}")
    assert (await repo.get_ticket(tid))["status"] == "accepted"
    assert "Здравствуйте, Иванов Иван Иванович" in api.last(STUDENT)[1]


# ── кнопка «Ответить и закрыть» ─────────────────────────────────────────────
async def test_close_button_is_in_the_ticket_card(api):
    tid = await make_ticket()
    await press(STAFF, f"t:{tid}")
    assert f"tplclose:{tid}" in api.payloads(STAFF)
    labels = [b["text"] for row in api.last(STAFF)[2] for b in row]
    # 16 ячеек, чтобы влезало в пару с «💬 Ответить»
    assert "✅ Ответ и закрыть" in labels


async def test_close_button_is_not_shown_for_closed_ticket(api):
    tid = await make_ticket()
    await repo.set_ticket_status(tid, "completed", actor_id=STAFF)
    await press(STAFF, f"t:{tid}")
    assert f"tplclose:{tid}" not in api.payloads(STAFF)


async def test_close_screen_offers_templates_and_own_text(api):
    tid = await make_ticket()
    template_id = await repo.add_template("Справка готова", "Заберите в {кабинет_выдачи}.",
                                          CATEGORY, "1")
    api.sent.clear()
    await press(STAFF, f"tplclose:{tid}")
    payloads = api.payloads(STAFF)
    assert f"tplcx:{tid}:{template_id}" in payloads
    assert f"tplct:{tid}" in payloads
    text = api.last(STAFF)[1]
    assert "Справка готова" in text and "✅ Завершено" in text


async def test_reply_and_close_sends_answer_and_closes(api, clear_templates):
    tid = await make_ticket()
    template_id = await repo.add_template(
        "Справка готова", "Здравствуйте, {ФИО}! Справка ждёт в {кабинет_выдачи}.",
        CATEGORY, "1")
    await press(STAFF, f"tplclose:{tid}")
    await press(STAFF, f"tplcx:{tid}:{template_id}")
    assert (await repo.get_ticket(tid))["status"] == "completed"
    answer = [row["text"] for row in await repo.ticket_thread(tid, 10)
              if row["sender_role"] == "staff"]
    assert answer == ["Здравствуйте, Иванов Иван Иванович! Справка ждёт в 215."]
    assert (await db.one("SELECT used_count FROM reply_templates"))["used_count"] == 1
    assert any("закрыто" in body for _to, body, _kb in api.to(STAFF))


async def test_student_gets_answer_and_closing_note_in_one_message(api, clear_templates):
    tid = await make_ticket()
    await repo.add_template("Справка готова", "Справка готова, {ФИО}!", CATEGORY, "1")
    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]
    await press(STAFF, f"tplclose:{tid}")
    api.sent.clear()
    await press(STAFF, f"tplcx:{tid}:{template_id}")
    notices = [body for to, body, _kb in api.sent if to == STUDENT]
    assert len(notices) == 1                       # одним сообщением
    assert "Справка готова, Иванов Иван Иванович!" in notices[0]   # текст ответа
    assert "закрыто" in notices[0]                                # и факт закрытия
    assert f"t:{tid}" in [b["payload"] for row in api.last(STUDENT)[2] for b in row]


async def test_second_press_does_not_close_twice(api):
    """Старый экран в телефоне остался - второй раз закрывать нельзя."""
    tid = await make_ticket()
    template_id = await repo.add_template("Справка готова", "Готово, {ФИО}!", CATEGORY, "1")
    await press(STAFF, f"tplclose:{tid}")
    await press(STAFF, f"tplcx:{tid}:{template_id}")
    api.sent.clear()
    await press(STAFF, f"tplcx:{tid}:{template_id}")
    assert (await repo.get_ticket(tid))["status"] == "completed"
    staff_messages = [row for row in await repo.ticket_thread(tid, 10)
                      if row["sender_role"] == "staff"]
    assert len(staff_messages) == 1
    assert not [body for _to, body, _kb in api.sent
                if _to == STUDENT and "Готово, Иванов" in body]
    assert "уже закрыто" in api.last(STAFF)[1].lower()


async def test_own_text_closes_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"tplclose:{tid}")
    await press(STAFF, f"tplct:{tid}")
    assert (await db.get_state(STAFF))["state"] == "tpl_close"
    await say(STAFF, "Вопрос решён, спасибо за обращение.")
    assert (await repo.get_ticket(tid))["status"] == "completed"
    assert "Вопрос решён" in api.last(STUDENT)[1]


async def test_student_cannot_reply_and_close(api):
    tid = await make_ticket()
    await press(STUDENT, f"tplclose:{tid}")
    assert (await repo.get_ticket(tid))["status"] == "new"
    assert not [p for p in api.payloads(STUDENT) if p.startswith("tplcx")]


async def test_close_screen_says_nothing_to_do_when_closed(api):
    tid = await make_ticket()
    await repo.set_ticket_status(tid, "completed", actor_id=STAFF)
    await press(STAFF, f"tplclose:{tid}")
    assert "уже закрыто" in api.last(STAFF)[1].lower()
    assert not [p for p in api.payloads(STAFF) if p.startswith("tplcx")]


# ── личные шаблоны ──────────────────────────────────────────────────────────
async def test_personal_template_is_shown_first_and_marked(api):
    tid = await make_ticket()
    shared = await repo.add_template("Общий ответ", "Текст без подстановок", CATEGORY, "1")
    own = await repo.add_template("Мой ответ", "Здравствуйте, {ФИО}!", CATEGORY, "1")
    assert not await common.is_personal_template(STAFF, own)
    await common.set_personal_template(STAFF, own)
    await press(STAFF, f"tplclose:{tid}")
    template_payloads = [p for p in api.payloads(STAFF) if p.startswith("tplcx:")]
    assert template_payloads[0] == f"tplcx:{tid}:{own}"      # личный - первым
    assert f"tplcx:{tid}:{shared}" in template_payloads
    assert "· Мой ответ  — мои" in lines_of(api, STAFF)
    assert "· Общий ответ" in lines_of(api, STAFF)


async def test_personal_flag_is_per_staff(api):
    tid = await make_ticket(staff_id=STAFF)
    other_ticket = await repo.create_ticket(STUDENT, OTHER, CATEGORY, "Второй вопрос")
    await add_staff(OTHER, "Сидоров Пётр", category="all")
    own = await repo.add_template("Мой ответ", "Текст", CATEGORY, "1")
    await common.set_personal_template(STAFF, own)
    assert await common.personal_template_ids(STAFF) == {own}
    assert await common.personal_template_ids(OTHER) == set()
    await press(STAFF, f"tplclose:{tid}")
    assert "· Мой ответ  — мои" in lines_of(api, STAFF)
    await press(OTHER, f"tplclose:{other_ticket}")
    assert "— мои" not in api.last(OTHER)[1]


async def test_staff_marks_template_personal_from_templates_screen(api):
    tid = await make_ticket()
    own = await repo.add_template("Справка готова", "Текст", CATEGORY, "1")
    await press(STAFF, f"tpl:{tid}")
    await press(STAFF, f"tplu:{own}:{tid}")
    assert f"tplmy:{tid}:{own}:1" in api.payloads(STAFF)
    api.sent.clear()
    await press(STAFF, f"tplmy:{tid}:{own}:1")
    assert await common.is_personal_template(STAFF, own)
    assert "личный" in api.last(STAFF)[1]
    # и снятие пометки возвращает шаблон в общие
    await press(STAFF, f"tpl:{tid}")
    await press(STAFF, f"tplu:{own}:{tid}")
    await press(STAFF, f"tplmy:{tid}:{own}:0")
    assert not await common.is_personal_template(STAFF, own)
    assert "не личный" in api.last(STAFF)[1]


# ── пределы MAX ─────────────────────────────────────────────────────────────
def assert_keyboard_ok(keyboard: list, where: str) -> None:
    """Ни одна подпись не обрезана, рядов не больше, чем MAX принимает."""
    rows = max_api.fit_keyboard(keyboard or [])
    assert len(rows) <= max_api.MAX_ROWS, f"{where}: {len(rows)} рядов"
    for row in rows:
        limit = max_api.row_limit(len(row))
        for button in row:
            assert len(button["text"]) <= limit, f"{where}: «{button['text']}»"
            assert not button["text"].endswith("…"), f"{where}: обрезано «{button['text']}»"


async def test_card_and_close_screen_fit_max(api):
    tid = await make_ticket()
    for index in range(12):
        await repo.add_template(f"Шаблон с очень длинным названием номер {index}",
                                "Здравствуйте, {ФИО}!", CATEGORY, "1")
    api.sent.clear()
    await press(STAFF, f"t:{tid}")
    assert_keyboard_ok(api.last(STAFF)[2], "карточка")
    api.sent.clear()
    await press(STAFF, f"tplclose:{tid}")
    assert_keyboard_ok(api.last(STAFF)[2], "ответ и закрыть")
    api.sent.clear()
    await press(STAFF, f"tpl:{tid}")
    assert_keyboard_ok(api.last(STAFF)[2], "шаблоны")


async def test_phones_come_from_college_reference():
    """Телефоны берём из справочника, а не пишем в шаблоне: их правят в панели."""
    assert await college.get("телефон_учебная_часть") == "+7 (34669) 2-71-33"
    assert await college.get("телефон_директор") == "+7 (34669) 2-26-18"
    values = await tickets.template_values({
        "ticket_id": 1, "student_id": STUDENT, "target_admin_id": STAFF,
        "category": CATEGORY, "topic": "", "pickup_place": "",
        "student": {"full_name": "Петров Пётр", "group_code": "БУ-11"},
        "staff": {"full_name": "Иванова Ирина", "position": "", "role": "director",
                  "office": "115"},
    })
    assert values["должность"] == "👔 Директор"      # должность по коду роли
    assert values["кабинет"] == "115"
    assert values["учебная_часть"] == await college.get("телефон_учебная_часть")
    assert values["колледж"] == values["учебная_часть"]
    assert "Справка" in values["тема"]                # тема пуста - берём раздел
