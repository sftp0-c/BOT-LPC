"""Края фильтров обращений: SQL до LIMIT, безопасные URL, CSV и честное время."""
from contextlib import asynccontextmanager
import csv
from datetime import datetime, timedelta
from html.parser import HTMLParser
import io
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import clock
import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, register
from web.tickets import csv_cell, filter_tickets, is_overdue

pytestmark = pytest.mark.panel
OWNER, STAFF, STUDENT = "46010397", "200", "100"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


class Document(HTMLParser):
    """Читаем ссылки и реальные значения полей, включая выбранные пункты select."""

    def __init__(self, body):
        super().__init__()
        self.links = []
        self.forms = []
        self.timers = []
        self.age_text = []
        self._link = None
        self._form = None
        self._select = None
        self._option = None
        self._age = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._link = {**attrs, "text": ""}
            self.links.append(self._link)
        if tag == "form":
            assert self._form is None, "Вложенная форма ломает CSRF, action и return в браузере"
            self._form = {**attrs, "fields": {}, "selects": {}}
            self.forms.append(self._form)
        if tag == "input" and self._form is not None and attrs.get("name"):
            self._form["fields"].setdefault(attrs["name"], []).append(attrs.get("value", ""))
        if tag == "select" and self._form is not None and attrs.get("name"):
            self._select = {**attrs, "options": []}
            self._form["selects"][attrs["name"]] = self._select
        if tag == "option" and self._select is not None:
            self._option = {**attrs, "text": ""}
            self._select["options"].append(self._option)
        if "live-age" in attrs.get("class", "").split():
            self.timers.append(attrs)
        if tag == "span" and "ws-age" in attrs.get("class", "").split():
            self._age = ""

    def handle_data(self, text):
        if self._link is not None:
            self._link["text"] += text
        if self._option is not None:
            self._option["text"] += text
        if self._age is not None:
            self._age += text

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        if tag == "option":
            self._option = None
        if tag == "select" and self._select is not None:
            options = self._select["options"]
            selected = [option for option in options if "selected" in option] or options[:1]
            self._form["fields"].setdefault(self._select["name"], []).extend(
                option.get("value", option["text"]) for option in selected)
            self._select = None
        if tag == "form":
            self._form = None
        if tag == "span" and self._age is not None:
            self.age_text.append(self._age)
            self._age = None

    def link(self, label):
        return next(link["href"] for link in self.links if label in link["text"])

    def queue(self, ticket_id):
        return next(link["href"] for link in self.links
                    if "data-ws-ticket-link" in link and query(link["href"]).get("t") == str(ticket_id))

    def ticket_ids(self):
        return {int(query(link["href"])["t"]) for link in self.links if "data-ws-ticket-link" in link}

    def filter_form(self):
        form = next(form for form in self.forms if "data-ws-filter-form" in form)
        assert form["method"] == "get"
        assert all(len(values) == 1 for values in form["fields"].values()), "Поля фильтров не должны повторяться"
        return form, {key: values[0] for key, values in form["fields"].items()}


def query(url):
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert all(len(items) == 1 for items in values.values()), f"Повторён параметр: {url}"
    return {key: items[0] for key, items in values.items()}


def csv_rows(response):
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["cache-control"] == "no-store"
    return list(csv.reader(io.StringIO(response.text.lstrip("\ufeff")), delimiter=";"))


async def seed(text="Нужна справка", *, admin=OWNER, status="new", topic="", category="feedback",
               when="2026-11-01 10:00:00", archived=False):
    ticket_id = await repo.create_ticket(STUDENT, admin, category, text, topic)
    await db.run("UPDATE tickets SET status=?, created_at=?, updated_at=?, deleted_at=? WHERE ticket_id=?",
                 (status, when, when, when if archived else "", ticket_id))
    await db.run("UPDATE ticket_messages SET created_at=? WHERE ticket_id=?", (when, ticket_id))
    return ticket_id


async def fill_recent(count):
    """Много новых неподходящих строк поверх старой нужной; без тысяч транзакций."""
    async with db._conn() as conn:
        await conn.executemany(
            "INSERT INTO tickets(student_id,target_admin_id,category,text_content,status,created_at,updated_at) "
            "VALUES('101','200','academic','Новое лишнее','accepted','2026-11-04 11:00:00','2026-11-04 11:00:00')",
            [()] * count,
        )
        await conn.commit()


