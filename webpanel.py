"""Веб-панель сис-админа: /panel — те же данные, что и в боте, но редактируются мышью.

Панель живёт в том же процессе, что и бот (FastAPI), поэтому все изменения сразу
видны боту и в MAX. Вход — по MAX ID сис-админа и паролю WEB_PANEL_PASSWORD из .env;
пока пароль не задан, панель отвечает 503.

Вкладки: обзор, обращения, пользователи, сотрудники, коды и заявки, база данных,
настройки, журнал и тесты. JSON-API для скриптов и проверок — /panel/api/*.
"""
import clock
import contextvars
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
import college
import database as db
import repository as repo
import timetable as tt
import charts
import schedule_import
from panel_theme import (ICON_NAMES_BY_PATH, STYLESHEET, actions_script, hotkeys_script,
                          icon, panel_toast_js, theme_script)
from handlers import faq, schedules
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
    group_code,
    is_sysadmin_role,
    log_level_of,
    norm_code,
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
# приглашения открыты всем, поэтому отдельный роутер без префикса /panel
open_router = APIRouter(tags=["join"])

COLLEGE_SCHEDULE_PAGE = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"
COOKIE = "lpc_panel"          # имя cookie-сессии
LOG_LINES = 400               # сколько строк журнала показывать по умолчанию
# Флеш-сообщение раньше было одной переменной на весь модуль, поэтому при двух
# открытых панелях сообщение одного сис-админа показывалось другому. Теперь
# сообщения лежат по токену сессии, а токен кладёт middleware на каждый запрос.
_flashes: dict[str, str] = {}
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar("panel_token", default="")
# Бейджи меню считает nav_badges() на каждый показ страницы, а читает page().
_nav_badges: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "panel_nav_badges", default={})


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
    """MAX ID вошедшего сис-админа, иначе — 503 (панель выключена) или 303 на вход.

    Здесь же запоминаем токен сессии: через эту проверку проходит каждая
    страница панели, поэтому flash() знает, в чью сессию писать сообщение.
    И считаются бейджи меню - тем же местом, чтобы не искать его в каждой
    странице отдельно.
    """
    _current_token.set(request.cookies.get(COOKIE, ""))
    if not panel_enabled():
        raise HTTPException(status_code=503, detail="Панель выключена: задайте WEB_PANEL_PASSWORD в .env")
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/panel/login"})
    if not await is_sysadmin(user):
        raise HTTPException(status_code=403, detail="Панель доступна только сис-админам")
    # бейджи нужны только страницам: JSON-API и выгрузки CSV их не показывают
    if request.method == "GET" and "/api/" not in request.url.path \
            and not request.url.path.endswith(".csv"):
        await nav_badges()
    return user


async def nav_badges() -> dict[str, int]:
    """Числа для бейджиков меню: сколько ждёт внимания в каждой группе.

    «Обращения» — сколько без ответа, «Люди» — сколько писало боту, но без прав
    сотрудника, «Справочники» — сумма пробелов в данных. Числа кладём в
    contextvar: ``page()`` остаётся обычной функцией (её вызывает и тест без
    запроса), а свежесть гарантирует один пересчёт на страницу.

    Считается в общем блоке try: когда схема неполна, сводка не читается, и
    без этого поймать ошибку панель не смогла бы - вместо страницы с кнопкой
    «Восстановить схему» сис-админ получил бы 500.
    """
    try:
        today = await repo.admin_today()
        gaps = await repo.data_gaps()
    except Exception as exc:                       # noqa: BLE001 - бейджи не блокируют страницу
        log.warning("бейджи меню не посчитались: %s", exc)
        _nav_badges.set({})
        return {}
    data = {
        "Обращения": to_int(today["no_answer"]),
        "Люди": to_int(today["no_staff"]),
        "Справочники": sum(to_int(row["count"]) for row in gaps),
    }
    _nav_badges.set(data)
    return data


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
STYLE = STYLESHEET   # токены, тёмная тема по умолчанию, светлая по переключателю



def esc(value) -> str:
    return _escape(as_str(value), quote=True)


EMOJI_HEAD = re.compile(r"^[^\w]+", re.UNICODE)


def plain(label) -> str:
    """Название без эмодзи: в панели их место занимают иконки.

    Сами названия («🆕 Новое», «🔁 Всё») живут в utils и нужны боту как есть,
    поэтому чистим их только на том, что показываем в панели. В списках выбора
    (``<option>``) эмодзи остаются: там SVG не поддерживается.
    """
    text = as_str(label)
    return EMOJI_HEAD.sub("", text).strip() or text


def pill(text, kind: str = "") -> str:
    """Плашка-статус вместо эмодзи: «включено», «активна», «готово» и тому же."""
    cls = f' pill-{kind}' if kind in ("on", "off") else ""
    return f'<span class="pill{cls}">{esc(text)}</span>'


def state_pill(dot: str, text) -> str:
    """Состояние с цветной точкой: on - работает, off - выключено, bad - сломан."""
    return f'<span class="pill"><i class="dot-state {esc(dot)}"></i> {esc(text)}</span>'


def copy_btn(value, note: str = "Скопировано") -> str:
    """Кнопка-иконка: копирует значение по клику и подтверждает это тостом.

    Копировать приходится часто - MAX ID, код группы, код приглашения, ссылка
    на обращение, - а выделять текст в таблице неудобно.
    """
    return (f'<button type="button" class="copy-btn" data-copy="{esc(value)}" '
            f'data-copy-note="{esc(note)}" title="{esc(note)}" '
            f'aria-label="{esc(note)}: {esc(value)}">{icon("copy", 14)}</button>')


def code_cell(value, note: str = "") -> str:
    """Значение, рядом с которым стоит кнопка копирования."""
    return f'<span class="code-cell"><code>{esc(value)}</code>{copy_btn(value, note or f"Скопировано: {value}")}</span>'


def open_in_bot(target: str) -> str:
    """Кнопка «Открыть в боте». Без настройки bot_username её не будет вовсе."""
    if not target:
        return ""
    return (f'<a class="btn btn-grey" href="{esc(target)}" target="_blank" rel="noopener">'
            f'{icon("external", 16)} Открыть в боте</a>')


async def bot_open_link() -> str:
    """Прямая ссылка на бота в MAX: имя бот узнаёт о себе при старте.

    Пока настройки bot_username нет, ссылки нет - и кнопки «Открыть в боте» тоже.
    """
    name = as_str(await db.get_setting("bot_username", "")).strip().lstrip("@")
    return f"https://max.ru/{name}" if name else ""


def flag(value) -> bool:
    return as_str(value).strip().lower() in ("1", "true", "yes", "on")


# ── оболочка страницы ─────────────────────────────────────────────────────────
# Один и тот же <nav> на всех страницах: на широком экране CSS превращает его
# в боковое меню, на узком - в верхнюю ленту. Поэтому разметка не дублируется.
#
# Пунктов было шестнадцать, и половина экрана уходила на вкладки, которыми
# пользуются раз в неделю. Теперь их пять групп, а внутри группы - то же самое
# количество разделов: пути не менялись, старые /panel/<раздел> работают как
# раньше. Группа: название + пункты (путь, название, иконка).
NAV_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Пульт", (("/", "Обзор", "home"),
               ("/activity", "Лента событий", "activity"))),
    ("Обращения", (("/tickets", "Рабочее место", "tickets"),
                   ("/templates", "Шаблоны", "templates"),
                   ("/analytics", "Аналитика", "analytics"))),
    ("Люди", (("/people", "Реестр", "people"),
              ("/students", "Студенты", "students"),
              ("/staff", "Сотрудники", "staff"),
              ("/nostaff", "Без прав", "user-off"),
              ("/access", "Коды и заявки", "access"))),
    ("Справочники", (("/college", "Колледж", "college"),
                     ("/groups", "Группы", "groups"),
                     ("/schedules", "Расписания", "schedules"),
                     ("/broadcasts", "Рассылки", "broadcasts"))),
    ("Система", (("/settings", "Настройки", "settings"),
                  ("/database", "База данных", "database"),
                  ("/logs", "Журнал", "logs"))),
)

# Все разделы плоским списком - этим пользуются поиск по разделам и тесты.
TABS = tuple((path, name) for _group, items in NAV_GROUPS for path, name, _ico in items)

NAV_ICONS = ICON_NAMES_BY_PATH   # имена иконок вместо эмодзи

# Разделы для палитры Ctrl+K: путь с префиксом /panel и готовая иконка.
NAV_SECTIONS = tuple(
    (group, tuple((name, f"/panel{item_path}", icon(item_icon, 16))
                  for item_path, name, item_icon in items))
    for group, items in NAV_GROUPS
)

# Бейджи групп: сколько в группе ждёт внимания сис-админа. Считает nav_badges().
NAV_BADGE_GROUPS = ("Обращения", "Люди", "Справочники")


# страница приглашения: открытая, без входа в панель, читается с телефона
JOIN_STYLE = """
:root{--ink:#16202c;--mut:#67748a;--acc:#2563eb;--line:#e2e8f0}
*{box-sizing:border-box}
body{margin:0;background:#f1f4f9;color:var(--ink);
     font:16px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
main{max-width:520px;margin:0 auto;padding:28px 18px 40px;background:#fff;min-height:100vh}
h1{font-size:22px;margin:0 0 14px}
.lead{color:var(--mut);margin:0 0 12px}
.code{font:600 30px/1.2 ui-monospace,Consolas,monospace;letter-spacing:4px;
      text-align:center;padding:18px;margin:0 0 16px;border:2px dashed var(--acc);
      border-radius:12px;color:var(--acc);background:#eff4ff}
code{font-family:ui-monospace,Consolas,monospace;background:#eef2f7;padding:2px 6px;border-radius:6px}
.btn{display:block;text-align:center;background:var(--acc);color:#fff;text-decoration:none;
     padding:14px 18px;border-radius:10px;font-weight:600;margin:18px 0}
.small{color:var(--mut);font-size:13px}
"""
# Ссылка на бота в MAX: у бота есть числовой ID, и MAX открывает диалог по ссылке
# вида max.ru/bot<id>. Если адрес не задан - кнопка не показывается.
OPEN_BOT_BUTTON = ""


def join_link(code: str) -> str:
    """Адрес страницы-приглашения: открыта без входа в панель, код одноразовый."""
    base = as_str(config.PUBLIC_URL or "").strip().rstrip("/")
    if not base:
        return f"/join/{norm_code(code)}"          # внутри сети: относительный адрес
    return f"{base}/join/{norm_code(code)}"


async def bot_profile_link() -> str:
    """Ссылка на профиль бота в MAX по шаблону MAX_PROFILE_LINK.

    Имя бота бот узнаёт о себе при старте и кладёт в настройку bot_username,
    поэтому ссылку не приходится вписывать руками. Пусто - кнопки не будет.
    """
    template = as_str(config.MAX_PROFILE_LINK).strip()
    name = as_str(await db.get_setting("bot_username", "")).strip().lstrip("@")
    if not template or not name:
        return ""
    return template.replace("{username}", name)


def nav_html(tab: str, badges: dict | None = None) -> str:
    """Меню пятью группами. На узком экране группы выстраиваются в одну строку."""
    badges = badges if badges is not None else {}
    blocks = []
    for group, items in NAV_GROUPS:
        number = to_int(badges.get(group, 0))
        badge = f'<b class="nav-badge{" hot" if number else ""}">{number}</b>' if number else ""
        links = "".join(
            f'<a href="/panel{item_path}"{" class=\"on\"" if item_path == tab else ""}>'
            f'<span class="nav-ico" aria-hidden="true">{icon(item_icon, 18)}</span>'
            f'<span class="nav-txt">{esc(name)}</span></a>'
            for item_path, name, item_icon in items
        )
        blocks.append(
            f'<div class="nav-group{" on" if any(p == tab for p, _n, _i in items) else ""}">'
            f'<span class="nav-group-label"><span class="nav-group-name">{esc(group)}</span>'
            f'{badge}</span><div class="nav-group-items">{links}</div></div>'
        )
    return '<div class="nav-groups">' + "".join(blocks) + "</div>"


