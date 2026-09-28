"""Графики панели: роли палитры вместо «голых» цветов, доступность, анимация.

Модуль проверяет то, что легко сломать при правке разметки руками:

* в SVG нет ни одного «голого» hex - цвет приходит из роли палитры через
  ``currentColor`` (единственное исключение - ``style="color:…"`` от вызывающего
  кода с цветом, которого нет в палитре);
* ``palette_css()`` описывает все роли, умеет анимацию и уважает
  ``prefers-reduced-motion``;
* у каждого графика есть ``role="img"`` и русский ``<title>``, сетка спрятана от
  скринридера, у столбиков и полос - подсказки;
* ``bump()`` действительно меняет значение, иначе при смене периода браузер не
  перерисует график и анимация не начнётся заново;
* пустое состояние объясняет по-русски, что произошло и что делать.
"""
import re

import charts

HEX = re.compile(r"#[0-9a-fA-F]{3,8}")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
STYLE_ATTR = re.compile(r'style="[^"]*"')

DAYS = [{"day": "2026-09-24", "count": 4, "done": 2},
        {"day": "2026-09-25", "count": 7, "done": 3},
        {"day": "2026-09-26", "count": 1, "done": 0}]
STATUS = [{"status": "new", "label": "Новое", "count": 4},
          {"status": "in_progress", "label": "В работе", "count": 2},
          {"status": "completed", "label": "Готово", "count": 1}]
LOAD = [{"full_name": "Петрова Анна", "tickets": 7}, {"full_name": "Соколов", "tickets": 2}]
SVG_CHARTS = ("bar_chart", "donut", "sparkline")  # bars() собран на HTML, не на SVG


def panel_charts() -> dict:
    """Все диаграммы панели: имя → HTML (цвета из старой панели - в отдельном тесте)."""
    return {
        "bar_chart": charts.bar_chart(DAYS, "count", "day", "Обращения по дням, 30 дн.",
                                      second_key="done"),
        "donut": charts.donut(STATUS, "count", "label", "Статусы обращений"),
        "bars": charts.bars(LOAD, "tickets", "full_name", "Нагрузка на сотрудников"),
        "sparkline": charts.sparkline([1, 4, 2, 6, 3], "Динамика обращений"),
    }


def svgs_of(html: str) -> list:
    """Все куски разметки <svg>…</svg>."""
    return re.findall(r"<svg\b.*?</svg>", html, re.S)


def bare_hex(html: str) -> set:
    """Цвета, которые не спрятаны в style вызывающего кода."""
    return set(HEX.findall(STYLE_ATTR.sub("", html)))


def test_svg_has_no_raw_hex_colors():
    """В SVG цветов-констант нет: только currentColor и классы ролей палитры."""
    for name, html in panel_charts().items():
        for svg in svgs_of(html):
            found = HEX.search(svg)
            assert not found, f"{name}: в SVG остался «голый» цвет {found.group(0)}"
            assert "currentColor" in svg, f"{name}: фигура рисуется не через currentColor"


def test_colors_come_from_palette_roles():
    """Каждый цвет разметки - роль палитры, а роль описана в CSS панели."""
    css = charts.palette_css()
    for name, html in panel_charts().items():
        assert not bare_hex(html), f"{name}: цвета не из ролей: {bare_hex(html)}"
        roles = set(re.findall(r"chart-c-([a-z_0-9]+)", html))
        assert roles, f"{name}: ни одного класса роли"
        assert roles <= set(charts.PALETTE), f"{name}: неизвестные роли {roles - set(charts.PALETTE)}"
        for role in roles:
            assert f".chart-c-{role}{{" in css, f"{name}: роль {role} не описана в CSS"


def test_known_colors_become_roles():
    """Цвета старой панели не теряются: они находят ближайшую роль палитры."""
    assert charts.role_for("#8b5cf6") == "bar_3"
    assert charts.role_for("#06b6d4") == "bar_4"
    assert charts.role_for("#22c55e") == "bar_2"
    assert charts.role_for("warn") == "warn"
    assert charts.role_for("") == "bar"
    assert charts.role_for("непонятный цвет") == "bar"
    html = charts.bars(LOAD, "tickets", "full_name", "Нагрузка", color="#8b5cf6")
    assert "chart-c-bar_3" in html
    assert not bare_hex(html)
    # незнакомый цвет не теряется, но уходит в style, а не в атрибут фигуры
    own = charts.bars(LOAD, "tickets", "full_name", "Нагрузка", color="#123456")
    assert 'style="color:#123456' in own
    assert not bare_hex(own)


def test_palette_css_describes_every_role():
    """CSS панели получает правило на каждую роль и её значение по умолчанию."""
    css = charts.palette_css()
    assert css.strip()
    for role, value in charts.PALETTE.items():
        assert f".chart-c-{role}{{" in css, f"нет правила для роли {role}"
        assert charts.css_var(role) in css, f"роль {role} не описана переменной"
        assert value in css, f"у роли {role} потерялось значение по умолчанию"
    assert "currentColor" in css


def test_palette_css_has_animation_and_reduced_motion():
    """Появление полос и уважение к «не двигать картинки»."""
    css = charts.palette_css()
    assert "@keyframes chart-grow" in css
    assert ".chart-anim .chart-bar" in css
    assert "@keyframes chart-grow-x" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation:none" in reduced


