"""Главное меню и точки входа (/start, home)."""
import database as db
import repository as repo
from handlers.admin import command as admin_command
from handlers.common import DEFAULT_WELCOME, BACK, admin_of, api, can_broadcast, is_super, need_super
from handlers.registry import STATES, callback, state
from max_api import btn, link_btn
from utils import norm_group, valid_group


# ── входящие сообщения и команды ──────────────────────────────────────────────
async def on_message(x: str, text: str):
    cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    if cmd == "/start":
        return await start(x)
    if cmd == "/id":
        return await api.send(x, f"Ваш MAX ID: {x}")
    if cmd == "/cancel":
        await db.clear_state(x)
        return await show_home(x)
    if cmd == "/supersecret_admin":  # старая скрытая команда — то же, что кнопка «🔐 Сис-админ»
        if is_super(await admin_of(x)):
            await db.clear_state(x)
            return await sysadmin_menu(x)
        return  # для остальных команды «не существует»
    if cmd and await admin_command(x, cmd):  # служебные команды сис-админа: /logs, /test, /panel
        return
    st = await db.get_state(x)
    if st and st["state"] in STATES:
        return await STATES[st["state"]](x, text, st["payload"])
    if not (await admin_of(x) or await repo.is_registered(x)):
        return await start(x)
    await api.send(x, "Используйте кнопки меню. Команды: /start, /cancel, /id")
    return await show_home(x)


def student_menu():
    return [
        [btn("🎓 Учебная часть", "academic"), btn("💰 Бухгалтерия", "accounting")],
        [btn("💬 Обратная связь", "new:feedback"), btn("📅 Моё расписание", "sched")],
        [btn("📚 Все расписания", "view_schedules")],
        [btn("📋 Мои обращения", "tickets"), btn("👤 Мой профиль", "profile")],
    ]


def _clean_fio(value) -> str:
    return " ".join(str(value or "").split())


def _valid_fio(value) -> bool:
    value = _clean_fio(value)
    return len([part for part in value.replace(":", " ").split() if part]) >= 2 and len(value) <= 100


def _group_code(value) -> str:
    value = "" if value is None else str(value).strip()
    return norm_group(value)


def _row_value(row, key, default=""):
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _group_from_row(row) -> str:
    value = row
    if isinstance(row, dict):
        value = row.get("group_code") or row.get("code") or row.get("group") or row.get("name")
    elif isinstance(row, (tuple, list)):
        value = row[0] if row else ""
    else:
        value = _row_value(row, "group_code", None)
        if value is None:
            value = getattr(row, "group_code", None)
        if value is None:
            value = getattr(row, "code", None)
        if value is None:
            value = row
    return _group_code(value)


async def _active_group_rows():
    lister = getattr(repo, "list_groups", None)
    if lister is None:
        lister = getattr(repo, "groups", None)
    if lister is None:
        return []
    rows = await lister(active_only=True)
    return [row for row in rows or [] if _row_value(row, "active", 1) not in (0, False, "0")]


async def _schedule_subscription_label(x: str, group: str) -> str:
    checker = getattr(repo, "is_schedule_subscribed", None)
    subscribed = bool(await checker(x, group)) if checker is not None else False
    return "🔕 Отписаться от обновлений" if subscribed else "🔔 Подписаться на обновления"


async def _group_allowed(group) -> bool:
    rows = await _active_group_rows()
    if not rows:
        return True
    code = _group_code(group)
    return any(_group_from_row(row) == code for row in rows)


async def _save_user(user_id, fio, group) -> None:
    fio = _clean_fio(fio)
    group = _group_code(group)
    adder = getattr(repo, "add_user", None)
    if adder is not None:
        await adder(user_id, fio, group)
    else:
        await repo.upsert_user(user_id, fio, group)


async def _group_confirmation(x: str, group: str, fio: str) -> None:
    group = _group_code(group)
    fio = _clean_fio(fio)
    await db.set_state(x, "reg_group", {"name": fio, "group": group})
    await api.send(
        x,
        f"⚠️ Группа {group} не найдена в активном справочнике. Сохранить её?",
        [[btn("✅ Сохранить", f"regok:{group}:{fio}"), btn("✏️ Изменить", "editname")]],
    )


