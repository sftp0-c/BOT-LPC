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
PERMISSION_HINTS = {
    "reply": "Отправлять ответы в MAX", "status": "Принимать и завершать обращения",
    "note": "Оставлять заметки только для команды", "forward": "Назначать другого сотрудника",
    "templates": "Использовать готовые ответы", "attachments": "Работать с файлами обращения",
    "export": "Скачивать разрешённые обращения в CSV", "view_others": "Читать обращения других сотрудников",
    "manage_others": "Применять выданные права к чужим обращениям",
}


def _access_badge(enabled: bool) -> str:
    state = "on" if enabled else "off"
    label = "Включён" if enabled else "Выключен"
    return f'<span class="pill pill-{state}">{label}</span>'


def _login_help() -> str:
    return """<details class="staff-web-help"><summary>Если сотруднику не приходит код</summary>
<ol class="staff-web-steps"><li>Откройте «Настроить», включите доступ к кабинету и сохраните изменения.
Регистрация по приглашению в MAX сама по себе не открывает сайт.</li>
<li>Сотрудник должен открыть диалог с ботом в MAX и нажать «Начать» либо отправить /start.</li>
<li>На странице входа укажите тот же MAX ID, который показан в карточке сотрудника.
После нескольких запросов подряд подождите 10 минут и запросите новый код.</li></ol>
<p>Сис-админы входят через обычную страницу входа в панель, а не по коду сотрудника.</p></details>"""


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
    body = f"""<section class="card staff-web"><div class="staff-web-intro"><div>
<h2>Разрешите сотруднику вход на сайт</h2>
<p>Роль в MAX и веб-доступ — разные настройки. Выберите сотрудника, включите доступ и назначьте права.
Существующие сотрудники не получают веб-доступ автоматически. Пока доступ выключен, код для входа не отправляется.</p></div>
<a class="btn btn-grey" href="/workspace/login">Страница входа сотрудника</a></div>
<p class="small">Здесь только обычные действующие сотрудники. Тестовые профили и сис-админы не показаны.</p>
<form method="get" action="/panel/staff-access" class="staff-web-search"><div>
<label for="staff-access-search">Найти сотрудника</label>
<input id="staff-access-search" name="q" value="{esc(search)}" maxlength="100" placeholder="ФИО, должность или MAX ID">
</div><button class="btn" type="submit">Найти</button>
{'<a class="btn btn-grey" href="/panel/staff-access">Сбросить поиск</a>' if search else ''}</form></section>
<section class="card staff-web"><h2>Сотрудники и доступ</h2>"""
    if rows:
        body += '<ul class="staff-web-list">'
        for row in rows[:50]:
            hint = "Вход по коду из MAX разрешён" if row["enabled"] else "Код не отправляется, пока доступ выключен"
            body += (f'<li><div class="staff-web-person"><strong>{esc(row["full_name"]) or "Без ФИО"}</strong>'
                     f'<p>{esc(row["position"]) or "Должность не указана"}</p><small>MAX ID: {esc(row["user_id"])}</small></div>'
                     f'<div class="staff-web-status">{_access_badge(row["enabled"])}<small>{hint}</small></div>'
                     f'<a class="btn" href="/panel/staff-access/{esc(row["user_id"])}" '
                     f'aria-label="Настроить доступ: {esc(row["full_name"]) or esc(row["user_id"])}">Настроить</a></li>')
        body += '</ul>'
    else:
        body += ('<div class="staff-web-empty"><strong>Подходящих сотрудников нет.</strong>'
                 '<p>Проверьте поиск или добавьте обычного сотрудника по MAX ID.</p>'
                 '<a class="btn btn-grey" href="/panel/staff">Открыть сотрудников</a></div>')
    body += '</section>'
    if number > 1 or len(rows) > 50:
        body += '<nav class="pager" aria-label="Страницы сотрудников">'
        for visible, target, label in ((number > 1, number - 1, "Назад"), (len(rows) > 50, number + 1, "Далее")):
            if visible:
                body += f'<a class="btn btn-grey" href="/panel/staff-access?{esc(urlencode({"q": search, "page": target}))}">{label}</a>'
        body += f'<span class="small">Страница {number}</span></nav>'
    body += f'<section class="card staff-web"><h2>Проверка входа</h2>{_login_help()}</section>'
    return _secure_response(page("Веб-доступ сотрудников", body, user, "/staff-access"))


@router.get("/staff-access/{user_id}", include_in_schema=False)
async def staff_web_access_edit(request: Request, user_id: str):
    user = await require_user(request)
    staff = await _staff(user_id)
    access = await get_staff_web_access(user_id) or {"enabled": 0, "permissions": []}
    selected = set(access["permissions"])
    options = ''.join(f'<option value="{name}">{label}</option>' for name, label in PRESET_LABELS.items())
    checks = ''.join(
        f'<label class="staff-web-check"><input type="checkbox" name="permissions" value="{name}"'
        f'{" checked" if name in selected else ""}><span>{PERMISSION_LABELS[name]}'
        f'<small>{PERMISSION_HINTS[name]}</small></span></label>' for name in WEB_PERMISSIONS)
    body = f"""<section class="card staff-web"><div class="staff-web-intro"><div>
<h2>{esc(staff['full_name'])}</h2><p>{esc(staff['position']) or 'Должность не указана'} · MAX ID: {esc(user_id)}</p>
</div><div class="staff-web-status">{_access_badge(access['enabled'])}</div></div>
<p>Просмотр собственных обращений включён в сам доступ. Право MAX «видеть все» не выдаёт веб-права.</p>
<form method="post" action="/panel/staff-access/{esc(user_id)}">{csrf(request)}
<div class="staff-web-settings"><div><label for="staff-access-enabled">Доступ к кабинету</label>
<select id="staff-access-enabled" name="enabled">
<option value="0"{" selected" if not access['enabled'] else ""}>Выключен</option>
<option value="1"{" selected" if access['enabled'] else ""}>Включён</option></select></div>
<div><label for="staff-access-preset">Набор прав</label><select id="staff-access-preset" name="preset">{options}</select></div></div>
<p>«Работа со своими»: ответы, статусы, заметки, передача, шаблоны и вложения. Выгрузка и чужие обращения выключены.
Отдельные флажки учитываются только при выборе «Настроить отдельно».</p>
<fieldset class="staff-web-permissions"><legend>Отдельные права</legend><div class="staff-web-checks">{checks}</div></fieldset>
<p class="small">Изменение чужих обращений включает их просмотр. После сохранения прежние входы и коды будут отозваны.</p>
<div class="btn-row"><button class="btn" type="submit">Сохранить доступ</button>
<a class="btn btn-grey" href="/panel/staff-access">К списку сотрудников</a></div></form>{_login_help()}</section>
<section class="card staff-web"><h2>Завершить прежние входы</h2><p>Отзыв завершает все сессии сотрудника и гасит выданные коды.
Разрешение на новый вход остаётся.</p><form method="post" action="/panel/staff-access/{esc(user_id)}/revoke">
{csrf(request)}<button class="btn btn-grey" type="submit">Отозвать все сессии</button></form></section>"""
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
