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
from html import unescape
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
    """Настоящая ссылка новой очереди по номеру обращения, без старых wb-обёрток."""
    items = re.findall(r'<a\b[^>]*\bdata-ws-ticket-link\b[^>]*>.*?</a>', body, re.S)
    wanted = [item for item in items if f'aria-label="Обращение №{ticket_id},' in item]
    assert len(wanted) == 1, f"в очереди должна быть ровно одна ссылка на обращение {ticket_id}"
    return wanted[0]


def text_of(html: str) -> str:
    """Видимый текст без разметки, с декодированными HTML-сущностями."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def fio_of(html: str) -> str:
    """Видимый текст имени в очереди или карточке, без соседней группы."""
    block = re.search(r'<strong class="ws-person-name">(.*?)</strong>', html, re.S)
    if block is None:
        block = re.search(r'<p class="ws-detail-person">(.*?)</p>', html, re.S)
    assert block, "в разметке нет видимого блока с ФИО"
    name = re.sub(r'<span class="ws-detail-group">.*?</span>', "", block.group(1), flags=re.S)
    return text_of(name)


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
    group = re.search(r'<span class="ws-queue-group">(.*?)</span>', item, re.S)
    assert group, "у пункта очереди нет группы в строке метаданных"
    assert text_of(group.group(1)) == "24-23"
    assert fio_of(item) == LONG_FIO
    assert f"{LONG_FIO}24-23" not in item          # склейки больше нет
    # Текст обращения и строка метаданных отделены от имени.
    assert 'class="ws-ticket-category"' in item
    assert 'class="ws-preview"' in item
    assert "Нужна справка" in text_of(item)


async def test_queue_keeps_fio_when_group_is_missing(panel_client, env):
    """Нет группы - не значит «нет человека»: ФИО всё равно своё."""
    await repo.upsert_user(STUDENT, LONG_FIO, "")
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    assert fio_of(item) == LONG_FIO
    assert "ws-meta-dot" not in item


async def test_queue_without_fio_shows_max_id(panel_client, env):
    """ФИО не заполнено - подписью становится ID, а не пустая строка."""
    await repo.upsert_user("301", "", "")
    ticket_id = await repo.create_ticket("301", STAFF, "feedback", "Вопрос по справке", "Прочее")
    assert login_panel(panel_client)
    item = queue_item(panel_client.get("/panel/tickets").text, ticket_id)
    assert fio_of(item) == "ID 301"
    assert webpanel.person_label("", "301") == "ID 301"
    # Пустое ФИО не превращается в «Автор обращения» и после открытия карточки.
    for path in (f"/panel/tickets?t={ticket_id}", f"/panel/tickets/{ticket_id}"):
        response = panel_client.get(path)
        assert response.status_code == 200
        head = re.search(r'<p class="ws-detail-person">.*?</p>', response.text, re.S)
        assert head, "в карточке нет видимой подписи автора"
        assert fio_of(head.group(0)) == "ID 301"


# ── карточка обращения ────────────────────────────────────────────────────────
async def test_ticket_card_shows_the_whole_fio(panel_client, env):
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    head = re.search(r'<p class="ws-detail-person">.*?</p>', body, re.S)
    assert head, "в карточке обращения нет шапки с ФИО"
    assert fio_of(head.group(0)) == LONG_FIO


async def test_ticket_card_keeps_group_apart_from_fio(panel_client, env):
    """Группа не приклеивается к имени: между ними своя плашка."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    head = re.search(r'<p class="ws-detail-person">.*?</p>', body, re.S)
    assert head and '<span class="ws-detail-group">24-23</span>' in head.group(0)
    assert fio_of(head.group(0)) == LONG_FIO
    assert f"{LONG_FIO}24-23" not in head.group(0)


