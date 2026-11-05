"""Обращения: рабочее место, карточка, массовые действия, архив и шаблоны ответов."""
import csv
import io
import os
import unicodedata
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode

import clock
import database as db
import repository as repo
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from handlers.common import admin_of, is_personal_template, notify
from handlers.tickets import TPL_FIELDS, render_template, unknown_placeholders
from panel_theme import icon
from store.tickets import ticket_filter_dates
from utils import (CATS, OPEN_STATUSES, STAFF_CATS, STATUS, as_str, cut_plain, fmt_when,
                    group_code, to_int)

from .common import (FIO_MAX, _action_form, bot_open_link, code_cell, copy_btn, csrf, esc, fio, flash, form, log, open_in_bot,
                     page, page_window, pager, pages_of, panel_link, pill, plain, redirect, require_form,
                     require_user, safe_return, select, value, window_tail)
from .router import router


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


TEMPLATES_PAGE = 50     # шаблонов на страницу


def _template_fields(title: str = "", text: str = "", category: str = "all") -> str:
    """Поля шаблона ответа: название, раздел и текст.

    Одни и те же поля в форме добавления и в форме правки - разница только в
    подставленных значениях. Всё, что пришло из базы, экранируется (esc): текст
    шаблона пишут руками, а в разметку он попасть не должен.
    """
    return (
        '<div class="full"><label>Название - как это выглядит в кнопке</label>'
        f'<input name="title" value="{esc(title)}" placeholder="Справка готова"></div>'
        + select("category", dict(STAFF_CATS), category or "all", label="Раздел")
        + '<div class="full"><label>Текст ответа</label>'
        '<textarea name="text" rows="4" placeholder="Здравствуйте! Справка готова, '
        'заберите её в кабинете 214.">'
        f"{esc(text)}</textarea></div>"
    )


def _tpl_hint(unknown: list) -> str:
    """Подсказка под формой ответа: какие подстановки понятны и что осталось лишнего.

    Список имён берём у бота (handlers.tickets.TPL_FIELDS), чтобы панель и бот
    понимали ровно один и тот же набор, иначе сотрудник напишет в панели {ФИО},
    а в боте она останется неподставленной. Эмодзи из подсказки бота сюда не
    тащим: в панели их место занимают иконки темы.
    """
    names = ", ".join("{%s}" % name for name in TPL_FIELDS)
    tail = f" Непонятно: {', '.join('{%s}' % name for name in unknown)}." if unknown else ""
    return f"Подстановки: {names}.{tail}"


@router.get("/templates")
async def templates_page(request: Request, page_no: int = 1):
    """Шаблоны ответов: что сотрудники отвечают чаще всего и почему.

    Раньше читались первые 100 шаблонов, а заголовок показывал их число как
    «всего» - то есть в панели тихо пропадал каждый шаблон после сотого.
    Теперь страниц по 50, а счётчик честный: templates_count().
    """
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    window = page_window(page_no, TEMPLATES_PAGE)
    found = await repo.list_templates(limit=window)
    total = await repo.templates_count()
    pages = pages_of(min(len(found), total), TEMPLATES_PAGE)
    rows = found[(page_no - 1) * TEMPLATES_PAGE: page_no * TEMPLATES_PAGE]
    body_rows = ""
    for row in rows:
        title = as_str(row["title"])
        confirm = f"Удалить шаблон «{title}»?"
        edit_link = (f'<a class="btn btn-grey" href="/panel/templates/{esc(row["id"])}/edit">'
                     f'{icon("edit", 16)} Правка</a>')
        body_rows += (
            f"<tr data-hk><td><b>{esc(title)}</b><div class='small mut'>ID {esc(row['id'])}</div></td>"
            f"<td class='small'>{esc((as_str(row['text']) or '')[:220])}</td>"
            f"<td>{esc(plain(STAFF_CATS.get(row['category'], row['category'])))}</td>"
            f"<td>{esc(row['used_count'])}</td>"
            f"<td class='small mut'>{esc(fmt_when(row['created_at']))}</td>"
            f'<td class="col-act">{edit_link}'
            f"{_action_form(request, f'/panel/templates/{esc(row['id'])}/delete', f'{icon("delete", 16)} Удалить', confirm_text=confirm, cls='btn-bad')}"
            f"</td></tr>"
        )
    body_rows = body_rows or "<tr><td class='mut'>Шаблонов пока нет</td></tr>"
    table = ("<table><tr><th>Название</th><th>Текст</th><th>Раздел</th><th>Применён</th>"
             f'<th>Добавлен</th><th class="col-act">Действие</th></tr>{body_rows}</table>')
    pages_bar = pager("/panel/templates", [], page_no, pages, f"шаблонов: {total}")
    tail = window_tail(len(found), window, "Ненужные шаблоны лучше удалить - их ищут по названию.")
    add = form(request, "/panel/templates/add", _template_fields(),
               "Добавить шаблон", "btn-ok")
    body_all = f"""
<div class="card"><h2>{icon("templates", 20)} Шаблоны ответов: {total}</h2>
<p class="small mut">Сотрудник в карточке обращения открывает «Шаблоны» - выбирает подходящий
и отправляет как есть или дописывает своё. Шаблон с разделом «Всё» показывается всегда,
остальные - только в своём разделе. Колонка «Применён» показывает, какие ответы реально нужны.</p>
{table}{pages_bar}{tail}</div>
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
    log.info("панель: добавлен шаблон %s (сис-админ %s)", template_id, user)
    await repo.log_action(user, "шаблон ответа добавлен", f"ID {template_id}; поля: title, text, category")
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
    await repo.log_action(user, "шаблон ответа удалён", f"ID {template_id}")
    flash(f"Шаблон «{template['title']}» удалён.")
    return redirect("/panel/templates")


@router.get("/templates/{template_id}/edit")
async def templates_edit(request: Request, template_id: int):
    """Правка шаблона ответа: испорченный текст выправляется на месте.

    Отдельная страница, а не поле в таблице: в тексте шаблона живут
    подстановки и переносы строк, в ячейке их не разобрать, а чинить обычно
    нужно именно текст.
    """
    user = await require_user(request)
    template = await repo.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    title = as_str(template["title"])
    text = as_str(template["text"])
    unknown = unknown_placeholders(text)
    # пометка «личный» живёт в настройках, а не в строке шаблона
    # (см. handlers.common), поэтому её достаточно показать: правка её не трогает
    mine = await is_personal_template(user, template_id)
    mark = f" {pill('мои', 'on')}" if mine else ""
    about = ("" if not mine else
             " Шаблон личный у вас: в боте он остаётся первым в вашем списке "
             "ответов, и правка этого не меняет.")
    warning = (f'<p class="msg msg-bad">В тексте есть неизвестные подстановки: '
               f'{esc(", ".join("{%s}" % name for name in unknown))}. Студенту они '
               f'уйдут как есть - перепишите их обычным текстом или уберите.</p>') \
        if unknown else ""
    edit = form(request, f"/panel/templates/{template_id}/edit",
                _template_fields(title, text, as_str(template["category"])),
                "Сохранить", "btn-ok")
    body = f"""
