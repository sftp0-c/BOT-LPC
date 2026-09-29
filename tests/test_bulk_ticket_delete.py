"""Массовое удаление обращений: по умолчанию архив, переключатель, защиты, журнал."""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

import config
import database as db
import repository as repo
from conftest import login_panel, post_form, register

import web.data as data_section
import webpanel   # noqa: F401  - панель поднимает маршруты раздела «Данные»

OWNER = "46010397"     # владелец бота: в тестах он же ROOT_IDS
STUDENT = "100"
STAFF = "200"
FIO = "Иванов Иван Иванович"
SECRET = "тихое дело без свидетелей"   # текст, которого не должно быть в журнале

PURGE = "/panel/data/tickets/purge"
TICKETS = "/panel/data/table/tickets"
ARCHIVED_WORD = "удалить"
OPEN_WORD = "удалить всё"


# ── фикстуры и хелперы ───────────────────────────────────────────────────────
@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    """Панель, вошедшая как владелец бота: раздел «Данные» открыт только ему."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()                       # владелец попадает в admins как owner
    assert login_panel(panel_client, OWNER)
    return panel_client


async def make_ticket(number: int, archived: bool = False) -> int:
    """Обращение с перепиской и историей - как от живого человека."""
    ticket = await repo.create_ticket(STUDENT, STAFF, "feedback",
                                      f"{SECRET} №{number}", f"Тема {number}")
    await repo.add_ticket_message(ticket, STAFF, "staff", "Ответ сотрудника")
    if archived:
        await repo.archive_ticket(ticket, OWNER)
    return ticket


async def seed(archived: int = 0, opened: int = 0) -> tuple[list, list]:
    """Наполняет базу обращениями двух областей: архивные и живые."""
    return ([await make_ticket(number, archived=True) for number in range(archived)],
            [await make_ticket(100 + number) for number in range(opened)])


async def rows_of(table: str) -> int:
    return (await db.one(f"SELECT COUNT(*) n FROM {table}"))["n"]


async def orphans() -> tuple[int, int]:
    """Сколько сообщений и событий осталось без своего обращения."""
    row = await db.one(
        "SELECT (SELECT COUNT(*) FROM ticket_messages m WHERE NOT EXISTS"
        " (SELECT 1 FROM tickets t WHERE t.ticket_id=m.ticket_id)) messages, "
        "(SELECT COUNT(*) FROM ticket_events e WHERE NOT EXISTS"
        " (SELECT 1 FROM tickets t WHERE t.ticket_id=e.ticket_id)) events")
    return row["messages"], row["events"]


def cell_value(body: str, label: str) -> str:
    """Число из строки таблицы предпросмотра по её названию."""
    match = re.search(r"<tr><td>" + re.escape(label) + r"</td><td>(\d+)</td></tr>", body)
    assert match, f"в предпросмотре нет строки «{label}»"
    return match.group(1)


async def admin_log_text() -> str:
    """Всё, что попало в admin_log последним действием - строкой."""
    return "\n".join(f"{item['action']} {item['details']}"
                     for item in await repo.admin_log(50))


# ── кнопка на странице таблицы ────────────────────────────────────────────────
async def test_button_stands_on_the_tickets_table_page(owner_client, env):
    """Кнопка стоит на странице таблицы tickets и ведёт на предпросмотр."""
    body = owner_client.get(TICKETS).text
    assert f'href="{PURGE}"' in body
    assert "Массовое удаление обращений" in body
    # кнопка архива на своём месте: массовое удаление её не заменило
    ticket = await make_ticket(1)
    row = owner_client.get(f"{TICKETS}/row/{ticket}").text
    assert f"/panel/data/ticket/{ticket}/archive" in row
    assert f"/panel/data/ticket/{ticket}/restore" in row


async def test_button_is_not_shown_to_other_tables(owner_client, env):
    """Массовое удаление - про обращения, другим таблицам оно не про дело."""
    assert PURGE not in owner_client.get("/panel/data/table/users").text
    assert PURGE not in owner_client.get("/panel/data/table/groups").text


# ── область выбора: по умолчанию только архив ─────────────────────────────────
async def test_only_archived_tickets_go_by_default(owner_client, env):
    """По умолчанию уходят архивные, живые остаются в работе."""
    archived, opened = await seed(archived=3, opened=2)
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    left = {row["ticket_id"] for row in await db.many("SELECT ticket_id FROM tickets")}
    assert left == set(opened), "живые обращения должны были остаться"
    for ticket in archived:
        assert await repo.get_ticket(ticket, include_archived=True) is None
    for ticket in opened:
        assert (await repo.get_ticket(ticket))["deleted_at"] == ""


async def test_open_scope_deletes_open_tickets_too(owner_client, env):
    """С переключателем «включая открытые» уходят и живые обращения."""
    archived, opened = await seed(archived=2, opened=3)
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    assert await rows_of("tickets") == 0
    for ticket in archived + opened:
        assert await repo.get_ticket(ticket, include_archived=True) is None


async def test_open_scope_needs_its_own_word(owner_client, env):
    """Для «включая открытые» слово другое: обычное «удалить» не подходит."""
    await seed(archived=1, opened=2)
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": ARCHIVED_WORD}).status_code == 303
    assert await rows_of("tickets") == 3, "ничего удаляться не должно было"
    assert "не введено" in owner_client.get(TICKETS).text
    # а со своим словом - удаляется
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    assert await rows_of("tickets") == 0


async def test_scope_comes_from_the_form_not_from_guess(owner_client, env):
    """Область «archived» стоит по умолчанию, даже если её не передали."""
    await seed(archived=2, opened=1)
    assert post_form(owner_client, PURGE, {"word": ARCHIVED_WORD}).status_code == 303
    assert await rows_of("tickets") == 1


# ── предпросмотр ничего не удаляет ────────────────────────────────────────────
async def test_preview_counts_and_deletes_nothing(owner_client, env):
    """Предпросмотр - это чтение: числа верные, в базе всё на месте."""
    archived, opened = await seed(archived=2, opened=3)
    before = (await rows_of("tickets"), await rows_of("ticket_messages"),
              await rows_of("ticket_events"))

    default_page = owner_client.get(PURGE).text
    assert (cell_value(default_page, "Обращений") == "2"
            and cell_value(default_page, "Сообщений переписки") == "4"
            and cell_value(default_page, "Событий истории") == "6"), "предпросмотр соврал"

    wide_page = owner_client.get(PURGE, params={"scope": "all"}).text
    assert (cell_value(wide_page, "Обращений") == "5"
            and cell_value(wide_page, "Сообщений переписки") == "10"
            and cell_value(wide_page, "Событий истории") == "12"), "предпросмотр соврал"

    # сколько бы страницу ни открывали, ничего не пропало
    owner_client.get(PURGE, params={"scope": "all"})
    assert (await rows_of("tickets"), await rows_of("ticket_messages"),
            await rows_of("ticket_events")) == before
    assert all(ticket for ticket in archived + opened)


async def test_preview_numbers_match_what_is_actually_deleted(owner_client, env):
    """Цифры на странице и результат действия - из одного замера."""
    await seed(archived=2, opened=3)
    page = owner_client.get(PURGE, params={"scope": "all"}).text
    promised = cell_value(page, "Сообщений переписки")
    before = await rows_of("ticket_messages")
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    assert before == int(promised), "предпросмотр обещал не то, что удалили"
    assert await rows_of("ticket_messages") == 0


# ── каскад: нет ни сирот, ни половины переписки ───────────────────────────────
async def test_no_orphan_messages_or_events_are_left(owner_client, env):
    """После удаления не остаётся ни одного сообщения и ни одного события."""
    await seed(archived=3, opened=4)
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    assert await orphans() == (0, 0)
    assert await rows_of("tickets") == 0
    assert await rows_of("ticket_messages") == 0
    assert await rows_of("ticket_events") == 0


async def test_archived_scope_leaves_open_tickets_intact(owner_client, env):
    """Частичное удаление не задевает переписку оставшихся обращений."""
    _, opened = await seed(archived=2, opened=2)
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    for ticket in opened:
        assert await repo.ticket_thread(ticket, limit=50) != []
        assert await repo.ticket_events(ticket, limit=50) != []
    assert await orphans() == (0, 0)


async def test_cascade_is_not_relied_upon_alone(owner_client, env):
    """Каскад включён, но удаление идёт по порядку: это видно по коду.

    Проверка не выключает внешние ключи (тогда проверялось бы, что база умеет
    каскад, а не что удаление не relies только на него) - она требует, чтобы
    store.tickets удалял сообщения и события сам, своими DELETE.
    """
    import inspect
    import store.tickets as store_tickets

    source = inspect.getsource(store_tickets.bulk_delete_tickets)
    deletes = [line.strip() for line in source.splitlines() if "DELETE FROM" in line]
    assert len(deletes) == 3, f"ожидались три своих DELETE, а их {len(deletes)}"
    tables = [re.search(r"DELETE FROM (\w+)", line).group(1) for line in deletes]
    assert tables == ["ticket_messages", "ticket_events", "tickets"], tables


async def test_foreign_keys_are_enabled_on_app_connections(owner_client, env):
    """PRAGMA foreign_keys включается на соединении приложения - иначе каскада нет."""
    async with db._conn() as connection:
        turned_on = (await (await connection.execute("PRAGMA foreign_keys")).fetchone())[0]
    assert turned_on == 1, "внешние ключи выключены: ON DELETE CASCADE не сработает"


# ── снимок базы обязателен ───────────────────────────────────────────────────
async def test_snapshot_is_made_before_purge(owner_client, env):
    """Перед удалением делается снимок базы, и путь к нему показывается."""
    await seed(archived=2)
    assert db.list_backups() == []
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    assert len(db.list_backups()) == 1
    assert "Снимок базы" in owner_client.get(TICKETS).text


async def test_without_snapshot_nothing_is_deleted(owner_client, env, monkeypatch):
    """Снимок не создался - действие не выполняется вовсе."""
    await seed(archived=2, opened=1)

    async def broken(target=None):
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(db, "backup_to", broken)
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    assert await rows_of("tickets") == 3, "без снимка удалять нельзя"
    assert db.list_backups() == []
    assert "снимок базы не создан" in owner_client.get(TICKETS).text


# ── слово-подтверждение ──────────────────────────────────────────────────────
async def test_without_the_word_nothing_is_deleted(owner_client, env):
    """Без слова действие не выполняется: кнопки «да» здесь нет вовсе."""
    await seed(archived=2, opened=1)
    for word in ("", "стереть", "удалеит", "УДАЛИТЬ ВСЁ", "да", "yes"):
        assert post_form(owner_client, PURGE,
                         {"scope": "archived", "word": word}).status_code == 303
        assert await rows_of("tickets") == 3, f"слово «{word}» не должно было подойти"
    assert db.list_backups() == [], "и снимок делаться не должен"


async def test_word_is_case_and_space_insensitive(owner_client, env):
    """Регистр и лишние пробелы не мешают: слово набирают руками."""
    await seed(archived=1)
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": "  Удалить  "}).status_code == 303
    assert await rows_of("tickets") == 0


# ── журнал действий ──────────────────────────────────────────────────────────
async def test_admin_log_records_who_how_many_and_which_scope(owner_client, env):
    """В журнале кто удалил, сколько обращений и какая была область."""
    await seed(archived=2, opened=1)
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    entry = next(item for item in await repo.admin_log(50)
                 if item["action"] == "массовое удаление обращений")
    assert entry["actor_id"] == OWNER
    assert "только архивные" in entry["details"]
    assert "обращений: 2" in entry["details"]


async def test_admin_log_keeps_no_correspondence_and_no_names(owner_client, env):
    """Ни текста переписки, ни ФИО студентов в журнале быть не должно."""
    await register(STUDENT, FIO, "ис-21")
    await seed(archived=2, opened=1)
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    written = await admin_log_text()
    assert "массовое удаление обращений" in written
    assert SECRET not in written, "текст переписки попал в журнал"
    assert FIO not in written, "ФИО студента попало в журнал"
    assert "Ответ сотрудника" not in written, "ответ сотрудника попал в журнал"


# ── пустая область ───────────────────────────────────────────────────────────
async def test_empty_scope_gives_an_honest_message(owner_client, env):
    """Нечего удалять - честное сообщение, а не ошибка и не 500."""
    assert owner_client.get(PURGE).status_code == 200
    assert "Удалять нечего" in owner_client.get(PURGE).text
    # и действие тоже не падает, а честно отвечает
    assert post_form(owner_client, PURGE,
                     {"scope": "archived", "word": ARCHIVED_WORD}).status_code == 303
    assert "нечего" in owner_client.get(TICKETS).text
    assert db.list_backups() == []


async def test_empty_open_scope_is_honest_too(owner_client, env):
    """Пустая область «включая открытые» - тоже сообщение, а не ошибка."""
    await seed(opened=2)
    assert "Удалять нечего" in owner_client.get(PURGE, params={"scope": "archived"}).text
    assert "Удалять нечего" not in owner_client.get(PURGE, params={"scope": "all"}).text
    assert post_form(owner_client, PURGE,
                     {"scope": "all", "word": OPEN_WORD}).status_code == 303
    assert await rows_of("tickets") == 0


# ── CSRF и доступ ────────────────────────────────────────────────────────────
async def test_purge_requires_csrf(owner_client, env):
    """Без токена формы действие не проходит."""
    await seed(archived=2, opened=1)
    for data in ({"scope": "archived", "word": ARCHIVED_WORD},
                 {"scope": "all", "word": OPEN_WORD}):
        assert owner_client.post(PURGE, data=data, follow_redirects=False).status_code == 403
    assert await rows_of("tickets") == 3
    assert db.list_backups() == []


async def test_purge_page_and_action_are_closed_to_others(panel_client, monkeypatch, env):
    """Раздел открыт владельцу: сис-админу и анониму страница не отвечает."""
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    await register(STUDENT, FIO, "ис-21")
    assert panel_client.get(PURGE, follow_redirects=False).status_code in (303, 404)
    assert panel_client.post(PURGE, data={"scope": "archived", "word": ARCHIVED_WORD},
                             follow_redirects=False).status_code in (303, 404)
    assert login_panel(panel_client, "1")           # обычный сис-админ, не владелец
    assert panel_client.get(PURGE, follow_redirects=False).status_code == 404
    assert panel_client.post(PURGE, data={"scope": "all", "word": OPEN_WORD},
                             follow_redirects=False).status_code == 404
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


# ── раздел не стал дырой ─────────────────────────────────────────────────────
async def test_no_arbitrary_sql_console_appeared(owner_client, env):
    """Массовое удаление - действие с защитами, а не консоль для SQL."""
    body = owner_client.get(PURGE).text
    for forbidden in ("DROP TABLE", "ALTER TABLE", "DELETE FROM", "INSERT INTO", "UPDATE "):
        assert forbidden not in body, f"на странице есть {forbidden}"
    assert await data_section.known_table("tickets") == "tickets"
    # и область задаётся только из списка, а не произвольной строкой
    assert data_section.purge_scope("всё подряд") == "archived"
    assert data_section.purge_scope("all") == "all"
