"""Проверка: архив, правки, массы, право видеть все обращения."""
import pytest

import database as db
import repository as repo
from utils import as_str
from conftest import add_staff, register

STUDENT, STAFF, OTHER, SYS = "300", "200", "201", "1"


async def ticket(student=STUDENT, admin=STAFF, text="Нужна справка", status="new") -> int:
    ticket_id = await repo.create_ticket(student, admin, "feedback", text, "Справка")
    if status != "new":
        await repo.set_ticket_status(ticket_id, status)
    return ticket_id


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    await add_staff(OTHER, "Сидоров Пётр Петрович", position="Кассир")
    await repo.grant_sysadmin(SYS, "Администратор Системы")
    return env


# ── мягкое удаление ──────────────────────────────────────────────────────────
async def test_archived_ticket_disappears_from_queue(env):
    ticket_id = await ticket()
    assert [r["ticket_id"] for r in await repo.admin_tickets(None, 50)] == [ticket_id]
    await repo.archive_ticket(ticket_id, SYS)
    assert await repo.admin_tickets(None, 50) == []


async def test_archived_ticket_is_still_readable(env):
    ticket_id = await ticket()
    await repo.archive_ticket(ticket_id, SYS)
    assert await repo.get_ticket(ticket_id) is None
    assert (await repo.get_ticket(ticket_id, include_archived=True))["ticket_id"] == ticket_id


async def test_archived_list_shows_it(env):
    ticket_id = await ticket()
    await repo.archive_ticket(ticket_id, SYS)
    assert [r["ticket_id"] for r in await repo.admin_tickets(None, 50, archived=True)] == [ticket_id]
    assert await repo.archive_count() == 1


async def test_archived_ticket_keeps_thread_and_events(env):
    ticket_id = await ticket()
    await repo.add_ticket_message(ticket_id, STAFF, "staff", "Подготовим")
    await repo.archive_ticket(ticket_id, SYS)
    assert await repo.ticket_thread(ticket_id)          # переписка на месте
    events = [as_str(e["event"]) for e in await repo.ticket_events(ticket_id)]
    assert "archived" in events


async def test_restore_brings_ticket_back(env):
    ticket_id = await ticket()
    await repo.archive_ticket(ticket_id, SYS)
    ok, message = await repo.restore_ticket(ticket_id, SYS)
    assert ok and "снова в работе" in message
    assert [r["ticket_id"] for r in await repo.admin_tickets(None, 50)] == [ticket_id]
    assert await repo.archive_count() == 0


async def test_double_archive_and_double_restore_are_reported(env):
    ticket_id = await ticket()
    assert (await repo.archive_ticket(ticket_id, SYS))[0]
    assert "уже в архиве" in (await repo.archive_ticket(ticket_id, SYS))[1]
    await repo.restore_ticket(ticket_id, SYS)
    assert "и так в работе" in (await repo.restore_ticket(ticket_id, SYS))[1]


async def test_archive_of_missing_ticket(env):
    assert "не найдено" in (await repo.archive_ticket(999, SYS))[1]
    assert "не найдено" in (await repo.restore_ticket(999, SYS))[1]


async def test_hard_delete_still_available_but_archives_are_default(env):
    """Жёсткое удаление осталось для неисправимых случаев, но очередь его не ждёт."""
    ticket_id = await ticket()
    assert (await repo.archive_ticket(ticket_id, SYS))[0]
    assert await repo.delete_ticket(ticket_id)       # чистит окончательно
    assert await repo.get_ticket(ticket_id, include_archived=True) is None


# ── правки ────────────────────────────────────────────────────────────────────
async def test_update_changes_fields_and_records_them(env):
    ticket_id = await ticket()
    ok, message = await repo.update_ticket(ticket_id, SYS, text_content="Уточнил вопрос",
                                            pickup_place="115")
    assert ok and "сохранено" in message
    row = await repo.get_ticket(ticket_id)
    assert row["text_content"] == "Уточнил вопрос"
    assert row["pickup_place"] == "115"
    assert any("кабинет выдачи" in as_str(e["detail"])
               for e in await repo.ticket_events(ticket_id))


