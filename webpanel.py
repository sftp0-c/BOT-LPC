"""Веб-панель сис-админа: /panel — те же данные, что и в боте, но редактируются мышью.

Панель живёт в том же процессе, что и бот (FastAPI), поэтому все изменения сразу
видны боту и в MAX. Вход — по MAX ID сис-админа и паролю WEB_PANEL_PASSWORD из .env;
пока пароль не задан, панель отвечает 503.

Вкладки: обзор, обращения, пользователи, сотрудники, коды и заявки, база данных,
настройки, журнал и тесты. JSON-API для скриптов и проверок — /panel/api/*.
"""
import csv
import hmac
import io
import logging
import os
import re
import secrets
import tempfile
import time
from html import escape as _escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

import config
import database as db
import repository as repo
import timetable as tt
import charts
from handlers import schedules
from handlers.admin import STAFF_ROLES, approve_request, notify_schedule_subscribers, probe_pdf_url, reject_request
from handlers.broadcast import run_broadcast
from handlers.common import api as max_api
from handlers.common import notify, spawn
from timetable import WEEKDAYS_FULL
from utils import (
    CATS,
    CODE_TTL_CHOICES,
    OPEN_STATUSES,
    STAFF_CATS,
    STATUS,
    as_str,
    fmt_time,
    fmt_when,
    gen_code,
    is_sysadmin_role,
    log_level_of,
    norm_group,
    profile_url,
    short,
    tail_file,
    to_int,
    ttl_label,
    valid_group,
)

log = logging.getLogger("panel")
router = APIRouter(prefix="/panel", tags=["panel"])

COOKIE = "lpc_panel"          # имя cookie-сессии
LOG_LINES = 400               # сколько строк журнала показывать по умолчанию
_flash = ""                   # одноразовое сообщение для следующей страницы


# ── сессии и доступ ───────────────────────────────────────────────────────────
# _sessions: токен сессии → (MAX ID, время окончания)
# _csrf: токен сессии → CSRF-токен формы. Раньше токеном формы был сам cookie
# сессии: утечка cookie давала и CSRF-защиту. Теперь это разные значения.
_sessions: dict[str, tuple[str, float]] = {}
_csrf: dict[str, str] = {}


def _purge_sessions() -> None:
    now = time.time()
    for token in [t for t, (_, exp) in _sessions.items() if exp < now]:
        _sessions.pop(token, None)
        _csrf.pop(token, None)


def start_session(user_id: str) -> str:
    """Новый токен сессии; время жизни — WEB_PANEL_HOURS."""
    _purge_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = (str(user_id), time.time() + max(1, config.WEB_PANEL_HOURS) * 3600)
    _csrf[token] = secrets.token_urlsafe(32)
    return token


def end_session(token: str) -> None:
    _sessions.pop(token, None)
    _csrf.pop(token, None)


def form_token(request: Request) -> str:
    """CSRF-токен текущей сессии; подставляется в каждую форму."""
    return _csrf.get(request.cookies.get(COOKIE, ""), "")


def session_user(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    item = _sessions.get(token)
    if not item or item[1] < time.time():
        _sessions.pop(token, None)
        return ""
    return item[0]


def panel_enabled() -> bool:
    return bool(config.WEB_PANEL_PASSWORD)


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303)
async def is_sysadmin(user_id: str) -> bool:
    """Своим ли ID пользователь: запись в БД (в т.ч. владелец) или SYSADMIN_IDS из .env."""
    return await repo.is_sysadmin(user_id)


async def require_user(request: Request) -> str:
    """MAX ID вошедшего сис-админа, иначе — 503 (панель выключена) или 303 на вход."""
    if not panel_enabled():
        raise HTTPException(status_code=503, detail="Панель выключена: задайте WEB_PANEL_PASSWORD в .env")
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/panel/login"})
    if not await is_sysadmin(user):
        raise HTTPException(status_code=403, detail="Панель доступна только сис-админам")
    return user


async def require_form(request: Request) -> str:
    """То, что require_user, плюс проверка CSRF-токена из формы."""
    user = await require_user(request)
    form_data = await request.form()
    expected = form_token(request)
    given = as_str(form_data.get("csrf", ""))
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail="Устаревшая форма — обновите страницу")
    return user


# ── HTML ──────────────────────────────────────────────────────────────────────
STYLE = """
:root{--bg:#eef1f6;--card:#fff;--line:#dde3ec;--ink:#1b2430;--mut:#67748a;--acc:#2563eb;
      --acc-soft:#eff4ff;--bad:#d13b32;--ok:#17915b;--warn:#c98a12;--radius:12px;
      --shadow:0 1px 2px rgba(16,24,40,.06),0 4px 12px rgba(16,24,40,.06)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
     -webkit-font-smoothing:antialiased}
a{color:var(--acc)}
header{background:linear-gradient(135deg,#1e293b,#2b3b53);color:#fff;padding:14px 22px;
      display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline}
header h1{margin:0;font-size:17px;font-weight:650;letter-spacing:.2px}
header .sub{color:#a8b8cc;font-size:13px;margin-left:auto}
nav{display:flex;flex-wrap:wrap;gap:2px;background:#fff;border-bottom:1px solid var(--line);
    padding:0 10px;position:sticky;top:0;z-index:5}
nav a{color:#4a586c;padding:11px 13px;text-decoration:none;font-size:14px;font-weight:500;
      border-bottom:2px solid transparent;border-radius:6px 6px 0 0}
nav a:hover{background:var(--acc-soft);color:var(--acc)}
nav a.on{color:var(--acc);border-bottom-color:var(--acc);font-weight:600}main{max-width:1180px;margin:22px auto;padding:0 18px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
      padding:18px 20px;margin-bottom:18px;box-shadow:var(--shadow)}
.card h2{margin:0 0 14px;font-size:16px;font-weight:650}
.card h2:first-child{margin-top:-2px}
h3{margin:20px 0 8px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.3px;
   white-space:nowrap;background:#fafbfd}
tbody tr:hover{background:#f8fafd}
tr:last-child td{border-bottom:0}
.cards{display:flex;flex-wrap:wrap;gap:12px}
.stat{flex:1 1 150px;background:linear-gradient(180deg,#fff,#f7f9fc);border:1px solid var(--line);
      border-radius:var(--radius);padding:14px 16px}
.stat b{display:block;font-size:26px;line-height:1.2;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:13px}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid #cdd6e2;border-radius:8px;
      font:inherit;background:#fff;color:var(--ink);transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--acc);
     box-shadow:0 0 0 3px rgba(37,99,235,.12)}
textarea{min-height:110px;resize:vertical}
label{display:block;margin:8px 0 4px;font-size:12.5px;color:var(--mut);font-weight:500}
button,.btn{display:inline-block;padding:8px 14px;border:0;border-radius:8px;background:var(--acc);
     color:#fff;font:inherit;font-weight:500;cursor:pointer;text-decoration:none;
     transition:filter .15s,transform .05s}
button:hover,.btn:hover{filter:brightness(1.07)}
button:active,.btn:active{transform:translateY(1px)}
.btn-grey{background:#64748b}.btn-bad{background:var(--bad)}.btn-ok{background:var(--ok)}
.grid{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.grid>*{flex:1 1 170px}
.grid .full{flex:1 1 100%}
.mut{color:var(--mut)}.small{font-size:13px}
pre{background:#0f1723;color:#d3e2f0;padding:16px;border-radius:var(--radius);overflow:auto;
     max-height:560px;font:12.5px/1.5 Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.msg{padding:12px 14px;border-radius:10px;margin-bottom:14px;font-size:14px}
.msg-ok{background:#e8f7ef;border:1px solid #b6e2c9;color:#12603d}
.msg-bad{background:#fdeceb;border:1px solid #f3c3bf;color:#8f241d}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;background:#eef1f5;color:#41505f;
      font-size:12px;font-weight:500}
.pill-on{background:#e6f6ec;color:#14663c}.pill-off{background:#f1f2f4;color:#77808a}
form.inline{display:inline}
footer{color:var(--mut);font-size:12px;padding:12px 18px 28px;text-align:center}

/* ── диаграммы и карточки аналитики ─────────────────────────────────────── */
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.chart-box{margin:0;background:#fbfcfe;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.chart-box figcaption{font-size:13px;font-weight:600;color:var(--mut);margin-bottom:8px}
.chart{width:100%;height:auto;display:block}
.chart .grid-line{stroke:#e6eaf0;stroke-width:1}
.chart .axis{font-size:10px;fill:#8b95a3}
.donut{width:150px;height:150px;flex:0 0 150px}
.donut-total{font-size:22px;font-weight:700;fill:var(--ink)}
.donut-sub{font-size:11px;fill:var(--mut)}
.donut-wrap{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-top:6px}
.legend span{display:flex;align-items:center;gap:5px}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.legend-list{list-style:none;margin:0;padding:0;flex:1 1 130px;font-size:13px}
.legend-list li{display:flex;align-items:center;gap:7px;padding:3px 0}
.legend-list i{width:10px;height:10px;border-radius:2px;flex:0 0 10px}
.legend-list span{flex:1;color:#41505f}
.legend-list b{font-variant-numeric:tabular-nums}
.hbar-list{list-style:none;margin:0;padding:0;font-size:13px}
.hbar-list li{display:flex;align-items:center;gap:8px;padding:3px 0}
.hbar-label{flex:0 0 34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#41505f}
.hbar-track{flex:1;background:#eef1f5;border-radius:4px;height:12px;overflow:hidden}
.hbar-track i{display:block;height:100%;border-radius:4px}
.hbar-list b{flex:0 0 34px;text-align:right;font-variant-numeric:tabular-nums}
.chart-empty{color:#98a2b3;font-size:13px;padding:26px 0;text-align:center}
.spark svg{height:46px}
.kpi{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.kpi div{flex:1 1 120px;background:linear-gradient(180deg,#fff,#f7f9fc);border:1px solid var(--line);
     border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:26px;line-height:1.15;font-variant-numeric:tabular-nums}
.kpi span{color:var(--mut);font-size:12.5px}
.kpi .warn b{color:#c0392b}
.kpi .good b{color:#1d8a4e}
@media (max-width:640px){
    main{padding:0 10px}
    .donut-wrap{flex-direction:column;align-items:flex-start}
    .hbar-label{flex-basis:45%}
}
"""


def esc(value) -> str:
    return _escape(as_str(value), quote=True)


def flag(value) -> bool:
    return as_str(value).strip().lower() in ("1", "true", "yes", "on")


TABS = (
    ("/", "Обзор"),
    ("/tickets", "Обращения"),
    ("/analytics", "Аналитика"),
    ("/people", "Пользователи"),
    ("/nostaff", "Без прав"),
    ("/students", "Студенты"),
    ("/staff", "Сотрудники"),
    ("/access", "Коды и заявки"),
    ("/templates", "Шаблоны"),
    ("/groups", "Группы"),
    ("/schedules", "Расписания"),
    ("/broadcasts", "Рассылки"),
    ("/database", "База данных"),
    ("/settings", "Настройки"),
    ("/logs", "Журнал и тесты"),
)


def page(title: str, body: str, user: str = "", tab: str = "") -> str:
    global _flash
    nav = "".join(
        f'<a href="/panel{path}" class="{"on" if path == tab else ""}">{esc(name)}</a>' for path, name in TABS
    )
    notice, _flash = _flash, ""
    kind = "bad" if notice.startswith("!") else "ok"
    banner = f'<div class="msg msg-{kind}">{esc(notice.lstrip("!"))}</div>' if notice else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — панель сис-админа</title><style>{STYLE}</style></head><body>
<header><h1>🏫 BOT-LPC — панель сис-админа</h1>
<div class="sub">вошёл как {esc(user) or '—'} · <a style="color:#9fb0c3" href="/panel/logout">выйти</a></div></header>
<nav>{nav}</nav><main>{banner}{body}</main>
<footer>Данные те же, что и в боте: изменения применяются сразу. MAX ID: {esc(user)}</footer>
</body></html>"""
    )


def flash(message: str) -> None:
    """Одноразовое сообщение для следующей страницы (ошибка помечается «!»)."""
    global _flash
    _flash = message[:300]


def csrf(request: Request) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(form_token(request))}">'


def form(request: Request, action: str, fields: str, submit: str = "Сохранить", cls: str = "") -> str:
    return (
        f'<form method="post" action="{esc(action)}">{csrf(request)}'
        f'<div class="grid">{fields}</div>'
        f'<div class="grid" style="margin-top:10px"><button class="{cls}">{esc(submit)}</button></div></form>'
    )


def input(name: str, value="", kind: str = "text", full: bool = False) -> str:
    extra = ' class="full"' if full else ""
    return f'<div{extra}><label>{esc(name)}</label><input name="{esc(name)}" type="{kind}" value="{esc(value)}"></div>'


def select(name: str, options: dict, current: str, full: bool = False) -> str:
    items = "".join(
        f'<option value="{esc(code)}"{" selected" if code == current else ""}>{esc(label)}</option>'
        for code, label in options.items()
    )
    extra = ' class="full"' if full else ""
    return f'<div{extra}><label>{esc(name)}</label><select name="{esc(name)}">{items}</select></div>'


def value(form, *names: str, default: str = "") -> str:
    """Первое непустое значение из формы: принимает «новое» или «старое» имя поля."""
    for name in names:
        found = as_str(form.get(name, "")).strip()
        if found:
            return found
    return default


def redirect(path: str) -> RedirectResponse:
    return _redirect(path)


# ── вход и выход ──────────────────────────────────────────────────────────────
@router.get("/login")
async def login_page(request: Request):
    if not panel_enabled():
        return page(
            "Панель выключена",
            '<div class="card"><h2>Панель выключена</h2><p class="mut">Впишите в <code>.env</code> строку '
            "<code>WEB_PANEL_PASSWORD=ваш_пароль</code> и перезапустите бота.</p></div>",
        )
    if session_user(request):
        return redirect("/panel/")
    return page(
        "Вход",
        """<div class="card" style="max-width:420px;margin:40px auto"><h2>Вход для сис-админа</h2>
