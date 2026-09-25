"""Обращения студентов: создание, переписка, статусы, списки."""
import database as db
import repository as repo
from handlers import common
from handlers.common import BACK, admin_of, is_super, notify
from handlers.menus import need_student
from handlers.registry import callback, state
from max_api import btn
from utils import CATS, OPEN_STATUSES, STATUS, short, to_int


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


def ticket_kb(t, staff_side: bool):
    tid, status = t["ticket_id"], t["status"]
    if not staff_side:
        rows = [[btn("✍️ Написать сотруднику", f"rp:{tid}")]] if status in OPEN_STATUSES else []
        return [*rows, [btn("↩️ К списку", "tickets")]]
    rows = [[btn("💬 Ответить", f"rp:{tid}")]]
    changes = [btn(label, f"st:{tid}:{code}") for code, label in STATUS.items() if code not in (status, "new")]
    rows += [changes[i : i + 2] for i in range(0, len(changes), 2)]
    return [*rows, [btn("↩️ К списку", "staff")]]


async def sender_label(m) -> str:
    """Кто написал сообщение: ФИО и группа (для студента) или ФИО и MAX ID (для сотрудника)."""
    person = await (repo.get_user if m["sender_role"] == "student" else repo.get_admin)(m["sender_id"])
    if not person:
        return "🎓 Студент" if m["sender_role"] == "student" else "🏫 Сотрудник"
    tail = person["group_code"] if m["sender_role"] == "student" else f"ID {m['sender_id']}"
    return f"{person['full_name']} ({tail})"


async def ticket_text(t, staff_side: bool) -> str:
    lines = [f"📂 Обращение №{t['ticket_id']} · {STATUS.get(t['status'], t['status'])}",
             f"Категория: {CATS.get(t['category'], t['category'])}"]
    st = await repo.get_user(t["student_id"])
    ad = await admin_of(t["target_admin_id"])
    lines.append(f"Ответственный: {ad['full_name']} (ID {ad['user_id']})" if ad else "Ответственный: —")
    msgs = await repo.ticket_messages(t["ticket_id"])
    lines.append("")
    for m in reversed(msgs):
        icon = "🎓" if m["sender_role"] == "student" else "🏫"
        lines.append(f"{icon} {await sender_label(m)} ({m['sender_id']}): {short(m['text'], 700)}")
    return "\n".join(lines)


async def send_ticket(x: str, t, staff_side: bool):
    await common.api.send(x, await ticket_text(t, staff_side), ticket_kb(t, staff_side))


def ticket_rows_kb(rows):
    return [[btn(f"№{r['ticket_id']} · {STATUS[r['status']]} · {CATS[r['category']]}", f"t:{r['ticket_id']}")] for r in rows]


