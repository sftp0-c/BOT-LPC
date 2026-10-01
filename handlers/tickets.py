"""Обращения студентов: создание, переписка, статусы, списки."""
from datetime import datetime, timedelta
import re

import clock
import college
import config
import os

import database as db
import repository as repo
from handlers.admin import STAFF_ROLES, audit
from handlers.common import (BACK, admin_of, api, is_personal_template, is_super, log,
                             notify, personal_template_ids, set_personal_template)
from handlers.menus import need_author
from handlers.registry import callback, state
import max_api
from max_api import btn, link_btn
from utils import (
    ACCEPT_ON_REPLY,
    CATS,
    OPEN_STATUSES,
    STAFF_CATS,
    STATUS,
    STATUS_SHORT,
    TOPIC_CATS,
    TOPIC_CATS_BTN,
    as_str,
    cut_plain,
    fmt_when,
    POSITION_LEADERS,
    group_by_position,
    has_position,
    norm_position,
    person_label,
    position_group,
    position_label,
    short,
    short_name,
    to_int,
    topic_title,
)

NEXT_STATUSES = {
    "new": ("accepted", "rejected"),
    "accepted": ("ready", "rejected"),
    "in_progress": ("ready", "rejected"),
    "ready": ("accepted",),
    "rejected": ("accepted",),
    "completed": ("accepted",),
}
READY_HOUR = 18
READY_LEAD = timedelta(hours=1)
READY_MAX = 100
PICKUP_FALLBACK = "кабинет не указан"
OFFICE_REQUIRED = "Сначала укажите кабинет в карточке сотрудника"
LEGACY_COMPLETED_FROM = ("new", "accepted", "in_progress")
CLOSED_STATUSES = ("completed", "rejected")   # дела закрыты: отвечать и закрывать уже нечего
# 16 ячеек, чтобы влезало и в ряд из двух кнопок: «✅ Ответить и закрыть»
# (21 ячейка) обрезалось до «✅ Ответить и…»
CLOSE_BTN = "✅ Ответ и закрыть"             # ответ шаблоном и статус «завершено» сразу
ARCHIVE_VIEW = "archive"        # значение фильтра очереди: архив обращений
MINE_VIEW = "mine"              # значение фильтра очереди: только свои обращения
ARCHIVE_LIMIT = 200             # сколько архивных обращений держим в одном экране
ARCHIVE_BTN = "🗄 В архив"      # закрытое дело - в архив
RESTORE_BTN = "📂 Вернуть из архива"
# Ряд «Назад» + «Меню». Обе подписи влезают в 16 ячеек ряда из двух
# кнопок, поэтому показываются целиком, без многоточия. Это список
# рядов, как BACK: раскладывается в клавиатуру через *.
BACK_TO_CREATE = [[btn("↩️ Назад", "back:ticket_create"), btn("🏠 Меню", "home")]]
BACK_TO_TICKETS = [[btn("↩️ Назад", "back:ticket_menu"), btn("🏠 Меню", "home")]]


def _get(row, key: str, default=None):
    if row is None or not hasattr(row, "keys") or key not in row.keys():
        return default
    return row[key]


def _row_value(row, key: str) -> str:
    return as_str(_get(row, key)).strip()


def _person(person, tail: str = "", prefix: str = "") -> str:
    name = _row_value(person, "full_name")
    if not name:
        return ""
    value = _row_value(person, tail)
    return f"{name} ({prefix}{value})" if value else name


def position_of(person) -> str:
    """Должность сотрудника: свободный текст из карточки, иначе подпись по коду роли."""
    free = _row_value(person, "position")
    if free:
        return norm_position(free)
    code = _row_value(person, "role")
    return STAFF_ROLES.get(code, code)


def role_label(person) -> str:
    return position_of(person)


def staff_pick_label(person) -> str:
    """Подпись кнопки выбора сотрудника - фамилия с инициалами.

    Ни должность, ни полное ФИО в кнопку не влезают: MAX рисует подпись в одну
    строку и обрезает, а «Соколова Мария Сергее…» не отличить ни от кого.
    Инициалы помещаются всегда («Соколова М. С.»), а полное ФИО и должность
    показываются текстом над списком - там места хватает.
    """
    return short_name(_row_value(person, "full_name"), max_api.BUTTON_TEXT)


async def ticket_people(t) -> dict:
    student = _get(t, "student") or _get(t, "sender") or await repo.get_user(t["student_id"])
    staff = _get(t, "staff") or await admin_of(t["target_admin_id"])
    return {**t, "student": student, "staff": staff}


def cat_topic_line(cat: str, topic: str) -> str:
    line = f"[Тип] {CATS.get(cat, cat)}"
    return f"{line}\n[Тема] {as_str(topic).strip()}" if as_str(topic).strip() else line


def office_of(person) -> str:
    for key in ("office", "cabinet", "pickup_place", "room"):
        value = _row_value(person, key)
        if value:
            return value
    return ""


def ready_deadline(kind: str) -> str:
    now = datetime.now()
    if kind == "today":
        day = now.replace(hour=READY_HOUR, minute=0, second=0, microsecond=0)
        if day - now < READY_LEAD:
            day += timedelta(days=1)
    else:
        day = (now + timedelta(days=1)).replace(hour=READY_HOUR, minute=0, second=0, microsecond=0)
    return f"{day:%d.%m.%Y} до {READY_HOUR}:00"


def is_archived(t) -> bool:
    """Обращение убрано в архив (deleted_at проставлен)."""
    return bool(as_str(_get(t, "deleted_at")).strip())


async def load_ticket(x: str, ticket_id: int, include_archived: bool = False):
    """→ (обращение, сторона_сотрудника). (None, False), если обращения нет или доступа нет."""
    t = (await repo.get_ticket(ticket_id) if not include_archived
         else await repo.get_ticket(ticket_id, include_archived=True))
    if not t:
        return None, False
    if as_str(t["student_id"]) == str(x):
        return t, False
    a = await admin_of(x)
    if not a:
        return None, False
    if is_super(a) or as_str(t["target_admin_id"]) == str(x):
        return t, True
    # право «видит все обращения»: очередь та же, что у сис-админа, поэтому и
    # карточка чужого дела открывается - иначе кнопка в очереди вела бы враньё
    return (t, True) if await repo.staff_sees_all(x) else (None, False)


async def may_archive(x: str, t) -> bool:
    """Кто убирает обращение в архив и возвращает его обратно.

    Ответственный за обращение и тот, кому открыта вся очередь (staff_sees_all).
    Студенту архив недоступен: закрытое дело убирает сотрудник, который его вёл,
    иначе архив стал бы кнопкой «спрятать неудобное».
    """
    a = await admin_of(x)
    if not a:
        return False
    if is_super(a) or as_str(_get(t, "target_admin_id")) == str(x):
        return True
    return await repo.staff_sees_all(x)


# Кабинет 115 зашит ТОЛЬКО под справки - так и было сказано приёмной.
# Для остального это не константа: сотрудник выбирает кабинет, иначе «готово»
# отправляло бы студенту «Заберите в кабинете 115», то есть враньё. Та же мысль
# есть в webpanel.py, но код дублируется намеренно: бот не должен зависеть
# от панели, иначе получится круг на импорте.
CERT_PICKUP = "115"
CERT_CATEGORIES = ("certificates", "certificate", "spravka", "справка", "docs", "documents")
CERT_WORDS = ("справк", "справка", "справки")
PICKUP_ASK = "Укажите кабинет выдачи: 115 зашит только под справки"
TAKE_BTN = "✋ Взять"             # взять чужое обращение в работу
PLACE_MAX = 40                   # длиннее «каб. 204» кабинет в колледже не бывает


def is_certificate(t) -> bool:
    """Обращение про справку - и только для них 115 остаётся зашитым.

    Раздел, тема или текст: студент пишет «нужна справка» в любом разделе и
    раздел выбирает наугад, а вопрос про учёбу приходит в раздел «Справка».
    """
    if not t:
        return False
    if _row_value(t, "category").lower() in CERT_CATEGORIES:
        return True
    text = f"{_row_value(t, 'topic')} {_row_value(t, 'text_content')}".lower()
    return any(word in text for word in CERT_WORDS)


def pickup_hint(t) -> str:
    """Подсказка про кабинет: что подставится, если его не указали."""
    if is_certificate(t):
        return f"справка — {CERT_PICKUP}"
    return "кабинет ответственного сотрудника"


def default_place(t) -> str:
    """Кабинет, который подставляется сам. Для не-справок его нет."""
    return CERT_PICKUP if is_certificate(t) else ""


def ready_label(t) -> str:
    """Подпись кнопки «Готово»: известный кабинет видно прямо в кнопке."""
    place = _row_value(t, "pickup_place") or default_place(t)
    return f"✅ Готово · {place}" if place else "✅ Готово"


def ready_needed(t) -> bool:
    """Показывать ли кнопку «Готово» в карточке.

    Закрытое дело (завершено или отклонено) её не получает: там нечего ни
    отвечать, ни переоткрывать. Готовое - только когда кабинет так и не
    указан: документ объявлен готовым, а сказать студенту, где его забрать,
    нечем. Всё остальное кнопку получает.
    """
    status = as_str(_get(t, "status")).strip()
    if status in CLOSED_STATUSES:
        return False
    return status != "ready" or not _row_value(t, "pickup_place")


def room_label(room: str) -> str:
    """Кабинет в кнопку выбора: в ряду из двух помещается 16 ячеек."""
    room = " ".join(as_str(room).split())
    if max_api.display_width(room) > max_api.row_limit(2):
        room = cut_plain(room, max_api.row_limit(2))
    return room or max_api.EMPTY_LABEL


async def pickup_rooms() -> list[str]:
    """Кабинеты для выбора: из карточек сотрудников плюс 115.

    Спрашивать «какой кабинет» без списка бесполезно: сотрудник не помнит, в
    каком кабинете сидит коллега, а набирать номер вручную каждый раз долго.
    """
    rooms = {CERT_PICKUP}
    for row in await repo.list_staff():
        office = office_of(row).strip()
        if office and office != PICKUP_FALLBACK:
            rooms.add(office)
    return sorted(rooms)


