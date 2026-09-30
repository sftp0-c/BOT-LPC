"""Кнопки страницы «Расписания»: подпись в одну строку, зазор и работоспособность.

Жалобы сис-админа на этой странице («кнопки слипшиеся», «подпись по буквам вниз»,
«поля ложатся друг на друга») выглядели одинаково, а причины было три:

* кнопка отправки формы лежала прямо в ``.grid``. Тема растягивает каждого
  прямого потомка сетки (``flex:1 1 180px``) и разрешает рвать слова в любом
  месте (``overflow-wrap:anywhere``), поэтому подпись «Импортировать с сайта»
  распадалась на три строки. Приём, который это снимает, - обернуть кнопку в
  ``<div>``: растягивается ячейка, а не сама кнопка;
* подпись уходила в ``esc()``, поэтому ``icon(...)`` попадал в кнопку не значком,
  а текстом вроде ``<svg class="ico ico-download" ...>`` - кнопка выглядела как
  три строки технической разметки;
* две кнопки строки стояли в одной ячейке таблицы подряд, зазор между ними задавал
  пробел между тегами, а не раскладка темы; удаление при этом было нарисовано
  одной иконкой, без подписи.

Проверки поведенческие, а не «в файле есть такая строка»: страница собирается
настоящим запросом к TestClient, разметка разбирается в дерево, и утверждения
идут по узлам этого дерева. Кнопки, ряды кнопок и формы считаются по всем
элементам ``<main>``, а не по выборочно названным - новая кнопка попадёт в
проверку сама.

Отдельно проверяется, что страница не сломалась: разбор PDF по кнопке, разбор
всех, выбор группы, правка времени пары, сохранение звонков, ручное сохранение,
импорт, удаление и обязательный CSRF на каждой форме.
"""
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo

from conftest import login_panel, post_form
from handlers import schedules
from test_timetable import cyrillic_week


ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "web" / "schedules.py"

GROUP = "ИС-21"                 # группа, ради которой всё и затевалось
OTHER = "ИС-22"
SAVED = "ИС-99"
URL = "https://college.example/r.pdf"

# Классы темы, которые дают ряд кнопок зазор. Всё остальное рядом не разводит.
GAP = {"grid", "wb-tools", "page-actions", "cards", "kpi", "pager", "msg", "dochead"}

# Элементы, где соседство задаёт сама таблица или текст ячейки, а не раскладка.
NOT_A_ROW = {"html", "body", "main", "table", "thead", "tbody", "tfoot", "tr", "td",
             "th", "ul", "ol", "li", "label", "p", "caption"}

# Строчные элементы: два таких рядом встают в одну строку, и зазор между ними
# задаёт только раскладка темы. Блочные, наоборот, встают столбиком и липнуть
# не могут - поэтому проверка смотрит именно на них.
INLINE = {"span", "a", "b", "i", "em", "strong", "small", "code", "kbd", "time",
          "input", "button", "textarea", "output"}

# Теги, которые не имеют закрывающего тега: иначе разбор уедет вправо.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr", "path", "circle", "rect", "line",
        "polyline", "polygon", "ellipse", "use", "stop"}

# Что означает «кнопка» в панели: <button> и ссылки, которые тема рисует кнопками.
BUTTON_TAGS = {"button"}
BUTTON_CLASSES = {"btn"}


# ── разбор страницы в дерево ─────────────────────────────────────────────────
class Node:
    """Узел разобранной страницы: тег, атрибуты, текст, дети и родитель."""

    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.parent = parent
        self.text = []

    @property
    def classes(self) -> set:
        return set(self.attrs.get("class", "").split())

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, tag: str) -> list:
        return [node for node in self.walk() if node.tag == tag]

    def label(self) -> str:
        """Подпись кнопки: её собственный текст, иначе aria-label или title."""
        parts = []
        for node in self.walk():
            parts.extend(node.text)
        text = " ".join("".join(parts).split())
        return text or self.attrs.get("aria-label", "") or self.attrs.get("title", "")

    def bears_button(self) -> bool:
        """Внутри этого элемента есть кнопка (может быть глубоко)."""
        return any(node.tag in BUTTON_TAGS for node in self.walk())


