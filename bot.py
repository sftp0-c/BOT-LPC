"""Бот колледжа для мессенджера MAX.

Запуск: uvicorn bot:app --host 0.0.0.0 --port 8080
Режим определяется настройкой MAX_WEBHOOK_URL: задан — webhook, пусто — long polling.
"""
import asyncio
import hashlib
import hmac
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

import config
import database as db
from max_api import MaxAPI, btn, link_btn

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не пишем в лог каждый HTTP-запрос
log = logging.getLogger("bot")

api = MaxAPI()

DEFAULT_WELCOME = "🏫 Бот колледжа. Выберите действие:"
STATUS = {"new": "🆕 Новое", "in_progress": "🔧 В работе", "completed": "✅ Завершено", "rejected": "❌ Отклонено"}
OPEN_STATUSES = ("new", "in_progress")
CATS = {"feedback": "💬 Обратная связь", "certificates": "📄 Справка"}
STAFF_CATS = {"feedback": "💬 Обратная связь", "certificates": "📄 Справки", "all": "🔁 Всё"}
BACK = [[btn("↩️ В меню", "home")]]

# ───────────────────────── вспомогательное ─────────────────────────


def s(value) -> str:
    return "" if value is None else str(value)


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def norm_group(text: str) -> str:
    return " ".join(text.split()).upper()