def _chunk(buttons: list, per_row: int = 2) -> list:
    """Ряды по per_row кнопок: чем их больше в ряду, тем короче подпись (max_api.row_limit)."""
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def ticket_kb(t, staff_side: bool, can_delete: bool = False, can_archive: bool = False,
              can_take: bool = False):
    """Кнопки карточки обращения.

    can_delete - удалить может только сис-админ; can_archive - убрать в архив
    и вернуть обратно ответственный за обращение и тот, кому открыта вся очередь;
    can_take - взять чужое обращение в работу (сис-админ или право «видит все
    обращения»). Студенту ни того, ни другого: архив закрывает сотрудник, а не
    автор, и чужие дела студенту не видны.
    """
    tid, status = t["ticket_id"], t["status"]
    if not staff_side:
        rows = [[btn("✍️ Написать", f"rp:{tid}")]] if status in OPEN_STATUSES else []
        return [*rows, [btn("↩️ К списку", "tickets")]]
    if is_archived(t):
        # архивное дело закрыто: ответить и сменить статус уже нельзя,
        # остаётся только вернуть его в работу
        rows = [[btn("↩️ К списку", "staff")]]
        if can_archive:
            rows.append([btn(RESTORE_BTN, f"tarch:{tid}")])   # подпись длинная - одна в ряду
        return rows
    rows = [[btn("💬 Ответить", f"rp:{tid}")]]
    if as_str(status) not in CLOSED_STATUSES:
        # «Ответить и закрыть» - отдельным рядом: подпись в 20 символов, а в
        # паре с «Ответить» MAX показывает 16 и обрезал бы её многоточием.
        # На закрытом обращении кнопки нет: закрывать уже нечего.
        rows.append([btn(CLOSE_BTN, f"tplclose:{tid}")])
    # статус в кнопке короткий: в ряду из двух кнопок помещается 16 символов,
    # а «📄 Готово к выдаче» обрезалось бы многоточием (полный - в тексте карточки)
    changes = [btn(STATUS_SHORT[code], f"st:{tid}:{code}") for code in NEXT_STATUSES.get(status, ())]
    rows += [changes[i : i + 2] for i in range(0, len(changes), 2)]
    # по две кнопки в ряду: в ряду из четырёх подписи режутся на телефоне,
    # поэтому дальше этот список нарезается парами
    tail = [btn("↩️ К списку", "staff"), btn("⚡ Шаблоны", f"tpl:{tid}"),
            btn("📝 Заметка", f"note:{tid}"), btn("↪️ Переслать", f"fwd:{tid}")]
    if can_take:
        tail.append(btn(TAKE_BTN, f"ttake:{tid}"))
    # кабинет в кнопке виден сразу: для справки это 115, для остального - тот,
    # что в обращении, а если его нет, сотрудника спросят на следующем экране
    if ready_needed(t):
        tail.append(btn(ready_label(t), f"tdready:{tid}"))
    if can_archive:
        tail.append(btn(ARCHIVE_BTN, f"tarch:{tid}"))
    if can_delete:
        tail.append(btn("🗑 Удалить", f"tdel:{tid}"))
    return [*rows, *_chunk(tail, 2)]


@callback("tdready")
async def cb_ticket_ready(x, arg):
    """«Готово»: закрываем обращение и пишем студенту, где забрать.

    Кабинет 115 зашит только под справки. Кабинет, указанный в обращении,
    уважается всегда, в том числе для справки. А для остального обращения без
    кабинета закрывать молча нельзя: студент получил бы «Заберите в кабинете
    115», то есть не в том месте. Поэтому вместо закрытия спрашиваем кабинет
    (см. ask_pickup_place).
    """
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    place = _row_value(t, "pickup_place") or default_place(t)
    if not place:
        return await ask_pickup_place(x, t)
    return await finish_ready(x, t, place)


async def finish_ready(x: str, t, place: str) -> None:
    """Закрываем обращение как готовое и пишем студенту, где забрать."""
    tid = t["ticket_id"]
    await repo.update_ticket(tid, x, status="ready", pickup_place=place)
    await repo.add_ticket_message(tid, x, "staff",
                                  f"✅ Документ готов. Заберите в кабинете {place}.")
    await notify(t["student_id"],
                 f"✅ Документ по обращению №{tid} готов. Заберите в кабинете {place}.",
                 [[btn("📂 Открыть обращение", f"t:{tid}")]])
    await repo.log_action(x, "документ готов", f"№{tid}, кабинет {place}")
    await send_ticket(x, await repo.get_ticket(tid), True)


async def ask_pickup_place(x: str, t) -> None:
    """Кабинет выдачи не указан: 115 зашит только под справки, дальше - выбор.

    Список кабинетов собирается из карточек сотрудников (pickup_rooms): вопрос
    «какой кабинет?» без списка сотрудник не осилит, а выбор из списка - да.
    """
    tid = t["ticket_id"]
    rooms = await pickup_rooms()
    keyboard = _chunk([btn(room_label(room), f"tdplace:{tid}:{room}") for room in rooms], 2)
    keyboard += [[btn("✏️ Ввести свой", f"tdplacex:{tid}")],
                 [btn("↩️ К обращению", f"t:{tid}")]]
    await api.send(x, f"{PICKUP_ASK}.\n"
                      f"Обращение №{tid} пока не закрыто. Выберите кабинет, где документ "
                      "заберёт студент, — «Готово» его закроет и напишет адрес.",
                   keyboard)


@callback("tdplace")
async def cb_ticket_place(x, arg):
    """Сотрудник выбрал кабинет из списка: обращение закрывается как готовое."""
    tid, _, room = as_str(arg).partition(":")
    return await _place_and_close(x, to_int(tid), room)


@callback("tdplacex")
async def cb_ticket_place_own(x, arg):
    """Кабинет вручную: ждём следующего сообщения."""
    tid = to_int(arg)
    t, staff_side = await load_ticket(x, tid)
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    await db.set_state(x, "ticket_place", {"tid": tid})
    await api.send(x, f"Кабинет выдачи по обращению №{tid}.\n"
                      "Напишите его так, как его называют: «каб. 204», «115». "
                      "Отмена — /cancel.")


@state("ticket_place")
async def st_ticket_place(x, text, p):
    """Кабинет введён вручную: сохраняем его и закрываем обращение."""
    await db.clear_state(x)
    return await _place_and_close(x, to_int(p.get("tid")), text)


async def _place_and_close(x: str, tid: int, room: str) -> None:
    """Общая часть выбора кабинета: из списка или введённого вручную."""
    room = " ".join(as_str(room).split())[:PLACE_MAX]
    t, staff_side = await load_ticket(x, tid)
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    if not room:
        return await api.send(x, "Кабинет не указан — обращение осталось в работе.",
                              [[btn("↩️ К обращению", f"t:{tid}")], *BACK])
    return await finish_ready(x, t, room)


@callback("tdel")
async def cb_ticket_delete(x, arg):
    """Удаление обращения: только сис-админу и с подтверждением."""
    if not is_super(await admin_of(x)):
        return
    t = await repo.get_ticket(to_int(arg))
    if not t:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    student = t.get("student") or {}
    name = student.get("full_name") or f"ID {t['student_id']}"
    await api.send(
        x,
        f"Удалить обращение №{t['ticket_id']} от {name}?\nПереписка и история исчезнут безвозвратно. "
        "Студент получит уведомление.",
        [[btn("🗑 Да, удалить", f"tdely:{t['ticket_id']}"), btn("↩️ К обращению", f"t:{t['ticket_id']}")]],
    )


@callback("tdely")
async def cb_ticket_delete_yes(x, arg):
    if not is_super(await admin_of(x)):
        return
    tid = to_int(arg)
    t = await repo.get_ticket(tid)
    if not t:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    done, message = await repo.delete_ticket(tid)
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("↩️ К списку", "staff")]])
    await notify(t["student_id"],
                 f"🗑 Обращение №{tid} удалено администратором. Если вопрос остался актуальным — "
                 "напишите новое обращение.")
    await repo.log_action(x, "обращение удалено", f"№{tid} (автор {t['student_id']})")
    await audit(x, f"Сис-админ {x} удалил обращение №{tid} от {t['student_id']}.")
    return await api.send(x, f"🗑 {message}", [[btn("↩️ К списку", "staff")]])


@callback("tarch")
async def cb_ticket_archive(x, arg):
    """Сотрудник убирает закрытое обращение в архив и возвращает его в работу.

    Архивирование - обычное дело, а не привилегия сис-админа: раньше закрытые
    дела годами висели в общем списке просто потому, что убрать их было некому.
    """
    tid = to_int(arg)
    t = await repo.get_ticket(tid, include_archived=True)
    if not t:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ В меню", "home")]])
    if not await may_archive(x, t):
        return await api.send(
            x, "🔒 Убрать обращение в архив может только сотрудник, которому оно "
               "назначено или у которого открыт весь список обращений.",
            [[btn("↩️ В меню", "home")]])
    if is_archived(t):
        done, message = await repo.restore_ticket(tid, x)
        if not done:
            return await api.send(x, f"❌ {message}", [[btn("↩️ К обращению", f"t:{tid}")]])
        await repo.log_action(x, "обращение возвращено из архива", f"№{tid}")
        return await api.send(x, f"📂 {message}", [[btn("📬 К очереди", "staff")]])
    done, message = await repo.archive_ticket(tid, x)
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("↩️ К обращению", f"t:{tid}")]])
    await repo.log_action(x, "обращение убрано в архив", f"№{tid} (автор {t['student_id']})")
    # студенту говорим прямо: дело закрыто и лежит в архиве, а не пропало
    await notify(t["student_id"],
                 f"🗄 Обращение №{tid} убрано в архив: сотрудник закрыл его как решённое. "
                 "Переписка сохранена, посмотреть его можно в «🗄 Архив» списка обращений.",
                 [[btn("🗄 Мои архивные", f"tickets:{ARCHIVE_VIEW}")]])
    return await api.send(x, f"🗄 {message}", [[btn("🗄 Архив", f"staff:{ARCHIVE_VIEW}")]])


async def may_take(x: str, t) -> bool:
    """Может ли сотрудник взять это обращение в работу.

    Только тот, кому открыта вся очередь (сис-админ или право «видит все
    обращения»): остальным чужие дела и не видны. Своё обращение брать нечего,
    архивное - нечем.
    """
    if is_archived(t) or as_str(_get(t, "target_admin_id")) == str(x):
        return False
    return is_super(await admin_of(x)) or await repo.staff_sees_all(x)


@callback("ttake")
async def cb_ticket_take(x, arg):
    """Взять чужое обращение в работу: оно переходит в очередь сотрудника.

    Системные права и право «видит все обращения» дают право не только
    посмотреть чужое дело, но и взять его: иначе ответ сис-админа остаётся
    в чужой очереди, а студент не понимает, кто теперь ведёт обращение.
    """
    tid = to_int(arg)
    t, staff_side = await load_ticket(x, tid)
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", [[btn("↩️ К списку", "staff")]])
    if not await may_take(x, t):
        return await api.send(
            x, "🔒 Взять чужое обращение в работу может только тот, кому открыта вся "
               "очередь: сис-админ или сотрудник с правом «видит все обращения».",
            [[btn("↩️ К обращению", f"t:{tid}")], *BACK])
    previous = as_str(_get(t, "target_admin_id"))
    done, message = await repo.update_ticket(tid, x, target_admin_id=x)
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("↩️ К обращению", f"t:{tid}")]])
    await repo.log_action(x, "обращение взято в работу", f"№{tid} (был {previous or '—'})")
    if previous and previous != str(x):
        me = await admin_of(x)
        await notify(
            previous,
            f"↪️ Обращение №{tid} взял в работу {person_label(_row_value(me, 'full_name'), x)}.\n"
            "Автор и переписка те же — отвечать теперь будет он.",
            [[btn("📂 Открыть обращение", f"t:{tid}")]],
        )
    return await send_ticket(x, await repo.get_ticket(tid), True)


