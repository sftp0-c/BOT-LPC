"""Сценарии заявок: выбор темы, статусы, готовность и выдача."""
from datetime import datetime

import pytest

import database as db
import repository
from conftest import add_staff, press, register, say
from handlers import tickets

STUDENT, STAFF, STAFF2, OTHER = "100", "200", "201", "300"


class FakeRepo:
    def __init__(self):
        self.created: list[dict] = []
        self.ready_calls: list[tuple] = []
        self.extra: dict[int, dict] = {}

    def __getattr__(self, name):
        return getattr(repository, name)

    async def get_ticket(self, ticket_id):
        row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))
        if not row:
            return None
        t = {key: row[key] for key in row.keys()}
        t.setdefault("topic", "")
        t["student"] = await repository.get_user(t["student_id"])
        t["staff"] = await repository.get_admin(t["target_admin_id"])
        for key, value in self.extra.get(t["ticket_id"], {}).items():
            t[key] = value
        return t

    async def create_ticket(self, student_id, admin_id, category, text, topic=""):
        tid = await db.run(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content, topic) VALUES(?,?,?,?,?)",
            (student_id, admin_id, category, text, topic),
        )
        await db.run(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (tid, student_id, "student", text),
        )
        self.created.append({"student_id": student_id, "admin_id": admin_id,
                             "category": category, "text": text, "topic": topic})
        return tid

    async def add_ticket_message(self, ticket_id, sender_id, role, text, new_status=None):
        await db.run(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, sender_id, role, text),
        )
        await repository.log_ticket_event(
            ticket_id, sender_id, "message_student" if role == "student" else "message_staff", text
        )
        if new_status:
            await self.set_ticket_status(ticket_id, new_status, actor_id=sender_id)

    async def set_ticket_status(self, ticket_id, status, actor_id=""):
        await db.run("UPDATE tickets SET status=? WHERE ticket_id=?", (status, ticket_id))
        if actor_id:
            await repository.log_ticket_event(ticket_id, actor_id, "status", status)

    async def set_ticket_ready(self, ticket_id, ready_until, pickup_place="", doc_url="", actor_id=""):
        self.ready_calls.append((ticket_id, ready_until, pickup_place, doc_url))
        await self.set_ticket_status(ticket_id, "ready", actor_id=actor_id)
        self.extra[int(ticket_id)] = {"ready_until": ready_until,
                                      "pickup_place": pickup_place, "doc_url": doc_url}


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(tickets, "repo", fake)
    return fake


async def make_ticket(
    api,
    cat="feedback",
    topic_code="",
    text="Нужна справка",
    staff_id=STAFF,
    office="каб. 204",
    role="",
):
    await register(STUDENT)
    await add_staff(staff_id, "Петрова Анна", cat)
    if office or role:
        await repository.set_admin_profile(staff_id, office=office, role=role)
    await press(STUDENT, f"new:{cat}")
    if topic_code:
        await press(STUDENT, f"topic:{cat}:{topic_code}")
    await press(STUDENT, f"pick:{cat}:{staff_id}" + (f":{topic_code}" if topic_code else ""))
    await say(STUDENT, text)
    return (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]


async def test_academic_ticket_picks_topic_and_staff(api, fake_repo):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "academic")

    await press(STUDENT, "new:academic")
    assert "topic:academic:study" in api.payloads(STUDENT)
    assert "topic:academic:period" in api.payloads(STUDENT)
    assert "topic:academic:vacancies" in api.payloads(STUDENT)

    await press(STUDENT, "topic:academic:study")
    st = await db.get_state(STUDENT)
    assert st["state"] == "ticket" and st["payload"] == {"cat": "academic", "topic": "Учёба и оценки"}
    assert f"pick:academic:{STAFF}:study" in api.payloads(STUDENT)

    await press(STUDENT, f"pick:academic:{STAFF}:study")
    st = await db.get_state(STUDENT)
    assert st["payload"] == {"admin": STAFF, "cat": "academic", "topic": "Учёба и оценки"}


async def test_unknown_topic_is_ignored(api, fake_repo):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "academic")
    api.sent.clear()
    await press(STUDENT, "topic:academic:hacking")
    assert not api.to(STUDENT)
    assert await db.get_state(STUDENT) is None

    await press(STUDENT, "topic:feedback:study")
    assert not api.to(STUDENT)
    assert await db.get_state(STUDENT) is None


async def test_accounting_ticket_saves_topic_in_insert(api, fake_repo):
    tid = await make_ticket(api, cat="accounting", topic_code="scholarship", text="Когда стипендия?")
    row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (tid,))
    assert row["category"] == "accounting" and row["topic"] == "Стипендия и выплаты"
    assert fake_repo.created[0]["topic"] == "Стипендия и выплаты"

    notice = api.last(STAFF)
    assert "[Тип]" in notice[1] and "[Тема] Стипендия и выплаты" in notice[1]
    assert "Когда стипендия?" in notice[1]

    confirm = api.last(STUDENT)
    assert f"№{tid}" in confirm[1] and "Стипендия и выплаты" in confirm[1] and "Когда стипендия?" in confirm[1]

    await press(STAFF, f"t:{tid}")
    card = api.last(STAFF)[1]
    assert "[Тип]" in card and "[Тема] Стипендия и выплаты" in card