# ── списки выбора сотрудника ─────────────────────────────────────────────────
async def test_staff_choice_keeps_fio_and_moves_position_to_hint(panel_client, env):
    """На админ-странице ФИО исполнителя целиком, должность — в подсказке пункта."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    response = panel_client.get(f"/panel/tickets/{ticket_id}/admin")
    assert response.status_code == 200
    block = re.search(r'<select name="target_admin_id">.*?</select>', response.text, re.S)
    assert block, "в администрировании обращения нет списка исполнителя"
    option = re.search(r'<option value="%s".*?</option>' % OTHER, block.group(0), re.S)
    assert option, "в списке исполнителей нет сотрудника"
    text = option.group(0)
    assert text_of(text) == LONG_FIO
    assert f'title="{LONG_POSITION}"' in text
    # Полное имя находится в видимой подписи, а не только в атрибуте title.
    assert LONG_POSITION not in text_of(text)
    assert "…" not in text_of(text)


async def test_staff_picker_on_new_ticket_keeps_fio(panel_client, env):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets/new").text
    block = re.search(r'<select name="target_admin_id">.*?</select>', body, re.S)
    assert block
    option = re.search(r'<option value="%s".*?</option>' % OTHER, block.group(0), re.S)
    assert option and text_of(option.group(0)) == LONG_FIO


async def test_access_list_shows_fio_above_position(panel_client, env):
    """MAX-права на странице сотрудников: ФИО и должность остаются раздельными."""
    assert login_panel(panel_client)
    response = panel_client.get("/panel/staff")
    assert response.status_code == 200
    container = re.search(r'<details\b[^>]*\bid="bot-ticket-access"[^>]*>(.*?)</details>', response.text, re.S)
    assert container, "на странице сотрудников нет блока прав в MAX"
    forms = re.findall(r'<form method="post" action="/panel/tickets/see-all".*?</form>',
                       container.group(1), re.S)
    item = [form for form in forms if f'name="user_id" value="{OTHER}"' in form]
    assert len(item) == 1, "в списке MAX-доступа должна быть одна форма сотрудника"
    name_block = re.search(r'<span class="wb-access-name">(.*?)<span class="wb-access-pos">', item[0], re.S)
    assert name_block, "в списке MAX-доступа нет отдельного блока с именем"
    assert text_of(name_block.group(1)) == LONG_FIO
    assert f'<span class="wb-access-pos">{LONG_POSITION}</span>' in item[0]
    assert 'name="return" value="/panel/staff"' in item[0]
    assert re.search(r'name="csrf" value="[^"]+"', item[0])


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
def workspace_css() -> str:
    """Настоящий отдельный CSS рабочего места, подключённый страницей."""
    path = Path(__file__).resolve().parents[1] / "web" / "static" / "workspace.css"
    return path.read_text(encoding="utf-8-sig")


def print_css(source: str | None = None) -> str:
    """Все блоки @media print выбранной темы, включая её вложенные правила."""
    source = webpanel.STYLE if source is None else source
    blocks = []
    for match in re.finditer(r"@media print\s*\{", source):
        depth, index = 1, match.end()
        while index < len(source) and depth:
            depth += (source[index] == "{") - (source[index] == "}")
            index += 1
        blocks.append(source[match.end():index - 1])
    assert blocks, "в теме нет @media print"
    return "\n".join(blocks)


def fio_style_rules(source: str, selector: str) -> list[str]:
    """Правила самого элемента ФИО во всех media, не правила соседнего превью."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    rules = [body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", source)
             if any(item.strip() == selector or item.strip().endswith(" " + selector)
                    for item in selectors.split(","))]
    assert rules, f"в CSS нет правила для {selector}"
    return rules


def assert_fio_not_clipped(source: str, selector: str):
    """Нормальные браузерные defaults допустимы, ограничения имени — нет."""
    for rule in fio_style_rules(source, selector):
        properties = dict(part.strip().split(":", 1) for part in rule.split(";") if ":" in part)
        properties = {key.strip(): value.replace("!important", "").strip().lower()
                      for key, value in properties.items()}
        assert properties.get("white-space", "normal") == "normal", selector
        assert properties.get("text-overflow", "clip") == "clip", selector
        for key in ("overflow", "overflow-x", "overflow-y"):
            assert properties.get(key, "visible") == "visible", (selector, key)
        assert properties.get("height", "auto") == "auto", selector
        assert properties.get("max-height", "none") == "none", selector
        for key in ("line-clamp", "-webkit-line-clamp"):
            assert properties.get(key, "none") == "none", (selector, key)


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
    workspace = workspace_css()
    for selector in (".ws-detail-person", ".ws-message-author"):
        # Правила без media продолжают действовать при печати; многоточий в них нет.
        assert_fio_not_clipped(workspace, selector)
    printed = print_css(workspace)
    history = " ".join(fio_style_rules(printed, ".ws-history-scroll"))
    assert re.search(r"max-height\s*:\s*none", history)
    assert re.search(r"overflow\s*:\s*visible", history)
    assert_fio_not_clipped(printed, ".ws-detail-main")


