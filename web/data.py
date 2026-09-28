"""Раздел «Данные»: содержимое базы глазами и руками владельца бота.

Панель показывает таблицы, колонки и строки так, как их видит SQLite, и
позволяет поправить значение, добавить строку или удалить её. Раздел открыт
только владельцу бота (``config.ROOT_IDS``): здесь обходятся проверки
предметной области, которые на обычных путях бота стоят, поэтому доступ - это
отдельное решение, а не побочный эффект входа сис-админа.

Чего здесь нет и почему:

* нет консоли для произвольного SQL - это дыра диаметром с базу;
* нет ``DROP TABLE`` и ``ALTER TABLE`` - для этого нужен остановленный бот и
  внешний инструмент sqlite3, а панель работает на живой базе;
* удаление человека, снятие прав и работа с обращением идут не через
  ``DELETE`` по таблице, а через функции репозитория (``repo.delete_user``,
  ``repo.revoke_sysadmin``, ``repo.delete_staff``, ``repo.archive_ticket``):
  у человека бывают открытые обращения, а роль сотрудника настраивается в
  своём разделе.

Каждое изменение пишется в ``admin_log`` (кто, таблица, строка, что было и
что стало), а перед ним делается согласованный снимок базы через ту же
механику ``VACUUM INTO``, что и на вкладке «База данных».
"""
import csv
import io
import re
from urllib.parse import quote

import clock
import database as db
import repository as repo
from fastapi import HTTPException, Request
from fastapi.responses import Response
from panel_theme import icon
from utils import as_str, is_sysadmin_role, to_int

from .common import (code_cell, csrf, esc, flash, log, page, pages_of, pager, redirect, require_form,
                     require_user)
from .database import human_bytes
from .router import router


# ── доступ ───────────────────────────────────────────────────────────────────
async def require_owner(request: Request) -> str:
    """Раздел «Данные» открыт только владельцу бота (``config.ROOT_IDS``).

    Владелец заводится в таблицу ``admins`` при старте бота
    (``database.init_db``) и не отзывается. Сис-админ без прав владельца
    получает 404, а не 403: раздела для него нет, и ответ «доступ запрещён»
    только напомнил бы, что он существует. Так же закрыт вебхук в ``bot.py``.
    """
    user = await require_user(request)
    if not await repo.is_owner(user):
        log.warning("панель: «Данные» попытался открыть не владелец: id=%s", user)
        raise HTTPException(status_code=404, detail="Раздел не найден")
    return user


async def require_owner_form(request: Request) -> str:
    """require_owner плюс CSRF-токен формы: как require_form, но для владельца."""
    await require_owner(request)          # 404 тому, кому раздел не положен
    return await require_form(request)   # 403 форме без токена


# ── как устроена работа с таблицами ──────────────────────────────────────────
DATA_PAGE = 50              # строк на странице просмотра таблицы
CSV_LIMIT = 5000            # строк в выгрузке
SEARCH_HITS = 5             # совпадений на пару «таблица-колонка» в общем поиске
SCAN_ROW_LIMIT = 200_000    # дальше уже не считаем объём и не ищем подстроку

# Колонки, где лежит время. Имя кончается на _at / _seen / _date и тому же:
# в проекте время пишется в такие колонки строкой «ГГГГ-ММ-ДД ЧЧ:ММ:СС»
# (clock.STAMP_FORMAT) и без часового пояса.
TIME_SUFFIXES = ("_at", "_seen", "_date", "_time", "_until", "_from", "_to")
TIME_NAMES = ("until", "date", "time", "deadline")
# Принимаем и дату, и дату со временем: отпуск хранится как «2026-10-05».
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
# Слово, которым подтверждается необратимое действие.
CONFIRM_WORD = "удалить"
# Таблицы, где строку нельзя удалять сырым DELETE: у человека и у обращения
# есть правила проекта, и они живут в функциях репозитория.
GUARDED_TABLES = ("users", "admins", "tickets")
# Таблицы, где строка - это человек.
PERSON_TABLES = ("users", "admins", "contacts")


def is_time_column(name: str) -> bool:
    """Похоже ли имя колонки на то, где лежит дата или время."""
    text = as_str(name).lower()
    return text.endswith(TIME_SUFFIXES) or text in TIME_NAMES


def time_problem(value) -> str:
    """Что не так со строкой времени; пустая строка - всё в порядке.

    Пустую строку проверять нечем, а свободный текст («сегодня до 18:00» в
    ``tickets.ready_until``) пропускаем: ругаться будем только на то, что
    начали писать дату с цифры, а дальше написали не дату.
    """
    text = as_str(value).strip()
    if not text or not text[0].isdigit():
        return ""
    return "" if STAMP_RE.fullmatch(text) else "не похоже на дату"


def is_required(column: dict) -> bool:
    """Обязательное поле: без него INSERT не пройдёт.

    NOT NULL без значения по умолчанию - очевидный случай. Отдельный -
    первичный ключ: INTEGER заполняется сам (AUTOINCREMENT), а текстовый
    (например group_code) без значения не вставится вовсе.
    """
    if column["default"]:
        return False
    if column["notnull"]:
        return True
    return column["pk"] and column["type"].upper() != "INTEGER"


# Колонки, которые проект пишет в каноническом виде, а сырая правка может
# испортить: значение сохраняется как ввели, но сис-админ об этом узнаёт.
CANONICAL = {"group_code": repo.canonical_group}


def canonical_note(key: str, value) -> str:
    """Подсказка про канонический вид, если введённое значение от него отличается."""
    rule = CANONICAL.get(key)
    text = as_str(value)
    if not rule or not text:
        return ""
    proper = rule(text)
    return f" {key} обычно пишут канонически: «{proper}»." if proper and proper != text else ""


def ident(name: str) -> str:
    """Имя таблицы или колонки в двойных кавычках: иначе SQLite его не поймёт."""
    return '"' + as_str(name).replace('"', '""') + '"'