MESSAGE_LABEL = {"student": "уточнение студента", "staff": "ответ сотрудника"}
EVENT_LABEL = {"status": "статус", "ready": "документ готов", "created": "создано",
               "message_student": "сообщение студента", "message_staff": "ответ сотрудника",
               "note": "заметка (внутренняя)", "forward": "передано другому сотруднику"}


def message_author(m) -> str:
    """«ФИО, должность» для автора сообщения; роль и группа — в скобках."""
    name = _row_value(m, "sender_name") or ("🎓 Студент" if _row_value(m, "sender_role") == "student" else "🏫 Сотрудник")
    if _row_value(m, "sender_role") == "student":
        group = _row_value(m, "group_code")
        return f"{name} ({group})" if group else name
    position = _row_value(m, "position")
    return f"{name}, {position}" if position else name


def event_text(row) -> str:
    """Строка ленты событий: «сегодня 15:10 — статус: Готово к выдаче»."""
    event = _row_value(row, "event")
    detail = _row_value(row, "detail")
    if event == "status":
        return f"статус: {STATUS.get(detail, detail)}"
    if event == "ready":
        return f"документ готов: {detail}" if detail else "документ готов"
    if event == "note":
        return f"заметка: {detail}" if detail else "заметка"
    if event == "forward":
        return f"передано сотруднику {detail}" if detail else "передано другому сотруднику"
    label = EVENT_LABEL.get(event, event)
    return f"{label}: {detail}" if detail else label


async def ticket_text(t, staff_side: bool) -> str:
    t = await ticket_people(t)
    lines = [f"📂 Обращение №{t['ticket_id']} · {STATUS.get(t['status'], t['status'])}",
             cat_topic_line(t["category"], _row_value(t, "topic"))]
    if is_archived(t):
        # и сотруднику, и студенту говорим правду: дело в архиве, но не удалено
        lines.append("🗄 Обращение в архиве: убрано из очереди, переписка и история сохранены.")
    lines.append(f"Студент: {_person(_get(t, 'student'), 'group_code') or '—'}")
    staff = _get(t, "staff")
    lines.append(f"Ответственный: {_person(staff, 'user_id', 'ID ') or '—'}")
    if position_of(staff):
        lines.append(f"Должность: {position_of(staff)}")
    when = _row_value(t, "ready_until")
    if when:
        place = _row_value(t, "pickup_place") or office_of(staff) or PICKUP_FALLBACK
        lines.append(f"Готово: {when} · {place}")
    if staff_side and not _row_value(t, "pickup_place"):
        # кабинет ещё не выбран: говорим прямо, что будет, если нажать «Готово».
        # Студенту это не нужно - кнопки «Готово» у него нет.
        lines.append(f"Кабинет выдачи: {pickup_hint(t)}")
    msgs = list(reversed(await repo.ticket_thread(t["ticket_id"], 10)))
    lines.append("")
    for index, m in enumerate(msgs):
        role = _row_value(m, "sender_role")
        icon = "🎓" if role == "student" else "🏫"
        label = "автор обращения" if role == "student" and index == 0 else MESSAGE_LABEL[role]
        lines.append(f"{icon} {message_author(m)} · {label} · {fmt_when(m['created_at'])}:")
        lines.append(f"   {short(m['text'], 700)}")
    # заметки и передачи - внутренние: студенту их показывать нельзя
    visible = ("status", "ready", "note", "forward") if staff_side else ("status", "ready")
    history = [e for e in await repo.ticket_events(t["ticket_id"], 20)
               if _row_value(e, "event") in visible]
    if history:
        lines.append("")
        lines.append("📌 " + "; ".join(f"{fmt_when(e['created_at'])} — {event_text(e)}" for e in reversed(history[-4:])))
    return "\n".join(lines)


async def send_ticket(x: str, t, staff_side: bool):
    """Карточка обращения. Кнопку удаления показываем только сис-админу."""
    can_delete = staff_side and is_super(await admin_of(x))
    can_archive = staff_side and await may_archive(x, t)
    can_take = staff_side and await may_take(x, t)
    await api.send(x, await ticket_text(t, staff_side),
                   ticket_kb(t, staff_side, can_delete, can_archive, can_take))


def staff_roster(rows) -> str:
    """Кто принимает обращения: ФИО и должность текстом.

    В кнопке помещается только имя, поэтому должности перечислены сверху -
    так студент видит, к кому пишет, и не получает «Петрова Мария Сергее…».
    """
    lines = [f"· {short(_row_value(r, 'full_name'), 40)}"
             + (f" — {short(role_label(r), 30)}" if role_label(r) else "")
             for r in rows[:12]]
    return "Кто принимает:\n" + "\n".join(lines) if lines else ""


async def ticket_rows_kb(rows, staff_side: bool = False, marks: bool = True):
    """Кнопки списка обращений; помечает те, где последнее слово не за сотрудником.

    Статус в кнопке короткий, а пометка «ждёт» заменяет его совсем: важнее
    сказать, что дело ждёт, чем назвать статус - «№1234 · 🔔 ждёт» и
    «№1234 · 🔧 В работе» помещаются в строку MAX, а «№1234 · 🔧 В работе ·
    🔔 ждёт» обрезался бы многоточием. Полный статус остаётся в тексте карточки.
    """
    latest = await repo.latest_message_roles([row["ticket_id"] for row in rows]) if marks else {}
    buttons = []
    for r in rows:
        mark = ""
        if marks:
            last = latest.get(int(r["ticket_id"]))
            # «🔔 ждёт» показываем сотруднику, «📌 ждёте» - студенту:
            # иначе человек не понимает, кому теперь писать
            if staff_side and last == "student":
                mark = "🔔 ждёт"
            elif not staff_side and last == "staff":
                mark = "📌 ждёте"
        # раздел в кнопку не влезает (MAX обрезает), он и так виден в карточке
        number = f"№{r['ticket_id']} · "
        status = as_str(r["status"])
        label = f"{number}{mark}" if mark else f"{number}{STATUS_SHORT.get(status, status)}"
        buttons.append(btn(label, f"t:{r['ticket_id']}"))
    return [[item] for item in buttons]


# Фильтры очереди сотрудника: сгруппированы по смыслу, а не по алфавиту.
# Четыре входа в очередь вместо восьми фильтров на одном экране: список
# открытых, но непонятных кнопок сотрудник пролистывал, не понимая, что есть.
STAFF_QUEUE_VIEWS = (("waiting", "🔔 Ждут ответа"), ("in_progress", "🔧 В работе"),
                     ("ready", "📄 Готовы"), ("", "🗂 Все"))
# Подпись выбранного вида: виды очереди не совпадают со статусами обращения,
# поэтому «Фильтр: waiting» студенту показывать нельзя.
STAFF_VIEW_LABEL = {"waiting": "🔔 Ждут ответа", "in_progress": "🔧 В работе",
                    "ready": "📄 Готовы", "": "🗂 Все", "open": "🔓 Открытые",
                    "new": "🆕 Без ответа", "completed": "✅ Завершённые",
                    MINE_VIEW: "📥 Только мои"}
STAFF_QUEUE_FILTERS = (("open", "🔓 Открытые"), ("new", "🆕 Без ответа"),
                       ("ready", "📄 К выдаче"), ("completed", "✅ Завершённые"))


async def archive_rows(x: str, a) -> list:
    """Архивные обращения, которые сотрудник может открыть и вернуть в работу.

    admin_tickets(archived=True) отдаёт архив целиком, поэтому лишнее отсекаем
    сами: весь архив видят системные права и те, кому открыт весь список
    обращений (как в may_archive), остальные - только свои дела.
    Кнопка «📂 Вернуть из архива» в чужом деле всё равно была бы враньём:
    карточку чужого обращения сотрудник не открывает.
    """
    rows = list(await repo.admin_tickets(None, ARCHIVE_LIMIT, archived=True))
    if is_super(a) or await repo.staff_sees_all(x):
        return rows
    return [row for row in rows if as_str(_get(row, "target_admin_id")) == str(x)]


async def send_staff_archive(x: str, a) -> None:
    """Экран архива: закрытые дела убраны из очереди, но их можно вернуть.

    Отдельный экран обязателен: из очереди архивное обращение уже не открыть,
    и без этого кнопка «📂 Вернуть из архива» ни к чему не вела бы.
    """
    rows = await archive_rows(x, a)
    lines = [f"🗄 Архив обращений — {len(rows)}",
             "Закрытые дела: они убраны из очереди, но переписка и история сохранены.",
             "Вернуть обращение в работу можно кнопкой на его карточке."]
    keyboard = await ticket_rows_kb(rows[:15], True, marks=False) if rows else \
        [[btn("🗂 Архив пуст", "noop")]]
    keyboard += [[btn("📬 К очереди", "staff"), btn("🔄 Обновить", f"staff:{ARCHIVE_VIEW}")], *BACK]
    await api.send(x, "\n".join(lines), keyboard)


async def send_staff_queue(x: str, view: str = "") -> None:
    """Очередь сотрудника: счётчики, фильтры и список.

    Фильтр передаётся в кнопке, а не хранится в состоянии: так он не слетает
    при возврате из карточки и не зависит от того, какую кнопку нажали раньше.
    """
    a = await admin_of(x)
    if not a:
        return await api.send(x, "Сотрудник не найден.", BACK)
    # Системные права видят всё; остальным достаточно выданного права:
    # scope=None в admin_tickets отдаёт всю очередь, scope=x - только свои.
    # Право «видит все обращения» проверяется и в load_ticket: очередь, в
    # которой нельзя открыть карточку, - это кнопка, ведущая в «не найдено».
    super_view = is_super(a)
    sees_all = super_view or await repo.staff_sees_all(x)
    scope = None if sees_all else x
    if view == ARCHIVE_VIEW:
        return await send_staff_archive(x, a)
    mine = view == MINE_VIEW
    arch_n = len(await archive_rows(x, a))
    all_rows = await repo.admin_tickets(scope)
    counts = await repo.status_counts(scope)
    if view == "__waiting__":
        latest = await repo.latest_message_roles([row["ticket_id"] for row in all_rows])
        rows = [row for row in all_rows if latest.get(int(row["ticket_id"])) == "student"]
        view = "waiting"
    elif mine:
        # «Только мои»: право видеть все обращения не значит права отвечать за всех
        rows = [row for row in all_rows if as_str(row["target_admin_id"]) == str(x)]
    elif view == "open":
        rows = [row for row in all_rows if row["status"] in OPEN_STATUSES]
    elif view in dict(STAFF_QUEUE_FILTERS):
        rows = [row for row in all_rows if as_str(row["status"]) == view]
    elif view in CATS:
        rows = [row for row in all_rows if as_str(row["category"]) == view]
    else:
        rows = all_rows
    lines = ["📬 Очередь обращений"]
    lines.append(" · ".join(f"{STATUS[code]} — {counts.get(code, 0)}"
                            for code in ("new", "accepted", "in_progress", "ready", "completed")))
    # кто что видит - говорим прямо: молчаливый чужой список пугает людей
    lines.append("👁 Все обращения: у вас системные права" if super_view else
                 "👁 Все обращения: право выдано сис-админом" if sees_all else
                 "👁 Только обращения, назначенные вам")
    if mine:
        lines.append(f"\nФильтр: {STAFF_VIEW_LABEL[MINE_VIEW]} — {len(rows)}")
    elif view:
        title = STAFF_VIEW_LABEL.get(view) or CATS.get(view) or dict(STAFF_QUEUE_FILTERS).get(view)
        lines.append(f"\nФильтр: {title or view} — {len(rows)}")
    else:
        lines.append("Фильтр не выбран — показаны все обращения")
    latest_roles = await repo.latest_message_roles([row["ticket_id"] for row in all_rows])
    views = []
    for code, label in STAFF_QUEUE_VIEWS:
        count = (sum(1 for row in all_rows
                     if latest_roles.get(int(row["ticket_id"])) == "student")
                 if code == "waiting"
                 else sum(1 for row in all_rows
                          if not code or as_str(row["status"]) == code))
        views.append(btn(f"{'▸ ' if view == code else ''}{label} {count}", f"staffv:{code}"))
    # счётчики - по одной кнопке в ряду: в паре помещается 16 символов, а
    # «🔔 Ждут ответа 1234» в неё не влезает и обрезалось бы многоточием
    keyboard = [[item] for item in views]
    keyboard.append([btn(f"🗄 Архив: {arch_n}", f"staff:{ARCHIVE_VIEW}")])
    keyboard.append([btn("👥 По отделам", "staffcat"), btn("🔄 Обновить", f"staff:{view}")])
    if sees_all:
        # право «видит все» без переключателя превращает чужую очередь в
        # ежедневную: вернуться к своим делам можно одним нажатием
        keyboard.append([btn("👁 Все обращения" if mine else "📥 Только мои",
                             f"staff:{'' if mine else MINE_VIEW}")])
    if view:
        keyboard.append([btn("Сбросить фильтр", "staff:")])
    if rows:
        keyboard += await ticket_rows_kb(rows[:15], True)
    else:
        keyboard.append([btn("🔍 Ничего не найдено", "noop")])
    await api.send(x, "\n".join(lines), [*keyboard, *BACK])