async def _save_or_confirm(x: str, fio: str, group: str, after_save) -> None:
    group = _group_code(group)
    fio = _clean_fio(fio)
    if not _valid_fio(fio) or not valid_group(group):
        return
    if not await _group_allowed(group):
        await _group_confirmation(x, group, fio)
        return
    await _save_user(x, fio, group)
    await db.clear_state(x)
    await after_save()


async def _registration_saved(x: str, fio: str, group: str) -> None:
    await api.send(x, f"✅ Регистрация завершена: {fio}, группа {group}.")
    if hasattr(repo, "get_admin") and hasattr(repo, "is_registered") and hasattr(db, "get_setting"):
        await show_home(x)
    else:
        await api.send(x, "Выберите действие:", student_menu())


def staff_menu(a):
    rows = [[btn("📋 Мои обращения", "staff")], [btn("📊 Статистика", "staffstats")]]
    if can_broadcast(a):
        rows.append([btn("📢 Рассылка", "broadcast")])
    if is_super(a):  # переход в панель сис-админа — кнопкой, для тех, кто вписан в .env
        rows.append([btn("🔐 Сис-админ", "sysadm")])
    return rows


async def sysadmin_menu(x: str):
    await db.clear_state(x)
    await api.send(x, "🔐 Панель сис-админа", super_menu())


def super_menu():
    return [
        [btn("📋 Все обращения", "staff"), btn("📊 Статистика", "stats")],
        [btn("👥 Сотрудники", "admins"), btn("📅 Расписания", "schedules")],
        [btn("👥 Группы", "groups")],
        [btn("📢 Рассылка", "broadcast"), btn("⚙️ Настройки", "settings")],
        [btn("🧪 Тест и журнал", "diag")],
    ]


async def show_home(x: str):
    a = await admin_of(x)
    if a:
        return await api.send(x, "🏫 Кабинет сотрудника", staff_menu(a))
    if not await repo.is_registered(x):
        return await start(x)
    return await api.send(x, await db.get_setting("welcome_text", DEFAULT_WELCOME), student_menu())


@state("reg_name")
async def st_reg_name(x, text, p):
    name = _clean_fio(text)
    if not _valid_fio(name):
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя), например: Иванов Иван Иванович.")
    await db.set_state(x, "reg_group", {"name": name})
    await api.send(x, "Укажите код вашей группы, например: ИС-21.")


@state("reg_group")
async def st_reg_group(x, text, p):
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, "Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\nНапример: ИС-21. Попробуйте ещё раз.")
    payload = p or {}
    fio = _clean_fio(payload.get("name", ""))
    if not _valid_fio(fio):
        return await start(x)
    if not await _group_allowed(group):
        return await _group_confirmation(x, group, fio)
    await _save_user(x, fio, group)
    await db.clear_state(x)
    return await _registration_saved(x, fio, group)


@state("registration_name")
async def st_registration_name(x, text, p):
    return await st_reg_name(x, text, p)


@state("registration_group")
async def st_registration_group(x, text, p):
    return await st_reg_group(x, text, p)


@callback("registration_name")
async def cb_registration_name(x, arg):
    if arg:
        name = _clean_fio(arg)
        if _valid_fio(name):
            await db.set_state(x, "reg_group", {"name": name})
            return await api.send(x, "Укажите код вашей группы, например: ИС-21.")
    await db.set_state(x, "reg_name")
    return await api.send(x, "Укажите ФИО полностью, например: Иванов Иван Иванович.")


@callback("registration_group")
async def cb_registration_group(x, arg):
    if arg:
        user = await repo.get_user(x)
        fio = _row_value(user, "full_name") if user else ""
        if _valid_fio(fio):
            return await st_reg_group(x, arg, {"name": fio})
    await db.set_state(x, "reg_group", {"name": ""})
    return await api.send(x, "Укажите код вашей группы, например: ИС-21.")


@callback("home")
async def cb_home(x, arg):
    await show_home(x)


@callback("sysadm")
async def cb_sysadmin(x, arg):
    """Кнопка перехода в панель сис-админа (доступна только тем, чей ID в .env)."""
    if await need_super(x):
        await sysadmin_menu(x)


