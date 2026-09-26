"""Главное меню и точки входа (/start, home)."""
from datetime import datetime

import config
import database as db
import repository as repo
import timetable as tt
from handlers import schedules
from handlers.admin import audit, command as admin_command, sysadmin_ids
from handlers.common import DEFAULT_WELCOME, BACK, admin_of, api, can_broadcast, is_super, need_super, notify
from handlers.registry import STATES, callback, state
from max_api import btn, link_btn
from timetable import WEEKDAYS_FULL
from utils import as_str, norm_code, norm_group, short, to_int, valid_group


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
        [btn("🔔 Что сделать сегодня", "today"), btn("📋 Все обращения", "staff")],
        [btn("✍️ Создать обращение", "snew"), btn("👥 Расписания", "view_schedules")],
        [btn("👥 Пользователи", "people"), btn("👥 Сотрудники", "admins")],
        [btn("🗝 Коды и заявки", "codes"), btn("📊 Статистика", "stats")],
        [btn("📅 Расписания (PDF)", "schedules"), btn("👥 Группы", "groups")],
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
    await api.send(
        x,
        "Здравствуйте! Это бот колледжа.\nКто вы?",
        [[btn("🎓 Я студент", "who:student"), btn("👔 Я сотрудник", "who:staff")]],
    )


# ── регистрация: студент, сотрудник по коду, заявка ──────────────────────────
@callback("who")
async def cb_who(x, arg):
    kind = as_str(arg)
    if kind == "student":
        await db.set_state(x, "reg_name")
        return await api.send(x, "Укажите ваши ФИО полностью, например: Иванов Иван Иванович.")
    if kind == "staff":
        if await admin_of(x):
            return await show_home(x)
        return await _ask_staff_code(x)
    if kind == "guest":
        return await _guest_home(x)
    return await start(x)


async def _guest_home(x: str):
    """Вход без регистрации: расписание и заявка на роль сотрудника."""
    await db.clear_state(x)
    await api.send(
        x,
        "👤 Ничего страшного — можно пользоваться ботом и без регистрации:\n"
        "📚 посмотреть расписание группы по её коду\n"
        "📥 подать заявку на роль сотрудника\n"
        "Позже сможете зарегистрироваться как студент.",
        [
            [btn("📚 Все расписания", "view_schedules")],
            [btn("🎓 Я всё-таки студент", "who:student"), btn("👔 Я сотрудник", "who:staff")],
        ],
    )


async def _ask_staff_code(x: str):
    await db.set_state(x, "staff_code")
    await api.send(
        x,
        f"🗝 Введите код, который выдал сотрудник или сис-админ ({config.STAFF_CODE_ATTEMPTS} попытки в час).",
        [[btn("📥 Нет кода — подать заявку", "staffreq")], *BACK],
    )


async def _code_locked(x: str, tries: int):
    """Лимит попыток исчерпан: подсказываем заявку и предупреждаем сис-админов."""
    await db.clear_state(x)
    await audit(x, f"{x}: превышен лимит попыток ввода кода сотрудника ({tries}).")
    await api.send(
        x,
        f"🚫 Лимит попыток исчерпан ({tries}). Подождите час и попробуйте снова.\n"
        "Если код не помогает — подайте заявку, её рассмотрит сис-админ.",
        [[btn("📥 Подать заявку", "staffreq")], *BACK],
    )


@state("staff_code")
async def st_staff_code(x, text, p):
    if await admin_of(x):
        await db.clear_state(x)
        return await show_home(x)
    code = norm_code(text)
    if not code:
        return await api.send(x, "Код состоит из букв и цифр. Введите его ещё раз или /cancel.")
    await repo.note_attempt(x)
    tries = await repo.attempts_count(x)
    ok, reason = await repo.use_invite(code, x)
    if ok:
        await repo.clear_attempts(x)
        await db.set_state(x, "staff_join_name", {"code": code})
        return await api.send(x, "✅ Код принят.\nВведите ФИО — так вас увидят студенты.")
    if tries >= config.STAFF_CODE_ATTEMPTS:
        return await _code_locked(x, tries)
    return await api.send(x, f"❌ {reason}\nПопыток в этом часе: {tries}/{config.STAFF_CODE_ATTEMPTS}.")