def like_escape(value: str) -> str:
    """Экранирование спецсимволов LIKE: «%» ищется как «%», а не как шаблон."""
    return as_str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def file_name(name: str) -> str:
    """Имя файла для выгрузки: только безопасные символы."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", as_str(name)) or "table"


def link(base: str, **params) -> str:
    """Адрес с параметрами: значения кодируются, пустые отбрасываются."""
    clean = {key: as_str(val) for key, val in params.items() if as_str(val) != ""}
    query = "&".join(f"{key}={quote(val)}" for key, val in clean.items())
    return f"{base}?{query}" if query else base


async def table_names() -> list:
    """Все таблицы базы, кроме служебных sqlite_*."""
    rows = await db.many(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [as_str(row["name"]) for row in rows]


async def known_table(name: str) -> str:
    """Имя таблицы, если она есть. Иначе 404: неизвестное имя - не повод гадать."""
    wanted = as_str(name)
    if wanted in await table_names():
        return wanted
    raise HTTPException(status_code=404, detail="Такой таблицы в базе нет")


async def table_columns(name: str) -> list:
    """PRAGMA table_info: имя, тип, NOT NULL, значение по умолчанию, ключ."""
    rows = await db.many(f"PRAGMA table_info({ident(name)})")
    return [{"name": as_str(row["name"]), "type": as_str(row["type"]),
             "notnull": bool(row["notnull"]), "default": as_str(row["dflt_value"]),
             "pk": bool(row["pk"]), "time": is_time_column(row["name"])}
            for row in rows]


async def table_sql(name: str) -> str:
    """Текст CREATE TABLE; его же показываем в разделе и он отвечает про rowid."""
    row = await db.one("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,))
    if not row:
        raise HTTPException(status_code=404, detail="Такой таблицы в базе нет")
    return as_str(row["sql"])


def has_rowid(sql: str) -> bool:
    """Есть ли у таблицы rowid: у таблицы WITHOUT ROWID его нет."""
    return "WITHOUT ROWID" not in as_str(sql).upper()


async def row_count(name: str) -> int:
    """Сколько строк в таблице.

    Отдельной мелочью, потому что ``COUNT(*)`` - единственный дешёвый вопрос к
    таблице: объём данных и время последней правки считать на каждый поиск
    незачем.
    """
    return to_int((await db.one(f"SELECT COUNT(*) n FROM {ident(name)}"))["n"])


async def table_stats(name: str, columns: list, has_rowid: bool) -> dict:
    """Сколько строк, сколько примерно занимают и когда менялись.

    Дата последней правки берётся из ``MAX(created_at)`` и ``MAX(updated_at)``:
    это единственные колонки, где лежит время, и в формате проекта
    «ГГГГ-ММ-ДД ЧЧ:ММ:СС» такие строки сравниваются между собой так же, как
    даты. ``MAX(rowid)`` - только подсказка: это счётчик вставок, а не время,
    и о правке он не говорит ничего.

    Объём - сумма длин значений в байтах: SQLite не умеет отдавать размер
    таблицы (виртуальной таблицы dbstat в сборке нет), поэтому это честная
    оценка «сколько данных лежит в таблице», а не размер куска файла.
    """
    total = await row_count(name)
    stats = {"rows": total, "size_text": "—", "last": "—",
             "last_raw": "", "newest": 0}
    if not total:
        return stats
    stats["last"] = "времени в таблице нет"
    if total > SCAN_ROW_LIMIT:
        stats["size_text"] = "много строк"
        stats["last"] = "не считали"
        return stats
    lengths = [f"LENGTH(CAST(COALESCE({ident(c['name'])},'') AS BLOB))" for c in columns]
    parts = ["COUNT(*) n", (f"SUM({'+'.join(lengths)}) payload" if lengths else "0 payload")]
    parts += [f"MAX({ident(c['name'])}) {c['name']}" for c in columns
              if c["name"] in ("created_at", "updated_at")]
    if has_rowid:
        parts.append("MAX(rowid) newest")
    row = await db.one(f"SELECT {', '.join(parts)} FROM {ident(name)}")
    got = row.keys()
    stats["size_text"] = human_bytes(to_int(row["payload"]))
    stamps = [as_str(row[key]) for key in ("created_at", "updated_at")
              if key in got and as_str(row[key])]
    if stamps:
        stats["last_raw"] = max(stamps)
        stats["last"] = clock.format_when(stats["last_raw"])
    if "newest" in got:
        stats["newest"] = to_int(row["newest"])
    return stats


async def table_info(name: str) -> dict:
    """Всё, что нужно про таблицу: текст CREATE TABLE, колонки и ключи.

    Статистики здесь намеренно нет: объём данных и время последней правки
    считаются обходом таблицы, а нужны они только в списке таблиц.
    """
    sql = await table_sql(name)
    columns = await table_columns(name)
    return {"name": name, "sql": sql, "columns": columns, "rowid": has_rowid(sql),
            "keys": [c["name"] for c in columns if c["pk"]]}


def filters_of(columns: list, col: str, eq: str, fcol: str, like: str) -> tuple:
    """WHERE из формы: точное значение по одной колонке и подстрока по другой."""
    names = [c["name"] for c in columns]
    where, params = [], []
    if col in names and eq != "":
        where.append(f"{ident(col)} = ?")
        params.append(eq)
    if fcol in names and like != "":
        where.append(f"{ident(fcol)} LIKE ? ESCAPE '\\'")
        params.append(f"%{like_escape(like)}%")
    return (" WHERE " + " AND ".join(where) if where else ""), tuple(params)


async def fetch_rows(name: str, columns: list, has_rowid: bool, sort: str, direction: str,
                     where: str, params: tuple, limit: int, offset: int = 0) -> list:
    """Строки таблицы с сортировкой и фильтрами."""
    names = [c["name"] for c in columns]
    key = sort if sort in names else (names[0] if names else "rowid")
    order = f" ORDER BY {ident(key)} {direction}"
    if has_rowid:                 # одинаковые значения не должны тасовать страницы
        order += ", rowid"
    # ключ строки нужен ссылке «№» и поиску: у таблицы без rowid его не будет
    prefix = "rowid AS _rid, " if has_rowid else ""
    return await db.many(f"SELECT {prefix}* FROM {ident(name)}{where}{order} LIMIT ? OFFSET ?",
                         params + (max(1, int(limit)), max(0, int(offset))))


# ── снимок базы перед опасным действием ──────────────────────────────────────
async def snapshot(note: str) -> str:
    """Копия базы через VACUUM INTO - та же механика, что на вкладке «База данных».

    Возвращает путь к копии; пустая строка - снимок не получился (действие всё
    равно выполняется, но в журнал падает ошибка, а не молчание).
    """
    try:
        path = await db.backup_to()
    except Exception as exc:  # noqa: BLE001 - снимок не должен ронять действие
        log.error("панель «Данные»: снимок базы перед «%s» не создан: %s", note, exc)
        return ""
    log.warning("панель «Данные»: снимок базы перед «%s» - %s", note, path)
    return path


def snapshot_text(path: str) -> str:
    """Как показать снимок сис-админу: путь и что он уже сделан."""
    return f"Снимок базы: {path}" if path else "Резервная копия не создана - см. вкладку «База данных»."


# ── список таблиц ────────────────────────────────────────────────────────────
@router.get("/data")
async def data_page(request: Request, q: str = ""):
    """Что лежит в базе: таблицы с размерами и поиск значения по всем сразу."""
    user = await require_owner(request)
    needle = as_str(q).strip()[:200]
    names = await table_names()
    rows, total = [], 0
    for name in names:
        info = await table_info(name)
        stats = await table_stats(name, info["columns"], info["rowid"])
        total += stats["rows"]
        if stats["last_raw"]:
            when = f'<span title="{esc(stats["last_raw"])}">{esc(stats["last"])}</span>'
        elif stats["newest"]:
            when = (f'{esc(stats["last"])}<br><span class="small mut">'
                    f'последняя строка №{stats["newest"]}</span>')
        else:
            when = esc(stats["last"])
        rows.append(
            f'<tr><td><a href="/panel/data/table/{quote(name)}"><code>{esc(name)}</code></a></td>'
            f'<td>{stats["rows"]}</td><td class="small">{esc(stats["size_text"])}</td>'
            f'<td class="small mut">{when}</td></tr>'
        )
    listing = ('<table class="data-table"><tr><th>Таблица</th><th>Строк</th>'
               f'<th>Объём данных</th><th>Когда менялась</th></tr>'
               f'{"".join(rows) or "<tr><td class=mut>Таблиц нет</td></tr>"}</table>')
    hits = await search_everywhere(needle) if needle else []
    body = f"""
