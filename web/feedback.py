"""Публичная анонимная форма и закрытая очередь обратной связи о сервисе."""
import hashlib
import hmac
import ipaddress
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import ClientDisconnect

import config

import version
import repository as repo

from .common import csrf, esc, icon, page, require_form, require_user
from .router import open_router, router

FEEDBACK_COOKIE = "lpc_feedback"
FEEDBACK_TTL = 2 * 3600
MAX_BODY_BYTES = 64 * 1024
KINDS = {"bug": "Ошибка", "suggestion": "Предложение"}
STATUSES = {"new": "Новое", "in_progress": "В работе", "closed": "Закрыто"}
SECURITY = {
    "Content-Security-Policy": "default-src 'none'; script-src 'none'; style-src 'self'; "
    "img-src 'self'; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin", "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


def _now() -> int:
    return int(time.time())


def _secret() -> bytes | None:
    # Не берём пароль панели, токен MAX или запасной ключ: выключаем только эту форму.
    value = config.WEB_SESSION_SECRET
    if not isinstance(value, str) or len(value.strip()) < 32:
        return None
    try:
        return value.encode("utf-8")
    except UnicodeError:
        return None


def _mac(secret: bytes, purpose: str, value: str) -> str:
    return hmac.new(secret, (purpose + "\0" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def _nonce(secret: bytes, purpose: str, expires: int, context: str = "") -> str:
    payload = f"{expires}.{secrets.token_hex(16)}"
    return payload + "." + _mac(secret, purpose, context + ":" + payload)


def _valid_nonce(secret: bytes, purpose: str, token: str, now: int, context: str = "") -> int:
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", token):
        return 0
    expires, _random, signature = token.split(".")
    if not now < int(expires) <= now + FEEDBACK_TTL:
        return 0
    expected = _mac(secret, purpose, context + ":" + token.rsplit(".", 1)[0])
    return int(expires) if hmac.compare_digest(signature, expected) else 0


def _csrf(secret: bytes, cookie: str) -> str:
    return _mac(secret, "public-feedback-csrf-v1", cookie)


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
    if not origins:
        return True
    trusted = _origin(config.PUBLIC_URL, configured=True)
    return len(origins) == 1 and trusted is not None and _origin(origins[0]) == trusted


def _peer(request: Request) -> tuple[str, bool]:
    # X-Forwarded-For не источник личности. За общим/неизвестным прокси лимит строже.
    host = request.client.host if request.client else "unknown"
    try:
        shared = not ipaddress.ip_address(host).is_global
    except ValueError:
        shared = True
    return host, shared or "x-forwarded-for" in request.headers or "forwarded" in request.headers


def _public_page(title: str, content: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} — Навигатор ЛПК</title>
<link rel="stylesheet" href="/public/landing.css?v={version.__version__}"></head><body class="fb-page">
<a class="lp-skip" href="#content">Перейти к содержимому</a>
<header class="lp-head"><a class="lp-brand" href="/" aria-label="Навигатор ЛПК">
<span class="lp-mark" aria-hidden="true">{icon("navigator", 28)}</span><span><b>Навигатор ЛПК</b><small>Обратная связь</small></span></a>
<span class="lp-beta">Бета-тестирование</span><a class="lp-login" href="/">На главную</a></header>
<main id="content" class="fb-main"><section class="fb-card">{content}</section></main></body></html>""",
                        status_code=status, headers=SECURITY)


def _notice(message: str, status: int) -> HTMLResponse:
    return _public_page("Обратная связь", f'<h1 class="fb-title">Обратная связь</h1>'
                        f'<p class="fb-alert" role="alert">{esc(message)}</p>'
                        '<a class="lp-secondary" href="/feedback">Вернуться к форме</a>', status)


def _form_page(secret: bytes, cookie: str, nonce: str, *, kind: str = "bug", body: str = "",
               error: str = "", status: int = 200) -> HTMLResponse:
    choices = "".join(f'<label class="fb-kind"><input type="radio" name="kind" value="{key}"'
                      f'{" checked" if kind == key else ""} required><span>{label}</span></label>'
                      for key, label in KINDS.items())
    alert = f'<p class="fb-alert" role="alert">{esc(error)}</p>' if error else ""
    return _public_page("Обратная связь", f"""<span class="lp-kicker">ПОМОГИТЕ СТАТЬ ЛУЧШЕ</span>
<h1 class="fb-title">Обратная связь</h1><p class="fb-intro">Сообщите об ошибке или предложите улучшение.
Сервис проходит бета-тестирование. Не отправляйте ФИО, контакты, документы и пароли.
Форма анонимная — обратный ответ не предусмотрен. Сообщение увидят только сис-админы.</p>{alert}
<form class="fb-form" method="post" action="/feedback" accept-charset="UTF-8">
<input type="hidden" name="csrf" value="{_csrf(secret, cookie)}">
<input type="hidden" name="submission_nonce" value="{esc(nonce)}">
<fieldset class="fb-kinds"><legend>Тип сообщения</legend>{choices}</fieldset>
<label class="fb-field" for="description">Описание</label>
<textarea id="description" name="description" rows="8" minlength="10" maxlength="4000" required
aria-describedby="description-hint">{esc(body[:4000])}</textarea>
<p id="description-hint" class="fb-hint">От 10 до 4000 символов. Без контактов и вложений.</p>
<div class="fb-honeypot" aria-hidden="true"><label for="website">Оставьте это поле пустым</label>
<input id="website" name="website" type="text" tabindex="-1" autocomplete="off"></div>
<div class="fb-actions"><button class="lp-primary" type="submit">Отправить</button>
<a class="lp-secondary" href="/">На главную</a></div></form>""", status)


class _Rejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def _read_form(request: Request) -> dict[str, str]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/x-www-form-urlencoded" or request.headers.get("content-encoding", "identity") != "identity":
        raise _Rejected(415, "Отправьте обычную текстовую форму без файлов.")
    raw = bytearray()
    # Content-Length может отсутствовать или врать; считаем реальные байты до разбора.
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise _Rejected(413, "Сообщение слишком большое. Сократите его и отправьте снова.")
            raw.extend(chunk)
    except ClientDisconnect:
        raise _Rejected(400, "Отправка прервалась. Попробуйте ещё раз.") from None
    try:
        encoded = raw.decode("utf-8")
        if re.search(r"%(?![0-9a-fA-F]{2})", encoded):
            raise ValueError
        fields = parse_qs(encoded, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=8)
    except (UnicodeError, ValueError):
        raise _Rejected(400, "Не удалось прочитать форму. Обновите страницу.") from None
    allowed = {"kind", "description", "body", "csrf", "submission_nonce", "website"}
    if set(fields) - allowed or any(len(values) != 1 for values in fields.values()) \
            or {"description", "body"} <= set(fields):
        raise _Rejected(400, "Форма содержит лишние или повторяющиеся поля. Обновите страницу.")
    return {key: values[0] for key, values in fields.items()}


@open_router.get("/feedback", include_in_schema=False)
async def public_feedback_form(request: Request):
    secret = _secret()
    if secret is None:
        return _notice("Форма временно недоступна: не настроена безопасная отправка. Попробуйте позже.", 503)
    now = _now()
    cookie = request.cookies.get(FEEDBACK_COOKIE, "")
    expires = _valid_nonce(secret, "public-feedback-cookie-v1", cookie, now)
    if not expires:
        expires = now + FEEDBACK_TTL
        cookie = _nonce(secret, "public-feedback-cookie-v1", expires)
    submission = _nonce(secret, "public-feedback-submission-v1", expires, cookie)
    kind = request.query_params.get("kind", "bug")
    response = _form_page(secret, cookie, submission, kind=kind if kind in KINDS else "bug")
    trusted = _origin(config.PUBLIC_URL, configured=True)
    response.set_cookie(FEEDBACK_COOKIE, cookie, max_age=expires - now, path="/feedback",
                        httponly=True, samesite="lax", secure=request.url.scheme == "https"
                        or bool(trusted and trusted[0] == "https"))
    return response


@open_router.post("/feedback", include_in_schema=False)
async def public_feedback_submit(request: Request):
    secret = _secret()
    if secret is None:
        return _notice("Форма временно недоступна: не настроена безопасная отправка. Попробуйте позже.", 503)
    now = _now()
    cookie = request.cookies.get(FEEDBACK_COOKIE, "")
    cookie_ok = bool(_valid_nonce(secret, "public-feedback-cookie-v1", cookie, now))
    host, shared = _peer(request)
    fields, kind, body, nonce = {}, "", "", ""
    rejected = None
    try:
        if not cookie_ok or not _origin_allowed(request):
            raise _Rejected(403, "Форма устарела или открыта с другого сайта. Обновите страницу.")
        fields = await _read_form(request)
        if not hmac.compare_digest(fields.get("csrf", "").encode("utf-8"), _csrf(secret, cookie).encode("ascii")):
            raise _Rejected(403, "Форма устарела. Обновите страницу перед отправкой.")
        nonce = fields.get("submission_nonce", "")
        if not _valid_nonce(secret, "public-feedback-submission-v1", nonce, now, cookie):
            raise _Rejected(403, "Форма устарела. Обновите страницу перед отправкой.")
        if fields.get("website", ""):
            raise _Rejected(400, "Не удалось отправить форму. Обновите страницу.")
        kind = fields.get("kind", "")
        body = fields.get("description", fields.get("body", "")).strip()
        if kind not in KINDS:
            raise _Rejected(422, "Выберите тип сообщения: ошибка или предложение.")
        if not 10 <= len(body) <= 4000 or "\x00" in body:
            raise _Rejected(422, "Описание должно содержать от 10 до 4000 символов.")
    except _Rejected as exc:
        rejected = exc
    try:
        result, number = await repo.submit_public_feedback(
            now=now, peer_key=_mac(secret, "public-feedback-peer-v1", host),
            cookie_key=_mac(secret, "public-feedback-rate-cookie-v1", cookie) if cookie_ok else "",
            shared_peer=shared, kind=kind if rejected is None else "",
            body=body if rejected is None else "", submission_nonce=nonce if rejected is None else "")
    except sqlite3.Error:
        # Ни исключение с параметрами SQL, ни тело формы не попадают в журнал или ответ.
        return _notice("Сохранение временно недоступно. Попробуйте отправить эту форму позже.", 503)
    if result == "limited":
        response = _notice("Слишком много попыток. Подождите перед следующей отправкой.", 429)
        response.headers["Retry-After"] = str(number)
        return response
    if rejected is not None:
        if rejected.status == 422:
            return _form_page(secret, cookie, nonce, kind=kind if kind in KINDS else "bug",
                              body=body, error=rejected.message, status=422)
        return _notice(rejected.message, rejected.status)
    return RedirectResponse("/feedback/thanks", status_code=303, headers=SECURITY)


@open_router.get("/feedback/thanks", include_in_schema=False)
async def public_feedback_thanks():
    return _public_page("Спасибо за обратную связь", '<div class="fb-success">'
                        '<h1 class="fb-title">Спасибо за обратную связь</h1>'
                        '<p class="fb-intro">Сообщение принято. Команда сервиса рассмотрит его.</p>'
                        '<a class="lp-secondary" href="/">На главную</a></div>')


@router.get("/feedback")
async def public_feedback_list(request: Request):
    user = await require_user(request)
    status = request.query_params.get("status", "")
    page_raw = request.query_params.get("page", "1")
    if status not in ("", *STATUSES) or not re.fullmatch(r"[1-9][0-9]{0,5}", page_raw) \
            or len(request.query_params.getlist("status")) > 1:
        raise HTTPException(400, "Некорректный фильтр обратной связи")
    page_no = int(page_raw)
    total = await repo.count_public_feedback(status)
    rows = await repo.list_public_feedback(status, 50, (page_no - 1) * 50)
    options = "".join(f'<option value="{key}"{" selected" if status == key else ""}>{label}</option>'
                      for key, label in {"": "Все статусы", **STATUSES}.items())
    entries = "".join(f'<tr><td><a href="/panel/feedback/{int(row["id"])}">№{int(row["id"])}</a></td>'
                      f'<td>{esc(KINDS[row["kind"]])}</td><td>{esc(STATUSES[row["status"]])}</td>'
                      f'<td>{esc(row["created_at"])}</td></tr>' for row in rows)
    entries = entries or '<tr><td colspan="4">Сообщений пока нет.</td></tr>'
    links = ""
    for number, label in ((page_no - 1, "Назад"), (page_no + 1, "Далее")):
        if number >= 1 and (number < page_no or page_no * 50 < total):
            url = "/panel/feedback?" + urlencode({"status": status, "page": number})
            links += f'<a class="btn btn-grey" href="{esc(url)}">{label}</a> '
    body = f"""<div class="card"><p>Анонимные сообщения об ошибках и предложения по сервису.
Контактов для ответа нет; сообщения в MAX не отправляются.</p>
<form method="get" action="/panel/feedback"><label for="feedback-status">Статус</label>
<select id="feedback-status" name="status">{options}</select><button>Показать</button></form>
<div class="table-wrap"><table><thead><tr><th>Номер</th><th>Тип</th><th>Статус</th><th>Получено</th></tr></thead>
<tbody>{entries}</tbody></table></div><p>Всего: {total}. Страница {page_no}.</p>{links}</div>"""
    return page("Отзывы о бета-версии", body, user, "/feedback")


@router.get("/feedback/{feedback_id}")
async def public_feedback_detail(request: Request, feedback_id: int):
    user = await require_user(request)
    item = await repo.get_public_feedback(feedback_id)
    if item is None:
        raise HTTPException(404, "Сообщение не найдено")
    options = "".join(f'<option value="{key}"{" selected" if item["status"] == key else ""}>{label}</option>'
                      for key, label in STATUSES.items())
    content = "<br>".join(esc(item["body"]).splitlines())
    body = f"""<div class="card"><p><a href="/panel/feedback">К списку обратной связи</a></p>
<p>{esc(KINDS[item["kind"]])} · {esc(STATUSES[item["status"]])} · {esc(item["created_at"])}</p>
<div class="feedback-body">{content}</div><form method="post" action="/panel/feedback/{feedback_id}/status">
{csrf(request)}<label for="feedback-status">Статус</label><select id="feedback-status" name="status">{options}</select>
<button>Сохранить статус</button></form></div>"""
    return page(f"Обратная связь №{feedback_id}", body, user, "/feedback")


@router.post("/feedback/{feedback_id}/status")
async def public_feedback_status(request: Request, feedback_id: int):
    user = await require_form(request)
    form = await request.form()
    statuses = form.getlist("status")
    if len(statuses) != 1 or statuses[0] not in repo.PUBLIC_FEEDBACK_STATUSES \
            or set(form) - {"csrf", "status"} or any(len(form.getlist(key)) != 1 for key in form):
        raise HTTPException(400, "Некорректный статус обратной связи")
    if not await repo.set_public_feedback_status(feedback_id, statuses[0], user):
        raise HTTPException(404, "Сообщение не найдено")
    # AuditRoute запишет только код действия и feedback_id, не значения формы.
    return RedirectResponse(f"/panel/feedback/{feedback_id}", status_code=303)
