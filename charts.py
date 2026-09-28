"""Диаграммы для панели: чистые SVG без внешних библиотек и без JavaScript.

Почему так: панель открывается в обычном браузере, работает офлайн и не должна
тянуть за собой Chart.js. Данные приходят уже посчитанными из репозитория,
здесь только геометрия: столбики, кольцо, полоски.

Цвет задаётся ролью палитры, а не «голым» hex: в разметке везде
``fill="currentColor"``/``stroke="currentColor"``, а оттенок подставляет класс
``chart-c-<роль>``. Значения ролей живут в CSS-переменных панели, поэтому смена
темы меняет графики вместе со страницей. Правила для ролей возвращает
``palette_css()``, и его нужно вставить в стиль панели (например, рядом с
``STYLESHEET`` в ``webpanel.py``) - без него все фигуры остаются одноцветными.

Интерактив и анимация:

* у столбиков и кольца внутри фигур лежит ``<title>`` - браузер показывает
  подсказку «26.09 - 12 обращений», не нужно ни JavaScript, ни обработчиков;
* у полос в HTML подсказка живёт в атрибуте ``title`` (``<title>`` в обычном
  HTML не работает, а в SVG работает);
* появление: ``@keyframes chart-grow`` растит полосу от нуля. Чтобы при смене
  периода анимация начиналась заново, у графика есть атрибут-маркер
  ``data-reload="chart-N"`` (``RELOAD_MARKER`` и ``bump()``): пока значение не
  изменилось, браузер не перерисовывает график.

Все функции возвращают готовый HTML-строку и безопасны для вставки: значения
подставляются только числами, подписи экранируются.
"""
from utils import as_str

# ── палитра: роль → значение по умолчанию ────────────────────────────────────
# Значения - «середина» между светлой и тёмной темами: на белой карточке и на
# почти чёрной читаются одинаково. Панель переопределяет их своими переменными
# --chart-<роль> (или токенами --chart-a…--chart-h из panel_theme), поэтому тему
# можно задать где угодно - графики последуют за ней.
PALETTE: dict[str, str] = {
    "line": "#4d8ef7",      # линия мини-графика
    "line_2": "#2fb27a",    # вторая линия и точка «последний день»
    "bar": "#4d8ef7",       # основные столбики и полосы
    "bar_2": "#2fb27a",     # вложенный ряд («завершено»)
    "bar_3": "#8b6ff0",     # дополнительный ряд: фиолетовый
    "bar_4": "#2fb6c9",     # дополнительный ряд: бирюзовый
    "grid": "#8e9aad",      # линии сетки
    "axis": "#6b7789",      # подписи осей
    "donut_1": "#4d8ef7",
    "donut_2": "#2fb27a",
    "donut_3": "#e0a02f",
    "donut_4": "#2fb6c9",
    "donut_5": "#8b6ff0",
    "good": "#2fb27a",      # успех, «завершено»
    "warn": "#e0a02f",      # внимание, «в работе»
    "bad": "#e05a4f",       # отказ, просрочка
}

# Роль → токен панели: второе звено цепочки цвета в palette_css(). Благодаря
# ему роли сразу опираются на --chart-a…--chart-h, --ok/--warn/--bad темы.
THEME_VAR: dict[str, str] = {
    "line": "--chart-a", "line_2": "--chart-b",
    "bar": "--chart-a", "bar_2": "--chart-b",
    "bar_3": "--chart-e", "bar_4": "--chart-f",
    "grid": "--chart-grid", "axis": "--chart-axis",
    "donut_1": "--chart-a", "donut_2": "--chart-b", "donut_3": "--chart-c",
    "donut_4": "--chart-f", "donut_5": "--chart-e",
    "good": "--ok", "warn": "--warn", "bad": "--bad",
}

# Цвета старой панели → роль: вызовы вида color="#8b5cf6" продолжают работать.
COLOR_ROLE: dict[str, str] = {
    "#3b82f6": "bar", "#22c55e": "bar_2", "#f59e0b": "warn", "#ef4444": "bad",
    "#8b5cf6": "bar_3", "#06b6d4": "bar_4", "#64748b": "axis",
}

# Статус обращения → роль: кольцо статусов остаётся цветным в любой теме.
STATUS_ROLE: dict[str, str] = {
    "new": "line", "accepted": "donut_5", "in_progress": "warn",
    "ready": "donut_4", "completed": "good", "rejected": "bad",
}

