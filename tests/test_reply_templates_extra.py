"""Правила типовых шаблонов ответов, которых нет в test_templates_shipped.py.

Дефект, из-за которого написан этот файл, был в типовом шаблоне «Обращение взято
в работу». Он обещал сразу несколько разных вещей и ни одной по делу:
  * повторял номер обращения («Обращение №N принято»), хотя при создании
    обращения бот уже написал студенту «✅ Обращение №N отправлено»;
  * в одной фразе говорил «я отвечаю» и «если что-то срочное, отвечу
    быстрее» - обещание ускориться в том же чате, где отвечает этот же
    сотрудник, читается как нелепость;
  * кроме номера и обещания в письме не было ничего.

Поэтому проверяем не весь текст, а правила, из-за которых письмо ломалось:
номер обращения в типовом письме не повторяется, в шаблоне «в работу» нет слов
«принято» и «быстрее», а ответ обещан в том же чате. Заодно - что шаблоны,
которые просили «больше шаблонов», доезжают до базы, лежат в известных разделах
и не повторяют друг друга названиями.
"""
import pytest

import college
import database as db
import utils
from utils import as_str
from handlers import tickets

from test_templates_shipped import BARE_TICKET, SHIPPED, TITLES, holes

TAKEN = "Обращение взято в работу"

NEW_TITLES = (
    # certificates
    "Справка для практики",
    "Справка из архива",
    # academic
    "Ошибка в расписании",
    "Сессия и пересдача",
    "Освобождение от занятий",
    "Перевод в другую группу",
    # accounting
    "Стипендия: выплата не пришла",
    "Смена банковской карты",
    # feedback
    "Забыли ответить",
    "Вопрос решён",
)
"""Шаблоны, добавленные по просьбе владельца: «больше шаблонов»."""


def shipped_text(title: str) -> str:
    return dict((name, text) for name, _cat, text in SHIPPED)[title]


# ── «Обращение взято в работу» ─────────────────────────────────────────────
def test_taken_into_work_says_it_took_the_request():
    """Главное в письме - сказать, что обращение взято в работу."""
    assert "в работу" in shipped_text(TAKEN)


def test_taken_into_work_has_no_accepted():
    """Слова «принято» в письме нет: приём обращения бот уже подтвердил."""
    assert "принято" not in shipped_text(TAKEN).lower()


def test_taken_into_work_does_not_promise_to_answer_faster():
    """Обещания «отвечу быстрее» больше нет.

    Оно спорило с первой же фразой того же письма («я отвечаю»): сотрудник,
    который и так отвечает в этом чате, не может ответить в нём же быстрее.
    У другого шаблона («Вопрос по бухгалтерии») слово «быстрее» осталось
    законно - там речь о звонке в бухгалтерию, то есть о другом месте.
    """
    assert "быстрее" not in shipped_text(TAKEN).lower()


def test_taken_into_work_promises_the_answer_in_the_same_chat():
    """Ответ обещан там же, где студент писал, - иначе письмо повисает."""
    text = shipped_text(TAKEN)
    assert "здесь же" in text or "в чат" in text


async def test_taken_into_work_renders_as_a_letter():
    """Проверяем не текст шаблона, а письмо, каким его увидит студент."""
    text = await tickets.render_template(shipped_text(TAKEN), BARE_TICKET)
    assert holes(text) == [], text
    assert text.startswith(f"{tickets.TPL_STUDENT}, добрый день! Обращение взято в работу")


async def test_taken_into_work_gives_a_phone_to_call_in_a_hurry():
    """Совет «позвоните» без телефона бесполезен.

    Телефон подставляется из справочника college, а не пишется в шаблон руками:
    устаревший номер в типовом письме разъехался бы первым.
    """
    text = await tickets.render_template(shipped_text(TAKEN), BARE_TICKET)
    assert await college.get("телефон_учебная_часть") in text


def test_no_shipped_template_repeats_the_ticket_number():
    """Ни одно типовое письмо не повторяет номер обращения.

    При создании обращения бот уже пишет студенту «Обращение №N отправлено».
    Второе напоминание о номере незачем, а на обращении без номера оно
    превращалось в «№ не указано».
    """
    for title, _category, text in SHIPPED:
        assert "№" not in text, f"«{title}» повторяет номер обращения"


# ── новые шаблоны ───────────────────────────────────────────────────────────
def test_new_templates_are_shipped():
    """Шаблоны, которых просили «больше шаблонов», не потерялись по дороге."""
    missing = [title for title in NEW_TITLES if title not in TITLES]
    assert missing == [], f"пропали типовые шаблоны: {missing}"


def test_shipped_templates_are_in_known_sections():
    """Раздел взят из CATS: опечатка создала бы раздел, которого у бота нет."""
    unknown = sorted({cat for _title, cat, _text in SHIPPED if cat not in utils.CATS})
    assert unknown == [], f"неизвестные разделы: {unknown}"


@pytest.mark.parametrize("category", sorted(utils.CATS), ids=sorted(utils.CATS))
def test_every_section_has_templates(category):
    """В каждом разделе есть чем ответить: откроет сотрудник - а там пусто."""
    titles = [title for title, cat, _text in SHIPPED if cat == category]
    assert len(titles) >= 2, f"в разделе {category} типовых шаблонов мало: {titles}"


def test_shipped_titles_are_unique():
    """Названия не повторяются.

    seed_reply_templates() добавляет по названию, поэтому второй шаблон с тем же
    названием просто не залился бы, а сотрудник увидел бы два одинаковых пункта.
    """
    assert len(TITLES) == len(set(TITLES))


def test_shipped_titles_are_clean():
    """Название без лишних пробелов: seed сравнивает без них, а сотрудник
    видит в кнопке и в списке именно то, что лежит в базе."""
    for title in TITLES:
        assert title and title == title.strip(), repr(title)


def test_every_shipped_template_is_a_letter():
    """Типовое письмо начинается с обращения по имени - как и обещает
    комментарий над DEFAULT_REPLY_TEMPLATES в database.py."""
    for title, _category, text in SHIPPED:
        assert text.startswith("{ФИО}, добрый день!"), f"«{title}» начат не с приветствия"


async def test_seeded_text_is_exactly_the_shipped_text():
    """В базе шаблон лежит с тем текстом, что в коде.

    seed_reply_templates() дописывает только недостающие названия, поэтому
    свежая база - единственное место, где видно, что залилось именно то, что
    написано. В живой базе старый текст останется, пока её не поправят руками.
    """
    rows = {as_str(row["title"]).strip(): as_str(row["text"])
            for row in await db.many("SELECT title, text FROM reply_templates")}
    for title, _category, text in SHIPPED:
        assert rows.get(title) == text, f"«{title}» в базе отличается от кода"