<div class="card"><h2>{icon("edit", 20)} Правка шаблона «{esc(title)}»{mark}</h2>
<p class="small mut">ID {esc(template_id)} · добавлен {esc(fmt_when(template['created_at']))} ·
применений: {esc(template['used_count'])}. Сотрудник видит шаблон в карточке обращения
своего раздела, а «Всё» - в любом.{about}</p>
{warning}
{edit}
<p><a class="btn btn-grey" href="/panel/templates">{icon("chevron-left", 16)} К шаблонам</a></p>
</div>"""
    return page(f"Шаблон: {title}", body, user, "/templates")


@router.post("/templates/{template_id}/edit")
async def templates_edit_submit(request: Request, template_id: int):
    """Сохранение правки: название, раздел и текст ответа.

    В журнал идут только ID и имена полей, без названия и текста шаблона.
    Счётчик применений и дата добавления не трогаются: правка не «применение».
    """
    user = await require_form(request)
    data = await request.form()
    if not await repo.get_template(template_id):
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    title = value(data, "title")
    text = value(data, "text")
    if not title or not text:
        flash("!Нужны и название, и текст ответа: шаблон без текста бесполезен.")
        return redirect(f"/panel/templates/{template_id}/edit")
    category = value(data, "category") or "all"      # раздел - как при добавлении
    await db.run("UPDATE reply_templates SET title=?, text=?, category=? WHERE id=?",
                 (title[:80], text[:2000], category, template_id))
    log.info("панель: изменён шаблон %s (сис-админ %s)", template_id, user)
    await repo.log_action(user, "шаблон ответа изменён", f"ID {template_id}; поля: title, text, category")
    flash(f"Шаблон «{title}» сохранён.")
    return redirect("/panel/templates")


def csv_cell(value) -> str:
    """Excel не должен исполнить пользовательское значение как формулу."""
    text = as_str(value)
    # Таблица может отбросить пробелы, BOM и управляющие символы перед формулой.
    significant = text
    while significant and (significant[0].isspace() or
                           unicodedata.category(significant[0]) in ("Cc", "Cf")):
        significant = significant[1:]
    return "'" + text if significant.startswith(("=", "+", "-", "@")) else text


@router.get("/tickets.csv")
async def tickets_csv(request: Request, status: str = "", category: str = "",
                      q: str = "", scope: str = "", view: str = "", mine: str = "",
                      date_from: str = "", date_to: str = ""):
    """Выгрузка обращений в CSV: для отчётов и разборов вне панели.

    Фильтры те же, что на странице, и правила те же - берём общую функцию.
    Раньше выгрузка знала только статус и раздел, поэтому из архива отдавала
    пустоту, а из списка с поиском - не то, что человек видит на экране.
    """
    user = await require_user(request)  # CSRF-токен не нужен: это скачивание
    from .workspace import _check_result, workspace_filters
    from store.workspace import workspace_list

    filters, _shown, _page = workspace_filters(request.query_params)
    result = await workspace_list(user, admin=True, filters=filters, for_export=True, page_size=2000)
    _check_result(result)
    rows = result["rows"]
    # Имена уже присоединены к каждой строке: не зависят от окна списка людей.
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["№", "Создано", "Статус", "Раздел", "Тема", "Студент", "ID студента",
                     "Сотрудник", "ID сотрудника", "Текст"])
    for row in rows:
        student_id = as_str(row["student_id"])
        admin_id = as_str(row["target_admin_id"])
        writer.writerow([csv_cell(value) for value in [
            row["ticket_id"], as_str(row["created_at"]),
            STATUS.get(as_str(row["status"]), as_str(row["status"])),
            STAFF_CATS.get(as_str(row["category"]), as_str(row["category"])),
            as_str(row["topic"]), as_str(row["student_name"]) or student_id, student_id,
            as_str(row["staff_name"]) or admin_id, admin_id,
            as_str(row["text_content"])[:1000],
        ]])
    return Response(
        "\ufeff" + buffer.getvalue(),   # BOM - чтобы Excel открыл в UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tickets.csv"',
                 "Cache-Control": "no-store"},
    )


# ── обращения ─────────────────────────────────────────────────────────────────
# Бот пишет вложение так - по этому префиксу панель узнаёт файл в переписке.
ATTACH_PREFIX = "📎 Файл: "


EVENT_LABELS = {"created": "обращение создано", "status": "статус", "ready": "документ готов",
                "message_student": "сообщение студента", "message_staff": "ответ сотрудника"}


OVERDUE_HOURS = 24
"""Сутки. Письмо, которое ждёт дольше суток, к этому времени уже забыто:
именно на такие и нужен счётчик, а не на все ожидающие."""


TICKET_FILTERS = ("status", "category", "q", "scope", "view", "mine", "date_from", "date_to")


def _filter_params(status: str = "", category: str = "", q: str = "", scope: str = "",
                   view: str = "", mine: str = "", date_from: str = "", date_to: str = "") -> dict:
    """Один канонический набор для SQL, формы, ссылок, CSV и возврата после POST."""
    try:
        date_from, date_to = ticket_filter_dates(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": as_str(status), "category": as_str(category), "q": as_str(q),
            "scope": as_str(scope), "view": as_str(view),
            "mine": "1" if as_str(mine).strip().lower() in ("1", "true", "yes", "on") else "0",
            "date_from": date_from, "date_to": date_to}


def _filter_query(params: dict, **changes) -> str:
    """Изменяет только указанный фильтр и кодирует все значения ровно один раз."""
    return urlencode({key: val for key, val in (params | changes).items() if val != "" and val is not None})


def _tickets_url(params: dict, **changes) -> str:
    query = _filter_query(params, **changes)
    return "/panel/tickets" + ("?" + query if query else "")


def is_overdue(row, latest: dict) -> bool:
    """Сутки от последнего сообщения студента, не от правки карточки."""
    state = latest.get(to_int(row["ticket_id"]), {})
    role = state.get("role", "") if isinstance(state, dict) else as_str(state)
    if as_str(row["status"]) not in OPEN_STATUSES or role != "student":
        return False
    stamp = state.get("created_at", "") if isinstance(state, dict) else _row_get(row, "latest_message_at")
    touched = clock.parse(stamp)
    return touched is not None and (clock.now() - touched) >= timedelta(hours=OVERDUE_HOURS)


def _ticket_timer(row) -> str:
    """Одинаковый источник времени в очереди и карточке; неизвестную дату не выдумываем."""
    data = dict(row)
    if data["status"] == "in_progress":
        kind, label, stamp = "in_progress", "в работе", data.get("in_progress_at", "")
    elif data.get("is_waiting"):
        kind, label, stamp = "waiting", "ждёт ответа", data.get("latest_message_at", "")
    else:
        kind, label, stamp = "message", "последнее сообщение", data.get("latest_message_at", "")
    moment = clock.parse(stamp)
    if moment is None:
        if kind == "in_progress":
            return '<span class="wb-age">в работе · время начала неизвестно</span>'
        return f'<span class="wb-age">{esc(label)} · время неизвестно</span>' if stamp or data.get("is_waiting") else ""
    aware = moment.replace(tzinfo=clock.zone())
    return (f'<span class="wb-age live-age" data-time="{esc(aware.isoformat())}" '
            f'data-time-ms="{int(aware.timestamp() * 1000)}" data-prefix="{esc(label)}" '
            f'data-timer-kind="{kind}">{esc(label)} · {esc(fmt_when(stamp))}</span>')


async def filter_tickets(status: str = "", category: str = "", q: str = "",
                         scope: str = "", view: str = "", limit: int = 500,
                         mine: str = "", actor_id: str = "", date_from: str = "",
                         date_to: str = "") -> tuple:
    """Условия и счётчики применяются в SQL до LIMIT, без фильтрации в Python."""
    params = _filter_params(status, category, q, scope, view, mine, date_from, date_to)
    mine_enabled = params.pop("mine") == "1"
    archived = params.pop("view") == "archive"
    # Без известного сотрудника «Только мои» не должно раскрывать общую очередь.
    if mine_enabled and not actor_id:
        return [], {}, 0, 0
    rows = await repo.admin_tickets(as_str(actor_id) if mine_enabled else None, limit,
                                    archived=archived, with_counts=True,
                                    overdue_hours=OVERDUE_HOURS, **params)
    states = {int(row["ticket_id"]): {"role": as_str(row["latest_role"]),
                                      "created_at": as_str(row["latest_message_at"])} for row in rows}
    waiting = int(rows[0]["filtered_waiting"]) if rows else 0
    overdue = int(rows[0]["filtered_overdue"]) if rows else 0
    return rows, states, waiting, overdue


@router.get("/tickets")
async def tickets_list(request: Request, status: str = "", q: str = "", category: str = "",
                       scope: str = "", t: int = 0, view: str = "", mine: str = "",
                       date_from: str = "", date_to: str = ""):
    """Обновлённая очередь: настоящая пагинация и общий интерфейс без выдачи прав."""
    from .workspace import admin_principal, render_workspace

    actor = await admin_principal(request)
    return await render_workspace(request, actor, selected=t)


def print_btn(label: str = "Печать") -> str:
    """Кнопка печати: печатная версия описана в @media print темы."""
    return (f'<button type="button" class="btn btn-grey" onclick="window.print()">'
            f'{icon("print", 16)} {esc(label)}</button>')


def _tickets_actions(live_query: str, selected, bot: str) -> str:
    """Шапка рабочего места: выгрузка, печать карточки и ссылка на бота.

    ``live_query`` - готовая строка фильтров со всеми условиями, какие сейчас
    стоят на странице, включая архив и поиск. Раньше сюда передавались только
    статус и раздел, и выгрузка молча отдавала не то, что человек видит.
    """
    return (f'<a class="btn" href="/panel/tickets.csv?{esc(live_query)}">'
            f'{icon("download", 16)} Выгрузить в CSV</a>'
            f'<span class="small mut">те же фильтры, что на экране, до 2000 строк</span>'
            + (print_btn("Печать карточки") if selected else "")
            + open_in_bot(bot))


async def _staff_choices(keep_current: str = "", current_name: str = "") -> tuple[dict, dict]:
    """Сотрудники панели для выбора: текущий исполнитель виден первым.

    Возвращает пары «подписи» и «подсказки». В подписи - ФИО целиком:
    исполнителя выбирают по имени, и обрезанное «Ковалевский Ко… · Секрет…»
    для этого не годится, поэтому должность уходит в ``title`` пункта.
    """
    choices = {"": "— не назначен —"}
    titles: dict[str, str] = {}
    if keep_current:
        choices[keep_current] = f"{fio(current_name, keep_current, FIO_MAX)} (сейчас)"
        titles[keep_current] = "сейчас ведёт это обращение"
    for row in await repo.list_staff():
        person = dict(row)          # db.many отдаёт sqlite3.Row, а нужны ключи по имени
        uid = as_str(person.get("user_id"))
        if uid and uid != keep_current:
            choices[uid] = fio(person.get("full_name"), uid, FIO_MAX)
            titles[uid] = as_str(person.get("position") or person.get("role") or "—")
    return choices, titles


@router.post("/tickets/see-all")
async def tickets_see_all(request: Request):
    """Кто из сотрудников видит чужие обращения: очередь отдела или только свои."""
    actor = await require_form(request)
    data = await request.form()
    user_id = value(data, "user_id")
    flag = as_str(data.get("value", "")).strip() in ("1", "on", "true", "да")
    if not await repo.get_admin(user_id):
        flash("!Сотрудник не найден.")
        return redirect(safe_return(data))
    await repo.set_staff_see_all(user_id, flag)
    await repo.log_action(actor, "доступ к чужим обращениям",
                          f"ID {user_id}; поля: see_all_tickets")
    flash(f"{user_id}: {'видит все обращения' if flag else 'видит только свои'}.")
    return redirect(safe_return(data))


async def _tickets_access(request: Request, return_to: str = "/panel/tickets", q: str = "") -> str:
    """Кто видит чужие обращения: галочка рядом с именем сотрудника.

    Права читает staff_sees_all_bulk(): раньше на каждого сотрудника уходил
    свой SELECT, и на самой посещаемой странице панели запросов было ровно
    столько же, сколько сотрудников, - открыть «Обращения» подтормаживало тем
    сильнее, чем больше людей работало.
    """
    rows = [dict(row) for row in await repo.list_staff()]
    needle = as_str(q).strip().lower()
    if needle:
        rows = [row for row in rows if needle in " ".join((
            as_str(row.get("user_id")), as_str(row.get("full_name")),
            as_str(row.get("position")), as_str(row.get("department")),
            as_str(row.get("office")),
        )).lower()]
    if not rows:
        return ""
    sees_all_by_id = await repo.staff_sees_all_bulk()
    items = []
    for row in rows:
        person = dict(row)
        uid = as_str(person.get("user_id"))
        sees_all = sees_all_by_id.get(uid, False)
        position = as_str(person.get("position") or person.get("role") or "—")
        # Галочку ставят человеку, а не строке с обрезанным именем, поэтому
        # ФИО целиком, а должность - отдельной строкой мельче.
        who = fio(person.get("full_name"), uid, FIO_MAX)
        items.append(
            f"""<form method="post" action="/panel/tickets/see-all" class="wb-access">
{csrf(request)}<input type="hidden" name="user_id" value="{esc(uid)}">
<input type="hidden" name="return" value="{esc(return_to)}">
<label><input type="checkbox" name="value" value="1" {'checked' if sees_all else ''}
 onchange="this.form.submit()"><span class="wb-access-name">{esc(who)}