def page(title: str, body: str, user: str = "", tab: str = "",
         actions: str = "") -> HTMLResponse:
    """Оболочка страницы: шапка, меню группами, заголовок с действиями, скрипты.

    ``actions`` - кнопки в шапке (выгрузка CSV, печать): раньше они прятались
    в середине страницы, а на телефоне до них приходилось долистывать.
    """
    nav = nav_html(tab, _nav_badges.get())
    notice = _flashes.pop(_current_token.get(""), "")
    kind = "bad" if notice.startswith("!") else "ok"
    mark = icon("warning", 20) if kind == "bad" else icon("check", 20)
    banner = (f'<div class="msg msg-{kind}">{mark}<span>{esc(notice.lstrip("!"))}</span></div>'
              if notice else "")
    head = (f'<div class="dochead"><h1 class="page-title">{esc(title)}</h1>'
            f'<div class="page-actions">{actions}</div></div>' if actions
            else f'<h1 class="page-title">{esc(title)}</h1>')
    return HTMLResponse(
        f"""<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — панель сис-админа</title><style>{STYLE}</style></head><body>
<header>
  <div class="brand"><span class="logo">{icon("college", 22)}</span><span>
    <b>Навигатор ЛПК</b><small>панель сис-админа</small></span></div>
  <form class="gsearch" method="get" action="/panel/search">
    <input name="q" value="" placeholder="Поиск: обращение, человек, сотрудник, группа…" autocomplete="off">
    <button type="submit" aria-label="Найти">{icon("search", 18)}</button>
  </form>
  <button class="theme-toggle" type="button" title="Клавиатура: Ctrl+K — разделы, ? — подсказка"></button>
  <div class="who">вошёл как <b>{esc(user) or '—'}</b> · <a href="/panel/logout">выйти</a></div>
</header>
<nav aria-label="Разделы панели">{nav}</nav>
<main>{head}{banner}{body}</main>
<footer>Данные те же, что в боте: изменения применяются сразу · MAX ID {esc(user)}</footer>
<script>{theme_script()}</script>
<script>{panel_toast_js()}</script>
<script>{actions_script()}</script>
<script>{hotkeys_script(NAV_SECTIONS)}</script>
</body></html>"""
    )


@router.get("/search")
async def global_search(request: Request, q: str = ""):
    """Общий поиск по панели: обращения, люди и сотрудники одним запросом."""
    user = await require_user(request)
    needle = as_str(q).strip()
    if len(needle) < 2:
        return page("Поиск", '<div class="card msg-bad">Введите хотя бы два символа.</div>', user, "/")
    low = needle.lower()
    tickets = [row for row in await repo.admin_tickets(None, 300)
               if low in as_str(row["text_content"]).lower()
               or low in as_str(row["topic"]).lower()
               or low in as_str(row["student_id"])]
    people = await repo.people(q=needle, limit=30)
    staff = [row for row in await repo.list_staff()
             if low in " ".join([as_str(row["user_id"]), as_str(row["full_name"]),
                                 as_str(row["position"]), as_str(row["department"])]).lower()]
    staff_rows = "".join(
        f'<tr><td><a href="/panel/staff/{esc(row["user_id"])}"><b>{esc(row["full_name"])}</b></a>'
        f'<div class="small mut">ID {esc(row["user_id"])}</div></td>'
        f'<td>{esc(as_str(row["position"]) or "—")}</td>'
        f'<td>{esc(as_str(row["department"]) or "—")}</td></tr>'
        for row in staff
    ) or "<tr><td colspan='3' class='mut'>Никого не нашлось</td></tr>"
    staff_table = f"<table><tr><th>Сотрудник</th><th>Должность</th><th>Отдел</th></tr>{staff_rows}</table>"
    body = (f'<p class="small mut">По запросу «{esc(needle)}»</p>'
            f"<div class=\"card\"><h2>{icon("tickets", 20)} Обращения: {len(tickets)}</h2>{_tickets_table(tickets[:20])}</div>"
            f"<div class=\"card\"><h2>{icon("people", 20)} Пользователи: {len(people)}</h2>{_people_table(people)}</div>"
            f"<div class=\"card\"><h2>{icon("staff", 20)} Сотрудники: {len(staff)}</h2>{staff_table}</div>")
    return page("Поиск", body, user, "/")


# Кабинет 115 зашит ТОЛЬКО под справки - так и было сказано приёмной.
# Для остального это не константа: сотрудник выбирает кабинет, иначе
# «готово» не закрывает обращение молча.
CERT_PICKUP = "115"

# Разделы, где справка забирается в 115. Всё, чего здесь нет, идёт в
# кабинет ответственного сотрудника.
CERT_CATEGORIES = ("certificates", "certificate", "spravka", "справка", "docs", "documents")
CERT_WORDS = ("справк", "справка", "справки")


def is_certificate(t) -> bool:
    """Обращение про справку - и только для них 115 остаётся зашитым."""
    category = as_str((t or {}).get("category") if isinstance(t, dict) else t["category"]).lower()
    if category in CERT_CATEGORIES:
        return True
    text = as_str((t or {}).get("topic") if isinstance(t, dict) else t["topic"]).lower()
    text += " " + as_str((t or {}).get("text_content") if isinstance(t, dict) else t["text_content"]).lower()
    return any(word in text for word in CERT_WORDS)


def pickup_hint(t) -> str:
    """Подсказка под полем кабинета: что подставится, если оставить пустым."""
    if is_certificate(t):
        return f"справка - {CERT_PICKUP}"
    return "кабинет ответственного сотрудника"


async def pickup_options() -> str:
    """Кабинеты для подсказки при вводе: из кабинетов сотрудников плюс 115.

    Спрашивать у человека «какой кабинет» бесполезно - он и не знает, что
    сотрудник сидит в 204-м. Список собирается из того, что уже есть в базе.
    """
    rooms = {CERT_PICKUP}
    for row in await repo.list_staff():
        office = as_str(row["office"]).strip() if "office" in row.keys() else ""
        if office:
            rooms.add(office)
    return "".join(f"<option value=\"{esc(room)}\">" for room in sorted(rooms))
TICKETS_PAGE = 25             # обращений в очереди рабочего места


def flash(message: str) -> None:
    """Одноразовое сообщение для следующей страницы (ошибка помечается «!»).

    Сообщение получает только та сессия, из которой оно отправлено.
    """
    _flashes[_current_token.get("")] = message[:300]


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


def select(name: str, options: dict, current: str, full: bool = False,
           label: str = "") -> str:
    """Список выбора с подписью: по умолчанию - имя поля, у важных фильтров
    подпись задаётся явно («Статус», «Раздел», «Тип события»)."""
    items = "".join(
        f'<option value="{esc(code)}"{" selected" if code == current else ""}>{esc(label)}</option>'
        for code, label in options.items()
    )
    extra = ' class="full"' if full else ""
    title = esc(label if label else name)
    return f'<div{extra}><label>{title}</label><select name="{esc(name)}">{items}</select></div>'


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


def panel_link(path: str) -> str:
    """Адрес раздела панели: абсолютный, если в .env задан PUBLIC_URL.

    Так ссылку на обращение можно вставить в чат - относительный путь там
    бесполезен.
    """
    base = as_str(config.PUBLIC_URL or "").strip().rstrip("/")
    return f"{base}/panel{path}" if base else f"/panel{path}"


# ── обзор ─────────────────────────────────────────────────────────────────────
@router.get("/")
async def overview(request: Request):
    """«Пульт»: счётчики, что требует действия сегодня, лента и пробелы в данных.

    Страница отвечает на два вопроса: «что сделать сейчас» и «кто что делал
    последним». Всё остальное живёт в своих разделах, поэтому сюда не возвращаются
    таблицы ради таблиц.
    """
    user = await require_user(request)
    st = await repo.stats_overview()
    people = await repo.people_overview()
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    today = await repo.admin_today()
    events = await repo.recent_ticket_events(20)
    open_total = sum(counts.get(code, 0) for code in OPEN_STATUSES)
    bot = await bot_open_link()

    counters = "".join([
        _big_stat("/panel/tickets?scope=waiting", "tickets", today["no_answer"],
                  "без ответа", "bad"),
        _big_stat("/panel/access", "access", today["requests"],
                  "заявок на роль", "warn"),
        _big_stat("/panel/nostaff", "user-off", today["no_staff"],
                  "без прав сотрудника", "warn"),
        _big_stat("/panel/tickets?status=ready", "check", today["ready_not_picked"],
                  "готово к выдаче", "good"),
        _big_stat("/panel/database", "refresh", today["stuck_states"],
                  "зависших диалогов", "warn"),
        _big_stat("/panel/analytics", "clock", today["avg_reply"] or "—",
                  "среднее время ответа", "", text_value=True),
    ])
    scale = " · ".join([
        f"<b>{st['students']}</b> студентов",
        f"<b>{st['staff']}</b> сотрудников",
        f"<b>{people['total']}</b> писали боту",
        f"<b>{st['total']}</b> обращений всего",
        f"<b>{open_total}</b> открытых",
        f"<b>{st['week']}</b> за 7 дней",
        f"<b>{today['tickets_day']}</b> за сутки",
        f"<b>{esc(dedupe['processed'])}</b> событий обработано",
    ])
    # что требует действия: строки всегда на месте, ноль означает «делать нечего»
    actions = "".join([
        _todo_row("Обращений без ответа", today["no_answer"],
                  "/panel/tickets?scope=waiting", "tickets"),
        _todo_row("Готово к выдаче, но не отмечено", today["ready_not_picked"],
                  "/panel/tickets?status=ready", "check"),
        _todo_row("Заявок на роль сотрудника", today["requests"],
                  "/panel/access", "access"),
        _todo_row("Писали боту, но без прав", today["no_staff"],
                  "/panel/nostaff", "user-off"),
        _todo_row("Активных кодов сотрудника", today["codes_active"],
                  "/panel/access", "college"),
        _todo_row("Зависших диалогов", today["stuck_states"],
                  "/panel/database", "refresh",
                  extra=_clear_states_form(request)),
    ])
    gaps = [row for row in await repo.data_gaps() if to_int(row["count"])]
    gaps_line = (' · '.join(f'<a href="{esc(row["link"])}">{esc(row["title"])}: '
                             f'<b>{esc(row["count"])}</b></a>' for row in gaps)
                 or "все данные заполнены")
    feed = _feed_list(events) or _empty_state("activity", "Событий пока нет")
    body = f"""
<div class="big-stats">{counters}</div>
<div class="scale-line">{scale}</div>
<div class="card"><h2>Что требует действия сегодня</h2>
<p class="small mut">Что сделать сегодня: строка ведёт в раздел, где это и делается,
а ноль означает «срочного нет». У зависших диалогов есть кнопка очистки.</p>
<ul class="todo">{actions}</ul></div>
<div class="card"><h2>{icon("activity", 20)} Последние события</h2>
<p class="small mut">Кто и что делал с обращениями. <a href="/panel/activity">Вся лента событий</a></p>
{feed}</div>
<div class="card"><h2>{icon("warning", 20)} Пробелы в данных</h2>
<p class="small mut">Пока эти строки не заполнены, части бота работают неполно: {gaps_line}</p></div>"""
    return page("Обзор", body, user, "/",
                actions=f'<a class="btn btn-grey" href="/panel/activity">'
                        f'{icon("activity", 16)} Вся лента событий</a>'
                        + open_in_bot(bot))


def _big_stat(href: str, icon_name: str, value, label: str, kind: str = "",
              text_value: bool = False) -> str:
    """Крупный счётчик: сам кликабельный и ведёт в раздел, где с этим работают.

    Число докручивает CSS (``--to``), а текстовое значение - как есть: так
    счётчик анимируется и не требует JavaScript. При выключенной анимации
    ``prefers-reduced-motion`` показывается конечное число.
    """
    number = (f'<b class="big-txt">{esc(value)}</b>' if text_value
              else f'<b class="count" style="--to:{to_int(value)}"></b>')
    empty = "" if text_value or to_int(value) else " zero"
    return (f'<a class="big-stat{" " + kind if kind else ""}{empty}" href="{esc(href)}" '
            f'aria-label="{esc(label)}: {esc(value)}">'
            f'<span class="big-ico">{icon(icon_name, 18)}</span>{number}'
            f'<span>{esc(label)}</span></a>')


