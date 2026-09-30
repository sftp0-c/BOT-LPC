"""Расписания групп: ссылка на PDF, разбор файла, звонки и импорт с сайта колледжа."""
import re
from urllib.parse import quote

import database as db
import repository as repo
import schedule_import
import timetable as tt
from fastapi import Request
from handlers import schedules
from handlers.admin import notify_schedule_subscribers, probe_pdf_url
from panel_theme import icon
from timetable import WEEKDAYS_FULL
from utils import as_str, fmt_when, norm_group, short, to_int, valid_group

from .common import (_action_form, csrf, esc, flash, input, log, page, redirect, require_form,
                     require_user, value)
from .router import router


COLLEGE_SCHEDULE_PAGE = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"


# ── расписания ────────────────────────────────────────────────────────────────
@router.get("/schedules")
async def schedules_list(request: Request):
    user = await require_user(request)
    rows = await repo.schedule_groups(300)
    stamps = {}
    parsed_total = 0
    body = ""
    for row in rows:
        code = as_str(row["group_code"])
        full = await repo.get_schedule(code)
        stamp = repo.schedule_stamp(full)
        count = await repo.lessons_count(code)
        parsed_total += count
        stamps[code] = stamp
        if stamp["parse_error"]:
            state = f"<span class='pill pill-off'>ошибка: {esc(short(stamp['parse_error'], 60))}</span>"
        elif count:
            state = f"<span class='pill pill-on'>{count} пар</span> <span class='small mut'>{esc(fmt_when(stamp['parsed_at']))}</span>"
        else:
            state = "<span class='mut'>не разобрано</span>"
        found = ", ".join(stamp["found_groups"][:8]) or "—"
        hidden = f'<input type="hidden" name="group_code" value="{esc(code)}">'
        # Кнопки строки лежат в .wb-tools: у темы там зазор и перенос по словам.
        # Раньше они стояли в ячейке вплотную - одна формой, другая через пробел,
        # а удаление было вовсе без подписи, только иконка: по кнопке нельзя было
        # понять, что она делает. Обёртка задаёт кнопкам зазор, а ширину -
        # собственная подпись, поэтому текст всегда в одну строку.
        #
        # Класс .num на ячейке с кнопками - запрет переноса подписи. td в теме
        # рвёт слова в любом месте (overflow-wrap:anywhere), и в узкой ячейке на
        # телефоне кнопка рассыпалась по одной букве в строке. Из общих классов
        # темы запрет переноса есть только у .num, поэтому он и стоит здесь.
        body += (
            f'<tr><td><a href="/panel/schedules/{quote(code)}"><b>{esc(code)}</b></a></td>'
            f"<td class='small'>{esc(short(full['pdf_url'], 70))}</td>"
            f"<td>{state}</td><td class='small mut'>{esc(found)}</td>"
            f"<td class='num'><div class='wb-tools'>"
            f"{_action_form(request, '/panel/schedules/parse', f'{icon("search", 16)} Разобрать', hidden)}"
            f"{_action_form(request, '/panel/schedules/delete', f'{icon("delete", 16)} Удалить', hidden, f'Удалить расписание группы {code}?', 'btn-bad')}"
            f"</div></td></tr>"
        )
    table = (f"<table><tr><th>Группа</th><th>Ссылка на PDF</th><th>Разбор</th>"
             f"<th>Группы в PDF</th><th>Действия</th></tr>"
             f"{body or '<tr><td colspan=5 class=mut>Расписаний пока нет</td></tr>'}</table>")
    save = (
        f'<form method="post" action="/panel/schedules/save">{csrf(request)}'
        f'<div class="grid">{input("group_code", "")}{input("pdf_url", "", full=True)}</div>'
        f'<div class="grid"><div><button class="btn-ok">'
        f'{icon("check", 16)} Сохранить (сразу разберём)</button></div></div></form>'
    )
    bells = await _lesson_times_form(request)
    import_box = (
        f'<form method="post" action="/panel/schedules/import">{csrf(request)}'
        '<div class="grid"><div class="full"><label>Адрес страницы с расписаниями или список ссылок на PDF '
        '(по одной в строке)</label>'
        f'<textarea name="source">{esc(COLLEGE_SCHEDULE_PAGE)}</textarea></div></div>'
        '<div class="grid"><div><button class="btn-ok">'
        f'{icon("download", 16)} Импортировать с сайта</button></div></div></form>'
    )
    body_all = f"""
<div class="card"><h2>{icon("schedules", 20)} Расписания</h2>{table}
<p class="small mut">Разобранных пар: {parsed_total}. Скачанные PDF лежат рядом с базой в папке
<code>schedules</code> и перечитываются, когда файл по ссылке меняется.</p>
<div class="wb-tools">{_action_form(request, '/panel/schedules/parse_all', f'{icon("refresh", 16)} Обновить все расписания')}</div></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>Сохранить расписание</h2>{save}
  <p class="small mut">Ссылку достаёт преподаватель или бот. Сначала проверяем, что по ней отдаётся PDF,
  затем разбираем файл в занятия — студенты увидят расписание текстом, а не ссылкой.</p></div>
  <div class="card" style="flex:1"><h2>{icon("clock", 20)} Звонки (время пар)</h2>{bells}</div>
</div>"""
    body_all = f"""<div class="card"><h2>{icon("download", 20)} Импорт с сайта колледжа</h2>{import_box}
<p class="small mut">Бот скачает указанные PDF, найдёт в них группы и заведёт их расписание
вместе со справочником кодов. Дальше файлы обновляются на стороне колледжа: бот сам
перечитывает PDF, когда файл меняется, - руками ничего обновлять не нужно.</p></div>
{body_all}"""
    return page("Расписания", body_all, user, "/schedules")