@state("staff_join_name")
async def st_staff_join_name(x, text, p):
    name = _clean_fio(text)
    if not _valid_fio(name):
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя).")
    await db.set_state(x, "staff_join_position", {"name": name})
    return await api.send(
        x,
        f"Должность сотрудника {name} — напишите свободным текстом, например «Преподаватель математики».\n"
        "Этот текст увидят студенты. Отправьте «-», если должность назначит сис-админ.",
    )


@state("staff_join_position")
async def st_staff_join_position(x, text, p):
    position = _clean_fio(text)[:100]
    if not position:
        return await api.send(x, "Введите должность текстом или «-», если её назначит сис-админ.")
    name = (p or {}).get("name", "")
    await db.set_state(x, "staff_join_office", {"name": name, "position": "" if position == "-" else position})
    return await api.send(x, f"Кабинет сотрудника {name} (например, 214) или «-», если кабинета нет.")


@state("staff_join_office")
async def st_staff_join_office(x, text, p):
    if await admin_of(x):
        await db.clear_state(x)
        return await show_home(x)
    office = _clean_fio(text)[:100]
    payload = p or {}
    name = payload.get("name", "")
    if not name:
        await db.clear_state(x)
        return await start(x)
    await repo.add_staff(x, name, position=payload.get("position", ""), office="" if office == "-" else office)
    await db.clear_state(x)
    await notify(
        x,
        "🏫 Вы зарегистрированы как сотрудник. Если должность указана неверно — "
        "попросите сис-админа исправить её в карточке сотрудника.",
    )
    return await show_home(x)


@callback("staffreq")
async def cb_staff_request(x, arg):
    """Заявка на роль сотрудника — для тех, у кого нет кода."""
    if await admin_of(x):
        return await show_home(x)
    await db.set_state(x, "req_name")
    return await api.send(x, "📥 Заявка на роль сотрудника.\nВведите ФИО — так вас увидят студенты.")


@state("req_name")
async def st_req_name(x, text, p):
    name = _clean_fio(text)
    if not _valid_fio(name):
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя).")
    await db.set_state(x, "req_position", {"name": name})
    return await api.send(
        x,
        "Должность: напишите свободным текстом, например «Секретарь учебной части».\n"
        "Или «-», если затрудняетесь — уточним при рассмотрении заявки.",
    )


@state("req_position")
async def st_req_position(x, text, p):
    position = _clean_fio(text)[:100]
    if not position:
        return await api.send(x, "Введите должность текстом или «-».")
    name = (p or {}).get("name", "")
    await db.set_state(x, "req_office", {"name": name, "position": "" if position == "-" else position})
    return await api.send(x, "Кабинет (например, 214) или «-», если кабинета нет.")


@state("req_office")
async def st_req_office(x, text, p):
    office = _clean_fio(text)[:100]
    payload = p or {}
    await db.set_state(x, "req_note", {"name": payload.get("name", ""),
                                       "position": payload.get("position", ""),
                                       "office": "" if office == "-" else office})
    return await api.send(x, "Комментарий для сис-админа (например, кто вас назначил) или «-».")


@state("req_note")
async def st_req_note(x, text, p):
    if await admin_of(x):
        await db.clear_state(x)
        return await show_home(x)
    note = _clean_fio(text)[:300]
    payload = p or {}
    name = payload.get("name", "")
    if not name:
        await db.clear_state(x)
        return await start(x)
    await repo.create_staff_request(x, name, payload.get("position", ""), payload.get("office", ""),
                                    "" if note == "-" else note)
    await db.clear_state(x)
    for uid in sysadmin_ids():
        await notify(
            uid,
            f"📥 Новая заявка на роль сотрудника: {name} (ID {x}).",
            [[btn("✅ Открыть заявку", f"req:{x}")]],
        )
    return await api.send(
        x,
        "📥 Заявка отправлена сис-админам — обычно отвечают в рабочее время.\n"
        "Пока можно смотреть расписание.",
        [[btn("📚 Все расписания", "view_schedules")], *BACK],
    )


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
    """Строка users; если человек не зарегистрирован — предлагает зарегистрироваться."""
    user = await repo.get_user(x)
    if not user:
        await start(x)
    return user