# Доли кольца без известного статуса: по кругу.
DONUT_ROLES: tuple[str, ...] = ("donut_1", "donut_2", "donut_3", "donut_4", "donut_5")

# Маркер перерисовки: значение вида «chart-30-7» на каждой отрисовке новое, и
# браузер заново запускает CSS-анимацию появления.
RELOAD_MARKER = "chart"
_RENDER: dict = {"n": 0}  # счётчик отрисовок, его двигает bump()

# Пустое состояние: коротко, по-русски и с подсказкой, что делать дальше.
EMPTY_TEXT = "Пока нет данных"
EMPTY_HINT = "График заполнится, как только появятся обращения."
EMPTY_PERIOD_TEXT = "Нет данных за период"
EMPTY_PERIOD_HINT = "Попробуйте взять период побольше или дождаться новых обращений."

# Скорость анимации графиков: сначала панель, потом наш запасной вариант.
_MOTION = "var(--chart-motion,var(--motion-slow,420ms))"
_EASE = "var(--ease-out,cubic-bezier(.16,.84,.44,1))"
_STEP = "var(--motion-fast,120ms) var(--ease-standard,cubic-bezier(.2,.7,.3,1))"


def esc(text) -> str:
    """Мини-экранирование: панель уже escape'ит текст, здесь подстраховка."""
    return (as_str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _nice_max(value: float) -> float:
    """Округляет верх шкалы до «человеческого» числа: 3, 5, 10, 20, 50, 100..."""
    if value <= 0:
        return 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        if value <= step:
            return float(step)
    return float(value)


def plural(number: int, one: str, few: str, many: str) -> str:
    """«1 обращение», «2 обращения», «12 обращений» - по-русски, без библиотек."""
    number = abs(int(number))
    tail, head = number % 10, number % 100
    if tail == 1 and head != 11:
        return one
    if 2 <= tail <= 4 and not 12 <= head <= 14:
        return few
    return many


def count_text(number: int) -> str:
    """Величина с существительным: «12 обращений»."""
    return f"{int(number)} {plural(number, 'обращение', 'обращения', 'обращений')}"


def short_label(text: str, limit: int = 6) -> str:
    """Короткая подпись: «26.09» вместо «2026-09-26», длинные ФИО обрезаются."""
    text = as_str(text)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}.{text[5:7]}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── роли палитры ─────────────────────────────────────────────────────────────
def css_var(role: str) -> str:
    """Имя CSS-переменной роли: ``donut_1`` → ``--chart-donut-1``."""
    return f"--chart-{as_str(role).strip().replace('_', '-')}"


def role_for(color: str, default: str = "bar") -> str:
    """Имя роли палитры для цвета.

    Понимает роль («bar_2»), любой цвет из ``PALETTE`` и цвета старой панели.
    Незнакомый цвет не выбрасываем: отдаём роль по умолчанию, а сам цвет уходит
    в style элемента (см. ``paint``), чтобы график не потерял выбранный цвет.
    """
    value = as_str(color).strip().lower()
    if not value:
        return default
    if value in PALETTE:
        return value
    if value in COLOR_ROLE:
        return COLOR_ROLE[value]
    for role, own in PALETTE.items():
        if own.lower() == value:
            return role
    return default


def _known_color(color: str) -> bool:
    """Цвет из палитры или из старой панели - его можно описать ролью."""
    value = as_str(color).strip().lower()
    if not value or value in PALETTE or value in COLOR_ROLE:
        return True
    return any(own.lower() == value for own in PALETTE.values())


def paint(shape: str, color: str = "", default: str = "bar", delay: int = 0,
          extra: str = "") -> str:
    """Атрибуты фигуры: класс роли палитры и стиль, если он нужен.

    В разметке остаётся только ``currentColor``: известный цвет живёт в
    CSS-переменной роли и меняется вместе с темой, незнакомый - в style, и тогда
    это осознанный выход за тему. ``delay`` задаёт номер фигуры для анимации
    (полосы появляются по очереди), ``extra`` - свои CSS-декларации.
    """
    value = as_str(color).strip()
    role = role_for(value, default)
    css = [f"--chart-i:{int(delay)}"] if delay else []
    if value and not _known_color(value):
        css.insert(0, f"color:{esc(value)}")
    if extra:
        css.append(extra)
    style = f' style="{";".join(css)}"' if css else ""
    classes = " ".join(part for part in (as_str(shape).strip(), f"chart-c-{role}") if part)
    return f'class="{classes}"{style}'


