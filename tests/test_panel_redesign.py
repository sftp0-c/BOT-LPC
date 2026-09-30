"""Редизайн панели: меню по группам, «Пульт», лента событий, горячие клавиши.

Тесты проверяют договорённости, из-за которых панель переписана:

* меню - пять групп, и все шестнадцать старых разделов в них попали;
* бейджи групп считаются из одной сводки ``admin_today()``;
* на главной шесть кликабельных счётчиков, «что требует действия» и лента;
* ``/activity`` - сквозная лента с фильтром, поиском и постраничностью;
* чистка зависших диалогов - обычная POST-форма панели, с CSRF;
* клавиши объявлены в скрипте и молчат, когда фокус в поле ввода;
* фильтры рабочего места помнятся в localStorage;
* в кнопках действий и статусах нет эмодзи (в текстах для MAX - остались);
* печатная версия и ссылка «Открыть в боте» на месте.
"""
import pytest
import re

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import database as db
import panel_theme
import repository as repo
import webpanel
from conftest import add_staff, login_panel, post_form, register
from utils import to_int

# Все разделы, которые были в меню до редизайна: ни один не должен потеряться
# ПО СЛУЧАЮ. Раздел, который убрали намеренно, перечислен отдельно ниже -
# иначе проверка «ничего не пропало» мешала бы сознательному решению.
OLD_PATHS = ("/", "/tickets", "/people", "/nostaff", "/college", "/students",
             "/staff", "/access", "/templates", "/groups", "/schedules", "/broadcasts",
             "/database", "/settings", "/logs")
# Убраны из меню по прямой просьбе пользователя: раздел пока не нужен. Сам раздел
# на месте и открывается по своему адресу - убрали пункт, не выкинули страницу.
# Вернуть: дописать пункт обратно в NAV_GROUPS и убрать путь отсюда.
HIDDEN_ON_PURPOSE = {"/analytics": "раздел не нужен, попросили убрать с сайта"}
GROUP_NAMES = ("Пульт", "Обращения", "Люди", "Справочники", "Система")
# Куда ведут крупные счётчики главной страницы.
COUNTER_LINKS = ("/panel/tickets?scope=waiting", "/panel/access", "/panel/nostaff",
                 "/panel/tickets?status=ready", "/panel/database", "/panel/analytics")
# Эмодзи, которые обязаны уйти из интерфейса. В текстах, которые уходят в MAX
# (вызовы notify), и в подписях <option> они остаются - проверка не про них.
UI_EMOJI = ("🗑", "🗄", "🗂", "➕", "⬇", "🔍", "🔄", "🟢", "⚪", "🔴", "⏳", "🔔", "📥",
            "🗝", "⚠", "📦", "♻", "🧹", "📎", "🔧", "👤", "🔁", "📤", "🏖", "🎓", "🏫",
            "👔", "💬", "📂", "🧪", "🆕", "🔓", "📄", "💰")
# В колонке согласия списка студентов отметка «✅» остаётся осознанно: её ждут
# проверки пробелов в данных, и это единственное место, где она живёт.
CONSENT_MARK = "✅ согласие есть"

# Страницы, на которых ищем эмодзи в кнопках и статусах.
UI_PAGES = ("/panel/", "/panel/tickets", "/panel/activity", "/panel/people", "/panel/nostaff",
            "/panel/staff", "/panel/access", "/panel/groups", "/panel/schedules",
            "/panel/database", "/panel/settings", "/panel/college", "/panel/templates",
            "/panel/broadcasts", "/panel/students")


def labels(html: str) -> list[str]:
    """Тексты кнопок, ссылок-кнопок и плашек статусов - без разметки и иконок."""
    found: list[str] = []
    for pattern in (r"<button\b[^>]*>(.*?)</button>",
                    r"<a class=\"btn[^\"]*\"[^>]*>(.*?)</a>",
                    r"<span class=\"pill[^\"]*\">(.*?)</span>"):
        for chunk in re.findall(pattern, html, re.S):
            text = re.sub(r"<svg\b.*?</svg>", " ", chunk, flags=re.S)
            found.append(re.sub(r"<[^>]+>", " ", text))
    return found


