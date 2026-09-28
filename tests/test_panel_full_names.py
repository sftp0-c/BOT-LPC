"""Панель показывает ФИО целиком - там, где человек ищут в записи.

Сотрудник приёмной комиссии сверяет обращение с человеком по журналу, поэтому
обрезанное имя («Ковалевский Ко…») для такой работы бесполезно. Проверяем
поведение страниц, а не отдельные строки кода:

* в очереди ФИО и группа читаются отдельно, имя не уезжает в многоточие;
* в карточке обращения имя целиком, группа не прилипает к нему вплотную;
* без ФИО подписью становится MAX ID (``utils.person_label``);
* ФИО сотрудника в списках выбора целиком, должность ушла в подсказку;
* печатная карточка печатает имя целиком, а не режет его молча;
* и, наконец, статикой - в ``webpanel.py`` не появилось нового ``short()``
  или ``cut_plain()`` по ФИО: иначе проблема вернётся через месяц.
"""
import ast
import re
from pathlib import Path

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
import webpanel
from conftest import add_staff, login_panel, register

STUDENT, STAFF, OTHER = "300", "200", "201"
# Самое длинное ФИО из наших тестовых данных: 33 символа, в одну строку
# колонки очереди оно не влезало и уезжало в многоточие.
LONG_FIO = "Ковалевский Константин Юрьевичович"
LONG_POSITION = "Методист учебной части факультета"