<form method="post" action="/panel/login">
<label>MAX ID</label><input name="user_id" autofocus required>
<label>Пароль</label><input name="password" type="password" required>
<div class="grid" style="margin-top:12px"><button>Войти</button></div></form>
<p class="small mut" style="margin-bottom:0">MAX ID — кнопка «🔐 Сис-админ» в боте или команда <code>/id</code>.
Роль сис-админа проверяется по SYSADMIN_IDS и таблице сотрудников.</p></div>""",
    )


@router.post("/login")
async def login_submit(request: Request):
    form_data = await request.form()
    if not panel_enabled():
        raise HTTPException(status_code=503, detail="Панель выключена")
    user_id = as_str(form_data.get("user_id", "")).strip()
    password = as_str(form_data.get("password", ""))
    ok_password = hmac.compare_digest(password.encode(), config.WEB_PANEL_PASSWORD.encode())
    if not user_id or not ok_password or not await is_sysadmin(user_id):
        log.warning("неудачный вход в панель: id=%s", user_id or "?")
        raise HTTPException(status_code=403, detail="Неверный MAX ID или пароль")
    token = start_session(user_id)
    log.info("вход в панель: %s", user_id)
    response = redirect("/panel/")
    response.set_cookie(
        COOKIE, token, max_age=max(1, config.WEB_PANEL_HOURS) * 3600,
        httponly=True, samesite="lax", path="/panel",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    end_session(request.cookies.get(COOKIE, ""))
    response = redirect("/panel/login")
    response.delete_cookie(COOKIE, path="/panel")
    return response


# ── обзор ─────────────────────────────────────────────────────────────────────
@router.get("/")
async def overview(request: Request):
    user = await require_user(request)
    st = await repo.stats_overview()
    people = await repo.people_overview()
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    groups = await repo.top_groups(10)
    tickets = await repo.admin_tickets(None, 10)
    last_bc = (await repo.broadcast_history(3)) or []

    def stat(value_, label: str) -> str:
        return f'<div class="stat"><b>{esc(value_)}</b><span>{esc(label)}</span></div>'

    open_total = sum(counts.get(code, 0) for code in OPEN_STATUSES)
    today = await repo.admin_today()
    cards = "".join([
        stat(today["no_answer"], "без ответа"),
        stat(today["requests"], "заявок"),
        stat(st["students"], "студентов"),
        stat(st["staff"], "сотрудников"),
        stat(people["total"], "писали боту"),
        stat(st["total"], "обращений всего"),
        stat(open_total, "открытых"),
        stat(st["week"], "за 7 дней"),
        stat(today["tickets_day"], "за сутки"),
        stat(dedupe["processed"], "событий обработано"),
    ])
    group_rows = "".join(
        f"<tr><td><a href='/panel/groups'>{esc(row['group_code'])}</a></td><td>{esc(row['students'])}</td></tr>"
        for row in groups
    ) or "<tr><td class='mut'>Групп пока нет</td></tr>"
    status_rows = "".join(
        f"<tr><td>{esc(label)}</td><td>{esc(counts.get(code, 0))}</td></tr>" for code, label in STATUS.items()
    )
    ticket_rows = _tickets_table(tickets)
    bc_rows = _broadcasts_table(last_bc)
    body = f"""
<div class="cards">{cards}</div>
<div class="card"><h2>Что сделать сегодня</h2>
<table>
<tr><th>Обращений без ответа</th><td>{today['no_answer']}</td><th>Заявок на роль сотрудника</th><td>{today['requests']}</td></tr>
<tr><th>Готово к выдаче, но не отмечено</th><td>{today['ready_not_picked']}</td><th>Активных кодов</th><td>{today['codes_active']}</td></tr>
<tr><th>Зависших диалогов</th><td>{today['stuck_states']}</td><th>Обращений за сутки</th><td>{today['tickets_day']}</td></tr>
<tr><th>Среднее время ответа</th><td>{esc(today['avg_reply'] or '—')}</td><th>Обращений за 7 дней</th><td>{st['week']}</td></tr>
</table></div>
<div class="card"><h2>Последние обращения</h2>{ticket_rows}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Студенты по группам</h2>
    <table><tr><th>Группа</th><th>Студентов</th></tr>{group_rows}</table></div>
  <div class="card" style="flex:1"><h2>Обращения по статусам</h2>
    <table>{status_rows}</table></div>
</div>
<div class="card"><h2>Последние рассылки</h2>{bc_rows}</div>"""
    return page("Обзор", body, user, "/")


def _tickets_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Обращений пока нет.</p>"
    body = "".join(
        f"<tr><td><a href='/panel/tickets/{esc(row['ticket_id'])}'>№{esc(row['ticket_id'])}</a></td>"
        f"<td>{esc(row['student_id'])}</td><td>{esc(row['target_admin_id'])}</td>"
        f"<td>{esc(row['category'])}</td><td>{esc(STATUS.get(row['status'], row['status']))}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    )
    return f"<table><tr><th>№</th><th>Студент</th><th>Сотрудник</th><th>Категория</th><th>Статус</th><th>Создано</th></tr>{body}</table>"


def _broadcasts_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Рассылок пока не было.</p>"
    body = "".join(
        f"<tr><td><a href='/panel/broadcasts'>#{esc(row['id'])}</a></td>"
        f"<td>{esc(row['sender_name'] or row['sender_id'])}"
        f"{' <span class=\"small mut\">' + esc(row['sender_role']) + '</span>' if row['sender_role'] else ''}</td>"
        f"<td>{esc('всем' if row['audience'] == 'all' else row['audience'])}</td>"
        f"<td class='small'>{esc((row['text'] or '')[:90])}</td>"
        f"<td>✅{esc(row['sent'])} ❌{esc(row['failed'])}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    )
    return (f"<table><tr><th>№</th><th>Отправитель</th><th>Кому</th><th>Текст</th>"
            f"<th>Доставлено</th><th>Когда</th></tr>{body}</table>")


@router.get("/templates")
async def templates_page(request: Request):
    """Шаблоны ответов: что сотрудники отвечают чаще всего и почему."""
    user = await require_user(request)
    rows = await repo.list_templates(limit=100)
    total = len(rows)
    body_rows = ""
    for row in rows:
        title = as_str(row["title"])
        confirm = f"Удалить шаблон «{title}»?"
        body_rows += (
            f"<tr><td><b>{esc(title)}</b><div class='small mut'>ID {esc(row['id'])}</div></td>"
            f"<td class='small'>{esc((as_str(row['text']) or '')[:220])}</td>"
            f"<td>{esc(STAFF_CATS.get(row['category'], row['category']))}</td>"
            f"<td>{esc(row['used_count'])}</td>"
            f"<td class='small mut'>{esc(fmt_when(row['created_at']))}</td>"
            f"<td>{_action_form(request, f'/panel/templates/{esc(row['id'])}/delete', 'Удалить', confirm_text=confirm, cls='btn-bad')}"
            f"</td></tr>"
        )
    body_rows = body_rows or "<tr><td class='mut'>Шаблонов пока нет</td></tr>"
    table = ("<table><tr><th>Название</th><th>Текст</th><th>Раздел</th><th>Применён</th>"
             f"<th>Добавлен</th><th></th></tr>{body_rows}</table>")
    add = form(
        request, "/panel/templates/add",
        ('<div class="full"><label>Название - как это выглядит в кнопке</label>'
         '<input name="title" placeholder="Справка готова"></div>')
        + ('<div class="full"><label>Текст ответа</label>'
           '<textarea name="text" placeholder="Здравствуйте! Справка готова, заберите её в кабинете 214."></textarea></div>')
        + select("category", dict(STAFF_CATS), "all"),
        "Добавить шаблон", "btn-ok",
    )
    body_all = f"""
<div class="card"><h2>⚡ Шаблоны ответов: {total}</h2>
<p class="small mut">Сотрудник в карточке обращения нажимает «⚡ Шаблоны» - выбирает подходящий
и отправляет как есть или дописывает своё. Шаблон с разделом «🔁 Всё» показывается всегда,
остальные - только в своём разделе. Колонка «Применён» показывает, какие ответы реально нужны.</p>
{table}</div>
<div class="card"><h2>Добавить шаблон</h2>{add}</div>"""
    return page("Шаблоны ответов", body_all, user, "/templates")


@router.post("/templates/add")
async def templates_add(request: Request):
    user = await require_form(request)
    data = await request.form()
    title = value(data, "title")
    text = value(data, "text")
    if not title or not text:
        flash("!Нужны и название, и текст ответа.")
        return redirect("/panel/templates")
    template_id = await repo.add_template(title, text, value(data, "category") or "all", user)
    log.info("панель: добавлен шаблон «%s» (сис-админ %s)", title, user)
    await repo.log_action(user, "шаблон ответа добавлен", f"{title} (ID {template_id})")
    flash(f"Шаблон «{title}» добавлен.")
    return redirect("/panel/templates")


@router.post("/templates/{template_id}/delete")
async def templates_delete(request: Request, template_id: int):
    user = await require_form(request)
    template = await repo.get_template(template_id)
    if not template:
        flash("!Шаблон не найден.")
        return redirect("/panel/templates")
    await repo.delete_template(template_id)
    log.info("панель: удалён шаблон %s (сис-админ %s)", template_id, user)
    await repo.log_action(user, "шаблон ответа удалён", f"{template['title']} (ID {template_id})")
    flash(f"Шаблон «{template['title']}» удалён.")
    return redirect("/panel/templates")


# ── аналитика ─────────────────────────────────────────────────────────────────
DASH_PERIODS = (7, 30, 90)


def minutes_text(value) -> str:
    """Минуты в человеческий вид: «2 ч 15 мин», «45 мин», «—»."""
    minutes = to_int(value, 0)
    if minutes <= 0:
        return "—"
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч {minutes % 60} мин" if minutes % 60 else f"{minutes // 60} ч"
    return f"{minutes // (24 * 60)} дн"


@router.get("/analytics")
async def analytics_page(request: Request, days: int = 30):
    """Диаграммы без внешних библиотек: динамика, статусы, разделы, нагрузка."""
    user = await require_user(request)
    days = days if days in DASH_PERIODS else 30
    per_day = await repo.tickets_by_day(days)
    speed = await repo.response_speed(days)
    load = await repo.staff_load(days)
    by_status = await repo.tickets_by_status()
    by_category = await repo.tickets_by_category()
    groups = await repo.students_by_group()
    status_rows = [{"status": row["status"], "label": STATUS.get(row["status"], row["status"]),
                    "count": row["count"]} for row in by_status]
    category_rows = [{"category": row["category"],
                      "label": STAFF_CATS.get(row["category"], row["category"] or "без раздела"),
                      "count": row["count"]} for row in by_category]
    unanswered = [row for row in by_status if row["status"] in OPEN_STATUSES]
    kpi = f"""
<div class="kpi">
  <div><b>{speed['total']}</b><span>обращений за {days} дн.</span></div>
  <div class="good"><b>{speed['share']}%</b><span>получили ответ</span></div>
  <div><b>{esc(minutes_text(speed['avg_minutes']))}</b><span>среднее время ответа</span></div>
  <div class="warn"><b>{esc(minutes_text(speed['worst_minutes']))}</b><span>худший ответ</span></div>
  <div class="warn"><b>{sum(row['count'] for row in unanswered)}</b><span>сейчас открыто</span></div>
