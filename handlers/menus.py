"""Главное меню и точки входа (/start, home)."""

import clock
import config
import college
import database as db
import repository as repo
import timetable as tt
from handlers import faq, schedules
from handlers.admin import audit, command as admin_command, sysadmin_ids
from handlers.common import (BACK, admin_of, api, can_broadcast, is_super, log, need_super,
                             notify, welcome_text)
from bot_commands import command_payload
from handlers.registry import CALLBACKS, STATES, callback, state
from max_api import BUTTON_TEXT, MAX_ROWS, btn, link_btn
from timetable import WEEKDAYS_FULL
from utils import (OPEN_STATUSES, STATUS, STATUS_SHORT, as_str, cut_plain, fmt_time, group_code,
                    group_digits, has_position, norm_code, norm_group, short, to_int, valid_group)


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
    if cmd.split(":")[0] == "/join":
        # приглашение по ссылке: /join ABC123. Без кода - обычный вход кодом
        parts = text.split()
        code = parts[1] if len(parts) > 1 else ""
        if not code and ":" in parts[0]:
            code = parts[0].partition(":")[2]
        if not code:
            return await _ask_staff_code(x)
        return await use_staff_code(x, code)
    # Команда из нижнего меню MAX: /today, today или /today@bot ведут туда же,
    # куда кнопка, - чтобы меню не приводило в пустоту.
    if cmd:
        payload = command_payload(cmd)
        if payload and payload.split(":")[0] in CALLBACKS:
            if payload == "sysadm" and not is_super(await admin_of(x)):
                return  # закрытая команда: молча, как и кнопка сис-админа
            if payload == "staff" and not (await admin_of(x)):
                return await show_home(x)
            # аргумент команды: и после двоеточия (/view:student), и после
            # пробела (/view student) - как в обычных консольных командах
            arg = payload.partition(":")[2]
            if not arg:
                word, _, rest = text.strip().partition(" ")
                arg = rest.strip() or (word.partition(":")[2] if ":" in word else "")
            return await CALLBACKS[payload.split(":")[0]](x, arg)
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
    # Студент написал вопрос обычным текстом: сначала пробуем ответить сами.
    # Не нашли - не выдумываем, а показываем меню и кнопку частых вопросов.
    if await faq.ask_enabled() and (await faq.find_answer(x, text))[0]:
        return await faq.answer_text(x, text)
    await api.send(x, "Используйте кнопки меню. Команды: /start, /cancel, /id\n"
                      "Вопрос можно задать голосом текстом - ответы ищутся в частых вопросах.",
                   [[btn("❓ Частые вопросы", "faq")]])
    return await show_home(x)


def student_menu():
    """Три подменю и три частые кнопки - вместо россыпи кнопок в один экран.

    Подменю «Справка», «Бухгалтерия» и «Обратная связь» раскрываются
    конкретными вопросами, а не общими категориями.
    """
    # «Ошибка в боте» живёт внутри «Обратной связи», а контакты - внутри
    # частых вопросов: меню студента не должно быть россыпью второстепенных
    # кнопок ради одной жалобы.
    # Ряды строим по две кнопки. В ряду из трёх и более на телефоне подпись
    # режется многоточием - это видно на скриншоте из MAX: «📅 Расписание» и
    # «📋 Обращения» превращались в «Расп...» и «Обр...». В двух кнопках
    # помещается 16 ячеек, и это единственная измеренная величина.
    # «Обратная связь» - длинная, поэтому стоит в своём ряду.
    return [
        [btn("📄 Справка", "sub:cert"), btn("💰 Бухгалтерия", "sub:acc")],
        [btn("💬 Обратная связь", "sub:fb")],
        [btn("📅 Расписание", "sched"), btn("📋 Обращения", "tickets")],
        [btn("👤 Профиль", "profile"), btn("❓ Вопросы", "faq")],
    ]


CONSENT_VERSION = "1.0"
CONSENT_TEXT = (
    "Для работы бота колледж хранит ваши данные: ФИО, код группы, ваш MAX ID и "
    "переписку по обращениям. Они нужны, чтобы отвечать вам и сотрудникам, и не "
    "передаются третьим лицам.\n\n"
    "Согласие можно отозвать в любой момент: сообщите в учебную часть, и данные "
    "будут удалены."
)


async def consent_text() -> str:
    """Текст согласия: сис-админ может подставить свою редакцию."""
    return (await db.get_setting("consent_text", "")) or CONSENT_TEXT


def _clean_fio(value) -> str:
    return " ".join(str(value or "").split())


def _valid_fio(value) -> bool:
    value = _clean_fio(value)
    return len([part for part in value.replace(":", " ").split() if part]) >= 2 and len(value) <= 100


def _group_code(value) -> str:
    """Единый вид кода группы (тот же, что в repository и справочнике)."""
    return group_code("" if value is None else str(value).strip())


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


