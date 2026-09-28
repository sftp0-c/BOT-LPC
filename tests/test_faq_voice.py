"""FAQ: голосовой ввод и кнопка «Это не помогло».

Проверяем три вещи, которые человек замечает сразу:
  * голос приходит тем же текстом, поэтому вопрос нормализуется (пунктуация,
    «ё», повторы слов), а обрывок из пары слов не превращается в обращение;
  * «Это не помогло» благодарит и тут же заводит ровно одно обращение -
    сколько бы раз кнопку ни нажали;
  * в списке вопросов номер в кнопке совпадает с номером в тексте, подпись не
    обрезается многоточием, а рядов меньше, чем предел MAX.
"""
import re

import pytest

import database as db
import handlers.faq as faq
import max_api
from conftest import add_staff, press, register, say

STUDENT, STAFF, SECRETARY = "100", "200", "201"
QUESTION = "Есть ли у вас общежитие?"
ANSWER = "Общежития у колледжа нет."
STUDY_QUESTION = "Кто отвечает на вопросы по учёбе?"


@pytest.fixture(autouse=True)
async def with_sections(env):
    """Колонку разделов заводит наполнение FAQ - без неё вопрос не создать."""
    await faq.ensure_category_column()
    return env


@pytest.fixture
async def one_question():
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 (QUESTION, ANSWER, "общежитие, проживание, поселить", "live"))
    return await db.one("SELECT * FROM faq ORDER BY id")


@pytest.fixture
async def staff():
    """Сотрудник обратной связи: без него жалобу принять некому."""
    await add_staff(STAFF, "Секретарь учебной части", category="feedback", office="115")
    return STAFF


# ── нормализация голоса ───────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, clean", [
    ("какие документы?", "Какие документы"),
    ("  Где   принимают  документы??  ", "Где принимают документы"),
    ("какие документы, для поступления?", "Какие документы, для поступления"),
    ("приёмная комиссия", "Приемная комиссия"),
    ("ПРИЁМНАЯ Комиссия!", "ПРИЕМНАЯ Комиссия"),
    ("какие какие документы документы", "Какие документы"),
    ("общежитие общежитие", "Общежитие"),
    ("какие  документы,  ", "Какие документы"),
    ("", ""),
    ("?! . ,", ""),
])
def test_normalize_question_cleans_voice(raw, clean):
    assert faq.normalize_question(raw) == clean


def test_normalize_question_keeps_inner_punctuation_and_sense():
    """Внутри вопроса знаки остаются: «часы приёма 8:00 - 17:00»."""
    assert faq.normalize_question("часы приёма 8:00 - 17:00") == "Часы приема 8:00 - 17:00"
    assert faq.normalize_question("второе высшее, если есть") == "Второе высшее, если есть"


async def test_voice_hint_is_in_the_state(api, one_question):
    """Человек должен знать, что можно не печатать, а сказать."""
    await register(STUDENT)
    await press(STUDENT, "faqask")
    assert "Можно голосом" in api.last(STUDENT)[1]


async def test_voice_question_finds_same_answer_as_typed(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "обЩеЖИТИЕ?? общежитие")
    assert ANSWER in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_voice_question_reaches_staff_normalized(api, one_question, staff):
    """В обращении вопрос выглядит как вопрос, а не как «ааааааААА ?!»."""
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "  а где  сдают  экзамены  и кто  принимает??  ")
    ticket = await db.one("SELECT * FROM tickets")
    assert ticket and ticket["text_content"] == "А где сдают экзамены и кто принимает"
    assert "?" not in ticket["topic"]


# ── слишком короткий вопрос ───────────────────────────────────────────────────
@pytest.mark.parametrize("garbage", ["ааааа", "ну", "какие", "эээ", "да"])
async def test_short_garbage_is_not_sent_to_staff(api, one_question, staff, garbage):
    """Голос иногда приносит обрывок: лучше попросить сформулировать, чем слать мусор."""
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, garbage)
    assert faq.TOO_SHORT in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0
    # и предложение продолжить в том же разделе
    assert "faqask" in api.payloads(STUDENT)


