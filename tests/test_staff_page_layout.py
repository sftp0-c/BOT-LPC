"""Вёрстка страницы «Сотрудники»: ряды кнопок, подписи полей и работа страницы.

Проверки страницы, а не строк исходника. Жалобы «кнопки наезжают друг на друга»,
«подпись поля английская» и «подсказку обрезает» в HTML не видны: их видно
только по разметке - где какой контейнер, какая у него подпись и не спрятан ли
ряд в боксе с прокруткой. Поэтому проверки смотрят на структуру:

* ряд подсказок должностей и ряд действий стоят в РАЗНЫХ блоках, и у каждого
  класса в теме есть зазор (иначе кнопки снова сойдутся бортами);
* ряд подсказок не спрятан в бокс с прокруткой и не обрезан по высоте, а в
  нём лежит кнопка на каждую должность справочника;
* ни одно поле не подписано именем колонки: и те семь, что были, и любое
  новое, добавленное позже (список лежит здесь, рядом с проверкой, и правило
  «подпись != имя поля» ловит новое поле без правки теста);
* подсказка в поле не обрезается: у поля есть разумный минимум ширины.

И сверху - что страница не сломалась: выдача и снятие прав, правка в строке,
добавление сотрудника, переход «Всё о сотруднике» и CSRF на каждом POST.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
from conftest import add_staff, login_panel, post_form
from panel_theme import STYLESHEET
from utils import POSITION_CODES, POSITION_TITLES, POSITIONS_BTN

STAFF = "200"         # сотрудник, у которого правят строку
SECOND = "201"        # второй: страница обязана показывать оба ряда
NEW_ONE = "300"       # кого добавляют через форму
NAME = "Петрова Анна"

# Имена колонок, которыми подписывались поля страницы. Проверка ловит и новые
# такие подписи: правило «подпись поля не равна его имени» действует для
# любого поля, а этот список нужен, чтобы поймать английский след и в других
# местах страницы (карточка сотрудника, отпуск).
ENGLISH_LABELS = ("full_name", "department", "role", "office",
                  "ticket_category", "can_broadcast", "see_all_tickets")

# Подпись поля не обрезается, если под ней есть хотя бы столько пикселей.
HINT_MIN_WIDTH = 200

ACTION_LABELS = ("Сделать сис-админом", "Удалить", "Всё о сотруднике")


# ── разбор разметки ───────────────────────────────────────────────────────────
def labels_with_fields(body: str) -> list[tuple[str, str]]:
    """Пары «подпись — имя поля» со всей страницы, в порядке появления.

    Разметка поля везде одна: ``<label>Подпись</label><input name="поле">``.
    Берём всё, что между подписью и полем, - тогда проверка не зависит от того,
    чем именно поле нарисовано: input(), select() или руками в модуле.
    """
    pattern = (r"<label[^>]*>(?P<label>[^<]*)</label>\s*"
               r"<(?:input|select|textarea)\b[^>]*\bname=[\"'](?P<name>[^\"']+)[\"']")
    return [(match["label"].strip(), match["name"])
            for match in re.finditer(pattern, body)]


def labels(body: str) -> list[str]:
    """Все подписи полей страницы."""
    return re.findall(r"<label[^>]*>([^<]*)</label>", body)


def blocks_of(body: str, css_class: str) -> list[str]:
    """Внутренность каждого ``<div class="...css_class...">…</div>``.

    Считаем вложенность вручную: регексп с ``.*?`` обрезал бы блок на первом
    же закрывающем ``</div>`` внутри него, и проверка «второй ряд не внутри
    первого» стала бы проверкой обрезка.
    """
    found: list[str] = []
    for match in re.finditer(rf'<div[^>]*class="[^"]*\b{re.escape(css_class)}\b[^"]*"[^>]*>', body):
        depth, index = 1, match.end()
        while depth and index < len(body):
            nxt_open = body.find("<div", index)
            nxt_close = body.find("</div>", index)
            if nxt_close < 0:
                break
            if 0 <= nxt_open < nxt_close:
                depth, index = depth + 1, nxt_open + 4
            else:
                depth, index = depth - 1, nxt_close + 6
        if depth == 0:
            found.append(body[match.end():index - 6])
    return found


def post_forms(body: str) -> list[str]:
    """Внутренность каждой POST-формы страницы.

    Формы в HTML не вкладываются друг в друга, поэтому закрывающий тег
    ищется простым ``str.find`` - вложенности тут быть не может.
    """
    found = []
    for match in re.finditer(r'<form\b[^>]*>', body):
        if not re.search(r"method=['\"]post['\"]", match.group(0)):
            continue
        close = body.find("</form>", match.end())
        if close > 0:
            found.append(body[match.end():close])
    return found


def theme_gap_classes(css: str) -> set[str]:
    """Классы, которым тема задаёт ряд с зазором (``display:flex`` + ``gap``)."""
    out: set[str] = set()
    for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "display:flex" not in decls or "gap:" not in decls:
            continue
        for part in selector.split(","):
            token = part.strip()
            if re.fullmatch(r"\.[A-Za-z][\w-]*", token):
                out.add(token[1:])
    return out


def class_list(tag_html: str) -> list[str]:
    match = re.search(r'class="([^"]*)"', tag_html)
    return match.group(1).split() if match else []


def open_tags(body: str, css_class: str) -> list[str]:
    return re.findall(rf'<div[^>]*class="[^"]*\b{re.escape(css_class)}\b[^"]*"[^>]*>', body)


def has_link(body: str, path: str) -> bool:
    """Ссылка есть независимо от кавычек: в разметке местами одинарные."""
    return re.search("href=[\"']" + re.escape(path) + "[\"']", body) is not None


def min_width_before(body: str, needle: str) -> int:
    """Минимальная ширина блока, в котором лежит поле с подсказкой."""
    worst = 0
    for match in re.finditer(re.escape(needle), body):
        window = body[max(0, match.start() - 220):match.start()]
        found = re.findall(r"min-width:(\d+)px", window)
        if found:
            worst = max(worst, int(found[-1]))
    return worst


# ── страница с двумя сотрудниками ─────────────────────────────────────────────
async def staff_page(panel_client) -> str:
    """Страница «Сотрудники» с двумя сотрудниками, у одного заполнена должность."""
    await add_staff(STAFF, NAME, position="Секретарь", office="204")
    await add_staff(SECOND, "Сидорова Мария")
    assert login_panel(panel_client)
    return panel_client.get("/panel/staff").text


# ── два ряда, а не один ───────────────────────────────────────────────────────
async def test_hints_and_actions_are_two_separate_rows(panel_client):
    """Подсказки должностей и действия - разные блоки, действия ниже.

    Раньше это был один ряд: бокс подсказок обрезал третью строку, а кнопки
    действий вставали ровно на неё. Проверка ловит и обратное - если действия
    снова положат внутрь блока подсказок.
    """
    body = await staff_page(panel_client)
    hints = blocks_of(body, "staff-hints")
    actions = blocks_of(body, "staff-actions")
    assert len(hints) == 2 and len(actions) == 2, "по одному ряду на каждого сотрудника"

    for block in hints:
        assert block.count('name="position_pick"') == len(POSITION_CODES)
        for text in ACTION_LABELS:
            assert text not in block, f"действие «{text}» попало в ряд подсказок"
    for block in actions:
        assert 'name="position_pick"' not in block
        for text in ACTION_LABELS:
            assert text in block, f"действие «{text}» потерялось из ряда действий"

    # блоки стоят в порядке «сначала подсказки, потом действия» и не вложены
    order = [(match.start(), match.group(0)) for match
             in re.finditer(r'<div[^>]*class="[^"]*\bstaff-(?:hints|actions)\b[^"]*"[^>]*>', body)]
    assert [("hints" if "hints" in tag else "actions") for _pos, tag in order] == [
        "hints", "actions", "hints", "actions"]


async def test_both_rows_get_a_gap_from_the_theme(panel_client):
    """У обоих рядов есть класс, которому тема задаёт зазор.

    Проверяется не по имени класса, а по самой теме: класс подходит, только
    если в ``STYLESHEET`` есть правило с ``display:flex`` и ``gap``. Список
    таких классов — один на тест, иначе проверка была бы «класс называется
    так-то».
    """
    body = await staff_page(panel_client)
    gap_classes = theme_gap_classes(STYLESHEET)
    for css_class in ("staff-hints", "staff-actions"):
        tags = open_tags(body, css_class)
        assert tags, css_class
        for tag in tags:
            own = [name for name in class_list(tag) if name != css_class]
            assert any(name in gap_classes for name in own), (
                f"{tag}: ни один класс {own} не задан в теме как ряд с зазором")


async def test_hints_row_is_not_a_scroll_box(panel_client):
    """Ряд подсказок не обрезан по высоте: кнопка на каждую должность видна.

    Именно ``max-height`` с ``overflow:auto`` прятал восемь кнопок из
    восемнадцати, и именно из-за этого кнопки действий попадали на них.
    """
    body = await staff_page(panel_client)
    for tag in open_tags(body, "staff-hints"):
        assert "max-height" not in tag, f"ряд подсказок обрезан по высоте: {tag}"
        assert "overflow" not in tag, f"ряд подсказок ушёл в прокрутку: {tag}"
    for block in blocks_of(body, "staff-hints"):
        assert "overflow" not in block and "max-height" not in block


async def test_every_position_of_the_registry_has_a_button(panel_client):
    """Подсказка есть у каждой должности из ``utils.POSITIONS``, и она своя.

    Проверяется и полным списком, и по одной кнопке: пропущенная должность -
    это сотрудник, которому нельзя назначить должность.
    """
    body = await staff_page(panel_client)
    expected = len(POSITION_CODES)
    assert expected >= 15, "справочник должностей подозрительно короткий"
    for block in blocks_of(body, "staff-hints"):
        assert block.count('name="position_pick"') == expected
        for code, title in POSITION_TITLES.items():
            assert f'name="position_pick" value="{title}"' in block, code
            assert POSITIONS_BTN[code] in block, code
    # кнопки не спрятаны атрибутом и не помечены как отключённые
    for button in re.findall(r"<button[^>]*name=\"position_pick\"[^>]*>", body):
        assert "hidden" not in button and "disabled" not in button, button


# ── подписи полей ─────────────────────────────────────────────────────────────
async def test_no_field_is_labelled_with_its_column_name(panel_client):
    """Подпись поля - по-русски, а не именем колонки.

    Правило общее: подпись не равна имени поля. Список ENGLISH_LABELS рядом
    нужен для явной проверки, а новое поле с такой подписью поймает общее
    правило, даже если его имени в списке ещё нет.
    """
    body = await staff_page(panel_client)
    pairs = labels_with_fields(body)
    assert pairs, "на странице не нашлось ни одного подписанного поля"
    english = [(lab, name) for lab, name in pairs if lab == name]
    assert not english, f"поля подписаны именем колонки: {english}"
    for lab in labels(body):
        assert lab not in ENGLISH_LABELS, f"английская подпись поля: {lab!r}"


async def test_fields_keep_their_russian_meaning(panel_client):
    """Подписи на месте и по-русски: человек должен понимать, что заполняет."""
    body = await staff_page(panel_client)
    pairs = {name: label for label, name in labels_with_fields(body)}
    for name, expected in (("full_name", "ФИО"), ("department", "Отдел"),
                           ("office", "Кабинет"), ("role", "Тип должности"),
                           ("ticket_category", "Раздел обращений"),
                           ("can_broadcast", "Рассылка"), ("position", "Должность")):
        assert pairs.get(name) == expected, f"{name}: {pairs.get(name)!r} вместо {expected!r}"


async def test_placeholder_is_not_cut_by_the_field(panel_client):
    """Подсказка «например, Преподаватель информатики» помещается в поле.

    Без минимума ширины от неё остаётся «например, Преподават», и человек не
    понимает, о чём поле.
    """
    body = await staff_page(panel_client)
    widest = min_width_before(body, 'placeholder="например, Преподаватель информатики"')
    assert widest >= HINT_MIN_WIDTH, (
        f"у поля должности минимум {widest}px, подсказка обрежется до «Преподават»")


# ── страница не сломалась ─────────────────────────────────────────────────────
async def test_row_edit_saves_position_and_office(panel_client):
    """Правка в строке на месте и по-прежнему сохраняет."""
    body = await staff_page(panel_client)
    row = next(part for part in body.split("<tr") if NAME in part)
    assert f'action="/panel/staff/{STAFF}/quick"' in row
    for field in ("position", "office", "ticket_category", "csrf"):
        assert f'name="{field}"' in row, field
    assert post_form(panel_client, f"/panel/staff/{STAFF}/quick",
                     {"position": "Учебная часть", "office": "101",
                      "ticket_category": "all"}).status_code == 303
    admin = await repo.get_admin(STAFF)
    assert (admin["position"], admin["office"]) == ("Учебная часть", "101")


async def test_position_pick_button_saves_and_delete_works(panel_client):
    """Кнопка подсказки и удаление сотрудника работают как раньше."""
    await staff_page(panel_client)
    assert post_form(panel_client, f"/panel/staff/{STAFF}/position",
                     {"position_pick": POSITION_TITLES["secretary"]}).status_code == 303
    assert (await repo.get_admin(STAFF))["position"] == "Секретарь"
    assert post_form(panel_client, f"/panel/staff/{STAFF}/delete").status_code == 303
    assert await repo.get_admin(STAFF) is None
    rest = panel_client.get("/panel/staff").text
    assert len(blocks_of(rest, "staff-actions")) == 1, "у второго сотрудника ряд действий пропал"


async def test_promote_and_revoke_sysadmin_still_work(panel_client):
    """Выдача и снятие прав сис-админа - из строки и массово."""
    await staff_page(panel_client)
    body = panel_client.get("/panel/staff").text
    assert f"/panel/staff/promote/{STAFF}" in body
    assert f"/panel/staff/sysadmin/{STAFF}/revoke" not in body, "сотрудник ещё не сис-админ"
    assert post_form(panel_client, f"/panel/staff/promote/{STAFF}").status_code == 303
    assert (await repo.get_admin(STAFF))["role_type"] == "sysadmin"
    assert post_form(panel_client, "/panel/staff/sysadmin/revoke",
                     {"user_id": STAFF}).status_code == 303
    assert await repo.get_admin(STAFF) is None
    assert post_form(panel_client, "/panel/staff/sysadmin",
                     {"user_id": NEW_ONE}).status_code == 303
    assert (await repo.get_admin(NEW_ONE))["role_type"] == "sysadmin"


async def test_add_staff_and_dossier_link_still_work(panel_client):
    """Добавление сотрудника и переход «Всё о сотруднике» на месте."""
    body = await staff_page(panel_client)
    assert has_link(body, f"/panel/people/{STAFF}/dossier")
    assert 'action="/panel/staff/add"' in body
    assert post_form(panel_client, "/panel/staff/add", {
        "user_id": NEW_ONE, "full_name": "Соколов Иван", "position": "Секретарь",
        "department": "учебная часть", "office": "101", "role": "",
        "ticket_category": "feedback", "can_broadcast": "0"}).status_code == 303
    admin = await repo.get_admin(NEW_ONE)
    assert (admin["full_name"], admin["position"], admin["office"]) == (
        "Соколов Иван", "Секретарь", "101")
    assert "Соколов Иван" in panel_client.get("/panel/staff").text


async def test_card_page_also_has_russian_labels_and_full_hints(panel_client):
    """Карточка сотрудника - тот же раздел: подсказки все и подписи русские."""
    await staff_page(panel_client)
    body = panel_client.get(f"/panel/staff/{STAFF}").text
    assert body.count('name="position_pick"') == len(POSITION_CODES)
    for title in POSITION_TITLES.values():
        assert f'value="{title}"' in body, title
    for lab in labels(body):
        assert lab not in ENGLISH_LABELS, lab


# ── CSRF ──────────────────────────────────────────────────────────────────────
async def test_every_post_form_on_the_page_carries_a_token(panel_client):
    """У каждой POST-формы страницы есть токен - в том числе у кнопок действий.

    Проверка идёт по разметке, а не по адресам: если токен забыли в разметке,
    но сервер его требует, кнопка просто не сработает - и это видно только
    здесь. Обратное (токен есть, сервер не требует) ловит проверка адресов.
    """
    body = await staff_page(panel_client)
    forms = post_forms(body)
    assert len(forms) >= 8, f"на странице всего форм: {len(forms)}"
    for chunk in forms:
        assert 'name="csrf"' in chunk, chunk[:160]


async def test_card_page_forms_carry_a_token(panel_client):
    """Карточка сотрудника: формы там тоже с токеном."""
    await staff_page(panel_client)
    forms = post_forms(panel_client.get(f"/panel/staff/{STAFF}").text)
    assert forms, "в карточке нет ни одной POST-формы"
    for chunk in forms:
        assert 'name="csrf"' in chunk, chunk[:160]


async def test_every_post_of_the_section_requires_csrf(panel_client):
    """Без токена формы не проходит ни одна кнопка раздела."""
    await staff_page(panel_client)
    forms = (f"/panel/staff/{STAFF}",
             f"/panel/staff/{STAFF}/quick",
             f"/panel/staff/{STAFF}/position",
             f"/panel/staff/{STAFF}/delete",
             f"/panel/staff/{STAFF}/vacation",
             f"/panel/staff/promote/{STAFF}",
             "/panel/staff/add",
             "/panel/staff/sysadmin",
             "/panel/staff/sysadmin/revoke",
             f"/panel/staff/sysadmin/{STAFF}/revoke",
             f"/panel/staff/sysadmin/{STAFF}/restore")
    for path in forms:
        response = panel_client.post(path, data={"user_id": NEW_ONE, "position": "Взлом"},
                                     follow_redirects=False)
        assert response.status_code == 403, path
    assert (await repo.get_admin(STAFF))["position"] == "Секретарь"
    assert await repo.get_admin(NEW_ONE) is None