def test_charts_are_labelled_for_screen_readers():
    """У каждого <svg> есть роль и русское описание, первым внутри - <title>."""
    for name, html in panel_charts().items():
        found = svgs_of(html)
        if name not in SVG_CHARTS:
            assert not found, f"{name}: SVG-диаграмма собрана на HTML-фигурах"
            continue
        assert found, f"{name}: нет svg"
        for svg in found:
            assert 'role="img"' in svg, f"{name}: у svg нет role=img"
            inner = svg[svg.index(">") + 1:]
            assert inner.lstrip().startswith("<title>"), f"{name}: <title> не первый внутри svg"
            title = re.search(r"<title>(.*?)</title>", inner, re.S).group(1)
            assert CYRILLIC.search(title), f"{name}: описание без русского текста"


def test_grid_lines_are_hidden_from_assistive_tech():
    """Линии сетки - украшение, о них не нужно рассказывать скринридеру."""
    svg = svgs_of(panel_charts()["bar_chart"])[0]
    group = re.search(r"<g class=\"chart-grid[^\"]*\"[^>]*>", svg)
    assert group, "нет группы сетки"
    assert 'aria-hidden="true"' in group.group(0)
    assert svg.count("<line") == 4
    assert svg.index("</g>") < svg.index('class="axis')  # подписи оси остались снаружи


def test_columns_have_russian_tooltips():
    """Подсказка столбика: «26.09 — 4 обращения», у вложенного ряда — «завершено»."""
    html = panel_charts()["bar_chart"]
    rects = re.findall(r"<rect\b.*?</rect>", html, re.S)
    assert len(rects) == 5  # три столбика и два вложенных «завершено»
    tips = []
    for rect in rects:
        title = re.search(r"<title>(.*?)</title>", rect, re.S)
        assert title, f"у столбика нет подсказки: {rect}"
        tips.append(title.group(1))
        assert CYRILLIC.search(title.group(1))
        assert re.search(r"\d\d\.\d\d — (?:завершено )?\d+ обращени", title.group(1)), title.group(1)
    assert any("завершено 2" in tip for tip in tips)
    assert any("1 обращение" in tip for tip in tips)


def test_horizontal_bars_have_tooltips():
    """В HTML подсказка живёт в атрибуте title: <title> там не работает."""
    html = panel_charts()["bars"]
    tips = re.findall(r'title="([^"]*)"', html)
    assert "Петрова Анна — 7 обращений" in tips, tips
    assert any("2 обращения" in tip for tip in tips), tips


def test_animation_class_and_reload_marker():
    """Анимация включена классом, а маркер перерисовки меняется на каждом вызове."""
    for name, html in panel_charts().items():
        assert "chart-anim" in html, f"{name}: нет класса анимации"
        marker = re.search(r'data-reload="([^"]+)"', html)
        assert marker, f"{name}: нет маркера перерисовки"
        assert marker.group(1).startswith(charts.RELOAD_MARKER), marker.group(1)
    row = [{"day": "2026-09-24", "count": 3}]
    first = charts.bar_chart(row, "count", "day", "Динамика")
    second = charts.bar_chart(row, "count", "day", "Динамика")
    week = charts.bar_chart(row, "count", "day", "Динамика", reload=7)
    values = [re.search(r'data-reload="([^"]+)"', h).group(1) for h in (first, second, week)]
    assert values[0] != values[1], "маркер не изменился - график не перерисуется"
    assert values[2] != values[0], "смена периода не отразилась в маркере"


def test_bump_changes_value():
    """bump() - тот же словарь, но значение всегда новое."""
    data = {"reload": 30}
    assert charts.bump(data, "reload") is data
    assert data["reload"] == 31
    assert charts.bump(data, "reload")["reload"] == 32
    assert charts.bump({"mark": "r-7"}, "mark")["mark"] == "r-8"
    assert charts.bump({}, "reload")["reload"] == 1
    assert charts.bump({"reload": None}, "reload")["reload"] == 1
    assert charts.bump({"reload": "вчера"}, "reload")["reload"] == 1


def test_empty_state_is_russian_and_actionable():
    """Пустое состояние говорит, что данных нет, и подсказывает, что делать."""
    for name, html in {
        "bar_chart": charts.bar_chart([], "count", "day", "Пусто"),
        "donut": charts.donut([], "count", "label", "Пусто"),
        "bars": charts.bars([], "tickets", "full_name", "Пусто"),
        "sparkline": charts.sparkline([1], "Пусто"),
    }.items():
        assert "<svg" not in html, f"{name}: нарисован пустой график"
        assert charts.EMPTY_TEXT in html, f"{name}: нет понятной подписи"
        hint = re.search(r'class="chart-empty-hint">([^<]*)<', html)
        assert hint and CYRILLIC.search(hint.group(1)), f"{name}: нет подсказки"


def test_empty_state_mentions_period():
    """Отдельный вариант: «нет данных за период» + что делать дальше."""
    html = charts.bar_chart([], "count", "day", "Динамика", period="30 дн.")
    assert "Нет данных за период 30 дн." in html
    assert charts.EMPTY_PERIOD_HINT in html
    assert "<svg" not in html
    own = charts.empty("Нагрузка", hint="Сотрудников пока нет")
    assert "Сотрудников пока нет" in own


def test_labels_stay_escaped_in_tooltips():
    """Подписи приходят из базы: спецсимволы не должны ломать разметку."""
    html = charts.bar_chart([{"day": '"><script>alert(1)</script>', "count": 2}],
                            "count", "day", "График")
    assert "<script" not in html
    assert "<rect" in html
    bars = charts.bars([{"full_name": '<script>alert("x")</script>', "tickets": 1}],
                       "tickets", "full_name", "X")
    assert "<script>alert" not in bars
    assert "&lt;script&gt;" in bars
    assert 'title=""' not in bars  # атрибут не порван кавычкой
