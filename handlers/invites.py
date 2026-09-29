"""Вход сотрудника по ссылке-приглашению: диплинк MAX и подтверждение.

Правила MAX прямо запрещают выдавать права «через диплинк», поэтому по
переходу бот ничего не делает сам: он только показывает человеку его данные
и одну кнопку. Права появляются лишь после нажатия, и ссылка после этого
гаснет (store.access.claim_invite) - по одной ссылке войти можно ровно один
раз.

Ссылка приходит событием bot_started с payload вида inv_<КОД> (см.
updates.start_payload и bot.process). Здесь этот payload разбирается, а
дальше всё решает человек.
"""
from handlers import menus
from handlers.common import admin_of, api, log
from handlers.registry import callback
from max_api import btn
import repository as repo
from store.access import INVITE_PREFIX, claim_invite, get_invite, invite_code, invite_status
from utils import STAFF_CATS, as_str, fmt_time, norm_code, person_label, short_name


WELCOME = "👋 Вас пригласили в бот колледжа"
POSITION_MISSING = "должность не указана"
OFFICE_MISSING = "кабинет не указан"
CATEGORY_ALL = "all"
NAME_MISSING = "ФИО не указано"
MENU_BTN = [[btn("↩️ В меню", "home")]]


def category_label(value) -> str:
    """Раздел обращений по-человечески: «Справки», «Всё».

    Из utils.STAFF_CATS в названии есть эмодзи - на экране подтверждения они
    лишние, там и так четыре строки данных, поэтому знак отбрасывается.
    """
    code = as_str(value).strip() or CATEGORY_ALL
    title = STAFF_CATS.get(code, code)
    return title.split(" ", 1)[-1] if " " in title else title


def invite_deadline(invite) -> str:
    """Срок приглашения словами: «действует до 29.09.2026 18:00» или «бессрочное»."""
    until = as_str(invite["expires_at"]).strip()
    return f"действует до {fmt_time(until)}" if until else "бессрочное"


def invite_screen_text(invite) -> str:
    """Экран подтверждения: все четыре поля, даже если что-то не заполнено.

    Все четыре строки показаны всегда: сотрудник сверяет приглашение именно по
    ним, и пропуск пустого кабинета означал бы, что он не сможет отличить
    «кабинета нет» от «бот потерял данные».
    """
    return "\n".join([
        WELCOME,
        "",
        f"ФИО: {as_str(invite['full_name']).strip() or NAME_MISSING}",
        f"Должность: {as_str(invite['position']).strip() or POSITION_MISSING}",
        f"Кабинет: {as_str(invite['office']).strip() or OFFICE_MISSING}",
        f"Раздел обращений: {category_label(invite['category'])}",
        f"Приглашение {invite_deadline(invite)}",
        "",
        "Согласны с этими данными - нажмите кнопку ниже. Права появятся только "
        "после вашего подтверждения, само по себе переход по ссылке их не даёт.",
    ])


def invite_keyboard(code: str) -> list:
    """Две кнопки в двух рядах: в паре MAX показывает 16 ячеек, а по одной 20.

    Ряд из двух кнопок сэкономил бы место, но подпись обрезалась бы многоточием:
    «✓ Войти как сотрудник» это 22 ячейки даже для одиночной кнопки, поэтому
    в кнопке «✓ Стать сотрудником» (ровно 20), а полная формулировка осталась
    текстом над списком кнопок.
    """
    return [[btn("✓ Стать сотрудником", f"invok:{code}")],
            [btn("✖️ Отмена", f"invno:{code}")]]


async def _refuse(x: str, title: str, reason: str):
    """Вежливый отказ с одним и тем же предложением: попросить новую ссылку."""
    return await api.send(x, f"⛔️ {title}\n\n{reason}", MENU_BTN)