def test_queue_styles_allow_fio_to_wrap():
    """Новое ФИО переносится на экране; старые wb-правила это больше не проверяют."""
    source = workspace_css()
    for selector in (".ws-person-name", ".ws-detail-person", ".ws-handler span"):
        assert_fio_not_clipped(source, selector)
        rules = " ".join(fio_style_rules(source, selector))
        assert re.search(r"overflow-wrap\s*:\s*anywhere", rules), selector
    for selector in (".ws-workbench", ".ws-ticket-row", ".ws-detail-heading", ".ws-message-author"):
        assert_fio_not_clipped(source, selector)


async def test_printed_card_contains_the_whole_fio(panel_client, env):
    """В обычной и отдельной печатной карточке ФИО не теряется и не сокращается."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    assert login_panel(panel_client)
    for path in (f"/panel/tickets?t={ticket_id}", f"/panel/tickets/{ticket_id}"):
        response = panel_client.get(path)
        assert response.status_code == 200
        assert 'href="/workspace/assets/workspace.css' in response.text
        card = re.search(r'<article class="ws-detail"[^>]*>(.*)</article>', response.text, re.S)
        assert card, "нет настоящей карточки обращения, которая попадёт на печать"
        assert fio_of(card.group(1)) == LONG_FIO
        assert "…" not in text_of(card.group(1))


# ── статическая страховка: сокращать ФИО больше нельзя ───────────────────────
# Сократители проекта: short() режет многоточием, cut_plain() - по границе слова.
SHORTENERS = ("short", "cut_plain")
# Имена, по которым видно, что режут ФИО, а не текст обращения или детали.
NAME_HINTS = ("fio", "full_name", "student_name", "person", "sender_name",
              "staff_name", "display_name")


PANEL_SOURCES = [Path("webpanel.py")] + sorted(Path("web").glob("*.py"))
"""Файлы панели: фасад и все модули web/.

Один только webpanel.py после разбиения - это 249 строк реэкспорта: читая
его, проверка «ФИО нигде не режут» обходила пустоту вместо панели. utf-8-sig:
файл панели сохранён с BOM, модули пакета - без.
"""


def panel_text(name: str) -> str:
    """Исходник панели по имени файла, где он теперь лежит."""
    return next(path.read_text(encoding="utf-8-sig")
                for path in PANEL_SOURCES if path.name == name)


def source_tree() -> ast.Module:
    """Дерево разбора всей панели: фасад плюс модули web/.

    Имена импортированных помощников перекрываются, поэтому достаточно
    дерева самого большого по числу вызовов модуля разбора: проверка ниже
    ищет вызовы short()/cut_plain() по именам-подсказкам, а не сравнивает
    определения. Модулей ровно столько, сколько файлов в PANEL_SOURCES, и
    каждый входит ровно один раз.
    """
    trees = [ast.parse(path.read_text(encoding="utf-8-sig")) for path in PANEL_SOURCES]
    biggest = max(trees, key=lambda tree: sum(1 for _ in ast.walk(tree)))
    merged = ast.Module(body=sum((tree.body for tree in trees), []), type_ignores=[])
    merged.name = "panel"
    return merged if biggest is not None else merged


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
            found.append(f"панель:{node.lineno} {node.func.id}(...)")
    assert not found, "ФИО снова сокращают: " + "; ".join(found)


def test_fio_helper_never_uses_ellipsis():
    """Единственное место, где ФИО режется, - ``fio()``, и без многоточия.

    Всё остальное панель обязана звать именно его: иначе обрезание вернётся
    в обход проверки выше.
    """
    holders = [name for name in (path.name for path in PANEL_SOURCES)
               if re.search(r"\ndef fio\(", panel_text(name))]
    assert holders, "в панели нет помощника fio() - проверяем все файлы: " + ", ".join(
        path.name for path in PANEL_SOURCES)
    rule = re.search(r"def fio\(.*?(?=\ndef |\Z)", panel_text(holders[0]), re.S)
    body = rule.group(0)
    assert "short(" not in body, "fio() снова зовёт short() - это многоточие"
    assert "cut_plain(" in body, "fio() должен резать по границе слова"


def test_no_ellipsis_left_in_queue_markup():
    """Проверяется новая очередь, а не оставшийся неиспользуемый wb-renderer."""
    source = panel_text("workspace_view.py")
    tree = ast.parse(source)
    queue = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_queue_row"), None)
    assert queue is not None, "нет рендерера элемента очереди"
    block = ast.get_source_segment(source, queue)
    assert "short(" not in block, "в очереди снова появился short()"
    assert 'class="ws-person-name"' in block and 'class="ws-preview"' in block
    # Превью текста можно сокращать, ФИО отдельного элемента — нельзя.
    for node in ast.walk(queue):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            assert not isinstance(node.value, ast.Name) or node.value.id != "name", "ФИО обрезается срезом"
