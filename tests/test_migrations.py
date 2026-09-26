"""Обновление схемы на базе, созданной прошлой версией бота.

init_db обязан быть идемпотентным: бот стартует на уже существующей базе, поэтому
каждый запуск должен аккуратно дописывать новое и не трогать старое.
"""
import inspect
import sqlite3

import pytest

import config
import database as db
import repository as repo

# Схема бота до появления справочника groups и колонки tickets.topic — ровно та,
# что создавал прежний init_db (git show HEAD:database.py), плюс данные.
LEGACY_SCHEMA = """
CREATE TABLE users(
    user_id    TEXT PRIMARY KEY,
    full_name  TEXT NOT NULL,
    group_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE admins(
    user_id         TEXT PRIMARY KEY,
    full_name       TEXT NOT NULL,
    role_type       TEXT NOT NULL DEFAULT 'staff',
    ticket_category TEXT NOT NULL DEFAULT 'all',
    can_broadcast   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE user_states(
    user_id TEXT PRIMARY KEY,
    state   TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE tickets(
    ticket_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      TEXT NOT NULL,
    target_admin_id TEXT NOT NULL,
    category        TEXT NOT NULL,
    text_content    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_tickets_student ON tickets(student_id);
CREATE INDEX idx_tickets_admin   ON tickets(target_admin_id);
CREATE TABLE ticket_messages(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    sender_id   TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ticket_messages ON ticket_messages(ticket_id);
CREATE TABLE schedules(
    group_code TEXT PRIMARY KEY,
    pdf_url    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE schedule_subscriptions(
    user_id    TEXT PRIMARY KEY,
    group_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_schedule_subscriptions_group
    ON schedule_subscriptions(group_code);
CREATE TABLE broadcasts(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id  TEXT NOT NULL,
    audience   TEXT NOT NULL,
    text       TEXT NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE processed_updates(
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

LEGACY_USERS = [
    ("100", "Иванов Иван", "ИС-21"),
    ("101", "Петров Пётр", "ИС-21"),
    ("102", "Сидоров Сидор", "  Бух-20  "),   # код с пробелами — до нормализации
    ("103", "Сидоров-дубль", "бух-20"),      # тот же код в другом регистре
]
LEGACY_SCHEDULES = [("АП-22", "https://example.com/ap-22.pdf")]
LEGACY_TICKET = ("100", "200", "feedback", "Не работает расписание")

# Определение groups из актуальной SCHEMA — чтобы тест про заранее созданный
# справочник не разъехался с настоящей схемой.
GROUPS_DDL = db.SCHEMA[db.SCHEMA.index("CREATE TABLE IF NOT EXISTS groups("):].split(";", 1)[0]


def make_legacy_db(path) -> None:
    """Создаёт базу прошлой версии бота и наполняет её данными (через обычный sqlite3)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany("INSERT INTO users(user_id, full_name, group_code) VALUES(?,?,?)", LEGACY_USERS)
        conn.executemany("INSERT INTO schedules(group_code, pdf_url) VALUES(?,?)", LEGACY_SCHEDULES)
        conn.execute(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content) VALUES(?,?,?,?)",
            LEGACY_TICKET,
        )
        conn.execute("INSERT INTO settings(key, value) VALUES('welcome_text', 'Привет!')")
        conn.commit()
    finally:
        conn.close()


def columns(path, table) -> dict:
    """{имя колонки: (тип, notnull, default)} из PRAGMA table_info."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        conn.close()
    return {r[1]: (r[2], r[3], r[4]) for r in rows}


def schema_snapshot(path) -> dict:
    """Всё содержимое sqlite_master: таблицы, индексы и их точные определения."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]): r[2] for r in rows}


async def group_rows() -> list:
    """Строки справочника в стабильном порядке — для сравнения до/после миграции."""
    return [tuple(r) for r in await db.many("SELECT group_code, title, active, created_at FROM groups ORDER BY group_code")]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """База прошлой версии бота вместо обычной тестовой (автофикстура env её создаёт)."""
    path = str(tmp_path / "legacy.db")
    make_legacy_db(path)
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    return path