# ── свой раздел сотрудника ───────────────────────────────────────────────────
SECTION_TEMPLATES = 4        # шаблонов на экране раздела: остальные - из карточки
NO_POSITION_HELP = (
    "⚠️ Должность не заполнена, поэтому показать свой раздел нечего.\n"
    "Без неё бот не знает, к чему вас привязать: не будет ни счётчиков, ни "
    "шаблонов, ни подсказки кабинета, а в подменю «Обратная связь» вас не будет.\n"
    "Попросите сис-админа заполнить её: панель → «Сотрудники» → ваша строка → должность."
)


@callback("mysection")
async def cb_staff_section(x, arg):
    """Свой раздел: что не сделано, чем отвечать, где кабинет и кто ещё в должности.

    Экран собран из того, чего больше нигде нет: в очереди есть счётчики, но там
    нет кабинета, коллег по должности и шаблонов раздела. Поэтому он не
    повторяет очередь, а показывает своё рабочее место.
    """
    a = await admin_of(x)
    if not a:
        return              # не сотрудник: молча, как и кнопка «Обращения»
    if not has_position(_row_value(a, "position")):
        return await api.send(x, NO_POSITION_HELP,
                              [[btn("📬 Обращения", "staff")], [btn("🏠 Меню", "home")]])
    category = _row_value(a, "ticket_category") or "all"
    counts = await repo.status_counts(x)
    code = position_group(_row_value(a, "position"), _row_value(a, "role"))
    office = office_of(a)
    department = _row_value(a, "department")
    lines = [f"🗂 Ваш раздел: {position_of(a)}"]
    if department:
        lines.append(f"Отдел: {department}")
    # кабинет - то, что чаще всего ищут и что больше нигде не показано
    lines.append(f"Кабинет: {office}" if office else "Кабинет: не указан — попросите сис-админа")
    lines.append(f"Обращения по разделу: {STAFF_CATS.get(category, category)}")
    lines.append("")
    lines.append("Очередь: " + " · ".join(f"{STATUS[code]} — {counts.get(code, 0)}"
                                         for code in OPEN_STATUSES))
    colleagues = [row for row in group_by_position(await repo.list_staff()).get(code, [])
                  if as_str(_row_value(row, "user_id")) != str(x)]
    if colleagues:
        lines.append("")
        lines.append("👥 В этой должности ещё работают:")
        for row in colleagues[:6]:
            room = office_of(row)
            lines.append(f"· {short(as_str(_row_value(row, 'full_name')), 30)}"
                         + (f" — каб. {room}" if room else " — кабинет не указан"))
    else:
        lines.append("👥 В этой должности вы один - обращения по разделу ваши.")
    templates = await repo.list_templates("" if category == "all" else category, limit=50)
    if templates:
        lines.append("")
        lines.append(f"⚡ Шаблоны раздела ({len(templates)}):")
        lines += [f"· {short(as_str(t['title']), 40)}" for t in templates[:SECTION_TEMPLATES]]
        if len(templates) > SECTION_TEMPLATES:
            lines.append("· остальные - из карточки обращения")
    keyboard = [[btn(f"⚡ {cut_plain(as_str(t['title']), max_api.BUTTON_TEXT - 2)}", f"mystpl:{t['id']}")]
                for t in templates[:SECTION_TEMPLATES]]
    if category in CATS:
        keyboard.append([btn("📬 Очередь раздела", f"stafff:{category}")])
    keyboard.append([btn("📬 Все обращения", "staff"), btn("📊 Статистика", "staffstats")])
    keyboard.append([btn("🏠 Меню", "home")])
    await api.send(x, "\n".join(lines), keyboard)


@callback("mystpl")
async def cb_section_template(x, arg):
    """Шаблон из своего раздела: показать текст и напомнить, куда его вставить."""
    a = await admin_of(x)
    if not a:
        return
    template = await repo.get_template(to_int(as_str(arg)))
    if not template:
        return await api.send(x, "Шаблон удалён.", [[btn("↩️ В раздел", "mysection")], *BACK])
    raw = as_str(template["text"])
    await api.send(
        x,
        f"⚡ {short(as_str(template['title']), 60)}\n\n{short(raw, 1200)}\n\n"
        "Текст подставляется в ответ прямо из карточки обращения: «Ответить» → "
        "«Шаблоны». Подстановки вида {ФИО} бот заменяет сам.",
        [[btn("📬 К обращениям", "staff")], [btn("↩️ В раздел", "mysection")], *BACK],
    )


@callback("staffv")
async def cb_staff_view(x, arg):
    """Очередь по подменю: ждут ответа, в работе, готовы или все."""
    return await send_staff_queue(x, "__waiting__" if as_str(arg).strip() == "waiting"
                                  else as_str(arg).strip())


@callback("staffcat")
async def cb_staff_categories(x, arg):
    """Отделы отдельным подменю, иначе очередь перегружена кнопками."""
    keyboard = [[btn(label, f"stafff:{code}")] for code, label in CATS.items()]
    keyboard += [[btn("🔓 Все открытые", "stafff:open")], [btn("📥 В меню", "staff")]]
    await api.send(x, "👥 Очередь по отделам:", keyboard)


@callback("stafff")
async def cb_staff_filter(x, arg):
    """Фильтр очереди: открытые, по статусу или по разделу."""
    if not await admin_of(x):
        return
    return await send_staff_queue(x, as_str(arg))


@callback("snew")
async def cb_new_ticket_start(x, arg):
    """Создание обращения из меню сис-админа: сначала раздел, дальше — как у студента."""
    if not is_super(await admin_of(x)):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    await api.send(
        x,
        "✍️ Новое обращение. Выберите раздел — дальше сотрудника и текст:",
        [[btn(label, f"new:{code}")] for code, label in CATS.items()] + BACK,
    )


# Подменю меню студента. Ключ - код категории, значение - конкретные вопросы:
# студент выбирает суть, а не «категорию вообще».
SUBMENU_TOPICS = {
    "cert": ("certificates", (
        ("place", "📍 Место обучения"),
        ("period", "🗓 Период обучения"),
        ("vacancies", "🎓 Вакантные места"),
    )),
    "acc": ("accounting", (
        ("scholarship", "💰 Стипендия"),
        ("payout", "🧾 Выплаты"),
        ("other", "❓ Другой вопрос"),
    )),
}



@callback("sub")
async def cb_submenu(x, arg):
    """Подменю меню: справки, бухгалтерия или адресаты обратной связи."""
    key = as_str(arg).strip()
    if key == "fb":
        return await _feedback_menu(x)
    if key not in SUBMENU_TOPICS:
        return await api.send(x, "Раздел не найден.", [[btn("🏠 Меню", "home")]])
    category, items = SUBMENU_TOPICS[key]
    keyboard = [[btn(label, f"ask:{category}:{code}")] for code, label in items]
    keyboard += [[btn("👥 Другой сотрудник", f"new:{category}")],
                 *BACK_TO_CREATE]
    await api.send(x, f"{CATS[category]}\nВыберите, что именно:", keyboard)


async def _feedback_menu(x: str) -> None:
    """Обратная связь адресная: сначала должность, потом человек этой должности.

    Кнопка — это должность, а фамилии видно текстом: в кнопке «должность —
    фамилия» MAX обрезал фамилию многоточием.

    Список должностей больше не зашит в коде: он собирается из сотрудников по
    справочнику (utils.POSITIONS), поэтому кнопка появляется у той должности,
    которую реально назначили, а «ПК» и «Приёмная комиссия», написанные по-
    разному, попадают в одно подменю, а не в два.
    """
    groups = group_by_position(await repo.list_staff())
    tail = [[btn("👥 Другой сотрудник", "new:feedback")],
            [btn("⚠️ Ошибка в боте", "bugreport")],
            *BACK_TO_CREATE]
    if not groups:
        await api.send(
            x, "👤 Обратная связь\n\nДолжности ещё не назначены в системе — напишите "
                "любому сотруднику из общего списка.",
            [[btn("👥 Выбрать", "new:feedback")], *tail])
        return
    # Руководящих назначают первыми: к ним пишут адресно, и их кнопка не должна
    # уезжать вниз экрана. Остальные - по алфавиту, чтобы порядок не прыгал.
    order = [code for code in POSITION_LEADERS if code in groups]
    order += sorted(code for code in groups if code not in POSITION_LEADERS)
    lines = "\n".join(f"· {position_label(code)}: " + ", ".join(
        short(as_str(_row_value(person, "full_name")), 30)
        for person in groups[code]) for code in order)
    keyboard = [[btn(position_label(code), f"fbrole:{code}")] for code in order]
    keyboard += tail
    # Руководящих нет - говорим об этом прямо, но должности других не прячем:
    # назначить директора и секретаря можно одним приказом.
    hint = "" if any(code in POSITION_LEADERS for code in groups) else (
        "\n\nРуководящие должности ещё не назначены — напишите любому сотруднику "
        "из общего списка.")
    await api.send(x, f"👤 Обратная связь — кому пишете?\n{lines}{hint}", keyboard)


