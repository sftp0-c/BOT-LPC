"""Обзор, лента событий и аналитика: что сделать сейчас и что было раньше."""
from fastapi import Request

import charts
import database as db
import repository as repo
from panel_theme import icon
from utils import OPEN_STATUSES, STAFF_CATS, STATUS, as_str, fmt_when, short, to_int

from .common import (bot_open_link, copy_btn, csrf, esc, flash, log, minutes_text, open_in_bot, page,
                     pager, pages_of, panel_link, plain, redirect, require_form, require_user, select)
from .people import _people_table
from .router import router


@router.get("/search")
async def global_search(request: Request, q: str = ""):
    """Общий поиск по панели: обращения, люди и сотрудники одним запросом."""
    user = await require_user(request)
    needle = as_str(q).strip()
    if len(needle) < 2:
        return page("Поиск", '<div class="card msg-bad">Введите хотя бы два символа.</div>', user, "/")
    low = needle.lower()
    tickets = [row for row in await repo.admin_tickets(None, 300)
               if low in as_str(row["text_content"]).lower()
               or low in as_str(row["topic"]).lower()
               or low in as_str(row["student_id"])]
    people = await repo.people(q=needle, limit=30)
    staff = [row for row in await repo.list_staff()
             if low in " ".join([as_str(row["user_id"]), as_str(row["full_name"]),
                                 as_str(row["position"]), as_str(row["department"])]).lower()]
    staff_rows = "".join(
        f'<tr><td><a href="/panel/staff/{esc(row["user_id"])}"><b>{esc(row["full_name"])}</b></a>'
        f'<div class="small mut">ID {esc(row["user_id"])}</div></td>'
        f'<td>{esc(as_str(row["position"]) or "—")}</td>'
        f'<td>{esc(as_str(row["department"]) or "—")}</td></tr>'
        for row in staff
    ) or "<tr><td colspan='3' class='mut'>Никого не нашлось</td></tr>"
    staff_table = f"<table><tr><th>Сотрудник</th><th>Должность</th><th>Отдел</th></tr>{staff_rows}</table>"
    body = (f'<p class="small mut">По запросу «{esc(needle)}»</p>'
            f"<div class=\"card\"><h2>{icon("tickets", 20)} Обращения: {len(tickets)}</h2>{_tickets_table(tickets[:20])}</div>"
            f"<div class=\"card\"><h2>{icon("people", 20)} Пользователи: {len(people)}</h2>{_people_table(people)}</div>"
            f"<div class=\"card\"><h2>{icon("staff", 20)} Сотрудники: {len(staff)}</h2>{staff_table}</div>")
    return page("Поиск", body, user, "/")