async def test_links_round_trip_and_switch_one_filter(owner_client, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 4, 12))
    needle = 'Иванов & +%_#?=" / ё'
    tid = await seed(needle, archived=True)
    params = {"status": "new", "category": "feedback", "q": needle, "scope": "overdue",
              "view": "archive", "mine": "1", "date_from": "2026-11-01", "date_to": "2026-11-03"}
    response = owner_client.get("/panel/tickets", params=params)
    assert response.status_code == 200
    doc = Document(response.text)
    canonical = params | {"tab": "closed"}
    assert query(doc.queue(tid)) == canonical | {"page": "1", "t": str(tid)}
    assert query(doc.link("Выгрузить CSV")) == canonical
    assert [int(row[0]) for row in csv_rows(owner_client.get(doc.link("Выгрузить CSV")))[1:]] == [tid]

    # Вкладка меняет вид целиком; остальные фильтры остаются без потерь.
    for tab in ("open", "waiting", "closed"):
        link = next(link for link in doc.links if link.get("data-ws-tab") == tab)
        expected = {key: val for key, val in params.items() if key not in ("status", "scope", "view")}
        assert query(link["href"]) == expected | {"tab": tab, "page": "1"}
    without_scope = {key: val for key, val in canonical.items() if key != "scope"}
    assert query(doc.link("Все сроки")) == without_scope | {"page": "1"}
    response = owner_client.get(doc.link("Все сроки"))
    assert response.status_code == 200
    assert query(Document(response.text).link("Дольше суток")) == canonical | {"page": "1"}

    card = owner_client.get(doc.queue(tid))
    assert card.status_code == 200
    assert f'id="ws-ticket-title">Обращение <span>№ {tid}</span>' in card.text
    form, controls = doc.filter_form()
    assert form.get("data-filters")
    assert controls == canonical | {"page": "1"}

    # Исполнитель и даты теперь задаются формой, а не отдельными ссылками.
    changed_mine = owner_client.get(form["action"], params=controls | {"mine": ""})
    assert changed_mine.status_code == 200
    assert query(Document(changed_mine.text).link("Выгрузить CSV")) == canonical | {"mine": "0"}
    for days in (7, 14, 30):
        dates = {"date_from": (clock.today() - timedelta(days=days - 1)).isoformat(), "date_to": "2026-11-04"}
        changed_dates = owner_client.get(form["action"], params=controls | dates)
        assert changed_dates.status_code == 200
        updated = Document(changed_dates.text)
        assert query(updated.link("Выгрузить CSV")) == canonical | dates
        assert updated.filter_form()[1] == controls | dates
        assert [int(row[0]) for row in csv_rows(owner_client.get(updated.link("Выгрузить CSV")))[1:]] == [tid]


@pytest.mark.parametrize(("mine", "expected"), [("1", "1"), ("true", "1"), (" on ", "1"),
                                               ("0", "0"), ("false", "0"), ("", "0")])
async def test_mine_normalizes_and_toggle_changes_only_mine(owner_client, mine, expected):
    own = await seed("Моё")
    other = await seed("Чужое", admin=STAFF)
    response = owner_client.get("/panel/tickets", params={"mine": mine})
    assert response.status_code == 200
    doc = Document(response.text)
    canonical = {"tab": "open", "view": "open", "mine": expected}
    assert query(doc.link("Выгрузить CSV")) == canonical
    visible = {own} if expected == "1" else {own, other}
    assert doc.ticket_ids() == visible
    assert {int(row[0]) for row in csv_rows(owner_client.get(doc.link("Выгрузить CSV")))[1:]} == visible
    form, controls = doc.filter_form()
    assert controls["mine"] == ("1" if expected == "1" else "")
    assert {option["value"] for option in form["selects"]["mine"]["options"]} == {"", "1"}
    toggled = "0" if expected == "1" else "1"
    response = owner_client.get(form["action"], params=controls | {"mine": "" if toggled == "0" else "1"})
    assert response.status_code == 200
    switched = Document(response.text)
    assert query(switched.link("Выгрузить CSV")) == canonical | {"mine": toggled}
    assert switched.ticket_ids() == ({own} if toggled == "1" else {own, other})
    rows, *_ = await filter_tickets(mine=mine, actor_id=OWNER)
    assert {row["ticket_id"] for row in rows} == visible


