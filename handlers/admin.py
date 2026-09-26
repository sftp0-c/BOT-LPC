"""Панель сис-админа: сотрудники, справочник групп, расписания, настройки, статистика."""
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

import config
import database as db
import repository as repo
from handlers.common import BACK, DEFAULT_WELCOME, admin_of, api, is_super, log, need_super, notify, spawn
from handlers.registry import callback, state
from max_api import btn
from utils import (CODE_TTL_CHOICES, POSITION_HINTS, STAFF_CATS, STATUS, as_str, fmt_when, gen_code, is_sysadmin_role,
                   norm_group, profile_url, short, tail_file, to_int, ttl_label, valid_group)


# ── общие мелочи для строк из БД ─────────────────────────────────────────────
def _field(row, *names: str, default: str = "") -> str:
    for name in names:
        try:
            value = row[name]
        except (KeyError, IndexError, TypeError):
            continue
        if value is not None:
            return as_str(value)
    return default


def _flag(value) -> bool:
    return as_str(value).strip().lower() in ("1", "true", "yes", "on")


def staff_id_of(a) -> str:
    return _field(a, "id", "user_id", "admin_id")


def role_of(a) -> str:
    return _field(a, "role", "role_type", "position")


def office_of(a) -> str:
    return _field(a, "office", "room")


def department_of(a) -> str:
    return _field(a, "department", "unit", "subdivision")


# ── должности сотрудников ────────────────────────────────────────────────────
STAFF_ROLES = {
    "director": "👔 Директор",
    "deputy_uvr": "👥 Заместитель директора по УВР",
    "deputy_upr": "👥 Заместитель директора по УПР",
    "deputy_unr": "👥 Заместитель директора по УНР",
    "social_pedagogue": "🧑‍🏫 Социальный педагог",
}
ROLE_PRESET = "📋 Тип должности"


def position_text(a) -> str:
    """Должность сотрудника: свободный текст из карточки, иначе подпись по типу роли."""
    free = _field(a, "position")
    if free:
        return free
    return STAFF_ROLES.get(_field(a, "role"), "не назначена")


def role_text(a) -> str:
    return position_text(a)


def staff_signature(a) -> str:
    """«ФИО, должность» — подпись под рассылкой, в тикетах и в списках."""
    name = _field(a, "full_name") or "Сотрудник"
    position = position_text(a)
    office = office_of(a)
    if position == "не назначена":
        return name
    tail = f", каб. {office}" if office else ""
    return f"{name}, {position}{tail}"


def sysadmin_ids() -> list[str]:
    return [str(i) for i in (config.SYSADMIN_IDS or [])]


async def audit(actor: str, text: str) -> None:
    for uid in sysadmin_ids():
        if uid != str(actor):
            await notify(uid, f"🔔 {text}")


async def notify_schedule_subscribers(group: str, text: str) -> None:
    getter = getattr(repo, "schedule_subscribers", None)
    if getter is None:
        return
    try:
        subscribers = await getter(norm_group(as_str(group)))
    except Exception:
        return
    for user_id in subscribers or []:
        try:
            await notify(user_id, text)
        except Exception:
            continue


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
def staff_list_kb(rows) -> list:
    """Кнопки списка сотрудников, сгруппированные по отделам: заголовок отдела, потом люди."""
    departments: dict[str, list] = {}
    for row in rows:
        departments.setdefault(_field(row, "department") or "Без отдела", []).append(row)
    keyboard: list = []
    for department in sorted(departments, key=lambda name: (name == "Без отдела", name)):
        members = departments[department]
        keyboard.append([btn(f"🏛 {department} — {len(members)}", f"sdep:{short(department, 30)}")])
        for row in members:
            sid = _field(row, "user_id")
            cat = _field(row, "ticket_category", default="all") or "all"
            label = f"{short(_field(row, 'full_name'), 24)} · {short(position_text(row), 24)}"
            keyboard.append([btn(label, f"sf:{sid}"), btn(short(STAFF_CATS.get(cat, cat), 16), f"sf:{sid}")])
    return keyboard


@callback("admins")
async def cb_admins(x, arg):
    if not await need_super(x):
        return
    rows = await repo.list_staff()
    text = "👥 Сотрудники колледжа" if rows else "👥 Сотрудников пока нет. Добавьте первого."
    kb = staff_list_kb(rows)
    await api.send(
        x, text,
        [*kb,
         [btn("➕ Добавить сотрудника", "sfadd"), btn("👤 Кто без прав", "nostaff")],
         [btn("🔐 Сис-админы", "syslist"), btn("🗝 Коды и заявки", "codes")],
         *BACK],
    )


@callback("sdep")
async def cb_staff_department_view(x, arg):
    """Сотрудники одного отдела."""
    if not await need_super(x):
        return
    department = as_str(arg)
    rows = [r for r in await repo.list_staff() if (_field(r, "department") or "Без отдела") == department]
    if not rows:
        return await api.send(x, f"В отделе «{short(department, 30)}» сотрудников не нашлось.",
                              [[btn("↩️ К сотрудникам", "admins")]])
    keyboard = [[btn(f"{short(_field(r, 'full_name'), 26)} · {short(position_text(r), 22)}", f"sf:{_field(r, 'user_id')}")]
                for r in rows]
    await api.send(x, f"🏛 {short(department, 60)}: {len(rows)}",
                   [*keyboard, [btn("↩️ К сотрудникам", "admins")]])


async def send_staff_card(x: str, staff_id: str):
    a = await admin_of(staff_id)
    if not a or is_super(a):
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    sid = staff_id_of(a) or as_str(staff_id)
    cat = _field(a, "ticket_category", default="all") or "all"
    activity = (await repo.staff_activity(90)).get(sid, {})
    load = (f"Обращений за 90 дней: {activity.get('tickets', 0)}"
            f" · открытых: {activity.get('open', 0)}"
            f" · последнее: {fmt_when(activity['last_reply']) if activity.get('last_reply') else '—'}")
    text = (
        f"👤 {_field(a, 'full_name')}\nMAX ID: {sid}\n"
        f"Должность: {position_text(a)}\nОтдел: {department_of(a) or '—'}\nКабинет: {office_of(a) or '—'}\n"
        f"Обращения: {STAFF_CATS.get(cat, cat)}\n"
        f"Рассылка: {'разрешена' if _flag(_field(a, 'can_broadcast', default='0')) else 'запрещена'}\n"
        f"{load}"
    )
    cat_row = [btn(("● " if code == cat else "") + label, f"sfc:{sid}:{code}")
               for code, label in STAFF_CATS.items()]
    cat_rows = [cat_row[i:i + 2] for i in range(0, len(cat_row), 2)]
    await api.send(
        x,
        text,
        [
            [btn("✏️ Должность", f"sfr:{sid}"), btn("🏛 Отдел", f"sfdep:{sid}")],
            [btn("🏢 Кабинет", f"sfo:{sid}"), btn(ROLE_PRESET, f"sft:{sid}")],
            *cat_rows,
            [btn("📢 Рассылка: " + ("запретить" if _flag(_field(a, "can_broadcast", default="0")) else "разрешить"), f"sfb:{sid}")],
            [btn("🔐 Сделать сис-админом", f"sfsa:{sid}"), btn("🗑 Удалить", f"sfdel:{sid}")],
            [btn("↩️ К списку", "admins")],
        ],
    )


@callback("sfsa")
async def cb_staff_promote(x, arg):
    """Повышает сотрудника до сис-админа прямо из карточки — без ввода ID вручную."""
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a:
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    if is_super(a):
        return await api.send(x, f"{_field(a, 'full_name')} уже сис-админ.", [[btn("🔐 Сис-админы", "syslist")]])
    done, message = await repo.grant_sysadmin(as_str(arg), _field(a, "full_name"))
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("🔐 Сис-админы", "syslist")]])
    await notify(arg, "🔐 Вам выдали права сис-админа бота колледжа: в панели появится кнопка «🔐 Сис-админ».")
    await repo.log_action(x, "права сис-админа выданы", f"{_field(a, 'full_name')} (ID {arg})")
    await audit(x, f"Сис-админ {x} повысил сотрудника {arg} до сис-админа.")
    await api.send(x, f"🔐 {message}.", [[btn("🔐 Сис-админы", "syslist")], *BACK])


