"""Рабочее место сотрудников: отдельные права, те же обращения, без административных данных."""
import csv
import hashlib
import hmac
import io
import logging
import re
import secrets
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

import attachments
import config
import repository as repo
import version
from handlers import common as bot_common
from handlers.tickets import render_template, unknown_placeholders
from panel_theme import STYLESHEET, icon, theme_boot_script, theme_script
from store import workspace as policy
from utils import CATS, STATUS, as_str

from .common import bot_open_link, copy_btn, esc, form_token, page, panel_link, require_form, require_user
from .router import router, workspace_router
from .workspace_auth import WORKSPACE_SECURITY, require_staff_form, require_staff_web, staff_form_token
from .workspace_view import render_workbench

log = logging.getLogger("workspace")
STATIC = Path(__file__).with_name("static")
HEADERS = dict(WORKSPACE_SECURITY)
ACTIONS = frozenset({"reply", "reply_close", "status", "note", "forward", "template_preview"})
FILTER_NAMES = ("tab", "status", "category", "q", "scope", "view", "mine", "date_from", "date_to", "period")
_NOTICES: dict[str, dict] = {}


def _secret() -> bytes:
    value = as_str(config.WEB_SESSION_SECRET)
    if len(value) < 32:
        raise HTTPException(503, "Не настроен безопасный вход")
    return value.encode("utf-8")


def _context(actor: dict, csrf_token: str) -> str:
    audience = "panel" if actor.get("is_admin") else "workspace"
    session = as_str(actor.get("session_hash")) or csrf_token
    return hashlib.sha256((audience + ":" + actor["user_id"] + ":" + session).encode()).hexdigest()