<div class="card"><h2>{icon("database", 20)} Таблицы базы: {len(names)}, всего строк {total}</h2>
<p class="small mut">«Объём данных» - сумма длин значений в таблице: SQLite не отдаёт размер
таблицы отдельно от файла, поэтому это сравнение между таблицами, а не куски файла.
«Когда менялась» - MAX(created_at)/MAX(updated_at), то есть время последней добавленной
строки, а не последней правки. Нажмите на таблицу, чтобы открыть её.</p>
{listing}
<p class="small mut">Состояние базы, сжатие, проверка целостности и резервные копии -
это вкладка «База данных», здесь их нет намеренно.</p></div>
<div class="card"><h2>{icon("search", 20)} Поиск по всем таблицам</h2>
<form method="get" action="/panel/data" class="grid">
<div class="full"><label>Что найти: MAX ID, ФИО, группа, код, фрагмент текста</label>
<input name="q" value="{esc(needle)}" autocomplete="off" placeholder="например: 46010397"></div>
<div><button>{icon("search", 16)} Найти по всем таблицам</button></div>
</form>{_search_hits(hits, needle)}</div>
<div class="card"><h2>{icon("warning", 20)} Чего здесь нет</h2>
<ul>
<li>Консоли для произвольного SQL: «DELETE FROM …» без правил проекта снёс бы базу,
а «UPDATE без WHERE» - половину.</li>
<li><code>DROP TABLE</code> и <code>ALTER TABLE</code>: для этого нужен остановленный бот
и внешний инструмент <code>sqlite3</code>. Схему чинит кнопка «Восстановить схему»
на вкладке «База данных».</li>
<li>Сырого удаления человека, сотрудника и обращения: у них есть свои правила
(открытые обращения, роль, архив), и они вынесены в кнопки, которые зовут
функции репозитория.</li>
</ul></div>"""
    return page("Данные", body, user, "/data")


def _search_hits(hits: list, needle: str) -> str:
    """Результаты общего поиска: таблица, колонка, ключ строки и значение."""
    if not needle:
        return ""
    if not hits:
        return (f'<p class="msg msg-bad">{icon("warning", 20)} «{esc(needle)}» не нашлось '
                f'ни в одной таблице</p>')
    found = [hit for hit in hits if not hit["skipped"]]
    rows = "".join(
        f'<tr><td><a href="/panel/data/table/{quote(hit["table"])}/row/{hit["rowid"]}">'
        f'<code>{esc(hit["table"])}</code></a></td><td>{esc(hit["rowid"])}</td>'
        f'<td class="small mut">{esc(hit["column"])}</td><td><code>{esc(hit["value"])}</code></td></tr>'
        for hit in found)
    tables = sorted({hit["table"] for hit in found})
    note = f"Найдено в {len(tables)} таблицах, показаны первые {SEARCH_HITS} совпадений."
    skipped = [hit["table"] for hit in hits if hit["skipped"]]
    if skipped:
        note += " Пропущены слишком длинные таблицы: " + ", ".join(sorted(set(skipped)))
    tail = ", ".join(f'<a href="/panel/data/table/{quote(t)}"><code>{esc(t)}</code></a>'
                     for t in tables)
    return (f'<table class="data-table"><tr><th>Таблица</th><th>Строка</th><th>Колонка</th>'
            f'<th>Значение</th></tr>{rows}</table><p class="small mut">{esc(note)} '
            f'Открыть: {tail}</p>')


async def search_everywhere(needle: str) -> list:
    """Где встречается значение: по всем таблицам сразу.

    Текстовые колонки ищем подстрокой, числовые - точным равенством. Таблицы
    длиннее SCAN_ROW_LIMIT пропускаем: полный просмотр миллиона строк ради
    одного совпадения - это минуты ожидания на странице.
    """
    hits: list = []
    needle = as_str(needle).strip()
    if not needle:
        return hits
    like = f"%{like_escape(needle)}%"
    number = needle.lstrip("-")
    is_number = number.isdigit()
    for name in await table_names():
        rows_count = await row_count(name)
        if not rows_count or not has_rowid(await table_sql(name)):
            continue
        if rows_count > SCAN_ROW_LIMIT:
            hits.append({"table": name, "rowid": "—", "column": "—", "value": "—",
                         "skipped": True})
            continue
        for column in await table_columns(name):
            if is_number and not column["time"] \
                    and column["type"].upper() in ("INTEGER", "REAL"):
                sql = (f"SELECT rowid rid, {ident(column['name'])} val FROM {ident(name)} "
                       f"WHERE {ident(column['name'])} = ? LIMIT ?")
                args: tuple = (number, SEARCH_HITS)
            else:
                sql = (f"SELECT rowid rid, {ident(column['name'])} val FROM {ident(name)} "
                       f"WHERE {ident(column['name'])} LIKE ? ESCAPE '\\' LIMIT ?")
                args = (like, SEARCH_HITS)
            for row in await db.many(sql, args):
                hits.append({"table": name, "rowid": as_str(row["rid"]),
                             "column": column["name"], "value": as_str(row["val"])[:120],
                             "skipped": False})
    return hits


# ── просмотр таблицы ─────────────────────────────────────────────────────────
@router.get("/data/table/{name}")
async def data_table(request: Request, name: str, page_no: int = 1, sort: str = "", dir: str = "asc",
                     col: str = "", eq: str = "", fcol: str = "", like: str = ""):
    """Одна таблица: колонки, схема, строки постранично, сортировка и фильтры."""
    user = await require_owner(request)
    name = await known_table(name)
    info = await table_info(name)
    columns, names = info["columns"], [c["name"] for c in info["columns"]]
    sort = sort if sort in names else ""
    direction = "DESC" if as_str(dir).lower() == "desc" else "ASC"
    where, params = filters_of(columns, col, eq, fcol, like)
    total = to_int((await db.one(f"SELECT COUNT(*) n FROM {ident(name)}{where}", params))["n"])
    pages = pages_of(total, DATA_PAGE)
    page_no = max(1, min(to_int(page_no, 1), pages))
    rows = await fetch_rows(name, columns, info["rowid"], sort, direction, where, params,
                            DATA_PAGE, (page_no - 1) * DATA_PAGE)
    base = f"/panel/data/table/{quote(name)}"
    keep = {"sort": sort, "dir": "desc" if direction == "DESC" else "asc",
            "col": col, "eq": eq, "fcol": fcol, "like": like}

    def head_link(column: str) -> str:
        if column == sort:
            nxt, mark = ("desc", " ↓") if direction == "ASC" else ("asc", " ↑")
        else:
            nxt, mark = "asc", ""
        return f'<a href="{link(base, **dict(keep, sort=column, dir=nxt))}">{esc(column)}{mark}</a>'

    head = "".join(f"<th>{head_link(c['name'])}"
                   f"<div class='small mut'>{esc(c['type'] or '—')}</div></th>" for c in columns)
    body_rows = "".join(
        f'<tr><td class="data-key"><a href="/panel/data/table/{quote(name)}/row/{row["_rid"]}">'
        f'№{row["_rid"]}</a></td>'
        + "".join(f'<td>{"—" if row[c["name"]] is None else esc(row[c["name"]])}</td>'
                  for c in columns) + "</tr>"
        for row in rows
    ) or '<tr><td class="mut">Ничего не найдено: проверьте фильтры.</td></tr>'
    picked = "".join(f'<option value="{esc(c["name"])}"'
                     f'{" selected" if c["name"] == sort else ""}>{esc(c["name"])}</option>'
                     for c in columns)
    plain = "".join(f'<option value="{esc(c["name"])}">{esc(c["name"])}</option>' for c in columns)
    filter_form = f"""
