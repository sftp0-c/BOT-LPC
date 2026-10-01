"""Главный экран студента: четыре кнопки, «Обращения» — первое.

Задача владельца, дословно: на главном экране четыре кнопки; «Обращения»
открывает два дела — «Мои обращения» и «Создать обращение»; внутри «Создать
обращение» живут все разделы (Справка, Бухгалтерия, Вопросы, Обратная связь);
кнопка «Назад» есть везде и возвращает на предыдущий экран, а не только в
главное меню; «Что умеет» стоит четвёртой кнопкой.

Что тут проверяется и почему это не косметика.
1. Структура главного экрана: четыре кнопки, три ряда, «Обращения» первое.
   Порядок не украшение: обращение - главное действие, ради которого человек
   открывает бота, и оно должно быть видно сразу.
2. Обратный путь. Раньше разделы можно было убрать с первого экрана и не
   вернуть - ни один тест бы не покраснел, и так уже случалось, пока
   подменю не существовало. Теперь проверка обязательна.
3. «Назад» ведёт на предыдущий экран, а не в главное меню. Владелец сказал:
   возвращаться в главное меню неудобно, и это не только его мнение.
4. Ширина подписей. Предел зависит от числа кнопок в ряду (измерено: одна -
   20 ячеек, две - 16, три и больше - 9), в ряду из трёх подпись режется
   многоточием почти всегда.
"""
import max_api
from conftest import press, register
from handlers import menus
from handlers.registry import CALLBACKS

STUDENT = "300"

# разделы, которые не должны вернуться на главный экран студента
ВТОРОСТЕПЕННЫЕ = ("sub:cert", "sub:acc", "sub:fb", "faq", "college", "bugreport")

# четыре кнопки главного экрана: обращения впереди, справка бота последней
ГЛАВНЫЙ = ["ticket_menu", "sched", "profile", "help"]

# дела внутри «Обращений»
ДЕЛА = ["tickets", "ticket_create"]

# разделы внутри «Создать обращение»
РАЗДЕЛЫ = ["sub:cert", "sub:acc", "faq", "sub:fb"]


def payloads() -> list:
    return [b["payload"] for row in menus.student_menu() for b in row]


def rows_of(text: str) -> list:
    """Ряды клавиатуры из живого ответа бота на нажатие."""
    return text


# ── главный экран ──────────────────────────────────────────────────────────
def test_first_screen_is_four_buttons_in_three_rows():
    rows = menus.student_menu()
    assert [len(row) for row in rows] == [1, 2, 1], f"получилось {[len(r) for r in rows]}"
    assert payloads() == ГЛАВНЫЙ, payloads()


def test_ticket_button_is_the_first_thing_on_screen():
    """Обращение - главное дело, и оно видно сразу, без лишнего нажатия."""
    first = menus.student_menu()[0]
    assert len(first) == 1, "главная кнопка должна быть одна и самой первой"
    assert first[0]["payload"] == "ticket_menu"
    assert "Обращения" in first[0]["text"]


def test_help_screen_is_the_fourth_button():
    """«Что умеет» владелец поставил четвёртой кнопкой - проверяем, что он не съехал."""
    last = menus.student_menu()[-1]
    assert len(last) == 1
    assert last[0]["payload"] == "help"
    assert "умеет" in last[0]["text"]


def test_secondary_sections_are_not_on_the_first_screen():
    """Разброса на главном экране быть не должно - это и просил владелец."""
    on_screen = payloads()
    for buried in ВТОРОСТЕПЕННЫЕ:
        assert buried not in on_screen, f"{buried} не должен быть на главном экране"


def test_every_menu_payload_is_a_live_callback():
    """Кнопка, которой нет в реестре, в MAX не рисуется вовсе.

    Неизвестный payload бот только пишет в журнал, и человек нажимает в пустоту.
    Поэтому каждый payload из меню обязан быть зарегистрированным обработчиком.
    """
    for p in payloads():
        assert p in CALLBACKS, f"{p} нет обработчика - кнопка мёртвая"


# ── второй уровень: «Обращения» ────────────────────────────────────────────
async def test_tickets_screen_offers_exactly_two_things(api):
    """«Обращения» - это ровно два дела: посмотреть своё и написать новое."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    живые = [p for p in api.payloads(STUDENT) if p not in ("home",)]
    assert set(живые) == set(ДЕЛА), f"внутри «Обращений» получилось {живые}, ждали {ДЕЛА}"


async def test_both_things_inside_tickets_are_reachable(api):
    """Ни одно из двух дел не должно быть мёртвой кнопкой."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    for дело in ДЕЛА:
        имя = дело.split(":", 1)[0]
        assert имя in CALLBACKS, f"у кнопки {дело} нет обработчика {имя}"


