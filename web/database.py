"""Состояние базы, обслуживание и резервные копии."""
import os
import tempfile

import config
import database as db
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from panel_theme import icon
from starlette.background import BackgroundTask
from utils import as_str, fmt_time

from .common import (_action_form, csrf, esc, flash, log, page, pill, redirect, require_form,
                     require_user)
from .router import router


# ── база данных: состояние, обслуживание, резервные копии ──────────────────────
PRUNE_JOBS = {
    "processed_updates": ("Отпечатки обработанных событий", 2),   # таблица, подпись, варианты дней
    "login_attempts": ("Попытки ввода кода", 30),
    "user_states": ("Зависшие состояния диалогов", 1),
}


PRUNE_TITLES = {"processed_updates": "дубли событий", "login_attempts": "попытки ввода кода",
                "user_states": "зависшие состояния"}


def _remove_file(path: str) -> None:
    """Удаляет временный файл выгрузки — вызывается после отправки ответа."""
    try:
        os.remove(path)
    except OSError:
        pass


def human_bytes(value) -> str:
    size = float(as_str(value) or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def _prune_form(request: Request, table: str, default_days: int) -> str:
    """Выбор срока и кнопка чистки одной служебной таблицы."""
    options = "".join(
        f"<option value='{d}'{' selected' if d == default_days else ''}>старше {d} дн.</option>"
        for d in (1, 3, 7, 30, 90)
    )
    question = f"Удалить записи «{PRUNE_TITLES.get(table, table)}» выбранного срока?"
    return (f'<form method="post" action="/panel/database/prune" class="inline" '
            f"onclick=\"return confirm('{esc(question)}')\">"
            f'{csrf(request)}<input type="hidden" name="table" value="{esc(table)}">'
            f"<select name='days'>{options}</select>"
            f"<button class='btn-grey'>{icon("delete", 16)} Очистить</button></form>")


def schema_broken_page(request: Request, detail: str, missing: list[str]):
    """Страница для случая «в базе не хватает объектов» вместо 500 со стектрейсом."""
    items = "".join(f"<li><code>{esc(item)}</code></li>" for item in missing) or "<li>—</li>"
    return page(
        "База требует восстановления",
        f"""<div class="card"><h2>{icon("warning", 20)} В базе не хватает объектов</h2>
<p>Бот не может работать, пока схема неполна: {esc(detail)}.</p>
<p>Чего не хватает:</p><ul>{items}</ul>
<p>Кнопка создаёт недостающие таблицы, индексы и колонки. Данные, которые уже
есть, не меняются, но удалённую таблицу придётся наполнить заново (например,
заново зарегистрировать студентов) — либо восстановить базу из резервной копии.</p>
<div class="grid">
{_action_form(request, '/panel/database/repair', f'{icon("settings", 16)} Восстановить схему', cls='btn-ok')}
<a class="btn-grey" href="/panel/database">Вкладка «База данных»</a>
</div></div>""",
    )


@router.get("/database")
async def database_page(request: Request):
    user = await require_user(request)
    sizes = db.file_sizes()
    storage = await db.storage_info()
    check = await db.integrity_check()
    counts = await db.table_counts()
    backups = db.list_backups()
    missing = await db.missing_objects()

    banner = ""
    if missing:
        banner = f"""
<div class="card" style="border-left:4px solid var(--bad)"><h2>{icon("warning", 20)} Схема базы неполна</h2>
<p>Не хватает: {esc(', '.join(missing))}. Из-за этого часть страниц панели и бот могут падать.</p>
{_action_form(request, '/panel/database/repair', f'{icon("settings", 16)} Восстановить схему', cls='btn-ok')}</div>"""

    verdict = pill("цела", "on") if check == "ok" else pill(as_str(check))
    state_rows = "".join(
        f"<tr><td>Файл базы</td><td><code>{esc(db.database_file())}</code></td></tr>"
        f"<tr><td>Размер (с журналом WAL)</td><td>{esc(human_bytes(sizes['total']))} "
        f"<span class='small mut'>({esc(human_bytes(sizes['db']))} + {esc(human_bytes(sizes['wal']))})</span></td></tr>"
        f"<tr><td>SQLite</td><td>{esc(storage['sqlite_version'])}</td></tr>"
        f"<tr><td>Режим журнала</td><td>{esc(storage['journal_mode'])}</td></tr>"
        f"<tr><td>Страниц / свободно</td><td>{storage['page_count']} / {storage['freelist_count']}</td></tr>"
        f"<tr><td>Можно освободить сжатием</td><td>{esc(human_bytes(storage['reclaimable']))}</td></tr>"
        f"<tr><td>Проверка целостности</td><td>{verdict}</td></tr>"
        f"<tr><td>Схема</td><td>{pill("соответствует коду", "on") if not missing else
        f'{icon("warning", 14)} не хватает: {esc(", ".join(missing))}'}</td></tr>"
        f"<tr><td>Резервных копий</td><td>{len(backups)} (хранится {config.BACKUP_KEEP}, "
        f"папка <code>{esc(db.backups_folder())}</code>)</td></tr>"
    )
    counts_rows = "".join(
        f"<tr><td><code>{esc(name)}</code></td><td>{esc(count)}</td></tr>" for name, count in counts
    ) or "<tr><td class='mut'>Таблиц нет</td></tr>"
    prune_rows = "".join(
        f"<tr><td>{esc(label)}</td><td>{_prune_form(request, table, default_days)}</td></tr>"
        for table, (label, default_days) in PRUNE_JOBS.items()
    )
    backup_rows = "".join(
        f"<tr><td><b>{esc(item['name'])}</b></td><td>{esc(human_bytes(item['size']))}</td>"
        f"<td class='small mut'>{esc(fmt_time(item['mtime']))}</td>"
        f"<td><a class='btn-grey' href='/panel/database/backup/{esc(item['name'])}'>{icon("download", 16)} Скачать</a> "
        f"{_action_form(request, '/panel/database/restore', f'{icon("refresh", 16)} Восстановить', f'<input type=\"hidden\" name=\"name\" value=\"{esc(item['name'])}\">', 'Восстановить базу из этой копии? Текущие данные будут заменены, перед этим бот сделает страховочную копию.')}"
        f" {_action_form(request, '/panel/database/backup/delete', icon("delete", 16), f'<input type=\"hidden\" name=\"name\" value=\"{esc(item['name'])}\">', 'Удалить копию?', 'btn-bad')}</td></tr>"
        for item in backups
    ) or "<tr><td colspan='4' class='mut'>Копий пока нет</td></tr>"

    body = f"""
{banner}
<div class="card"><h2>{icon("database", 20)} Состояние базы</h2>
<table>{state_rows}</table>
<div style="margin-top:12px" class="grid">
  {_action_form(request, '/panel/database/backup', f'{icon("database", 16)} Создать копию', cls='btn-ok')}
  <a class="btn-grey" href="/panel/database/download">{icon("download", 16)} Скачать текущую базу</a>
</div></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>{icon("settings", 20)} Обслуживание</h2>
  <p class="small mut">Сжатие безопасно: освободившиеся страницы уходят в конец файла, данные не меняются.
  Слияние журнала WAL нужно, если вы копируете файл базы вручную.</p>
  <div class="grid">
    {_action_form(request, '/panel/database/vacuum', f'{icon("check", 16)} Сжать (VACUUM)')}
    {_action_form(request, '/panel/database/checkpoint', f'{icon("download", 16)} Слить журнал WAL')}
  </div>
  <h2>Чистка служебных таблиц</h2>
  <table>{prune_rows}</table>
  <p class="small mut">Обращения, сообщения, сотрудники и реестр пользователей чистка не трогает.</p></div>
  <div class="card" style="flex:1"><h2>{icon("database", 20)} Данные по таблицам</h2>
  <table><tr><th>Таблица</th><th>Строк</th></tr>{counts_rows}</table></div>
</div>
<div class="card"><h2>{icon("archive", 20)} Резервные копии</h2>
<table><tr><th>Файл</th><th>Размер</th><th>Создан</th><th>Действия</th></tr>{backup_rows}</table></div>"""
    return page("База данных", body, user, "/database")