def swatch(color: str = "", default: str = "bar") -> str:
    """Квадратик в легенде: тот же цвет, что у полосы, и без своих стилей."""
    return f"<i {paint('', color, default)}></i>"


# ── перерисовка и анимация ───────────────────────────────────────────────────
def bump(data: dict, key: str) -> dict:
    """Поднимает счётчик в словаре: значение меняется - браузер перерисовывает график.

    Нужно там, где данные те же, а перерисовать всё равно нужно: смена периода,
    обновление без перезагрузки страницы. Число растёт на единицу, строка вида
    «r-7» - на шаг («r-8»), пустое или нечисловое значение становится 1.
    Возвращает тот же словарь, чтобы вызывать в цепочке.
    """
    text = as_str(data.get(key, 0)).strip()
    head, dash, tail = text.rpartition("-")
    if dash and tail.isdigit():
        data[key] = f"{head}{dash}{int(tail) + 1}"
    elif text.lstrip("+-").isdigit():
        data[key] = int(text) + 1
    else:
        data[key] = 1
    return data


def reload_attr(reload="") -> str:
    """Атрибут-маркер перерисовки: значение всегда новое, поэтому анимация заново.

    Маркер состоит из RELOAD_MARKER, периода (если его передали) и счётчика
    отрисовок: пока строка не изменилась, браузер считает, что график тот же, и
    не перерисовывает его - значит, не перезапускает и анимацию появления.
    """
    seed = as_str(reload).strip()
    prefix = f"{RELOAD_MARKER}-{seed}-" if seed else f"{RELOAD_MARKER}-"
    return f' data-reload="{prefix}{bump(_RENDER, "n")["n"]}"'


def describe(title: str, kind: str, extra: str = "") -> str:
    """Описание графика для скринридера: «Обращения по дням: Столбиковая диаграмма, 30 шт.»."""
    text = f"{as_str(title).strip() or kind}: {kind}"
    return esc(f"{text}, {extra}" if extra else text)


def _svg_head(box: str, cls: str, label: str, reload="") -> str:
    """Начало ``<svg>``: role="img", русский ``<title>`` и маркер перерисовки."""
    return (f'<svg viewBox="{box}" class="{cls}" role="img"{reload_attr(reload)}>'
            f"<title>{label}</title>")


# ── диаграммы ────────────────────────────────────────────────────────────────
def bar_chart(rows: list, value_key: str, label_key: str, title: str = "",
              second_key: str = "", height: int = 150, color: str = "",
              *, hint: str = "", period: str = "", reload="") -> str:
    """Столбики: основной ряд + необязательный второй (например, «завершено»).

    Нет данных - не рисуем пустую сетку, а прямо говорим об этом. Цвет столбиков
    задаётся ролью ``color`` (по умолчанию ``bar``), второй ряд - ``bar_2``.
    """
    data = [r for r in rows if int(r.get(value_key) or 0) >= 0]
    values = [int(r.get(value_key) or 0) for r in data]
    if not data or not any(values):
        return _empty_state(title, hint, period)
    top = _nice_max(max(values))
    width, pad, bottom, left = 640, 8, 26, 34
    plot_h = height - bottom - 10
    count = len(data)
    slot = (width - left - pad) / max(count, 1)
    bar_w = max(3, min(28, slot * 0.62))
    label = describe(title, "Столбиковая диаграмма",
                     f"{count} {plural(count, 'столбик', 'столбика', 'столбиков')}, "
                     f"максимум {max(values)}")
    parts = [_svg_head(f"0 0 {width} {height}", "chart", label, reload)]
    # сетка: декоративная, поэтому отдаём её aria-hidden, а подписи оси - рядом
    grid, ticks = [], []
    for step in range(0, 4):
        value = top * step / 3
        y = 10 + plot_h - plot_h * step / 3
        grid.append(f'<line class="grid-line" x1="{left}" y1="{y:.1f}" '
                    f'x2="{width - pad}" y2="{y:.1f}"/>')
        ticks.append(f'<text class="axis chart-c-axis" x="{left - 6}" y="{y + 4:.1f}" '
                     f'fill="currentColor" text-anchor="end">{int(value)}</text>')
    parts.append('<g class="chart-grid chart-c-grid" fill="none" stroke="currentColor" '
                 f'aria-hidden="true">{"".join(grid)}</g>{"".join(ticks)}')
    for index, (row, value) in enumerate(zip(data, values)):
        bar_h = plot_h * (value / top)
        x = left + slot * index + (slot - bar_w) / 2
        y = 10 + plot_h - bar_h
        shown = short_label(row.get(label_key, ""))
        step = min(index, 24)  # больше 24 задержек - уже не «по очереди», а долго
        if second_key:
            second = int(row.get(second_key) or 0)
            if second:
                inner_h = plot_h * (second / top)
                tip = f"{shown} — завершено {count_text(second)}"
                parts.append(
                    f'<rect {paint("chart-bar chart-bar--inner", "", "bar_2", step)} '
                    f'x="{x:.1f}" y="{10 + plot_h - inner_h:.1f}" width="{bar_w:.1f}" '
                    f'height="{inner_h:.1f}" rx="2" fill="currentColor">'
                    f"<title>{esc(tip)}</title></rect>")
        tip = f"{shown} — {count_text(value)}"
        parts.append(
            f'<rect {paint("chart-bar", color, "bar", step)} x="{x:.1f}" y="{y:.1f}" '
            f'width="{bar_w:.1f}" height="{bar_h:.1f}" rx="3" fill="currentColor">'
            f"<title>{esc(tip)}</title></rect>")
        if count <= 16 or index % 2 == 0:
            parts.append(f'<text class="axis chart-c-axis" x="{x + bar_w / 2:.1f}" '
                         f'y="{height - 8}" fill="currentColor" text-anchor="middle">'
                         f"{esc(shown)}</text>")
    parts.append("</svg>")
    legend = ""
    if second_key:
        legend = ('<div class="legend">'
                  f'<span>{swatch(color, "bar")}всего</span>'
                  f'<span>{swatch("", "bar_2")}завершено</span></div>')
    return (f'<figure class="chart-box chart-anim"><figcaption>{esc(title)}</figcaption>'
            f'{"".join(parts)}{legend}</figure>')