class Tree(HTMLParser):
    """Разбор страницы панели в дерево по правилам HTML5.

    Отличие от HTMLParser - незакрытый ``<tr>``, ``<td>`` или ``<li>`` закрывается
    сам. Таблицы в панели печатаются вплотную (``<tr><th>..</th><th>..``), и на
    строгом разборе дерево уехало бы вправо: кнопки «Разобрать» и «Удалить»
    оказались бы не в своей ячейке, и проверки врут.
    """

    CLOSE = {"li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "p": {"p"},
             "option": {"option", "optgroup"}, "optgroup": {"option"},
             "tr": {"tr", "td", "th"}, "td": {"td", "th"}, "th": {"td", "th"},
             "thead": {"td", "th", "tr"}, "tbody": {"td", "th", "tr"},
             "tfoot": {"td", "th", "tr"}}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("[document]", {}, None)
        self.stack = [self.root]

    def _implied_end(self, tag: str) -> None:
        closes = self.CLOSE.get(tag)
        if not closes:
            return
        while len(self.stack) > 1 and self.stack[-1].tag in closes:
            self.stack.pop()

    def _open(self, tag, attrs):
        self._implied_end(tag)
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].text.append(data)


def parse(body: str) -> Node:
    tree = Tree()
    tree.feed(body)
    tree.close()
    return tree.root


def main_of(root: Node) -> Node:
    for node in root.walk():
        if node.tag == "main":
            return node
    return root


def buttons(root: Node) -> list:
    """Все кнопки страницы: <button> и ссылки, которые тема рисует кнопками."""
    return [node for node in main_of(root).walk()
            if node.tag in BUTTON_TAGS or (node.tag == "a" and node.classes & BUTTON_CLASSES)]


def inline_row(node: Node) -> bool:
    """Строчный элемент: встаёт в общую строку с соседями, а не столбиком."""
    if node.tag == "input" and node.attrs.get("type") == "hidden":
        return False           # скрытое поле в строку не встаёт
    return node.tag in INLINE or "inline" in node.classes


# ── фикстуры ─────────────────────────────────────────────────────────────────
async def _probe_ok(url: str) -> tuple:
    return True, ""