GROUPS_PAGE = 12      # групп на экране выбора: 30 строк кнопок - предел MAX
GROUPS_ALL = 300      # справочник целиком, колледж даёт больше 25 групп


async def _active_group_rows():
    lister = getattr(repo, "list_groups", None)
    if lister is None:
        lister = getattr(repo, "groups", None)
    if lister is None:
        return []
    rows = await lister(active_only=True)
    return [row for row in rows or [] if _row_value(row, "active", 1) not in (0, False, "0")]


async def _schedule_subscription_label(x: str, group: str) -> str:
    """Кнопка подписки на обновления расписания.

    Подпись короткая: MAX рисует кнопку в одну строку и обрезает
    «🔔 Подписаться на обновления» многоточием. Смысл кнопки объясняет
    текст над ней - функция sub_hint.
    """
    checker = getattr(repo, "is_schedule_subscribed", None)
    subscribed = bool(await checker(x, group)) if checker is not None else False
    return "🔕 Отписка" if subscribed else "🔔 Подписка"


def sub_hint(label: str) -> str:
    """Пояснение к кнопке подписки: в кнопке слова не помещаются."""
    if label.startswith("🔕"):
        return "🔕 «Отписка» — больше не присылать обновления расписания группы."
    return "🔔 «Подписка» — бот напишет сам, когда расписание группы изменится."


async def _group_allowed(group) -> bool:
    rows = await _active_group_rows()
    if not rows:
        return True
    code = _group_code(group)
    return any(_group_from_row(row) == code for row in rows)


async def _consent_ok(x: str, fio: str, group: str) -> bool:
    """Показывает ли согласие на обработку данных. False - надо ответить.

    Согласие спрашивается только при первой регистрации: у того, кто уже есть
    в базе, данные обрабатываются давно, и переспрашивать на каждой правке
    группы бессмысленно.
    """
    checker = getattr(repo, "consent_of", None)
    if checker is not None:
        if (await checker(x)).get("at"):
            return True
    if await repo.get_user(x):
        return True
    await db.set_state(x, "consent", {"fio": fio, "group": group})
    await api.send(
        x, f"📄 Согласие на обработку данных\n\n{await consent_text()}",
        [[btn("✅ Согласиться", "consentyes")],
         [btn("❌ Не согласен", "consentno")]])
    return False


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
    if not await _consent_ok(x, fio, group):
        return
    await _save_user(x, fio, group)
    await db.clear_state(x)
    await after_save()


@callback("consentyes")
async def cb_consent_yes(x, arg):
    """Согласие дано: сохраняем регистрацию с датой и редакцией текста."""
    st = await db.get_state(x)
    payload = st["payload"] if st and st["state"] == "consent" else {}
    fio, group = payload.get("fio", ""), payload.get("group", "")
    if not group:
        return await api.send(x, "Регистрация не начата — напишите /start.", BACK)
    await _save_user(x, fio, group)
    # согласие помечаем после записи профиля: upsert пересоздаёт строку
    # и иначе стёр бы только что проставленную дату
    await repo.give_consent(x, CONSENT_VERSION)
    await db.clear_state(x)
    return await _registration_saved(x, fio, group)


@callback("consentno")
async def cb_consent_no(x, arg):
    """Отказ без согласия - осознанный, а не ошибка."""
    await db.clear_state(x)
    await api.send(x, "Понял, без согласия регистрацию продолжить нельзя.\n\n"
                      "Данные о вас не сохранены. Вопросы по работе бота - в учебной части.",
                      [[btn("🏠 В меню", "home")]])


async def _registration_saved(x: str, fio: str, group: str) -> None:
    await api.send(x, f"✅ Регистрация завершена: {fio}, группа {group}.")
    if hasattr(repo, "get_admin") and hasattr(repo, "is_registered") and hasattr(db, "get_setting"):
        await show_home(x)
    else:
        await api.send(x, "Выберите действие:", student_menu())


# Подпись кнопки раздела. Сам экран - в handlers.tickets рядом с очередью:
# tickets и так берёт need_author из menus, и наоборот импортировать нельзя.
SECTION_BTN = "🗂 Мой раздел"

NO_POSITION_LINE = (
    "\n⚠️ Должность не заполнена, поэтому своего раздела нет. Попросите сис-админа "
    "заполнить её: панель → «Сотрудники» → ваша строка → должность."
)


def staff_menu(a):
    rows = []
    if staff_position_filled(a):
        rows.append([btn(SECTION_BTN, "mysection")])
    rows.append([btn("📬 Обращения", "staff"), btn("📊 Статистика", "staffstats")])
    if can_broadcast(a):
        rows.append([btn("📢 Рассылка", "broadcast")])
    if is_super(a):  # переход в панель сис-админа — кнопкой, для тех, кто вписан в .env
        rows.append([btn("🔐 Сис-админ", "sysadm")])
    return rows