async def test_short_garbage_asks_to_rephrase_not_to_staff(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "эээ")
    assert "сформулируйте" in api.last(STUDENT)[1].lower()
    assert "new:feedback" not in api.payloads(STUDENT)


async def test_normal_question_still_goes_to_staff(api, one_question, staff):
    """Защита от обрывка не должна съедать нормальный вопрос."""
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "а где сдают экзамены и кто принимает")
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 1


# ── «Это не помогло» ──────────────────────────────────────────────────────────
async def test_no_help_button_is_under_every_answer(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faqq:1")
    assert "faqno:1" in api.payloads(STUDENT)
    assert faq.NO_HELP_BUTTON in [b["text"] for row in api.last(STUDENT)[2] for b in row]


async def test_no_help_button_is_under_auto_answer(api, one_question):
    await register(STUDENT)
    await say(STUDENT, "а общежитие у вас есть")
    assert "faqno:1" in api.payloads(STUDENT)


async def test_no_help_creates_ticket_and_notifies(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqq:1")
    await press(STUDENT, "faqno:1")
    assert "Спасибо" in api.last(STUDENT)[1]
    ticket = await db.one("SELECT * FROM tickets")
    assert ticket and ticket["target_admin_id"] == STAFF
    assert ticket["text_content"] == faq.NO_HELP_TEXT
    assert ticket["topic"] == f"{faq.NO_HELP_TOPIC}{QUESTION}"
    # сотрудник узнаёт, на какой вопрос пожаловались
    notice = api.to(STAFF)[-1]
    assert QUESTION in notice[1]
    assert f"rp:{ticket['ticket_id']}" in [b["payload"] for row in notice[2] for b in row]


async def test_no_help_twice_creates_one_ticket(api, one_question, staff):
    """Кнопку жмут подряд и случайно: второе обращение сотруднику не нужно."""
    await register(STUDENT)
    await press(STUDENT, "faqq:1")
    await press(STUDENT, "faqno:1")
    await press(STUDENT, "faqno:1")
    await press(STUDENT, "faqno:1")
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 1
    assert "Уже отправлено" in api.last(STUDENT)[1]
    # экран с ответом на месте, а вопрос по-прежнему читается
    assert QUESTION in api.last(STUDENT)[1] and ANSWER in api.last(STUDENT)[1]
    # сотрудника позвали ровно один раз
    notices = [message for message in api.to(STAFF) if "Ответ на вопрос не подошёл" in message[1]]
    assert len(notices) == 1


async def test_no_help_goes_to_staff_of_the_question_section(api, staff):
    """Жалобу получает сотрудник своего раздела, а не первый подряд."""
    await add_staff(SECRETARY, "Учебный секретарь", category="academic", office="224")
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 (STUDY_QUESTION, "Учебная часть.", "учёба, оценки, куратор", "study"))
    await register(STUDENT)
    await press(STUDENT, "faqno:1")
    ticket = await db.one("SELECT * FROM tickets")
    assert ticket["target_admin_id"] == SECRETARY
    assert ticket["category"] == "academic"


async def test_no_help_falls_back_to_feedback_staff(api, one_question):
    """Сотрудника раздела нет - берём того, кто принимает обращения."""
    await add_staff(STAFF, "Секретарь приёмной", category="feedback")
    await register(STUDENT)
    await press(STUDENT, "faqno:1")
    ticket = await db.one("SELECT * FROM tickets")
    assert ticket["target_admin_id"] == STAFF
    assert ticket["category"] == "feedback"


async def test_no_help_without_staff_says_so(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faqno:1")
    assert "не назначен" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_no_help_respects_tickets_switch(api, one_question, staff):
    await db.set_setting("tickets_enabled", "0")
    await register(STUDENT)
    await press(STUDENT, "faqno:1")
    assert "временно отключён" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_no_help_on_missing_question(api, staff):
    await register(STUDENT)
    await press(STUDENT, "faqno:999")
    assert "не найден" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


def test_section_of_guesses_the_ticket_section():
    """Раздел жалобы угадывается по теме, когда у вопроса раздела нет."""
    assert faq.section_of({"question": STUDY_QUESTION, "keywords": "учёба, оценки",
                           "category": "study"}) == "academic"
    assert faq.section_of({"question": "Где справка?",
                           "keywords": "справка, дубликат"}) == "certificates"
    assert faq.section_of({"question": "Как платят стипендию?",
                           "keywords": "стипендия"}) == "accounting"
    assert faq.section_of({"question": "Есть ли филиал?",
                           "keywords": "филиал"}) == "feedback"
    # раздел FAQ важнее подбора по слову: деньги - к бухгалтерии
    assert faq.section_of({"question": "Льготы?", "keywords": "стипендия",
                           "category": "money"}) == "accounting"


# ── список вопросов: номер в кнопке и в тексте ─────────────────────────────────
async def questions(count: int) -> None:
    for index in range(count):
        await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                     (f"Вопрос номер {index} и его длинный текст без обрезки",
                      "Ответ.", f"вопрос номер {index}", "study"))


def number_buttons(keyboard) -> list:
    return [button for row in keyboard for button in row
            if button["payload"].startswith("faqq:")]


def numbers_in_text(text) -> list:
    return [int(match.group(1)) for match in re.finditer(r"^(\d+)\. ", text, re.M)]


def question_of(text, number: int) -> str:
    return next(line for line in text.splitlines()
                if line.startswith(f"{number}. ")).split(". ", 1)[1]


async def test_question_buttons_fit_the_row_and_have_no_ellipsis(api):
    await questions(25)
    await register(STUDENT)
    await press(STUDENT, "faqall:0")
    keyboard = api.last(STUDENT)[2]
    for row in keyboard:
        assert len(row) <= 7, row
        limit = max_api.row_limit(len(row))
        for button in row:
            assert len(button["text"]) <= limit, button["text"]
            assert "…" not in button["text"], button["text"]
    # подпись кнопки - только номер: длинный вопрос в неё не влезает
    labels = {button["text"] for button in number_buttons(keyboard)}
    assert labels == {str(number) for number in range(1, faq.FAQ_PAGE + 1)}


async def test_keyboard_rows_stay_below_max_rows(api):
    await questions(25)
    await register(STUDENT)
    for payload in ("faqall:0", "faqall:1", "faqall:2", "faq:study", "faq"):
        await press(STUDENT, payload)
        assert len(api.last(STUDENT)[2]) <= max_api.MAX_ROWS, payload


async def test_numbers_in_buttons_match_numbers_in_text(api):
    await questions(25)
    await register(STUDENT)
    for page, first in ((0, 1), (1, 13), (2, 25)):
        await press(STUDENT, f"faqall:{page}")
        text, keyboard = api.last(STUDENT)[1], api.last(STUDENT)[2]
        listed = numbers_in_text(text)
        buttons = [int(button["text"]) for button in number_buttons(keyboard)]
        assert listed == buttons == list(range(first, first + len(listed)))
        # под каждым номером - свой вопрос: строка списка и payload кнопки совпали
        for index, button in enumerate(number_buttons(keyboard)):
            await press(STUDENT, button["payload"])
            assert question_of(text, buttons[index]) in api.last(STUDENT)[1]


async def test_question_text_is_not_cut_in_the_message(api):
    """Вопрос читается в сообщении целиком - в кнопке он обрезался бы всегда."""
    long_question = "Какие документы и в какие сроки их нужно подать в приёмную комиссию?"
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 (long_question, "Ответ.", "документы, сроки", "admission"))
    await register(STUDENT)
    await press(STUDENT, "faq:admission")
    assert f"1. {long_question}" in api.last(STUDENT)[1]


# ── подсказка «ответа нет» ────────────────────────────────────────────────────
async def test_no_answer_hint_offers_to_ask_staff(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "а где сдают экзамены и кто принимает")
    assert {"new:feedback", "faqask"} <= set(api.payloads(STUDENT))
    assert faq.STAFF_ASK_BUTTON in [b["text"] for row in api.last(STUDENT)[2] for b in row]
