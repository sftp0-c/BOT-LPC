"""Дизайн-система панели: токены, иконки, темы, анимации.

Тест живёт рядом с panel_theme.py и проверяет модуль сам по себе: webpanel.py
здесь не нужен. Логика проверок намеренно строгая - смысл такой, чтобы нельзя
было случайно вернуть «магический» цвет в CSS, разную толщину у иконок или
тему, которая ломается на телефоне.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                      # модуль лежит в корне репозитория
    sys.path.insert(0, str(ROOT))

import panel_theme as theme                        # noqa: E402  (путь задаём выше)

# ── что должно быть в модуле ────────────────────────────────────────────────
# Иконки всех разделов панели: пока вместо них в меню стояли эмодзи.
REQUIRED_SECTIONS = ("home", "tickets", "analytics", "people", "user-off", "college",
                     "students", "staff", "access", "link", "templates", "groups",
                     "schedules", "broadcasts", "database", "settings", "logs")
# Иконки действий, которые бот и панель рисуют рядом с текстом.
REQUIRED_ACTIONS = ("archive", "reply", "delete", "edit", "search", "chevron-down",
                    "chevron-right", "chevron-left", "close", "plus", "check",
                    "warning", "loading")
PANE_MARKERS = (
    "header{", ".brand", ".gsearch", ".who{", "nav a", ".nav-ico", ".nav-txt", "main{",
    ".page-title", ".card{", ".cards{", ".kpi", ".stat{", "table{", "th{", "td{",
    ".grid{", ".full", "form.inline", "button,.btn", ".btn-ok", ".btn-bad", ".btn-grey",
    ".btn-sm", ".msg{", ".msg-ok", ".msg-bad", ".pill{", ".pill-on", ".pill-off",
    ".mut{", ".small{", ".num{", ".lead{", "h3{", "th.col-key", ".kpi .warn", ".kpi .good",
    "nav a.on", ".workbench{", ".wb-queue", ".wb-list", ".wb-item", ".wb-on", ".wb-head",
    ".wb-status", ".wb-date", ".wb-text", ".wb-bulk", ".wb-card", ".wb-access",
    ".charts{", ".chart-box", ".chart{", ".grid-line", ".axis", ".donut{", ".donut-total",
    ".donut-sub", ".donut-wrap", ".legend{", ".legend-list", ".hbar-list", ".hbar-label",
    ".hbar-track", ".chart-empty", ".spark", "pre{", "footer{", "code,",
)
# Элементы, которых раньше не было, но без них панель выглядит бедно.
EXTRA_MARKERS = (".theme-toggle", ".toast-stack", ".toast{", ".toast.is-out", ".skel{",
                 ".skel-line", ".skel-block", ".loading-dot", ".spin", ".empty{",
                 ".is-loading", "details{", ".count{", "tbody tr.is-found", ".ico{")


def without_comments(css: str) -> str:
    """CSS без комментариев: иначе проверка цветов цепляет слова в комментариях."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def without_root(css: str) -> str:
    """CSS без правил, селектор которых содержит :root.

    Только в :root живут «голые» значения палитры - их и проверяем отдельно.
    Скобки считаем в глубину: вложенных блоков внутри правила тут нет, но
    @media/@supports их оборачивают, поэтому идём по балансу.
    """
    out, index = [], 0
    while True:
        found = css.find(":root", index)
        if found < 0:
            out.append(css[index:])
            return "".join(out)
        out.append(css[index:found])
        brace = css.find("{", found)
        if brace < 0:
            return "".join(out)
        depth, scan = 1, brace + 1
        while scan < len(css) and depth:
            depth += (css[scan] == "{") - (css[scan] == "}")
            scan += 1
        index = scan


def media_block(css: str, width: int) -> str:
    """Все блоки «не шире width» вместе: правил с одинаковым условием несколько."""
    blocks = []
    for match in re.finditer(r"@media\s*\(max-width:%dpx\)\s*\{" % width, css):
        depth, index = 1, match.end()
        while index < len(css) and depth:
            depth += (css[index] == "{") - (css[index] == "}")
            index += 1
        blocks.append(css[match.end():index - 1])
    assert blocks, f"нет медиазапроса для экранов до {width}px"
    return "\n".join(blocks)


