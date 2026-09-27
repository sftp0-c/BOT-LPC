"""Профиль студента: сводка «Всё моё» и кнопка «Ошибка в боте»."""

import database as db
import max_api
import repository as repo
from conftest import add_staff, press, register, say
from handlers import menus

SYS, STAFF, STUDENT = "1", "200", "100"


def texts_of(api, uid: str) -> str:
    """Всё, что бот написал человеку, одной строкой — удобно искать подписи."""
    return "\n".join(body for user, body, _ in api.to(uid))


async def bug_reports() -> list:
    """Записи об ошибках в боте из admin_log (в журнале есть и другие действия)."""
    return [row for row in await repo.admin_log(50) if row["action"] == "ошибка в боте"]


async def make_ticket(status: str = "new", staff_answered: bool = False) -> int:
    """Обращение студента; staff_answered — последнее слово за сотрудником."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    if staff_answered:
        await repo.add_ticket_message(ticket_id, STAFF, "staff", "Справка готова")
    if status != "new":
        await repo.set_ticket_status(ticket_id, status, actor_id=STAFF)
    return ticket_id


# ── «всё моё» ─────────────────────────────────────────────────────────────────
async def test_profile_has_everything_mine_button(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "profile")
    assert "myall" in api.payloads(STUDENT)
    assert "Иванов Иван Иванович" in api.last(STUDENT)[1]


async def test_my_all_summarizes_tickets_group_and_data(api):
    """Сводка отвечает на всё, ради чего человек лезет в профиль: обращения, группа, данные."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    first = await make_ticket()                                            # новое, ждёт меня
    working = await make_ticket("in_progress", staff_answered=True)       # в работе, ждёт сотрудника
    done = await make_ticket("completed", staff_answered=True)             # закрытое
    api.sent.clear()

    await press(STUDENT, "myall")
    text = api.last(STUDENT)[1]
    assert "всего 3" in text and "в работе 2" in text and "ждут ответа 1" in text
    assert f"№{first}" in text and f"№{working}" in text and f"№{done}" in text
    assert "ИС-21" in text
    assert "Иванов Иван Иванович" in text
    assert f"ID: {STUDENT}" in text
    assert "В боте с:" in text
    assert "подписок нет" in text


async def test_my_all_opens_last_five_tickets_by_number(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    tickets = [await make_ticket() for _ in range(7)]
    api.sent.clear()

    await press(STUDENT, "myall")
    payloads = api.payloads(STUDENT)
    # кнопками открываются только последние пять (свежие сверху) - иначе экран не влезет в MAX
    assert [p for p in payloads if p.startswith("t:")] == [f"t:{tid}" for tid in reversed(tickets[-5:])]
    assert {"home", "tickets", "sched", "profile"} <= set(payloads)


async def test_my_all_reports_schedule_subscription(api):
    await register(STUDENT)
    await repo.set_schedule_subscription(STUDENT, "ИС-21")
    api.sent.clear()
    await press(STUDENT, "myall")
    assert "подписка на обновления группы ИС-21" in api.last(STUDENT)[1]


async def test_my_all_survives_repository_without_subscriptions(api, monkeypatch):
    """Сводка не должна падать, если в репозитории нет подписок (старые сборки)."""
    await register(STUDENT)
    monkeypatch.delattr(repo, "is_schedule_subscribed")
    api.sent.clear()
    await press(STUDENT, "myall")
    assert "подписок нет" in api.last(STUDENT)[1]


async def test_my_all_keyboard_fits_max_rows(api):
    """Предел MAX - 30 строк; сводка и меню студента в него укладываются."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    for _ in range(40):
        await make_ticket()
    assert len(menus.student_menu()) <= max_api.MAX_ROWS

    await press(STUDENT, "myall")
    keyboard = api.last(STUDENT)[2] or []
    assert 0 < len(keyboard) <= max_api.MAX_ROWS
    assert api.payloads(STUDENT)[-1] == "profile"   # нижние кнопки не срезало


# ── «ошибка в боте» ───────────────────────────────────────────────────────────
async def test_bug_report_button_is_in_student_menu(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "home")
    assert "bugreport" in api.payloads(STUDENT)
    assert any("Ошибка в боте" in button["text"]
               for row in api.last(STUDENT)[2] for button in row)


async def test_bug_report_asks_and_can_be_cancelled(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "bugreport")

    assert "Опишите одним сообщением" in api.last(STUDENT)[1]
    assert api.payloads(STUDENT) == ["home"]        # только «Отмена»
    assert (await db.get_state(STUDENT))["state"] == "bug_report"

    await press(STUDENT, "home")
    assert "bugreport" in api.payloads(STUDENT)    # вернулись в меню
    assert await db.get_state(STUDENT) is None
    assert not await bug_reports()                 # отменённая ошибка в журнал не попала

    await say(STUDENT, "привет")                   # состояние снято: текст — обычное сообщение
    assert not await bug_reports()


async def test_bug_report_writes_log_and_wakes_sysadmins(api):
    """Главное: жалоба попадает в admin_log и сис-админ получает уведомление."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "bugreport")
    await say(STUDENT, "нажал «Мой профиль», а открылось расписание")

    entry = (await repo.admin_log(5))[0]
    assert entry["action"] == "ошибка в боте"
    assert entry["details"].startswith("ошибка в боте: нажал «Мой профиль»")
    assert "Иванов Иван Иванович" in entry["details"]
    assert "ИС-21" in entry["details"] and f"ID {STUDENT}" in entry["details"]

    to_sys = texts_of(api, SYS)
    assert "нажал «Мой профиль»" in to_sys and "Иванов Иван Иванович" in to_sys
    assert api.to(SYS)[-1][2][0][0]["payload"] == "diag"   # кнопка «Журнал»


async def test_bug_report_answers_and_returns_to_menu(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "bugreport")
    await say(STUDENT, "кнопка не работает")

    student_text = texts_of(api, STUDENT)
    assert "Спасибо, сообщили" in student_text
    assert "Опишите одним сообщением" in student_text
    assert "bugreport" in api.payloads(STUDENT)    # снова в меню
    assert await db.get_state(STUDENT) is None


async def test_bug_report_asks_again_for_empty_message(api):
    """Пустое описание бесполезно сис-админу: спрашиваем ещё раз, в журнал не пишем."""
    await register(STUDENT)
    await press(STUDENT, "bugreport")
    api.sent.clear()

    await menus.st_bug_report(STUDENT, "   ", {})       # состояние спросило ввод напрямую

    assert "хотя бы парой слов" in api.last(STUDENT)[1]
    assert api.payloads(STUDENT) == ["home"]
    assert not await bug_reports()
    assert not api.to(SYS)