def short(text: str, n: int) -> str:
    text = " ".join(s(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def admin_of(x: str):
    return await db.one("SELECT * FROM admins WHERE user_id=?", (x,))


def is_super(a) -> bool:
    return bool(a) and a["role_type"] == "superadmin"


def can_broadcast(a) -> bool:
    return bool(a) and (is_super(a) or bool(a["can_broadcast"]))


async def notify(user_id, text, keyboard=None) -> bool:
    """Отправка без падения: пользователь мог не запускать бота или заблокировать его."""
    try:
        await api.send(user_id, text, keyboard)
        return True
    except Exception as exc:
        log.warning("не удалось отправить сообщение %s: %s", user_id, exc)
        return False


_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


# ───────────────────────── меню ─────────────────────────


def student_menu():
    return [
        [btn("📅 Расписание", "sched"), btn("💬 Обратная связь", "new:feedback")],
        [btn("📄 Заказать справку", "new:certificates"), btn("📋 Мои обращения", "tickets")],
        [btn("👤 Профиль", "profile")],
    ]


def staff_menu(a):
    rows = [[btn("📋 Мои обращения", "staff")], [btn("📊 Статистика", "staffstats")]]
    if can_broadcast(a):
        rows.append([btn("📢 Рассылка", "broadcast")])
    return rows


def super_menu():
    return [
        [btn("📋 Все обращения", "staff"), btn("📊 Статистика", "stats")],
        [btn("👥 Сотрудники", "admins"), btn("📅 Расписания", "schedules")],
        [btn("📢 Рассылка", "broadcast"), btn("⚙️ Настройки", "settings")],
    ]


async def show_home(x: str):
    a = await admin_of(x)
    if is_super(a):
        return await api.send(x, "🔐 Панель superadmin", super_menu())
    if a:
        return await api.send(x, "🏫 Кабинет сотрудника", staff_menu(a))
    if not await db.one("SELECT 1 FROM users WHERE user_id=?", (x,)):
        return await start(x)
    return await api.send(x, await db.get_setting("welcome_text", DEFAULT_WELCOME), student_menu())


async def start(x: str):
    await db.clear_state(x)
    if await admin_of(x) or await db.one("SELECT 1 FROM users WHERE user_id=?", (x,)):
        return await show_home(x)
    await db.set_state(x, "reg_name")
    await api.send(x, "Здравствуйте! Это бот колледжа.\nУкажите ваши ФИО полностью, например: Иванов Иван Иванович.")


# ───────────────────────── диспетчеры ─────────────────────────

CALLBACKS: dict = {}
STATES: dict = {}


def callback(name: str):
    def deco(fn):
        CALLBACKS[name] = fn
        return fn

    return deco


def state(name: str):
    def deco(fn):
        STATES[name] = fn
        return fn

    return deco


async def on_message(x: str, text: str):
    cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    if cmd == "/start":
        return await start(x)
    if cmd == "/id":
        return await api.send(x, f"Ваш MAX ID: {x}")
    if cmd == "/cancel":
        await db.clear_state(x)
        return await show_home(x)
    if cmd == "/supersecret_admin":
        if is_super(await admin_of(x)):
            await db.clear_state(x)
            return await show_home(x)
        return  # для остальных команды «не существует»
    st = await db.get_state(x)
    if st and st["state"] in STATES:
        return await STATES[st["state"]](x, text, st["payload"])
    if not (await admin_of(x) or await db.one("SELECT 1 FROM users WHERE user_id=?", (x,))):
        return await start(x)
    await api.send(x, "Используйте кнопки меню. Команды: /start, /cancel, /id")
    return await show_home(x)


async def on_callback(x: str, payload: str):
    name, _, arg = payload.partition(":")
    handler = CALLBACKS.get(name)
    if not handler:
        return log.warning("неизвестный callback %r от %s", payload, x)
    if name != "bcgo":  # нажатие любой кнопки прерывает незавершённый ввод
        await db.clear_state(x)
    return await handler(x, arg)


# ───────────────────────── студент: регистрация и профиль ─────────────────────────


@state("reg_name")
async def st_reg_name(x, text, p):
    name = " ".join(text.split())
    if len(name.split()) < 2 or len(name) > 100:
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя), например: Иванов Иван Иванович.")
    await db.set_state(x, "reg_group", {"name": name})
    await api.send(x, "Укажите код вашей группы, например: ИС-21.")


@state("reg_group")
async def st_reg_group(x, text, p):
    group = norm_group(text)
    if not 1 <= len(group) <= 30:
        return await api.send(x, "Код группы — до 30 символов. Попробуйте ещё раз.")
    await db.run(
        "INSERT INTO users(user_id, full_name, group_code) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, group_code=excluded.group_code",
        (x, p["name"], group),
    )
    await db.clear_state(x)
    await api.send(x, f"✅ Регистрация завершена: {p['name']}, группа {group}.")
    await show_home(x)


async def need_student(x: str):
    user = await db.one("SELECT * FROM users WHERE user_id=?", (x,))
    if not user:
        await start(x)
    return user


@callback("home")
async def cb_home(x, arg):
    await show_home(x)


@callback("profile")
async def cb_profile(x, arg):
    user = await need_student(x)
    if user:
        await api.send(
            x,
            f"👤 Профиль\nФИО: {user['full_name']}\nГруппа: {user['group_code']}",
            [[btn("✏️ Изменить ФИО", "pf:name"), btn("✏️ Изменить группу", "pf:group")], *BACK],
        )


@callback("pf")
async def cb_profile_edit(x, arg):
    if not await need_student(x):
        return
    if arg == "name":
        await db.set_state(x, "edit_name")
        await api.send(x, "Введите новые ФИО (или /cancel).")
    elif arg == "group":
        await db.set_state(x, "edit_group")
        await api.send(x, "Введите новый код группы (или /cancel).")


@state("edit_name")
async def st_edit_name(x, text, p):
    name = " ".join(text.split())
    if len(name.split()) < 2 or len(name) > 100:
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя).")
    await db.run("UPDATE users SET full_name=? WHERE user_id=?", (name, x))
    await db.clear_state(x)
    await cb_profile(x, "")


@state("edit_group")
async def st_edit_group(x, text, p):
    group = norm_group(text)
    if not 1 <= len(group) <= 30:
        return await api.send(x, "Код группы — до 30 символов. Попробуйте ещё раз.")
    await db.run("UPDATE users SET group_code=? WHERE user_id=?", (group, x))
    await db.clear_state(x)
    await cb_profile(x, "")


@callback("sched")
async def cb_schedule(x, arg):
    user = await need_student(x)
    if not user:
        return
    row = await db.one("SELECT pdf_url FROM schedules WHERE group_code=?", (user["group_code"],))
    if not row:
        return await api.send(x, f"Расписание группы {user['group_code']} пока не добавлено.", BACK)
    await api.send(
        x, f"📅 Расписание группы {user['group_code']}:\n{row['pdf_url']}", [[link_btn("Открыть расписание", row["pdf_url"])], *BACK]
    )


# ───────────────────────── обращения ─────────────────────────


async def load_ticket(x: str, ticket_id: int):
    """→ (обращение, сторона_сотрудника). (None, False), если обращения нет или доступа нет."""
    t = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))
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