def _todo_row(name: str, number, href: str, icon_name: str, extra: str = "") -> str:
    """Строка «что требует действия»: название-ссылка, число и действие рядом."""
    count = to_int(number)
    return (f'<li><div class="todo-row{" zero" if not count else " hot"}">'
            f'<span class="todo-ico">{icon(icon_name, 18)}</span>'
            f'<a class="todo-name" href="{esc(href)}">{esc(name)}'
            f'{icon("chevron-right", 14)}</a>'
            f'<b class="todo-n">{count}</b>{extra}</div></li>')


def _clear_states_form(request: Request) -> str:
    """Кнопка «Очистить» рядом с зависшими диалогами: та же чистка, что в боте."""
    question = "Очистить зависшие диалоги? Студенты смогут начать заново."
    # подтверждение в браузере - как у остальных опасных кнопок панели
    ask = " onclick=\"return confirm('" + esc(question) + "')\""
    return (f'<form method="post" action="/panel/cleanup/states" class="inline wb-tools"{ask}>'
            f'{csrf(request)}<button class="btn-sm btn-grey">'
            f'{icon("delete", 16)} Очистить</button></form>')


def _empty_state(icon_name: str, text: str) -> str:
    return (f'<div class="empty">{icon(icon_name, 34)}<span>{esc(text)}</span></div>')


# ── лента событий по всем обращениям ─────────────────────────────────────────
ACTIVITY_PAGE = 50        # событий на страницу ленты
ACTIVITY_TYPES = (
    ("", "все события"),
    ("created", "обращение создано"),
    ("status", "смена статуса"),
    ("message_student", "сообщение студента"),
    ("message_staff", "ответ сотрудника"),
    ("ready", "документ готов"),
    ("archive", "в архив"),
    ("restore", "вернулось из архива"),
    ("assign", "назначение сотрудника"),
)


def _event_matches(row, event: str, needle: str) -> bool:
    """Подходит ли событие под фильтры ленты: тип события и текст."""
    if event and as_str(row["event"]) != event:
        return False
    if not needle:
        return True
    haystack = " ".join([as_str(row["student_name"]), as_str(row["student_group"]),
                         as_str(row["topic"]), as_str(row["detail"]),
                         as_str(row["actor_name"]), as_str(row["actor_id"]),
                         str(to_int(row["ticket_id"]))]).lower()
    return needle in haystack


def _feed_list(rows) -> str:
    """Короткая лента для главной страницы: когда, кто и что сделал."""
    if not rows:
        return ""
    items = "".join(
        f'<li data-hk><span class="feed-time">{esc(fmt_when(row["created_at"]))}</span>'
        f'<span class="feed-what">{esc(repo.event_feed_label(row))}'
        + (f' <span class="small mut">— {esc(short(as_str(row["detail"]), 60))}</span>'
           if as_str(row["detail"]) else "")
        + "</span>"
        + copy_btn(panel_link(f"/tickets?t={to_int(row['ticket_id'])}"),
                   f"Ссылка на обращение №{to_int(row['ticket_id'])} скопирована")
        + "</li>"
        for row in rows
    )
    return f'<ul class="feed">{items}</ul>'


