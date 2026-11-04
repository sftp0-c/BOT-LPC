"""Общее для всех разделов панели: доступ, формы, разметка и оболочка страницы.

Здесь то, чем пользуется любая страница: проверка входа и CSRF-токен, хелперы
разметки (esc, pill, code_cell, fio_brief), формы и поля, постраничный переход,
меню и page(), вход и выход, шкала диаграмм. Разделы панели лежат в соседних
модулях пакета и берут отсюда всё, что нужно их страницам.
"""
import base64
import binascii
import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from html import escape as _escape
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import version
import charts
import database as db
import repository as repo
from panel_theme import (ICON_NAMES_BY_PATH, STYLESHEET, actions_script, hotkeys_script, icon,
                          panel_toast_js, theme_boot_script, theme_script)
from utils import as_str, cut_plain, norm_code, person_label, short_name, to_int

from .router import router


log = logging.getLogger("panel")


COOKIE = "lpc_panel"          # имя cookie-сессии


LOG_LINES = 400               # сколько строк журнала показывать по умолчанию


# Флеш-сообщение раньше было одной переменной на весь модуль, поэтому при двух
# открытых панелях сообщение одного сис-админа показывалось другому. Теперь
# сообщения лежат по токену сессии, а токен кладёт middleware на каждый запрос.
_flashes: dict[str, str] = {}


_current_token: contextvars.ContextVar[str] = contextvars.ContextVar("panel_token", default="")
_current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar("panel_request", default=None)


# Бейджи меню считает nav_badges() на каждый показ страницы, а читает page().
_nav_badges: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "panel_nav_badges", default={})


# ── сессии и доступ ───────────────────────────────────────────────────────────
# Словари остаются только как совместимый наблюдаемый кэш для тестов и flash.
# Источник истины — подписанная cookie: поэтому «Запомнить меня» переживает
# перезапуск процесса. В cookie нет пароля, только MAX ID, срок и случайный nonce.
_sessions: dict[str, tuple[str, float]] = {}
_csrf: dict[str, str] = {}


def _session_secret() -> bytes:
    secret = as_str(config.WEB_SESSION_SECRET)
    if len(secret) < 32:
        raise RuntimeError("WEB_SESSION_SECRET должен быть не короче 32 символов")
    return secret.encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(payload: str, purpose: bytes = b"session") -> str:
    digest = hmac.new(_session_secret(), purpose + b":" + payload.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)


