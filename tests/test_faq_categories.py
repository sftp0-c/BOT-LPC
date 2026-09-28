"""Частые вопросы по разделам: меню разделов, поиск по всем разделам, наполнение.

Разделы нужны, потому что вопросов стало больше сорока: список одним экраном
не читается, а искать ответ по-прежнему можно обычным сообщением. Проверяем
три вещи - разделы разложены правильно, бот отвечает по живой формулировке
человека, и всё это укладывается в пределы MAX.
"""
import pytest

import college
import database as db
import handlers.faq as faq
import max_api
from conftest import press, register, say
from handlers.registry import CALLBACKS

STUDENT = "100"

# Фразы, которыми спрашивают по-человечески. Поиск должен находить ответ на
# каждую: если не находит - человек получит «не знаю» вместо ответа.
LIVING_PHRASES = [
    "справка 086у",
    "общежитие для иногородних",
    "отчислиться",
    "стипендия сколько",
    "перевестись с платного",
    "сколько фотографий",
    "заочная форма",
    "практика",
    "отсрочка от призыва",
    "материнский капитал",
]


@pytest.fixture(autouse=True)
async def seeded(env):
    """Черновик с сайта в таблице faq - на нём проверяем и меню, и поиск."""
    await faq.seed_defaults()
    return env


# ── черновик college.DEFAULT_FAQ ──────────────────────────────────────────────
def test_every_question_has_a_category():
    """Без раздела вопрос не попадёт ни в один раздел меню - значит, пропадёт."""
    for item in college.DEFAULT_FAQ:
        assert item["category"].strip(), item["question"]
        assert item["category"] in college.FAQ_CATEGORY_CODES, item["question"]


def test_every_section_is_covered():
    """Раздел без вопросов - кнопка, которая ничего не открывает."""
    used = {item["category"] for item in college.DEFAULT_FAQ}
    assert used == set(college.FAQ_CATEGORY_CODES)


def test_sections_are_in_menu_order():
    """Порядок разделов в меню должен совпадать с порядком в FAQ_CATEGORIES."""
    assert [code for code, _ in college.FAQ_CATEGORIES] == list(college.FAQ_CATEGORY_CODES)
    assert college.FAQ_CATEGORY_BUTTONS["admission"].endswith("Поступление")
    for code, title in college.FAQ_CATEGORIES:
        assert college.category_title(code) == title
        assert college.category_label(code) and " " not in college.category_label(code)


def test_draft_is_big_enough():
    assert len(college.DEFAULT_FAQ) >= 45


def test_clarifying_answers_name_a_phone():
    """Где сайт отвечает неполно, в ответе обязан быть телефон, а не выдумка."""
    phones = (college.DEFAULTS["телефон_учебная_часть"], college.DEFAULTS["телефон_приёмная"],
              college.DEFAULTS["телефон_бухгалтерия"])
    checked = 0
    for item in college.DEFAULT_FAQ:
        if "уточняйте" not in item["answer"].lower():
            continue
        checked += 1
        assert any(phone in item["answer"] for phone in phones), item["question"]
    assert checked >= 10, "оговорок «уточняйте» должно быть много, а не одна"


def test_draft_has_no_invented_percentages():
    for item in college.DEFAULT_FAQ:
        assert "86 200" in item["answer"] or "%" not in item["answer"], item["question"]


# ── схема: колонка разделов ──────────────────────────────────────────────────
async def test_ensure_category_column_is_idempotent(api):
    """Повторный вызов не падает: db.run глушит «duplicate column name»."""
    for _ in range(3):
        await faq.ensure_category_column()
    columns = [row["name"] for row in await db.many("PRAGMA table_info('faq')")]
    assert columns.count("category") == 1


async def test_ensure_category_column_runs_alter_again_without_error(api, monkeypatch):
    """Даже если проверка по файлу базы обошла стороной, ALTER TABLE безопасен."""
    faq._category_ready.clear()
    await faq.ensure_category_column()          # колонка уже есть - db.run это глотает
    assert "category" in {row["name"] for row in await db.many("PRAGMA table_info('faq')")}


async def test_seed_fills_category_for_questions_added_before_sections(api):
    """Вопрос, заведённый до разделов, после заливки попадает в свой раздел."""
    await db.run("DELETE FROM faq")
    await db.run("INSERT INTO faq(question, answer, keywords) VALUES(?, ?, ?)",
                 ("Старый вопрос", "Старый ответ.", "старый"))
    assert (await faq.active_items())[0]["category"] == ""

    assert await faq.seed_defaults() == len(college.DEFAULT_FAQ)

    rows = {row["question"]: row["category"] for row in await db.many("SELECT question, category FROM faq")}
    assert rows["Старый вопрос"] == ""
    for item in college.DEFAULT_FAQ:
        assert rows[item["question"]] == item["category"]