async def test_feedback_ticket_has_no_topic(api, fake_repo):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "feedback")
    api.sent.clear()
    await press(STUDENT, "new:feedback")
    assert f"pick:feedback:{STAFF}" in api.payloads(STUDENT)  # без тем — сразу выбор сотрудника
    assert not [p for p in api.payloads(STUDENT) if p.startswith("topic:")]

    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Не работает электронный журнал")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]
    row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (tid,))
    assert row["topic"] == "" and row["category"] == "feedback"
    assert "[Тема]" not in api.last(STAFF)[1]
    assert "[Тип]" in api.last(STAFF)[1]
    assert "Не работает электронный журнал" in api.last(STUDENT)[1]


async def test_staff_reply_accepts_new_ticket(api, fake_repo):
    tid = await make_ticket(api)
    assert f"st:{tid}:accepted" in api.payloads(STAFF)
    assert f"st:{tid}:in_progress" not in api.payloads(STAFF)

    await press(STAFF, f"rp:{tid}")
    await say(STAFF, "Проверим, ответим сегодня")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "accepted"
    assert "Проверим, ответим сегодня" in api.last(STUDENT)[1]

    await press(STAFF, f"t:{tid}")
    assert f"st:{tid}:ready" in api.payloads(STAFF)  # принятую можно сделать готовой


async def test_legacy_in_progress_reply_becomes_accepted(api, fake_repo):
    tid = await make_ticket(api)
    await db.run("UPDATE tickets SET status='in_progress' WHERE ticket_id=?", (tid,))
    await press(STAFF, f"rp:{tid}")
    await say(STAFF, "Уточним")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "accepted"


async def test_legacy_in_progress_callback_becomes_accepted(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:in_progress")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "accepted"


async def test_invalid_status_transition_is_rejected(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:ready")
    assert not [p for p in api.payloads(STAFF) if p.startswith(f"rt:{tid}:")]
    assert not fake_repo.ready_calls
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "new"

    await press(STAFF, f"st:{tid}:rejected")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"st:{tid}:rejected")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "rejected"
    assert not fake_repo.ready_calls


async def test_staff_picker_shows_role(api, fake_repo):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "certificates")
    await repository.set_admin_profile(STAFF, role="director", office="каб. 204")
    await press(STUDENT, "new:certificates")
    buttons = [b for row in (api.last(STUDENT)[2] or []) for b in row]
    assert any("Петрова Анна" in b["text"] and "Директор" in b["text"] for b in buttons)


async def test_ready_quick_choice_sets_status_and_notifies(api, fake_repo):
    tid = await make_ticket(api, role="director")
    await press(STAFF, f"rp:{tid}")
    await say(STAFF, "Готовим")

    await press(STAFF, f"st:{tid}:ready")
    assert f"rt:{tid}:today" in api.payloads(STAFF)
    assert f"rt:{tid}:tomorrow" in api.payloads(STAFF)
    assert f"rt:{tid}:custom" in api.payloads(STAFF)

    api.sent.clear()
    await press(STAFF, f"rt:{tid}:tomorrow")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "ready"
    when = fake_repo.extra[tid]["ready_until"]
    assert "18:00" in when
    notice = [m for m in api.to(STUDENT) if "Заявка готова" in m[1]][-1]
    assert f"№{tid}" in notice[1] and when in notice[1]
    assert "Петрова Анна" in notice[1] and "Директор" in notice[1] and "Должность" in notice[1]
    assert "каб. 204" in notice[1]
    assert "Где забрать" in notice[1]
    assert f"st:{tid}:ready" not in api.payloads(STAFF)  # готовой заявке кнопка готовности не нужна


