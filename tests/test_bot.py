import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import repository
from conftest import add_staff, click, press, register, say
from handlers.common import pending_tasks

STUDENT, STAFF, STAFF2, OTHER = "100", "200", "201", "300"


async def test_callback_user_is_taken_from_callback_not_message_sender(api):
    """Регресс: в message_callback message.sender — бот, а не пользователь."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "profile")
    assert api.to(STUDENT), "ответ должен уйти студенту"
    assert not api.to("999"), "бот не должен писать сам себе"
    assert "Иванов Иван Иванович" in api.last(STUDENT)[1]


async def test_start_offers_only_two_ways(api):
    """На регистрации ровно две кнопки: студент и сотрудник — «пока не знаю» убрали."""
    await say(STUDENT, "/start")
    assert set(api.payloads(STUDENT)) == {"who:student", "who:staff"}


async def test_registration_validates_and_normalizes(api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иван")  # одно слово - не ФИО
    assert "полностью" in api.last(STUDENT)[1]
    await say(STUDENT, "Иванов Иван")
    await say(STUDENT, "  ИС-21 ")
    # последний шаг: сверить данные перед сохранением
    assert "Проверьте данные" in api.last(STUDENT)[1]
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None
    await press(STUDENT, "regyes")
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["group_code"] == "ИС-21" and row["full_name"] == "Иванов Иван"
    assert "sched" in api.payloads(STUDENT)  # расписание группы доступно сразу


async def test_unregistered_user_is_sent_to_registration(api):
    await press(STUDENT, "profile")
    assert "Кто вы" in api.last(STUDENT)[1]
    await press(STUDENT, "who:student")
    await say(STUDENT, "привет")  # состояние reg_name → ФИО из одного слова
    assert "полностью" in api.last(STUDENT)[1]


async def test_guest_can_see_schedules_without_registration(api):
    await repository.upsert_group("ИС-21", "Информационные системы")
    await repository.upsert_schedule("ИС-21", "https://college.example/is-21.pdf")
    await say(STUDENT, "/start")
    await press(STUDENT, "who:guest")
    assert "view_schedules" in api.payloads(STUDENT)
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None
    await press(STUDENT, "view_schedules")
    assert "sched:ИС-21" in api.payloads(STUDENT)
    await press(STUDENT, "sched:ИС-21")
    assert "is-21.pdf" in api.last(STUDENT)[1]


async def test_id_command_and_hidden_admin_command(api):
    await say(OTHER, "/id")
    assert OTHER in api.last(OTHER)[1]
    api.sent.clear()
    await say(OTHER, "/supersecret_admin")
    assert not api.to(OTHER)  # обычный пользователь — тишина
    await say("1", "/supersecret_admin")
    assert "admins" in api.payloads("1")


async def test_superadmin_seeded_from_env(api):
    row = await db.one("SELECT role_type, can_broadcast FROM admins WHERE user_id='1'")
    assert row["role_type"] == "sysadmin" and row["can_broadcast"] == 1


async def test_full_ticket_conversation(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "feedback")

    await press(STUDENT, "new:feedback")
    assert f"pick:feedback:{STAFF}" in api.payloads(STUDENT)
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Не работает электронный журнал")

    t = await db.one("SELECT * FROM tickets")
    assert (t["student_id"], t["target_admin_id"], t["status"]) == (STUDENT, STAFF, "new")
    notice = api.last(STAFF)
    assert "Не работает электронный журнал" in notice[1] and "Иванов Иван Иванович" in notice[1]
    assert {f"rp:{t['ticket_id']}", f"st:{t['ticket_id']}:accepted"} <= set(api.payloads(STAFF))

    # сотрудник отвечает — студент получает, статус «в работе»
    await press(STAFF, f"rp:{t['ticket_id']}")
    await say(STAFF, "Проверим, ответим сегодня")
    assert "Проверим, ответим сегодня" in api.last(STUDENT)[1]
    assert (await db.one("SELECT status FROM tickets"))["status"] == "accepted"

    # студент отвечает — сотрудник получает
    await press(STUDENT, f"rp:{t['ticket_id']}")
    await say(STUDENT, "Спасибо, жду")
    assert "Спасибо, жду" in api.last(STAFF)[1]

    # завершение: студент уведомлён, писать в закрытое обращение нельзя
    await press(STAFF, f"st:{t['ticket_id']}:completed")
    assert "Завершено" in api.last(STUDENT)[1]
    await press(STUDENT, f"rp:{t['ticket_id']}")
    assert "закрыто" in api.last(STUDENT)[1]

    view = await db.many("SELECT sender_role FROM ticket_messages ORDER BY id")
    assert [r["sender_role"] for r in view] == ["student", "staff", "student"]


async def test_ticket_access_control(api):
    await register(STUDENT)
    await register(OTHER, "Сидоров Пётр", "ис-22")
    await add_staff(STAFF, "Петрова Анна")
    await add_staff(STAFF2, "Козлов Иван")
    await press(STUDENT, f"pick:certificates:{STAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]

    for outsider in (OTHER, STAFF2):
        api.sent.clear()
        await press(outsider, f"t:{tid}")
        assert "не найдено" in api.last(outsider)[1]
        await press(outsider, f"st:{tid}:completed")
        assert (await db.one("SELECT status FROM tickets"))["status"] == "new"
    await press("1", f"t:{tid}")  # superadmin видит всё
    assert f"№{tid}" in api.last("1")[1]


async def test_staff_only_sees_matching_category(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "feedback")
    await add_staff(STAFF2, "Козлов Иван", "certificates")
    await press(STUDENT, "new:certificates")
    payloads = api.payloads(STUDENT)
    assert f"pick:certificates:{STAFF2}" in payloads and f"pick:certificates:{STAFF}" not in payloads


async def test_admin_callbacks_are_protected(api):
    await register(STUDENT)
    for payload in ("admins", "sfadd", "schedules", "scadd", "stats", "settings", "set:tickets", f"sfb:{STUDENT}", "sfdy:200"):
        await press(STUDENT, payload)
    assert await db.one("SELECT 1 FROM admins WHERE user_id=?", (STUDENT,)) is None
    assert await db.get_setting("tickets_enabled", "1") == "1"


async def test_schedule_management_and_student_view(api, monkeypatch):
    async def fake_probe(url):
        if str(url).startswith("https://"):
            return True, ""
        return False, "Нужна ссылка вида https://…"

    monkeypatch.setattr("handlers.admin.probe_pdf_url", fake_probe)
    await register(STUDENT)
    await press(STUDENT, "sched")
    assert "не добавлено" in api.last(STUDENT)[1]

    await press("1", "scadd")
    await say("1", "ис-21")
    await say("1", "не ссылка")
    assert "https://" in api.last("1")[1]  # валидация ссылки
    await say("1", "https://college.example/is-21.pdf")

    await press(STUDENT, "sched")
    assert "https://college.example/is-21.pdf" in api.last(STUDENT)[1]
    await press("1", "scdel:ИС-21")
    assert await db.one("SELECT 1 FROM schedules") is None


async def test_profile_edit(api):
    await register(STUDENT)  # группа ИС-21 попадает в справочник при регистрации
    # группы, которой нет в справочнике, бот просит подтвердить вручную
    await press(STUDENT, "pf:group")
    await say(STUDENT, "ис-31")
    assert "не найдена в активном справочнике" in api.last(STUDENT)[1]
    await press(STUDENT, "regok:ИС-31:Иванов Иван Иванович")
    assert (await db.one("SELECT group_code FROM users"))["group_code"] == "ИС-31"
    # а на группу из справочника — без лишних вопросов
    await press(STUDENT, "pf:group")
    await say(STUDENT, "ис-21")
    assert (await db.one("SELECT group_code FROM users"))["group_code"] == "ИС-21"


async def test_tickets_can_be_switched_off(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    await press("1", "set:tickets")
    await press(STUDENT, "new:feedback")
    assert "отключён" in api.last(STUDENT)[1]


async def test_broadcast_permissions_and_group_targeting(api):
    await register(STUDENT, "Иванов Иван", "ис-21")
    await register(OTHER, "Сидоров Пётр", "ис-22")
    await add_staff(STAFF, "Петрова Анна")  # без права рассылки
    await add_staff(STAFF2, "Козлов Иван", broadcast=True)

    await press(STAFF, "broadcast")
    assert "нет права" in api.last(STAFF)[1]

    await press(STAFF2, "broadcast")
    await press(STAFF2, "bcaud:group")
    await say(STAFF2, "ис-21")
    await say(STAFF2, "Завтра сокращённые пары")
    assert "1 чел." in api.last(STAFF2)[1]
    api.sent.clear()
    await press(STAFF2, "bcgo")
    await bot.asyncio.gather(*pending_tasks())
    assert "Завтра сокращённые пары" in api.last(STUDENT)[1]
    assert not [m for m in api.to(OTHER) if "Завтра" in m[1]]
    assert "Доставлено: 1" in api.last(STAFF2)[1]
    assert (await db.one("SELECT sent FROM broadcasts"))["sent"] == 1


async def test_broadcast_counts_undelivered(api):
    await register(STUDENT, "Иванов Иван", "ис-21")
    await register(OTHER, "Сидоров Пётр", "ис-21")
    api.blocked.add(OTHER)
    await press("1", "broadcast")
    await press("1", "bcaud:all")
    await say("1", "Всем привет")
    await press("1", "bcgo")
    await bot.asyncio.gather(*pending_tasks())
    assert "Доставлено: 1, не доставлено: 1" in api.last("1")[1]


async def test_bcgo_without_prepared_broadcast_does_nothing(api):
    await press("1", "bcgo")
    assert "Нет подготовленной" in api.last("1")[1]
    assert await db.one("SELECT 1 FROM broadcasts") is None


async def test_broadcast_is_signed_with_sender_name_and_position(api):
    """Студент видит, от кого объявление: ФИО, должность и кабинет отправителя."""
    await register(STUDENT, "Иванов Иван", "ис-21")
    await add_staff(STAFF2, "Козлов Иван", broadcast=True)
    await press("1", f"srset:{STAFF2}:director")
    await press("1", f"sfo:{STAFF2}")
    await say("1", "214")

    await press(STAFF2, "broadcast")
    await press(STAFF2, "bcaud:all")
    await say(STAFF2, "Завтра сокращённые пары")
    # предпросмотр у отправителя — с той же подписью
    assert "— Козлов Иван, 👔 Директор, каб. 214" in api.last(STAFF2)[1]

    api.sent.clear()
    await press(STAFF2, "bcgo")
    await bot.asyncio.gather(*pending_tasks())
    sent = api.last(STUDENT)[1]
    assert sent.startswith("📢 Объявление колледжа:")
    assert "— Козлов Иван, 👔 Директор, каб. 214" in sent
    row = await db.one("SELECT * FROM broadcasts")
    assert (row["sender_name"], row["sender_role"]) == ("Козлов Иван", "👔 Директор")


async def test_sysadmin_broadcast_signed_as_sysadmin(api):
    await register(STUDENT, "Иванов Иван", "ис-21")
    await press("1", "broadcast")
    await press("1", "bcaud:all")
    await say("1", "Праздник")
    await press("1", "bcgo")
    await bot.asyncio.gather(*pending_tasks())
    assert "— Сис-админ" in api.last(STUDENT)[1]


async def test_sysadmin_service_commands(api):
    await say("1", "/logs")
    assert "Журнал" in api.last("1")[1]
    await say("1", "/panel")
    assert "панель сис-админа" in api.last("1")[1].lower()
    await say("1", "/test")
    assert "Самопроверка" in api.last("1")[1]
    await bot.asyncio.gather(*pending_tasks())


async def test_service_commands_are_invisible_for_students(api):
    await register(STUDENT, "Иванов Иван", "ис-21")
    await say(STUDENT, "/logs")
    assert "Журнал" not in api.last(STUDENT)[1]


async def test_delete_staff_blocked_while_tickets_open(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Вопрос")
    await press("1", f"sfdy:{STAFF}")
    assert await db.one("SELECT 1 FROM admins WHERE user_id=?", (STAFF,))
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]
    await press("1", f"st:{tid}:completed")
    await press("1", f"sfdy:{STAFF}")
    assert await db.one("SELECT 1 FROM admins WHERE user_id=?", (STAFF,)) is None


async def test_stats(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна")
    await press("1", "stats")
    assert "Студентов: 1" in api.last("1")[1] and "Сотрудников: 1" in api.last("1")[1]
    await press(STAFF, "staffstats")
    assert "Ваши обращения" in api.last(STAFF)[1]


async def test_non_text_message_and_group_chat_ignored(api):
    await register(STUDENT)
    api.sent.clear()
    await bot.process({"update_type": "message_created", "message": {"sender": {"user_id": 100}, "recipient": {"chat_type": "dialog"}, "body": {"text": None}}})
    assert "только текстовые" in api.last(STUDENT)[1]
    api.sent.clear()
    await bot.process({"update_type": "message_created", "message": {"sender": {"user_id": 100}, "recipient": {"chat_type": "chat"}, "body": {"text": "в группе"}}})
    assert not api.sent


async def test_bot_started_and_callback_answer(api):
    await bot.process({"update_type": "bot_started", "user": {"user_id": 100}})
    assert "Кто вы" in api.last("100")[1]
    await bot.process(click("100", "home"))
    assert api.answers and api.answers[-1].startswith("cb-")


async def test_handler_error_does_not_crash_and_user_is_told(api, monkeypatch):
    await register(STUDENT)

    async def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setitem(bot.CALLBACKS, "profile", boom)
    await press(STUDENT, "profile")
    assert "пошло не так" in api.last(STUDENT)[1]


# ── защита от дублей доставки ─────────────────────────────────────────────


async def test_redelivered_callback_is_processed_once(api):
    """Повторная доставка того же нажатия (webhook retry / long polling) не дублирует ответ."""
    await register(STUDENT)
    api.sent.clear()
    dup = click(STUDENT, "profile")
    await bot.process(dup)
    assert "Профиль" in api.last(STUDENT)[1]
    api.sent.clear()
    await bot.process(dup)
    assert not api.to(STUDENT), "повторно доставленный callback не должен обрабатываться"


async def test_redelivered_message_is_processed_once(api):
    api.sent.clear()
    dup = {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": int(STUDENT), "is_bot": False},
            "recipient": {"chat_id": int(STUDENT), "chat_type": "dialog"},
            "timestamp": 1710000000000,
            "body": {"text": "/id"},
        },
    }
    await bot.process(dup)
    await bot.process(dup)
    assert len(api.to(STUDENT)) == 1, "ответ /id должен прийти ровно один раз"


async def test_redelivered_bot_started_is_processed_once(api):
    dup = {"update_type": "bot_started", "user": {"user_id": int(STUDENT)}, "timestamp": 1700000000000}
    await bot.process(dup)
    api.sent.clear()
    await bot.process(dup)
    assert not api.to(STUDENT)


async def test_distinct_callback_presses_are_all_processed(api):
    """Разные нажатия одной кнопки (разные callback_id) обрабатываются все."""
    await register(STUDENT)
    api.sent.clear()
    await bot.process(click(STUDENT, "profile"))
    await bot.process(click(STUDENT, "profile"))
    assert len(api.to(STUDENT)) == 2, "два разных нажатия — два ответа"


# ── webhook ─────────────────────────────────────────────────────────────────


def test_webhook_secret_is_checked(monkeypatch):
    seen = []

    async def fake_process(u):
        seen.append(u)

    monkeypatch.setattr(config, "WEBHOOK_URL", "https://bot.example.com/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret_value")
    monkeypatch.setattr(bot, "process", fake_process)
    client = TestClient(bot.app)  # без lifespan: подписка на MAX не создаётся
    body = {"update_type": "bot_started", "user": {"user_id": 1}}
    assert client.post("/webhook", json=body).status_code == 401
    assert client.post("/webhook", json=body, headers={"X-Max-Bot-Api-Secret": "wrong"}).status_code == 401
    assert client.post("/webhook", json=body, headers={"X-Max-Bot-Api-Secret": "тест".encode()}).status_code == 401
    assert client.post("/webhook", json=body, headers={"X-Max-Bot-Api-Secret": "s3cret_value"}).status_code == 200
    assert len(seen) == 1


def test_webhook_disabled_in_polling_mode(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    client = TestClient(bot.app)
    assert client.post("/webhook", json={"update_type": "bot_started", "user": {"user_id": 1}}).status_code == 404


def test_config_validation(monkeypatch):
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "")
    with pytest.raises(RuntimeError, match="MAX_BOT_TOKEN"):
        config.validate()
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "tok")
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://insecure")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
    with pytest.raises(RuntimeError, match="https"):
        config.validate()
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://ok.example/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "good_secret-1")
    config.validate()