@callback("new")
async def cb_new_ticket(x, cat):
    if cat not in CATS or not await need_student(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await common.api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    rows = await repo.staff_for_category(cat)
    if not rows:
        return await common.api.send(x, "Сотрудники для этого раздела пока не назначены. Обратитесь в учебную часть.", BACK)
    kb = [[btn(short(r["full_name"], 60), f"pick:{cat}:{r['user_id']}")] for r in rows]
    await common.api.send(x, f"{CATS[cat]}\nВыберите сотрудника:", kb + BACK)


@callback("pick")
async def cb_pick_staff(x, arg):
    cat, _, admin_id = arg.partition(":")
    if cat not in CATS or not await need_student(x):
        return
    a = await admin_of(admin_id)
    if not a or is_super(a) or a["ticket_category"] not in (cat, "all"):
        return await common.api.send(x, "Этот сотрудник больше не принимает такие обращения. Выберите другого.", BACK)
    await db.set_state(x, "ticket", {"admin": admin_id, "cat": cat})
    await common.api.send(x, f"Кому: {a['full_name']}\nНапишите обращение одним сообщением (или /cancel для отмены).")


@state("ticket")
async def st_ticket(x, text, p):
    user = await need_student(x)
    if not user:
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        await db.clear_state(x)
        return await common.api.send(x, "Приём обращений временно отключён.", BACK)
    admin = await admin_of(p["admin"])
    if not admin:
        await db.clear_state(x)
        return await common.api.send(x, "Сотрудник больше недоступен. Начните заново.", BACK)
    text = text[:3000]
    tid = await repo.create_ticket(x, p["admin"], p["cat"], text)
    await db.clear_state(x)
    t = await repo.get_ticket(tid)
    delivered = await notify(
        p["admin"],
        f"🔔 Новое обращение №{tid}\nКатегория: {CATS[p['cat']]}\nОт: {user['full_name']} ({user['group_code']})\n\n{text}",
        ticket_kb(t, True),
    )
    note = "" if delivered else "\n⚠️ Сотрудник пока не запускал бота — уведомление не дошло, но обращение сохранено."
    await common.api.send(x, f"✅ Обращение №{tid} отправлено.{note}", [[btn("📂 Открыть", f"t:{tid}")], *BACK])


@callback("tickets")
async def cb_my_tickets(x, arg):
    if not await need_student(x):
        return
    rows = await repo.recent_student_tickets(x)
    if not rows:
        return await common.api.send(x, "У вас пока нет обращений.", BACK)
    await common.api.send(x, "📋 Мои обращения (последние 15). Нажмите на обращение, чтобы открыть переписку:",
                   ticket_rows_kb(rows) + BACK)


@callback("staff")
async def cb_staff_tickets(x, arg):
    a = await admin_of(x)
    if not a:
        return
    rows = await repo.admin_tickets(None if is_super(a) else x)
    if not rows:
        return await common.api.send(x, "Обращений нет.", BACK)
    await common.api.send(x, "📋 Обращения (сначала открытые, максимум 20):", ticket_rows_kb(rows) + BACK)


@callback("t")
async def cb_open_ticket(x, arg):
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t:
        return await common.api.send(x, "Обращение не найдено.", BACK)
    await send_ticket(x, t, staff_side)


@callback("rp")
async def cb_reply(x, arg):
    t, staff_side = await load_ticket(x, to_int(arg))
    if not t:
        return await common.api.send(x, "Обращение не найдено.", BACK)
    if not staff_side and t["status"] not in OPEN_STATUSES:
        return await common.api.send(x, "Обращение закрыто. Создайте новое через меню.", BACK)
    await db.set_state(x, "reply", {"tid": t["ticket_id"]})
    await common.api.send(x, f"Введите сообщение по обращению №{t['ticket_id']} (или /cancel).")


@state("reply")
async def st_reply(x, text, p):
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or (not staff_side and t["status"] not in OPEN_STATUSES):
        await db.clear_state(x)
        return await common.api.send(x, "Обращение недоступно или закрыто.", BACK)
    text = text[:3000]
    await db.clear_state(x)
    tid = t["ticket_id"]
    if staff_side:
        await repo.add_ticket_message(tid, x, "staff", text, "in_progress" if t["status"] == "new" else None)
        t = await repo.get_ticket(tid)
        await notify(t["student_id"], f"💬 Ответ по обращению №{tid}:\n\n{text}",
                     [[btn("✍️ Ответить", f"rp:{tid}"), btn("📂 Открыть", f"t:{tid}")]])
    else:
        await repo.add_ticket_message(tid, x, "student", text)
        user = await repo.get_user(x)
        await notify(
            t["target_admin_id"],
            f"💬 Новое сообщение по обращению №{tid}\nОт: {user['full_name']} ({user['group_code']})\n\n{text}",
            ticket_kb(t, True),
        )
    await common.api.send(x, f"✅ Сообщение по обращению №{tid} отправлено.", [[btn("📂 Открыть", f"t:{tid}")], *BACK])


@callback("st")
async def cb_status(x, arg):
    tid, _, status = arg.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side or status not in STATUS:
        return
    if t["status"] != status:
        await repo.set_ticket_status(t["ticket_id"], status)
        await notify(t["student_id"], f"🔔 Статус обращения №{t['ticket_id']}: {STATUS[status]}",
                     [[btn("📂 Открыть", f"t:{t['ticket_id']}")]])
        t = await repo.get_ticket(t["ticket_id"])
    await send_ticket(x, t, True)