</div>"""
    switcher = "".join(
        f'<a class="btn{"-grey" if d != days else ""}" href="/panel/analytics?days={d}">{d} дн.</a>'
        for d in DASH_PERIODS)
    load_rows = "".join(
        f"<tr><td><b>{esc(row['full_name'])}</b><div class='small mut'>ID {esc(row['user_id'])}</div></td>"
        f"<td>{row['tickets']}</td><td>{row['open']}</td>"
        f"<td>{esc(minutes_text(row['avg_minutes']))}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['last_reply'])) if row['last_reply'] else '—'}</td></tr>"
        for row in load
    ) or "<tr><td class='mut'>Сотрудников пока нет</td></tr>"
    # мини-график имеет смысл только когда есть хотя бы два дня с данными
    spark = charts.sparkline([row["count"] for row in per_day], "Всего обращений за период") \
        if len(per_day) >= 2 else ""
    body = f"""
{kpi}
<div class="card"><h2>Динамика обращений</h2>
<div class="grid" style="margin-bottom:10px">{switcher}</div>
<div class="charts">
  {charts.bar_chart(per_day, 'count', 'day', f'Обращения по дням, {days} дн.', second_key='done')}
  {spark}
  {charts.donut(status_rows, 'count', 'label', 'Статусы обращений')}
  {charts.donut(category_rows, 'count', 'label', 'Разделы обращений')}
  {charts.bars(load, 'tickets', 'full_name', f'Обращения у сотрудников за {days} дн.', color='#8b5cf6')}
  {charts.bars(groups, 'count', 'group', 'Студенты по группам', color='#06b6d4')}
</div></div>
<div class="card"><h2>Нагрузка на сотрудников</h2>
<table><tr><th>Сотрудник</th><th>Обращений</th><th>Открытых</th><th>Средний ответ</th><th>Последний ответ</th></tr>
{load_rows}</table>
<p class="small mut">Среднее время ответа считается по первой ответной реплике сотрудника.
Обращения без ответа в среднее не попадают, но видны в колонке «Открытых».</p></div>"""
    return page("Аналитика", body, user, "/analytics")


# ── обращения ─────────────────────────────────────────────────────────────────
EVENT_LABELS = {"created": "обращение создано", "status": "статус", "ready": "документ готов",
                "message_student": "сообщение студента", "message_staff": "ответ сотрудника"}


@router.get("/tickets")
async def tickets_list(request: Request, status: str = "", q: str = "", category: str = "",
                       scope: str = ""):
    """Очередь обращений с фильтрами: статус, раздел, «только ждут ответа»."""
    user = await require_user(request)
    rows = await repo.admin_tickets(None, 200)
    if status == "open":
        rows = [r for r in rows if as_str(r["status"]) in OPEN_STATUSES]
    elif status:
        rows = [r for r in rows if as_str(r["status"]) == status]
    if category:
        rows = [r for r in rows if as_str(r["category"]) == category]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in as_str(r["text_content"]).lower() or needle in as_str(r["student_id"])]
    counts = await repo.status_counts()
    latest = await repo.latest_message_roles([row["ticket_id"] for row in rows])
    if scope == "waiting":
        rows = [row for row in rows if latest.get(int(row["ticket_id"])) == "student"]
    waiting = sum(1 for row in rows if latest.get(int(row["ticket_id"])) == "student")
    options = {"": "все статусы", "open": "🔓 открытые"} | {code: label for code, label in STATUS.items()}
    cat_options = {"": "все разделы", **{code: label for code, label in CATS.items()}}
    filters = f"""
<form method="get" action="/panel/tickets" class="grid" style="margin-bottom:14px">
<div>{select("status", options, status)}</div>
<div>{select("category", cat_options, category)}</div>
<div><label>Поиск по тексту или ID</label><input name="q" value="{esc(q)}"></div>
<div><button>Найти</button></div></form>
<p class="small mut"><a class="btn{'-grey' if scope != 'waiting' else ''}" href="/panel/tickets?status={esc(status)}&category={esc(category)}&q={esc(q)}{'&scope=waiting' if scope != 'waiting' else ''}">🔔 Только ждут ответа: {waiting}</a></p>"""
    summary = " · ".join(f"{STATUS.get(c, c)}: {counts.get(c, 0)}" for c in STATUS)
    return page("Обращения", f'<div class="card"><p class="small mut">{esc(summary)}</p>{filters}'
                 f"{_tickets_table(rows)}<p class=\"small mut\">Показано обращений: {len(rows)}</p></div>",
                 user, "/tickets")


@router.get("/tickets/{ticket_id}")
async def ticket_card(request: Request, ticket_id: int):
    user = await require_user(request)
    t = await repo.get_ticket(ticket_id)
    if not t:
        return page("Обращение", '<div class="card msg-bad">Обращение не найдено.</div>', user, "/tickets")
    student = t.get("student") or {}
    staff = t.get("staff") or {}
    messages = await repo.ticket_thread(ticket_id, 100)
    history = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(m['created_at']))}</td>"
        f"<td class='small'>{'🎓 студент' if m['sender_role'] == 'student' else '🏫 сотрудник'} "
        f"{esc(m['sender_name'])}{' · ' + esc(m['position']) if m['position'] else ''}"
        f"<div class='small mut'>{esc(m['group_code'] or m['sender_id'])}</div></td>"
        f"<td>{esc(m['text'])}</td></tr>"
        for m in reversed(messages)
    ) or "<tr><td colspan='3' class='mut'>Сообщений нет</td></tr>"
    events = await repo.ticket_events(ticket_id, 50)
    event_rows = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(e['created_at']))}</td>"
        f"<td class='small'>{esc(e['actor_name'] or e['actor_id'])}</td>"
        f"<td class='small'>{esc(EVENT_LABELS.get(e['event'], e['event']))}: {esc(e['detail'] or '—')}</td></tr>"
        for e in reversed(events)
    ) or "<tr><td colspan='3' class='mut'>Событий нет</td></tr>"
    status_options = dict(STATUS)
    head = f"""
<div class="card"><h2>Обращение №{esc(t['ticket_id'])}</h2>
<table>
<tr><th>Студент</th><td>{esc(student.get('full_name', '—'))} (ID {esc(student.get('user_id', t['student_id']))}),
    группа {esc(student.get('group_code', '—'))}</td></tr>
<tr><th>Сотрудник</th><td>{esc(staff.get('full_name', '—'))} (ID {esc(t['target_admin_id'])}),
    {esc(staff.get('position') or staff.get('role') or '—')}
    {('· ' + esc(staff['department'])) if staff.get('department') else ''}</td></tr>
<tr><th>Категория</th><td>{esc(t['category'])} {esc(t['topic'] or '')}</td></tr>
<tr><th>Статус</th><td>{esc(STATUS.get(t['status'], t['status']))}</td></tr>
<tr><th>Текст</th><td>{esc(t['text_content'])}</td></tr>
<tr><th>Создано</th><td>{esc(fmt_when(t['created_at']))}</td></tr>
<tr><th>Обновлено</th><td>{esc(fmt_when(t['updated_at']))}</td></tr>
</table>
<div style="margin-top:14px">{form(request, f"/panel/tickets/{t['ticket_id']}/status",
     select("status", status_options, t["status"]), "Сменить статус", "btn-ok")}</div></div>
<div class="card"><h2>Быстрый ответ</h2>
<form method="post" action="/panel/tickets/{t['ticket_id']}/reply">{csrf(request)}
<textarea name="text" rows="3" required placeholder="Ответ студенту — уйдёт в MAX от имени сотрудника"></textarea>
<div class="grid" style="margin-top:10px"><button class="btn-ok">Отправить</button></div></form>
<p class="small mut">Ответ уходит студенту и появляется в переписке бота.</p></div>
<div class="card"><h2>Переписка</h2><table><tr><th>Когда</th><th>Кто</th><th>Текст</th></tr>{history}</table></div>
<div class="card"><h2>История</h2><table><tr><th>Когда</th><th>Кто</th><th>Событие</th></tr>{event_rows}</table></div>
<div class="card"><h2>Удаление</h2>
<p class="small mut">Обращение удаляется вместе с перепиской и историей — восстановить его можно
только из резервной копии базы (вкладка «База данных»). Студент получит уведомление.</p>
{_action_form(request, f'/panel/tickets/{ticket_id}/delete', '🗑 Удалить обращение',
              f'Удалить обращение №{ticket_id} вместе с перепиской? Действие необратимо.', 'btn-bad')}</div>"""
    return page(f"Обращение №{ticket_id}", head, user, "/tickets")


@router.post("/tickets/{ticket_id}/delete")
async def ticket_delete(request: Request, ticket_id: int):
    actor = await require_form(request)
    t = await repo.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    done, message = await repo.delete_ticket(ticket_id)
    if not done:
        flash(f"!{message}")
        return redirect("/panel/tickets")
    await repo.log_action(actor, "обращение удалено", f"№{ticket_id} (автор {t['student_id']})")
    await notify(t["student_id"],
                 f"🗑 Обращение №{ticket_id} удалено администратором. Если вопрос остался актуальным — "
                 "напишите новое обращение.")
    log.warning("панель: %s (сис-админ %s)", message, actor)
    flash(message)
    return redirect("/panel/tickets")


@router.post("/tickets/{ticket_id}/reply")
async def ticket_reply(request: Request, ticket_id: int):
    user = await require_form(request)
    data = await request.form()
    text = as_str(data.get("text", "")).strip()
    t = await repo.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if not text:
        flash("!Пустой ответ не отправлен.")
        return redirect(f"/panel/tickets/{ticket_id}")
    await repo.add_ticket_message(ticket_id, user, "staff", text)
    if as_str(t["status"]) in ("new", "in_progress"):
        await repo.transition_ticket_status(ticket_id, t["status"], "accepted", actor_id=user)
    await notify(
        t["student_id"], f"💬 Ответ на обращение №{ticket_id}:\n\n{text}",
        [[{"type": "callback", "text": "📂 Открыть", "payload": f"t:{ticket_id}"}]],
    )
    log.info("панель: ответ на обращение %s от %s", ticket_id, user)
    flash(f"Ответ на обращение №{ticket_id} отправлен.")
    return redirect(f"/panel/tickets/{ticket_id}")


@router.post("/tickets/{ticket_id}/status")
async def ticket_status(request: Request, ticket_id: int):
    user = await require_form(request)
    form_data = await request.form()
    status = as_str(form_data.get("status", "")).strip()
    t = await repo.get_ticket(ticket_id)
    if not t or status not in STATUS:
        raise HTTPException(status_code=400, detail="Нет такого обращения или статуса")
    await repo.set_ticket_status(ticket_id, status, actor_id=user)
    await notify(t["student_id"],
                 f"🔔 Статус обращения №{ticket_id}: {STATUS[status]}",
                 [[{"type": "callback", "text": "📂 Открыть", "payload": f"t:{ticket_id}"}]])
    log.info("панель: статус обращения %s → %s (сис-админ %s)", ticket_id, status, user)
    flash(f"Статус обращения №{ticket_id}: {STATUS[status]}")
    return redirect(f"/panel/tickets/{ticket_id}")


# ── студенты ──────────────────────────────────────────────────────────────────
@router.get("/students")
async def students(request: Request, group: str = ""):
    user = await require_user(request)
    rows = await repo.list_users(200, group)
    groups = await repo.top_groups(300)
    options = {"": "все группы"} | {row["group_code"]: row["group_code"] for row in groups}
    head = ('<form method="get" action="/panel/students" class="grid" style="margin-bottom:14px">'
            f'<div>{select("group", options, norm_group(group))}</div>'
            "<div><button>Показать</button></div></form>")
    body = "".join(
        f"<tr><td>{esc(row['full_name'])}</td><td>{esc(row['user_id'])}</td><td>{esc(row['group_code'])}</td>"
        f"<td>{esc(row['tickets'])}</td><td class='small mut'>{esc(row['created_at'])}</td>"
        f"<td><a class='btn-grey' href='/panel/people/{esc(row['user_id'])}'>Открыть</a> "
        f"{_action_form(request, f'/panel/people/{esc(row['user_id'])}/delete', '🗑', confirm_text=f'Удалить {row['full_name']} ({row['user_id']})? Обращения останутся.')}</td></tr>"
        for row in rows
    ) or "<tr><td class='mut'>Студентов не найдено</td></tr>"
    table = (f"<table><tr><th>ФИО</th><th>MAX ID</th><th>Группа</th><th>Обращений</th><th>В базе с</th><th></th></tr>"
             f"{body}</table>")
    return page("Студенты", f'<div class="card">{head}{table}<p class="small mut">Показаны первые 200.</p></div>',
                user, "/students")


# ── сотрудники и сис-админы ───────────────────────────────────────────────────
@router.get("/staff")
async def staff_list(request: Request, q: str = ""):
    user = await require_user(request)
    rows = await repo.all_admins()
    sysadmins = await repo.list_sysadmins()
    activity = await repo.staff_activity(90)
    needle = as_str(q).strip().lower()
    if needle:
        rows = [row for row in rows if needle in " ".join([
            as_str(row["user_id"]), as_str(row["full_name"]), as_str(row["position"]),
            as_str(row["department"]), as_str(row["office"])]).lower()]
    body = ""
    role_options = {"": "— не назначена —", **{code: label for code, label in STAFF_ROLES.items()}}
    for row in rows:
        uid = as_str(row["user_id"])
        super_row = is_sysadmin_role(as_str(row["role_type"]))
        cat_options = dict(STAFF_CATS)
        load = activity.get(uid, {})
        tickets_90 = load.get("tickets", 0)
        head_row = f"""