<span class="wb-access-pos">{esc(position)}</span></span></label>
</form>""")
    return f"""<div class="card"><h2>Кто видит чужие обращения</h2>
<p class="small mut">В MAX сотрудник без галочки видит только свои обращения. Галочка разрешает
всю очередь, а не только отдел. Системные права в список не попадают. Веб-доступ выдаётся отдельно.</p>{''.join(items)}</div>"""


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
    staff_options, staff_titles = await _staff_choices()
    return_to = safe_return(request.query_params)
    body = f"""<div class="card"><h2>Новое обращение</h2>
<form method="post" action="/panel/tickets/new">{csrf(request)}
<input type="hidden" name="return" value="{esc(return_to)}">
<div class="grid">
<div><label>MAX ID студента</label><input name="student_id" required></div>
<div><label>ФИО (если ещё не регистрировался)</label><input name="full_name"></div>
<div><label>Группа</label><input name="group_code" placeholder="24-23"></div>
<div>{select("category", dict(CATS), "feedback")}</div>
<div>{select("target_admin_id", staff_options, "", titles=staff_titles)}</div>
<div class="full"><label>Текст обращения</label>
<textarea name="text_content" rows="4" required></textarea></div>
<div><label>Кабинет выдачи</label>
<input name="pickup_place" list="pickup-rooms-new" placeholder="для справок - {CERT_PICKUP}"></div>
<datalist id="pickup-rooms-new">{await pickup_options()}</datalist>
</div>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Создать</button></div>
<div><a class="btn btn-grey" href="{esc(return_to)}">Отмена</a></div></div>
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
    return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))


