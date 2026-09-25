"""SQLite-слой: схема, простые запросы, состояния диалога, настройки."""
import json
import os
from contextlib import asynccontextmanager

import aiosqlite

import config
from utils import as_str, norm_group

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id    TEXT PRIMARY KEY,
    full_name  TEXT NOT NULL,
    group_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS admins(
    user_id         TEXT PRIMARY KEY,
    full_name       TEXT NOT NULL,
    role_type       TEXT NOT NULL DEFAULT 'staff',      -- staff | sysadmin (сис-админ)
    role            TEXT NOT NULL DEFAULT '',
    office          TEXT NOT NULL DEFAULT '',
    ticket_category TEXT NOT NULL DEFAULT 'all',        -- feedback | certificates | all
    can_broadcast   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_states(
    user_id TEXT PRIMARY KEY,
    state   TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS tickets(
    ticket_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      TEXT NOT NULL,
    target_admin_id TEXT NOT NULL,
    category        TEXT NOT NULL,                      -- feedback | certificates
    text_content    TEXT NOT NULL,
    topic           TEXT NOT NULL DEFAULT '',           -- свободная тема обращения
    ready_until     TEXT NOT NULL DEFAULT '',
    doc_url         TEXT NOT NULL DEFAULT '',
    pickup_place    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'new',        -- new | in_progress | completed | rejected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tickets_student ON tickets(student_id);
CREATE INDEX IF NOT EXISTS idx_tickets_admin   ON tickets(target_admin_id);
CREATE TABLE IF NOT EXISTS ticket_messages(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    sender_id   TEXT NOT NULL,
    sender_role TEXT NOT NULL,                          -- student | staff
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticket_messages ON ticket_messages(ticket_id);
CREATE TABLE IF NOT EXISTS schedules(
    group_code TEXT PRIMARY KEY,
    pdf_url    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS schedule_subscriptions(
    user_id    TEXT PRIMARY KEY,
    group_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_schedule_subscriptions_group
    ON schedule_subscriptions(group_code);
CREATE TABLE IF NOT EXISTS groups(
    group_code TEXT PRIMARY KEY,                         -- нормализованный код группы, см. utils.norm_group
    title      TEXT NOT NULL DEFAULT '',                 -- человекочитаемое название группы
    active     INTEGER NOT NULL DEFAULT 1,              -- 0 — группа скрыта из подсказок
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcasts(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id   TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',                -- ФИО отправителя на момент рассылки
    sender_role TEXT NOT NULL DEFAULT '',                -- должность отправителя
    audience    TEXT NOT NULL,
    text        TEXT NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS processed_updates(
    key        TEXT PRIMARY KEY,                         -- отпечаток события (см. bot.update_key)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Колонки, добавленные после первой публикации бота: таблицы, созданные старой
# версией, CREATE TABLE IF NOT EXISTS не обновляет, поэтому недостающие колонки
# дописываются через ALTER TABLE в _add_missing_columns().
# Формат: {имя таблицы: {имя колонки: определение для ADD COLUMN}}.
COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "admins": {
        "role": "TEXT NOT NULL DEFAULT ''",
        "office": "TEXT NOT NULL DEFAULT ''",
    },
    "tickets": {
        "topic": "TEXT NOT NULL DEFAULT ''",
        "ready_until": "TEXT NOT NULL DEFAULT ''",
        "doc_url": "TEXT NOT NULL DEFAULT ''",
        "pickup_place": "TEXT NOT NULL DEFAULT ''",
    },
    "broadcasts": {
        "sender_name": "TEXT NOT NULL DEFAULT ''",
        "sender_role": "TEXT NOT NULL DEFAULT ''",
    },
}

# Таблицы, из которых справочник групп наполняется кодами, уже встречающимися в данных.
GROUP_SOURCES: tuple[str, ...] = ("users", "schedules")
TOPIC_SOURCES: tuple[str, ...] = GROUP_SOURCES

# Основная часть наполнения: одна вставка на таблицу-источник, без чтения данных
# в Python. INSERT OR IGNORE делает шаг идемпотентным — перезапуск бота не меняет
# ни названия, ни created_at уже заведённых групп. Коды копируются как есть:
# приводит их к общему виду _fix_group_codes.
GROUP_BACKFILL: tuple[str, ...] = tuple(
    f"INSERT OR IGNORE INTO groups(group_code) "
    f'SELECT DISTINCT group_code FROM "{table}" WHERE TRIM(group_code) <> \'\''
    for table in GROUP_SOURCES
)
GROUP_BACKFILL_MARKER = "groups_backfill_v1"


@asynccontextmanager
async def _conn():
    conn = await aiosqlite.connect(config.DATABASE_PATH, timeout=15)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        await conn.close()


async def add_column(c, table: str, name: str, definition: str) -> bool:
    cur = await c.execute(f'PRAGMA table_info("{table}")')
    rows = await cur.fetchall()
    existing = {
        str(row["name"] if hasattr(row, "keys") else row[1]).lower()
        for row in rows
    }
    if not existing or name.lower() in existing:
        return False
    await c.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')
    return True


async def _add_missing_columns(c, table: str, columns: dict[str, str]) -> None:
    """Дописывает в таблицу только те колонки из columns, которых в ней ещё нет.

    Источник истины о текущем состоянии таблицы — PRAGMA table_info, поэтому
    повторный вызов (например, при каждом старте бота) ничего не меняет, а
    ALTER TABLE не падает на «duplicate column name». Несуществующая таблица
    даёт пустой набор колонок: её создаст executescript с SCHEMA выше.
    """
    for name, definition in columns.items():
        await add_column(c, table, name, definition)


async def _fix_group_codes(c) -> None:
    """Приводит коды групп к тому же виду, что и запись через utils.norm_group.

    Нужно для баз, где код успели записать до нормализации: «ис-21» и «ИС-21» —
    это одна группа, но разные строки справочника. Регистр в SQL привести нельзя
    (upper() в SQLite работает только с ASCII, а коды групп кириллические), поэтому
    нормализованный код заводится в Python, а «старый» удаляется как дубль: любая
    такая строка в groups появилась либо только что из GROUP_BACKFILL, либо из такой
    же старой записи, и содержать ей нечего.
    """
    for table in GROUP_SOURCES:
        cur = await c.execute(f'SELECT DISTINCT group_code FROM "{table}" WHERE TRIM(group_code) <> \'\'')
        for row in await cur.fetchall():
            raw = row["group_code"]
            code = norm_group(as_str(raw))
            if code == raw:
                continue
            await c.execute("INSERT OR IGNORE INTO groups(group_code) VALUES(?)", (code,))
            await c.execute("DELETE FROM groups WHERE group_code=?", (raw,))


async def init_db() -> None:
    folder = os.path.dirname(os.path.abspath(config.DATABASE_PATH))
    os.makedirs(folder, exist_ok=True)
    async with _conn() as c:
        await c.execute("PRAGMA journal_mode=WAL")
        await c.executescript(SCHEMA)
        for table, columns in COLUMN_UPGRADES.items():
            await _add_missing_columns(c, table, columns)
        marker = await c.execute("SELECT value FROM settings WHERE key=?", (GROUP_BACKFILL_MARKER,))
        marker_row = await marker.fetchone()
        if marker_row is None or marker_row["value"] != "1":
            for sql in GROUP_BACKFILL:
                await c.execute(sql)
            await _fix_group_codes(c)
            await c.execute(
                "INSERT INTO settings(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'",
                (GROUP_BACKFILL_MARKER,),
            )
        for admin_id in config.SYSADMIN_IDS:
            await c.execute(
                "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?, 'Сис-админ', 'sysadmin', 1) "
                "ON CONFLICT(user_id) DO UPDATE SET role_type='sysadmin', can_broadcast=1",
                (admin_id,),
            )
        await c.commit()


async def run(sql: str, params: tuple = ()) -> int:
    """Выполняет запрос на запись, возвращает lastrowid."""
    async with _conn() as c:
        try:
            cur = await c.execute(sql, params)
        except aiosqlite.OperationalError as exc:
            if str(exc).startswith("duplicate column name:"):
                return 0
            raise
        await c.commit()
        return cur.lastrowid


async def one(sql: str, params: tuple = ()):
    async with _conn() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchone()


async def many(sql: str, params: tuple = ()):
    async with _conn() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


async def _load_admins() -> dict[str, dict]:
    rows = await many("SELECT * FROM admins ORDER BY user_id")
    result = {}
    for row in rows:
        admin = {key: row[key] for key in row.keys()}
        admin.setdefault("id", str(row["user_id"]))
        admin.setdefault("role", "")
        admin.setdefault("office", "")
        result[str(row["user_id"])] = admin
    return result


async def admin_ids() -> list[str]:
    return list((await _load_admins()).keys())


# ── состояния диалога ─────────────────────────────────────────────────────────
async def get_state(user_id: str):
    row = await one("SELECT state, payload FROM user_states WHERE user_id=?", (user_id,))
    return {"state": row["state"], "payload": json.loads(row["payload"])} if row else None


async def set_state(user_id: str, state: str, payload: dict | None = None) -> None:
    await run(
        "INSERT INTO user_states(user_id, state, payload) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, payload=excluded.payload",
        (user_id, state, json.dumps(payload or {}, ensure_ascii=False)),
    )


async def clear_state(user_id: str) -> None:
    await run("DELETE FROM user_states WHERE user_id=?", (user_id,))


# ── настройки ─────────────────────────────────────────────────────────────────
async def get_setting(key: str, default: str = "") -> str:
    row = await one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await run(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# Примечание: SQL-запросы по обращениям вынесены в repository.py (слой запросов).


# ── защита от дублей событий ───────────────────────────────────────────────
async def mark_processed(key: str, ttl_hours: int = 24) -> bool:
    """Помечает событие обработанным. True — событие новое, False — дубль.

    MAX доставляет события «как минимум один раз»: long polling может
    переотдать события после обрыва, webhook — повторить по своей политике,
    а два процесса/воркера бота могут забрать одно событие. Атомарный
    INSERT OR IGNORE гарантирует, что один ключ разовьётся ровно один раз,
    даже при конкурентной обработке. Старые записи вычищаются по TTL.
    """
    async with _conn() as c:
        cur = await c.execute("INSERT OR IGNORE INTO processed_updates(key) VALUES(?)", (key,))
        await c.execute("DELETE FROM processed_updates WHERE created_at < datetime('now', ?)", (f"-{ttl_hours} hours",))
        await c.commit()
        return cur.rowcount > 0