async def test_database_api_is_async():
    """Слой доступа остаётся асинхронным: бот и тесты ждут его вызовы через await.

    Если кто-то сделает эти функции обычными, await-вызовы начнут падать по всему
    коду с невнятным TypeError — дешевле поймать расхождение здесь.
    """
    for func in (db.init_db, db.run, db.one, db.many, db.add_column, db._load_admins, db.admin_ids,
                 repo.get_group, repo.groups, repo.list_groups, repo.known_groups, repo.upsert_group, repo.set_group_active, repo.delete_group,
                 repo._load_admins, repo.admin_ids):
        assert inspect.iscoroutinefunction(func), f"{func.__name__} должен быть coroutine-функцией"


# ── чистая база ───────────────────────────────────────────────────────────────
async def test_init_db_creates_groups_and_topic_on_empty_db(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "empty.db"))
    await db.init_db()

    group_cols = columns(config.DATABASE_PATH, "groups")
    assert set(group_cols) == {"group_code", "title", "active", "created_at"}
    assert group_cols["title"] == ("TEXT", 1, "''")
    assert group_cols["active"] == ("INTEGER", 1, "1")
    assert {"role", "office"} <= set(columns(config.DATABASE_PATH, "admins"))
    assert {"topic", "ready_until", "doc_url", "pickup_place"} <= set(columns(config.DATABASE_PATH, "tickets"))


async def test_init_db_adds_only_missing_objects(legacy):
    """Появляются только новые объекты; прежние таблицы не пересоздаются и не теряют ничего."""
    before = schema_snapshot(legacy)
    before_cols = {name: columns(legacy, name) for kind, name in before if kind == "table"}

    await db.init_db()

    after = schema_snapshot(legacy)
    after_cols = {name: columns(legacy, name) for kind, name in after if kind == "table"}
    new_tables = {("table", name) for name in ("groups", "contacts", "ticket_events", "staff_invites",
                                              "staff_requests", "login_attempts", "lessons", "admin_log",
                                              "reply_templates")}
    new_indexes = {("index", name) for name in ("idx_contacts_last_seen", "idx_ticket_events",
                                               "idx_login_attempts", "idx_lessons_group", "idx_admin_log")}
    added = {item for item in set(after) - set(before) if not item[1].startswith("sqlite_autoindex_")}
    assert added == new_tables | new_indexes
    assert set(before) <= set(after)
    for name, cols in before_cols.items():
        # прежние колонки не изменились, добавиться могла только новая
        assert {k: v for k, v in after_cols[name].items() if k in cols} == cols, name
        expected = {
            "admins": {"role", "office", "position", "department"},
            "tickets": {"topic", "ready_until", "doc_url", "pickup_place"},
            "broadcasts": {"sender_name", "sender_role"},
            "user_states": {"created_at"},
            "schedules": {"parsed_at", "parsed_hash", "found_groups", "parse_error"},
        }.get(name, set())
        assert set(after_cols[name]) - set(cols) == expected, name


async def test_init_db_creates_new_tables_and_indexes(legacy):
    await db.init_db()
    created = schema_snapshot(legacy)
    names = {name for kind, name in created if kind == "table"}
    assert {"groups", "contacts", "ticket_events", "staff_invites", "staff_requests", "login_attempts"} <= names
    indexes = {name for kind, name in created if kind == "index"}
    assert {"idx_contacts_last_seen", "idx_ticket_events", "idx_login_attempts"} <= indexes
    assert {"username", "messages", "last_seen", "first_seen"} <= set(columns(legacy, "contacts"))
    assert {"event", "detail"} <= set(columns(legacy, "ticket_events"))
    assert {"code", "expires_at", "used_by"} <= set(columns(legacy, "staff_invites"))
    assert {"position", "office", "status"} <= set(columns(legacy, "staff_requests"))