async def _lesson_times_form(request: Request) -> str:
    """Редактор звонков: время каждого урока по его номеру.

    Нумерация — как в PDF и в боте: 1 урок, 2 урок, … (в паре их два, поэтому
    номера идут подряд). Время подставляется в расписание по номеру урока.

    Строки - обычная таблица темы, а не ряд полей: номер урока и два времени
    встают в колонки и не липнут друг к другу ни на телефоне, ни на широком
    экране.
    """
    times = await schedules.lesson_times()
    rows = []
    for index, (start, end) in enumerate(times, start=1):
        rows.append(
            f"<tr><td>{index}</td>"
            f"<td><input name='t{index}a' value='{esc(start)}' pattern='\\d{{1,2}}:\\d{{2}}' size='5'></td>"
            f"<td><input name='t{index}b' value='{esc(end)}' pattern='\\d{{1,2}}:\\d{{2}}' size='5'></td></tr>"
        )
    editor = ('<div class="grid"><div class="full">'
              '<table><tr><th>№</th><th>Начало</th><th>Конец</th></tr>'
              + "".join(rows) + "</table></div></div>")
    return (f'<form method="post" action="/panel/schedules/times">{csrf(request)}{editor}'
            '<div class="grid"><div><button class="btn-ok">'
            f'{icon("check", 16)} Сохранить звонки</button></div></div></form>'
            + "<p class='small mut'>Номер урока — как в расписании и как показывает бот: в паре уроков два, "
              "поэтому 1 и 2 уроки — это первая пара, 3 и 4 — вторая. Формат 09:00. Изменение сразу "
              "применяется к уже разобранным занятиям, перечитывать PDF не нужно.</p>")


