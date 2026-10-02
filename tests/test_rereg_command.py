"""Повторная регистрация сис-админа: скрытая команда /rereg.

Задача владельца: «в боте должна быть возможность, чтобы я мог проходить
повторную регистрацию. Я это владелец, мой айди у тебя есть».

Два решения владельца, которые здесь проверяются
------------------------------------------------
1. Данные перезаписываются только после подтверждения. До нажатия «Всё верно» в
   базе остаются прежние ФИО и группа; отмена на любом шаге ничего не портит.
   Это главное свойство, поэтому проверяется на каждом шаге отдельно - иначе
   «перезаписать по подтверждению» незаметно превратится в «перезаписать сразу».
2. Команда скрытая: в нижнем меню MAX её нет, она набирается руками, и право
   проверяется в обработчике, а не списком команд. MAX показывает меню всем, так
   что список команд защитой не считается.

Почему сис-админу вообще нужна отдельная команда
-----------------------------------------------
start() видит, что человек админ, и отправляет его в меню:

    if await admin_of(x) or await repo.is_registered(x):
        return await show_home(x)

Поэтому путь студента с первого шага своему /start пройти нельзя. Команда идёт
мимо start(). Отдельная проверка ниже держит именно эту причину, чтобы команда
не выглядела лишней, если start() когда-нибудь перепишут.
"""
import bot_commands as bc
import database as db
import repository as repo
from conftest import add_staff, press, register, say
from handlers.registry import CALLBACKS

OWNER = "1"        # сис-админ из фикстуры env
STAFF = "200"
STUDENT = "100"

СТАРОЕ_ИМЯ = "Мусаелян Артём Анатольевич"
СТАРАЯ_ГРУППА = "25-28"
НОВОЕ_ИМЯ = "Проверочный Пользователь"
НОВАЯ_ГРУППА = "25-27"


async def владелец_и_студент(api):
    """Сис-админ, у которого есть и права, и профиль студента."""
    await repo.upsert_group(СТАРАЯ_ГРУППА, title="Старая группа")
    await repo.upsert_group(НОВАЯ_ГРУППА, title="Новая группа")
    await register(OWNER, name=СТАРОЕ_ИМЯ, group=СТАРАЯ_ГРУППА)
    api.sent.clear()
    return await db.one("SELECT * FROM users WHERE user_id=?", (OWNER,))


def весь_текст(api, uid):
    return "\n".join(text for _to, text, _kb in api.to(uid))


async def данные_владельца():
    row = await db.one("SELECT * FROM users WHERE user_id=?", (OWNER,))
    return (row["full_name"], row["group_code"]) if row else (None, None)


# ── команда скрытая ─────────────────────────────────────────────────────
def test_rereg_is_not_in_the_bottom_menu():
    """В нижнем меню MAX команда не показывается - только в списке скрытых."""
    имена_меню = [c["name"] for c in bc.bot_command_list()]
    assert "rereg" not in имена_меню, f"rereg попал в нижнее меню: {имена_меню}"

    скрытые = [bc.command_name(name) for name, _d, _p in bc.ADMIN_COMMANDS]
    assert "rereg" in скрытые, "команда должна быть в списке скрытых"


def test_rereg_is_not_advertised_in_help():
    """В подсказке команд для /help её тоже нет - скрытая значит скрытая."""
    assert "rereg" not in bc.command_labels()


def test_rereg_is_recognised_however_it_is_typed():
    """MAX присылает команду по-разному, и все формы должны вести в обработчик."""
    for текст in ("/rereg", "rereg", "/rereg@bot", "/REREG", "  /rereg  "):
        assert bc.command_payload(текст) == "rereg", f"{текст!r} не распознаётся"


def test_rereg_has_a_handler():
    """Команда без обработчика - это кнопка в пустоту."""
    assert "rereg" in CALLBACKS
    assert "rereg" not in bc.unknown_payloads()


# ── кому нельзя ─────────────────────────────────────────────────────────
async def test_student_cannot_start_reregistration(api):
    """Студент команду не запускает: это право сис-админа."""
    await register(STUDENT, name="Иванов Иван Иванович", group=СТАРАЯ_ГРУППА)
    api.sent.clear()

    await press(STUDENT, "rereg")

    assert "только сис-админу" in весь_текст(api, STUDENT)
    адреса = api.payloads(STUDENT)
    assert not any(p.startswith("regname:") or p == "regpick:" for p in адреса), \
        f"студенту предложили регистрацию: {адреса}"
    # и в базе ничего не поменялось
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["full_name"] == "Иванов Иван Иванович"


async def test_plain_staff_cannot_start_reregistration(api):
    """Сотрудник без прав сис-админа - тоже нет.

    Проверка отдельная от студента: сотрудник есть в admins, и ошибка «достаточно
    быть администратором» здесь была бы самой естественной.
    """
    await add_staff(STAFF, "Петров Пётр")
    await api.send(STAFF, "")
    api.sent.clear()

    await press(STAFF, "rereg")

    assert "только сис-админу" in весь_текст(api, STAFF)