<tr><td><b>{esc(row['full_name'])}</b><div class="small mut">ID {esc(uid)}</div></td>
<td>{"сис-админ" if super_row else "сотрудник"}</td>
<td>{esc(as_str(row['position']) or STAFF_ROLES.get(row['role'], '—'))}<div class="small mut">{esc(row['role'])}</div></td>
<td>{esc(as_str(row['department']) or '—')}</td>
<td>{esc(as_str(row['office']) or '—')}</td>
<td>{esc(STAFF_CATS.get(row['ticket_category'], row['ticket_category']))}</td>
<td class="small">{tickets_90} / {load.get('open', 0)}<div class="small mut">{esc(fmt_when(load['last_reply'])) if load.get('last_reply') else 'не отвечал'}</div></td>
<td>{"✅" if flag(row['can_broadcast']) else "—"}</td></tr>"""
        if super_row:
            body += head_row
            continue
        fields = (
            f'<div class="full"><label>ФИО</label><input name="full_name" value="{esc(row["full_name"])}"></div>'
            f"{input('position', row['position'])}"
            f"{input('department', row['department'])}"
            f"{select('role', role_options, row['role'])}"
            f"{input('office', row['office'])}"
            f"{select('ticket_category', cat_options, row['ticket_category'])}"
            f"{select('can_broadcast', {'0': 'нет', '1': 'да'}, '1' if flag(row['can_broadcast']) else '0')}"
        )
        body += (f"{head_row}<tr><td colspan='8' style='background:#f8fafc;padding:10px'>"
                 f"{form(request, f'/panel/staff/{esc(uid)}', fields)}"
                 f"<form method='post' action='/panel/staff/promote/{esc(uid)}' class='inline'>{csrf(request)}"
                 f"<button class='btn-grey'>🔐 Сделать сис-админом</button></form> "
                 f"<form method='post' action='/panel/staff/{esc(uid)}/delete' class='inline' "
                 f"onclick=\"return confirm('Удалить сотрудника {esc(row['full_name'])}?')\">"
                 f"{csrf(request)}<button class='btn-bad'>Удалить</button></form></td></tr>")
    search = ('<form method="get" action="/panel/staff" class="grid" style="margin-bottom:14px">'
              f'<div><input name="q" value="{esc(q)}" placeholder="Поиск: ФИО, ID, должность, отдел, кабинет"></div>'
              "<div><button>Найти</button></div></form>")
    table = f'{search}<table><tr><th>Сотрудник</th><th>Роль в боте</th><th>Должность</th><th>Отдел</th><th>Кабинет</th>' \
            f"<th>Обращения</th><th>За 90 дней / открытых</th><th>Рассылка</th></tr>{body}</table>"
    add = form(
        request, "/panel/staff/add",
        ('<div class="full"><label>MAX ID — можно сразу нескольких (через запятую, @ник или ссылку на профиль)</label>'
         '<input name="user_id" value="" placeholder="12345, 67890"></div>')
        + input("full_name", "") + input("position", "") + input("department", "")
        + select("role", role_options, "") + input("office", "")
        + select("ticket_category", dict(STAFF_CATS), "all")
        + select("can_broadcast", {"0": "нет", "1": "да"}, "0"),
        "Добавить сотрудника", "btn-ok",
    )
    add_sys = form(
        request, "/panel/staff/sysadmin",
        ('<div class="full"><label>MAX ID — можно сразу нескольких</label>'
         '<input name="user_id" value="" placeholder="12345, 67890"></div>'),
        "Выдать права сис-админа", "btn-ok",
    )
    revoke_sys = form(
        request, "/panel/staff/sysadmin/revoke",
        ('<div class="full"><label>Снять права у нескольких — MAX ID через запятую</label>'
         '<input name="user_id" value="" placeholder="12345, 67890"></div>'),
        "Снять права", "btn-bad",
    )
    revoked = await repo.revoked_sysadmins()
    sys_rows = ""
    for row in sysadmins:
        name = row["full_name"]
        owner = row.get("is_owner")
        badge = ("<span class='pill pill-on'>владелец</span> " if owner else "")
        source = "из .env" if row["in_env"] else "выдан в панели"
        revoke = ("<span class='small mut'>права постоянны</span>" if owner else
                  _action_form(request, f'/panel/staff/sysadmin/{esc(row["user_id"])}/revoke', 'Снять права',
                               confirm_text=f"Снять права сис-админа с {name} ({row['user_id']})? Панель ему будет недоступна."))
        sys_rows += (
            f"<tr><td><b>{esc(name)}</b>{badge}<div class='small mut'>ID {esc(row['user_id'])}</div></td>"
            f"<td>{esc(source)}</td>"
            f"<td class='small'>{profile_cell(row['username'])}</td>"
            f"<td class='small mut'>{esc(fmt_when(row['last_seen'])) if row['last_seen'] else 'ещё не писал боту'}</td>"
            f"<td>{revoke}</td></tr>"
        )
    sys_rows = sys_rows or "<tr><td class='mut'>Сис-админов нет</td></tr>"
    revoked_rows = "".join(
        f"<tr><td>ID {esc(uid)}</td><td class='small mut'>был в .env, права сняты в панели</td>"
        f"<td>{_action_form(request, f'/panel/staff/sysadmin/{esc(uid)}/restore', 'Вернуть', cls='btn-ok')}</td></tr>"
        for uid in sorted(revoked)
    )
    sysadmins_card = f"""
<div class="card"><h2>🔐 Сис-админы</h2>
<table><tr><th>Кто</th><th>Источник</th><th>Профиль MAX</th><th>Был в боте</th><th></th></tr>{sys_rows}</table>
{('<table><tr><th>Отозванные</th><th>Почему</th><th></th></tr>' + revoked_rows + '</table>') if revoked_rows else ''}
<div class="grid" style="margin-top:12px">{add_sys}{revoke_sys}
<p class="small mut">Список живёт в базе. <code>SYSADMIN_IDS</code> в .env заводит сис-админов при старте,
но права, снятые здесь, перезапуск не вернёт — иначе нельзя было бы отозвать доступа.
<code>ROOT_IDS</code> — владелец бота: максимальные права на корневом уровне, снять их нельзя.
Можно выдать и снять права сразу у нескольких человек — впишите ID через запятую.</p></div></div>"""
    body_all = f"""
{sysadmins_card}
<div class="card"><h2>Сотрудники</h2>{table}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Добавить сотрудника</h2>{add}
  <p class="small mut">Можно вписать сразу несколько ID через запятую — должность, отдел и категория
  применятся ко всем. Тех, кто уже есть в списке, добавлять не нужно: пропустим и покажем кого.
  Тех, кто писал боту, но прав ещё не имеет, видно на вкладке <a href="/panel/nostaff">Без прав</a>.</p></div>
</div>"""
    return page("Сотрудники", body_all, user, "/staff")


@router.post("/staff/add")
async def staff_add(request: Request):
    """Добавляет сотрудника — одного или сразу нескольких по списку ID."""
    user = await require_form(request)
    data = await request.form()
    targets, missing = await repo.staff_targets(value(data, "user_id"))
    if not targets:
        flash("!Введите MAX ID: он состоит только из цифр (или @ник, или ссылка на профиль). "
              "Можно несколько через запятую." + (f" Ники не найдены: {', '.join(missing)}." if missing else ""))
        return redirect("/panel/staff")
    common = {
        "position": value(data, "position"),
        "department": value(data, "department"),
        "office": value(data, "office"),
        "role": value(data, "role"),
        "ticket_category": value(data, "ticket_category", default="all") or "all",
        "can_broadcast": 1 if value(data, "can_broadcast") == "1" else 0,
    }
    typed_name = value(data, "full_name")
    entries = [{"user_id": item["user_id"],
                "full_name": (typed_name if len(targets) == 1 else "") or item["full_name"]}
               for item in targets]
    skipped = [item for item in targets if item["exists"]]
    added = await repo.add_staff_many(entries, **common)
    for uid in added:
        await notify(uid, "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, "
                          "чтобы открыть кабинет.")
    parts = [f"Добавлено сотрудников: {len(added)}"]
    if skipped:
        parts.append("Уже были в списке, пропущены: " + ", ".join(item["user_id"] for item in skipped))
    if missing:
        parts.append("Ники не найдены: " + ", ".join(f"@{nick}" for nick in missing))
    log.info("панель: добавлено сотрудников %s (сис-админ %s)", len(added), user)
    await repo.log_action(user, "сотрудники добавлены", ", ".join(added) or "—")
    flash(". ".join(parts))
    return redirect("/panel/staff")


@router.post("/staff/promote/{user_id}")
async def staff_promote(request: Request, user_id: str):
    """Кнопка «Сделать сис-админом» прямо в строке сотрудника."""
    user = await require_form(request)
    admin = await repo.get_admin(user_id)
    if not admin:
        flash("!Сотрудник не найден.")
        return redirect("/panel/staff")
    done, message = await repo.grant_sysadmin(user_id, as_str(admin["full_name"]))
    if not done:
        flash(f"!{message}.")
        return redirect("/panel/staff")
    await notify(user_id, "🔐 Вам выдали права сис-админа бота колледжа: в панели появится кнопка «🔐 Сис-админ».")
    log.info("панель: %s (сис-админ %s)", message, user)
    await repo.log_action(user, "права сис-админа выданы", message)
    flash(message + ".")
    return redirect("/panel/staff")


@router.post("/staff/sysadmin/revoke")
async def staff_revoke_many(request: Request):
    """Снимает права сразу у нескольких сис-админов."""
    user = await require_form(request)
    data = await request.form()
    targets, _ = await repo.staff_targets(value(data, "user_id"))
    if not targets:
        flash("!Введите MAX ID через запятую.")
        return redirect("/panel/staff")
    revoked: list[str] = []
    problems: list[str] = []
    for item in targets:
        done, message = await repo.revoke_sysadmin(item["user_id"])
        if not done:
            problems.append(message)
            continue
        revoked.append(item["user_id"])
        await notify(item["user_id"], "🔐 Права сис-админа бота колледжа сняты. Панель вам больше не доступна.")
    parts = [f"Права сняты: {len(revoked)}"]
    if problems:
        parts.append("Не сняты: " + "; ".join(problems))
    log.info("панель: сняты права с %s (сис-админ %s)", len(revoked), user)
    await repo.log_action(user, "права сис-админа сняты", ", ".join(revoked) or "—")
    flash(". ".join(parts))
    return redirect("/panel/staff")


@router.post("/staff/sysadmin")
async def staff_add_sysadmin(request: Request):
    """Выдаёт права сис-админа одному или сразу нескольким по списку ID."""
    user = await require_form(request)
    data = await request.form()
    targets, missing = await repo.staff_targets(value(data, "user_id"))
    if not targets:
        flash("!Введите MAX ID: он состоит только из цифр (или @ник, или ссылка на профиль). "
              "Можно несколько через запятую." + (f" Ники не найдены: {', '.join(missing)}." if missing else ""))
        return redirect("/panel/staff")
    granted: list[str] = []
    problems: list[str] = []
    typed_name = value(data, "full_name")
    for item in targets:
        name = (typed_name if len(targets) == 1 else "") or item["full_name"]
        done, message = await repo.grant_sysadmin(item["user_id"], name)
        if not done:
            problems.append(message)
            continue
        granted.append(item["user_id"])
        await notify(item["user_id"], "🔐 Вам выдали права сис-админа бота колледжа: "
                                      "в панели появится кнопка «🔐 Сис-админ».")
    parts = [f"Права сис-админа выданы: {len(granted)}"]
    if problems:
        parts.append("Не выданы: " + "; ".join(problems))
    log.info("панель: права сис-админа выданы %s (сис-админ %s)", len(granted), user)
    await repo.log_action(user, "права сис-админа выданы", ", ".join(granted) or "—")
    flash(". ".join(parts))
    return redirect("/panel/staff")


@router.post("/staff/sysadmin/{user_id}/revoke")
async def staff_revoke_sysadmin(request: Request, user_id: str):
    user = await require_form(request)
    done, message = await repo.revoke_sysadmin(user_id)
    if not done:
        flash(f"!{message}.")
        return redirect("/panel/staff")
    await notify(user_id, "🔐 Права сис-админа бота колледжа сняты. Панель вам больше не доступна.")
    await repo.log_action(user, "права сис-админа сняты", message)
    log.warning("панель: %s (сис-админ %s)", message, user)
    flash(message + ". Если в .env остался этот ID, перезапуск его не вернёт — "
                   "вернуть можно кнопкой «Вернуть из .env».")
    return redirect("/panel/staff")


@router.post("/staff/sysadmin/{user_id}/restore")
async def staff_restore_sysadmin(request: Request, user_id: str):
    """Возвращает права, снятые в панели: ID снова перестаёт быть отозванным."""
    user = await require_form(request)
    uid = as_str(user_id)
    if uid not in await repo.revoked_sysadmins():
        flash("Этот ID и так не был отозван.")
        return redirect("/panel/staff")
    done, message = await repo.grant_sysadmin(uid)
    if not done:
        flash(f"!{message}.")
        return redirect("/panel/staff")
    await notify(uid, "🔐 Права сис-админа бота колледжа восстановлены.")
    await repo.log_action(user, "сис-админ возвращён", f"ID {uid}")
    flash(f"ID {uid} снова сис-админ.")
    return redirect("/panel/staff")


@router.post("/staff/{user_id}")
async def staff_update(request: Request, user_id: str):
    actor = await require_form(request)
    data = await request.form()
    if not await repo.get_admin(user_id):
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    before = await repo.get_admin(user_id)
    await repo.update_admin(
        user_id,
        full_name=value(data, "full_name") or None,
        role=value(data, "role"),
        position=value(data, "position"),
        department=value(data, "department"),
        office=value(data, "office"),
        ticket_category=value(data, "ticket_category") or "all",
        can_broadcast=1 if value(data, "can_broadcast") == "1" else 0,
    )
    log.info("панель: изменён сотрудник %s", user_id)
    changed = [field for field in ("full_name", "role", "position", "department", "office", "ticket_category")
               if as_str(before[field]) != as_str((await repo.get_admin(user_id))[field])]
    await repo.log_action(actor, "карточка сотрудника изменена", f"{user_id}: {', '.join(changed) or 'без изменений'}")
    flash(f"Сотрудник {user_id} сохранён.")
    return redirect("/panel/staff")


@router.post("/staff/{user_id}/delete")
async def staff_delete(request: Request, user_id: str):
    user = await require_form(request)
    a = await repo.get_admin(user_id)
    if not a or is_sysadmin_role(as_str(a["role_type"])):
        flash("!Сис-админа нельзя удалить из панели.")
        return redirect("/panel/staff")
    open_n = await repo.open_tickets_count(user_id)
    if open_n:
        flash(f"!У сотрудника {open_n} открытых обращений — закройте их и повторите.")
        return redirect("/panel/staff")
    await repo.delete_staff(user_id)
    log.info("панель: удалён сотрудник %s (сис-админ %s)", user_id, user)
    await repo.log_action(user, "сотрудник удалён", f"{user_id} {as_str(a['full_name'])}")
    flash(f"Сотрудник {user_id} удалён.")
    return redirect("/panel/staff")


# ── пользователи: все, кто писал боту ─────────────────────────────────────────
PEOPLE_PAGE = 100


def profile_cell(username) -> str:
    """Ссылка на профиль MAX; без ника — прочерк (ник бывает скрытым)."""
    name = as_str(username).strip()
    link = profile_url(name)
    if not link:
        return "<span class='mut'>ник скрыт</span>"
    return f'<a href="{esc(link)}" target="_blank" rel="noopener">@{esc(name)}</a>'


def _people_table(rows) -> str:
    body = "".join(
        f"<tr><td><a href='/panel/people/{esc(row['user_id'])}'><b>{esc(row['fio'] or row['staff_name'] or row['display_name'])}</b></a>"
        f"<div class='small mut'>{esc(repo.KIND_TITLES.get(repo.contact_kind(row), '—'))}</div></td>"
        f"<td>{esc(row['group_code'] or row['position'] or '—')}</td>"
        f"<td>{esc(row['user_id'])}</td><td>{profile_cell(row['username'])}</td>"
        f"<td>{esc(row['tickets'])}</td><td class='small mut'>{esc(fmt_when(row['last_seen']))}</td></tr>"
        for row in rows
    ) or "<tr><td class='mut'>Никого не найдено</td></tr>"
    return ("<table><tr><th>Человек</th><th>Группа / должность</th><th>MAX ID</th>"
            f"<th>Профиль MAX</th><th>Обращений</th><th>Был</th></tr>{body}</table>")


@router.get("/people")
async def people_list(request: Request, kind: str = "", q: str = ""):
    user = await require_user(request)
    overview = await repo.people_overview()
    total = await repo.people_count(kind, q)
    rows = await repo.people(kind, q, limit=PEOPLE_PAGE)
    stats = " · ".join(
        f"{repo.CONTACT_KIND_LABELS[code]}: {overview[key]}"
        for code, key in (("student", "students"), ("staff", "staff"), ("guest", "guests"))
    )
    filters = f"""
