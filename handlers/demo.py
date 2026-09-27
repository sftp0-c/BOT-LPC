"""Демо-стенд: режим показа бота, где экраны настоящие, а данные не трогаются.

Зачем: показать бота комиссии, новому сотруднику или коллеге можно без страха.
Любая кнопка нажимается, любой экран открывается, но ни одно обращение, ни один
статус, ни одна рассылка, ни одна выдача прав и ни одно архивирование не
выполняется: бот честно отвечает «в демо-режиме это не выполняется» и базу не
трогает. В журнал действий пишется только строка с пометкой «демо».

Переключатель один на весь бот — settings.demo_mode («1» — включён, «» — выключен),
поэтому его и видно в панели, и он переживает перезапуск. Состояние «demo» в
user_states — отметка «кто сейчас показывает», и проверкой режима оно не служит:
иначе выключенный в панели стенд оставил бы человека в демо навсегда.

Сообщения из демо-режима уходят только вошедшему: рассылок сотрудникам и любых
уведомлений третьим лицам здесь нет (notify/audit не вызываются).

Пределы MAX соблюдаются явно: не больше max_api.MAX_ROWS рядов и не больше
MAX_BUTTONS кнопок в ряду — limit_rows() приводит любую клавиатуру стенда к этим
пределам.
"""
import database as db
import repository as repo
from handlers.common import admin_of, api, is_super, log
from handlers.registry import callback
from max_api import MAX_ROWS, btn
from utils import as_str

SETTING = "demo_mode"        # ключ в settings: «1» — стенд включён
STATE = "demo"               # состояние user_states: кто показывает
MAX_BUTTONS = 7              # предел MAX: не больше 7 кнопок в ряду

# ── тексты демо-режима ─────────────────────────────────────────────────────────
TITLE = "🎬 Демо-стенд бота"
LEAD = "Режим показа: экраны настоящие, а изменения данных отключены."
RULES = ("• Расписание, профиль, списки, очередь и справка открываются как обычно.\n"
         "• Обращения, статусы, ответы, рассылки, архивирование и смена прав — не выполняются.\n"
         "• Регистрация и поиск преподавателя не запускаются — вместо них этот экран.\n"
         "• В журнал действий пишется только отметка «демо».")
SCREEN_HINT = "Нажимайте кнопки ниже и обычное меню: смотреть можно смело, менять — нет."
NO_SCENARIO = "В демо-режиме сценарий не запускается: данные не сохраняются."
REFUSE = "🎬 Демо-режим: {action} не выполняется.\nВ базе ничего не изменилось — это показ."
STATUS_ON = "🎬 Демо-стенд включён: бот показывает экраны, но ничего не меняет."
STATUS_OFF = "🎬 Демо-стенд выключен: бот работает как обычно."
BANNER = "🎬 Демо-режим: просмотр работает, изменения не выполняются."
SWITCH_HINT = ("Стенд нужен, чтобы показать бота, не боясь испортить данные. "
               "Пока он включён, никто не сможет создать обращение, сменить статус, "
               "разослать сообщение сотрудникам или выдать права.")
OFF_DONE = "✅ Демо-режим выключен: бот снова работает как обычно."
ON_DONE = "✅ Демо-режим включён. Данные защищены от изменений — выключите кнопкой ниже."
EXIT_TEXT = "↩️ Выйти из демо"