<form method="get" action="{base}" class="grid">
<div><label>Сортировать по</label><select name="sort">
<option value="">как в базе</option>{picked}</select></div>
<div><label>Направление</label><select name="dir">
<option value="asc"{" selected" if direction == "ASC" else ""}>по возрастанию</option>
<option value="desc"{" selected" if direction == "DESC" else ""}>по убыванию</option></select></div>
<div><label>Значение точно</label><input name="eq" value="{esc(eq)}" autocomplete="off"></div>
<div><label>в колонке</label><input name="col" value="{esc(col)}" list="data-columns"
 autocomplete="off"></div>
<div><label>Подстрока</label><input name="like" value="{esc(like)}" autocomplete="off"></div>
<div><label>в колонке</label><input name="fcol" value="{esc(fcol)}" list="data-columns"
 autocomplete="off"></div>
<div><button>{icon("filter", 16)} Показать</button> <a class="btn-grey" href="{base}">Сбросить</a></div>
</form><datalist id="data-columns">{plain}</datalist>"""
    schema = (f'<details class="card"><summary>{icon("settings", 20)} Схема: PRAGMA table_info '
              f'и CREATE TABLE</summary><table><tr><th>Колонка</th><th>Тип</th><th>NOT NULL</th>'
              f'<th>По умолчанию</th><th>Ключ</th></tr>'
              + "".join(
                  f'<tr><td><code>{esc(c["name"])}</code></td><td>{esc(c["type"] or "—")}</td>'
                  f'<td>{"да" if c["notnull"] else "нет"}</td>'
                  f'<td><code>{esc(c["default"] or "—")}</code></td>'
                  f'<td>{"да" if c["pk"] else "нет"}</td></tr>' for c in columns)
              + f'</table><p class="small mut">CREATE TABLE</p><pre>{esc(info["sql"])}</pre></details>')
    hint = "" if info["rowid"] else " Таблица создана без rowid: построчная правка недоступна."
    body = f"""
