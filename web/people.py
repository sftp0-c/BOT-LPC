"""Люди: реестр, карточка человека, студенты, сотрудники, сис-админы, «без прав»."""
import csv
import io

import repository as repo
from fastapi import HTTPException, Request
from fastapi.responses import Response
from handlers.admin import STAFF_ROLES
from handlers.common import notify
from panel_theme import icon
from utils import (STAFF_CATS, STATUS, as_str, fmt_when, is_sysadmin_role, norm_group, profile_url,
                   to_int)

from .access import request_status_cell
from .common import (_action_form, code_cell, csrf, esc, fio_brief, flag, flash, form, input, log,
                     minutes_text, page, page_window, pager, pages_of, pill, plain, redirect,
                     require_form, require_user, select, value, window_tail)
from .router import router


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


STUDENTS_PAGE = 50       # студентов на страницу списка


@router.get("/students")
async def students(request: Request, group: str = "", consent: str = "", page_no: int = 1):
    """Студенты: фильтр по группе и согласию, постранично.

    Раньше список обрывался на 200 строках и дальше некуда было пойти: в группе
    больше двухсот человек - вторая половина просто не существовала для панели.
    Теперь страниц по 50, внизу переход, а фильтры в нём не теряются.
    """
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    window = page_window(page_no, STUDENTS_PAGE)
    only_no_consent = consent == "0"
    if only_no_consent:
        found = [dict(row) for row in await repo.users_without_consent(window)]
        if group:
            found = [row for row in found if norm_group(row["group_code"]) == norm_group(group)]
    else:
        # sqlite3.Row не умеет .get - приводим строки к словарям
        found = [dict(row) for row in await repo.list_users(window)]
        # группу отбираем сами: в repo.list_users условие приклеивается к
        # «ON c.user_id=u.user_id» без пробела, и выборка с group_code падает.
        # Правка того модуля не здесь, а фильтр странице нужен - иначе нельзя
        # ни сузить список, ни листать его по страницам.
        if group:
            found = [row for row in found if norm_group(row["group_code"]) == norm_group(group)]
    pages = pages_of(len(found), STUDENTS_PAGE)
    rows = found[(page_no - 1) * STUDENTS_PAGE: page_no * STUDENTS_PAGE]
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
    pages_bar = pager("/panel/students", [("group", group), ("consent", consent)],
                      page_no, pages, f"студентов: {len(found)}")
    tail = window_tail(len(found), window, "сузьте список группой или фильтром согласия.")
    return page("Студенты",
                f'<div class="card">{head}{table}{pages_bar}{tail}</div>',
                user, "/students")


# ── сотрудники и сис-админы ───────────────────────────────────────────────────
def _vacation_replacement(staff, all_rows: list[dict], away: dict) -> dict | None:
    """Кто замещает сотрудника в отпуске - по уже загруженным строкам, без запросов.

    Правило ровно то же, что у ``repo.vacation_replacement``: сначала сосед по
    той же должности, потом сосед по разделу обращений, и оба - кто сейчас на
    месте (не в отпуске). Здесь оно нужно без обращения к базе: список
    сотрудников уже загружен целиком, а поштучный поиск стоил два запроса на
    каждого человека, кто в отпуске, и по одному на каждого кандидата.
    """
    def pick(field: str) -> dict | None:
        wanted = as_str(staff.get(field, ""))
        if not wanted:
            return None
        for candidate in sorted(all_rows, key=lambda item: as_str(item["full_name"])):
            uid = as_str(candidate["user_id"])
            if uid == as_str(staff["user_id"]) or is_sysadmin_role(as_str(candidate["role_type"])):
                continue
            if as_str(candidate.get(field, "")) != wanted or uid in away:
                continue
            return candidate
        return None

    return pick("role") or pick("ticket_category")