@router.post("/tickets/bulk")
async def tickets_bulk(request: Request):
    actor = await require_form(request)
    data = await request.form()
    chosen = [item for item in value(data, "tids").split(",") if item]
    action = value(data, "action")
    if action not in {"assign", "status", "pickup", "archive"}:
        raise HTTPException(status_code=400, detail="Неизвестное массовое действие")
    if not chosen:
        flash("!Отметьте обращения галочкой слева.")
        return redirect(safe_return(data))
    done, message = await repo.bulk_update(chosen, action, value(data, "value"), actor)
    await repo.log_action(actor, f"массово: {action}", f"{done} обращений")
    flash(message)
    return redirect(safe_return(data))


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
    return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))


@router.post("/tickets/{ticket_id}/ready")
async def ticket_ready(request: Request, ticket_id: int):
    """Кнопка «Готово»: сообщение студенту с кабинетом и закрытие.

    Кабинет 115 подставляется только под справки. Для остального кабинет
    должен быть указан - иначе документ готов, а сказать студенту, где его
    забрать, нечем.
    """
    actor = await require_form(request)
    data = await request.form()
    t = await repo.get_ticket(ticket_id)
    if not t:
        flash("!Обращение не найдено.")
        return redirect(safe_return(data, "/panel/tickets"))
    place = as_str(t["pickup_place"]).strip()
    if not place and is_certificate(t):
        place = CERT_PICKUP
    if not place:
        flash("!Укажите кабинет выдачи: 115 зашит только под справки.")
        return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))
    await repo.update_ticket(ticket_id, actor, status="ready", pickup_place=place)
    await repo.add_ticket_message(ticket_id, actor, "staff",
                                  f"✅ Документ готов. Заберите в кабинете {place}.")
    await notify(t["student_id"],
                 f"✅ Документ по обращению №{ticket_id} готов. Заберите в кабинете {place}.")
    await repo.log_action(actor, "справка готова", f"№{ticket_id}; поля: status, pickup_place")
    flash(f"Обращение №{ticket_id}: документ готов, кабинет {place}. Студенту отправлено уведомление.")
    return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))