<form method="get" action="/panel/people" class="grid" style="margin-bottom:14px">
<div>{select("kind", dict(repo.CONTACT_KIND_LABELS), kind)}</div>
<div><label>Поиск: ФИО, ID, @ник, группа, должность</label><input name="q" value="{esc(q)}"></div>
<div><button>Найти</button></div>
<div><a class="btn-grey" href="/panel/people.csv?kind={esc(kind)}&q={esc(q)}">⬇️ CSV</a></div>
</form>"""
    body = f'<div class="card"><p class="small mut">{esc(stats)} · показано {len(rows)} из {total}</p>{filters}{_people_table(rows)}</div>'
    return page("Пользователи", body, user, "/people")


@router.get("/people.csv")
async def people_csv(request: Request, kind: str = "", q: str = ""):
    await require_user(request)
    rows = await repo.people(kind, q, limit=5000)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["MAX ID", "ФИО", "Роль в боте", "Группа", "Должность", "Отдел", "@ник",
                     "Профиль MAX", "Сообщений", "Обращений", "Первый контакт", "Последний контакт"])
    for row in rows:
        username = as_str(row["username"]).strip()
        writer.writerow([
            row["user_id"], row["fio"] or row["staff_name"] or row["display_name"],
            repo.KIND_TITLES.get(repo.contact_kind(row), ""), row["group_code"], row["position"],
            row["department"], f"@{username}" if username else "",
            profile_url(username), row["messages"], row["tickets"], row["first_seen"], row["last_seen"],
        ])
    payload = "\ufeff" + buffer.getvalue()  # BOM — чтобы Excel открыл кириллицу
    return Response(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users.csv"'},
    )


@router.get("/people/{user_id}")
async def person_card(request: Request, user_id: str):
    user = await require_user(request)
    card = await repo.user_card(user_id)
    if not card:
        return page("Пользователь", '<div class="card msg-bad">Этот человек ещё не писал боту.</div>', user, "/people")
    name = card["fio"] or card["staff_name"] or card["display_name"] or f"ID {user_id}"
    rows = "".join(
        f"<tr><td><a href='/panel/tickets/{esc(t['ticket_id'])}'>№{esc(t['ticket_id'])}</a></td>"
        f"<td>{esc(STATUS.get(t['status'], t['status']))}</td>"
        f"<td class='small mut'>{esc(fmt_when(t['created_at']))}</td></tr>"
        for t in card["tickets_list"]
    ) or "<tr><td class='mut'>Обращений не было</td></tr>"
    dept_options = {"": "— отдел не выбран —"}
    dept_options.update({name: name for name in await repo.department_names()})
    request_row = ""
    if card["request"]:
        request_row = f"""
<div class="card"><h2>Заявка на роль сотрудника</h2>
<table><tr><th>Статус</th><td>{esc(REQUEST_STATUS_LABEL.get(card['request']['status'], card['request']['status']))}</td></tr>
<tr><th>Должность</th><td>{esc(card['request']['position'] or '—')}</td></tr>
<tr><th>Кабинет</th><td>{esc(card['request']['office'] or '—')}</td></tr>
<tr><th>Комментарий</th><td>{esc(card['request']['note'] or '—')}</td></tr>
<tr><th>Подана</th><td>{esc(fmt_when(card['request']['created_at']))}</td></tr></table></div>"""
    actions = ""
    if card["kind"] == "request" and card["request"] and card["request"]["status"] == "new":
        actions = (f'<form method="post" action="/panel/access/request/{esc(user_id)}/ok" class="inline">{csrf(request)}'
                   f'<button class="btn-ok">Одобрить — сделать сотрудником</button></form> '
                   f'<form method="post" action="/panel/access/request/{esc(user_id)}/no" class="inline">{csrf(request)}'
                   f'<button class="btn-bad">Отклонить</button></form>')
    elif not card["role_type"]:
        actions = (f'<form method="post" action="/panel/access/make/{esc(user_id)}" class="inline">{csrf(request)}'
                   f'<input name="full_name" value="{esc(name)}">'
                   f'<input name="position" placeholder="Должность" value="">'
                   f'{select("department", dept_options, "")}'
                   f'{select("ticket_category", dict(STAFF_CATS), "all")}'
                   f'<button class="btn-ok">Сделать сотрудником</button></form>'
                   f'<form method="post" action="/panel/access/sysadmin/{esc(user_id)}" class="inline">{csrf(request)}'
                   f'<input name="full_name" value="{esc(name)}">'
                   f'<button class="btn-grey">🔐 Сделать сис-админом</button></form>')
    tickets_count = len(card["tickets_list"])
    delete_note = ("Сис-админа удаляют во вкладке «Сотрудники»."
                   if is_sysadmin_role(as_str(card["role_type"])) else
                   f"Удаляются регистрация, карточка сотрудника, контакт и состояние. "
                   f"Обращений: {tickets_count}"
                   + (" (есть открытые — удалять можно только вместе с ними)." if tickets_count else "."))
    remove = ""
    if not is_sysadmin_role(as_str(card["role_type"])):
        remove = f"""
<div class="card"><h2>Удаление</h2>
<p class="small mut">{esc(delete_note)}</p>
{_action_form(request, f'/panel/people/{user_id}/delete', '🗑 Удалить пользователя',
              confirm_text=f"Удалить {name} ({user_id})? Действие необратимо.")}
{_action_form(request, f'/panel/people/{user_id}/delete', '🗑 Удалить вместе с обращениями',
              '<input type="hidden" name="with_tickets" value="1">',
              f"Удалить {name} вместе с {tickets_count} обращениями и перепиской? Действие необратимо.",
              "btn-bad")}
</div>"""
    body = f"""
<div class="card"><h2>{esc(name)}</h2>
<table>
<tr><th>Роль в боте</th><td>{esc(repo.KIND_TITLES.get(card['kind'], card['kind']))}</td></tr>
<tr><th>MAX ID</th><td>{esc(user_id)}</td></tr>
<tr><th>Профиль MAX</th><td>{profile_cell(card['username'])}</td></tr>
<tr><th>Студент</th><td>{esc(card['fio'] or '—')}, группа {esc(card['group_code'] or '—')}</td></tr>
<tr><th>Сотрудник</th><td>{esc(card['staff_name'] or '—')}, {esc(card['position'] or 'должность не назначена')}</td></tr>
<tr><th>Отдел</th><td>{esc(card['department'] or '—')}</td></tr>
<tr><th>Сообщений боту</th><td>{esc(card['messages'])}</td></tr>
<tr><th>Первый контакт</th><td>{esc(fmt_when(card['first_seen']))}</td></tr>
<tr><th>Последний контакт</th><td>{esc(fmt_when(card['last_seen']))}</td></tr>
<tr><th>Последнее сообщение</th><td>{esc(card['last_text'] or '—')}</td></tr>
</table>
<div style="margin-top:12px">{actions}</div></div>
<div class="card"><h2>Обращения</h2>
<table><tr><th>№</th><th>Статус</th><th>Создано</th></tr>{rows}</table></div>
{request_row}
{remove}"""
    return page(f"Пользователь {name}", body, user, "/people")


@router.post("/people/{user_id}/delete")
async def people_delete(request: Request, user_id: str):
    user = await require_form(request)
    data = await request.form()
    with_tickets = as_str(data.get("with_tickets", "")) == "1"
    card = await repo.user_card(user_id)
    if not card:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    name = card["fio"] or card["staff_name"] or card["display_name"] or user_id
    done, message = await repo.delete_user(user_id, with_tickets=with_tickets)
    if not done:
        flash(f"!Удаление не выполнено: {message}")
        return redirect(f"/panel/people/{user_id}")
    log.warning("панель: удалён пользователь %s (%s), с обращениями: %s (сис-админ %s)",
                user_id, name, with_tickets, user)
    flash(message + f". MAX ID {user_id}. Резервная копия базы — на вкладке «База данных».")
    return redirect("/panel/people")


# ── коды и заявки на роль сотрудника ──────────────────────────────────────────
INVITE_STATE_LABEL = {"active": "🟢 активен", "used": "⚪ использован", "expired": "🔴 истёк", "unknown": "—"}
REQUEST_STATUS_LABEL = {"new": "📥 новая", "approved": "✅ одобрена", "rejected": "❌ отклонена"}


@router.get("/access")
async def access_page(request: Request):
    user = await require_user(request)
    invites = await repo.list_invites(50)
    requests_new = await repo.staff_requests("new")
    requests_closed = await repo.staff_requests("", 30)
    attempts = await repo.attempts_log(20)
    code_rows = []
    for row in invites:
        state = await repo.invite_state(row["code"])
        who = row["full_name"] or row["fio"] or (f"ID {row['user_id']}" if row["user_id"] else "любому")
        revoke = ""
        if state == "active":
            revoke = (f'<form method="post" action="/panel/access/code/delete" class="inline">{csrf(request)}'
                      f'<input type="hidden" name="code" value="{esc(row["code"])}">'
                      f'<button class="btn-grey">Отозвать</button></form>')
        code_rows.append(
            f"<tr><td><b>{esc(row['code'])}</b></td><td>{esc(who)}</td>"
            f"<td>{esc(INVITE_STATE_LABEL.get(state, state))}</td>"
            f"<td class='small mut'>{esc(row['expires_at'] or 'бессрочный')}</td>"
            f"<td class='small mut'>{esc(row['used_by_name'] or row['used_by'] or '—')}</td><td>{revoke}</td></tr>"
        )
    code_table = (f"<table><tr><th>Код</th><th>Кому</th><th>Состояние</th><th>Истекает</th>"
                  f"<th>Использован</th><th></th></tr>{''.join(code_rows) or '<tr><td colspan=6 class=mut>Кодов пока нет</td></tr>'}</table>")
    request_rows = "".join(
        f"<tr><td><a href='/panel/people/{esc(row['user_id'])}'>{esc(row['full_name'] or row['display_name'] or 'без имени')}</a>"
        f"<div class='small mut'>ID {esc(row['user_id'])}</div></td>"
        f"<td>{esc(row['position'] or '—')}</td><td>{esc(row['office'] or '—')}</td>"
        f"<td class='small'>{esc(row['note'] or '—')}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['created_at']))}</td>"
        f"<td><form method='post' action='/panel/access/request/{esc(row['user_id'])}/ok' class='inline'>{csrf(request)}"
        f"<button class='btn-ok'>Одобрить</button></form> "
        f"<form method='post' action='/panel/access/request/{esc(row['user_id'])}/no' class='inline'>{csrf(request)}"
        f"<button class='btn-bad'>Отклонить</button></form></td></tr>"
        for row in requests_new
    ) or "<tr><td class='mut'>Новых заявок нет</td></tr>"
    closed_rows = "".join(
        f"<tr><td>{esc(row['full_name'] or row['display_name'] or '—')}</td>"
        f"<td>{esc(REQUEST_STATUS_LABEL.get(row['status'], row['status']))}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['updated_at']))}</td></tr>"
        for row in requests_closed if as_str(row["status"]) != "new"
    ) or ""
    attempt_rows = "".join(
        f"<tr><td><a href='/panel/people/{esc(row['user_id'])}'>{esc(row['label'] or row['user_id'])}</a>"
        f"<div class='small mut'>ID {esc(row['user_id'])}</div></td><td>{esc(row['tries'])}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['last_try']))}</td></tr>"
        for row in attempts
    ) or "<tr><td class='mut'>Попыток не было</td></tr>"
    body = f"""
