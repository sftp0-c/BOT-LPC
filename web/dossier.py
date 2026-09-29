"""Досье человека: всё о нём на одной странице.

Карточка человека отвечала на вопрос «кто он и что о нём записано», а всё
остальное - обращения, переписка, права, подписки, действия сис-админа - лежало
по другим вкладкам. Чтобы понять, что с человеком происходит, приходилось
переходить туда-сюда и собирать картину по кускам. Досье показывает её целиком:
сверху кто он, ниже согласие и права, затем нагрузка, все его обращения,
переписка по выбранному обращению с поиском по тексту, подписки на расписание
и журнал действий с ним.

Данные приходят одним вызовом store.people.person_dossier(): страница задаёт
вопрос «расскажи всё про этого человека» и рисует ответ. В шаблоне ни одного
запроса - иначе карточка стоила бы столько запросов, сколько на ней блоков.
"""
import clock
from fastapi import Request
from handlers.admin import STAFF_ROLES
from panel_theme import icon
from store.people import person_dossier
from store.staff import staff_activity
from store.tickets import ticket_thread
from utils import (STAFF_CATS, STATUS, as_str, fmt_when, is_sysadmin_role, norm_position,
                   profile_url, to_int)

from .common import (code_cell, copy_btn, esc, fio, fio_brief, is_sysadmin, page, pager,
                     pages_of, panel_link, pill, plain, require_user)
from .router import router


TICKETS_PAGE = 25        # обращений на страницу списка в досье
THREAD_PAGE = 100       # сколько сообщений переписки показываем
LOAD_DAYS = 90          # окно нагрузки сотрудника - как в списке сотрудников


async def _extra(uid: str, wanted: int, needle: str, is_staff: bool) -> dict:
    """Нагрузка сотрудника и переписка по обращению - из своих разделов.

    Ни одна из этих строк не считается заново: нагрузку берёт
    ``store.staff.staff_activity`` (та же, что в списке сотрудников и в его
    карточке), переписку - ``store.tickets.ticket_thread`` (та же, что в
    рабочем месте). Здесь только отбор: чужое обращение по подставленному в
    адрес номеру не открывается.
    """
    result = {"load": {}, "thread": [], "thread_ticket": 0}
    if is_staff:
        result["load"] = (await staff_activity(LOAD_DAYS)).get(uid, {})
    if wanted:
        messages = await ticket_thread(wanted, THREAD_PAGE)
        low = needle.lower()
        result["thread"] = [row for row in reversed(messages)
                            if not low or low in as_str(row["text"]).lower()]
        result["thread_ticket"] = wanted
    return result


def profile_cell(username) -> str:
    """Ссылка на профиль MAX; без ника — прочерк (ник у MAX бывает скрытым).

    Копия помощника из web.people. Модуль досье подключается к пакету из
    web.people, и обратный импорт собрал бы круг; четыре строки дублируются
    намеренно, чтобы профиль человека на всех страницах выглядел одинаково.
    """
    name = as_str(username).strip()
    link = profile_url(name)
    if not link:
        return "<span class='mut'>ник скрыт</span>"
    return f'<a href="{esc(link)}" target="_blank" rel="noopener">@{esc(name)}</a>'


def dossier_url(user_id: str, status: str = "", ticket: int = 0, q: str = "",
                page_no: int = 0) -> str:
    """Адрес досье с фильтрами: им пользуются все кнопки страницы.

    Сборщик один, потому что фильтры теряться не должны: открытая переписка и
    выбранный статус переживают переход по любой кнопке, иначе каждый клик
    возвращал бы сис-админа к общему списку.
    """
    parts = []
    if as_str(status).strip():
        parts.append(f"status={esc(as_str(status).strip())}")
    if to_int(ticket):
        parts.append(f"ticket={to_int(ticket)}")
    if as_str(q).strip():
        parts.append(f"q={esc(as_str(q).strip())}")
    if to_int(page_no) > 1:
        parts.append(f"page_no={to_int(page_no)}")
    base = f"/panel/people/{esc(user_id)}/dossier"
    return f"{base}?{'&'.join(parts)}" if parts else base


def _table(rows: list[tuple[str, str]]) -> str:
    """Пары «подпись — значение» разметкой таблицы карточки."""
    return "".join(f"<tr><th>{esc(name)}</th><td>{value}</td></tr>" for name, value in rows)