@router.get("/activity")
async def activity_page(request: Request, event: str = "", q: str = "", page_no: int = 1):
    """Сквозная лента событий по всем обращениям: фильтр, поиск, постраничный просмотр.

    Источник - ``repo.recent_ticket_events()``: тот же список, что и на главной,
    но с поиском по тексту и постранично. События приходят с конца, поэтому
    фильтр отбирает их уже в прочитанном окне: окно берём на страницу вперёд,
    иначе про следующую страницу узнать нельзя. При редком типе события лента
    заканчивается раньше, и панель честно пишет об этом.
    """
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    needle = as_str(q).strip().lower()
    window = min(2000, (page_no + 1) * ACTIVITY_PAGE)
    rows = [row for row in await repo.recent_ticket_events(window)
            if _event_matches(row, event, needle)]
    total = len(rows)
    pages = max(1, (total + ACTIVITY_PAGE - 1) // ACTIVITY_PAGE)
    current = rows[(page_no - 1) * ACTIVITY_PAGE: page_no * ACTIVITY_PAGE]
    bot = await bot_open_link()

    body_rows = "".join(
        f'<tr data-hk><td class="small mut">{esc(fmt_when(row["created_at"]))}</td>'
        f'<td>{esc(as_str(row["actor_name"]) or as_str(row["actor_id"]) or "кто-то")}'
        f'<div class="small mut">{esc(as_str(row["actor_position"]))}</div></td>'
        f'<td><a href="/panel/tickets?t={to_int(row["ticket_id"])}">'
        f'№{to_int(row["ticket_id"])}</a>'
        f'<div class="small mut">{esc(as_str(row["student_name"]) or as_str(row["student_id"]))}</div></td>'
        f'<td>{esc(_event_name(as_str(row["event"])))}</td>'
        f'<td class="small">{esc(short(as_str(row["detail"]), 90) or "—")}'
        f'{copy_btn(panel_link(f"/tickets?t={to_int(row["ticket_id"])}"), "Ссылка скопирована")}</td></tr>'
        for row in current
    ) or "<tr><td colspan='5' class='mut'>Событий не нашлось</td></tr>"
    table = ("<table><tr><th>Когда</th><th>Кто</th><th>Обращение</th><th>Событие</th>"
             f"<th>Деталь</th></tr>{body_rows}</table>")
    # список типов полный, а не «что нашлось на странице»: иначе выбранный
    # фильтр исчезал бы из списка ровно тогда, когда по нему ничего не нашлось
    kind_options = dict(ACTIVITY_TYPES)
    filters = f"""
<form method="get" action="/panel/activity" class="grid" style="margin-bottom:12px">
<div>{select("event", kind_options, event, label="Тип события")}</div>
<div><label>Поиск по тексту: студент, сотрудник, тема, деталь</label>
<input name="q" value="{esc(q)}" placeholder="например: справка"></div>
<div><button>{icon("search", 16)} Найти</button></div></form>"""
    pager = ""
    if pages > 1:
        def _step(number: int, label: str) -> str:
            link = f"/panel/activity?event={esc(event)}&q={esc(q)}&page_no={number}"
            cls = "btn-grey" if number != page_no else ""
            return f'<a class="btn {cls}" href="{link}">{label}</a>'
        pager = (f'<div class="pager">{_step(max(1, page_no - 1), f'{icon("chevron-left", 16)} Назад')}'
                 f'<span class="small mut">Страница {page_no} из {pages} · всего событий: {total}</span>'
                 f'{_step(min(pages, page_no + 1), f'Вперёд {icon("chevron-right", 16)}')}</div>')
    tail = "" if page_no < pages else (
        f'<p class="small mut">Это последняя страница: событий в окне — {total}.</p>')
    body = f"""<div class="card"><h2>{icon("activity", 20)} События по обращениям</h2>
<p class="small mut">Одно обращение - одна лента: создание, ответы, смена статуса, архив.
Свежие сверху, по {ACTIVITY_PAGE} событий на страницу.</p>
{filters}{table}{pager}{tail}</div>"""
    return page("Лента событий", body, user, "/activity",
                actions=open_in_bot(bot))


def _event_name(code: str) -> str:
    """Название типа события по его коду из базы."""
    for known, label in ACTIVITY_TYPES:
        if known == code:
            return label
    return code or "что-то сделал"


@router.post("/cleanup/states")
async def cleanup_states(request: Request):
    """Чистка зависших диалогов: та же кнопка, что в боте, но из панели.

    Состояния старше суток - это брошенные посреди регистрации диалоги: из-за
    них человек не может начать заново. Живые диалоги (свежий created_at)
    остаются нетронутыми.
    """
    actor = await require_form(request)
    removed = await db.prune("user_states", 1)
    log.info("панель: очищены зависшие диалоги - %s (сис-админ %s)", removed, actor)
    await repo.log_action(actor, "очищены зависшие диалоги", f"удалено состояний: {removed}")
    flash(f"Удалено зависших состояний: {removed}.")
    return redirect("/panel/")

def _tickets_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Обращений пока нет.</p>"
    body = "".join(
        f"<tr data-hk><td><a href='/panel/tickets/{esc(row['ticket_id'])}'>№{esc(row['ticket_id'])}</a></td>"
        f"<td>{esc(row['student_id'])}</td><td>{esc(row['target_admin_id'])}</td>"
        f"<td>{esc(plain(STAFF_CATS.get(row['category'], row['category'])))}</td>"
        f"<td>{esc(plain(STATUS.get(row['status'], row['status'])))}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    )
    return f"<table><tr><th>№</th><th>Студент</th><th>Сотрудник</th><th>Категория</th><th>Статус</th><th>Создано</th></tr>{body}</table>"


def _delivered_cell(sent, failed) -> str:
    """Доставка рассылки: плашками вместо зелёной галочки и красного крестика."""
    parts = [pill(f"доставлено {sent}", "on")]
    if to_int(failed):
        parts.append(pill(f"не доставлено {failed}"))
    return " ".join(parts)


def _broadcasts_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Рассылок пока не было.</p>"
    body = "".join(
        f"<tr><td><a href='/panel/broadcasts'>#{esc(row['id'])}</a></td>"
        f"<td>{esc(row['sender_name'] or row['sender_id'])}"
        f"{' <span class=\"small mut\">' + esc(row['sender_role']) + '</span>' if row['sender_role'] else ''}</td>"
        f"<td>{esc('всем' if row['audience'] == 'all' else row['audience'])}</td>"
        f"<td class='small'>{esc((row['text'] or '')[:90])}</td>"
        f"<td>{_delivered_cell(row['sent'], row['failed'])}</td>"
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
            f"<tr data-hk><td><b>{esc(title)}</b><div class='small mut'>ID {esc(row['id'])}</div></td>"
            f"<td class='small'>{esc((as_str(row['text']) or '')[:220])}</td>"
            f"<td>{esc(plain(STAFF_CATS.get(row['category'], row['category'])))}</td>"
            f"<td>{esc(row['used_count'])}</td>"
            f"<td class='small mut'>{esc(fmt_when(row['created_at']))}</td>"
            f"<td>{_action_form(request, f'/panel/templates/{esc(row['id'])}/delete', f'{icon("delete", 16)} Удалить', confirm_text=confirm, cls='btn-bad')}"
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
<div class="card"><h2>{icon("templates", 20)} Шаблоны ответов: {total}</h2>
<p class="small mut">Сотрудник в карточке обращения открывает «Шаблоны» - выбирает подходящий
и отправляет как есть или дописывает своё. Шаблон с разделом «Всё» показывается всегда,
остальные - только в своём разделе. Колонка «Применён» показывает, какие ответы реально нужны.</p>
{table}</div>
<div class="card"><h2>{icon("plus", 20)} Добавить шаблон</h2>{add}</div>"""
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


@router.get("/tickets.csv")
async def tickets_csv(request: Request, status: str = "", category: str = ""):
    """Выгрузка обращений в CSV: для отчётов и разборов вне панели."""
    await require_user(request)  # CSRF-токен не нужен: это скачивание
    rows = await repo.admin_tickets(None, 1000)
    if status:
        rows = [r for r in rows if as_str(r["status"]) == status]
    if category:
        rows = [r for r in rows if as_str(r["category"]) == category]
    # имена подставляем словарём: в строках обращений их нет, а десятки
    # отдельных запросов на каждую строку были бы лишней нагрузкой
    students = {as_str(u["user_id"]): as_str(u["full_name"]) for u in await repo.list_users(2000)}
    staff_names = {as_str(a["user_id"]): as_str(a["full_name"]) for a in await repo.all_admins()}
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["№", "Создано", "Статус", "Раздел", "Тема", "Студент", "ID студента",
                     "Сотрудник", "ID сотрудника", "Текст"])
    for row in rows:
        student_id = as_str(row["student_id"])
        admin_id = as_str(row["target_admin_id"])
        writer.writerow([
            row["ticket_id"], as_str(row["created_at"]),
            STATUS.get(as_str(row["status"]), as_str(row["status"])),
            STAFF_CATS.get(as_str(row["category"]), as_str(row["category"])),
            as_str(row["topic"]), students.get(student_id, student_id), student_id,
            staff_names.get(admin_id, admin_id), admin_id,
            as_str(row["text_content"])[:1000],
        ])
    return Response(
        "\ufeff" + buffer.getvalue(),   # BOM - чтобы Excel открыл в UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tickets.csv"'},
    )


@router.post("/staff/{user_id}/vacation")
async def staff_vacation(request: Request, user_id: str):
    """Отметка отпуска: обращения уходят заместителю, сотрудник их не видит."""
    actor = await require_form(request)
    data = await request.form()
    if not await repo.get_admin(user_id):
        flash("!Сотрудник не найден.")
        return redirect("/panel/staff")
    raw = as_str(data.get("until", "")).strip()
    if raw and raw != "none":
        saved = await repo.set_vacation(user_id, raw)
        if not saved:
            flash("!Дата не понята. Формат: ГГГГ-ММ-ДД.")
            return redirect(f"/panel/staff/{user_id}")
    else:
        saved = await repo.set_vacation(user_id, "")
    await repo.log_action(actor, "отпуск сотрудника",
                          f"{user_id}: {'до ' + saved if saved else 'вернулся'}")
    flash(f"{user_id}: {'отпуск до ' + saved if saved else 'сотрудник на месте'}.")
    return redirect(f"/panel/staff/{user_id}")


@router.get("/staff/{user_id}")
async def staff_card(request: Request, user_id: str):
    """Всё о сотруднике на одном экране: карточка, нагрузка, последние обращения."""
    user = await require_user(request)
    admin = await repo.get_admin(user_id)
    if not admin:
        return page("Сотрудник", '<div class="card msg-bad">Сотрудник не найден.</div>', user, "/staff")
    uid = as_str(admin["user_id"])
    load = (await repo.staff_activity(90)).get(uid, {})
    speed = await repo.response_speed(90)
    tickets = [row for row in await repo.admin_tickets(None, 500) if as_str(row["target_admin_id"]) == uid]
    recent = tickets[:12]
    last_reply = load.get("last_reply")
    cards = f"""
<div class="kpi">
  <div><b>{load.get('tickets', 0)}</b><span>обращений за 90 дней</span></div>
  <div class="warn"><b>{load.get('open', 0)}</b><span>открытых</span></div>
  <div class="good"><b>{esc(minutes_text(load.get('avg_minutes')))}</b><span>среднее время ответа</span></div>
  <div><b>{esc(fmt_when(last_reply) if last_reply else '—')}</b><span>последний ответ</span></div>
  <div><b>{len(tickets)}</b><span>всего обращений</span></div>
</div>"""
    recent_rows = "".join(
        f"<tr><td><a href='/panel/tickets/{esc(row['ticket_id'])}'>№{esc(row['ticket_id'])}</a></td>"
        f"<td>{esc(plain(STATUS.get(as_str(row['status']), as_str(row['status']))))}</td>"
        f"<td>{esc(plain(STAFF_CATS.get(as_str(row['category']), as_str(row['category']))))}</td>"
        f"<td>{esc(as_str(row['topic']) or '—')}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['created_at']))}</td></tr>"
        for row in recent
    ) or "<tr><td class='mut'>Обращений не было</td></tr>"
    super_row = is_sysadmin_role(as_str(admin["role_type"]))
    away = await repo.on_vacation(uid)
    replacement = None if not away else await repo.vacation_replacement(admin)
    until = as_str(admin["vacation_until"] if "vacation_until" in admin.keys() else "")
    vacation_block = (
        f"{icon('clock', 16)} в отпуске до {esc(until)}"
        + (f", обращения принимает {esc(as_str(replacement['full_name']))}" if replacement
           else ", заместитель не назначен")
        if away else "на месте")
    vacation_form = f"""
<form method="post" action="/panel/staff/{esc(uid)}/vacation">{csrf(request)}
<div class="grid" style="margin-top:10px">
<div><label>Отпуск до</label><input type="date" name="until" value="{esc(until)}"></div>
<div><button class="{'btn-grey' if away else 'btn'}">{'Вернулся' if away else 'Отметить отпуск'}</button></div>
</div>
<p class="small mut">Пока сотрудник в отпуске, новые обращения к нему уходят заместителю
с той же должностью, а студенту честно говорят, кто ответит.</p></form>"""
    body = f"""
{cards}
<div class="card"><h2>{esc(admin['full_name'])}</h2>
<table>
<tr><th>MAX ID</th><td>{code_cell(uid, "MAX ID скопирован")}</td></tr>
<tr><th>Роль в боте</th><td>{"сис-админ" if super_row else "сотрудник"}</td></tr>
<tr><th>Должность</th><td>{esc(as_str(admin['position']) or STAFF_ROLES.get(admin['role'], '—'))}</td></tr>
<tr><th>Отдел</th><td>{esc(as_str(admin['department']) or '—')}</td></tr>
<tr><th>Кабинет</th><td>{esc(as_str(admin['office']) or '—')}</td></tr>
<tr><th>Обращения</th><td>{esc(plain(STAFF_CATS.get(admin['ticket_category'], as_str(admin['ticket_category']))))}</td></tr>
<tr><th>Рассылка</th><td>{pill("разрешена", "on") if flag(admin["can_broadcast"]) else pill("запрещена", "off")}</td></tr>
<tr><th>Профиль MAX</th><td>{profile_cell((await repo.user_card(uid) or {}).get("username", ""))}</td></tr>
<tr><th>Отпуск</th><td>{vacation_block}</td></tr>
</table>
<div style="margin-top:12px">
<a class="btn" href="/panel/staff?q={esc(uid)}">{icon("staff", 16)} Все сотрудники</a>
<a class="btn-grey btn" href="/panel/analytics">К аналитике</a>
</div>{vacation_form}</div>
<div class="card"><h2>Последние обращения</h2>
<table><tr><th>№</th><th>Статус</th><th>Раздел</th><th>Тема</th><th>Создано</th></tr>{recent_rows}</table></div>
<p class="small mut">Скорость ответа по боту в целом: {esc(minutes_text(speed['avg_minutes']))} в среднем,
{esc(minutes_text(speed['worst_minutes']))} худший случай, отвечено {speed['share']}% за 90 дней.</p>"""
    return page(f"Сотрудник {as_str(admin['full_name'])}", body, user, "/staff")


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
# Бот пишет вложение так - по этому префиксу панель узнаёт файл в переписке.
ATTACH_PREFIX = "📎 Файл: "
EVENT_LABELS = {"created": "обращение создано", "status": "статус", "ready": "документ готов",
                "message_student": "сообщение студента", "message_staff": "ответ сотрудника"}


@router.get("/tickets")
async def tickets_list(request: Request, status: str = "", q: str = "", category: str = "",
                       scope: str = "", t: int = 0, view: str = ""):
    """Рабочее место: очередь слева, выбранное обращение справа.

    Отдельная страница карточки больше не нужна - рутина идёт без переходов,
    а старые ссылки из отчётов ведут сюда же.
    """
    user = await require_user(request)
    archived = view == "archive"
    rows = await repo.admin_tickets(None, 500, archived=archived)
    if status == "open":
        rows = [r for r in rows if as_str(r["status"]) in OPEN_STATUSES]
    elif status:
        rows = [r for r in rows if as_str(r["status"]) == status]
    if category:
        rows = [r for r in rows if as_str(r["category"]) == category]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in as_str(r["text_content"]).lower() or needle in as_str(r["student_id"])]
    latest = await repo.latest_message_roles([row["ticket_id"] for row in rows])
    if scope == "waiting":
        rows = [row for row in rows if latest.get(int(row["ticket_id"])) == "student"]
    waiting = sum(1 for row in rows if latest.get(int(row["ticket_id"])) == "student")
    selected = next((row for row in rows if int(row["ticket_id"]) == int(t or 0)), None)
    counts = await repo.status_counts()
    archived_n = await repo.archive_count()

    options = {"": "все статусы", "open": "🔓 открытые"}
    options |= {code: label for code, label in STATUS.items()}
    cat_options = {"": "все разделы", **{code: label for code, label in CATS.items()}}
    query = f"status={esc(status)}&category={esc(category)}&q={esc(q)}&scope={esc(scope)}"
    live_query = query + (f"&view={esc(view)}" if view else "")
    # фильтры запоминает actions_script(): пустая форма - восстанавливаем прошлые
    fresh = "" if (status or category or q or scope) else ' data-filters-saved="1"'
    filters = f"""
<form method="get" action="/panel/tickets" class="grid wb-filters" style="margin-bottom:10px"
 data-filters="status,category,q"{fresh}>
<div>{select("status", options, status, label="Статус")}</div>
<div>{select("category", cat_options, category, label="Раздел")}</div>
<div><label>Поиск: текст обращения или MAX ID</label>
<input name="q" value="{esc(q)}" placeholder="например: справка"></div>
<div><button>Найти</button></div></form>
<p class="small mut wb-tools">
<a class="btn{' btn-grey' if scope else ''}" href="/panel/tickets?{live_query}">{icon("clock", 16)} Только ждут ответа: {waiting}</a>
<a class="btn{' btn-grey' if not archived else ''}" href="/panel/tickets?{query}">{icon("inbox", 16)} В работе</a>
<a class="btn{' btn-grey' if not archived else ' btn-ok'}" href="/panel/tickets?{query}&view=archive">{icon("archive", 16)} Архив: {archived_n}</a>
<a class="btn" href="/panel/tickets/new">{icon("plus", 16)} Создать обращение</a>
<a class="btn btn-grey" href="/panel/tickets" data-filters-reset>{icon("close", 16)} Сбросить фильтры</a></p>"""
    summary = " · ".join(f"{plain(STATUS.get(c, c))}: {counts.get(c, 0)}" for c in STATUS)
    bot = await bot_open_link()
    access = await _tickets_access(request)
    queue = _tickets_queue(request, rows, latest, query, selected, archived)
    card = await _ticket_workbench(request, selected, bot) if selected else (
        '<div class="card mut">Выберите обращение в очереди слева — здесь появятся переписка, '
        'правки и быстрые ответы. Клавиши: j и k — по очереди, Enter — открыть, '
        'a — в архив, ? — все клавиши.</div>')
    body = f"""<div class="card"><p class="small mut">{esc(summary)}</p>{filters}</div>
<div class="workbench"><div class="wb-queue">{queue}</div><div class="wb-card">{card}{access}</div></div>"""
    title = "Архив обращений" if archived else "Обращения"
    return page(title, body, user, "/tickets",
                actions=_tickets_actions(status, category, selected, bot))


def print_btn(label: str = "Печать") -> str:
    """Кнопка печати: печатная версия описана в @media print темы."""
    return (f'<button type="button" class="btn btn-grey" onclick="window.print()">'
            f'{icon("print", 16)} {esc(label)}</button>')


def _tickets_actions(status: str, category: str, selected, bot: str) -> str:
    """Шапка рабочего места: выгрузка, печать карточки и ссылка на бота."""
    return (f'<a class="btn" href="/panel/tickets.csv?status={esc(status)}&category={esc(category)}">'
            f'{icon("download", 16)} Выгрузить в CSV</a>'
            f'<span class="small mut">все обращения со статусами, до 1000 строк</span>'
            + (print_btn("Печать карточки") if selected else "")
            + open_in_bot(bot))


async def _staff_choices(keep_current: str = "", current_name: str = "") -> dict:
    """Сотрудники панели для выбора: текущий исполнитель виден первым."""
    choices = {"": "— не назначен —"}
    if keep_current:
        choices[keep_current] = f"{current_name or keep_current} (сейчас)"
    for row in await repo.list_staff():
        person = dict(row)          # db.many отдаёт sqlite3.Row, а нужны ключи по имени
        uid = as_str(person.get("user_id"))
        if uid and uid != keep_current:
            choices[uid] = short(f"{person.get('full_name', uid)} · "
                                 f"{person.get('position') or person.get('role') or '—'}", 40)
    return choices


def _tickets_queue(request: Request, rows, latest: dict, query: str, selected, archived: bool) -> str:
    """Левая колонка: отметки для массовых действий и переход в карточку."""
    if not rows:
        return '<div class="card mut">Обращений нет.</div>'
    visible = rows[:TICKETS_PAGE]
    ids = ",".join(str(row["ticket_id"]) for row in visible)
    items = []
    for row in visible:
        ticket_id = row["ticket_id"]
        mark = "wb-on" if selected and int(selected["ticket_id"]) == int(ticket_id) else ""
        waiting = latest.get(int(ticket_id)) == "student"
        # db.many отдаёт sqlite3.Row, поэтому по именам колонок идём через dict()
        data = dict(row)
        who = " ".join(part for part in (as_str(data.get("student_name")),
                                         as_str(data.get("student_group"))) if part)
        status = esc(plain(STATUS.get(data["status"], data["status"])))
        if waiting:
            status += f' <span class="wb-wait">{icon("clock", 14)} ждёт ответа</span>'
        items.append(
            f'<label class="wb-item {mark}" data-hk><input type="checkbox" name="tids" value="{esc(ticket_id)}">'
            f'<a href="/panel/tickets?{query}&t={esc(ticket_id)}">'
            f'<span class="wb-head"><b>№{esc(ticket_id)}</b>'
            f'<span class="wb-status">{status}</span>'
            f'<span class="wb-date">{esc(fmt_when(data["updated_at"]))}</span></span>'
            f'<span class="wb-text" title="{esc(data["text_content"])}">'
            f'{esc(short(who, 40))} · {esc(short(data["text_content"], 90))}</span>'
            f'</a></label>')
    head = f'{icon("archive", 18)} Из архива' if archived else f'{icon("inbox", 18)} Очередь · {len(rows)}'
    more = (f'<p class="small mut">Показано {len(visible)} из {len(rows)} — сузьте фильтр '
            f'по статусу, чтобы увидеть нужное.</p>' if len(rows) > len(visible) else "")
    return f"""<div class="card"><h2>{head}</h2>
<form method="post" action="/panel/tickets/bulk">{csrf(request)}
<input type="hidden" name="return" value="{esc(query)}">
<div class="wb-list">{''.join(items)}</div>
<details class="wb-bulk"><summary>Групповые действия для отмеченных</summary>
<div class="grid" style="margin-top:8px">
<div>{select("action", {"assign": "Назначить сотрудника",
                        "status": "Сменить статус",
                        "pickup": "Кабинет выдачи",
                        "archive": "В архив"}, "assign")}</div>
<div><button class="btn-ok">Применить</button></div></div>
<div><label>Куда</label><input name="value"
  placeholder="сотрудник, статус или кабинет 115"></div>
<p class="small mut">Отметьте обращения галочкой слева. Поле «Куда» принимает свой текст:
например <code>115</code> для кабинета справок или <code>in_progress</code> для статуса.</p>
</details>
<input type="hidden" name="all" value="{esc(ids)}">
</form>{more}</div>"""


async def _ticket_workbench(request: Request, t, bot: str = "") -> str:
    """Правая колонка: карточка со всеми правками и быстрыми ответами.

    Формы правки и ответа помечены классами ``wb-edit`` и ``wb-reply`` - на
    печати их нет, поэтому на бумагу попадает переписка, а не пустые поля.
    """
    ticket_id = int(t["ticket_id"])
    student_name = as_str(t["student_name"]) or as_str(t["student_id"])
    student_group = as_str(t["student_group"]) or "—"
    messages = await repo.ticket_thread(ticket_id, 100)
    events = await repo.ticket_events(ticket_id, 50)
    staff_options = await _staff_choices(as_str(t["target_admin_id"]),
                                         as_str(t["staff_name"]))
    template_options = {"": "— шаблон —"}
    for row in await repo.list_templates():
        template = dict(row)
        key = as_str(template.get("template_id") or template.get("id"))
        if key:
            template_options[key] = as_str(template.get("title"))
    def _cell(m) -> str:
        body = esc(m["text"])
        if as_str(m["text"]).startswith(ATTACH_PREFIX):
            file_name = as_str(m["text"])[len(ATTACH_PREFIX):].split(" (")[0]
            body = (f'<a href="/panel/attachments/{esc(file_name)}">'
                    f'{icon("attach", 14)} {esc(file_name)}</a>'
                    f' <span class="small mut">открыть</span>')
        return f"<td>{body}</td></tr>"

    thread = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(m['created_at']))}</td>"
        f"<td class='small'>"
        f"{icon('students' if m['sender_role'] == 'student' else 'staff', 16)} "
        f"{esc(m['sender_name'])}{' · ' + esc(m['position']) if m['position'] else ''}"
        f"<div class='small mut'>{esc(m['group_code'] or m['sender_id'])}</div></td>"
        + _cell(m)
        for m in reversed(messages)
    ) or "<tr><td colspan='3' class='mut'>Сообщений нет</td></tr>"
    event_rows = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(e['created_at']))}</td>"
        f"<td class='small'>{esc(e['actor_name'] or e['actor_id'])}</td>"
        f"<td class='small'>{esc(EVENT_LABELS.get(e['event'], e['event']))}: {esc(e['detail'] or '—')}</td></tr>"
        for e in reversed(events)
    ) or "<tr><td colspan='3' class='mut'>Событий нет</td></tr>"
    pickup_options_html = await pickup_options()
    edit = f"""
<form method="post" action="/panel/tickets/{ticket_id}/edit" class="wb-edit">{csrf(request)}
<div class="grid">
<div class="full"><label>Текст обращения</label>
<textarea name="text_content" rows="2">{esc(t['text_content'])}</textarea></div>
<div>{select("target_admin_id", staff_options, "")}</div>
<div>{select("category", dict(CATS), as_str(t['category']))}</div>
<div><label>Тема</label><input name="topic" value="{esc(t['topic'])}"></div>
<div><label>Кабинет выдачи</label>
<input name="pickup_place" value="{esc(t['pickup_place'])}"
  list="pickup-rooms" placeholder="{esc(pickup_hint(t))}"></div>
<datalist id="pickup-rooms">{pickup_options_html}</datalist>
<div><label>Срок готовности</label><input name="ready_until" value="{esc(t['ready_until'])}"
  placeholder="например, 15:00 в пятницу"></div>
<div>{select("status", dict(STATUS), as_str(t['status']))}</div>
</div>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Сохранить</button></div>
<div>{_action_form(request, f'/panel/tickets/{ticket_id}/ready',
                   f'{icon("check", 16)} Готово')}</div>
<div>{_action_form(request, f'/panel/tickets/{ticket_id}/archive',
                   f'{icon("archive", 16)} В архив', cls="btn-grey")}</div>
</div>
<p class="small mut">Пустое поле не затирает старое значение. «Готово» пишет
студенту кабинет и закрывает обращение. Кабинет {CERT_PICKUP} зашит только
под справки; для остального нужно указать кабинет, иначе панель не закроет
обращение молча.</p></form>"""
    # шапка карточки - то, что должно попасть на бумагу
    head = f"""<div class="card"><h2>Обращение №{ticket_id}
<span class="pill">{esc(plain(STATUS.get(as_str(t['status']), as_str(t['status']))))}</span></h2>
<p class="small mut">{icon('students', 16)} {esc(student_name)}
(ID {code_cell(t['student_id'], "MAX ID студента скопирован")}),
группа {esc(student_group)} · создано {esc(fmt_when(t['created_at']))}</p>
<p class="small mut wb-tools">{open_in_bot(bot)}
{copy_btn(panel_link(f"/tickets?t={ticket_id}"), "Ссылка на обращение скопирована")}</p>
{edit}</div>"""
    return f"""{head}
<div class="card"><h2>Быстрый ответ</h2>
<form method="post" action="/panel/tickets/{ticket_id}/reply" class="wb-reply">{csrf(request)}
<textarea name="text" rows="3" required placeholder="Ответ студенту — уйдёт в MAX"></textarea>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Отправить</button></div>
<div>{select("template", template_options, "")}</div>
</div></form>
<p class="small mut">Ответ уходит студенту и остаётся в переписке бота.</p></div>
<div class="card"><h2>Переписка</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Текст</th></tr>{thread}</table></div>
<div class="card"><h2>История изменений</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Событие</th></tr>{event_rows}</table></div>"""


@router.post("/tickets/see-all")
async def tickets_see_all(request: Request):
    """Кто из сотрудников видит чужие обращения: очередь отдела или только свои."""
    actor = await require_form(request)
    data = await request.form()
    user_id = value(data, "user_id")
    flag = as_str(data.get("value", "")).strip() in ("1", "on", "true", "да")
    if not await repo.get_admin(user_id):
        flash("!Сотрудник не найден.")
        return redirect("/panel/tickets")
    await repo.set_staff_see_all(user_id, flag)
    await repo.log_action(actor, "доступ к чужим обращениям",
                          f"{user_id}: {'видит все' if flag else 'только свои'}")
    flash(f"{user_id}: {'видит все обращения' if flag else 'видит только свои'}.")
    return redirect("/panel/tickets")


async def _tickets_access(request: Request) -> str:
    """Кто видит чужие обращения: галочка рядом с именем сотрудника."""
    rows = [row for row in await repo.list_staff()]
    if not rows:
        return ""
    items = []
    for row in rows:
        person = dict(row)
        uid = as_str(person.get("user_id"))
        sees_all = await repo.staff_sees_all(uid)
        position = person.get("position") or person.get("role") or "—"
        items.append(
            f"""<form method="post" action="/panel/tickets/see-all" class="wb-access">
{csrf(request)}<input type="hidden" name="user_id" value="{esc(uid)}">
<label><input type="checkbox" name="value" value="1" {'checked' if sees_all else ''}
 onchange="this.form.submit()"> {esc(short(f"{person.get('full_name', uid)} · {position}", 40))}</label>
</form>""")
    return f"""<div class="card"><h2>Кто видит чужие обращения</h2>
<p class="small mut">Сотрудник без галочки видит только свои обращения. Системные права
в список не попадают - они и так видят всё.</p>{''.join(items)}
<style>.wb-access{{display:inline-block;margin:0 8px 4px 0}}
.wb-access label{{font-size:13px;display:flex;gap:6px;align-items:center;cursor:pointer}}</style>
</div>"""


@router.get("/attachments/{name}")
async def attachment_download(request: Request, name: str):
    """Отдаём файл обращения. Без входа в панель файл недоступен."""
    await require_user(request)
    import attachments

    safe = attachments.safe_name(name, "")
    if not safe:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = os.path.join(attachments.attachments_dir(), safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=safe)


@router.get("/tickets/new")
async def ticket_new(request: Request):
    """Создание обращения из панели: для заявок, пришедших не через бота."""
    user = await require_user(request)
    staff_options = await _staff_choices()
    body = f"""<div class="card"><h2>Новое обращение</h2>
<form method="post" action="/panel/tickets/new">{csrf(request)}
<div class="grid">
<div><label>MAX ID студента</label><input name="student_id" required></div>
<div><label>ФИО (если ещё не регистрировался)</label><input name="full_name"></div>
<div><label>Группа</label><input name="group_code" placeholder="24-23"></div>
<div>{select("category", dict(CATS), "feedback")}</div>
<div>{select("target_admin_id", staff_options, "")}</div>
<div class="full"><label>Текст обращения</label>
<textarea name="text_content" rows="4" required></textarea></div>
<div><label>Кабинет выдачи</label>
<input name="pickup_place" list="pickup-rooms-new" placeholder="для справок - {CERT_PICKUP}"></div>
<datalist id="pickup-rooms-new">{await pickup_options()}</datalist>
</div>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Создать</button></div>
<div><a class="btn btn-grey" href="/panel/tickets">Отмена</a></div></div>
</form></div>"""
    return page("Новое обращение", body, user, "/tickets")


@router.post("/tickets/new")
async def ticket_new_submit(request: Request):
    actor = await require_form(request)
    data = await request.form()
    student_id = value(data, "student_id").strip()
    text = value(data, "text_content").strip()
    if not student_id or not text:
        flash("!Нужен MAX ID студента и текст обращения.")
        return redirect("/panel/tickets/new")
    group_raw = value(data, "group_code").strip()
    if group_raw and not await repo.get_user(student_id):
        # created_at пишем явно: DEFAULT (datetime('now')) в схеме - это UTC,
        # а всё остальное время в проекте локальное
        await db.run(
            "INSERT INTO users(user_id, full_name, group_code, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, "
            "group_code=excluded.group_code",
            (student_id, value(data, "full_name").strip() or f"Студент {student_id}",
             group_code(group_raw), clock.stamp()),
        )
    ticket_id = await repo.create_ticket(student_id, value(data, "target_admin_id"),
                                         value(data, "category"), text, "")
    pickup = value(data, "pickup_place").strip()
    if pickup:
        await repo.update_ticket(ticket_id, actor, pickup_place=pickup)
    await repo.log_action(actor, "обращение создано из панели",
                          f"№{ticket_id}, студент {student_id}")
    flash(f"Обращение №{ticket_id} создано.")
    return redirect(f"/panel/tickets?t={ticket_id}")


@router.post("/tickets/bulk")
async def tickets_bulk(request: Request):
    actor = await require_form(request)
    data = await request.form()
    chosen = [item for item in value(data, "tids").split(",") if item]
    action = value(data, "action")
    if not chosen:
        flash("!Отметьте обращения галочкой слева.")
        return redirect(value(data, "return") or "/panel/tickets")
    done, message = await repo.bulk_update(chosen, action, value(data, "value"), actor)
    await repo.log_action(actor, f"массово: {action}", f"{done} обращений")
    flash(message)
    return redirect(value(data, "return") or "/panel/tickets")


@router.post("/tickets/{ticket_id}/edit")
async def ticket_edit(request: Request, ticket_id: int):
    actor = await require_form(request)
    data = await request.form()
    ok, message = await repo.update_ticket(
        ticket_id, actor,
        text_content=value(data, "text_content"),
        target_admin_id=value(data, "target_admin_id"),
        category=value(data, "category"),
        topic=value(data, "topic"),
        pickup_place=value(data, "pickup_place"),
        ready_until=value(data, "ready_until"),
        status=value(data, "status"),
    )
    flash(message if ok else f"!{message}")
    return redirect(f"/panel/tickets?t={ticket_id}")


@router.post("/tickets/{ticket_id}/ready")
async def ticket_ready(request: Request, ticket_id: int):
    """Кнопка «Готово»: сообщение студенту с кабинетом и закрытие.

    Кабинет 115 подставляется только под справки. Для остального кабинет
    должен быть указан - иначе документ готов, а сказать студенту, где его
    забрать, нечем.
    """
    actor = await require_form(request)
    t = await repo.get_ticket(ticket_id)
    if not t:
        flash("!Обращение не найдено.")
        return redirect("/panel/tickets")
    place = as_str(t["pickup_place"]).strip()
    if not place and is_certificate(t):
        place = CERT_PICKUP
    if not place:
        flash("!Укажите кабинет выдачи: 115 зашит только под справки.")
        return redirect(f"/panel/tickets?t={ticket_id}")
    await repo.update_ticket(ticket_id, actor, status="ready", pickup_place=place)
    await repo.add_ticket_message(ticket_id, actor, "staff",
                                  f"✅ Документ готов. Заберите в кабинете {place}.")
    await notify(t["student_id"],
                 f"✅ Документ по обращению №{ticket_id} готов. Заберите в кабинете {place}.")
    await repo.log_action(actor, "справка готова", f"№{ticket_id}, кабинет {place}")
    flash(f"Обращение №{ticket_id}: документ готов, кабинет {place}. Студенту отправлено уведомление.")
    return redirect(f"/panel/tickets?t={ticket_id}")


@router.post("/tickets/{ticket_id}/archive")
async def ticket_archive(request: Request, ticket_id: int):
    actor = await require_form(request)
    ok, message = await repo.archive_ticket(ticket_id, actor)
    if ok:
        t = await repo.get_ticket(ticket_id, include_archived=True)
        if t:
            await notify(t["student_id"],
                         f"🗄 Обращение №{ticket_id} убрано в архив. Вопрос не решён — "
                         "напишите новое обращение.")
        await repo.log_action(actor, "обращение в архиве", f"№{ticket_id}")
    flash(message if ok else f"!{message}")
    return redirect("/panel/tickets")


@router.post("/tickets/{ticket_id}/restore")
async def ticket_restore(request: Request, ticket_id: int):
    actor = await require_form(request)
    ok, message = await repo.restore_ticket(ticket_id, actor)
    if ok:
        await repo.log_action(actor, "обращение восстановлено", f"№{ticket_id}")
    flash(message if ok else f"!{message}")
    return redirect("/panel/tickets?view=archive")


@router.get("/tickets/{ticket_id}")
async def ticket_card(request: Request, ticket_id: int):
    """Старая ссылка на карточку ведёт в рабочее место - отдельной страницы нет."""
    await require_user(request)
    return redirect(f"/panel/tickets?t={ticket_id}")


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
# ── колледж: контакты и частые вопросы ───────────────────────────────────────
@router.get("/college")
async def college_page(request: Request):
    """Справочник колледжа: что бот рассказывает студенту о себе и о колледже."""
    user = await require_user(request)
    # обычный цикл, а не генератор: await внутри genexp даёт асинхронный генератор
    rows, faq_rows = [], []
    for key, val in (await college.contacts()).items():
        mark = "изменено" if await college.is_overridden(key) else "с сайта"
        rows.append(f"<tr><th class='col-key'>{esc(key.replace('_', ' '))}</th>"
                    f"<td><input name='{esc(college.setting_key(key))}' value='{esc(val)}'></td>"
                    f"<td class='small mut'>{mark}</td></tr>")
    enabled = await faq.ask_enabled()
    for row in await faq.active_items():
        toggle = _action_form(request, "/panel/faq/" + str(to_int(row["id"])) + "/toggle",
                              f'{icon("refresh", 16)} Вкл/выкл',
                              confirm_text="Включить или выключить этот вопрос?")
        faq_rows.append(
            f"<tr><td><b>{esc(row['question'])}</b>"
            f"<div class='small mut'>{esc(row['keywords'])}</div></td>"
            f"<td>{esc(row['answer'])}</td>"
            f"<td>{state_pill("on", "включён") if row["active"] else state_pill("off", "выключен")}</td>"
            f"<td>{toggle}</td></tr>")
    rows_html = "".join(rows)
    faq_html = "".join(faq_rows) or (
        "<tr><td colspan='4' class='mut'>Вопросов пока нет — нажмите «Залить вопросы с сайта».</td></tr>")
    body = f"""<div class="card"><h2>Контакты колледжа</h2>
<p class="small mut">Значения взяты с официального сайта {esc(college.SITE)}. Пустое поле
возвращает к данным сайта. То, что изменено, отмечено в третьей колонке.</p>
<form method="post" action="/panel/college">{csrf(request)}<table>{rows_html}</table>
<div class="grid" style="margin-top:10px"><button class="btn-ok">Сохранить справочник</button></div>
</form></div>
<div class="card"><h2>Частые вопросы</h2>
<p class="small mut">Бот ищет ответ по ключевым словам. Если не нашёл — не выдумывает,
а предлагает написать сотруднику.</p>
<form method="post" action="/panel/faq/toggle">{csrf(request)}
<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">
<button class="{"btn-bad" if enabled else "btn-ok"}">{icon("close" if enabled else "check", 16)} {"Выключить" if enabled else "Включить"}</button>
<span class="small mut">сейчас: {"включены" if enabled else "выключены"}</span></form>
<form method="post" action="/panel/faq/seed">{csrf(request)}<button class="btn-grey" style="margin-top:8px">
{icon("download", 16)} Залить вопросы с сайта</button></form>
<table style="margin-top:12px"><tr><th>Вопрос и ключевые слова</th><th>Ответ</th><th>Вкл.</th><th></th></tr>
{faq_html}</table>
<p class="small mut">Черновик — {len(college.DEFAULT_FAQ)} вопросов с сайта колледжа.
Кнопка «Залить» добавляет только новые и не трогает правки сис-админа.</p></div>"""
    return page("Колледж", body, user, "/college")


@router.post("/college")
async def college_save(request: Request):
    actor = await require_form(request)
    data = await request.form()
    saved = 0
    for key in college.FIELDS:
        name = college.setting_key(key)
        if hasattr(data, "getlist") and name in data:
            await college.override(key, as_str(data.get(name, "")).strip()[:college.MAX_VALUE])
            saved += 1
    await repo.log_action(actor, "справочник колледжа", f"полей сохранено: {saved}")
    flash(f"Справочник сохранён: {saved} полей.")
    return redirect("/panel/college")


@router.post("/faq/toggle")
async def faq_toggle(request: Request):
    await require_form(request)
    data = await request.form()
    await faq.set_ask_enabled(as_str(data.get("enabled", "")) == "1")
    flash("Ответы на частые вопросы " + ("выключены." if as_str(data.get("enabled", "")) == "1" else "включены."))
    return redirect("/panel/college")


@router.post("/faq/seed")
async def faq_seed(request: Request):
    actor = await require_form(request)
    added = await faq.seed_defaults()
    await repo.log_action(actor, "частые вопросы с сайта", f"добавлено: {added}")
    flash(f"Добавлено вопросов: {added}. Правки сис-админа не тронуты.")
    return redirect("/panel/college")


@router.post("/faq/{faq_id}/toggle")
async def faq_item_toggle(request: Request, faq_id: int):
    """Включить или выключить отдельный вопрос, не удаляя его."""
    actor = await require_form(request)
    row = await db.one("SELECT active FROM faq WHERE id=?", (int(faq_id),))
    if not row:
        flash("!Такого вопроса нет.")
        return redirect("/panel/college")
    await db.run("UPDATE faq SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (int(faq_id),))
    await repo.log_action(actor, "частый вопрос", f"№{faq_id}")
    flash(f"Вопрос №{faq_id} {'выключен' if to_int(row['active']) else 'включен'}.")
    return redirect("/panel/college")


@router.get("/students")
async def students(request: Request, group: str = "", consent: str = ""):
    user = await require_user(request)
    only_no_consent = consent == "0"
    if only_no_consent:
        rows = [dict(row) for row in await repo.users_without_consent(200)]
        if group:
            rows = [row for row in rows if norm_group(row["group_code"]) == norm_group(group)]
    else:
        # sqlite3.Row не умеет .get - приводим строки к словарям
        rows = [dict(row) for row in await repo.list_users(200, group)]
    groups = await repo.top_groups(300)
    options = {"": "все группы"} | {row["group_code"]: row["group_code"] for row in groups}
    head = ('<form method="get" action="/panel/students" class="grid" style="margin-bottom:14px">'
            f'<div>{select("group", options, norm_group(group), label="Группа")}</div>'
            f'<div>{select("consent", {"": "все", "0": "только без согласия"}, consent, label="Согласие")}</div>'
            f"<div><button>{icon('search', 16)} Показать</button></div></form>")
    # ✅ в колонке согласия остаётся: так эту отметку ждут в отчётах и в тестах
    body = "".join(
        f"<tr data-hk><td>{esc(row['full_name']) or '<span class=\'mut\'>без ФИО</span>'}</td>"
        f"<td>{code_cell(row['user_id'], 'MAX ID скопирован')}</td>"
        f"<td>{esc(row['group_code']) or '<span class=\'mut\'>—</span>'}</td>"
        f"<td>{esc(row.get('tickets', 0))}</td>"
        f"<td>{pill('✅ согласие есть', 'on') if row.get('consent_at') else pill('нет', 'off')}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td>"
        f"<td><a class='btn-grey' href='/panel/people/{esc(row['user_id'])}'>Открыть</a> "
        f"{_action_form(request, f'/panel/people/{esc(row['user_id'])}/delete', icon('delete', 16), confirm_text=f'Удалить {row['full_name']} ({row['user_id']})? Обращения останутся.')}</td></tr>"
        for row in rows
    ) or "<tr><td class='mut'>Студентов не найдено</td></tr>"
    table = (f"<table><tr><th>ФИО</th><th>MAX ID</th><th>Группа</th><th>Обращений</th>"
             f"<th>Согласие</th><th>В базе с</th><th></th></tr>{body}</table>")
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
        away = await repo.on_vacation(uid)
        vacation_mark = ""
        if away:
            until = as_str(row["vacation_until"])
            replacement = await repo.vacation_replacement(row)
            vacation_mark = (f" <span class='mut small'>{icon('clock', 14)} в отпуске до {esc(until)}"
                             + (f", ведёт {esc(as_str(replacement['full_name']))}" if replacement
                                else ", заместитель не назначен")
                             + "</span>")
        head_row = f"""
<tr data-hk><td><b>{esc(row['full_name'])}</b>{vacation_mark}<div class="small mut">ID {esc(uid)}</div></td>
<td>{"сис-админ" if super_row else "сотрудник"}</td>
<td>{esc(as_str(row['position']) or STAFF_ROLES.get(row['role'], '—'))}<div class="small mut">{esc(row['role'])}</div></td>
<td>{esc(as_str(row['department']) or '—')}</td>
<td>{esc(as_str(row['office']) or '—')}</td>
<td>{esc(plain(STAFF_CATS.get(row['ticket_category'], row['ticket_category'])))}</td>
<td class="small">{tickets_90} / {load.get('open', 0)}<div class="small mut">{esc(fmt_when(load['last_reply'])) if load.get('last_reply') else 'не отвечал'}</div></td>
<td>{pill("разрешена", "on") if flag(row["can_broadcast"]) else pill("нет", "off")}</td></tr>"""
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
        body += (f"{head_row}<tr><td colspan='8' style='background:var(--surface-sunken);padding:10px'>"
                 f"{form(request, f'/panel/staff/{esc(uid)}', fields)}"
                 f"<form method='post' action='/panel/staff/promote/{esc(uid)}' class='inline'>{csrf(request)}"
                 f"<button class='btn-grey'>{icon('access', 16)} Сделать сис-админом</button></form> "
                 f"<form method='post' action='/panel/staff/{esc(uid)}/delete' class='inline' "
                 f"onclick=\"return confirm('Удалить сотрудника {esc(row['full_name'])}?')\">"
                 f"{csrf(request)}<button class='btn-bad'>{icon('delete', 16)} Удалить</button></form></td></tr>")
    search = ('<form method="get" action="/panel/staff" class="grid" style="margin-bottom:14px">'
              f'<div><input name="q" value="{esc(q)}" placeholder="Поиск: ФИО, ID, должность, отдел, кабинет"></div>'
              f"<div><button>{icon('search', 16)} Найти</button></div></form>")
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
<div class="card"><h2>{icon("access", 20)} Сис-админы</h2>
<table><tr><th>Кто</th><th>Источник</th><th>Профиль MAX</th><th>Был в боте</th><th></th></tr>{sys_rows}</table>
{('<table><tr><th>Отозванные</th><th>Почему</th><th></th></tr>' + revoked_rows + '</table>') if revoked_rows else ''}
<div class="grid" style="margin-top:12px">{add_sys}{revoke_sys}
<p class="small mut">Список живёт в базе. <code>SYSADMIN_IDS</code> в .env заводит сис-админов при старте,
но права, снятые здесь, перезапуск не вернёт — иначе нельзя было бы отозвать доступа.
<code>ROOT_IDS</code> — владелец бота: максимальные права на корневом уровне, снять их нельзя.
Можно выдать и снять права сразу у нескольких человек — впишите ID через запятую.</p></div></div>"""
    body_all = f"""
{sysadmins_card}
<div class="card"><h2>{icon("staff", 20)} Сотрудники</h2>{table}</div>
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
        f"<tr data-hk><td><a href='/panel/people/{esc(row['user_id'])}'><b>{esc(row['fio'] or row['staff_name'] or row['display_name'])}</b></a>"
        f"<div class='small mut'>{esc(repo.KIND_TITLES.get(repo.contact_kind(row), '—'))}</div></td>"
        f"<td>{esc(row['group_code'] or row['position'] or '—')}</td>"
        f"<td>{code_cell(row['user_id'], 'MAX ID скопирован')}</td>"
        f"<td>{profile_cell(row['username'])}</td>"
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
<div>{select("kind", dict(repo.CONTACT_KIND_LABELS), kind, label="Кто это")}</div>
<div><label>Поиск: ФИО, ID, @ник, группа, должность</label><input name="q" value="{esc(q)}"></div>
<div><button>{icon("search", 16)} Найти</button></div>
</form>"""
    body = f'<div class="card"><p class="small mut">{esc(stats)} · показано {len(rows)} из {total}</p>{filters}{_people_table(rows)}</div>'
    return page("Люди", body, user, "/people",
                actions=f'<a class="btn" href="/panel/people.csv?kind={esc(kind)}&q={esc(q)}">'
                        f'{icon("download", 16)} Выгрузить в CSV</a>'
                        f'<span class="small mut">реестр с никами и ссылками на профиль MAX, до 5000 строк</span>')


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
<div class="card"><h2>{icon("access", 20)} Заявка на роль сотрудника</h2>
<table><tr><th>Статус</th><td>{request_status_cell(card['request']['status'])}</td></tr>
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
                   f'<button class="btn-grey">{icon("access", 16)} Сделать сис-админом</button></form>')
    tickets_count = len(card["tickets_list"])
    delete_note = ("Сис-админа удаляют во вкладке «Сотрудники»."
                   if is_sysadmin_role(as_str(card["role_type"])) else
                   f"Удаляются регистрация, карточка сотрудника, контакт и состояние. "
                   f"Обращений: {tickets_count}"
                   + (" (есть открытые — удалять можно только вместе с ними)." if tickets_count else "."))
    remove = ""
    if not is_sysadmin_role(as_str(card["role_type"])):
        remove = f"""
<div class="card"><h2>{icon("warning", 20)} Удаление</h2>
<p class="small mut">{esc(delete_note)}</p>
{_action_form(request, f'/panel/people/{user_id}/delete',
              f'{icon("delete", 16)} Удалить пользователя',
              confirm_text=f"Удалить {name} ({user_id})? Действие необратимо.")}
{_action_form(request, f'/panel/people/{user_id}/delete',
              f'{icon("delete", 16)} Удалить вместе с обращениями',
              '<input type="hidden" name="with_tickets" value="1">',
              f"Удалить {name} вместе с {tickets_count} обращениями и перепиской? Действие необратимо.",
              "btn-bad")}
</div>"""
    body = f"""
<div class="card"><h2>{esc(name)}</h2>
<table>
<tr><th>Роль в боте</th><td>{esc(repo.KIND_TITLES.get(card['kind'], card['kind']))}</td></tr>
<tr><th>MAX ID</th><td>{code_cell(user_id, "MAX ID скопирован")}</td></tr>
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
# Состояния показываем цветной точкой и словом, а не эмодзи: в тёмной теме
# точка читается ровнее, а «зелёный кружок» в разных браузерах выглядит по-разному.
INVITE_STATE = {"active": ("on", "активен"), "used": ("off", "использован"),
                "expired": ("bad", "истёк"), "unknown": ("off", "не найден")}
REQUEST_STATUS = {"new": ("", "новая"), "approved": ("on", "одобрена"),
                  "rejected": ("", "отклонена")}


def invite_state_cell(state) -> str:
    """Состояние кода приглашения точкой и словом."""
    dot, label = INVITE_STATE.get(as_str(state), ("off", as_str(state) or "—"))
    return state_pill(dot, label)


def request_status_cell(status) -> str:
    """Состояние заявки на роль сотрудника."""
    kind, label = REQUEST_STATUS.get(as_str(status), ("", as_str(status) or "—"))
    return pill(label, kind)


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
                      f'<button class="btn-grey">{icon("close", 16)} Отозвать</button></form>')
            invite = (f'<div class="small mut">{esc(join_link(row["code"]))}</div>')
        else:
            invite = ""
        code_rows.append(
            f"<tr data-hk><td>{code_cell(row['code'], 'Код скопирован')}{invite}</td><td>{esc(who)}</td>"
            f"<td>{invite_state_cell(state)}</td>"
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
        f"<td>{request_status_cell(row['status'])}</td>"
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
<div class="card"><h2>{icon("access", 20)} Коды сотрудников</h2>
<form method="post" action="/panel/access/code" class="grid" style="margin-bottom:14px">
{csrf(request)}
<div><label>MAX ID (пусто — код для любого, у кого есть код)</label><input name="user_id" value=""></div>
<div><label>ФИО для списка</label><input name="full_name" value=""></div>
<div>{select("ttl_hours", {hours: label for hours, label in CODE_TTL_CHOICES}, config.STAFF_CODE_TTL)}</div>
<div><button class="btn-ok">{icon("plus", 16)} Выдать код</button></div></form>
<p class="small mut">Одноразовый. Срок выбирается рядом; в боте то же самое кнопками под кодом.
В MAX сотрудник выбирает «👔 Я сотрудник» и вводит код.</p>
{code_table}</div>
<div class="card"><h2>{icon("inbox", 20)} Заявки на роль сотрудника</h2>
<table><tr><th>Человек</th><th>Должность</th><th>Кабинет</th><th>Комментарий</th><th>Подана</th><th></th></tr>{request_rows}</table>
{('<table><tr><th>Человек</th><th>Статус</th><th>Обновлена</th></tr>' + closed_rows + '</table>') if closed_rows else ''}</div>
<div class="card"><h2>{icon("warning", 20)} Попытки ввода кода за сутки</h2>
<table><tr><th>Человек</th><th>Попыток</th><th>Последняя</th></tr>{attempt_rows}</table>
<p class="small mut">Больше {esc(config.STAFF_CODE_ATTEMPTS)} попыток в час — ввод блокируется до истечения часа.</p></div>"""
    return page("Коды и заявки", body, user, "/access")


@open_router.get("/join/{code}")
async def join_page(request: Request, code: str):
    """Страница приглашения по ссылке: код виден, вход в бота - одним нажатием.

    Открыта без входа в панель: ссылку получают будущие сотрудники, у которых
    ещё нет прав. Код одноразовый и со сроком, поэтому страница ничего не даёт
    постороннему, кроме самого кода.
    """
    normalized = norm_code(code)
    state = await repo.invite_state(normalized)
    if state == "unknown":
        return await _join_page_html(
            "Код не найден",
            "Такого кода нет. Возможно, в ссылке опечатка или код уже удалён. "
            "Попросите сис-админа выдать новый.",
            "", "")
    expires = await db.one("SELECT expires_at, full_name, user_id FROM staff_invites WHERE code=?",
                           (normalized,))
    until = as_str(expires["expires_at"]) if expires else ""
    hint = ""
    if state == "used":
        hint = "Этот код уже использовали. Попросите сис-админа выдать новый."
    elif state == "expired":
        hint = "Срок действия кода истёк. Попросите сис-админа выдать новый."
    deadline = f"Код действует до {fmt_when(until)}." if until and state not in ("used", "expired") else ""
    who = as_str(expires["user_id"]) if expires else ""
    body = f"""<div class="code">{esc(normalized)}</div>
<p>Откройте бот в MAX и отправьте команду <code>/join {esc(normalized)}</code> —
код сработает один раз и только для вас.</p>
<p class="small mut">{esc(deadline)} {esc(hint)}</p>"""
    if who:
        body += f'<p class="small mut">Код выдан для MAX ID {esc(who)}.</p>'
    return await _join_page_html("Приглашение в бота колледжа", "", normalized, body)


async def _join_page_html(title: str, message: str, code: str, body: str) -> HTMLResponse:
    """Одна колонка без панели: страницу видно и с телефона, и с чужого компьютера."""
    target = await bot_profile_link()
    join_button = (f'<a class="btn" href="{esc(target)}">Открыть бота в MAX</a>' if target else
                   '<p class="small mut">Откройте бота в MAX и отправьте команду /join с кодом.</p>')
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{JOIN_STYLE}</style></head><body>
<main><h1>{esc(title)}</h1>
{f'<p class="lead">{esc(message)}</p>' if message else ''}
{body}
{join_button}
<p class="small mut">Бот колледжа: Лангепасский политехнический колледж, ул. Ленина, 52,
приёмная директора +7 (34669) 2-26-50, учебная часть +7 (34669) 2-71-33.</p>
</main></body></html>""", status_code=200)


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
            f"<div><button>{icon('search', 16)} Найти</button></div></form>")
    body = "".join(
        f"<tr data-hk><td><b>{esc(row['fio'] or row['display_name'] or 'без имени')}</b>"
        f"<div class='small mut'>{esc(repo.KIND_TITLES.get(repo.contact_kind(row), '-'))}</div></td>"
        f"<td>{code_cell(row['user_id'], 'MAX ID скопирован')}</td>"
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
<div class="card"><h2>{icon("user-off", 20)} Без прав сотрудника: {total}</h2>{head}{table}
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
    """Кнопка-действие: одна форма на действие, с подтверждением в браузере.

    В ``label`` можно передать готовую иконку ``icon(...)`` - такие подписи
    вставляются как есть, всё остальное экранируется.
    """
    script = f" onclick=\"return confirm('{confirm_text}')\"" if confirm_text else ""
    body = label if label.lstrip().startswith("<svg") else esc(label)
    return (f'<form method="post" action="{esc(action)}" class="inline"{script}>{csrf(request)}'
            f'{fields}<button class="{esc(cls)}">{body}</button></form>')


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


# ── группы ────────────────────────────────────────────────────────────────────
@router.get("/groups")
async def groups_list(request: Request):
    user = await require_user(request)
    rows = await repo.groups(active_only=False, limit=300)
    body = "".join(
        f"""<tr data-hk><td>{code_cell(row['group_code'], 'Код группы скопирован')}</td>
        <td>{esc(row['title'])}</td>
<td>{"<span class='pill pill-on'>активна</span>" if flag(row['active']) else "<span class='pill pill-off'>скрыта</span>"}</td>
<td class="small mut">{esc(row['created_at'])}</td>
<td class="small">
<form method="post" action="/panel/groups/rename" class="inline">{csrf(request)}
<input type="hidden" name="old" value="{esc(row['group_code'])}">
<input name="code" value="{esc(row['group_code'])}" style="width:110px;display:inline-block">
<button class="btn-grey">{icon("edit", 16)} Переименовать</button></form>
<form method="post" action="/panel/groups/toggle" class="inline">{csrf(request)}
<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-grey">{icon("eye-off" if flag(row['active']) else "eye", 16)}{"Скрыть" if flag(row['active']) else "Показать"}</button></form>
<form method="post" action="/panel/groups/delete" class="inline" onclick="return confirm('Удалить группу?')">
{csrf(request)}<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-bad">{icon("delete", 16)} Удалить</button></form></td></tr>"""
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
<div class="card"><h2>{icon("groups", 20)} Справочник групп</h2>{table}</div>
<div class="card" style="max-width:560px"><h2>{icon("plus", 20)} Добавить группу</h2>{add}
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
            f"<td>{_action_form(request, '/panel/schedules/parse', f'{icon("search", 16)} Разобрать', f'<input type=\"hidden\" name=\"group_code\" value=\"{esc(code)}\">')}"
            f" {_action_form(request, '/panel/schedules/delete', icon("delete", 16), f'<input type=\"hidden\" name=\"group_code\" value=\"{esc(code)}\">', f'Удалить расписание группы {code}?', 'btn-bad')}</td></tr>"
        )
    table = (f"<table><tr><th>Группа</th><th>Ссылка на PDF</th><th>Разбор</th><th>Группы в PDF</th><th></th></tr>"
             f"{body or '<tr><td colspan=5 class=mut>Расписаний пока нет</td></tr>'}</table>")
    save = form(
        request, "/panel/schedules/save",
        input("group_code", "") + input("pdf_url", "", full=True),
        "Сохранить (сразу разберём)", "btn-ok",
    )
    bells = await _lesson_times_form(request)
    import_box = form(
        request, "/panel/schedules/import",
        ('<div class="full"><label>Адрес страницы с расписаниями или список ссылок на PDF '
         '(по одной в строке)</label>'
         f'<textarea name="source">{esc(COLLEGE_SCHEDULE_PAGE)}</textarea></div>'),
        f'{icon("download", 16)} Импортировать с сайта', "btn-ok",
    )
    body_all = f"""
<div class="card"><h2>{icon("schedules", 20)} Расписания</h2>{table}
<p class="small mut">Разобранных пар: {parsed_total}. Скачанные PDF лежат рядом с базой в папке
<code>schedules</code> и перечитываются, когда файл по ссылке меняется.</p>
{_action_form(request, '/panel/schedules/parse_all', f'{icon("refresh", 16)} Обновить все расписания')}</div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Сохранить расписание</h2>{save}
  <p class="small mut">Ссылку достаёт преподаватель или бот. Сначала проверяем, что по ней отдаётся PDF,
  затем разбираем файл в занятия — студенты увидят расписание текстом, а не ссылкой.</p></div>
  <div class="card" style="flex:1"><h2>{icon("clock", 20)} Звонки (время пар)</h2>{bells}</div>
</div>"""
    body_all = f"""<div class="card"><h2>{icon("download", 20)} Импорт с сайта колледжа</h2>{import_box}
<p class="small mut">Бот скачает указанные PDF, найдёт в них группы и заведёт их расписание
вместе со справочником кодов. Дальше файлы обновляются на стороне колледжа: бот сам
перечитывает PDF, когда файл меняется, - руками ничего обновлять не нужно.</p></div>
{body_all}"""
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
            f"<button class='btn-grey' title='Сохранить время' aria-label='Сохранить время'>{icon("check", 16)}</button></form></td></tr>"
            for lesson in by_day[weekday]
        )
        days += (f"<h3>{esc(WEEKDAYS_FULL[weekday].capitalize())}</h3>"
                 f"<table><tr><th>Пара</th><th>Предмет</th><th>Преподаватель</th><th>Ауд.</th><th>Время</th></tr>"
                 f"{lessons}</table>")
    return page(f"Расписание {code}", f'<div class="card">{days}'
                                       f'<p><a class="btn-grey" href="/panel/schedules">{icon("chevron-left", 16)} К списку</a></p></div>',
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


@router.post("/schedules/import")
async def schedules_import(request: Request):
    """Импортирует расписания с сайта колледжа одной кнопкой.

    На входе - адрес страницы со ссылками на PDF или список ссылок. Группы
    заводятся автоматически, вместе со справочником вариантов написания кода.
    """
    actor = await require_form(request)
    data = await request.form()
    source = value(data, "source").strip() or COLLEGE_SCHEDULE_PAGE
    try:
        result = await schedule_import.import_sources(source)
    except Exception as exc:  # noqa: BLE001 - причину покажем сис-админу
        log.warning("импорт расписаний не получился: %s", exc)
        flash(f"!Не удалось импортировать: {exc}")
        return redirect("/panel/schedules")
    if not result["urls"]:
        flash("!Не нашлось ни одной ссылки на PDF. Проверьте адрес.")
        return redirect("/panel/schedules")
    await repo.log_action(actor, "импорт расписаний", f"файлов {result['files']}, групп {result['total']}")
    log.info("панель: импорт расписаний - файлов %s, групп %s (сис-админ %s)",
             result["files"], result["total"], actor)
    parts = [f"Импортировано файлов: {result['files']}, групп: {result['total']}, занятий: {result['lessons']}"]
    if result["problems"]:
        parts.append("Проблемы: " + "; ".join(result["problems"][:5]))
    flash(". ".join(parts))
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
<div class="card"><h2>{icon("send", 20)} Новая рассылка</h2>
<form method="post" action="/panel/broadcasts/send">{csrf(request)}<div class="grid">
<div>{select('audience', audience_options, 'all', label="Кому")}</div>
<div><label>MAX ID для проверки (необязательно)</label><input name="test_to" placeholder="проверить на себе"></div>
<div class="full"><label>Текст объявления</label><textarea name="text" maxlength="3500"
  placeholder="Уважаемые студенты…"></textarea></div></div>
<p class="small mut">Объявление придёт с подписью «— ФИО, должность». Отправка идёт в фоне,
итог придёт вам в MAX и появится в истории ниже.</p>
<div class="grid" style="margin-top:10px"><button>{icon("send", 16)} Отправить рассылку</button></div></form></div>
<div class="card"><h2>{icon("logs", 20)} История рассылок</h2>{_broadcasts_table(rows)}</div>"""
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
    consent_text = await db.get_setting("consent_text", "")
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
<div class="card"><h2>{icon("tickets", 20)} Приём обращений</h2>
<form method="post" action="/panel/settings/tickets">{csrf(request)}
<input type="hidden" name="enabled" value="{"0" if tickets_enabled else "1"}">
<button class="{"btn-bad" if tickets_enabled else "btn-ok"}">{icon("close" if tickets_enabled else "check", 16)} {"Выключить" if tickets_enabled else "Включить"}</button>
<span class="small mut">сейчас: {"включён" if tickets_enabled else "выключен"}</span></form></div>
<div class="card"><h2>{icon("check", 20)} Согласие на обработку данных</h2>
<p class="small mut">Этот текст студент видит при регистрации. Пустое поле - бот покажет
свою заготовку. Согласие хранится с датой и редакцией текста.</p>
<form method="post" action="/panel/settings/consent">{csrf(request)}
<textarea name="value" maxlength="1000" style="min-height:110px">{esc(consent_text)}</textarea>
<div class="grid" style="margin-top:10px"><button>Сохранить</button></div></form></div>
<div class="card"><h2>{icon("info", 20)} Приветствие студентов</h2>
<form method="post" action="/panel/settings/welcome">{csrf(request)}
<textarea name="value" maxlength="500">{esc(welcome)}</textarea>
<div class="grid" style="margin-top:10px"><button>Сохранить</button></div></form></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>{icon("logs", 20)} Действия сис-админов</h2>
  <table><tr><th>Когда</th><th>Кто</th><th>Что сделал</th><th>Подробности</th></tr>{action_rows}</table>
  <p class="small mut">Кто и когда менял должности, выдавал коды, удалял людей и трогал базу.
  Записи ведутся при каждом действии и не удаляются вместе с данными.</p></div>
  <div class="card" style="flex:1"><h2>{icon("analytics", 20)} За 30 дней</h2>
  <table><tr><th>Действие</th><th>Раз</th></tr>{count_rows}</table></div>
</div>
<div class="card"><h2>Прочие настройки (ключ → значение)</h2>
<form method="post" action="/panel/settings/raw">{csrf(request)}<table>"""
    for row in rows:
        body += (f"<tr><td class='col-key'><input name='key' value='{esc(row['key'])}'></td>"
                 f"<td><input name='value' value='{esc(row['value'])}'></td></tr>")
    body += f"""</table>
<div class="grid" style="margin-top:10px"><button>Сохранить</button>
<a class="btn btn-grey" href="/panel/settings">Обновить список</a></div></form></div>
<div class="card"><h2>{icon("settings", 20)} Служебное</h2><table>
<tr><th>Режим</th><td>{"webhook" if config.WEBHOOK_URL else "long polling"}</td></tr>
<tr><th>Адрес API</th><td>{esc(config.MAX_API_URL)}</td></tr>
<tr><th>Файл журнала</th><td>{esc(config.LOG_FILE)}</td></tr>
<tr><th>Сис-админов в базе</th><td>{len(sysadmins)} ({esc(", ".join(item["full_name"] for item in sysadmins) or "—")})</td></tr>
<tr><th>Системные администраторы (SYSADMIN_IDS в .env)</th><td>{esc(", ".join(str(i) for i in config.SYSADMIN_IDS) or "—")}</td></tr>
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


@router.post("/settings/consent")
async def settings_consent(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("consent_text", as_str(data.get("value", "")).strip()[:1000])
    flash("Текст согласия сохранён.")
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