# ── обновление существующих таблиц ────────────────────────────────────────────
async def test_init_db_adds_topic_column_to_legacy_tickets(legacy):
    assert "topic" not in columns(legacy, "tickets")

    await db.init_db()

    ticket_cols = columns(legacy, "tickets")
    for name in ("topic", "ready_until", "doc_url", "pickup_place"):
        column = ticket_cols[name]
        assert column[0] == "TEXT" and column[1] == 1 and column[2] == "''"
    # старая строка получила значение по умолчанию, а не NULL
    row = await db.one("SELECT * FROM tickets WHERE student_id=?", (LEGACY_TICKET[0],))
    assert row["topic"] == "" and row["ready_until"] == "" and row["doc_url"] == "" and row["pickup_place"] == ""
    assert row["text_content"] == LEGACY_TICKET[3] and row["status"] == "new"


async def test_init_db_creates_groups_table_in_legacy_db(legacy):
    await db.init_db()

    group_cols = columns(legacy, "groups")
    assert set(group_cols) == {"group_code", "title", "active", "created_at"}
    assert (group_cols["title"], group_cols["active"]) == (("TEXT", 1, "''"), ("INTEGER", 1, "1"))


async def test_init_db_keeps_legacy_data(legacy):
    await db.init_db()

    assert (await db.one("SELECT COUNT(*) n FROM users"))["n"] == len(LEGACY_USERS)
    assert (await db.one("SELECT pdf_url FROM schedules WHERE group_code='АП-22'"))["pdf_url"] == LEGACY_SCHEDULES[0][1]
    assert (await db.one("SELECT value FROM settings WHERE key='welcome_text'"))["value"] == "Привет!"


# ── наполнение справочника групп ──────────────────────────────────────────────
async def test_backfill_groups_from_users_and_schedules(legacy):
    await db.init_db()

    assert [r[0] for r in await group_rows()] == ["АП-22", "БУХ-20", "ИС-21"]
    # два «старых» написания одной группы схлопнулись в одну строку справочника,
    # а сами строки users не переписываются — это данные, а не справочник
    assert (await db.one("SELECT COUNT(*) n FROM users WHERE group_code IN ('бух-20', '  Бух-20  ')"))["n"] == 2
    # название не выдумывается, группа активна
    for _code, title, active, _created_at in await group_rows():
        assert title == "" and active == 1


async def test_backfill_ignores_blank_group_codes(legacy):
    """Пустой код в справочник не попадает: TRIM(...) <> '' отсекает его в SQL."""
    await db.run("INSERT INTO users(user_id, full_name, group_code) VALUES('104','Пустой','   ')")
    await db.run("INSERT INTO schedules(group_code, pdf_url) VALUES('', 'https://example.com/x.pdf')")

    await db.init_db()

    assert "" not in [r[0] for r in await group_rows()]


async def test_backfill_keeps_existing_group_untouched(legacy):
    """INSERT OR IGNORE не перетирает то, что завёл сис-админ."""
    conn = sqlite3.connect(legacy)
    conn.execute(GROUPS_DDL)
    conn.execute("INSERT INTO groups VALUES('ИС-21','Информационные системы',0,'2020-01-01 00:00:00')")
    conn.commit()
    conn.close()

    await db.init_db()

    kept = await db.one("SELECT * FROM groups WHERE group_code='ИС-21'")
    assert (kept["title"], kept["active"], kept["created_at"]) == ("Информационные системы", 0, "2020-01-01 00:00:00")
    fresh = await db.many("SELECT group_code, title, active FROM groups WHERE group_code<>'ИС-21' ORDER BY group_code")
    assert [tuple(r) for r in fresh] == [("АП-22", "", 1), ("БУХ-20", "", 1)]


async def test_backfill_does_not_pick_up_new_user_group_after_registry_created(legacy):
    await db.init_db()
    before = await group_rows()
    await db.run("INSERT INTO users(user_id, full_name, group_code) VALUES('105','Новенький','ПО-23')")

    await db.init_db()

    after = await group_rows()
    assert after == before
    assert await repo.list_groups(active_only=True) == [
        {"code": code, "active": active} for code, _title, active, _created_at in before
    ]
    await repo.upsert_group("по-23")
    assert {"code": "ПО-23", "active": 1} in await repo.list_groups(active_only=True)