async def ticket_text(t, staff_side: bool) -> str:
    lines = [f"📂 Обращение №{t['ticket_id']} · {STATUS.get(t['status'], t['status'])}", f"Категория: {CATS.get(t['category'], t['category'])}"]
    if staff_side:
        st = await db.one("SELECT full_name, group_code FROM users WHERE user_id=?", (t["student_id"],))
        lines.append(f"Студент: {st['full_name']} ({st['group_code']})" if st else "Студент: —")
    else:
        ad = await admin_of(t["target_admin_id"])
        lines.append(f"Сотрудник: {ad['full_name'] if ad else '—'}")
    msgs = await db.many("SELECT sender_role, text FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 10", (t["ticket_id"],))
    lines.append("")
    for m in reversed(msgs):
        who = "🎓 Студент" if m["sender_role"] == "student" else "🏫 Сотрудник"
        lines.append(f"{who}: {short(m['text'], 700)}")
    return "\n".join(lines)


async def send_ticket(x: str, t, staff_side: bool):
    await api.send(x, await ticket_text(t, staff_side), ticket_kb(t, staff_side))


@callback("new")
async def cb_new_ticket(x, cat):
    if cat not in CATS or not await need_student(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(x, "Приём обращений временно отключён. Попробуйте позже.", BACK)
    rows = await db.many(
        "SELECT user_id, full_name FROM admins WHERE role_type!='superadmin' AND (ticket_category=? OR ticket_category='all') ORDER BY full_name",
        (cat,),
    )
    if not rows:
        return await api.send(x, "Сотрудники для этого раздела пока не назначены. Обратитесь в учебную часть.", BACK)
    await api.send(x, f"{CATS[cat]}\nВыберите сотрудника:", [[btn(short(r["full_name"], 60), f"pick:{cat}:{r['user_id']}")] for r in rows[:25]] + BACK)


@callback("pick")
async def cb_pick_staff(x, arg):
    cat, _, admin_id = arg.partition(":")
    if cat not in CATS or not await need_student(x):
        return
    a = await admin_of(admin_id)
    if not a or is_super(a) or a["ticket_category"] not in (cat, "all"):
        return await api.send(x, "Этот сотрудник больше не принимает такие обращения. Выберите другого.", BACK)
    await db.set_state(x, "ticket", {"admin": admin_id, "cat": cat})
    await api.send(x, f"Кому: {a['full_name']}\nНапишите обращение одним сообщением (или /cancel для отмены).")


@state("ticket")
async def st_ticket(x, text, p):
    user = await need_student(x)
    if not user:
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        await db.clear_state(x)
        return await api.send(x, "Приём обращений временно отключён.", BACK)
    admin = await admin_of(p["admin"])
    if not admin:
        await db.clear_state(x)
        return await api.send(x, "Сотрудник больше недоступен. Начните заново.", BACK)
    text = text[:3000]
    tid = await db.create_ticket(x, p["admin"], p["cat"], text)
    await db.clear_state(x)
    t = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (tid,))
    delivered = await notify(
        p["admin"],
        f"🔔 Новое обращение №{tid}\nКатегория: {CATS[p['cat']]}\nОт: {user['full_name']} ({user['group_code']})\n\n{text}",
        ticket_kb(t, True),
    )
    note = "" if delivered else "\n⚠️ Сотрудник пока не запускал бота — уведомление не дошло, но обращение сохранено."
    await api.send(x, f"✅ Обращение №{tid} отправлено.{note}", [[btn("📂 Открыть", f"t:{tid}")], *BACK])


@callback("tickets")
async def cb_my_tickets(x, arg):
    if not await need_student(x):
        return
    rows = await db.many("SELECT * FROM tickets WHERE student_id=? ORDER BY ticket_id DESC LIMIT 15", (x,))
    if not rows:
        return await api.send(x, "У вас пока нет обращений.", BACK)
    kb = [[btn(f"№{r['ticket_id']} · {STATUS[r['status']]} · {CATS[r['category']]}", f"t:{r['ticket_id']}")] for r in rows]
    await api.send(x, "📋 Мои обращения (последние 15). Нажмите на обращение, чтобы открыть переписку:", kb + BACK)


