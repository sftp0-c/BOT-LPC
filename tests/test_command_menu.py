"""Нижнее меню MAX = команды бота (PATCH /me/commands).

Проверено по документации MAX и запросами к API: бот умеет присылать только
inline-кнопки, нижние кнопки в сообщении API принимает, но не рисует. Меню
снизу собирается из команд, поэтому здесь проверяем именно их.
"""
import re


import bot_commands
import max_api
from bot_commands import bot_command_list, command_name, command_payload
from conftest import add_staff, register, say

STUDENT, SYS = "300", "1"


# ── форма команды для MAX ────────────────────────────────────────────────────
def test_command_names_match_max_rules():
    """Только латиница, цифры, подчёркивание; начинается с буквы; не длиннее 32."""
    for command in bot_command_list():
        name = command["name"]
        assert re.match(r"^[a-z][a-z0-9_]*$", name), name
        assert 1 <= len(name) <= bot_commands.MAX_COMMAND_NAME
        assert command["description"].strip()
        assert len(command["description"]) <= 256


def test_command_list_fits_max_limit():
    assert len(bot_command_list()) <= 32


def test_command_name_sanitizes():
    assert command_name("Новое обращение").startswith("cmd_")
    assert command_name("2fast") == "cmd_2fast"
    assert command_name("my-command!") == "my_command_"
    assert len(command_name("a" * 60)) == 32


def test_no_duplicate_commands():
    names = [c["name"] for c in bot_command_list()]
    assert len(names) == len(set(names))


def test_every_command_has_a_handler():
    assert bot_commands.unknown_payloads() == []


# ── маршрутизация: команда ведёт туда же, куда кнопка ────────────────────────
def test_command_payload_maps_names():
    assert command_payload("/today") == "today"
    assert command_payload("today") == "today"
    assert command_payload("/TODAY") == "today"
    assert command_payload("/today@mybot") == "today"
    assert command_payload("/new_request") == "new:feedback"
    assert command_payload("/что-то другое") is None
    assert command_payload("") is None


async def test_command_opens_schedule(env, api):
    import repository as repo

    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await repo.upsert_schedule("24-23", "https://college.example/24-23.pdf")
    await say(STUDENT, "/schedule")
    assert "sched:24-23" in api.payloads(STUDENT)


async def test_command_today_shows_day_schedule(env, api, monkeypatch):
    import timetable as tt
    from handlers import schedules

    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    schedule = tt.GroupSchedule(group="24-23", days={})
    monkeypatch.setattr(schedules, "parse_group", lambda code, force=False: _result(schedule))
    await say(STUDENT, "/today")
    assert any("24-23" in body for _, body, _ in api.to(STUDENT))


def _result(schedule):
    async def inner(*args, **kwargs):
        return type("R", (), {"has_lessons": True, "schedule": schedule, "reason": ""})()
    return inner()


async def test_command_menu_returns_home(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/menu")
    assert "Меню" in api.to(STUDENT)[-1][1] or api.to(STUDENT)[-1][2]


async def test_command_help_lists_commands(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/help")
    text = "\n".join(body for _, body, _ in api.to(STUDENT))
    assert "/schedule" in text and "/tickets" in text
    assert "Что умею" in text or "умею" in text


async def test_command_tickets_lists_my_tickets(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/tickets")
    assert any("обращен" in body.lower() for _, body, _ in api.to(STUDENT))


async def test_command_profile_shows_profile(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/profile")
    assert any("24-23" in body for _, body, _ in api.to(STUDENT))


async def test_command_new_request_starts_ticket(env, api):
    """Команда из меню ведёт в тот же сценарий, что и кнопка «Новое обращение»."""
    await add_staff("200", "Петрова Мария Сергеевна")
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    before = len(api.to(STUDENT))
    await say(STUDENT, "/new_request")
    assert len(api.to(STUDENT)) > before                      # бот что-то ответил
    assert any(p.startswith("pick:") for p in api.payloads(STUDENT))   # выбор сотрудника


async def test_queue_command_is_staff_only(env, api):

    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/queue")
    # студент остаётся в своём меню, а не попадает в чужую очередь
    assert not any("Очередь" in body for _, body, _ in api.to(STUDENT))

    await add_staff("200", "Петрова Мария Сергеевна")
    await say("200", "/queue")
    assert any("обращен" in body.lower() for _, body, _ in api.to("200"))


async def test_admin_command_hidden_from_students(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/admin")
    assert not any("сис-админ" in body.lower() for _, body, _ in api.to(STUDENT))


async def test_admin_command_opens_menu_for_sysadmin(env, api):
    import repository as repo

    await repo.grant_sysadmin(SYS, "Иванов Иван Иванович")
    await say(SYS, "/admin")
    assert any("сис-админ" in body.lower() or "Меню" in body for _, body, _ in api.to(SYS))


async def test_command_with_bot_suffix_works(env, api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await say(STUDENT, "/help@lpc_bot")
    assert any("умею" in body for _, body, _ in api.to(STUDENT))


# ── регистрация меню ─────────────────────────────────────────────────────────
async def test_menu_is_registered_on_startup(env, monkeypatch):
    import bot as bot_module

    calls = []

    async def fake_set_commands(commands):
        calls.append(commands)
        return {}

    monkeypatch.setattr(bot_module.api, "set_commands", fake_set_commands, raising=False)
    await bot_module.register_bot_menu()
    assert len(calls) == 1
    assert [c["name"] for c in calls[0]] == [c["name"] for c in bot_command_list()]


async def test_menu_registration_failure_does_not_break_startup(env, monkeypatch):
    """Падение регистрации не должно ронять бота: меню просто не обновится."""
    import bot as bot_module

    async def boom(commands):
        raise RuntimeError("MAX недоступен")

    monkeypatch.setattr(bot_module.api, "set_commands", boom, raising=False)
    await bot_module.register_bot_menu()


def test_set_commands_hits_right_endpoint():
    """Ошибка в адресе - молчаливое отсутствие меню, поэтому проверяем путь."""
    import inspect

    source = inspect.getsource(max_api.MaxAPI.set_commands)
    assert '"/me/commands"' in source and "PATCH" in source