async def start(x: str):
    """Точка входа: регистрация нового пользователя или показ главного меню."""
    await db.clear_state(x)
    if await admin_of(x) or await repo.is_registered(x):
        return await show_home(x)
    await db.set_state(x, "reg_name")
    await api.send(x, "Здравствуйте! Это бот колледжа.\nУкажите ваши ФИО полностью, например: Иванов Иван Иванович.")


@callback("academic")
async def cb_academic(x, arg):
    if not await need_student(x):
        return
    await api.send(
        x,
        "🎓 Учебная часть\nВыберите тему обращения:",
        [
            [btn("📚 Учёба", "topic:academic:study"), btn("🗓 Период обучения", "topic:academic:period")],
            [btn("💼 Вакансии", "topic:academic:vacancies"), btn("✍️ Подать заявку", "new:academic")],
            [btn("⬅️ Назад", "back")],
        ],
    )


@callback("accounting")
async def cb_accounting(x, arg):
    if not await need_student(x):
        return
    await api.send(
        x,
        "💰 Бухгалтерия\nВыберите тему обращения:",
        [
            [btn("🎓 Стипендия", "topic:accounting:scholarship"), btn("✍️ Подать заявку", "new:accounting")],
            [btn("⬅️ Назад", "back")],
        ],
    )


@callback("back")
async def cb_back(x, arg):
    menu = student_menu()
    await db.clear_state(x)
    await api.send(x, "Выберите действие:", menu)
    return menu


# ── профиль студента ──────────────────────────────────────────────────────────
async def need_student(x: str):
    """Строка users; если пользователя нет — запускаем регистрацию и возвращаем None."""
    user = await repo.get_user(x)
    if not user:
        await start(x)
    return user


@callback("profile")
async def cb_profile(x, arg):
    user = await need_student(x)
    if user:
        full_name = _row_value(user, "full_name")
        group = _row_value(user, "group_code")
        await api.send(
            x,
            f"👤 Профиль\nФИО: {full_name}\nГруппа: {group}",
            [[btn("✏️ Изменить ФИО", "pf:name"), btn("✏️ Изменить группу", "pf:group")], *BACK],
        )


@callback("pf")
async def cb_profile_edit(x, arg):
    if not await need_student(x):
        return
    prompts = {"name": ("edit_name", "Введите новые ФИО (или /cancel)."),
               "group": ("edit_group", "Введите новый код группы (или /cancel).")}
    if arg in prompts:
        state_name, prompt = prompts[arg]
        await db.set_state(x, state_name)
        await api.send(x, prompt)


@state("edit_name")
async def st_edit_name(x, text, p):
    name = _clean_fio(text)
    if not _valid_fio(name):
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя).")
    user = await repo.get_user(x)
    if not user:
        await db.set_state(x, "reg_group", {"name": name})
        return await api.send(x, "Укажите код вашей группы, например: ИС-21.")
    await repo.set_user_name(x, name)
    await db.clear_state(x)
    return await cb_profile(x, "")


@callback("editname")
async def cb_editname(x, arg):
    await db.set_state(x, "edit_name")
    return await api.send(x, "Введите ФИО полностью (или /cancel).")


@state("edit_group")
async def st_edit_group(x, text, p):
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, "Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\nНапример: ИС-21. Попробуйте ещё раз.")
    user = await repo.get_user(x)
    fio = _row_value(user, "full_name") if user else ""
    return await _save_or_confirm(x, fio, group, lambda: cb_profile(x, ""))


@callback("regok")
async def cb_regok(x, payload):
    payload = str(payload or "")
    raw = payload if payload.startswith("regok:") else f"regok:{payload}"
    parts = raw.split(":", 2)
    if len(parts) != 3 or parts[0] != "regok":
        return
    group, fio = _group_code(parts[1]), _clean_fio(parts[2])
    if not valid_group(group) or not _valid_fio(fio):
        return
    await _save_user(x, fio, group)
    await db.clear_state(x)
    menu = student_menu()
    await api.send(x, f"✅ Регистрация завершена: {fio}, группа {group}.", menu)
    return menu


@callback("savegrp")
async def cb_savegrp(x, arg):
    if not arg:
        return
    group = arg.split(":", 1)[0]
    user = await repo.get_user(x)
    fio = _row_value(user, "full_name") if user else ""
    return await _save_or_confirm(x, fio, group, lambda: cb_profile(x, ""))


