"""Диаграммы для панели: чистые SVG без внешних библиотек и без JavaScript.

Почему так: панель открывается в обычном браузере, работает офлайн и не должна
тянуть за собой Chart.js. Данные приходят уже посчитанными из репозитория,
здесь только геометрия: столбики, кольцо, полоски.

Все функции возвращают готовый HTML-строку и безопасны для вставки: значения
подставляются только числами, подписи экранируются.
"""
from utils import as_str

PALETTE = ("#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#64748b")
STATUS_COLOR = {
    "new": "#3b82f6", "accepted": "#8b5cf6", "in_progress": "#f59e0b",
    "ready": "#06b6d4", "completed": "#22c55e", "rejected": "#ef4444",
}


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


def bar_chart(rows: list, value_key: str, label_key: str, title: str = "",
              second_key: str = "", height: int = 150, color: str = PALETTE[0]) -> str:
    """Столбики: основной ряд + необязательный второй (например, «завершено»).

    Нет данных - не рисуем пустую сетку, а прямо говорим об этом.
    """
    data = [r for r in rows if int(r.get(value_key) or 0) >= 0]
    if not data or not any(int(r.get(value_key) or 0) for r in data):
        return empty(title)
    top = _nice_max(max(int(r.get(value_key) or 0) for r in data))
    width, pad, bottom, left = 640, 8, 26, 34
    plot_h = height - bottom - 10
    count = len(data)
    slot = (width - left - pad) / max(count, 1)
    bar_w = max(3, min(28, slot * 0.62))
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="{esc(title)}">']
    # сетка и подписи оси Y
    for step in range(0, 4):
        value = top * step / 3
        y = 10 + plot_h - plot_h * step / 3
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" '
                     f'class="grid-line"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">'
                     f'{int(value)}</text>')
    for index, row in enumerate(data):
        value = int(row.get(value_key) or 0)
        bar_h = plot_h * (value / top)
        x = left + slot * index + (slot - bar_w) / 2
        y = 10 + plot_h - bar_h
        label = esc(row.get(label_key, ""))
        tip = f"{label}: {value}"
        if second_key:
            second = int(row.get(second_key) or 0)
            if second:
                inner_h = plot_h * (second / top)
                parts.append(f'<rect x="{x:.1f}" y="{10 + plot_h - inner_h:.1f}" width="{bar_w:.1f}" '
                             f'height="{inner_h:.1f}" rx="2" fill="#22c55e"><title>{esc(label)}: '
                             f'завершено {second}</title></rect>')
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                     f'rx="3" fill="{color}"><title>{esc(tip)}</title></rect>')
        if count <= 16 or index % 2 == 0:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" class="axis" '
                         f'text-anchor="middle">{esc(short_label(label))}</text>')
    parts.append("</svg>")
    legend = ""
    if second_key:
        legend = ('<div class="legend"><span><i style="background:%s"></i>всего</span>'
                  '<span><i style="background:#22c55e"></i>завершено</span></div>' % color)
    return f'<figure class="chart-box"><figcaption>{esc(title)}</figcaption>{"".join(parts)}{legend}</figure>'


def short_label(text: str, limit: int = 6) -> str:
    """Короткая подпись: «26.09» вместо «2026-09-26», длинные ФИО обрезаются."""
    text = as_str(text)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}.{text[5:7]}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def donut(rows: list, value_key: str, label_key: str, title: str = "", size: int = 168) -> str:
    """Кольцо с долями - для составных величин (статусы, разделы)."""
    data = [(r, int(r.get(value_key) or 0)) for r in rows]
    data = [(r, v) for r, v in data if v > 0]
    if not data:
        return empty(title)
    total = sum(v for _, v in data)
    radius, width_px = size / 2 - 8, 26
    circumference = 2 * 3.141592653589793 * radius
    parts = [f'<svg viewBox="0 0 {size} {size}" class="chart donut" role="img" '
             f'aria-label="{esc(title)}">']
    offset = 0.0
    for index, (row, value) in enumerate(data):
        length = circumference * value / total
        color = STATUS_COLOR.get(as_str(row.get(label_key))) or PALETTE[index % len(PALETTE)]
        parts.append(
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{radius:.1f}" fill="none" '
            f'stroke="{color}" stroke-width="{width_px}" '
            f'stroke-dasharray="{length:.2f} {circumference - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {size / 2} {size / 2})">'
            f'<title>{esc(row.get(label_key))}: {value} ({round(100 * value / total)}%)</title></circle>'
        )
        offset += length
    parts.append(f'<text x="{size / 2}" y="{size / 2 - 2}" class="donut-total" text-anchor="middle">'
                 f'{total}</text>')
    parts.append(f'<text x="{size / 2}" y="{size / 2 + 16}" class="donut-sub" text-anchor="middle">'
                 f'всего</text>')
    parts.append("</svg>")
    legend = "".join(
        f'<li><i style="background:'
        f'{STATUS_COLOR.get(as_str(r.get(label_key))) or PALETTE[i % len(PALETTE)]}"></i>'
        f'<span>{esc(r.get(label_key))}</span><b>{v}</b></li>'
        for i, (r, v) in enumerate(data)
    )
    return (f'<figure class="chart-box"><figcaption>{esc(title)}</figcaption>'
            f'<div class="donut-wrap">{"".join(parts)}<ul class="legend-list">{legend}</ul></div></figure>')


def bars(rows: list, value_key: str, label_key: str, title: str = "", color: str = PALETTE[0],
         suffix: str = "") -> str:
    """Горизонтальные полоски: нагрузка по сотрудникам, студенты по группам."""
    data = [(r, int(r.get(value_key) or 0)) for r in rows]
    data = [(r, v) for r, v in data if v > 0]
    if not data:
        return empty(title)
    top = max(v for _, v in data) or 1
    parts = [f'<figure class="chart-box"><figcaption>{esc(title)}</figcaption"><ul class="hbar-list">']
    for row, value in data:
        label = esc(row.get(label_key, ""))
        width = max(2, round(100 * value / top))
        parts.append(
            f'<li><span class="hbar-label" title="{label}">{esc(short_label(label, 28))}</span>'
            f'<span class="hbar-track"><i style="width:{width}%;background:{color}"></i></span>'
            f'<b>{value}{esc(suffix)}</b></li>'
        )
    parts.append("</ul></figure>")
    return "".join(parts)


def sparkline(values: list, title: str = "", color: str = PALETTE[0], height: int = 46) -> str:
    """Мини-график для карточки: одна линия без осей."""
    values = [int(v or 0) for v in values]
    if len(values) < 2:
        return empty(title)
    width, pad = 220, 4
    top = _nice_max(max(values))
    step = (width - 2 * pad) / (len(values) - 1)
    points = " ".join(
        f"{pad + step * index:.1f},{height - pad - (height - 2 * pad) * value / top:.1f}"
        for index, value in enumerate(values))
    area = f"{pad},{height - pad} {points} {width - pad},{height - pad}"
    return (f'<figure class="chart-box spark"><figcaption>{esc(title)}</figcaption>'
            f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">'
            f'<polygon points="{area}" fill="{color}" opacity="0.12"/>'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round"/></svg></figure>')


def empty(title: str) -> str:
    """Заглушка вместо пустой диаграммы: так видно, что данных просто нет."""
    return (f'<figure class="chart-box"><figcaption>{esc(title)}</figcaption>'
            f'<div class="chart-empty">Пока нет данных</div></figure>')