@router.post("/tickets/{ticket_id}/archive")
async def ticket_archive(request: Request, ticket_id: int):
    actor = await require_form(request)
    data = await request.form()
    ok, message = await repo.archive_ticket(ticket_id, actor)
    if ok:
        t = await repo.get_ticket(ticket_id, include_archived=True)
        if t:
            await notify(t["student_id"],
                         f"🗄 Обращение №{ticket_id} убрано в архив. Вопрос не решён — "
                         "напишите новое обращение.")
        await repo.log_action(actor, "обращение в архиве", f"№{ticket_id}")
    flash(message if ok else f"!{message}")
    return redirect(safe_return(data, "/panel/tickets"))


@router.post("/tickets/{ticket_id}/restore")
async def ticket_restore(request: Request, ticket_id: int):
    actor = await require_form(request)
    data = await request.form()
    ok, message = await repo.restore_ticket(ticket_id, actor)
    if ok:
        await repo.log_action(actor, "обращение восстановлено", f"№{ticket_id}")
    flash(message if ok else f"!{message}")
    return redirect(safe_return(data, "/panel/tickets?view=archive"))


@router.get("/tickets/{ticket_id}")
async def ticket_card(request: Request, ticket_id: int):
    """Полная карточка для телефона, закладки и прямого перехода."""
    from .workspace import admin_principal, render_workspace

    actor = await admin_principal(request)
    return await render_workspace(request, actor, selected=ticket_id, standalone=True)


@router.get("/tickets/{ticket_id}/admin")
async def ticket_administration(request: Request, ticket_id: int):
    """Редкие операции сис-админа вне ежедневного поля ответа."""
    user = await require_user(request)
    ticket = await repo.get_ticket(ticket_id, include_archived=True)
    if ticket is None:
        raise HTTPException(404, "Обращение не найдено")
    data = dict(ticket)
    student = await repo.get_user(data["student_id"])
    employee = await repo.get_admin(data["target_admin_id"])
    data.update(student_name=as_str(student["full_name"]) if student else "",
                student_group=as_str(student["group_code"]) if student else "",
                staff_name=as_str(employee["full_name"]) if employee else "")
    from .workspace import workspace_filters

    _query, filters, _page = workspace_filters(request.query_params)
    view = "archive" if data.get("deleted_at") else ""
    if view:
        filters["view"] = view
    live_query = _filter_query(filters)
    back = f"/panel/tickets/{ticket_id}?{live_query}"
    body = (f'<p><a class="btn btn-grey" href="{esc(back)}">'
            + icon("chevron-left", 18) + ' К рабочему месту</a></p>')
    body += await _ticket_workbench(request, data, await bot_open_link(), view, live_query)
    return page(f"Администрирование обращения №{ticket_id}", body, user, "/tickets")