# ── обзор ─────────────────────────────────────────────────────────────────────
@router.get("/")
async def overview(request: Request):
    """«Пульт»: счётчики, что требует действия сегодня, лента и пробелы в данных.

    Страница отвечает на два вопроса: «что сделать сейчас» и «кто что делал
    последним». Всё остальное живёт в своих разделах, поэтому сюда не возвращаются
    таблицы ради таблиц.
    """
    user = await require_user(request)
    st = await repo.stats_overview()
    people = await repo.people_overview()
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    today = await repo.admin_today()
    events = await repo.recent_ticket_events(20)
    open_total = sum(counts.get(code, 0) for code in OPEN_STATUSES)
    bot = await bot_open_link()

    counters = "".join([
        _big_stat("/panel/tickets?scope=waiting", "tickets", today["no_answer"],
                  "без ответа", "bad"),
        _big_stat("/panel/access", "access", today["requests"],
                  "заявок на роль", "warn"),
        _big_stat("/panel/nostaff", "user-off", today["no_staff"],
                  "без прав сотрудника", "warn"),
        _big_stat("/panel/tickets?status=ready", "check", today["ready_not_picked"],
                  "готово к выдаче", "good"),
        _big_stat("/panel/database", "refresh", today["stuck_states"],
                  "зависших диалогов", "warn"),
        _big_stat("/panel/analytics", "clock", today["avg_reply"] or "—",
                  "среднее время ответа", "", text_value=True),
    ])
    scale = " · ".join([
        f"<b>{st['students']}</b> студентов",
        f"<b>{st['staff']}</b> сотрудников",
        f"<b>{people['total']}</b> писали боту",
        f"<b>{st['total']}</b> обращений всего",
        f"<b>{open_total}</b> открытых",
        f"<b>{st['week']}</b> за 7 дней",
        f"<b>{today['tickets_day']}</b> за сутки",
        f"<b>{esc(dedupe['processed'])}</b> событий обработано",
    ])
    # что требует действия: строки всегда на месте, ноль означает «делать нечего»
    actions = "".join([
        _todo_row("Обращений без ответа", today["no_answer"],
                  "/panel/tickets?scope=waiting", "tickets"),
        _todo_row("Готово к выдаче, но не отмечено", today["ready_not_picked"],
                  "/panel/tickets?status=ready", "check"),
        _todo_row("Заявок на роль сотрудника", today["requests"],
                  "/panel/access", "access"),
        _todo_row("Писали боту, но без прав", today["no_staff"],
                  "/panel/nostaff", "user-off"),
        _todo_row("Активных кодов сотрудника", today["codes_active"],
                  "/panel/access", "college"),
        _todo_row("Зависших диалогов", today["stuck_states"],
                  "/panel/database", "refresh",
                  extra=_clear_states_form(request)),
    ])
    gaps = [row for row in await repo.data_gaps() if to_int(row["count"])]
    gaps_line = (' · '.join(f'<a href="{esc(row["link"])}">{esc(row["title"])}: '
                             f'<b>{esc(row["count"])}</b></a>' for row in gaps)
                 or "все данные заполнены")
    feed = _feed_list(events) or _empty_state("activity", "Событий пока нет")
    body = f"""
<div class="big-stats">{counters}</div>
<div class="scale-line">{scale}</div>
<div class="card"><h2>Что требует действия сегодня</h2>
<p class="small mut">Что сделать сегодня: строка ведёт в раздел, где это и делается,
а ноль означает «срочного нет». У зависших диалогов есть кнопка очистки.</p>
<ul class="todo">{actions}</ul></div>
<div class="card"><h2>{icon("activity", 20)} Последние события</h2>
<p class="small mut">Кто и что делал с обращениями. <a href="/panel/activity">Вся лента событий</a></p>
{feed}</div>
<div class="card"><h2>{icon("warning", 20)} Пробелы в данных</h2>
<p class="small mut">Пока эти строки не заполнены, части бота работают неполно: {gaps_line}</p></div>"""
    return page("Обзор", body, user, "/",
                actions=f'<a class="btn btn-grey" href="/panel/activity">'
                        f'{icon("activity", 16)} Вся лента событий</a>'
                        + open_in_bot(bot))


def _big_stat(href: str, icon_name: str, value, label: str, kind: str = "",
              text_value: bool = False) -> str:
    """Крупный счётчик: сам кликабельный и ведёт в раздел, где с этим работают.

    Число докручивает CSS (``--to``), а текстовое значение - как есть: так
    счётчик анимируется и не требует JavaScript. При выключенной анимации
    ``prefers-reduced-motion`` показывается конечное число.
    """
    number = (f'<b class="big-txt">{esc(value)}</b>' if text_value
              else f'<b class="count" style="--to:{to_int(value)}"></b>')
    empty = "" if text_value or to_int(value) else " zero"
    return (f'<a class="big-stat{" " + kind if kind else ""}{empty}" href="{esc(href)}" '
            f'aria-label="{esc(label)}: {esc(value)}">'
            f'<span class="big-ico">{icon(icon_name, 18)}</span>{number}'
            f'<span>{esc(label)}</span></a>')


def _todo_row(name: str, number, href: str, icon_name: str, extra: str = "") -> str:
    """Строка «что требует действия»: название-ссылка, число и действие рядом."""
    count = to_int(number)
    return (f'<li><div class="todo-row{" zero" if not count else " hot"}">'
            f'<span class="todo-ico">{icon(icon_name, 18)}</span>'
            f'<a class="todo-name" href="{esc(href)}">{esc(name)}'
            f'{icon("chevron-right", 14)}</a>'
            f'<b class="todo-n">{count}</b>{extra}</div></li>')