async def need_author(x: str):
    """Кто пишет обращение: студент из users или сис-админ.

    Сис-админу обращения тоже нужны — например, чтобы обратиться к коллеге или
    проверить цепочку целиком, поэтому он допускается наравне со студентом.
    """
    user = await repo.get_user(x)
    if user:
        return user
    a = await admin_of(x)
    if a and is_super(a):
        return {"full_name": f"{a['full_name']} · сис-админ", "group_code": "сис-админ"}
    await start(x)
    return None


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
    selected = arg[6:] if arg.startswith("sched:") else arg
    user = await repo.get_user(x)
    if not user and not selected:
        await need_student(x)
        return
    group = _group_code(selected.split(":", 1)[0]) if selected else _group_code(_row_value(user, "group_code"))
    return await send_schedule(x, group, back_to_list=bool(arg))


async def send_schedule(x: str, group: str, back_to_list: bool = False, view: str = "week") -> None:
    """Показывает расписание группы: разобранные занятия, иначе ссылку на PDF.

    view: week — вся неделя, day — один день, next — ближайшие занятия.
    """
    group = _group_code(group)
    row = await repo.get_schedule(group)
    if not row:
        return await api.send(x, f"Расписание группы {group} пока не добавлено.", BACK)
    url = _row_value(row, "pdf_url")
    result = await schedules.parse_group(group)
    label = await _schedule_subscription_label(x, group)
    keyboard: list = []
    tail = [btn(label, f"schedsub:{group}")]
    if back_to_list:
        tail.append(btn("⬅️ К списку", "view_schedules"))
    else:
        tail.extend(BACK[0])

    if not result.has_lessons:
        # разбор не получился — отдаём ссылку, как раньше, и честно говорим об этом
        hint = f"\n(разобрать не удалось: {short(result.reason, 80)})" if result.reason else ""
        return await api.send(
            x, f"📅 Расписание группы {group} — PDF{hint}\n{url}",
            [[link_btn("Открыть расписание", url)], tail],
        )

    schedule = result.schedule
    if view == "day":
        today = schedule.day(datetime.now().weekday())
        text = tt.format_day(today) if today and not today.is_empty else f"📅 На {WEEKDAYS_FULL[datetime.now().weekday()]} пар нет"
    elif view == "next":
        text = tt.format_upcoming(schedule) or "⏰ Ближайших занятий не найдено"
    else:
        text = tt.format_schedule(schedule)
    today_weekday = datetime.now().weekday()
    keyboard = [
        [btn("📆 Сегодня", f"schedday:{group}:{today_weekday}"),
         btn("⏰ Ближайшие", f"schednext:{group}")],
        [btn("📚 Вся неделя", f"sched:{group}")],
    ]
    await api.send(x, text, [*keyboard, tail])


@callback("schedday")
async def cb_schedule_day(x, arg):
    raw = str(arg or "")
    if raw.startswith("schedday:"):
        raw = raw[9:]
    group, _, weekday = raw.partition(":")
    result = await schedules.parse_group(_group_code(group))
    if not result.has_lessons:
        return await send_schedule(x, group)
    day = result.schedule.day(to_int(weekday, datetime.now().weekday()))
    text = tt.format_day(day) if day and not day.is_empty else "📅 На этот день пар нет"
    label = await _schedule_subscription_label(x, group)
    return await api.send(x, text, [
        [btn("📚 Вся неделя", f"sched:{group}"), btn("⏰ Ближайшие", f"schednext:{group}")],
        [btn(label, f"schedsub:{group}"), btn("⬅️ К списку", "view_schedules")],
    ])


@callback("schednext")
async def cb_schedule_next(x, arg):
    raw = str(arg or "")
    if raw.startswith("schednext:"):
        raw = raw[10:]
    return await send_schedule(x, _group_code(raw.split(":", 1)[0]), view="next")


@callback("schedreload")
async def cb_schedule_reload(x, arg):
    """Перечитывает PDF заново — когда наcollege выложили новый файл."""
    raw = str(arg or "")
    if raw.startswith("schedreload:"):
        raw = raw[11:]
    group = _group_code(raw.split(":", 1)[0])
    if not await repo.get_schedule(group):
        return await api.send(x, f"Расписание группы {group} пока не добавлено.", BACK)
    result = await schedules.parse_group(group, force=True)
    text = tt.format_schedule(result.schedule) if result.has_lessons else f"Обновить не вышло: {result.reason}"
    return await api.send(x, text, [[btn("📚 Вся неделя", f"sched:{group}"), *BACK[0]]])


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
