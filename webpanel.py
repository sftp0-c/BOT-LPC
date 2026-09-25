"""Веб-панель сис-админа: /panel — те же данные, что и в боте, но редактируются мышью.

Панель живёт в том же процессе, что и бот (FastAPI), поэтому все изменения сразу
видны боту и в MAX. Вход — по MAX ID сис-админа и паролю WEB_PANEL_PASSWORD из .env;
пока пароль не задан, панель отвечает 503.

Вкладки: обзор, обращения, студенты, сотрудники, группы, расписания, рассылки,
настройки, журнал и тесты. JSON-API для скриптов и проверок — /panel/api/*.
"""
import hmac
import logging
import secrets
import time
from html import escape as _escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import config
import database as db
import repository as repo
from handlers.admin import STAFF_ROLES, notify_schedule_subscribers, probe_pdf_url
from handlers.broadcast import run_broadcast
from handlers.common import api as max_api
from handlers.common import notify, spawn
from utils import (
    OPEN_STATUSES,
    STAFF_CATS,
    STATUS,
    as_str,
    is_sysadmin_role,
    log_level_of,
    norm_group,
    tail_file,
    valid_group,
)

log = logging.getLogger("panel")
router = APIRouter(prefix="/panel", tags=["panel"])

COOKIE = "lpc_panel"          # имя cookie-сессии
LOG_LINES = 400               # сколько строк журнала показывать по умолчанию
_flash = ""                   # одноразовое сообщение для следующей страницы


# ── сессии и доступ ───────────────────────────────────────────────────────────
_sessions: dict[str, tuple[str, float]] = {}  # токен → (MAX ID, время окончания)


def _purge_sessions() -> None:
    now = time.time()
    for token in [t for t, (_, exp) in _sessions.items() if exp < now]:
        _sessions.pop(token, None)


def start_session(user_id: str) -> str:
    """Новый токен сессии; время жизни — WEB_PANEL_HOURS."""
    _purge_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = (str(user_id), time.time() + max(1, config.WEB_PANEL_HOURS) * 3600)
    return token


def end_session(token: str) -> None:
    _sessions.pop(token, None)


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
    """Своим ли ID пользователь: запись сис-админа в БД или SYSADMIN_IDS из .env."""
    uid = str(user_id)
    if uid in {str(value) for value in config.SYSADMIN_IDS or []}:
        return True
    a = await repo.get_admin(uid)
    return bool(a) and is_sysadmin_role(as_str(a["role_type"]))


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
    """То же, что require_user, плюс проверка CSRF-токена из формы."""
    user = await require_user(request)
    form = await request.form()
    if not hmac.compare_digest(as_str(form.get("csrf", "")), request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=403, detail="Устаревшая форма — обновите страницу")
    return user


# ── HTML ──────────────────────────────────────────────────────────────────────
STYLE = """
:root{--bg:#f4f5f7;--card:#fff;--line:#dfe3e8;--ink:#1c2530;--mut:#6a7684;--acc:#2563eb;--bad:#c0392b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
header{background:#1f2a37;color:#fff;padding:12px 20px}
header h1{margin:0;font-size:17px;font-weight:600}
header .sub{color:#9fb0c3;font-size:13px}
nav{display:flex;flex-wrap:wrap;gap:2px;background:#243244;padding:0 12px}
nav a{color:#c7d3e0;padding:10px 13px;text-decoration:none;font-size:14px;border-bottom:3px solid transparent}
nav a:hover{background:#2e3f54;color:#fff}
nav a.on{color:#fff;border-bottom-color:var(--acc);background:#2e3f54}
main{max-width:1100px;margin:20px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:16px}
h3{margin:18px 0 8px;font-size:14px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:0}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.stat{flex:1 1 140px;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px}
.stat b{display:block;font-size:24px;line-height:1.2}
.stat span{color:var(--mut);font-size:13px}
input,select,textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;
     font:inherit;background:#fff;color:var(--ink)}
textarea{min-height:110px;resize:vertical}
label{display:block;margin:8px 0 3px;font-size:13px;color:var(--mut)}
button,.btn{display:inline-block;padding:7px 13px;border:0;border-radius:6px;background:var(--acc);
     color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button:hover,.btn:hover{filter:brightness(1.08)}
.btn-grey{background:#5b6675}.btn-bad{background:var(--bad)}.btn-ok{background:#1d8a4e}
.grid{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.grid>*{flex:1 1 160px}
.grid .full{flex:1 1 100%}
.mut{color:var(--mut)}.small{font-size:13px}
pre{background:#111a24;color:#cfe0f0;padding:14px;border-radius:8px;overflow:auto;
     max-height:560px;font:12.5px/1.45 Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.msg{padding:10px 12px;border-radius:6px;margin-bottom:12px;font-size:14px}
.msg-ok{background:#e6f6ec;border:1px solid #b6e0c6}
.msg-bad{background:#fdecea;border:1px solid #f2c2bd}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;background:#eef1f5;color:#41505f;font-size:12px}
.pill-on{background:#e6f6ec;color:#1d6b3d}.pill-off{background:#f1f2f4;color:#77808a}
form.inline{display:inline}
footer{color:var(--mut);font-size:12px;padding:10px 16px 24px;text-align:center}
"""


