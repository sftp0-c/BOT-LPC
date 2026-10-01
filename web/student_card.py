"""Карточка студента: все сведения о студенте на одной странице.

Сведения о студенте в панели лежали по разным местам: реестр, список студентов,
рабочее место обращений, расписания. Чтобы понять, что с человеком происходит,
сис-админ переходил между разделами и собирал картину по кускам, а вопрос был
один: что вообще известно об этом студенте. Карточка отвечает на него целиком:
кто он, когда зарегистрировался, все его обращения со статусом и датой, подписан
ли он на расписание своей группы, разобрано ли это расписание и дал ли он
согласие на обработку данных.

Данные берутся теми же функциями слоя store, что и в остальных разделах:
``store.people.person_dossier`` отдаёт человека и его обращения одним вызовом,
``store.schedules`` - ссылку на PDF, отметку о разборе и число занятий. В шаблоне
ни одного запроса - иначе карточка стоила бы столько запросов, сколько на ней
блоков, и подтормаживала бы тем сильнее, чем больше у человека жизни в боте.

Страница ничего не меняет: правок на ней нет, поэтому и CSRF-токен ей не нужен.
"""
from urllib.parse import quote

import clock
import config
from fastapi import Request
from panel_theme import icon
from store.people import person_dossier
from store.schedules import (get_schedule, lessons_count, schedule_stamp, stamp_is_fresh)
from utils import (OPEN_STATUSES, STAFF_CATS, STATUS, as_str, fmt_when, same_group, short,
                   to_int)

from .common import (code_cell, copy_btn, esc, fio, fio_brief, page, panel_link, pill, plain,
                     require_user)
from .people import profile_cell
from .router import router


def _table(rows: list[tuple[str, str]]) -> str:
    """Пары «подпись — значение» разметкой таблицы карточки."""
    return "".join(f"<tr><th>{esc(name)}</th><td>{value}</td></tr>" for name, value in rows)


def _group_cell(group: str) -> str:
    """Код группы ссылкой на её расписание: с карточки сразу видно, есть ли оно."""
    if not group:
        return '<span class="mut">группа не указана</span>'
    return f'<a href="/panel/schedules/{quote(esc(group))}">{esc(group)}</a>'


def _head_card(uid: str, person: dict, name: str, group: str) -> str:
    """Кто он: имя, ники, группа, дата регистрации и сколько дней он в боте."""
    days = to_int(person["days_in_bot"])
    stay = ("пришёл сегодня" if not days
            else f"{days} {clock.plural(days, 'день', 'дня', 'дней')} в боте")
    brief = fio_brief(name)
    rows = [
        ("Кто это", pill(plain(person["kind_title"]))),
        ("MAX ID", code_cell(uid, "MAX ID скопирован")),
        ("Профиль MAX", profile_cell(person["username"])),
        ("Группа", _group_cell(group)),
        ("Зарегистрирован", esc(fmt_when(person["registered_at"]) or "—")),
        ("Первый контакт", f"{esc(fmt_when(person['first_seen']) or '—')} · {esc(stay)}"),
        ("Сообщений боту", esc(to_int(person["messages"]))),
        ("Последний контакт", esc(fmt_when(person["last_seen"]) or "—")),
    ]
    # Права в боте у студента нет, а у сотрудника есть: молча показывать ему
    # таблицу студента значило бы скрыть, что перед нами сотрудник.
    staff_note = ""
    if person["has_admin"]:
        staff_note = ('<p class="small mut">' + icon("warning", 14) + ' У этого человека есть '
                      'права в боте: полная картина о нём — в '
                      f'<a href="/panel/people/{esc(uid)}/dossier">досье</a>.</p>')
    return f"""<div class="card"><h2 class="wb-name">{esc(name)}</h2>
{f'<p class="small mut wb-brief">{esc(brief)}</p>' if brief else ''}
<table>{_table(rows)}</table>{staff_note}
<p class="small mut wb-tools">
<a class="btn btn-grey" href="/panel/people/{esc(uid)}">{icon("user", 16)} Карточка человека</a>
<a class="btn btn-grey" href="/panel/students">{icon("students", 16)} К студентам</a>
{copy_btn(panel_link(f'/students/{uid}'), 'Ссылка на карточку скопирована')}</p></div>"""


