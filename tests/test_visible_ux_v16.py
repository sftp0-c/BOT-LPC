"""Видимые маршруты команд и клавиатуры UX v1.6.0."""
import max_api
import repository as repo
import timetable as tt
from conftest import add_staff, press, register, say
from handlers import admin, menus

SYS, STAFF, STUDENT, STRANGER = "1", "200", "300", "400"


def result(schedule):
    async def inner(*args, **kwargs):
        return type("R", (), {"has_lessons": True, "schedule": schedule,
                              "reason": "", "warning": ""})()
    return inner()


async def prepare_schedule(monkeypatch, group="24-23"):
    from handlers import schedules

    await repo.upsert_group(group, group)
    await repo.upsert_schedule(group, f"https://college.example/{group}.pdf")
    schedule = tt.GroupSchedule(group=group, days={})
    monkeypatch.setattr(schedules, "parse_group", lambda code, force=False: result(schedule))


async def test_schedule_command_opens_own_schedule_not_group_picker(api, monkeypatch):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await prepare_schedule(monkeypatch)
    await say(STUDENT, "/schedule")
    assert "Расписание группы 24-23" in api.last(STUDENT)[1]
    assert "Выберите группу" not in api.last(STUDENT)[1]


async def test_today_student_shows_lessons(api, monkeypatch):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await prepare_schedule(monkeypatch)
    await say(STUDENT, "/today")
    assert "24-23" in api.last(STUDENT)[1]
    assert "Что сделать сегодня" not in api.last(STUDENT)[1]


async def test_old_today_is_role_aware(api, monkeypatch):
    await press(SYS, "today")
    assert "Что сделать сегодня" in api.last(SYS)[1]

    await repo.upsert_group("24-23", "24-23")
    await repo.upsert_schedule("24-23", "https://college.example/24-23.pdf")
    schedule = tt.GroupSchedule(group="24-23", days={})
    from handlers import schedules
    monkeypatch.setattr(schedules, "parse_group", lambda code, force=False: result(schedule))
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await press(STUDENT, "today")
    assert "24-23" in api.last(STUDENT)[1]


async def test_admin_today_has_separate_route(api):
    await press(SYS, "sysadm")
    payloads = api.payloads(SYS)
    assert "admin_today" in payloads
    assert "today" not in payloads
    await press(SYS, "admin_today")
    assert "Что сделать сегодня" in api.last(SYS)[1]


async def test_weak_role_commands_never_silently_ignore(api):
    await register(STRANGER, "Сидоров Сидор", "24-23")
    for command, phrase in (("/queue", "доступна сотрудникам"),
                            ("/stats", "доступна сотрудникам"),
                            ("/view", "только сис-админ")):
        before = len(api.to(STRANGER))
        await say(STRANGER, command)
        assert len(api.to(STRANGER)) > before, command
        assert phrase in api.last(STRANGER)[1], command


async def test_staff_cabinet_button_switches_to_staff_view(api):
    await press(SYS, "sysadm")
    assert "view:staff" in api.payloads(SYS)
    assert "home" not in [p for p in api.payloads(SYS) if p == "home"]
    await press(SYS, "view:staff")
    assert "Режим сотрудника" in api.last(SYS)[1]


async def test_view_rows_are_two_plus_one(api):
    await say(SYS, "/view")
    role_rows = [row for row in api.last(SYS)[2]
                 if any(button.get("payload", "").startswith("view:") for button in row)]
    assert [len(row) for row in role_rows] == [2, 1]


async def test_ttl_rows_have_at_most_two_buttons(api):
    await press(SYS, "codegen")
    ttl_rows = [row for row in api.last(SYS)[2]
                if any(button.get("payload", "").startswith("codettl:") for button in row)]
    assert ttl_rows and all(1 <= len(row) <= 2 for row in ttl_rows)


def test_main_menu_topology_is_unchanged():
    assert [[button["payload"] for button in row] for row in menus.student_menu()] == [
        ["ticket_menu"], ["sched", "profile"], ["help"]
    ]


async def test_same_home_has_no_back_menu_duplicate(api):
    await register(STUDENT)
    await press(STUDENT, "ticket_menu")
    payloads = api.payloads(STUDENT)
    assert payloads.count("home") == 1

    await add_staff(STAFF, "Петрова Мария", position="Секретарь")
    await press(STAFF, "home")
    assert "home" not in api.payloads(STAFF)


async def test_welcome_hierarchy_keeps_exactly_four_main_buttons(api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await press(STUDENT, "home")
    text, keyboard = api.last(STUDENT)[1], api.last(STUDENT)[2]
    assert text.startswith("🏫 Навигатор ЛПК\n\n")
    assert "🧭 Выберите раздел:" in text
    assert sum(len(row) for row in keyboard) == 4


async def test_dynamic_group_list_keeps_full_data_in_text(api):
    long_code = "24-21-2С-ДЛИННАЯ"
    await repo.upsert_group(long_code, "Длинная группа")
    await menus._ask_group(STUDENT, "Иванов Иван", "", [{"code": long_code, "active": 1}])
    text, keyboard = api.last(STUDENT)[1], api.last(STUDENT)[2]
    assert long_code in text
    assert any(button["text"] == "Группа 1" for row in keyboard for button in row)


async def test_visible_dynamic_screens_survive_fit_keyboard(api):
    await repo.upsert_group("24-21-2С-ДЛИННАЯ", "Длинная группа")
    await repo.upsert_schedule("24-21-2С-ДЛИННАЯ", "https://college.example/long.pdf")
    await press(SYS, "groups")
    await press(SYS, "schedules")
    await press(SYS, "view_schedules")
    for _to, _text, keyboard in api.to(SYS):
        for row in max_api.fit_keyboard(keyboard or []):
            assert len(row) <= 2
            assert all(not button["text"].endswith("…") for button in row)


def test_admin_full_week_schedule_is_preserved():
    import inspect

    source = inspect.getsource(admin.cb_schedule_view)
    assert "tt.format_schedule(schedule)" in source