def staff_position_filled(a) -> bool:
    """Заполнена ли должность сотрудника. Пусто - значит, своего раздела нет."""
    return has_position(_row_value(a, "position"))


async def sysadmin_menu(x: str):
    """Кабинет сис-админа. Переключатель режимов есть всегда - из него и
    возвращаются, поэтому он должен быть тут, а не только в меню студента."""
    await db.clear_state(x)
    await api.send(x, "🔐 Панель сис-админа\n"
                 + MENU_VIEW_HINT["admin"],
                 [*await super_menu(x), *await view_switcher(x)])


MENU_VIEWS = {"admin": "⚙️ Сис-админ", "staff": "🏫 Сотрудник", "student": "🎓 Студент"}
MENU_VIEW_HINT = {
    "admin": "Режим сис-админа: видно всё, включая настройки.",
    "staff": "Режим сотрудника: очередь обращений и статистика — как у них.",
    "student": "Режим студента: меню, расписание и обращения — как у них.",
}


async def menu_view(user_id: str) -> str:
    """Какое меню показывать человеку: своё, сотрудника или студента.

    Хранится в settings по ключу на пользователя, поэтому переключатель
    переживает перезапуск и не смешивается у разных людей.
    """
    return await db.get_setting(f"menu_view:{user_id}", "admin")


@callback("view")
async def cb_menu_view(x, arg):
    """Переключатель вида меню: «хочу работать как сотрудник / как студент».

    Без аргумента показывает сам переключатель с пояснением, что даёт каждый
    режим: без пояснения его легко забыть включить и потом удивляться чужому меню.

    Переключать может только сис-админ: обычному сотруднику незачем смотреть
    меню студента, а ещё он успевал забыть, в каком режиме находится, и писал
    студенту вместо коллеги. Без аргумента показываем, чем это заканчивается.
    """
    if not is_super(await admin_of(x)):
        return await api.send(
            x, "🔒 Режим просмотра меняет только сис-админ.\n\n"
               "Вы работаете в кабинете сотрудника: обращения, расписание и статистика.",
            [[btn("↩️ В меню", "home")]])
    view = as_str(arg).strip().lower()
    if view in ("", "\u25a0"):
        current = await menu_view(x)
        rows = [btn(f"{'\N{WHITE HEAVY CHECK MARK} ' if code == current else ''}{MENU_VIEWS[code]}",
                    f"view:{code}") for code in MENU_VIEWS]
        lines = ["🎭 Режим просмотра — бот показывает меню так, как его видит выбранная роль:", ""]
        lines += [f"— {MENU_VIEWS[code]} — {MENU_VIEW_HINT[code]}" for code in MENU_VIEWS]
        return await api.send(x, "\n".join(lines),
                              [rows, [btn("↩ В меню", "home")]])
    if view not in MENU_VIEWS:
        return await show_home(x)
    if view == "admin" and not is_super(await admin_of(x)):
        return await show_home(x)              # не даём сотруднику попасть в системное меню
    await db.set_setting(f"menu_view:{x}", view)
    if view == "admin":
        return await sysadmin_menu(x)
    return await show_home(x)


async def view_switcher(x: str) -> list:
    """Переключатель режима: сис-админ / сотрудник / студент.

    Отдельный ряд с подписью текущего режима, чтобы его нельзя было спутать
    с рабочим меню.
    """
    if not is_super(await admin_of(x)):
        return []                 # у сотрудника переключателя нет вовсе
    current = await menu_view(x)
    # по две кнопки в ряду: три в ряд на телефоне обрезаются («Сис-адм...»)
    switcher = [btn(f"✅ {MENU_VIEWS[code]}" if code == current else MENU_VIEWS[code],
                    f"view:{code}") for code in MENU_VIEWS]
    return [switcher[0:2], switcher[2:3]]


async def super_menu(user_id: str) -> list:
    """Главное меню сис-админа: три блока и «ещё», чтобы не было простыни кнопок."""
    return [
        [btn("🔔 Сегодня", "today")],
        [btn("✍️ Создать", "snew")],
        [btn("📋 Обращения", "staff"), btn("👥 Расписания", "view_schedules")],
        [btn("👥 Пользователи", "people"), btn("👥 Сотрудники", "admins")],
        [btn("🗝 Коды", "codes"), btn("👤 Без прав", "nostaff")],
        [btn("⚙️ Ещё", "more")],
        [btn("↩️ Кабинет сотрудника", "home")],
    ]


@callback("more")
async def cb_more(x, arg):
    """Второй уровень меню: настройки, справочники и диагностика."""
    if not is_super(await admin_of(x)):
        return await show_home(x)
    keyboard = [
        [btn("📊 Статистика", "stats"), btn("📢 Рассылка", "broadcast")],
        [btn("📅 Расписания (PDF)", "schedules")],
        [btn("👥 Группы", "groups"), btn("⚙️ Настройки", "settings")],
        [btn("🧪 Тест и журнал", "diag")],
        # переключатель возвращает два ряда - разворачиваем, а не вкладываем
        *await view_switcher(x),
        [btn("🔐 Панель сис-админа", "sysadm")],
    ]
    await api.send(x, "⚙️ Ещё", [*keyboard, [btn("↩️ В меню", "home")]])