@router.get("/schedules/{group_code}")
async def schedule_card(request: Request, group_code: str):
    """Разобранное расписание группы: занятия по дням, время можно поправить."""
    user = await require_user(request)
    code = norm_group(group_code)
    rows = await repo.lessons_for_group(code)
    if not rows:
        return page("Расписание", f'<div class="card msg-bad">Для группы {esc(code)} расписание не разобрано. '
                                  f'<a class="btn btn-grey" href="/panel/schedules">К списку</a></div>', user, "/schedules")
    by_day: dict[int, list] = {}
    for row in rows:
        by_day.setdefault(int(row["weekday"]), []).append(row)
    days = ""
    for weekday in sorted(by_day):
        # Форма правки времени - ряд с зазором темы (.wb-tools): два поля и
        # кнопка не липнут, а кнопка остаётся по размеру своей подписи.
        lessons = "".join(
            f"<tr><td>{esc(lesson['lesson_num'])}</td><td>{esc(lesson['subject'])}</td>"
            f"<td>{esc(lesson['teacher'] or '—')}</td><td>{esc(lesson['room'] or '—')}</td>"
            f"<td><form method='post' action='/panel/schedules/lesson-time' class='wb-tools'>{csrf(request)}"
            f"<input type='hidden' name='group_code' value='{esc(code)}'>"
            f"<input type='hidden' name='weekday' value='{weekday}'>"
            f"<input type='hidden' name='lesson_num' value='{esc(lesson['lesson_num'])}'>"
            f"<input name='start' value='{esc(lesson['start'])}' size='5'>"
            f"<input name='end' value='{esc(lesson['end'])}' size='5'>"
            f"<button class='btn-grey' title='Сохранить время' aria-label='Сохранить время'>{icon("check", 16)}</button></form></td></tr>"
            for lesson in by_day[weekday]
        )
        days += (f"<h3>{esc(WEEKDAYS_FULL[weekday].capitalize())}</h3>"
                 f"<table><tr><th>Пара</th><th>Предмет</th><th>Преподаватель</th><th>Ауд.</th><th>Время</th></tr>"
                 f"{lessons}</table>")
    return page(f"Расписание {code}", f'<div class="card">{days}'
                                       f'<div class="wb-tools">'
                                       f'<a class="btn btn-grey" href="/panel/schedules">'
                                       f'{icon("chevron-left", 16)} К списку</a></div></div>',
                user, "/schedules")