# ── что в демо-режиме запрещено ───────────────────────────────────────────────
# Кнопки, которые меняют данные или пишут кому-то, кроме вошедшего.
# Ключ — payload без аргумента, значение — человеческое название действия
# (оно попадает в ответ: «создание обращения не выполняется»).
ACTIONS: dict[str, str] = {
    # обращения: создание, ответы, статусы, переписка
    "snew": "создание обращения",
    "new": "создание обращения",
    "ask": "создание обращения",
    "topic": "создание обращения",
    "pick": "создание обращения",
    "ticketsend": "отправка обращения",
    "draftclr": "изменение черновика",
    "tattach": "прикрепление файла к обращению",
    "rp": "ответ на обращение",
    "st": "смена статуса обращения",
    "tdready": "смена статуса обращения",
    "rt": "смена статуса обращения",
    "note": "внутренний комментарий",
    "fwd": "передача обращения другому сотруднику",
    "fwdto": "передача обращения другому сотруднику",
    "fwdok": "передача обращения другому сотруднику",
    "tplsend": "отправка ответа по шаблону",
    "tplu": "отправка ответа по шаблону",
    "tdel": "архивирование обращения",
    "tdely": "архивирование обращения",
    # рассылки: сюда попадает всё, что пишет третьим лицам
    "broadcast": "запуск рассылки",
    "bcaud": "рассылка сотрудникам",
    "bcgo": "рассылка сотрудникам",
    # сотрудники, права, доступ
    "sfadd": "добавление сотрудника",
    "sfbc": "добавление сотрудника",
    "sfc": "изменение карточки сотрудника",
    "sfb": "изменение карточки сотрудника",
    "sfo": "изменение карточки сотрудника",
    "sfr": "изменение должности сотрудника",
    "sfdep": "изменение карточки сотрудника",
    "sfph": "изменение должности сотрудника",
    "sfsa": "смена прав сотрудника",
    "srset": "смена прав сотрудника",
    "sfdel": "удаление сотрудника",
    "sfdy": "удаление сотрудника",
    "make": "заявка на права сотрудника",
    "mkc": "заявка на права сотрудника",
    "reqok": "выдача прав сотруднику",
    "reqno": "отказ в правах",
    "reqnoy": "отказ в правах",
    "sysadd": "выдача прав сис-админа",
    "sysdel": "снятие прав сис-админа",
    "sysdely": "снятие прав сис-админа",
    "codegen": "выпуск кода доступа",
    "codettl": "изменение срока кода",
    "codeinv": "личное приглашение сотрудника",
    "persondel": "удаление пользователя",
    "persondely": "удаление пользователя",
    # справочники, расписания, настройки
    "groupadd": "изменение справочника групп",
    "groupedit": "изменение справочника групп",
    "grouptoggle": "изменение справочника групп",
    "groupdel": "удаление группы",
    "scadd": "изменение расписания",
    "scaddg": "изменение расписания",
    "scedit": "изменение расписания",
    "scdel": "удаление расписания",
    "scimport": "импорт расписания",
    "set": "изменение настроек бота",
    "cleandlg": "очистка диалогов",
}
DANGEROUS = frozenset(ACTIONS)

# Состояния, из которых любое сообщение заканчивается записью в базу.
# Нужны, если защиту ставить в on_message по состоянию, а не сплошной.
WRITE_STATES = frozenset({
    "ticket", "attach_file", "reply", "ticket_note", "forward_comment", "ready_time",
    "bc_group", "bc_text", "bc_confirm", "set_welcome", "bug_report",
    "staff_position", "staff_department", "staff_office",
    "add_staff_id", "add_staff_batch", "add_staff_name", "add_staff_position",
    "add_staff_office", "make_staff_position",
    "code_invite_id", "code_invite_name", "reg_name", "reg_group", "reg_confirm",
    "registration_name", "registration_group", "edit_name", "edit_group",
    "staff_code", "staff_join_name", "staff_join_position", "staff_join_office",
    "req_name", "req_position", "req_office", "req_note", "teacher_search",
})

TRUE_VALUES = ("1", "true", "yes", "on", "да")


# ── флаг режима ───────────────────────────────────────────────────────────────
def is_on(value) -> bool:
    """Значение настройки как «включено»."""
    return as_str(value).strip().lower() in TRUE_VALUES


async def is_demo() -> bool:
    """Включён ли демо-режим для всего бота. Одна настройка, без побочных эффектов."""
    return is_on(await db.get_setting(SETTING))