def _clear_states_form(request: Request) -> str:
    """Кнопка «Очистить» рядом с зависшими диалогами: та же чистка, что в боте."""
    question = "Очистить зависшие диалоги? Студенты смогут начать заново."
    # подтверждение в браузере - как у остальных опасных кнопок панели
    ask = " onclick=\"return confirm('" + esc(question) + "')\""
    return (f'<form method="post" action="/panel/cleanup/states" class="inline wb-tools"{ask}>'
            f'{csrf(request)}<button class="btn-sm btn-grey">'
            f'{icon("delete", 16)} Очистить</button></form>')


def _empty_state(icon_name: str, text: str) -> str:
    return (f'<div class="empty">{icon(icon_name, 34)}<span>{esc(text)}</span></div>')


# ── лента событий по всем обращениям ─────────────────────────────────────────
ACTIVITY_PAGE = 50        # событий на страницу ленты


ACTIVITY_TYPES = (
    ("", "все события"),
    ("created", "обращение создано"),
    ("status", "смена статуса"),
    ("message_student", "сообщение студента"),
    ("message_staff", "ответ сотрудника"),
    ("ready", "документ готов"),
    ("archive", "в архив"),
    ("restore", "вернулось из архива"),
    ("assign", "назначение сотрудника"),
)


def _event_matches(row, event: str, needle: str) -> bool:
    """Подходит ли событие под фильтры ленты: тип события и текст."""
    if event and as_str(row["event"]) != event:
        return False
    if not needle:
        return True
    haystack = " ".join([as_str(row["student_name"]), as_str(row["student_group"]),
                         as_str(row["topic"]), as_str(row["detail"]),
                         as_str(row["actor_name"]), as_str(row["actor_id"]),
                         str(to_int(row["ticket_id"]))]).lower()
    return needle in haystack


def _feed_list(rows) -> str:
    """Короткая лента для главной страницы: когда, кто и что сделал."""
    if not rows:
        return ""
    items = "".join(
        f'<li data-hk><span class="feed-time">{esc(fmt_when(row["created_at"]))}</span>'
        f'<span class="feed-what">{esc(repo.event_feed_label(row))}'
        + (f' <span class="small mut">— {esc(short(as_str(row["detail"]), 60))}</span>'
           if as_str(row["detail"]) else "")
        + "</span>"
        + copy_btn(panel_link(f"/tickets?t={to_int(row['ticket_id'])}"),
                   f"Ссылка на обращение №{to_int(row['ticket_id'])} скопирована")
        + "</li>"
        for row in rows
    )
    return f'<ul class="feed">{items}</ul>'