async def test_duplicate_legacy_mine_is_rejected_as_ambiguous(owner_client):
    await seed()
    for path in ("/panel/tickets", "/panel/tickets.csv"):
        response = owner_client.get(path + "?mine=1&mine=")
        assert response.status_code == 400
        assert "повторяется" in response.json()["detail"]


@pytest.mark.parametrize("params", [{"date_from": "2026-02-30"}, {"date_to": "2026-13-01"},
                                   {"date_from": "20261101"}, {"date_to": "2026-W44-1"},
                                   {"date_from": "2026-11-01T00:00:00"},
                                   {"date_from": "2026-11-03", "date_to": "2026-11-01"}])
def test_bad_dates_return_readable_400_in_html_and_csv(owner_client, params):
    for path in ("/panel/tickets", "/panel/tickets.csv"):
        response = owner_client.get(path, params=params)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "даты" in detail.lower() or "дату" in detail.lower()
        assert "ГГГГ-ММ-ДД" in detail or "позже" in detail


async def test_calendar_boundaries_include_whole_day_and_timezone(owner_client):
    expected = []
    for stamp in ("2026-11-03 00:00:00", "2026-11-03 23:59:59.999999", "2026-11-03T18:59:59.999Z"):
        expected.append(await seed(stamp, when=stamp))
    await seed("До", when="2026-11-02 23:59:59.999")
    await seed("После", when="2026-11-03T19:00:00Z")
    params = {"date_from": "2026-11-03", "date_to": "2026-11-03"}
    rows, *_ = await filter_tickets(**params)
    assert {row["ticket_id"] for row in rows} == set(expected)
    exported = csv_rows(owner_client.get("/panel/tickets.csv", params=params))
    assert {int(row[0]) for row in exported[1:]} == set(expected)


@pytest.mark.parametrize("name", ["q", "status", "open", "category", "mine", "waiting", "overdue", "date"])
async def test_every_filter_precedes_limit(name, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 4, 12))
    wanted = await seed("Искомое старое обращение")
    await fill_recent(30)
    params = {"q": {"q": "искомое"}, "status": {"status": "new"}, "open": {"status": "open"},
              "category": {"category": "feedback"}, "mine": {"mine": "1", "actor_id": OWNER},
              "waiting": {"scope": "waiting"}, "overdue": {"scope": "overdue"},
              "date": {"date_from": "2026-11-01", "date_to": "2026-11-01"}}[name]
    if name == "open":
        await db.run("UPDATE tickets SET status='completed' WHERE ticket_id<>?", (wanted,))
    rows, *_ = await filter_tickets(limit=1, **params)
    assert [row["ticket_id"] for row in rows] == [wanted]


async def test_search_finds_rows_beyond_both_default_limits(owner_client):
    wanted = await seed("Самая старая уникальная заявка")
    await fill_recent(2010)
    for params in ({"q": "уникальная"}, {"q": f"№{wanted}"}, {"q": str(wanted), "status": "new"}):
        rows, *_ = await filter_tickets(**params)
        assert [row["ticket_id"] for row in rows] == [wanted]
        page = Document(owner_client.get("/panel/tickets", params=params).text)
        assert query(page.queue(wanted))["t"] == str(wanted)
        exported = csv_rows(owner_client.get("/panel/tickets.csv", params=params))
        assert [int(row[0]) for row in exported[1:]] == [wanted]


@pytest.mark.parametrize("needle", ["иванов", "нестандартная тема", "текст заявки",
                                  "100", "200", "100%_", "' OR 1=1 --"])
async def test_sql_search_supports_names_topic_ids_and_literal_text(needle):
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await add_staff(STAFF, "Петрова Анна", "all")
    wanted = await seed("Текст заявки 100%_ ' OR 1=1 --", admin=STAFF, topic="Нестандартная тема")
    await repo.create_ticket("999", "888", "academic", "Другая заявка без совпадений", "Иная")
    rows, *_ = await filter_tickets(q=needle)
    assert [row["ticket_id"] for row in rows] == [wanted]