def _head_card(uid: str, person: dict, name: str) -> str:
    """Кто он: имя, ники, группа и сколько дней он в боте."""
    days = to_int(person["days_in_bot"])
    stay = ("пришёл сегодня" if not days
            else f"{days} {clock.plural(days, 'день', 'дня', 'дней')} в боте")
    brief = fio_brief(name)
    rows = [
        ("Кто это", pill(plain(person["kind_title"]))),
        ("MAX ID", code_cell(uid, "MAX ID скопирован")),
        ("Профиль MAX", profile_cell(person["username"])),
        ("Группа", esc(as_str(person["group_code"]) or "—")),
        ("Первое обращение", f"{esc(fmt_when(person['first_seen']) or '—')} · {esc(stay)}"),
        ("Сообщений боту", esc(to_int(person["messages"]))),
        ("Последний контакт", esc(fmt_when(person["last_seen"]) or "—")),
    ]
    return f"""<div class="card"><h2 class="wb-name">{esc(name)}</h2>
{f'<p class="small mut wb-brief">{esc(brief)}</p>' if brief else ''}
<table>{_table(rows)}</table>
<p class="small mut wb-tools">
<a class="btn" href="/panel/people/{esc(uid)}">{icon("user", 16)} Карточка</a>
<a class="btn btn-grey" href="/panel/staff/{esc(uid)}">{icon("staff", 16)} Сотрудник</a>
{copy_btn(panel_link(f'/people/{uid}/dossier'), 'Ссылка на досье скопирована')}</p></div>"""


def _consent_card(person: dict) -> str:
    """Согласие на обработку данных: когда человек его дал и на каких условиях."""
    given = as_str(person["consent_at"])
    mark = pill("согласие есть", "on") if given else pill("согласия нет", "off")
    rows = [
        ("Отметка", mark),
        ("Когда дал", esc(fmt_when(given)) if given else "—"),
        ("Редакция текста", esc(as_str(person["consent_version"]) or "—")),
        ("Зарегистрирован", esc(fmt_when(person["registered_at"]) or "—")),
    ]
    return f"""<div class="card"><h2>{icon("check", 20)} Согласие на обработку данных</h2>
<table>{_table(rows)}</table>
<p class="small mut">Отметка хранится рядом с человеком: по ней видно, что согласие
было получено и на каких условиях. Пустая отметка - это не «не важно», а «человек
не соглашался».</p></div>"""


async def _rights_card(uid: str, person: dict) -> str:
    """Права в боте: роль, должность, раздел обращений, рассылка, отпуск.

    Блок есть только у сотрудников и сис-админов: у студента прав в боте нет,
    и пустая таблица «собаки не ждут» вводила бы в заблуждение.
    """
    if not person["has_admin"]:
        return ""
    role_type = as_str(person["role_type"])
    role = ("владелец" if role_type == "owner" else
            "сис-админ" if is_sysadmin_role(role_type) else "сотрудник")
    category = as_str(person["ticket_category"])
    position = norm_position(person["position"]) or STAFF_ROLES.get(as_str(person["role"]), "—")
    # Доступ к панели спрашиваем тем же правилом, что и вход: у человека может
    # быть строка сотрудника, а его ID лежать в .env - тогда он в панели есть.
    sees_panel = await is_sysadmin(uid)
    rows = [
        ("Роль в боте", esc(role)),
        ("Доступ в панель", pill("есть", "on") if sees_panel else pill("нет", "off")),
        ("Должность", esc(position)),
        ("Кабинет", esc(as_str(person["office"]) or "—")),
        ("Отдел", esc(as_str(person["department"]) or "—")),
        ("Раздел обращений", esc(plain(STAFF_CATS.get(category, category or "—")))),
        ("Видит чужие обращения", pill("все", "on") if bool(person["see_all_tickets"]) or sees_panel
         else pill("только свои", "off")),
        ("Рассылка", pill("разрешена", "on") if to_int(person["can_broadcast"])
         else pill("запрещена", "off")),
        ("Отпуск до", esc(as_str(person["vacation_until"]) or "не в отпуске")),
        ("В системе с", esc(fmt_when(person["staff_since"]) or "—")),
    ]
    return f"""<div class="card"><h2>{icon("access", 20)} Права</h2>
<table>{_table(rows)}</table>
<p class="small mut">Раздел обращений решает, какие обращения сотрудник увидит:
«Всё» - все, «Справки» - только справки. Меняется прямо в строке таблицы
<a href="/panel/staff">сотрудников</a>, с подтверждением.</p></div>"""


def _load_card(load: dict) -> str:
    """Нагрузка сотрудника за 90 дней.

    Числа те же, что в списке сотрудников и в карточке сотрудника: посчитать их
    второй раз здесь - значит через месяц получить две разные правды.
    """
    last = as_str(load.get("last_reply", ""))
    return f"""<div class="card"><h2>{icon("activity", 20)} Нагрузка за 90 дней</h2>
<div class="kpi">
  <div><b>{to_int(load.get('tickets', 0))}</b><span>обращений</span></div>
  <div class="warn"><b>{to_int(load.get('open', 0))}</b><span>открытых</span></div>
  <div><b>{esc(fmt_when(last) if last else '—')}</b><span>последнее движение</span></div>
</div>
<p class="small mut">Считает <code>store.staff.staff_activity</code> - та же строка,
что и в списке сотрудников: обращения, где он ответственный, за 90 дней.</p></div>"""