@router.get("/activity")
async def activity_page(request: Request, event: str = "", q: str = "", page_no: int = 1):
    """Сквозная лента событий по всем обращениям: фильтр, поиск, постраничный просмотр.

    Источник - ``repo.recent_ticket_events()``: тот же список, что и на главной,
    но с поиском по тексту и постранично. События приходят с конца, поэтому
    фильтр отбирает их уже в прочитанном окне: окно берём на страницу вперёд,
    иначе про следующую страницу узнать нельзя. При редком типе события лента
    заканчивается раньше, и панель честно пишет об этом.
    """
    user = await require_user(request)
    page_no = max(1, to_int(page_no, 1))
    needle = as_str(q).strip().lower()
    window = min(2000, (page_no + 1) * ACTIVITY_PAGE)
    rows = [row for row in await repo.recent_ticket_events(window)
            if _event_matches(row, event, needle)]
    total = len(rows)
    pages = pages_of(total, ACTIVITY_PAGE)
    current = rows[(page_no - 1) * ACTIVITY_PAGE: page_no * ACTIVITY_PAGE]
    bot = await bot_open_link()

    body_rows = "".join(
        f'<tr data-hk><td class="small mut">{esc(fmt_when(row["created_at"]))}</td>'
        f'<td>{esc(as_str(row["actor_name"]) or as_str(row["actor_id"]) or "кто-то")}'
        f'<div class="small mut">{esc(as_str(row["actor_position"]))}</div></td>'
        f'<td><a href="/panel/tickets?t={to_int(row["ticket_id"])}">'
        f'№{to_int(row["ticket_id"])}</a>'
        f'<div class="small mut">{esc(as_str(row["student_name"]) or as_str(row["student_id"]))}</div></td>'
        f'<td>{esc(_event_name(as_str(row["event"])))}</td>'
        f'<td class="small">{esc(short(as_str(row["detail"]), 90) or "—")}'
        f'{copy_btn(panel_link(f"/tickets?t={to_int(row["ticket_id"])}"), "Ссылка скопирована")}</td></tr>'
        for row in current
    ) or "<tr><td colspan='5' class='mut'>Событий не нашлось</td></tr>"
    table = ("<table><tr><th>Когда</th><th>Кто</th><th>Обращение</th><th>Событие</th>"
             f"<th>Деталь</th></tr>{body_rows}</table>")
    # список типов полный, а не «что нашлось на странице»: иначе выбранный
    # фильтр исчезал бы из списка ровно тогда, когда по нему ничего не нашлось
    kind_options = dict(ACTIVITY_TYPES)
    filters = f"""
<form method="get" action="/panel/activity" class="grid" style="margin-bottom:12px">
<div>{select("event", kind_options, event, label="Тип события")}</div>
<div><label>Поиск по тексту: студент, сотрудник, тема, деталь</label>
<input name="q" value="{esc(q)}" placeholder="например: справка"></div>
<div><button>{icon("search", 16)} Найти</button></div></form>"""
    # переход рисует общий pager(): та же разметка, что и на других списках
    pages_bar = pager("/panel/activity", [("event", event), ("q", q)],
                      page_no, pages, f"всего событий: {total}")
    tail = "" if page_no < pages else (
        f'<p class="small mut">Это последняя страница: событий в окне — {total}.</p>')
    body = f"""<div class="card"><h2>{icon("activity", 20)} События по обращениям</h2>
<p class="small mut">Одно обращение - одна лента: создание, ответы, смена статуса, архив.
Свежие сверху, по {ACTIVITY_PAGE} событий на страницу.</p>
{filters}{table}{pages_bar}{tail}</div>"""
    return page("Лента событий", body, user, "/activity",
                actions=open_in_bot(bot))


def _event_name(code: str) -> str:
    """Название типа события по его коду из базы."""
    for known, label in ACTIVITY_TYPES:
        if known == code:
            return label
    return code or "что-то сделал"


@router.post("/cleanup/states")
async def cleanup_states(request: Request):
    """Чистка зависших диалогов: та же кнопка, что в боте, но из панели.

    Состояния старше суток - это брошенные посреди регистрации диалоги: из-за
    них человек не может начать заново. Живые диалоги (свежий created_at)
    остаются нетронутыми.
    """
    actor = await require_form(request)
    removed = await db.prune("user_states", 1)
    log.info("панель: очищены зависшие диалоги - %s (сис-админ %s)", removed, actor)
    await repo.log_action(actor, "очищены зависшие диалоги", f"удалено состояний: {removed}")
    flash(f"Удалено зависших состояний: {removed}.")
    return redirect("/panel/")


def _tickets_table(rows) -> str:
    if not rows:
        return "<p class='mut'>Обращений пока нет.</p>"
    body = "".join(
        f"<tr data-hk><td><a href='/panel/tickets/{esc(row['ticket_id'])}'>№{esc(row['ticket_id'])}</a></td>"
        f"<td>{esc(row['student_id'])}</td><td>{esc(row['target_admin_id'])}</td>"
        f"<td>{esc(plain(STAFF_CATS.get(row['category'], row['category'])))}</td>"
        f"<td>{esc(plain(STATUS.get(row['status'], row['status'])))}</td>"
        f"<td class='small mut'>{esc(row['created_at'])}</td></tr>"
        for row in rows
    )
    return f"<table><tr><th>№</th><th>Студент</th><th>Сотрудник</th><th>Категория</th><th>Статус</th><th>Создано</th></tr>{body}</table>"