def esc(value) -> str:
    return _escape(as_str(value), quote=True)


def flag(value) -> bool:
    return as_str(value).strip().lower() in ("1", "true", "yes", "on")


TABS = (
    ("/", "Обзор"),
    ("/tickets", "Обращения"),
    ("/students", "Студенты"),
    ("/staff", "Сотрудники"),
    ("/groups", "Группы"),
    ("/schedules", "Расписания"),
    ("/broadcasts", "Рассылки"),
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
    return f'<input type="hidden" name="csrf" value="{esc(request.cookies.get(COOKIE, ""))}">'


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
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    groups = await repo.top_groups(10)
    tickets = await repo.admin_tickets(None, 10)
    last_bc = (await repo.broadcast_history(3)) or []

    def stat(value_, label: str) -> str:
        return f'<div class="stat"><b>{esc(value_)}</b><span>{esc(label)}</span></div>'

    open_total = sum(counts.get(code, 0) for code in OPEN_STATUSES)
    cards = "".join([
        stat(st["students"], "студентов"),
        stat(st["staff"], "сотрудников"),
        stat(st["total"], "обращений всего"),
        stat(open_total, "открытых"),
        stat(st["week"], "за 7 дней"),
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


# ── обращения ─────────────────────────────────────────────────────────────────
@router.get("/tickets")
async def tickets_list(request: Request, status: str = "", q: str = ""):
    user = await require_user(request)
    rows = await repo.admin_tickets(None, 200)
    if status:
        rows = [r for r in rows if as_str(r["status"]) == status]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in as_str(r["text_content"]).lower() or needle in as_str(r["student_id"])]
    counts = await repo.status_counts()
    options = {"": "все статусы"} | {code: label for code, label in STATUS.items()}
    filters = f"""
<form method="get" action="/panel/tickets" class="grid" style="margin-bottom:14px">
<div>{select("status", options, status)}</div>
<div><label>Поиск по тексту или ID</label><input name="q" value="{esc(q)}"></div>
<div><button>Найти</button></div></form>"""
    summary = " · ".join(f"{STATUS.get(c, c)}: {counts.get(c, 0)}" for c in STATUS)
    return page("Обращения", f'<div class="card"><p class="small mut">{esc(summary)}</p>{filters}'
                 f"{_tickets_table(rows)}</div>", user, "/tickets")


@router.get("/tickets/{ticket_id}")
async def ticket_card(request: Request, ticket_id: int):
    user = await require_user(request)
    t = await repo.get_ticket(ticket_id)
    if not t:
        return page("Обращение", '<div class="card msg-bad">Обращение не найдено.</div>', user, "/tickets")
    student = t.get("student") or {}
    staff = t.get("staff") or {}
    messages = await repo.ticket_messages(ticket_id, 50)
    history = "".join(
        f"<tr><td class='small mut'>{'студент' if m['sender_role'] == 'student' else 'сотрудник'} "
        f"{esc(m['sender_id'])}</td><td>{esc(m['text'])}</td></tr>"
        for m in reversed(messages)
    ) or "<tr><td class='mut'>Сообщений нет</td></tr>"
    status_options = dict(STATUS)
    head = f"""
<div class="card"><h2>Обращение №{esc(t['ticket_id'])}</h2>
<table>
<tr><th>Студент</th><td>{esc(student.get('full_name', '—'))} (ID {esc(student.get('user_id', t['student_id']))}),
    группа {esc(student.get('group_code', '—'))}</td></tr>
<tr><th>Сотрудник</th><td>{esc(staff.get('full_name', '—'))} (ID {esc(t['target_admin_id'])}),
    {esc(staff.get('role') or '—')}</td></tr>
<tr><th>Категория</th><td>{esc(t['category'])} {esc(t['topic'] or '')}</td></tr>
<tr><th>Статус</th><td>{esc(STATUS.get(t['status'], t['status']))}</td></tr>
<tr><th>Текст</th><td>{esc(t['text_content'])}</td></tr>
<tr><th>Создано</th><td>{esc(t['created_at'])}</td></tr>
</table>
<div style="margin-top:14px">{form(request, f"/panel/tickets/{t['ticket_id']}/status",
     select("status", status_options, t["status"]), "Сменить статус", "btn-ok")}</div></div>
<div class="card"><h2>Переписка</h2><table>{history}</table></div>"""
    return page(f"Обращение №{ticket_id}", head, user, "/tickets")


@router.post("/tickets/{ticket_id}/status")
async def ticket_status(request: Request, ticket_id: int):
    user = await require_form(request)
    form_data = await request.form()
    status = as_str(form_data.get("status", "")).strip()
    t = await repo.get_ticket(ticket_id)
    if not t or status not in STATUS:
        raise HTTPException(status_code=400, detail="Нет такого обращения или статуса")
    await repo.set_ticket_status(ticket_id, status)
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
        f"<td>{esc(row['tickets'])}</td><td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    ) or "<tr><td class='mut'>Студентов не найдено</td></tr>"
    table = (f"<table><tr><th>ФИО</th><th>MAX ID</th><th>Группа</th><th>Обращений</th><th>В базе с</th></tr>"
             f"{body}</table>")
    return page("Студенты", f'<div class="card">{head}{table}<p class="small mut">Показаны первые 200.</p></div>',
                user, "/students")


# ── сотрудники и сис-админы ───────────────────────────────────────────────────
@router.get("/staff")
async def staff_list(request: Request):
    user = await require_user(request)
    rows = await repo.all_admins()
    body = ""
    for row in rows:
        uid = as_str(row["user_id"])
        super_row = is_sysadmin_role(as_str(row["role_type"]))
        role_options = {"": "— не назначена —", **{code: label for code, label in STAFF_ROLES.items()}}
        cat_options = dict(STAFF_CATS)
        head_row = f"""
<tr><td><b>{esc(row['full_name'])}</b><div class="small mut">ID {esc(uid)}</div></td>
<td>{"сис-админ" if super_row else "сотрудник"}</td>
<td>{esc(as_str(row['role']) or '—')}</td><td>{esc(as_str(row['office']) or '—')}</td>
<td>{esc(STAFF_CATS.get(row['ticket_category'], row['ticket_category']))}</td>
<td>{"✅" if flag(row['can_broadcast']) else "—"}</td></tr>"""
        if super_row:
            body += head_row
            continue
        fields = (
            f'<div class="full"><label>ФИО</label><input name="full_name" value="{esc(row["full_name"])}"></div>'
            f"{select('role', role_options, row['role'])}"
            f"{input('office', row['office'])}"
            f"{select('ticket_category', cat_options, row['ticket_category'])}"
            f"{select('can_broadcast', {'0': 'нет', '1': 'да'}, '1' if flag(row['can_broadcast']) else '0')}"
        )
        body += (f"{head_row}<tr><td colspan='6' style='background:#f8fafc;padding:10px'>"
                 f"{form(request, f'/panel/staff/{esc(uid)}', fields)}"
                 f"<form method='post' action='/panel/staff/{esc(uid)}/delete' class='inline' "
                 f"onclick=\"return confirm('Удалить сотрудника {esc(row['full_name'])}?')\">"
                 f"{csrf(request)}<button class='btn-bad'>Удалить</button></form></td></tr>")
    table = f'<table><tr><th>Сотрудник</th><th>Роль в боте</th><th>Должность</th><th>Кабинет</th>' \
            f"<th>Обращения</th><th>Рассылка</th></tr>{body}</table>"
    add = form(
        request, "/panel/staff/add",
        input("user_id", "", full=False) + input("full_name", "")
        + input("role", "") + input("office", "")
        + select("ticket_category", dict(STAFF_CATS), "all")
        + select("can_broadcast", {"0": "нет", "1": "да"}, "0"),
        "Добавить сотрудника", "btn-ok",
    )
    add_sys = form(request, "/panel/staff/sysadmin", input("user_id", ""), "Добавить сис-админа", "btn-grey")
    body_all = f"""
<div class="card"><h2>Сотрудники</h2>{table}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Добавить сотрудника</h2>{add}
  <p class="small mut">Сотрудник узнаёт свой MAX ID командой <code>/id</code> в боте.</p></div>
  <div class="card" style="flex:1"><h2>Добавить сис-админа</h2>{add_sys}
  <p class="small mut">Роль сохраняется в базе — работает сразу, без правки .env и перезапуска.
  Строка <code>SYSADMIN_IDS</code> в .env остаётся основным способом.</p></div>
</div>"""
    return page("Сотрудники", body_all, user, "/staff")


@router.post("/staff/add")
async def staff_add(request: Request):
    await require_form(request)
    data = await request.form()
    uid = value(data, "user_id")
    if not uid.isdigit():
        flash("!MAX ID состоит только из цифр.")
        return redirect("/panel/staff")
    if await repo.get_admin(uid):
        flash("!Этот пользователь уже есть в списке.")
        return redirect("/panel/staff")
    await repo.add_staff(uid, value(data, "full_name") or f"Сотрудник {uid}")
    await repo.update_admin(
        uid,
        role=value(data, "role"),
        office=value(data, "office"),
        ticket_category=value(data, "ticket_category", default="all") or "all",
        can_broadcast=1 if value(data, "can_broadcast") == "1" else 0,
    )
    log.info("панель: добавлен сотрудник %s", uid)
    await notify(uid, "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, чтобы открыть кабинет.")
    flash(f"Сотрудник {uid} добавлен.")
    return redirect("/panel/staff")


@router.post("/staff/sysadmin")
async def staff_add_sysadmin(request: Request):
    await require_form(request)
    data = await request.form()
    uid = value(data, "user_id")
    if not uid.isdigit():
        flash("!MAX ID состоит только из цифр.")
        return redirect("/panel/staff")
    await repo.add_sysadmin(uid)
    log.info("панель: добавлен сис-админ %s", uid)
    flash(f"Сис-админ {uid} добавлен.")
    return redirect("/panel/staff")


@router.post("/staff/{user_id}")
async def staff_update(request: Request, user_id: str):
    await require_form(request)
    data = await request.form()
    if not await repo.get_admin(user_id):
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    await repo.update_admin(
        user_id,
        full_name=value(data, "full_name") or None,
        role=value(data, "role"),
        office=value(data, "office"),
        ticket_category=value(data, "ticket_category") or "all",
        can_broadcast=1 if value(data, "can_broadcast") == "1" else 0,
    )
    log.info("панель: изменён сотрудник %s", user_id)
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
    flash(f"Сотрудник {user_id} удалён.")
    return redirect("/panel/staff")


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
    await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    if not valid_group(code):
        flash("!Код группы: буквы, цифры, дефис и точка, до 30 символов (например ИС-21).")
        return redirect("/panel/groups")
    await repo.upsert_group(code, value(data, "title"), value(data, "active") == "1")
    flash(f"Группа {code} добавлена.")
    return redirect("/panel/groups")


@router.post("/groups/rename")
async def groups_rename(request: Request):
    await require_form(request)
    data = await request.form()
    old, new = norm_group(value(data, "old")), norm_group(value(data, "code"))
    if old == new:
        flash("Код не изменился.")
        return redirect("/panel/groups")
    if not await repo.rename_group(old, new):
        flash(f"!Не удалось переименовать {old} → {new}: проверьте код и что новый ещё не используется.")
        return redirect("/panel/groups")
    log.info("панель: группа %s → %s", old, new)
    flash(f"Группа {old} переименована в {new}.")
    return redirect("/panel/groups")


@router.post("/groups/toggle")
async def groups_toggle(request: Request):
    await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    row = await repo.get_group(code)
    if not row:
        flash("!Группа не найдена.")
        return redirect("/panel/groups")
    await repo.set_group_active(code, not flag(row["active"]))
    flash(f"Группа {code} {'показана' if not flag(row['active']) else 'скрыта'}.")
    return redirect("/panel/groups")


@router.post("/groups/delete")
async def groups_delete(request: Request):
    await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    await repo.delete_group(code)
    flash(f"Группа {code} удалена из справочника (данные студентов сохранены).")
    return redirect("/panel/groups")


# ── расписания ────────────────────────────────────────────────────────────────
@router.get("/schedules")
async def schedules_list(request: Request):
    user = await require_user(request)
    rows = await repo.schedule_groups(300)
    body = "".join(
        f"<tr><td><b>{esc(row['group_code'])}</b></td><td class='small'>{esc(row['pdf_url'])}</td></tr>" for row in rows
    ) or "<tr><td class='mut'>Расписаний пока нет</td></tr>"
    table = f"<table><tr><th>Группа</th><th>Ссылка на PDF</th></tr>{body}</table>"
    save = form(
        request, "/panel/schedules/save",
        input("group_code", "") + input("pdf_url", "", full=True),
        "Сохранить (проверяем ссылку)", "btn-ok",
    )
    drop = form(request, "/panel/schedules/delete", input("group_code", ""), "Удалить", "btn-bad")
    body_all = f"""
<div class="card"><h2>Расписания</h2>{table}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Сохранить расписание</h2>{save}
  <p class="small mut">Ссылка проверяется запросом к серверу. Подписчики группы получат уведомление.</p></div>
  <div class="card" style="flex:1"><h2>Удалить расписание</h2>{drop}</div>
</div>"""
    return page("Расписания", body_all, user, "/schedules")


@router.post("/schedules/save")
async def schedules_save(request: Request):
    await require_form(request)
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
    await repo.upsert_schedule(code, url)
    await notify_schedule_subscribers(code, f"📅 Расписание группы {code} обновлено\n{url}")
    log.info("панель: сохранено расписание %s", code)
    flash(f"Расписание группы {code} сохранено, подписчики уведомлены.")
    return redirect("/panel/schedules")


@router.post("/schedules/delete")
async def schedules_delete(request: Request):
    await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    if not await repo.get_schedule(code):
        flash("!Расписание не найдено.")
        return redirect("/panel/schedules")
    await repo.delete_schedule(code)
    await notify_schedule_subscribers(code, f"🗑 Расписание группы {code} удалено.")
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
<tr><th>SYSADMIN_IDS (.env)</th><td>{esc(", ".join(str(i) for i in config.SYSADMIN_IDS) or "—")}</td></tr>
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


@router.get("/api/health")
async def api_health(request: Request):
    """Диагностика без авторизации: только состояние, без данных."""
    try:
        await db.one("SELECT 1")
        database_ok = True
    except Exception as exc:  # noqa: BLE001 — в диагностике важно показать саму ошибку
        log.error("health: база недоступна: %s", exc)
        database_ok = False
    return JSONResponse({
        "ok": database_ok,
        "panel_enabled": panel_enabled(),
        "mode": "webhook" if config.WEBHOOK_URL else "polling",
        "log_file": config.LOG_FILE,
        "sysadmins": len(config.SYSADMIN_IDS),
    })
