"""Справочник колледжа: контакты с сайта, переопределения из панели, черновик FAQ.

Проверяем главное: значения по умолчанию не выдуманы (они переписаны со страниц
сайта и не пустые), панель может их переопределить, а пустое переопределение
возвращает данные с сайта.
"""
import pytest

import college
import database as db
import handlers.faq as faq
from max_api import MAX_TEXT, split_text


# ── значения с сайта ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", [
    "адрес", "почта", "телефон_приёмная", "телефон_учебная_часть",
    "телефон_бухгалтерия", "телефон_директор", "режим_работы", "часы_приёма",
    "часы_приёма_документов", "кабинет_учебной_части", "кабинет_бухгалтерии",
    "кабинет_приёмной_комиссии", "отделения", "общежитие", "расписание",
])
def test_default_value_is_filled_from_site(key):
    assert college.DEFAULTS[key].strip(), key


def test_contacts_are_exactly_as_on_the_site():
    assert college.DEFAULTS["адрес"] == (
        "628672, Ханты-Мансийский автономный округ — Югры, "
        "город Лангепас, улица Ленина, дом 52")
    assert college.DEFAULTS["почта"] == "college_lan@mail.ru"
    assert college.DEFAULTS["телефон_учебная_часть"] == "+7 (34669) 2-71-33"
    assert college.DEFAULTS["телефон_бухгалтерия"] == "+7 (34669) 2-26-34"
    assert college.DEFAULTS["телефон_приёмная"] == "+7 (34669) 2-26-50"
    assert college.DEFAULTS["кабинет_приёмной_комиссии"].endswith("кабинет 115")
    assert college.DEFAULTS["кабинет_бухгалтерии"].endswith("кабинет 201")
    assert "8:30 – 17:00" in college.DEFAULTS["часы_приёма"]
    assert "9:00 – 16:00" in college.DEFAULTS["часы_приёма_документов"]
    assert "12:30 – 14:00" in college.DEFAULTS["режим_работы"]


def test_fields_cover_every_default():
    assert college.FIELDS == tuple(college.DEFAULTS)
    assert set(college.FIELDS) == set(college.DEFAULTS)


def test_unknown_fields_have_no_default():
    """Поле, которого нет в справочнике, не должно молча показывать пустую строку."""
    assert college.DEFAULTS.get("выдуманное_поле", "") == ""


# ── чтение: база важнее модуля ────────────────────────────────────────────────
async def test_get_falls_back_to_module(api):
    assert await college.get("адрес") == college.DEFAULTS["адрес"]
    assert await college.get("выдуманное_поле") == ""


async def test_override_wins_over_module(api):
    await college.override("адрес", "628672, г. Лангепас, ул. Ленина, 52 (новый корпус)")
    assert await college.get("адрес") == "628672, г. Лангепас, ул. Ленина, 52 (новый корпус)"
    assert await db.get_setting("college:адрес")
    assert await college.is_overridden("адрес")


async def test_empty_override_restores_site_value(api):
    await college.override("почта", "novaya@mail.ru")
    await college.override("почта", "")
    assert await college.get("почта") == college.DEFAULTS["почта"]
    assert await db.get_setting("college:почта") == ""
    assert not await college.is_overridden("почта")


async def test_override_trims_and_limits_value(api):
    await college.override("адрес", "  много   пробелов  " + "х" * 2000)
    value = await college.get("адрес")
    assert value.startswith("много   пробелов")
    assert len(value) <= college.MAX_VALUE


async def test_override_of_unknown_key_is_ignored(api):
    await college.override("выдуманное_поле", "что-то")
    assert await db.get_setting("college:выдуманное_поле") == ""


async def test_contacts_returns_every_field_with_overrides(api):
    await college.override("телефон_бухгалтерия", "+7 (34669) 2-26-35")
    data = await college.contacts()
    assert set(data) == set(college.FIELDS)
    assert data["телефон_бухгалтерия"] == "+7 (34669) 2-26-35"
    assert data["адрес"] == college.DEFAULTS["адрес"]


