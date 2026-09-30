"""Справочники: контакты колледжа с частыми вопросами и группы студентов."""
import college
import database as db
import repository as repo
from fastapi import Request
from handlers import faq
from panel_theme import icon
from utils import as_str, norm_group, to_int, valid_group

from .common import (_action_form, code_cell, csrf, esc, flag, flash, form, input, log, page, redirect,
                     require_form, require_user, select, state_pill, value)
from .router import router


# ── колледж: контакты и частые вопросы ───────────────────────────────────────
# Класс колонки действий.
#
# Ответ на частый вопрос длинный и забирает почти всю ширину таблицы, а тема
# задаёт ячейкам ``overflow-wrap:anywhere``: браузер сжимал последнюю колонку
# до одного символа, и подпись «Вкл/выкл» переносилась по буквам вниз
# («В к л / в ы к л») — нажать такую кнопку было нельзя. Класс запрещает
# перенос внутри колонки и отдаёт ей ширину по содержимому.
ACT_COL = "col-act"

# Правило, которое раздел ожидает от темы. В panel_theme.py (его переделывает
# другой агент) в блок «таблицы» нужно вставить ровно эти две строки:
#
#     .col-act{white-space:nowrap}
#     th.col-act,td.col-act{width:1%}
#
# Само правило здесь только задокументировано и на страницу не попадает:
# инлайновых <style> в разделах панели нет.
ACT_COL_CSS = ".col-act{white-space:nowrap}\nth.col-act,td.col-act{width:1%}"


def _empty(icon_name: str, title: str, text: str) -> str:
    """Пустое состояние блока: рамка темы вместо таблицы из одной строки."""
    return (f'<div class="empty">{icon(icon_name, 34)}<b>{esc(title)}</b>'
            f'<span>{esc(text)}</span></div>')


async def _faq_counts() -> tuple[int, int]:
    """Сколько вопросов в таблице faq всего и сколько из них включено.

    ``faq.active_items()`` отдаёт только включённые — боту остальные не нужны,
    поэтому общий счётчик берём отдельным запросом: без него на странице не
    видно, что выключенные вопросы в базе тоже есть.
    """
    await faq.ensure_category_column()
    row = await db.one("SELECT COUNT(*) AS total, COALESCE(SUM(active),0) AS enabled FROM faq")
    return to_int(row["total"]), to_int(row["enabled"])


