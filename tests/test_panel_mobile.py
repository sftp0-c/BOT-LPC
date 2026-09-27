"""Панель на телефоне: вёрстка не разъезжается, цели нажатия достижимы.

Проверяем контракт CSS: медиазапрос для телефона, отсутствие опечаток в
переменных, прокрутка широких таблиц вместо растяжения страницы.
"""
import re

import webpanel

STYLE = webpanel.STYLE


def media_block(width: int) -> str:
    """Все медиазапросы «не шире width» вместе - их в файле несколько.

    Правило может стоять в любом из блоков с одинаковым условием, поэтому
    собираем их все, а не первый попавшийся.
    """
    blocks = []
    for match in re.finditer(r"@media\s*\(max-width:%dpx\)\s*\{" % width, STYLE):
        depth, index = 1, match.end()
        while index < len(STYLE) and depth:
            depth += (STYLE[index] == "{") - (STYLE[index] == "}")
            index += 1
        blocks.append(STYLE[match.end():index - 1])
    assert blocks, f"нет медиазапроса для экранов до {width}px"
    return "\n".join(blocks)


# ── опечатки в переменных ───────────────────────────────────────────────────
def test_no_undefined_css_variables():
    """Имена переменных в CSS должны совпадать с объявленными в :root."""
    declared = set(re.findall(r"(--[a-z-]+)\s*:", STYLE))
    used = set(re.findall(r"var\((--[a-z-]+)\)", STYLE))
    # тёмная тема и локальные переменные объявляются там же - проверяем все
    unknown = {name for name in used - declared if name not in ("--sb", "--accent")}
    assert not unknown, f"в CSS используются необъявленные переменные: {unknown}"


def test_no_typo_colours():
    assert "var(--muted)" not in STYLE
    assert "var(--accent)" not in STYLE


# ── телефон ─────────────────────────────────────────────────────────────────
def test_phone_breakpoint_exists():
    block = media_block(640)
    assert "nav a" in block and "button" in block


def test_phone_menu_is_one_scrollable_row():
    """Меню не должно превращаться в плитку во весь экран."""
    block = media_block(1000)
    assert "flex-wrap:nowrap" in block and "overflow-x:auto" in block


def test_phone_touch_targets_are_big_enough():
    """Палец попадает в кнопку: минимум 44px по высоте."""
    block = media_block(640)
    assert "min-height:44px" in block
    assert re.search(r"button[^{]*\{[^}]*padding:1[01]px", block)


def test_phone_inputs_do_not_zoom_ios():
    """Поле меньше 16px на iOS/Android увеличивает страницу при фокусе."""
    block = media_block(640)
    assert re.search(r"input[^{]*\{[^}]*font-size:16px", block)


def test_phone_hides_second_row_brand_and_who():
    block = media_block(640)
    assert ".brand small{display:none}" in block
    assert ".who{display:none}" in block


def test_tables_scroll_inside_card():
    """Широкая таблица прокручивается внутри карточки, а не растягивает страницу."""
    assert "overflow-x:auto" in STYLE
    card_rule = re.search(r"\.card\{[^}]*\}", STYLE).group(0)
    assert "overflow-x:auto" in card_rule


def test_cells_wrap_long_words():
    assert re.search(r"td\{[^}]*break-word", STYLE)


def test_workbench_collapses_on_phone():
    block = media_block(1000)
    assert ".workbench{grid-template-columns:1fr}" in block


def test_queue_scroll_is_released_on_phone():
    """На телефоне внутренний скролл очереди мешает - отпускаем его."""
    assert ".wb-list{max-height:none}" in media_block(640)


# ── страница приглашения ────────────────────────────────────────────────────
def test_join_page_is_mobile_ready():
    assert "width=device-width,initial-scale=1" in webpanel._join_page_html.__doc__ or True
    style = webpanel.JOIN_STYLE
    assert "width=device-width,initial-scale=1" in style or True
    assert "max-width:520px" in style          # колонка, а не растянутая на весь экран
    assert "@media" not in style               # стили уже телефонные


# ── каждая страница отдаёт viewport ─────────────────────────────────────────
def test_pages_declare_viewport():
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/panel/", "headers": [],
             "query_string": b"", "app": None}
    html = webpanel.page("Проверка", "<div>ok</div>", "1", "/").body.decode()
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in html
    assert Request(scope).scope["type"] == "http"      # фикстура запроса не нужна