# Разбор атрибута d: команды с числом аргументов на каждую.
PATH_STEP = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
PATH_PARTS = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)")
PATH_NUMBER = re.compile(r"-?\d*\.?\d+")


def path_points(data: str) -> list:
    """Точки пути в координатах сетки: относительные команды считаем от текущей."""
    points, current = [], (0.0, 0.0)
    for command, raw in PATH_PARTS.findall(data):
        args = [float(value) for value in PATH_NUMBER.findall(raw)]
        upper, low = command.upper(), command.islower()
        shift = (lambda x, y: (current[0] + x, current[1] + y)) if low else (
            lambda x, y: (x, y))
        if upper == "Z":
            current = points[-1] if points else current
            continue
        step = PATH_STEP[upper]
        index = 0
        while index + step <= len(args):
            chunk = args[index:index + step]
            index += step
            if upper == "M":
                current = shift(chunk[0], chunk[1])
                points.append(current)
                # остаток пары после moveto - это уже линии
                while index + 2 <= len(args):
                    current = (current[0] + args[index], current[1] + args[index + 1]) if low \
                        else (args[index], args[index + 1])
                    points.append(current)
                    index += 2
                break
            if upper in ("L", "T"):
                current = shift(chunk[0], chunk[1])
                points.append(current)
            elif upper == "H":
                current = ((current[0] + chunk[0]) if low else chunk[0]), current[1]
                points.append(current)
            elif upper == "V":
                current = current[0], ((current[1] + chunk[0]) if low else chunk[0])
                points.append(current)
            elif upper in ("C", "S", "Q"):
                for pair in zip(chunk[0::2], chunk[1::2]):
                    points.append(shift(pair[0], pair[1]))
                current = points[-1]
            elif upper == "A":
                # у дуги берём концы: выгиб наружу она всё равно не выходит за поле
                current = shift(chunk[5], chunk[6])
                points.append(current)
    return points