<div class="card"><h2>🗝 Коды сотрудников</h2>
<form method="post" action="/panel/access/code" class="grid" style="margin-bottom:14px">
{csrf(request)}
<div><label>MAX ID (пусто — код для любого, у кого есть код)</label><input name="user_id" value=""></div>
<div><label>ФИО для списка</label><input name="full_name" value=""></div>
<div>{select("ttl_hours", {hours: label for hours, label in CODE_TTL_CHOICES}, config.STAFF_CODE_TTL)}</div>
<div><button class="btn-ok">Выдать код</button></div></form>
<p class="small mut">Одноразовый. Срок выбирается рядом; в боте то же самое кнопками под кодом.
В MAX сотрудник выбирает «👔 Я сотрудник» и вводит код.</p>
{code_table}</div>
<div class="card"><h2>📥 Заявки на роль сотрудника</h2>
<table><tr><th>Человек</th><th>Должность</th><th>Кабинет</th><th>Комментарий</th><th>Подана</th><th></th></tr>{request_rows}</table>
{('<table><tr><th>Человек</th><th>Статус</th><th>Обновлена</th></tr>' + closed_rows + '</table>') if closed_rows else ''}</div>
<div class="card"><h2>⚠️ Попытки ввода кода за сутки</h2>
<table><tr><th>Человек</th><th>Попыток</th><th>Последняя</th></tr>{attempt_rows}</table>
<p class="small mut">Больше {esc(config.STAFF_CODE_ATTEMPTS)} попыток в час — ввод блокируется до истечения часа.</p></div>"""
    return page("Коды и заявки", body, user, "/access")


@router.post("/access/code")
async def access_code_create(request: Request):
    """Выдаёт код сотрудника: MAX ID можно не указывать, срок выбирается рядом."""
    user = await require_form(request)
    data = await request.form()
    uid = value(data, "user_id")
    if uid and not uid.isdigit():
        flash("!MAX ID состоит только из цифр.")
        return redirect("/panel/access")
    ttl = to_int(value(data, "ttl_hours", default=str(config.STAFF_CODE_TTL)), config.STAFF_CODE_TTL)
    code = gen_code(6)
    while await repo.invite_state(code) != "unknown":
        code = gen_code(6)
    await repo.create_invite(code, user_id=uid, full_name=value(data, "full_name"),
                             created_by=user, ttl_hours=ttl)
    log.info("панель: выдан код сотрудника %s для %s, срок %s ч (сис-админ %s)", code, uid or "любого", ttl, user)
    await repo.log_action(user, "код сотрудника выдан",
                          f"{code} → {uid or 'любому, у кого есть код'} · срок {ttl_label(ttl)}")
    flash(f"Код {code} выдан" + (f" для ID {uid}." if uid else " (для любого, кто его введёт)")
          + f" Срок: {ttl_label(ttl)}.")
    return redirect("/panel/access")


@router.post("/access/code/delete")
async def access_code_delete(request: Request):
    user = await require_form(request)
    data = await request.form()
    code = as_str(data.get("code", "")).strip()
    if code:
        await repo.delete_invite(code)
        log.info("панель: отозван код %s (сис-админ %s)", code, user)
        await repo.log_action(user, "код сотрудника отозван", code)
        flash(f"Код {code} отозван.")
    return redirect("/panel/access")


@router.post("/access/request/{user_id}/ok")
async def access_request_ok(request: Request, user_id: str):
    user = await require_form(request)
    _, message = await approve_request(user, user_id)
    log.info("панель: одобрена заявка %s (сис-админ %s)", user_id, user)
    await repo.log_action(user, "заявка на роль сотрудника одобрена", f"ID {user_id}")
    flash(message)
    return redirect(f"/panel/people/{user_id}")


@router.post("/access/request/{user_id}/no")
async def access_request_no(request: Request, user_id: str):
    user = await require_form(request)
    await reject_request(user, user_id)
    log.info("панель: отклонена заявка %s (сис-админ %s)", user_id, user)
    await repo.log_action(user, "заявка отклонена", f"ID {user_id}")
    flash("Заявка отклонена, сотрудник уведомлён.")
    return redirect("/panel/access")


@router.post("/access/make/{user_id}")
async def access_make_staff(request: Request, user_id: str):
    """Делает сотрудником пользователя из реестра, без заявки."""
    user = await require_form(request)
    data = await request.form()
    card = await repo.user_card(user_id)
    if not card:
        flash("!Пользователь не найден.")
        return redirect("/panel/people")
    if card["role_type"]:
        flash("!Он уже сотрудник.")
        return redirect(f"/panel/people/{user_id}")
    name = value(data, "full_name") or card["fio"] or card["staff_name"] or card["display_name"] or f"Сотрудник {user_id}"
    position = value(data, "position")
    await repo.add_staff(user_id, name, position=position)
    await repo.update_admin(
        user_id,
        department=value(data, "department"),
        office=value(data, "office"),
        ticket_category=value(data, "ticket_category") or "all",
    )
    await repo.clear_attempts(user_id)
    await notify(user_id, f"🏫 Вы зарегистрированы как сотрудник: {name}. Отправьте /start, чтобы открыть кабинет.")
    log.info("панель: %s сделан сотрудником (сис-админ %s)", user_id, user)
    await repo.log_action(user, "сделан сотрудником из реестра", f"{name} (ID {user_id}) · {position or 'без должности'}")
    flash(f"Сотрудник {name} создан.")
    return redirect(f"/panel/people/{user_id}")


@router.post("/access/sysadmin/{user_id}")
async def access_make_sysadmin(request: Request, user_id: str):
    """Выдаёт права сис-админа прямо из карточки человека."""
    actor = await require_form(request)
    data = await request.form()
    done, message = await repo.grant_sysadmin(user_id, value(data, "full_name"))
    if not done:
        flash(f"!{message}.")
        return redirect(f"/panel/people/{user_id}")
    await notify(user_id, "🔐 Вам выдали права сис-админа бота колледжа: в панели появится кнопка «🔐 Сис-админ».")
    log.info("панель: %s (сис-админ %s)", message, actor)
    await repo.log_action(actor, "права сис-админа выданы", message)
    flash(message + ".")
    return redirect(f"/panel/people/{user_id}")


@router.get("/nostaff")
async def nostaff_page(request: Request):
    """Кто писал боту, но прав сотрудника не имеет: выдать их можно прямо отсюда."""
    user = await require_user(request)
    q = request.query_params.get("q", "")
    rows = await repo.people_without_staff(60, q)
    total = await repo.people_without_staff_count(q)
    head = ('<form method="get" action="/panel/nostaff" class="grid" style="margin-bottom:14px">'
            f'<div><input name="q" value="{esc(q)}" placeholder="Поиск: ФИО, ID, ник или группа"></div>'
            "<div><button>Найти</button></div></form>")
    body = "".join(
        f"<tr><td><b>{esc(row['fio'] or row['display_name'] or 'без имени')}</b>"
        f"<div class='small mut'>{esc(repo.KIND_TITLES.get(repo.contact_kind(row), '-'))}</div></td>"
        f"<td>{esc(row['user_id'])}</td>"
        f"<td>{esc(row['group_code'] or '—')}</td>"
        f"<td>{profile_cell(row['username'])}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['last_seen']))}</td>"
        f"<td>{_action_form(request, f'/panel/access/make/{esc(row['user_id'])}', 'Сделать сотрудником',)}"
        f" <a class='btn-grey' href='/panel/people/{esc(row['user_id'])}'>Открыть</a></td></tr>"
        for row in rows
    ) or "<tr><td class='mut'>Таких нет — все, кто писал боту, уже сотрудники.</td></tr>"
    table = ("<table><tr><th>Кто</th><th>MAX ID</th><th>Группа</th><th>Профиль MAX</th><th>Был в боте</th><th></th></tr>"
             f"{body}</table>")
    body_all = f"""
<div class="card"><h2>👤 Без прав сотрудника: {total}</h2>{head}{table}
<p class="small mut">Сотсортировано по последнему обращению. Нажмите «Сделать сотрудником» — карточка
создастся с именем из реестра, должность и отдел можно поправить там же.</p></div>"""
    return page("Без прав", body_all, user, "/nostaff")


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


def _action_form(request: Request, action: str, label: str, fields: str = "",
                 confirm_text: str = "", cls: str = "btn-grey") -> str:
    """Кнопка-действие: одна форма на действие, с подтверждением в браузере."""
    script = f" onclick=\"return confirm('{confirm_text}')\"" if confirm_text else ""
    return (f'<form method="post" action="{esc(action)}" class="inline"{script}>{csrf(request)}'
            f'{fields}<button class="{esc(cls)}">{esc(label)}</button></form>')


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
            f"<button class='btn-grey'>Очистить</button></form>")


def schema_broken_page(request: Request, detail: str, missing: list[str]):
    """Страница для случая «в базе не хватает объектов» вместо 500 со стектрейсом."""
    items = "".join(f"<li><code>{esc(item)}</code></li>" for item in missing) or "<li>—</li>"
    return page(
        "База требует восстановления",
        f"""<div class="card"><h2>⚠️ В базе не хватает объектов</h2>
<p>Бот не может работать, пока схема неполна: {esc(detail)}.</p>
<p>Чего не хватает:</p><ul>{items}</ul>
<p>Кнопка создаёт недостающие таблицы, индексы и колонки. Данные, которые уже
есть, не меняются, но удалённую таблицу придётся наполнить заново (например,
заново зарегистрировать студентов) — либо восстановить базу из резервной копии.</p>
<div class="grid">
{_action_form(request, '/panel/database/repair', '🔧 Восстановить схему', cls='btn-ok')}
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
<div class="card" style="border-left:4px solid var(--bad)"><h2>⚠️ Схема базы неполна</h2>
<p>Не хватает: {esc(', '.join(missing))}. Из-за этого часть страниц панели и бот могут падать.</p>
{_action_form(request, '/panel/database/repair', '🔧 Восстановить схему', cls='btn-ok')}</div>"""

    verdict = "✅ цела" if check == "ok" else f"⚠️ {check}"
    state_rows = "".join(
        f"<tr><td>Файл базы</td><td><code>{esc(db.database_file())}</code></td></tr>"
        f"<tr><td>Размер (с журналом WAL)</td><td>{esc(human_bytes(sizes['total']))} "
        f"<span class='small mut'>({esc(human_bytes(sizes['db']))} + {esc(human_bytes(sizes['wal']))})</span></td></tr>"
        f"<tr><td>SQLite</td><td>{esc(storage['sqlite_version'])}</td></tr>"
        f"<tr><td>Режим журнала</td><td>{esc(storage['journal_mode'])}</td></tr>"
        f"<tr><td>Страниц / свободно</td><td>{storage['page_count']} / {storage['freelist_count']}</td></tr>"
        f"<tr><td>Можно освободить сжатием</td><td>{esc(human_bytes(storage['reclaimable']))}</td></tr>"
        f"<tr><td>Проверка целостности</td><td>{esc(verdict)}</td></tr>"
        f"<tr><td>Схема</td><td>{'✅ соответствует коду' if not missing else '⚠️ не хватает: ' + esc(', '.join(missing))}</td></tr>"
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
        f"<td><a class='btn-grey' href='/panel/database/backup/{esc(item['name'])}'>⬇️ Скачать</a> "
        f"{_action_form(request, '/panel/database/restore', '♻️ Восстановить', f'<input type=\"hidden\" name=\"name\" value=\"{esc(item['name'])}\">', 'Восстановить базу из этой копии? Текущие данные будут заменены, перед этим бот сделает страховочную копию.')}"
        f" {_action_form(request, '/panel/database/backup/delete', '🗑', f'<input type=\"hidden\" name=\"name\" value=\"{esc(item['name'])}\">', 'Удалить копию?', 'btn-bad')}</td></tr>"
        for item in backups
    ) or "<tr><td colspan='4' class='mut'>Копий пока нет</td></tr>"

    body = f"""
{banner}
<div class="card"><h2>Состояние базы</h2>
<table>{state_rows}</table>
<div style="margin-top:12px" class="grid">
  {_action_form(request, '/panel/database/backup', '📦 Создать копию', cls='btn-ok')}
  <a class="btn-grey" href="/panel/database/download">⬇️ Скачать текущую базу</a>
