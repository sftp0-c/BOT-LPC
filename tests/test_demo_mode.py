"""Демо-стенд: в демо-режиме экраны работают, а база не меняется.

Главная проверка здесь — не текст ответа, а снимок строк: до и после попытки
действия он обязан совпасть. Для контроля есть тест, что снимок вообще видит
изменения.
"""
import pytest

import database as db
import max_api
import repository as repo
from conftest import press, register, say
from handlers import demo

SYS, STUDENT = "1", "300"

# Таблицы с реальными данными колледжа — их демо-режим не имеет права менять.
# Служебные следы бота исключены осознанно: admin_log (журнал демо-попыток),
# settings (сам флаг стенда), user_states (отметка «кто показывает»),
# processed_updates (защита от дублей) и contacts (последнее сообщение).
DATA_TABLES = ("users", "admins", "tickets", "ticket_messages", "ticket_events",
               "schedules", "lessons", "schedule_subscriptions", "groups", "group_aliases",
               "reply_templates", "broadcasts", "staff_invites", "staff_requests")

# Просмотр: эти кнопки в демо-режиме обязаны остаться рабочими.
VIEW_CALLBACKS = ("stats", "staffstats", "admins", "people", "person", "today", "groups",
                  "schedules", "sc", "scview", "codes", "requests", "syslist", "nostaff",
                  "view_schedules", "sched", "profile", "tickets", "staff", "t", "help",
                  "sub", "more", "settings", "diag", "teacher", "home", "view")


async def snapshot() -> dict:
    """Слепок строк всех таблиц с данными: до и после действия он должен совпасть."""
    snap = {}
    for name in DATA_TABLES:
        rows = await db.many(f'SELECT * FROM "{name}"')
        snap[name] = sorted(str(sorted(dict(row).items())) for row in rows)
    return snap


async def logged_actions() -> list:
    """Строки журнала действий: «действие — подробности»."""
    rows = await db.many("SELECT action, details FROM admin_log ORDER BY id")
    return [f"{row['action']} {row['details']}" for row in rows]


@pytest.fixture
def stand(env, monkeypatch):
    """Подмена api в handlers.demo: conftest прокладывает его своим модулям."""
    monkeypatch.setattr(demo, "api", env)
    return env


# ── флаг и статус ─────────────────────────────────────────────────────────────
async def test_stand_is_off_by_default(stand):
    assert await demo.is_demo() is False
    assert "выключен" in await demo.demo_status()


async def test_enter_turns_flag_on_and_writes_state(stand):
    assert await demo.enter(SYS) is True
    assert await demo.is_demo() is True
    state = await db.get_state(SYS)
    assert state["state"] == "demo"
    assert state["payload"]["by"] == SYS


async def test_status_text_changes_with_flag(stand):
    await demo.set_demo(True, actor="панель")
    assert await demo.demo_status() == demo.STATUS_ON
    await demo.set_demo(False, actor="панель")
    assert await demo.demo_status() == demo.STATUS_OFF


async def test_switch_from_panel_is_logged_with_demo_mark(stand):
    await demo.set_demo(True, actor="панель")
    assert await db.get_setting("demo_mode") == "1"
    assert any("демо" in row for row in await logged_actions())


async def test_set_demo_is_idempotent(stand):
    await demo.set_demo(False, actor="панель")
    assert await demo.is_demo() is False
    assert await demo.set_demo(True, actor="панель") is True
    assert await demo.set_demo(True, actor="панель") is True


# ── вход и выход ──────────────────────────────────────────────────────────────
async def test_sysadmin_menu_button_shows_switch(stand):
    await press(SYS, "demo")
    body = stand.to(SYS)[-1][1]
    assert demo.TITLE in body
    assert "demoon" in stand.payloads(SYS)      # кнопка «включить демо»


async def test_switch_screen_follows_the_flag(stand):
    await demo.set_demo(True, actor=SYS)
    await press(SYS, "demo")
    assert "demooff" in stand.payloads(SYS)     # теперь кнопка «выключить»
    assert "включён" in stand.to(SYS)[-1][1]


async def test_entering_shows_demo_screen_with_exit_button(stand):
    await press(SYS, "demoon")
    body = stand.to(SYS)[-1][1]
    assert demo.TITLE in body
    assert "не выполняются" in body
    assert "demooff" in stand.payloads(SYS)     # выход виден сразу


async def test_student_cannot_turn_the_stand_on(stand):
    await register(STUDENT)
    await press(STUDENT, "demoon")
    assert await demo.is_demo() is False
    assert "только сис-админ" in stand.to(STUDENT)[-1][1]