@callback("sf")
async def cb_staff_card(x, arg):
    if await need_super(x):
        await send_staff_card(x, arg)


@callback("sft")
async def cb_staff_role_pick(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    current = role_of(a)
    await api.send(
        x,
        f"{ROLE_PRESET} для {_field(a, 'full_name')}\n"
        f"Код роли удобен для фильтров, а то, что видят студенты, — свободный текст в «✏️ Должность».",
        [
            [btn(("● " if code == current else "") + label, f"srset:{staff_id_of(a) or as_str(arg)}:{code}")]
            for code, label in STAFF_ROLES.items()
        ]
        + [[btn("Очистить тип", f"srset:{staff_id_of(a) or as_str(arg)}:-")]],
    )


@callback("sfr")
async def cb_staff_position_ask(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    await db.set_state(x, "staff_position", {"admin_id": staff_id_of(a) or as_str(arg)})
    hints = [btn(hint, f"sfph:{staff_id_of(a) or as_str(arg)}:{hint}") for hint in POSITION_HINTS]
    await api.send(
        x,
        f"Введите должность сотрудника {_field(a, 'full_name')} — свободным текстом, "
        "например: «Преподаватель информатики» или «Заведующий отделением».\n"
        "Этот текст увидят студенты в подписи к рассылкам и в обращениях. Частые должности — кнопками ниже (или /cancel).",
        [hints[i:i + 2] for i in range(0, len(hints), 2)],
    )


@callback("sfph")
async def cb_staff_position_hint(x, arg):
    """Подсказка должности в карточке сотрудника: сохраняет и возвращает в карточку."""
    if not await need_super(x):
        return
    staff_id, _, position = as_str(arg).partition(":")
    a = await admin_of(staff_id)
    if not a or is_super(a):
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    await repo.update_admin(staff_id, position=position[:100])
    await repo.log_action(x, "должность изменена", f"{staff_id}: {position}")
    await api.send(x, f"✏️ {_field(a, 'full_name')}: должность «{position}».")
    await send_staff_card(x, staff_id)


@state("staff_position")
async def st_staff_position(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    sid = as_str((p or {}).get("admin_id"))
    a = await admin_of(sid)
    if not a or is_super(a):
        await db.clear_state(x)
        return await send_staff_card(x, sid)
    position = short(text, 100)
    if not position:
        return await api.send(x, "Введите должность текстом или «-», чтобы очистить.")
    if position == "-":
        await repo.clear_admin_fields(sid, "position")
    else:
        await repo.set_admin_profile(sid, position=position)
    await db.clear_state(x)
    await send_staff_card(x, sid)


@callback("sfdep")
async def cb_staff_department_ask(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    await db.set_state(x, "staff_department", {"admin_id": staff_id_of(a) or as_str(arg)})
    await api.send(
        x,
        f"Введите отдел сотрудника {_field(a, 'full_name')} — например, «Учебная часть» "
        "или «Бухгалтерия» (или /cancel).",
    )


@state("staff_department")
async def st_staff_department(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    sid = as_str((p or {}).get("admin_id"))
    a = await admin_of(sid)
    if not a or is_super(a):
        await db.clear_state(x)
        return await send_staff_card(x, sid)
    department = short(text, 100)
    if not department:
        return await api.send(x, "Введите отдел текстом или «-», чтобы очистить.")
    if department == "-":
        await repo.clear_admin_fields(sid, "department")
    else:
        await repo.set_admin_profile(sid, department=department)
    await db.clear_state(x)
    await send_staff_card(x, sid)


@callback("srset")
async def cb_staff_role_set(x, arg):
    sid, _, role = arg.partition(":")
    a = await admin_of(sid)
    if not (await need_super(x) and a and not is_super(a)):
        return
    if role in STAFF_ROLES:
        await repo.set_admin_profile(sid, role=role)
    elif role == "-":
        await repo.clear_admin_fields(sid, "role")
    else:
        return
    await send_staff_card(x, sid)


@callback("sfo")
async def cb_staff_office_ask(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    await db.set_state(x, "staff_office", {"admin_id": staff_id_of(a) or as_str(arg)})
    await api.send(x, f"Отправьте кабинет сотрудника {_field(a, 'full_name')} (например, 214) или /cancel.")


@state("staff_office")
async def st_staff_office(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    sid = as_str((p or {}).get("admin_id"))
    a = await admin_of(sid)
    if not a or is_super(a):
        await db.clear_state(x)
        return await send_staff_card(x, sid)
    office = short(text, 100)
    if not office:
        return await api.send(x, "Введите кабинет сотрудника.")
    await repo.set_admin_profile(sid, office=office)
    await db.clear_state(x)
    await send_staff_card(x, sid)


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
        await repo.set_staff_broadcast(arg, not _flag(_field(a, "can_broadcast", default="0")))
        await send_staff_card(x, arg)


@callback("sfdel")
async def cb_staff_delete_ask(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await api.send(x, f"Удалить сотрудника {_field(a, 'full_name')}?",
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
    await api.send(
        x,
        "Введите MAX ID сотрудника — можно сразу нескольких: цифры, @ник или ссылка на профиль, "
        "через запятую или с новой строки.\nСотрудник узнает свой ID командой /id. (или /cancel)",
    )


@state("add_staff_id")
async def st_add_staff_id(x, text, p):
    """Один сотрудник — как раньше, шаг за шагом. Несколько — общая должность и категория."""
    if not await need_super(x):
        return await db.clear_state(x)
    targets, missing = await repo.staff_targets(text)
    if not targets:
        hint = " Напишите цифры ID, @ник или ссылку на профиль MAX." if not missing else ""
        if missing:
            return await api.send(x, f"Ник {', '.join('@' + nick for nick in missing)} мне не знаком — "
                                     f"этот человек ни разу не писал боту.{hint}")
        return await api.send(x, f"Не нашёл ни одного ID.{hint}")
    fresh = [item for item in targets if not item["exists"]]
    skipped = [item for item in targets if item["exists"]]
    if not fresh:
        names = ", ".join(f"{item['full_name'] or item['user_id']} (ID {item['user_id']})" for item in skipped)
        return await api.send(
            x,
            f"Все уже в списке: {names}. Права не выдаются повторно.",
            [[btn("↩️ К сотрудникам", "admins")], *BACK],
        )
    if len(fresh) == 1:
        staff_id = fresh[0]["user_id"]
        note = f"\n⚠️ Уже в списке, пропускаю: {', '.join(item['user_id'] for item in skipped)}" if skipped else ""
        await db.set_state(x, "add_staff_name", {"id": staff_id})
        await api.send(x, f"Добавляю {fresh[0]['full_name'] or 'сотрудника'} (ID {staff_id}).{note}\n"
                          "Введите ФИО (как его увидят студенты).")
        return
    ids = [item["user_id"] for item in fresh]
    listed = "\n".join(f"• {item['full_name'] or 'без имени'} — ID {item['user_id']}" for item in fresh)
    note = f"\n⚠️ Уже в списке, пропускаю: {', '.join(item['user_id'] for item in skipped)}" if skipped else ""
    await db.set_state(x, "add_staff_batch", {"ids": ids})
    await send_position_hints(
        x,
        f"Добавлю {len(ids)} сотрудников:\n{listed}{note}\nВведите должность для всех — её увидят студенты.",
    )


@state("add_staff_batch")
async def st_add_staff_batch(x, text, p):
    """Должность для пачки сотрудников — одна на всех."""
    if not await need_super(x):
        return await db.clear_state(x)
    ids = (p or {}).get("ids") or []
    position = "" if short(text, 100) == "-" else short(text, 100)
    if not ids:
        await db.clear_state(x)
        return await api.send(x, "Список сотрудников потерялся, начните заново.", [[btn("➕ Добавить", "sfadd")]])
    await db.set_state(x, "add_staff_batch_cat", {"ids": ids, "position": position})
    await send_batch_category(x, ids, position)


async def send_batch_category(x: str, ids: list, position: str) -> None:
    """Категория обращений для пачки: одна кнопка на всех."""
    row = [btn(("● " if code == "all" else "") + label, f"sfbc:{code}") for code, label in STAFF_CATS.items()]
    await api.send(
        x,
        f"Должность: {position or '—'} для {len(ids)} сотрудников.\nКому они будут отвечать?",
        [row[i:i + 2] for i in range(0, len(row), 2)],
    )


@callback("sfbc")
async def cb_add_staff_batch_category(x, arg):
    """Создаёт пачку сотрудников с общей должностью и категорией."""
    if not await need_super(x):
        return
    payload = (await db.get_state(x) or {}).get("payload") or {}
    ids = payload.get("ids") or []
    if not ids:
        return await api.send(x, "Список сотрудников потерялся, начните заново.", [[btn("➕ Добавить", "sfadd")]])
    category = as_str(arg) or "all"
    entries = [{"user_id": uid} for uid in ids]
    added = await repo.add_staff_many(entries, position=payload.get("position", ""), ticket_category=category)
    await db.clear_state(x)
    for uid in added:
        await notify(uid, "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, "
                          "чтобы открыть кабинет.")
    await repo.log_action(x, "сотрудники добавлены", f"{len(added)} шт: {', '.join(added)}")
    await audit(x, f"Сис-админ {x} добавил сотрудников: {', '.join(added)}.")
    lines = [f"✅ Добавлено сотрудников: {len(added)}"]
    lines += [f"• ID {uid} · {STAFF_CATS.get(category, category)}" for uid in added]
    lines.append("Должность и отдел можно поправить в карточке сотрудника.")
    await api.send(x, "\n".join(lines), [[btn("👥 Сотрудники", "admins")], *BACK])


@state("add_staff_name")
async def st_add_staff_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    name = short(text, 100)
    if not name:
        return await api.send(x, "Введите ФИО сотрудника.")
    await db.set_state(x, "add_staff_position", {"id": p["id"], "name": name})
    await api.send(
        x,
        f"Должность сотрудника {name} — свободным текстом, например «Преподаватель математики».\n"
        "Этот текст увидят студенты. Отправьте «-», чтобы пропустить.",
    )


@state("add_staff_position")
async def st_add_staff_position(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    position = "" if short(text, 100) == "-" else short(text, 100)
    await db.set_state(x, "add_staff_office", {"id": p["id"], "name": p["name"], "position": position})
    await api.send(x, f"Кабинет сотрудника {p['name']} (например, 214) или «-», если кабинета нет.")


@state("add_staff_office")
async def st_add_staff_office(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    office = "" if short(text, 100) == "-" else short(text, 100)
    await repo.add_staff(p["id"], p["name"], position=p.get("position", ""), office=office)
    await db.clear_state(x)
    delivered = await notify(
        p["id"],
        "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, чтобы открыть кабинет.",
    )
    await api.send(x, "✅ Сотрудник добавлен." + ("" if delivered else "\n⚠️ Он пока не запускал бота — пусть нажмёт «Начать»."))
    await audit(x, f"Сис-админ {x} добавил сотрудника {p['name']} (ID {p['id']}).")
    await send_staff_card(x, p["id"])


# ── пользователи: все, кто писал боту ─────────────────────────────────────────
PEOPLE_KINDS = repo.CONTACT_KIND_LABELS
PEOPLE_PAGE = 15


def contact_name(row) -> str:
    return _field(row, "fio") or _field(row, "staff_name") or _field(row, "display_name") or f"ID {_field(row, 'user_id')}"


def contact_line(row) -> str:
    """Строка реестра: имя, роль в боте, ник и ссылка на профиль."""
    kind = repo.contact_kind(row)
    parts = [f"{repo.KIND_TITLES.get(kind, kind)}: {contact_name(row)}"]
    for extra in (_field(row, "group_code"), _field(row, "position")):
        if extra:
            parts.append(extra)
    username = _field(row, "username")
    parts.append(f"@{username}" if username else "ник не открыт")
    link = profile_url(username)
    return " · ".join(parts) + (f" · {link}" if link else "")


async def send_people(x: str, kind: str = "", offset: int = 0):
    total = await repo.people_count(kind)
    rows = await repo.people(kind, limit=PEOPLE_PAGE, offset=offset)
    if rows:
        text = f"👥 Пользователи бота: {total} · фильтр: {PEOPLE_KINDS.get(kind, kind)}\nПоказано {offset + 1}–{offset + len(rows)}"
    else:
        text = f"👥 {PEOPLE_KINDS.get(kind, kind)}: пока пусто — никто не писал боту."
    filters = [btn(f"{'● ' if code == kind else ''}{label}", f"people:{code}")
               for code, label in PEOPLE_KINDS.items()]
    keyboard = [filters[i:i + 2] for i in range(0, len(filters), 2)]
    for row in rows:
        keyboard.append([btn(short(contact_line(row), 60), f"person:{_field(row, 'user_id')}")])
    if offset > 0:
        keyboard.append([btn("⬅️ Назад", f"people:{kind}:{max(0, offset - PEOPLE_PAGE)}")])
    if offset + len(rows) < total:
        keyboard.append([btn("➡️ Вперёд", f"people:{kind}:{offset + PEOPLE_PAGE}")])
    await api.send(x, text, [*keyboard, *BACK])


@callback("today")
async def cb_today(x, arg):
    """Сводка дня: что требует внимания сис-админа прямо сейчас."""
    if not await need_super(x):
        return
    summary = await repo.admin_today()
    lines = ["🔔 Что сделать сегодня"]
    if summary["no_answer"]:
        lines.append(f"📬 Обращений без ответа: {summary['no_answer']}")
    if summary["ready_not_picked"]:
        lines.append(f"📄 Готово к выдаче, но не отмечено: {summary['ready_not_picked']}")
    if summary["requests"]:
        lines.append(f"📥 Заявок на роль сотрудника: {summary['requests']}")
    if summary["no_staff"]:
        lines.append(f"👤 Писали боту, но без прав сотрудника: {summary['no_staff']}")
    if summary["codes_active"]:
        lines.append(f"🗝 Активных кодов сотрудника: {summary['codes_active']}")
    if summary["stuck_states"]:
        lines.append(f"🌀 Зависших диалогов (почистить): {summary['stuck_states']}")
    if len(lines) == 1:
        lines.append("Всё спокойно: неотвеченных обращений и заявок нет.")
    lines.append("")
    lines.append(f"⏱ Среднее время ответа: {summary['avg_reply'] or '—'}")
    lines.append(f"📦 Обращений за сутки: {summary['tickets_day']}")
    await api.send(
        x, "\n".join(lines),
        [[btn("📬 Обращения без ответа", "staff"), btn("📥 Заявки", "requests")],
         [btn("👤 Кто без прав", "nostaff"), btn("🗝 Коды", "codes")],
         [btn("🧹 Почистить диалоги", "cleandlg")], *BACK],
    )


@callback("cleandlg")
async def cb_clean_dialogs(x, arg):
    """Сбрасывает зависшие состояния диалогов — иначе человек не может начать заново."""
    if not await need_super(x):
        return
    removed = await db.prune("user_states", 1)
    await repo.log_action(x, "очищены зависшие диалоги", f"удалено состояний: {removed}")
    await api.send(x, f"🧹 Удалено зависших состояний: {removed}.", [[btn("🔔 Что сделать сегодня", "today")]])


@callback("people")
async def cb_people(x, arg):
    if not await need_super(x):
        return
    kind, _, offset = as_str(arg).partition(":")
    await send_people(x, kind, to_int(offset))


@callback("person")
async def cb_person(x, arg):
    """Карточка человека: контакт, регистрация, обращения, заявка."""
    if not await need_super(x):
        return
    card = await repo.user_card(arg)
    if not card:
        return await api.send(x, "Человек ещё не писал боту.", [[btn("↩️ К списку", "people")]])
    lines = [f"👤 {contact_name(card)}", f"MAX ID: {card['user_id']}"]
    username = card["username"] or ""
    link = profile_url(username)
    if username:
        lines.append(f"Профиль MAX: @{username}" + (f" — {link}" if link else ""))
    else:
        lines.append("Профиль MAX: ник не открыт пользователем")
    lines.append(f"Роль в боте: {repo.KIND_TITLES.get(card['kind'], card['kind'])}")
    if card["fio"]:
        lines.append(f"Студент: {card['fio']}, группа {card['group_code'] or '—'}")
    if card["position"] or card["staff_name"]:
        lines.append(f"Сотрудник: {card['staff_name']} · {card['position'] or 'должность не назначена'}")
    if card["department"]:
        lines.append(f"Отдел: {card['department']}")
    lines.append(f"Сообщений боту: {card['messages']}")
    lines.append(f"Обращений: {card['tickets']}")
    lines.append(f"Первый контакт: {fmt_when(card['first_seen'])} · последний: {fmt_when(card['last_seen'])}")
    if card["last_text"]:
        lines.append(f"Последнее сообщение: {short(card['last_text'], 200)}")
    if card["request"]:
        request = card["request"]
        lines.append(f"Заявка ({request['status']}): должность «{request['position'] or '—'}», "
                     f"кабинет {request['office'] or '—'}")
    if card["tickets_list"]:
        recent = ", ".join(f"№{t['ticket_id']} {STATUS.get(t['status'], t['status'])}" for t in card["tickets_list"][:5])
        lines.append(f"Последние обращения: {recent}")
    keyboard = [[btn("↩️ К пользователям", "people")]]
    if card["kind"] == "request" and card["request"] and card["request"]["status"] == "new":
        keyboard.insert(0, [btn("✅ Одобрить заявку", f"reqok:{card['user_id']}"),
                            btn("❌ Отклонить", f"reqno:{card['user_id']}")])
    elif not card["role_type"]:
        keyboard.insert(0, [btn("👔 Сделать сотрудником", f"make:{card['user_id']}")])
    if not is_sysadmin_role(as_str(card["role_type"])):
        opened = await repo.student_open_tickets_count(card["user_id"])
        if opened:
            keyboard.append([btn(f"🗑 Удалить вместе с обращениями ({card['tickets']})", f"persondel:{card['user_id']}:1")])
        else:
            keyboard.append([btn("🗑 Удалить пользователя", f"persondel:{card['user_id']}")])
    await api.send(x, "\n".join(lines), keyboard)


@callback("persondel")
async def cb_person_delete(x, arg):
    """Удаление человека из реестра. Открытые обращения — только вместе с ними."""
    if not await need_super(x):
        return
    uid, _, flag = as_str(arg).partition(":")
    card = await repo.user_card(uid)
    if not card:
        return await api.send(x, "Человек не найден.", [[btn("↩️ К пользователям", "people")]])
    name = contact_name(card)
    with_tickets = flag == "1"
    opened = await repo.student_open_tickets_count(uid)
    if opened and not with_tickets:
        return await api.send(
            x,
            f"У {name} {opened} открытых обращений. Чтобы удалить, нажмите кнопку "
            f"«Удалить вместе с обращениями» — переписка тоже исчезнет.",
            [[btn(f"🗑 Удалить {name} вместе с обращениями", f"persondely:{uid}:1")],
             [btn("↩️ К пользователям", "people")]],
        )
    done, message = await repo.delete_user(uid, with_tickets=with_tickets)
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("↩️ К пользователям", "people")]])
    await audit(x, f"Сис-админ {x} удалил пользователя {name} (ID {uid}), с обращениями: {with_tickets}.")
    await api.send(x, f"🗑 {message}", [[btn("👥 К пользователям", "people")]])


@callback("persondely")
async def cb_person_delete_confirm(x, arg):
    """Подтверждение удаления вместе с обращениями: кнопка появляется только для этого."""
    if not await need_super(x):
        return
    uid = as_str(arg).partition(":")[0]
    card = await repo.user_card(uid)
    if not card:
        return await api.send(x, "Человек не найден.", [[btn("↩️ К пользователям", "people")]])
    name = contact_name(card)
    done, message = await repo.delete_user(uid, with_tickets=True)
    if not done:
        return await api.send(x, f"❌ {message}", [[btn("↩️ К пользователям", "people")]])
    await audit(x, f"Сис-админ {x} удалил пользователя {name} (ID {uid}) вместе с обращениями.")
    await api.send(x, f"🗑 {message}", [[btn("👥 К пользователям", "people")]])


@callback("make")
async def cb_make_staff(x, arg):
    """Превращает зарегистрированного пользователя в сотрудника — прямо из его карточки."""
    if not await need_super(x):
        return
    card = await repo.user_card(arg)
    if not card:
        return await api.send(x, "Человек не найден.")
    if card["role_type"]:
        role = "сис-админ" if is_super(card) else "сотрудник"
        return await api.send(
            x,
            f"{contact_name(card)} уже {role} — права выдавать не нужно. Открыть карточку?",
            [[btn("👤 Открыть карточку", f"sf:{card['user_id']}"), btn("↩️ К сотрудникам", "admins")]],
        )
    if card["kind"] == "request":
        return await api.send(x, "Сначала одобрите заявку.", [[btn("✅ Одобрить заявку", f"reqok:{card['user_id']}")]])
    await db.set_state(x, "make_staff_position", {"id": card["user_id"], "name": contact_name(card)})
    await send_position_hints(
        x,
        f"Сотрудник: {contact_name(card)} (ID {card['user_id']}).\nВведите должность или выберите подсказку.",
    )


async def send_position_hints(x: str, text: str) -> None:
    """Экран выбора должности: подсказки одним нажатием и «свой текст» для остального."""
    keyboard = [btn(hint, f"mph:{hint}") for hint in POSITION_HINTS]
    rows = [keyboard[i:i + 2] for i in range(0, len(keyboard), 2)]
    await api.send(x, text, [*rows, [btn("✏️ Свой текст", "mphtext")]])


@callback("mphtext")
async def cb_position_hint_text(x, arg):
    """Подсказки не подошли — возвращаемся к вводу текстом (состояние уже стоит)."""
    if await need_super(x):
        await api.send(x, "Напишите должность текстом. Отправьте «-», если должность не нужна.")


@callback("mph")
async def cb_position_hint(x, arg):
    """Применил подсказку должности: в выдаче прав пачкой, в карточке человека или в коде."""
    if not await need_super(x):
        return
    position = as_str(arg)[:100]
    session = await db.get_state(x) or {}
    payload = session.get("payload") or {}
    if session.get("state") == "make_staff_position":
        return await ask_staff_category(x, payload.get("id", ""), payload.get("name", ""), position)
    if session.get("state") == "add_staff_batch":
        ids = payload.get("ids") or []
        if ids:
            await db.set_state(x, "add_staff_batch_cat", {"ids": ids, "position": position})
            return await send_batch_category(x, ids, position)
    await api.send(x, "Сессия вышла — начните с «➕ Добавить сотрудника».", [[btn("➕ Добавить", "sfadd")]])


async def ask_staff_category(x: str, staff_id: str, name: str, position: str) -> None:
    """Последний шаг выдачи прав из карточки человека: категория обращений."""
    await db.set_state(x, "make_staff_category", {"id": staff_id, "name": name, "position": position})
    row = [btn(("● " if code == "all" else "") + label, f"mkc:{code}") for code, label in STAFF_CATS.items()]
    await api.send(
        x,
        f"Должность: {position or '—'}. Обращения по категориям будут приходить сотруднику {name or staff_id}.\n"
        "Кому он отвечает?",
        [row[i:i + 2] for i in range(0, len(row), 2)],
    )


@callback("mkc")
async def cb_make_staff_category(x, arg):
    """Создаёт сотрудника из карточки человека с выбранной должностью и категорией."""
    if not await need_super(x):
        return
    payload = (await db.get_state(x) or {}).get("payload") or {}
    staff_id = as_str(payload.get("id", ""))
    if not staff_id.isdigit() or await admin_of(staff_id):
        await db.clear_state(x)
        return await api.send(x, "Нечего выдавать: сотрудник уже есть или ID не тот.",
                              [[btn("👥 К сотрудникам", "admins")]])
    await repo.add_staff(staff_id, payload.get("name") or f"Сотрудник {staff_id}",
                         position=payload.get("position", ""), ticket_category=as_str(arg) or "all")
    await db.clear_state(x)
    delivered = await notify(staff_id, "🏫 Вам назначили роль сотрудника в боте колледжа. Отправьте /start, "
                                       "чтобы открыть кабинет.")
    await repo.log_action(x, "сотрудник добавлен", f"{staff_id} {payload.get('name') or ''} · {payload.get('position') or 'без должности'}")
    await audit(x, f"Сис-админ {x} добавил сотрудника {staff_id} из списка пользователей.")
    await api.send(x, f"✅ {payload.get('name') or staff_id} теперь сотрудник."
                      + ("" if delivered else "\n⚠️ Он пока не запускал бота — пусть нажмёт «Начать»."))
    await send_staff_card(x, staff_id)


@state("make_staff_position")
async def st_make_staff_position(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    position = "" if short(text, 100) == "-" else short(text, 100)
    return await ask_staff_category(x, as_str((p or {}).get("id", "")), as_str((p or {}).get("name", "")), position)


async def send_nostaff(x: str):
    """Кто писал боту, но прав сотрудника не имеет: выдать их можно отсюда."""
    rows = await repo.people_without_staff(20)
    if not rows:
        return await api.send(x, "✅ Все, кто писал боту, уже сотрудники.",
                              [[btn("👥 Сотрудники", "admins")], *BACK])
    keyboard = [[btn(short(contact_line(row), 58), f"make:{_field(row, 'user_id')}")]
                for row in rows]
    await api.send(
        x,
        "👤 Писали боту, но без прав сотрудника\nНажмите на человека — выдадите права сразу тут.",
        [*keyboard, [btn("👥 Сотрудники", "admins")], *BACK],
    )


@callback("nostaff")
async def cb_nostaff(x, arg):
    if await need_super(x):
        await send_nostaff(x)


# ── коды и заявки на роль сотрудника ──────────────────────────────────────────
INVITE_STATE_LABEL = {"active": "🟢 активен", "used": "⚪ использован", "expired": "🔴 истёк", "unknown": "—"}
REQUEST_STATUS_LABEL = {"new": "📥 новая", "approved": "✅ одобрена", "rejected": "❌ отклонена"}


async def send_codes(x: str):
    rows = await repo.list_invites(20)
    lines = ["🗝 Коды сотрудников"]
    for row in rows:
        state = await repo.invite_state(row["code"])
        who = f" для {row['full_name'] or _field(row, 'fio') or 'ID ' + as_str(row['user_id'])}" if row["user_id"] else " (любому, у кого есть код)"
        lines.append(f"{_field(row, 'code')} · {INVITE_STATE_LABEL.get(state, state)}{who} · до {row['expires_at'] or 'бессрочно'}")
    if not rows:
        lines.append("Кодов пока нет. Выдайте код — сотрудник сможет сам зарегистрироваться.")
    pending = await repo.staff_requests("new")
    if pending:
        lines.append("")
        lines.append(f"📥 Необработанных заявок: {len(pending)}")
    keyboard = [[btn("🎟 Новый код", "codegen"), btn("✉️ Пригласить по ID", "codeinv")]]
    if pending:
        keyboard.append([btn(f"📥 Заявки ({len(pending)})", "requests")])
    keyboard.append([btn("↩️ К сотрудникам", "admins")])
    await api.send(x, "\n".join(lines), keyboard)


@callback("codes")
async def cb_codes(x, arg):
    if await need_super(x):
        await send_codes(x)


@callback("codegen")
async def cb_code_generate(x, arg):
    if not await need_super(x):
        return
    for _ in range(5):
        code = gen_code(6)
        if await repo.invite_state(code) == "unknown":
            break
    else:
        return await api.send(x, "Не удалось создать свободный код. Попробуйте ещё раз.")
    await repo.create_invite(code, created_by=x, ttl_hours=config.STAFF_CODE_TTL)
    log.info("сис-админ %s выдал код сотрудника", x)
    await send_new_code(x, code, f"🎟 Код: {code}\n")
    await audit(x, f"Сис-админ {x} выдал код сотрудника.")


async def send_new_code(x: str, code: str, header: str) -> None:
    """Показывает код и кнопки срока: поменять можно сразу, не выпуская новый код."""
    ttl_row = [btn(f"⏳ {label}", f"codettl:{code}:{hours}") for hours, label in CODE_TTL_CHOICES]
    keyboard = [ttl_row[i:i + 3] for i in range(0, len(ttl_row), 3)]
    keyboard.append([btn("🗝 Все коды", "codes")])
    await api.send(
        x,
        f"{header}Срок: {ttl_label(config.STAFF_CODE_TTL)}, одноразовый.\n"
        "Передайте сотруднику: в боте он выбирает «👔 Я сотрудник» и вводит этот код.\n"
        "Если нужен другой срок — нажмите кнопку ниже, код останется тем же.",
        [*keyboard, *BACK],
    )


@callback("codettl")
async def cb_code_ttl(x, arg):
    """Меняет срок выданного кода на тот, что сис-админ выбрал кнопкой."""
    if not await need_super(x):
        return
    code, _, raw_hours = as_str(arg).rpartition(":")
    if not code:
        return await api.send(x, "Код не распознан.")
    done, label = await repo.set_invite_ttl(code, to_int(raw_hours, 0))
    if not done:
        return await api.send(x, f"❌ {label}", [[btn("🗝 Все коды", "codes")]])
    await repo.log_action(x, "срок кода изменён", f"{code}: {label}")
    await api.send(x, f"⏳ Код {code}: срок теперь {label}.",
                   [[btn("🗝 Все коды", "codes")], *BACK])


@callback("codeinv")
async def cb_code_invite_ask(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "code_invite_id")
    await api.send(x, "Введите MAX ID сотрудника, которому выдаётся личное приглашение (цифры), или /cancel.")


@state("code_invite_id")
async def st_code_invite_id(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    uid = text.strip()
    if not uid.isdigit():
        return await api.send(x, "ID состоит только из цифр. Попробуйте ещё раз.")
    if await admin_of(uid):
        await db.clear_state(x)
        return await api.send(x, "Этот человек уже сотрудник.")
    card = await repo.user_card(uid)
    name = contact_name(card) if card else ""
    await db.set_state(x, "code_invite_name", {"id": uid, "name": name})
    await api.send(x, f"Приглашение для ID {uid} ({name or 'имя неизвестно'}). Введите ФИО для списка или «-».")


@state("code_invite_name")
async def st_code_invite_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    name = "" if short(text, 100) == "-" else short(text, 100)
    code = gen_code(6)
    await repo.create_invite(code, user_id=p["id"], full_name=name, created_by=x, ttl_hours=config.STAFF_CODE_TTL)
    await db.clear_state(x)
    await send_new_code(
        x, code,
        f"✉️ Личное приглашение для {name or 'ID ' + p['id']}\nКод: {code}\n"
        f"Только для ID {p['id']}.\n",
    )
    log.info("сис-админ %s пригласил %s по коду", x, p["id"])


async def send_requests(x: str):
    rows = await repo.staff_requests("new")
    if not rows:
        return await api.send(x, "📥 Новых заявок нет.", [[btn("🗝 Коды", "codes")], *BACK])
    keyboard = []
    for row in rows:
        label = (f"ID {row['user_id']} · {row['full_name'] or row['display_name'] or 'без имени'} · "
                 f"{row['position'] or 'должность не указана'}")
        keyboard.append([btn(short(label, 60), f"req:{row['user_id']}")])
    await api.send(x, f"📥 Заявки на роль сотрудника: {len(rows)}", [*keyboard, [btn("🗝 Коды", "codes")], *BACK])


@callback("requests")
async def cb_requests(x, arg):
    if await need_super(x):
        await send_requests(x)


@callback("syslist")
async def cb_sysadmin_list(x, arg):
    """Список сис-админов: он живёт в базе, .env только заводит первых."""
    if not await need_super(x):
        return
    rows = await repo.list_sysadmins()
    lines = ["🔐 Сис-админы"]
    keyboard: list = []
    for row in rows:
        source = "из .env" if row["in_env"] else "выдан в панели"
        lines.append(f"{row['full_name']} · ID {row['user_id']} · {source}")
        keyboard.append([btn(f"Снять права: {short(row['full_name'], 24)}", f"sysdel:{row['user_id']}")])
    revoked = sorted(await repo.revoked_sysadmins())
    if revoked:
        lines.append("")
        lines.append("Отозванные (перезапуск бота их не вернёт): " + ", ".join(revoked))
    await api.send(
        x,
        "\n".join(lines),
        [*keyboard, [btn("➕ Выдать права", "sysadd")], [btn("↩️ К сотрудникам", "admins")], *BACK],
    )


@callback("sysadd")
async def cb_sysadmin_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "sysadd_id")
    await api.send(
        x,
        "Введите MAX ID человека, которому выдать права сис-админа — можно сразу нескольких: "
        "цифры, @ник или ссылка на профиль, через запятую.\nИмена подставлю сам из реестра. (или /cancel)",
    )


@state("sysadd_id")
async def st_sysadmin_id(x, text, p):
    """Выдача прав сис-админа: один человек или список — имена берём из реестра."""
    if not await need_super(x):
        return await db.clear_state(x)
    targets, missing = await repo.staff_targets(text)
    if not targets:
        if missing:
            return await api.send(x, f"Ник {', '.join('@' + nick for nick in missing)} мне не знаком — "
                                     "этот человек ни разу не писал боту.")
        return await api.send(x, "Не нашёл ни одного ID. Напишите цифры, @ник или ссылку на профиль.")
    fresh = [item for item in targets if not item["exists"]]
    if not fresh:
        names = ", ".join(f"{item['full_name'] or item['user_id']} (ID {item['user_id']})" for item in targets)
        return await api.send(x, f"Уже сис-админ: {names}. Права повторно не выдаются.",
                              [[btn("🔐 Сис-админы", "syslist")], *BACK])
    if len(fresh) == 1:
        target = fresh[0]
        note = f"\n⚠️ Уже сис-админ, пропускаю: {', '.join(item['user_id'] for item in targets if item['exists'])}" \
            if len(targets) > 1 else ""
        await db.set_state(x, "sysadd_name", {"id": target["user_id"]})
        return await api.send(x, f"Выдаю права {target['full_name'] or 'человеку'} (ID {target['user_id']}).{note}\n"
                                  "Введите имя для списка или «-».")
    return await finish_grant_sysadmin(x, [(item["user_id"], item["full_name"]) for item in fresh])


async def finish_grant_sysadmin(x: str, pairs: list[tuple[str, str]]) -> None:
    """Выдаёт права всем сразу и показывает, кому именно."""
    done: list[str] = []
    problems: list[str] = []
    for uid, name in pairs:
        ok, message = await repo.grant_sysadmin(uid, name or "")
        if not ok:
            problems.append(message)
            continue
        done.append(uid)
        await notify(uid, "🔐 Вам выдали права сис-админа бота колледжа: в панели появится кнопка «🔐 Сис-админ».")
    await db.clear_state(x)
    lines = [f"✅ Права сис-админа выданы: {len(done)}"]
    lines += [f"• {name or 'ID ' + uid} (ID {uid})" for uid, name in pairs if uid in done]
    if problems:
        lines += ["", "⚠️ Не выданы:", *[f"• {message}" for message in problems]]
    await repo.log_action(x, "права сис-админа выданы", ", ".join(done) or "—")
    await audit(x, f"Сис-админ {x} выдал права сис-админа: {', '.join(done) or '—'}.")
    await api.send(x, "\n".join(lines), [[btn("🔐 Сис-админы", "syslist")], *BACK])


@state("sysadd_name")
async def st_sysadmin_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    uid = (p or {}).get("id", "")
    name = "" if short(text, 100) == "-" else short(text, 100)
    return await finish_grant_sysadmin(x, [(uid, name)])


@callback("sysdel")
async def cb_sysadmin_delete(x, arg):
    """Снятие прав сис-админа с подтверждением. Можно сразу с нескольких."""
    if not await need_super(x):
        return
    uids = [uid for uid in [part.strip() for part in as_str(arg).split(",")] if uid]
    admins = [a for a in [await admin_of(uid) for uid in uids] if a and is_super(a)]
    if not admins:
        return await api.send(x, "Это не сис-админ.", [[btn("🔐 Сис-админы", "syslist")]])
    listed = "\n".join(f"• {_field(a, 'full_name')} (ID {_field(a, 'user_id')})" for a in admins)
    many = "" if len(admins) == 1 else f"\nВсего {len(admins)} — сниму у всех, кого перечислил."
    await api.send(
        x, f"Снять права сис-админа?{many}\n{listed}\n\nВ панели они больше не смогут войти. "
           f"Отменить можно будет только вручную.",
        [[btn("❌ Да, снять", f"sysdely:{','.join(_field(a, 'user_id') for a in admins)}"),
          btn("↩️ Список", "syslist")]],
    )


@callback("sysdely")
async def cb_sysadmin_delete_yes(x, arg):
    if not await need_super(x):
        return
    uids = [uid for uid in [part.strip() for part in as_str(arg).split(",")] if uid]
    if x in uids:
        return await api.send(x, "Себе снять права нельзя — вы потеряете доступ.", [[btn("🔐 Сис-админы", "syslist")]])
    revoked: list[str] = []
    problems: list[str] = []
    for uid in uids:
        done, message = await repo.revoke_sysadmin(uid)
        if not done:
            problems.append(message)
            continue
        revoked.append(uid)
        await notify(uid, "🔐 Права сис-админа бота колледжа сняты. Панель вам больше не доступна.")
        await repo.log_action(x, "права сис-админа сняты", f"ID {uid}")
        await audit(x, f"Сис-админ {x} снял права сис-админа с ID {uid}.")
    lines = [f"🗑 Права сняты: {len(revoked)}"]
    lines += [f"• ID {uid}" for uid in revoked]
    if problems:
        lines += ["", "⚠️ Не сняты:", *[f"• {message}" for message in problems]]
    return await api.send(x, "\n".join(lines), [[btn("🔐 Сис-админы", "syslist")]])


@callback("req")
async def cb_request_card(x, arg):
    if not await need_super(x):
        return
    row = await repo.get_staff_request(arg)
    if not row:
        return await api.send(x, "Заявка не найдена.", [[btn("↩️ К заявкам", "requests")]])
    uid = as_str(row["user_id"])
    card = await repo.user_card(uid)
    username = (card or {}).get("username") or ""
    link = profile_url(username)
    text = (
        f"📥 Заявка от ID {uid}\n"
        f"Имя: {row['full_name'] or (card or {}).get('display_name') or '—'}\n"
        f"Должность: {row['position'] or '—'}\nКабинет: {row['office'] or '—'}\n"
        f"Комментарий: {row['note'] or '—'}\n"
        f"Профиль: {'@' + username + (' — ' + link if link else '') if username else 'не открыт'}\n"
        f"Статус: {REQUEST_STATUS_LABEL.get(row['status'], row['status'])}\n"
        f"Подана: {fmt_when(row['created_at'])}"
    )
    await api.send(
        x, text,
        [[btn("✅ Одобрить — сделать сотрудником", f"reqok:{uid}"), btn("✏️ Должность изменить", f"reqpos:{uid}")],
         [btn("❌ Отклонить", f"reqno:{uid}"), btn("↩️ К заявкам", "requests")]],
    )


@callback("reqpos")
async def cb_request_position(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "req_position", {"id": as_str(arg), "actor": x})
    await api.send(x, "Введите должность сотрудника для этой заявки — свободным текстом (или /cancel).")


@state("req_position")
async def st_req_position(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    position = short(text, 100)
    row = await repo.get_staff_request(p.get("id"))
    if not row:
        await db.clear_state(x)
        return await api.send(x, "Заявка не найдена.")
    await db.run("UPDATE staff_requests SET position=?, updated_at=datetime('now') WHERE user_id=?",
                 (position, p["id"]))
    await db.clear_state(x)
    await cb_request_card(p.get("actor", x), p["id"])


async def approve_request(actor: str, user_id: str, position: str = "", office: str = "") -> tuple[bool, str]:
    """Одобряет заявку: сотрудник создаётся, ему уходят уведомления, автору — отчёт."""
    row = await repo.get_staff_request(user_id)
    card = await repo.user_card(user_id)
    name = as_str(row["full_name"]) if row else ""
    if not name and card:
        name = as_str(card["fio"]) or as_str(card["staff_name"]) or as_str(card["display_name"])
    name = name or f"Сотрудник {user_id}"
    position = position or (as_str(row["position"]) if row else "")
    office = office or (as_str(row["office"]) if row else "")
    await repo.add_staff(user_id, name, position=position, office=office)
    if row:
        await repo.set_staff_request_status(user_id, "approved")
    await repo.clear_attempts(user_id)
    delivered = await notify(
        user_id,
        f"🏫 Ваша заявка одобрена: вы сотрудник колледжа — {name}"
        + (f", {position}" if position else "")
        + ". Отправьте /start, чтобы открыть кабинет.",
    )
    await audit(actor, f"Сис-админ {actor} одобрил заявку {name} (ID {user_id}).")
    return True, ("Сотрудник создан." if delivered else "Сотрудник создан, но он ещё не запускал бота.")


async def reject_request(actor: str, user_id: str, reason: str = "") -> None:
    await repo.set_staff_request_status(user_id, "rejected")
    await repo.clear_attempts(user_id)
    await notify(
        user_id,
        "Заявка на роль сотрудника отклонена" + (f": {reason}" if reason else "")
        + ". Если вопрос снялся — обратитесь в учебную часть.",
    )
    await audit(actor, f"Сис-админ {actor} отклонил заявку от ID {user_id}.")


@callback("reqok")
async def cb_request_ok(x, arg):
    if not await need_super(x):
        return
    ok, message = await approve_request(x, as_str(arg))
    await api.send(x, f"✅ {message}", [[btn("↩️ К сотрудникам", "admins")], *BACK])
    await send_staff_card(x, as_str(arg))


@callback("reqno")
async def cb_request_no(x, arg):
    if not await need_super(x):
        return
    uid = as_str(arg)
    await api.send(x, f"Отклонить заявку от ID {uid}?",
                   [[btn("❌ Да, отклонить", f"reqnoy:{uid}"), btn("Отмена", f"req:{uid}")]])


@callback("reqnoy")
async def cb_request_no_yes(x, arg):
    if not await need_super(x):
        return
    await reject_request(x, as_str(arg))
    await send_requests(x)


# ── справочник групп ──────────────────────────────────────────────────────────
GROUP_HINT = ("Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\n"
              "Например: ИС-21. Попробуйте ещё раз.")


async def list_groups(active_only: bool = False):
    lister = getattr(repo, "list_groups", None) or getattr(repo, "groups", None)
    return await lister(active_only=active_only) if lister is not None else []


async def all_groups() -> list[tuple[str, bool]]:
    seen: dict[str, bool] = {}
    for r in await (list_groups(active_only=False) or []):
        code = norm_group(_field(r, "code", "group_code", "group"))
        if code:
            seen[code] = _flag(_field(r, "active", "is_active", default="1"))
    return sorted(seen.items())


async def send_groups(x: str, note: str = ""):
    rows = await all_groups()
    kb = [[btn(f"{'🟢' if active else '⚪'} {code}", f"groupedit:{code}"),
           btn("🔄 Скрыть" if active else "👁 Показать", f"grouptoggle:{code}"),
           btn("🗑 Удалить", f"groupdel:{code}")] for code, active in rows]
    text = f"👥 Группы в справочнике: {len(rows)}" if rows else "👥 Справочник групп пуст. Добавьте первую группу."
    await api.send(x, "\n".join(part for part in (text, note) if part),
                   [*kb, [btn("➕ Добавить группу", "groupadd")], *BACK])


@callback("groups")
async def cb_groups(x, arg):
    if await need_super(x):
        await send_groups(x)


@callback("groupadd")
async def cb_group_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "group_add")
    await api.send(x, "Введите код группы, например ИС-21 (или /cancel).")


@state("group_add")
async def st_group_add(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    code = norm_group(text)
    if not valid_group(code):
        return await api.send(x, GROUP_HINT)
    if code in dict(await all_groups()):
        return await api.send(x, f"Группа {code} уже есть в справочнике.")
    await repo.upsert_group(code)
    await db.clear_state(x)
    await send_groups(x, f"✅ Группа {code} добавлена.")


@callback("groupedit")
async def cb_group_edit(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    if code not in dict(await all_groups()):
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    await db.set_state(x, "group_edit", {"code": code})
    await api.send(x, f"Код группы: {code}\nОтправьте новый код группы (например, ИС-22) или «-», чтобы ничего не менять.")


@state("group_edit")
async def st_group_edit(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    old = norm_group(as_str((p or {}).get("code")))
    if text.strip() in ("-", "—", "0", "не менять", "без изменений"):
        await db.clear_state(x)
        return await api.send(x, f"Код группы не изменился: {old}.")
    code = norm_group(text)
    if not valid_group(code):
        return await api.send(x, GROUP_HINT)
    rows = dict(await all_groups())
    if old not in rows:
        await db.clear_state(x)
        return await send_groups(x, f"⚠️ Группа {old} не найдена в справочнике.")
    if code == old:
        await db.clear_state(x)
        return await send_groups(x, f"Код группы не изменился: {old}.")
    if code in rows:
        return await api.send(x, f"Группа {code} уже есть в справочнике. Введите другой код.")
    renamer = getattr(repo, "rename_group", None)
    if renamer is None:
        await repo.upsert_group(code)
        await repo.delete_group(old)
    else:
        renamed = await renamer(old, code)
        if renamed is False:
            return await api.send(x, f"⚠️ Не удалось переименовать группу {old} → {code}.")
    await db.clear_state(x)
    await send_groups(x, f"✅ Группа переименована: {old} → {code}.")


@callback("grouptoggle")
async def cb_group_toggle(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    rows = dict(await all_groups())
    if code not in rows:
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    active = not rows[code]
    await repo.set_group_active(code, active)
    await send_groups(x, f"{'👁 Группа' if active else '⚪ Группа'} {code} {'активна' if active else 'скрыта'}.")


@callback("groupdel")
async def cb_group_delete(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    if code not in dict(await all_groups()):
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    await repo.delete_group(code)
    await send_groups(x, f"🗑 Группа {code} удалена из справочника.")


# ── проверка ссылки на расписание ─────────────────────────────────────────────
PROBE_TIMEOUT = 8.0
PROBE_BYTES = 2048
PDF_TYPES = ("application/pdf", "application/octet-stream", "binary")
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".arpa")


def probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(PROBE_TIMEOUT))


def host_allowed(host) -> bool:
    h = as_str(host).strip().lower().strip("[]")
    if not h or h == "localhost" or h.endswith(BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def probe_pdf_url(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "Нужна ссылка вида https://… (http или https)."
    if not host_allowed(parsed.hostname):
        return False, "Этот адрес проверять нельзя: локальный или служебный хост."
    try:
        async with probe_client() as client:
            async with client.stream("GET", url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"}) as resp:
                status = resp.status_code
                ctype = resp.headers.get("content-type", "").lower()
                async for _ in resp.aiter_bytes():
                    break
    except Exception as exc:
        log.warning("не удалось проверить ссылку %s: %s", url, exc)
        return False, "Ссылка не открывается: сервер недоступен или адрес неверный."
    if status not in (200, 206):
        return False, f"Сервер ответил кодом {status} — по ссылке нет файла."
    if not any(t in ctype for t in PDF_TYPES) and not parsed.path.lower().endswith(".pdf"):
        return False, "По ссылке отдаётся не PDF-файл. Нужна прямая ссылка на .pdf."
    return True, ""


# ── расписания ────────────────────────────────────────────────────────────────
async def schedule_group_codes() -> list[str]:
    codes = {norm_group(_field(r, "group_code", "code")) for r in (await repo.schedule_groups() or [])}
    codes |= {code for code, _ in await all_groups()}
    return sorted(codes - {""})


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
    if not await need_super(x):
        return
    code = norm_group(as_str(group))
    if not await repo.get_schedule(code):
        return await api.send(x, f"Расписание группы {code} не найдено.", [[btn("↩️ К списку", "schedules")]])
    await repo.delete_schedule(code)
    await notify_schedule_subscribers(code, f"🗑 Расписание группы {code} удалено.")
    await api.send(x, f"🗑 Расписание группы {code} удалено.")
    await audit(x, f"Сис-админ {x} удалил расписание группы {code}.")
    await cb_schedules(x, "")


@callback("scadd")
async def cb_schedule_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "sc_group")
    codes = await schedule_group_codes()
    kb = [[btn(code, f"scaddg:{code}")] for code in codes]
    text = "Введите код группы, например ИС-21, или выберите группу из списка (или /cancel)."
    await api.send(x, text, [*kb, [btn("↩️ К расписаниям", "schedules")]])


@state("sc_group")
async def st_sc_group(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, GROUP_HINT)
    await db.set_state(x, "add_sched", {"group": group})
    await api.send(x, f"Отправьте ссылку на расписание группы {group} (http/https). Проверим, что это PDF.")


@callback("scaddg")
async def cb_schedule_add_group(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "add_sched", {"group": norm_group(arg)})
    await api.send(x, f"Отправьте ссылку на расписание группы {norm_group(arg)} (http/https). Проверим, что это PDF.")


@callback("scedit")
async def cb_schedule_edit(x, group):
    if not await need_super(x):
        return
    code = norm_group(group)
    await db.set_state(x, "update_sched", {"group": code})
    await api.send(x, f"Отправьте новую ссылку на расписание группы {code} (http/https). Проверим, что это PDF.")


async def save_schedule(x, group: str, url: str, edit: bool) -> None:
    code = norm_group(as_str(group))
    ok, reason = await probe_pdf_url(url)
    if not ok:
        return await api.send(x, f"⚠️ {reason}\nСсылка не сохранена — попробуйте ещё раз.")
    await repo.upsert_schedule(code, url)
    await notify_schedule_subscribers(code, f"📅 Расписание группы {code} обновлено\n{url}")
    await db.clear_state(x)
    await api.send(x, f"✅ Ссылка на расписание группы {code} сохранена.")
    await audit(x, f"Сис-админ {x} {'изменил' if edit else 'добавил'} ссылку на расписание группы {code}.")
    await cb_schedules(x, "")


@state("add_sched")
async def st_add_sched(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(as_str((p or {}).get("group")))
    if not valid_group(group):
        await db.clear_state(x)
        return await api.send(x, "Не задан код группы. Начните заново: «➕ Добавить / изменить».", BACK)
    await save_schedule(x, group, text.strip(), edit=False)


@state("update_sched")
async def st_update_sched(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(as_str((p or {}).get("group")))
    if not valid_group(group):
        await db.clear_state(x)
        return await api.send(x, "Не задан код группы. Откройте карточку расписания.", BACK)
    await save_schedule(x, group, text.strip(), edit=True)


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


# ── служебные команды и самопроверка ──────────────────────────────────────────
LOG_TAIL = 15  # сколько строк журнала показывать в ответ на /logs
OPEN = ("new", "accepted", "in_progress")


def panel_url() -> str:
    """Адрес веб-панели: из WEBHOOK_URL, иначе — подсказка для режима polling."""
    if config.WEBHOOK_URL:
        return config.WEBHOOK_URL.rsplit("/", 1)[0] + "/panel"
    return "http://<хост>:8080/panel"


def panel_hint() -> str:
    if config.WEB_PANEL_PASSWORD:
        return panel_url()
    return f"{panel_url()} — выключена, задайте WEB_PANEL_PASSWORD в .env"


async def diagnostics_text(x: str) -> str:
    """Сводка о состоянии бота — для кнопки «🧪 Тест и журнал» и команд /test, /diag."""
    st = await repo.stats_overview()
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    log.info("самопроверка: сис-админ %s", x)
    return (
        "🧪 Самопроверка бота\n"
        f"Ваш MAX ID: {x}\n"
        f"Режим: {'webhook' if config.WEBHOOK_URL else 'long polling'}\n"
        f"Журнал: {config.LOG_FILE} (уровень {config.LOG_LEVEL})\n"
        f"Обработано событий: {dedupe['processed']}\n"
        f"Студентов: {st['students']}, сотрудников: {st['staff']}, обращений: {st['total']} "
        f"(открытых {sum(counts.get(code, 0) for code in OPEN)})\n"
        f"Панель: {panel_hint()}"
    )


async def check_api(x: str) -> None:
    """Фоновая проверка связи с MAX API: ответ приходит отдельным сообщением."""
    started = time.monotonic()
    try:
        info = await api.me()
    except Exception as exc:
        log.warning("проверка API не удалась: %s", exc)
        await notify(x, f"❌ MAX API не отвечает: {exc}")
        return
    await notify(x, f"✅ MAX API отвечает ({time.monotonic() - started:.1f} с): {info}")


@callback("diag")
async def cb_diag(x, arg):
    if not await need_super(x):
        return
    await api.send(x, f"{await diagnostics_text(x)}\n\nПроверяю связь с MAX API…", BACK)
    spawn(check_api(x))


async def command(x: str, cmd: str) -> bool:
    """Служебные команды сис-админа. True — обработано (иначе команда считается неизвестной)."""
    if cmd not in ("/logs", "/test", "/diag", "/panel"):
        return False
    if not await need_super(x):
        return True  # не показываем, что команда вообще существует
    if cmd == "/logs":
        records = tail_file(config.LOG_FILE, LOG_TAIL)
        tail = "\n".join(short(line, 150) for line in records) or "Журнал пока пуст."
        dedupe = await repo.dedupe_stats()
        await api.send(
            x, f"🧾 Журнал: {config.LOG_FILE}\nСобытий обработано: {dedupe['processed']}\n\n{tail}", BACK
        )
        return True
    if cmd == "/panel":
        await api.send(x, "🖥 Веб-панель сис-админа\n"
                          f"{panel_hint()}\n\nВход: ваш MAX ID и пароль WEB_PANEL_PASSWORD из .env.", BACK)
        return True
    await api.send(x, f"{await diagnostics_text(x)}\n\nПроверяю связь с MAX API…", BACK)
    spawn(check_api(x))
    return True