def _donut_role(row: dict, index: int, role_key: str = "status") -> str:
    """Роль доли кольца: у известного статуса своя, остальные - по кругу."""
    return STATUS_ROLE.get(as_str(row.get(role_key)).strip()) or DONUT_ROLES[index % len(DONUT_ROLES)]


def donut(rows: list, value_key: str, label_key: str, title: str = "", size: int = 168,
          *, role_key: str = "status", hint: str = "", period: str = "", reload="") -> str:
    """Кольцо с долями - для составных величин (статусы, разделы)."""
    data = [(r, int(r.get(value_key) or 0)) for r in rows]
    data = [(r, v) for r, v in data if v > 0]
    if not data:
        return _empty_state(title, hint, period)
    total = sum(v for _, v in data)
    radius, ring = size / 2 - 8, 26
    circumference = 2 * 3.141592653589793 * radius
    parts = [_svg_head(f"0 0 {size} {size}", "chart donut",
                       describe(title, "Кольцевая диаграмма", f"всего {total}"), reload)]
    offset = 0.0
    legend = []
    for index, (row, value) in enumerate(data):
        length = circumference * value / total
        role = _donut_role(row, index, role_key)
        tip = f"{as_str(row.get(label_key))}: {value} ({round(100 * value / total)}%)"
        parts.append(
            f'<circle {paint("chart-donut-seg", "", role)} cx="{size / 2}" cy="{size / 2}" '
            f'r="{radius:.1f}" fill="none" stroke="currentColor" stroke-width="{ring}" '
            f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {size / 2} {size / 2})">'
            f"<title>{esc(tip)}</title></circle>")
        legend.append(f'<li>{swatch("", role)}<span>{esc(row.get(label_key))}</span>'
                      f"<b>{value}</b></li>")
        offset += length
    parts.append(f'<text class="donut-total" x="{size / 2}" y="{size / 2 - 2}" '
                 f'fill="currentColor" text-anchor="middle">{total}</text>')
    parts.append(f'<text class="donut-sub" x="{size / 2}" y="{size / 2 + 16}" '
                 f'fill="currentColor" text-anchor="middle">всего</text>')
    parts.append("</svg>")
    return (f'<figure class="chart-box chart-anim"><figcaption>{esc(title)}</figcaption>'
            f'<div class="donut-wrap">{"".join(parts)}'
            f'<ul class="legend-list">{"".join(legend)}</ul></div></figure>')


