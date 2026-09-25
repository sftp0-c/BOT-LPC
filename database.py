"""SQLite-слой: схема, простые запросы, состояния диалога, настройки."""
import json
import os
from contextlib import asynccontextmanager

import aiosqlite

import config

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
    role_type       TEXT NOT NULL DEFAULT 'staff',      -- staff | superadmin
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
    pdf_url    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcasts(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id  TEXT NOT NULL,
    audience   TEXT NOT NULL,
    text       TEXT NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS processed_updates(
    key        TEXT PRIMARY KEY,                         -- отпечаток события (см. bot.update_key)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@asynccontextmanager
async def _conn():
    conn = await aiosqlite.connect(config.DATABASE_PATH, timeout=15)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    folder = os.path.dirname(os.path.abspath(config.DATABASE_PATH))
    os.makedirs(folder, exist_ok=True)
    async with _conn() as c:
        await c.execute("PRAGMA journal_mode=WAL")
        await c.executescript(SCHEMA)
        for admin_id in config.SUPERADMIN_IDS:
            await c.execute(
                "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?, 'Superadmin', 'superadmin', 1) "
                "ON CONFLICT(user_id) DO UPDATE SET role_type='superadmin', can_broadcast=1",
                (admin_id,),
            )
        await c.commit()


async def run(sql: str, params: tuple = ()) -> int:
    """Выполняет запрос на запись, возвращает lastrowid."""
    async with _conn() as c:
        cur = await c.execute(sql, params)
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


# ── обращения ─────────────────────────────────────────────────────────────────
async def create_ticket(student_id: str, admin_id: str, category: str, text: str) -> int:
    """Создаёт обращение и его первое сообщение в одной транзакции."""
    async with _conn() as c:
        cur = await c.execute(
            "INSERT INTO tickets(student_id, target_admin_id, category, text_content) VALUES(?,?,?,?)",
            (student_id, admin_id, category, text),
        )
        ticket_id = cur.lastrowid
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, student_id, "student", text),
        )
        await c.commit()
    return ticket_id


async def add_ticket_message(ticket_id: int, sender_id: str, role: str, text: str, new_status: str | None = None) -> None:
    async with _conn() as c:
        await c.execute(
            "INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) VALUES(?,?,?,?)",
            (ticket_id, sender_id, role, text),
        )
        if new_status:
            await c.execute(
                "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (new_status, ticket_id)
            )
        else:
            await c.execute("UPDATE tickets SET updated_at=datetime('now') WHERE ticket_id=?", (ticket_id,))
        await c.commit()


async def set_ticket_status(ticket_id: int, status: str) -> None:
    await run("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (status, ticket_id))


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
