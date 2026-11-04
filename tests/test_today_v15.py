"""Календарная статистика «Сегодня» и безопасные последние действия."""
from datetime import datetime

import clock
import database as db
import repository as repo
from conftest import add_staff, login_panel, register


async def ticket_at(created: str, status: str = "new") -> int:
    await register("100", "Иванов Иван", "ис-21")
    await add_staff("200", "Петрова Анна", "all")
    ticket_id = await repo.create_ticket("100", "200", "feedback", "Тест")
    await db.run("UPDATE tickets SET created_at=?, updated_at=?, status=? WHERE ticket_id=?",
                 (created, created, status, ticket_id))
    await db.run("UPDATE ticket_messages SET created_at=? WHERE ticket_id=?", (created, ticket_id))
    return ticket_id


async def test_today_starts_at_local_midnight(env, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 3, 1, 0, 0))
    yesterday = await ticket_at("2026-11-02 23:30:00")
    today = await ticket_at("2026-11-03 00:15:00")
    summary = await repo.admin_today()
    assert summary["created_day"] == summary["tickets_day"] == 1
    assert [row["hour"] for row in summary["hourly"]] == ["00"]
    assert yesterday != today


async def test_today_counts_taken_answered_and_closed(env, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 3, 12, 0, 0))
    ticket_id = await ticket_at("2026-11-03 08:00:00")
    await db.run("INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) "
                 "VALUES(?, '200', 'status', 'accepted', '2026-11-03 08:10:00')", (ticket_id,))
    await db.run("INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text, created_at) "
                 "VALUES(?, '200', 'staff', 'Ответ', '2026-11-03 08:15:00')", (ticket_id,))
    await db.run("INSERT INTO ticket_events(ticket_id, actor_id, event, detail, created_at) "
                 "VALUES(?, '200', 'status', 'completed', '2026-11-03 08:30:00')", (ticket_id,))
    summary = await repo.admin_today()
    assert (summary["taken_day"], summary["answered_day"], summary["closed_day"]) == (1, 1, 1)
    assert summary["avg_reply"] == "15 мин"


async def test_operational_counts_ignore_archive(env):
    new = await ticket_at(clock.stamp(), "new")
    ready = await ticket_at(clock.stamp(), "ready")
    await repo.archive_ticket(new, "1")
    await repo.archive_ticket(ready, "1")
    summary = await repo.admin_today()
    assert summary["no_answer"] == 0 and summary["ready_not_picked"] == 0


async def test_overview_shows_safe_recent_audit(panel_client, env):
    await repo.log_audit("100", "student", "message_received", "ticket", "9", "ok")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert "Последние действия" in body and "/panel/audit" in body
    assert "message_received" in body and "ticket" in body
    assert "Переписка-секрет" not in body
