"""Реестр пользователей, коды сотрудников, заявки и лента событий обращения."""
import config
import database as db
import repository as repo
from conftest import press, register, say
from utils import fmt_when, profile_url

SYS, SYS2, STUDENT, NEWSTAFF, STRANGER = "1", "2", "100", "400", "500"


def user_event(uid, username="", text="привет"):
    """Событие MAX с ником пользователя — как приходит от живого человека."""
    sender = {"user_id": int(uid), "is_bot": False}
    if username:
        sender["username"] = username
    return {
        "update_type": "message_created",
        "message": {
            "sender": sender,
            "recipient": {"chat_id": 1, "chat_type": "dialog"},
            "body": {"text": text},
        },
    }


# ── ссылка на профиль MAX ─────────────────────────────────────────────────────
def test_profile_url_guards_against_foreign_address():
    assert profile_url("se14445139_bot") == "https://max.ru/se14445139_bot"
    assert profile_url("@ivanov") == "https://max.ru/ivanov"
    assert profile_url("") == ""
    assert profile_url(None) == ""
    # попытка подсунуть свой адрес или кавычку в атрибут href
    assert profile_url("evil.com/x") == ""
    assert profile_url('a" onclick="alert(1)') == ""
    assert profile_url("иван") == ""


# ── реестр пользователей ─────────────────────────────────────────────────────
async def test_every_message_is_remembered_in_people_registry(api):
    from bot import process

    await register(STUDENT)
    await process(user_event(STUDENT, "ivanov_i", "Здравствуйте"))
    await process(user_event(STRANGER, "", "просто гость"))

    rows = await repo.people()
    ids = {row["user_id"]: row for row in rows}
    assert STUDENT in ids and STRANGER in ids
    assert ids[STUDENT]["username"] == "ivanov_i"
    assert ids[STUDENT]["fio"] == "Иванов Иван Иванович"
    assert ids[STUDENT]["group_code"] == "ИС-21"
    assert repo.contact_kind(ids[STUDENT]) == "student"
    assert repo.contact_kind(ids[STRANGER]) == "guest"
    assert ids[STUDENT]["messages"] >= 1
    assert profile_url(ids[STUDENT]["username"]) == "https://max.ru/ivanov_i"


async def test_people_filters_and_search(api):
    from bot import process

    await register(STUDENT)
    await process(user_event(STUDENT, "ivanov_i"))
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "каб. 301")
    await process(user_event(NEWSTAFF, "sokolova_m"))

    # сис-админ тоже пишал боту, поэтому в «сотрудниках» их двое
    assert (await repo.people_count("student")) == 1
    assert (await repo.people_count("staff")) == 2
    assert (await repo.people_count()) == 3
    assert [r["user_id"] for r in await repo.people(q="Соколова")] == [NEWSTAFF]
    assert [r["user_id"] for r in await repo.people(q="мимо")] == []

    card = await repo.user_card(NEWSTAFF)
    assert card["staff_name"] == "Соколова Мария" and card["position"] == "Методист"
    assert card["kind"] == "staff" and card["username"] == "sokolova_m"

    overview = await repo.people_overview()
    assert overview["students"] == 1 and overview["staff"] == 2 and overview["total"] == 3


async def test_username_is_not_overwritten_by_empty_value(api):
    from bot import process

    await process(user_event(STRANGER, "first_nick"))
    await process(user_event(STRANGER, "", "ещё раз"))
    rows = await repo.people(q="first_nick")
    assert rows and rows[0]["username"] == "first_nick"


async def test_sysadmin_sees_people_menu_with_profile_links(api):
    from bot import process

    await register(STUDENT)
    await process(user_event(STUDENT, "ivanov_i"))
    await press(SYS, "people")
    text = api.last(SYS)[1]
    assert "Пользователи бота" in text
    assert "person:100" in api.payloads(SYS)

    await press(SYS, "person:100")
    card = api.last(SYS)[1]
    assert "Иванов Иван Иванович" in card and "https://max.ru/ivanov_i" in card
    assert "ИС-21" in card and "Кто вы" not in card