def _decode_session(token: str) -> tuple[str, float] | None:
    if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
        return None
    try:
        payload, signature = token.split(".")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", payload) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", signature):
            return None
        if not hmac.compare_digest(signature, _signature(payload)):
            return None
        decoded = json.loads(_unb64(payload).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 3:
            return None
        user_id, expires, nonce = decoded
        if not isinstance(user_id, str) or not re.fullmatch(r"[0-9]{1,20}", user_id):
            return None
        if type(expires) is not int or not time.time() < expires <= time.time() + 30 * 86400 + 60:
            return None
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", nonce):
            return None
        return user_id, float(expires)
    except (ValueError, TypeError, binascii.Error, UnicodeError, RuntimeError):
        return None


def _csrf_for(token: str) -> str:
    return _signature(token, purpose=b"csrf") if _decode_session(token) else ""


def _purge_sessions() -> None:
    now = time.time()
    for token in [t for t, (_, exp) in _sessions.items() if exp <= now]:
        _sessions.pop(token, None)
        _csrf.pop(token, None)


def start_session(user_id: str, hours: int | None = None) -> str:
    """Подписанная сессия: обычная на WEB_PANEL_HOURS, запомненная — на 30 дней."""
    _purge_sessions()
    if hours is None:
        hours = config.WEB_PANEL_HOURS
    expires = int(time.time() + max(1, min(720, int(hours))) * 3600)
    payload = _b64(json.dumps(
        [str(user_id), expires, secrets.token_urlsafe(12)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    token = payload + "." + _signature(payload)
    _sessions[token] = (str(user_id), float(expires))
    _csrf[token] = _csrf_for(token)
    return token


def end_session(token: str) -> None:
    # Подписанная cookie не хранится на сервере: выход удаляет её в браузере.
    # Отзыв роли всё равно действует сразу — require_user проверяет БД на каждый запрос.
    _sessions.pop(token, None)
    _csrf.pop(token, None)


def form_token(request: Request) -> str:
    """CSRF-токен независимо выводится из подписанной cookie и переживает рестарт."""
    token = request.cookies.get(COOKIE, "")
    value = _csrf_for(token)
    if value:
        _csrf[token] = value
    return value


def session_user(request: Request) -> str:
    _purge_sessions()
    token = request.cookies.get(COOKIE, "")
    item = _decode_session(token)
    if not item:
        _sessions.pop(token, None)
        _csrf.pop(token, None)
        return ""
    _sessions[token] = item
    _csrf[token] = _csrf_for(token)
    return item[0]


def panel_enabled() -> bool:
    return bool(config.WEB_PANEL_PASSWORD)


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303)


async def is_sysadmin(user_id: str) -> bool:
    """Своим ли ID пользователь: запись в БД (в т.ч. владелец) или SYSADMIN_IDS из .env."""
    return await repo.is_sysadmin(user_id)


async def panel_role(user_id: str) -> str:
    """Точная роль вошедшего человека для безопасного аудита панели."""
    if not user_id:
        return "unknown"
    if await repo.is_owner(user_id):
        return "owner"
    return "sysadmin" if await repo.is_sysadmin(user_id) else "unknown"


async def log_panel_audit(user_id: str, action: str, target_type: str = "",
                          target_id: str = "", result: str = "ok") -> None:
    """Пишет аудит панели, не мешая запросу, но сообщая о сбое в техжурнал."""
    try:
        await repo.log_audit(user_id, await panel_role(user_id), action,
                             target_type, target_id, result)
    except Exception as exc:  # noqa: BLE001 — панель работает, потеря аудита видна
        log.warning("аудит панели %s не записан: %s", action, type(exc).__name__)


async def require_user(request: Request) -> str:
    """MAX ID вошедшего сис-админа, иначе — 503 (панель выключена) или 303 на вход.

    Здесь же запоминаем токен сессии: через эту проверку проходит каждая
    страница панели, поэтому flash() знает, в чью сессию писать сообщение.
    И считаются бейджи меню - тем же местом, чтобы не искать его в каждой
    странице отдельно.
    """
    _current_token.set(request.cookies.get(COOKIE, ""))
    _current_request.set(request)
    if not panel_enabled():
        raise HTTPException(status_code=503, detail="Панель выключена: задайте WEB_PANEL_PASSWORD в .env")
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/panel/login"})
    request.state.audit_user = user
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
    """Проверяет вход и CSRF. Результат действия журналируется после ответа маршрута."""
    user = await require_user(request)
    form_data = await request.form()
    expected = form_token(request)
    given = as_str(form_data.get("csrf", ""))
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        request.state.audit_result = "denied"
        raise HTTPException(status_code=403, detail="Устаревшая форма — обновите страницу")
    return user


# ── HTML ──────────────────────────────────────────────────────────────────────
# Стили раздела «Данные»: там в таблицах произвольные колонки базы, и их вид
# задаёт не тема, а сам раздел. Лежат здесь, а не в panel_theme, потому что
# тема - общая для всех разделов, а эти правила - только для одного.
DATA_STYLE = """
.data-table{width:100%;table-layout:auto}
.data-table th a{color:inherit}
.data-table td{max-width:340px}
.data-table .data-key{white-space:nowrap}
.data-table summary{cursor:pointer;padding:2px 0}
"""


STYLE = STYLESHEET + charts.palette_css(theme_only=True) + DATA_STYLE   # токены темы + стили раздела «Данные»


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


# Ширина кнопки MAX (max_api.BUTTON_TEXT): столько символов уходит на ФИО
# в боте, поэтому столько же отводим под подпись «Ковалевский К. Ю.».
FIO_BRIEF = 26


# Сколько символов влезает в ФИО в панели, прежде чем его придётся резать.
# Панель - не кнопка MAX: здесь имя переносится по словам, а предел нужен
# как страховка там, где переноса не будет (выпадающий список).
FIO_MAX = 60


def fio_brief(name) -> str:
    """«Ковалевский К. Ю.» - подпись, по которой человека узнают с ходу.

    Полное ФИО при этом никуда не девается: оно стоит рядом крупным шрифтом,
    а инициалы нужны только чтобы сориентироваться среди похожих фамилий.
    Если сокращение совпадает с полным именем (короткое ФИО), не возвращаем
    ничего - повторять одно и то же двумя строками незачем.
    """
    label = " ".join(as_str(name).split())
    brief = short_name(label, FIO_BRIEF)
    return "" if brief == label else brief


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
NAV_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("Пульт", (("/", "Обзор", "home", ""),
               ("/activity", "Лента событий", "activity", ""))),
    # Рабочее место - про обращения, Шаблоны - про ответы на них. Второй уровень
    # тут не нужен: пунктов мало, и они разные.
    ("Обращения", (("/tickets", "Рабочее место", "tickets", ""),
                   ("/templates", "Шаблоны", "templates", ""))),
    # В «Людях» было шесть пунктов подряд, и три из них - про выдачу доступа в
    # бота, а не про сотрудников. Отделили подзаголовком: список стал понятнее,
    # а разделы остались на своих местах.
    ("Люди", (("/people", "Реестр", "people", ""),
              ("/students", "Студенты", "students", ""),
              ("/staff", "Сотрудники", "staff", ""),
              ("/invites", "Выпуск по ссылкам", "link", "Доступ в бот"),
              ("/access", "Коды и заявки", "access", "Доступ в бот"),
              ("/nostaff", "Без прав", "user-off", "Доступ в бот"))),
    ("Справочники", (("/college", "Колледж", "college", ""),
                     ("/groups", "Группы", "groups", ""),
                     ("/schedules", "Расписания", "schedules", ""),
                     ("/broadcasts", "Рассылки", "broadcasts", ""))),
    # Данные и Тест - инструменты владельца, а не настройки. Без подзаголовка
    # «Настройки» читались бы как их родитель, а это не так.
    ("Система", (("/settings", "Настройки", "settings", ""),
                 ("/database", "База данных", "database", ""),
                 ("/data", "Данные", "archive", "Владельцу"),
                 ("/test", "Тест", "check", "Владельцу"),
                 ("/logs", "Технический журнал", "logs", ""),
                 ("/audit", "Полный аудит", "audit", ""))),
)


# Все разделы плоским списком - этим пользуются поиск по разделам и тесты.
TABS = tuple((path, name) for _group, items in NAV_GROUPS for path, name, _ico, _sub in items)


NAV_ICONS = ICON_NAMES_BY_PATH   # имена иконок вместо эмодзи


# Разделы для палитры Ctrl+K: путь с префиксом /panel и готовая иконка.
# Подкатегории в палитру не попадают: у них нет своего адреса, они только
# разделитель в меню.
NAV_SECTIONS = tuple(
    (group, tuple((name, f"/panel{item_path}", icon(item_icon, 16))
                  for item_path, name, item_icon, _sub in items))
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
        # Подкатегория - не ссылка, а разделитель: своих адресов у неё нет.
        # Пункты с одинаковой подкатегорией идут под одним мелким заголовком.
        chunks, current = [], None
        for item_path, name, item_icon, sub in items:
            if sub != current:
                current = sub
                if sub:
                    chunks.append(f'<span class="nav-sub-label">{esc(sub)}</span>')
            chosen = ' class="on" aria-current="page"' if item_path == tab else ""
            chunks.append(
                f'<a href="/panel{item_path}"{chosen}>'
                f'<span class="nav-ico" aria-hidden="true">{icon(item_icon, 18)}</span>'
                f'<span class="nav-txt">{esc(name)}</span></a>')
        links = "".join(chunks)
        blocks.append(
            f'<div class="nav-group{" on" if any(p == tab for p, _n, _i, _s in items) else ""}">'
            f'<span class="nav-group-label"><span class="nav-group-name">{esc(group)}</span>'
            f'{badge}</span><div class="nav-group-items">{links}</div></div>'
        )
    return '<div class="nav-groups">' + "".join(blocks) + "</div>"


def page(title: str, body: str, user: str = "", tab: str = "",
         actions: str = "", status_code: int = 200) -> HTMLResponse:
    """Оболочка страницы: шапка, меню группами, заголовок с действиями, скрипты.

    ``actions`` - кнопки в шапке (выгрузка CSV, печать): раньше они прятались
    в середине страницы, а на телефоне до них приходилось долистывать.
    """
    notice = _flashes.pop(_current_token.get(""), "")
    kind = "bad" if notice.startswith("!") else "ok"
    mark = icon("warning", 20) if kind == "bad" else icon("check", 20)
    banner = (f'<div class="msg msg-{kind}" data-flash data-kind="{kind}" role="status">'
              f'{mark}<span>{esc(notice.lstrip("!"))}</span></div>' if notice else "")
    theme_boot = f'<script>{theme_boot_script()}</script>'
    css_head = (f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta name="color-scheme" content="dark light">{theme_boot}'
                f'<title>{esc(title)} — Навигатор ЛПК</title><style>{STYLE}</style>')
    scripts = (f'<script>{theme_script()}</script><script>{panel_toast_js()}</script>'
               f'<script>{actions_script()}</script>')
    if title in ("Вход", "Панель выключена"):
        return HTMLResponse(
            f'<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">'
            f'{css_head}</head><body class="login-page">'
            f'<button class="theme-toggle login-theme" type="button" aria-label="Сменить тему"></button>'
            f'<main id="main-content" class="login-shell">{banner}{body}</main>'
            f'<footer class="login-footer">Навигатор ЛПК · Работа с обращениями в MAX · '
            f'версия {esc(version.__version__)}</footer>{scripts}</body></html>',
            status_code=status_code, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"},
        )
    scripts += f'<script>{hotkeys_script(NAV_SECTIONS)}</script>'
    nav = nav_html(tab, _nav_badges.get())
    section = next((group for group, items in NAV_GROUPS
                    if any(item[0] == tab for item in items)), "Рабочее пространство")
    head = (f'<div class="dochead"><div class="dochead-title"><span class="eyebrow">{esc(section)}</span>'
            f'<h1 class="page-title">{esc(title)}</h1></div>'
            f'<div class="page-actions">{actions}</div></div>')
    return HTMLResponse(
        f"""<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">
{css_head}</head><body class="panel-app" data-tab="{esc(tab)}">
<a class="skip-link" href="#main-content">Перейти к содержимому</a>
<header class="app-header">
  <div class="brand"><span class="logo">{icon("navigator", 24)}</span><span>
    <b>Навигатор <em>ЛПК</em></b><small>панель сис-админа</small></span></div>
  <button class="gsearch palette-trigger" type="button" data-palette-open aria-label="Поиск по панели">
    {icon("search", 18)}<span>Найти обращение, человека, группу</span><kbd>Ctrl K</kbd>
  </button>
  <div class="header-account"><button class="theme-toggle" type="button" aria-label="Сменить тему"></button>
    <div class="who"><span class="avatar">{icon("user", 18)}</span><span><small>Учётная запись</small><b>{esc(user) or '—'}</b></span></div>
    <form method="post" action="/panel/logout" class="inline shell-logout">{csrf_from_current()}</form>
  </div>
</header>
<nav class="app-nav" aria-label="Разделы панели"><div class="nav-caption">Рабочее пространство</div>{nav}</nav>
<footer class="foot-ver"><span>{icon("college", 14)} Панель колледжа</span><span>версия {esc(version.__version__)}</span>
<a href="/panel/api/health">{icon("activity", 14)} состояние</a></footer>
<main id="main-content" tabindex="-1">{head}{banner}{body}</main>
<footer class="app-footer"><span>Навигатор ЛПК</span><span>Единая очередь · Данные из бота в MAX</span></footer>
{scripts}
</body></html>""", status_code=status_code, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"},
    )


def flash(message: str) -> None:
    """Одноразовое сообщение для следующей страницы (ошибка помечается «!»).

    Сообщение получает только та сессия, из которой оно отправлено.
    """
    request = _current_request.get()
    if request is not None and message.startswith("!"):
        request.state.audit_result = "denied"
    _flashes[_current_token.get("")] = message[:300]


def csrf_from_current() -> str:
    """Кнопка выхода в общей шапке: токен берётся из текущего подписанного cookie."""
    token = _current_token.get("")
    value = _csrf.get(token, "") or _csrf_for(token)
    return (f'<input type="hidden" name="csrf" value="{esc(value)}">'
            f'<button class="link-button" type="submit" title="Выйти" aria-label="Выйти из панели">'
            f'{icon("logout", 18)}<span>Выйти</span></button>')


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
           label: str = "", titles: dict | None = None,
           datas: dict | None = None) -> str:
    """Список выбора с подписью: по умолчанию - имя поля, у важных фильтров
    подпись задаётся явно («Статус», «Раздел», «Тип события»).

    ``titles`` кладёт вторую строку (должность сотрудника) в подсказку пункта.
    Выпадающий список режет всё, что не помещается, сам и без предупреждения,
    поэтому важное - ФИО целиком - должно быть в самом пункте, а
    второстепенное - в ``title``, где оно остаётся читаемым.

    ``datas`` - дополнительные атрибуты пункта: ``{код: {"data-text": "..."}}``.
    Нужен там, где значение не помещается в подпись и должно достаться на
    стороне браузера - например текст шаблона ответа, который выбирают из
    списка, а подставить его должна форма.
    """
    hints = titles or {}
    extras = datas or {}

    def option(code, text) -> str:
        hint = as_str(hints.get(code))
        extra = f' title="{esc(hint)}"' if hint else ""
        for attr, value in (extras.get(code) or {}).items():
            extra += f' {esc(attr)}="{esc(value)}"'
        picked = " selected" if code == current else ""
        return f'<option value="{esc(code)}"{picked}{extra}>{esc(text)}</option>'

    items = "".join(option(code, text) for code, text in options.items())
    extra = ' class="full"' if full else ""
    title = esc(label if label else name)
    return f'<div{extra}><label>{title}</label><select name="{esc(name)}">{items}</select></div>'


PAGE_WINDOW = 2000      # сколько строк читаем «вперёд», чтобы узнать про следующую страницу


def pager(path: str, params: list[tuple[str, str]], page_no: int, pages: int,
          note: str = "") -> str:
    """Постраничный переход: та же разметка, что на ленте событий (/activity).

    params - пары «поле → значение» из фильтров страницы: они переносятся в
    каждую ссылку, поэтому при переходе фильтр не сбрасывается. Номер
    страницы добавляется сам, последним. Когда страница одна, переход не
    рисуется вовсе: лишние кнопки на странице только мешают.
    """
    if pages <= 1:
        return ""

    def step(number: int, label: str) -> str:
        query = esc(urlencode([*params, ("page_no", str(number))]))
        cls = "btn-grey" if number != page_no else ""
        return f'<a class="btn {cls}" href="{path}?{query}">{label}</a>'

    middle = f'<span class="small mut">Страница {page_no} из {pages}'
    if note:
        middle += f" · {esc(note)}"
    return (f'<div class="pager">{step(max(1, page_no - 1), f'{icon("chevron-left", 16)} Назад')}'
            f'{middle}</span>'
            f'{step(min(pages, page_no + 1), f'Вперёд {icon("chevron-right", 16)}')}</div>')


def page_window(page_no: int, per_page: int) -> int:
    """Сколько строк прочитать, чтобы показать страницу и понять, есть ли следующая.

    Списки репозиторий отдаёт без OFFSET, поэтому страница берётся из окна:
    читаем на страницу вперёд, ровно как на ленте событий. PAGE_WINDOW
    ограничивает окно, чтобы глубокая страница не тянула из базы весь список.
    """
    return min(PAGE_WINDOW, (page_no + 1) * per_page)


def pages_of(total: int, per_page: int) -> int:
    """Сколько страниц в списке из total строк; список меньше страницы - это одна."""
    return max(1, (int(total) + per_page - 1) // per_page)


def window_tail(found: int, window: int, hint: str) -> str:
    """Честная оговорка, когда список упёрся в окно: дальше что-то есть, но не видно."""
    if found < window:
        return ""
    return (f'<p class="small mut">Список длиннее окна в {window} строк: показаны первые. '
            f'{esc(hint)}</p>')


def safe_return(data, fallback: str = "/panel/tickets") -> str:
    """Адрес возврата из формы: только свой раздел панели.

    Значение приходит из запроса и уходит в redirect, поэтому берём его не
    как есть. Разрешаем лишь путь внутри /panel: без схемы, без "//" (иначе
    это "http://чужой-сайт" относительным адресом) и без переводов строк -
    иначе в Location попадёт новая строка и ответ развалится на два.

    Нужно не для красоты. Без проверки поле return уезжало в Location без
    слеша, браузер разрешал адрес относительно текущего и получался
    /panel/tickets/status=&category=&q=&scope= - 422 вместо списка.
    """
    raw = as_str(value(data, "return")).strip()
    if not raw or not raw.startswith("/panel/") or raw.startswith("//"):
        return fallback
    if "\n" in raw or "\r" in raw or "://" in raw or raw.startswith("/panel/../"):
        return fallback
    return raw


def value(form, *names: str, default: str = "") -> str:
    """Первое непустое значение из формы: принимает «новое» или «старое» имя поля."""
    for name in names:
        found = as_str(form.get(name, "")).strip()
        if found:
            return found
    return default


# ── шкала диаграмм ───────────────────────────────────────────────────────────
NICE_STEPS = (1, 2, 2.5, 5)      # «красивые» числа внутри одного десятка


def nice_max(value: float) -> float:
    """Верх шкалы диаграммы: округление вверх до ближайшего «красивого» числа.

    Правило. Берём ряд 1 - 2 - 2,5 - 5 в каждом десятке (1, 2, 2,5, 5, 10, 20,
    25, 50, 100, 200, 250, 500 …) и отдаём первое число, которое не меньше
    значения. Отсюда три свойства, ради которых функция и написана:

    * ноль, минус и пустое значение дают 1, а не 0: столбики не превращаются в
      сетку с нулём наверху, и деление на верх шкалы всегда безопасно;
    * округление всегда вверх и всегда в самую мелкую «красивую» ступень,
      поэтому завышение не больше, чем в 2 раза: 11 обращений дают верх 20, а
      не 1000 (столбик в треть высоты графика лучше, чем в одной сотой);
    * ряд не обрывается на 10000, как было раньше: 20001 обращение даёт верх
      25000, а не само число, поэтому сетка остаётся ровной на любом масштабе.

    Раньше ряд был выписан руками (1, 2, 5, 10, 20, 25, 50, 100, 200, 500 …):
    в нём потерялись 2,5, 250 и 2500, а всё, что больше 10000, возвращалось
    как есть - ось подписывалась «12347», и график выглядел сломанным.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not value > 0:            # ноль, минус и nan: шкала всё равно нужна
        return 1.0
    decade = 1.0
    while True:
        for nice in NICE_STEPS:
            top = decade * nice
            if top >= value:
                return top
        decade *= 10.0


# charts зовёт свою копию этой функции: подменяем её правилом выше, чтобы
# столбики и мини-график панели считались по одному и тому же ряду.
charts._nice_max = nice_max


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
    return login_response()


def login_response(message: str = "", status_code: int = 200) -> HTMLResponse:
    """Вход без данных студентов: фирменное вступление и доступная форма."""
    error = (f'<div class="msg msg-bad" role="alert">{icon("warning", 18)}'
             f'<span>{esc(message)}</span></div>') if message else ""
    body = f"""<section class="login-story" aria-labelledby="login-heading">
<div class="login-brand"><span class="logo">{icon("navigator", 32)}</span><div><b>Навигатор ЛПК</b><small>Колледж ближе</small></div></div>
<span class="eyebrow">ЕДИНАЯ РАБОЧАЯ СРЕДА</span>
<h1 id="login-heading">Меньше рутины.<br><span>Больше внимания.</span></h1>
<p class="login-lead">Обращения, расписание и работа колледжа — в одном месте. Студенты остаются в привычном MAX.</p>
<div class="login-flow" aria-label="Путь обращения"><span>{icon("tickets", 20)} Обращение</span><i>{icon("chevron-right", 16)}</i>
<span>{icon("clock", 20)} В работе</span><i>{icon("chevron-right", 16)}</i><span>{icon("check", 20)} Ответ</span></div>
<div class="login-points"><span>{icon("inbox", 18)} Прозрачная очередь</span><span>{icon("audit", 18)} История действий</span><span>{icon("schedules", 18)} Расписание в MAX</span></div>
<div class="login-orbit" aria-hidden="true"><div class="orbit-ring"></div><div class="orbit-core">{icon("navigator", 100)}</div>
<span class="orbit-note orbit-note-a">{icon("check", 16)} Ни одного потерянного обращения</span>
<span class="orbit-note orbit-note-b">{icon("reply", 16)} Понятный следующий шаг</span></div>
</section>
<section class="card login-card"><span class="login-lock">{icon("lock", 24)}</span><h2>Вход для сис-админа</h2>
<p class="mut">Рады видеть вас снова.<br>Продолжим работу с обращениями.</p>{error}
<form method="post" action="/panel/login">
<div><label for="login-id">MAX ID</label><input id="login-id" name="user_id" required autocomplete="username" inputmode="numeric" placeholder="Ваш идентификатор в MAX"></div>
<div><label for="login-password">Пароль</label><input id="login-password" name="password" type="password" required autocomplete="current-password" placeholder="Введите пароль панели"></div>
<label class="remember-row"><input type="checkbox" name="remember" value="1"> Запомнить меня на 30 дней</label>
<button class="login-submit" type="submit">Войти {icon("chevron-right", 18)}</button></form>
<p class="login-help">Не знаете свой ID? Отправьте боту <code>/id</code>.</p>
<div class="login-security">{icon("audit", 16)} Доступ только по роли сис-админа</div></section>"""
    return page("Вход", body, status_code=status_code)


@router.post("/login")
async def login_submit(request: Request):
    _current_request.set(request)
    _current_token.set("")
    request.state.audit_skip = True
    form_data = await request.form()
    if not panel_enabled():
        raise HTTPException(status_code=503, detail="Панель выключена")
    raw_user_id = as_str(form_data.get("user_id", "")).strip()
    user_id = raw_user_id if re.fullmatch(r"[0-9]{1,20}", raw_user_id) else ""
    password = as_str(form_data.get("password", ""))
    remember = flag(form_data.get("remember"))
    ok_password = hmac.compare_digest(password.encode(), config.WEB_PANEL_PASSWORD.encode())
    if not user_id or not ok_password or not await is_sysadmin(user_id):
        log.warning("неудачный вход в панель: id=%s", user_id or "?")
        await log_panel_audit(user_id or "", "panel_login", result="denied")
        return login_response("Неверный MAX ID или пароль", status_code=403)
    hours = 24 * 30 if remember else max(1, min(720, config.WEB_PANEL_HOURS))
    token = start_session(user_id, hours=hours)
    log.info("вход в панель: %s%s", user_id, " (запомнить)" if remember else "")
    await log_panel_audit(user_id, "panel_login", "session", "remember" if remember else "regular")
    response = redirect("/panel/")
    max_age = hours * 3600
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    secure = request.url.scheme == "https" or forwarded == "https"
    response.set_cookie(
        COOKIE, token, max_age=max_age,
        httponly=True, secure=secure, samesite="lax", path="/panel",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    user_id = await require_form(request)
    request.state.audit_skip = True
    token = request.cookies.get(COOKIE, "")
    await log_panel_audit(user_id, "panel_logout")
    end_session(token)
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


# Карточку сотрудника и аналитику волнует одно и то же «2 ч 15 мин»,
# поэтому помощник общий, а не свой у каждого раздела.
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


# Кнопку-действие рисуют шесть разделов (студенты, сотрудники, реестр,
# коды, расписания, база), поэтому она живёт здесь, а не в базе данных.
def _action_form(request: Request, action: str, label: str, fields: str = "",
                 confirm_text: str = "", cls: str = "btn-grey") -> str:
    """Кнопка-действие: одна форма на действие, с подтверждением в браузере.

    В ``label`` можно передать готовую иконку ``icon(...)`` - такие подписи
    вставляются как есть, всё остальное экранируется.
    """
    script = (f' onclick="{esc("return confirm(" + json.dumps(confirm_text, ensure_ascii=False) + ")")}"'
              if confirm_text else "")
    body = label if label.lstrip().startswith("<svg") else esc(label)
    return (f'<form method="post" action="{esc(action)}" class="inline"{script}>{csrf(request)}'
            f'{fields}<button class="{esc(cls)}">{body}</button></form>')


def fio(name, user_id: str = "", limit: int = 0) -> str:
    """ФИО для панели: целиком, а если ФИО не заполнено - по MAX ID.

    Сокращать ФИО в панели нельзя: сотрудник приёмной комиссии сверяет
    обращение с человеком по записи, и «Ковалевский Ко…» для этого бесполезно.
    Имя переносится по словам средствами темы (``.wb-fio``), а ``limit`` -
    последняя страховка для мест, где переноса не будет (выпадающий список):
    обрезка идёт по границе слова и без многоточия (``utils.cut_plain``).
    """
    label = " ".join(as_str(name).split())
    if not label:
        return person_label("", user_id, limit)   # ФИО не заполнено: «ID 300»
    return cut_plain(label, limit) if limit else label