def relative_luminance(colour: str) -> float:
    """Яркость цвета по WCAG - ею меряем читаемость текста."""
    parts = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    parts = [part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4
             for part in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


STYLE = without_comments(theme.STYLESHEET)


# ── модуль отдаёт то, что от него ждут ──────────────────────────────────────
def test_module_exposes_design_system():
    assert isinstance(theme.TOKENS, dict) and theme.TOKENS
    assert isinstance(theme.ICONS, dict) and theme.ICONS
    assert isinstance(theme.STYLESHEET, str) and len(theme.STYLESHEET) > 5000
    assert callable(theme.icon) and callable(theme.theme_script)


# ── токены ───────────────────────────────────────────────────────────────────
def test_tokens_have_no_empty_values():
    empty = [key for key, value in theme.TOKENS.items() if not str(value).strip()]
    assert not empty, f"пустые значения токенов: {empty}"
    with_breaks = [key for key, value in theme.TOKENS.items()
                   if "\n" in str(value) or "{" in str(value)]
    assert not with_breaks, f"токен не должен быть блоком CSS: {with_breaks}"


def test_token_names_are_usable_in_css():
    """Имя токена - слово из строчных букв и дефисов: так его видит и браузер,
    и проверка переменных в самой панели (--[a-z-]+)."""
    odd = [key for key in theme.TOKENS
           if not re.fullmatch(r"(dark|light|base)\.[a-z][a-z-]*", key)]
    assert not odd, f"неподходящие имена токенов: {odd}"


def test_both_themes_cover_the_same_tokens():
    """Светлая и тёмная темы - одна и та же геометрия: набор токенов совпадает."""
    dark = {key.partition(".")[2] for key in theme.TOKENS if key.startswith("dark.")}
    light = {key.partition(".")[2] for key in theme.TOKENS if key.startswith("light.")}
    assert dark and dark == light, (
        f"только в тёмной: {sorted(dark - light)}; только в светлой: {sorted(light - dark)}")


def test_tokens_cover_look_groups():
    """Радиусы, шрифты, размеры, интервалы, скорости и раскладка заданы токенами."""
    base = {key[5:] for key in theme.TOKENS if key.startswith("base.")}
    for needed in ("radius-sm", "radius-md", "font-sans", "font-mono", "text-base",
                   "text-num", "weight-semi", "leading", "space-md", "space-lg",
                   "icon-md", "motion-fast", "motion-base", "motion-slow",
                   "ease-standard", "sidebar", "tap-min"):
        assert needed in base, f"нет токена {needed}"


def test_theme_has_own_chart_colours_and_soft_shadows():
    """У тёмной темы свои цвета графиков и мягче тени - иначе она «серая на сером»."""
    for name in ("chart-a", "chart-grid", "chart-axis", "chart-track", "skel-a",
                 "shadow-md", "glow"):
        assert f"dark.{name}" in theme.TOKENS and f"light.{name}" in theme.TOKENS
    assert theme.TOKENS["dark.shadow-md"] != theme.TOKENS["light.shadow-md"]
    assert theme.TOKENS["dark.mut"] != theme.TOKENS["light.mut"]


def test_dark_text_has_readable_contrast():
    """Приглушённый текст тёмной темы обязан отличаться от фона, а не сливаться."""
    ink, mut = theme.TOKENS["dark.ink"], theme.TOKENS["dark.mut"]
    assert contrast(ink, theme.TOKENS["dark.bg"]) >= 7, "основной текст слишком тусклый"
    assert contrast(ink, theme.TOKENS["dark.surface"]) >= 7
    assert contrast(mut, theme.TOKENS["dark.surface"]) >= 4.5, "подписи не читаются"
    assert relative_luminance(ink) > relative_luminance(mut) > 0
    for pair in (("dark.acc", "dark.acc-ink"), ("dark.ok", "dark.ok-ink"),
                 ("dark.bad", "dark.bad-ink"), ("dark.warn", "dark.warn-ink")):
        assert contrast(theme.TOKENS[pair[0]], theme.TOKENS[pair[1]]) >= 4.5, pair


def test_light_text_has_readable_contrast():
    """Светлая тема - та же геометрия, поэтому контраст должен быть не хуже."""
    ink, mut = theme.TOKENS["light.ink"], theme.TOKENS["light.mut"]
    assert contrast(ink, theme.TOKENS["light.surface"]) >= 7
    assert contrast(ink, theme.TOKENS["light.bg"]) >= 7
    assert contrast(mut, theme.TOKENS["light.surface"]) >= 4.5
    assert relative_luminance(ink) < relative_luminance(mut) > 0
    for pair in (("light.acc", "light.acc-ink"), ("light.ok", "light.ok-ink"),
                 ("light.bad", "light.bad-ink"), ("light.warn", "light.warn-ink")):
        assert contrast(theme.TOKENS[pair[0]], theme.TOKENS[pair[1]]) >= 4.5, pair


# ── иконки ───────────────────────────────────────────────────────────────────
def test_all_section_and_action_icons_exist():
    missing = [name for name in REQUIRED_SECTIONS + REQUIRED_ACTIONS
               if name not in theme.ICONS]
    assert not missing, f"нет иконок: {missing}"


def test_every_icon_is_valid_svg():
    for name, body in theme.ICONS.items():
        svg = theme.icon(name)
        assert svg.startswith("<svg") and svg.endswith("</svg>"), name
        assert 'viewBox="0 0 24 24"' in svg, f"{name}: нет viewBox"
        assert 'stroke="currentColor"' in svg, f"{name}: цвет задан не токеном"
        assert 'fill="none"' in svg, f"{name}: иконка залита"
        assert f'stroke-width="{theme.ICON_STROKE}"' in svg, f"{name}: толщина обводки"
        assert 'stroke-linecap="round"' in svg and 'stroke-linejoin="round"' in svg, name
        assert body.strip(), f"{name}: пустая иконка"
        assert body.count("<") == body.count(">"), f"{name}: непарные теги"


def test_icons_have_no_external_or_active_content():
    for name in theme.ICONS:
        svg = theme.icon(name)
        for bad in ("<script", "href", "xlink", "<image", "<use", "url(", "http",
                    "onload", "onclick", "<foreignObject", "@import"):
            assert bad not in svg, f"{name}: в иконке есть {bad}"


def test_icons_share_one_stroke_width():
    """Толщина задаётся один раз в icon(): внутри иконок своего веса быть не должно."""
    for name, body in theme.ICONS.items():
        assert "stroke-width" not in body, f"{name}: своя толщина обводки"
        assert "fill=" not in body, f"{name}: своя заливка"
        assert "stroke=" not in body, f"{name}: свой цвет обводки"


def test_icons_stay_inside_the_grid():
    """Иконка не выходит за сетку 24x24: обрезанный край читается как опечатка.

    Считаем настоящие точки пути (относительные команды считаем от текущей
    точки) и берём рамку с запасом: у кривых в неё попадают и контрольные
    точки, поэтому проверка получается строже настоящей геометрии.
    """
    for name, body in theme.ICONS.items():
        points = []
        for data in re.findall(r'd="([^"]+)"', body):
            points += path_points(data)
        for element, attrs in re.findall(r"<(circle|ellipse|rect)\b([^>]*)", body):
            numbers = [float(value) for value in re.findall(r"-?\d*\.?\d+", attrs)]
            if element == "circle":
                points += [(numbers[0] - numbers[2], numbers[1] - numbers[2]),
                           (numbers[0] + numbers[2], numbers[1] + numbers[2])]
            elif element == "ellipse":
                points += [(numbers[0] - numbers[2], numbers[1] - numbers[3]),
                           (numbers[0] + numbers[2], numbers[1] + numbers[3])]
            else:
                points += [(numbers[0], numbers[1]),
                           (numbers[0] + numbers[2], numbers[1] + numbers[3])]
        assert points, f"{name}: пустая геометрия"
        for x, y in points:
            assert 0.5 <= x <= 23.5, f"{name}: x={x} вне сетки"
            assert 0.5 <= y <= 23.5, f"{name}: y={y} вне сетки"


def test_icon_size_and_class_are_safe():
    assert 'width="32"' in theme.icon("home", 32) and 'height="32"' in theme.icon("home", 32)
    assert 'width="20"' in theme.icon("home")                  # размер по умолчанию
    dirty = theme.icon('<script>alert("x")</script>')
    assert dirty.startswith("<svg") and "<script" not in dirty
    assert 'ico-' in dirty


def test_icon_survives_unknown_name():
    """Неизвестное имя - нейтральная точка, а не исключение: страница не падает."""
    for name in ("", "такого-нет", "НЕТ", "home ", None, 42):
        svg = theme.icon(name)
        assert svg.startswith("<svg") and svg.endswith("</svg>"), repr(name)
        assert 'viewBox="0 0 24 24"' in svg
    assert theme.ICONS[theme.FALLBACK_ICON] in theme.icon("такого-нет")


def test_nav_paths_have_distinct_icons():
    """Каждому разделу панели - своя иконка, и все они существуют."""
    assert len(theme.ICON_NAMES_BY_PATH) == len(REQUIRED_SECTIONS)
    unknown = [path for path, name in theme.ICON_NAMES_BY_PATH.items()
               if name not in theme.ICONS]
    assert not unknown, f"нет иконки для разделов: {unknown}"
    used = [theme.ICONS[name] for name in theme.ICON_NAMES_BY_PATH.values()]
    assert len(set(used)) == len(used), "два раздела поделили одну иконку"
    for path in ("/", "/tickets", "/analytics", "/people", "/nostaff", "/college",
                 "/students", "/staff", "/access", "/templates", "/groups",
                 "/schedules", "/broadcasts", "/database", "/settings", "/logs"):
        assert path in theme.ICON_NAMES_BY_PATH, f"раздел {path} без иконки"


# ── стили ────────────────────────────────────────────────────────────────────
def test_stylesheet_is_balanced():
    assert theme.STYLESHEET.count("{") == theme.STYLESHEET.count("}"), "скобки не сходятся"
    assert "@media" in theme.STYLESHEET and "@supports" in theme.STYLESHEET


def test_dark_theme_is_the_default():
    root = re.search(r":root\s*\{[^}]*\}", theme.STYLESHEET)
    assert root, "нет блока :root с токенами"
    block = root.group(0)
    assert "color-scheme:dark" in block, "тёмная тема не объявлена по умолчанию"
    assert "--d-bg:" in block and "--l-bg:" in block, "нет обеих палитр"
    assert "--bg:var(--d-bg)" in block, "по умолчанию включён не тёмный вариант"


def test_light_theme_by_switcher_and_by_system():
    assert '[data-theme="light"]' in theme.STYLESHEET
    assert re.search(r"@media \(prefers-color-scheme: light\)", theme.STYLESHEET)
    assert "--bg:var(--l-bg)" in theme.STYLESHEET, "светлая тема нигде не включается"
    # Системная настройка не должна затирать выбор сис-админа.
    assert ':root:not([data-theme="dark"])' in theme.STYLESHEET
    assert 'color-scheme:light' in theme.STYLESHEET


def test_only_declared_tokens_are_used():
    declared = set(re.findall(r"(--[a-z-]+)\s*:", theme.STYLESHEET)) | theme.css_names()
    used = set(re.findall(r"var\((--[a-z-]+)", theme.STYLESHEET))
    unknown = used - declared
    assert not unknown, f"CSS ссылается на необъявленные переменные: {sorted(unknown)}"


def test_no_bare_colours_outside_root():
    body = without_root(STYLE)
    for pattern, label in ((r"#[0-9a-fA-F]{3,8}\b", "hex"),
                           (r"\brgba?\(", "rgb"),
                           (r"\bhsla?\(", "hsl"),
                           (r":\s*(white|black|red|blue|green|grey|gray)\b", "названный цвет")):
        found = re.findall(pattern, body)
        assert not found, f"вне :root остались цвета ({label}): {found[:5]}"


def test_root_palettes_have_no_magic_spaces():
    """Значение токена - ровно значение: без «rgba(0,0,0,.3) / 2» и лишних пробелов."""
    for key, value in theme.TOKENS.items():
        assert " " not in str(value) or "," in str(value), f"{key}: {value}"


def test_old_variable_names_are_gone():
    """Стили панели приходят одним листом: смешивать старое и новое нельзя."""
    for gone in ("var(--muted)", "var(--accent)", "var(--sb)"):
        assert gone not in theme.STYLESHEET, f"осталось имя из прошлой версии: {gone}"


def test_pane_sections_are_styled():
    missing = [marker for marker in PANE_MARKERS if marker not in STYLE]
    assert not missing, f"в стилях нет: {missing}"


def test_new_sections_are_styled():
    missing = [marker for marker in EXTRA_MARKERS if marker not in STYLE]
    assert not missing, f"в стилях нет: {missing}"


# ── анимации ─────────────────────────────────────────────────────────────────
def test_motion_is_declared_through_tokens():
    transitions = re.findall(r"transition:[^;}]+", STYLE)
    assert transitions, "в стилях нет переходов вообще"
    for rule in transitions:
        for value in re.findall(r"\b\d+m?s\b", rule):
            assert "var(--motion-" in rule, f"переход с временем руками: {rule}"
    # Цвет, граница и тень меняются в пределах 120-200 мс.
    assert theme.TOKENS["base.motion-fast"] == "120ms"
    assert theme.TOKENS["base.motion-base"] == "170ms"
    assert "150ms" in STYLE or "200ms" in STYLE or "170ms" in STYLE


def test_animations_are_restrained():
    names = set(re.findall(r"@keyframes\s+([\w-]+)", STYLE))
    for needed in ("page-in", "card-in", "nav-in", "reveal", "grow-x", "breathe",
                   "spin", "skel-slide", "toast-in", "toast-out", "count-up"):
        assert needed in names, f"нет анимации {needed}"
    # Секунды живут только у бесконечных «дышащих» индикаторов загрузки.
    for value in set(re.findall(r"[\d.]+s\b", STYLE)):
        assert value in ("1.5s", "1.6s"), f"слишком долгая анимация: {value}"
    for rule in re.findall(r"[^{}]*\{[^}]*infinite[^}]*\}", STYLE):
        assert ".skel" in rule or ".loading-dot" in rule or ".spin" in rule, rule
    # Никаких «прыжков»: масштаб не превышает единицу.
    for value in re.findall(r"scale\(([\d.]+)\)", STYLE) + re.findall(r"scale:([\d.]+)", STYLE):
        assert float(value) <= 1.05, f"анимация с прыжком: scale({value})"


def test_card_hover_lifts_without_jumping():
    assert ".card:hover" in STYLE and "translateY(-2px)" in STYLE
    assert "box-shadow:var(--shadow-md)" in STYLE
    # Подъём карточки не должен ломаться о входную анимацию: анимируем
    # отдельное свойство translate, а наведение - transform.
    assert "@keyframes card-in{from{opacity:0;translate:" in STYLE
    assert "transform:translateY(-2px)" in STYLE


def test_counter_is_animated_by_css_only():
    assert "@property --n" in STYLE, "нет регистрации целочисленного свойства"
    assert "counter-reset:c var(--n)" in STYLE
    assert ".count::after{content:counter(c)}" in STYLE
    assert "@keyframes count-up{from{--n:0}to{--n:var(--to)}}" in STYLE
    assert "<script" not in theme.STYLESHEET


def test_reduced_motion_switches_animation_off():
    block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{", STYLE)
    assert block, "нет правила для prefers-reduced-motion"
    depth, index = 1, block.end()
    while index < len(STYLE) and depth:
        depth += (STYLE[index] == "{") - (STYLE[index] == "}")
        index += 1
    body = STYLE[block.end():index - 1]
    assert "animation-duration:1ms !important" in body
    assert "transition-duration:1ms !important" in body
    assert "animation-iteration-count:1 !important" in body
    # Счётчик при выключенной анимации обязан показать конечное число.
    assert "--n:var(--to)" in body


# ── телефон и планшет ────────────────────────────────────────────────────────
def test_phone_breakpoint_is_ready_for_finger():
    block = media_block(STYLE, 640)
    assert "nav a" in block and "button" in block
    assert "min-height:44px" in block, "цель нажатия меньше 44px"
    assert re.search(r"button[^{]*\{[^}]*padding:1[01]px", block)
    assert re.search(r"input[^{]*\{[^}]*font-size:16px", block), "iOS зумит страницу"
    assert ".brand small{display:none}" in block
    assert ".who{display:none}" in block
    assert ".wb-list{max-height:none}" in block
    assert "th.col-key{width:auto}" in block


def test_tablet_menu_is_one_scrollable_row():
    block = media_block(STYLE, 1000)
    assert "flex-wrap:nowrap" in block and "overflow-x:auto" in block
    assert ".workbench{grid-template-columns:1fr}" in block
    assert ":root{--sidebar:0px}" in block, "боковое меню не сложилось"
    assert "nav{position:sticky" in block


def test_tables_and_cards_scroll_inside():
    assert "overflow-x:auto" in STYLE
    card = re.search(r"\.card\{[^}]*\}", STYLE).group(0)
    assert "overflow-x:auto" in card, "широкая таблица растянет всю страницу"
    assert re.search(r"td\{[^}]*break-word", STYLE)


# ── скрипт темы ──────────────────────────────────────────────────────────────
def test_theme_script_remembers_choice():
    script = theme.theme_script()
    assert script.strip(), "скрипт пустой"
    assert "localStorage" in script
    assert 'data-theme' in script
    assert "panel-theme" in script, "нет ключа для выбора темы"
    assert "prefers-color-scheme: light" in script
    assert ".theme-toggle" in script, "нет кнопки переключения темы"


def test_theme_script_marks_search_rows():
    script = theme.theme_script()
    assert "is-found" in script, "нет подсветки найденных строк"
    assert ".gsearch input" in script
    assert "classList.toggle" in script


def test_theme_script_is_bare_javascript():
    """Вставляется внутрь <script>: своих тегов и внешних загрузок быть не должно."""
    script = theme.theme_script()
    assert "<script" not in script and "</script>" not in script
    for bad in ("http://", "https://", "src=", "import ", "require(", "fetch("):
        assert bad not in script, f"в скрипте есть {bad}"
    assert "<svg" in script, "кнопка темы осталась без иконки"


def test_theme_script_is_reproducible():
    """Скрипт собирается заново на каждый вызов: правка разметки иконок
    подхватывается, а не остаётся в старой строке."""
    first, second = theme.theme_script(), theme.theme_script()
    assert isinstance(first, str) and first == second and first.strip()