async def test_people_menu_is_closed_for_outsiders(api):
    from handlers.registry import CALLBACKS

    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "people")
    assert not api.to(STUDENT)
    await CALLBACKS["person"](STUDENT, SYS2)
    assert not api.to(STUDENT)


# ── вход сотрудника по коду ───────────────────────────────────────────────────
async def test_staff_joins_with_code_and_keeps_free_text_position(api):
    code = await repo.create_invite("ABCD23", ttl_hours=24)
    assert code == "ABCD23"
    await press(NEWSTAFF, "who:staff")
    assert (await db.get_state(NEWSTAFF))["state"] == "staff_code"

    await say(NEWSTAFF, " abcd-23 ")  # раскладка и дефис не важны
    assert (await db.get_state(NEWSTAFF))["state"] == "staff_join_name"
    await say(NEWSTAFF, "Соколова Мария")
    await say(NEWSTAFF, "Методист учебной части")
    await say(NEWSTAFF, "301")

    a = await repo.get_admin(NEWSTAFF)
    assert a["full_name"] == "Соколова Мария"
    assert a["position"] == "Методист учебной части" and a["office"] == "301"
    assert "сотрудник" in api.last(NEWSTAFF)[1].lower()
    assert await repo.attempts_count(NEWSTAFF) == 0  # успех сбрасывает счётчик


async def test_code_is_one_time_only(api):
    await repo.create_invite("ZZZZZZ", ttl_hours=24)
    await press(NEWSTAFF, "who:staff")
    await say(NEWSTAFF, "ZZZZZZ")
    await say(NEWSTAFF, "Первый Сотрудник")
    await say(NEWSTAFF, "-")
    await say(NEWSTAFF, "-")
    assert await repo.get_admin(NEWSTAFF)

    await press(STRANGER, "who:staff")
    await say(STRANGER, "ZZZZZZ")
    assert "уже использовали" in api.last(STRANGER)[1]
    assert await repo.get_admin(STRANGER) is None


async def test_personal_code_does_not_work_for_another_person(api):
    await repo.create_invite("PRSN01", user_id=NEWSTAFF, ttl_hours=24)
    await press(STRANGER, "who:staff")
    await say(STRANGER, "PRSN01")
    assert "другому сотруднику" in api.last(STRANGER)[1]
    assert await repo.get_admin(STRANGER) is None


async def test_expired_code_is_refused(api):
    await repo.create_invite("OLD001", ttl_hours=0)
    await db.run("UPDATE staff_invites SET expires_at=datetime('now','-1 hour') WHERE code='OLD001'")
    await press(NEWSTAFF, "who:staff")
    await say(NEWSTAFF, "OLD001")
    assert "Срок действия кода истёк" in api.last(NEWSTAFF)[1]


async def test_code_attempts_are_limited_per_hour(api):
    for _ in range(config.STAFF_CODE_ATTEMPTS):
        await press(NEWSTAFF, "who:staff")
        await say(NEWSTAFF, "NOSUCH")
    assert "Лимит попыток исчерпан" in api.last(NEWSTAFF)[1]
    assert await db.get_state(NEWSTAFF) is None
    # сис-админы узнают о попытках подбора
    assert "превышен лимит попыток" in "\n".join(m[1] for m in api.to(SYS))

    # через час попытки снова доступны
    await db.run("UPDATE login_attempts SET created_at=datetime('now','-2 hours')")
    assert await repo.attempts_count(NEWSTAFF) == 0


async def test_sysadmin_generates_code_and_shows_it(api):
    await press(SYS, "codegen")
    text = api.last(SYS)[1]
    assert "Код:" in text and "одноразовый" in text
    code = text.split("Код: ")[1].split("\n")[0].strip()
    assert await repo.invite_state(code) == "active"

    await press(SYS, "codes")
    assert code in api.last(SYS)[1]