async def test_backfill_normalizes_cyrillic_case(legacy):
    conn = sqlite3.connect(legacy)
    conn.execute("INSERT INTO users(user_id, full_name, group_code) VALUES('105','Иванов','ис-21')")
    conn.commit()
    conn.close()

    await db.init_db()

    assert [r[0] for r in await group_rows()] == ["АП-22", "БУХ-20", "ИС-21"]


# ── идемпотентность ───────────────────────────────────────────────────────────
async def test_init_db_is_idempotent(legacy):
    await db.init_db()
    schema_after_first = schema_snapshot(legacy)
    rows_after_first = await group_rows()

    await db.init_db()
    await db.init_db()

    assert schema_snapshot(legacy) == schema_after_first
    assert await group_rows() == rows_after_first


async def test_repeated_init_db_is_idempotent_on_empty_db(env, tmp_path, monkeypatch):
    path = str(tmp_path / "repeat.db")
    monkeypatch.setattr(config, "DATABASE_PATH", path)

    await db.init_db()
    snapshot = schema_snapshot(path)
    await db.run("INSERT INTO users(user_id, full_name, group_code) VALUES('1','Иванов','ИС-21')")

    await db.init_db()

    assert schema_snapshot(path) == snapshot
    assert await group_rows() == []
    await repo.upsert_group("ИС-21")
    assert [r[0] for r in await group_rows()] == ["ИС-21"]


async def test_tickets_topic_insert_works_without_explicit_value(legacy):
    """После ALTER TABLE код, не передающий topic, продолжает работать."""
    await db.init_db()

    ticket_id = await repo.create_ticket("100", "200", "feedback", "Тема не указана")

    assert (await repo.get_ticket(ticket_id))["topic"] == ""


# ── repository API справочника ────────────────────────────────────────────────
async def test_groups_listing_respects_active_flag(env):
    await repo.upsert_group("ис-21", "Информационные системы")
    await repo.upsert_group("БУХ-20", title="Бухгалтерия", active=False)

    assert [r["group_code"] for r in await repo.groups()] == ["ИС-21"]
    assert [r["group_code"] for r in await repo.groups(active_only=False)] == ["БУХ-20", "ИС-21"]
    assert [r["group_code"] for r in await repo.groups(active_only=False, limit=1)] == ["БУХ-20"]


async def test_group_codes_are_normalized_on_write_and_read(env):
    assert await repo.upsert_group("  бух-20  ", "  Бухгалтерия  ") is True

    assert (await repo.get_group("бух-20"))["group_code"] == "БУХ-20"
    assert (await repo.get_group(" БУХ-20 "))["title"] == "Бухгалтерия"
    assert [r["group_code"] for r in await repo.groups()] == ["БУХ-20"]


async def test_empty_group_code_is_rejected(env):
    for value in ("", "   ", None):
        assert await repo.upsert_group(value) is False
        assert await repo.get_group(value) is None

    assert await repo.groups() == []
    assert await repo.known_groups() == []

    await repo.delete_group("")  # не должно падать
    assert await repo.groups() == []


async def test_upsert_group_creates_group_with_defaults(env):
    await repo.upsert_group("ИС-21")

    row = await repo.get_group("ИС-21")
    assert row["title"] == "" and row["active"] == 1 and row["created_at"]


async def test_upsert_group_without_arguments_keeps_title_and_active(env):
    """Повторный вызов «просто завести группу» не стирает её название и активность."""
    await repo.upsert_group("ИС-21", "Информационные системы", active=False)
    created_at = (await repo.get_group("ИС-21"))["created_at"]

    await repo.upsert_group("ИС-21")

    row = await repo.get_group("ИС-21")
    assert row["title"] == "Информационные системы" and row["active"] == 0 and row["created_at"] == created_at


async def test_upsert_group_updates_only_given_fields(env):
    await repo.upsert_group("ИС-21", "Информационные системы")

    await repo.upsert_group("ИС-21", active=False)
    assert (await repo.get_group("ИС-21"))["active"] == 0
    assert (await repo.get_group("ИС-21"))["title"] == "Информационные системы"

    await repo.upsert_group("ИС-21", "ИС")
    assert (await repo.get_group("ИС-21"))["title"] == "ИС"
    assert (await repo.get_group("ИС-21"))["active"] == 0


