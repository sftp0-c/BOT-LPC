"""Отдельный вход сотрудников в кабинет: код в MAX, CSRF и отзывная сессия."""
import contextvars
import hmac
import logging
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import ClientDisconnect

import config
from handlers import common as bot_common
from panel_theme import STYLESHEET, icon, theme_boot_script, theme_script
from store.web_access import (
    STAFF_WEB_SESSION_TTL, cancel_staff_web_challenge, request_staff_web_code,
    revoke_staff_web_session, staff_web_hash, staff_web_principal, staff_web_secret,
    staff_web_session_hash, verify_staff_web_code,
)

from .common import esc
from .router import workspace_router

STAFF_WEB_COOKIE = "lpc_staff_web"
WORKSPACE_BROWSER_COOKIE = "lpc_workspace_browser"
WORKSPACE_BROWSER_TTL = 10 * 60
MAX_LOGIN_BODY = 8 * 1024
MAX_STAFF_BODY = 64 * 1024
WORKSPACE_SECURITY = {
    "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
NEUTRAL_REQUEST = "Если для этого MAX ID включён веб-доступ и бот может написать вам, код придёт в MAX. Он действует 5 минут."
NEUTRAL_VERIFY = "Код не подошёл или истёк. Проверьте его либо запросите новый."
log = logging.getLogger("workspace.auth")
_delivery_private = contextvars.ContextVar("workspace_otp_delivery", default=False)


class _DeliveryFilter(logging.Filter):
    def filter(self, record):
        # HTTPX пишет URL с адресатом; произвольные DEBUG драйвера тоже не нужны.
        return not _delivery_private.get()


@contextmanager
def _private_delivery():
    """Скрывает только логи текущей задачи отправки, не выключая другие запросы."""
    guard = _DeliveryFilter()
    loggers = [logging.getLogger(), *[item for item in logging.Logger.manager.loggerDict.values()
                                      if isinstance(item, logging.Logger)]]
    handlers = {handler for logger in loggers for handler in logger.handlers}
    for handler in handlers:
        handler.addFilter(guard)
    token = _delivery_private.set(True)
    try:
        yield
    finally:
        _delivery_private.reset(token)
        for handler in handlers:
            handler.removeFilter(guard)


def _now() -> int:
    return int(time.time())


def _origin(value: str, *, configured: bool = False):
    try:
        if not isinstance(value, str) or any(char.isspace() for char in value):
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None \
                or parsed.password is not None or parsed.query or parsed.fragment:
            return None
        if not configured and parsed.path:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _origin_allowed(request: Request) -> bool:
    origins = request.headers.getlist("origin")
    # Небраузерные клиенты без Origin обязаны предъявить обычный CSRF; null запрещён.
    if not origins:
        return request.headers.get("sec-fetch-site", "") != "cross-site"
    trusted = _origin(config.PUBLIC_URL, configured=True)
    return len(origins) == 1 and trusted is not None and _origin(origins[0]) == trusted


def _workspace_address() -> str:
    trusted = _origin(config.PUBLIC_URL, configured=True)
    if not trusted:
        return "кабинет сотрудника колледжа"
    scheme, host, port = trusted
    hostname = f"[{host}]" if ":" in host else host
    suffix = "" if port == (443 if scheme == "https" else 80) else f":{port}"
    return f"{scheme}://{hostname}{suffix}/workspace/login"


def _secure_cookie(request: Request) -> bool:
    trusted = _origin(config.PUBLIC_URL, configured=True)
    return request.url.scheme == "https" or bool(trusted and trusted[0] == "https")


def _error(status: int, detail: str):
    return HTTPException(status_code=status, detail=detail, headers=WORKSPACE_SECURITY)


def _require_secret() -> None:
    if staff_web_secret() is None:
        raise _error(503, "Вход сотрудников временно недоступен. Попробуйте позже.")


def _new_browser(now: int) -> str:
    payload = f"{now + WORKSPACE_BROWSER_TTL}.{secrets.token_urlsafe(32)}"
    return payload + "." + staff_web_hash("browser", payload)


def _browser_expires(cookie: str, now: int) -> int:
    if not isinstance(cookie, str) or not re.fullmatch(r"[0-9]{1,12}\.[A-Za-z0-9_-]{43}\.[0-9a-f]{64}", cookie):
        return 0
    payload, signature = cookie.rsplit(".", 1)
    expires = int(payload.split(".", 1)[0])
    if not now < expires <= now + WORKSPACE_BROWSER_TTL:
        return 0
    return expires if hmac.compare_digest(signature, staff_web_hash("browser", payload)) else 0


def _browser_csrf(cookie: str) -> str:
    return staff_web_hash("browser-csrf", cookie)


def _peer_hash(request: Request) -> str:
    # Не доверяем Forwarded/XFF. За прокси это общий ограничитель соседа.
    return staff_web_hash("peer", request.client.host if request.client else "unknown")


def workspace_auth_page(title: str, content: str, *, status_code: int = 200) -> HTMLResponse:
    """Изолированная оболочка без меню, счётчиков и скриптов панели сис-админа."""
    return HTMLResponse(f"""<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark light">
<title>{esc(title)} — Навигатор ЛПК</title><script>{theme_boot_script()}</script><style>{STYLESHEET}</style>
<link rel="stylesheet" href="/workspace/assets/workspace.css"></head><body class="ws-auth">
<a class="skip-link" href="#main-content">Перейти к содержимому</a>
<header class="ws-auth-header"><a class="ws-auth-brand" href="/">{icon("navigator", 28)}
<span><b>Навигатор ЛПК</b><small>Для сотрудников колледжа</small></span></a>
<button class="theme-toggle ws-auth-theme" type="button" aria-label="Сменить тему"></button></header>
<main id="main-content" class="ws-auth-shell" tabindex="-1">
<section class="ws-auth-story" aria-labelledby="workspace-intro-title">
<span class="ws-auth-kicker">Ваше рабочее место</span>
<h2 id="workspace-intro-title">Каждое обращение —<br><span>на своём месте.</span></h2>
<p class="ws-auth-lead">Откройте свои обращения, посмотрите историю и ответьте студенту.
Общение продолжится в привычном MAX.</p>
<ol class="ws-auth-flow" aria-label="Как устроена работа с обращением">
<li><span class="ws-auth-flow-icon" aria-hidden="true">{icon("inbox", 24)}</span>
<strong>Обращение</strong><small>В вашей очереди</small></li>
<li><span class="ws-auth-flow-icon" aria-hidden="true">{icon("reply", 24)}</span>
<strong>Ответ</strong><small>С учётом истории</small></li>
<li><span class="ws-auth-flow-icon" aria-hidden="true">{icon("check", 24)}</span>
<strong>Результат</strong><small>Студенту в MAX</small></li></ol>
<p class="ws-auth-footnote">{icon("lock", 18)}<span>Только разрешённые вам обращения и действия.
Доступ к кабинету выдаёт сис-админ.</span></p></section>
<section class="ws-auth-card" aria-labelledby="auth-title">{content}</section></main>
<footer class="ws-auth-footer"><span>Навигатор ЛПК · Бета-тестирование</span>
<span>Не передавайте код входа другим людям</span></footer>
<script>{theme_script()}</script></body></html>""", status_code=status_code, headers=WORKSPACE_SECURITY)


def _login_page(cookie: str, challenge: str = "", notice: str = "", *, status_code: int = 200) -> HTMLResponse:
    message = f'<p class="ws-auth-notice" role="status">{esc(notice)}</p>' if notice else ""
    token = esc(_browser_csrf(cookie))
    if challenge:
        step, intro = "Шаг 2 из 2 · Подтверждение", "Введите код из личного сообщения бота."
        fields = f"""<form class="ws-auth-form" method="post" action="/workspace/login/verify" accept-charset="UTF-8">
<input type="hidden" name="csrf" value="{token}"><input type="hidden" name="challenge" value="{esc(challenge)}">
<div class="ws-auth-input-group"><label class="ws-auth-field" for="workspace-code">Код из MAX</label>
<input id="workspace-code" name="code" type="text" inputmode="numeric" pattern="[0-9]{{8}}" minlength="8" maxlength="8"
 autocomplete="one-time-code" aria-describedby="code-hint" required>
<p id="code-hint" class="ws-auth-hint">8 цифр. Код действует только в этом браузере и только один раз.</p></div>
<button class="btn btn-primary" type="submit">Войти</button></form>
<a class="ws-auth-back" href="/workspace/login">Запросить другой код</a>"""
    else:
        step, intro = "Шаг 1 из 2 · Идентификатор", "Укажите свой MAX ID — код входа придёт в MAX."
        fields = f"""<form class="ws-auth-form" method="post" action="/workspace/login/request" accept-charset="UTF-8">
<input type="hidden" name="csrf" value="{token}"><div class="ws-auth-input-group">
<label class="ws-auth-field" for="workspace-id">Ваш MAX ID</label>
<input id="workspace-id" name="user_id" type="text" inputmode="numeric" pattern="[0-9]+" maxlength="19"
 autocomplete="username" aria-describedby="id-hint" required>
<p id="id-hint" class="ws-auth-hint">Вход доступен сотрудникам, которым сис-админ отдельно разрешил веб-доступ.
Сначала откройте диалог с ботом в MAX и нажмите «Начать» или отправьте /start.
Ваш ID можно узнать командой /id.</p></div><button class="btn btn-primary" type="submit">Получить код в MAX</button></form>"""
    return workspace_auth_page("Вход сотрудника", f'<p class="ws-auth-step">{step}</p>'
                               '<h1 id="auth-title">Кабинет сотрудника</h1>'
                               f'<p class="ws-auth-intro">{intro}</p>' + message + fields
                               + '<details class="ws-auth-help"><summary>Код не приходит?</summary>'
                               '<p class="ws-auth-hint">Попросите сис-админа проверить «Доступ сотрудников» → '
                               '«Доступ к кабинету»: он должен быть включён. Приглашение в MAX не включает веб-доступ. '
                               'Убедитесь, что указан ваш MAX ID, а диалог с ботом уже открыт. '
                               'После нескольких запросов подряд подождите 10 минут перед новой попыткой.</p></details>'
                               '<a class="ws-auth-back ws-auth-admin" href="/panel/login">Я сис-админ — вход по паролю</a>',
                               status_code=status_code)


def staff_form_token(request: Request) -> str:
    token = request.cookies.get(STAFF_WEB_COOKIE, "")
    if staff_web_secret() is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return ""
    return staff_web_hash("session-csrf", token)


def staff_csrf(request: Request) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(staff_form_token(request))}">'


