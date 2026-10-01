"""Тесты нового меню: подменю студента, вопросы, адресная обратная связь, очередь."""
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register, say
from handlers import menus

STUDENT, STAFF, DIRECTOR = "300", "200", "202"


@pytest.fixture
async def student(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    return env


async def set_role(user_id: str, role: str, name: str = "Должность") -> None:
    """Назначить должность: сотрудник должен существовать в справочнике."""
    await add_staff(user_id, name, category="all", position=name)
    await repo.set_admin_profile(user_id, role=role, position=name, office="101")


# ── меню: шесть кнопок вместо россыпи ─────────────────────────────────────────
def test_student_menu_is_short_and_starts_with_tickets():
    """Четыре кнопки в трёх рядах, первая - обращение.

    Владелец назвал главный экран разбросом: справка, бухгалтерия, вопросы и
    обратная связь занимали место рядом с главным делом. Теперь на первом
    экране только обращение, расписание, профиль и «Ещё», а второстепенные
    разделы ушли за «Ещё».

    Порядок проверяется не косметически: обращение должно быть первым, иначе
    ради него человек всё равно пришлось бы искать глазами.
    """
    rows = menus.student_menu()
    assert len(rows) == 3, f"ожидалось 3 ряда, получилось {len(rows)}"
    payloads = [b["payload"] for row in rows for b in row]
    assert payloads == ["tickets", "sched", "profile", "student_more"], payloads
    assert payloads[0] == "tickets", "обращение должно быть первым на экране"


def test_secondary_sections_are_not_on_the_first_screen():
    """Справка, бухгалтерия, вопросы и обратная связь не висят на первом экране.

    Именно это и было причиной правки: они и занимали место. Проверка отдельно
    от структуры, чтобы перестановка кнопок не проскочила мимо неё.
    """
    payloads = {b["payload"] for row in menus.student_menu() for b in row}
    второстепенные = {"sub:cert", "sub:acc", "sub:fb", "faq"}
    assert второстепенные & payloads == set(), \
        f"второстепенные разделы вернулись на главный экран: {второстепенные & payloads}"


def test_student_menu_fits_keyboard_limits():
    import max_api

    assert len(menus.student_menu()) <= max_api.MAX_ROWS
    assert all(len(row) <= 7 for row in menus.student_menu())


# ── справки ──────────────────────────────────────────────────────────────────
def labels(api, user) -> list:
    """Подписи всех кнопок последнего сообщения."""
    return [b["text"] for row in (api.to(user)[-1][2] or []) for b in row]


async def test_certificates_submenu_lists_three_kinds(api, student):
    await press(STUDENT, "sub:cert")
    payloads = api.payloads(STUDENT)
    assert "ask:certificates:place" in payloads
    assert "ask:certificates:period" in payloads
    assert "ask:certificates:vacancies" in payloads
    text = " ".join(labels(api, STUDENT)).lower()
    assert "место обучения" in text
    assert "период обучения" in text
    assert "вакантные места" in text


async def test_accounting_submenu_mentions_scholarship(api, student):
    await press(STUDENT, "sub:acc")
    assert "ask:accounting:scholarship" in api.payloads(STUDENT)
    assert "Стипендия" in " ".join(labels(api, STUDENT))


async def test_choosing_a_kind_asks_who_to_write(api, student):
    await press(STUDENT, "ask:certificates:place")
    assert f"pick:certificates:{STAFF}:place" in api.payloads(STUDENT)
    state = await db.get_state(STUDENT)
    assert state and state["payload"]["topic"] == "📍 Место обучения"
    # должность сотрудника видна текстом, а не обрезанной кнопкой
    assert "Кто принимает" in api.last(STUDENT)[1]


async def test_topic_reaches_the_ticket(api, student):
    await press(STUDENT, "ask:certificates:vacancies")
    await press(STUDENT, f"pick:certificates:{STAFF}:vacancies")
    await say(STUDENT, "Нужна для поступления")
    await press(STUDENT, "ticketsend")
    row = await db.one("SELECT * FROM tickets")
    assert row["category"] == "certificates"
    assert "Вакантные места" in row["topic"]


# ── обратная связь: адресная ─────────────────────────────────────────────────
async def test_feedback_menu_lists_officials_by_position(api, student):
    await set_role(DIRECTOR, "director", "Сидоров Пётр Петрович")
    await press(STUDENT, "sub:fb")
    # кнопка - должность, фамилия видна текстом: так подпись не обрезается
    assert "fbrole:director" in api.payloads(STUDENT)
    text = " ".join(labels(api, STUDENT))
    assert "Директор" in text
    assert "Сидоров" in api.last(STUDENT)[1]
    await press(STUDENT, "fbrole:director")
    assert f"pick:feedback:{DIRECTOR}" in api.payloads(STUDENT)


async def test_feedback_lists_all_four_positions(api, student):
    for index, role in enumerate(("director", "deputy_uvr", "deputy_upr", "deputy_unr")):
        await set_role(f"20{index}", role, f"Зам {index}")
    await press(STUDENT, "sub:fb")
    text = " ".join(labels(api, STUDENT))
    for label in ("Директор", "Зам УРП", "Зам УПР", "Зам УМР"):
        assert label in text


async def test_feedback_without_officials_says_so(api, student):
    await press(STUDENT, "sub:fb")
    assert "не назначены" in api.to(STUDENT)[-1][1]   # честно говорим, а не молчим
    assert "new:feedback" in api.payloads(STUDENT)


async def test_writing_to_director_creates_ticket_for_him(api, student):
    await set_role(DIRECTOR, "director", "Сидоров Пётр Петрович")
    await press(STUDENT, "sub:fb")
    await press(STUDENT, f"pick:feedback:{DIRECTOR}")
    await say(STUDENT, "Прошу разобраться")
    await press(STUDENT, "ticketsend")
    row = await db.one("SELECT * FROM tickets")
    assert row["target_admin_id"] == DIRECTOR
    assert row["category"] == "feedback"
    assert any("Прошу разобраться" in body for uid, body, _ in api.to(DIRECTOR) if uid == DIRECTOR)


# ── очередь сотрудника: четыре входа вместо восьми фильтров ─────────────────
@pytest.fixture
async def staff_with_queue(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", category="all", position="Секретарь")
    for text, staff in (("Первый вопрос", STAFF), ("Второй вопрос", STAFF),
                        ("Третий вопрос", STAFF)):
        await press(STUDENT, "new:feedback")
        await press(STUDENT, f"pick:feedback:{staff}")
        await say(STUDENT, text)
        await press(STUDENT, "ticketsend")
    return env


async def test_queue_has_four_views(api, staff_with_queue):
    await press(STAFF, "staff")
    payloads = api.payloads(STAFF)
    for view in ("staffv:waiting", "staffv:in_progress", "staffv:ready", "staffv:"):
        assert view in payloads
    assert "staffcat" in payloads


async def test_queue_keyboard_is_short(api, staff_with_queue):
    await press(STAFF, "staff")
    rows = api.to(STAFF)[-1][2]
    # четыре счётчика + переходы + сами обращения, без восьми фильтров
    filter_rows = [row for row in rows if any(p.startswith("stafff:") for p in
                                             [b["payload"] for b in row])]
    assert not filter_rows
    assert len(rows) <= 12


async def test_waiting_view_shows_new_tickets(api, staff_with_queue):
    await press(STAFF, "staffv:waiting")
    text = " ".join(labels(api, STAFF))
    assert "Ждут ответа" in text
    assert any(p.startswith("t:") for p in api.payloads(STAFF))


async def test_in_progress_view_filters(api, staff_with_queue):
    await press(STAFF, "staffv:in_progress")
    assert "В работе" in " ".join(labels(api, STAFF))


async def test_departments_moved_to_submenu(api, staff_with_queue):
    await press(STAFF, "staffcat")
    payloads = api.payloads(STAFF)
    assert "stafff:feedback" in payloads
    assert "stafff:certificates" in payloads
    assert "stafff:open" in payloads


async def test_old_filter_still_works(api, staff_with_queue):
    await press(STAFF, "stafff:feedback")
    assert "Обратная связь" in api.to(STAFF)[-1][1]