def _status_counters(uid: str, counts: dict, status: str) -> str:
    """Счётчики по статусам: они же фильтр - нажатый оставляет один статус."""
    total = sum(counts.values())
    items = [f'<a class="btn{" btn-grey" if status else ""}" data-ds-all="{total}" '
             f'href="{esc(dossier_url(uid))}">все <b>{total}</b></a>']
    for code, number in sorted(counts.items(), key=lambda item: -item[1]):
        items.append(f'<a class="btn{" btn-ok" if status == code else " btn-grey"}" '
                     f'data-ds-status="{esc(code)}" data-ds-count="{number}" '
                     f'href="{esc(dossier_url(uid, status=code))}">'
                     f'{esc(plain(STATUS.get(code, code)))} <b>{number}</b></a>')
    return f'<p class="small mut wb-tools">{"".join(items)}</p>'


def _tickets_card(uid: str, tickets: list, counts: dict, status: str, rows: list,
                  needle: str, page_no: int, pages: int) -> str:
    """Список всех обращений человека: счётчики, фильтр и переход в переписку."""
    body = "".join(
        f'<tr data-hk data-ds-ticket="{esc(ticket["ticket_id"])}" '
        f'data-ds-state="{esc(ticket["status"])}">'
        f'<td><a href="{esc(dossier_url(uid, status=status, ticket=to_int(ticket["ticket_id"]), q=needle))}">'
        f'№{esc(ticket["ticket_id"])}</a>'
        f'{" " + pill("в архиве", "off") if as_str(ticket["deleted_at"]) else ""}</td>'
        f'<td>{esc(plain(STAFF_CATS.get(as_str(ticket["category"]), as_str(ticket["category"]))))}</td>'
        f'<td>{esc(as_str(ticket["topic"]) or "—")}</td>'
        f'<td>{esc(plain(STATUS.get(as_str(ticket["status"]), as_str(ticket["status"]))))}</td>'
        f'<td class="small">{esc(as_str(ticket["staff_name"]) or as_str(ticket["target_admin_id"]))}</td>'
        f'<td class="small mut">{esc(fmt_when(ticket["created_at"]))}</td>'
        f'<td class="small mut">{esc(fmt_when(ticket["updated_at"]))}</td></tr>'
        for ticket in rows
    ) or "<tr><td colspan='7' class='mut'>Обращений с таким статусом нет</td></tr>"
    table = (f'<table class="data-table"><tr><th>№</th><th>Раздел</th><th>Тема</th>'
             f"<th>Статус</th><th>Отвечает</th><th>Создано</th><th>Движение</th></tr>"
             f"{body}</table>")
    pages_bar = pager(f"/panel/people/{esc(uid)}/dossier",
                      [("status", status), ("q", needle)], page_no, pages,
                      f"обращений: {len(tickets)}")
    return f"""<div class="card"><h2>{icon("tickets", 20)} Обращения: {len(tickets)}</h2>
{_status_counters(uid, counts, status)}{table}{pages_bar}
<p class="small mut">Номер обращения открывает переписку на этой же странице.
Архивные обращения помечены, но из счётчиков статусов не убраны.</p></div>"""


def _thread_card(uid: str, ticket_id: int, thread: list, needle: str) -> str:
    """Переписка по выбранному обращению - с поиском по тексту сообщений."""
    if not ticket_id:
        return ('<div class="card mut">Откройте обращение в списке выше по номеру: '
                "переписка и поиск по её тексту появятся здесь, на этой же странице.</div>")
    search = f"""<form method="get" action="/panel/people/{esc(uid)}/dossier" class="grid">
<div><label>Поиск по тексту сообщений</label>
<input name="q" value="{esc(needle)}" placeholder="например: справка готова"></div>
<div><input type="hidden" name="ticket" value="{to_int(ticket_id)}"></div>
<div><button class="btn-grey">{icon("search", 16)} Найти</button></div>
<div><a class="btn btn-grey" href="{esc(dossier_url(uid, ticket=ticket_id))}">
{icon("close", 16)} Вся переписка</a></div></form>"""
    body = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(message['created_at']))}</td>"
        f"<td class='small'>{icon('students' if as_str(message['sender_role']) == 'student' else 'staff', 16)} "
        f"{esc(as_str(message['sender_name']) or as_str(message['sender_id']))}"
        f"<div class='small mut'>{esc(as_str(message['group_code']) or as_str(message['position']) or '—')}</div></td>"
        f"<td>{esc(message['text'])}</td></tr>"
        for message in thread
    ) or "<tr><td colspan='3' class='mut'>Сообщений с этим текстом нет</td></tr>"
    found = (f"Найдено сообщений: <b>{len(thread)}</b> по слову «{esc(needle)}»."
             if needle else "Вся переписка, свежие сверху.")
    return f"""<div class="card"><h2>{icon("reply", 20)} Переписка по обращению
№{to_int(ticket_id)}</h2>{search}
<p class="small mut">{found}</p>
<table><tr><th>Когда</th><th>Кто</th><th>Текст</th></tr>{body}</table>
<p class="small mut">Переписка показана здесь же: уходить в карточку обращения не
нужно. <a href="/panel/tickets?t={to_int(ticket_id)}">Открыть в рабочем месте</a>.</p></div>"""