async def show_home(x: str):
    a = await admin_of(x)
    if a:
        view = await menu_view(x)
        if is_super(a):
            # у сис-админа все три режима: смотрит и своим кабинетом, и глазами
            # сотрудника, и глазами студента
            if view == "student":
                return await api.send(
                    x, f"🎓 {MENU_VIEW_HINT['student']}\n"
                       + ("" if await repo.get_user(x)
                          else "Профиля студента у вас нет, поэтому расписание и обращения "
                               "покажутся общими списками.\n"),
                    [*student_menu(), *await view_switcher(x), [btn("↩️ В меню", "home")]])
            if view == "staff":
                return await api.send(
                    x, f"🏫 {MENU_VIEW_HINT['staff']}",
                    [*staff_menu(a), *await view_switcher(x), [btn("↩️ В меню", "home")]])
            return await sysadmin_menu(x)      # режим «сис-админ» - свой кабинет
        # обычный сотрудник: кабинет сотрудника, без системных кнопок и без
        # переключателя режима. Если режим был включён раньше - сбрасываем,
        # иначе человек продолжит видеть чужое меню.
        if view != "staff":
            await db.set_setting(f"menu_view:{x}", "staff")
        return await api.send(x, f"🏫 {MENU_VIEW_HINT['staff']}"
                              + ("" if staff_position_filled(a) else NO_POSITION_LINE),
                              [*staff_menu(a), [btn("↩️ В меню", "home")]])
    if not await repo.is_registered(x):
        return await start(x)
    # приветствие с подстановками имени и группы: тот же механизм, что в
    # шаблонах ответов, но берётся из профиля студента
    return await api.send(x, await welcome_text(x), student_menu())


@state("reg_name")
async def st_reg_name(x, text, p):
    name = _clean_fio(text)
    if not _valid_fio(name):
        return await api.send(x, "Укажите ФИО полностью (минимум фамилия и имя), например: Иванов Иван Иванович.")
    await db.set_state(x, "reg_group", {"name": name})
    return await _ask_group(x, name)


@state("reg_group")
async def st_reg_group(x, text, p):
    """Принимает код группы в любом написании и подсказывает похожие.

    «24-23 (П)», «24-23П», «2423П» и «24 23 п» - это одна группа 24-23П.
    """
    payload = p or {}
    fio = _clean_fio(payload.get("name", ""))
    typed = as_str(text).strip()
    if not typed:
        return await _ask_group(x, fio)
    if not _valid_fio(fio):
        return await start(x)
    if not group_code(typed):
        return await _ask_group(x, fio, typed)

    resolved = await repo.resolve_group(typed)
    if not resolved["found"]:
        # похожие коды = скорее всего опечатка, спрашиваем; иначе группа новая -
        # заводим её, чтобы регистрация не вставала из-за того, что сис-админ
        # ещё не завёл группу в справочнике
        if _looks_like_typo(typed, resolved["suggestions"]):
            return await _ask_group(x, fio, typed, resolved["suggestions"])
        await _register_new_group(x, typed)
    group = resolved["code"]
    if not await _group_allowed(group):
        return await _group_confirmation(x, group, fio)
    # последний шаг - сверить данные: опечатка в ФИО потом ищется по всему боту
    await db.set_state(x, "reg_confirm", {"name": fio, "group": group})
    return await api.send(
        x,
        f"Проверьте данные:\n\n👤 {fio}\n🎓 {group}\n\nВсё верно?",
        [[btn("✅ Всё верно", "regyes")],
         [btn("✏️ ФИО", "regname:"), btn("🔤 Другая", "regpick:")], *BACK],
    )


async def _finish_registration(x: str, fio: str, group: str):
    """Сохраняет студента после подтверждения. Единственное место записи в users."""
    code = group_code(group)
    if code and not await repo.find_group(code):
        await repo.upsert_group(code, title=as_str(group).strip())
    group = code or group
    if not await _group_allowed(group):
        return await _group_confirmation(x, group, fio)
    if not await _consent_ok(x, fio, group):
        return
    await _save_user(x, fio, group)
    await db.clear_state(x)
    return await _registration_saved(x, fio, group)


@state("reg_confirm")
async def st_reg_confirm(x, text, p):
    """Человек всё равно написал текст вместо кнопки - принимаем как ответ."""
    payload = p or {}
    if _valid_fio(_clean_fio(text)):
        return await _finish_registration(x, _clean_fio(text), as_str(payload.get("group", "")))
    return await api.send(x, "Нажмите «✅ Всё верно» или пришлите исправленное ФИО.",
                          [[btn("✅ Всё верно", "regyes")], *BACK])


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