<div class="card"><h2>{icon("database", 20)} {esc(name)}: {total} строк</h2>
<p class="small mut">Ключ строки: {esc(", ".join(info["keys"]) or "rowid")}. На странице
{len(rows)} строк. Правка строки — по ссылке «№» в первом столбце.{hint}</p>
{filter_form}
<table class="data-table"><tr><th>Строка</th>{head}</tr>{body_rows}</table>
{pager(base, [(k, v) for k, v in keep.items() if v], page_no, pages, f"строк: {total}")}
</div>
{schema}
<div class="card"><h2>{icon("plus", 20)} Добавить строку</h2>
<p class="small mut">Заполняются только нужные колонки, остальные получат значения по
умолчанию из схемы. Перед записью делается снимок базы.</p>
<a class="btn-ok" href="{base}/row/new">{icon("plus", 16)} Новая строка</a></div>"""
    return page(f"Данные: {name}", body, user, "/data",
                actions=f'<a class="btn" href="{link("/panel/data/table.csv", name=name, **keep)}">'
                        f'{icon("download", 16)} Выгрузить в CSV</a>'
                        f'<span class="small mut">до {CSV_LIMIT} строк, с теми же фильтрами</span>')


@router.get("/data/table.csv")
async def data_table_csv(request: Request, name: str = "", sort: str = "", dir: str = "asc",
                         col: str = "", eq: str = "", fcol: str = "", like: str = ""):
    """Выгрузка выборки в CSV: та же механика, что у реестра людей (BOM для Excel)."""
    await require_owner(request)
    name = await known_table(name)
    info = await table_info(name)
    sort = sort if sort in [c["name"] for c in info["columns"]] else ""
    direction = "DESC" if as_str(dir).lower() == "desc" else "ASC"
    where, params = filters_of(info["columns"], col, eq, fcol, like)
    rows = await fetch_rows(name, info["columns"], info["rowid"], sort, direction, where, params,
                            CSV_LIMIT)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([c["name"] for c in info["columns"]])
    for row in rows:
        writer.writerow([row[c["name"]] for c in info["columns"]])
    return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{file_name(name)}.csv"'})


# ── форма строки: создание ───────────────────────────────────────────────────
def _field_html(column: dict, raw, creating: bool) -> str:
    """Поле формы для одной колонки: тип из схемы, текущее значение, подсказки."""
    name, kind = column["name"], column["type"].upper()
    text = "" if raw is None else as_str(raw)
    required = is_required(column)
    if kind == "INTEGER":
        field = f'<input name="{esc(name)}" type="number" step="1" value="{esc(text)}">'
    elif kind == "REAL":
        field = f'<input name="{esc(name)}" type="number" step="any" value="{esc(text)}">'
    elif column["time"]:
        field = (f'<input name="{esc(name)}" value="{esc(text)}" autocomplete="off"'
                 f' placeholder="ГГГГ-ММ-ДД ЧЧ:ММ:СС">')
    else:
        field = f'<input name="{esc(name)}" value="{esc(text)}" autocomplete="off">'
    note = ""
    if column["time"]:
        note = ('<div class="small mut">Формат: ГГГГ-ММ-ДД ЧЧ:ММ:СС — местное время '
                'колледжа, без часового пояса. Час прибавлять не нужно.</div>')
    elif required:
        note = '<div class="small mut">Обязательное поле: без него строку не создать.</div>'
    clear = ""
    if not creating and not is_required(column):
        clear = (f'<label class="small" style="margin-top:4px"><input type="checkbox" '
                 f'name="clear__{esc(name)}" value="1" style="width:auto;margin-right:6px">'
                 f'очистить поле</label>')
    mark = " *" if required else ""
    return (f'<div class="full"><label>{esc(name)}{mark}<span class="small mut"> · '
            f'{esc(column["type"] or "TEXT")}</span></label>{field}{clear}{note}</div>')


def _row_form(request: Request, name: str, columns: list, row, creating: bool, back: str) -> str:
    """Форма правки или создания: поля из PRAGMA table_info."""
    action = (f"/panel/data/table/{quote(name)}/row" if creating
              else f"/panel/data/table/{quote(name)}/row/{row['rowid']}")
    fields = "".join(_field_html(column, "" if creating else row[column["name"]], creating)
                     for column in columns)
    return (f'<form method="post" action="{esc(action)}">{csrf(request)}'
            f'<input type="hidden" name="back" value="{esc(back)}">'
            f'<div class="grid">{fields}</div>'
            f'<div class="grid" style="margin-top:10px">'
            f'<button class="{"btn-ok" if creating else ""}">{icon("check", 16)} '
            f'{"Создать строку" if creating else "Сохранить"}</button>'
            f'<a class="btn-grey" href="{esc(back)}">Отмена</a></div></form>')


@router.get("/data/table/{name}/row/new")
async def data_row_new(request: Request, name: str):
    """Форма новой строки: те же поля, что при правке, но без текущих значений."""
    user = await require_owner(request)
    name = await known_table(name)
    info = await table_info(name)
    back = f"/panel/data/table/{quote(name)}"
    body = (f'<div class="card"><h2>{icon("plus", 20)} Новая строка в таблице {esc(name)}</h2>'
            f'<p class="small mut">В INSERT идут только заполненные поля: остальные получат '
            f'значения по умолчанию из схемы. Пустое поле не значит «стереть» — при правке '
            f'для этого есть галочка «очистить поле».</p>'
            f'{_row_form(request, name, info["columns"], None, True, back)}</div>')
    return page(f"Данные: {name} — новая строка", body, user, "/data")


# ── форма строки: правка ─────────────────────────────────────────────────────
@router.get("/data/table/{name}/row/{rowid}")
async def data_row(request: Request, name: str, rowid: str):
    """Одна строка: форма правки, правила проекта и удаление."""
    user = await require_owner(request)
    name = await known_table(name)
    info = await table_info(name)
    if not info["rowid"] or not as_str(rowid).isdigit():
        raise HTTPException(status_code=404, detail="Такой строки нет")
    found = await db.one(f"SELECT rowid AS rid, * FROM {ident(name)} WHERE rowid=?", (int(rowid),))
    if not found:
        raise HTTPException(status_code=404, detail="Такой строки нет")
    row = {key: found[key] for key in found.keys()}
    row["rowid"] = row.pop("rid")
    base = f"/panel/data/table/{quote(name)}"
    back = f"{base}/row/{rowid}"
    keys = "".join(f'<tr><th class="col-key">{esc(c["name"])}</th>'
                   f'<td>{esc(as_str(row[c["name"]]) or "—")}</td></tr>'
                   for c in info["columns"] if c["pk"])
    note = ('<p class="small mut">Пустое поле ничего не стирает молча: значение меняется, '
            'только если его ввели, а для очистки есть отдельная галочка. Перед записью '
            'делается снимок базы.</p>')
    body = (f'<div class="card"><h2>{icon("edit", 20)} Строка №{esc(as_str(rowid))} таблицы '
            f'<a href="{base}"><code>{esc(name)}</code></a></h2>{keys}{note}'
            f'{_row_form(request, name, info["columns"], row, False, back)}</div>'
            f'{await _danger_card(request, name, row, back)}')
    return page(f"Данные: {name} — строка {rowid}", body, user, "/data")


def _confirm_form(request: Request, action: str, word: str, title: str, note: str, button: str,
                  cls: str = "btn-bad", back: str = "", extra: str = "") -> str:
    """Форма необратимого действия: слово-подтверждение вместо кнопки «да».

    Кнопка «да» не защищает ничего: её нажимают вместе с соседней. Слово
    приходится набрать руками, и ошибиться в нём случайно нельзя.
    """
    return f"""