@callback("fbrole")
async def cb_feedback_role(x, arg):
    """Люди одной должности: выбираем конкретного."""
    code = as_str(arg).strip()
    title = position_label(code)
    people = [dict(row) for row in group_by_position(await repo.list_staff()).get(code, [])]
    if not people:
        return await api.send(x, f"{title}: сотрудник не назначен — напишите через общий список.",
                              [[btn("👥 Выбрать", "new:feedback")], *BACK])
    keyboard = [[btn(staff_pick_label(p), f"pick:feedback:{as_str(p.get('user_id'))}")]
                for p in people]
    keyboard += [[btn("👥 Другой сотрудник", "new:feedback")], *BACK]
    who = "\n".join(f"· {short(as_str(p.get('full_name')), 34)}"
                    + (f" — {short(position_of(p), 24)}" if position_of(p) else "")
                    for p in people)
    return await api.send(x, f"{title}\n{who}", keyboard)


@callback("ask")
async def cb_ask(x, arg):
    """Конкретный вопрос из подменю: сразу с темой и выбором сотрудника."""
    category, _, code = as_str(arg).partition(":")
    title = ""
    for cat, items in SUBMENU_TOPICS.values():
        for key, label in items:
            if cat == category and key == code:
                title = label
    if not title or not await need_author(x):
        return await api.send(x, "Вопрос не найден.", [[btn("🏠 Меню", "home")]])
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён.", BACK)
    await db.set_state(x, "ticket", {"cat": category, "topic": title})
    rows = await repo.staff_for_category(category)
    if not rows:
        return await api.send(x, "Сотрудник по этому вопросу ещё не назначен — напишите через "
                                 "общий список.",
                              [[btn("👥 Выбрать", f"new:{category}")],
                               [btn("🏠 Меню", "home")]])
    keyboard = [[btn(staff_pick_label(r), f"pick:{category}:{r['user_id']}:{code}")]
                for r in rows]
    await api.send(x, f"{title}\n{staff_roster(rows)}\nКому пишете?",
                   keyboard + [[btn("🏠 Меню", "home")]])


# Категории для кнопок меню: студент сразу выбирает, что его волнует,
# и не разбирается в общем «новом обращении» с двумя шагами выбора.
MENU_TICKETS = (("certificates", "📄 Справки"), ("academic", "🎓 Учёба"),
                ("accounting", "💰 Стипендия"), ("feedback", "💬 Другое"))