@router.post("/database/repair")
async def database_repair(request: Request):
    """Создаёт недостающие таблицы, индексы и колонки (init_db идемпотентен)."""
    user = await require_form(request)
    created = await db.repair_schema()
    if created:
        log.warning("панель: восстановлена схема — %s (сис-админ %s)", ", ".join(created), user)
        flash("Создано: " + ", ".join(created) + ". Удалённые таблицы пусты — их нужно наполнить заново.")
    else:
        flash("Схема в порядке: ничего дописывать не пришлось.")
    return redirect("/panel/database")


@router.post("/database/vacuum")
async def database_vacuum(request: Request):
    user = await require_form(request)
    before = db.file_sizes()["db"]
    await db.vacuum()
    after = db.file_sizes()["db"]
    freed = max(0, before - after)
    log.info("панель: база сжата (сис-админ %s), освобождено %s байт", user, freed)
    flash(f"База сжата: {human_bytes(before)} → {human_bytes(after)} (освобождено {human_bytes(freed)}).")
    return redirect("/panel/database")


@router.post("/database/checkpoint")
async def database_checkpoint(request: Request):
    user = await require_form(request)
    pages = await db.checkpoint()
    log.info("панель: журнал WAL слит (сис-админ %s), страниц %s", user, pages)
    flash(f"Журнал WAL слит в базу ({pages} страниц).")
    return redirect("/panel/database")