@callback("staff")
async def cb_staff_tickets(x, arg):
    a = await admin_of(x)
    if not a:
        return
    where, params = ("", ()) if is_super(a) else ("WHERE target_admin_id=?", (x,))
    rows = await db.many(
        f"SELECT * FROM tickets {where} ORDER BY (status IN ('completed','rejected')), ticket_id DESC LIMIT 20", params
    )
    if not rows:
        return await api.send(x, "Обращений нет.", BACK)
    kb = [[btn(f"№{r['ticket_id']} · {STATUS[r['status']]} · {CATS[r['category']]}", f"t:{r['ticket_id']}")] for r in rows]
    await api.send(x, "📋 Обращения (сначала открытые, максимум 20):", kb + BACK)


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


@state("reply")
async def st_reply(x, text, p):
    t, staff_side = await load_ticket(x, to_int(p.get("tid")))
    if not t or (not staff_side and t["status"] not in OPEN_STATUSES):
        await db.clear_state(x)
        return await api.send(x, "Обращение недоступно или закрыто.", BACK)
    text = text[:3000]
    await db.clear_state(x)
    tid = t["ticket_id"]
    if staff_side:
        await db.add_ticket_message(tid, x, "staff", text, "in_progress" if t["status"] == "new" else None)
        t = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (tid,))
        await notify(t["student_id"], f"💬 Ответ по обращению №{tid}:\n\n{text}", [[btn("✍️ Ответить", f"rp:{tid}"), btn("📂 Открыть", f"t:{tid}")]])
    else:
        await db.add_ticket_message(tid, x, "student", text)
        user = await db.one("SELECT full_name, group_code FROM users WHERE user_id=?", (x,))
        await notify(
            t["target_admin_id"],
            f"💬 Новое сообщение по обращению №{tid}\nОт: {user['full_name']} ({user['group_code']})\n\n{text}",
            ticket_kb(t, True),
        )
    await api.send(x, f"✅ Сообщение по обращению №{tid} отправлено.", [[btn("📂 Открыть", f"t:{tid}")], *BACK])


@callback("st")
async def cb_status(x, arg):
    tid, _, status = arg.partition(":")
    t, staff_side = await load_ticket(x, to_int(tid))
    if not t or not staff_side or status not in STATUS:
        return
    if t["status"] != status:
        await db.set_ticket_status(t["ticket_id"], status)
        await notify(t["student_id"], f"🔔 Статус обращения №{t['ticket_id']}: {STATUS[status]}", [[btn("📂 Открыть", f"t:{t['ticket_id']}")]])
    t = await db.one("SELECT * FROM tickets WHERE ticket_id=?", (t["ticket_id"],))
    await send_ticket(x, t, True)


# ───────────────────────── статистика ─────────────────────────


async def status_counts(admin_id: str | None = None) -> str:
    where, params = ("WHERE target_admin_id=?", (admin_id,)) if admin_id else ("", ())
    rows = await db.many(f"SELECT status, COUNT(*) n FROM tickets {where} GROUP BY status", params)
    counts = {r["status"]: r["n"] for r in rows}
    return "\n".join(f"{label}: {counts.get(code, 0)}" for code, label in STATUS.items())


@callback("stats")
async def cb_stats(x, arg):
    if not is_super(await admin_of(x)):
        return
    students = (await db.one("SELECT COUNT(*) n FROM users"))["n"]
    staff = (await db.one("SELECT COUNT(*) n FROM admins WHERE role_type!='superadmin'"))["n"]
    week = (await db.one("SELECT COUNT(*) n FROM tickets WHERE created_at >= datetime('now','-7 days')"))["n"]
    total = (await db.one("SELECT COUNT(*) n FROM tickets"))["n"]
    await api.send(
        x, f"📊 Статистика\nСтудентов: {students}\nСотрудников: {staff}\nОбращений всего: {total} (за 7 дней: {week})\n\n{await status_counts()}", BACK
    )


@callback("staffstats")
async def cb_staff_stats(x, arg):
    if not await admin_of(x):
        return
    await api.send(x, f"📊 Ваши обращения\n{await status_counts(x)}", BACK)


# ───────────────────────── superadmin: сотрудники ─────────────────────────


async def need_super(x: str):
    a = await admin_of(x)
    return a if is_super(a) else None


@callback("admins")
async def cb_admins(x, arg):
    if not await need_super(x):
        return
    rows = await db.many("SELECT * FROM admins WHERE role_type!='superadmin' ORDER BY full_name")
    text = "👥 Сотрудники колледжа" if rows else "👥 Сотрудников пока нет. Добавьте первого."
    kb = [[btn(f"{short(r['full_name'], 40)} · {STAFF_CATS[r['ticket_category']]}", f"sf:{r['user_id']}")] for r in rows[:25]]
    await api.send(x, text, [*kb, [btn("➕ Добавить сотрудника", "sfadd")], *BACK])