async def set_demo(value: bool, actor: str = "система") -> bool:
    """Включает или выключает стенд. Возвращает новое состояние.

    Запись в журнал действий — с пометкой «демо»: видно, когда стенд включали
    и что во время него пробовали нажать.
    """
    on = bool(value)
    await db.set_setting(SETTING, "1" if on else "")
    await repo.log_action(
        as_str(actor).strip() or "система",
        "демо-режим: включён" if on else "демо-режим: выключен",
        "демо: изменения данных запрещены" if on else "демо: бот работает как обычно",
    )
    log.info("демо-режим %s (кто: %s)", "включён" if on else "выключен", actor)
    return on


async def demo_status() -> str:
    """Человеческий статус стенда — для панели, экрана сис-админа и /demo."""
    return STATUS_ON if await is_demo() else STATUS_OFF


# ── вход и выход ──────────────────────────────────────────────────────────────
async def enter(x) -> bool:
    """Включить стенд и показать экран демо. Только сис-админ."""
    if not is_super(await admin_of(x)):
        await api.send(x, "🔒 Демо-стенд включает только сис-админ.", exit_rows())
        return False
    await set_demo(True, actor=x)
    await db.set_state(x, STATE, {"by": as_str(x)})      # кто показывает
    await screen(x, reason=ON_DONE)
    return True


async def leave(x, reason: str = "") -> bool:
    """Выйти из демо-режима: гасим стенд и чистим состояние.

    Выход доступен любому, кто в демо: стенд не должен «залипать» и мешать
    живой работе. Ничего не отправляется, если демо и не было включено —
    тогда нажатие «🏠 Меню» остаётся обычным.
    """
    if not await is_demo():
        return False
    await set_demo(False, actor=x)
    await db.clear_state(x)
    text = OFF_DONE + (f"\nПричина: {reason}." if reason else "")
    await api.send(x, text)
    return True


async def off_command(x) -> bool:
    """Команда /demo_off: выключить стенд. True — обработано.

    Вне демо-режима команда не существует: молча уходим в обычное меню,
    как /panel для посторонних.
    """
    if not await is_demo():
        return False
    await leave(x, "команда /demo_off")
    return True


async def command(x, cmd: str) -> bool:
    """Служебные команды стенда: /demo (экран сис-админа) и /demo_off (выход)."""
    cmd = as_str(cmd).split("@")[0].lower()
    if cmd == "/demo_off":
        return await off_command(x)
    if cmd == "/demo":
        if not is_super(await admin_of(x)):
            return False
        await switch(x)
        return True
    return False


# ── экраны и клавиатуры ───────────────────────────────────────────────────────
def limit_rows(rows: list | None) -> list:
    """Клавиатура стенда в пределах MAX: ≤ MAX_ROWS рядов, ≤ MAX_BUTTONS в ряду.

    Широкий ряд режется на части по MAX_BUTTONS, лишние ряды отбрасываются:
    API MAX отвечает errors.maxRows, и событие падает с «Ошибка при обработке».
    """
    out: list = []
    for row in [r for r in (rows or []) if r]:
        for start in range(0, len(row), MAX_BUTTONS):
            out.append(list(row[start:start + MAX_BUTTONS]))
    return out[:MAX_ROWS]


def exit_rows() -> list:
    """Кнопки, которые есть на любом экране демо: выход и «домой»."""
    return [[btn(EXIT_TEXT, "demooff"), btn("🏠 Меню", "home")]]


def demo_kb() -> list:
    """Экран показа: что можно посмотреть, и выход отдельным рядом."""
    return [
        [btn("📅 Расписание", "view_schedules"), btn("👤 Профиль", "profile"),
         btn("📋 Обращения", "tickets")],
        [btn("📊 Очередь", "staff"), btn("❓ Справка о боте", "help"),
         btn("👥 Сотрудники", "admins")],
        [btn("🏠 Меню", "home")],
        [btn(EXIT_TEXT, "demooff")],
    ]


def refuse_kb() -> list:
    """Клавиатура отказа: выход, домой и объяснение режима."""
    return limit_rows([*exit_rows(), [btn("🎬 Что можно в демо", "demoscreen")]])


def banner() -> str:
    """Строка-предупреждение для верхней строки экранов в демо-режиме."""
    return BANNER


