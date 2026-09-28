"""Данные для «Пульта»: лента событий и массовые проверки вместо N+1."""
from datetime import date, timedelta

import repository as repo
from conftest import add_staff, press, register, say


# ── лента событий по всем обращениям ────────────────────────────────────────
async def test_feed_is_empty_on_clean_base():
    assert await repo.recent_ticket_events() == []


async def test_feed_lists_events_of_all_tickets():
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_staff("500", "Петрова Анна", category="all")
    await press("100", "new:feedback")
    await press("100", "pick:feedback:500")
    await say("100", "Нужна справка")
    await press("100", "ticketsend")
    await press("500", "staff")
    rows = await repo.recent_ticket_events(10)
    assert rows, "лента не пустая"
    assert all(row["ticket_id"] for row in rows)
    # свежие события первыми
    ids = [int(row["id"]) for row in rows]
    assert ids == sorted(ids, reverse=True)


async def test_feed_joins_student_data():
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_staff("500", "Петрова Анна", category="all")
    await press("100", "new:feedback")
    await press("100", "pick:feedback:500")
    await say("100", "Нужна справка")
    await press("100", "ticketsend")
    row = (await repo.recent_ticket_events(1))[0]
    assert row["student_name"] == "Иванов Иван Иванович"
    assert row["student_group"] == "ИС-21"


async def test_feed_respects_limit():
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_staff("500", "Петрова Анна", category="all")
    for index in range(3):
        await press("100", "new:feedback")
        await press("100", "pick:feedback:500")
        await say("100", f"Вопрос номер {index}")
        await press("100", "ticketsend")
    assert len(await repo.recent_ticket_events(2)) == 2


async def test_feed_label_is_human():
    label = repo.event_feed_label({"ticket_id": 12, "actor_name": "Петрова Анна",
                                   "actor_id": "500", "event": "status"})
    assert "№12" in label and "Петрова Анна" in label and "статус" in label


async def test_feed_label_falls_back_to_actor_id():
    label = repo.event_feed_label({"ticket_id": 7, "actor_name": "", "actor_id": "500",
                                   "event": "created"})
    assert "500" in label and "создал" in label


async def test_feed_label_survives_empty_row():
    assert "кто-то" in repo.event_feed_label({})


# ── массовые проверки вместо N+1 ────────────────────────────────────────────
async def test_sees_all_bulk_reads_flags():
    await add_staff("500", "Петрова Анна")
    await add_staff("501", "Сидорова Мария")
    await repo.set_staff_see_all("500", True)
    flags = await repo.staff_sees_all_bulk()
    assert flags["500"] is True
    assert flags["501"] is False


async def test_sees_all_bulk_ignores_sysadmins():
    await repo.grant_sysadmin("1", "Владелец")
    flags = await repo.staff_sees_all_bulk()
    assert "1" not in flags


async def test_vacations_bulk_reads_dates():
    await add_staff("500", "Петрова Анна")
    assert await repo.vacations_bulk() == {}
    await repo.set_vacation("500", "05.10.2026")
    rows = await repo.vacations_bulk()
    assert "500" in rows
    assert rows["500"]["vacation_until"] == "2026-10-05"
    assert rows["500"]["full_name"] == "Петрова Анна"


async def test_vacations_bulk_ignores_present_staff():
    await add_staff("500", "Петрова Анна")
    await repo.set_vacation("500", (date.today() - timedelta(days=2)).isoformat())
    assert "500" not in await repo.vacations_bulk()