<form method="post" action="{esc(action)}" class="grid">{csrf(request)}
<input type="hidden" name="back" value="{esc(back)}">{extra}
<div class="full"><label>{esc(title)}</label>
<p class="small mut">{esc(note)} Чтобы подтвердить, введите слово <code>{esc(word)}</code>.</p>
<input name="word" value="" autocomplete="off" placeholder="{esc(word)}"></div>
<div><button class="{esc(cls)}">{button}</button></div></form>"""


def _confirmed(data, word: str) -> bool:
    """Введено ли подтверждающее слово (регистр и лишние пробелы не важны)."""
    return as_str(data.get("word", "")).strip().casefold() == word.casefold()


def _safe_back(data, fallback: str) -> str:
    """Куда вернуться после действия: только свой раздел, иначе палец в общий адрес."""
    back = as_str(data.get("back", "")).strip()
    return back if back.startswith("/panel/data") else fallback


async def _danger_card(request: Request, name: str, row: dict, back: str) -> str:
    """Необратимые действия над строкой: функции репозитория или удаление строки."""
    parts = ['<p class="small mut">Под каждым действием — снимок базы (VACUUM INTO), путь '
             'показывается в сообщении, и запись в журнал действий.</p>']
    if name in GUARDED_TABLES:
        parts.append(f'<p class="msg msg-bad">{icon("warning", 20)} Сырое удаление строк '
                     f'таблицы <code>{esc(name)}</code> запрещено: у человека и у обращения '
                     f'есть правила проекта. Ниже — кнопки, которые зовут репозиторий.</p>')
    else:
        parts.append(_confirm_form(
            request, f"/panel/data/table/{quote(name)}/row/{row['rowid']}/delete", name,
            f"Удалить строку №{esc(as_str(row['rowid']))} из таблицы {esc(name)}",
            "Строка исчезнет из базы. Отменить это можно только из резервной копии.",
            f'{icon("delete", 16)} Удалить строку', "btn-bad", back))
    if name in PERSON_TABLES and as_str(row.get("user_id", "")):
        parts.append(await _person_card(request, as_str(row["user_id"]), back))
    if name == "tickets":
        parts.append(await _ticket_card(request, as_str(row.get("ticket_id", "")), back))
    return f'<div class="card"><h2>{icon("warning", 20)} Опасные действия</h2>{"".join(parts)}</div>'


async def _person_card(request: Request, uid: str, back: str) -> str:
    """Кнопки бизнес-действий над человеком вместо DELETE по таблице.

    Сырой DELETE обошёл бы правила проекта: у человека бывают открытые
    обращения, а роль сис-админа снимается с проверкой «не последний».
    Здесь эти проверки живут в репозитории, и мы только зовём его функции.
    """
    opened = await repo.student_open_tickets_count(uid)
    if await repo.is_owner(uid):
        return (f'<p class="msg msg-ok">{icon("warning", 20)} {code_cell(uid)} — это владелец '
                f'бота (<code>ROOT_IDS</code>). Удалить его нельзя: права заданы в .env, '
                f'не отзываются, и раздел «Данные» открыт только ему.</p>')
    about = (f'<p class="small mut">MAX ID: {code_cell(uid, "MAX ID скопирован")}. Открытых '
             f'обращений: {opened}. Удаление человека убирает регистрацию, карточку '
             f'сотрудника, контакт, состояние и подписки; обращения по умолчанию остаются.</p>')
    return (about
            + _confirm_form(request, f"/panel/data/user/{quote(uid)}/delete", CONFIRM_WORD,
                            f"Удалить человека {uid}", "Проверки проекта выполняет репозиторий.",
                            f'{icon("delete", 16)} Удалить человека', "btn-bad", back)
            + _confirm_form(request, f"/panel/data/user/{quote(uid)}/delete", CONFIRM_WORD,
                            f"Удалить человека {uid} вместе с обращениями",
                            "Вместе с обращениями уйдут переписка и события по каскаду.",
                            f'{icon("delete", 16)} Удалить и обращения', "btn-bad", back,
                            extra='<input type="hidden" name="with_tickets" value="1">')
            + _confirm_form(request, f"/panel/data/user/{quote(uid)}/revoke", CONFIRM_WORD,
                            f"Снять права сотрудника у {uid}",
                            "Репозиторий не даст снять права у последнего сис-админа "
                            "и у владельца.",
                            f'{icon("staff", 16)} Снять права', "btn-grey", back))


async def _ticket_card(request: Request, ticket_id: str, back: str) -> str:
    """Обращение убирают в архив, а не стирают: так его можно вернуть одним действием."""
    if not ticket_id.isdigit():
        return ""
    about = ('<p class="small mut">Переписка и история обращения остаются, вернуть его можно '
             'одним действием. Полная работа с обращением — на вкладке «Обращения».</p>')
    return (about
            + _confirm_form(request, f"/panel/data/ticket/{quote(ticket_id)}/archive", CONFIRM_WORD,
                            f"Убрать обращение №{esc(ticket_id)} в архив",
                            "Обращение пропадёт из работы сотрудника.",
                            f'{icon("archive", 16)} В архив', "btn-grey", back)
            + _confirm_form(request, f"/panel/data/ticket/{quote(ticket_id)}/restore", CONFIRM_WORD,
                            f"Вернуть обращение №{esc(ticket_id)} в работу",
                            "Обращение снова будет видно сотруднику.",
                            f'{icon("refresh", 16)} Вернуть', "btn-grey", back))


# ── запись: правка, создание, удаление ───────────────────────────────────────
def _typed(column: dict, raw: str, problems: list) -> object:
    """Значение поля формы в типе колонки. Ошибка - в problems, значение None."""
    kind = column["type"].upper()
    if column["time"]:
        trouble = time_problem(raw)
        if trouble:
            problems.append(f"{column['name']}: {trouble} (ожидается ГГГГ-ММ-ДД ЧЧ:ММ:СС)")
            return None
    if kind == "INTEGER":
        try:
            return int(raw.strip())
        except ValueError:
            problems.append(f"{column['name']}: нужно целое число")
            return None
    if kind == "REAL":
        try:
            return float(raw.strip())
        except ValueError:
            problems.append(f"{column['name']}: нужно число")
            return None
    return raw


@router.post("/data/table/{name}/row/{rowid}")
async def data_row_save(request: Request, name: str, rowid: str):
    """Сохранить правку строки: меняются только колонки с новым значением.

    Пустое поле означает «не трогать», а не «стереть»: иначе один невнимательный
    клик обнулял бы колонку. Стерётся только то, что отмечено галочкой
    «очистить поле».
    """
    actor = await require_owner_form(request)
    name = await known_table(name)
    data = await request.form()
    back = _safe_back(data, f"/panel/data/table/{quote(name)}")
    if not as_str(rowid).isdigit():
        raise HTTPException(status_code=404, detail="Такой строки нет")
    row = await db.one(f"SELECT * FROM {ident(name)} WHERE rowid=?", (int(rowid),))
    if not row:
        raise HTTPException(status_code=404, detail="Такой строки нет")
    columns = await table_columns(name)
    changes, problems, sets, params, wanted_values = [], [], [], [], []
    for column in columns:
        key = column["name"]
        if f"clear__{key}" in data:
            wanted = "" if column["notnull"] else None
        else:
            raw = as_str(data.get(key, ""))
            if not raw:
                continue
            wanted = _typed(column, raw, problems)
            if problems and problems[-1].startswith(f"{key}:"):
                continue
        if as_str(row[key]) == as_str(wanted):
            continue
        changes.append(f"{key}: «{as_str(row[key])[:40]}» → «{as_str(wanted)[:40]}»")
        sets.append(f"{ident(key)}=?")
        params.append(wanted)
        wanted_values.append((column, wanted))
    if problems:
        flash("!Не сохранено: " + "; ".join(problems[:4]))
        return redirect(f"/panel/data/table/{quote(name)}/row/{rowid}")
    if not sets:
        flash("Ничего не изменилось: пустое поле не затирает значение, "
              "а чтобы стереть, нужна галочка «очистить поле».")
        return redirect(back)
    path = await snapshot(f"правка {name} №{rowid}")
    try:
        await db.run(f"UPDATE {ident(name)} SET {', '.join(sets)} WHERE rowid=?",
                     tuple(params) + (int(rowid),))
    except Exception as exc:  # noqa: BLE001 - причину показываем сис-админу, а не 500
        log.error("панель «Данные»: правка %s №%s не удалась: %s", name, rowid, exc)
        flash(f"!Правка не сохранена: {exc}")
        return redirect(f"/panel/data/table/{quote(name)}/row/{rowid}")
    log.warning("панель «Данные»: %s №%s изменён сис-админом %s: %s", name, rowid, actor,
                "; ".join(changes))
    await repo.log_action(actor, "правка строки данных",
                          f"{name} №{rowid}: " + "; ".join(changes)[:240])
    hint = "".join(canonical_note(column["name"], value) for column, value in wanted_values)
    flash(f"Сохранено полей: {len(sets)} — {name} №{rowid}.{hint} {snapshot_text(path)}")
    return redirect(back)


@router.post("/data/table/{name}/row")
async def data_row_create(request: Request, name: str):
    """Создать строку: в INSERT идут только заполненные поля."""
    actor = await require_owner_form(request)
    name = await known_table(name)
    data = await request.form()
    columns = await table_columns(name)
    keys, params, problems, used = [], [], [], []
    for column in columns:
        key = column["name"]
        raw = as_str(data.get(key, ""))
        if not raw:
            if is_required(column):
                problems.append(f"{key} — обязательное поле")
            continue
        value = _typed(column, raw, problems)
        if problems and problems[-1].startswith(f"{key}:"):
            continue
        keys.append(ident(key))
        params.append(value)
        used.append((column, value))
    if problems:
        flash("!Строка не создана: " + "; ".join(problems[:4]))
        return redirect(f"/panel/data/table/{quote(name)}/row/new")
    path = await snapshot(f"новая строка {name}")
    try:
        rowid = await db.run(f"INSERT INTO {ident(name)}({', '.join(keys)}) "
                             f"VALUES({', '.join('?' * len(keys))})", tuple(params))
    except Exception as exc:  # noqa: BLE001 - уникальность и внешние ключи видны сис-админу
        log.error("панель «Данные»: вставка в %s не удалась: %s", name, exc)
        flash(f"!Строка не создана: {exc}")
        return redirect(f"/panel/data/table/{quote(name)}/row/new")
    log.warning("панель «Данные»: в %s добавлена строка №%s сис-админом %s", name, rowid, actor)
    await repo.log_action(actor, "добавлена строка данных", f"{name} №{rowid}: полей {len(keys)}")
    hint = "".join(canonical_note(column["name"], value) for column, value in used)
    flash(f"Строка создана: {name} №{rowid}.{hint} {snapshot_text(path)}")
    return redirect(f"/panel/data/table/{quote(name)}/row/{rowid}")


@router.post("/data/table/{name}/row/{rowid}/delete")
async def data_row_delete(request: Request, name: str, rowid: str):
    """Удалить строку: только после слова-подтверждения и только после снимка базы."""
    actor = await require_owner_form(request)
    name = await known_table(name)
    data = await request.form()
    back = _safe_back(data, f"/panel/data/table/{quote(name)}")
    if name in GUARDED_TABLES:
        flash("!Сырое удаление строк в этой таблице запрещено: у человека и обращения есть "
              "правила проекта. Кнопки выше зовут функции репозитория.")
        return redirect(back)
    if not as_str(rowid).isdigit():
        raise HTTPException(status_code=404, detail="Такой строки нет")
    row = await db.one(f"SELECT * FROM {ident(name)} WHERE rowid=?", (int(rowid),))
    if not row:
        raise HTTPException(status_code=404, detail="Такой строки нет")
    if not _confirmed(data, name):
        flash(f"!Слово «{name}» не введено — строка не удалена.")
        return redirect(f"/panel/data/table/{quote(name)}/row/{rowid}")
    path = await snapshot(f"удаление {name} №{rowid}")
    await db.run(f"DELETE FROM {ident(name)} WHERE rowid=?", (int(rowid),))
    shown = ", ".join(f"{key}={as_str(row[key])[:24]}" for key in row.keys())[:200]
    log.warning("панель «Данные»: удалена строка %s №%s сис-админом %s: %s", name, rowid, actor, shown)
    await repo.log_action(actor, "удалена строка данных", f"{name} №{rowid}: {shown}")
    flash(f"Строка {name} №{rowid} удалена. {snapshot_text(path)}")
    return redirect(back)


# ── действия с человеком: только через функции репозитория ───────────────────
@router.post("/data/user/{user_id}/delete")
async def data_user_delete(request: Request, user_id: str):
    """Удаление человека - только через store.people.delete_user.

    Репозиторий откажет, если у человека открытые обращения (без with_tickets)
    или он сис-админ. Владельца бота удалить нельзя, и это запрещено здесь, до
    снимка базы: его права заданы в ROOT_IDS и не отзываются.
    """
    actor = await require_owner_form(request)
    data = await request.form()
    uid = as_str(user_id)
    back = _safe_back(data, f"/panel/data/table/users?q={quote(uid)}")
    if await repo.is_owner(uid):
        log.warning("панель «Данные»: попытка удалить владельца бота id=%s (сис-админ %s)",
                    uid, actor)
        await repo.log_action(actor, "попытка удалить владельца", f"id={uid}: запрещено")
        flash("!Владельца бота удалить нельзя: его права заданы в ROOT_IDS и не отзываются.")
        return redirect(back)
    if not _confirmed(data, CONFIRM_WORD):
        flash(f"!Слово «{CONFIRM_WORD}» не введено — человек не удалён.")
        return redirect(back)
    with_tickets = as_str(data.get("with_tickets", "")) == "1"
    path = await snapshot(f"удаление человека {uid}")
    done, message = await repo.delete_user(uid, with_tickets=with_tickets)
    if not done:
        flash(f"!Удаление не выполнено: {message}")
        return redirect(back)
    log.warning("панель «Данные»: удалён человек %s сис-админом %s (с обращениями: %s)",
                uid, actor, with_tickets)
    await repo.log_action(actor, "удаление человека", f"id={uid}, с обращениями: {with_tickets}")
    flash(f"{message}. {snapshot_text(path)}")
    return redirect("/panel/data")


@router.post("/data/user/{user_id}/revoke")
async def data_user_revoke(request: Request, user_id: str):
    """Снятие прав: у сис-админа - revoke_sysadmin, у сотрудника - delete_staff."""
    actor = await require_owner_form(request)
    data = await request.form()
    uid = as_str(user_id)
    back = _safe_back(data, f"/panel/data/table/admins?q={quote(uid)}")
    if await repo.is_owner(uid):
        log.warning("панель «Данные»: попытка снять права владельца id=%s (сис-админ %s)",
                    uid, actor)
        await repo.log_action(actor, "попытка снять права владельца", f"id={uid}: запрещено")
        flash("!У владельца бота нельзя снять права: они заданы в ROOT_IDS.")
        return redirect(back)
    if not _confirmed(data, CONFIRM_WORD):
        flash(f"!Слово «{CONFIRM_WORD}» не введено — права не сняты.")
        return redirect(back)
    path = await snapshot(f"снятие прав {uid}")
    admin = await repo.get_admin(uid)
    if admin and is_sysadmin_role(as_str(admin["role_type"])):
        done, message = await repo.revoke_sysadmin(uid)
    else:
        await repo.delete_staff(uid)
        done, message = True, f"Карточка сотрудника {uid} удалена"
    if not done:
        flash(f"!Права не сняты: {message}")
        return redirect(back)
    log.warning("панель «Данные»: сняты права %s сис-админом %s", uid, actor)
    await repo.log_action(actor, "снятие прав сотрудника", f"id={uid}: {message}")
    flash(f"{message}. {snapshot_text(path)}")
    return redirect("/panel/data")


# ── обращение: архив и возврат, а не удаление строки ─────────────────────────
@router.post("/data/ticket/{ticket_id}/{action}")
async def data_ticket(request: Request, ticket_id: str, action: str):
    """Архив и возврат обращения - через store.tickets, а не DELETE по строке."""
    actor = await require_owner_form(request)
    data = await request.form()
    back = _safe_back(data, "/panel/data/table/tickets")
    if action not in ("archive", "restore") or not as_str(ticket_id).isdigit():
        raise HTTPException(status_code=404, detail="Такого действия нет")
    number = int(ticket_id)
    if action == "archive":
        done, message = await repo.archive_ticket(number, actor)
    else:
        done, message = await repo.restore_ticket(number, actor)
    if not done:
        flash(f"!Действие не выполнено: {message}")
        return redirect(back)
    log.warning("панель «Данные»: обращение №%s — %s (сис-админ %s)", number, action, actor)
    await repo.log_action(actor, f"обращение {action}", f"№{number}: {message}")
    flash(f"{message}.")
    return redirect(back)