def _consent_card(person: dict) -> str:
    """Согласие на обработку данных: когда человек его дал и на каких условиях.

    Отдельной «учебной подписки» в базе нет - отметка одна, ``users.consent_at``,
    и она же единственный след согласия. Показываем её под своим именем и
    говорим прямо, что других отметок в базе не заведено: иначе сис-админ будет
    искать вторую подписку, которой не существует.
    """
    given = as_str(person["consent_at"])
    mark = pill("согласие есть", "on") if given else pill("согласия нет", "off")
    rows = [
        ("Согласие на обработку данных", mark),
        ("Когда дал", esc(fmt_when(given)) if given else "—"),
        ("Редакция текста", esc(as_str(person["consent_version"]) or "—")),
    ]
    return f"""<div class="card"><h2>{icon("check", 20)} Согласие на обработку данных</h2>
<table>{_table(rows)}</table>
<p class="small mut">Отметка хранится рядом с человеком: по ней видно, что согласие
было получено и на каких условиях. Пустая отметка - это не «не важно», а «человек
не соглашался». Отдельной учебной подписки в базе не заведено: других отметок
о согласии здесь взять неоткуда.</p></div>"""


def _tickets_card(tickets: list, capped: bool) -> str:
    """Все обращения студента: номер, раздел, тема, статус, кто ведёт и даты.

    Списком, а не счётчиками: у карточки одна задача - показать всю историю
    обращений человека сразу, а не отбирать её по статусам. Номер ведёт в
    рабочее место с уже выбранным обращением, оттуда же видно и переписку.
    """
    body = "".join(
        f'<tr data-hk data-sc-ticket="{esc(ticket["ticket_id"])}" '
        f'data-sc-state="{esc(ticket["status"])}">'
        f'<td><a href="/panel/tickets?t={esc(ticket["ticket_id"])}">'
        f'№{esc(ticket["ticket_id"])}</a>'
        f'{" " + pill("в архиве", "off") if as_str(ticket["deleted_at"]) else ""}</td>'
        f'<td>{esc(plain(STAFF_CATS.get(as_str(ticket["category"]), as_str(ticket["category"]))))}</td>'
        f'<td>{esc(as_str(ticket["topic"]) or "—")}</td>'
        f'<td>{esc(plain(STATUS.get(as_str(ticket["status"]), as_str(ticket["status"]))))}</td>'
        f'<td class="small">{esc(as_str(ticket["staff_name"]) or as_str(ticket["target_admin_id"]))}</td>'
        f'<td class="small mut">{esc(fmt_when(ticket["created_at"]))}</td>'
        f'<td class="small mut">{esc(fmt_when(ticket["updated_at"]))}</td></tr>'
        for ticket in tickets
    ) or "<tr><td colspan='7' class='mut'>Обращений не было</td></tr>"
    open_n = sum(1 for ticket in tickets if as_str(ticket["status"]) in OPEN_STATUSES)
    done_n = len(tickets) - open_n
    table = (f'<table><tr><th>№</th><th>Раздел</th><th>Тема</th><th>Статус</th>'
             f"<th>Ведёт</th><th>Создано</th><th>Движение</th></tr>{body}</table>")
    # capped - честная оговорка: person_dossier читает ограниченное число
    # обращений, и молчать об этом значило бы выдать список за весь.
    tail = (f'<p class="small mut">Показаны первые {len(tickets)} обращений: '
            "остальные ищите в рабочем месте по MAX ID.</p>" if capped else "")
    return f"""<div class="card"><h2>{icon("tickets", 20)} Обращения: {len(tickets)}</h2>
<div class="kpi">
  <div><b>{len(tickets)}</b><span>обращений всего</span></div>
  <div class="warn"><b>{open_n}</b><span>открытых</span></div>
  <div class="good"><b>{done_n}</b><span>закрытых</span></div>
</div>{table}{tail}
<p class="small mut">Номер обращения открывает рабочее место с выбранным
обращением. Архивные обращения помечены, но в счётчики не убраны: они тоже были
обращениями этого человека.</p></div>"""