# ── третий уровень: «Создать обращение» ────────────────────────────────────
async def test_create_screen_holds_all_four_sections(api):
    """Главное требование: все разделы собраны внутри «Создать обращение»."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    живые = [p for p in api.payloads(STUDENT) if p != "home" and not p.startswith("back:")]
    assert set(живые) == set(РАЗДЕЛЫ), (
        f"внутри «Создать обращение» получилось {живые}, ждали все {РАЗДЕЛЫ}")


async def test_sections_are_opened_from_create_screen_and_not_only_declared(api):
    """Раздел не только назван в кнопке - он должен открываться нажатием.

    Проверка на прошлом опыте: разделы убрали с первого экрана, подменю не
    придумали, и они стали недостижимы - при этом ни один тест не покраснел,
    потому что проверялось только наличие кнопок в списке.
    """
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    for раздел in РАЗДЕЛЫ:
        # бот режет payload по первому двоеточию: «sub:cert» - это обработчик
        # «sub» с аргументом «cert». Проверять надо имя ДО двоеточия, иначе
        # проверка ругается на живую кнопку (так и вышло: «sub:cert» в реестре
        # не лежит, лежит «sub» - и это нормально).
        имя = раздел.split(":", 1)[0]
        assert имя in CALLBACKS, f"у кнопки {раздел} нет обработчика {имя}"
        api.sent.clear()
        await press(STUDENT, раздел)
        assert api.sent, f"нажатие на {раздел} не дало ни одного сообщения"


# ── «Назад» ────────────────────────────────────────────────────────────────
def test_back_callback_is_registered():
    """«Назад» - один общий обработчик, а не копия на каждом экране."""
    assert "back" in CALLBACKS, "обработчик «Назад» не зарегистрирован"


async def test_back_returns_to_the_previous_screen_not_to_home(api):
    """Возврат должен быть на предыдущий экран: это и просил владелец.

    Проверяем не текст, а куда ведёт кнопка: из «Создать обращение» «Назад»
    должен открыть «Обращения», а не главное меню.
    """
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    api.sent.clear()
    await press(STUDENT, "back:ticket_menu")
    найдено = set(api.payloads(STUDENT))
    assert set(ДЕЛА) <= найдено, (
        f"«Назад» из «Создать обращение» открыл не «Обращения», а {найдено}")


async def test_back_from_sections_leads_back_to_create(api):
    """Из раздела «Назад» возвращает в «Создать обращение», а не в начало."""
    await register(STUDENT)
    await press(STUDENT, "home")
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    await press(STUDENT, "sub:cert")
    api.sent.clear()
    await press(STUDENT, "back:ticket_create")
    живые = [p for p in api.payloads(STUDENT) if p != "home" and not p.startswith("back:")]
    assert set(живые) == set(РАЗДЕЛЫ), f"«Назад» из раздела открыл {живые}"


async def test_back_to_unknown_address_does_not_fail_silently(api):
    """Мусор в адресе не должен ронять бота и не должен молчать.

    Если экран переименуют, старая кнопка «Назад» останется в переписке. Она
    обязана открыть главное меню, а не ничего.
    """
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "back:экрана_которого_нет")
    assert api.sent, "на «Назад» с чужим адресом бот промолчал"
    assert "tickets" in api.payloads(STUDENT) or "ticket_menu" in api.payloads(STUDENT), \
        "при неизвестном адресе должно открыться меню"


async def test_back_to_back_does_not_loop(api):
    """«Назад» на самом себе - это кольцо; оно обязано разрываться.

    Адрес «back» означал бы «вернуться туда, откуда пришли», а пришли мы из
    «Назад» же - и бот звал бы себя бесконечно. Проверяем, что этого не будет.
    """
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "back:back")
    assert len(api.sent) < 20, "«Назад» на «Назад» разошёлся, больше 20 сообщений"


# ── вёрстка: подписи помещаются, многоточий нет ────────────────────────────
def test_no_row_is_wider_than_two_buttons():
    """В ряду из трёх и более подпись режется многоточием - это измерено."""
    assert all(len(row) <= 2 for row in menus.student_menu())


def test_every_label_fits_its_row(api=None):
    """Каждая подпись главного экрана помещается в свой ряд.

    Считаем настоящий предел из max_api, а не на глаз: одна кнопка - 20 ячеек,
    две - 16, три и больше - 9.
    """
    for row in menus.student_menu():
        лимит = max_api.row_limit(len(row))
        for button in row:
            ширина = max_api.display_width(button["text"])
            assert ширина <= лимит, (
                f"{button['text']!r} это {ширина} ячеек, а в ряду из {len(row)} "
                f"помещается {лимит} - MAX обрежет многоточием")


def test_no_label_ends_with_ellipsis():
    """Многоточий в подписи - это уже обрезанная кнопка, MAX их не рисует."""
    for row in menus.student_menu():
        for button in row:
            assert not button["text"].endswith("…"), \
                f"{button['text']!r} обрезано многоточием"


def test_keyboard_after_fit_has_no_ellipsis():
    """Проверяем то, что реально уходит в MAX, а не то, что вернул код."""
    for row in max_api.fit_keyboard(menus.student_menu()):
        for button in row:
            assert not button["text"].endswith("…"), \
                f"{button['text']!r} обрезано после fit_keyboard"


def test_callback_payload_names_are_split_on_the_first_colon():
    """Имя обработчика берётся до ПЕРВОГО двоеточия - это правило бота.

    Написано, потому что я сам его нарушил в этом файле: проверял, что
    «sub:cert» лежит в реестре обработчиков, и получал красный тест на живой
    кнопке. В реестре лежит «sub», потому что бот режет payload ровно так.
    Отличается от настоящей поломки (кнопка ведёт в несуществующий обработчик)
    только этим разбором - поэтому правило и закреплено отдельно.
    """
    import inspect
    import bot

    код = inspect.getsource(bot.on_callback)
    assert 'partition(":")' in код, "бот больше не режет payload по первому двоеточию"
    for раздел in РАЗДЕЛЫ:
        имя, _, аргумент = раздел.partition(":")
        assert имя in CALLBACKS, f"обработчик {имя} (кнопка {раздел}) отсутствует"
        if аргумент:
            # аргумент не должен попадать в имя: иначе «sub» и «faq» смешаются
            assert ":" not in имя
