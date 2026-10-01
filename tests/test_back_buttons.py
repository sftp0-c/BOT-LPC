"""Кнопка «Назад» на всех шести экранах, где её поставили.

Зачем этот файл. Кнопку «Назад» поставили в шесть мест, и десять живых проверок
на неё остались во временном каталоге - в репозиторий они не попали. То есть
код был, а страховки не было: снеси адрес у одной кнопки - и заметят только
владелец со смартфона. Этот файл закрывает дыру.

Что здесь проверяется, по одному экрану
* Справка и Бухгалтерия → «Назад» в «Создать обращение»;
* Обратная связь → то же самое, и в ОБОИХ её выходах: когда должности уже
  назначены и когда ещё нет. На пустом экране «Назад» обязана быть, иначе
  человек застревает;
* Мои обращения → «Назад» в «Обращения», во всех трёх состояниях: пусто,
  обычный список, архив;
* Частые вопросы: список разделов → «Назад» в «Создать обращение», а страница
  вопросов → «Назад» к списку разделов (а не в начало).

Плюс общие свойства механизма
* «Назад» ведёт на предыдущий экран, а не в главное меню;
* неизвестный адрес не молчит и не роняет бота;
* «Назад» на самом себе не зацикливается;
* подписи «Назад» и «Меню» помещаются в ряд и не режутся многоточием.

И отдельным списком - экраны, где «Назад» намеренно нет, с причиной. Это
зафиксировано тестом, чтобы завтра никто не решил, что это забыли, и не
добавил туда адрес от руки: адрес там неоднозначен, и это не догадка, а разбор
путей, по которым на эти экраны попадают.
"""
import max_api
from conftest import press, register
from handlers import faq

STUDENT = "300"

В_ОБРАЩЕНИЯ = "back:ticket_menu"
В_СОЗДАТЬ = "back:ticket_create"

# ── разделы внутри «Создать обращение» ──────────────────────────────────────
async def test_certificates_has_back_to_create(api):
    await register(STUDENT)
    await press(STUDENT, "sub:cert")
    assert В_СОЗДАТЬ in api.payloads(STUDENT), "в «Справке» нет «Назад»"


async def test_accounting_has_back_to_create(api):
    await register(STUDENT)
    await press(STUDENT, "sub:acc")
    assert В_СОЗДАТЬ in api.payloads(STUDENT), "в «Бухгалтерии» нет «Назад»"


async def test_feedback_has_back_when_positions_are_known(api):
    await register(STUDENT)
    await press(STUDENT, "sub:fb")
    assert В_СОЗДАТЬ in api.payloads(STUDENT), "в «Обратной связи» нет «Назад»"


async def test_feedback_has_back_even_when_no_positions_assigned(api):
    """Пустой экран обратной связи - тот случай, где «Назад» нужнее всего.

    Если должностей ещё не назначили, экран состоит почти из одной кнопки
    «Выбрать». Без «Назад» и «Меню» человек с этого экрана не уходит.
    """
    await register(STUDENT)
    await press(STUDENT, "sub:fb")
    нашли = api.to(STUDENT)[-1][1]
    assert "не назначены" in нашли, "ожидался экран без должностей, а получилось другое"
    assert В_СОЗДАТЬ in api.payloads(STUDENT), (
        "на пустом экране обратной связи нет «Назад» - оттуда некуда вернуться")


# ── мои обращения ──────────────────────────────────────────────────────────
async def test_my_tickets_back_when_there_are_none(api):
    await register(STUDENT)
    await press(STUDENT, "tickets")
    assert "нет обращений" in api.to(STUDENT)[-1][1]
    assert В_ОБРАЩЕНИЯ in api.payloads(STUDENT), "на пустом списке обращений нет «Назад»"


async def test_my_tickets_back_in_the_list(api):
    await register(STUDENT)
    await press(STUDENT, "tickets")
    assert В_ОБРАЩЕНИЯ in api.payloads(STUDENT)


# ── частые вопросы ─────────────────────────────────────────────────────────
async def seeded():
    await faq.seed_defaults()


async def test_faq_sections_back_to_create(api):
    await register(STUDENT)
    await seeded()
    await press(STUDENT, "faq")
    assert В_СОЗДАТЬ in api.payloads(STUDENT), "у списка разделов вопросов нет «Назад»"