@router.post("/schedules/lesson-time")
async def schedule_lesson_time(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    weekday = to_int(value(data, "weekday"), -1)
    number = to_int(value(data, "lesson_num"), -1)
    start, end = value(data, "start"), value(data, "end")
    if not _time_ok(start) or not _time_ok(end):
        flash("!Время в формате ЧЧ:ММ, например 09:00.")
        return redirect(f"/panel/schedules/{code}")
    changed = await repo.set_lesson_time(code, weekday, number, start, end)
    if changed:
        await repo.log_action(actor, "время пары исправлено", f"{code}, день {weekday}, пара {number}: {start}–{end}")
        flash(f"Время пары сохранено: {start}–{end}.")
    else:
        flash("!Такая пара не найдена — возможно, расписание переразобрали.")
    return redirect(f"/panel/schedules/{code}")


def _time_ok(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", value.strip())) and 0 <= int(value.split(":")[0]) <= 23


@router.post("/schedules/parse")
async def schedules_parse(request: Request):
    """Разбирает PDF одной группы и показывает, что получилось."""
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    if not await repo.get_schedule(code):
        flash("!У группы нет ссылки на PDF.")
        return redirect("/panel/schedules")
    result = await schedules.parse_group(code, force=True)
    if result.has_lessons:
        await repo.log_action(actor, "расписание разобрано", f"{code}: {result.schedule.lessons_count} пар")
        flash(f"Группа {code}: разобрано {result.schedule.lessons_count} пар по {len(result.schedule.days)} дням.")
    else:
        await repo.log_action(actor, "разбор расписания не удался", f"{code}: {result.reason}")
        flash(f"!Группа {code}: {result.reason}.")
    return redirect("/panel/schedules")


@router.post("/schedules/parse_all")
async def schedules_parse_all(request: Request):
    actor = await require_form(request)
    results = await schedules.refresh_all()
    good = [code for code, result in results.items() if result.has_lessons]
    bad = [f"{code}: {result.reason}" for code, result in results.items() if not result.has_lessons]
    await repo.log_action(actor, "расписания обновлены", f"успешно {len(good)}, с ошибкой {len(bad)}")
    if bad:
        flash(f"Обновлено: {len(good)}. С ошибкой — " + "; ".join(bad[:5]))
    else:
        flash(f"Обновлено расписаний: {len(good)}.")
    return redirect("/panel/schedules")


@router.post("/schedules/times")
async def schedules_times(request: Request):
    """Сохраняет звонки по номерам уроков и сразу пересчитывает время разобранных занятий."""
    actor = await require_form(request)
    data = await request.form()
    collected: dict[int, tuple[str, str]] = {}
    for key in data.keys():
        # поля приходят парами: t1a — начало первого урока, t1b — конец
        match = re.fullmatch(r"t(\d+)a", key)
        if not match:
            continue
        number = int(match.group(1))
        start, end = value(data, key), value(data, f"t{number}b")
        if _time_ok(start) and _time_ok(end):
            collected[number] = (start, end)
    if not collected:
        flash("!Не найдено ни одного корректного времени.")
        return redirect("/panel/schedules")
    size = max(collected)
    times = tuple(collected.get(number, ("", "")) for number in range(1, size + 1))
    await db.set_setting("lesson_times", schedules.times_to_json(times))
    updated = await repo.reapply_lesson_times(times)
    await repo.log_action(actor, "звонки изменены", f"уроков: {size}, обновлено занятий: {updated}")
    flash(f"Звонки сохранены: {size} уроков. Время пересчитано у {updated} занятий.")
    return redirect("/panel/schedules")


@router.post("/schedules/import")
async def schedules_import(request: Request):
    """Импортирует расписания с сайта колледжа одной кнопкой.

    На входе - адрес страницы со ссылками на PDF или список ссылок. Группы
    заводятся автоматически, вместе со справочником вариантов написания кода.
    """
    actor = await require_form(request)
    data = await request.form()
    source = value(data, "source").strip() or COLLEGE_SCHEDULE_PAGE
    try:
        result = await schedule_import.import_sources(source)
    except Exception as exc:  # noqa: BLE001 - причину покажем сис-админу
        log.warning("импорт расписаний не получился: %s", exc)
        flash(f"!Не удалось импортировать: {exc}")
        return redirect("/panel/schedules")
    if not result["urls"]:
        flash("!Не нашлось ни одной ссылки на PDF. Проверьте адрес.")
        return redirect("/panel/schedules")
    await repo.log_action(actor, "импорт расписаний", f"файлов {result['files']}, групп {result['total']}")
    log.info("панель: импорт расписаний - файлов %s, групп %s (сис-админ %s)",
             result["files"], result["total"], actor)
    parts = [f"Импортировано файлов: {result['files']}, групп: {result['total']}, занятий: {result['lessons']}"]
    if result["problems"]:
        parts.append("Проблемы: " + "; ".join(result["problems"][:5]))
    flash(". ".join(parts))
    return redirect("/panel/schedules")


@router.post("/schedules/save")
async def schedules_save(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    url = value(data, "pdf_url")
    if not valid_group(code):
        flash("!Код группы: буквы, цифры, дефис и точка (например ИС-21).")
        return redirect("/panel/schedules")
    ok, reason = await probe_pdf_url(url)
    if not ok:
        flash(f"!{reason} Ссылка не сохранена.")
        return redirect("/panel/schedules")
    changed = await repo.upsert_schedule(code, url)
    result = await schedules.parse_group(code, force=True)
    if result.has_lessons:
        await notify_schedule_subscribers(
            code, f"🔔 Расписание группы {code} обновлено.\n\n{tt.format_upcoming(result.schedule)}")
        flash(f"Расписание группы {code} сохранено и разобрано: "
              f"{result.schedule.lessons_count} пар по {len(result.schedule.days)} дням.")
    else:
        flash(f"Ссылка сохранена, но разобрать не вышло: {result.reason}. Студенты пока увидят ссылку на PDF.")
    await repo.log_action(actor, "ссылка на расписание сохранена", f"{code}: {short(url, 60)}")
    log.info("панель: расписание %s — %s, разбор: %s", code, "изменено" if changed else "прежнее",
             "ок" if result.has_lessons else result.reason)
    return redirect("/panel/schedules")


@router.post("/schedules/delete")
async def schedules_delete(request: Request):
    actor = await require_form(request)
    data = await request.form()
    code = norm_group(value(data, "group_code"))
    if not await repo.get_schedule(code):
        flash("!Расписание не найдено.")
        return redirect("/panel/schedules")
    await repo.delete_schedule(code)
    await notify_schedule_subscribers(code, f"🗑 Расписание группы {code} удалено.")
    await repo.log_action(actor, "расписание удалено", f"{code}, подписчики уведомлены")
    flash(f"Расписание группы {code} удалено.")
    return redirect("/panel/schedules")