async def send_staff_card(x: str, staff_id: str):
    a = await admin_of(staff_id)
    if not a or is_super(a):
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    text = (
        f"👤 {a['full_name']}\nMAX ID: {a['user_id']}\nОбращения: {STAFF_CATS[a['ticket_category']]}\n"
        f"Рассылка: {'разрешена' if a['can_broadcast'] else 'запрещена'}"
    )
    cat_row = [btn(("● " if code == a["ticket_category"] else "") + label, f"sfc:{staff_id}:{code}") for code, label in STAFF_CATS.items()]
    await api.send(
        x,
        text,
        [
            cat_row[:2],
            cat_row[2:],
            [btn("📢 Рассылка: " + ("запретить" if a["can_broadcast"] else "разрешить"), f"sfb:{staff_id}")],
            [btn("🗑 Удалить", f"sfd:{staff_id}"), btn("↩️ К списку", "admins")],
        ],
    )


@callback("sf")
async def cb_staff_card(x, arg):
    if await need_super(x):
        await send_staff_card(x, arg)


@callback("sfc")
async def cb_staff_category(x, arg):
    staff_id, _, cat = arg.partition(":")
    a = await admin_of(staff_id)
    if await need_super(x) and a and not is_super(a) and cat in STAFF_CATS:
        await db.run("UPDATE admins SET ticket_category=? WHERE user_id=?", (cat, staff_id))
        await send_staff_card(x, staff_id)