async def test_sysadmin_invites_person_by_id(api):
    await press(SYS, "codeinv")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    text = api.last(SYS)[1]
    assert "Личное приглашение" in text
    code = text.split("Код: ")[1].split("\n")[0].strip()
    row = await db.one("SELECT user_id, full_name FROM staff_invites WHERE code=?", (code,))
    assert row["user_id"] == NEWSTAFF and row["full_name"] == "Соколова Мария"

    await press(NEWSTAFF, "who:staff")
    await say(NEWSTAFF, code)
    assert "Код принят" in api.last(NEWSTAFF)[1]


# ── заявка на роль сотрудника ─────────────────────────────────────────────────
async def test_staff_request_creates_row_and_notifies_sysadmins(api):
    await press(STRANGER, "staffreq")
    await say(STRANGER, "Соколова Мария")
    await say(STRANGER, "Секретарь")
    await say(STRANGER, "104")
    await say(STRANGER, "Назначена приказом №12")
    assert "Заявка отправлена" in api.last(STRANGER)[1]

    row = await repo.get_staff_request(STRANGER)
    assert (row["full_name"], row["position"], row["office"]) == ("Соколова Мария", "Секретарь", "104")
    assert row["note"] == "Назначена приказом №12" and row["status"] == "new"
    assert "Новая заявка" in "\n".join(m[1] for m in api.to(SYS))
    assert f"req:{STRANGER}" in api.payloads(SYS)


async def test_sysadmin_approves_request(api):
    await press(STRANGER, "staffreq")
    await say(STRANGER, "Соколова Мария")
    await say(STRANGER, "Секретарь")
    await say(STRANGER, "-")
    await say(STRANGER, "-")

    await press(SYS, "requests")
    assert f"req:{STRANGER}" in api.payloads(SYS)
    await press(SYS, f"req:{STRANGER}")
    assert "Секретарь" in api.last(SYS)[1]

    await press(SYS, f"reqok:{STRANGER}")
    a = await repo.get_admin(STRANGER)
    assert a["position"] == "Секретарь"
    assert (await repo.get_staff_request(STRANGER))["status"] == "approved"
    assert "одобрена" in "\n".join(m[1] for m in api.to(STRANGER))


async def test_sysadmin_rejects_request(api):
    await press(STRANGER, "staffreq")
    await say(STRANGER, "Соколова Мария")
    await say(STRANGER, "-")
    await say(STRANGER, "-")
    await say(STRANGER, "-")

    await press(SYS, f"reqno:{STRANGER}")
    await press(SYS, f"reqnoy:{STRANGER}")
    assert (await repo.get_staff_request(STRANGER))["status"] == "rejected"
    assert await repo.get_admin(STRANGER) is None
    assert "отклонена" in "\n".join(m[1] for m in api.to(STRANGER)).lower()


async def test_request_card_is_closed_for_outsiders(api):
    await press(STRANGER, "staffreq")
    await say(STRANGER, "Соколова Мария")
    await say(STRANGER, "-")
    await say(STRANGER, "-")
    await say(STRANGER, "-")
    api.sent.clear()
    await press(STRANGER, f"req:{STRANGER}")
    assert not api.to(STRANGER)


# ── должность, отдел и группировка сотрудников ────────────────────────────────
async def test_staff_card_saves_position_and_department(api):
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Преподаватель математики")
    await say(SYS, "каб. 214")
    assert "Преподаватель математики" in api.last(SYS)[1]
    assert "Отдел: —" in api.last(SYS)[1]

    await press(SYS, f"sfdep:{NEWSTAFF}")
    await say(SYS, "Учебная часть")
    assert "Учебная часть" in api.last(SYS)[1]

    await press(SYS, "admins")
    buttons = [b["text"] for row in api.last(SYS)[2] for b in row]
    assert any("Учебная часть" in text for text in buttons)
    assert any("Преподаватель математики" in text for text in buttons)


async def test_position_is_cleared_with_dash(api):
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Преподаватель математики")
    await say(SYS, "-")

    await press(SYS, f"sfr:{NEWSTAFF}")
    await say(SYS, "-")
    assert (await repo.get_admin(NEWSTAFF))["position"] == ""
    assert "не назначена" in api.last(SYS)[1]