@callback("new")
async def cb_new_ticket(x, cat):
    if cat not in CATS or not await need_author(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    if cat in TOPIC_CATS:
        # в кнопке - короткая подпись (предел 20 ячеек), в заголовке и в тексте
        # обращения - полная, она и объясняет, о чём тема
        return await api.send(x, f"{CATS[cat]}\nВыберите тему обращения:",
                              [[btn(label, f"topic:{cat}:{code}")]
                               for code, label in TOPIC_CATS_BTN[cat].items()] + BACK)
    rows = await repo.staff_for_category(cat)
    if not rows:
        return await api.send(x, "Сотрудники для этого раздела пока не назначены. Обратитесь в учебную часть.", BACK)
    kb = [[btn(staff_pick_label(r), f"pick:{cat}:{r['user_id']}")] for r in rows]
    await api.send(x, f"{CATS[cat]}\n{staff_roster(rows)}\nВыберите сотрудника:", kb + BACK)


@callback("topic")
async def cb_ticket_topic(x, arg):
    cat, _, code = arg.partition(":")
    title = topic_title(cat, code)
    if not title or not await need_author(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    rows = await repo.staff_for_category(cat)
    if not rows:
        return await api.send(x, "Сотрудники для этого раздела пока не назначены. Обратитесь в учебную часть.", BACK)
    await db.set_state(x, "ticket", {"cat": cat, "topic": title})
    kb = [[btn(staff_pick_label(r), f"pick:{cat}:{r['user_id']}:{code}")] for r in rows]
    await api.send(x, f"{CATS[cat]} · {title}\nВыберите сотрудника:", kb + BACK)


def submenu_topic(cat: str, code: str) -> str:
    """Тема вопроса из подменю меню («Справка с места обучения» и подобные)."""
    if not code:
        return ""
    for category, items in SUBMENU_TOPICS.values():
        if category != cat:
            continue
        for key, label in items:
            if key == code:
                return label
    return ""


async def _pending_topic(x: str, cat: str, code: str) -> str:
    topic = topic_title(cat, code) or submenu_topic(cat, code)
    if topic:
        return topic
    st = await db.get_state(x)
    payload = st["payload"] if st and st["state"] == "ticket" else {}
    return as_str(payload.get("topic", "")).strip() if payload.get("cat") == cat else ""


@callback("pick")
async def cb_pick_staff(x, arg):
    cat, _, rest = arg.partition(":")
    admin_id, _, code = rest.partition(":")
    if cat not in CATS or not await need_author(x):
        return
    a = await admin_of(admin_id)
    if not a or is_super(a) or a["ticket_category"] not in (cat, "all"):
        return await api.send(x, "Этот сотрудник больше не принимает такие обращения. Выберите другого.", BACK)
    payload = {"admin": admin_id, "cat": cat, "topic": await _pending_topic(x, cat, code)}
    replacement = await repo.vacation_replacement(a)
    away = bool(replacement) and await repo.on_vacation(admin_id)
    if away:
        # молча менять адресата нельзя: человек должен знать, кто ответит
        payload["admin"] = replacement["user_id"]
        payload["vacation"] = 1
        payload["was"] = admin_id
    await db.set_state(x, "ticket", payload)
    if away:
        # молча менять адресата нельзя: человек должен знать, кто ответит
        head = f"Кому: {replacement['full_name']} (за {a['full_name']})"
        foot = "\n\n" + await repo.vacation_note(a)
    else:
        head, foot = f"Кому: {a['full_name']}", ""
    await api.send(x, f"{head}{foot}\nНапишите обращение одним сообщением (или /cancel для отмены).")


@state("ticket")
async def st_ticket(x, text, p):
    """Сценарий обращения: сообщения копятся в черновике, создаёт обращение кнопка."""
    user = await need_author(x)
    if not user:
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        await db.clear_state(x)
        return await api.send(x, "Приём обращений временно отключён.", BACK)
    admin_id = as_str(p.get("admin"))
    if not admin_id:
        return await _ask_draft(x, p, text)     # сотрудника выберут позже или сейчас
    admin = await admin_of(admin_id)
    if not admin:
        await db.clear_state(x)
        return await api.send(x, "Сотрудник больше недоступен. Начните заново.", BACK)
    draft = as_str(p.get("draft")).strip()
    if draft:
        text = f"{draft}\n\n{text}"          # второе и третье сообщение дописывают
    return await _ask_draft(x, p, text[:3000])


async def _send_draft(x: str, payload) -> None:
    """Создаёт обращение из черновика и уведомляет сотрудника."""
    user = await need_author(x)
    if not user:
        return
    text = as_str(payload.get("draft")).strip()[:3000]
    admin_id = as_str(payload.get("admin"))
    if not text or not admin_id:
        return await api.send(x, "Черновик пуст — напишите текст обращения заново.", BACK)
    topic = " ".join(as_str(payload.get("topic", "")).split())[:READY_MAX]
    tid = await repo.create_ticket(x, admin_id, payload["cat"], text, topic=topic)
    await db.clear_state(x)
    t = await repo.get_ticket(tid)
    delivered = await notify(
        admin_id,
        f"🔔 Новое обращение №{tid}\n{cat_topic_line(payload['cat'], topic)}\n"
        f"От: {user['full_name']} ({user['group_code']})\n\n{text}",
        ticket_kb(t, True),
    )
    lines = [f"✅ Обращение №{tid} отправлено."]
    if as_str(payload.get("vacation")) == "1" and as_str(payload.get("was")):
        lines.append("Ответит заместитель: сотрудник сейчас в отпуске.")
    if topic:
        lines.append(f"Тема: {topic}")
    lines += ["", short(text, 700)]
    if not delivered:
        lines.append("\n⚠️ Сотрудник пока не запускал бота — уведомление не дошло, но обращение сохранено.")
    # attach_kb возвращает список рядов: вкладывать его в клавиатуру нельзя,
    # иначе MAX получает кнопки списком внутри ряда и не рисует их
    await api.send(x, "\n".join(lines),
                   [*attach_kb(tid), [btn("↩️ В меню", "home")]])


def draft_keyboard(x: str) -> list:
    """Кнопки черновика: отправить, дочистить, выйти в меню.

    По одной в ряду: в двух кнопках под длинную подпись места нет (16 символов).
    """
    return [[btn("✉️ Отправить", "ticketsend")],
            [btn("🗑 Очистить черновик", "draftclr")],
            [btn("↩️ В меню", "home")]]


async def _ask_draft(x: str, payload, text: str) -> None:
    """Черновик: первое сообщение сохранено, обращение ещё не создано."""
    await db.set_state(x, "ticket", {**payload, "draft": text})
    count = len([line for line in text.splitlines() if line.strip()])
    await api.send(x, "✍️ Сообщение сохранено"
                      + (f" ({count} шт. в тексте)" if count > 1 else "")
                      + ".\n\nМожно дописать ещё сообщение или отправить обращение сейчас.",
                  draft_keyboard(x))


ATTACH_MAX = 3       # больше трёх файлов к одному обращению не нужно


def attach_kb(tid: int) -> list:
    return [[btn("📎 Прикрепить файл", f"tattach:{tid}")],
            [btn("📂 Открыть обращение", f"t:{tid}")],
            [btn("↩️ В меню", "home")]]


@callback("tattach")
async def cb_ticket_attach(x, arg):
    """Предложение прикрепить файл: ждём следующего вложения."""
    tid = to_int(arg)
    t = await repo.get_ticket(tid)
    if not t or as_str(t["student_id"]) != str(x):
        return await api.send(x, "Обращение не найдено.", BACK)
    await db.set_state(x, "attach_file", {"tid": tid})
    await api.send(x, "📎 Пришлите файл (фото или документ) — он прикрепится к обращению.\n"
                      "Жду сообщение с файлом. Отмена — /cancel или «↩️ В меню».", attach_kb(tid))


@state("attach_file")
async def st_attach_file(x, text, p):
    """Обычный текст вместо файла: подсказываем, что ждём именно файл."""
    tid = to_int(p.get("tid"))
    return await api.send(x, "Нужен файл: пришлите фото или документ сообщением.\n"
                             "Если передумали — /cancel.", attach_kb(tid))


async def on_attachment(x: str, files: list) -> bool:
    """Файл пришёл в личный диалог. True - событие обработано, больше не отвечать."""
    item = (files or [{}])[0]
    st = await db.get_state(x)
    if not st or st["state"] != "attach_file":
        await api.send(x, "Принял файл. Прикрепить файл к обращению можно кнопкой "
                          "«📎 Прикрепить файл» в самом обращении.", BACK)
        return True
    tid = to_int(st["payload"].get("tid"))
    t = await repo.get_ticket(tid)
    if not t:
        await db.clear_state(x)
        return await api.send(x, "Обращение не найдено — файл не прикреплён.", BACK) or True
    existing = [name for name in await attached_names(tid)]
    if len(existing) >= ATTACH_MAX:
        await db.clear_state(x)
        return await api.send(x, f"К обращению уже прикреплено {ATTACH_MAX} файла.", attach_kb(tid)) or True
    try:
        from attachments import download_attachment

        path, name = await download_attachment(item.get("url", ""), item.get("name", ""))
    except Exception as exc:  # noqa: BLE001 - студенту нужна причина, а не трассировка
        log.warning("не удалось сохранить вложение %s: %s", item.get("name"), exc)
        return await api.send(x, f"⚠️ Файл не сохранился: {exc}\nПришлите другой или напишите текстом.",
                              attach_kb(tid)) or True
    size = item.get("size") or os.path.getsize(path)
    line = f"📎 Файл: {name} ({size // 1024} КБ)"
    await repo.add_ticket_message(tid, x, "student", line)
    await notify(t["target_admin_id"], f"📎 Файл к обращению №{tid}: {name}", ticket_kb(t, True))
    await db.clear_state(x)
    await api.send(x, f"✅ Файл прикреплён к обращению №{tid}.", attach_kb(tid))
    return True


async def attached_names(ticket_id: int) -> list[str]:
    """Имена файлов, прикреплённых к обращению: по строкам «📎 Файл: …»."""
    names = []
    for row in await repo.ticket_thread(ticket_id, 100):
        text = as_str(row["text"])
        if text.startswith("📎 Файл: "):
            names.append(text[len("📎 Файл: "):].split(" (")[0])
    return names


@callback("ticketsend")
async def cb_ticket_send(x, arg):
    """Отправка черновика: последний шанс передумать."""
    st = await db.get_state(x)
    if not st or st["state"] != "ticket":
        return await api.send(x, "Черновик пуст — напишите текст обращения заново.", BACK)
    payload = st["payload"]
    if not as_str(payload.get("draft")).strip():
        return await api.send(x, "Черновик пуст — напишите текст обращения заново.", BACK)
    return await _send_draft(x, payload)


@callback("draftclr")
async def cb_draft_clear(x, arg):
    """Очистить черновик и начать заново."""
    st = await db.get_state(x)
    if st and st["state"] == "ticket":
        await db.set_state(x, "ticket", {k: v for k, v in st["payload"].items() if k != "draft"})
        return await api.send(x, "🗑 Черновик очищен. Напишите обращение заново.",
                              draft_keyboard(x))
    return await api.send(x, "Активных черновиков нет.", BACK)


@callback("tickets")
async def cb_my_tickets(x, arg):
    if not await need_author(x):
        return
    rows = await repo.recent_student_tickets(x)
    if not rows:
        return await api.send(
            x, "У вас пока нет обращений.",
            BACK_TO_TICKETS)
    arch_n = sum(1 for row in rows if is_archived(row))
    if as_str(arg).strip() == ARCHIVE_VIEW:
        rows = [row for row in rows if is_archived(row)]
        title = "🗄 Архив ваших обращений — они закрыты, переписка и история сохранены."
        switch = btn("📋 Все обращения", "tickets")
    else:
        title = ("📋 Мои обращения (последние 15). "
                 "Нажмите на обращение, чтобы открыть переписку:")
        switch = btn(f"🗄 Архив: {arch_n}", f"tickets:{ARCHIVE_VIEW}")
    # «Назад» возвращает на экран «Обращения», а «Меню» остаётся в
    # главном меню: из карточки студент выходит сюда «Мои обращения».
    await api.send(x, title,
                   [*await ticket_rows_kb(rows), [switch], *BACK_TO_TICKETS])


@callback("staff")
async def cb_staff_tickets(x, arg):
    """Очередь сотрудника: из меню - без фильтра, с кнопки «Обновить» - с тем же."""
    if not await admin_of(x):
        return
    return await send_staff_queue(x, as_str(arg))


@callback("t")
async def cb_open_ticket(x, arg):
    # архивное обращение тоже должно открываться: из архива приходят по t:<id>
    t, staff_side = await load_ticket(x, to_int(arg), include_archived=True)
    if not t:
        return await api.send(x, "Обращение не найдено.", BACK)
    await send_ticket(x, t, staff_side)


@callback("rp")
async def cb_reply(x, arg):
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t:
        return await api.send(x, "Обращение не найдено.", BACK)
    if not staff_side and t["status"] not in OPEN_STATUSES:
        return await api.send(x, "Обращение закрыто. Создайте новое через меню.", BACK)
    await db.set_state(x, "reply", {"tid": t["ticket_id"]})
    await api.send(x, f"Введите сообщение по обращению №{t['ticket_id']} (или /cancel).")


@callback("note")
async def cb_internal_note(x, arg):
    """Внутренняя заметка: её видит команда, студент - нет."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Заметки доступны сотруднику по его обращению.", BACK)
    await db.set_state(x, "ticket_note", {"tid": t["ticket_id"]})
    await api.send(x, f"Заметка по обращению №{t['ticket_id']} (её увидит только команда).\n"
                      "Напишите текст или /cancel.")


@state("ticket_note")
async def st_internal_note(x, text, p):
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or not staff_side:
        await db.clear_state(x)
        return await api.send(x, "Обращение недоступно.", BACK)
    note = as_str(text).strip()[:200]
    await db.clear_state(x)
    if not note:
        return await api.send(x, "Пустую заметку не сохраняем.", [[btn("↩️ К обращению", f"t:{t['ticket_id']}")]])
    await repo.add_internal_note(t["ticket_id"], x, note)
    await send_ticket(x, t, True)


@callback("fwd")
async def cb_forward(x, arg):
    """Передача обращения другому сотруднику: пригодится, когда вопрос не его."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    current = as_str(t["target_admin_id"])
    rows = [row for row in await repo.staff_for_category(as_str(t["category"]), limit=50)
            if as_str(row["user_id"]) != current]
    if not rows:
        return await api.send(
            x, "Передавать некому: в этом разделе других сотрудников нет.",
            [[btn("↩️ К обращению", f"t:{t['ticket_id']}")]])
    keyboard = [[btn(staff_pick_label(row), f"fwdto:{t['ticket_id']}:{row['user_id']}")]
                for row in rows[:10]]
    await api.send(x, "Кому передать обращение? Автор и переписка останутся прежними.\n" + staff_roster(rows),
                   [*keyboard, [btn("↩️ К обращению", f"t:{t['ticket_id']}")]])


@callback("fwdto")
async def cb_forward_to(x, arg):
    """Подтверждение передачи и уведомление нового исполнителя."""
    tid, _, target = as_str(arg).partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    if not target.isdigit():
        return await api.send(x, "Сотрудник не найден.", BACK)
    await db.set_state(x, "forward_comment", {"tid": t["ticket_id"], "target": target})
    a = await admin_of(target)
    await api.send(
        x,
        f"Передать обращение №{t['ticket_id']} сотруднику {a['full_name'] if a else target}?\n"
        "Можно добавить комментарий для него или передать сразу.",
        [[btn("↪️ Передать", f"fwdok:{t['ticket_id']}:{target}"),
          btn("💬 Комментом", f"fwdok:{t['ticket_id']}:{target}:ask")],
         [btn("✖️ Отмена", f"t:{t['ticket_id']}")]],
    )


@state("forward_comment")
async def st_forward_comment(x, text, p):
    """Комментарий при передаче сохраняется как заметка - её увидит новый сотрудник."""
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or not staff_side:
        await db.clear_state(x)
        return await api.send(x, "Обращение недоступно.", BACK)
    target = as_str(p.get("target", ""))
    comment = as_str(text).strip()[:200]
    await db.clear_state(x)
    return await _do_forward(x, t, target, comment)


@callback("fwdok")
async def cb_forward_ok(x, arg):
    """Передача без комментария."""
    tid, _, rest = as_str(arg).partition(":")
    target, _, _ask = rest.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    await db.clear_state(x)
    return await _do_forward(x, t, target, "")


async def _do_forward(x, t, target: str, comment: str) -> None:
    """Общая часть передачи: меняем исполнителя, пишем историю, уведомляем нового."""
    done, message = await repo.forward_ticket(t["ticket_id"], target, x, comment)
    if not done:
        await db.clear_state(x)
        return await api.send(x, f"❌ {message}", [[btn("↩️ К обращению", f"t:{t['ticket_id']}")]])
    if comment:
        await repo.add_internal_note(t["ticket_id"], x, f"Передано: {comment}")
    fresh = await repo.get_ticket(t["ticket_id"])
    text = (f"↪️ Вам передали обращение №{t['ticket_id']}\n{CATS.get(fresh['category'], '')}\n"
            + (f"Комментарий: {comment}\n" if comment else "")
            + f"\n{short(fresh['text_content'], 700)}")
    delivered = await notify(target, text, ticket_kb(fresh, True, is_super(await admin_of(x))))
    await repo.log_action(x, "обращение передано", f"№{t['ticket_id']} → ID {target}")
    tail = "" if delivered else "\n⚠️ Новый сотрудник ещё не запускал бота - напишите ему лично."
    await api.send(x, f"✅ {message}.{tail}", [[btn("👤 Открыть обращение", f"t:{t['ticket_id']}")]])


# ── подстановки в шаблонах ответов ─────────────────────────────────────────
PLACEHOLDER_RE = re.compile(r"\{([^{}\n]{1,40})\}")
"""Плейсхолдер в фигурных скобках: {ФИО}, {группа}, {дата}…"""

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
"""Месяцы в родительном падеже: дата в письме — «28 сентября 2026»."""

TPL_STUDENT = "студент"        # ФИО неизвестно - лучше слово, чем дыра в письме
TPL_MISSING = "не указано"     # данных нет - в письме слово лучше дыры
TPL_FIELDS: tuple[str, ...] = (
    "ФИО", "фамилия", "имя", "отчество", "группа", "преподаватель", "должность",
    "кабинет", "кабинет_выдачи", "дата", "время", "номер", "тема",
    "колледж", "учебная_часть", "директор",
)
"""Подстановки, которые бот понимает: перечисляем одной строкой, когда встретил неизвестную."""

TPL_PAGE = 12               # сколько шаблонов помещается на экран ответа


def topic_words(t) -> set:
    """Ключевые слова темы обращения: по ним шаблоны в списке идут выше."""
    topic = _row_value(t, "topic") or CATS.get(_row_value(t, "category"), "")
    return set(re.findall(r"[а-яёa-z]{4,}", topic.lower()))


async def template_values(t, staff=None) -> dict:
    """Значения подстановок по обращению и сотруднику. Ключи - в нижнем регистре.

    Пропуски в данных не оставляем пустыми: в письме студенту «ваша группа —»
    читается как ошибка бота, поэтому подставляем слово.
    """
    student = _get(t, "student") or await repo.get_user(_row_value(t, "student_id"))
    person = staff if staff is not None else _get(t, "staff")
    if person is None and _row_value(t, "target_admin_id"):
        person = await admin_of(_row_value(t, "target_admin_id"))
    full = _row_value(student, "full_name")
    parts = full.split()
    now = clock.now()
    return {
        "фио": full or TPL_STUDENT,
        "фамилия": parts[0] if parts else TPL_STUDENT,
        "имя": parts[1] if len(parts) > 1 else TPL_STUDENT,
        "отчество": parts[2] if len(parts) > 2 else TPL_MISSING,
        "группа": _row_value(student, "group_code") or "не указана",
        "преподаватель": _row_value(person, "full_name") or "сотрудник",
        "должность": (position_of(person) if person is not None else "") or "не назначена",
        "кабинет": (office_of(person) if person is not None else "") or "не указан",
        # место выдачи: сначала кабинет из самого обращения, потом кабинет
        # сотрудника (документ отдают на его рабочем месте), потом честное «не указан»
        "кабинет_выдачи": (_row_value(t, "pickup_place")
                           or (office_of(person) if person is not None else "")
                           or PICKUP_FALLBACK),
        "дата": f"{now.day} {MONTHS[now.month - 1]} {now.year}",
        "время": now.strftime("%H:%M"),
        "номер": as_str(_get(t, "ticket_id")) or TPL_MISSING,
        "тема": _row_value(t, "topic") or CATS.get(_row_value(t, "category"), "обращения"),
        # телефоны берём из справочника college (их правят в панели, в шаблоне
        # писать номера нельзя - устареют первыми). {колледж} и {учебная_часть} -
        # один и тот же телефон: учебная часть это общий контакт для студентов
        # колледжа, отдельного «телефона колледжа» в справочнике нет.
        "колледж": await college.get("телефон_учебная_часть"),
        "учебная_часть": await college.get("телефон_учебная_часть"),
        "директор": await college.get("телефон_директор"),
    }


def unknown_placeholders(text: str) -> list:
    """Имена в скобках, которых нет среди известных подстановок."""
    known = {name.lower() for name in TPL_FIELDS}
    found = []
    for match in PLACEHOLDER_RE.finditer(as_str(text)):
        name = match.group(1).strip()
        if name and name.lower() not in known and name not in found:
            found.append(name)
    return found


def placeholder_help(unknown=()) -> str:
    """Что можно подставить: одна строка, чтобы сотрудник понял, где ошибка."""
    names = ", ".join("{%s}" % name for name in TPL_FIELDS)
    tail = f" Непонятно: {', '.join('{%s}' % name for name in unknown)}." if unknown else ""
    return f"🔤 Подстановки: {names}.{tail}"


async def render_template(text, ticket, staff=None) -> str:
    """Текст шаблона с данными обращения: {ФИО} -> «Иванов Иван».

    Имя в скобках пишется в любом регистре: {ФИО}, {фио} и {Фио} - одно и то же.
    Неизвестное имя остаётся в тексте как есть: выбросить его молча нельзя
    (сотрудник отправит письмо с дырой), поэтому подсказка идёт в его сообщение
    вместе с текстом - см. unknown_placeholders() и placeholder_help().
    """
    values = await template_values(ticket, staff)
    return PLACEHOLDER_RE.sub(
        lambda match: values.get(match.group(1).strip().lower(), match.group(0)), as_str(text))


async def staff_templates(x: str, t, limit: int = TPL_PAGE) -> list:
    """Шаблоны для обращения: личные сотрудника сначала, потом подходящие по теме.

    Возвращает пары (шаблон, личный_ли) - пометка «мои» нужна в тексте экрана.
    """
    rows = list(await repo.list_templates(as_str(t["category"]), limit=50))
    mine = await personal_template_ids(x)
    words = topic_words(t)

    def rank(item):
        row, own = item
        hay = f"{as_str(row['title'])} {as_str(row['text'])}".lower()
        return (0 if own else 1, 0 if words & set(re.findall(r"[а-яёa-z]{4,}", hay)) else 1)

    paired = [(row, int(row["id"]) in mine) for row in rows]
    return sorted(paired, key=rank)[:limit]


def templates_text(rows: list) -> str:
    """Названия шаблонов текстом: в кнопке длинное название обрезается."""
    return "\n".join(f"· {short(row['title'], 40)}{'  — мои' if own else ''}"
                     for row, own in rows)


@callback("tpl")
async def cb_templates(x, arg):
    """Шаблоны ответов: подходящие под раздел обращения + общие.

    Показываем и по кнопке в меню, и из карточки обращения (тогда сразу
    подставляем номер обращения в состояние ответа).
    """
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Шаблоны доступны сотруднику в его обращении.", BACK)
    rows = await staff_templates(x, t)
    if not rows:
        return await api.send(
            x,
            "⚡ Шаблонов для этого раздела пока нет. Их добавляет сис-админ в панели: "
            "«⚡ Шаблоны ответов».",
            [[btn("↩️ К обращению", f"t:{t['ticket_id']}")], *BACK],
        )
    # название шаблона режем по границе слова и без многоточия: MAX обрезает
    # кнопку в одну строку, а полное название и так видно в тексте ниже
    keyboard = [[btn(cut_plain(row["title"], max_api.BUTTON_TEXT),
                     f"tplu:{row['id']}:{t['ticket_id']}")] for row, _own in rows]
    await api.send(
        x,
        f"⚡ Шаблоны для раздела {CATS.get(t['category'], t['category'])}\n"
        "Выберите - текст подставится в ответ, его можно поправить перед отправкой.\n"
        "Подстановки: {ФИО}, {группа}, {дата} и другие - полный список виден "
        "при выборе шаблона.\n\n"
        f"{templates_text(rows)}",
        [*keyboard, [btn("↩️ К обращению", f"t:{t['ticket_id']}")], *BACK],
    )


@callback("tplu")
async def cb_template_use(x, arg):
    """Подставляет шаблон в ответ: сотрудник может дописать своё и отправить."""
    template_id, _, tid = as_str(arg).partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    template = await repo.get_template(to_int(template_id))
    if not template:
        return await api.send(x, "Шаблон удалён.", [[btn("↩️ К обращению", f"t:{tid}")]])
    await repo.count_template_use(to_int(template_id))
    raw = as_str(template["text"])
    draft = await render_template(raw, t, await admin_of(x))
    # неизвестное имя в скобках оставляем как есть, но говорим сотруднику,
    # какие имена бот понимает: иначе «{ФИО student}» уйдёт студенту как есть
    unknown = unknown_placeholders(raw)
    await db.set_state(x, "reply", {"tid": t["ticket_id"], "draft": draft[:3000]})
    own = await is_personal_template(x, to_int(template_id))
    await api.send(
        x,
        f"⚡ Шаблон «{template['title']}» готов к отправке в обращение №{t['ticket_id']}:\n\n"
        f"{short(draft, 1200)}\n\n"
        f"{placeholder_help(unknown)}\n\n"
        "Отправить как есть, дописать своё или отменить?",
        [[btn("📤 Отправить", f"tplsend:{t['ticket_id']}")],
         [btn("✏️ Дописать", f"tplmore:{t['ticket_id']}")],
         [btn("⭐ Убрать из моих" if own else "⭐ Сделать личным",
              f"tplmy:{t['ticket_id']}:{template_id}:{0 if own else 1}")],
         [btn("✖️ Отмена", f"t:{t['ticket_id']}")]],
    )


@callback("tplmy")
async def cb_template_mine(x, arg):
    """Пометить шаблон личным: свои ответы сотрудник видит первыми.

    Отдельной колонки в таблице reply_templates нет, а править схему ради
    одной отметки нельзя - пометка живёт в settings по ключу
    tpl:<user_id>:<id> (см. handlers.common).
    """
    tid, _, rest = as_str(arg).partition(":")
    template_id, _, flag = rest.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    if not await repo.get_template(to_int(template_id)):
        return await api.send(x, "Шаблон удалён.", [[btn("↩️ К обращению", f"t:{tid}")]])
    await set_personal_template(x, to_int(template_id), as_str(flag).strip() == "1")
    rows = await staff_templates(x, t)
    own = int(template_id) in await personal_template_ids(x)
    title = title_of(rows, template_id)
    note = (f"⭐ Шаблон «{title}» теперь личный: он первым в списке ответов и помечен «мои»."
            if own else f"⭐ Шаблон «{title}» больше не личный: он снова среди общих.")
    return await api.send(
        x, note,
        [[btn("⚡ К шаблонам", f"tpl:{tid}"), btn("↩️ К обращению", f"t:{tid}")], *BACK],
    )


@callback("tplsend")
async def cb_template_send(x, arg):
    """Отправляет шаблон как есть - самый частый случай."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    session = await db.get_state(x) or {}
    draft = as_str((session.get("payload") or {}).get("draft", ""))
    if not draft:
        return await api.send(x, "Шаблон уже отправлен или сброшен.", [[btn("↩️ К обращению", f"t:{arg}")]])
    return await st_reply(x, "", {"tid": t["ticket_id"], "draft": draft})


@callback("tplmore")
async def cb_template_more(x, arg):
    """Оставляет шаблон в буфере: сотрудник дописывает своё обычным сообщением."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    await api.send(x, "Допишите текст — он уйдёт студенту после шаблона. Или /cancel.")


# ── «Ответить и закрыть» ───────────────────────────────────────────────────
# Сценарий ради двух нажатий: кнопка в карточке, затем шаблон (или свой
# текст). Ответ уходит студенту вместе с пометкой о закрытии, статус сразу
# «✅ Завершено» - отдельный переход по «Завершено» сотруднику делать не нужно.
@callback("tplclose")
async def cb_reply_close(x, arg):
    """Экран «Ответить и закрыть»: шаблоны по теме обращения + свой текст."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    if is_archived(t) or as_str(t["status"]) in CLOSED_STATUSES:
        return await api.send(
            x, f"Обращение №{t['ticket_id']} уже закрыто - отвечать на него не нужно.",
            [[btn("📂 Открыть обращение", f"t:{t['ticket_id']}")], *BACK])
    rows = await staff_templates(x, t)
    keyboard = [[btn(cut_plain(row["title"], max_api.BUTTON_TEXT),
                     f"tplcx:{t['ticket_id']}:{row['id']}")] for row, _own in rows]
    await api.send(
        x,
        f"{CLOSE_BTN} — обращение №{t['ticket_id']}\n"
        f"{cat_topic_line(t['category'], _row_value(t, 'topic'))}\n"
        "Ответ уйдёт студенту, обращение сразу станет «✅ Завершено». "
        "В шаблонах подставляются ФИО, группа, кабинет, дата и время.\n\n"
        + (templates_text(rows) if rows else "Готовых шаблонов для этого раздела нет."),
        [*keyboard,
         [btn("✍️ Свой текст", f"tplct:{t['ticket_id']}")],
         [btn("↩️ К обращению", f"t:{t['ticket_id']}")], *BACK],
    )


@callback("tplcx")
async def cb_reply_close_by_template(x, arg):
    """Шаблон в режиме «ответить и закрыть»: отправляем и закрываем одним делом."""
    tid, _, template_id = as_str(arg).partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    template = await repo.get_template(to_int(template_id))
    if not template:
        return await api.send(x, "Шаблон удалён.", [[btn("↩️ К обращению", f"t:{tid}")]])
    await repo.count_template_use(to_int(template_id))
    text = await render_template(template["text"], t, await admin_of(x))
    return await send_and_close(x, t, text, f"⚡ Шаблон «{template['title']}»",
                                unknown_placeholders(as_str(template["text"])))


@callback("tplct")
async def cb_reply_close_own_text(x, arg):
    """Свой текст в режиме «ответить и закрыть»: пишем ответ обычным сообщением."""
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Обращение не найдено.", BACK)
    if is_archived(t) or as_str(t["status"]) in CLOSED_STATUSES:
        return await api.send(
            x, f"Обращение №{t['ticket_id']} уже закрыто - отвечать на него не нужно.",
            [[btn("📂 Открыть обращение", f"t:{t['ticket_id']}")], *BACK])
    await db.set_state(x, "tpl_close", {"tid": t["ticket_id"]})
    await api.send(
        x,
        f"Ответ по обращению №{t['ticket_id']} — он уйдёт студенту и закроет обращение.\n"
        "Напишите текст или /cancel для отмены.")


@state("tpl_close")
async def st_reply_close(x, text, p):
    """Свой текст в режиме закрытия: отправляем ответ и закрываем обращение."""
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or not staff_side:
        await db.clear_state(x)
        return await api.send(x, "Обращение недоступно.", BACK)
    return await send_and_close(x, t, as_str(text), "")


async def send_and_close(x: str, t, text: str, source: str = "", unknown=()):
    """Ответ сотрудника и закрытие обращения одним действием.

    Закрываем раньше, чем пишем сообщение: repo.transition_ticket_status
    меняет статус только если он ещё прежний, поэтому второе нажатие той же
    кнопки (старый экран у сотрудника мог остаться) не закроет обращение
    повторно и не отправит студенту второе уведомление.
    """
    tid = t["ticket_id"]
    text = text.strip()[:3000]
    if not text:
        await db.clear_state(x)
        return await api.send(x, "Ответ пустой - отправлять нечего.",
                              [[btn("↩️ К обращению", f"t:{tid}")], *BACK])
    if as_str(t["status"]) in CLOSED_STATUSES or is_archived(t):
        await db.clear_state(x)
        return await api.send(x, f"Обращение №{tid} уже закрыто.",
                              [[btn("📂 Открыть обращение", f"t:{tid}")], *BACK])
    if not await repo.transition_ticket_status(tid, as_str(t["status"]), "completed", actor_id=x):
        await db.clear_state(x)
        return await api.send(x, f"Обращение №{tid} уже закрыто - второй раз не закрываем.",
                              [[btn("📂 Открыть обращение", f"t:{tid}")], *BACK])
    await repo.add_ticket_message(tid, x, "staff", text)
    fresh = await repo.get_ticket(tid)
    student = fresh["student_id"] if fresh else t["student_id"]
    await notify(
        student,
        f"✅ Обращение №{tid} закрыто - сотрудник ответил и закрыл его.\n\n{text}\n\n"
        "Если появятся новые вопросы - напишите новое обращение через меню.",
        [[btn("📂 Открыть обращение", f"t:{tid}")]],
    )
    lines = [f"{source}. " if source else "",
             f"✅ Ответ отправлен, обращение №{tid} закрыто."]
    if unknown:
        lines.append(placeholder_help(unknown))
    await api.send(x, "\n".join(lines), [[btn("📂 Открыть обращение", f"t:{tid}")], *BACK])
    if fresh:
        await send_ticket(x, fresh, True)


def title_of(rows: list, template_id) -> str:
    """Название шаблона из списка - для подтверждения пометки «личный»."""
    for row, _own in rows:
        if int(row["id"]) == int(template_id):
            return short(row["title"], 40)
    return f"ID {template_id}"


@state("reply")
async def st_reply(x, text, p):
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or (not staff_side and t["status"] not in OPEN_STATUSES):
        await db.clear_state(x)
        return await api.send(x, "Обращение недоступно или закрыто.", BACK)
    draft = as_str((p or {}).get("draft", ""))
    text = text[:3000]
    if draft and text:
        text = f"{draft}\n\n{text}"  # сотрудник дописал своё к шаблону
    elif draft:
        text = draft
    if not text.strip():
        await db.clear_state(x)
        return await api.send(x, "Отправлять нечего.", [[btn("📂 Открыть", f"t:{t['ticket_id']}")]])
    await db.clear_state(x)
    tid = t["ticket_id"]
    if staff_side:
        await repo.add_ticket_message(tid, x, "staff", text)
        if t["status"] in ACCEPT_ON_REPLY:
            await repo.transition_ticket_status(tid, t["status"], "accepted", actor_id=x)
        t = await repo.get_ticket(tid)
        await notify(t["student_id"], f"💬 Ответ по обращению №{tid}:\n\n{text}",
                     [[btn("✍️ Ответить", f"rp:{tid}"), btn("📂 Открыть", f"t:{tid}")]])
    else:
        await repo.add_ticket_message(tid, x, "student", text)
        user = await repo.get_user(x)
        target_is_super = is_super(await admin_of(t["target_admin_id"]))
        await notify(
            t["target_admin_id"],
            f"📨 Новое сообщение в обращении №{tid} от {user['full_name']} ({user['group_code']})\n\n{text}",
            ticket_kb(t, True, target_is_super),
        )
    await api.send(x, f"✅ Сообщение по обращению №{tid} отправлено.", [[btn("📂 Открыть", f"t:{tid}")], *BACK])


def _status_change_error(t, status: str) -> str:
    current = as_str(_get(t, "status")).strip()
    if current in ("rejected", "completed"):
        return (
            f"Заявка №{t['ticket_id']} закрыта. Сначала верните её в работу: "
            f"нажмите «{STATUS['accepted']}»."
        )
    return (
        f"Заявка №{t['ticket_id']} нельзя перевести из статуса "
        f"«{STATUS.get(current, current)}» в «{STATUS.get(status, status)}»."
    )


async def _notify_office_required(x: str, t):
    message = (
        f"{OFFICE_REQUIRED}. Обращение №{t['ticket_id']} нельзя перевести в готовность."
    )
    keyboard = [[btn("📂 Открыть", f"t:{t['ticket_id']}")], *BACK]
    recipients = {str(x), as_str(_get(t, "target_admin_id"))}
    recipients.update(str(value) for value in config.SYSADMIN_IDS)
    for recipient in recipients:
        if recipient:
            await notify(recipient, message, keyboard)


@callback("st")
async def cb_status(x, arg):
    tid, _, requested_status = arg.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side or requested_status not in STATUS:
        return
    current_status = as_str(_get(t, "status")).strip()
    status = requested_status
    if status == "in_progress" and current_status in ("new", "in_progress"):
        status = "accepted"
    legacy_completed = status == "completed" and current_status in LEGACY_COMPLETED_FROM
    if status == "ready" and current_status == "ready":
        return await start_ready(x, t)
    if not legacy_completed and status not in NEXT_STATUSES.get(current_status, ()):
        return await api.send(
            x,
            _status_change_error(t, status),
            [[btn("📂 Открыть", f"t:{t['ticket_id']}")], *BACK],
        )
    if status == "ready":
        return await start_ready(x, t)
    changed = await repo.transition_ticket_status(t["ticket_id"], current_status, status, actor_id=x)
    if not changed:
        current = await repo.get_ticket(t["ticket_id"])
        if current:
            await send_ticket(x, current, True)
        return
    await notify(t["student_id"], f"🔔 Статус обращения №{t['ticket_id']}: {STATUS[status]}",
                 [[btn("📂 Открыть", f"t:{t['ticket_id']}")]])
    t = await repo.get_ticket(t["ticket_id"])
    await send_ticket(x, t, True)


async def start_ready(x: str, t):
    tid = t["ticket_id"]
    status = as_str(_get(t, "status")).strip()
    if status == "ready":
        return await api.send(x, f"📄 Заявка №{tid} уже готова — время выдачи: "
                                 f"{_row_value(t, 'ready_until') or 'не задано'}.",
                              [[btn("📂 Открыть", f"t:{tid}")], *BACK])
    if "ready" not in NEXT_STATUSES.get(status, ()):
        return await api.send(x, _status_change_error(t, "ready"), BACK)
    staff = (await ticket_people(t))["staff"]
    if not office_of(staff):
        return await _notify_office_required(x, t)
    await api.send(x, f"📄 Заявка №{tid}: когда студент сможет забрать документ?",
                   [[btn("Сегодня до 18:00", f"rt:{tid}:today")],
                    [btn("Завтра до 18:00", f"rt:{tid}:tomorrow")],
                    [btn("✏️ Ввести своё", f"rt:{tid}:custom")], *BACK])


@callback("rt")
async def cb_ready_time(x, arg):
    tid, _, kind = arg.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side:
        return
    status = as_str(_get(t, "status")).strip()
    if status == "ready":
        return await start_ready(x, t)
    if "ready" not in NEXT_STATUSES.get(status, ()):
        return await api.send(x, _status_change_error(t, "ready"), BACK)
    staff = (await ticket_people(t))["staff"]
    if not office_of(staff):
        return await _notify_office_required(x, t)
    if kind == "custom":
        await db.set_state(x, "ready_time", {"tid": t["ticket_id"]})
        return await api.send(x, "Введите время выдачи: например, «завтра до 15:00» или «25.09.2026 до 18:00».")
    if kind not in ("today", "tomorrow"):
        return
    await set_ready(x, t, ready_deadline(kind))


@state("ready_time")
async def st_ready_time(x, text, p):
    await db.clear_state(x)
    value = " ".join(as_str(text).split())[:READY_MAX]
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not value or not t or not staff_side:
        return await api.send(x, "Обращение недоступно. Нажмите «Готово» ещё раз.", BACK)
    staff = (await ticket_people(t))["staff"]
    if not office_of(staff):
        return await _notify_office_required(x, t)
    await set_ready(x, t, value)


async def set_ready(x: str, t, ready_until: str):
    tid = t["ticket_id"]
    status = as_str(_get(t, "status")).strip()
    if status == "ready":
        return await start_ready(x, t)
    if "ready" not in NEXT_STATUSES.get(status, ()):
        return await api.send(x, _status_change_error(t, "ready"), BACK)
    staff = (await ticket_people(t))["staff"]
    place = _row_value(t, "pickup_place") or office_of(staff)
    doc = _row_value(t, "doc_url")
    await repo.set_ticket_ready(tid, ready_until, place, doc, actor_id=x)
    updated = await repo.get_ticket(tid)
    if updated:
        t = updated
    when = _row_value(t, "ready_until") or ready_until
    place = _row_value(t, "pickup_place") or place or PICKUP_FALLBACK
    doc = _row_value(t, "doc_url") or doc
    staff = (await ticket_people(t))["staff"]
    name = _row_value(staff, "full_name") or "не указан"
    lines = [
        f"📄 Заявка готова №{tid}",
        f"Ответственный: {name}",
        f"Должность: {role_label(staff) or 'не назначена'}",
        f"Когда забрать: {when}",
        f"Где забрать: {place}",
    ]
    kb = [[btn("📂 Открыть", f"t:{tid}")]]
    if doc:
        lines.append(f"Документ: {doc}")
        kb.insert(0, [link_btn("📄 Открыть документ", doc)])
    await notify(t["student_id"], "\n".join(lines), kb)
    await send_ticket(x, t, True)