@router.post("/tickets/{ticket_id}/delete")
async def ticket_delete(request: Request, ticket_id: int):
    actor = await require_form(request)
    data = await request.form()
    t = await repo.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    done, message = await repo.delete_ticket(ticket_id)
    if not done:
        flash(f"!{message}")
        return redirect(safe_return(data, "/panel/tickets"))
    await repo.log_action(actor, "обращение удалено", f"№{ticket_id} (автор {t['student_id']})")
    await notify(t["student_id"],
                 f"🗑 Обращение №{ticket_id} удалено администратором. Если вопрос остался актуальным — "
                 "напишите новое обращение.")
    log.warning("панель: %s (сис-админ %s)", message, actor)
    flash(message)
    return redirect(safe_return(data, "/panel/tickets"))


async def _answerer_choices(keep_current: str = "", current_name: str = "") -> tuple[dict, dict]:
    """Сотрудники для выбора «Отвечает как»: те, кто ведёт обращение, первыми.

    Отдельный помощник, а не переиспользование выбора исполнителя: там список
    нужен, чтобы НАЗНАЧИТЬ, здесь - чтобы ОТВЕТИТЬ от имени. И подпись важна
    другая: человеку надо знать, как его увидит студент.
    """
    choices: dict[str, str] = {}
    titles: dict[str, str] = {}
    if keep_current:
        choices[keep_current] = f"{fio(current_name, keep_current, FIO_MAX)} (ведёт это обращение)"
        titles[keep_current] = "ответ уйдёт от этого имени"
    for row in await repo.list_staff():
        person = dict(row)
        uid = as_str(person.get("user_id"))
        if not uid or uid in choices:
            continue
        choices[uid] = fio(person.get("full_name"), uid, FIO_MAX)
        titles[uid] = as_str(person.get("position") or person.get("role") or "—")
    return choices, titles


def _row_get(person, key: str) -> str:
    """Значение поля строки сотрудника: строки sqlite не поддерживают .get()."""
    if person is None:
        return ""
    try:
        return as_str(person[key])
    except (IndexError, KeyError):
        return ""


ANSWERER_UNKNOWN = "сотрудник колледжа"
# Подпись, когда сотрудника найти не удалось: слово, а не его MAX ID.
# Письмо вида «Отвечает: test-1» - техническая заглушка вместо человека.


def _answerer_label(person, fallback_id: str) -> str:
    # Как сотрудник выглядит у студента: должность и имя.
    if person is None:
        return ANSWERER_UNKNOWN
    name = _row_get(person, "full_name") or as_str(fallback_id)
    position = _row_get(person, "position") or _row_get(person, "role")
    return f"{position}, {name}" if position else name


def _ticket_subject(t) -> str:
    """О чём обращение - одной строкой для уведомления.

    Раньше студент получал «Ответ на обращение №9» и по этому не мог понять ни
    что за обращение, ни от кого ответ. Тема обращения - то, что человек сам и
    писал, поэтому узнаётся сразу.
    """
    topic = as_str(t["topic"]).strip()
    if topic:
        return topic
    return plain(CATS.get(as_str(t["category"]), "обращение"))