async def test_counts_cover_filtered_rows_before_limit(owner_client, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 4, 12))
    for _ in range(4):
        await seed("Моё ожидание", status="in_progress")
    await seed("Моё свежее", when="2026-11-04 11:00:00")
    await seed("Готовое", status="ready")
    await seed("Чужое ожидание", admin=STAFF)
    await seed("Архивное", archived=True)
    rows, _states, waiting, overdue = await filter_tickets(mine="1", actor_id=OWNER, limit=2)
    assert len(rows) == 2
    assert rows[0]["filtered_total"] == 6
    assert rows[0]["filtered_in_progress"] == 4
    assert (waiting, overdue) == (5, 4)
    scoped, _, waiting, overdue = await filter_tickets(scope="overdue", mine="1", actor_id=OWNER, limit=1)
    assert scoped[0]["filtered_total"] == waiting == overdue == 4
    response = owner_client.get("/panel/tickets", params={"mine": "1"})
    assert response.status_code == 200
    page = response.text
    assert '<span class="ws-range">1–6 из 6</span>' in page
    for label, count in (("Открытые", 6), ("Ждут ответа", 5), ("Закрытые", 0)):
        assert f'<span>{label}</span><span class="ws-count">{count}</span>' in page
    assert 'Дольше суток<span class="ws-count">4</span>' in page
    doc = Document(page)
    assert len(doc.ticket_ids()) == 6
    in_progress = owner_client.get("/panel/tickets", params={"mine": "1", "status": "in_progress"})
    assert in_progress.status_code == 200
    assert '<span class="ws-range">1–4 из 4</span>' in in_progress.text
    assert len(csv_rows(owner_client.get(doc.link("Выгрузить CSV")))) == 7


async def test_admin_api_preserves_max_defaults_and_batches_joins(monkeypatch):
    for _ in range(4):
        await seed(admin=STAFF)
    await seed(admin=OWNER)
    await seed(admin=STAFF, archived=True)
    original_conn = db._conn
    connections = []
    statements = []

    @asynccontextmanager
    async def counted():
        connections.append(1)
        async with original_conn() as conn:
            await conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "_conn", counted)
    rows = await repo.admin_tickets(STAFF, 3)
    assert len(rows) == 3
    assert all(row["target_admin_id"] == STAFF and not row["deleted_at"] for row in rows)
    assert len(connections) == 1
    selects = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) == 1, "Метаданные таймеров не должны запрашиваться отдельно для каждого обращения"
    for row in rows:
        assert {"student_name", "staff_name", "latest_role", "latest_message_at", "in_progress_at"} <= set(row.keys())
        assert row["latest_role"] == "student"
    assert len(await repo.admin_tickets(None, 20)) == 5
    assert len(await repo.admin_tickets("", 20)) == 5, "Старый API MAX допускает пустой admin_id"
    assert len(await repo.admin_tickets(STAFF, 20, True)) == 1


async def test_mine_without_actor_is_not_all_tickets():
    await seed()
    assert (await filter_tickets(mine="1"))[0] == []


@pytest.mark.parametrize("action", ["edit", "reply", "ready", "archive", "restore", "status", "bulk", "delete", "see-all"])
async def test_actions_keep_safe_return_and_require_csrf(owner_client, action):
    await add_staff(STAFF, "Сотрудник", "all")
    tid = await seed(archived=action == "restore")
    params = {"status": "new", "category": "feedback", "q": "справка & +%#?",
              "scope": "waiting", "view": "archive", "mine": "0",
              "date_from": "2026-11-01", "date_to": "2026-11-04", "t": str(tid)}
    return_to = "/panel/tickets?" + urlencode(params)
    path = f"/panel/tickets/{tid}/{action}" if action not in ("bulk", "see-all") else f"/panel/tickets/{action}"
    fields = {"return": return_to, "text": "Спасибо", "status": "in_progress", "topic": "Уточнение",
              "tids": str(tid), "action": "status", "value": "in_progress", "user_id": STAFF}
    assert owner_client.post(path, data=fields, follow_redirects=False).status_code == 403
    response = post_form(owner_client, path, fields)
    assert response.status_code == 303
    assert response.headers["location"] == return_to


