"""Веб-панель сис-админа: /panel — те же данные, что и в боте, но редактируются мышью.

Панель живёт в том же процессе, что и бот (FastAPI), поэтому все изменения сразу
видны боту и в MAX. Вход — по MAX ID сис-админа и паролю WEB_PANEL_PASSWORD из .env;
пока пароль не задан, панель отвечает 503.

Вкладки: обзор, обращения, пользователи, сотрудники, коды и заявки, база данных,
настройки, журнал и тесты. JSON-API для скриптов и проверок — /panel/api/*.

Сам код панели разложен по разделам в пакете web/: web/common.py — доступ, формы и
оболочка страницы, web/tickets.py — обращения, web/people.py — люди и так далее.
Этот модуль — фасад: он отдаёт наружу всё, чем панель пользуются снаружи (bot.py
подключает webpanel:router, schedule_watch и handlers берут константы, тесты —
хелперы), поэтому список исчерпывающе задан в __all__.

Три функции остались здесь намеренно: fio(), _tickets_queue() и
_ticket_workbench(). Проверки test_panel_full_names смотрят на исходник
webpanel.py, а не на страницу, и ищут их именно здесь. Из-за этого
web/people.py и web/tickets.py берут fio(), а web/tickets.py - ещё и обе
колонки рабочего места, локальным импортом внутри функции: иначе вышел бы круг
импортов webpanel -> web.tickets -> webpanel.
"""
import sys
from types import ModuleType

from fastapi import Request

import repository as repo
import schedule_import
from handlers.admin import probe_pdf_url
from handlers.broadcast import run_broadcast
from panel_theme import actions_script, hotkeys_script, icon
from utils import CATS, STATUS, as_str, cut_plain, fmt_when, person_label

from web.access import _join_page_html
from web.common import (COOKIE, FIO_MAX, JOIN_STYLE, NAV_GROUPS, NAV_SECTIONS, STYLE, _action_form,
                        _csrf, _flashes, _sessions, code_cell, copy_btn, csrf, esc, is_sysadmin,
                        nice_max, open_in_bot, page, panel_link, plain, select)
from web.database import schema_broken_page
from web.mailer import BROADCASTS_PAGE
from web.overview import ACTIVITY_PAGE
from web.people import PEOPLE_PAGE, STUDENTS_PAGE
from web.router import open_router, router
from web.schedules import COLLEGE_SCHEDULE_PAGE
from web.settings import HUMAN_SETTINGS, HUMAN_SETTING_KEYS
from web.tickets import (ATTACH_PREFIX, CERT_PICKUP, EVENT_LABELS, TEMPLATES_PAGE, TICKETS_PAGE,
                         _staff_choices, is_certificate, pickup_hint, pickup_options)


def fio(name, user_id: str = "", limit: int = 0) -> str:
    """ФИО для панели: целиком, а если ФИО не заполнено - по MAX ID.

    Сокращать ФИО в панели нельзя: сотрудник приёмной комиссии сверяет
    обращение с человеком по записи, и «Ковалевский Ко…» для этого бесполезно.
    Имя переносится по словам средствами темы (``.wb-fio``), а ``limit`` -
    последняя страховка для мест, где переноса не будет (выпадающий список):
    обрезка идёт по границе слова и без многоточия (``utils.cut_plain``).
    """
    label = " ".join(as_str(name).split())
    if not label:
        return person_label("", user_id, limit)   # ФИО не заполнено: «ID 300»
    return cut_plain(label, limit) if limit else label


