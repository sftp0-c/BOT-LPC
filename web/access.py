"""Коды сотрудников, заявки на роль и приглашение по ссылке (без входа в панель)."""
import config
import database as db
import repository as repo
from fastapi import Request
from fastapi.responses import HTMLResponse
from handlers.admin import approve_request, reject_request
from handlers.common import notify
from panel_theme import icon
from utils import (CODE_TTL_CHOICES, as_str, fmt_when, gen_code, norm_code, to_int, ttl_label)

from .common import (JOIN_STYLE, bot_profile_link, code_cell, csrf, esc, flash, join_link, log, page,
                     pill, redirect, require_form, require_user, select, state_pill, value)
from .router import open_router, router


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
