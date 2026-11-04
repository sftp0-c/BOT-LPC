"""Полный аудит панели и MAX: только безопасные метаданные за последние 30 дней."""
import csv
import io
from datetime import date
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import Response

import repository as repo
from panel_theme import icon
from utils import as_str, fmt_when, to_int

from .common import esc, page, pager, pages_of, require_user, select
from .router import router

AUDIT_PAGE = 50
AUDIT_ROLES = {
    "": "все роли", "owner": "владелец", "sysadmin": "сис-админ",
    "staff": "сотрудник", "student": "студент", "bot": "бот", "unknown": "неизвестно",
}
AUDIT_RESULTS = {"": "все результаты", "ok": "выполнено", "denied": "отказано", "failed": "ошибка", "received": "получено"}


def _filters(user_id: str, role: str, action: str, result: str,
             date_from: str, date_to: str) -> dict[str, str]:
    for value in (date_from, date_to):
        if not value:
            continue
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Укажите дату в формате ГГГГ-ММ-ДД") from None
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="Начало периода позже окончания")
    if user_id and not (user_id.isascii() and user_id.isdigit() and len(user_id) <= 20):
        raise HTTPException(status_code=400, detail="MAX ID состоит из цифр")
    if role not in AUDIT_ROLES or result not in AUDIT_RESULTS:
        raise HTTPException(status_code=400, detail="Неизвестный фильтр журнала")
    return {"user_id": as_str(user_id).strip(), "role": as_str(role).strip(),
            "action": as_str(action).strip(), "result": as_str(result).strip(),
            "date_from": as_str(date_from).strip(), "date_to": as_str(date_to).strip()}


def _csv_cell(value) -> str:
    text = as_str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


@router.get("/audit")
async def audit_page(request: Request, user_id: str = "", role: str = "", action: str = "",
                     result: str = "", date_from: str = "", date_to: str = "",
                     page_no: int = 1):
    """Комбинированные фильтры и постраничный просмотр структурированного аудита."""
    user = await require_user(request)
    filters = _filters(user_id, role, action, result, date_from, date_to)
    total = await repo.audit_count(**filters)
    pages = pages_of(total, AUDIT_PAGE)
    page_no = min(max(1, to_int(page_no, 1)), pages)
    rows = await repo.audit_entries(AUDIT_PAGE, (page_no - 1) * AUDIT_PAGE, **filters)
    actions = {"": "все действия"} | {code: code for code in await repo.audit_actions()}
    body_rows = "".join(
        f'<tr data-hk><td class="small mut">{esc(fmt_when(row["created_at"]))}</td>'
        f'<td>{esc(row["user_id"] or "—")}<div class="small mut">{esc(AUDIT_ROLES.get(row["role"], row["role"]))}</div></td>'
        f'<td><code>{esc(row["action"])}</code></td>'
        f'<td>{esc(row["target_type"] or "—")} {esc(row["target_id"])}</td>'
        f'<td><span class="pill audit-result-{esc(row["result"])}">{esc(AUDIT_RESULTS.get(row["result"], row["result"]))}</span></td></tr>'
        for row in rows
    ) or '<tr><td colspan="5" class="mut">Записей по этим фильтрам нет</td></tr>'
    filter_html = f"""
<form method="get" action="/panel/audit" class="grid audit-filters" style="margin-bottom:12px">
<div><label>MAX ID</label><input name="user_id" value="{esc(user_id)}" autocomplete="off"></div>
<div>{select("role", AUDIT_ROLES, role, label="Роль")}</div>
<div>{select("action", actions, action, label="Действие")}</div>
<div>{select("result", AUDIT_RESULTS, result, label="Результат")}</div>
<div><label>С даты</label><input name="date_from" type="date" value="{esc(date_from)}"></div>
<div><label>По дату</label><input name="date_to" type="date" value="{esc(date_to)}"></div>
<div><button>{icon("filter", 16)} Показать</button></div></form>"""
    params = [(key, value) for key, value in filters.items()]
    table = ("<table><tr><th>Когда</th><th>Кто</th><th>Действие</th><th>Цель</th>"
             f"<th>Результат</th></tr>{body_rows}</table>")
    note = ('<p class="small mut">Хранятся только тип действия и ID объекта. Переписка, '
            'значения полей, коды приглашений, IP и данные браузера сюда не записываются. '
            'Записи старше 30 дней удаляются автоматически.</p>')
    body = f'<div class="card"><h2>{icon("activity", 20)} Полный аудит</h2>{note}{filter_html}{table}'            f'{pager("/panel/audit", params, page_no, pages, f"записей: {total}")}</div>'
    query = esc(urlencode({key: value for key, value in filters.items() if value}))
    return page("Полный аудит", body, user, "/audit",
                actions=f'<a class="btn" href="/panel/audit.csv?{query}">'
                        f'{icon("download", 16)} Выгрузить CSV</a>')


@router.get("/audit.csv")
async def audit_csv(request: Request, user_id: str = "", role: str = "", action: str = "",
                    result: str = "", date_from: str = "", date_to: str = ""):
    """До 5000 безопасных записей с теми же фильтрами, что на экране."""
    await require_user(request)
    filters = _filters(user_id, role, action, result, date_from, date_to)
    rows = await repo.audit_entries(5000, 0, **filters)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Когда", "MAX ID", "Роль", "Действие", "Тип цели", "ID цели", "Результат"])
    for row in rows:
        writer.writerow([_csv_cell(row[key]) for key in
                         ("created_at", "user_id", "role", "action", "target_type", "target_id", "result")])
    return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="audit.csv"',
                             "Cache-Control": "no-store"})