</div></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Обслуживание</h2>
  <p class="small mut">Сжатие безопасно: освободившиеся страницы уходят в конец файла, данные не меняются.
  Слияние журнала WAL нужно, если вы копируете файл базы вручную.</p>
  <div class="grid">
    {_action_form(request, '/panel/database/vacuum', '🧹 Сжать (VACUUM)')}
    {_action_form(request, '/panel/database/checkpoint', '📥 Слить журнал WAL')}
  </div>
  <h2>Чистка служебных таблиц</h2>
  <table>{prune_rows}</table>
  <p class="small mut">Обращения, сообщения, сотрудники и реестр пользователей чистка не трогает.</p></div>
  <div class="card" style="flex:1"><h2>Данные по таблицам</h2>
  <table><tr><th>Таблица</th><th>Строк</th></tr>{counts_rows}</table></div>
</div>
<div class="card"><h2>Резервные копии</h2>
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


# ── группы ────────────────────────────────────────────────────────────────────
@router.get("/groups")
async def groups_list(request: Request):
    user = await require_user(request)
    rows = await repo.groups(active_only=False, limit=300)
    body = "".join(
        f"""<tr><td><b>{esc(row['group_code'])}</b></td><td>{esc(row['title'])}</td>
<td>{"<span class='pill pill-on'>активна</span>" if flag(row['active']) else "<span class='pill pill-off'>скрыта</span>"}</td>
<td class="small mut">{esc(row['created_at'])}</td>
<td class="small">
<form method="post" action="/panel/groups/rename" class="inline">{csrf(request)}
<input type="hidden" name="old" value="{esc(row['group_code'])}">
<input name="code" value="{esc(row['group_code'])}" style="width:110px;display:inline-block">
<button class="btn-grey">Переименовать</button></form>
<form method="post" action="/panel/groups/toggle" class="inline">{csrf(request)}
<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-grey">{"Скрыть" if flag(row['active']) else "Показать"}</button></form>
<form method="post" action="/panel/groups/delete" class="inline" onclick="return confirm('Удалить группу?')">
{csrf(request)}<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-bad">Удалить</button></form></td></tr>"""
        for row in rows
    ) or "<tr><td class='mut'>Справочник пуст</td></tr>"
    table = f"<table><tr><th>Код</th><th>Название</th><th>Состояние</th><th>Создана</th><th>Действия</th></tr>{body}</table>"
    add = form(
        request, "/panel/groups/add",
        input("code", "") + input("title", "")
        + select("active", {"1": "активна", "0": "скрыта"}, "1"),
        "Добавить группу", "btn-ok",
    )
    body_all = f"""
<div class="card"><h2>Справочник групп</h2>{table}</div>
<div class="card" style="max-width:560px"><h2>Добавить группу</h2>{add}
<p class="small mut">Переименование меняет код и у студентов, и в расписаниях, и в подписках.
Скрытая группа не показывается в подсказках бота.</p></div>"""
    return page("Группы", body_all, user, "/groups")