# ── сис-админу можно ────────────────────────────────────────────────────
async def test_sysadmin_starts_registration_again(api):
    """Путь студента начинается с первого шага, хотя он и админ."""
    await владелец_и_студент(api)

    await press(OWNER, "rereg")

    текст = весь_текст(api, OWNER)
    assert "Повторная регистрация" in текст
    # спросили ФИО - значит пошли по-настоящему, а не показали меню
    assert "Укажите ваши ФИО" in текст or "Проверьте ФИО" in текст


async def test_sysadmin_sees_what_will_be_replaced(api):
    """Перед началом видно, что сейчас в базе: человек должен знать, что заменяет."""
    await владелец_и_студент(api)

    await press(OWNER, "rereg")

    текст = весь_текст(api, OWNER)
    assert СТАРОЕ_ИМЯ in текст, "не показано текущее ФИО"
    assert СТАРАЯ_ГРУППА in текст, "не показана текущая группа"
    assert "не изменится" in текст, "не сказано, что данные пока целы"


async def test_sysadmin_without_student_row_is_told_so(api):
    """Если профиля студента нет, это говорится прямо - регистрация его создаст."""
    await press(OWNER, "rereg")

    текст = весь_текст(api, OWNER)
    assert "Профиля студента у вас нет" in текст
    assert "Укажите ваши ФИО" in текст or "Проверьте ФИО" in текст


# ── главное свойство: перезапись только после подтверждения ─────────────
async def test_rereg_does_not_touch_data_until_confirmation(api):
    """Ни на одном шаге до «Всё верно» база не меняется.

    Проверяется по шагам, а не один раз в конце: если перезапись съехала выше по
    потоку, одна итоговая проверка этого не покажет - всё будет зелёное, а данные
    испорчены при отмене.
    """
    await владелец_и_студент(api)

    await press(OWNER, "rereg")
    assert await данные_владельца() == (СТАРОЕ_ИМЯ, СТАРАЯ_ГРУППА), "имя испорчено на старте"

    await say(OWNER, НОВОЕ_ИМЯ)
    assert await данные_владельца() == (СТАРОЕ_ИМЯ, СТАРАЯ_ГРУППА), "имя испорчено после ФИО"

    await say(OWNER, НОВАЯ_ГРУППА)
    assert await данные_владельца() == (СТАРОЕ_ИМЯ, СТАРАЯ_ГРУППА), \
        "имя или группа испорчены до подтверждения"

    assert "Проверьте данные" in весь_текст(api, OWNER), "шаг подтверждения не показан"
    assert "regyes" in api.payloads(OWNER), "нет кнопки подтверждения"

    # и только теперь - смена
    await press(OWNER, "regyes")
    assert await данные_владельца() == (НОВОЕ_ИМЯ, НОВАЯ_ГРУППА), "подтверждение не сменило данные"


async def test_cancelling_rereg_keeps_old_data(api):
    """Отмена на любом шаге оставляет прежние данные на месте."""
    await владелец_и_студент(api)

    await press(OWNER, "rereg")
    await say(OWNER, НОВОЕ_ИМЯ)
    await say(OWNER, НОВАЯ_ГРУППА)
    await press(OWNER, "home")          # свернулись до подтверждения

    assert await данные_владельца() == (СТАРОЕ_ИМЯ, СТАРАЯ_ГРУППА), \
        "отмена всё-таки испортила данные"


async def test_rereg_does_not_ask_consent_again(api):
    """Согласие повторно не спрашивается: человек его уже давал.

    У сис-админа, который ещё и студент, строка в users есть, поэтому вопроса
    согласия не должно быть. Если он появится - человек решит, что его заставляют
    соглашаться заново.
    """
    await владелец_и_студент(api)

    await press(OWNER, "rereg")
    await say(OWNER, НОВОЕ_ИМЯ)
    await say(OWNER, НОВАЯ_ГРУППА)
    await press(OWNER, "regyes")

    assert "Согласие на обработку данных" not in весь_текст(api, OWNER), \
        "согласие спросили повторно"


async def test_rereg_ends_with_registration_saved(api):
    """После подтверждения приходит обычное «регистрация завершена»."""
    await владелец_и_студент(api)

    await press(OWNER, "rereg")
    await say(OWNER, НОВОЕ_ИМЯ)
    await say(OWNER, НОВАЯ_ГРУППА)
    await press(OWNER, "regyes")

    assert "Регистрация завершена" in весь_текст(api, OWNER)


async def test_start_alone_still_cannot_do_it(api):
    """Причина, по которой нужна команда, остаётся на месте.

    Своим /start сис-админ в регистрацию не попадает - иначе команда была бы
    лишней. Проверка специально на будущее: если start() перепишут так, что
    админ сможет пройти регистрацию сам, то эту проверку надо будет убрать
    вместе с командой, и думать об этом лучше явно.
    """
    await владелец_и_студент(api)

    await say(OWNER, "/start")

    текст = весь_текст(api, OWNER)
    assert "Кто вы?" not in текст, "start() больше не отправляет админа в меню - команда лишняя"
    assert "Укажите ваши ФИО" not in текст