def _tickets_queue(request: Request, rows, latest: dict, query: str, selected, archived: bool) -> str:
    """Левая колонка: отметки для массовых действий и переход в карточку."""
    if not rows:
        return '<div class="card mut">Обращений нет.</div>'
    visible = rows[:TICKETS_PAGE]
    ids = ",".join(str(row["ticket_id"]) for row in visible)
    items = []
    for row in visible:
        ticket_id = row["ticket_id"]
        mark = "wb-on" if selected and int(selected["ticket_id"]) == int(ticket_id) else ""
        waiting = latest.get(int(ticket_id)) == "student"
        # db.many отдаёт sqlite3.Row, поэтому по именам колонок идём через dict()
        data = dict(row)
        # ФИО из базы приходит целиком, и резать его нельзя: очередь сверяют
        # с записью человека. Поэтому ФИО - своей строкой, группа - рядом с
        # ним отдельным элементом, а текст обращения идёт следующей строкой.
        who = fio(data.get("student_name"), data.get("student_id"), FIO_MAX)
        group = " ".join(as_str(data.get("student_group")).split())
        group_html = f'<span class="wb-group">{esc(group)}</span>' if group else ""
        status = esc(plain(STATUS.get(data["status"], data["status"])))
        if waiting:
            status += f' <span class="wb-wait">{icon("clock", 14)} ждёт ответа</span>'
        items.append(
            f'<label class="wb-item {mark}" data-hk><input type="checkbox" name="tids" value="{esc(ticket_id)}">'
            f'<a href="/panel/tickets?{query}&t={esc(ticket_id)}">'
            f'<span class="wb-head"><b>№{esc(ticket_id)}</b>'
            f'<span class="wb-status">{status}</span>'
            f'<span class="wb-date">{esc(fmt_when(data["updated_at"]))}</span></span>'
            f'<span class="wb-who"><span class="wb-fio">{esc(who)}</span>{group_html}</span>'
            f'<span class="wb-text" title="{esc(data["text_content"])}">'
            f'{esc(cut_plain(data["text_content"], 90))}</span>'
            f'</a></label>')
    head = f'{icon("archive", 18)} Из архива' if archived else f'{icon("inbox", 18)} Очередь · {len(rows)}'
    more = (f'<p class="small mut">Показано {len(visible)} из {len(rows)} — сузьте фильтр '
            f'по статусу, чтобы увидеть нужное.</p>' if len(rows) > len(visible) else "")
    return f"""<div class="card"><h2>{head}</h2>
<form method="post" action="/panel/tickets/bulk">{csrf(request)}
<input type="hidden" name="return" value="{esc(query)}">
<div class="wb-list">{''.join(items)}</div>
<details class="wb-bulk"><summary>Групповые действия для отмеченных</summary>
<div class="grid" style="margin-top:8px">
<div>{select("action", {"assign": "Назначить сотрудника",
                        "status": "Сменить статус",
                        "pickup": "Кабинет выдачи",
                        "archive": "В архив"}, "assign")}</div>
<div><button class="btn-ok">Применить</button></div></div>
<div><label>Куда</label><input name="value"
  placeholder="сотрудник, статус или кабинет 115"></div>
<p class="small mut">Отметьте обращения галочкой слева. Поле «Куда» принимает свой текст:
например <code>115</code> для кабинета справок или <code>in_progress</code> для статуса.</p>
</details>
<input type="hidden" name="all" value="{esc(ids)}">
</form>{more}</div>"""


