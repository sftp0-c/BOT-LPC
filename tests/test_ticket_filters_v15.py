"""Фильтр «Только мои», период, CSV-защита и таймер рабочего места."""
import csv
import io
from datetime import datetime
from html.parser import HTMLParser

import pytest

import clock
import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, register
from web.tickets import filter_tickets

OWNER = "46010397"
STUDENT = "100"
MINE = OWNER
OTHER = "200"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


async def seed_ticket(admin_id: str, text: str, when: str) -> int:
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    if admin_id != OWNER:
        await add_staff(admin_id, f"Сотрудник {admin_id}", "all")
    ticket_id = await repo.create_ticket(STUDENT, admin_id, "feedback", text)
    await db.run("UPDATE tickets SET created_at=?, updated_at=? WHERE ticket_id=?",
                 (when, when, ticket_id))
    await db.run("UPDATE ticket_messages SET created_at=? WHERE ticket_id=?", (when, ticket_id))
    return ticket_id


async def test_mine_filters_rows_and_csv_with_the_same_rule(owner_client, env):
    mine = await seed_ticket(MINE, "Моё обращение", clock.stamp_at(-60))
    other = await seed_ticket(OTHER, "Чужое обращение", clock.stamp_at(-60))
    rows, _states, _waiting, _overdue = await filter_tickets(mine="1", actor_id=MINE)
    assert [int(row["ticket_id"]) for row in rows] == [mine]

    page = owner_client.get("/panel/tickets", params={"mine": "1"}).text
    exported = owner_client.get("/panel/tickets.csv", params={"mine": "1"}).text
    assert f"№{mine}" in page and f"№{other}" not in page
    assert "Моё обращение" in exported and "Чужое обращение" not in exported
    assert "mine=1" in page


async def test_period_is_applied_before_screen_and_csv(owner_client, env):
    old = await seed_ticket(MINE, "Старое", "2026-10-01 10:00:00")
    current = await seed_ticket(MINE, "В периоде", "2026-11-03 10:00:00")
    params = {"date_from": "2026-11-01", "date_to": "2026-11-03"}
    rows, *_ = await filter_tickets(**params)
    assert [int(row["ticket_id"]) for row in rows] == [current]
    page = owner_client.get("/panel/tickets", params=params).text
    exported = owner_client.get("/panel/tickets.csv", params=params).text
    assert f"№{current}" in page and f"№{old}" not in page
    assert "В периоде" in exported and "Старое" not in exported
    assert "date_from=2026-11-01" in page and "date_to=2026-11-03" in page


async def test_csv_neutralizes_spreadsheet_formulas(owner_client, env):
    await seed_ticket(MINE, "=HYPERLINK(\"https://example.test\")", clock.stamp())
    response = owner_client.get("/panel/tickets.csv")
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff")), delimiter=";"))
    assert rows[1][-1].startswith("'=")


async def test_queue_has_live_context_timer(owner_client, env):
    ticket_id = await seed_ticket(MINE, "Ждёт ответа", clock.stamp_at(-95))
    body = owner_client.get("/panel/tickets", params={"t": ticket_id}).text
    assert f"№{ticket_id}" in body

    class Timers(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "live-age" in attrs.get("class", "").split():
                self.values.append(attrs)

    timers = Timers()
    timers.feed(body)
    assert len(timers.values) == 2, "таймер есть и в очереди, и в карточке"
    for timer in timers.values:
        assert timer["data-prefix"] == "ждёт ответа"
        assert timer["data-timer-kind"] == "waiting"
        moment = datetime.fromisoformat(timer["data-time"])
        assert moment.tzinfo is not None
        assert int(moment.timestamp() * 1000) == int(timer["data-time-ms"])


def test_workbench_filter_state_covers_every_parameter(owner_client):
    body = owner_client.get("/panel/tickets").text
    assert 'data-filters="status,category,q,scope,view,mine,date_from,date_to"' in body
    assert 'data-filter-key="tickets:46010397"' in body
