"""SQLite-слой: схема, простые запросы, состояния диалога, настройки."""
import json
import os
import re
import time
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
    role            TEXT NOT NULL DEFAULT '',            -- код типа должности, см. handlers.admin.STAFF_ROLES
    position        TEXT NOT NULL DEFAULT '',            -- должность свободным текстом (главное, что видят студенты)
    department      TEXT NOT NULL DEFAULT '',            -- отдел/подразделение для группировки
    office          TEXT NOT NULL DEFAULT '',
    ticket_category TEXT NOT NULL DEFAULT 'all',        -- feedback | certificates | all
    can_broadcast   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_states(
    user_id    TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''                 -- когда состояние поставили (для чистки зависших)
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
CREATE TABLE IF NOT EXISTS ticket_events(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    actor_id   TEXT NOT NULL DEFAULT '',                 -- кто вызвал событие
    event      TEXT NOT NULL,                            -- created | message_student | message_staff | status | ready
    detail     TEXT NOT NULL DEFAULT '',                 -- новый статус, тема сообщения и т.п.
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticket_events ON ticket_events(ticket_id);
CREATE TABLE IF NOT EXISTS schedules(
    group_code   TEXT PRIMARY KEY,
    pdf_url      TEXT NOT NULL,
    parsed_at    TEXT NOT NULL DEFAULT '',                 -- когда последний раз удалось разобрать PDF
    parsed_hash  TEXT NOT NULL DEFAULT '',                 -- отпечаток файла: файл изменился — пора перечитать
    found_groups TEXT NOT NULL DEFAULT '',                 -- какие группы нашлись в самом PDF
    parse_error  TEXT NOT NULL DEFAULT '',                 -- почему не вышло (показывается в панели)
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS lessons(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code TEXT NOT NULL,
    weekday    INTEGER NOT NULL,                          -- 0 — понедельник … 5 — суббота
    lesson_num INTEGER NOT NULL,                          -- номер пары в дне
    subject    TEXT NOT NULL,                             -- предмет и вид занятия
    teacher    TEXT NOT NULL DEFAULT '',
    room       TEXT NOT NULL DEFAULT '',
    start      TEXT NOT NULL DEFAULT '',                  -- из звонков, см. timetable.LESSON_TIMES
    end        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lessons_group ON lessons(group_code, weekday, lesson_num);
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
CREATE TABLE IF NOT EXISTS reply_templates(                          -- готовые ответы сотрудников
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,                             -- короткое название: «Справка готова»
    text       TEXT NOT NULL,                             -- сам ответ (шаблон можно поправить перед отправкой)
    category   TEXT NOT NULL DEFAULT 'all',               -- раздел обращений: all - для всех
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    used_count INTEGER NOT NULL DEFAULT 0                -- сколько раз применили: показывает полезные
);
CREATE TABLE IF NOT EXISTS admin_log(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   TEXT NOT NULL,                            -- кто: сис-админ или бота
    action     TEXT NOT NULL,                            -- что сделал: краткий код действия
    details    TEXT NOT NULL DEFAULT '',                 -- подробности: над кем и с чем
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_log ON admin_log(id);
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
    key         TEXT PRIMARY KEY,                        -- отпечаток события (см. bot.update_key)
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS contacts(
    user_id      TEXT PRIMARY KEY,                       -- все, кто писал боту, включая незарегистрированных
    username     TEXT NOT NULL DEFAULT '',               -- публичный ник из профиля MAX → ссылка на профиль
    display_name TEXT NOT NULL DEFAULT '',               -- отображаемое имя из профиля MAX
    messages     INTEGER NOT NULL DEFAULT 0,             -- сколько раз писал боту (роль считается по users/admins)
    last_text    TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_last_seen ON contacts(last_seen);
CREATE TABLE IF NOT EXISTS staff_invites(
    code       TEXT PRIMARY KEY,                         -- код из букв и цифр, регистр не важен
    user_id    TEXT NOT NULL DEFAULT '',                 -- приглашение личному ID; пусто — любой, у кого есть код
    full_name  TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL DEFAULT '',
    used_by    TEXT NOT NULL DEFAULT '',
    used_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS staff_requests(
    user_id    TEXT PRIMARY KEY,                         -- заявка «хочу стать сотрудником» без кода
    full_name  TEXT NOT NULL DEFAULT '',
    position   TEXT NOT NULL DEFAULT '',
    office     TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'new',              -- new | approved | rejected
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS login_attempts(
    user_id    TEXT NOT NULL,                            -- подбор кода сотрудника
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(user_id);
"""

# Колонки, добавленные после первой публикации бота: таблицы, созданные старой
# версией, CREATE TABLE IF NOT EXISTS не обновляет, поэтому недостающие колонки
# дописываются через ALTER TABLE в _add_missing_columns().
# Формат: {имя таблицы: {имя колонки: определение для ADD COLUMN}}.
COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "admins": {
        "role": "TEXT NOT NULL DEFAULT ''",
        "office": "TEXT NOT NULL DEFAULT ''",
        "position": "TEXT NOT NULL DEFAULT ''",
        "department": "TEXT NOT NULL DEFAULT ''",
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
    "user_states": {
        "created_at": "TEXT NOT NULL DEFAULT ''",
    },
    "schedules": {
        "parsed_at": "TEXT NOT NULL DEFAULT ''",
        "parsed_hash": "TEXT NOT NULL DEFAULT ''",
        "found_groups": "TEXT NOT NULL DEFAULT ''",
        "parse_error": "TEXT NOT NULL DEFAULT ''",
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
# Список ID сис-админов, отозванных в панели: их нельзя вернуть перезапуском бота
SYSADMINS_REVOKED_KEY = "sysadmins_revoked"


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
    if not await column_exists(c, table, name):
        await c.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')
        return True
    return False


async def column_exists(c, table: str, name: str) -> bool:
    """Есть ли колонка в таблице. Пустая таблица (её нет) — False."""
    rows = await (await c.execute(f'PRAGMA table_info("{table}")')).fetchall()
    existing = {
        str(row["name"] if hasattr(row, "keys") else row[1]).lower()
        for row in rows
    }
    return bool(existing) and name.lower() in existing


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
        for admin_id in await _sysadmins_to_import(c):
            await c.execute(
                "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?, 'Сис-админ', 'sysadmin', 1) "
                "ON CONFLICT(user_id) DO UPDATE SET role_type='sysadmin', can_broadcast=1",
                (admin_id,),
            )
        # Владелец: максимальные права на корневом уровне. Обновляется при каждом
        # старте, и отзыв прав в панели его не касается (см. revoke_sysadmin).
        for owner_id in config.ROOT_IDS or []:
            await c.execute(
                "INSERT INTO admins(user_id, full_name, role_type, can_broadcast) VALUES(?, 'Владелец', 'owner', 1) "
                "ON CONFLICT(user_id) DO UPDATE SET role_type='owner', can_broadcast=1",
                (str(owner_id),),
            )
        await c.commit()


async def _sysadmins_to_import(c) -> list[str]:
    """Какие ID из SYSADMIN_IDS ещё нужно завести в базе.

    Список сис-админов живёт в таблице admins, а .env остаётся источником для
    первого запуска и для аварийного восстановления доступа. Отозванного в
    панели сис-админа (о нём .env не знает) повторно не заводим: его ID лежит
    в settings под ключом SYSADMINS_REVOKED_KEY. Владелец (ROOT_IDS) не в этом
    списке и назначается отдельно.
    """
    if not config.SYSADMIN_IDS:
        return []
    found = await (await c.execute("SELECT value FROM settings WHERE key=?", (SYSADMINS_REVOKED_KEY,))).fetchone()
    revoked = {part for part in as_str(found["value"]).split(",") if part} if found else set()
    owners = {str(value) for value in config.ROOT_IDS or []}
    return [str(value) for value in config.SYSADMIN_IDS
            if str(value) not in revoked and str(value) not in owners]


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


async def run_count(sql: str, params: tuple = ()) -> int:
    """Выполняет UPDATE/DELETE и возвращает, сколько строк он изменил.

    Нужен там, где важно, что правка действительно произошла (например, погашение
    одноразового кода): run() возвращает lastrowid, а для UPDATE он всегда 0.
    """
    async with _conn() as c:
        cur = await c.execute(sql, params)
        await c.commit()
        return cur.rowcount


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
        "INSERT INTO user_states(user_id, state, payload, created_at) VALUES(?,?,?,datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, payload=excluded.payload, "
        "created_at=excluded.created_at",
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


# ── обслуживание базы: состояние, бэкапы, чистка ───────────────────────────────
# Всё, чем пользуется вкладка «База данных» панели. Операции разбиты на
# безопасные (посмотреть, сделать копию, сжать) и необратимые (удалить данные) —
# вызывающий код решает, что показать пользователю.
def database_file() -> str:
    return os.path.abspath(config.DATABASE_PATH)


def backups_folder() -> str:
    """Папка с резервными копиями; по умолчанию — рядом с базой, а не в .env."""
    folder = config.BACKUP_DIR or os.path.join(os.path.dirname(database_file()), "backups")
    os.makedirs(folder, exist_ok=True)
    return folder


def file_sizes() -> dict:
    """Размеры файлов базы: основной, журнал WAL и его индекс."""
    path = database_file()
    sizes = {"total": 0}
    for name, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
        try:
            sizes[name] = os.path.getsize(path + suffix)
        except OSError:
            sizes[name] = 0
    sizes["total"] = sizes["db"] + sizes["wal"]
    return sizes


async def integrity_check() -> str:
    """Результат PRAGMA integrity_check: 'ok' — база цела."""
    row = await one("PRAGMA integrity_check")
    return as_str(row[0]) if row else "неизвестно"


async def storage_info() -> dict:
    """Состояние файла: версия SQLite, режим журнала, страницы и свободное место."""
    async with _conn() as c:
        version = as_str((await (await c.execute("SELECT sqlite_version()")).fetchone())[0])
        mode = as_str((await (await c.execute("PRAGMA journal_mode")).fetchone())[0])
        page_size = (await (await c.execute("PRAGMA page_size")).fetchone())[0]
        page_count = (await (await c.execute("PRAGMA page_count")).fetchone())[0]
        freelist = (await (await c.execute("PRAGMA freelist_count")).fetchone())[0]
    return {
        "sqlite_version": version,
        "journal_mode": mode,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "reclaimable": freelist * page_size,
    }


async def table_counts() -> list[tuple[str, int]]:
    """Сколько строк в каждой таблице — «сколько данных накопилось»."""
    names = [as_str(row["name"]) for row in await many(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = []
    for name in names:
        row = await one(f'SELECT COUNT(*) n FROM "{name}"')
        counts.append((name, row["n"] if row else 0))
    return counts


# Ожидаемая схема берётся из SCHEMA и COLUMN_UPGRADES, поэтому список не нужно
# вести вручную: удалённая таблица или потерянная колонка обнаруживаются сразу.
EXPECTED_TABLES: tuple[str, ...] = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
EXPECTED_INDEXES: tuple[str, ...] = tuple(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", SCHEMA))


async def missing_objects() -> list[str]:
    """Чего в базе не хватает относительно схемы в коде: ['таблица users', ...].

    Пустой список — база в порядке. Нужен, чтобы поймать удалённую таблицу или
    потерянную колонку: без этого запросы падают с «no such table», и понять
    причину можно только по журналу.
    """
    missing: list[str] = []
    async with _conn() as c:
        rows = await (await c.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )).fetchall()
        present = {(as_str(row["type"]), as_str(row["name"])) for row in rows}
        missing += [f"таблица {name}" for name in EXPECTED_TABLES if ("table", name) not in present]
        missing += [f"индекс {name}" for name in EXPECTED_INDEXES if ("index", name) not in present]
        for table, columns in COLUMN_UPGRADES.items():
            if ("table", table) not in present:
                continue  # таблицы нет в списке выше, колонки ей не нужны
            for column in columns:
                if not await column_exists(c, table, column):
                    missing.append(f"{table}.{column}")
    return missing


async def repair_schema() -> list[str]:
    """Создаёт недостающие таблицы, индексы и колонки. Возвращает, что именно создано.

    Безопасно для данных: init_db() только дописывает (CREATE ... IF NOT EXISTS
    и ALTER TABLE ADD COLUMN), существующие таблицы не трогает. Удалённые
    таблицы появятся пустыми — восстановить их содержимое может только копия.
    """
    before = set(await missing_objects())
    await init_db()
    return sorted(before - set(await missing_objects()))


async def vacuum() -> None:
    """Сжимает файл базы: освободившиеся страницы уходят в конец файла."""
    async with _conn() as c:
        await c.execute("VACUUM")


async def checkpoint() -> int:
    """Сливает WAL в основной файл и обрезает журнал. Возвращает число страниц WAL."""
    async with _conn() as c:
        await c.commit()
        row = await (await c.execute("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()
        return int(row[2]) if row else 0


def backup_name() -> str:
    return time.strftime("bot-%Y%m%d-%H%M%S.db")


def _unique_path(folder: str, name: str) -> str:
    """Путь с именем name; если файл уже есть — с номером: bot-…-2.db, -3.db.

    Имя копии строится с точностью до секунды, поэтому две копии подряд в одну
    секунду не должны затирать друг друга.
    """
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(name)
    for number in range(2, 100):
        candidate = os.path.join(folder, f"{stem}-{number}{extension}")
        if not os.path.exists(candidate):
            return candidate
    return path


async def backup_to(target: str | None = None) -> str:
    """Согласованная копия базы (VACUUM INTO). Возвращает путь к файлу копии.

    Копия делается на работающей базе и не требует остановки бота: SQLite
    пишет в новый файл, исходный остаётся нетронутым. Прежние копии сверх
    BACKUP_KEEP удаляются.
    """
    if target is None:
        target = _unique_path(backups_folder(), backup_name())
    if os.path.exists(target):
        os.remove(target)
    async with _conn() as c:
        await c.commit()
        await c.execute("VACUUM INTO ?", (target,))
    await _keep_backups()
    return target


async def _keep_backups() -> None:
    keep = max(1, int(config.BACKUP_KEEP))
    files = list_backups()
    for item in files[keep:]:
        try:
            os.remove(os.path.join(backups_folder(), item["name"]))
        except OSError:
            continue


def list_backups() -> list[dict]:
    """Резервные копии: новые сверху. Имя — только имя файла, путь не собирается.

    Сортировка по времени изменения, а не по имени: копии одной секунды
    различаются суффиксом -2, -3, и по имени они шли бы не по порядку.
    """
    folder = backups_folder()
    items = []
    for name in os.listdir(folder):
        if not (name.endswith(".db") and name.startswith("bot-")):
            continue
        try:
            stat = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        items.append({"name": name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return items


def backup_path(name: str) -> str:
    """Путь к копии по имени. '' — если имя недопустимое (обход каталога)."""
    if not name.startswith("bot-") or not name.endswith(".db") or "/" in name or "\\" in name:
        return ""
    path = os.path.join(backups_folder(), name)
    return path if os.path.exists(path) else ""


def delete_backup(name: str) -> bool:
    path = backup_path(name)
    if not path:
        return False
    os.remove(path)
    return True


async def check_backup(name: str) -> str:
    """Проверяет копию перед восстановлением: цела и это вообще наша база."""
    path = backup_path(name)
    if not path:
        return "Файл копии не найден."
    try:
        async with aiosqlite.connect(path) as c:
            c.row_factory = aiosqlite.Row
            check = as_str((await (await c.execute("PRAGMA integrity_check")).fetchone())[0])
            tables = {as_str(row["name"]) for row in
                      await (await c.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    except aiosqlite.Error as exc:
        return f"Файл не читается как база SQLite: {exc}"
    if check != "ok":
        return f"Копия повреждена: {check}"
    if "users" not in tables or "tickets" not in tables:
        return "В копии нет таблиц users и tickets — это не база бота."
    return "ok"


async def restore_from(name: str) -> tuple[bool, str]:
    """Восстанавливает базу из копии. Перед этим автоматически делает страховочную копию.

    Возвращает (получилось ли, сообщение для панели). Копия переносится в живой
    файл через API backup SQLite: файл подменяется целиком, незавершённых
    запросов и потерянных записей не бывает.
    """
    verdict = await check_backup(name)
    if verdict != "ok":
        return False, verdict
    path = backup_path(name)
    safety = await backup_to(_unique_path(backups_folder(), f"bot-before-restore-{backup_name()}"))
    async with _conn() as live:
        await live.commit()
        async with aiosqlite.connect(path) as source:
            # backup(target) копирует ЭТУ базу В target: источник — копия, цель — живой файл
            await source.backup(live)
        await live.commit()
    await checkpoint()
    await init_db()  # схема из копии могла быть старее текущей
    return True, os.path.basename(safety)


async def prune(table: str, days: int) -> int:
    """Удаляет записи старше N дней. Разрешены только таблицы со временем created_at.

    Исключение бросается на всё прочее: чистка по имени таблицы приходит из
    панели, и лишняя таблица в списке не должна превращаться в потерю данных.
    """
    allowed = ("processed_updates", "login_attempts", "user_states")
    if table not in allowed:
        return -1
    if table == "user_states":
        # состояния незавершённых диалогов: пустое created_at — заведомо старые
        return await run_count(
            "DELETE FROM user_states WHERE created_at='' OR created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
    return await run_count(f"DELETE FROM {table} WHERE created_at < datetime('now', ?)", (f"-{int(days)} days",))