@callback("help")
async def cb_help(x, arg):
    """Справка: что умеет бот. Команды из нижнего меню перечислены тут же."""
    from bot_commands import command_labels

    lines = ["ℹ️ Что я умею:",
             "• 📚 Расписание — неделя, сегодня, ближайшие пары, своя группа.",
             "• 🗂 Обращения — написать обращение, следить за ответом, ответить сотруднику.",
             "• 👤 Профиль — ФИО, группа, свои данные.",
             "• ✉️ Новое обращение — с выбором сотрудника или отдела.",
             "",
             f"Команды: {command_labels()}",
             "",
             "Меню: /cancel — отменить текущее действие, /id — свой MAX ID."]
    await api.send(x, "\n".join(lines), [[btn("🏠 Меню", "home")]])


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
async def _suggested_name(x: str) -> str:
    """ФИО из профиля MAX - чтобы человеку не набирать своё имя вручную.

    Берём подпись профиля, но только если она похожа на ФИО (фамилия и имя),
    иначе предлагать «Студент» бесполезно.
    """
    row = await db.one("SELECT display_name FROM contacts WHERE user_id=?", (x,))
    name = _clean_fio(_row_value(row, "display_name") if row else "")
    return name if _valid_fio(name) else ""


@callback("regname")
async def cb_registration_confirm_name(x, arg):
    """Человек подтвердил ФИО, предложенное из профиля MAX.

    Проверки «зарегистрирован ли» здесь нет и быть не должно: на этом шаге
    человек как раз ещё не зарегистрирован. Защита - само состояние reg_group.
    """
    await db.set_state(x, "reg_group", {"name": _clean_fio(arg)})
    await _ask_group(x, _clean_fio(arg))


@callback("who")
async def cb_who(x, arg):
    kind = as_str(arg)
    if kind == "student":
        suggestion = await _suggested_name(x)
        if suggestion:
            await db.set_state(x, "reg_name", {"suggest": suggestion})
            return await api.send(
                x,
                f"Проверьте ФИО: {suggestion} — так вас видят в боте.\n"
                "Если всё верно, нажмите кнопку. Иначе введите своё написание.",
                # ФИО может быть длиннее кнопки: полностью оно и так в тексте
                [[btn(f"✅ {cut_plain(suggestion, BUTTON_TEXT - 2)}",
                       f"regname:{suggestion}")],
                 [btn("✏️ Введу сам", "regname:")], *BACK],
            )
        await db.set_state(x, "reg_name")
        return await api.send(x, "Укажите ваши ФИО полностью, например: Иванов Иван Иванович.")
    if kind == "staff":
        if await admin_of(x):
            return await show_home(x)
        return await _ask_staff_code(x)
    if kind == "guest":
        return await _guest_home(x)
    return await start(x)


@callback("regpick")
async def cb_registration_pick_group(x, arg):
    """Группа выбрана кнопкой из списка - не нужно набирать вручную."""
    session = await db.get_state(x) or {}
    payload = session.get("payload") or {}
    return await st_reg_group(x, as_str(arg), {"name": payload.get("name", payload.get("suggest", ""))})


@callback("regnew")
async def cb_registration_new_group(x, arg):
    """Студент уверен в коде: заводим группу в справочнике и продолжаем."""
    code = group_code(arg)
    if not valid_group(code):
        return await _ask_group(x, "")
    await repo.upsert_group(code, title=as_str(arg).strip())
    await repo.add_group_aliases(code, [as_str(arg)])
    session = await db.get_state(x) or {}
    name = as_str((session.get("payload") or {}).get("name", ""))
    return await st_reg_group(x, code, {"name": name})


@callback("regyes")
async def cb_registration_confirm(x, arg):
    """Финальное подтверждение: одна опечатка в ФИО потом ищется по всему боту."""
    session = await db.get_state(x) or {}
    payload = session.get("payload") or {}
    return await _finish_registration(x, as_str(payload.get("name", "")),
                                      as_str(payload.get("group", "")))


def _looks_like_typo(typed: str, suggestions) -> bool:
    """Похоже ли, что человек ошибся в букве, а не назвал новую группу."""
    digits = group_digits(typed)
    if not digits:
        return False
    for group in suggestions or []:
        code = group_code(group.get("code", ""))
        if code == group_code(typed) or group_digits(code) == digits:
            return True
    return False


async def _register_new_group(x: str, typed: str) -> None:
    """Новая группа попадает в справочник сразу, сис-админы получают уведомление."""
    code = group_code(typed)
    if not code or not valid_group(code):
        return
    await repo.upsert_group(code, title=as_str(typed).strip())
    await repo.add_group_aliases(code, [as_str(typed)])
    await repo.log_action("bot", "группа добавлена при регистрации", f"{code} (написано: {typed})")
    for admin_id in {str(value) for value in config.SYSADMIN_IDS} | {as_str(row["user_id"]) for row in await repo.all_admins()}:
        await notify(admin_id, f"🆕 Новая группа в справочнике: {code} (написал студент: «{typed}»). "
                              "Проверьте расписание во вкладке «Расписания».")
    log.info("студент %s добавил группу %s (написал «%s»)", x, code, typed)


