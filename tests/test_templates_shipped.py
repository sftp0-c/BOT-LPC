"""Типовые шаблоны ответов: бот должен понимать каждую подстановку сам.

Дефект, ради которого написан этот файл: типовой шаблон «Не выдадут без причины»
держал подстановку {причина}, которой нет среди известных. render_template()
неизвестное имя оставляет как есть (выбросить его молча нельзя - сотрудник
отправит письмо с дырой), и студент получал письмо с буквами
«выдать не получится: {причина}» прямо в тексте.

Поэтому проверяем не сам список шаблонов, а результат: каждое типовое письмо
проходит настоящий рендер и не оставляет ни скобок, ни пустот; ни одна
подстановка не молчит на пустых данных; список известных имён совпадает с тем,
что бот действительно умеет подставлять.
"""
import re

import pytest

import database as db
from utils import as_str
from handlers import common, tickets

SHIPPED = db.DEFAULT_REPLY_TEMPLATES
"""Типовые шаблоны из database.py: (название, раздел, текст)."""

TITLES = [title for title, _category, _text in SHIPPED]
IDS = TITLES

# Имена, которые в типовых письмах не нужны, но сотрудник вписывает их в свои
# («Здравствуйте, {имя} {отчество}», «вопрос по {теме}», «звоните директору»).
# Они не мёртвые: бот их подставляет (см. test_every_known_name_is_filled) и
# показывает в подсказке placeholder_help() и в панели. В типовом письме их нет
# не по забывчивости, а по смыслу: письмо «всем сразу» не пишут с отчеством
# или с телефоном директора. Список явный, чтобы новое имя в TPL_FIELDS нельзя
# было добавить молча и забыть.
# «номер» в типовом письме тоже не нужен: при создании обращения бот уже
# пишет студенту «Обращение №N отправлено», и повторять номер в письме
# незачем - на обращении без номера он превращался в «№ не указано».
STAFF_ONLY = frozenset({
    "фамилия", "отчество", "преподаватель", "должность", "кабинет",
    "кабинет_выдачи", "время", "тема", "учебная_часть", "директор", "номер",
})

# Пустое обращение: данных нет вообще, даже номера. Ровно тот случай, из-за
# которого в письме появляются пустые подстановки.
EMPTY_TICKET = {
    "student_id": "", "target_admin_id": "", "category": "feedback",
    "topic": "", "pickup_place": "",
    "student": {"full_name": "", "group_code": ""},
    "staff": {"full_name": "", "position": "", "role": "", "office": ""},
}
BARE_TICKET = {}          # даже ключей нет: _get() отдаёт None

FULL_TICKET = {
    "ticket_id": 7, "student_id": "100", "target_admin_id": "200",
    "category": "certificates", "topic": "Справка для места практики",
    "pickup_place": "203",
    "student": {"full_name": "Иванов Иван Иванович", "group_code": "ИС-21"},
    "staff": {"full_name": "Петрова Анна Сергеевна", "position": "Методист",
              "role": "staff", "office": "215"},
}


def holes(text: str) -> list:
    """Где в письме осталась дыра: скобки, «№» без номера, пробел перед знаком.

    Пустая подстановка видна студенту как «вопросы: .» или «обращение № принято»,
    поэтому ищем именно следы пропуска, а не сам текст шаблона.
    """
    found = []
    if "{" in text or "}" in text:
        found.append("осталась неподставленная скобка")
    if re.search(r"№\s*($|[\s,.!?;:—-])", text):
        found.append("«№» без номера после него")
    if re.search(r"\s[,.!?;:]", text) or "  " in text:
        found.append("пустая подстановка: пробел перед знаком или два пробела")
    return found


def used_names(*texts: str) -> set:
    """Имена подстановок, встречающиеся в текстах (в любом регистре)."""
    return {name.strip().lower() for text in texts
            for name in tickets.PLACEHOLDER_RE.findall(text)}


