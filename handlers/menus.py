"""Главное меню и точки входа (/start, home)."""
import database as db
import repository as repo
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
    st = await db.get_state(x)
    if st and st["state"] in STATES:
        return await STATES[st["state"]](x, text, st["payload"])
    if not (await admin_of(x) or await repo.is_registered(x)):
        return await start(x)
    await api.send(x, "Используйте кнопки меню. Команды: /start, /cancel, /id")
    return await show_home(x)


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
        [btn("📢 Рассылка", "broadcast"), btn("⚙️ Настройки", "settings")],
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
    name = " ".join(text.split())
    if len(name.split()) < 2 or len(name) > 100:
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя), например: Иванов Иван Иванович.")
    await db.set_state(x, "reg_group", {"name": name})
    await api.send(x, "Укажите код вашей группы, например: ИС-21.")


@state("reg_group")
async def st_reg_group(x, text, p):
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, "Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\nНапример: ИС-21. Попробуйте ещё раз.")
    await repo.upsert_user(x, p["name"], group)
    await db.clear_state(x)
    await api.send(x, f"✅ Регистрация завершена: {p['name']}, группа {group}.")
    await show_home(x)


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
        await api.send(
            x,
            f"👤 Профиль\nФИО: {user['full_name']}\nГруппа: {user['group_code']}",
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
    name = " ".join(text.split())
    if len(name.split()) < 2 or len(name) > 100:
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя).")
    await repo.set_user_name(x, name)
    await db.clear_state(x)
    await cb_profile(x, "")


@state("edit_group")
async def st_edit_group(x, text, p):
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, "Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\nНапример: ИС-21. Попробуйте ещё раз.")
    await repo.set_user_group(x, group)
    await db.clear_state(x)
    await cb_profile(x, "")


# ── расписание ────────────────────────────────────────────────────────────────
@callback("sched")
async def cb_schedule(x, arg):
    user = await need_student(x)
    if not user:
        return
    row = await repo.get_schedule(user["group_code"])
    if not row:
        return await api.send(x, f"Расписание группы {user['group_code']} пока не добавлено.", BACK)
    await api.send(
        x,
        f"📅 Расписание группы {user['group_code']}:\n{row['pdf_url']}",
        [[link_btn("Открыть расписание", row["pdf_url"])], *BACK],
    )