async def _ticket_workbench(request: Request, t, bot: str = "") -> str:
    """Правая колонка: карточка со всеми правками и быстрыми ответами.

    Формы правки и ответа помечены классами ``wb-edit`` и ``wb-reply`` - на
    печати их нет, поэтому на бумагу попадает переписка, а не пустые поля.
    """
    ticket_id = int(t["ticket_id"])
    # ФИО целиком; без ФИО подписью становится MAX ID (person_label) - так же,
    # как в очереди, чтобы человека узнавали в обоих списках одинаково.
    student_name = fio(t["student_name"], t["student_id"])
    student_group = as_str(t["student_group"]) or "—"
    messages = await repo.ticket_thread(ticket_id, 100)
    events = await repo.ticket_events(ticket_id, 50)
    staff_options, staff_titles = await _staff_choices(as_str(t["target_admin_id"]),
                                                       as_str(t["staff_name"]))
    template_options = {"": "— шаблон —"}
    for row in await repo.list_templates():
        template = dict(row)
        key = as_str(template.get("template_id") or template.get("id"))
        if key:
            template_options[key] = as_str(template.get("title"))
    def _cell(m) -> str:
        body = esc(m["text"])
        if as_str(m["text"]).startswith(ATTACH_PREFIX):
            file_name = as_str(m["text"])[len(ATTACH_PREFIX):].split(" (")[0]
            body = (f'<a href="/panel/attachments/{esc(file_name)}">'
                    f'{icon("attach", 14)} {esc(file_name)}</a>'
                    f' <span class="small mut">открыть</span>')
        return f"<td>{body}</td></tr>"

    thread = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(m['created_at']))}</td>"
        f"<td class='small'>"
        f"{icon('students' if m['sender_role'] == 'student' else 'staff', 16)} "
        f"{esc(m['sender_name'])}{' · ' + esc(m['position']) if m['position'] else ''}"
        f"<div class='small mut'>{esc(m['group_code'] or m['sender_id'])}</div></td>"
        + _cell(m)
        for m in reversed(messages)
    ) or "<tr><td colspan='3' class='mut'>Сообщений нет</td></tr>"
    event_rows = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(e['created_at']))}</td>"
        f"<td class='small'>{esc(e['actor_name'] or e['actor_id'])}</td>"
        f"<td class='small'>{esc(EVENT_LABELS.get(e['event'], e['event']))}: {esc(e['detail'] or '—')}</td></tr>"
        for e in reversed(events)
    ) or "<tr><td colspan='3' class='mut'>Событий нет</td></tr>"
    pickup_options_html = await pickup_options()
    edit = f"""
<form method="post" action="/panel/tickets/{ticket_id}/edit" class="wb-edit">{csrf(request)}
<div class="grid">
<div class="full"><label>Текст обращения</label>
<textarea name="text_content" rows="2">{esc(t['text_content'])}</textarea></div>
<div>{select("target_admin_id", staff_options, "", titles=staff_titles)}</div>
<div>{select("category", dict(CATS), as_str(t['category']))}</div>
<div><label>Тема</label><input name="topic" value="{esc(t['topic'])}"></div>
<div><label>Кабинет выдачи</label>
<input name="pickup_place" value="{esc(t['pickup_place'])}"
  list="pickup-rooms" placeholder="{esc(pickup_hint(t))}"></div>
<datalist id="pickup-rooms">{pickup_options_html}</datalist>
<div><label>Срок готовности</label><input name="ready_until" value="{esc(t['ready_until'])}"
  placeholder="например, 15:00 в пятницу"></div>
<div>{select("status", dict(STATUS), as_str(t['status']))}</div>
</div>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Сохранить</button></div>
<div>{_action_form(request, f'/panel/tickets/{ticket_id}/ready',
                   f'{icon("check", 16)} Готово')}</div>
<div>{_action_form(request, f'/panel/tickets/{ticket_id}/archive',
                   f'{icon("archive", 16)} В архив', cls="btn-grey")}</div>
</div>
<p class="small mut">Пустое поле не затирает старое значение. «Готово» пишет
студенту кабинет и закрывает обращение. Кабинет {CERT_PICKUP} зашит только
под справки; для остального нужно указать кабинет, иначе панель не закроет
обращение молча.</p></form>"""
    # шапка карточки - то, что должно попасть на бумагу
    head = f"""<div class="card"><h2>Обращение №{ticket_id}
<span class="pill">{esc(plain(STATUS.get(as_str(t['status']), as_str(t['status']))))}</span></h2>
<p class="wb-who">{icon('students', 16)}<span class="wb-fio">{esc(student_name)}</span>
<span class="wb-group">{esc(student_group)}</span>
<span class="wb-id">ID {code_cell(t['student_id'], "MAX ID студента скопирован")}</span>
<span class="wb-when">создано {esc(fmt_when(t['created_at']))}</span></p>
<p class="small mut wb-tools">{open_in_bot(bot)}
{copy_btn(panel_link(f"/tickets?t={ticket_id}"), "Ссылка на обращение скопирована")}</p>
{edit}</div>"""
    return f"""{head}
<div class="card"><h2>Быстрый ответ</h2>
<form method="post" action="/panel/tickets/{ticket_id}/reply" class="wb-reply">{csrf(request)}
<textarea name="text" rows="3" required placeholder="Ответ студенту — уйдёт в MAX"></textarea>
<div class="grid" style="margin-top:10px">
<div><button class="btn-ok">Отправить</button></div>
<div>{select("template", template_options, "")}</div>
</div></form>
<p class="small mut">Ответ уходит студенту и остаётся в переписке бота.</p></div>
<div class="card"><h2>Переписка</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Текст</th></tr>{thread}</table></div>
<div class="card"><h2>История изменений</h2>
<table><tr><th>Когда</th><th>Кто</th><th>Событие</th></tr>{event_rows}</table></div>"""


# Список исчерпывающий: наружу панель отдаёт ровно то, чем пользуются снаружи.
__all__ = [
    "ACTIVITY_PAGE", "BROADCASTS_PAGE", "CERT_PICKUP", "COLLEGE_SCHEDULE_PAGE", "COOKIE",
    "HUMAN_SETTINGS", "HUMAN_SETTING_KEYS", "JOIN_STYLE", "NAV_GROUPS", "NAV_SECTIONS",
    "PEOPLE_PAGE", "STUDENTS_PAGE", "STYLE", "TEMPLATES_PAGE",
    "_csrf", "_flashes", "_join_page_html", "_sessions", "_tickets_queue", "_ticket_workbench",
    "actions_script", "hotkeys_script", "fio", "is_certificate", "is_sysadmin", "nice_max",
    "open_router", "page", "panel_link", "person_label", "pickup_hint", "pickup_options",
    "probe_pdf_url", "router", "run_broadcast", "schedule_import", "schema_broken_page",
]


class _Facade(ModuleType):
    """Подмена имени на фасаде доходит до модуля, который им пользуется.

    Проверки подменяют webpanel.run_broadcast и webpanel.probe_pdf_url. Когда обе
    функции лежали в webpanel.py, это работало само собой. Теперь они в
    web/mailer.py и web/schedules.py, и обычный setattr на фасаде изменил бы
    только ссылку здесь, а маршрут позвал бы старую функцию - подмена вышла бы
    тихой и незаметной. Поэтому пишем в оба места.
    """

    PATCHABLE = {"run_broadcast": "web.mailer", "probe_pdf_url": "web.schedules"}

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        owner = self.PATCHABLE.get(name)
        if owner:
            setattr(sys.modules[owner], name, value)


sys.modules[__name__].__class__ = _Facade