# ── типовые шаблоны знают только известные подстановки ───────────────────────
def test_there_are_shipped_templates():
    """Пустой список типовых - тоже дефект: сотруднику нечего отправлять."""
    assert SHIPPED, "DEFAULT_REPLY_TEMPLATES пуст"
    assert len(SHIPPED) >= 5


@pytest.mark.parametrize("title,category,text", SHIPPED, ids=IDS)
def test_shipped_template_uses_only_known_placeholders(title, category, text):
    """Главная защита от повторения дефекта 1: неизвестных имён быть не должно.

    Неизвестное имя не выбрасывается, а остаётся в письме, поэтому шаблон с
    незнакомой подстановкой - это не ошибка разбора, а готовое письмо студенту
    с буквами в скобках.
    """
    unknown = tickets.unknown_placeholders(text)
    assert unknown == [], f"«{title}»: бот не знает подстановки {unknown}"


def test_refusal_template_is_not_shipped():
    """Отказ без причины - не типовой шаблон, в DEFAULT_REPLY_TEMPLATES его нет.

    Причину сотрудник пишет под конкретного человека, готового текста у отказа
    нет, а заготовка только мешала: одно нажатие - и письмо с местом для
    заполнения ушло студенту. Вернуть его можно, но не раньше, чем в TPL_FIELDS
    появится «причина» (см. комментарий в database.py).
    """
    assert "Не выдадут без причины" not in TITLES
    # и ни в одном типовом письме не осталось подстановки-причины
    assert "причина" not in used_names(
        *(text for _title, _category, text in SHIPPED))


# ── рендер типовых писем не оставляет дыр ────────────────────────────────────
@pytest.mark.parametrize("title,category,text", SHIPPED, ids=IDS)
async def test_shipped_template_renders_without_holes(title, category, text):
    """Даже на пустых данных письмо остаётся связным: ни скобок, ни пропусков."""
    text = await tickets.render_template(text, EMPTY_TICKET)
    assert holes(text) == [], f"«{title}» на пустых данных: {text}"
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("title,category,text", SHIPPED, ids=IDS)
async def test_shipped_template_renders_with_full_data(title, category, text):
    """С полными данными в письме остаются настоящие имена, а не заглушки."""
    text = await tickets.render_template(text, FULL_TICKET)
    assert holes(text) == [], f"«{title}»: {text}"
    assert "Иванов Иван Иванович" in text       # {ФИО} подставился
    assert "не указано" not in text            # данных хватало на всё


@pytest.mark.parametrize("title,category,text", SHIPPED, ids=IDS)
async def test_shipped_template_renders_without_the_ticket(title, category, text):
    """Обращения нет вообще - даже ключей. Письмо всё равно остаётся связным.

    Хуже пустого обращения представить трудно: ни данных, ни номера. Шаблон,
    который и тут оставляет дыру, студенту отправят одним нажатием.
    """
    text = await tickets.render_template(text, BARE_TICKET)
    assert holes(text) == [], f"«{title}» без обращения: {text}"
    assert tickets.TPL_STUDENT in text      # вместо ФИО - слово, а не пустота
    assert "{" not in text and "}" not in text


# ── подстановки не молчат ────────────────────────────────────────────────────
@pytest.mark.parametrize("ticket", [EMPTY_TICKET, BARE_TICKET, FULL_TICKET],
                         ids=["пустое обращение", "вообще без ключей", "полные данные"])
async def test_no_placeholder_is_empty(ticket):
    """Ни одно значение template_values() не пустое - ни при каких данных.

    Пустая строка в письме читается как ошибка бота, а docstring обещает, что
    вместо пропуска будет слово. Раньше молчали {отчество} (нет отчества) и
    {номер} (нет ticket_id).
    """
    values = await tickets.template_values(ticket)
    empty = sorted(name for name, value in values.items() if not as_str(value).strip())
    assert empty == [], f"молчат подстановки: {empty}"


