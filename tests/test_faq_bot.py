"""FAQ-бот: поиск ответа, меню вопросов и заведение обращения.

Проверяем главное - бот не выдумывает: на не related вопросе он не отвечает,
а заводит обращение. Сеть и API не используются: конftest подменяет клиент MAX.
"""
import pytest

import college
import database as db
import handlers.faq as faq
import max_api
from conftest import add_staff, press, register, say
from handlers.registry import CALLBACKS, STATES

STUDENT, STAFF = "100", "200"
QUESTION = "Есть ли у вас общежитие?"
ANSWER = "Общежития у колледжа нет."


@pytest.fixture
async def staff():
    """Один сотрудник, принимающий обращения: без него FAQ некуда передавать вопрос."""
    await add_staff(STAFF, "Секретарь учебной части", category="feedback", office="115")
    return STAFF


@pytest.fixture(autouse=True)
def faq_api(env, monkeypatch):
    """Подмена клиента MAX для faq: conftest знает про основные модули, но не про этот."""
    monkeypatch.setattr(faq, "api", env)


@pytest.fixture(autouse=True)
async def faq_sections(env):
    """Колонку разделов заводит handlers.faq.ensure_category_column, а не
    database.SCHEMA, поэтому тестам, которые пишут в faq.category, нужна она сама."""
    await faq.ensure_category_column()


@pytest.fixture
async def one_question():
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 (QUESTION, ANSWER, "общежитие, проживание, поселить", "live"))
    return await db.one("SELECT * FROM faq ORDER BY id")


# ── регистрация обработчиков ──────────────────────────────────────────────────
def test_handlers_are_registered():
    assert CALLBACKS["faq"] is faq.cb_faq
    assert CALLBACKS["faqq"] is faq.cb_faq_item
    assert CALLBACKS["faqask"] is faq.cb_faq_ask
    assert STATES["faq_ask"] is faq.st_faq_ask


# ── нормализация ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, clean", [
    ("  ПРИЁМНАЯ   КОМИССИЯ ", "приемная комиссия"),
    ("Когда  же, наконец?!", "когда же наконец"),
    ("абитуриент-2026", "абитуриент 2026"),
])
def test_norm_cleans_text(raw, clean):
    assert faq.norm(raw) == clean


def test_keywords_split_by_comma_and_semicolon():
    assert faq.keywords_of("общежитие, заселение; поселить ") == ["общежитие", "заселение", "поселить"]


def test_stem_matches_inflections():
    assert faq.stem("стипендию") == faq.stem("стипендия")
    assert faq.stem("общежитие") == faq.stem("общежитии")


# ── поиск ответа ──────────────────────────────────────────────────────────────
async def test_find_answer_by_keyword(api, one_question):
    answer, title = await faq.find_answer(STUDENT, "а общежитие есть?")
    assert answer == ANSWER and title == QUESTION


async def test_find_answer_by_fragment_of_question(api, one_question):
    answer, title = await faq.find_answer(STUDENT, "вы общежитие предоставляете")
    assert answer == ANSWER and title == QUESTION


async def test_find_answer_ignores_case_and_yo(api, one_question):
    assert (await faq.find_answer(STUDENT, "  ОБЩЕЖИТИЕ  "))[0] == ANSWER


async def test_find_answer_empty_for_unrelated(api, one_question):
    """Нет ответа - есть ответа быть не должно: бот не имеет права выдумывать."""
    assert await faq.find_answer(STUDENT, "а когда каникулы") == ("", "")
    assert await faq.find_answer(STUDENT, "привет") == ("", "")


async def test_find_answer_empty_when_disabled(api, one_question):
    await faq.set_ask_enabled(False)
    assert not await faq.ask_enabled()
    assert await faq.find_answer(STUDENT, "общежитие") == ("", "")
    await faq.set_ask_enabled(True)
    assert await faq.find_answer(STUDENT, "общежитие") == (ANSWER, QUESTION)


async def test_inactive_question_is_not_used(api, one_question):
    await db.run("UPDATE faq SET active=0")
    assert await faq.find_answer(STUDENT, "общежитие") == ("", "")


async def test_equally_good_questions_are_not_answered(api, one_question):
    """Два разных вопроса подошли одинаково - лучше не ответить, чем ответить не на тот.

    Ключевые слова и текст вопроса у записей одинаковые, отличается только
    формулировка: выбрать одну из них - значит ответить наугад.
    """
    await db.run("UPDATE faq SET keywords='общежитие'")
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 ("Общежитие есть?", "Другой ответ.", "общежитие", "live"))
    assert faq.score({"question": QUESTION, "keywords": "общежитие"}, "общежитие") == \
           faq.score({"question": "Общежитие есть?", "keywords": "общежитие"}, "общежитие")
    assert await faq.find_answer(STUDENT, "общежитие") == ("", "")