def _subs_card(person: dict) -> str:
    """На что подписан: группы, обновления расписания которых он получает."""
    groups = [part.strip() for part in as_str(person["subscriptions"]).split("·")]
    groups = [group for group in groups if group]
    body = (" ".join(pill(group) for group in groups) if groups
            else "<span class='mut'>Не подписан ни на одну группу: уведомлений о "
                 "расписании не приходит.</span>")
    return f"""<div class="card"><h2>{icon("schedules", 20)} Подписки на расписание</h2>
<p>{body}</p><p class="small mut">Подписка приходит из бота: человек подписался
сам, когда ему понадобилось расписание своей группы.</p></div>"""


def _history_card(history: list) -> str:
    """Что сис-админ делал с этим человеком: тот же журнал, что на вкладке «Журнал»."""
    body = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(row['created_at']))}</td>"
        f"<td class='small'>{esc(as_str(row['actor_name']) or as_str(row['actor_id']))}</td>"
        f"<td class='small'>{esc(as_str(row['action']))}"
        f"<div class='small mut'>{esc(as_str(row['details']))}</div></td></tr>"
        for row in history
    ) or "<tr><td colspan='3' class='mut'>С этим человекем действий не было</td></tr>"
    return f"""<div class="card"><h2>{icon("logs", 20)} Действия сис-админа с человеком</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Что сделал</th></tr>{body}</table>
<p class="small mut">Только те строки журнала, где упомянут этот MAX ID. Содержимого
переписки в журнал не пишется: оно и не ищется, и не хранится.</p></div>"""


@router.get("/people/{user_id}/dossier")
async def dossier_page(request: Request, user_id: str, status: str = "", ticket: int = 0,
                       q: str = "", page_no: int = 1):
    """Досье человека: личность, согласие, права, обращения, переписка, журнал.

    Человека, которого бот не знает, страница не выдаёт за ошибку: показан
    честный отказ со ссылкой на реестр - иначе сис-админ решил бы, что панель
    сломалась, и полез бы искать неисправность вместо опечатки в MAX ID.
    """
    viewer = await require_user(request)
    uid = as_str(user_id)
    data = await person_dossier(uid)
    if not data:
        return page(f"Досье {uid}",
                    '<div class="card msg-bad">Такого человека бот не знает: он ни разу '
                    "не писал боту и не регистрировался. Проверьте MAX ID.</div>"
                    '<p><a class="btn btn-grey" href="/panel/people">К реестру людей</a></p>',
                    viewer, "/people")
    person = data["person"]
    name = fio(person["full_name"], uid)
    needle = as_str(q).strip()
    rows = [row for row in data["tickets"] if not status or as_str(row["status"]) == status]
    # переписку показываем только по обращению, которое действительно его
    wanted = to_int(ticket)
    if wanted and not any(to_int(row["ticket_id"]) == wanted for row in data["tickets"]):
        wanted = 0
    data |= await _extra(uid, wanted, needle, bool(person["has_admin"]))
    pages = pages_of(len(rows), TICKETS_PAGE)
    page_no = max(1, min(to_int(page_no, 1), pages))
    current = rows[(page_no - 1) * TICKETS_PAGE: page_no * TICKETS_PAGE]
    body = "".join(part for part in (
        _head_card(uid, person, name),
        _consent_card(person),
        await _rights_card(uid, person),
        _load_card(data["load"]) if person["has_admin"] else "",
        _tickets_card(uid, data["tickets"], data["counts"], status, current, needle, page_no, pages),
        _thread_card(uid, data["thread_ticket"], data["thread"], needle),
        _subs_card(person),
        _history_card(data["history"]),
    ) if part)
    actions = (f'<a class="btn btn-grey" href="/panel/tickets?t={data["thread_ticket"]}">'
               f'{icon("tickets", 16)} Открыть обращение</a>' if data["thread_ticket"] else "")
    return page(f"Досье: {name}", body, viewer, "/people", actions=actions)