@pytest.fixture
def panel(panel_client, monkeypatch, env, tmp_path):
    """Панель, вошедшая как сис-админ, с подменённой сетью: PDF отдаётся всегда."""
    async def fake_download(url: str) -> bytes:
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(schedules, "download", fake_download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    import webpanel
    monkeypatch.setattr(webpanel, "probe_pdf_url", _probe_ok)
    assert login_panel(panel_client)
    return panel_client


@pytest.fixture
async def groups(panel, env):
    """Две группы в базе: одна разобрана, вторая со ссылкой, но без уроков."""
    await repo.upsert_schedule(GROUP, URL)
    await repo.upsert_schedule(OTHER, "https://college.example/22.pdf")
    await schedules.parse_group(GROUP, force=True)
    return panel


CARD = f"/panel/schedules/{quote(GROUP)}"


def body(client, path: str = "/panel/schedules") -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.text


def hidden_code(page: str) -> set:
    """Коды групп, которым в списке принадлежат кнопки строки."""
    return {node.attrs.get("value") for node in parse(page).find_all("input")
            if node.attrs.get("name") == "group_code" and node.attrs.get("value")}


# ── главное: кнопка не должна быть прямым ребёнком .grid ─────────────────────
async def test_no_button_is_a_direct_child_of_a_grid(groups):
    """Главная проверка против повторения схлопнутой кнопки.

    Тема: ``.grid>*{flex:1 1 180px}`` плюс ``overflow-wrap:anywhere`` в ячейке.
    Прямой потомок сетки получает ширину ячейки вместо своей, и длинная подпись
    распадается на несколько строк - ровно то, что намерил браузер (кнопка
    «Импортировать с сайта» высотой 90 пикселей в три строки). Обёрнутая в
    ``<div>`` кнопка такой ширины не получает.
    """
    for path in ("/panel/schedules", CARD):
        found = buttons(parse(body(groups, path)))
        assert found, f"{path}: кнопок не нашлось, проверка бы ничего не проверяла"
        for node in found:
            assert "grid" not in node.parent.classes, (
                f"{path}: кнопка {node.label()!r} лежит прямо в .grid - "
                "оберните её в <div>, тогда её растягивает ячейка, а не она сама")


async def test_page_source_never_puts_a_button_straight_into_a_grid():
    """Та же гарантия по исходнику: кнопка не печатается прямо в ``<div class="grid">``.

    Проверка по разметке ловит уже сломанную страницу, эта - момент появления
    поломки. Смысл не только в кнопке: ``form()`` из web.common кладёт кнопку
    отправки прямо в ``.grid``, поэтому вернуть его на этой странице нельзя.
    """
    source = MODULE.read_text(encoding="utf-8-sig")
    assert not re.search(r"""<div class=["']grid["'][^>]*>\s*<button""", source), (
        "в web/schedules.py кнопка печатается прямо в .grid - схлопнется снова")
    assert not re.search(r"""\bform\(request,""", source), (
        "web/schedules.py снова зовёт form(): его кнопка отправки ложится прямо в .grid")


async def test_buttons_keep_the_own_label_even_inside_a_grid_free_wrapper(groups):
    """Подпись кнопки - это текст плюс, возможно, значок, но не его исходник.

    Раньше ``esc()`` превращал ``<svg ...>`` в видимый текст, и кнопка «Импортировать
    с сайта» показывала сис-админу разметку значка в три строки. Значит подпись
    не должна содержать ни ``<``, ни ``>``, ни ``&``.
    """
    for path in ("/panel/schedules", CARD):
        for node in buttons(parse(body(groups, path))):
            label = node.label()
            assert "<" not in label, f"{path}: в подписи кнопки есть < - {label[:70]!r}"
            assert ">" not in label, f"{path}: в подписи кнопки есть > - {label[:70]!r}"
            assert "&" not in label, f"{path}: в подписи кнопки есть & - {label[:70]!r}"


async def test_import_button_holds_a_real_icon(groups):
    """У кнопки импорта настоящий значок ``<svg>``, а не его текст."""
    form_node = next(node for node in parse(body(groups)).find_all("form")
                     if node.attrs.get("action") == "/panel/schedules/import")
    assert form_node.find_all("svg"), "у кнопки импорта нет значка в разметке"
    assert "Импортировать с сайта" in " ".join(
        "".join(text for node in form_node.walk() for text in node.text).split())


# ── подпись кнопки ───────────────────────────────────────────────────────────
async def test_every_button_has_a_meaningful_label(groups):
    """Ни пустых кнопок, ни кнопок из одной иконки без подписи.

    Раньше удаление в строке было нарисовано одной иконкой: подписи не было ни
    текстом, ни ``aria-label``, и по кнопке нельзя было понять, что она делает.
    """
    for path in ("/panel/schedules", CARD):
        for node in buttons(parse(body(groups, path))):
            label = node.label()
            assert label.strip(), f"{path}: кнопка без подписи: {node.attrs}"
            assert "…" not in label and "..." not in label, (
                f"{path}: подпись кнопки обрезана многоточием - {label[:70]!r}")


async def test_page_has_all_its_buttons_named(groups):
    """Страница не потеряла кнопки: импорт, разбор, обновление, сохранение, удаление."""
    labels = [node.label() for node in buttons(parse(body(groups)))]
    for expected in ("Импортировать с сайта", "Сохранить", "Сохранить звонки",
                     "Обновить все расписания", "Разобрать", "Удалить"):
        assert any(expected in label for label in labels), (
            f"на странице нет кнопки {expected!r}: {labels}")
    back = [node.label() for node in buttons(parse(body(groups, CARD)))]
    assert any("К списку" in label for label in back), back


async def test_row_buttons_are_named(groups):
    """В строке таблицы обе кнопки подписаны, и удаление несёт значок."""
    row = next(node for node in parse(body(groups)).find_all("td")
               if any(child.attrs.get("action") == "/panel/schedules/delete"
                      for child in node.walk() if child.tag == "form"))
    found = [node for node in row.walk() if node.tag in BUTTON_TAGS]
    assert len(found) == 2, f"в строке ждали две кнопки, а нашлось {len(found)}"
    parse_button = next(node for node in found if "Разобрать" in node.label())
    delete_button = next(node for node in found if "btn-bad" in node.classes)
    assert delete_button.label().strip() == "Удалить", delete_button.attrs
    assert delete_button.find_all("svg"), "у кнопки удаления нет значка"
    assert parse_button.find_all("svg"), "у кнопки разбора нет значка"


# ── кнопки не липнут друг к другу ────────────────────────────────────────────
async def test_side_by_side_buttons_have_a_gap_container(groups):
    """Строчные соседи страницы должны лежать в контейнере темы с зазором.

    Именно про это жалобы: две формы ``class="inline"`` в одной ячейке вставали в
    одну строку, и зазор между ними задавал пробел между тегами, а не раскладка
    темы; то же было с двумя полями времени и кнопкой в форме правки. Блочные
    соседи такой проверки не проходят - столбиком они не липнут.
    """
    checked = 0
    for path in ("/panel/schedules", CARD):
        for parent in main_of(parse(body(groups, path))).walk():
            if parent.tag in NOT_A_ROW:
                continue
            flush = [child for child in parent.children if inline_row(child)]
            if len(flush) < 2:
                continue
            checked += 1
            assert parent.classes & GAP, (
                f"{path}: <{parent.tag} class={parent.attrs.get('class', '')!r}> держит "
                f"{len(flush)} строчных соседей ({[c.tag for c in flush]}) - зазор "
                "между ними не задан, они сольются бортами")
    assert checked, "не нашлось ни одного ряда - проверка ничего не проверяет"


async def test_row_actions_are_wrapped_into_one_gap_container(groups):
    """Кнопки строки лежат в одном контейнере с зазором, а не двумя подряд в ячейке.

    Самая узкая проверка на «слипшиеся»: в таблице зазор между кнопками строки
    обязан задавать контейнер темы. Если обёртку убрать, у ячейки окажется два
    ребёнка с кнопками - и проверка покраснеет.
    """
    root = parse(body(groups))
    cell = next(node for node in root.find_all("td")
                if any(child.attrs.get("action") == "/panel/schedules/delete"
                       for child in node.walk() if child.tag == "form"))
    holders = [child for child in cell.children if child.bears_button()]
    assert len(holders) == 1, (
        f"в ячейке с кнопками строки детей с кнопками {len(holders)}, а должен быть "
        "один контейнер с зазором")
    assert holders[0].classes & GAP, (
        f"контейнер кнопок строки {holders[0].attrs.get('class', '')!r} не даёт зазора")


async def test_row_label_may_not_break_by_letter(groups):
    """Ячейка с кнопками строки запрещает перенос подписи.

    В теме ``td`` рвёт слова в любом месте (``overflow-wrap:anywhere``), и в
    узкой ячейке на телефоне кнопка «Разобрать» рассыпалась по одной букве в
    строке - «кнопка идёт по буквам вниз». Запрет переноса в общих классах темы
    есть только у ``.num``, поэтому он и стоит на ячейке с кнопками.
    """
    cell = next(node for node in parse(body(groups)).find_all("td")
                if any(child.attrs.get("action") == "/panel/schedules/delete"
                       for child in node.walk() if child.tag == "form"))
    has_guard = any("num" in node.classes for node in (cell, *cell.walk()))
    assert has_guard, (
        "у ячейки с кнопками строки нет запрета переноса - на телефоне подпись "
        "разобьётся по буквам")


# ── страница не сломалась ────────────────────────────────────────────────────
async def test_parse_button_parses_one_group(groups):
    """Кнопка «Разобрать» разбирает PDF выбранной группы."""
    assert await repo.lessons_count(GROUP) > 0
    response = post_form(groups, "/panel/schedules/parse", {"group_code": GROUP})
    assert response.status_code == 303
    assert "разобрано" in body(groups)
    assert any("разобрано" in item["action"] for item in await repo.admin_log(20))


async def test_parse_all_button_refreshes_every_group(groups):
    """Кнопка «Обновить все расписания» обновляет обе группы."""
    assert post_form(groups, "/panel/schedules/parse_all").status_code == 303
    assert "Обновлено расписаний: 2" in body(groups)


async def test_group_is_selectable_and_its_time_editable(groups):
    """Выбор группы открывает карточку, а правка времени в ней работает."""
    card = body(groups, CARD)
    assert GROUP in card and "Сохранить время" in card
    lesson = (await repo.lessons_for_group(GROUP))[0]
    response = post_form(groups, "/panel/schedules/lesson-time", {
        "group_code": GROUP, "weekday": str(lesson["weekday"]),
        "lesson_num": lesson["lesson_num"], "start": "08:30", "end": "09:15"})
    assert response.status_code == 303
    assert (await repo.lessons_for_group(GROUP))[0]["start"] == "08:30"
    assert "Время пары сохранено" in body(groups, CARD)


async def test_save_and_delete_buttons_still_work(groups):
    """Ручное сохранение ссылки и удаление расписания работают."""
    assert post_form(groups, "/panel/schedules/save",
                     {"group_code": SAVED, "pdf_url": URL}).status_code == 303
    assert await repo.get_schedule(SAVED), "ссылка не сохранилась"
    assert hidden_code(body(groups)) == {GROUP, OTHER, SAVED}, "новая группа не попала в список"
    assert post_form(groups, "/panel/schedules/delete",
                     {"group_code": SAVED}).status_code == 303
    assert not await repo.get_schedule(SAVED), "расписание не удалилось"
    assert hidden_code(body(groups)) == {GROUP, OTHER}, "удалённая группа осталась в списке"


async def test_lesson_times_editor_is_on_the_page_and_works(groups):
    """Редактор звонков на месте: поля по номерам урока и кнопка сохранения."""
    page = body(groups)
    assert post_form(groups, "/panel/schedules/times",
                     {"t1a": "07:30", "t1b": "08:15"}).status_code == 303
    assert (await schedules.lesson_times())[0] == ("07:30", "08:15")
    assert "Звонки сохранены" in body(groups)
    fields = {node.attrs.get("name") for node in parse(page).find_all("input")}
    assert {"t1a", "t1b", "t2a", "t2b"} <= fields, sorted(fields)[:8]
    assert "Сохранить звонки" in page


async def test_import_button_posts_the_source(panel, monkeypatch, env):
    """Импорт с сайта по кнопке страницы доходит до импортера."""
    import webpanel

    async def fake_import(source, times=None):
        return {"files": 2, "groups": [GROUP], "lessons": 12, "problems": [],
                "total": 1, "urls": ["https://college.example/1.pdf"]}

    monkeypatch.setattr(webpanel.schedule_import, "import_sources", fake_import)
    page = body(panel)
    assert 'name="source"' in page and "collegelan.ru" in page
    assert post_form(panel, "/panel/schedules/import",
                     {"source": "https://college.example/"}).status_code == 303
    assert "Импортировано файлов: 2" in body(panel)


# ── CSRF на всех формах ──────────────────────────────────────────────────────
async def test_every_form_on_the_page_carries_a_csrf_token(groups):
    """Каждая POST-форма страницы содержит непустой csrf.

    Поиск в форме (а не у формы) - токен кладёт ``csrf(request)`` внутрь формы, и
    у действий это единственный способ его положить.
    """
    for path in ("/panel/schedules", CARD):
        forms = [node for node in parse(body(groups, path)).find_all("form")
                 if node.attrs.get("method", "get").lower() == "post"]
        assert forms, f"{path}: на странице нет ни одной POST-формы"
        for form_node in forms:
            tokens = [child for child in form_node.walk()
                      if child.tag == "input" and child.attrs.get("name") == "csrf"]
            assert tokens, f"{path}: у формы {form_node.attrs.get('action')} нет csrf"
            assert tokens[0].attrs.get("value"), (
                f"{path}: csrf пустой в форме {form_node.attrs.get('action')}")


async def test_posts_without_csrf_are_rejected(panel, env):
    """Без токена ни одна кнопка страницы не срабатывает: 403 и ничего не меняется."""
    await repo.upsert_schedule(GROUP, URL)
    was = (await schedules.lesson_times())[0]
    forms = (("/panel/schedules/parse", {"group_code": GROUP}),
             ("/panel/schedules/parse_all", {}),
             ("/panel/schedules/times", {"t1a": "07:30", "t1b": "08:15"}),
             ("/panel/schedules/import", {"source": "мусор"}),
             ("/panel/schedules/save", {"group_code": SAVED, "pdf_url": URL}),
             ("/panel/schedules/delete", {"group_code": GROUP}),
             ("/panel/schedules/lesson-time", {"group_code": GROUP, "weekday": "0",
                                               "lesson_num": "1", "start": "08:30",
                                               "end": "09:15"}))
    for path, data in forms:
        assert panel.post(path, data=data, follow_redirects=False).status_code == 403, path
    assert await repo.get_schedule(GROUP), "расписание удалилось без csrf"
    assert not await repo.get_schedule(SAVED), "ссылка сохранилась без csrf"
    assert (await schedules.lesson_times())[0] == was, "звонки сменились без csrf"


# ── страница без своих стилей ────────────────────────────────────────────────
async def test_page_brings_no_styles_of_its_own(groups):
    """Внутри страницы нет своих ``<style>``: оформление только классами темы."""
    for path in ("/panel/schedules", CARD):
        assert not main_of(parse(body(groups, path))).find_all("style"), (
            f"{path}: на странице свой <style> - оформление только классами темы")