async def _ask_group(x: str, name: str = "", typed: str = "", suggestions=None) -> None:
    """Просит группу и показывает подходящие варианты вместо отказа.

    Если человек уже что-то написал, но такой группы нет, - предлагаем похожие
    коды: обычно это опечатка в одной букве или забытые скобки.
    """
    if suggestions is None:
        suggestions = await repo.suggest_groups(typed)
    keyboard = [[btn(group["code"], f"regpick:{group['code']}")]
                for group in suggestions[:8] if group.get("active")]
    if typed and not keyboard:
        # Код группы в кнопку не влезает рядом со словом, поэтому коротко, а сам
        # код идёт строкой текстом в заголовке экрана.
        keyboard.append([btn(f"✍️ Создать {cut_plain(group_code(typed), 4)}",
                             f"regnew:{group_code(typed)}")])
    keyboard.append([btn("🔤 Введу код", "regpick:")])
    head = (f"{name}, группа «{typed}» в списке не найдена. Похожее — проверьте и выберите:"
            if typed else
            f"{name}, укажите код группы. Можно выбрать кнопкой или написать: «24-23 (П)» "
            f"и «2423П» - это одно и то же.")
    await api.send(x, head, [*keyboard, *BACK])


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
            [btn("🎓 Я студент", "who:student")],
            [btn("👔 Я сотрудник", "who:staff")],
        ],
    )