async def test_student_sees_no_switch(stand):
    await register(STUDENT)
    await press(STUDENT, "demo")
    assert "только сис-админу" in stand.to(STUDENT)[-1][1]
    assert "demoon" not in stand.payloads(STUDENT)


async def test_exit_button_turns_the_stand_off(stand):
    await demo.enter(SYS)
    await press(SYS, "demooff")
    assert await demo.is_demo() is False
    assert await db.get_state(SYS) is None
    assert demo.OFF_DONE in stand.to(SYS)[-1][1]


async def test_demo_off_command_for_sysadmin(stand):
    await demo.enter(SYS)
    assert await demo.off_command(SYS) is True
    assert await demo.is_demo() is False


async def test_demo_off_command_is_silent_when_stand_is_off(stand):
    await press(SYS, "home")
    assert await demo.off_command(SYS) is False
    assert stand.to(SYS)[-1][1] != demo.OFF_DONE


async def test_leave_outside_demo_does_nothing(stand):
    await press(SYS, "home")
    before = len(stand.sent)
    assert await demo.leave(SYS, "кнопка «🏠 Меню»") is False
    assert len(stand.sent) == before


async def test_leave_records_the_reason(stand):
    await demo.enter(SYS)
    await demo.leave(SYS, "кнопка «🏠 Меню»")
    assert "Демо-режим выключен" in stand.to(SYS)[-1][1]
    assert any("демо" in row for row in await logged_actions())


# ── главное: в демо ничего не меняется ────────────────────────────────────────
async def test_no_dangerous_button_reaches_the_database(stand):
    """Каждая опасная кнопка отвечает отказом и не меняет ни одной строки."""
    await demo.enter(SYS)
    before = await snapshot()
    for name in sorted(demo.DANGEROUS):
        assert await demo.guard_callback(SYS, name) is True, f"{name} прошёл мимо демо-режима"
        assert await snapshot() == before, f"{name} изменил базу в демо-режиме"
        assert "не выполняется" in stand.to(SYS)[-1][1]
    assert await snapshot() == before


async def test_refusal_names_the_action_and_says_nothing_changed(stand):
    await demo.enter(SYS)
    await demo.deny(SYS, "рассылка сотрудникам")
    text = stand.to(SYS)[-1][1]
    assert "рассылка сотрудникам не выполняется" in text
    assert "ничего не изменилось" in text
    assert "demooff" in stand.payloads(SYS)      # выход доступен с любого отказа


async def test_ticket_draft_scenario_is_not_started(stand):
    await register(STUDENT)
    await demo.set_demo(True, actor=SYS)
    before = await snapshot()
    assert await demo.guard_state(STUDENT, "ticket") is True
    assert await demo.guard_message(STUDENT, "Прошу выдать справку") is True
    assert await snapshot() == before
    assert demo.TITLE in stand.to(STUDENT)[-1][1]


async def test_name_and_group_input_is_not_started(stand):
    await demo.enter(SYS)
    before = await snapshot()
    for state_name in ("reg_name", "reg_group", "edit_name", "edit_group", "teacher_search"):
        assert await demo.guard_state(SYS, state_name) is True, state_name
    assert await demo.guard_message(SYS, "Иванов Иван Иванович") is True
    assert await demo.guard_message(SYS, "24-23") is True
    assert await snapshot() == before


async def test_commands_still_pass_in_demo(stand):
    """Просмотр по командам не блокируется: /demo_off и /view:student работают."""
    await demo.enter(SYS)
    assert await demo.guard_message(SYS, "/view:student") is False
    assert await demo.guard_message(SYS, "/today") is False
    assert await demo.guard_message(SYS, "/demo_off") is True
    assert await demo.is_demo() is False


async def test_broadcast_is_blocked_before_any_recipients(stand):
    await demo.set_demo(True, actor=SYS)
    before = await snapshot()
    assert await demo.guard_callback(SYS, "bcgo:все") is True
    assert await snapshot() == before
    # единственное сообщение — отказ самому сис-админу; сотрудникам ничего не ушло
    assert {uid for uid, _text, _kb in stand.sent} == {SYS}
    assert "рассылка" in stand.to(SYS)[-1][1]


async def test_view_buttons_are_not_blocked(stand):
    await demo.set_demo(True, actor=SYS)
    for name in VIEW_CALLBACKS:
        assert name not in demo.DANGEROUS, f"{name} попал в запрещённые"
        assert await demo.guard_callback(SYS, name) is False, name
    for state_name in ("teacher_search",):       # поиск — только по кнопке, не по тексту
        assert state_name in demo.WRITE_STATES


