"""Панель сис-админа: сотрудники, расписания групп, настройки, статистика."""
import database as db
import repository as repo
from handlers.common import BACK, DEFAULT_WELCOME, admin_of, api, is_super, need_super, notify
from handlers.registry import callback, state
from max_api import btn
from utils import STAFF_CATS, STATUS, norm_group, short


# ── статистика ────────────────────────────────────────────────────────────────
def status_counts_line(counts: dict) -> str:
    return "\n".join(f"{label}: {counts.get(code, 0)}" for code, label in STATUS.items())


@callback("stats")
async def cb_stats(x, arg):
    if not await need_super(x):
        return
    st = await repo.stats_overview()
    counts = status_counts_line(await repo.status_counts())
    await api.send(
        x,
        f"📊 Статистика\nСтудентов: {st['students']}\nСотрудников: {st['staff']}\n"
        f"Обращений всего: {st['total']} (за 7 дней: {st['week']})\n\n{counts}",
        BACK,
    )


@callback("staffstats")
async def cb_staff_stats(x, arg):
    if not await admin_of(x):
        return
    counts = status_counts_line(await repo.status_counts(x))
    await api.send(x, f"📊 Ваши обращения\n{counts}", BACK)


# ── сотрудники ────────────────────────────────────────────────────────────────
@callback("admins")
async def cb_admins(x, arg):
    if not await need_super(x):
        return
    rows = await repo.list_staff()
    text = "👥 Сотрудники колледжа" if rows else "👥 Сотрудников пока нет. Добавьте первого."
    kb = [[btn(f"{short(r['full_name'], 30)} · ID {r['user_id']} · {STAFF_CATS[r['ticket_category']]}", f"sf:{r['user_id']}")] for r in rows]
    await api.send(x, text, [*kb, [btn("➕ Добавить сотрудника", "sfadd")], *BACK])


async def send_staff_card(x: str, staff_id: str):
    a = await admin_of(staff_id)
    if not a or is_super(a):
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    text = (
        f"👤 {a['full_name']}\nMAX ID: {a['user_id']}\nОбращения: {STAFF_CATS[a['ticket_category']]}\n"
        f"Рассылка: {'разрешена' if a['can_broadcast'] else 'запрещена'}"
    )
    cat_row = [btn(("● " if code == a["ticket_category"] else "") + label, f"sfc:{staff_id}:{code}")
               for code, label in STAFF_CATS.items()]
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
        await repo.set_staff_category(staff_id, cat)
        await send_staff_card(x, staff_id)


@callback("sfb")
async def cb_staff_broadcast(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await repo.set_staff_broadcast(arg, not a["can_broadcast"])
        await send_staff_card(x, arg)


@callback("sfd")
async def cb_staff_delete_ask(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await api.send(x, f"Удалить сотрудника {a['full_name']}?",
                       [[btn("🗑 Да, удалить", f"sfdy:{arg}"), btn("Отмена", f"sf:{arg}")]])


@callback("sfdy")
async def cb_staff_delete(x, arg):
    a = await admin_of(arg)
    if not (await need_super(x) and a and not is_super(a)):
        return
    open_n = await repo.open_tickets_count(arg)
    if open_n:
        return await api.send(
            x,
            f"У сотрудника есть открытые обращения ({open_n}). Закройте их (сис-админ может менять статусы) и повторите.",
            [[btn("↩️ К списку", "admins")]],
        )
    await repo.delete_staff(arg)
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
    if await admin_of(staff_id):
        await db.clear_state(x)
        return await api.send(x, "Этот пользователь уже в списке сотрудников/сис-админов.", [[btn("↩️ К списку", "admins")]])
    await db.set_state(x, "add_staff_name", {"id": staff_id})
    await api.send(x, "Введите ФИО сотрудника (как его увидят студенты).")


@state("add_staff_name")
async def st_add_staff_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    name = short(text, 100)
    if not name:
        return await api.send(x, "Введите ФИО сотрудника.")
    await repo.add_staff(p["id"], name)
    await db.clear_state(x)
    delivered = await notify(p["id"], "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, чтобы открыть кабинет.")
    await api.send(x, "✅ Сотрудник добавлен." + ("" if delivered else "\n⚠️ Он пока не запускал бота — пусть нажмёт «Начать»."))
    await send_staff_card(x, p["id"])


# ── расписания ────────────────────────────────────────────────────────────────
@callback("schedules")
async def cb_schedules(x, arg):
    if not await need_super(x):
        return
    rows = await repo.schedule_groups()
    text = f"📅 Расписания групп: {len(rows)}" if rows else "📅 Расписаний пока нет."
    kb = [[btn(r["group_code"], f"sc:{r['group_code']}")] for r in rows]
    await api.send(x, text, [*kb, [btn("➕ Добавить / изменить", "scadd")], *BACK])


@callback("sc")
async def cb_schedule_card(x, group):
    row = await repo.get_schedule(group)
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
        await repo.delete_schedule(group)
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


def is_http_url(text: str) -> bool:
    url = text.strip()
    return url.lower().startswith(("http://", "https://")) and " " not in url


@state("sc_url")
async def st_sc_url(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    if not is_http_url(text):
        return await api.send(x, "Нужна ссылка вида https://… Попробуйте ещё раз.")
    await repo.upsert_schedule(p["group"], text.strip())
    await db.clear_state(x)
    await api.send(x, f"✅ Расписание группы {p['group']} сохранено.")
    await cb_schedules(x, "")


# ── настройки ─────────────────────────────────────────────────────────────────
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