async def test_delete_group_normalizes_code_and_keeps_data(env):
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.upsert_group("ИС-21")

    await repo.delete_group("  ис-21  ")

    assert await repo.get_group("ИС-21") is None
    assert (await repo.get_user("100"))["group_code"] == "ИС-21"


async def test_known_groups_unites_reference_and_data(env):
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.upsert_user("101", "Петров Пётр", "БУХ-20")
    await repo.upsert_schedule("АП-22", "https://example.com/ap-22.pdf")
    await repo.upsert_group("ПО-23", "Правовое обеспечение")

    assert await repo.known_groups() == ["АП-22", "БУХ-20", "ИС-21", "ПО-23"]
    assert await repo.known_groups(limit=2) == ["АП-22", "БУХ-20"]


async def test_known_groups_keeps_group_deleted_from_reference(env):
    """Скрытая в справочнике группа остаётся известной, если в ней есть студенты."""
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.upsert_group("ИС-21")
    await repo.delete_group("ИС-21")

    assert await repo.groups() == []
    assert await repo.known_groups() == ["ИС-21"]


async def test_known_groups_normalizes_legacy_codes(env):
    """Коды, записанные до нормализации, приводятся к общему виду и не дублируются."""
    await db.run("INSERT INTO users(user_id, full_name, group_code) VALUES('100','Иванов',' ис-21 ')")
    await db.run("INSERT INTO schedules(group_code, pdf_url) VALUES('ИС-21','https://college.example/is-21.pdf')")

    assert await repo.known_groups() == ["ИС-21"]


async def test_admin_profile_is_partial_and_readable(env):
    await repo.add_staff("200", "Петрова Анна")

    await repo.set_admin_profile("200", role="director")
    row = await repo.get_admin("200")
    assert (row["role"], row["office"]) == ("director", "")

    await repo.set_admin_profile("200", office="214")
    await repo.set_admin_profile("200")
    row = await repo.get_admin("200")
    assert (row["role"], row["office"]) == ("director", "214")
    admins = await repo._load_admins()
    assert admins["200"]["role"] == "director"
    assert admins["200"]["office"] == "214"
    assert "200" in await repo.admin_ids()


async def test_ticket_topic_ready_and_related_people(env):
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.add_staff("200", "Петрова Анна")
    await repo.set_admin_profile("200", role="director", office="214")

    ticket_id = await repo.create_ticket("100", "200", "feedback", "Текст", topic="Учебный вопрос")
    await repo.set_ticket_status(ticket_id, "in_progress")
    await repo.set_ticket_ready(ticket_id, "сегодня до 18:00", "каб. 204", "https://college.example/doc.pdf")

    ticket = await repo.get_ticket(ticket_id)
    assert ticket["topic"] == "Учебный вопрос"
    assert ticket["status"] == "ready"
    assert ticket["ready_until"] == "сегодня до 18:00"
    assert ticket["pickup_place"] == "каб. 204"
    assert ticket["doc_url"] == "https://college.example/doc.pdf"
    assert ticket["student"]["full_name"] == "Иванов Иван"
    assert ticket["staff"]["full_name"] == "Петрова Анна"
    assert ticket["staff"]["role"] == "director"
    assert ticket["staff"]["office"] == "214"


async def test_list_groups_returns_normalized_registry_entries(env):
    await repo.upsert_group(" ис-21 ", active=False)
    await repo.upsert_group("бух-20", True)

    assert await repo.list_groups() == [{"code": "БУХ-20", "active": 1}]
    assert await repo.list_groups(active_only=False) == [
        {"code": "БУХ-20", "active": 1},
        {"code": "ИС-21", "active": 0},
    ]

    await repo.set_group_active("ис-21", True)
    assert {"code": "ИС-21", "active": 1} in await repo.list_groups()
    await repo.delete_group(code=" ис-21 ")
    assert {"code": "ИС-21", "active": 1} not in await repo.list_groups(active_only=False)
