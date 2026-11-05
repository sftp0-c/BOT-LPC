"""Чистая HTML-разметка рабочего места сотрудников и сис-администраторов.

Оболочку и доступ проверяет вызывающий маршрут. Здесь нет запросов, сессий и
побочных действий: sqlite3.Row заранее превращаются в обычные словари.

Родитель подключает panel_theme.STYLESHEET, theme_boot_script()/theme_script()
и assets из workspace_assets(). actions_script() умеет обновлять .live-age;
скрипт рабочего места не подключает глобальный поиск или навигацию сотруднику.

Дополнения к основному контракту detail:
* draft_text/draft_mode — введённый текст после ошибки или предпросмотра;
* allowed_statuses — допустимые значения селектора статуса;
* notice/notices — безопасные сообщения {kind, text}; conflict — конфликт;
* action_success — ТОЛЬКО серверный маркер успешного POST после PRG:
  {ticket_id, action, operation_key}. query-параметр успехом не считается;
* extra_admin_html — доверенная, уже защищённая разметка родителя;
  admin_links — локальные ссылки {href, label}. Оба видны только admin.
Маркер успеха и админ-дополнения допустимы также в queue без открытой карточки.
Шаблон передаётся как template_id; POST action=template_preview возвращает
страницу с draft_text без JS, а с Accept: text/plain — подставленный текст.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from html import escape
import re
from urllib.parse import unquote, urlencode, urlsplit

import clock
from panel_theme import icon
from utils import CATS, STATUS, fmt_when, person_label


WORKSPACE_STYLESHEET_HREF = "/workspace/assets/workspace.css"
WORKSPACE_SCRIPT_SRC = "/workspace/assets/workspace.js"
ACTIONS = frozenset({"reply", "reply_close", "status", "note", "forward", "template_preview"})
FILTER_KEYS = ("tab", "q", "status", "category", "scope", "view", "mine", "date_from", "date_to")
TABS = (("open", "Открытые"), ("waiting", "Ждут ответа"), ("closed", "Закрытые"))
STATUS_LABELS = {code: re.sub(r"^[^\w]+", "", label) for code, label in STATUS.items()}
STATUS_LABELS["ready"] = "К выдаче"
STATUS_LABELS["closed"] = "Закрыто"
CATEGORY_LABELS = {code: re.sub(r"^[^\w]+", "", label) for code, label in CATS.items()}
EVENT_LABELS = {
    "created": "Обращение создано", "status": "Статус изменён",
    "ready": "Документ к выдаче", "forward": "Обращение передано",
    "forwarded": "Обращение передано", "assigned": "Исполнитель назначен",
    "assign": "Исполнитель назначен", "archive": "Обращение в архиве",
    "archived": "Обращение в архиве", "restore": "Обращение восстановлено",
    "restored": "Обращение восстановлено", "reopened": "Обращение открыто снова",
    "message_student": "Сообщение студента", "message_staff": "Ответ сотрудника",
    "reply": "Ответ сотрудника", "reply_close": "Ответ и закрытие обращения",
    "closed": "Обращение закрыто", "edited": "Карточка изменена",
}


def workspace_assets() -> tuple[str, str]:
    """Отдельно head и конец body; рендерер не вставляет ресурсы сам."""
    return (
        f'<link rel="stylesheet" href="{WORKSPACE_STYLESHEET_HREF}">',
        f'<script src="{WORKSPACE_SCRIPT_SRC}" defer></script>',
    )


def _text(value) -> str:
    return "" if value is None else str(value)


def _esc(value) -> str:
    return escape(_text(value), quote=True)


def _pick(row: Mapping, *keys: str, default=""):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _number(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _flag(value) -> bool:
    return _text(value).strip().lower() in {"1", "true", "yes", "on"}


def _rows(value) -> list[Mapping]:
    return [row for row in (value or []) if isinstance(row, Mapping)]


def _id(row: Mapping) -> str:
    value = _text(_pick(row, "ticket_id", "id"))
    return value if re.fullmatch(r"[1-9][0-9]*", value) else ""


def _local_url(value) -> str:
    """Не пропускаем схемы, внешние хосты, обратные слеши и управляющие знаки."""
    value = _text(value)
    decoded = unquote(value)
    if not value.startswith("/") or decoded.startswith("//"):
        return ""
    if "\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if not parts.scheme and not parts.netloc else ""


def _is_admin(actor: Mapping) -> bool:
    return _flag(actor.get("is_admin"))


def _archive_allowed(actor: Mapping) -> bool:
    permissions = set(actor.get("permissions") or [])
    return (_is_admin(actor) or _flag(actor.get("can_view_archive"))
            or bool(permissions & {"archive", "view_archive", "tickets.archive", "tickets:archive"}))


def _filters(filters: Mapping, actor: Mapping) -> dict[str, str]:
    result = {key: _text(filters.get(key)) for key in FILTER_KEYS}
    if result["tab"] not in {key for key, _ in TABS}:
        result["tab"] = ("waiting" if result["scope"] == "waiting" else
                         "closed" if result["status"] in {"closed", "completed", "rejected"} else "open")
    if not _archive_allowed(actor):
        result["view"] = ""
    return result


def _url(base: str, params: Mapping, **changes) -> str:
    values = {key: value for key, value in dict(params, **changes).items()
              if key in (*FILTER_KEYS, "page", "t") and value is not None and value != ""}
    query = urlencode(values)
    return base + ("?" + query if query else "")


def _hidden(name: str, value) -> str:
    return f'<input type="hidden" name="{name}" value="{_esc(value)}">'


def _time(stamp) -> str:
    moment = clock.parse(stamp)
    if moment is None:
        return '<span class="ws-muted">Время не указано</span>'
    aware = moment.replace(tzinfo=clock.zone())
    return (f'<time datetime="{_esc(aware.isoformat())}" '
            f'title="{_esc(moment.strftime("%d.%m.%Y %H:%M"))}">{_esc(fmt_when(stamp))}</time>')


def _timer(ticket: Mapping, actor: Mapping) -> str:
    status = _text(ticket.get("status"))
    if status == "in_progress":
        kind, label = "in_progress", "в работе"
        stamp = _pick(ticket, "in_progress_at", "status_changed_at")
    elif _flag(ticket.get("is_waiting")):
        kind, label = "waiting", "ждёт ответа"
        stamp = _pick(ticket, "waiting_since", "latest_student_message_at", "latest_message_at")
    elif ticket.get("status_changed_at"):
        kind, label, stamp = "status", "статус обновлён", ticket["status_changed_at"]
    else:
        kind, label, stamp = "message", "последнее сообщение", ticket.get("latest_message_at")
    moment = clock.parse(stamp)
    if moment is None:
        return (f'<span class="ws-age ws-muted">{_esc(label)} · время не указано</span>'
                if kind in {"waiting", "in_progress"} else "")
    aware = moment.replace(tzinfo=clock.zone())
    return (f'<span class="ws-age live-age" data-time="{_esc(aware.isoformat())}" '
            f'data-time-ms="{int(aware.timestamp() * 1000)}" data-prefix="{_esc(label)}" '
            f'data-timer-kind="{kind}" data-principal-id="{_esc(actor.get("user_id"))}">'
            f'{_esc(label)} · {_esc(fmt_when(stamp))}</span>')


def _status(ticket: Mapping) -> str:
    status = _text(ticket.get("status"))
    tone = {"new": "new", "accepted": "active", "in_progress": "active",
            "ready": "ready", "completed": "closed", "closed": "closed", "rejected": "closed"}.get(status, "neutral")
    label = STATUS_LABELS.get(status, "Закрыто" if status == "closed" else status or "Без статуса")
    return f'<span class="ws-status ws-status-{tone}">{_esc(label)}</span>'


def _count(counts: Mapping, key: str) -> str:
    if key not in counts or counts[key] is None:
        return ""
    return f'<span class="ws-count">{_number(counts[key])}</span>'


def _options(options: Mapping, selected: str) -> str:
    choices = dict(options)
    if selected and selected not in choices:
        choices[selected] = selected
    return "".join(f'<option value="{_esc(key)}"{" selected" if _text(key) == selected else ""}>'
                   f'{_esc(label)}</option>' for key, label in choices.items())


def _filter_form(base: str, params: Mapping, actor: Mapping, counts: Mapping) -> str:
    active = sum(bool(params.get(key)) and params.get(key) not in {"0", "open", "waiting", "closed"}
                 for key in ("category", "status", "date_from", "date_to", "view", "mine", "scope"))
    summary = f'<span class="ws-count">{active}</span>' if active else ""
    extra = ""
    saved_attrs = (' data-filters="status,category,q,scope,view,mine,date_from,date_to"'
                   f' data-filter-key="tickets:{_esc(actor.get("user_id"))}"' if _is_admin(actor) else "")
    if _is_admin(actor) and actor.get("restore_filters"):
        saved_attrs += ' data-filters-saved="1"'
    reset_attrs = ' data-filters-reset' if _is_admin(actor) else ""
    overdue = params.get("scope") == "overdue"
    overdue_url = _url(base, params, scope="" if overdue else "overdue", page=1)
    overdue_link = (f'<a class="ws-btn ws-filter-overdue {"ws-btn-primary" if overdue else "ws-btn-quiet"}" '
                    f'href="{_esc(overdue_url)}"' + (' aria-current="true"' if overdue else "")
                    + f'>{icon("clock", 16)} {"Все сроки" if overdue else "Дольше суток"}{_count(counts, "overdue")}</a>')
    if _archive_allowed(actor):
        extra += ('<label class="ws-field"><span>Расположение</span><select class="ws-input" name="view">'
                  + _options({"": "Текущие обращения", "archive": "Архив"}, "archive" if params["view"] == "archive" else "") + '</select></label>')
    if _is_admin(actor):
        # Прежний actions_script сохраняет value, но не checked: здесь нужен select.
        extra += ('<label class="ws-field"><span>Исполнитель</span><select class="ws-input" name="mine">'
                  + _options({"": "Все доступные", "1": "Только мои обращения"},
                             "1" if _flag(params.get("mine")) else "") + '</select></label>')
    elif params.get("mine"):
        extra += _hidden("mine", params["mine"])
    return (
        f'<form class="ws-filters" method="get" action="{base}" role="search" data-ws-filter-form{saved_attrs}>'
        + _hidden("tab", params["tab"]) + _hidden("scope", params["scope"]) + _hidden("page", "1")
        + '<label class="ws-label" for="ws-search">Поиск обращений</label><div class="ws-search-row">'
        f'<input id="ws-search" class="ws-input" type="search" name="q" value="{_esc(params["q"])}" '
        'placeholder="Номер, фамилия или текст" autocomplete="off">'
        f'<button class="ws-btn ws-btn-quiet" type="submit" aria-label="Найти обращения">{icon("search")}<span>Найти</span></button></div>'
        f'<details class="ws-filters-advanced"><summary>{icon("filter", 16)} Фильтры {summary}'
        f'<span class="ws-disclosure-icon">{icon("chevron-down", 16)}</span></summary>'
        '<div class="ws-filter-fields"><label class="ws-field"><span>Раздел</span>'
        '<select class="ws-input" name="category">'
        + _options({"": "Все разделы", **CATEGORY_LABELS}, params["category"]) + '</select></label>'
        '<label class="ws-field"><span>Статус</span><select class="ws-input" name="status">'
        + _options({"": "Все статусы вкладки", **STATUS_LABELS}, params["status"]) + '</select></label>'
        f'<label class="ws-field"><span>С даты</span><input class="ws-input" type="date" name="date_from" '
        f'value="{_esc(params["date_from"])}"></label><label class="ws-field"><span>По дату</span>'
        f'<input class="ws-input" type="date" name="date_to" value="{_esc(params["date_to"])}"></label>'
        + extra + f'</div><div class="ws-filter-shortcuts">{overdue_link}</div>'
        '<div class="ws-filter-actions"><button class="ws-btn" type="submit">Применить</button>'
        f'<a class="ws-btn ws-btn-quiet" href="{base}"{reset_attrs}>Сбросить</a></div></details></form>'
    )


def _queue_row(base: str, params: Mapping, row: Mapping, actor: Mapping, page: int, selected: str) -> str:
    ticket_id = _id(row)
    if not ticket_id:
        return ""
    current = ticket_id == selected
    name = _pick(row, "student_name", "author_name", "full_name", default=person_label("", _text(row.get("student_id"))) or "Автор обращения")
    group = _pick(row, "student_group", "group_code")
    category = _text(row.get("category"))
    handler = _pick(row, "staff_name", "handler_name", "assigned_name", default="Не назначен")
    preview = " ".join(_text(_pick(row, "text_content", "text", "preview")).split())
    if len(preview) > 150:
        preview = preview[:147].rsplit(" ", 1)[0] + "…"
    attention = ('<span class="ws-waiting">Ждёт ответа</span>' if _flag(row.get("is_waiting")) else "")
    if _flag(row.get("is_overdue")):
        attention = '<span class="ws-waiting ws-overdue">Дольше суток</span>'
    check = (f'<label class="ws-bulk-check"><input type="checkbox" name="tids" value="{ticket_id}" '
             f'form="ws-bulk-form"><span class="ws-sr-only">Отметить обращение №{ticket_id}</span></label>'
             if _is_admin(actor) else "")
    return (
        f'<li class="ws-ticket-entry">{check}<a class="ws-ticket-row{" ws-ticket-current" if current else ""}" '
        f'href="{_esc(_url(base, params, page=page, t=ticket_id))}" data-ws-ticket-link '
        + ('aria-current="true" ' if current else '')
        + f'aria-label="Обращение №{ticket_id}, {_esc(name)}">'
        f'<span class="ws-ticket-top"><span class="ws-ticket-number">№ {ticket_id}</span>{_status(row)}</span>'
        f'<strong class="ws-person-name">{_esc(name)}</strong>'
        f'<span class="ws-ticket-category">{_esc(CATEGORY_LABELS.get(category, category))}'
        + (f'<span class="ws-meta-dot" aria-hidden="true">·</span><span class="ws-queue-group">{_esc(group)}</span>' if group else '') + '</span>'
        + (f'<span class="ws-preview">{_esc(preview)}</span>' if preview else '')
        + f'<span class="ws-handler">{icon("staff", 16)}<span>Ведёт: {_esc(handler)}</span></span>'
        f'<span class="ws-ticket-bottom">{attention}{_timer(row, actor)}</span></a></li>'
    )


def _pager(base: str, params: Mapping, page: int, page_size: int, total: int, selected: str) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    if pages == 1 and page == 1:
        return ""
    previous = (f'<a class="ws-btn ws-btn-quiet ws-page-arrow" rel="prev" '
                f'href="{_esc(_url(base, params, page=min(page - 1, pages), t=selected))}" '
                f'aria-label="Предыдущая страница">{icon("chevron-left")}</a>' if page > 1 else
                f'<span class="ws-btn ws-btn-quiet ws-page-arrow" aria-disabled="true">{icon("chevron-left")}</span>')
    following = (f'<a class="ws-btn ws-btn-quiet ws-page-arrow" rel="next" '
                 f'href="{_esc(_url(base, params, page=page + 1, t=selected))}" '
                 f'aria-label="Следующая страница">{icon("chevron-right")}</a>' if page < pages else
                 f'<span class="ws-btn ws-btn-quiet ws-page-arrow" aria-disabled="true">{icon("chevron-right")}</span>')
    return (f'<div class="ws-pagination" role="navigation" aria-label="Страницы обращений">{previous}'
            f'<span>Страница <b>{page}</b> из {pages}</span>{following}</div>')


def _queue(base: str, queue: Mapping, params: Mapping, actor: Mapping, selected: str) -> str:
    rows = _rows(queue.get("rows"))
    total = _number(queue.get("total"))
    page, page_size = max(1, _number(queue.get("page"), 1)), max(1, _number(queue.get("page_size"), 25))
    counts = queue.get("counts") if isinstance(queue.get("counts"), Mapping) else {}
    tabs = "".join(
        f'<a class="ws-tab{" ws-tab-current" if params["tab"] == code else ""}" '
        f'href="{_esc(_url(base, params, tab=code, status="", scope="", view="", page=1))}" '
        + ('aria-current="page" ' if params["tab"] == code else '')
        + f'data-ws-tab="{code}"><span>{label}</span>{_count(counts, code)}</a>' for code, label in TABS
    )
    items = "".join(_queue_row(base, params, row, actor, page, selected) for row in rows)
    start = (page - 1) * page_size + 1
    if items and total and start <= total:
        summary = f'{start}–{min(start + len(rows) - 1, total)} из {total}'
    else:
        summary = f'Всего {total}'
    empty = ('<div class="ws-empty ws-queue-empty">' + icon("search", 28)
             + '<h3>Ничего не найдено</h3><p>Попробуйте другой запрос или сбросьте фильтры.</p>'
             f'<a class="ws-btn ws-btn-quiet" href="{base}">Сбросить фильтры</a></div>')
    if not any(params.get(key) not in {"", "open", "0"} for key in ("q", "category", "status", "scope", "view", "mine", "date_from", "date_to")):
        empty = ('<div class="ws-empty ws-queue-empty">' + icon("inbox", 28)
                 + '<h3>Здесь пока нет обращений</h3><p>Новые обращения появятся в очереди после поступления.</p></div>')
    if total and not items:
        empty = ('<div class="ws-empty ws-queue-empty"><h3>На этой странице нет обращений</h3>'
                 f'<a class="ws-btn ws-btn-quiet" href="{_esc(_url(base, params, page=1))}">На первую страницу</a></div>')
    return (
        '<aside class="ws-queue" aria-labelledby="ws-queue-title">'
        '<div class="ws-queue-heading"><h2 id="ws-queue-title">Очередь</h2>'
        f'<span class="ws-range">{summary}</span></div>'
        f'<div class="ws-tabs" role="navigation" aria-label="Состояние обращений">{tabs}</div>'
        + _filter_form(base, params, actor, counts)
        + _bulk_admin(base, rows, params, actor, _text(actor.get("csrf_token")))
        + (f'<ol class="ws-ticket-list">{items}</ol>' if items else empty)
        + _pager(base, params, page, page_size, total, selected) + '</aside>'
    )


def _bulk_admin(base: str, rows: list[Mapping], params: Mapping, actor: Mapping, csrf_token: str) -> str:
    if not _is_admin(actor) or not rows:
        return ""
    ids = ",".join(_id(row) for row in rows if _id(row))
    return_to = _url(base, params)
    form = (
        '<form id="ws-bulk-form" class="ws-bulk-admin" method="post" action="/panel/tickets/bulk">'
        + _hidden("csrf", csrf_token) + _hidden("return", return_to) + _hidden("all", ids)
        + '<input type="hidden" name="tids" value="">'
        '<details><summary>' + icon("more", 18) + ' Групповые действия</summary>'
        '<div class="ws-bulk-fields"><label class="ws-field"><span>Действие</span>'
        '<select class="ws-input" name="action">'
        '<option value="assign">Назначить сотрудника</option><option value="status">Сменить статус</option>'
        '<option value="pickup">Кабинет выдачи</option><option value="archive">В архив</option></select></label>'
        '<label class="ws-field"><span>Куда</span><input class="ws-input" name="value" '
        'placeholder="MAX ID, статус или кабинет"></label><button class="ws-btn" type="submit">Применить к отмеченным</button>'
        '</div></details></form>'
    )
    return form


def _delivery(row: Mapping) -> str:
    state = _text(_pick(row, "delivery_status", "delivery_state", default="unknown")).lower()
    if _text(row.get("delivery_code")) == "403":
        state = "forbidden"
    labels = {"pending": "Ожидает отправки", "sending": "Отправляется",
              "sent": "Отправлено в MAX", "delivered": "Доставлено",
              "failed": "Не доставлено", "forbidden": "Доставка недоступна (403)",
              "blocked": "Доставка недоступна", "unknown": "Доставка не подтверждена"}
    tone = "error" if state in {"failed", "forbidden", "blocked"} else "ok" if state == "delivered" else "muted"
    symbol = "warning" if tone == "error" else "check" if state in {"sent", "delivered"} else "clock"
    return (f'<span class="ws-delivery ws-delivery-{tone}">{icon(symbol, 16)}'
            f'{labels.get(state, labels["unknown"])}</span>')


def _is_note(row: Mapping) -> bool:
    return (_flag(row.get("is_internal")) or _flag(row.get("internal"))
            or _text(_pick(row, "kind", "event", "type")) in {"note", "internal_note", "staff_note"}
            or _text(_pick(row, "sender_role", "role")) in {"note", "internal", "staff_note"})


def _message(row: Mapping, initial: bool = False) -> str:
    note = _is_note(row)
    role = _text(_pick(row, "sender_role", "role"))
    outgoing = role in {"staff", "admin", "sysadmin"} and not note
    name = _pick(row, "sender_name", "author_name", "actor_name",
                 default="Сотрудник колледжа" if outgoing or note else "Автор обращения")
    position = _text(row.get("position"))
    title = "Внутренняя заметка" if note else "Обращение" if initial else "Ответ сотрудника" if outgoing else "Сообщение студента"
    style = "note" if note else "outgoing" if outgoing else "incoming"
    text = _pick(row, "text", "text_content", "detail")
    return (
        f'<li class="ws-timeline-item ws-timeline-message"><article class="ws-message ws-message-{style}">'
        f'<div class="ws-message-top"><span class="ws-message-kind">{icon("lock" if note else "reply" if outgoing else "user", 16)}'
        f'{title}</span><span class="ws-message-time">{_time(row.get("created_at"))}</span></div>'
        f'<div class="ws-message-author">{_esc(name)}'
        + (f'<span class="ws-message-position">{_esc(position)}</span>' if position else '') + '</div>'
        f'<div class="ws-message-text" dir="auto">{_esc(text)}</div>'
        + ('<p class="ws-note-hint">Видна только сотрудникам. Студенту не отправляется.</p>' if note else '')
        + (_delivery(row) if outgoing else '') + '</article></li>'
    )


def _event(row: Mapping) -> str:
    if _is_note(row):
        return _message(row)
    code = _text(_pick(row, "event", "kind", "type"))
    title = EVENT_LABELS.get(code, "Обращение обновлено")
    detail = _pick(row, "detail", "text")
    if code == "status":
        detail = STATUS_LABELS.get(_text(detail), detail)
    actor_name = _text(row.get("actor_name"))
    return (
        '<li class="ws-timeline-item ws-event">'
        f'<span class="ws-event-icon">{icon("activity", 16)}</span><div class="ws-event-body">'
        f'<span class="ws-event-title">{_esc(title)}</span>'
        + (f'<span class="ws-event-description">{_esc(detail)}</span>' if detail else '')
        + '<span class="ws-event-meta">'
        + (f'{_esc(actor_name)} · ' if actor_name else '') + _time(row.get("created_at")) + '</span></div></li>'
    )


def _timeline(ticket: Mapping, detail: Mapping) -> str:
    thread, events = _rows(detail.get("thread")), _rows(detail.get("events"))
    entries = [(row, False, False) for row in thread]
    for row in events:
        role = {"message_student": "student", "message_staff": "staff"}.get(row.get("event"))
        duplicate_event = role and not row.get("detail") and any(
            message.get("sender_role") == role
            and _text(message.get("sender_id")) == _text(row.get("actor_id"))
            and clock.parse(message.get("created_at")) == clock.parse(row.get("created_at"))
            for message in thread
        )
        if not duplicate_event:
            entries.append((row, True, False))
    initial = _text(_pick(ticket, "text_content", "text"))
    duplicated = any(_text(_pick(row, "text", "text_content")) == initial
                     and _text(_pick(row, "sender_role", "role")) == "student"
                     and clock.parse(row.get("created_at")) == clock.parse(ticket.get("created_at"))
                     for row in thread)
    if initial and not duplicated:
        entries.insert(0, ({"text": initial, "created_at": ticket.get("created_at"), "sender_role": "student",
                            "sender_name": _pick(ticket, "student_name", "author_name", "full_name")}, False, True))
    entries.sort(key=lambda entry: clock.parse(entry[0].get("created_at")) or datetime.max)
    content = "".join(_event(row) if event else _message(row, initial) for row, event, initial in entries)
    if not content:
        content = '<li class="ws-empty ws-thread-empty"><p>В этом обращении пока нет сообщений.</p></li>'
    return ('<section class="ws-conversation" aria-labelledby="ws-history-title">'
            '<div class="ws-section-heading"><h3 id="ws-history-title">Переписка и история</h3>'
            '<span class="ws-muted">По порядку событий</span></div>'
            '<div class="ws-history-scroll" tabindex="0" role="region" aria-label="История обращения">'
            f'<ol class="ws-timeline">{content}</ol></div></section>')


def _files(files) -> str:
    result = []
    for row in _rows(files):
        name = _pick(row, "filename", "name", default="Вложение")
        href = _local_url(_pick(row, "href", "url"))
        size = _text(row.get("size_label"))
        content = (icon("attach", 18) + f'<span><span class="ws-file-name">{_esc(name)}</span>'
                   + (f'<small>{_esc(size)}</small>' if size else '') + '</span>')
        result.append(f'<li><a class="ws-file" href="{_esc(href)}">{content}</a></li>' if href else
                      f'<li><span class="ws-file ws-file-unavailable">{content}<small>Ссылка недоступна</small></span></li>')
    if not result:
        return ""
    return ('<section class="ws-files" aria-labelledby="ws-files-title"><h3 id="ws-files-title">Вложения</h3>'
            f'<ul>{"".join(result)}</ul></section>')


def _context(ticket: Mapping, detail: Mapping) -> str:
    handler = _pick(ticket, "staff_name", "handler_name", "assigned_name", default="Не назначен")
    position = _pick(ticket, "staff_position", "handler_position")
    category = _text(ticket.get("category"))
    facts = (
        '<div><dt>Исполнитель</dt><dd>' + _esc(handler)
        + (f'<span class="ws-muted ws-context-sub">{_esc(position)}</span>' if position else '') + '</dd></div>'
        '<div><dt>Раздел</dt><dd>' + _esc(CATEGORY_LABELS.get(category, category) or "Не указан") + '</dd></div>'
        '<div><dt>Создано</dt><dd>' + _time(ticket.get("created_at")) + '</dd></div>'
    )
    if ticket.get("student_id"):
        facts += f'<div><dt>MAX ID студента</dt><dd>{_esc(ticket["student_id"])}</dd></div>'
    for key, title in (("pickup_place", "Кабинет выдачи"), ("ready_until", "Срок готовности")):
        if ticket.get(key):
            facts += f'<div><dt>{title}</dt><dd>{_esc(ticket[key])}</dd></div>'
    return ('<aside class="ws-context" aria-label="Сведения об обращении">'
            f'<section class="ws-context-card"><h3>Об обращении</h3><dl>{facts}</dl></section>'
            + _files(detail.get("files")) + '</aside>')


def _notice(kind: str, text: str) -> str:
    tone = kind if kind in {"error", "warning", "success", "info"} else "info"
    symbol = "warning" if tone in {"error", "warning"} else "check" if tone == "success" else "info"
    role = "alert" if tone == "error" else "status"
    return (f'<div class="ws-notice ws-notice-{tone}" role="{role}">{icon(symbol)}'
            f'<span>{_esc(text)}</span></div>')


def _notices(detail: Mapping) -> str:
    result = ""
    if detail.get("conflict") or detail.get("http_status") == 409:
        result += _notice("warning", "Обращение уже изменено другим сотрудником. Проверьте новую историю перед повторной отправкой. Неотправленный текст не удаляется.")
    messages = _rows(detail.get("notices"))
    if isinstance(detail.get("notice"), Mapping):
        messages = [detail["notice"], *messages]
    for message in messages:
        if message.get("text"):
            result += _notice(_text(message.get("kind")), _text(message["text"]))
    states = [_text(row.get("delivery_status")).lower() for row in _rows(detail.get("thread"))]
    blocked = ("forbidden" in states or _text(detail.get("delivery_code")) == "403"
               or any(_text(row.get("delivery_code")) == "403" for row in _rows(detail.get("thread"))))
    if blocked:
        result += _notice("warning", "MAX не разрешил доставку сообщения (403). Проверьте его состояние в истории перед повторной отправкой: повтор может создать дубликат.")
    elif detail.get("delivery_warning") or any(state in {"failed", "blocked"} for state in states):
        result += _notice("warning", "Доставка сообщения не подтверждена. Проверьте историю перед повторной отправкой, чтобы не отправить ответ дважды.")
    return result


def _draft(detail: Mapping, mode: str) -> str:
    drafts = detail.get("drafts")
    if isinstance(drafts, Mapping) and mode in drafts:
        return _text(drafts[mode])
    return _text(detail.get("draft_text")) if detail.get("draft_mode", "reply") == mode else ""


def _form_open(base: str, ticket_id: str, detail: Mapping, mode: str, csrf_token, operation_key, return_to: str) -> str:
    return (
        f'<form class="ws-action-form ws-form-{mode}" method="post" action="{base}/{ticket_id}/action" '
        f'data-ws-action-form data-draft-mode="{mode}" data-ticket-id="{ticket_id}">'
        + _hidden("csrf", csrf_token) + _hidden("operation_key", operation_key)
        + _hidden("revision", detail.get("revision", "")) + _hidden("return", return_to)
    )


def _textarea(detail: Mapping, mode: str, label: str, placeholder: str, required: bool = True) -> str:
    return (f'<label class="ws-field ws-text-field" for="ws-{mode}-text"><span>{label}</span>'
            f'<textarea class="ws-input ws-textarea" id="ws-{mode}-text" name="text" rows="5" '
            f'placeholder="{placeholder}" maxlength="{4000 if mode == "reply" else 200}" '
            f'data-ws-draft-field aria-describedby="ws-{mode}-hint"'
            f'{" required" if required else ""}>{_esc(_draft(detail, mode))}</textarea></label>')


def _submit(action: str, label: str, primary: bool = False, symbol: str = "") -> str:
    return (f'<button class="ws-btn{" ws-btn-primary" if primary else " ws-btn-quiet"}" '
            f'type="submit" name="action" value="{action}">'
            + (icon(symbol, 18) if symbol else '') + f'{label}</button>')


def _composer(base: str, ticket: Mapping, detail: Mapping, actor: Mapping, csrf_token, operation_key, return_to: str) -> str:
    allowed = ACTIONS.intersection(detail.get("allowed_actions") or [])
    if _flag(detail.get("archived")) or _flag(ticket.get("archived")) or ticket.get("archived_at"):
        allowed = frozenset()
    ticket_id = _id(ticket)
    if not ticket_id:
        return ""
    reply = ""
    if allowed & {"reply", "reply_close", "template_preview"}:
        reply = _form_open(base, ticket_id, detail, "reply", csrf_token, operation_key, return_to)
        reply += '<div class="ws-composer-heading"><h3 id="ws-composer-title">Ответ студенту</h3>'
        reply += '<span class="ws-reply-as">От вашего имени: ' + _esc(actor.get("full_name") or "Сотрудник колледжа") + '</span></div>'
        reply += _textarea(detail, "reply", "Текст ответа", "Напишите понятный ответ студенту…")
        reply += '<p class="ws-field-hint" id="ws-reply-hint">Ответ уйдёт студенту в MAX. До 4000 символов. Проверьте текст перед отправкой.</p>'
        templates = _rows(detail.get("templates"))
        if "template_preview" in allowed and templates:
            options = {"": "Выберите шаблон"}
            for row in templates:
                template_id = _text(_pick(row, "template_id", "id"))
                if template_id:
                    options[template_id] = _text(row.get("title")) or "Шаблон ответа"
            reply += (
                '<details class="ws-template-picker"><summary>' + icon("templates", 16) + ' Шаблон ответа</summary>'
                '<div class="ws-template-controls"><label class="ws-field" for="ws-template"><span>Подходящие шаблоны</span>'
                '<select class="ws-input" name="template_id" id="ws-template" data-ws-template>'
                + _options(options, _text(_pick(detail, "template_id", "selected_template_id"))) + '</select></label>'
                '<button class="ws-btn ws-btn-quiet" type="submit" name="action" value="template_preview" '
                'formnovalidate data-ws-template-preview>Подставить в ответ</button></div>'
                '<p class="ws-field-hint">Подставится только текст. Отправки не будет, пока вы не нажмёте кнопку ответа.</p></details>'
            )
        reply += '<div class="ws-compose-actions">'
        if "reply" in allowed:
            reply += _submit("reply", "Ответить", True, "send")
        if "reply_close" in allowed:
            reply += _submit("reply_close", "Ответить и закрыть", False, "check")
        reply += '</div><p class="ws-draft-status ws-field-hint" data-ws-draft-status role="status" aria-live="polite"></p></form>'
    more = ""
    if "note" in allowed:
        more += '<section class="ws-secondary-action"><h4>' + icon("lock", 18) + ' Внутренняя заметка</h4>'
        more += _form_open(base, ticket_id, detail, "note", csrf_token, operation_key, return_to)
        more += _textarea(detail, "note", "Заметка для сотрудников", "Что важно знать коллегам…")
        more += ('<p class="ws-field-hint" id="ws-note-hint">Только для сотрудников. До 200 символов. Студент не увидит эту заметку.</p>'
                 + _submit("note", "Сохранить заметку", False, "lock") + '</form></section>')
    if "status" in allowed:
        statuses = detail.get("allowed_statuses")
        status_options = (dict(statuses) if isinstance(statuses, Mapping) else
                          {key: STATUS_LABELS.get(key, key) for key in statuses} if isinstance(statuses, (list, tuple)) else STATUS_LABELS)
        more += '<section class="ws-secondary-action"><h4>' + icon("activity", 18) + ' Статус обращения</h4>'
        more += _form_open(base, ticket_id, detail, "status", csrf_token, operation_key, return_to)
        more += '<label class="ws-field"><span>Новый статус</span><select class="ws-input" name="status" required>'
        # Текущий запрещённый переход не добавляется к разрешённым вариантам.
        selected = _text(ticket.get("status")) if ticket.get("status") in status_options else ""
        more += _options({"": "Выберите статус", **status_options}, selected) + '</select></label>'
        more += _submit("status", "Изменить статус") + '</form></section>'
    if "forward" in allowed:
        recipients = {"": "Выберите сотрудника"}
        for row in _rows(detail.get("recipients")):
            uid = _text(_pick(row, "user_id", "id"))
            if uid:
                label = _text(_pick(row, "full_name", "name", default="Сотрудник колледжа"))
                if row.get("position"):
                    label += " · " + _text(row["position"])
                recipients[uid] = label
        more += '<section class="ws-secondary-action"><h4>' + icon("reply", 18) + ' Передать обращение</h4>'
        more += _form_open(base, ticket_id, detail, "forward", csrf_token, operation_key, return_to)
        more += '<label class="ws-field"><span>Новый исполнитель</span><select class="ws-input" name="target_id" required>'
        target_id = _text(detail.get("target_id"))
        more += _options(recipients, target_id if target_id in recipients else "") + '</select></label>'
        more += _textarea(detail, "forward", "Пояснение для сотрудника", "Необязательно: что уже сделано и что нужно дальше…", False)
        more += '<p class="ws-field-hint" id="ws-forward-hint">Выберите исполнителя из доступных получателей. Пояснение — до 200 символов.</p>'
        more += _submit("forward", "Передать обращение") + '</form></section>'
    if more:
        expanded = " open" if detail.get("draft_mode") in {"note", "forward", "status"} else ""
        more = (f'<details class="ws-more-actions"{expanded}><summary>{icon("more", 18)} Ещё действия'
                f'<span class="ws-disclosure-icon">{icon("chevron-down", 16)}</span></summary>'
                f'<div class="ws-secondary-actions">{more}</div></details>')
    if not reply and not more:
        return '<div class="ws-readonly">' + icon("lock", 18) + '<span>Для этого обращения доступны просмотр и история.</span></div>'
    return '<section class="ws-composer" aria-label="Действия с обращением">' + reply + more + '</section>'


def _admin_extra(data: Mapping, actor: Mapping) -> str:
    if not _is_admin(actor):
        return ""
    links = []
    for row in _rows(data.get("admin_links")):
        href = _local_url(row.get("href"))
        if href and href.startswith("/panel/"):
            links.append(f'<a class="ws-btn ws-btn-quiet" href="{_esc(href)}">{_esc(row.get("label"))}</a>')
    # Исключение из экранирования — только явный доверенный HTML родителя.
    extra = _text(data.get("extra_admin_html"))
    if not links and not extra:
        return ""
    return ('<details class="ws-admin-tools"><summary>' + icon("settings", 18) + ' Администрирование</summary>'
            '<div class="ws-admin-content"><div class="ws-admin-links">' + ''.join(links) + '</div>' + extra + '</div></details>')


def _copy_control(value, label: str, actor: Mapping) -> str:
    marker = "data-copy" if _is_admin(actor) else "data-ws-copy"
    return (f'<button type="button" class="ws-btn ws-btn-quiet" {marker}="{_esc(value)}" '
            f'data-copy-note="{_esc(label)}: скопировано" aria-label="{_esc(label)}">'
            + icon("copy", 16) + f'{_esc(label)}</button>')


def _card_tools(base: str, ticket: Mapping, detail: Mapping, actor: Mapping) -> str:
    tid = _id(ticket)
    link = _text(detail.get("share_url")) or f"{base}/{tid}"
    controls = _copy_control(link, "Скопировать ссылку", actor)
    controls += ('<button type="button" class="ws-btn ws-btn-quiet" data-ws-print>'
                 + icon("print", 16) + ' Печать карточки</button>')
    bot_url = _text(detail.get("bot_url"))
    if re.fullmatch(r"https://max\.ru/[A-Za-z0-9_]+", bot_url):
        controls += (f'<a class="ws-btn ws-btn-quiet" href="{_esc(bot_url)}" target="_blank" rel="noopener">'
                     + icon("external", 16) + ' Открыть в боте</a>')
    return '<div class="ws-card-tools" aria-label="Инструменты карточки">' + controls + '</div>'


def _detail(base: str, detail: Mapping, queue: Mapping, params: Mapping, actor: Mapping, csrf_token, operation_key, standalone: bool) -> str:
    page = max(1, _number(queue.get("page", params.get("page")), 1))
    return_queue = _url(base, params, page=page)
    back = f'<a class="ws-btn ws-btn-quiet ws-back" href="{_esc(return_queue)}">{icon("chevron-left", 18)} К очереди</a>'
    if _flag(detail.get("forbidden")) or detail.get("http_status") == 403:
        return ('<section class="ws-detail ws-empty ws-access-empty">' + icon("lock", 32)
                + '<h2>Обращение недоступно</h2><p>Возможно, изменился исполнитель или ваши права. Вернитесь к доступной очереди.</p>' + back + '</section>')
    ticket = detail.get("ticket")
    if not isinstance(ticket, Mapping) or not _id(ticket):
        return ('<section class="ws-detail ws-empty">' + icon("empty", 32)
                + '<h2>Обращение не найдено</h2><p>Оно могло быть перемещено или удалено.</p>' + back + '</section>')
    ticket_id = _id(ticket)
    canonical = _url(f'{base}/{ticket_id}', params, page=page)
    return_to = canonical if standalone else _url(base, params, page=page, t=ticket_id)
    full = (f'<a class="ws-btn ws-btn-quiet ws-open-full" href="{_esc(canonical)}">{icon("external", 16)}'
            '<span>Отдельная карточка</span></a>' if not standalone else '')
    name = _pick(ticket, "student_name", "author_name", "full_name", default=person_label("", _text(ticket.get("student_id"))) or "Автор обращения")
    group = _pick(ticket, "student_group", "group_code")
    topic = _text(ticket.get("topic"))
    archived = '<span class="ws-status ws-status-neutral">В архиве</span>' if _flag(ticket.get("archived")) or ticket.get("archived_at") else ''
    return (
        '<article class="ws-detail" aria-labelledby="ws-ticket-title">'
        f'<div class="ws-detail-toolbar">{back}{full}</div>{_card_tools(base, ticket, detail, actor)}'
        '<div class="ws-detail-heading"><div><div class="ws-detail-title-row">'
        f'<h2 id="ws-ticket-title">Обращение <span>№ {ticket_id}</span></h2>{_status(ticket)}{archived}</div>'
        f'<p class="ws-detail-person">{_esc(name)}' + (f'<span class="ws-detail-group">{_esc(group)}</span>' if group else '') + '</p>'
        + (f'<p class="ws-detail-topic">{_esc(topic)}</p>' if topic else '')
        + f'</div><div class="ws-detail-age">{_timer(ticket, actor)}</div></div>'
        + _notices(detail)
        + (_notice("info", "Старые вложения без подтверждённой связи доступны только сис-админу.")
           if detail.get("legacy_files_restricted") and not _is_admin(actor) else "")
        + '<div class="ws-detail-grid"><div class="ws-detail-main">'
        + _timeline(ticket, detail) + _composer(base, ticket, detail, actor, csrf_token, operation_key, return_to)
        + '</div>' + _context(ticket, detail) + '</div>' + _admin_extra(detail, actor) + '</article>'
    )


def _success(data: Mapping) -> str:
    marker = data.get("action_success")
    if not isinstance(marker, Mapping):
        return ""
    action, ticket_id = _text(marker.get("action")), _id(marker)
    operation = _text(marker.get("operation_key"))
    if action not in ACTIONS - {"template_preview"} or not ticket_id or not operation:
        return ""
    return (f' data-success-ticket="{ticket_id}" data-success-action="{action}" '
            f'data-success-operation="{_esc(operation)}"')


def render_workbench(*, base_path, actor, queue, detail, filters, csrf_token, operation_key, standalone=False) -> str:
    """Вернуть body-фрагмент; все полномочия и данные передаёт родитель.

    actor: user_id, full_name, position, is_admin, permissions.
    queue: rows, total, counts (open/waiting/closed/archive), page, page_size.
    detail: ticket, thread, events, templates, recipients, files,
            allowed_actions, revision. None означает очередь без карточки.
    filters: tab=open|waiting|closed, q/category/status/date_from/date_to,
             scope/view/mine. Неизвестные ключи никогда не попадают в URL.
    """
    base = _text(base_path).rstrip("/")
    if base not in {"/workspace/tickets", "/panel/tickets"}:
        raise ValueError("Неизвестный путь рабочего места")
    actor, queue, filters = dict(actor), dict(queue), dict(filters)
    detail = dict(detail) if detail is not None else None
    params = _filters(filters, actor)
    if "page" not in queue:
        queue["page"] = max(1, _number(filters.get("page"), 1))
    ticket = (detail or {}).get("ticket") or {}
    selected = _id(ticket) if isinstance(ticket, Mapping) else ""
    restricted = detail and (_flag(detail.get("forbidden")) or detail.get("http_status") == 403)
    if restricted:
        selected = ""
    classes = "ws-workbench" + (" ws-has-detail" if detail is not None else "") + (" ws-standalone" if standalone else "")
    success = _success(detail or {}) or _success(queue)
    admin = ""
    if _is_admin(actor) or _flag(actor.get("can_export")) or "export" in (actor.get("permissions") or []):
        export_url = _url(base + ".csv", params)
        admin += f'<a class="ws-btn ws-btn-quiet" href="{_esc(export_url)}">{icon("download", 18)} Выгрузить CSV</a><span class="ws-export-note">Те же фильтры, до 2000 строк</span>'
    if _is_admin(actor):
        create_url = "/panel/tickets/new?" + urlencode({"return": _url(base, params, page=queue["page"])})
        admin += f'<a class="ws-btn ws-btn-quiet" href="{_esc(create_url)}">{icon("plus", 18)} Создать обращение</a>'
    card = (_detail(base, detail, queue, params, actor, csrf_token, operation_key, bool(standalone))
            if detail is not None else
            '<section class="ws-detail ws-empty ws-select-empty">' + icon("inbox", 40)
            + '<span class="ws-eyebrow">Всё для ответа — рядом</span><h2>Выберите обращение</h2>'
            '<p>Здесь появятся переписка, история и действия.<br>Начните с очереди слева.</p></section>')
    return (
        f'<section class="{classes}" data-workspace data-user-id="{_esc(actor.get("user_id"))}" '
        f'data-ticket-id="{selected}" data-base-path="{base}"{success} aria-label="Рабочее место">'
        '<div class="ws-topbar"><div><p class="ws-eyebrow">Рабочее место</p>'
        '<h2 class="ws-title">Обращения</h2><p class="ws-subtitle">Спокойно разобраться. Понятно ответить.</p></div>'
        f'<div class="ws-top-actions">{admin}</div></div>'
        + (_notices(queue) if detail is None else '')
        + '<div class="ws-layout">'
        + ('' if standalone else _queue(base, queue, params, actor, selected)) + card + '</div>'
        + _admin_extra(queue, actor)
        + '<p class="ws-sr-only" data-ws-announcer role="status" aria-live="polite"></p></section>'
    )


__all__ = ["render_workbench", "workspace_assets", "WORKSPACE_STYLESHEET_HREF", "WORKSPACE_SCRIPT_SRC"]