# ── аналитика ─────────────────────────────────────────────────────────────────
DASH_PERIODS = (7, 30, 90)


@router.get("/analytics")
async def analytics_page(request: Request, days: int = 30):
    """Диаграммы без внешних библиотек: динамика, статусы, разделы, нагрузка."""
    user = await require_user(request)
    days = days if days in DASH_PERIODS else 30
    per_day = await repo.tickets_by_day(days)
    speed = await repo.response_speed(days)
    load = await repo.staff_load(days)
    by_status = await repo.tickets_by_status()
    by_category = await repo.tickets_by_category()
    groups = await repo.students_by_group()
    status_rows = [{"status": row["status"], "label": STATUS.get(row["status"], row["status"]),
                    "count": row["count"]} for row in by_status]
    category_rows = [{"category": row["category"],
                      "label": STAFF_CATS.get(row["category"], row["category"] or "без раздела"),
                      "count": row["count"]} for row in by_category]
    unanswered = [row for row in by_status if row["status"] in OPEN_STATUSES]
    kpi = f"""
<div class="kpi">
  <div><b>{speed['total']}</b><span>обращений за {days} дн.</span></div>
  <div class="good"><b>{speed['share']}%</b><span>получили ответ</span></div>
  <div><b>{esc(minutes_text(speed['avg_minutes']))}</b><span>среднее время ответа</span></div>
  <div class="warn"><b>{esc(minutes_text(speed['worst_minutes']))}</b><span>худший ответ</span></div>
  <div class="warn"><b>{sum(row['count'] for row in unanswered)}</b><span>сейчас открыто</span></div>
</div>"""
    switcher = "".join(
        f'<a class="btn{"-grey" if d != days else ""}" href="/panel/analytics?days={d}">{d} дн.</a>'
        for d in DASH_PERIODS)
    load_rows = "".join(
        f"<tr><td><b>{esc(row['full_name'])}</b><div class='small mut'>ID {esc(row['user_id'])}</div></td>"
        f"<td>{row['tickets']}</td><td>{row['open']}</td>"
        f"<td>{esc(minutes_text(row['avg_minutes']))}</td>"
        f"<td class='small mut'>{esc(fmt_when(row['last_reply'])) if row['last_reply'] else '—'}</td></tr>"
        for row in load
    ) or "<tr><td class='mut'>Сотрудников пока нет</td></tr>"
    # мини-график имеет смысл только когда есть хотя бы два дня с данными
    spark = charts.sparkline([row["count"] for row in per_day], "Всего обращений за период") \
        if len(per_day) >= 2 else ""
    body = f"""
{kpi}
<div class="card"><h2>Динамика обращений</h2>
<div class="grid" style="margin-bottom:10px">{switcher}</div>
<div class="charts">
  {charts.bar_chart(per_day, 'count', 'day', f'Обращения по дням, {days} дн.', second_key='done')}
  {spark}
  {charts.donut(status_rows, 'count', 'label', 'Статусы обращений')}
  {charts.donut(category_rows, 'count', 'label', 'Разделы обращений')}
  {charts.bars(load, 'tickets', 'full_name', f'Обращения у сотрудников за {days} дн.', color='#8b5cf6')}
  {charts.bars(groups, 'count', 'group', 'Студенты по группам', color='#06b6d4')}
</div></div>
<div class="card"><h2>Нагрузка на сотрудников</h2>
<table><tr><th>Сотрудник</th><th>Обращений</th><th>Открытых</th><th>Средний ответ</th><th>Последний ответ</th></tr>
{load_rows}</table>
<p class="small mut">Среднее время ответа считается по первой ответной реплике сотрудника.
Обращения без ответа в среднее не попадают, но видны в колонке «Открытых».</p></div>"""
    return page("Аналитика", body, user, "/analytics")