def bars(rows: list, value_key: str, label_key: str, title: str = "", color: str = "",
         suffix: str = "", *, hint: str = "", period: str = "", reload="") -> str:
    """Горизонтальные полоски: нагрузка по сотрудникам, студенты по группам."""
    data = [(r, int(r.get(value_key) or 0)) for r in rows]
    data = [(r, v) for r, v in data if v > 0]
    if not data:
        return _empty_state(title, hint, period)
    top = max(v for _, v in data) or 1
    parts = [f'<figure class="chart-box chart-anim"><figcaption>{esc(title)}</figcaption>'
             f'<ul class="hbar-list"{reload_attr(reload)}>']
    for index, (row, value) in enumerate(data):
        raw = as_str(row.get(label_key, ""))
        width = max(2, round(100 * value / top))
        tail = f"{value} {suffix}" if suffix else count_text(value)
        tip = esc(f"{raw} — {tail}")
        parts.append(
            f'<li><span class="hbar-label" title="{esc(raw)}">{esc(short_label(raw, 28))}</span>'
            f'<span class="hbar-track" title="{tip}">'
            f'<i {paint("hbar-fill", color, "bar", min(index, 20), f"width:{width}%")}></i></span>'
            f"<b>{value}{esc(suffix)}</b></li>")
    parts.append("</ul></figure>")
    return "".join(parts)


def sparkline(values: list, title: str = "", color: str = "", height: int = 46,
              *, hint: str = "", period: str = "", reload="") -> str:
    """Мини-график для карточки: одна линия без осей."""
    values = [int(v or 0) for v in values]
    if len(values) < 2:
        return _empty_state(title, hint, period)
    width, pad = 220, 4
    top = _nice_max(max(values))
    step = (width - 2 * pad) / (len(values) - 1)
    points = " ".join(
        f"{pad + step * index:.1f},{height - pad - (height - 2 * pad) * value / top:.1f}"
        for index, value in enumerate(values))
    area = f"{pad},{height - pad} {points} {width - pad},{height - pad}"
    last_x = pad + step * (len(values) - 1)
    last_y = height - pad - (height - 2 * pad) * values[-1] / top
    head = _svg_head(f"0 0 {width} {height}", "chart",
                     describe(title, "Мини-график", f"последний день: {count_text(values[-1])}"),
                     reload)
    return (f'<figure class="chart-box spark chart-anim"><figcaption>{esc(title)}</figcaption>'
            f"{head}"
            f'<polygon {paint("chart-spark-area", color, "line")} points="{area}" '
            f'fill="currentColor" opacity="0.14"/>'
            f'<polyline {paint("chart-spark-line", color, "line")} points="{points}" '
            f'fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round"/>'
            f'<circle {paint("chart-spark-dot", "", "line_2")} cx="{last_x:.1f}" '
            f'cy="{last_y:.1f}" r="2.6" fill="currentColor">'
            f"<title>{esc(f'последний день: {count_text(values[-1])}')}</title>"
            f"</circle></svg></figure>")


# ── пустое состояние ─────────────────────────────────────────────────────────
def _empty_box(title: str, head: str, hint: str) -> str:
    """Рамка пустого состояния: что не так и что делать дальше."""
    return (f'<figure class="chart-box"><figcaption>{esc(title)}</figcaption>'
            f'<div class="chart-empty" role="status">{esc(head)}'
            f'<span class="chart-empty-hint">{esc(hint)}</span></div></figure>')


def empty(title: str = "", hint: str = "") -> str:
    """Заглушка вместо пустой диаграммы: так видно, что данных просто нет."""
    return _empty_box(title, EMPTY_TEXT, hint or EMPTY_HINT)


def empty_period(title: str = "", period: str = "", hint: str = "") -> str:
    """Пустое состояние с упором на период: подсказка, что делать сис-админу."""
    return _empty_box(title, f"{EMPTY_PERIOD_TEXT} {as_str(period).strip()}".strip(),
                      hint or EMPTY_PERIOD_HINT)


def _empty_state(title: str, hint: str, period: str) -> str:
    """Пустое состояние диаграммы: с периодом или без него."""
    return empty_period(title, period, hint) if period else empty(title, hint)