async def test_clearer_match_wins_over_unrelated_one(api, one_question):
    """Если подошёл только один вопрос - отвечаем на него, второй не мешает."""
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 ("Где сдать справку?", "Другой ответ.", "справка, документы", "study"))
    answer, title = await faq.find_answer(STUDENT, "общежитие")
    assert answer == ANSWER and title == QUESTION


async def test_seed_defaults_is_idempotent(api):
    import college

    total = len(college.DEFAULT_FAQ)
    assert await faq.seed_defaults() == total
    assert (await db.one("SELECT COUNT(*) n FROM faq"))["n"] == total
    await faq.seed_defaults()          # повторный запуск не должен плодить дубли
    assert (await db.one("SELECT COUNT(*) n FROM faq"))["n"] == total


# ── меню разделов и списка вопросов ───────────────────────────────────────────
async def test_faq_menu_lists_sections(api, one_question):
    """«Частые вопросы» без аргумента открывает разделы, а не список вопросов."""
    await register(STUDENT)
    await db.run("UPDATE faq SET category='live'")
    await press(STUDENT, "faq")
    text, payloads = api.last(STUDENT)[1], api.payloads(STUDENT)
    assert "Выберите раздел" in text
    assert college.FAQ_CATEGORY_BUTTONS["live"] in text
    assert "faq:live" in payloads
    # служебные кнопки на месте: поиск, сотрудник, контакты, все вопросы
    assert {"faqall:0", "faqask", "new:feedback", "college"} <= set(payloads)


async def test_faq_menu_shows_questions_of_a_section(api, one_question):
    await register(STUDENT)
    await db.run("UPDATE faq SET category='live'")
    await press(STUDENT, "faq:live")
    text = api.last(STUDENT)[1]
    assert "Вопросы раздела «Общежитие» (1)" in text
    # вопрос уходит в текст, а кнопка несёт только номер - подпись не обрежется
    assert QUESTION in text
    assert "faqq:1" in api.payloads(STUDENT)
    assert "1" in [button["text"] for row in api.last(STUDENT)[2] for button in row]


async def test_faq_menu_is_paged_and_below_max_rows(api):
    for index in range(25):
        await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                     (f"Вопрос номер {index}", "Ответ.", f"вопрос номер {index}", "study"))
    await press(STUDENT, "faqall:0")
    body = api.last(STUDENT)
    assert "Все частые вопросы (25)" in body[1]
    assert "страница 1 из 3" in body[1]
    assert len(body[2]) <= max_api.MAX_ROWS
    for row in body[2]:
        assert len(row) <= 7          # предел MAX на ширину ряда
    # номера страниц сквозные: второй вопрос второй страницы - «13»
    await press(STUDENT, "faqall:1")
    assert "страница 2 из 3" in api.last(STUDENT)[1]
    assert "13. Вопрос номер 12" in api.last(STUDENT)[1]

    await press(STUDENT, "faqall:2")
    assert "страница 3 из 3" in api.last(STUDENT)[1]
    # на последней странице «вперёд» уже нет, а «назад» ведёт на предыдущую
    assert "faqall:3" not in api.payloads(STUDENT)
    assert "faqall:1" in api.payloads(STUDENT)


async def test_faq_menu_pads_number_rows_to_max_width(api):
    for index in range(20):
        await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                     (f"Вопрос номер {index}", "Ответ.", f"вопрос номер {index}", "study"))
    await press(STUDENT, "faqall:0")
    numbers = [button["text"] for row in api.last(STUDENT)[2] for button in row]
    assert "12" in numbers
    # ни одна кнопка с номером не обрезана многоточием
    assert not [label for label in numbers if label.endswith("…")]