@router.get("/staff")
async def staff_list(request: Request, q: str = ""):
    user = await require_user(request)
    # строки нужны словарями: заместителя ищем по полям, а не по индексам
    every_staff = [dict(row) for row in await repo.all_admins()]
    rows = every_staff
    sysadmins = await repo.list_sysadmins()
    activity = await repo.staff_activity(90)
    # кто сейчас в отпуске - один запрос на всех (vacations_bulk вместо
    # on_vacation на каждого), по той же причине: список сотрудников открывают
    # чаще всего, и он дорожался вместе с числом людей
    away = await repo.vacations_bulk()
    needle = as_str(q).strip().lower()
    if needle:
        # фильтр прячет строки из таблицы, но не из подбора заместителя: тот
        # ищется по всем сотрудникам, как и раньше
        rows = [row for row in every_staff if needle in " ".join([
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
        vacation = away.get(uid)
        vacation_mark = ""
        if vacation:
            until = as_str(row["vacation_until"])
            replacement = _vacation_replacement(row, every_staff, away)
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
PEOPLE_PAGE = 100       # человек на страницу реестра


NOSTAFF_PAGE = 60       # человек на страницу «Без прав»


def profile_cell(username) -> str:
    """Ссылка на профиль MAX; без ника — прочерк (ник бывает скрытым)."""
    name = as_str(username).strip()
    link = profile_url(name)
    if not link:
        return "<span class='mut'>ник скрыт</span>"
    return f'<a href="{esc(link)}" target="_blank" rel="noopener">@{esc(name)}</a>'


def _people_table(rows) -> str:
    from webpanel import fio   # см. комментарий в webpanel.py
    body = []
    for row in rows:
        # Полное ФИО - то, по чему человека ищут в записи, поэтому целиком;
        # инициалы под ним помогают сориентироваться, но имя не заменяют.
        name = fio(row["fio"] or row["staff_name"] or row["display_name"], row["user_id"])
        brief = fio_brief(name)
        brief_html = f'<div class="small mut wb-brief">{esc(brief)}</div>' if brief else ""
        body.append(
            f"<tr data-hk><td><a href='/panel/people/{esc(row['user_id'])}' class='wb-name'>"
            f"<b>{esc(name)}</b></a>{brief_html}"
            f"<div class='small mut'>{esc(repo.KIND_TITLES.get(repo.contact_kind(row), '—'))}</div></td>"
            f"<td>{esc(row['group_code'] or row['position'] or '—')}</td>"
            f"<td>{code_cell(row['user_id'], 'MAX ID скопирован')}</td>"
            f"<td>{profile_cell(row['username'])}</td>"
            f"<td>{esc(row['tickets'])}</td>"
            f"<td class='small mut'>{esc(fmt_when(row['last_seen']))}</td></tr>")
    empty = "<tr><td class='mut'>Никого не найдено</td></tr>"
    return ("<table><tr><th>Человек</th><th>Группа / должность</th><th>MAX ID</th>"
            f"<th>Профиль MAX</th><th>Обращений</th><th>Был</th></tr>"
            f"{''.join(body) or empty}</table>")


@router.get("/people")
async def people_list(request: Request, kind: str = "", q: str = "", page_no: int = 1):
    """Реестр всех, кто писал боту: фильтр, поиск и постраничный просмотр.

    Здесь OFFSET поддерживается, поэтому страницы настоящие: в базе лежит
    LIMIT/OFFSET, а не «прочитать побольше и отрезать».
    """
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    overview = await repo.people_overview()
    total = await repo.people_count(kind, q)
    pages = pages_of(total, PEOPLE_PAGE)
    page_no = min(page_no, pages)         # ссылка на страницу за последней - последняя
    rows = await repo.people(kind, q, limit=PEOPLE_PAGE, offset=(page_no - 1) * PEOPLE_PAGE)
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
    pages_bar = pager("/panel/people", [("kind", kind), ("q", q)],
                      page_no, pages, f"человек: {total}")
    body = (f'<div class="card"><p class="small mut">{esc(stats)} · показано {len(rows)} из {total}</p>'
            f'{filters}{_people_table(rows)}{pages_bar}</div>')
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
    from webpanel import fio   # см. комментарий в webpanel.py
    user = await require_user(request)
    card = await repo.user_card(user_id)
    if not card:
        return page("Пользователь", '<div class="card msg-bad">Этот человек ещё не писал боту.</div>', user, "/people")
    name = fio(card["fio"] or card["staff_name"] or card["display_name"], user_id)
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
    # ФИО целиком, а инициалы - отдельной строкой под ним: сисадмин узнаёт
    # человека с ходу, но полное имя при этом не теряется.
    brief = fio_brief(name)
    brief_html = f'<p class="small mut wb-brief">{esc(brief)}</p>' if brief else ""
    body = f"""
<div class="card"><h2 class="wb-name">{esc(name)}</h2>{brief_html}
<table>
<tr><th>Роль в боте</th><td>{esc(repo.KIND_TITLES.get(card['kind'], card['kind']))}</td></tr>
<tr><th>MAX ID</th><td>{code_cell(user_id, "MAX ID скопирован")}</td></tr>
<tr><th>Профиль MAX</th><td>{profile_cell(card['username'])}</td></tr>
<tr><th>Студент</th><td>{esc(card['fio'] or '—')}
<span class="wb-group">{esc(card['group_code'] or '—')}</span></td></tr>
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


@router.get("/nostaff")
async def nostaff_page(request: Request, page_no: int = 1):
    """Кто писал боту, но прав сотрудника не имеет: выдать их можно прямо отсюда."""
    user = await require_user(request)
    q = request.query_params.get("q", "")
    page_no = max(1, to_int(page_no, 1))
    window = page_window(page_no, NOSTAFF_PAGE)
    found = await repo.people_without_staff(window, q)
    total = await repo.people_without_staff_count(q)
    pages = pages_of(len(found), NOSTAFF_PAGE)
    rows = found[(page_no - 1) * NOSTAFF_PAGE: page_no * NOSTAFF_PAGE]
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
    pages_bar = pager("/panel/nostaff", [("q", q)], page_no, pages, f"человек: {total}")
    tail = window_tail(len(found), window, "Найдите нужного поиском по ФИО или нику.")
    body_all = f"""
<div class="card"><h2>{icon("user-off", 20)} Без прав сотрудника: {total}</h2>{head}{table}{pages_bar}
<p class="small mut">Сотсортировано по последнему обращению. Нажмите «Сделать сотрудником» — карточка
создастся с именем из реестра, должность и отдел можно поправить там же.</p>{tail}</div>"""
    return page("Без прав", body_all, user, "/nostaff")