@router.post("/groups/add")
async def groups_add(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    if not valid_group(code):
        flash("!Код группы: буквы, цифры, дефис и точка, до 30 символов (например ИС-21).")
        return redirect("/panel/groups")
    await repo.upsert_group(code, value(data, "title"), value(data, "active") == "1")
    await repo.log_action(actor, "группа добавлена", f"{code} {value(data, 'title')}")
    flash(f"Группа {code} добавлена.")
    return redirect("/panel/groups")


@router.post("/groups/rename")
async def groups_rename(request: Request):
    actor = await require_form(request)
    data = await request.form()
    old, new = norm_group(value(data, "old")), norm_group(value(data, "code"))
    if old == new:
        flash("Код не изменился.")
        return redirect("/panel/groups")
    if not await repo.rename_group(old, new):
        flash(f"!Не удалось переименовать {old} → {new}: проверьте код и что новый ещё не используется.")
        return redirect("/panel/groups")
    log.info("панель: группа %s → %s", old, new)
    await repo.log_action(actor, "группа переименована", f"{old} → {new}")
    flash(f"Группа {old} переименована в {new}.")
    return redirect("/panel/groups")


@router.post("/groups/toggle")
async def groups_toggle(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    row = await repo.get_group(code)
    if not row:
        flash("!Группа не найдена.")
        return redirect("/panel/groups")
    await repo.set_group_active(code, not flag(row["active"]))
    await repo.log_action(actor, "группа скрыта или показана", code)
    flash(f"Группа {code} {'показана' if not flag(row['active']) else 'скрыта'}.")
    return redirect("/panel/groups")


@router.post("/groups/delete")
async def groups_delete(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    await repo.delete_group(code)
    await repo.log_action(actor, "группа удалена из справочника", f"{code} (данные студентов сохранены)")
    flash(f"Группа {code} удалена из справочника (данные студентов сохранены).")
    return redirect("/panel/groups")


# ── расписания ────────────────────────────────────────────────────────────────
@router.get("/schedules")
async def schedules_list(request: Request):
    user = await require_user(request)
    rows = await repo.schedule_groups(300)
    stamps = {}
    parsed_total = 0
    body = ""
    for row in rows:
        code = as_str(row["group_code"])
        full = await repo.get_schedule(code)
        stamp = repo.schedule_stamp(full)
        count = await repo.lessons_count(code)
        parsed_total += count
        stamps[code] = stamp
        if stamp["parse_error"]:
            state = f"<span class='pill pill-off'>ошибка: {esc(short(stamp['parse_error'], 60))}</span>"
        elif count:
            state = f"<span class='pill pill-on'>{count} пар</span> <span class='small mut'>{esc(fmt_when(stamp['parsed_at']))}</span>"
        else:
            state = "<span class='mut'>не разобрано</span>"
        found = ", ".join(stamp["found_groups"][:8]) or "—"
        body += (
            f"<tr><td><b>{esc(code)}</b></td><td class='small'>{esc(short(full['pdf_url'], 70))}</td>"
            f"<td>{state}</td><td class='small mut'>{esc(found)}</td>"
            f"<td>{_action_form(request, '/panel/schedules/parse', '🔍 Разобрать', f'<input type=\"hidden\" name=\"group_code\" value=\"{esc(code)}\">')}"
            f" {_action_form(request, '/panel/schedules/delete', '🗑', f'<input type=\"hidden\" name=\"group_code\" value=\"{esc(code)}\">', f'Удалить расписание группы {code}?', 'btn-bad')}</td></tr>"
        )
    table = (f"<table><tr><th>Группа</th><th>Ссылка на PDF</th><th>Разбор</th><th>Группы в PDF</th><th></th></tr>"
             f"{body or '<tr><td colspan=5 class=mut>Расписаний пока нет</td></tr>'}</table>")
    save = form(
        request, "/panel/schedules/save",
        input("group_code", "") + input("pdf_url", "", full=True),
        "Сохранить (сразу разберём)", "btn-ok",
    )
    bells = await _lesson_times_form(request)
    body_all = f"""
<div class="card"><h2>Расписания</h2>{table}
<p class="small mut">Разобранных пар: {parsed_total}. Скачанные PDF лежат рядом с базой в папке
<code>schedules</code> и перечитываются, когда файл по ссылке меняется.</p>
{_action_form(request, '/panel/schedules/parse_all', '🔄 Обновить все расписания')}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Сохранить расписание</h2>{save}
  <p class="small mut">Ссылку достаёт преподаватель или бот. Сначала проверяем, что по ней отдаётся PDF,
  затем разбираем файл в занятия — студенты увидят расписание текстом, а не ссылкой.</p></div>
  <div class="card" style="flex:1"><h2>Звонки (время пар)</h2>{bells}</div>
</div>"""
    return page("Расписания", body_all, user, "/schedules")


async def _lesson_times_form(request: Request) -> str:
    """Редактор звонков: время каждого урока по его номеру.

    Нумерация — как в PDF и в боте: 1 урок, 2 урок, … (в паре их два, поэтому
    номера идут подряд). Время подставляется в расписание по номеру урока.
    """
    times = await schedules.lesson_times()
    rows = []
    for index, (start, end) in enumerate(times, start=1):
        rows.append(
            f"<div class='full' style='display:flex;gap:8px;align-items:center'>"
            f"<span style='width:72px'>{index} урок</span>"
            f"<input name='t{index}a' value='{esc(start)}' pattern='\\d{{1,2}}:\\d{{2}}' size='5'>"
            f"<input name='t{index}b' value='{esc(end)}' pattern='\\d{{1,2}}:\\d{{2}}' size='5'></div>"
        )
    return (form(request, "/panel/schedules/times", "".join(rows), "Сохранить звонки", "btn-ok")
            + "<p class='small mut'>Номер урока — как в расписании и как показывает бот: в паре уроков два, "
              "поэтому 1 и 2 уроки — это первая пара, 3 и 4 — вторая. Формат 09:00. Изменение сразу "
              "применяется к уже разобранным занятиям, перечитывать PDF не нужно.</p>")


@router.get("/schedules/{group_code}")
async def schedule_card(request: Request, group_code: str):
    """Разобранное расписание группы: занятия по дням, время можно поправить."""
    user = await require_user(request)
    code = norm_group(group_code)
    rows = await repo.lessons_for_group(code)
    if not rows:
        return page("Расписание", f'<div class="card msg-bad">Для группы {esc(code)} расписание не разобрано. '
                                  f'<a class="btn-grey" href="/panel/schedules">К списку</a></div>', user, "/schedules")
    by_day: dict[int, list] = {}
    for row in rows:
        by_day.setdefault(int(row["weekday"]), []).append(row)
    days = ""
    for weekday in sorted(by_day):
        lessons = "".join(
            f"<tr><td>{esc(lesson['lesson_num'])}</td><td>{esc(lesson['subject'])}</td>"
            f"<td>{esc(lesson['teacher'] or '—')}</td><td>{esc(lesson['room'] or '—')}</td>"
            f"<td><form method='post' action='/panel/schedules/lesson-time' class='inline'>{csrf(request)}"
            f"<input type='hidden' name='group_code' value='{esc(code)}'>"
            f"<input type='hidden' name='weekday' value='{weekday}'>"
            f"<input type='hidden' name='lesson_num' value='{esc(lesson['lesson_num'])}'>"
            f"<input name='start' value='{esc(lesson['start'])}' size='5'>"
            f"<input name='end' value='{esc(lesson['end'])}' size='5'>"
            f"<button class='btn-grey'>✓</button></form></td></tr>"
            for lesson in by_day[weekday]
        )
        days += (f"<h3>{esc(WEEKDAYS_FULL[weekday].capitalize())}</h3>"
                 f"<table><tr><th>Пара</th><th>Предмет</th><th>Преподаватель</th><th>Ауд.</th><th>Время</th></tr>"
                 f"{lessons}</table>")
    return page(f"Расписание {code}", f'<div class="card">{days}'
                                       f'<p><a class="btn-grey" href="/panel/schedules">← К списку</a></p></div>',
                user, "/schedules")


@router.post("/schedules/lesson-time")
async def schedule_lesson_time(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    weekday = to_int(value(data, "weekday"), -1)
    number = to_int(value(data, "lesson_num"), -1)
    start, end = value(data, "start"), value(data, "end")
    if not _time_ok(start) or not _time_ok(end):
        flash("!Время в формате ЧЧ:ММ, например 09:00.")
        return redirect(f"/panel/schedules/{code}")
    changed = await repo.set_lesson_time(code, weekday, number, start, end)
    if changed:
        await repo.log_action(actor, "время пары исправлено", f"{code}, день {weekday}, пара {number}: {start}–{end}")
        flash(f"Время пары сохранено: {start}–{end}.")
    else:
        flash("!Такая пара не найдена — возможно, расписание переразобрали.")
    return redirect(f"/panel/schedules/{code}")


def _time_ok(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", value.strip())) and 0 <= int(value.split(":")[0]) <= 23


@router.post("/schedules/parse")
async def schedules_parse(request: Request):
    """Разбирает PDF одной группы и показывает, что получилось."""
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    if not await repo.get_schedule(code):
        flash("!У группы нет ссылки на PDF.")
        return redirect("/panel/schedules")
    result = await schedules.parse_group(code, force=True)
    if result.has_lessons:
        await repo.log_action(actor, "расписание разобрано", f"{code}: {result.schedule.lessons_count} пар")
        flash(f"Группа {code}: разобрано {result.schedule.lessons_count} пар по {len(result.schedule.days)} дням.")
    else:
        await repo.log_action(actor, "разбор расписания не удался", f"{code}: {result.reason}")
        flash(f"!Группа {code}: {result.reason}.")
    return redirect("/panel/schedules")


@router.post("/schedules/parse_all")
async def schedules_parse_all(request: Request):
    actor = await require_form(request)
    results = await schedules.refresh_all()
    good = [code for code, result in results.items() if result.has_lessons]
    bad = [f"{code}: {result.reason}" for code, result in results.items() if not result.has_lessons]
    await repo.log_action(actor, "расписания обновлены", f"успешно {len(good)}, с ошибкой {len(bad)}")
    if bad:
        flash(f"Обновлено: {len(good)}. С ошибкой — " + "; ".join(bad[:5]))
    else:
        flash(f"Обновлено расписаний: {len(good)}.")
    return redirect("/panel/schedules")


@router.post("/schedules/times")
async def schedules_times(request: Request):
    """Сохраняет звонки по номерам уроков и сразу пересчитывает время разобранных занятий."""
    actor = await require_form(request)
    data = await request.form()
    collected: dict[int, tuple[str, str]] = {}
    for key in data.keys():
        # поля приходят парами: t1a — начало первого урока, t1b — конец
        match = re.fullmatch(r"t(\d+)a", key)
        if not match:
            continue
        number = int(match.group(1))
        start, end = value(data, key), value(data, f"t{number}b")
        if _time_ok(start) and _time_ok(end):
            collected[number] = (start, end)
    if not collected:
        flash("!Не найдено ни одного корректного времени.")
        return redirect("/panel/schedules")
    size = max(collected)
    times = tuple(collected.get(number, ("", "")) for number in range(1, size + 1))
    await db.set_setting("lesson_times", schedules.times_to_json(times))
    updated = await repo.reapply_lesson_times(times)
    await repo.log_action(actor, "звонки изменены", f"уроков: {size}, обновлено занятий: {updated}")
    flash(f"Звонки сохранены: {size} уроков. Время пересчитано у {updated} занятий.")
    return redirect("/panel/schedules")


@router.post("/schedules/save")
async def schedules_save(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    url = value(data, "pdf_url")
    if not valid_group(code):
        flash("!Код группы: буквы, цифры, дефис и точка (например ИС-21).")
        return redirect("/panel/schedules")
    ok, reason = await probe_pdf_url(url)
    if not ok:
        flash(f"!{reason} Ссылка не сохранена.")
        return redirect("/panel/schedules")
    changed = await repo.upsert_schedule(code, url)
    result = await schedules.parse_group(code, force=True)
    if result.has_lessons:
        await notify_schedule_subscribers(
            code, f"🔔 Расписание группы {code} обновлено.\n\n{tt.format_upcoming(result.schedule)}")
        flash(f"Расписание группы {code} сохранено и разобрано: "
              f"{result.schedule.lessons_count} пар по {len(result.schedule.days)} дням.")
    else:
        flash(f"Ссылка сохранена, но разобрать не вышло: {result.reason}. Студенты пока увидят ссылку на PDF.")
    await repo.log_action(actor, "ссылка на расписание сохранена", f"{code}: {short(url, 60)}")
    log.info("панель: расписание %s — %s, разбор: %s", code, "изменено" if changed else "прежнее",
             "ок" if result.has_lessons else result.reason)
    return redirect("/panel/schedules")


@router.post("/schedules/delete")
async def schedules_delete(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    if not await repo.get_schedule(code):
        flash("!Расписание не найдено.")
        return redirect("/panel/schedules")
    await repo.delete_schedule(code)
    await notify_schedule_subscribers(code, f"🗑 Расписание группы {code} удалено.")
    await repo.log_action(actor, "расписание удалено", f"{code}, подписчики уведомлены")
    flash(f"Расписание группы {code} удалено.")
    return redirect("/panel/schedules")


# ── рассылки ──────────────────────────────────────────────────────────────────
@router.get("/broadcasts")
async def broadcasts_list(request: Request):
    user = await require_user(request)
    rows = await repo.broadcast_history(100)
    audience_options = {"all": "всем студентам"}
    for row in await repo.top_groups(300):
        audience_options[row["group_code"]] = f"группе {row['group_code']} ({row['students']} чел.)"
    body = f"""
<div class="card"><h2>Новая рассылка</h2>
<form method="post" action="/panel/broadcasts/send">{csrf(request)}<div class="grid">
<div>{select('audience', audience_options, 'all')}</div>
<div><label>MAX ID для проверки (необязательно)</label><input name="test_to" placeholder="проверить на себе"></div>
<div class="full"><label>Текст объявления</label><textarea name="text" maxlength="3500"
  placeholder="Уважаемые студенты…"></textarea></div></div>
<p class="small mut">Объявление придёт с подписью «— ФИО, должность». Отправка идёт в фоне,
итог придёт вам в MAX и появится в истории ниже.</p>
<div class="grid" style="margin-top:10px"><button>Отправить рассылку</button></div></form></div>
<div class="card"><h2>История рассылок</h2>{_broadcasts_table(rows)}</div>"""
    return page("Рассылки", body, user, "/broadcasts")


@router.post("/broadcasts/send")
async def broadcasts_send(request: Request):
    user = await require_form(request)
    data = await request.form()
    text = as_str(data.get("text", "")).strip()[:3500]
    audience = value(data, "audience", default="all")
    test_to = as_str(data.get("test_to", "")).strip()
    if not text:
        flash("!Введите текст объявления.")
        return redirect("/panel/broadcasts")
    if audience != "all" and not valid_group(audience):
        flash("!Неизвестная группа — выберите её из списка.")
        return redirect("/panel/broadcasts")
    if test_to:
        ok = await notify(test_to, f"🧪 Тест рассылки от {user}. Текст:\n\n{text}")
        flash("Тестовое сообщение отправлено." if ok else f"Не удалось отправить тестовое сообщение: {test_to}")
    audience_size = len(await repo.audience_ids(audience))
    if not audience_size:
        flash("!Нет получателей: в группе никто не зарегистрирован.")
        return redirect("/panel/broadcasts")
    spawn(run_broadcast(user, audience, text))
    log.info("панель: рассылка запущена %s от %s", audience, user)
    flash(f"Рассылка запущена: {audience_size} получателей.")
    return redirect("/panel/broadcasts")


# ── настройки ─────────────────────────────────────────────────────────────────
@router.get("/settings")
async def settings_page(request: Request):
    user = await require_user(request)
    rows = await repo.all_settings()
    welcome = await db.get_setting("welcome_text", "")
    tickets_enabled = await db.get_setting("tickets_enabled", "1") == "1"
    actions = await repo.admin_log(15)
    counts = await repo.admin_log_counts(30)
    sysadmins = await repo.list_sysadmins()
    action_rows = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(item['created_at']))}</td>"
        f"<td class='small'>{esc(item['actor_name'] or item['actor_id'])}</td>"
        f"<td>{esc(item['action'])}</td><td class='small'>{esc(item['details'])}</td></tr>"
        for item in actions
    ) or "<tr><td colspan='4' class='mut'>Действий пока не было</td></tr>"
    count_rows = "".join(
        f"<tr><td>{esc(action)}</td><td>{esc(number)}</td></tr>"
        for action, number in counts.items()
    ) or "<tr><td class='mut'>Пусто</td></tr>"
    body = f"""
<div class="card"><h2>Приём обращений</h2>
<form method="post" action="/panel/settings/tickets">{csrf(request)}
<input type="hidden" name="enabled" value="{"0" if tickets_enabled else "1"}">
<button class="{"btn-bad" if tickets_enabled else "btn-ok"}">{"Выключить" if tickets_enabled else "Включить"}</button>
<span class="small mut">сейчас: {"включён" if tickets_enabled else "выключен"}</span></form></div>
<div class="card"><h2>Приветствие студентов</h2>
<form method="post" action="/panel/settings/welcome">{csrf(request)}
<textarea name="value" maxlength="500">{esc(welcome)}</textarea>
<div class="grid" style="margin-top:10px"><button>Сохранить</button></div></form></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Действия сис-админов</h2>
  <table><tr><th>Когда</th><th>Кто</th><th>Что сделал</th><th>Подробности</th></tr>{action_rows}</table>
  <p class="small mut">Кто и когда менял должности, выдавал коды, удалял людей и трогал базу.
  Записи ведутся при каждом действии и не удаляются вместе с данными.</p></div>
  <div class="card" style="flex:1"><h2>За 30 дней</h2>
  <table><tr><th>Действие</th><th>Раз</th></tr>{count_rows}</table></div>
</div>
<div class="card"><h2>Прочие настройки (ключ → значение)</h2>
<form method="post" action="/panel/settings/raw">{csrf(request)}<table>"""
    for row in rows:
        body += (f"<tr><td style='width:200px'><input name='key' value='{esc(row['key'])}'></td>"
                 f"<td><input name='value' value='{esc(row['value'])}'></td></tr>")
    body += f"""</table>
<div class="grid" style="margin-top:10px"><button>Сохранить</button>
<a class="btn btn-grey" href="/panel/settings">Обновить список</a></div></form></div>
<div class="card"><h2>Служебное</h2><table>
<tr><th>Режим</th><td>{"webhook" if config.WEBHOOK_URL else "long polling"}</td></tr>
<tr><th>Адрес API</th><td>{esc(config.MAX_API_URL)}</td></tr>
<tr><th>Файл журнала</th><td>{esc(config.LOG_FILE)}</td></tr>
<tr><th>Сис-админов в базе</th><td>{len(sysadmins)} ({esc(", ".join(item["full_name"] for item in sysadmins) or "—")})</td></tr>
<tr><th>SYSADMIN_IDS (.env)</th><td>{esc(", ".join(str(i) for i in config.SYSADMIN_IDS) or "—")}</td></tr>
<tr><th>Автокопия базы</th><td>{("раз в %s ч" % config.BACKUP_EVERY_HOURS) if config.BACKUP_EVERY_HOURS else "выключена"}</td></tr>
<tr><th>Перечитывание PDF расписания</th><td>раз в {config.SCHEDULE_CACHE_HOURS} ч</td></tr>
</table><p class="small mut">Секреты (.env, токен бота) панель не показывает.</p></div>"""
    return page("Настройки", body, user, "/settings")


@router.post("/settings/tickets")
async def settings_tickets(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("tickets_enabled", "0" if value(data, "enabled") == "1" else "1")
    flash("Настройка сохранена.")
    return redirect("/panel/settings")


@router.post("/settings/welcome")
async def settings_welcome(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("welcome_text", as_str(data.get("value", "")).strip()[:500])
    flash("Приветствие сохранено.")
    return redirect("/panel/settings")


@router.post("/settings/raw")
async def settings_raw(request: Request):
    await require_form(request)
    data = await request.form()
    keys = data.getlist("key") if hasattr(data, "getlist") else []
    values = data.getlist("value") if hasattr(data, "getlist") else []
    saved = 0
    for key, val in zip(keys, values, strict=False):
        name = as_str(key).strip()
        if name:
            await db.set_setting(name, as_str(val).strip())
            saved += 1
    flash(f"Сохранено настроек: {saved}.")
    return redirect("/panel/settings")


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
<div class="card"><h2>Проверки</h2>
<form method="post" action="/panel/logs/test" class="grid">
{csrf(request)}
<div><label>Отправить тестовое сообщение сис-админу (MAX ID)</label><input name="to" value="{esc(user)}"></div>
<div><button>Отправить</button></div>
<div><button name="action" value="api" class="btn-grey">Проверить API MAX</button></div>
<div><button name="action" value="ping" class="btn-grey">Записать строку в журнал</button></div>
</form>
<table style="margin-top:12px">
<tr><th>Обработано событий</th><td>{esc(dedupe['processed'])}</td></tr>
<tr><th>Самое новое событие</th><td>{esc(dedupe['newest'] or '—')}</td></tr>
<tr><th>Самое старое в очистке</th><td>{esc(dedupe['oldest'] or '—')}</td></tr>
</table></div>
<div class="card"><h2>Журнал: {esc(config.LOG_FILE)}</h2>
<form method="get" action="/panel/logs" class="grid" style="margin-bottom:10px">
<div>{select('level', options, level)}</div>
<div><label>Строк</label><input name="lines" type="number" value="{lines}" min="20" max="2000"></div>
<div><button>Показать</button></div></form>
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
