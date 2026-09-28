"""Журнал, самопроверка и JSON-API для скриптов."""
import time

import config
import database as db
import repository as repo
from fastapi import Request
from fastapi.responses import JSONResponse
from handlers.common import api as max_api
from handlers.common import notify
from panel_theme import icon
from utils import as_str, log_level_of, profile_url, tail_file

from .common import (LOG_LINES, csrf, esc, flash, log, page, panel_enabled, redirect, require_form,
                     require_user, select, value)
from .mailer import _broadcasts_table
from .router import router


# ── журнал и тесты ────────────────────────────────────────────────────────────
@router.get("/logs")
async def logs_page(request: Request, lines: int = LOG_LINES, level: str = ""):
    user = await require_user(request)
    lines = max(20, min(int(lines or LOG_LINES), 2000))
    records = tail_file(config.LOG_FILE, lines)
    if level:
        records = [r for r in records if log_level_of(r) == level]
    options = {"": "все уровни", "DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING",
               "ERROR": "ERROR", "CRITICAL": "CRITICAL"}
    dedupe = await repo.dedupe_stats()
    recent = await repo.broadcast_history(5)
    tail = "\n".join(records) or "Журнал пуст — бот ещё ничего не писал."
    body = f"""
<div class="card"><h2>{icon("check", 20)} Проверки</h2>
<form method="post" action="/panel/logs/test" class="grid">
{csrf(request)}
<div><label>Отправить тестовое сообщение сис-админу (MAX ID)</label><input name="to" value="{esc(user)}"></div>
<div><button>{icon("send", 16)} Отправить</button></div>
<div><button name="action" value="api" class="btn-grey">{icon("check", 16)} Проверить API MAX</button></div>
<div><button name="action" value="ping" class="btn-grey">{icon("logs", 16)} Записать строку в журнал</button></div>
</form>
<table style="margin-top:12px">
<tr><th>Обработано событий</th><td>{esc(dedupe['processed'])}</td></tr>
<tr><th>Самое новое событие</th><td>{esc(dedupe['newest'] or '—')}</td></tr>
<tr><th>Самое старое в очистке</th><td>{esc(dedupe['oldest'] or '—')}</td></tr>
</table></div>
<div class="card"><h2>{icon("logs", 20)} Журнал: {esc(config.LOG_FILE)}</h2>
<form method="get" action="/panel/logs" class="grid" style="margin-bottom:10px">
<div>{select('level', options, level, label="Уровень")}</div>
<div><label>Строк</label><input name="lines" type="number" value="{lines}" min="20" max="2000"></div>
<div><button>{icon("search", 16)} Показать</button></div></form>
<pre>{esc(tail)}</pre></div>
<div class="card"><h2>Последние рассылки</h2>{_broadcasts_table(recent)}</div>"""
    return page("Журнал", body, user, "/logs")


@router.post("/logs/test")
async def logs_test(request: Request):
    user = await require_form(request)
    data = await request.form()
    action = value(data, "action", default="message")
    to = value(data, "to") or user
    if action == "api":
        try:
            info = await max_api.me()
            flash(f"API отвечает: {info}")
        except Exception as exc:
            log.warning("проверка API не удалась: %s", exc)
            flash(f"!API недоступен: {exc}")
        return redirect("/panel/logs")
    if action == "ping":
        log.info("тестовая запись в журнал от сис-админа %s", user)
        flash("Запись добавлена в журнал — обновите страницу.")
        return redirect("/panel/logs")
    if not to.isdigit():
        flash("!MAX ID состоит только из цифр.")
        return redirect("/panel/logs")
    ok = await notify(to, f"🧪 Тестовое сообщение панели. Вы вошли как {user}, {time.strftime('%d.%m.%Y %H:%M')}")
    flash("Тестовое сообщение отправлено." if ok else f"!Не удалось отправить: {to} (он не запускал бота?)")
    return redirect("/panel/logs")


# ── JSON-API: проверки и скрипты ──────────────────────────────────────────────
@router.get("/api/stats")
async def api_stats(request: Request):
    await require_user(request)
    return {
        "overview": await repo.stats_overview(),
        "statuses": await repo.status_counts(),
        "dedupe": await repo.dedupe_stats(),
        "groups": await repo.top_groups(50),
    }


@router.get("/api/logs")
async def api_logs(request: Request, lines: int = 200, level: str = ""):
    await require_user(request)
    records = tail_file(config.LOG_FILE, max(1, min(int(lines or 200), 5000)))
    if level:
        records = [r for r in records if log_level_of(r) == level]
    return {"file": config.LOG_FILE, "count": len(records), "lines": records}


@router.get("/api/broadcasts")
async def api_broadcasts(request: Request):
    await require_user(request)
    return {"broadcasts": [dict(row) for row in await repo.broadcast_history(100)]}


@router.get("/api/people")
async def api_people(request: Request, kind: str = "", q: str = "", limit: int = 200):
    """Реестр пользователей: ФИО, роль, ник, ссылка на профиль MAX."""
    await require_user(request)
    rows = await repo.people(kind, q, limit=max(1, min(int(limit or 200), 2000)))
    people_out = []
    for row in rows:
        username = as_str(row["username"]).strip()
        people_out.append({
            "user_id": as_str(row["user_id"]),
            "name": as_str(row["fio"]) or as_str(row["staff_name"]) or as_str(row["display_name"]),
            "kind": repo.contact_kind(row),
            "group": as_str(row["group_code"]),
            "position": as_str(row["position"]),
            "department": as_str(row["department"]),
            "username": username,
            "profile": profile_url(username),
            "tickets": row["tickets"],
            "last_seen": as_str(row["last_seen"]),
        })
    return {"count": len(people_out), "people": people_out}


@router.get("/api/requests")
async def api_requests(request: Request, status: str = "new"):
    """Заявки на роль сотрудника и нерасшифрованные коды."""
    await require_user(request)
    requests_out = [dict(row) for row in await repo.staff_requests(status, 200)]
    invites = []
    for row in await repo.list_invites(50):
        invites.append({
            "code": as_str(row["code"]),
            "state": await repo.invite_state(row["code"]),
            "user_id": as_str(row["user_id"]),
            "full_name": as_str(row["full_name"]) or as_str(row["fio"]),
            "expires_at": as_str(row["expires_at"]),
            "used_by": as_str(row["used_by"]),
        })
    return {"requests": requests_out, "invites": invites, "attempts": [dict(r) for r in await repo.attempts_log(30)]}


@router.get("/api/health")
async def api_health(request: Request):
    """Диагностика без авторизации: только состояние, без данных."""
    try:
        await db.one("SELECT 1")
        database_ok = True
    except Exception as exc:  # noqa: BLE001 — в диагностике важно показать саму ошибку
        log.error("health: база недоступна: %s", exc)
        database_ok = False
    missing = await db.missing_objects() if database_ok else []
    return JSONResponse({
        "ok": database_ok and not missing,
        "panel_enabled": panel_enabled(),
        "mode": "webhook" if config.WEBHOOK_URL else "polling",
        "log_file": config.LOG_FILE,
        "sysadmins": len(config.SYSADMIN_IDS),
        "missing_schema": missing,
    })