async def test_missing_patronymic_and_number_are_named():
    """Конкретные случаи из докстринга: отчества и номера нет - подставляем слово."""
    values = await tickets.template_values(EMPTY_TICKET)
    assert values["отчество"] == "не указано"
    assert values["номер"] == "не указано"
    assert values["фио"] == tickets.TPL_STUDENT


async def test_missing_placeholder_word_is_used_in_letter():
    """Слово-заглушка доходит до письма студенту, а не остаётся в шаблоне."""
    template = "{ФИО}, ваше обращение №{номер}, отчество {отчество}."
    text = await tickets.render_template(template, BARE_TICKET)
    assert text == f"{tickets.TPL_STUDENT}, ваше обращение №не указано, отчество не указано."
    assert holes(text) == []


# ── список имён совпадает с тем, что бот умеет ───────────────────────────────
async def test_every_known_name_is_filled():
    """Обратная сторона: нет имени, которое бот обещает, но не подставляет.

    Имя из TPL_FIELDS показывается сотруднику в подсказке, но если его нет в
    template_values(), render_template() оставит его в письме как есть - тот же
    дефект, только с другой стороны.
    """
    values = await tickets.template_values(FULL_TICKET)
    known = {name.lower() for name in tickets.TPL_FIELDS}
    assert known == set(values), {
        "обещано, но не подставляется": sorted(known - set(values)),
        "подставляется, но не обещано": sorted(set(values) - known),
    }


def test_no_dead_placeholder_names():
    """Каждое имя из TPL_FIELDS или используется в типовых письмах, или объявлено.

    Иначе оно мёртвое: сотруднику показывают в подсказке имя, которое никто не
    подставит. Новое имя в TPL_FIELDS обязано появиться либо в типовом письме,
    либо в STAFF_ONLY - с объяснением, почему типовым оно не нужно.
    """
    used = used_names(*(text for _title, _category, text in SHIPPED),
                      common.DEFAULT_WELCOME)
    declared = {name.lower() for name in STAFF_ONLY}
    assert declared <= {name.lower() for name in tickets.TPL_FIELDS}, \
        "в STAFF_ONLY есть имя, которого нет в TPL_FIELDS"
    dead = {name.lower() for name in tickets.TPL_FIELDS} - used - declared
    assert dead == set(), f"мёртвые имена: {sorted(dead)}"
    # и наоборот: объявленные «только для своих шаблонов» правда не типовые
    used_shipped = used_names(*(text for _title, _category, text in SHIPPED))
    assert declared & used_shipped == set(), \
        f"эти имена уже есть в типовых, уберите их из STAFF_ONLY: {sorted(declared & used_shipped)}"


# ── типовые письма действительно попадают сотруднику ──────────────────────────
async def test_shipped_templates_are_seeded():
    """Все типовые шаблоны заливаются в базу, и убранного отказа среди них нет."""
    titles = {as_str(row["title"]).strip() for row
              in await db.many("SELECT title FROM reply_templates")}
    assert set(TITLES) <= titles, f"не залились: {sorted(set(TITLES) - titles)}"
    assert "Не выдадут без причины" not in titles


async def test_shipped_refusal_text_is_never_able_to_reach_student():
    """Даже если такую запись завели руками, бот честно скажет о неизвестном имени."""
    await db.run("INSERT INTO reply_templates(title, text, category, created_by, "
                 "created_at) VALUES(?,?,?,?,?)",
                 ("Отказ", "Выдать не получится: {причина}.", "certificates",
                  "система", "2026-01-01 00:00:00"))
    row = await db.one("SELECT * FROM reply_templates WHERE title='Отказ'")
    text = await tickets.render_template(row["text"], FULL_TICKET)
    assert text == "Выдать не получится: {причина}."     # не выдумываем причину
    unknown = tickets.unknown_placeholders(row["text"])
    assert unknown == ["причина"]
    assert "Непонятно: {причина}" in tickets.placeholder_help(unknown)