def _parse_cell(schedule, count: int) -> str:
    """Разбор расписания группы: что получилось, когда и не устарело ли оно."""
    if not schedule:
        return '<span class="mut">ссылки на PDF нет: расписание не заведено</span>'
    stamp = schedule_stamp(schedule)
    if stamp["parse_error"]:
        return (f'{pill("ошибка разбора", "off")} '
                f'<span class="small">{esc(short(stamp["parse_error"], 90))}</span>')
    if not count:
        return '<span class="mut">не разобрано: студент увидит ссылку, а не пары</span>'
    fresh = stamp_is_fresh(stamp, config.SCHEDULE_CACHE_HOURS)
    age = esc(fmt_when(stamp["parsed_at"]) or "—")
    hours = esc(config.SCHEDULE_CACHE_HOURS)
    return (f'{pill("разобрано", "on")} <b>{count} пар</b> · {age} · '
            + (pill("актуально", "on") if fresh
               else f'<span class="small mut">разбор старше {hours} ч, бот перечитает файл сам</span>'))


async def _schedule_card(person: dict, group: str) -> str:
    """Расписание группы студента: подписан ли он на неё и разобрано ли оно.

    Подписка и разбор - разные вещи, и их постоянно путают: подписку студент
    ставит себе сам кнопкой в боте, а разбор делает сис-админ на вкладке
    «Расписания». Поэтому в таблице две отдельные строки, и каждая говорит о
    своём: подписан студент или нет, и что бот покажет ему по расписанию.
    """
    groups = [part.strip() for part in as_str(person["subscriptions"]).split("·")]
    groups = [item for item in groups if item]
    own = bool(group) and any(same_group(item, group) for item in groups)
    rows = [
        ("Подписка на свою группу",
         pill("подписан", "on") if own else pill("не подписан", "off")),
        ("Подписан на группы",
         " ".join(pill(item) for item in groups) if groups
         else "<span class='mut'>ни на одну: уведомлений о расписании не приходит</span>"),
    ]
    if not group:
        rows.append(("Расписание группы", '<span class="mut">группа не указана</span>'))
    else:
        schedule = await get_schedule(group)
        count = await lessons_count(group)
        rows.append(("Ссылка на PDF",
                     esc(short(as_str(schedule["pdf_url"]), 70)) if schedule
                     else "<span class='mut'>не задана</span>"))
        rows.append(("Разбор расписания", _parse_cell(schedule, count)))
        found = ", ".join(schedule_stamp(schedule)["found_groups"][:8]) if schedule else ""
        if found:
            rows.append(("Группы в самом PDF", esc(found)))
    return f"""<div class="card"><h2>{icon("schedules", 20)} Расписание</h2>
<table>{_table(rows)}</table>
<p class="small mut wb-tools">
<a class="btn btn-grey" href="/panel/schedules">{icon("schedules", 16)} Все расписания</a>
{f'<a class="btn btn-grey" href="/panel/schedules/{quote(esc(group))}">{icon("chevron-right", 16)} Пары группы {esc(group)}</a>' if group else ''}
</p>
<p class="small mut">Подписку студент ставит себе сам кнопкой в боте: без неё
уведомлений об изменении расписания ему не придёт. Разбор делает сис-админ на
вкладке «Расписания» - пока файл не разобран, бот показывает студенту ссылку на
PDF, а не пары текстом.</p></div>"""


@router.get("/students/{user_id}")
async def student_card(request: Request, user_id: str):
    """Карточка студента: личность, регистрация, обращения, расписание, согласие.

    Человека, которого бот не знает, страница не выдаёт за ошибку: показан
    честный отказ со ссылкой на список студентов - иначе сис-админ решил бы, что
    панель сломалась, и полез бы искать неисправность вместо опечатки в MAX ID.
    """
    viewer = await require_user(request)
    uid = as_str(user_id)
    data = await person_dossier(uid)
    if not data:
        return page(f"Карточка {uid}",
                    '<div class="card msg-bad">Такого человека бот не знает: он ни разу '
                    "не писал боту и не регистрировался. Проверьте MAX ID.</div>"
                    '<p class="wb-tools"><a class="btn btn-grey" href="/panel/students">'
                    f'{icon("students", 16)} К студентам</a></p>',
                    viewer, "/students")
    person = data["person"]
    name = fio(person["full_name"], uid)
    group = as_str(person["group_code"])
    body = "".join(part for part in (
        _head_card(uid, person, name, group),
        await _schedule_card(person, group),
        _tickets_card(data["tickets"], data["capped"]),
        _consent_card(person),
    ) if part)
    return page(f"Студент: {name}", body, viewer, "/students",
                actions=f'<a class="btn btn-grey" href="/panel/people/{esc(uid)}/dossier">'
                        f'{icon("eye", 16)} Всё о человеке</a>')