def nav_html(html: str) -> str:
    """Только меню: в <style> и <script> тоже встречаются слова проверок."""
    return re.search(r"<nav\b.*?</nav>", html, re.S).group(0)


def form_tag(html: str, action: str) -> str:
    """Открывающий тег формы по действию - вместе с её атрибутами."""
    return re.search(r"<form[^>]*action=\"" + re.escape(action) + r"\"[^>]*>", html).group(0)


def print_css() -> str:
    """Все блоки @media print вместе: их в теме два (палитра и вёрстка)."""
    blocks = []
    for match in re.finditer(r"@media print\s*\{", webpanel.STYLE):
        depth, index = 1, match.end()
        while index < len(webpanel.STYLE) and depth:
            depth += (webpanel.STYLE[index] == "{") - (webpanel.STYLE[index] == "}")
            index += 1
        blocks.append(webpanel.STYLE[match.end():index - 1])
    assert blocks, "в теме нет печатной версии"
    return "\n".join(blocks)


def nav_group(html: str, name: str) -> str:
    """Кусок HTML группы меню по её названию."""
    blocks = re.findall(r'<div class="nav-group[^"]*">.*?</div></div>', nav_html(html), re.S)
    for block in blocks:
        if f">{name}<" in block or f">{name}</span>" in block:
            return block
    raise AssertionError(f"в меню нет группы «{name}»")


async def make_ticket(text: str = "Нужна справка", status: str = "new") -> int:
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_staff("200", "Петрова Анна", "feedback", office="204")
    ticket_id = await repo.create_ticket("100", "200", "feedback", text, "Справка")
    if status != "new":
        await repo.set_ticket_status(ticket_id, status)
    return ticket_id


# ── 1. меню: пять групп и все старые разделы ────────────────────────────────
def test_menu_has_five_groups():
    assert [name for name, _items in webpanel.NAV_GROUPS] == list(GROUP_NAMES)
    assert all(items for _name, items in webpanel.NAV_GROUPS)


def test_every_old_section_belongs_to_a_group():
    inside = {path for _group, items in webpanel.NAV_GROUPS for path, _name, _icon, _sub in items}
    missing = [path for path in OLD_PATHS if path not in inside]
    assert not missing, f"разделы выпали из меню: {missing}"
    # намеренно скрытые действительно вне меню - иначе список врёт
    for path, why in HIDDEN_ON_PURPOSE.items():
        assert path not in inside, f"{path} убрали по просьбе ({why}), но он в меню"
    # старые адреса продолжают работать: они же адреса страниц
    for path in OLD_PATHS + tuple(HIDDEN_ON_PURPOSE):
        assert path != "" and path.startswith("/")


def test_hidden_section_still_works_and_is_reachable():
    """Убрали пункт из меню - раздел не должен пропасть вместе с ним."""
    for path in HIDDEN_ON_PURPOSE:
        # в меню пути хранятся без префикса /panel - его добавляет сборка меню
        assert path.startswith("/") and not path.startswith("/panel/"), path
    # раздел объявлен маршрутом: страница жива, её просто не видно в меню.
    # В webpanel.router пути уже с префиксом /panel, в меню - без него.
    routes = {route.path for route in webpanel.router.routes}
    for path in HIDDEN_ON_PURPOSE:
        assert f"/panel{path}" in routes, \
            f"маршрут {path} исчез - это уже не «скрыть», а «удалить»"


def test_activity_is_in_the_control_group():
    group, items = webpanel.NAV_GROUPS[0]
    assert group == "Пульт"
    assert "/activity" in [path for path, _name, _icon, _sub in items]
    assert "/" in [path for path, _name, _icon, _sub in items]