async def test_ready_custom_time_is_saved(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:custom")
    st = await db.get_state(STAFF)
    assert st["state"] == "ready_time" and st["payload"]["tid"] == tid

    await say(STAFF, "в четверг после 15:00")
    assert fake_repo.extra[tid]["ready_until"] == "в четверг после 15:00"
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "ready"
    assert "в четверг после 15:00" in [m for m in api.to(STUDENT) if "Заявка готова" in m[1]][-1][1]


async def test_ready_custom_time_is_truncated(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:custom")
    await say(STAFF, "п " * 90)
    assert len(fake_repo.extra[tid]["ready_until"]) == tickets.READY_MAX


async def test_ready_pickup_place_falls_back_to_staff_office(api, fake_repo, monkeypatch):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", "certificates")
    await db.run("ALTER TABLE admins ADD COLUMN office TEXT NOT NULL DEFAULT ''")
    await db.run("UPDATE admins SET office='каб. 204' WHERE user_id=?", (STAFF,))
    await press(STUDENT, f"pick:certificates:{STAFF}")
    await say(STUDENT, "Нужна справка")
    tid = (await db.one("SELECT ticket_id FROM tickets"))["ticket_id"]

    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:today")
    assert fake_repo.ready_calls[-1][2] == "каб. 204"
    assert "каб. 204" in [m for m in api.to(STUDENT) if "Заявка готова" in m[1]][-1][1]


async def test_ready_without_office_is_blocked(api, fake_repo):
    tid = await make_ticket(api, office="")
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    assert "Сначала укажите кабинет в карточке сотрудника" in api.last(STAFF)[1]
    assert not [p for p in api.payloads(STAFF) if p.startswith(f"rt:{tid}:")]
    assert "Сначала укажите кабинет в карточке сотрудника" in api.last("1")[1]
    assert not fake_repo.ready_calls
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "accepted"

    await press(STAFF, f"rt:{tid}:today")
    assert not fake_repo.ready_calls


async def test_ready_adds_document_link(api, fake_repo):
    tid = await make_ticket(api)
    fake_repo.extra[tid] = {"doc_url": "https://college.example/doc.pdf"}
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:today")
    notice = [m for m in api.to(STUDENT) if "Заявка готова" in m[1]][-1]
    assert "https://college.example/doc.pdf" in notice[1]
    assert any(b["type"] == "link" and b["url"] == "https://college.example/doc.pdf"
               for row in (notice[2] or []) for b in row)


async def test_card_works_without_joined_people(api, monkeypatch, fake_repo):
    tid = await make_ticket(api)
    row = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (tid,))

    async def plain(_tid):
        return {key: row[key] for key in row.keys()}

    monkeypatch.setattr(fake_repo, "get_ticket", plain)
    await press(STAFF, f"t:{tid}")
    card = api.last(STAFF)[1]
    assert "Иванов Иван Иванович" in card and "Петрова Анна" in card and f"№{tid}" in card


async def test_ready_twice_does_not_notify_again(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:today")
    api.sent.clear()
    await press(STAFF, f"st:{tid}:ready")
    assert "уже готова" in api.last(STAFF)[1]
    assert not [m for m in api.to(STUDENT) if "Заявка готова" in m[1]]


async def test_ready_is_not_offered_for_closed_ticket(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:rejected")
    assert f"st:{tid}:ready" not in api.payloads(STAFF)
    assert f"st:{tid}:accepted" in api.payloads(STAFF)

    await press(STAFF, f"st:{tid}:ready")
    assert "закрыта" in api.last(STAFF)[1]
    assert not fake_repo.ready_calls


async def test_closed_ticket_can_be_returned_to_accepted(api, fake_repo):
    tid = await make_ticket(api)
    await press(STAFF, f"st:{tid}:rejected")
    await press(STAFF, f"st:{tid}:accepted")
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "accepted"
    assert f"st:{tid}:ready" in api.payloads(STAFF)


async def test_foreign_staff_cannot_manage_ticket(api, fake_repo):
    tid = await make_ticket(api)
    await add_staff(STAFF2, "Козлов Иван", "certificates")
    await register(OTHER, "Сидоров Пётр", "ис-22")

    for payload in (f"t:{tid}", f"st:{tid}:accepted", f"st:{tid}:ready", f"rt:{tid}:today", f"rp:{tid}"):
        api.sent.clear()
        await press(STAFF2, payload)
        if payload.startswith(("t:", "rp:")):
            assert "не найдено" in api.last(STAFF2)[1]
        else:
            assert not api.to(STAFF2)
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "new"
    assert not fake_repo.ready_calls

    api.sent.clear()
    await press(OTHER, f"st:{tid}:rejected")
    assert not api.to(OTHER)
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=?", (tid,)))["status"] == "new"


async def test_student_cannot_press_ready_button(api, fake_repo):
    tid = await make_ticket(api)
    await press(STUDENT, f"st:{tid}:ready")
    assert "Заявка готова" not in api.last(STUDENT)[1]
    assert not fake_repo.ready_calls


def test_ready_deadline_rolls_over(monkeypatch):
    class Late(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 25, 17, 30)

    class Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 25, 10, 0)

    monkeypatch.setattr(tickets, "datetime", Noon)
    assert tickets.ready_deadline("today") == "25.09.2026 до 18:00"
    assert tickets.ready_deadline("tomorrow") == "26.09.2026 до 18:00"

    monkeypatch.setattr(tickets, "datetime", Late)
    assert tickets.ready_deadline("today") == "26.09.2026 до 18:00"
    assert tickets.ready_deadline("tomorrow") == "26.09.2026 до 18:00"
