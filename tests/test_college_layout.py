"""Раздел «Колледж»: раскладка страницы и все её маршруты.

Жалоба сис-админа была такой: «в разделе колледж кнопка вкл выкл идёт в
колонну, то есть по буквам вниз». Причина не в подписи, а в раскладке: в
таблице частых вопросов колонка с длинным ответом забирала почти всю ширину,
последнюю колонку с кнопкой браузер сжимал до одного символа, а тема задаёт
ячейкам ``overflow-wrap:anywhere`` - и подпись «Вкл/выкл» рассыпалась по
буквам. Лечится разметкой: у ячейки действий стоит класс ``col-act``
(см. ``web.directory.ACT_COL``), который запрещает перенос.

Проверки здесь пять слоёв:

* кнопка «Вкл/выкл» стоит в ячейке, которая не сжимается, и подпись в ней
  целиком - по буквам она больше не идёт;
* страница разделена на блоки, и числа в счётчиках совпадают с базой;
* все маршруты раздела (``/college``, ``/faq*``, ``/groups*``) работают, и ни
  одна форма не потерялась по дороге;
* CSRF обязателен на каждом POST, а пустые состояния не выглядят поломкой.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

import college
import database as db
import repository as repo
import web.directory as directory
import webpanel
from conftest import csrf_of, login_panel, post_form
from handlers import faq
from utils import to_int


COLLEGE = "/panel/college"
GROUPS = "/panel/groups"
ON_LABEL, OFF_LABEL = "Выключить", "Включить"          # подпись кнопки, которая рассыпалась по буквам


# ── разметка страницы ────────────────────────────────────────────────────────
def headings(body: str) -> list[str]:
    """Заголовки блоков - одной строкой, без разметки и без иконок."""
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)).strip()
            for chunk in re.findall(r"<h2>(.*?)</h2>", body, re.S)]


def action_cells(body: str) -> list[str]:
    """Ячейки колонки действий - те, что помечены классом ACT_COL."""
    return re.findall(r"<td class=['\"]" + directory.ACT_COL + r"['\"]>(.*?)</td>", body, re.S)


BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)


def button_label(html: str) -> str:
    """Текст первой кнопки в куске разметки - без иконки."""
    _, inner = BUTTON.search(html).groups()
    return re.sub(r"<svg\b.*?</svg>", " ", inner, flags=re.S).strip()


def toggle_buttons(body: str) -> list[str]:
    """Классы кнопок «Выключить»/«Включить»: подпись должна быть в одну строку.

    Кнопки разбираем по одной: общая регулярка по документу перешагивала из
    кнопки в кнопку и находила «Вкл/выкл» даже там, где её нет.
    """
    # Подписей две - по состоянию вопроса, поэтому берём обе.
    return [re.search(r'class="([^"]*)"', attrs).group(1)
            for attrs, inner in BUTTON.findall(body)
            if re.sub(r"<svg\b.*?</svg>", " ", inner, flags=re.S).strip()
            in (ON_LABEL, OFF_LABEL)]


def ask_label(body: str) -> str:
    """Подпись кнопки, которая переключает ответы на вопросы целиком."""
    form = re.search(r'<form[^>]*action="/panel/faq/toggle".*?</form>', body, re.S)
    assert form, "на странице нет формы переключения ответов"
    return button_label(form.group(0))


def pill_number(body: str, name: str) -> int:
    """Число из плашки-счётчика: «вопросов: 42»."""
    match = re.search(r'<span class="pill[^"]*">' + re.escape(name) + r":\s*(\d+)</span>", body)
    assert match, f"на странице нет счётчика «{name}»"
    return int(match.group(1))


def kpi_numbers(body: str) -> dict:
    """Верхний ряд счётчиков: подпись → число."""
    block = re.search(r'<div class="kpi">(.*?)</div>\s*<div class="card"', body, re.S)
    assert block, "на странице нет верхнего ряда счётчиков"
    pairs = re.findall(r"<b>([^<]*)</b><span>([^<]*)</span>", block.group(1))
    assert pairs, "ряд счётчиков пуст"
    return {label: value for value, label in pairs}


def css_rule(css: str, name: str) -> str:
    """Объявления первого правила темы, в селекторе которого есть класс."""
    match = re.search(r"[^{}]*\." + re.escape(name) + r"[^{}]*\{([^{}]*)\}", css)
    return match.group(1) if match else ""


# ── маршруты раздела ─────────────────────────────────────────────────────────
def section_posts() -> list[str]:
    """POST-маршруты раздела: берёмся из роутера, чтобы новый не остался
    без проверки CSRF."""
    return sorted(route.path for route in webpanel.router.routes
                  if "POST" in getattr(route, "methods", set())
                  and getattr(route.endpoint, "__module__", "") == "web.directory")


POSTS = section_posts()


def concrete(path: str, faq_id: int = 1) -> str:
    """Путь маршрута с подставленным номером вопроса."""
    return re.sub(r"\{[^}]+\}", str(faq_id), path)


def page_of(path: str) -> str:
    """На какой из двух страниц раздела живёт действие."""
    return GROUPS if path.startswith("/panel/groups") else COLLEGE


@pytest.fixture
async def seeded(panel_client):
    """Панель сис-админа, вошедшая и залитая вопросами с сайта."""
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/faq/seed").status_code == 303
    return panel_client


async def add_question(question: str, active: int = 1) -> int:
    """Один вопрос в таблице faq - как будто сис-админ завёл его в панели."""
    await db.run("INSERT INTO faq(question, answer, keywords, active) VALUES(?,?,?,?)",
                 (question, f"Ответ на вопрос «{question}».", "ключевое слово", active))
    return to_int((await db.one("SELECT id FROM faq WHERE question=?", (question,)))["id"])


async def add_group(client, code: str = "ИС-30") -> None:
    """Одна группа в справочнике: её строка и есть место для кнопок."""
    assert post_form(client, "/panel/groups/add",
                     {"code": code, "title": code, "active": "1"}).status_code == 303


async def counts() -> tuple[int, int]:
    """Сколько вопросов в таблице faq и сколько из них включено."""
    row = await db.one("SELECT COUNT(*) total, COALESCE(SUM(active),0) enabled FROM faq")
    return to_int(row["total"]), to_int(row["enabled"])


# ── 1. кнопка «Вкл/выкл» больше не идёт по буквам ─────────────────────────────
async def test_toggle_button_sits_in_a_cell_that_is_not_squeezed(seeded):
    """Главное: у каждого вопроса ячейка действия с классом ACT_COL.

    Именно этот класс запрещает перенос. Без него браузер отдаёт всю ширину
    колонке с ответом, сжимает последнюю до одного символа, и подпись идёт по
    буквам - нажать такую кнопку нельзя.
    """
    items = await faq.active_items()
    cells = action_cells(seeded.get(COLLEGE).text)
    assert len(cells) == len(items), "не у каждого вопроса есть ячейка действий"
    for cell in cells:
        # подпись говорит, что произойдёт: «Выключить» у включённого
        assert "/toggle" in cell, cell[:120]
        assert ON_LABEL in cell or OFF_LABEL in cell, cell[:120]


async def test_state_of_the_question_is_shown(seeded):
    """Рядом с кнопкой видно состояние вопроса.

    В таблице только включённые (её строит ``faq.active_items()``), поэтому
    плашка должна стоять в каждой строке, а не в шапке.
    """
    rows = re.findall(r"<tr><td><b>.*?</tr>", seeded.get(COLLEGE).text, re.S)
    assert len(rows) == len(await faq.active_items())
    for row in rows:
        assert 'class="dot-state on"' in row, row[:120]
        assert "включён" in row


async def test_toggle_label_is_never_split_into_letters(seeded):
    """У каждой кнопки подпись целая, и она одна из двух понятных.

    Жалоба была: «кнопка вкл выкл идёт по буквам вниз». Причина: у ячейки не
    было минимальной ширины, а тема задаёт overflow-wrap:anywhere, при котором
    минимальная ширина текста равна одному символу.

    Обеих подписа�� на странице быть не может: у включённого вопроса кнопка
    «Выключить», у выключенного - «Включить». Поэтому проверяем каждую
    кнопку: подпись должна заканчиваться прямо на закрывающем теге (пробел
    перед словом допустим - идёт после иконки, разрыв внутри - нет) и должна
    быть одной из двух.
    """
    body = seeded.get(COLLEGE).text
    # отбираем по классу: адрес /toggle стоит на форме, а не на кнопке,
    # а класс col-act мы завели именно для кнопок этого столбца
    labels = [re.sub(r"<svg\b.*?</svg>", " ", inner, flags=re.S).strip()
              for attrs, inner in BUTTON.findall(body)
              if "col-act" in (re.search(r'class="([^"]*)"', attrs) or type("", (), {"group": lambda *a: ""})()).group(1)]
    assert labels, "на странице нет ни одной кнопки переключения вопроса"
    for label in labels:
        assert label in (ON_LABEL, OFF_LABEL), f"непонятная подпись кнопки: {label!r}"
        assert f"{label}</button>" in body, f"подпись {label} разорвана в разметке"




async def test_group_buttons_are_wrapped_too(panel_client):
    """Та же причина в справочнике групп: кнопки в обёртке ACT_COL.

    Обёртка своя у каждой формы: подпись кнопки не переносится, а сами формы
    переносятся друг за другом - на телефоне три кнопки в ряд не влезают.
    """
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/groups/add",
                     {"code": "ИС-30", "title": "ИС-30", "active": "1"}).status_code == 303
    body = panel_client.get(GROUPS).text
    assert re.search(r"<th class=['\"]" + directory.ACT_COL + r"['\"]>Действия</th>", body), \
        f"у колонки действий нет класса {directory.ACT_COL}"
    for action in ("/panel/groups/rename", "/panel/groups/toggle", "/panel/groups/delete"):
        assert re.search(r'<span class="' + directory.ACT_COL + r'">\s*<form method="post" '
                         r'action="' + re.escape(action) + r'"', body), action


def test_action_column_rule_is_written_down_for_the_theme():
    """Правило колонки действий задокументировано в разделе.

    panel_theme.py переделывает другой агент, поэтому здесь не правило темы, а
    его текст: раздел объявляет, что ему от темы нужно. Как только правило
    появится в STYLESHEET, проверка потребует, чтобы оно совпало.
    """
    assert directory.ACT_COL in directory.ACT_COL_CSS
    assert "white-space:nowrap" in directory.ACT_COL_CSS
    # Правил может быть два, и это не ошибка: класс висит и на ячейке таблицы,
    # и на инлайновой обёртке span, а ширина у span не действует - поэтому
    # width живёт в отдельном правиле для th/td. Поэтому собираем ВСЕ правила
    # класса и требуем: запрет переноса есть, минимальная ширина ячейки есть.
    style = re.sub(r"/\*.*?\*/", "", webpanel.STYLE, flags=re.S)
    rules = [body for head, body in re.findall(r"([^{}]+)\{([^}]*)\}", style)
             if directory.ACT_COL in head]
    assert rules, f"в теме нет ни одного правила для {directory.ACT_COL}"
    together = " ".join(rules)
    assert "white-space:nowrap" in together, \
        f"у {directory.ACT_COL} нет запрета переноса: {together}"
    assert "width:1%" in together, (
        f"колонке действий не задан минимальный размер - она отдаст ширину "
        f"колонке с текстом и кнопка снова развалится: {together}"
    )


@pytest.mark.parametrize("path", [COLLEGE, GROUPS])
async def test_page_carries_no_inline_style_block(seeded, path):
    """Раскладка живёт в теме: своего <style> раздел не тащит на страницу."""
    body = re.search(r"<main>(.*)</main>", seeded.get(path).text, re.S).group(1)
    assert "<style" not in body


# ── 2. страница разделена на блоки и в ней есть счётчики ──────────────────────
async def test_page_is_split_into_two_blocks(seeded):
    """Два блока с заголовками и ряд счётчиков сверху - простыни больше нет."""
    body = seeded.get(COLLEGE).text
    titles = headings(body)
    assert len(titles) == 2, titles
    assert "Контакты колледжа" in titles[0]
    assert "Частые вопросы" in titles[1]
    assert body.count('<div class="card"') == 2
    assert kpi_numbers(body)


async def test_counters_match_the_database(seeded):
    """Числа на странице - из базы, а не из воздуха."""
    body = seeded.get(COLLEGE).text
    total, enabled = await counts()
    assert pill_number(body, "вопросов") == total
    assert pill_number(body, "полей") == len(college.FIELDS)
    numbers = kpi_numbers(body)
    assert numbers["вопросов в базе"] == str(total)
    assert numbers["включено"] == str(enabled)
    assert numbers["выключено"] == str(total - enabled)
    assert numbers["полей в справочнике"] == str(len(college.FIELDS))


async def test_counter_follows_the_questions(panel_client):
    """Вопрос заведён - счётчик пошёл вверх, выключенный считается отдельно."""
    assert login_panel(panel_client)
    assert pill_number(panel_client.get(COLLEGE).text, "вопросов") == 0
    await add_question("Где столовая?", active=1)
    await add_question("Есть ли общежитие?", active=0)
    body = panel_client.get(COLLEGE).text
    numbers = kpi_numbers(body)
    assert numbers["вопросов в базе"] == "2"
    assert numbers["включено"] == "1"
    assert numbers["выключено"] == "1"
    # в таблице ВСЕ вопросы, включая выключенный: иначе его не вернуть из панели
    assert "Где столовая?" in body
    assert "Есть ли общежитие?" in body
    assert len(action_cells(body)) == 2
    # выключенный помечен и стоит после включённых
    assert body.index("Есть ли общежитие?") > body.index("Где столовая?")
    assert "выключен" in body


async def test_edited_contacts_are_counted(seeded):
    """Пометка «изменено» и счётчик изменённых полей - из одного замера."""
    assert pill_number(seeded.get(COLLEGE).text, "изменено") == 0
    data = {college.setting_key(key): "" for key in college.FIELDS}
    data[college.setting_key("адрес")] = "улица Ленина, 52"
    assert post_form(seeded, COLLEGE, data).status_code == 303
    body = seeded.get(COLLEGE).text
    assert pill_number(body, "изменено") == 1
    assert kpi_numbers(body)["изменено"] == "1"
    assert "улица Ленина, 52" in body


# ── 3. маршруты раздела работают как раньше ──────────────────────────────────
def test_section_declares_the_expected_actions():
    """Список маршрутов зафиксирован: новый POST автоматически попадёт
    в проверку CSRF ниже."""
    assert POSTS == ["/panel/college", "/panel/faq/seed", "/panel/faq/toggle",
                     "/panel/faq/{faq_id}/toggle", "/panel/groups/add",
                     "/panel/groups/delete", "/panel/groups/rename", "/panel/groups/toggle"]


@pytest.mark.parametrize("path", POSTS)
async def test_every_action_has_a_form_on_its_page(seeded, path):
    """Ни одно действие раздела не потерялось: у каждого POST есть форма."""
    if "{faq_id}" in path:
        path = concrete(path, to_int((await faq.active_items())[0]["id"]))
    page = page_of(path)
    if page == GROUPS and path != "/panel/groups/add":
        await add_group(seeded)          # кнопки группы живут в её строке
    assert f'action="{path}"' in seeded.get(page).text


async def test_contacts_are_saved_from_the_page(seeded):
    body = seeded.get(COLLEGE).text
    assert college.setting_key("адрес") in body
    data = {college.setting_key(key): "из панели" for key in college.FIELDS}
    assert post_form(seeded, COLLEGE, data).status_code == 303
    assert await college.get("адрес") == "из панели"
    assert await college.is_overridden("адрес") is True


async def test_empty_field_returns_to_site_data(seeded):
    """Пустое поле возвращает значение с сайта - как было до редизайна."""
    data = {college.setting_key(key): "из панели" for key in college.FIELDS}
    assert post_form(seeded, COLLEGE, data).status_code == 303
    data[college.setting_key("адрес")] = ""
    assert post_form(seeded, COLLEGE, data).status_code == 303
    assert await college.is_overridden("адрес") is False
    assert await college.get("адрес") == college.DEFAULTS["адрес"]


async def test_single_question_is_toggled(seeded):
    """Кнопка гасит вопрос, но вопрос остаётся в панели - иначе его не вернуть.

    Раньше выключенный вопрос исчезал из таблицы, и включить его обратно можно
    было только правкой базы через раздел «Данные». Теперь он виден, помечен
    и включается той же кнопкой.
    """
    first = (await faq.active_items())[0]
    assert post_form(seeded, f"/panel/faq/{first['id']}/toggle").status_code == 303
    left = [row["question"] for row in await faq.active_items()]
    assert first["question"] not in left, "вопрос должен перестать работать в боте"
    body = seeded.get(COLLEGE).text
    # но в панели он остаётся - иначе это тупик
    assert first["question"] in body, "выключенный вопрос исчез из панели"
    assert "выключен" in body
    assert OFF_LABEL in body, "у выключенного вопроса должна быть кнопка «Включить»"
    # вопрос в базе остался, но выключен - считается отдельно
    assert pill_number(body, "вопросов") == len(left) + 1
    assert kpi_numbers(body)["выключено"] == "1"
    # и кнопка включения на месте: клик по ней возвращает вопрос в работу
    assert len(action_cells(body)) == len(left) + 1
    assert post_form(seeded, f"/panel/faq/{first['id']}/toggle").status_code == 303
    assert first["question"] in [row["question"] for row in await faq.active_items()]


async def test_bot_still_sees_only_active_questions(seeded):
    """Бот показывает студентам только работающие вопросы.

    Панель видит все, чтобы можно было вернуть выключенный, но бот по-прежнему
    не должен отдавать студенту выключенный вопрос.
    """
    first = (await faq.active_items())[0]
    assert post_form(seeded, f"/panel/faq/{first['id']}/toggle").status_code == 303
    assert first["question"] in seeded.get(COLLEGE).text, "в панели вопрос должен остаться"
    assert first["question"] not in [row["question"] for row in await faq.active_items()]


async def test_unknown_question_is_answered_honestly(seeded):
    """Вопроса нет - понятное сообщение, а не ошибка."""
    assert post_form(seeded, "/panel/faq/999999/toggle").status_code == 303
    assert "Такого вопроса нет" in seeded.get(COLLEGE).text


async def test_mass_toggle_of_all_questions(seeded):
    """Кнопка «Выключить» гасит ответы целиком, «Включить» возвращает."""
    assert post_form(seeded, "/panel/faq/toggle", {"enabled": "0"}).status_code == 303
    assert await faq.ask_enabled() is False
    body = seeded.get(COLLEGE).text
    assert '<input type="hidden" name="enabled" value="1">' in body
    assert ask_label(body) == "Включить"
    assert '<span class="pill pill-off">ответы: выключены</span>' in body
    assert post_form(seeded, "/panel/faq/toggle", {"enabled": "1"}).status_code == 303
    assert await faq.ask_enabled() is True
    body = seeded.get(COLLEGE).text
    assert ask_label(body) == "Выключить"
    assert '<input type="hidden" name="enabled" value="0">' in body
    assert '<span class="pill pill-on">ответы: включены</span>' in body


async def test_seed_adds_questions_and_keeps_edits(panel_client):
    """Заливка с сайта наполняет таблицу и не трогает правки сис-админа."""
    assert login_panel(panel_client)
    await add_question("Свой вопрос сис-админа")
    assert post_form(panel_client, "/panel/faq/seed").status_code == 303
    body = panel_client.get(COLLEGE).text
    assert college.DEFAULT_FAQ[0]["question"] in body
    assert "Свой вопрос сис-админа" in body
    assert pill_number(body, "вопросов") == len(college.DEFAULT_FAQ) + 1
    # повторная заливка ничего не добавляет и не затирает
    assert post_form(panel_client, "/panel/faq/seed").status_code == 303
    body = panel_client.get(COLLEGE).text
    assert pill_number(body, "вопросов") == len(college.DEFAULT_FAQ) + 1
    assert "Свой вопрос сис-админа" in body


async def test_groups_registry_round_trip(panel_client):
    """Добавление, переименование, скрытие и удаление группы - как раньше."""
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/groups/add",
                     {"code": "ИС-30", "title": "Информационные системы", "active": "1"}
                     ).status_code == 303
    body = panel_client.get(GROUPS).text
    assert "ИС-30" in body
    assert pill_number(body, "групп") == 1
    assert post_form(panel_client, "/panel/groups/rename",
                     {"old": "ИС-30", "code": "ИС-31"}).status_code == 303
    assert await repo.get_group("ИС-31") is not None
    assert post_form(panel_client, "/panel/groups/toggle", {"code": "ИС-31"}).status_code == 303
    assert (await repo.get_group("ИС-31"))["active"] == 0
    body = panel_client.get(GROUPS).text
    assert "скрыта" in body
    assert pill_number(body, "активных") == 0
    assert post_form(panel_client, "/panel/groups/delete", {"code": "ИС-31"}).status_code == 303
    assert await repo.get_group("ИС-31") is None


async def test_group_code_is_still_checked(panel_client):
    """Кривой код группы по-прежнему не проходит, и код нормализуется."""
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/groups/add", {"code": "и с пробелами!"}).status_code == 303
    assert "буквы, цифры" in panel_client.get(GROUPS).text
    assert post_form(panel_client, "/panel/groups/add",
                     {"code": "ис-30", "title": "ИС-30", "active": "1"}).status_code == 303
    assert await repo.get_group("ИС-30") is not None


# ── 4. пустые состояния ──────────────────────────────────────────────────────
async def test_no_questions_is_an_explicit_empty_state(panel_client):
    """Нет вопросов - рамка темы, а не таблица с одной строкой."""
    assert login_panel(panel_client)
    body = panel_client.get(COLLEGE).text
    assert 'class="empty"' in body
    assert "Вопросов пока нет" in body
    assert "Вопрос и ключевые слова" not in body
    assert pill_number(body, "вопросов") == 0
    assert action_cells(body) == []


async def test_no_contacts_is_an_explicit_empty_state(panel_client, monkeypatch):
    """Пустой справочник - рамка «полей пока нет», и сохранять нечего."""
    async def nothing():
        return {}

    monkeypatch.setattr(college, "contacts", nothing)
    assert login_panel(panel_client)
    body = panel_client.get(COLLEGE).text
    assert "Полей пока нет" in body
    assert 'class="empty"' in body
    assert f'action="{COLLEGE}"' not in body          # формы сохранения нет
    assert pill_number(body, "полей") == 0


async def test_empty_groups_registry(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get(GROUPS).text
    assert "Справочник пуст" in body
    assert 'class="empty"' in body
    assert pill_number(body, "групп") == 0
    assert f'action="{GROUPS}/add"' in body           # добавить группу по-прежнему можно


# ── 5. CSRF на всех POST раздела ─────────────────────────────────────────────
@pytest.mark.parametrize("path", POSTS)
async def test_every_post_of_the_section_wants_csrf(panel_client, path):
    """Без токена формы ни одно действие раздела не проходит."""
    assert login_panel(panel_client)
    target = concrete(path)
    assert panel_client.post(target, data={}, follow_redirects=False).status_code == 403
    # с токеном - обычный ответ раздела (редирект), а не отказ
    assert panel_client.post(target, data={"csrf": csrf_of(panel_client)},
                             follow_redirects=False).status_code == 303