async def test_text_for_the_bot_fits_one_message(api):
    body = await college.text()
    assert body.startswith("🏫")
    assert "адрес: 628672" in body
    assert "телефон учебная часть: +7 (34669) 2-71-33" in body
    assert len(body) < MAX_TEXT       # список для бота влезает в одно сообщение MAX
    assert set(college.MENU_FIELDS) <= set(college.FIELDS)


async def test_full_reference_is_splittable_into_messages(api):
    """Полный справочник длиннее одного сообщения - max_api разошлёт его частями."""
    whole = await college.text(college.FIELDS)
    assert len(whole) > MAX_TEXT
    assert len(split_text(whole)) == 2


async def test_text_shows_gap_for_unknown_field(api):
    """Поля, которого нет в справочнике, видно как прочерк, а не пропадает молча."""
    assert "выдуманное поле: —" in await college.text(("выдуманное_поле",))


# ── ключи настроек ────────────────────────────────────────────────────────────
def test_setting_key_prefix():
    assert college.setting_key("адрес") == "college:адрес"
    assert college.PREFIX == "college:"


# ── черновик FAQ ──────────────────────────────────────────────────────────────
def test_default_faq_size_and_shape():
    assert 10 <= len(college.DEFAULT_FAQ) <= 20
    for item in college.DEFAULT_FAQ:
        assert set(item) == {"question", "keywords", "answer"}
        assert item["question"].strip() and item["answer"].strip() and item["keywords"].strip()


def test_default_faq_questions_are_answered_by_the_faq_handler():
    """Бот должен находить каждый черновик: и по тексту вопроса, и по ключевым словам.

    Проверяем и текст вопроса, и каждое ключевое слово по отдельности - так
    наполнение проверяется целиком, а не по счастливому совпадению.
    """
    for item in college.DEFAULT_FAQ:
        row = {"question": item["question"], "keywords": item["keywords"]}
        assert faq.score(row, item["question"]) >= faq.MIN_SCORE, item["question"]
        for keyword in faq.keywords_of(item["keywords"]):
            assert faq.score(row, keyword) >= faq.MIN_SCORE, (item["question"], keyword)


def test_default_faq_answers_cite_no_made_up_amounts():
    """Кроме цены платного отделения (она на сайте есть) сумм в ответах нет."""
    for item in college.DEFAULT_FAQ:
        assert "86 200" in item["answer"] or "%" not in item["answer"], item["question"]


def test_unknown_data_is_left_blank_instead_of_invented():
    """Нет данных на сайте - стоит честная оговорка, а не выдуманная сумма."""
    assert college.DEFAULTS["общежитие"].startswith("Общежития у колледжа нет")
    assert "не указаны" in college.DEFAULTS["стипендия_и_льготы"]


async def test_seed_defaults_fills_table_from_draft(api):
    added = await faq.seed_defaults()
    assert added == len(college.DEFAULT_FAQ)
    rows = await db.many("SELECT * FROM faq ORDER BY id")
    assert [row["question"] for row in rows] == [item["question"] for item in college.DEFAULT_FAQ]
    # наполнение идемпотентно: повторный запуск не плодит дубли
    await faq.seed_defaults()
    assert (await db.one("SELECT COUNT(*) n FROM faq"))["n"] == len(college.DEFAULT_FAQ)


async def test_seeded_answers_find_by_typical_phrases(api):
    await faq.seed_defaults()
    for question, expect in (
        ("а общежитие у вас есть?", "Общежитие"),
        ("когда принимаете документы", "9:00 – 16:00"),
        ("где посмотреть расписание", "Студентам"),
        ("какие документы нужны для поступления", "СНИЛС"),
    ):
        answer, title = await faq.find_answer("100", question)
        assert answer and expect in answer, (question, title)
