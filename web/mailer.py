"""Рассылки: новая рассылка и её история."""
import repository as repo
from fastapi import Request
from handlers.broadcast import run_broadcast
from handlers.common import notify, spawn
from panel_theme import icon
from utils import as_str, to_int, valid_group

from .common import (csrf, esc, flash, log, page, page_window, pager, pages_of, pill, redirect,
                     require_form, require_user, select, value, window_tail)
from .router import router


def _delivered_cell(sent, failed) -> str:
    """Доставка рассылки: плашками вместо зелёной галочки и красного крестика."""
    parts = [pill(f"доставлено {sent}", "on")]
    if to_int(failed):
        parts.append(pill(f"не доставлено {failed}"))
    return " ".join(parts)


def _broadcasts_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Рассылок пока не было.</p>"
    body = "".join(
        f"<tr><td><a href='/panel/broadcasts'>#{esc(row['id'])}</a></td>"
        f"<td>{esc(row['sender_name'] or row['sender_id'])}"
        f"{' <span class=\"small mut\">' + esc(row['sender_role']) + '</span>' if row['sender_role'] else ''}</td>"
        f"<td>{esc('всем' if row['audience'] == 'all' else row['audience'])}</td>"
        f"<td class='small'>{esc((row['text'] or '')[:90])}</td>"
        f"<td>{_delivered_cell(row['sent'], row['failed'])}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    )
    return (f"<table><tr><th>№</th><th>Отправитель</th><th>Кому</th><th>Текст</th>"
            f"<th>Доставлено</th><th>Когда</th></tr>{body}</table>")


# ── рассылки ──────────────────────────────────────────────────────────────────


async def _persons_datalist() -> str:
    """Выпадающий список людей для рассылки отдельным лицам."""
    contacts = await repo.people(limit=300)
    opts = []
    seen = set()
    for c in contacts:
        row = dict(c)
        uid = as_str(row.get("user_id", ""))
        if not uid or uid in seen:
            continue
        seen.add(uid)
        name = as_str(row.get("fio") or row.get("staff_name") or row.get("display_name") or f"ID {uid}")
        sub = as_str(row.get("group_code") or row.get("position") or "")
        sub_label = f" ({sub})" if sub else ""
        opts.append(f'<option value="{esc(uid)}">{esc(name)}{esc(sub_label)} — ID {esc(uid)}</option>')
        if name and name != f"ID {uid}":
            opts.append(f'<option value="{esc(name)}">{esc(name)}{esc(sub_label)} — ID {esc(uid)}</option>')
    return "".join(opts)

BROADCASTS_PAGE = 25    # рассылок на страницу истории


@router.get("/broadcasts")
async def broadcasts_list(request: Request, page_no: int = 1):
    """Новая рассылка и её история. История растёт неделями, поэтому постранично."""
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    window = page_window(page_no, BROADCASTS_PAGE)
    found = await repo.broadcast_history(window)
    pages = pages_of(len(found), BROADCASTS_PAGE)
    rows = found[(page_no - 1) * BROADCASTS_PAGE: page_no * BROADCASTS_PAGE]
    pages_bar = pager("/panel/broadcasts", [], page_no, pages, f"рассылок: {len(found)}")
    tail = window_tail(len(found), window, "Свежие рассылки сверху, дальше - на следующих страницах.")
    audience_options = {
        "all": "всем студентам",
        "staff": "преподавателям и сотрудникам",
        "custom": "отдельным лицам (выбор из списка ниже)",
    }
    for row in await repo.top_groups(300):
        audience_options[row["group_code"]] = f"группе {row['group_code']} ({row['students']} чел.)"
    body = f"""
<div class="card"><h2>{icon("send", 20)} Новая рассылка</h2>
<form method="post" action="/panel/broadcasts/send">{csrf(request)}<div class="grid">
<div>{select('audience', audience_options, 'all', label="Кому")}</div>
<div><label>MAX ID для проверки (необязательно)</label><input name="test_to" placeholder="проверить на себе"></div>
<div class="full"><label>Отдельные лица (выберите из списка или введите ФИО / MAX ID через запятую)</label>
<input name="custom_recipients" list="recipients-list" placeholder="Начните вводить фамилию или MAX ID...">
<datalist id="recipients-list">{await _persons_datalist()}</datalist></div>
<div class="full"><label>Текст объявления</label><textarea name="text" maxlength="3500"
  placeholder="Уважаемые студенты…"></textarea></div></div>
<p class="small mut">Объявление придёт с подписью «— ФИО, должность». Отправка идёт в фоне,
итог придёт вам в MAX и появится в истории ниже.</p>
<div class="grid" style="margin-top:10px"><button>{icon("send", 16)} Отправить рассылку</button></div></form></div>
<div class="card"><h2>{icon("logs", 20)} История рассылок</h2>{_broadcasts_table(rows)}{pages_bar}{tail}</div>"""
    return page("Рассылки", body, user, "/broadcasts")


@router.post("/broadcasts/send")
async def broadcasts_send(request: Request):
    user = await require_form(request)
    data = await request.form()
    text = as_str(data.get("text", "")).strip()[:3500]
    audience = value(data, "audience", default="all")
    test_to = as_str(data.get("test_to", "")).strip()
    if not text:
        flash("!Введите текст объявления.")
        return redirect("/panel/broadcasts")
    if audience == "custom":
        custom_uids = as_str(data.get("custom_recipients", "")).strip()
        if not custom_uids:
            flash("!Укажите хотя бы одного получателя (ФИО или MAX ID).")
            return redirect("/panel/broadcasts")
        audience = f"custom:{custom_uids}"
    elif audience not in ("all", "staff") and not valid_group(audience):
        flash("!Неизвестная группа — выберите её из списка.")
        return redirect("/panel/broadcasts")
    if test_to:
        ok = await notify(test_to, f"🧪 Тест рассылки от {user}. Текст:\n\n{text}")
        flash("Тестовое сообщение отправлено." if ok else f"Не удалось отправить тестовое сообщение: {test_to}")
    audience_size = len(await repo.audience_ids(audience))
    if not audience_size:
        flash("!Нет получателей: в группе никто не зарегистрирован.")
        return redirect("/panel/broadcasts")
    spawn(run_broadcast(user, audience, text))
    log.info("панель: рассылка запущена %s от %s", audience, user)
    flash(f"Рассылка запущена: {audience_size} получателей.")
    return redirect("/panel/broadcasts")
