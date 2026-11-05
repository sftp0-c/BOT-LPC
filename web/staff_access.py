"""Отдельная выдача веб-доступа сотрудникам только из панели сис-админа."""
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from store.staff import get_admin
from store.web_access import (
    WEB_PERMISSIONS, WEB_PERMISSION_PRESETS, get_staff_web_access, list_staff_web_access,
    normalize_staff_web_id, revoke_staff_web_sessions, set_staff_web_access,
)

from .common import csrf, esc, flash, page, require_form, require_user
from .router import router

PERMISSION_LABELS = {
    "reply": "Ответы студентам", "status": "Смена статуса", "note": "Внутренние заметки",
    "forward": "Передача обращений", "templates": "Шаблоны ответов", "attachments": "Вложения",
    "export": "Выгрузка CSV", "view_others": "Просмотр чужих обращений",
    "manage_others": "Изменение чужих обращений",
}
PRESET_LABELS = {"custom": "Настроить отдельно", "view": "Только просмотр своих", "work": "Работа со своими"}


def _secure_response(response):
    response.headers.update({"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
                             "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})
    return response


async def _staff(user_id: str) -> dict:
    row = await get_admin(user_id) if normalize_staff_web_id(user_id) else None
    if not row or row["role_type"] != "staff" or row["is_test"]:
        raise HTTPException(404, "Сотрудник для веб-доступа не найден")
    return dict(row)


@router.get("/staff-access", include_in_schema=False)
async def staff_web_access_list(request: Request):
    user = await require_user(request)
    search = request.query_params.get("q", "")[:100]
    raw_page = request.query_params.get("page", "1")
    number = min(10000, max(1, int(raw_page))) if raw_page.isascii() and raw_page.isdigit() else 1
    rows = await list_staff_web_access(search, limit=51, offset=(number - 1) * 50)
    body = f"""<section class="card"><p>Вход в кабинет выдаётся отдельно от роли в MAX.
Существующие сотрудники не получают веб-доступ автоматически. Тестовые сотрудники и сис-админы не входят в этот список.</p>
<form method="get" action="/panel/staff-access" class="filters"><label for="staff-access-search">Сотрудник</label>
<input id="staff-access-search" name="q" value="{esc(search)}" maxlength="100" placeholder="ФИО, должность или MAX ID">
<button class="btn" type="submit">Найти</button></form></section>
<section class="card"><div class="table-wrap"><table><thead><tr><th>Сотрудник</th><th>Должность</th><th>Веб-доступ</th><th>Действие</th></tr></thead><tbody>"""
    for row in rows[:50]:
        body += (f'<tr><td>{esc(row["full_name"])}<br><small>{esc(row["user_id"])}</small></td>'
                 f'<td>{esc(row["position"]) or "—"}</td><td>{"Включён" if row["enabled"] else "Выключен"}</td>'
                 f'<td><a class="btn" href="/panel/staff-access/{esc(row["user_id"])}">Настроить</a></td></tr>')
    if not rows:
        body += '<tr><td colspan="4">Подходящих сотрудников нет.</td></tr>'
    body += '</tbody></table></div></section><nav class="pager" aria-label="Страницы сотрудников">'
    for visible, target, label in ((number > 1, number - 1, "Назад"), (len(rows) > 50, number + 1, "Далее")):
        if visible:
            body += f'<a class="btn" href="/panel/staff-access?{esc(urlencode({"q": search, "page": target}))}">{label}</a>'
    body += '</nav>'
    return _secure_response(page("Веб-доступ сотрудников", body, user, "/staff-access"))


@router.get("/staff-access/{user_id}", include_in_schema=False)
async def staff_web_access_edit(request: Request, user_id: str):
    user = await require_user(request)
    staff = await _staff(user_id)
    access = await get_staff_web_access(user_id) or {"enabled": 0, "permissions": []}
    selected = set(access["permissions"])
    options = ''.join(f'<option value="{name}">{label}</option>' for name, label in PRESET_LABELS.items())
    checks = ''.join(
        f'<label class="check"><input type="checkbox" name="permissions" value="{name}"'
        f'{" checked" if name in selected else ""}><span>{PERMISSION_LABELS[name]}</span></label>'
        for name in WEB_PERMISSIONS)
    body = f"""<section class="card"><h2>{esc(staff['full_name'])}</h2><p>{esc(staff['position'])}</p>
<p>Просмотр собственных обращений включён в сам доступ. Право изменения чужих обращений включает их просмотр.
Право MAX «видеть все» не выдаёт веб-права.</p>
<form method="post" action="/panel/staff-access/{esc(user_id)}">{csrf(request)}
<label for="staff-access-enabled">Доступ к кабинету</label><select id="staff-access-enabled" name="enabled">
<option value="0"{" selected" if not access['enabled'] else ""}>Выключен</option>
<option value="1"{" selected" if access['enabled'] else ""}>Включён</option></select>
<label for="staff-access-preset">Набор прав</label><select id="staff-access-preset" name="preset">{options}</select>
<p>«Работа со своими»: ответы, статусы, заметки, передача, шаблоны и вложения. Выгрузка и чужие обращения выключены.
Отдельные флажки учитываются только при выборе «Настроить отдельно».</p>
<fieldset><legend>Отдельные права</legend>{checks}</fieldset>
<button class="btn btn-primary" type="submit">Сохранить и отозвать прежние сессии</button></form></section>
<section class="card"><h2>Активные входы</h2><p>Отзыв завершает все сессии сотрудника и гасит выданные коды.
Разрешение на новый вход остаётся.</p><form method="post" action="/panel/staff-access/{esc(user_id)}/revoke">
{csrf(request)}<button class="btn" type="submit">Отозвать все сессии</button></form></section>"""
    return _secure_response(page("Веб-доступ сотрудника", body, user, "/staff-access"))


@router.post("/staff-access/{user_id}", include_in_schema=False)
async def staff_web_access_save(request: Request, user_id: str):
    actor = await require_form(request)
    data = await request.form()
    allowed = {"csrf", "enabled", "preset", "permissions"}
    if set(data) - allowed or any(len(data.getlist(key)) != 1 for key in ("csrf", "enabled", "preset")):
        raise HTTPException(400, "Некорректная форма доступа")
    enabled, preset = data.get("enabled"), data.get("preset")
    requested = data.getlist("permissions")
    if enabled not in ("0", "1") or preset not in PRESET_LABELS or any(
            not isinstance(value, str) or value not in WEB_PERMISSIONS for value in requested):
        raise HTTPException(400, "Неизвестный набор прав")
    permissions = requested if preset == "custom" else WEB_PERMISSION_PRESETS[preset]
    if not await set_staff_web_access(user_id, enabled == "1", permissions, actor):
        raise HTTPException(403, "Права сотрудника или сис-админа изменились. Обновите страницу.")
    flash("Изменения сохранены. Прежние сессии и коды отозваны.")
    return _secure_response(RedirectResponse(f"/panel/staff-access/{user_id}", status_code=303))


@router.post("/staff-access/{user_id}/revoke", include_in_schema=False)
async def staff_web_access_revoke(request: Request, user_id: str):
    actor = await require_form(request)
    if not normalize_staff_web_id(user_id) or not await revoke_staff_web_sessions(user_id, actor):
        raise HTTPException(403, "Доступ не найден или права сис-админа изменились.")
    flash("Прежние сессии и коды отозваны. Разрешение на новый вход сохранено.")
    return _secure_response(RedirectResponse(f"/panel/staff-access/{user_id}", status_code=303))