@pytest.fixture
async def env(env):
    await register(STUDENT, LONG_FIO, "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    await add_staff(OTHER, LONG_FIO, position=LONG_POSITION)
    return env


def queue_item(body: str, ticket_id: int) -> str:
    """Разметка одного пункта очереди - по номеру обращения."""
    items = re.findall(r'<label class="wb-item.*?</label>', body, re.S)
    wanted = [item for item in items if f'value="{ticket_id}"' in item]
    assert wanted, f"в очереди нет обращения {ticket_id}"
    return wanted[0]


def text_of(html: str) -> str:
    """Текст без разметки: так проверяем, что видит человек, а не что в коде."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def fio_of(html: str) -> str:
    """Видимый текст блока .wb-fio - полное ФИО, как его рисует страница."""
    block = re.search(r'<span class="wb-fio">(.*?)</span>', html, re.S)
    assert block, "в разметке нет блока с ФИО (.wb-fio)"
    return text_of(block.group(1))


# ── очередь рабочего места ───────────────────────────────────────────────────
async def test_queue_shows_the_whole_fio(panel_client, env):
    """Имя видно целиком: многоточие в очереди больше не появляется."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    assert fio_of(item) == LONG_FIO
    assert "…" not in text_of(item)


async def test_queue_separates_fio_from_group(panel_client, env):
    """ФИО и группа - разные элементы, а не «Имя Группа» в одной строке."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    group = re.search(r'<span class="wb-group">(.*?)</span>', item, re.S)
    assert group, "у пункта очереди нет отдельного блока с группой"
    assert text_of(group.group(1)) == "24-23"
    assert f"{LONG_FIO}24-23" not in item          # склейки больше нет
    # текст обращения - отдельной строкой, имя его не делит
    assert 'class="wb-text"' in item
    assert "Нужна справка" in text_of(item)


async def test_queue_keeps_fio_when_group_is_missing(panel_client, env):
    """Нет группы - не значит «нет человека»: ФИО всё равно своё."""
    await repo.upsert_user(STUDENT, LONG_FIO, "")
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    assert fio_of(item) == LONG_FIO
    assert "wb-group" not in item


async def test_queue_without_fio_shows_max_id(panel_client, env):
    """ФИО не заполнено - подписью становится ID, а не пустая строка."""
    await repo.upsert_user("301", "", "")
    ticket_id = await repo.create_ticket("301", STAFF, "feedback", "Вопрос по справке", "Прочее")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    assert fio_of(item) == "ID 301"
    assert webpanel.person_label("", "301") == "ID 301"      # тот же путь, что в панели


# ── карточка обращения ────────────────────────────────────────────────────────
async def test_ticket_card_shows_the_whole_fio(panel_client, env):
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    head = re.search(r'<p class="wb-who">.*?</p>', body, re.S)
    assert head, "в карточке обращения нет шапки с ФИО"
    assert fio_of(head.group(0)) == LONG_FIO


async def test_ticket_card_keeps_group_apart_from_fio(panel_client, env):
    """Группа не приклеивается к имени: между ними своя плашка."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    head = re.search(r'<p class="wb-who">.*?</p>', body, re.S)
    assert head and '<span class="wb-group">24-23</span>' in head.group(0)
    assert f"{LONG_FIO}24-23" not in head.group(0)


# ── списки выбора сотрудника ─────────────────────────────────────────────────
async def test_staff_choice_keeps_fio_and_moves_position_to_hint(panel_client, env):
    """В выпадающем списке ФИО целиком, должность - в подсказке пункта."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    block = re.search(r'<select name="target_admin_id">.*?</select>', body, re.S)
    assert block, "в карточке обращения нет списка исполнителя"
    option = re.search(r'<option value="%s".*?</option>' % OTHER, block.group(0), re.S)
    assert option, "в списке исполнителей нет сотрудника"
    text = option.group(0)
    assert LONG_FIO in text
    assert f'title="{LONG_POSITION}"' in text
    # должность - в подсказке, а не в самом пункте: там важнее имя
    assert LONG_POSITION not in text.split("</option>")[0].split(">")[-1]


async def test_staff_picker_on_new_ticket_keeps_fio(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets/new").text
    block = re.search(r'<select name="target_admin_id">.*?</select>', body, re.S)
    assert block
    option = re.search(r'<option value="%s".*?</option>' % OTHER, block.group(0), re.S)
    assert option and LONG_FIO in option.group(0)


async def test_access_list_shows_fio_above_position(panel_client, env):
    """Список «кто видит чужие обращения»: ФИО и должность двумя строками."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    block = body.split("Кто видит чужие обращения", 1)[1]
    forms = re.findall(r'<form method="post" action="/panel/tickets/see-all".*?</form>',
                       block, re.S)
    item = [form for form in forms if f'name="user_id" value="{OTHER}"' in form]
    assert item, "в списке доступа нет сотрудника"
    name_block = re.search(r'<span class="wb-access-name">(.*?)\n', item[0], re.S)
    assert name_block, "в списке доступа нет отдельного блока с именем"
    assert text_of(name_block.group(1)) == LONG_FIO
    assert f'<span class="wb-access-pos">{LONG_POSITION}</span>' in item[0]


# ── карточка человека и реестр людей ─────────────────────────────────────────
async def test_person_card_shows_fio_with_initials(panel_client, env):
    """Полное имя шапкой, инициалы - отдельной строкой под ним."""
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/people/{STUDENT}").text
    title = re.search(r'<h2 class="wb-name">(.*?)</h2>', body, re.S)
    assert title and text_of(title.group(1)) == LONG_FIO
    brief = re.search(r'<p class="small mut wb-brief">(.*?)</p>', body, re.S)
    assert brief, "в карточке человека нет строки с инициалами"
    assert text_of(brief.group(1)) == "Ковалевский К. Ю."
    # сокращение не вытеснило полное имя
    assert LONG_FIO in body and "Ковалевский К. Ю." in body


async def test_people_list_shows_fio_whole(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/people").text
    assert LONG_FIO in text_of(body)
    assert "Ковалевский Ко…" not in body


# ── печатная версия ──────────────────────────────────────────────────────────
def print_css() -> str:
    """Все блоки @media print вместе: правил с этим условием несколько."""
    blocks = []
    for match in re.finditer(r"@media print\s*\{", webpanel.STYLE):
        depth, index = 1, match.end()
        while index < len(webpanel.STYLE) and depth:
            depth += (webpanel.STYLE[index] == "{") - (webpanel.STYLE[index] == "}")
            index += 1
        blocks.append(webpanel.STYLE[match.end():index - 1])
    assert blocks, "в теме нет @media print"
    return "\n".join(blocks)


def test_print_does_not_truncate_fio():
    """На бумаге многоточие не видно - текст обрезался бы молча.

    Поэтому печатная версия обязана отменять и ellipsis, и запрет на перенос
    строк у ФИО и у шапки карточки.
    """
    rules = print_css()
    for selector in (".wb-fio", ".wb-who", ".wb-name"):
        assert selector in rules, f"в @media print нет правила для {selector}"
    assert "text-overflow:clip" in rules
    assert re.search(r"\.wb-fio[^{}]*\{[^}]*white-space:normal", rules)


def test_queue_styles_allow_fio_to_wrap():
    """На экране ФИО переносится по словам, а не уезжает в многоточие."""
    rule = re.search(r"\.wb-fio\{[^}]*\}", webpanel.STYLE)
    assert rule, "в теме нет правила для .wb-fio"
    body = rule.group(0)
    assert "text-overflow:ellipsis" not in body
    assert "white-space:normal" in body
    assert "overflow-wrap:anywhere" in body


async def test_printed_card_contains_the_whole_fio(panel_client, env):
    """Печатается та же карточка, что и на экране: имя в ней целиком."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    card = body.split('class="wb-card"', 1)[1]
    assert fio_of(card) == LONG_FIO
    assert "…" not in text_of(card)


# ── статическая страховка: сокращать ФИО больше нельзя ───────────────────────
# Сократители проекта: short() режет многоточием, cut_plain() - по границе слова.
SHORTENERS = ("short", "cut_plain")
# Имена, по которым видно, что режут ФИО, а не текст обращения или детали.
NAME_HINTS = ("fio", "full_name", "student_name", "person", "sender_name",
              "staff_name", "display_name")


def source_tree() -> ast.Module:
    # utf-8-sig: файл панели сохранён с BOM
    return ast.parse(Path("webpanel.py").read_text(encoding="utf-8-sig"))


def test_no_shortening_of_names_in_webpanel():
    """Ни одного short()/cut_plain() по ФИО: иначе обрезанное имя вернётся.

    Проверка по дереву разбора, а не по grep: она видит и ``short(row["fio"])``,
    и ``short(f"{person['full_name']} · ...")`` - там, где имя спрятано внутрь
    f-строки. Текст обращения и детали журнала резать можно, поэтому под
    проверку попадают только вызовы, где в аргументах упоминается имя.
    """
    found = []
    for node in ast.walk(source_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in SHORTENERS:
            continue
        if any(hint in ast.dump(node) for hint in NAME_HINTS):
            found.append(f"webpanel.py:{node.lineno} {node.func.id}(...)")
    assert not found, "ФИО снова сокращают: " + "; ".join(found)


def test_fio_helper_never_uses_ellipsis():
    """Единственное место, где ФИО режется, - ``fio()``, и без многоточия.

    Всё остальное панель обязана звать именно его: иначе обрезание вернётся
    в обход проверки выше.
    """
    rule = re.search(r"def fio\(.*?(?=\ndef )", Path("webpanel.py").read_text(
        encoding="utf-8-sig"), re.S)
    assert rule, "в webpanel.py нет помощника fio()"
    body = rule.group(0)
    assert "short(" not in body, "fio() снова зовёт short() - это многоточие"
    assert "cut_plain(" in body, "fio() должен резать по границе слова"


def test_no_ellipsis_left_in_queue_markup():
    """В очереди не осталось склейки «имя · текст» через многоточие."""
    text = Path("webpanel.py").read_text(encoding="utf-8-sig")
    block = text[text.index("def _tickets_queue("):text.index("async def _ticket_workbench(")]
    assert "short(" not in block, "в очереди снова появился short()"
    assert 'class="wb-who"' in block and 'class="wb-fio"' in block