async def screen(x, reason: str = "") -> None:
    """Экран демо-режима: что работает, что нет, и кнопка выхода."""
    lines = [TITLE, "", LEAD, "", RULES, ""]
    if reason:
        lines += [reason, ""]
    lines.append(SCREEN_HINT)
    await api.send(x, "\n".join(lines), limit_rows(demo_kb()))


async def switch(x) -> None:
    """Экран стенда для сис-админа: статус и включение/выключение."""
    if not is_super(await admin_of(x)):
        return await api.send(x, "🔒 Демо-стенд доступен только сис-админу.", exit_rows())
    on = await is_demo()
    rows = [[btn("⏹ Выключить демо" if on else "🎬 Включить демо",
                 "demooff" if on else "demoon")],
            [btn("🔄 Обновить", "demo")]]
    if on:
        rows += exit_rows()               # кнопка выхода видна и на этом экране
    rows.append([btn("↩️ В меню", "home")])
    await api.send(x, f"{TITLE}\n{await demo_status()}\n\n{SWITCH_HINT}", limit_rows(rows))


# ── защита: что не выполняется в демо-режиме ──────────────────────────────────
async def deny(x, action: str, extra: str = "") -> bool:
    """Запретить изменение данных. True — действие заблокировано, ответ отправлен.

    Единственная запись в базу здесь — строка журнала с пометкой «демо».
    Ничего не уведомляется: ответ уходит только вошедшему.
    """
    if not await is_demo():
        return False
    await repo.log_action(as_str(x), f"демо: {action}", "демо-режим, действие не выполнено")
    log.info("демо-режим: %s от %s не выполнено", action, x)
    text = REFUSE.format(action=action) + (f"\n{extra}" if extra else "")
    await api.send(x, text, refuse_kb())
    return True


async def guard_callback(x, name: str) -> bool:
    """Проверка кнопки по её имени. True — нажатие заблокировано."""
    name = as_str(name).split(":")[0].strip()
    if name not in DANGEROUS:
        return False
    return await deny(x, ACTIONS.get(name, "это действие"))


async def guard_state(x, state_name: str) -> bool:
    """Проверка состояния сценария. True — состояние не запускается."""
    if as_str(state_name) not in WRITE_STATES:
        return False
    return await deny(x, "ввод данных в сценарии")


async def guard_message(x, text) -> bool:
    """Первая проверка в on_message. True — сообщение обработано демо-режимом.

    В демо не запускаются ни черновик обращения, ни ввод ФИО и группы, ни поиск
    преподавателя: любой текст получает экран демо с кнопкой выхода. Команды
    (/start, /view:student, /today и прочие) продолжают работать — это просмотр,
    а данные меняют кнопки, и они проверяются guard_callback.
    """
    if not await is_demo():
        return False
    text = as_str(text)
    cmd = text.split()[0].lower() if text.startswith("/") else ""
    if cmd == "/demo_off":
        return await off_command(x)
    if cmd:                       # команда — это просмотр, а не ввод данных
        return False
    st = await db.get_state(x)
    if st and st["state"] in WRITE_STATES:
        return await deny(x, f"ввод данных в сценарии «{st['state']}»")
    await repo.log_action(as_str(x), "демо: ввод текста", "демо-режим, сценарий не запущен")
    await screen(x, reason=NO_SCENARIO)
    return True


# ── кнопки стенда ─────────────────────────────────────────────────────────────
@callback("demo")
async def cb_demo(x, arg):
    """Экран стенда из меню сис-админа."""
    await switch(x)


@callback("demoon")
async def cb_demo_on(x, arg):
    if not is_super(await admin_of(x)):
        return await api.send(x, "🔒 Демо-стенд включает только сис-админ.", exit_rows())
    await enter(x)


@callback("demooff")
async def cb_demo_off(x, arg):
    """Кнопка выхода: работает и в стенде сис-админа, и в общем демо-режиме."""
    if not await leave(x, f"кнопка «{EXIT_TEXT}»"):
        await api.send(x, OFF_DONE)


@callback("demoscreen")
async def cb_demo_screen(x, arg):
    """Экран демо для любого: что работает, что нет, и кнопка выхода."""
    await screen(x)
