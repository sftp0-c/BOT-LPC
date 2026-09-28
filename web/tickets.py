"""Обращения: рабочее место, карточка, массовые действия, архив и шаблоны ответов."""
import csv
import io
import os

import clock
import database as db
import repository as repo
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from handlers.common import notify
from panel_theme import icon
from utils import (CATS, OPEN_STATUSES, STAFF_CATS, STATUS, as_str, cut_plain, fmt_when,
                    group_code, to_int)

from .common import (FIO_MAX, _action_form, bot_open_link, code_cell, copy_btn, csrf, esc, fio, flash, form, log, open_in_bot,
                     page, page_window, pager, pages_of, panel_link, plain, redirect, require_form,
                     require_user, select, value, window_tail)
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
    pages_bar = pager("/panel/templates", [], page_no, pages, f"шаблонов: {total}")
    tail = window_tail(len(found), window, "Ненужные шаблоны лучше удалить - их ищут по названию.")
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
        return redirect("/panel/tickets")
    await repo.set_staff_see_all(user_id, flag)
    await repo.log_action(actor, "доступ к чужим обращениям",
                          f"{user_id}: {'видит все' if flag else 'только свои'}")
    flash(f"{user_id}: {'видит все обращения' if flag else 'видит только свои'}.")
    return redirect("/panel/tickets")


async def _tickets_access(request: Request) -> str:
    """Кто видит чужие обращения: галочка рядом с именем сотрудника.

    Права читает staff_sees_all_bulk(): раньше на каждого сотрудника уходил
    свой SELECT, и на самой посещаемой странице панели запросов было ровно
    столько же, сколько сотрудников, - открыть «Обращения» подтормаживало тем
    сильнее, чем больше людей работало.
    """
    rows = [row for row in await repo.list_staff()]
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
<label><input type="checkbox" name="value" value="1" {'checked' if sees_all else ''}
 onchange="this.form.submit()"><span class="wb-access-name">{esc(who)}
<span class="wb-access-pos">{esc(position)}</span></span></label>
</form>""")
    return f"""<div class="card"><h2>Кто видит чужие обращения</h2>
<p class="small mut">Сотрудник без галочки видит только свои обращения. Системные права
в список не попадают - они и так видят всё.</p>{''.join(items)}</div>"""


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
    body = f"""<div class="card"><h2>Новое обращение</h2>
<form method="post" action="/panel/tickets/new">{csrf(request)}
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
        # ФИО из базы приходит целиком, и резать его нельзя: очередь сверяют
        # с записью человека. Поэтому ФИО - своей строкой, группа - рядом с
        # ним отдельным элементом, а текст обращения идёт следующей строкой.
        who = fio(data.get("student_name"), data.get("student_id"), FIO_MAX)
        group = " ".join(as_str(data.get("student_group")).split())
        group_html = f'<span class="wb-group">{esc(group)}</span>' if group else ""
        status = esc(plain(STATUS.get(data["status"], data["status"])))
        if waiting:
            status += f' <span class="wb-wait">{icon("clock", 14)} ждёт ответа</span>'
        items.append(
            f'<label class="wb-item {mark}" data-hk><input type="checkbox" name="tids" value="{esc(ticket_id)}">'
            f'<a href="/panel/tickets?{query}&t={esc(ticket_id)}">'
            f'<span class="wb-head"><b>№{esc(ticket_id)}</b>'
            f'<span class="wb-status">{status}</span>'
            f'<span class="wb-date">{esc(fmt_when(data["updated_at"]))}</span></span>'
            f'<span class="wb-who"><span class="wb-fio">{esc(who)}</span>{group_html}</span>'
            f'<span class="wb-text" title="{esc(data["text_content"])}">'
            f'{esc(cut_plain(data["text_content"], 90))}</span>'
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
    # ФИО целиком; без ФИО подписью становится MAX ID (person_label) - так же,
    # как в очереди, чтобы человека узнавали в обоих списках одинаково.
    student_name = fio(t["student_name"], t["student_id"])
    student_group = as_str(t["student_group"]) or "—"
    messages = await repo.ticket_thread(ticket_id, 100)
    events = await repo.ticket_events(ticket_id, 50)
    staff_options, staff_titles = await _staff_choices(as_str(t["target_admin_id"]),
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
<p class="wb-who">{icon('students', 16)}<span class="wb-fio">{esc(student_name)}</span>
<span class="wb-group">{esc(student_group)}</span>
<span class="wb-id">ID {code_cell(t['student_id'], "MAX ID студента скопирован")}</span>
<span class="wb-when">создано {esc(fmt_when(t['created_at']))}</span></p>
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