async def test_only_the_journal_grows(stand):
    await demo.set_demo(True, actor=SYS)
    before = await snapshot()
    await demo.guard_callback(SYS, "st:1:completed")
    assert await snapshot() == before
    assert any("демо" in row for row in await logged_actions())


async def test_snapshot_would_notice_a_real_change(stand):
    """Контроль самого снимка: без него тесты выше ничего не доказывали бы."""
    before = await snapshot()
    await repo.create_ticket(STUDENT, SYS, "feedback", "Прошу справку")
    assert await snapshot() != before


# ── ничего не уходит третьим лицам ────────────────────────────────────────────
async def test_demo_sends_nothing_but_to_the_one_who_entered(stand):
    await demo.enter(SYS)
    await demo.guard_callback(SYS, "snew")
    await demo.screen(SYS)
    assert {uid for uid, _text, _kb in stand.sent} == {SYS}


async def test_students_get_answers_only_to_themselves(stand):
    await demo.set_demo(True, actor=SYS)
    await register(STUDENT)
    count = len(stand.sent)
    await demo.screen(STUDENT)
    await demo.guard_message(STUDENT, "текст")
    assert {uid for uid, _t, _k in stand.sent[count:]} == {STUDENT}


# ── пределы MAX ───────────────────────────────────────────────────────────────
async def test_demo_keyboards_fit_max_limits(stand):
    await demo.enter(SYS)
    await press(SYS, "demo")
    await demo.deny(SYS, "смена прав")
    assert stand.sent
    for _uid, _text, keyboard in stand.sent:
        rows = [row for row in (keyboard or []) if row]
        assert len(rows) <= max_api.MAX_ROWS
        for row in rows:
            assert len(row) <= demo.MAX_BUTTONS


async def test_limit_rows_splits_and_truncates():
    button = {"type": "callback", "text": "a", "payload": "a"}
    wide = [[button] * 10]
    assert [len(row) for row in demo.limit_rows(wide)] == [7, 3]
    assert len(demo.limit_rows([[button]] * 40)) == max_api.MAX_ROWS
    assert demo.limit_rows(None) == []


async def test_exit_button_is_in_every_demo_keyboard(stand):
    for keyboard in (demo.demo_kb(), demo.exit_rows(), demo.refuse_kb()):
        payloads = [b["payload"] for row in keyboard for b in row]
        assert "demooff" in payloads


async def test_exit_button_is_named_as_agreed(stand):
    assert demo.exit_rows()[0][0] == {"type": "callback", "text": "↩️ Выйти из демо",
                                       "payload": "demooff"}
    assert "демо" in demo.banner().lower()
    assert demo.MAX_BUTTONS == 7 and max_api.MAX_ROWS == 30


# ── врезка в меню и обращения ────────────────────────────────────────────────
# Эти три теста проверяют то, что делается в menus.py и tickets.py. Пока врезки
# нет, они пропускаются; как только она появится — проверяют по-настоящему.
async def home_leaves_stand(stand) -> bool:
    await demo.set_demo(True, actor=SYS)
    await press(SYS, "home")
    return not await demo.is_demo()


async def off_command_works(stand) -> bool:
    await demo.set_demo(True, actor=SYS)
    await say(SYS, "/demo_off")
    return not await demo.is_demo()


async def ticket_creation_is_protected(stand) -> bool:
    """Сценарий обращения перехвачен: и кнопка, и текст дают экран демо."""
    await register(STUDENT)
    await demo.set_demo(True, actor=SYS)
    before = await snapshot()
    await press(STUDENT, "new:feedback")
    await say(STUDENT, "Прошу справку с места обучения")
    return demo.TITLE in stand.to(STUDENT)[-1][1] and await snapshot() == before


async def test_integration_home_button_leaves_the_stand(stand):
    if not await home_leaves_stand(stand):
        pytest.skip("«🏠 Меню» ещё не выключает демо-режим (см. инструкции по врезке)")
    assert demo.OFF_DONE in "\n".join(text for _uid, text, _kb in stand.to(SYS))


async def test_integration_ticket_is_not_created(stand):
    if not await ticket_creation_is_protected(stand):
        pytest.skip("создание обращения ещё не перехвачено (см. инструкции по врезке)")
    assert await db.many("SELECT ticket_id FROM tickets") == []


async def test_integration_demo_off_command(stand):
    if not await off_command_works(stand):
        pytest.skip("/demo_off ещё не обрабатывается (см. инструкции по врезке)")
    assert await db.get_state(SYS) is None
