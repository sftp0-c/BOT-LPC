"""Расширенный Ctrl+K: защищённый поиск данных и корректная клавиатура."""
import repository as repo
from conftest import add_staff, csrf_of, login_panel, register


async def seed_search_data():
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_staff("200", "Петрова Анна", "all", position="Секретарь")
    ticket = await repo.create_ticket("100", "200", "feedback", "Секретный текст", "Справка")
    await repo.upsert_group("ис-21", title="Информационные системы")
    return ticket


async def test_quick_search_requires_login_and_csrf(panel_client, env):
    assert panel_client.post("/panel/search/quick", data={"q": "Иванов"},
                             follow_redirects=False).status_code == 303
    assert login_panel(panel_client)
    assert panel_client.post("/panel/search/quick", data={"q": "Иванов"}).status_code == 403


async def test_quick_search_finds_ticket_person_staff_and_group(panel_client, env):
    ticket = await seed_search_data()
    assert login_panel(panel_client)

    def search(query):
        return panel_client.post("/panel/search/quick", data={
            "q": query, "csrf": csrf_of(panel_client),
        }).json()["results"]

    by_ticket = search(str(ticket))
    assert any(row["href"] == f"/panel/tickets?t={ticket}" for row in by_ticket)
    assert "Секретный текст" not in str(by_ticket)
    assert any(row["href"] == "/panel/people/100" for row in search("Иванов"))
    assert any(row["href"] == "/panel/staff/200" for row in search("Петрова"))
    assert any(row["kind"] == "Группы" and "ИС-21" in row["label"] for row in search("ИС-21"))


def test_palette_script_uses_post_csrf_loading_and_distinct_openers(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    for marker in ('fetch("/panel/search/quick"', 'method: "POST"', 'body.set("csrf"',
                   'class="skel skel-line"', "function openRow(", "function openPalette("):
        assert marker in body
    assert "function open()" not in body
    assert "openRow(picked())" in body
    assert "data-palette-open" in body


def test_search_query_is_not_submitted_in_url(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert 'action="/panel/search"' not in body
    assert 'method="get"' not in body.split('<header class="app-header">', 1)[1].split("</header>", 1)[0]



async def test_quick_search_action_is_audited(panel_client, env):
    assert login_panel(panel_client)
    panel_client.post("/panel/search/quick", data={"q": "Иванов", "csrf": csrf_of(panel_client)})
    actions = [row["action"] for row in await repo.audit_entries(20)]
    assert "quick_search" in actions