@router.get("/college")
async def college_page(request: Request):
    """Справочник колледжа: что бот рассказывает студенту о себе и о колледже.

    Сверху счётчики, ниже два отдельных блока — контакты и частые вопросы.
    Раньше обе таблицы стояли одна под другой, без заголовков и без чисел, и
    раздел в 165 КБ читался сплошной простынёй.
    """
    user = await require_user(request)
    # обычный цикл, а не генератор: await внутри genexp даёт асинхронный генератор
    rows, edited = [], 0
    for key, val in (await college.contacts()).items():
        changed = await college.is_overridden(key)
        edited += int(changed)
        mark = "изменено" if changed else "с сайта"
        rows.append(f"<tr><th class='col-key'>{esc(key.replace('_', ' '))}</th>"
                    f"<td><input name='{esc(college.setting_key(key))}' value='{esc(val)}'></td>"
                    f"<td class='small mut'>{mark}</td></tr>")
    rows_html = "".join(rows)

    enabled = await faq.ask_enabled()
    items = await faq.active_items()
    faq_total, faq_on = await _faq_counts()
    faq_rows = []
    for row in items:
        toggle = _action_form(request, "/panel/faq/" + str(to_int(row["id"])) + "/toggle",
                              f'{icon("refresh", 16)} Вкл/выкл',
                              cls="btn-grey " + ACT_COL,
                              confirm_text="Включить или выключить этот вопрос?")
        mark = state_pill("on", "включён") if row["active"] else state_pill("off", "выключен")
        faq_rows.append(
            f"<tr><td><b>{esc(row['question'])}</b>"
            f"<div class='small mut'>{esc(row['keywords'])}</div></td>"
            f"<td>{esc(row['answer'])}</td><td>{mark}</td>"
            f"<td class='{ACT_COL}'>{toggle}</td></tr>")
    faq_html = "".join(faq_rows)

    contacts_block = (
        f'<form method="post" action="/panel/college">{csrf(request)}'
        f"<table><tr><th class='col-key'>Поле</th><th>Значение</th><th>Источник</th></tr>"
        f"{rows_html}</table>"
        '<div class="grid" style="margin-top:10px">'
        f'<button class="btn-ok">{icon("check", 16)} Сохранить справочник</button></div></form>'
        if rows else
        _empty("college", "Полей пока нет",
               "Справочник пуст: сохранять нечего, добавьте поля в college.DEFAULTS."))
    faq_block = (
        f"<table><tr><th class='col-key'>Вопрос и ключевые слова</th><th>Ответ</th>"
        f"<th>Состояние</th><th class='{ACT_COL}'>Действие</th></tr>{faq_html}</table>"
        if faq_html else
        _empty("inbox", "Вопросов пока нет",
               "Нажмите «Залить вопросы с сайта» — бот получит черновик вопросов с сайта."))
    ask_label = "Выключить" if enabled else "Включить"
    ask_word = "включены" if enabled else "выключены"
    ask_cls, ask_ico = ("btn-bad", "close") if enabled else ("btn-ok", "check")
    ask_form = (f'<form method="post" action="/panel/faq/toggle" class="inline">{csrf(request)}'
                f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
                f'<button class="{ask_cls}">{icon(ask_ico, 16)} {ask_label}</button></form>')
    seed_form = ('<form method="post" action="/panel/faq/seed" class="inline">'
                 f'{csrf(request)}<button class="btn-grey">{icon("download", 16)} '
                 'Залить вопросы с сайта</button></form>')
    kpi = f"""
<div class="kpi">
  <div><b>{len(rows)}</b><span>полей в справочнике</span></div>
  <div><b>{edited}</b><span>изменено</span></div>
  <div><b>{faq_total}</b><span>вопросов в базе</span></div>
  <div class="good"><b>{faq_on}</b><span>включено</span></div>
  <div><b>{faq_total - faq_on}</b><span>выключено</span></div>
</div>"""
    body = f"""{kpi}
<div class="card"><h2>{icon("college", 20)} Контакты колледжа
<span class="pill">полей: {len(rows)}</span>
<span class="pill {'pill-on' if edited else 'pill-off'}">изменено: {edited}</span></h2>
<p class="small mut">Значения взяты с официального сайта {esc(college.SITE)}. Пустое поле
возвращает к данным сайта. Пометка «изменено» стоит у полей, которые правил сис-админ.</p>
{contacts_block}</div>
<div class="card"><h2>{icon("info", 20)} Частые вопросы
<span class="pill">вопросов: {faq_total}</span>
<span class="pill {'pill-on' if enabled else 'pill-off'}">ответы: {ask_word}</span></h2>
<p class="small mut">Бот ищет ответ по ключевым словам. Если не нашёл — не выдумывает,
а предлагает написать сотруднику. В таблице {len(items)} включённых вопросов.</p>
<div class="grid" style="margin:12px 0">{ask_form}{seed_form}
<span class="full small mut">Сейчас ответы на частые вопросы {ask_word}.</span></div>
{faq_block}
<p class="small mut">Черновик — {len(college.DEFAULT_FAQ)} вопросов с сайта колледжа.
Кнопка «Залить» добавляет только новые и не трогает правки сис-админа.</p></div>"""
    return page("Колледж", body, user, "/college")


@router.post("/college")
async def college_save(request: Request):
    actor = await require_form(request)
    data = await request.form()
    saved = 0
    for key in college.FIELDS:
        name = college.setting_key(key)
        if hasattr(data, "getlist") and name in data:
            await college.override(key, as_str(data.get(name, "")).strip()[:college.MAX_VALUE])
            saved += 1
    await repo.log_action(actor, "справочник колледжа", f"полей сохранено: {saved}")
    flash(f"Справочник сохранён: {saved} полей.")
    return redirect("/panel/college")


@router.post("/faq/toggle")
async def faq_toggle(request: Request):
    await require_form(request)
    data = await request.form()
    await faq.set_ask_enabled(as_str(data.get("enabled", "")) == "1")
    flash("Ответы на частые вопросы " + ("выключены." if as_str(data.get("enabled", "")) == "1" else "включены."))
    return redirect("/panel/college")


@router.post("/faq/seed")
async def faq_seed(request: Request):
    actor = await require_form(request)
    added = await faq.seed_defaults()
    await repo.log_action(actor, "частые вопросы с сайта", f"добавлено: {added}")
    flash(f"Добавлено вопросов: {added}. Правки сис-админа не тронуты.")
    return redirect("/panel/college")


