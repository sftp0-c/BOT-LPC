"""Временная конвенция на уровне данных: база живёт по локальному времени.

Раньше время в базу писал сам SQLite (datetime('now') — это UTC), поэтому в
контейнере с часовым поясом UTC все даты отставали на 5 часов от
екатеринбургских. Теперь «сейчас» берётся из clock, и эти тесты не дают
вернуться к старому: новая запись локальная, а уже записанное время
переводится ровно один раз.
"""
from datetime import datetime, timedelta, timezone

import pytest

import clock
import config
import database as db
import repository as repo

# Сдвиг старой конвенции (UTC) к новой: Екатеринбург, перевода часов нет.
SHIFT_MINUTES = 300
# Время, заведённое до перехода: то, что раньше писал SQLite само.
LEGACY = "2026-09-20 10:00:00"
LEGACY_LATER = "2026-09-20 18:30:00"


def local(stamp: str) -> datetime:
    """Строка из базы, разобранная как локальное время."""
    return clock.parse(stamp)


def utc_now() -> datetime:
    """Настоящее UTC: строки в старой конвенции были именно такими."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_fresh(stamp: str) -> bool:
    """Похоже ли записанное время на «только что»."""
    moment = local(stamp)
    return moment is not None and abs(moment - clock.now()) < timedelta(minutes=1)


# ── новая запись ──────────────────────────────────────────────────────────────
async def test_new_ticket_is_written_in_local_time(env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.add_staff("200", "Петрова Анна")

    ticket_id = await repo.create_ticket("100", "200", "feedback", "Текст", topic="Расписание")
    ticket = await repo.get_ticket(ticket_id)

    assert is_fresh(ticket["created_at"])
    assert is_fresh(ticket["updated_at"])
    # это локальное время, а не UTC: в UTC было бы на 5 часов раньше
    assert abs(local(ticket["created_at"]) - utc_now()) > timedelta(hours=4)
    # и вся цепочка записи обращения: переписка и события
    assert is_fresh((await repo.ticket_thread(ticket_id))[0]["created_at"])
    assert is_fresh((await repo.ticket_events(ticket_id))[0]["created_at"])


async def test_registration_staff_and_logs_are_written_in_local_time(env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.add_staff("200", "Петрова Анна")
    await repo.log_action("1", "событие", "подробности")
    await repo.add_template("Ответ", "Текст ответа")

    for sql in ("SELECT created_at FROM users WHERE user_id='100'",
                "SELECT created_at FROM admins WHERE user_id='200'",
                "SELECT created_at FROM admin_log",
                "SELECT created_at FROM reply_templates"):
        assert is_fresh((await db.one(sql))["created_at"]), sql


async def test_state_and_contact_are_written_in_local_time(env):
    await db.set_state("100", "reg_name", {"step": 1})
    await repo.touch_contact("100", "ivan", "Иван")

    state = await db.one("SELECT created_at FROM user_states WHERE user_id=?", ("100",))
    contact = await db.one("SELECT first_seen, last_seen FROM contacts WHERE user_id=?", ("100",))
    assert is_fresh(state["created_at"])
    assert is_fresh(contact["first_seen"])
    assert is_fresh(contact["last_seen"])


# ── миграция ──────────────────────────────────────────────────────────────────
async def seed_legacy_time(env) -> int:
    """Записи, заведённые по старой конвенции: время в них UTC."""
    await db.run("UPDATE admins SET created_at=? WHERE user_id='1'", (LEGACY,))
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await db.run("UPDATE users SET created_at=?, consent_at=? WHERE user_id='100'",
                 (LEGACY, LEGACY))
    ticket_id = await repo.create_ticket("100", "200", "feedback", "Текст", topic="Расписание")
    await db.run("UPDATE tickets SET created_at=?, updated_at=?, ready_until=? WHERE ticket_id=?",
                 (LEGACY, LEGACY, "сегодня до 18:00", ticket_id))
    await db.run("UPDATE ticket_events SET created_at=? WHERE ticket_id=?", (LEGACY, ticket_id))
    await db.run("UPDATE ticket_messages SET created_at=? WHERE ticket_id=?", (LEGACY, ticket_id))
    await repo.touch_contact("100", "ivan", "Иван")
    await db.run("UPDATE contacts SET first_seen=?, last_seen=? WHERE user_id='100'",
                 (LEGACY, LEGACY_LATER))
    await db.run("INSERT INTO admin_log(actor_id, action, created_at) VALUES('1', 'тест', ?)",
                 (LEGACY,))
    await db.run("INSERT INTO broadcasts(sender_id, audience, text, created_at) "
                 "VALUES('1', 'all', 'текст', ?)", (LEGACY,))
    await db.run("INSERT INTO staff_invites(code, created_at, expires_at) VALUES('LEGACY', ?, ?)",
                 (LEGACY, LEGACY_LATER))
    await db.run("INSERT INTO faq(question, created_at, updated_at) VALUES('Вопрос?', ?, ?)",
                 (LEGACY, LEGACY))
    await db.run("INSERT INTO staff_requests(user_id, created_at, updated_at) "
                 "VALUES('300', ?, ?)", (LEGACY, LEGACY))
    await db.run("INSERT INTO schedules(group_code, pdf_url, updated_at, parsed_at) "
                 "VALUES('ИС-21', 'https://college.example/is-21.pdf', ?, ?)", (LEGACY, LEGACY))
    # пустая строка — не дата, её сдвигать нельзя
    await db.set_state("100", "old")
    await db.run("UPDATE user_states SET created_at='' WHERE user_id='100'")
    # справочник групп в миграцию не входит
    await repo.upsert_group("ИС-21")
    await db.run("UPDATE groups SET created_at=? WHERE group_code='ИС-21'", (LEGACY,))
    # отметка о миграции снята: база выглядит как до перехода
    await db.run("DELETE FROM settings WHERE key=?", (db.TZ_MIGRATION_KEY,))
    return ticket_id


async def snapshot(env) -> dict:
    """{колонка базы: {rowid: значение}} по всем колонкам из TIME_COLUMNS."""
    data: dict = {}
    for table, column in db.TIME_COLUMNS:
        rows = await db.many(f'SELECT rowid AS _rid, "{column}" AS _value FROM "{table}" '
                             f'WHERE "{column}" <> \'\'')
        data[(table, column)] = {int(row["_rid"]): row["_value"] for row in rows}
    return data


async def read_stamps(env) -> dict:
    """Все проверяемые значения времени — обычными словарями."""
    queries = {
        "admin": "SELECT created_at FROM admins WHERE user_id='1'",
        "user": "SELECT created_at, consent_at FROM users WHERE user_id='100'",
        "ticket": "SELECT created_at, updated_at, ready_until FROM tickets",
        "event": "SELECT created_at FROM ticket_events",
        "message": "SELECT created_at FROM ticket_messages",
        "contact": "SELECT first_seen, last_seen FROM contacts WHERE user_id='100'",
        "log": "SELECT created_at FROM admin_log",
        "broadcast": "SELECT created_at FROM broadcasts",
        "invite": "SELECT created_at, expires_at FROM staff_invites",
        "faq": "SELECT created_at, updated_at FROM faq",
        "request": "SELECT created_at, updated_at FROM staff_requests",
        "schedule": "SELECT parsed_at, updated_at FROM schedules",
        "state": "SELECT created_at FROM user_states WHERE user_id='100'",
        "group": "SELECT created_at FROM groups WHERE group_code='ИС-21'",
    }
    return {name: dict(await db.one(sql)) for name, sql in queries.items()}


async def test_migrate_timezone_shifts_old_values_once(env):
    await seed_legacy_time(env)

    before = await snapshot(env)
    moved = await db.migrate_timezone()
    after = await snapshot(env)

    # сдвинулось ровно то, что лежало в списке TIME_COLUMNS и было датой,
    # и ровно на сдвиг зоны — ничего лишнего и ничего «наполовину»
    changed = [(column, rid) for column in before for rid in before[column]
               if after[column].get(rid) != before[column][rid]]
    assert moved == len(changed) > 0
    for column, rid in changed:
        assert clock.looks_like_stamp(before[column][rid]), f"{column} = {before[column][rid]!r}"
        assert after[column][rid] == clock.shift(before[column][rid], SHIFT_MINUTES), column

    rows = await read_stamps(env)
    for name in ("admin", "user", "ticket", "event", "message", "log", "broadcast",
                 "invite", "faq", "request", "schedule"):
        for column, value in rows[name].items():
            # expires_at заводили отдельно, вторым временем — проверяется ниже
            if clock.looks_like_stamp(value) and column != "expires_at":
                assert value == clock.shift(LEGACY, SHIFT_MINUTES), f"{name}.{column}={value}"
    assert rows["contact"]["first_seen"] == clock.shift(LEGACY, SHIFT_MINUTES)
    assert rows["contact"]["last_seen"] == clock.shift(LEGACY_LATER, SHIFT_MINUTES)
    assert rows["invite"]["expires_at"] == clock.shift(LEGACY_LATER, SHIFT_MINUTES)
    # и теперь это правда местное время
    assert local(rows["user"]["created_at"]) == datetime(2026, 9, 20, 15, 0, 0)


async def test_migrate_timezone_touches_only_date_like_values(env):
    await seed_legacy_time(env)
    await db.migrate_timezone()

    rows = await read_stamps(env)
    assert rows["ticket"]["ready_until"] == "сегодня до 18:00"   # свободный текст
    assert rows["state"]["created_at"] == ""                     # пустая строка
    assert rows["group"]["created_at"] == LEGACY                 # справочник не в списке


async def test_migrate_timezone_is_not_applied_twice(env):
    await seed_legacy_time(env)

    await db.migrate_timezone()
    first = await read_stamps(env)

    assert await db.migrate_timezone() == 0
    assert await db.migrate_timezone() == 0
    assert await read_stamps(env) == first


async def test_migrate_timezone_runs_from_init_db(env):
    """Тот же перевод делает обычный старт бота, а не только ручной вызов."""
    await seed_legacy_time(env)

    await db.init_db()
    shifted = clock.shift(LEGACY, SHIFT_MINUTES)
    assert (await db.one("SELECT created_at FROM users WHERE user_id='100'"))["created_at"] == shifted
    assert await db.get_setting(db.TZ_MIGRATION_KEY) == str(SHIFT_MINUTES)

    # повторный старт бота данные не двигает
    await db.init_db()
    assert (await db.one("SELECT created_at FROM users WHERE user_id='100'"))["created_at"] == shifted


async def test_fresh_database_is_not_shifted_twice(env, tmp_path, monkeypatch):
    """Новая база: init_db проходит по пустым таблицам и сразу ставит отметку."""
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "fresh.db"))
    await db.init_db()
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    stamp = (await db.one("SELECT created_at FROM users WHERE user_id='100'"))["created_at"]

    await db.init_db()
    await db.migrate_timezone()

    assert (await db.one("SELECT created_at FROM users WHERE user_id='100'"))["created_at"] == stamp
    assert is_fresh(stamp)


# ── срок действия кода сотрудника ─────────────────────────────────────────────
async def test_staff_code_expiry_is_counted_in_local_time(env):
    await repo.create_invite("CODE24", ttl_hours=6)

    row = await db.one("SELECT created_at, expires_at FROM staff_invites WHERE code='CODE24'")
    # ровно на 6 часов позже выдачи — как и обещает ttl_hours
    assert abs((local(row["expires_at"]) - local(row["created_at"])) - timedelta(hours=6)) \
        < timedelta(minutes=1)
    assert abs(local(row["expires_at"]) - local(clock.stamp_at(6 * 60))) < timedelta(minutes=1)
    assert is_fresh(row["created_at"])
    assert await repo.invite_state("CODE24") == "active"


async def test_expires_at_is_compared_against_local_now(env):
    """«Код ещё жив» сравнивается с локальным временем, а не с UTC."""
    await repo.create_invite("LIVE1", ttl_hours=1)
    await db.run("UPDATE staff_invites SET expires_at=? WHERE code='LIVE1'", (clock.stamp_at(60),))
    assert await repo.invite_state("LIVE1") == "active"
    assert (await repo.use_invite("LIVE1", "300"))[0] is True

    await repo.create_invite("DEAD1", ttl_hours=1)
    await db.run("UPDATE staff_invites SET expires_at=? WHERE code='DEAD1'", (clock.stamp_at(-1),))
    assert await repo.invite_state("DEAD1") == "expired"
    ok, reason = await repo.use_invite("DEAD1", "300")
    assert ok is False and "Срок действия кода истёк" in reason
    # истёкший и использованный коды не попадают в число действующих на сводке дня
    assert (await repo.admin_today())["codes_active"] == 0


async def test_code_without_expiry_never_expires(env):
    await repo.create_invite("NEVER", ttl_hours=0)
    assert (await db.one("SELECT expires_at FROM staff_invites"))["expires_at"] == ""
    assert await repo.invite_state("NEVER") == "active"


async def test_set_invite_ttl_counts_local_hours(env):
    await repo.create_invite("TTLX", ttl_hours=0)
    await repo.set_invite_ttl("TTLX", 3)

    expires = (await db.one("SELECT expires_at FROM staff_invites WHERE code='TTLX'"))["expires_at"]
    assert abs(local(expires) - local(clock.stamp_at(3 * 60))) < timedelta(minutes=1)
    assert await repo.invite_state("TTLX") == "active"


# ── фильтры «прошло времени» ──────────────────────────────────────────────────
async def test_date_filters_use_local_now(env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.add_staff("200", "Петрова Анна")
    ticket_id = await repo.create_ticket("100", "200", "feedback", "Текст")

    # обращение создано «сейчас» — попадает и в сутки, и в неделю
    assert (await repo.admin_today())["tickets_day"] == 1
    assert (await repo.stats_overview())["week"] == 1
    assert [row["count"] for row in await repo.tickets_by_day(7)] == [1]
    assert (await repo.staff_activity(7)).get("200", {}).get("tickets") == 1

    # а созданное десять дней назад — уже не сегодня и не за неделю
    await db.run("UPDATE tickets SET created_at=? WHERE ticket_id=?",
                 (clock.stamp_at(-10 * 24 * 60), ticket_id))
    assert (await repo.admin_today())["tickets_day"] == 0
    assert (await repo.stats_overview())["week"] == 0
    assert await repo.tickets_by_day(7) == []
    assert await repo.staff_activity(7) == {}


async def test_prune_and_dedupe_use_local_now(env):
    await db.run("INSERT INTO processed_updates(key, created_at) VALUES('old', ?)",
                 (clock.stamp_at(-10 * 24 * 60),))
    await db.run("INSERT INTO processed_updates(key, created_at) VALUES('fresh', ?)", (clock.stamp(),))
    assert await db.mark_processed("new", ttl_hours=24) is True
    assert {row["key"] for row in await db.many("SELECT key FROM processed_updates")} == {"fresh", "new"}

    await db.set_state("100", "old")
    await db.run("UPDATE user_states SET created_at=? WHERE user_id='100'",
                 (clock.stamp_at(-10 * 24 * 60),))
    assert await db.prune("user_states", 7) == 1
    assert await db.get_state("100") is None


@pytest.mark.parametrize("days", [1, 7, 30])
async def test_admin_log_counts_recent_days_in_local_time(env, days):
    await repo.log_action("1", "событие", "подробности")
    assert await repo.admin_log_counts(days) == {"событие": 1}