async def _ask_staff_code(x: str):
    await db.set_state(x, "staff_code")
    await api.send(
        x,
        f"🗝 Введите код, который выдал сотрудник или сис-админ ({config.STAFF_CODE_ATTEMPTS} попытки в час).",
        [[btn("📥 Подать заявку", "staffreq")], *BACK],
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
    return await use_staff_code(x, text)


async def use_staff_code(x: str, text: str):
    """Принимает код сотрудника. Один вход и для кнопки, и для /join <код>."""
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
            [btn("📚 Учёба", "topic:academic:study")],
            [btn("🗓 Период обучения", "topic:academic:period")],
            [btn("💼 Вакансии", "topic:academic:vacancies"), btn("✍️ Заявка", "new:academic")],
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
            [btn("🎓 Стипендия", "topic:accounting:scholarship"), btn("✍️ Заявка", "new:accounting")],
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
    if not user and await admin_of(x):
        return await api.send(
            x, "👤 Профиля студента у вас нет.\n\nЧтобы посмотреть меню глазами студента, "
               "этого достаточно; а своё расписание по группе добавьте в панели.",
            [[btn("📚 Все расписания", "view_schedules")], [btn("↩️ В меню", "home")]])
    if user:
        full_name = _row_value(user, "full_name")
        group = _row_value(user, "group_code")
        contact = await college.get("телефон_приёмная") or "—"
        await api.send(
            x,
            f"👤 Профиль\nФИО: {full_name}\nГруппа: {group}\n"
            f"Учебная часть: {contact}",
            [[btn("🗂 Всё моё", "myall")],
             [btn("✏️ Изменить ФИО", "pf:name")],
             [btn("✏️ Изменить группу", "pf:group")],
             [btn("🏫 Контакты колледжа", "college")],
             [btn("❓ Частые вопросы", "faq")],
             *BACK],
        )


@callback("college")
async def cb_college(x, arg):
    """Справочник колледжа: адрес, телефоны, кабинеты, часы приёма."""
    await api.send(
        x, await college.text(),
        [[btn("❓ Частые вопросы", "faq")],
         [btn("✍️ В учебную часть", "new:certificates")],
         [btn("🏠 Меню", "home")]],
    )


# ── «всё моё»: сводка по одному нажатию ──────────────────────────────────────
MY_TICKETS_TOTAL = 200     # сколько обращений пересчитываем для счётчиков
MY_TICKETS_PREVIEW = 5     # столько последних показываем кнопками


async def _my_tickets(x: str, limit: int = 0) -> list:
    """Свои обращения из репозитория. getattr-безопасно: метод может отсутствовать."""
    lister = getattr(repo, "recent_student_tickets", None)
    if lister is None:
        return []
    try:
        rows = await (lister(x, limit) if limit else lister(x))
    except TypeError:        # старый вызов без лимита
        rows = await lister(x)
    return list(rows or [])


async def _my_ticket_stats(rows: list) -> dict:
    """Всего обращений, сколько в работе и сколько ждут ответа сотрудника."""
    open_ids = [to_int(_row_value(row, "ticket_id"), -1) for row in rows
                if as_str(_row_value(row, "status")) in OPEN_STATUSES]
    open_ids = [ticket_id for ticket_id in open_ids if ticket_id >= 0]
    waiting = 0
    roles = getattr(repo, "latest_message_roles", None)
    if roles is not None and open_ids:
        # ждёт ответа то обращение, где последнее слово за сотрудником
        latest = await roles(open_ids)
        waiting = sum(1 for ticket_id in open_ids if as_str(latest.get(ticket_id)) == "staff")
    return {"total": len(rows), "open": len(open_ids), "waiting": waiting}


async def _my_subscription_line(x: str, group: str) -> str:
    """Подписка на обновления расписания: getattr-безопасно, как в других местах."""
    checker = getattr(repo, "is_schedule_subscribed", None)
    if not group or checker is None:
        return "подписок нет"
    try:
        subscribed = bool(await checker(x, group))
    except Exception:       # подписки может не быть вовсе - сводка не должна падать
        return "подписок нет"
    return f"подписка на обновления группы {group}" if subscribed else "подписок нет"


def _ticket_label(row, short_status: bool = False) -> str:
    """«№12 · 🆕 Новое» — подпись обращения и в тексте сводки, и на кнопке.

    На кнопке статус короткий («📄 Готово»): «№1234 · 📄 Готово к выдаче» в одну
    строку кнопки не влезает, и MAX обрезал бы его многоточием. Полный статус
    остаётся в тексте сводки.
    """
    status = as_str(_row_value(row, "status"))
    table = STATUS_SHORT if short_status else STATUS
    return f"№{_row_value(row, 'ticket_id')} · {table.get(status, status or 'без статуса')}"


@callback("myall")
async def cb_my_all(x, arg):
    """Сводка «всё моё»: обращения, группа, подписка на расписание и данные."""
    user = await need_student(x)
    if not user:
        return
    full_name = _row_value(user, "full_name")
    group = _group_code(_row_value(user, "group_code"))
    rows = await _my_tickets(x, MY_TICKETS_TOTAL)
    stats = await _my_ticket_stats(rows)
    lines = [
        "🗂 Всё моё",
        "",
        f"🗂 Обращения: всего {stats['total']} · в работе {stats['open']} · "
        f"ждут ответа {stats['waiting']}",
    ]
    if rows:
        lines.append("")
        lines.append("📋 Последние обращения — нажмите, чтобы открыть:")
        lines += [f"  • {_ticket_label(row)}" for row in rows[:MY_TICKETS_PREVIEW]]
    lines += [
        "",
        f"🎓 Моя группа: {group or 'не указана'}",
        f"🔔 Расписание: {await _my_subscription_line(x, group)}",
        "",
        "👤 Мои данные",
        f"ФИО: {full_name}",
        f"ID: {x}",
        f"В боте с: {fmt_time(_row_value(user, 'created_at'), '%d.%m.%Y') or 'неизвестно'}",
    ]
    keyboard = [[btn(_ticket_label(row, short_status=True), f"t:{_row_value(row, 'ticket_id')}")]
                for row in rows[:MY_TICKETS_PREVIEW]]
    keyboard += [
        [btn("↩️ В меню", "home")],
        [btn("🗂 Обращения", "tickets"), btn("📅 Расписание", "sched")],
        [btn("👤 Профиль", "profile")],
    ]
    if len(keyboard) > MAX_ROWS:   # страховка: MAX не принимает больше 30 строк
        keyboard = keyboard[:MAX_ROWS]
    await api.send(x, "\n".join(lines), keyboard)


# ── «ошибка в боте»: короткое описание → журнал и сис-админы ──────────────────
BUGREPORT_LIMIT = 300      # сколько символов описания уходит в журнал


def _who_line(x: str, user) -> str:
    """Подпись человека для журнала: ФИО, группа и ID — как в заявках сотрудников."""
    fio = _clean_fio(_row_value(user, "full_name")) if user else ""
    group = _group_code(_row_value(user, "group_code")) if user else ""
    return f" от {fio or 'без имени'} ({group or 'группа не указана'}, ID {x})"


@callback("bugreport")
async def cb_bugreport(x, arg):
    """Кнопка «Ошибка в боте»: спрашиваем, что не сработало, и пишем сис-админам."""
    await db.set_state(x, "bug_report")
    await api.send(
        x,
        "⚠️ Ошибка в боте\nОпишите одним сообщением, что не сработало: какую кнопку "
        "нажали и что получили вместо ответа. Сис-админ прочитает и исправит.",
        [[btn("❌ Отмена", "home")]],
    )


@state("bug_report")
async def st_bug_report(x, text, p):
    """Сообщение об ошибке: пишем в admin_log и отправляем его всем сис-админам."""
    report = " ".join(as_str(text).split())
    if not report:
        return await api.send(
            x,
            "Опишите ошибку хотя бы парой слов — иначе сис-админ не разберётся.",
            [[btn("❌ Отмена", "home")]],
        )
    report = report[:BUGREPORT_LIMIT]
    details = f"ошибка в боте: {report}{_who_line(x, await repo.get_user(x))}"
    await repo.log_action(x, "ошибка в боте", details)
    for uid in sysadmin_ids():
        await notify(uid, f"⚠️ {details}", [[btn("🗂 Журнал", "diag")]])
    log.info("студент %s сообщил об ошибке: %s", x, short(report, 120))
    await db.clear_state(x)
    await api.send(x, "✅ Спасибо, сообщили. Сис-админ прочитает и исправит.")
    return await show_home(x)


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
    if not await _consent_ok(x, fio, group):
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
    codes = await _schedule_group_codes()
    if not codes:
        # раньше здесь был переход на cb_schedule, а тот - обратно сюда:
        # при пустом справочнике бот уходил в бесконечный цикл
        return await api.send(
            x, "📚 Расписаний пока нет в системе.\n\n"
               "Их завозит сис-админ кнопкой «⬇️ Импорт с сайта» в меню «Расписания».",
            [[btn("↩️ В меню", "home")]])
    raw = str(arg or "")
    page = max(0, to_int(raw.replace("view_schedules:", "", 1)))
    pages = max(1, -(-len(codes) // GROUPS_PAGE))
    page = min(page, pages - 1)
    chunk = codes[page * GROUPS_PAGE:(page + 1) * GROUPS_PAGE]
    keyboard = []
    hint = ""
    for code in chunk:
        label = await _schedule_subscription_label(x, code)
        hint = hint or sub_hint(label)      # смысл кнопки для всех групп один
        keyboard.append([btn(f"📅 {code}", f"sched:{code}"), btn(label, f"schedsub:{code}")])
    nav = []
    if page:
        nav.append(btn("◀️ Назад", f"view_schedules:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Вперёд ▶️", f"view_schedules:{page + 1}"))
    if nav:
        keyboard.append(nav)
    text = "📅 Выберите группу для расписания:"
    if pages > 1:
        text += f"\nГрупп: {len(codes)}, страница {page + 1} из {pages}."
    await api.send(x, f"{text}\n{hint}", [*keyboard, *BACK])


async def _schedule_group_codes() -> list[str]:
    """Все группы, у которых есть расписание: из справочника и из списка ссылок."""
    schedule_lister = getattr(repo, "schedule_groups", None)
    rows = list(await _active_group_rows())
    if schedule_lister is not None:
        try:
            rows += list(await schedule_lister(GROUPS_ALL) or [])
        except TypeError:          # старый вызов без лимита
            rows += list(await schedule_lister() or [])
    codes: list[str] = []
    for row in rows:
        code = _group_from_row(row)
        if code and code not in codes:
            codes.append(code)
    return sorted(codes)


@callback("sched")
async def cb_schedule(x, arg):
    arg = str(arg or "")
    selected = arg[6:] if arg.startswith("sched:") else arg
    user = await repo.get_user(x)
    if not user and not selected:
        if await admin_of(x):
            # сис-админ в режиме студента: показываем список, а не регистрацию
            return await cb_view_schedules(x, "")
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
        note = f"\n(разобрать не удалось: {short(result.reason, 80)})" if result.reason else ""
        return await api.send(
            x, f"📅 Расписание группы {group} — PDF{note}\n{url}\n{sub_hint(label)}",
            [[link_btn("Открыть расписание", url)], tail],
        )

    schedule = result.schedule
    if view == "day":
        today = schedule.day(clock.now().weekday())
        text = tt.format_day(today) if today and not today.is_empty else f"📅 На {WEEKDAYS_FULL[clock.now().weekday()]} пар нет"
    elif view == "next":
        text = tt.format_upcoming(schedule) or "⏰ Ближайших занятий не найдено"
    else:
        text = tt.format_schedule(schedule)
    today_weekday = clock.now().weekday()
    keyboard = [
        [btn("📆 Сегодня", f"schedday:{group}:{today_weekday}"),
         btn("⏰ Ближайшие", f"schednext:{group}")],
        [btn("📚 Вся неделя", f"sched:{group}")],
    ]
    await api.send(x, f"{text}\n{sub_hint(label)}", [*keyboard, tail])


@callback("schedday")
async def cb_schedule_day(x, arg):
    raw = str(arg or "")
    if raw.startswith("schedday:"):
        raw = raw[9:]
    group, _, weekday = raw.partition(":")
    result = await schedules.parse_group(_group_code(group))
    if not result.has_lessons:
        return await send_schedule(x, group)
    day = result.schedule.day(to_int(weekday, clock.now().weekday()))
    text = tt.format_day(day) if day and not day.is_empty else "📅 На этот день пар нет"
    label = await _schedule_subscription_label(x, group)
    return await api.send(x, f"{text}\n{sub_hint(label)}", [
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