async def test_staff_picker_shows_position(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист учебной части")
    await say(SYS, "301")

    await press(STUDENT, "new:feedback")
    labels = [b["text"] for row in api.last(STUDENT)[2] for b in row]
    assert any("Соколова Мария" in text and "Методист учебной части" in text for text in labels)


# ── лента событий обращения ───────────────────────────────────────────────────
async def test_ticket_events_are_logged_with_authors(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")

    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{NEWSTAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]

    await press(NEWSTAFF, f"rp:{tid}")
    await say(NEWSTAFF, "Готовим справку")
    await press(NEWSTAFF, f"t:{tid}")
    await press(NEWSTAFF, f"st:{tid}:ready")
    await press(NEWSTAFF, f"rt:{tid}:today")

    events = await repo.ticket_events(tid)
    kinds = [row["event"] for row in reversed(events)]
    # первое сообщение студента хранится в самом обращении, поэтому отдельного события нет
    assert kinds == ["created", "message_staff", "status", "ready"]
    assert any(row["actor_name"] == "Соколова Мария" for row in events)

    card = api.last(NEWSTAFF)[1]
    assert "Ответственный: Соколова Мария" in card
    assert "Должность: Методист" in card
    assert "автор обращения" in card
    assert "сегодня" in card or "вчера" in card


async def test_ticket_list_marks_who_waits_for_answer(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{NEWSTAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]

    await press(NEWSTAFF, "staff")
    labels = [b["text"] for row in api.last(NEWSTAFF)[2] for b in row]
    assert any(f"№{tid}" in text and "🔔 ждёт ответа" in text for text in labels)

    await press(NEWSTAFF, f"rp:{tid}")
    await say(NEWSTAFF, "Готовим")
    await press(NEWSTAFF, "staff")
    labels = [b["text"] for row in api.last(NEWSTAFF)[2] for b in row]
    assert all("🔔 ждёт ответа" not in text for text in labels)


# ── удаление пользователя ─────────────────────────────────────────────────────
async def test_delete_user_removes_registration_contact_and_state(api):
    await register(STUDENT)
    await repo.touch_contact(STUDENT, "ivanov_i", "Иван", "Здравствуйте")
    await db.set_state(STUDENT, "reg_group", {"name": "Иванов Иван Иванович"})
    assert await db.get_state(STUDENT) is not None

    done, message = await repo.delete_user(STUDENT)
    assert done is True and "Иванов Иван Иванович" in message
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None
    assert await db.one("SELECT 1 FROM contacts WHERE user_id=?", (STUDENT,)) is None
    assert await db.get_state(STUDENT) is None
    assert (await repo.people_count()) == 0


async def test_delete_user_keeps_tickets_by_default(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{NEWSTAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]

    # при открытом обращении удаление без «вместе с обращениями» запрещено
    done, message = await repo.delete_user(STUDENT)
    assert done is False and "открытых обращений" in message
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is not None

    done, message = await repo.delete_user(STUDENT, with_tickets=True)
    assert done is True and "удалено обращений: 1" in message
    assert await db.one("SELECT 1 FROM tickets WHERE ticket_id=?", (tid,)) is None
    assert await db.one("SELECT 1 FROM ticket_messages WHERE ticket_id=?", (tid,)) is None
    assert await db.one("SELECT 1 FROM ticket_events WHERE ticket_id=?", (tid,)) is None
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None


async def test_delete_closed_ticket_user_keeps_ticket(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{NEWSTAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]
    await press(NEWSTAFF, f"st:{tid}:completed")

    done, message = await repo.delete_user(STUDENT)
    assert done is True and "обращения оставлены" in message
    assert (await repo.get_ticket(tid))["student_id"] == STUDENT


async def test_sysadmin_cannot_be_deleted(api):
    done, message = await repo.delete_user(SYS)
    assert done is False and "Сотрудники" in message
    assert await repo.get_admin(SYS) is not None


async def test_staff_is_removed_as_staff_not_only_as_student(api):
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")
    done, message = await repo.delete_user(NEWSTAFF)
    assert done is True
    assert await repo.get_admin(NEWSTAFF) is None


async def test_bot_person_card_deletes_with_confirmation(api):
    await register(STUDENT)
    await repo.touch_contact(STUDENT, "ivanov_i")
    await press(SYS, f"person:{STUDENT}")
    assert f"persondel:{STUDENT}" in api.payloads(SYS)

    await press(SYS, f"persondel:{STUDENT}")
    assert "удалён" in api.last(SYS)[1]
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None


async def test_bot_refuses_to_delete_with_open_tickets(api):
    await register(STUDENT)
    await press(SYS, "sfadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Соколова Мария")
    await say(SYS, "Методист")
    await say(SYS, "301")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{NEWSTAFF}")
    await say(STUDENT, "Нужна справка")

    await press(SYS, f"persondel:{STUDENT}")
    assert f"persondely:{STUDENT}:1" in api.payloads(SYS)
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is not None

    await press(SYS, f"persondely:{STUDENT}:1")
    assert "удалено обращений" in api.last(SYS)[1]
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None


async def test_sysadmin_card_has_no_delete_button(api):
    await press(SYS, f"person:{SYS}")
    assert not [p for p in api.payloads(SYS) if p.startswith("persondel:")]


# ── сис-админы: выдача и отзыв прав ───────────────────────────────────────────
async def test_sysadmin_can_be_revoked_from_bot(api):
    await press(SYS, "sysadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Новиков Пётр")
    assert (await repo.get_admin(NEWSTAFF))["role_type"] == "sysadmin"
    assert "права сис-админа" in "\n".join(text for _, text, _ in api.to(NEWSTAFF)).lower()

    await press(SYS, "syslist")
    assert f"sysdel:{NEWSTAFF}" in api.payloads(SYS)
    await press(SYS, f"sysdel:{NEWSTAFF}")
    await press(SYS, f"sysdely:{NEWSTAFF}")
    assert await repo.get_admin(NEWSTAFF) is None
    assert "сняты" in "\n".join(text for _, text, _ in api.to(NEWSTAFF)).lower()


async def test_sysadmin_cannot_revoke_self_or_last(api):
    await press(SYS, f"sysdel:{SYS}")
    await press(SYS, f"sysdely:{SYS}")
    assert (await repo.get_admin(SYS))["role_type"] == "sysadmin"
    assert "Себе снять права нельзя" in api.last(SYS)[1]


async def test_revoked_sysadmin_is_not_revived_by_restart(api):
    await press(SYS, "sysadd")
    await say(SYS, NEWSTAFF)
    await say(SYS, "Новиков Пётр")
    await press(SYS, f"sysdel:{NEWSTAFF}")
    await press(SYS, f"sysdely:{NEWSTAFF}")
    assert NEWSTAFF in await repo.revoked_sysadmins()
    await db.init_db()  # перезапуск бота импортирует SYSADMIN_IDS из .env
    assert await repo.get_admin(NEWSTAFF) is None


async def test_deleted_user_starts_registration_again(api):
    await register(STUDENT)
    await repo.delete_user(STUDENT, with_tickets=True)
    api.sent.clear()
    await say(STUDENT, "/start")
    assert "Кто вы" in api.last(STUDENT)[1]
    await press(STUDENT, "who:student")
    await say(STUDENT, "Петров Пётр Петрович")
    await say(STUDENT, "ис-21")
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["full_name"] == "Петров Пётр Петрович"


# ── человеческое время ────────────────────────────────────────────────────────
def test_fmt_when_uses_relative_words_and_keeps_unparsable_as_is():
    from datetime import datetime, timedelta, timezone

    assert fmt_when("bad value") == "bad value"
    assert fmt_when("") == ""

    now = datetime.now().astimezone()
    def ago(delta):
        return (now - delta).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    assert fmt_when(ago(timedelta(hours=1)), now=now).startswith("сегодня ")
    assert fmt_when(ago(timedelta(days=1)), now=now).startswith("вчера ")
    week = fmt_when(ago(timedelta(days=3)), now=now)
    assert len(week.split()) == 2 and week.split()[1][0].isdigit()  # «вт 21:47»
    old = fmt_when(ago(timedelta(days=40)), now=now)
    assert old[:2].isdigit() and "." in old  # «15.08.2026 21:47»