@callback("saveprofile")
async def cb_saveprofile(x, arg):
    if not arg:
        return
    user = await repo.get_user(x)
    if not user:
        return
    name = _clean_fio(_row_value(user, "full_name"))
    group = _group_code(_row_value(user, "group_code"))
    changed_name = False
    changed_group = False
    for item in arg.split(";"):
        key, separator, value = item.partition("=")
        if not separator:
            key, separator, value = item.partition(":")
        if not separator:
            continue
        key = key.strip().lower()
        if key in ("name", "fio") and _valid_fio(value):
            name = _clean_fio(value)
            changed_name = True
        elif key == "group" and valid_group(_group_code(value)):
            group = _group_code(value)
            changed_group = True
    if not changed_name and not changed_group:
        return
    if changed_group and not await _group_allowed(group):
        return await _group_confirmation(x, group, name)
    await _save_user(x, name, group)
    await db.clear_state(x)
    return await cb_profile(x, "")


@callback("done")
async def cb_done(x, arg):
    if await repo.get_user(x):
        await db.clear_state(x)
        return await api.send(x, "✅ Готово.", student_menu())
    return await start(x)


# ── расписание ────────────────────────────────────────────────────────────────
@callback("view_schedules")
async def cb_view_schedules(x, arg):
    if not await need_student(x):
        return
    rows = await _active_group_rows()
    schedule_lister = getattr(repo, "schedule_groups", None)
    schedule_rows = await schedule_lister() if schedule_lister is not None else []
    codes = []
    for row in list(rows or []) + list(schedule_rows or []):
        code = _group_from_row(row)
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return await cb_schedule(x, arg)
    keyboard = []
    for code in codes:
        label = await _schedule_subscription_label(x, code)
        keyboard.append([btn(f"📅 {code}", f"sched:{code}"), btn(label, f"schedsub:{code}")])
    await api.send(x, "📅 Выберите группу для расписания:", [*keyboard, *BACK])


@callback("sched")
async def cb_schedule(x, arg):
    arg = str(arg or "")
    user = await need_student(x)
    if not user:
        return
    selected = arg[6:] if arg.startswith("sched:") else arg
    group = _group_code(selected.split(":", 1)[0]) if selected else _group_code(_row_value(user, "group_code"))
    row = await repo.get_schedule(group)
    if not row:
        return await api.send(x, f"Расписание группы {group} пока не добавлено.", BACK)
    url = _row_value(row, "pdf_url")
    label = await _schedule_subscription_label(x, group)
    keyboard = [[link_btn("Открыть расписание", url)], [btn(label, f"schedsub:{group}")]]
    if arg:
        keyboard.append([btn("⬅️ К списку", "view_schedules")])
    else:
        keyboard.extend(BACK)
    await api.send(x, f"📅 Расписание группы {group}:\n{url}", keyboard)


@callback("schedsub")
async def cb_schedsub(x, arg):
    raw = str(arg or "")
    if raw.startswith("schedsub:"):
        raw = raw[8:]
    group = _group_code(raw.split(":", 1)[0])
    if not valid_group(group):
        return await api.send(x, "Код группы для подписки указан неверно.", BACK)
    user = await need_student(x)
    if not user:
        return
    row = await repo.get_schedule(group)
    if not row:
        return await api.send(x, f"Расписание группы {group} пока не добавлено.", BACK)
    checker = getattr(repo, "is_schedule_subscribed", None)
    subscribed = bool(await checker(x, group)) if checker is not None else False
    if subscribed:
        deleter = getattr(repo, "delete_schedule_subscription", None)
        if deleter is not None:
            await deleter(x)
        text = f"🔕 Вы отписались от обновлений расписания группы {group}."
    else:
        setter = getattr(repo, "set_schedule_subscription", None)
        if setter is not None:
            await setter(x, group)
        text = f"🔔 Вы подписались на обновления расписания группы {group}."
    keyboard = [[link_btn("Открыть расписание", _row_value(row, "pdf_url"))], [btn("⬅️ К списку", "view_schedules")]]
    await api.send(x, text, keyboard)