async def test_faq_menu_empty(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "не заполнены" in api.last(STUDENT)[1]
    assert "new:feedback" in api.payloads(STUDENT)


async def test_faq_menu_when_disabled(api, one_question):
    await register(STUDENT)
    await faq.set_ask_enabled(False)
    await press(STUDENT, "faq")
    assert "выключены" in api.last(STUDENT)[1]
    assert "faqq:1" not in api.payloads(STUDENT)


async def test_faq_item_shows_answer_and_ticket_buttons(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faq")
    await press(STUDENT, "faqq:1")
    text, payloads = api.last(STUDENT)[1], api.payloads(STUDENT)
    assert ANSWER in text and QUESTION in text
    # ответ предлагает уйти в готовый сценарий обращения
    assert "new:feedback" in payloads and "sub:cert" in payloads
    assert "faq" in payloads


async def test_faq_item_missing(api):
    await register(STUDENT)
    await press(STUDENT, "faqq:999")
    assert "не найден" in api.last(STUDENT)[1]


# ── свободный вопрос ──────────────────────────────────────────────────────────
async def test_ask_gives_found_answer(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "у вас есть общежитие?")
    text = api.last(STUDENT)[1]
    assert ANSWER in text
    assert "сотруднику" in text
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0   # ответа хватило


async def test_ask_creates_ticket_when_not_found(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "когда будет контрольная работа по математике")
    text = api.last(STUDENT)[1]
    assert "нет" in text
    ticket = await db.one("SELECT * FROM tickets")
    assert ticket and ticket["category"] == "feedback"
    assert ticket["target_admin_id"] == STAFF
    assert "контрольная работа" in ticket["text_content"]
    assert "Вопрос из FAQ" in ticket["topic"]
    # сотрудник получил уведомление со ссылкой на обращение
    notice = api.to(STAFF)[-1]
    assert "Новый вопрос без ответа" in notice[1]
    payloads = [button["payload"] for row in notice[2] for button in row]
    assert f"rp:{ticket['ticket_id']}" in payloads      # «Ответить» на это обращение


# Вопрос из трёх и более значимых слов: короткий обрывок бота не отправляет
# сотруднику, а просит сформулировать (см. test_faq_voice.py)
LONG_QUESTION = "а где и когда сдают экзамены и кто принимает"


async def test_ask_without_staff_does_not_create_ticket(api, one_question):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, LONG_QUESTION)
    assert "некому передать" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_ask_respects_tickets_switch(api, one_question, staff):
    await db.set_setting("tickets_enabled", "0")
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, LONG_QUESTION)
    assert "временно отключён" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_ask_empty_message(api, one_question, staff):
    """Пустое сообщение - тоже не повод создавать обращение."""
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await faq.st_faq_ask(STUDENT, "   ", {})
    assert "Вопрос пустой" in api.last(STUDENT)[1]
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 0


async def test_ask_state_cleared_after_answer(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "общежитие")
    assert await db.get_state(STUDENT) is None
    # следующее сообщение - уже обычный сценарий бота, а не продолжение FAQ
    await say(STUDENT, "/cancel")
    # приветствие по умолчанию здоровается по имени и перечисляет, что есть
    assert "Это бот колледжа" in api.last(STUDENT)[1]


# ── черновик наполнения ───────────────────────────────────────────────────────
def test_default_faq_is_consistent():
    import college

    assert 45 <= len(college.DEFAULT_FAQ) <= 60
    questions = [item["question"] for item in college.DEFAULT_FAQ]
    assert len(set(questions)) == len(questions)      # без повторов - иначе поиск путается
    for item in college.DEFAULT_FAQ:
        assert item["question"] and item["answer"]
        assert faq.keywords_of(item["keywords"]), item["question"]
        assert len(item["answer"]) < 2000, item["question"]


def test_default_faq_answers_survive_scoring():
    """Каждый черновиковый вопрос находится и по тексту, и по любому своему ключевому слову.

    Это страховка от бесполезного наполнения: если запись не ищется собственными
    ключевыми словами, сис-админ её наполнял зря.
    """
    import college

    for item in college.DEFAULT_FAQ:
        row = {"question": item["question"], "keywords": item["keywords"]}
        assert faq.score(row, item["question"]) >= faq.MIN_SCORE, item["question"]
        for keyword in faq.keywords_of(item["keywords"]):
            assert faq.score(row, keyword) >= faq.MIN_SCORE, (item["question"], keyword)


async def test_seeded_default_faq_is_searchable(api):
    await faq.seed_defaults()
    for question, keywords in (("общежитие", "общежитие"),
                               ("стипендия", "стипендия"),
                               ("документы для поступления", "документы, поступление"),
                               ("расписание занятий", "расписание")):
        answer, title = await faq.find_answer(STUDENT, question)
        assert answer and title, question


# ── флаг настройки ────────────────────────────────────────────────────────────
async def test_ask_enabled_default_is_on(api):
    assert await faq.ask_enabled() is True
    assert await db.get_setting("faq_enabled", "1") == "1"
    await faq.set_ask_enabled(False)
    assert await db.get_setting("faq_enabled") == "0"
    await faq.set_ask_enabled(True)
    assert await db.get_setting("faq_enabled") == "1"


async def test_staff_side_notification_uses_feedback_staff(api, one_question, staff):
    await register(STUDENT)
    await press(STUDENT, "faqask")
    await say(STUDENT, "чем отличается сессия от промежуточной аттестации")
    assert (await db.one("SELECT COUNT(*) n FROM tickets"))["n"] == 1
    assert api.to(STAFF), "сотрудник категории feedback должен получить уведомление"