def operation_token(actor: dict, csrf_token: str, ticket_id: int, revision: int) -> str:
    """Ключ формы привязан к текущему сеансу, обращению и его ревизии."""
    payload = f"{ticket_id}.{revision}.{int(time.time()) + 12 * 3600}.{secrets.token_hex(16)}"
    signature = hmac.new(_secret(), ("workspace-operation:" + _context(actor, csrf_token)
                                    + ":" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid_operation(token: str, actor: dict, csrf_token: str, ticket_id: int, revision: int) -> bool:
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,20}\.[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", token):
        return False
    tid, rev, expires, _nonce, signature = token.split(".")
    if int(tid) != ticket_id or int(rev) != revision or not time.time() < int(expires) <= time.time() + 12 * 3600 + 60:
        return False
    payload = token.rsplit(".", 1)[0]
    expected = hmac.new(_secret(), ("workspace-operation:" + _context(actor, csrf_token)
                                   + ":" + payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _notice_put(actor: dict, csrf_token: str, notice: dict) -> None:
    now = time.monotonic()
    for key in [key for key, value in _NOTICES.items() if value["expires"] <= now]:
        _NOTICES.pop(key, None)
    if len(_NOTICES) >= 1000:
        _NOTICES.pop(next(iter(_NOTICES)))
    _NOTICES[_context(actor, csrf_token)] = {**notice, "expires": now + 300}


def _notice_take(actor: dict, csrf_token: str) -> dict:
    item = _NOTICES.pop(_context(actor, csrf_token), {})
    return item if item.get("expires", 0) > time.monotonic() else {}


def _integer(value, *, default: int = 0, maximum: int = 1_000_000_000) -> int:
    raw = as_str(value)
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,12}", raw) or int(raw) > maximum:
        raise HTTPException(400, "Некорректный номер")
    return int(raw)


def workspace_filters(params) -> tuple[dict, dict, int]:
    """Старые адреса с t/scope/view сохраняются, но никогда не задают права."""
    for name in (*FILTER_NAMES, "t", "page", "page_no"):
        if hasattr(params, "getlist") and len(params.getlist(name)) > 1:
            raise HTTPException(400, "Параметр повторяется")
    shown = {name: as_str(params.get(name, "")) for name in FILTER_NAMES}
    tab, view, status, scope = (shown[key] for key in ("tab", "view", "status", "scope"))
    if tab not in ("", "open", "waiting", "closed") or view not in ("", "open", "waiting", "closed", "archive"):
        raise HTTPException(400, "Неизвестный вид очереди")
    if status not in ("", "open", "closed", *STATUS) or scope not in ("", "waiting", "overdue"):
        raise HTTPException(400, "Неизвестный фильтр обращений")
    if len(shown["q"]) > 200:
        raise HTTPException(400, "Поиск слишком длинный")
    if view == "archive":
        effective = "archive"
    elif tab:
        effective = tab
    elif view:
        effective = view
    elif status in ("completed", "rejected", "closed"):
        effective = "closed"
    elif scope == "waiting":
        effective = "waiting"
    else:
        effective = "open"
    shown["view"] = effective
    shown["tab"] = "closed" if effective == "archive" else effective
    shown["mine"] = "1" if shown["mine"].strip().lower() in ("1", "on", "true", "yes") else "0"
    query = {name: value for name, value in shown.items() if name != "tab"}
    page_no = max(1, _integer(params.get("page", params.get("page_no", "1")), default=1, maximum=100_000))
    shown["page"] = page_no
    return query, shown, page_no


def _url(base: str, filters: dict, *, selected: int = 0, standalone: bool = False) -> str:
    values = {key: val for key, val in filters.items() if key in (*FILTER_NAMES, "page") and val not in ("", None)}
    if selected and not standalone:
        values["t"] = selected
    suffix = "/" + str(selected) if selected and standalone else ""
    query = urlencode(values)
    return base + suffix + ("?" + query if query else "")


async def admin_principal(request: Request, *, form: bool = False) -> dict:
    uid = await require_form(request) if form else await require_user(request)
    row = await repo.get_admin(uid)
    actor = dict(row) if row else {}
    actor.update(user_id=uid, is_admin=True, permissions=[], session_hash="")
    return actor


def _check_result(result: dict) -> None:
    code = result.get("result")
    if code == "ok":
        return
    if code == "invalid":
        raise HTTPException(400, as_str(result.get("message")) or "Некорректные данные")
    if code == "denied":
        raise HTTPException(403, "Недостаточно прав для действия")
    raise HTTPException(404, "Обращение не найдено")


def _policy_context(actor: dict) -> dict:
    return {"session_hash": as_str(actor.get("session_hash")), "admin": bool(actor.get("is_admin"))}


def _files_for_view(detail: dict, base: str) -> None:
    tid = int(detail["ticket"]["ticket_id"])
    for item in detail.get("files", []):
        item["filename"] = as_str(item.get("display_name")) or "Вложение"
        item["href"] = f"{base}/{tid}/files/{int(item['id'])}" if item.get("downloadable") else ""
        item["size_label"] = f"{max(0, int(item.get('size', 0))) // 1024} КБ"
    if "templates" in detail.get("allowed_actions", []):
        detail["allowed_actions"] = [*detail["allowed_actions"], "template_preview"]
    detail["allowed_statuses"] = list(STATUS)
    detail["ticket"]["archived"] = bool(detail.get("archived"))


def _workspace_shell(title: str, body: str, actor: dict, csrf_token: str, *, status_code: int = 200) -> HTMLResponse:
    uid = as_str(actor["user_id"])
    name = as_str(actor.get("full_name")) or uid
    position = as_str(actor.get("position"))
    return HTMLResponse(f"""<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark light">
<title>{esc(title)} — Навигатор ЛПК</title><script>{theme_boot_script()}</script><style>{STYLESHEET}</style>
<link rel="stylesheet" href="/workspace/assets/workspace.css?v={version.__version__}"></head>
<body class="ws-app" data-panel-user="{esc(uid)}"><a class="skip-link" href="#workspace-main">Перейти к обращениям</a>
<header class="ws-app-header"><a class="ws-brand" href="/workspace/tickets">{icon('navigator', 27)}
<span><b>Навигатор ЛПК</b><small>Кабинет сотрудника · Бета-тестирование</small></span></a>
<div class="ws-account"><span class="ws-account-name"><b>{esc(name)}</b><small>{esc(position)}</small></span>
<button type="button" class="theme-toggle" aria-label="Сменить тему"></button>
<form method="post" action="/workspace/logout"><input type="hidden" name="csrf" value="{esc(csrf_token)}"><button class="ws-btn ws-btn-quiet" type="submit">{icon('logout', 18)} Выйти</button></form></div></header>
<main id="workspace-main" class="ws-app-main">{body}</main>
<script>{theme_script()}</script><script src="/workspace/assets/workspace.js?v={version.__version__}" defer></script></body></html>""", status_code=status_code, headers=HEADERS)


async def render_workspace(request: Request, actor: dict, *, selected: int = 0, standalone: bool = False,
                           draft_text: str = "", draft_mode: str = "reply", notice: dict | None = None,
                           status_code: int = 200, extra_admin_html: str = "", filter_params=None) -> HTMLResponse:
    filters, shown, page_no = workspace_filters(request.query_params if filter_params is None else filter_params)
    uid = actor["user_id"]
    context = _policy_context(actor)
    queue = await policy.workspace_list(uid, filters=filters, page=page_no, **context)
    _check_result(queue)
    csrf_token = form_token(request) if actor.get("is_admin") else staff_form_token(request)
    actor = {**actor, "permissions": queue.get("permissions", actor.get("permissions", [])),
             "can_export": bool(queue.get("can_export")), "can_view_archive": True,
             "restore_filters": filter_params is None and not request.query_params,
             "csrf_token": csrf_token}
    detail = None
    base = "/panel/tickets" if actor.get("is_admin") else "/workspace/tickets"
    stored_notice = _notice_take(actor, csrf_token)
    info = notice or stored_notice.get("notice")
    operation_key = ""
    if selected:
        detail = await policy.workspace_ticket(uid, selected, **context)
        _check_result(detail)
        _files_for_view(detail, base)
        public_base = as_str(config.PUBLIC_URL).rstrip("/")
        detail["share_url"] = public_base + f"{base}/{selected}" if public_base else f"{base}/{selected}"
        detail["bot_url"] = await bot_open_link()
        detail["draft_text"] = draft_text
        detail["draft_mode"] = draft_mode
        detail["notice"] = info
        detail["extra_admin_html"] = extra_admin_html if actor.get("is_admin") else ""
        if actor.get("is_admin"):
            detail["share_url"] = panel_link(f"/tickets?t={selected}")
            detail["extra_admin_html"] += ('<p>MAX ID студента: ' + esc(detail["ticket"]["student_id"])
                                           + copy_btn(detail["ticket"]["student_id"], "MAX ID студента") + '</p>')
            admin_query = urlencode({key: val for key, val in shown.items() if val not in ("", None)})
            detail["admin_links"] = [{"href": f"/panel/tickets/{selected}/admin?{admin_query}",
                                      "label": "Администрирование обращения"}]
        if stored_notice.get("ticket_id") == selected:
            detail["action_success"] = stored_notice.get("action_success")
        operation_key = operation_token(actor, csrf_token, selected, int(detail["revision"]))
    if stored_notice.get("action_success"):
        queue["action_success"] = stored_notice["action_success"]
    if info:
        queue["notice"] = info
    body = render_workbench(base_path=base, actor=actor, queue=queue, detail=detail, filters=shown,
                            csrf_token=csrf_token, operation_key=operation_key, standalone=standalone)
    title = f"Обращение №{selected}" if standalone and selected else "Рабочее место"
    if actor.get("is_admin"):
        actions = '<a class="btn btn-grey" href="/panel/staff-access">' + icon("lock", 18) + ' Права сотрудников</a>'
        response = page(title, body, uid, "/tickets", actions=actions, status_code=status_code,
                        extra_head=f'<link rel="stylesheet" href="/workspace/assets/workspace.css?v={version.__version__}">',
                        extra_scripts=f'<script src="/workspace/assets/workspace.js?v={version.__version__}" defer></script>')
        response.headers.update(HEADERS)
        return response
    return _workspace_shell(title, body, actor, csrf_token, status_code=status_code)


@workspace_router.get("/assets/{name}", include_in_schema=False)
async def workspace_asset(name: str):
    types = {"workspace.css": "text/css; charset=utf-8", "workspace.js": "text/javascript; charset=utf-8"}
    if name not in types:
        return Response(status_code=404)
    return FileResponse(STATIC / name, media_type=types[name], headers={
        "Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    })


@workspace_router.get("/", include_in_schema=False)
async def workspace_home(request: Request):
    await require_staff_web(request)
    return RedirectResponse("/workspace/tickets", status_code=303, headers=HEADERS)


@workspace_router.get("/tickets")
async def staff_tickets(request: Request):
    actor = await require_staff_web(request)
    return await render_workspace(request, actor, selected=_integer(request.query_params.get("t", "")))


@workspace_router.get("/tickets/{ticket_id}")
async def staff_ticket(request: Request, ticket_id: int):
    actor = await require_staff_web(request)
    return await render_workspace(request, actor, selected=ticket_id, standalone=True)


def _csv_value(value) -> str:
    text = as_str(value)
    significant = text
    while significant and (significant[0].isspace() or unicodedata.category(significant[0]) in ("Cc", "Cf")):
        significant = significant[1:]
    return "'" + text if significant.startswith(("=", "+", "-", "@")) else text


@workspace_router.get("/tickets.csv")
async def staff_tickets_csv(request: Request):
    actor = await require_staff_web(request)
    filters, _shown, _page = workspace_filters(request.query_params)
    result = await policy.workspace_list(actor["user_id"], filters=filters, page=1, page_size=2000,
                                         for_export=True, **_policy_context(actor))
    _check_result(result)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Номер", "Создано", "Статус", "Раздел", "Студент", "Группа", "Сотрудник", "Тема", "Обращение"])
    for row in result["rows"]:
        writer.writerow([_csv_value(value) for value in (
            row["ticket_id"], row["created_at"], STATUS.get(row["status"], row["status"]),
            CATS.get(row["category"], row["category"]), row.get("student_name", ""), row.get("student_group", ""),
            row.get("staff_name", ""), row.get("topic", ""), row.get("text_content", ""),
        )])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={**HEADERS, "Content-Disposition": 'attachment; filename="my-tickets.csv"'})


async def download_workspace_file(request: Request, actor: dict, ticket_id: int, file_id: int):
    result = await policy.workspace_attachment(actor["user_id"], ticket_id, file_id, **_policy_context(actor))
    _check_result(result)
    item = result["file"]
    name = as_str(item["storage_name"])
    base = Path(attachments.attachments_dir()).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Вложение не найдено")
    return FileResponse(path, filename=as_str(item.get("display_name")) or name,
                        headers={**HEADERS, "Content-Security-Policy": "sandbox; default-src 'none'"})


@workspace_router.get("/tickets/{ticket_id}/files/{file_id}")
async def staff_file(request: Request, ticket_id: int, file_id: int):
    return await download_workspace_file(request, await require_staff_web(request), ticket_id, file_id)


@router.get("/tickets/{ticket_id}/files/{file_id}")
async def admin_workspace_file(request: Request, ticket_id: int, file_id: int):
    return await download_workspace_file(request, await admin_principal(request), ticket_id, file_id)


async def _read_action(request: Request) -> dict:
    data = await request.form()
    allowed = {"csrf", "operation_key", "revision", "action", "text", "status", "target_id", "template_id", "return"}
    if set(data) - allowed or any(len(data.getlist(key)) != 1 for key in data):
        raise HTTPException(400, "Форма содержит неизвестные или повторяющиеся поля")
    values = {key: as_str(data.get(key, "")) for key in allowed}
    if values["action"] not in ACTIONS:
        raise HTTPException(400, "Неизвестное действие")
    if len(values["text"]) > 4000 or "\x00" in values["text"]:
        raise HTTPException(400, "Текст должен содержать не больше 4000 символов")
    return values


async def _deliver(result: dict) -> bool:
    """Сбой отправки не откатывает сохранённый ответ и не маскируется успехом."""
    failed = False
    for item in result.get("notifications", []):
        uid = as_str(item.get("recipient_id"))
        if not uid:
            continue
        tid = int(item["ticket_id"])
        who = as_str(item.get("actor_name")) or "Сотрудник колледжа"
        position = as_str(item.get("actor_position"))
        if position:
            who += ", " + position
        kind = item.get("kind", "")
        text = as_str(item.get("text"))
        if kind in ("reply", "reply_close", "message"):
            text = f"💬 Ответ по обращению №{tid}\nОтвечает: {who}\n\n{text}"
            if kind == "reply_close":
                text += "\n\n✅ Обращение завершено."
        elif kind == "forward":
            text = f"↪️ Вам передано обращение №{tid}.\nПередал: {who}\n\n{text}"
        elif kind == "status":
            status = as_str(item.get("status"))
            text = f"🔔 Статус обращения №{tid}: {STATUS.get(status, status)}"
        elif not text:
            text = f"Обновление по обращению №{tid}. Откройте карточку, чтобы увидеть изменения."
        try:
            await bot_common.api.send(uid, text, [[{"type": "callback", "text": "📂 Открыть", "payload": f"t:{tid}"}]])
        except Exception as exc:  # текст, адресат и исключение с телом запроса не журналируются
            failed = True
            log.warning("Не доставлено уведомление рабочего места, обращение %s: %s", tid, type(exc).__name__)
    return failed


def _return_context(raw: str, base: str, ticket_id: int) -> tuple[dict, bool]:
    if not raw:
        return {}, True
    decoded = unquote(raw)
    if len(raw) > 4000 or "\\" in decoded or any(ord(char) < 32 for char in decoded):
        raise HTTPException(400, "Некорректный адрес возврата")
    target = urlsplit(raw)
    if target.scheme or target.netloc or target.fragment or target.path not in (base, f"{base}/{ticket_id}"):
        raise HTTPException(400, "Некорректный адрес возврата")
    try:
        values = parse_qs(target.query, keep_blank_values=True, max_num_fields=20)
    except ValueError as exc:
        raise HTTPException(400, "Некорректные фильтры возврата") from exc
    if any(len(value) != 1 for value in values.values()) or set(values) - {*FILTER_NAMES, "page", "t"}:
        raise HTTPException(400, "Некорректные фильтры возврата")
    clean = {key: value[0] for key, value in values.items() if key != "t"}
    return clean, target.path != base


async def workspace_action(request: Request, actor: dict, ticket_id: int) -> Response:
    values = await _read_action(request)
    action = values["action"]
    base = "/panel/tickets" if actor.get("is_admin") else "/workspace/tickets"
    return_params, standalone = _return_context(values["return"], base, ticket_id)
    workspace_filters(return_params)
    csrf_token = form_token(request) if actor.get("is_admin") else staff_form_token(request)
    revision = _integer(values["revision"], maximum=9_999_999_999)
    if not valid_operation(values["operation_key"], actor, csrf_token, ticket_id, revision):
        raise HTTPException(403, "Форма устарела. Откройте обращение заново.")
    request.state.audit_action = "workspace_" + action
    context = _policy_context(actor)
    uid = actor["user_id"]
    template_id = _integer(values["template_id"])
    if action == "template_preview":
        result = await policy.workspace_template_preview(uid, ticket_id, template_id, **context)
        _check_result(result)
        answerer = await repo.get_admin(uid)
        text = await render_template(as_str(result["template"]["text"]), result["ticket"], answerer)
        request.state.audit_result = "received"
        if request.headers.get("accept", "").startswith("text/plain"):
            return PlainTextResponse(text, headers=HEADERS)
        return await render_workspace(request, actor, selected=ticket_id, standalone=standalone,
                                      draft_text=text, filter_params=return_params,
                                      notice={"kind": "info", "text": "Шаблон вставлен в черновик. Проверьте текст перед отправкой."})
    text = values["text"].strip()
    if action in ("reply", "reply_close"):
        selected = await policy.workspace_ticket(uid, ticket_id, **context)
        _check_result(selected)
        answerer = await repo.get_admin(uid)
        raw = text
        text = await render_template(raw, selected["ticket"], answerer)
        if unknown_placeholders(raw):
            request.state.audit_result = "denied"
            return await render_workspace(request, actor, selected=ticket_id, standalone=standalone,
                                          draft_text=raw, status_code=400, filter_params=return_params,
                                          notice={"kind": "error", "text": "В ответе остались неизвестные подстановки. Исправьте их перед отправкой."})
    result = await policy.workspace_apply(uid, ticket_id, action, expected_revision=revision,
                                          operation_key=values["operation_key"], text=text, status=values["status"],
                                          target_id=values["target_id"], template_id=template_id, **context)
    code = result.get("result")
    if code in ("conflict", "invalid", "denied"):
        request.state.audit_result = "denied"
        message = {"conflict": "Обращение изменилось, пока вы работали. Проверьте обновлённую карточку; текст не отправлен.",
                   "invalid": as_str(result.get("message")) or "Проверьте поля формы.",
                   "denied": "Права на это действие изменились. Текст не отправлен."}[code]
        return await render_workspace(request, actor, selected=ticket_id, standalone=standalone,
                                      filter_params=return_params, draft_text=values["text"],
                                      draft_mode=action if action in ("note", "forward", "status") else "reply",
                                      notice={"kind": "error", "text": message}, status_code=409 if code == "conflict" else 400 if code == "invalid" else 403)
    if code not in ("ok", "replay"):
        _check_result(result)
    failed = await _deliver(result) if code == "ok" else False
    labels = {"reply": "Ответ сохранён и отправлен в MAX.", "reply_close": "Ответ сохранён, обращение завершено; уведомление отправлено в MAX.",
              "status": "Статус сохранён.", "note": "Внутренняя заметка сохранена. Студент её не видит.", "forward": "Обращение передано сотруднику."}
    message = "Это действие уже выполнено; повторной отправки не было." if code == "replay" else labels[action]
    if failed:
        message = "Изменение сохранено, но доставка уведомления в MAX не подтверждена. Не отправляйте ответ повторно: он уже в истории."
    receipt = {"notice": {"kind": "warning" if failed else "success", "text": message}, "ticket_id": ticket_id,
               "action_success": {"ticket_id": ticket_id, "action": action, "operation_key": values["operation_key"]}}
    _notice_put(actor, csrf_token, receipt)
    _filters, shown, _page = workspace_filters(return_params)
    location = _url(base, shown, selected=0 if action == "forward" else ticket_id,
                    standalone=standalone and action != "forward")
    return RedirectResponse(location, status_code=303, headers=HEADERS)


@workspace_router.post("/tickets/{ticket_id}/action")
async def staff_ticket_action(request: Request, ticket_id: int):
    actor = await require_staff_form(request)
    return await workspace_action(request, actor, ticket_id)


@router.post("/tickets/{ticket_id}/action")
async def admin_ticket_action(request: Request, ticket_id: int):
    actor = await admin_principal(request, form=True)
    return await workspace_action(request, actor, ticket_id)