async def require_staff_web(request: Request) -> dict:
    _require_secret()
    token = request.cookies.get(STAFF_WEB_COOKIE, "")
    principal = await staff_web_principal(staff_web_session_hash(token))
    if principal is None:
        raise HTTPException(status_code=303, headers={**WORKSPACE_SECURITY, "Location": "/workspace/login"})
    request.state.audit_user = principal["user_id"]
    request.state.staff_web_principal = principal
    return principal


async def _read_staff_body(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded" \
            or request.headers.get("content-encoding", "identity") != "identity":
        raise _error(415, "Отправьте обычную текстовую форму без файлов.")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_STAFF_BODY:
                raise _error(413, "Форма слишком большая.")
            body.extend(chunk)
    except ClientDisconnect:
        raise _error(400, "Отправка прервалась. Обновите страницу.") from None
    try:
        encoded = body.decode("utf-8")
        if re.search(r"%(?![0-9a-fA-F]{2})", encoded):
            raise ValueError
        parsed = parse_qs(encoded, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=32)
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError
    except (UnicodeError, ValueError):
        raise _error(400, "Некорректная форма. Обновите страницу.") from None
    # Starlette затем разбирает тот же ограниченный буфер; stream второй раз не читается.
    request._body = bytes(body)


async def require_staff_form(request: Request) -> dict:
    principal = await require_staff_web(request)
    if not _origin_allowed(request):
        raise _error(403, "Форма открыта с другого сайта. Обновите страницу.")
    await _read_staff_body(request)
    data = await request.form()
    values = data.getlist("csrf")
    given = values[0] if len(values) == 1 and isinstance(values[0], str) else ""
    expected = staff_form_token(request)
    if not expected or not hmac.compare_digest(given.encode("utf-8"), expected.encode("ascii")):
        raise _error(403, "Устаревшая форма — обновите страницу.")
    return principal


async def _login_form(request: Request, fields: set[str]) -> tuple[dict, str, int]:
    _require_secret()
    cookie = request.cookies.get(WORKSPACE_BROWSER_COOKIE, "")
    expires = _browser_expires(cookie, _now())
    if not expires or not _origin_allowed(request):
        raise _error(403, "Форма устарела или открыта с другого сайта. Обновите страницу.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded" \
            or request.headers.get("content-encoding", "identity") != "identity":
        raise _error(415, "Отправьте обычную текстовую форму без файлов.")
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_LOGIN_BODY:
                raise _error(413, "Форма слишком большая.")
            raw.extend(chunk)
    except ClientDisconnect:
        raise _error(400, "Отправка прервалась. Обновите страницу.") from None
    try:
        encoded = raw.decode("utf-8")
        if re.search(r"%(?![0-9a-fA-F]{2})", encoded):
            raise ValueError
        data = parse_qs(encoded, keep_blank_values=True, strict_parsing=True,
                        encoding="utf-8", errors="strict", max_num_fields=5)
        if set(data) - fields or any(len(values) != 1 for values in data.values()):
            raise ValueError
    except (UnicodeError, ValueError):
        raise _error(400, "Некорректная форма. Обновите страницу.") from None
    data = {key: values[0] for key, values in data.items()}
    if not hmac.compare_digest(data.get("csrf", "").encode("utf-8"), _browser_csrf(cookie).encode("ascii")):
        raise _error(403, "Устаревшая форма — обновите страницу.")
    return data, cookie, expires


@workspace_router.get("/login", include_in_schema=False)
async def workspace_login(request: Request):
    if staff_web_secret() is None:
        return workspace_auth_page("Вход временно недоступен", '<h1 id="auth-title">Вход временно недоступен</h1>'
                                   '<p class="ws-auth-notice" role="status">Попробуйте позже.</p>', status_code=503)
    token = request.cookies.get(STAFF_WEB_COOKIE, "")
    if token and await staff_web_principal(staff_web_session_hash(token)):
        return RedirectResponse("/workspace", status_code=303, headers=WORKSPACE_SECURITY)
    now = _now()
    cookie = request.cookies.get(WORKSPACE_BROWSER_COOKIE, "")
    fresh = not _browser_expires(cookie, now)
    if fresh:
        cookie = _new_browser(now)
    response = _login_page(cookie)
    if fresh:
        response.set_cookie(WORKSPACE_BROWSER_COOKIE, cookie, max_age=WORKSPACE_BROWSER_TTL, path="/workspace",
                            httponly=True, samesite="lax", secure=_secure_cookie(request))
    return response


def _login_failure(exc: HTTPException) -> HTMLResponse:
    """Просроченная форма остаётся понятной страницей, не голым JSON."""
    return workspace_auth_page("Не удалось войти",
                               '<h1 id="auth-title">Не удалось войти</h1>'
                               f'<p class="ws-auth-notice" role="alert">{esc(exc.detail)}</p>'
                               '<a class="btn btn-primary" href="/workspace/login">Открыть новую форму входа</a>',
                               status_code=exc.status_code)


@workspace_router.post("/login/request", include_in_schema=False)
async def workspace_login_request(request: Request):
    try:
        data, cookie, expires = await _login_form(request, {"csrf", "user_id"})
    except HTTPException as exc:
        return _login_failure(exc)
    try:
        delivery = await request_staff_web_code(data.get("user_id", "")[:100], staff_web_hash("browser-id", cookie),
                                               _peer_hash(request), browser_expires=expires)
    except sqlite3.Error:
        raise _error(503, "Вход временно недоступен. Попробуйте позже.") from None
    if delivery["user_id"]:
        try:
            with _private_delivery():
                await bot_common.api.send(delivery["user_id"],
                                          f"Код входа в кабинет сотрудника Навигатора ЛПК: {delivery['code']}\n"
                                          f"Сайт: {_workspace_address()}\n"
                                          "Действует 5 минут. Никому не сообщайте код. "
                                          "Если вы не входили на сайт, проигнорируйте сообщение.")
        except Exception as exc:
            log.warning("Не удалось отправить код входа сотрудника: %s", type(exc).__name__)
            try:
                await cancel_staff_web_challenge(delivery["challenge"])
            except sqlite3.Error:
                pass  # Клиенту всё равно только нейтральный ответ; код короткоживущий.
    return _login_page(cookie, delivery["challenge"], NEUTRAL_REQUEST)


@workspace_router.post("/login/verify", include_in_schema=False)
async def workspace_login_verify(request: Request):
    try:
        data, cookie, _expires = await _login_form(request, {"csrf", "challenge", "code"})
    except HTTPException as exc:
        return _login_failure(exc)
    challenge = data.get("challenge", "")[:200]
    try:
        token = await verify_staff_web_code(challenge, data.get("code", "")[:100],
                                           staff_web_hash("browser-id", cookie), _peer_hash(request))
    except sqlite3.Error:
        raise _error(503, "Вход временно недоступен. Попробуйте позже.") from None
    if token is None:
        return _login_page(cookie, challenge, NEUTRAL_VERIFY)
    response = RedirectResponse("/workspace", status_code=303, headers=WORKSPACE_SECURITY)
    response.set_cookie(STAFF_WEB_COOKIE, token, max_age=STAFF_WEB_SESSION_TTL, path="/workspace",
                        httponly=True, samesite="lax", secure=_secure_cookie(request))
    response.delete_cookie(WORKSPACE_BROWSER_COOKIE, path="/workspace", httponly=True,
                           samesite="lax", secure=_secure_cookie(request))
    return response


@workspace_router.post("/logout", include_in_schema=False)
async def workspace_logout(request: Request):
    principal = await require_staff_form(request)
    await revoke_staff_web_session(principal["session_hash"])
    response = RedirectResponse("/workspace/login", status_code=303, headers=WORKSPACE_SECURITY)
    response.delete_cookie(STAFF_WEB_COOKIE, path="/workspace", httponly=True,
                           samesite="lax", secure=_secure_cookie(request))
    return response