# ── стили ────────────────────────────────────────────────────────────────────
def palette_css() -> str:
    """CSS для ролей палитры: вставить в стиль панели рядом с блоком ``--chart-*``.

    Правило простое: цвет элемента задаёт класс ``chart-c-<роль>``, а сама фигура
    рисуется через ``currentColor``. Значение роли берётся из цепочки
    ``--chart-<роль>`` → токен панели (``--chart-a``…``--chart-h``, ``--ok``) →
    значение из ``PALETTE``. Поэтому переопределить цвет графика можно и
    переменной темы, и точечным правилом класса - без правок разметки.

    Здесь же анимация появления (``chart-grow`` выращивает полосу от нуля) и её
    отключение для тех, кто просил браузер не двигать картинки.
    """
    roles = []
    for role, value in PALETTE.items():
        own, theme = css_var(role), THEME_VAR.get(role, "")
        chain = f"var({own}, {value})" if theme in ("", own) else f"var({own}, var({theme}, {value}))"
        roles.append(f".chart-c-{role}{{color:{chain}}}")
    return "\n".join([
        "/* ── графики: роли палитры вместо «голых» цветов ─────────────────────── */",
        *roles,
        "",
        "/* фигуры: цвет приходит из currentColor, оттенок - из класса роли */",
        ".chart .chart-bar{fill:currentColor;transform-box:fill-box;transform-origin:bottom}",
        ".chart .chart-bar--inner{fill:currentColor;opacity:.95}",
        ".chart .grid-line{stroke:currentColor;stroke-width:1}",
        ".chart .axis{fill:currentColor}",
        ".chart .chart-donut-seg{fill:none;stroke:currentColor}",
        ".chart .chart-spark-area{fill:currentColor;opacity:.14}",
        (".chart .chart-spark-line{fill:none;stroke:currentColor;stroke-width:2;"
         "stroke-linejoin:round;stroke-linecap:round}"),
        ".chart .chart-spark-dot{fill:currentColor}",
        ".donut-total{color:var(--ink,#e9eff8)}",
        ".donut-sub{color:var(--mut,#93a5be)}",
        (".chart-box .legend i,.chart-box .legend-list i{background:currentColor;"
         "border-radius:3px}"),
        ".hbar-track{background:var(--chart-track,#e6ebf3)}",
        ".hbar-fill{display:block;height:100%;border-radius:5px;background:currentColor}",
        (".chart-empty-hint{display:block;margin-top:4px;"
         "font-size:var(--text-xs,12px);opacity:.75}"),
        "",
        "/* наведение: полоса слегка подсвечивается */",
        (f".chart .chart-bar{{transition:opacity {_STEP},transform {_STEP}}}"),
        ".chart .chart-bar:hover{opacity:.82;filter:brightness(1.12)}",
        (f".chart .chart-donut-seg{{transition:opacity {_STEP},filter {_STEP}}}"),
        ".chart .chart-donut-seg:hover{opacity:.72;filter:brightness(1.1)}",
        f".hbar-fill{{transition:opacity {_STEP},transform {_STEP}}}",
        ".hbar-track:hover .hbar-fill{opacity:.85;filter:brightness(1.1)}",
        "",
        "/* появление: полосы вырастают от нуля, линия и кольцо проявляются */",
        "@keyframes chart-grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}",
        "@keyframes chart-grow-x{from{transform:scaleX(0)}to{transform:scaleX(1)}}",
        "@keyframes chart-fade{from{opacity:0}to{opacity:1}}",
        (f".chart-anim .chart-bar{{animation:chart-grow {_MOTION} {_EASE} both;"
         "animation-delay:calc(var(--chart-i,0) * 22ms)}"),
        (f".chart-anim .hbar-fill{{transform-origin:left center;"
         f"animation:chart-grow-x {_MOTION} {_EASE} both;"
         "animation-delay:calc(var(--chart-i,0) * 28ms)}"),
        (".chart-anim .chart-donut,.chart-anim .chart-spark-area,"
         f".chart-anim .chart-spark-line,.chart-anim .chart-spark-dot"
         f"{{animation:chart-fade {_MOTION} {_EASE} both}}"),
        "@media (prefers-reduced-motion: reduce){",
        ("  .chart-anim .chart-bar,.chart-anim .hbar-fill,.chart-anim .chart-donut,"
         ".chart-anim .chart-spark-area,.chart-anim .chart-spark-line,"
         ".chart-anim .chart-spark-dot{animation:none!important}"),
        "  .chart .chart-bar,.chart .chart-donut-seg,.hbar-fill{transition:none!important}",
        "}",
    ])