@pytest.mark.parametrize("target", ["https://example.test/", "//example.test/panel/", "/panel/../outside", "/panel/tickets\r\nLocation: https://example.test"])
async def test_untrusted_return_cannot_leave_panel(owner_client, target):
    tid = await seed()
    response = post_form(owner_client, f"/panel/tickets/{tid}/edit", {"return": target, "topic": "Новое"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/panel/tickets?t={tid}"


@pytest.mark.parametrize("archived", [False, True])
async def test_card_forms_keep_filters_without_nested_forms(owner_client, archived):
    tid = await seed(archived=archived)
    params = {"mine": "1", "date_from": "2026-11-01", "date_to": "2026-11-04",
              "scope": "waiting", "status": "new", "category": "feedback", "q": "справка", "t": str(tid)}
    if archived:
        params["view"] = "archive"
    canonical = params | {"tab": "closed" if archived else "waiting",
                          "view": "archive" if archived else "waiting", "page": "1"}
    response = owner_client.get("/panel/tickets", params=params)
    assert response.status_code == 200
    doc = Document(response.text)
    actions = [form for form in doc.forms if f"/tickets/{tid}/" in form.get("action", "")]
    assert 'name="as_staff"' not in response.text
    if archived:
        assert actions == [], "Архив в рабочем месте доступен только для чтения"
        assert "Для этого обращения доступны просмотр и история" in response.text
    else:
        assert {form.get("data-draft-mode") for form in actions} >= {"reply", "note", "status", "forward"}
        for form in actions:
            assert form["action"] == f"/panel/tickets/{tid}/action"
            assert len(form["fields"]["csrf"]) == 1 and form["fields"]["csrf"][0]
            assert form["fields"]["operation_key"][0]
            assert urlsplit(form["fields"]["return"][0]).path == "/panel/tickets"
            assert query(form["fields"]["return"][0]) == canonical

    # Редактирование и ответ за сотрудника остались на отдельной админ-странице.
    admin_link = doc.link("Администрирование обращения")
    without_ticket = {key: val for key, val in canonical.items() if key != "t"}
    assert urlsplit(admin_link).path == f"/panel/tickets/{tid}/admin"
    assert query(admin_link) == without_ticket
    admin_response = owner_client.get(admin_link)
    assert admin_response.status_code == 200
    admin_doc = Document(admin_response.text)
    admin_actions = [form for form in admin_doc.forms if f"/tickets/{tid}/" in form.get("action", "")]
    assert {form["action"].rsplit("/", 1)[1] for form in admin_actions} >= {"edit", "reply", "restore" if archived else "archive"}
    reply = next(form for form in admin_actions if form["action"].endswith("/reply"))
    assert "as_staff" in reply["selects"]
    for form in admin_actions:
        assert len(form["fields"]["csrf"]) == 1 and form["fields"]["csrf"][0]
        assert urlsplit(form["fields"]["return"][0]).path == "/panel/tickets"
        assert query(form["fields"]["return"][0]) == canonical
    back = admin_doc.link("К рабочему месту")
    assert urlsplit(back).path == f"/panel/tickets/{tid}"
    assert query(back) == without_ticket


async def test_reply_validation_keeps_return(owner_client):
    tid = await seed()
    return_to = "/panel/tickets?mine=0&scope=waiting&date_from=2026-11-01"
    for fields in ({"text": ""}, {"template": "999999"}):
        response = post_form(owner_client, f"/panel/tickets/{tid}/reply", fields | {"return": return_to})
        assert response.headers["location"] == return_to


async def test_waiting_uses_latest_message_not_card_edits(owner_client, monkeypatch):
    now = datetime(2026, 11, 4, 12)
    monkeypatch.setattr(clock, "now", lambda: now)
    tid = await seed(when="2026-11-02T05:00:00Z")
    await repo.update_ticket(tid, OWNER, topic="Поздняя правка")
    await repo.log_ticket_event(tid, OWNER, "note", "Поздняя заметка")
    rows, states, waiting, overdue = await filter_tickets(scope="overdue")
    assert [row["ticket_id"] for row in rows] == [tid]
    assert waiting == overdue == 1
    assert is_overdue(rows[0], states)
    doc = Document(owner_client.get("/panel/tickets", params={"t": tid}).text)
    assert len(doc.timers) == 2
    for timer in doc.timers:
        assert timer["data-timer-kind"] == "waiting"
        assert timer["data-prefix"] == "ждёт ответа"
        moment = datetime.fromisoformat(timer["data-time"])
        assert moment.tzinfo is not None
        assert moment == datetime(2026, 11, 2, 10, tzinfo=clock.zone())
        assert int(timer["data-time-ms"]) == int(moment.timestamp() * 1000)
    await repo.add_ticket_message(tid, STUDENT, "student", "Новое уточнение")
    assert (await filter_tickets(scope="overdue"))[0] == []
    await repo.add_ticket_message(tid, OWNER, "staff", "Ответ")
    assert (await filter_tickets(scope="waiting"))[0] == []


@pytest.mark.parametrize("method", ["status", "edit", "bulk", "legacy"])
async def test_in_progress_timer_uses_current_status_event(owner_client, monkeypatch, method):
    moments = [datetime(2026, 11, 1, 9)]
    monkeypatch.setattr(clock, "now", lambda: moments[0])
    tid = await seed()
    await repo.set_ticket_status(tid, "in_progress", OWNER)
    moments[0] = datetime(2026, 11, 2, 10)
    await repo.set_ticket_status(tid, "accepted", OWNER)
    moments[0] = datetime(2026, 11, 3, 11)
    if method == "status":
        await repo.set_ticket_status(tid, "in_progress", OWNER)
    elif method == "edit":
        await repo.update_ticket(tid, OWNER, status="in_progress", topic="Одновременная правка")
    elif method == "bulk":
        await repo.bulk_update([tid], "status", "in_progress", OWNER)
    else:
        await db.run("UPDATE tickets SET status='in_progress' WHERE ticket_id=?", (tid,))
        await repo.log_ticket_event(tid, OWNER, "status", "статус: accepted → in_progress")
    expected = moments[0].replace(tzinfo=clock.zone())
    moments[0] = datetime(2026, 11, 4, 12)
    await repo.add_ticket_message(tid, OWNER, "staff", "Поздний ответ без смены статуса")
    await repo.update_ticket(tid, OWNER, pickup_place="203")
    for i in range(55):
        await repo.log_ticket_event(tid, OWNER, "note", f"Заметка {i}")
    doc = Document(owner_client.get("/panel/tickets", params={"t": tid}).text)
    assert len(doc.timers) == 2
    for timer in doc.timers:
        assert timer["data-timer-kind"] == "in_progress"
        assert timer["data-prefix"] == "в работе"
        assert datetime.fromisoformat(timer["data-time"]) == expected
        assert int(timer["data-time-ms"]) == int(expected.timestamp() * 1000)


@pytest.mark.parametrize("legacy", ["missing", "stale", "invalid", "ready_barrier"])
async def test_legacy_in_progress_has_no_invented_time(owner_client, legacy):
    tid = await seed(status="in_progress")
    if legacy != "missing":
        await repo.log_ticket_event(tid, OWNER, "status", "in_progress")
    if legacy == "stale":
        await repo.log_ticket_event(tid, OWNER, "status", "accepted")
    elif legacy == "invalid":
        await db.run("UPDATE ticket_events SET created_at='мусор' WHERE ticket_id=? AND event='status'", (tid,))
    elif legacy == "ready_barrier":
        await repo.log_ticket_event(tid, OWNER, "ready", "Выдача")
    doc = Document(owner_client.get("/panel/tickets", params={"t": tid}).text)
    assert doc.timers == []
    assert doc.age_text.count("в работе · время не указано") == 2


@pytest.mark.parametrize("status", ["ready", "completed", "rejected", "closed"])
async def test_finished_statuses_do_not_wait_for_reply(owner_client, status):
    tid = await seed(status=status, when="2000-01-01 00:00:00")
    rows, _, waiting, overdue = await filter_tickets()
    assert [row["ticket_id"] for row in rows] == [tid]
    assert waiting == overdue == 0
    assert (await filter_tickets(scope="waiting"))[0] == []
    assert (await filter_tickets(scope="overdue"))[0] == []
    doc = Document(owner_client.get("/panel/tickets", params={"t": tid}).text)
    assert all(timer["data-timer-kind"] == "message" for timer in doc.timers)
    assert all("ждёт ответа" not in value for value in doc.age_text)


@pytest.mark.parametrize("prefix", ["", " ", "\t", "\r\n", "\x00\x1f", "\ufeff", "\u200b", "\u00a0\t"])
@pytest.mark.parametrize("formula", ["=1+1", "+1+1", "-1+1", "@SUM(1)"])
def test_csv_formulas_after_whitespace_or_control_are_quoted(prefix, formula):
    assert csv_cell(prefix + formula) == "'" + prefix + formula
    assert csv_cell(prefix + "Текст") == prefix + "Текст"


async def test_csv_uses_joined_names_outside_users_window(owner_client, monkeypatch):
    await register(STUDENT, "Редкое полное ФИО", "ис-21")
    await add_staff(STAFF, "Сотрудник для CSV", "all")
    async with db._conn() as conn:
        await conn.executemany("INSERT INTO users(user_id,full_name,group_code) VALUES(?,?,'ис-21')",
                               [(str(10000 + i), f"Пользователь {i}") for i in range(2010)])
        await conn.commit()
    tid = await seed("\t=1+1", admin=STAFF, topic="\ufeff@SUM(1)")

    async def forbidden(*args, **kwargs):
        raise AssertionError("CSV должен читать присоединённые имена, а не общий список людей")

    monkeypatch.setattr(repo, "list_users", forbidden)
    monkeypatch.setattr(repo, "all_admins", forbidden)
    response = owner_client.get("/panel/tickets.csv", params={"q": f"№{tid}"})
    header, row = csv_rows(response)
    assert header == ["№", "Создано", "Статус", "Раздел", "Тема", "Студент", "ID студента", "Сотрудник", "ID сотрудника", "Текст"]
    assert row[5] == "Редкое полное ФИО"
    assert row[7] == "Сотрудник для CSV"
    assert row[4] == "'\ufeff@SUM(1)"
    assert row[-1] == "'\t=1+1"


async def test_template_and_pickup_logs_exclude_field_values(owner_client, caplog):
    title = "Название, которого не должно быть в журнале"
    secret = "Личный текст шаблона для студента"
    assert post_form(owner_client, "/panel/templates/add", {
        "title": title, "text": secret, "category": "feedback"}).status_code == 303
    template = await db.one("SELECT id FROM reply_templates WHERE title=?", (title,))
    template_id = int(template["id"])
    assert post_form(owner_client, f"/panel/templates/{template_id}/edit", {
        "title": title + " правка", "text": secret + " правка", "category": "academic"}).status_code == 303
    assert post_form(owner_client, f"/panel/templates/{template_id}/delete").status_code == 303
    tid = await seed()
    pickup = "Особый кабинет для выдачи"
    await repo.update_ticket(tid, OWNER, pickup_place=pickup)
    assert post_form(owner_client, f"/panel/tickets/{tid}/ready").status_code == 303
    logs = await repo.admin_log(100)
    details = " ".join(str(dict(row)) for row in logs)
    for hidden in (title, secret, pickup):
        assert hidden not in details
        assert hidden not in caplog.text
    template_logs = [row for row in logs if row["action"].startswith("шаблон ответа")]
    assert len(template_logs) == 3
    assert all(f"ID {template_id}" in row["details"] for row in template_logs)
    ready = next(row for row in logs if row["action"] == "справка готова")
    assert "pickup_place" in ready["details"]
    assert f"№{tid}" in ready["details"]


async def test_bulk_rejects_unknown_action_before_logging_payload(owner_client):
    tid = await seed()
    payload = "Неизвестное действие с личным текстом"
    response = post_form(owner_client, "/panel/tickets/bulk", {"tids": str(tid), "action": payload})
    assert response.status_code == 400
    assert all(payload not in str(dict(row)) for row in await repo.admin_log(100))