@router.post("/database/prune")
async def database_prune(request: Request):
    user = await require_form(request)
    data = await request.form()
    table = as_str(data.get("table", ""))
    days = max(1, min(int(as_str(data.get("days", "7")) or 7), 3650))
    if table not in PRUNE_JOBS:
        raise HTTPException(status_code=400, detail="Такую таблицу чистить нельзя")
    removed = await db.prune(table, days)
    log.info("панель: очищена %s (старше %s дн.) — %s строк (сис-админ %s)", table, days, removed, user)
    flash(f"Удалено записей: {removed} — {PRUNE_TITLES.get(table, table)}, старше {days} дн.")
    return redirect("/panel/database")


@router.post("/database/backup")
async def database_backup(request: Request):
    user = await require_form(request)
    path = await db.backup_to()
    log.info("панель: создана резервная копия %s (сис-админ %s)", os.path.basename(path), user)
    flash(f"Копия создана: {os.path.basename(path)} ({human_bytes(os.path.getsize(path))}).")
    return redirect("/panel/database")


@router.get("/database/download")
async def database_download(request: Request):
    """Отдаёт согласованную копию текущей базы (VACUUM INTO во временный файл)."""
    user = await require_user(request)
    path = await db.backup_to(os.path.join(tempfile.gettempdir(), f"bot-lpc-{db.backup_name()}"))
    log.info("панель: база выгружена файлом (сис-админ %s)", user)
    return FileResponse(path, filename=os.path.basename(path), media_type="application/octet-stream",
                        background=BackgroundTask(_remove_file, path))


@router.get("/database/backup/{name}")
async def database_backup_download(request: Request, name: str):
    await require_user(request)
    path = db.backup_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="Копия не найдена")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.post("/database/backup/delete")
async def database_backup_delete(request: Request):
    user = await require_form(request)
    data = await request.form()
    name = as_str(data.get("name", ""))
    if not db.delete_backup(name):
        raise HTTPException(status_code=404, detail="Копия не найдена")
    log.info("панель: удалена копия %s (сис-админ %s)", name, user)
    flash(f"Копия {name} удалена.")
    return redirect("/panel/database")


@router.post("/database/restore")
async def database_restore(request: Request):
    user = await require_form(request)
    data = await request.form()
    name = as_str(data.get("name", ""))
    done, message = await db.restore_from(name)
    if not done:
        flash(f"!Восстановление не выполнено: {message}")
        return redirect("/panel/database")
    log.warning("панель: база восстановлена из %s (сис-админ %s), страховочная копия %s", name, user, message)
    flash(f"База восстановлена из {name}. Прежнее состояние сохранено в {message}. Перезапустите бота.")
    return redirect("/panel/database")