async def on_start(x: str, payload: str):
    """Старт бота: с нашим payload - экран приглашения, без него - обычное меню."""
    code = invite_code(payload)
    if not code and not as_str(payload).strip().startswith(INVITE_PREFIX):
        return await menus.start(x)
    # наш префикс с нечитаемым кодом - это всё равно наша битая ссылка:
    # честнее сказать, что приглашения нет, чем показать обычный вход
    return await show_invite(x, code)


async def show_invite(x: str, code: str):
    """Данные приглашения и кнопка подтверждения - или честный отказ.

    Ничего не создаётся: пока человек не нажал кнопку, приглашение остаётся
    целым, иначе ссылка сгорала бы от одного открытия.
    """
    invite = await get_invite(code)
    status = invite_status(invite)
    if status == "used":
        return await _refuse(
            x, "Эта ссылка уже сработала",
            "Приглашение уже использовали. Попросите сис-админа выдать новую ссылку.")
    if status == "expired":
        return await _refuse(
            x, "Срок приглашения истёк",
            "Приглашение больше не действует. Попросите сис-админа выдать новую ссылку.")
    if status != "active":
        return await _refuse(
            x, "Ссылка не ведёт в колледж",
            "Приглашение не найдено: возможно, в ссылке опечатка или его удалили. "
            "Попросите сис-админа выдать новую ссылку.")
    if await admin_of(x):
        return await _refuse(x, "Вы уже сотрудник", "Эта ссылка вам больше не нужна.")
    return await api.send(x, invite_screen_text(invite), invite_keyboard(norm_code(code)))


@callback("invok")
async def cb_invite_accept(x, arg):
    """«Войти как сотрудник»: гасим ссылку и заводим сотрудника.

    Порядок именно такой - сначала атомарно забираем приглашение, потом создаём
    сотрудника. Иначе два человека с одной ссылкой успевали бы оба нажать
    кнопку и оба стали бы сотрудниками.
    """
    code = norm_code(arg)
    if await admin_of(x):
        return await menus.show_home(x)          # уже сотрудник: приглашение не трогаем
    if not code:
        return await _refuse(x, "Ссылка не ведёт в колледж",
                             "Приглашение не найдено: возможно, в ссылке опечатка. "
                             "Попросите сис-админа выдать новую ссылку.")
    taken, reason = await claim_invite(code, x)
    if not taken:
        return await _refuse(x, "Войти не получилось", reason)
    invite = await get_invite(code)
    name = person_label(invite["full_name"], x)
    position = as_str(invite["position"]).strip()
    office = as_str(invite["office"]).strip()
    category = as_str(invite["category"]).strip() or CATEGORY_ALL
    try:
        await repo.add_staff(x, name, position=position, office=office,
                            ticket_category=category)
    except Exception:  # noqa: BLE001 - ссылка уже сгорела, молчать об этом нельзя
        log.exception("вход по ссылке: карточку сотрудника создать не удалось")
        return await _refuse(
            x, "Карточку создать не удалось",
            "Ссылка сработала, а сотрудника завести не вышло. Сообщите сис-админу - "
            "он заведёт вас вручную, и в следующий раз приглашение будет другим.")
    # код приглашения в журнал не пишем: он виден в /panel/logs открытым текстом
    log.info("вход по ссылке-приглашению: сотрудник %s, приглашение %s", x, invite_deadline(invite))
    await api.send(x, "\n".join([
        f"✅ Вы зарегистрированы как сотрудник: {short_name(name, 26)}",
        f"Должность: {position or POSITION_MISSING}",
        f"Кабинет: {office or OFFICE_MISSING}",
        f"Раздел обращений: {category_label(category)}",
    ]))
    return await menus.show_home(x)


@callback("invno")
async def cb_invite_decline(x, arg):
    """«Отмена»: вежливо отказаться. Ссылка остаётся живой - вдруг человек передумает."""
    return await api.send(
        x, "👌 Хорошо, вход по этой ссылке я не подтверждаю.\n"
           "Если это была ошибка - напишите сис-админу, он выдаст новую ссылку.",
        MENU_BTN)