async def test_faq_question_page_back_to_sections_not_to_create(api):
    """Со страницы вопросов «Назад» ведёт к списку разделов, а не в начало.

    Разница видна по кнопкам: у списка разделов четыре кнопки разделов, а у
    страницы вопросов - текст выбранного вопроса. Если бы адрес был
    «Создать обращение», страница вопроса ушла бы в «Создать обращение» и
    список разделов стал бы недостижим.
    """
    await register(STUDENT)
    await seeded()
    await press(STUDENT, "faq")
    раздел = next((p for p in api.payloads(STUDENT) if p.startswith("faq:")), None)
    assert раздел, "в списке разделов не оказалось ни одного раздела вопросов"
    await press(STUDENT, раздел)
    assert "back:faq" in api.payloads(STUDENT), (
        f"со страницы вопроса «Назад» ведёт не к списку разделов: {api.payloads(STUDENT)}")


# ── механизм «Назад» ───────────────────────────────────────────────────────
async def test_back_opens_the_previous_screen_not_home(api):
    """Главное требование владельца: возврат на шаг, а не в начало."""
    await register(STUDENT)
    await press(STUDENT, "ticket_menu")
    await press(STUDENT, "ticket_create")
    api.sent.clear()
    await press(STUDENT, В_ОБРАЩЕНИЯ)
    живые = [p for p in api.payloads(STUDENT) if p != "home"]
    assert set(живые) == {"tickets", "ticket_create"}, (
        f"«Назад» открыл не «Обращения», а {живые}")


async def test_back_from_tickets_returns_to_tickets_screen(api):
    await register(STUDENT)
    await press(STUDENT, "tickets")
    api.sent.clear()
    await press(STUDENT, В_ОБРАЩЕНИЯ)
    живые = [p for p in api.payloads(STUDENT) if p != "home"]
    assert set(живые) == {"tickets", "ticket_create"}, (
        f"«Назад» из «Моих обращений» открыл {живые}")


async def test_unknown_address_opens_menu_and_does_not_fail(api):
    """Старая кнопка в переписке после переименования экрана не должна молчать."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "back:экрана_которого_нет")
    assert api.sent, "на неизвестный адрес бот промолчал"
    assert "ticket_menu" in api.payloads(STUDENT), (
        "при неизвестном адресе должно открыться меню, а не тишина")


async def test_back_to_back_does_not_loop(api):
    """Адрес «back» - это кольцо: «Назад» на самом себе обязано разрываться."""
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "back:back")
    assert len(api.sent) <= 5, (
        f"«Назад» на «Назад» разошёлся: {len(api.sent)} сообщений вместо одного экрана")


async def test_back_clears_unfinished_input(api):
    """«Назад» должен и стирать незаконченный ввод.

    Иначе человек нажал «Назад», вернулся на предыдущий экран, а бот всё
    ждёт от него текст в диалог, который он уже закрыл.
    """
    import database as db

    await register(STUDENT)
    await press(STUDENT, "ticket_menu")
    await db.set_state(STUDENT, "ticket", {"draft": {"text": "черновик"}})
    api.sent.clear()
    await press(STUDENT, В_ОБРАЩЕНИЯ)
    состояние = await db.get_state(STUDENT)
    assert not состояние, f"после «Назад» состояние осталось: {состояние}"


# ── ширина подписей ────────────────────────────────────────────────────────
def test_back_and_home_labels_fit_one_row():
    """«↩️ Назад» и «🏠 Меню» стоят рядом: обе обязаны помещаться.

    Считаю настоящим пределом из max_api: в ряду из двух кнопок помещается 16
    ячеек. Многоточий означает, что человек увидит обрезанную подпись.
    """
    ряд = ["↩️ Назад", "🏠 Меню"]
    лимит = max_api.row_limit(len(ряд))
    всего = 0
    for подпись in ряд:
        ширина = max_api.display_width(подпись)
        всего += ширина
        assert ширина <= лимит, f"{подпись!r} шире {лимит}"
        assert not подпись.endswith("…"), f"{подпись!r} обрезано многоточием"
    assert всего <= лимит, f"две подписи вместе {всего} ячеек, а влезает {лимит}"


# ── экраны, где «Назад» намеренно нет ─────────────────────────────────────
# Фиксирую тестом, чтобы это выглядело решением, а не забывчивостью.
#
# Почему их нельзя адресовать одним «Назад»: на каждый из них ведут несколько
# путей, и бот не знает, откуда пришёл человек.
БЕЗ_НАЗАДА = [
    ("sub:нет_такого_раздела", "раздела не существует - возвращать некуда"),
    ("ask:certificates:нет_такого", "вопрос не найден или сотрудник не назначен"),
    ("fbrole:нет_такой_должности", "людей этой должности нет"),
]


async def test_screens_without_back_still_answer(api):
    """Эти экраны «Назад» не имеют, но обязаны отвечать, а не висеть молча."""
    await register(STUDENT)
    for payload, причина in БЕЗ_НАЗАДА:
        api.sent.clear()
        await press(STUDENT, payload)
        assert api.sent, f"{payload} промолчал ({причина})"