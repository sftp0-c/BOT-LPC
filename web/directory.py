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
@router.get("/college")
async def college_page(request: Request):
    """Справочник колледжа: что бот рассказывает студенту о себе и о колледже."""
    user = await require_user(request)
    # обычный цикл, а не генератор: await внутри genexp даёт асинхронный генератор
    rows, faq_rows = [], []
    for key, val in (await college.contacts()).items():
        mark = "изменено" if await college.is_overridden(key) else "с сайта"
        rows.append(f"<tr><th class='col-key'>{esc(key.replace('_', ' '))}</th>"
                    f"<td><input name='{esc(college.setting_key(key))}' value='{esc(val)}'></td>"
                    f"<td class='small mut'>{mark}</td></tr>")
    enabled = await faq.ask_enabled()
    for row in await faq.active_items():
        toggle = _action_form(request, "/panel/faq/" + str(to_int(row["id"])) + "/toggle",
                              f'{icon("refresh", 16)} Вкл/выкл',
                              confirm_text="Включить или выключить этот вопрос?")
        faq_rows.append(
            f"<tr><td><b>{esc(row['question'])}</b>"
            f"<div class='small mut'>{esc(row['keywords'])}</div></td>"
            f"<td>{esc(row['answer'])}</td>"
            f"<td>{state_pill("on", "включён") if row["active"] else state_pill("off", "выключен")}</td>"
            f"<td>{toggle}</td></tr>")
    rows_html = "".join(rows)
    faq_html = "".join(faq_rows) or (
        "<tr><td colspan='4' class='mut'>Вопросов пока нет — нажмите «Залить вопросы с сайта».</td></tr>")
    body = f"""<div class="card"><h2>Контакты колледжа</h2>
<p class="small mut">Значения взяты с официального сайта {esc(college.SITE)}. Пустое поле
возвращает к данным сайта. То, что изменено, отмечено в третьей колонке.</p>
<form method="post" action="/panel/college">{csrf(request)}<table>{rows_html}</table>
<div class="grid" style="margin-top:10px"><button class="btn-ok">Сохранить справочник</button></div>
</form></div>
<div class="card"><h2>Частые вопросы</h2>
<p class="small mut">Бот ищет ответ по ключевым словам. Если не нашёл — не выдумывает,
а предлагает написать сотруднику.</p>
<form method="post" action="/panel/faq/toggle">{csrf(request)}
<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">
<button class="{"btn-bad" if enabled else "btn-ok"}">{icon("close" if enabled else "check", 16)} {"Выключить" if enabled else "Включить"}</button>
<span class="small mut">сейчас: {"включены" if enabled else "выключены"}</span></form>
<form method="post" action="/panel/faq/seed">{csrf(request)}<button class="btn-grey" style="margin-top:8px">
{icon("download", 16)} Залить вопросы с сайта</button></form>
<table style="margin-top:12px"><tr><th>Вопрос и ключевые слова</th><th>Ответ</th><th>Вкл.</th><th></th></tr>
{faq_html}</table>
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
    body = "".join(
        f"""<tr data-hk><td>{code_cell(row['group_code'], 'Код группы скопирован')}</td>
        <td>{esc(row['title'])}</td>
<td>{"<span class='pill pill-on'>активна</span>" if flag(row['active']) else "<span class='pill pill-off'>скрыта</span>"}</td>
<td class="small mut">{esc(row['created_at'])}</td>
<td class="small">
<form method="post" action="/panel/groups/rename" class="inline">{csrf(request)}
<input type="hidden" name="old" value="{esc(row['group_code'])}">
<input name="code" value="{esc(row['group_code'])}" style="width:110px;display:inline-block">
<button class="btn-grey">{icon("edit", 16)} Переименовать</button></form>
<form method="post" action="/panel/groups/toggle" class="inline">{csrf(request)}
<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-grey">{icon("eye-off" if flag(row['active']) else "eye", 16)}{"Скрыть" if flag(row['active']) else "Показать"}</button></form>
<form method="post" action="/panel/groups/delete" class="inline" onclick="return confirm('Удалить группу?')">
{csrf(request)}<input type="hidden" name="code" value="{esc(row['group_code'])}">
<button class="btn-bad">{icon("delete", 16)} Удалить</button></form></td></tr>"""
        for row in rows
    ) or "<tr><td class='mut'>Справочник пуст</td></tr>"
    table = f"<table><tr><th>Код</th><th>Название</th><th>Состояние</th><th>Создана</th><th>Действия</th></tr>{body}</table>"
    add = form(
        request, "/panel/groups/add",
        input("code", "") + input("title", "")
        + select("active", {"1": "активна", "0": "скрыта"}, "1"),
        "Добавить группу", "btn-ok",
    )
    body_all = f"""
<div class="card"><h2>{icon("groups", 20)} Справочник групп</h2>{table}</div>
<div class="card" style="max-width:560px"><h2>{icon("plus", 20)} Добавить группу</h2>{add}
<p class="small mut">Переименование меняет код и у студентов, и в расписаниях, и в подписках.
Скрытая группа не показывается в подсказках бота.</p></div>"""
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
