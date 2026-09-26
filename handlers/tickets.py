"""Обращения студентов: создание, переписка, статусы, списки."""
from datetime import datetime, timedelta

import config
import database as db
import repository as repo
from handlers.admin import STAFF_ROLES, audit
from handlers.common import BACK, admin_of, api, is_super, notify
from handlers.menus import need_author
from handlers.registry import callback, state
from max_api import btn, link_btn
from utils import (
    ACCEPT_ON_REPLY,
    CATS,
    OPEN_STATUSES,
    STATUS,
    TOPIC_CATS,
    as_str,
    fmt_when,
    short,
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
        return free
    code = _row_value(person, "role")
    return STAFF_ROLES.get(code, code)


def role_label(person) -> str:
    return position_of(person)


def staff_pick_label(person) -> str:
    name = _row_value(person, "full_name")
    role = role_label(person)
    return f"{name} · {role}" if name and role else name


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


async def load_ticket(x: str, ticket_id: int):
    """→ (обращение, сторона_сотрудника). (None, False), если обращения нет или доступа нет."""
    t = await repo.get_ticket(ticket_id)
    if not t:
        return None, False
    if t["student_id"] == x:
        return t, False
    a = await admin_of(x)
    if a and (is_super(a) or t["target_admin_id"] == x):
        return t, True
    return None, False


def ticket_kb(t, staff_side: bool, can_delete: bool = False):
    tid, status = t["ticket_id"], t["status"]
    if not staff_side:
        rows = [[btn("✍️ Написать сотруднику", f"rp:{tid}")]] if status in OPEN_STATUSES else []
        return [*rows, [btn("↩️ К списку", "tickets")]]
    rows = [[btn("💬 Ответить", f"rp:{tid}")]]
    changes = [btn(STATUS[code], f"st:{tid}:{code}") for code in NEXT_STATUSES.get(status, ())]
    rows += [changes[i : i + 2] for i in range(0, len(changes), 2)]
    tail = [btn("↩️ К списку", "staff")]
    if can_delete:
        tail.append(btn("🗑 Удалить", f"tdel:{tid}"))
    if staff_side:
        tail.insert(0, btn("⚡ Шаблоны", f"tpl:{tid}"))
    return [*rows, tail]


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


MESSAGE_LABEL = {"student": "уточнение студента", "staff": "ответ сотрудника"}
EVENT_LABEL = {"status": "статус", "ready": "документ готов", "created": "создано",
               "message_student": "сообщение студента", "message_staff": "ответ сотрудника"}


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
    label = EVENT_LABEL.get(event, event)
    return f"{label}: {detail}" if detail else label


async def ticket_text(t, staff_side: bool) -> str:
    t = await ticket_people(t)
    lines = [f"📂 Обращение №{t['ticket_id']} · {STATUS.get(t['status'], t['status'])}",
             cat_topic_line(t["category"], _row_value(t, "topic"))]
    lines.append(f"Студент: {_person(_get(t, 'student'), 'group_code') or '—'}")
    staff = _get(t, "staff")
    lines.append(f"Ответственный: {_person(staff, 'user_id', 'ID ') or '—'}")
    if position_of(staff):
        lines.append(f"Должность: {position_of(staff)}")
    when = _row_value(t, "ready_until")
    if when:
        place = _row_value(t, "pickup_place") or office_of(staff) or PICKUP_FALLBACK
        lines.append(f"Готово: {when} · {place}")
    msgs = list(reversed(await repo.ticket_thread(t["ticket_id"], 10)))
    lines.append("")
    for index, m in enumerate(msgs):
        role = _row_value(m, "sender_role")
        icon = "🎓" if role == "student" else "🏫"
        label = "автор обращения" if role == "student" and index == 0 else MESSAGE_LABEL[role]
        lines.append(f"{icon} {message_author(m)} · {label} · {fmt_when(m['created_at'])}:")
        lines.append(f"   {short(m['text'], 700)}")
    history = [e for e in await repo.ticket_events(t["ticket_id"], 20)
               if _row_value(e, "event") in ("status", "ready")]
    if history:
        lines.append("")
        lines.append("📌 " + "; ".join(f"{fmt_when(e['created_at'])} — {event_text(e)}" for e in reversed(history[-4:])))
    return "\n".join(lines)


async def send_ticket(x: str, t, staff_side: bool):
    """Карточка обращения. Кнопку удаления показываем только сис-админу."""
    can_delete = staff_side and is_super(await admin_of(x))
    await api.send(x, await ticket_text(t, staff_side), ticket_kb(t, staff_side, can_delete))


async def ticket_rows_kb(rows, staff_side: bool = False):
    """Кнопки списка обращений; помечает те, где последнее слово за сотрудником."""
    latest = await repo.latest_message_roles([row["ticket_id"] for row in rows])
    buttons = []
    for r in rows:
        mark = ""
        last = latest.get(int(r["ticket_id"]))
        if last == "student":
            mark = " · 🔔 ждёт ответа" if staff_side else ""
        elif last == "staff":
            mark = " · 📌 ждёт вашего ответа" if not staff_side else ""
        buttons.append(btn(f"№{r['ticket_id']} · {STATUS[r['status']]} · {CATS[r['category']]}{mark}",
                           f"t:{r['ticket_id']}"))
    return [[item] for item in buttons]


# Фильтры очереди сотрудника: сгруппированы по смыслу, а не по алфавиту.
STAFF_QUEUE_FILTERS = (("open", "🔓 Открытые"), ("new", "🆕 Без ответа"),
                       ("ready", "📄 К выдаче"), ("completed", "✅ Завершённые"))


async def send_staff_queue(x: str, view: str = "") -> None:
    """Очередь сотрудника: счётчики, фильтры и список.

    Фильтр передаётся в кнопке, а не хранится в состоянии: так он не слетает
    при возврате из карточки и не зависит от того, какую кнопку нажали раньше.
    """
    a = await admin_of(x)
    if not a:
        return await api.send(x, "Сотрудник не найден.", BACK)
    scope = None if is_super(a) else x
    all_rows = await repo.admin_tickets(scope)
    counts = await repo.status_counts(scope)
    if view == "open":
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
    if view:
        lines.append(f"\nФильтр: {dict(STAFF_QUEUE_FILTERS).get(view) or CATS.get(view, view)} — {len(rows)}")
    else:
        lines.append("Фильтр не выбран — показаны все обращения")
    status_row = [btn(("● " if code == view else "") + label, f"stafff:{code}")
                  for code, label in STAFF_QUEUE_FILTERS]
    cat_row = [btn(("● " if code == view else "") + label, f"stafff:{code}") for code, label in CATS.items()]
    keyboard = [status_row[i:i + 2] for i in range(0, len(status_row), 2)]
    keyboard += [cat_row[i:i + 2] for i in range(0, len(cat_row), 2)]
    keyboard.append([btn(f"🔄 Обновить ({len(rows)})", f"staff:{view}"),
                     btn("Сбросить фильтр", f"staff:{''}")])
    if rows:
        keyboard += await ticket_rows_kb(rows[:15], True)
    else:
        keyboard.append([btn("Под таким фильтром обращений нет", "noop")])
    await api.send(x, "\n".join(lines), [*keyboard, *BACK])


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


@callback("new")
async def cb_new_ticket(x, cat):
    if cat not in CATS or not await need_author(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    if cat in TOPIC_CATS:
        return await api.send(x, f"{CATS[cat]}\nВыберите тему обращения:",
                              [[btn(label, f"topic:{cat}:{code}")] for code, label in TOPIC_CATS[cat].items()] + BACK)
    rows = await repo.staff_for_category(cat)
    if not rows:
        return await api.send(x, "Сотрудники для этого раздела пока не назначены. Обратитесь в учебную часть.", BACK)
    kb = [[btn(short(staff_pick_label(r), 60), f"pick:{cat}:{r['user_id']}")] for r in rows]
    await api.send(x, f"{CATS[cat]}\nВыберите сотрудника:", kb + BACK)


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
    kb = [[btn(short(staff_pick_label(r), 60), f"pick:{cat}:{r['user_id']}:{code}")] for r in rows]
    await api.send(x, f"{CATS[cat]} · {title}\nВыберите сотрудника:", kb + BACK)


async def _pending_topic(x: str, cat: str, code: str) -> str:
    topic = topic_title(cat, code)
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
    await db.set_state(x, "ticket", {"admin": admin_id, "cat": cat, "topic": await _pending_topic(x, cat, code)})
    await api.send(x, f"Кому: {a['full_name']}\nНапишите обращение одним сообщением (или /cancel для отмены).")


@state("ticket")
async def st_ticket(x, text, p):
    user = await need_author(x)
    if not user:
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        await db.clear_state(x)
        return await api.send(x, "Приём обращений временно отключён.", BACK)
    admin_id = as_str(p.get("admin"))
    if not admin_id:
        await db.clear_state(x)
        return await api.send(x, "Сначала выберите сотрудника: нажмите «↩️ В меню» и начните заново.", BACK)
    admin = await admin_of(admin_id)
    if not admin:
        await db.clear_state(x)
        return await api.send(x, "Сотрудник больше недоступен. Начните заново.", BACK)
    text = text[:3000]
    topic = " ".join(as_str(p.get("topic", "")).split())[:READY_MAX]
    tid = await repo.create_ticket(x, admin_id, p["cat"], text, topic=topic)
    await db.clear_state(x)
    t = await repo.get_ticket(tid)
    delivered = await notify(
        admin_id,
        f"🔔 Новое обращение №{tid}\n{cat_topic_line(p['cat'], topic)}\n"
        f"От: {user['full_name']} ({user['group_code']})\n\n{text}",
        ticket_kb(t, True),
    )
    lines = [f"✅ Обращение №{tid} отправлено."]
    if topic:
        lines.append(f"Тема: {topic}")
    lines += ["", short(text, 700)]
    if not delivered:
        lines.append("\n⚠️ Сотрудник пока не запускал бота — уведомление не дошло, но обращение сохранено.")
    await api.send(x, "\n".join(lines), [[btn("📂 Открыть", f"t:{tid}")], *BACK])


@callback("tickets")
async def cb_my_tickets(x, arg):
    if not await need_author(x):
        return
    rows = await repo.recent_student_tickets(x)
    if not rows:
        return await api.send(x, "У вас пока нет обращений.", BACK)
    await api.send(x, "📋 Мои обращения (последние 15). Нажмите на обращение, чтобы открыть переписку:",
                   await ticket_rows_kb(rows) + BACK)


@callback("staff")
async def cb_staff_tickets(x, arg):
    """Очередь сотрудника: из меню - без фильтра, с кнопки «Обновить» - с тем же."""
    if not await admin_of(x):
        return
    return await send_staff_queue(x, as_str(arg))


@callback("t")
async def cb_open_ticket(x, arg):
    t, staff_side = await load_ticket(x, to_int(arg))
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


@callback("tpl")
async def cb_templates(x, arg):
    """Шаблоны ответов: подходящие под раздел обращения + общие.

    Показываем и по кнопке в меню, и из карточки обращения (тогда сразу
    подставляем номер обращения в состояние ответа).
    """
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t or not staff_side:
        return await api.send(x, "Шаблоны доступны сотруднику в его обращении.", BACK)
    rows = await repo.list_templates(as_str(t["category"]))
    if not rows:
        return await api.send(
            x,
            "⚡ Шаблонов для этого раздела пока нет. Их добавляет сис-админ в панели: "
            "«⚡ Шаблоны ответов».",
            [[btn("↩️ К обращению", f"t:{t['ticket_id']}")], *BACK],
        )
    keyboard = [[btn(short(row["title"], 40), f"tplu:{row['id']}:{t['ticket_id']}")] for row in rows[:12]]
    await api.send(
        x,
        f"⚡ Шаблоны для раздела {CATS.get(t['category'], t['category'])}\n"
        "Выберите - текст подставится в ответ, его можно поправить перед отправкой.",
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
    await db.set_state(x, "reply", {"tid": t["ticket_id"], "draft": as_str(template["text"])[:3000]})
    await api.send(
        x,
        f"⚡ Шаблон «{template['title']}» готов к отправке в обращение №{t['ticket_id']}:\n\n"
        f"{short(template['text'], 1200)}\n\n"
        "Отправить как есть, дописать своё или отменить?",
        [[btn("📤 Отправить как есть", f"tplsend:{t['ticket_id']}"), btn("✏️ Дописать", f"tplmore:{t['ticket_id']}")],
         [btn("✖️ Отмена", f"t:{t['ticket_id']}")]],
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
