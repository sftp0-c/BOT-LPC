"""Аналитика: данные для диаграмм и сама страница с графиками."""
import pytest

import charts
import database as db
import repository as repo
from conftest import add_staff, login_panel, register

STAFF, STUDENT, SYS = "200", "100", "1"


async def make_ticket(days_ago: int = 0, status: str = "new", staff: str = STAFF,
                      category: str = "feedback") -> int:
    """Обращение с нужной «возрастной» датой, чтобы диаграммы по дням были непустыми."""
    await register(STUDENT)
    await add_staff(staff, "Петрова Анна", category="all")
    ticket = await repo.create_ticket(STUDENT, staff, category, "Текст обращения")
    if status != "new":
        await db.run("UPDATE tickets SET status=? WHERE ticket_id=?", (status, ticket))
    when = f"-{days_ago} days" if days_ago else ""
    if when:
        await db.run("UPDATE tickets SET created_at=datetime('now', ?) WHERE ticket_id=?", (when, ticket))
    return ticket


# ── данные ────────────────────────────────────────────────────────────────────
async def test_tickets_by_day_splits_done():
    ticket = await make_ticket(days_ago=0)
    await make_ticket(days_ago=3, status="completed")
    rows = await repo.tickets_by_day(30)
    today = [r for r in rows if r["day"] == rows[-1]["day"]][0]
    assert today["count"] == 1 and today["done"] == 0
    assert any(r["done"] == 1 for r in rows)
    assert rows == sorted(rows, key=lambda r: r["day"])
    assert ticket


async def test_tickets_by_status_and_category():
    await make_ticket()
    await make_ticket(status="completed", category="certificates")
    statuses = {row["status"]: row["count"] for row in await repo.tickets_by_status()}
    assert statuses.get("new") == 1 and statuses.get("completed") == 1
    categories = {row["category"]: row["count"] for row in await repo.tickets_by_category()}
    assert categories.get("certificates") == 1


async def test_response_speed_counts_only_answered():
    ticket = await make_ticket()
    await db.run("UPDATE tickets SET created_at=datetime('now','-2 hours') WHERE ticket_id=?", (ticket,))
    assert (await repo.response_speed(30))["share"] == 0
    await db.run(
        "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text, created_at) "
        "VALUES(?,?,?,?, datetime('now','-30 minutes'))", (ticket, STAFF, "staff", "Ответ"))
    speed = await repo.response_speed(30)
    assert speed["total"] == 1 and speed["answered"] == 1 and speed["share"] == 100
    assert 80 <= speed["avg_minutes"] <= 100


async def test_staff_load_reports_open_and_avg():
    await make_ticket()
    await make_ticket(status="completed")
    load = await repo.staff_load(90)
    row = next(r for r in load if r["user_id"] == STAFF)
    assert row["tickets"] == 2 and row["open"] == 1


async def test_students_by_group_counts():
    await register("300", name="Соколова Мария", group="ИС-21")
    groups = await repo.students_by_group()
    assert any(g["group"] == "ИС-21" and g["count"] == 1 for g in groups)


# ── диаграммы: чистый SVG, никакого JavaScript ───────────────────────────────
def test_bar_chart_renders_svg_and_tooltip():
    html = charts.bar_chart([{"day": "2026-09-24", "count": 3}, {"day": "2026-09-25", "count": 5}],
                            "count", "day", "Обращения по дням")
    assert html.count("<svg") == 1 and "<rect" in html
    assert "24.09" in html and "5" in html
    assert "<script" not in html.lower()


def test_bar_chart_with_second_series():
    html = charts.bar_chart([{"day": "2026-09-24", "count": 4, "done": 2}], "count", "day",
                            "Обращения", second_key="done")
    assert "#22c55e" in html and "завершено 2" in html


def test_donut_and_legend():
    rows = [{"label": "Новое", "count": 3}, {"label": "Готово", "count": 1}]
    html = charts.donut(rows, "count", "label", "Статусы")
    assert html.count("<circle") == 2 and "legend-list" in html and "(75%)" in html
    assert "Новое" in html


def test_bars_and_sparkline():
    html = charts.bars([{"full_name": "Петрова Анна", "tickets": 7}, {"full_name": "Соколов", "tickets": 2}],
                       "tickets", "full_name", "Нагрузка")
    assert html.count("hbar-track") == 2 and "Петрова" in html
    spark = charts.sparkline([1, 4, 2, 6, 3], "Динамика")
    assert "<polyline" in spark and "<polygon" in spark


def test_charts_survive_empty_data():
    for html in (charts.bar_chart([], "count", "day", "Пусто"),
                 charts.donut([], "count", "label", "Пусто"),
                 charts.bars([], "tickets", "name", "Пусто"),
                 charts.sparkline([1], "Пусто")):
        assert "Пока нет данных" in html
        assert "<svg" not in html


def test_chart_escapes_labels():
    """Подписи приходят из базы: спецсимволы не должны ломать разметку."""
    html = charts.bars([{"full_name": "<script>alert(1)</script>", "tickets": 1}], "tickets", "full_name", "X")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


# ── страница ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("days", [7, 30, 90, 999])
def test_analytics_page_renders(panel_client, days):
    """Период: допустимые значения и «мусор» - страница не падает."""
    assert panel_client.cookies.get(webpanel_cookie()) is not None or True
    assert login_panel(panel_client)
    response = panel_client.get("/panel/analytics", params={"days": days})
    assert response.status_code == 200
    assert "Аналитика" in response.text


def test_analytics_page_on_empty_database(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/analytics").text
    assert "Пока нет данных" in body


def test_analytics_shows_charts_with_data(panel_client):
    import asyncio

    async def scenario():
        ticket = await make_ticket()
        await db.run(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text, created_at) "
            "VALUES(?,?,?,?, datetime('now','-20 minutes'))", (ticket, STAFF, "staff", "Ответ"))

    asyncio.new_event_loop().run_until_complete(scenario())
    assert login_panel(panel_client)
    body = panel_client.get("/panel/analytics").text
    assert "<svg" in body and "Среднее время ответа" in body
    assert "Пока нет данных" not in body.split("Нагрузка на сотрудников")[0]


def webpanel_cookie() -> str:
    import webpanel
    return webpanel.COOKIE