async def test_update_never_erases_fields_with_empty_value(env):
    ticket_id = await ticket()
    await repo.update_ticket(ticket_id, SYS, text_content="Нужна справка, срочно", topic="Справка")
    await repo.update_ticket(ticket_id, SYS, text_content="")
    row = await repo.get_ticket(ticket_id)
    assert row["text_content"] == "Нужна справка, срочно"
    assert row["topic"] == "Справка"


async def test_update_reports_no_changes(env):
    ticket_id = await ticket()
    ok, message = await repo.update_ticket(ticket_id, SYS, text_content="Нужна справка")
    assert ok and message == "Изменений не было"


async def test_cannot_edit_archived_ticket(env):
    ticket_id = await ticket()
    await repo.archive_ticket(ticket_id, SYS)
    ok, message = await repo.update_ticket(ticket_id, SYS, text_content="Правка")
    assert not ok and "архиве" in message


async def test_update_missing_ticket(env):
    assert "не найдено" in (await repo.update_ticket(999, SYS, text_content="x"))[1]


# ── массовые действия ─────────────────────────────────────────────────────────
async def test_bulk_assign(env):
    first, second = await ticket(admin=STAFF), await ticket(admin=OTHER)
    done, message = await repo.bulk_update([first, second], "assign", STAFF, SYS)
    assert done == 2
    for ticket_id in (first, second):
        assert (await repo.get_ticket(ticket_id))["target_admin_id"] == STAFF


async def test_bulk_status_and_pickup(env):
    first, second = await ticket(), await ticket()
    await repo.bulk_update([first, second], "status", "in_progress", SYS)
    await repo.bulk_update([first], "pickup", "115", SYS)
    assert (await repo.get_ticket(first))["status"] == "in_progress"
    assert (await repo.get_ticket(first))["pickup_place"] == "115"
    assert (await repo.get_ticket(second))["status"] == "in_progress"


async def test_bulk_archive(env):
    first, second = await ticket(), await ticket()
    done, _ = await repo.bulk_update([first, second], "archive", "", SYS)
    assert done == 2
    assert await repo.admin_tickets(None, 50) == []
    assert await repo.archive_count() == 2


async def test_bulk_skips_garbage_ids(env):
    ticket_id = await ticket()
    done, _ = await repo.bulk_update([ticket_id, "мусор", None], "status", "in_progress", SYS)
    assert done == 1


async def test_bulk_rejects_unknown_action(env):
    done, message = await repo.bulk_update([1], "выстрелить", "", SYS)
    assert done == 0 and "Неизвестное действие" in message


# ── право видеть чужие обращения ─────────────────────────────────────────────
async def test_staff_sees_only_own_by_default(env):
    await ticket(admin=OTHER)
    assert await repo.admin_tickets(STAFF, 50) == []
    assert len(await repo.admin_tickets(OTHER, 50)) == 1


async def test_granted_staff_sees_all(env):
    await ticket(admin=OTHER)
    assert not await repo.staff_sees_all(STAFF)
    await repo.set_staff_see_all(STAFF, True)
    assert await repo.staff_sees_all(STAFF)
    await repo.set_staff_see_all(STAFF, False)
    assert not await repo.staff_sees_all(STAFF)


async def test_sysadmin_always_sees_all(env):
    assert await repo.staff_sees_all(SYS)


async def test_unknown_user_sees_nothing(env):
    assert not await repo.staff_sees_all("999")


async def test_see_all_flag_persists_in_db(env):
    await repo.set_staff_see_all(STAFF, True)
    row = await db.one("SELECT see_all_tickets FROM admins WHERE user_id=?", (STAFF,))
    assert int(row["see_all_tickets"]) == 1