@router.post("/tickets/{ticket_id}/reply")
async def ticket_reply(request: Request, ticket_id: int):
    """Ответ студенту: свой текст или шаблон ответа из списка под формой.

    Раньше поле «шаблон» в форме было только для вида: обработчик его не читал,
    поэтому выбор ничего не делал, а подстановки {ФИО} и остальные в панели не
    подставлялись вовсе (в боте это делает handlers.tickets). Правила здесь те же,
    что в боте:

    * выбран шаблон, а поле ответа пустое - уходит текст шаблона;
    * сотрудник написал своё - его текст главнее, шаблон не навязывается;
    * подстановки делаются и в тексте шаблона, и в собственном тексте;
    * шаблон, удалённый пока сотрудник писал ответ, - понятное сообщение, а не
      пустой ответ студенту;
    * счётчик применений растёт только когда ответ действительно ушёл шаблоном.
    """
    user = await require_form(request)
    data = await request.form()
    own = as_str(data.get("text", "")).strip()
    t = await repo.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    template_id = to_int(value(data, "template"), 0)
    template = await repo.get_template(template_id) if template_id else None
    if template_id and not template:
        flash("!Шаблон удалён, пока вы писали ответ. Выберите другой или отправьте "
              "свой текст.")
        return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))
    # текст сотрудника главнее: подсказка из браузера могла подставить шаблон,
    # который он переписал, - такой ответ применением шаблона не считается
    by_template = bool(template) and not own
    raw = as_str(template["text"]) if by_template else own
    if not raw.strip():
        flash("!Пустой ответ не отправлен: напишите текст или выберите шаблон.")
        return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))
    # Автор ответа - выбранный сотрудник. Вошедший в панель может отвечать за
    # кого угодно, в том числе за тестового, и студент должен видеть, ОТ КОГО
    # пришёл ответ, а не кто нажал кнопку в панели.
    answerer_id = as_str(value(data, "as_staff")).strip() or user
    answerer = await repo.get_admin(answerer_id)
    if answerer is None:
        # выбранного нет (удалили) - отвечаем от того, кто ведёт обращение
        answerer_id, answerer = as_str(t["target_admin_id"]), None
    if answerer is None:
        answerer = await admin_of(user)
    text = await render_template(raw, t, answerer)
    unknown = unknown_placeholders(raw)
    await repo.add_ticket_message(ticket_id, answerer_id, "staff", text)
    if by_template:
        await repo.count_template_use(template_id)
    if as_str(t["status"]) in ("new", "in_progress"):
        await repo.transition_ticket_status(ticket_id, t["status"], "accepted",
                                            actor_id=answerer_id)
    await notify(
        t["student_id"],
        "💬 Ответ по обращению №{tid} — {subject}\nОтвечает: {who}\n\n{text}".format(
            tid=ticket_id, subject=_ticket_subject(t), who=_answerer_label(answerer, answerer_id),
            text=text),
        [[{"type": "callback", "text": "📂 Открыть", "payload": f"t:{ticket_id}"}]],
    )
    log.info("панель: ответ на обращение %s: вошёл %s, ответил за %s (шаблон %s)",
             ticket_id, user, answerer_id, template_id if by_template else "—")
    # в журнал - кто нажал и за кого ответил; текст ответа туда не пишем
    if answerer_id != user:
        await repo.log_action(user, "ответ за сотрудника",
                              f"№{ticket_id} от имени {answerer_id}")
    note = (f"Ответ на обращение №{ticket_id} отправлен от имени "
            f"{_answerer_label(answerer, answerer_id)}.")
    if by_template:
        note += f" Шаблон «{as_str(template['title'])}» отправлен как есть."
    if unknown:
        # молчать нельзя: в письме студенту осталось «{причина}» как есть
        note += (" Не подставилось: "
                 + ", ".join("{%s}" % name for name in unknown)
                 + " - эти слова ушли студенту как есть.")
    flash(f"!{note}" if unknown else note)
    return redirect(safe_return(data, f"/panel/tickets?t={ticket_id}"))


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
    return redirect(safe_return(form_data, f"/panel/tickets?t={ticket_id}"))