# ── поиск ответа ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("phrase", LIVING_PHRASES)
async def test_find_answer_by_living_phrase(api, phrase):
    answer, title = await faq.find_answer(STUDENT, phrase)
    assert answer and title, phrase


async def test_find_answer_ignores_chosen_section(api):
    """Раздел - это про навигацию по меню, а не про поиск: ответ ищется везде."""
    await press(STUDENT, "faq:work")
    answer, title = await faq.find_answer(STUDENT, "нужна справка 086у")
    assert "086у" in answer and "поступлении" in title


async def test_answer_shows_its_section(api):
    await register(STUDENT)
    await say(STUDENT, "стипендия сколько")
    text = api.last(STUDENT)[1]
    assert f"❓ {college.category_label('money')}:" in text
    assert "Как платят стипендию" in text
    assert "faq" in api.payloads(STUDENT)


async def test_question_card_shows_its_section(api):
    await register(STUDENT)
    await press(STUDENT, "faq:live")
    item_id = api.payloads(STUDENT)[0].split(":")[1]
    await press(STUDENT, f"faqq:{item_id}")
    text = api.last(STUDENT)[1]
    assert text.startswith(f"❓ {college.category_label('live')}: ")


# ── меню разделов ────────────────────────────────────────────────────────────
def test_handlers_are_registered():
    assert CALLBACKS["faq"] is faq.cb_faq
    assert CALLBACKS["faqall"] is faq.cb_faq_all
    assert CALLBACKS["faqq"] is faq.cb_faq_item


async def test_sections_menu_fits_max(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    body = api.last(STUDENT)
    assert len(body[2]) <= max_api.MAX_ROWS
    for row in body[2]:
        assert len(row) <= 7
        limit = max_api.row_limit(len(row))
        for button in row:
            assert len(button["text"]) <= limit, button["text"]
            assert not button["text"].endswith("…"), button["text"]


async def test_sections_menu_lists_every_filled_section(api):
    await register(STUDENT)
    await press(STUDENT, "faq")
    text, payloads = api.last(STUDENT)[1], api.payloads(STUDENT)
    for code, title in college.FAQ_CATEGORIES:
        assert f"faq:{code}" in payloads
        assert title in text
    assert str(len(college.DEFAULT_FAQ)) in text


async def test_section_list_fits_max(api):
    """Самый большой раздел - 22 вопроса, то есть две страницы по 12."""
    await register(STUDENT)
    await press(STUDENT, "faq:admission")
    body = api.last(STUDENT)
    assert len(body[2]) <= max_api.MAX_ROWS
    for row in body[2]:
        assert len(row) <= 7
    assert f"Вопросы раздела «{college.category_label('admission')}" in body[1]
    assert "страница 1 из 2" in body[1]
    # кнопка-номер открывает нужный вопрос, а не «какой-нибудь»
    first = body[2][0][0]["payload"]
    await press(STUDENT, first)
    assert "❓" in api.last(STUDENT)[1]


async def test_all_questions_list_and_numbering(api):
    await register(STUDENT)
    await press(STUDENT, "faqall:0")
    body = api.last(STUDENT)
    assert f"Все частые вопросы ({len(college.DEFAULT_FAQ)})" in body[1]
    assert "1. " in body[1]
    numbers = [button["text"] for row in body[2] for button in row]
    assert "1" in numbers and str(faq.FAQ_PAGE) in numbers
    await press(STUDENT, "faqall:3")
    assert "страница 4 из 5" in api.last(STUDENT)[1]
    assert f"{faq.FAQ_PAGE * 3 + 1}. " in api.last(STUDENT)[1]


async def test_empty_section_says_so_and_offers_others(api):
    await db.run("DELETE FROM faq")
    await db.run("INSERT INTO faq(question, answer, keywords, category) VALUES(?, ?, ?, ?)",
                 ("Только этот", "Ответ.", "только этот", "work"))
    await register(STUDENT)
    await press(STUDENT, "faq:money")
    assert "нет вопросов" in api.last(STUDENT)[1]
    assert "faqall:0" in api.payloads(STUDENT)


async def test_empty_faq_keeps_contacts_reachable(api):
    await db.run("DELETE FROM faq")
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "не заполнены" in api.last(STUDENT)[1]
    assert "college" in api.payloads(STUDENT)


async def test_questions_without_section_are_still_reachable(api):
    """Вопрос, заведённый в панели без раздела, не должен пропасть из бота."""
    await db.run("UPDATE faq SET category=''")
    await register(STUDENT)
    await press(STUDENT, "faq")
    assert "Вне разделов" in api.last(STUDENT)[1]
    await press(STUDENT, "faqall:0")
    assert "Все частые вопросы" in api.last(STUDENT)[1]