@router.post("/faq/{faq_id}/toggle")
async def faq_item_toggle(request: Request, faq_id: int):
    """Включить или выключить отдельный вопрос, не удаляя его."""
    actor = await require_form(request)
    row = await db.one("SELECT active FROM faq WHERE id=?", (int(faq_id),))
    if not row:
        flash("!Такого вопроса нет.")
        return redirect("/panel/college")
    await db.run("UPDATE faq SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (int(faq_id),))
    await repo.log_action(actor, "частый вопрос", f"№{faq_id}")
    flash(f"Вопрос №{faq_id} {'выключен' if to_int(row['active']) else 'включен'}.")
    return redirect("/panel/college")


# ── группы ────────────────────────────────────────────────────────────────────
@router.get("/groups")
async def groups_list(request: Request):
    user = await require_user(request)
    rows = await repo.groups(active_only=False, limit=300)
    # каждая форма в своей обёртке: класс запрещает переносить подпись кнопки
    # («Переименовать» иначе рассыпается по буквам в узкой колонке), а сами
    # формы переносятся друг за другом — на телефоне три кнопки в ряд не влезают
    body = "".join(
        f"""<tr data-hk><td>{code_cell(row['group_code'], 'Код группы скопирован')}</td>
        <td>{esc(row['title'])}</td>
<td>{"<span class='pill pill-on'>активна</span>" if flag(row['active']) else "<span class='pill pill-off'>скрыта</span>"}</td>
<td class="small mut">{esc(row['created_at'])}</td>
<td class="small"><span class="{ACT_COL}">
<form method="post" action="/panel/groups/rename" class="inline">{csrf(request)}
<input type="hidden" name="old" value="{esc(row['group_code'])}">
<input name="code" value="{esc(row['group_code'])}" style="width:110px;display:inline-block">
<button class="btn-grey">{icon("edit", 16)} Переименовать</button></form></span>
<span class="{ACT_COL}">
<form method="post" action="/panel/groups/toggle" class="inline">{csrf(request)}
<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-grey">{icon("eye-off" if flag(row['active']) else "eye", 16)}{"Скрыть" if flag(row['active']) else "Показать"}</button></form></span>
<span class="{ACT_COL}">
<form method="post" action="/panel/groups/delete" class="inline" onclick="return confirm('Удалить группу?')">
{csrf(request)}<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-bad">{icon("delete", 16)} Удалить</button></form></span></td></tr>"""
        for row in rows
    )
    table = (
        f"<table><tr><th>Код</th><th>Название</th><th>Состояние</th><th>Создана</th>"
        f"<th class='{ACT_COL}'>Действия</th></tr>{body}</table>"
        if rows else
        _empty("groups", "Справочник пуст",
               "Добавьте первую группу — её код студент назовёт при регистрации."))
    add = form(
        request, "/panel/groups/add",
        input("code", "") + input("title", "")
        + select("active", {"1": "активна", "0": "скрыта"}, "1"),
        "Добавить группу", "btn-ok",
    )
    shown = sum(1 for row in rows if flag(row["active"]))
    body_all = f"""
<div class="card"><h2>{icon("groups", 20)} Справочник групп
<span class="pill">групп: {len(rows)}</span>
<span class="pill pill-on">активных: {shown}</span></h2>{table}
<p class="small mut">Переименование меняет код и у студентов, и в расписаниях, и в подписках.
Скрытая группа не показывается в подсказках бота.</p></div>
<div class="card" style="max-width:560px"><h2>{icon("plus", 20)} Добавить группу</h2>{add}
<p class="small mut">Код берётся из названия группы: «ИС-21». Скрытая группа не показывается
в подсказках бота, но остаётся в расписаниях.</p></div>"""
    return page("Группы", body_all, user, "/groups")


@router.post("/groups/add")
async def groups_add(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    if not valid_group(code):
        flash("!Код группы: буквы, цифры, дефис и точка, до 30 символов (например ИС-21).")
        return redirect("/panel/groups")
    await repo.upsert_group(code, value(data, "title"), value(data, "active") == "1")
    await repo.log_action(actor, "группа добавлена", f"{code} {value(data, 'title')}")
    flash(f"Группа {code} добавлена.")
    return redirect("/panel/groups")


@router.post("/groups/rename")
async def groups_rename(request: Request):
    actor = await require_form(request)
    data = await request.form()
    old, new = norm_group(value(data, "old")), norm_group(value(data, "code"))
    if old == new:
        flash("Код не изменился.")
        return redirect("/panel/groups")
    if not await repo.rename_group(old, new):
        flash(f"!Не удалось переименовать {old} → {new}: проверьте код и что новый ещё не используется.")
        return redirect("/panel/groups")
    log.info("панель: группа %s → %s", old, new)
    await repo.log_action(actor, "группа переименована", f"{old} → {new}")
    flash(f"Группа {old} переименована в {new}.")
    return redirect("/panel/groups")


@router.post("/groups/toggle")
async def groups_toggle(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    row = await repo.get_group(code)
    if not row:
        flash("!Группа не найдена.")
        return redirect("/panel/groups")
    await repo.set_group_active(code, not flag(row["active"]))
    await repo.log_action(actor, "группа скрыта или показана", code)
    flash(f"Группа {code} {'показана' if not flag(row['active']) else 'скрыта'}.")
    return redirect("/panel/groups")


@router.post("/groups/delete")
async def groups_delete(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "code"))
    await repo.delete_group(code)
    await repo.log_action(actor, "группа удалена из справочника", f"{code} (данные студентов сохранены)")
    flash(f"Группа {code} удалена из справочника (данные студентов сохранены).")
    return redirect("/panel/groups")