def _tickets_queue(request: Request, rows, latest: dict, live_query: str, selected,
                    archived: bool) -> str:
    """Левая колонка: отметки для массовых действий и переход в карточку.

    Ссылки на обращение строятся по ``live_query`` - с фильтрами и, когда мы в
    архиве, с ``view=archive``. По ``query`` (без вида) клик в архиве вёл в
    рабочее место, где архивного обращения нет: карточка справа оставалась
    заглушкой «Выберите обращение», и с архивом нельзя было не сделать ничего.
    """
    if not rows:
        return '<div class="card mut">Обращений нет.</div>'
    visible = rows[:TICKETS_PAGE]
    ids = ",".join(str(row["ticket_id"]) for row in visible)
    items = []
    for row in visible:
        ticket_id = row["ticket_id"]
        mark = "wb-on" if selected and int(selected["ticket_id"]) == int(ticket_id) else ""
        waiting = bool(row["is_waiting"])
        # db.many отдаёт sqlite3.Row, поэтому по именам колонок идём через dict()
        data = dict(row)
        # ФИО из базы приходит целиком, и резать его нельзя: очередь сверяют
        # с записью человека. Поэтому ФИО - своей строкой, группа - рядом с
        # ним отдельным элементом, а текст обращения идёт следующей строкой.
        who = fio(data.get("student_name"), data.get("student_id"), FIO_MAX)
        group = " ".join(as_str(data.get("student_group")).split())
        group_html = f'<span class="wb-group">{esc(group)}</span>' if group else ""
        status = esc(plain(STATUS.get(data["status"], data["status"])))
        if waiting:
            status += f' <span class="wb-wait">{icon("clock", 14)}</span>'
        age = _ticket_timer(row)
        target = _tickets_url(dict(parse_qsl(live_query)), t=ticket_id)
        items.append(
            f'<label class="wb-item {mark}" data-hk><input type="checkbox" name="tids" value="{esc(ticket_id)}">'
            f'<a href="{esc(target)}">'
            f'<span class="wb-head"><b>№{esc(ticket_id)}</b>'
            f'<span class="wb-status">{status}</span>'
            f'<span class="wb-date">{esc(data["updated_at"])}</span></span>{age}'
            f'<span class="wb-who"><span class="wb-fio">{esc(who)}</span>{group_html}</span>'
            f'<span class="wb-text" title="{esc(data["text_content"])}">'
            f'{esc(cut_plain(data["text_content"], 90))}</span>'
            f'</a></label>')
    total = int(rows[0]["filtered_total"])
    head = (f'{icon("archive", 18)} Из архива · {total}' if archived
            else f'{icon("inbox", 18)} Очередь · {total}')
    more = (f'<p class="small mut">Показано {len(visible)} из {total} — сузьте фильтр '
            f'по статусу, чтобы увидеть нужное.</p>' if total > len(visible) else "")
    return f"""<div class="card"><h2>{head}</h2>
<form method="post" action="/panel/tickets/bulk">{csrf(request)}
<input type="hidden" name="return" value="/panel/tickets?{esc(live_query)}">
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


async def _ticket_workbench(request: Request, t, bot: str = "", view: str = "",
                            live_query: str = "") -> str:
    """Правая колонка: карточка со всеми правками и быстрыми ответами.

    Формы правки и ответа помечены классами ``wb-edit`` и ``wb-reply`` - на
    печати их нет, поэтому на бумагу попадает переписка, а не пустые поля.
    """
    ticket_id = int(t["ticket_id"])
    return_to = _tickets_url(dict(parse_qsl(live_query)), t=ticket_id)
    return_input = f'<input type="hidden" name="return" value="{esc(return_to)}">'
    # ФИО целиком; без ФИО подписью становится MAX ID (person_label) - так же,
    # как в очереди, чтобы человека узнавали в обоих списках одинаково.
    student_name = fio(t["student_name"], t["student_id"])
    student_group = as_str(t["student_group"]) or "—"
    messages = await repo.ticket_thread(ticket_id, 100)
    events = await repo.ticket_events(ticket_id, 50)
    staff_options, staff_titles = await _staff_choices(as_str(t["target_admin_id"]),
                                                       as_str(t["staff_name"]))
    # «Отвечает как»: ответ уходит от имени сотрудника, а не от того, кто сидит
    # в панели. По умолчанию - тот, кто ведёт это обращение.
    # исполнителя обращения ставим по умолчанию. Если он не назначен, ничего не
    # выбрано: обработчик ответа подставит того, кто вошёл в панели.
    answerer_options, answerer_titles = await _answerer_choices(
        as_str(t["target_admin_id"]), as_str(t["staff_name"]))
    answerer_default = as_str(t["target_admin_id"])
    template_options = {"": "— шаблон —"}
    template_texts: dict[str, dict] = {}
    unknown_names: list = []
    for row in await repo.list_templates():
        template = dict(row)
        key = as_str(template.get("template_id") or template.get("id"))
        if not key:
            continue
        draft = as_str(template.get("text"))
        template_options[key] = as_str(template.get("title"))
        # значение пункта - id шаблона, поэтому текст кладём в data-text: без
        # него форме нечего подставить в поле ответа
        template_texts[key] = {"data-text": draft}
        unknown_names += [name for name in unknown_placeholders(draft)
                          if name not in unknown_names]
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
<form method="post" action="/panel/tickets/{ticket_id}/edit" class="wb-edit">{csrf(request)}{return_input}
<div class="grid">
<div class="full"><label>Текст обращения</label>
<textarea name="text_content" rows="2">{esc(t['text_content'])}</textarea></div>
<div>{select("target_admin_id", staff_options, "", titles=staff_titles)}</div>
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
<p><button class="btn-ok">Сохранить</button></p>
<p class="small mut">Пустое поле не затирает старое значение. «Готово» пишет
студенту кабинет выдачи. Кабинет {CERT_PICKUP} зашит только
под справки; для остального нужно указать кабинет.</p></form>
<div class="wb-tools">
{_action_form(request, f'/panel/tickets/{ticket_id}/ready',
              f'{icon("check", 16)} Готово', fields=return_input) if view != "archive" else ""}
{_action_form(request, f'/panel/tickets/{ticket_id}/' + ('restore' if view == 'archive' else 'archive'),
              'Восстановить' if view == 'archive' else 'В архив', fields=return_input)}
</div>"""
    # в архиве ссылка на обращение тоже должна вести в архив: получатель открыл
    # бы рабочее место, где этого обращения уже нет
    card_link = f"/tickets?t={ticket_id}" + ("&view=archive" if view == "archive" else "")
    # шапка карточки - то, что должно попасть на бумагу
    head = f"""<div class="card"><h2>Обращение №{ticket_id}
<span class="pill">{esc(plain(STATUS.get(as_str(t['status']), as_str(t['status']))))}</span></h2>
{_ticket_timer(t)}
<p class="wb-who">{icon('students', 16)}<span class="wb-fio">{esc(student_name)}</span>
<span class="wb-group">{esc(student_group)}</span>
<span class="wb-id">ID {code_cell(t['student_id'], "MAX ID студента скопирован")}</span>
<span class="wb-when">создано {esc(fmt_when(t['created_at']))}</span></p>
<p class="small mut wb-tools">{open_in_bot(bot)}
{copy_btn(panel_link(card_link), "Ссылка на обращение скопирована")}</p>
{edit}</div>"""
    return f"""{head}
<div class="card"><h2>Быстрый ответ</h2>
<form method="post" action="/panel/tickets/{ticket_id}/reply" class="wb-reply">{csrf(request)}{return_input}
<textarea name="text" rows="3" placeholder="Ответ студенту — уйдёт в MAX"></textarea>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Отправить</button></div>
<div>{select("template", template_options, "", datas=template_texts)}</div>
<div>{select("as_staff", answerer_options, answerer_default, titles=answerer_titles,
         label="Отвечает как")}</div>
</div></form>
<p class="small mut">Ответ уходит студенту ОТ ИМЕНИ выбранного сотрудника - можно
ответить за тестового, не заходя в MAX. Сам сотрудник при этом ничего не
получает: у тестовых нет настоящего MAX ID. Шаблон можно
выбрать и не править: подстановки подставятся сами, по данным обращения. Свой текст
важнее шаблона - если написать его, шаблон не навязывается. {esc(_tpl_hint(unknown_names))}</p></div>
<div class="card"><h2>Переписка</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Текст</th></tr>{thread}</table></div>
<div class="card"><h2>История изменений</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Событие</th></tr>{event_rows}</table></div>"""