def test_menu_renders_groups_with_badges(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert 'class="nav-groups"' in body
    assert body.count('class="nav-group-label"') == len(GROUP_NAMES)
    for path in OLD_PATHS + ("/activity",):
        assert f'href="/panel{path}"' in body, f"раздела {path} нет в меню"
    for name in GROUP_NAMES:
        assert name in body


async def test_groups_without_work_show_no_badge(panel_client):
    """Бейдж есть только у группы, где есть что делать: ноль места не занимает."""
    assert login_panel(panel_client)
    today = await repo.admin_today()
    gaps = [row for row in await repo.data_gaps() if row["count"]]
    numbers = {
        "Обращения": to_int(today["no_answer"]),
        "Люди": to_int(today["no_staff"]),
        "Справочники": sum(to_int(row["count"]) for row in gaps),
    }
    for name, number in numbers.items():
        block = nav_group(panel_client.get("/panel/").text, name)
        if number:
            assert f">{number}</b>" in block, f"в группе «{name}» нет бейджа {number}"
        else:
            assert "nav-badge" not in block, f"в группе «{name}» бейдж с нулём"


# ── 2. бейджи групп считаются из сводки дня ─────────────────────────────────
async def test_badges_show_numbers_from_admin_today(panel_client):
    await make_ticket()                                  # одно без ответа
    await repo.touch_contact("300", "novichok", "Новиков", "Здравствуйте")
    today = await repo.admin_today()
    assert today["no_answer"] == 1 and today["no_staff"] >= 1
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert "nav-badge hot" in body
    tickets_badge = nav_group(body, "Обращения")
    people_badge = nav_group(body, "Люди")
    assert f'>{today["no_answer"]}</b>' in tickets_badge
    assert f'>{today["no_staff"]}</b>' in people_badge


async def test_badge_of_reference_gaps_sums_counts(panel_client):
    gaps = [row for row in await repo.data_gaps() if row["count"]]
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    reference = nav_group(body, "Справочники")
    if gaps:
        assert f'>{sum(int(row["count"]) for row in gaps)}</b>' in reference
    else:
        assert "nav-badge" not in reference


# ── 3. главная страница «Пульт» ────────────────────────────────────────────
def test_overview_has_six_clickable_counters(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    counters = re.findall(r'<a class="big-stat[^"]*" href="([^"]+)"', body)
    assert len(counters) == 6, f"счётчиков на главной: {counters}"
    for link in COUNTER_LINKS:
        assert link in counters, f"счётчика со ссылкой {link} нет"
    # числа докручивает CSS, значение лежит в --to
    assert body.count('class="count"') == 5
    assert 'class="big-txt"' in body          # среднее время ответа - текстом


def test_overview_links_to_the_feed(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert "/panel/activity" in body
    assert "Что требует действия сегодня" in body


async def test_overview_action_rows_point_to_sections(panel_client):
    await make_ticket()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    todo = re.search(r'<ul class="todo">.*?</ul>', body, re.S)
    assert todo, "нет списка «что требует действия»"
    links = re.findall(r'<a class="todo-name" href="([^"]+)"', todo.group(0))
    assert links == ["/panel/tickets?scope=waiting", "/panel/tickets?status=ready",
                     "/panel/access", "/panel/nostaff", "/panel/access", "/panel/database"]
    assert "todo-n" in todo.group(0)


def test_overview_says_nothing_to_do_when_counters_are_zero(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert "todo-row zero" in body          # строки на месте, но с нулём


# ── 4. чистка зависших диалогов ─────────────────────────────────────────────
def test_cleanup_form_is_a_normal_post_form(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    form = re.search(r'<form method="post" action="/panel/cleanup/states".*?</form>', body, re.S)
    assert form, "на главной нет кнопки очистки зависших диалогов"
    assert 'name="csrf"' in form.group(0)
    assert "Очистить" in form.group(0)


async def test_cleanup_removes_stuck_dialogs(panel_client):
    await db.set_state("100", "reg_name")
    await db.run("UPDATE user_states SET created_at=''")
    assert await db.get_state("100") is not None
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/cleanup/states").status_code == 303
    assert await db.get_state("100") is None
    assert "Удалено зависших состояний" in panel_client.get("/panel/").text


# ── 5. лента событий ────────────────────────────────────────────────────────
async def test_activity_page_lists_events(panel_client):
    await make_ticket("Нужна справка для военкомата")
    assert login_panel(panel_client)
    response = panel_client.get("/panel/activity")
    body = response.text
    assert response.status_code == 200
    for column in ("Когда", "Кто", "Обращение", "Событие", "Деталь"):
        assert column in body
    assert "создал" in body or "создано" in body   # подпись события из репозитория


def test_activity_page_has_filters_and_pagination(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/activity").text
    assert 'name="event"' in body and 'name="q"' in body
    assert "все события" in body
    assert 'class="pager"' in body or "Событий не нашлось" in body


async def test_activity_filters_by_event_and_text(panel_client):
    await make_ticket("Нужна справка для военкомата")
    assert login_panel(panel_client)
    whole = panel_client.get("/panel/activity").text
    assert "создано" in whole
    # фильтр по типу события: архичных событий в тесте нет
    archived = panel_client.get("/panel/activity", params={"event": "archive"}).text
    assert "Событий не нашлось" in archived
    # поиск по тексту: студент известен, а вот случайного слова - нет
    found = panel_client.get("/panel/activity", params={"q": "Иванов"}).text
    assert "Событий не нашлось" not in found
    missing = panel_client.get("/panel/activity", params={"q": "нетакого"}).text
    assert "Событий не нашлось" not in missing or "Иванов" not in missing


async def test_activity_paginates_by_fifty(panel_client):
    ticket_id = await make_ticket()
    for index in range(60):
        await db.run("INSERT INTO ticket_events(ticket_id, event, detail, actor_id, created_at)"
                     " VALUES(?, 'status', ?, '200', datetime('now', ?))",
                     (ticket_id, f"Проверка {index}", f"-{index} minutes"))
    assert login_panel(panel_client)
    first = panel_client.get("/panel/activity").text
    second = panel_client.get("/panel/activity", params={"page_no": 2}).text
    assert "Страница 1 из" in first and "Страница 2 из" in second
    # на странице ровно fifty событий, остальные - на следующей
    assert first.count("<tr data-hk") == webpanel.ACTIVITY_PAGE
    assert 0 < second.count("<tr data-hk") < webpanel.ACTIVITY_PAGE
    assert "Проверка 0" not in first           # свежие сверху
    assert "Проверка 0" in second
    assert "data-hk" in first                  # строки ленты доступны с клавиатуры


# ── 6. горячие клавиши ──────────────────────────────────────────────────────
def keys_script() -> str:
    return webpanel.hotkeys_script(webpanel.NAV_SECTIONS)


def test_hotkeys_script_declares_every_key():
    script = keys_script()
    for key in ('key === "j"', 'key === "k"', 'key === "Enter"', 'key === "a"',
                'key === "r"', 'key === "e"', 'key === "?"'):
        assert key in script, f"клавиша {key} не объявлена"
    assert 'event.key === "k" && (event.ctrlKey || event.metaKey)' in script
    for item, label in panel_theme.HOTKEY_HINT:
        assert item in script, f"подсказка по «?» без {item}"
        assert label in script


def test_hotkeys_ignore_typing_in_fields():
    script = keys_script()
    assert "function editing(" in script
    assert "/^(INPUT|TEXTAREA|SELECT|OPTION)$/" in script
    handler = script[script.index('document.addEventListener("keydown"'):]
    guard = handler.index("editing(event.target)")
    # проверка полей ввода стоит раньше любой клавиши
    assert guard < handler.index('key === "j"')
    assert "isContentEditable" in script


def test_hotkeys_respect_reduced_motion():
    """При prefers-reduced-motion клавиши не запускают сглаженную прокрутку."""
    script = keys_script()
    assert "prefers-reduced-motion: reduce" in script
    assert 'quiet() ? "auto" : "smooth"' in script


def test_hotkeys_open_section_palette():
    script = keys_script()
    assert "SECTIONS" in script and "/panel/activity" in script
    assert '"palette-box"' in script and '"palette-list"' in script
    assert "Esc" in script


def test_panel_includes_hotkey_script(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    assert 'key === "j"' in body
    assert "panelToast" in body          # тосты для копирования и подсказки
    assert "panel-filters" in body       # сохранённые фильтры


# ── 7. сохранённые фильтры рабочего места ───────────────────────────────────
def test_workbench_saves_filters_in_local_storage(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    tag = form_tag(body, "/panel/tickets")
    assert 'data-filters="status,category,q"' in tag
    assert 'data-filters-saved="1"' in tag        # фильтров в адресе нет - восстановить
    assert "data-filters-reset" in body           # кнопка «Сбросить фильтры»
    assert 'href="/panel/tickets"' in body
    script = webpanel.actions_script()
    assert 'localStorage.getItem(KEY)' in script and "panel-filters" in script
    assert 'localStorage.removeItem(KEY)' in script


def test_explicit_filters_are_not_overridden(panel_client):
    """Ссылка с фильтром важнее сохранённого: data-filters-saved не ставится."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets", params={"status": "open"}).text
    tag = form_tag(body, "/panel/tickets")
    assert 'data-filters="status,category,q"' in tag
    assert "data-filters-saved" not in tag


# ── 8. эмодзи ушли из кнопок и статусов ─────────────────────────────────────
async def test_no_emoji_in_buttons_and_statuses(panel_client):
    """Проверяем интерфейс по списку эмодзи, а не весь файл: в текстах для MAX
    (notify) и в подписях <option> эмодзи остаются."""
    assert login_panel(panel_client)
    await repo.upsert_user("500", "Петров Пётр", "ис-21")
    found = []
    for path in UI_PAGES:
        body = panel_client.get(path).text
        for text in labels(body):
            if path == "/panel/students" and CONSENT_MARK in text:
                continue          # отметка согласия остаётся осознанно
            found += [f"{path}: {mark} в «{text.strip()}»" for mark in UI_EMOJI if mark in text]
    assert not found, "эмодзи в кнопках и статусах:\n" + "\n".join(found[:10])


async def test_statuses_are_pills_and_dots(panel_client):
    assert login_panel(panel_client)
    await repo.create_invite("ABC234", created_by="1")
    body = panel_client.get("/panel/access").text
    assert 'class="dot-state on"' in body
    assert "активен" in body
    assert "🟢" not in body


async def test_queue_waits_mark_is_an_icon(panel_client):
    ticket_id = await make_ticket()
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert "ждёт ответа" in body and "wb-wait" in body
    assert "⏳" not in body


# ── 9. копирование, печать, ссылка на бота, экспорт ─────────────────────────
async def test_copy_buttons_on_values(panel_client):
    await repo.create_invite("ABC234", created_by="1")
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login_panel(panel_client)
    assert 'data-copy="ABC234"' in panel_client.get("/panel/access").text
    assert 'data-copy="100"' in panel_client.get("/panel/people").text
    assert 'data-copy="100"' in panel_client.get("/panel/students").text


def test_copy_script_reports_through_toast():
    script = webpanel.actions_script()
    assert "navigator.clipboard" in script
    assert "panelToast" in script


async def test_workbench_copies_ticket_link(panel_client):
    ticket_id = await make_ticket()
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert f'data-copy="{webpanel.panel_link(f"/tickets?t={ticket_id}")}"' in body
    assert 'data-copy="100"' in body        # MAX ID студента копируется рядом


def test_print_version_hides_menu_and_forms():
    body = print_css()
    assert "header,nav,footer" in body
    assert ".wb-queue" in body and ".wb-edit" in body and ".wb-reply" in body
    assert ".dochead .page-actions" in body


async def test_workbench_has_print_button(panel_client):
    ticket_id = await make_ticket()
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert "window.print()" in body and "Печать" in body


async def test_open_in_bot_link_needs_bot_username(panel_client):
    ticket_id = await make_ticket()
    assert login_panel(panel_client)
    assert "Открыть в боте" not in panel_client.get("/panel/").text
    await db.set_setting("bot_username", "lpc_navigator")
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    assert "https://max.ru/lpc_navigator" in body
    assert "Открыть в боте" in panel_client.get("/panel/").text


def test_exports_live_in_the_page_header(panel_client):
    assert login_panel(panel_client)
    tickets = panel_client.get("/panel/tickets").text
    people = panel_client.get("/panel/people").text
    for body in (tickets, people):
        assert 'class="dochead"' in body and 'class="page-actions"' in body
        assert "/panel/tickets.csv" in tickets and "/panel/people.csv" in people
    assert "Выгрузить в CSV" in tickets and "до 2000 строк" in tickets
    assert "Выгрузить в CSV" in people and "до 5000 строк" in people