@callback("sfb")
async def cb_staff_broadcast(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await db.run("UPDATE admins SET can_broadcast=? WHERE user_id=?", (0 if a["can_broadcast"] else 1, arg))
        await send_staff_card(x, arg)


@callback("sfd")
async def cb_staff_delete_ask(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await api.send(x, f"Удалить сотрудника {a['full_name']}?", [[btn("🗑 Да, удалить", f"sfdy:{arg}"), btn("Отмена", f"sf:{arg}")]])


@callback("sfdy")
async def cb_staff_delete(x, arg):
    a = await admin_of(arg)
    if not (await need_super(x) and a and not is_super(a)):
        return
    open_n = (await db.one("SELECT COUNT(*) n FROM tickets WHERE target_admin_id=? AND status IN ('new','in_progress')", (arg,)))["n"]
    if open_n:
        return await api.send(
            x, f"У сотрудника есть открытые обращения ({open_n}). Закройте их (superadmin может менять статусы) и повторите.", [[btn("↩️ К списку", "admins")]]
        )
    await db.run("DELETE FROM admins WHERE user_id=?", (arg,))
    await api.send(x, "✅ Сотрудник удалён.", [[btn("↩️ К списку", "admins")]])


@callback("sfadd")
async def cb_staff_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "add_staff_id")
    await api.send(x, "Введите MAX ID сотрудника (цифры). Сотрудник может узнать свой ID, написав боту /id. (или /cancel)")


@state("add_staff_id")
async def st_add_staff_id(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    staff_id = text.strip()
    if not staff_id.isdigit():
        return await api.send(x, "ID состоит только из цифр. Попробуйте ещё раз.")
    existing = await admin_of(staff_id)
    if existing:
        await db.clear_state(x)
        return await api.send(x, "Этот пользователь уже в списке сотрудников/superadmin.", [[btn("↩️ К списку", "admins")]])
    await db.set_state(x, "add_staff_name", {"id": staff_id})
    await api.send(x, "Введите ФИО сотрудника (как его увидят студенты).")


@state("add_staff_name")
async def st_add_staff_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    name = short(text, 100)
    if not name:
        return await api.send(x, "Введите ФИО сотрудника.")
    await db.run("INSERT OR IGNORE INTO admins(user_id, full_name, role_type) VALUES(?,?, 'staff')", (p["id"], name))
    await db.clear_state(x)
    delivered = await notify(p["id"], "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, чтобы открыть кабинет.")
    await api.send(x, "✅ Сотрудник добавлен." + ("" if delivered else "\n⚠️ Он пока не запускал бота — пусть нажмёт «Начать»."))
    await send_staff_card(x, p["id"])


# ───────────────────────── superadmin: расписания ─────────────────────────


@callback("schedules")
async def cb_schedules(x, arg):
    if not await need_super(x):
        return
    rows = await db.many("SELECT group_code FROM schedules ORDER BY group_code")
    text = f"📅 Расписания групп: {len(rows)}" if rows else "📅 Расписаний пока нет."
    kb = [[btn(r["group_code"], f"sc:{r['group_code']}")] for r in rows[:25]]
    await api.send(x, text + ("\n(показаны первые 25)" if len(rows) > 25 else ""), [*kb, [btn("➕ Добавить / изменить", "scadd")], *BACK])


@callback("sc")
async def cb_schedule_card(x, group):
    row = await db.one("SELECT * FROM schedules WHERE group_code=?", (group,))
    if not (await need_super(x) and row):
        return
    await api.send(
        x,
        f"📅 {row['group_code']}\n{row['pdf_url']}",
        [[btn("✏️ Изменить ссылку", f"scedit:{group}"), btn("🗑 Удалить", f"scdel:{group}")], [btn("↩️ К списку", "schedules")]],
    )


@callback("scdel")
async def cb_schedule_delete(x, group):
    if await need_super(x):
        await db.run("DELETE FROM schedules WHERE group_code=?", (group,))
        await cb_schedules(x, "")


@callback("scadd")
async def cb_schedule_add(x, arg):
    if await need_super(x):
        await db.set_state(x, "sc_group")
        await api.send(x, "Введите код группы, например ИС-21 (или /cancel).")


@callback("scedit")
async def cb_schedule_edit(x, group):
    if await need_super(x):
        await db.set_state(x, "sc_url", {"group": group})
        await api.send(x, f"Отправьте новую ссылку на расписание группы {group} (http/https).")


@state("sc_group")
async def st_sc_group(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(text)
    if not 1 <= len(group) <= 30:
        return await api.send(x, "Код группы — до 30 символов.")
    await db.set_state(x, "sc_url", {"group": group})
    await api.send(x, f"Отправьте ссылку на расписание группы {group} (http/https).")


@state("sc_url")
async def st_sc_url(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    url = text.strip()
    if not url.lower().startswith(("http://", "https://")) or " " in url:
        return await api.send(x, "Нужна ссылка вида https://… Попробуйте ещё раз.")
    await db.run(
        "INSERT INTO schedules(group_code, pdf_url) VALUES(?,?) ON CONFLICT(group_code) DO UPDATE SET pdf_url=excluded.pdf_url",
        (p["group"], url),
    )
    await db.clear_state(x)
    await api.send(x, f"✅ Расписание группы {p['group']} сохранено.")
    await cb_schedules(x, "")


# ───────────────────────── superadmin: настройки ─────────────────────────


async def send_settings(x: str):
    enabled = await db.get_setting("tickets_enabled", "1") == "1"
    welcome = await db.get_setting("welcome_text", DEFAULT_WELCOME)
    await api.send(
        x,
        f"⚙️ Настройки\n\nПриём обращений: {'включён' if enabled else 'выключен'}\nПриветствие студентов:\n{welcome}",
        [
            [btn("Приём обращений: " + ("выключить" if enabled else "включить"), "set:tickets")],
            [btn("✏️ Изменить приветствие", "set:welcome")],
            *BACK,
        ],
    )


@callback("settings")
async def cb_settings(x, arg):
    if await need_super(x):
        await send_settings(x)


@callback("set")
async def cb_set(x, arg):
    if not await need_super(x):
        return
    if arg == "tickets":
        enabled = await db.get_setting("tickets_enabled", "1") == "1"
        await db.set_setting("tickets_enabled", "0" if enabled else "1")
        await send_settings(x)
    elif arg == "welcome":
        await db.set_state(x, "set_welcome")
        await api.send(x, "Отправьте новый текст приветствия студентов (до 500 символов) или /cancel.")


@state("set_welcome")
async def st_set_welcome(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    await db.set_setting("welcome_text", text.strip()[:500] or DEFAULT_WELCOME)
    await db.clear_state(x)
    await send_settings(x)


# ───────────────────────── рассылка ─────────────────────────


async def audience(aud: str):
    if aud == "all":
        return await db.many("SELECT user_id FROM users")
    return await db.many("SELECT user_id FROM users WHERE group_code=?", (aud,))


@callback("broadcast")
async def cb_broadcast(x, arg):
    if not can_broadcast(await admin_of(x)):
        return await api.send(x, "У вас нет права на рассылку.", BACK)
    await api.send(x, "📢 Кому отправить?", [[btn("👥 Всем студентам", "bcaud:all")], [btn("🎓 Одной группе", "bcaud:group")], *BACK])


@callback("bcaud")
async def cb_broadcast_audience(x, arg):
    if not can_broadcast(await admin_of(x)):
        return
    if arg == "group":
        await db.set_state(x, "bc_group")
        return await api.send(x, "Введите код группы (или /cancel).")
    await db.set_state(x, "bc_text", {"aud": "all"})
    await api.send(x, "Введите текст рассылки для всех студентов (или /cancel).")


@state("bc_group")
async def st_bc_group(x, text, p):
    if not can_broadcast(await admin_of(x)):
        return await db.clear_state(x)
    group = norm_group(text)
    n = len(await audience(group))
    if not n:
        return await api.send(x, f"В группе «{group}» нет зарегистрированных студентов. Проверьте код и введите ещё раз.")
    await db.set_state(x, "bc_text", {"aud": group})
    await api.send(x, f"Группа {group}: получателей {n}. Введите текст рассылки (или /cancel).")


@state("bc_text")
async def st_bc_text(x, text, p):
    if not can_broadcast(await admin_of(x)):
        return await db.clear_state(x)
    text = text.strip()[:3500]
    n = len(await audience(p["aud"]))
    await db.set_state(x, "bc_confirm", {"aud": p["aud"], "text": text})
    where = "всем студентам" if p["aud"] == "all" else f"группе {p['aud']}"
    await api.send(x, f"Отправить {where} ({n} чел.)?\n\n📢 Объявление колледжа:\n\n{text}", [[btn("✅ Отправить", "bcgo"), btn("❌ Отмена", "home")]])


@state("bc_confirm")
async def st_bc_confirm(x, text, p):
    await api.send(x, "Нажмите «✅ Отправить» или «❌ Отмена» под сообщением с текстом рассылки (либо /cancel).")


@callback("bcgo")
async def cb_broadcast_go(x, arg):
    st = await db.get_state(x)
    if not (can_broadcast(await admin_of(x)) and st and st["state"] == "bc_confirm"):
        return await api.send(x, "Нет подготовленной рассылки. Начните заново.", BACK)
    await db.clear_state(x)
    spawn(run_broadcast(x, st["payload"]["aud"], st["payload"]["text"]))
    await api.send(x, "⏳ Рассылка запущена, пришлю итог по завершении.")


async def run_broadcast(sender: str, aud: str, text: str):
    sent = failed = 0
    try:
        for r in await audience(aud):
            if await notify(r["user_id"], "📢 Объявление колледжа:\n\n" + text):
                sent += 1
            else:
                failed += 1
    finally:
        await db.run("INSERT INTO broadcasts(sender_id, audience, text, sent, failed) VALUES(?,?,?,?,?)", (sender, aud, text, sent, failed))
        await notify(sender, f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.", BACK)


# ───────────────────────── обработка обновлений ─────────────────────────

_locks: defaultdict = defaultdict(asyncio.Lock)  # один пользователь — один обработчик за раз


def update_key(u: dict):
    """Уникальный «отпечаток» события для защиты от дублей доставки.

    MAX доставляет события с гарантией «как минимум один раз»: long polling
    может переотдать события после обрыва соединения, webhook — повторить по
    своей политике ретраев, а два процесса бота могут забрать одно событие
    одновременно. Возвращает ключ, на котором бот делает дедупликацию, или
    None, если отпечаток построить нельзя (событие пропустит дедупликацию).
    """
    kind = u.get("update_type")
    if kind == "message_callback":
        cid = ((u.get("callback") or {}).get("callback_id"))
        return f"cb:{cid}" if cid else None
    if kind == "message_created":
        m = u.get("message") or {}
        ts = m.get("timestamp") or u.get("timestamp")
        chat = (m.get("recipient") or {}).get("chat_id")
        uid = (m.get("sender") or {}).get("user_id")
        text = (m.get("body") or {}).get("text")
        if ts is None or chat is None or uid is None:
            return None
        digest = hashlib.sha1(s(text).encode("utf-8", "ignore")).hexdigest()[:16]
        return f"mc:{chat}:{ts}:{uid}:{digest}"
    if kind == "bot_started":
        ts = u.get("timestamp")
        uid = (u.get("user") or {}).get("user_id")
        return f"bs:{uid}:{ts}" if ts is not None and uid is not None else None
    return None


def sender_id(u: dict) -> str:
    """ID пользователя, который действовал.

    В message_callback поле message.sender — это сам бот (он отправил сообщение с кнопкой),
    настоящий пользователь лежит в callback.user.
    """
    kind = u.get("update_type")
    if kind == "message_callback":
        return s(((u.get("callback") or {}).get("user") or {}).get("user_id"))
    if kind == "message_created":
        sender = (u.get("message") or {}).get("sender") or {}
        return "" if sender.get("is_bot") else s(sender.get("user_id"))
    return s((u.get("user") or {}).get("user_id"))


async def process(u: dict):
    key = update_key(u)
    if key:
        try:
            if not await db.mark_processed(key):
                log.debug("Пропускаю повторную доставку события %s", key)
                return
        except Exception as exc:  # сбой базы не должен ронять обработку события
            log.warning("не удалось отметить событие %s: %s", key, exc)
    x = ""
    try:
        kind = u.get("update_type")
        x = sender_id(u)
        if not x:
            return
        async with _locks[x]:
            if kind == "bot_started":
                await start(x)
            elif kind == "message_created":
                m = u.get("message") or {}
                if (m.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
                    return  # группы и каналы не обслуживаем
                text = s((m.get("body") or {}).get("text")).strip()
                if not text:
                    return await api.send(x, "Пока я понимаю только текстовые сообщения.")
                await on_message(x, text)
            elif kind == "message_callback":
                cb = u.get("callback") or {}
                if cb.get("callback_id"):
                    try:
                        await api.answer(cb["callback_id"])
                    except Exception as exc:
                        log.debug("answer не удался: %s", exc)
                await on_callback(x, s(cb.get("payload")))
    except Exception:
        log.exception("ошибка обработки обновления")
        if x:
            await notify(x, "⚠️ Что-то пошло не так. Попробуйте ещё раз или отправьте /start.")


async def poll():
    marker = None
    while True:
        try:
            data = await api.updates(marker)
            marker = data.get("marker", marker)
            for u in data.get("updates", []):
                spawn(process(u))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("long polling: %s", exc)
            await asyncio.sleep(5)


# ───────────────────────── FastAPI ─────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    for warning in config.validate():
        log.warning(warning)
    await db.init_db()
    poller = None
    if config.WEBHOOK_URL:
        try:  # удаляем старую подписку с этим URL, чтобы при рестартах не было двойной доставки
            for sub in await api.subscriptions():
                if sub.get("url") == config.WEBHOOK_URL:
                    await api.unsubscribe(config.WEBHOOK_URL)
                    log.info("Удалена прежняя подписка %s (защита от дублей событий)", config.WEBHOOK_URL)
                    break
        except Exception as exc:
            log.warning("не удалось проверить/очистить подписки: %s", exc)
        await api.subscribe(config.WEBHOOK_URL, config.WEBHOOK_SECRET)
        log.info("Webhook зарегистрирован: %s", config.WEBHOOK_URL)
    else:
        try:
            if await api.subscriptions():
                log.warning("У бота есть webhook-подписка: long polling не получит события, пока её не удалить (DELETE /subscriptions).")
        except Exception as exc:
            log.warning("не удалось проверить подписки: %s", exc)
        poller = spawn(poll())
        log.info("Запущен long polling")
    yield
    if poller:
        poller.cancel()
    await api.close()


app = FastAPI(title="College MAX bot", lifespan=lifespan)


@app.get("/health")
async def health():
    await db.one("SELECT 1")
    return {"ok": True, "platform": "MAX", "mode": "webhook" if config.WEBHOOK_URL else "polling"}


@app.post("/webhook")
async def webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
    if not config.WEBHOOK_URL:  # в режиме long polling принимать входящие запросы нельзя — их можно подделать
        raise HTTPException(status_code=404)
    if not hmac.compare_digest(s(x_max_bot_api_secret).encode(), config.WEBHOOK_SECRET.encode()):
        raise HTTPException(status_code=401, detail="bad secret")
    spawn(process(await request.json()))
    return {"ok": True}
