"""Оформление панели сис-админа: токены, иконки, общий CSS и переключатель темы.

Зачем модуль: раньше стили лежали константой ``STYLE`` прямо в ``webpanel.py``,
и в них встречались «магические» цвета - из-за этого тёмная и светлая темы
расходились между собой (то белое, то чёрное), а эмодзи в меню выглядели
по-разному в разных браузерах. Здесь всё собрано в одном месте:

* ``TOKENS`` - единственный источник значений оформления. Ключи с префиксом
  ``dark.`` - тёмная тема (она же по умолчанию), ``light.`` - светлая,
  ``base.`` - то, что не зависит от темы: радиусы, шрифты, размеры, интервалы,
  скорости анимаций и ширины колонок. Голых цветов в CSS нет нигде: правила
  ссылаются только на ``var(--имя)``.
* ``ICONS`` - набор SVG-иконок в одном стиле (24x24, ``currentColor``,
  обводка 1.75, скруглённые концы, без заливки) вместо эмодзи.
* ``STYLESHEET`` - весь CSS панели на токенах, тёмная тема по умолчанию.
* ``theme_script()`` - маленький JS без зависимостей: переключает тему,
  запоминает выбор в ``localStorage`` и подсвечивает найденные строки.
* ``icon()`` - готовая ``<svg>`` по имени; на неизвестное имя не падает.

Три маленьких скрипта дополняют ``theme_script()`` и живут рядом с ним:
``hotkeys_script()`` (клавиши и палитра разделов по Ctrl+K), ``actions_script()``
(копирование значения по клику и сохранённые фильтры рабочего места) и общий
``panel_toast_js()`` - всплывающее сообщение, которым они оба пользуются.

Как подключить (в ``webpanel.py``):

    from panel_theme import (ICON_NAMES_BY_PATH, ICONS, STYLESHEET,
                             icon, theme_script)

    STYLE = STYLESHEET
    NAV_ICONS = ICON_NAMES_BY_PATH          # было: словарь с эмодзи

В ``page()`` иконка пункта меню вставляется так:

    f'<span class="nav-ico" aria-hidden="true">{icon(NAV_ICONS.get(path, "dot"), 18)}</span>'

Скрипт ставится в конец ``<body>``, а на ``<html>`` добавляется
``data-theme="dark"``, чтобы тема не мигала до загрузки скрипта.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

# ── токены оформления ────────────────────────────────────────────────────────
# Тёмная тема - основная: панель чаще открывают ночью, и тёмный фон не бьёт
# по глазам. Значения подобраны так, чтобы приглушённый текст (--mut) читался
# на тёмной поверхности: контраст около 7:1, а не «серый по серому».
TOKENS: dict[str, str] = {
    # ── тёмная тема (по умолчанию) ─────────────────────────────────────────
    "dark.bg": "#0c1017",                 # фон страницы
    "dark.bg-deep": "#080b11",            # подвал, самый глубокий слой
    "dark.surface": "#141a25",            # карточка, панель меню
    "dark.surface-raised": "#1b2432",     # наведение, плашки
    "dark.surface-sunken": "#0f151f",     # поля ввода, шапка таблицы
    "dark.line": "#26313f",               # граница
    "dark.line-soft": "#1c2431",          # мягкая граница: строки таблицы
    "dark.line-strong": "#3a4860",        # граница в фокусе
    "dark.ink": "#e9eff8",                # основной текст
    "dark.ink-soft": "#c2cfe2",           # вторичный текст
    "dark.mut": "#93a5be",                # подписи, даты
    "dark.acc": "#5d9bff",                # акцент: ссылки, активный пункт
    "dark.acc-ink": "#06101f",            # текст на акценте
    "dark.acc-soft": "#16243d",           # мягкая подложка акцента
    "dark.acc-line": "#2e4d80",           # граница мягкой подложки
    "dark.ok": "#3ecf8e",
    "dark.ok-ink": "#04170e",
    "dark.ok-soft": "#0f2a1f",
    "dark.ok-line": "#1e4d37",
    "dark.bad": "#ff7b72",
    "dark.bad-ink": "#1d0a08",
    "dark.bad-soft": "#2e1719",
    "dark.bad-line": "#5b2b2e",
    "dark.warn": "#f0c05a",
    "dark.warn-ink": "#1d1405",
    "dark.warn-soft": "#2b2211",
    "dark.warn-line": "#54421c",
    "dark.surface-head": "#111a28",       # шапка страницы и таблицы
    "dark.head-a": "#0f1725",             # градиент шапки: начало
    "dark.head-b": "#17233a",             # градиент шапки: конец
    "dark.head-ink": "#e9eff8",
    "dark.head-mut": "#a2b3cc",
    "dark.head-field": "rgba(255,255,255,.10)",
    "dark.head-line": "#2c3a52",
    "dark.code-bg": "#070b11",            # журнал: тёмный блок в обеих темах
    "dark.code-ink": "#d3e2f6",
    "dark.chart-a": "#6f9dff",
    "dark.chart-b": "#3ecf8e",
    "dark.chart-c": "#f0c05a",
    "dark.chart-d": "#ff7b72",
    "dark.chart-e": "#b18cff",
    "dark.chart-f": "#4fd6e8",
    "dark.chart-g": "#ff9ecb",
    "dark.chart-h": "#8fa0b8",
    "dark.chart-grid": "#202b3a",
    "dark.chart-axis": "#8ea0ba",
    "dark.chart-track": "#1a2330",
    "dark.skel-a": "#171f2b",
    "dark.skel-b": "#232e3f",
    "dark.sel-bg": "#2b3f63",             # выделение текста мышью
    "dark.glow": "rgba(93,155,255,.20)",   # подсветка найденной строки
    "dark.overlay": "rgba(6,9,14,.72)",
    "dark.shadow-sm": "0 1px 2px rgba(0,0,0,.34)",
    "dark.shadow-md": "0 1px 2px rgba(0,0,0,.30),0 10px 26px -16px rgba(0,0,0,.72)",
    "dark.shadow-lg": "0 2px 6px rgba(0,0,0,.34),0 22px 46px -22px rgba(0,0,0,.85)",
    "dark.shadow-focus": "0 0 0 3px rgba(93,155,255,.26)",

    # ── светлая тема: та же геометрия, другие поверхности и тени ───────────
    "light.bg": "#eef1f7",
    "light.bg-deep": "#e3e8f1",
    "light.surface": "#ffffff",
    "light.surface-raised": "#f3f6fb",
    "light.surface-sunken": "#f7f9fc",
    "light.line": "#dce3ee",
    "light.line-soft": "#eaeff6",
    "light.line-strong": "#bdcadb",
    "light.ink": "#16212f",
    "light.ink-soft": "#374963",
    "light.mut": "#5a6a84",               # тёмный текст, а не блёклый синий
    "light.acc": "#2563eb",
    "light.acc-ink": "#ffffff",
    "light.acc-soft": "#e8effe",
    "light.acc-line": "#b7cbf6",
    "light.ok": "#12855a",
    "light.ok-ink": "#ffffff",
    "light.ok-soft": "#e5f6ee",
    "light.ok-line": "#b0dfc6",
    "light.bad": "#d13b32",
    "light.bad-ink": "#ffffff",
    "light.bad-soft": "#fdebea",
    "light.bad-line": "#f1c2be",
    "light.warn": "#b57e0c",
    "light.warn-ink": "#2b2005",
    "light.warn-soft": "#fdf3de",
    "light.warn-line": "#ecd5a2",
    "light.surface-head": "#f6f8fc",
    "light.head-a": "#1c2a41",
    "light.head-b": "#2c3e5e",
    "light.head-ink": "#f2f6fc",
    "light.head-mut": "#b7c5da",
    "light.head-field": "rgba(255,255,255,.12)",
    "light.head-line": "#3b4d6e",
    "light.code-bg": "#0f1724",
    "light.code-ink": "#dae7f8",
    "light.chart-a": "#2563eb",
    "light.chart-b": "#16a34a",
    "light.chart-c": "#d97706",
    "light.chart-d": "#dc2626",
    "light.chart-e": "#7c3aed",
    "light.chart-f": "#0891b2",
    "light.chart-g": "#db2777",
    "light.chart-h": "#64748b",
    "light.chart-grid": "#e3e9f2",
    "light.chart-axis": "#5a6a84",
    "light.chart-track": "#e8eef6",
    "light.skel-a": "#e7ecf5",
    "light.skel-b": "#f5f8fc",
    "light.sel-bg": "#cfe0ff",
    "light.glow": "rgba(37,99,235,.12)",
    "light.overlay": "rgba(20,28,44,.42)",
    "light.shadow-sm": "0 1px 2px rgba(16,24,40,.06)",
    "light.shadow-md": "0 1px 2px rgba(16,24,40,.05),0 10px 26px -16px rgba(16,24,40,.26)",
    "light.shadow-lg": "0 2px 6px rgba(16,24,40,.06),0 22px 44px -22px rgba(16,24,40,.32)",
    "light.shadow-focus": "0 0 0 3px rgba(37,99,235,.16)",

    # ── независимые от темы значения ──────────────────────────────────────
    # радиусы
    "base.radius-xs": "7px",
    "base.radius-sm": "10px",
    "base.radius-md": "14px",
    "base.radius-lg": "20px",
    "base.radius-pill": "999px",
    # шрифты
    "base.font-sans": '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
    "base.font-mono": 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace',
    "base.leading": "1.55",
    # размеры текста
    "base.text-xs": "11.5px",
    "base.text-sm": "13px",
    "base.text-base": "15px",
    "base.text-lg": "17px",
    "base.text-xl": "20px",
    "base.text-xxl": "24px",
    "base.text-num": "27px",
    "base.weight-normal": "400",
    "base.weight-medium": "500",
    "base.weight-semi": "600",
    "base.weight-bold": "700",
    # интервалы
    "base.space-xs": "4px",
    "base.space-sm": "8px",
    "base.space-md": "12px",
    "base.space-lg": "16px",
    "base.space-xl": "22px",
    "base.space-xxl": "30px",
    # иконки
    "base.icon-sm": "16px",
    "base.icon-md": "20px",
    "base.icon-lg": "24px",
    # скорости анимаций и кривые
    "base.motion-fast": "120ms",           # цвет, граница, мелкие сдвиги
    "base.motion-base": "170ms",           # карточки, кнопки, поля
    "base.motion-slow": "280ms",           # появление блоков, тосты
    "base.ease-standard": "cubic-bezier(.2,.7,.3,1)",
    "base.ease-out": "cubic-bezier(.16,.84,.44,1)",
    "base.ease-in": "cubic-bezier(.4,0,.9,.4)",
    # раскладка
    "base.sidebar": "252px",               # ширина бокового меню
    "base.header-h": "58px",                # высота шапки
    "base.content-max": "1320px",           # предел ширины содержимого
    "base.tap-min": "44px",                 # минимальная цель нажатия на телефоне
}

# Токены, которые не зависят от темы
BASE_KEYS = tuple(key[5:] for key in TOKENS if key.startswith("base."))
# Токены, у которых есть своё значение в каждой теме
THEME_NAMES = tuple(key.partition(".")[2] for key in TOKENS if key.startswith("dark."))


def _group(prefix: str) -> dict[str, str]:
    """Значения одной группы токенов: ``_group('dark')``, ``_group('light')``…"""
    return {key.partition(".")[2]: value for key, value in TOKENS.items()
            if key.startswith(prefix + ".")}


def css_variables() -> dict[str, str]:
    """Плоская карта «CSS-переменная -> значение» (обе темы и общее)."""
    out: dict[str, str] = {}
    for key, value in TOKENS.items():
        prefix, _, name = key.partition(".")
        out[("--" if prefix == "base" else f"--{prefix[0]}-") + name] = value
    return out


def css_names() -> set[str]:
    """Имена переменных, на которые в ``STYLESHEET`` разрешено ссылаться.

    Это «сырые» имена (``--bg``, ``--d-bg``, ``--l-bg``, ``--radius-md``) и
    переключаемые (``--bg``, ``--line``, …) - последние объявляются в
    ``:root`` как ``var(--d-имя)`` / ``var(--l-имя)``.
    """
    names: set[str] = set()
    for key in TOKENS:
        prefix, _, name = key.partition(".")
        names.add("--" + name)
        if prefix != "base":
            names.add(f"--{prefix[0]}-{name}")
    return names


def theme_root_css() -> str:
    """Блок ``:root``: тёмная тема по умолчанию и все общие токены.

    Значения обеих тем лежат рядом (``--d-bg`` и ``--l-bg``), а «рабочее» имя
    ``--bg`` смотрит на одну из них. Переключение темы - это перестановка
    одного взгляда, поэтому разница тем гарантированно полная.
    """
    dark, light, base = _group("dark"), _group("light"), _group("base")
    parts = ["color-scheme:dark;"]
    parts += [f"--d-{name}:{value};" for name, value in dark.items()]
    parts += [f"--l-{name}:{value};" for name, value in light.items()]
    parts += [f"--{name}:{value};" for name, value in base.items()]
    parts += [f"--{name}:var(--d-{name});" for name in dark]
    return ":root{" + "".join(parts) + "}"


def light_rules() -> str:
    """Переключение на светлую тему: кнопкой или по системной настройке.

    Системное правило не срабатывает, если сис-админ выбрал тему руками:
    тогда ``data-theme="dark"`` стоит на ``<html>`` и ``:not()`` его отсекает.
    """
    mixin = "".join(f"--{name}:var(--l-{name});" for name in _group("light"))
    return (f'[data-theme="light"]{{color-scheme:light;{mixin}}}\n'
            f'@media (prefers-color-scheme: light){{:root:not([data-theme="dark"])'
            f'{{color-scheme:light;{mixin}}}}}\n'
            f'@media print{{:root{{color-scheme:light;{mixin}}}}}')


# ── иконки ───────────────────────────────────────────────────────────────────
# Единый стиль: сетка 24x24, обводка currentColor толщиной 1.75, скруглённые
# концы, никакой заливки и никаких внешних файлов. Значения - это содержимое
# <svg>; разметку собирает icon().
ICONS: dict[str, str] = {
    # ── разделы панели ───────────────────────────────────────────────────
    "home": '<path d="M3.2 11.4 12 4.3l8.8 7.1"/><path d="M5.6 10.1v9.6h12.8v-9.6"/>'
            '<path d="M10 19.7v-5.1h4v5.1"/>',
    "tickets": '<rect x="3.2" y="5.6" width="17.6" height="12.8" rx="2.4"/>'
               '<path d="M3.6 6.9 12 13.2l8.4-6.3"/>',
    "analytics": '<path d="M4.4 19.6h15.2"/><path d="M8.2 19.6v-5.4"/>'
                 '<path d="M12 19.6v-9.8"/><path d="M15.8 19.6v-3.2"/>',
    "people": '<circle cx="9.2" cy="8.4" r="3.3"/>'
              '<path d="M3.4 19.4c0-3.3 2.6-5.6 5.8-5.6s5.8 2.3 5.8 5.6"/>'
              '<path d="M16.1 5.5a3.2 3.2 0 0 1 0 5.8"/>'
              '<path d="M17.4 13.8c2 .5 3.3 2.4 3.3 4.6"/>',
    "user-off": '<circle cx="10.2" cy="8.2" r="3.4"/>'
                '<path d="M3.6 19.6c0-3.4 3-5.8 6.6-5.8 1 0 1.9.2 2.7.5"/>'
                '<path d="M4.4 4.2 19.6 19.4"/>',
    "college": '<path d="M3.4 9.4 12 4.4l8.6 5"/><path d="M5.4 9.4v11.2h13.2V9.4"/>'
               '<path d="M9.4 20.6v-6.6M14.6 20.6v-6.6"/><path d="M3 20.6h18"/>',
    "students": '<path d="M2.8 9.3 12 5.1l9.2 4.2L12 13.5 2.8 9.3Z"/>'
                '<path d="M6.6 11.2v4.3c0 1.6 2.4 2.8 5.4 2.8s5.4-1.2 5.4-2.8v-4.3"/>'
                '<path d="M20.4 10v4.8"/>',
    "staff": '<rect x="3.2" y="7.6" width="17.6" height="11.8" rx="2.4"/>'
             '<path d="M8.8 7.6V6a1.9 1.9 0 0 1 1.9-1.9h2.6A1.9 1.9 0 0 1 15.2 6v1.6"/>'
             '<path d="M3.2 12.4h17.6"/>',
    "access": '<circle cx="7.8" cy="9.6" r="4"/><path d="M10.6 12.4 18.6 20.4"/>'
              '<path d="M16.4 16.4 18.2 14.6"/><path d="M18.2 18.2 20 16.4"/>',
    "templates": '<path d="M13.4 2.8 6.2 13.4h5.4l-1.2 7.8 7.4-10.6h-5.4l1-7.8Z"/>',
    "groups": '<path d="M3.4 6.8a2 2 0 0 1 2-2h3.3l2.1 2.5h7.8a2 2 0 0 1 2 2v8.5'
              'a2 2 0 0 1-2 2H5.4a2 2 0 0 1-2-2V6.8Z"/>',
    "schedules": '<rect x="3.4" y="5.4" width="17.2" height="15.2" rx="2.4"/>'
                 '<path d="M3.4 10.2h17.2"/><path d="M8.2 3.4v3.6"/><path d="M15.8 3.4v3.6"/>'
                 '<path d="M7.6 13.6h.02"/><path d="M12 13.6h.02"/><path d="M16.4 13.6h.02"/>'
                 '<path d="M7.6 16.8h.02"/><path d="M12 16.8h.02"/>',
    "broadcasts": '<path d="M4.4 10.2h4.2l7.8-4.2v12l-7.8-4.2H4.4a1.8 1.8 0 0 1 0-3.6Z"/>'
                  '<path d="M6.6 13.9v2.9a1.7 1.7 0 0 0 1.7 1.7h1.4"/>'
                  '<path d="M19.2 9.4a4.2 4.2 0 0 1 0 5.2"/>',
    "database": '<ellipse cx="12" cy="6.2" rx="7.4" ry="2.9"/>'
                '<path d="M4.6 6.2v11.4c0 1.6 3.3 2.9 7.4 2.9s7.4-1.3 7.4-2.9V6.2"/>'
                '<path d="M4.6 11.9c0 1.6 3.3 2.9 7.4 2.9s7.4-1.3 7.4-2.9"/>',
    "settings": '<circle cx="12" cy="12" r="3.1"/>'
                '<path d="M18.9 14.2a1.5 1.5 0 0 0 .3 1.7l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1'
                'a1.5 1.5 0 0 0-1.7-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.9 1.9 0 1 1-3.8 0v-.1'
                'a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.7.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1'
                'a1.5 1.5 0 0 0 .3-1.7 1.5 1.5 0 0 0-1.4-.9h-.2a1.9 1.9 0 1 1 0-3.8h.1'
                'a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.7l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1'
                'a1.5 1.5 0 0 0 1.7.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.9 1.9 0 1 1 3.8 0v.1'
                'a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.7-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1'
                'a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.9 1.9 0 1 1 0 3.8h-.1'
                'a1.5 1.5 0 0 0-1.4.9Z"/>',
    "logs": '<path d="M9.6 3.4h4.8"/>'
            '<path d="M10.4 3.4v5.5L5.4 17.3a1.9 1.9 0 0 0 1.6 2.9h10a1.9 1.9 0 0 0 1.6-2.9'
            'l-5-8.4V3.4"/><path d="M7.4 14.4h9.2"/>',

    # ── действия ─────────────────────────────────────────────────────────
    "archive": '<rect x="3.2" y="4.2" width="17.6" height="4.6" rx="1.4"/>'
               '<path d="M5.2 8.8v9.2a2 2 0 0 0 2 2h9.6a2 2 0 0 0 2-2V8.8"/>'
               '<path d="M10 12.8h4"/>',
    "reply": '<path d="M9.4 5.6 4.4 10.4l5 4.8"/>'
             '<path d="M4.4 10.4h9.2a6.2 6.2 0 0 1 6.2 6.2v1.6"/>',
    "delete": '<path d="M4.6 6.6h14.8"/>'
              '<path d="M9.4 6.6V5a1.6 1.6 0 0 1 1.6-1.6h2a1.6 1.6 0 0 1 1.6 1.6v1.6"/>'
              '<path d="M6.6 6.6l.9 12.3a1.9 1.9 0 0 0 1.9 1.7h5.2a1.9 1.9 0 0 0 1.9-1.7l.9-12.3"/>'
              '<path d="M10.4 10.2v6.6"/><path d="M13.6 10.2v6.6"/>',
    "edit": '<path d="M4.4 19.6h3.4L19 8.4a2.2 2.2 0 0 0-3.1-3.1L4.4 16.5v3.1Z"/>'
            '<path d="M14.6 6.4 17.6 9.4"/>',
    "search": '<circle cx="10.8" cy="10.8" r="6.2"/><path d="M15.4 15.4 20.2 20.2"/>',
    "chevron-down": '<path d="M6.6 9.6 12 15l5.4-5.4"/>',
    "chevron-right": '<path d="M9.6 5.6 15.6 12l-6 6.4"/>',
    "chevron-left": '<path d="M14.4 5.6 8.4 12l6 6.4"/>',
    "close": '<path d="M6.6 6.6 17.4 17.4"/><path d="M17.4 6.6 6.6 17.4"/>',
    "plus": '<path d="M12 5.4v13.2"/><path d="M5.4 12h13.2"/>',
    "check": '<path d="M4.9 12.6 9.7 17.4 19.1 6.8"/>',
    "warning": '<path d="M10.6 5 3 18.2a1.9 1.9 0 0 0 1.6 2.8h14.8a1.9 1.9 0 0 0 1.6-2.8'
               'L13.4 5a1.9 1.9 0 0 0-2.8 0Z"/>'
               '<path d="M12 9.4v4.4"/><path d="M12 16.8v.4"/>',
    "loading": '<path d="M12 3.6a8.4 8.4 0 1 1-8.4 8.4"/>',
    "refresh": '<path d="M20.4 12a8.4 8.4 0 1 1-2.5-6"/><path d="M20.4 4.4v4.4H16"/>',
    "filter": '<path d="M3.8 5.4h16.4L14 12.4v5.4l-4 2.4v-7.8L3.8 5.4Z"/>',
    "download": '<path d="M12 4.2v10.9"/><path d="M7.6 11 12 15.4 16.4 11"/>'
                '<path d="M4.8 19.4h14.4"/>',
    "upload": '<path d="M12 19.8V8.9"/><path d="M7.6 13.2 12 8.8l4.4 4.4"/>'
              '<path d="M4.8 4.6h14.4"/>',
    "external": '<path d="M18.2 13.6v4.6a2 2 0 0 1-2 2H5.6a2 2 0 0 1-2-2V7.8a2 2 0 0 1 2-2h4.6"/>'
                '<path d="M14.8 4.4h4.8v4.8"/><path d="M19.6 4.4 11.4 12.6"/>',
    "menu": '<path d="M4 7.2h16"/><path d="M4 12h16"/><path d="M4 16.8h16"/>',
    "sun": '<circle cx="12" cy="12" r="4.1"/>'
           '<path d="M12 2.8v2.4"/><path d="M12 18.8v2.4"/><path d="M2.8 12h2.4"/>'
           '<path d="M18.8 12h2.4"/><path d="M5.5 5.5 7.3 7.3"/><path d="M16.7 16.7 18.5 18.5"/>'
           '<path d="M18.5 5.5 16.7 7.3"/><path d="M7.3 16.7 5.5 18.5"/>',
    "moon": '<path d="M20.4 14.6A8.6 8.6 0 0 1 9.4 3.6a8.6 8.6 0 1 0 11 11Z"/>',
    "clock": '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.2V12l3.2 2"/>',
    "lock": '<rect x="4.6" y="10.2" width="14.8" height="9.8" rx="2"/>'
            '<path d="M8.2 10.2V7.8a3.8 3.8 0 0 1 7.6 0v2.4"/>',
    "link": '<path d="M10.4 13.6a3.6 3.6 0 0 0 5.2.4l2.6-2.6a3.6 3.6 0 0 0-5.1-5.1l-1.5 1.5"/>'
            '<path d="M13.6 10.4a3.6 3.6 0 0 0-5.2-.4l-2.6 2.6a3.6 3.6 0 0 0 5.1 5.1l1.5-1.5"/>',
    "copy": '<rect x="8.4" y="8.4" width="11.2" height="11.2" rx="2"/>'
            '<path d="M15.6 8.4V6.6a2 2 0 0 0-2-2H6.6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h1.8"/>',
    "send": '<path d="M20.8 3.2 10.4 13.6"/>'
            '<path d="M20.8 3.2 14.4 20.8l-4-7.2-7.2-4 17.6-6.4Z"/>',
    "info": '<circle cx="12" cy="12" r="8.4"/><path d="M12 11v5.4"/><path d="M12 7.6v.4"/>',
    "attach": '<path d="M19.4 11.2 12.2 18.4a4.5 4.5 0 0 1-6.4-6.4l7.4-7.4a3 3 0 0 1 4.3 4.3'
              'l-7.4 7.4a1.5 1.5 0 0 1-2.1-2.1l6.7-6.7"/>',
    "grip": '<path d="M9.4 6.2h.02"/><path d="M9.4 12h.02"/><path d="M9.4 17.8h.02"/>'
            '<path d="M14.6 6.2h.02"/><path d="M14.6 12h.02"/><path d="M14.6 17.8h.02"/>',
    "more": '<path d="M6 12h.02"/><path d="M12 12h.02"/><path d="M18 12h.02"/>',
    "user": '<circle cx="12" cy="8.2" r="3.6"/>'
            '<path d="M4.8 20c0-3.6 3.2-6 7.2-6s7.2 2.4 7.2 6"/>',
    # ── лента, мелочи и обслуживание ─────────────────────────────────────
    "eye": '<path d="M2.6 12S6 6.4 12 6.4 21.4 12 21.4 12 18 17.6 12 17.6 2.6 12 2.6 12Z"/>'
           '<circle cx="12" cy="12" r="2.9"/>',
    "eye-off": '<path d="M9.5 6.8A8.9 8.9 0 0 1 12 6.6c6 0 9.4 5.4 9.4 5.4'
               'a15.6 15.6 0 0 1-2.7 3.3"/>'
               '<path d="M6.4 8.2A15.4 15.4 0 0 0 2.6 12S6 17.4 12 17.4a9 9 0 0 0 3.2-.6"/>'
               '<path d="M9.9 9.9a2.9 2.9 0 0 0 4.1 4.1"/>'
               '<path d="M4.4 4.4 19.6 19.6"/>',
    "activity": '<path d="M3.4 12.4h3.2l2.4-6.6 3.4 12 2.6-8.4 1.6 3h3.9"/>',
    "inbox": '<path d="M3.6 13.4 6 5.6h12l2.4 7.8v5a1.8 1.8 0 0 1-1.8 1.8H5.4'
             'a1.8 1.8 0 0 1-1.8-1.8Z"/>'
             '<path d="M3.6 13.4h4.2l1 2.4h6.4l1-2.4h4.2"/>',
    "print": '<path d="M7.4 9.4V4.6h9.2v4.8"/>'
             '<rect x="3.6" y="9.4" width="16.8" height="7.2" rx="1.8"/>'
             '<path d="M7.4 14.4h9.2v5H7.4Z"/>',
    "keyboard": '<rect x="2.6" y="6.4" width="18.8" height="11.2" rx="2.2"/>'
               '<path d="M6.4 10.2h.02"/><path d="M9.6 10.2h.02"/><path d="M12.8 10.2h.02"/>'
               '<path d="M16 10.2h.02"/>'
               '<path d="M6.4 13.4h.02"/><path d="M17.6 13.4h.02"/><path d="M9.6 15.2h4.8"/>',

    "empty": '<path d="M7.6 5.2h8.8l2 8.2v5.2a1.7 1.7 0 0 1-1.7 1.7H7.3a1.7 1.7 0 0 1-1.7-1.7'
             'v-5.2l2-8.2Z"/>'
             '<path d="M5.6 13.4h4.4l1.4 2.2h1.2l1.4-2.2h4.4"/>',
    "dot": '<circle cx="12" cy="12" r="3.2"/>',
}

FALLBACK_ICON = "dot"          # на неизвестное имя - нейтральная точка
ICON_STROKE = "1.75"           # толщина обводки у всех иконок одинаковая

# Путь раздела панели -> имя иконки. Заменяет прежний NAV_ICONS с эмодзи.
ICON_NAMES_BY_PATH: dict[str, str] = {
    "/": "home",
    "/tickets": "tickets",
    "/analytics": "analytics",
    "/people": "people",
    "/nostaff": "user-off",
    "/college": "college",
    "/students": "students",
    "/staff": "staff",
    "/invites": "link",
    "/access": "access",
    "/templates": "templates",
    "/groups": "groups",
    "/schedules": "schedules",
    "/broadcasts": "broadcasts",
    "/database": "database",
    "/settings": "settings",
    "/logs": "logs",
}


def icon(name: str, size: int = 20) -> str:
    """Готовая ``<svg>`` по имени из ``ICONS``.

    Неизвестное имя не поднимает исключение: в меню и в заголовках карточек
    иконка может прийти откуда угодно, и из-за одной чужой строки страница
    не должна падать - вернётся нейтральная точка.
    """
    body = ICONS.get(name) or ICONS[FALLBACK_ICON]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(name))
    safe = safe.strip("-") or FALLBACK_ICON
    return (f'<svg class="ico ico-{safe}" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="{ICON_STROKE}" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
            f'focusable="false">{body}</svg>')


# ── CSS панели ───────────────────────────────────────────────────────────────
# Ниже только правила: ни одного «голого» цвета, всё через var(--имя).
# Токены объявляет theme_root_css() и light_rules() - они собираются в
# STYLESHEET вместе с этим текстом.
_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-size:var(--text-base);
     line-height:var(--leading);font-family:var(--font-sans);-webkit-font-smoothing:antialiased;
     transition:background-color var(--motion-base) var(--ease-standard),
                color var(--motion-base) var(--ease-standard)}
::selection{background:var(--sel-bg);color:var(--ink)}
h1,h2,h3{margin:0}
p{margin:0 0 var(--space-md)}
a{color:var(--acc);text-decoration:none;transition:color var(--motion-fast) var(--ease-standard)}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
code,kbd{font-family:var(--font-mono);font-size:.92em;background:var(--surface-raised);
     border:1px solid var(--line-soft);border-radius:var(--radius-xs);padding:1px 6px}
.mut{color:var(--mut)}
.small{font-size:var(--text-sm)}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.lead{color:var(--mut)}

/* иконки: размер задаёт разметка, CSS только подстраховывает */
.ico{width:var(--icon-md);height:var(--icon-md);flex:0 0 auto;display:inline-block;vertical-align:-.18em}
.ico-sm{width:var(--icon-sm);height:var(--icon-sm)}
.ico-lg{width:var(--icon-lg);height:var(--icon-lg)}

/* ── шапка ───────────────────────────────────────────────────────────────── */
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:var(--space-lg);
       min-height:var(--header-h);padding:10px 20px;color:var(--head-ink);
       background:linear-gradient(115deg,var(--head-a),var(--head-b));
       border-bottom:1px solid var(--head-line);box-shadow:var(--shadow-sm)}
.brand{display:flex;align-items:center;gap:10px;min-width:var(--sidebar);color:inherit;text-decoration:none}
.brand:hover{text-decoration:none}
.brand .logo{display:grid;place-items:center;width:36px;height:36px;border-radius:var(--radius-sm);
       background:var(--head-field);color:var(--head-ink);
       transition:transform var(--motion-base) var(--ease-standard),background-color var(--motion-base) var(--ease-standard)}
.brand:hover .logo{transform:translateY(-1px)}
.brand b{display:block;font-size:var(--text-base);font-weight:var(--weight-semi);letter-spacing:.2px;line-height:1.2}
.brand small{display:block;color:var(--head-mut);font-size:var(--text-xs);letter-spacing:.4px;
       text-transform:uppercase}
.gsearch{flex:1;display:flex;align-items:center;gap:var(--space-sm);max-width:520px}
.gsearch input{flex:1;min-width:0;padding:8px 14px;border:1px solid var(--head-line);
       border-radius:var(--radius-pill);background:var(--head-field);color:var(--head-ink);
       font:inherit;font-size:var(--text-sm);
       transition:border-color var(--motion-fast) var(--ease-standard),
                  background-color var(--motion-fast) var(--ease-standard),
                  box-shadow var(--motion-fast) var(--ease-standard)}
.gsearch input::placeholder{color:var(--head-mut)}
.gsearch input:hover{border-color:var(--line-strong)}
.gsearch input:focus{outline:0;background:var(--surface);color:var(--ink);
       border-color:var(--acc);box-shadow:var(--shadow-focus)}
.gsearch button{display:grid;place-items:center;width:34px;height:34px;flex:0 0 34px;padding:0;
       border:1px solid var(--head-line);border-radius:50%;background:var(--head-field);
       color:var(--head-ink);cursor:pointer;
       transition:background-color var(--motion-fast) var(--ease-standard),
                  border-color var(--motion-fast) var(--ease-standard),
                  transform var(--motion-fast) var(--ease-standard)}
.gsearch button:hover{background:var(--line-strong);border-color:var(--line-strong)}
.gsearch button:active{transform:scale(.94)}
.gsearch input.is-filled{border-color:var(--acc);box-shadow:var(--shadow-focus)}
.who{margin-left:auto;display:flex;align-items:center;gap:var(--space-md);color:var(--head-mut);
     font-size:var(--text-sm);white-space:nowrap}
.who b{color:var(--head-ink);font-weight:var(--weight-semi)}
.who a{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border:1px solid var(--head-line);
     border-radius:var(--radius-pill);
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard)}
.who a:hover{text-decoration:none;background:var(--head-field);border-color:var(--line-strong)}
.theme-toggle{display:grid;place-items:center;width:34px;height:34px;flex:0 0 34px;padding:0;
     border:1px solid var(--head-line);border-radius:50%;background:var(--head-field);
     color:var(--head-ink);cursor:pointer;
     transition:background-color var(--motion-fast) var(--ease-standard),
                transform var(--motion-fast) var(--ease-standard)}
.theme-toggle:hover{background:var(--line-strong)}
.theme-toggle:active{transform:scale(.94)}

/* ── боковое меню (на узком экране превращается в верхнюю ленту) ────────── */
nav{position:fixed;top:var(--header-h);bottom:0;left:0;width:var(--sidebar);z-index:20;
    padding:var(--space-lg) 10px var(--space-xxl);background:var(--surface);
    border-right:1px solid var(--line);overflow-y:auto;overscroll-behavior:contain;
    animation:nav-in var(--motion-slow) var(--ease-standard) both}
nav a{display:flex;align-items:center;gap:10px;padding:9px 11px;margin-bottom:2px;
      border:1px solid transparent;border-radius:var(--radius-sm);color:var(--ink-soft);
      font-size:var(--text-sm);font-weight:var(--weight-medium);text-decoration:none;
      transition:background-color var(--motion-fast) var(--ease-standard),
                 color var(--motion-fast) var(--ease-standard),
                 border-color var(--motion-fast) var(--ease-standard),
                 transform var(--motion-fast) var(--ease-standard)}
nav a:hover{background:var(--surface-raised);color:var(--ink);text-decoration:none;transform:translateX(2px)}
nav a.on{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc);font-weight:var(--weight-semi)}
.nav-ico{display:grid;place-items:center;width:22px;height:22px;flex:0 0 22px;color:var(--mut);
     transition:color var(--motion-fast) var(--ease-standard)}
.nav-ico .ico{width:18px;height:18px}
nav a:hover .nav-ico{color:var(--ink-soft)}
nav a.on .nav-ico{color:var(--acc)}
.nav-txt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── меню по группам: пять разделов вместо шестнадцати вкладок ────────────── */
.nav-groups{display:flex;flex-direction:column;gap:var(--space-lg)}
.nav-group{display:flex;flex-direction:column;gap:2px;min-width:0}
.nav-group-items{display:flex;flex-direction:column;gap:2px}
.nav-group-label{display:flex;align-items:center;gap:7px;padding:0 11px 3px;color:var(--mut);
     font-size:var(--text-xs);font-weight:var(--weight-semi);letter-spacing:.7px;
     text-transform:uppercase;white-space:nowrap;
     transition:color var(--motion-fast) var(--ease-standard)}
.nav-group.on .nav-group-label{color:var(--acc)}
/* бейдж: сколько ждёт внимания. Мелкий, но читаемый в тёмной теме */
.nav-badge{min-width:21px;padding:0 6px;border:1px solid var(--line);border-radius:var(--radius-pill);
     background:var(--surface-raised);color:var(--ink-soft);font-size:11px;line-height:17px;
     text-align:center;font-variant-numeric:tabular-nums;text-transform:none;letter-spacing:0;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.nav-badge.hot{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad-ink)}

/* ── основная область ────────────────────────────────────────────────────── */
main{margin-left:var(--sidebar);padding:var(--space-xl) 26px var(--space-xxl);
     max-width:var(--content-max);animation:page-in var(--motion-slow) var(--ease-standard) both}
.page-title{margin:0 0 var(--space-lg);display:flex;align-items:center;gap:10px;flex-wrap:wrap;
     font-size:var(--text-xxl);font-weight:var(--weight-bold);letter-spacing:-.2px}
/* шапка страницы: заголовок слева, действия справа - экспорт и печать на виду */
.dochead{display:flex;align-items:center;gap:var(--space-md);flex-wrap:wrap;margin:0 0 var(--space-lg)}
.dochead .page-title{margin:0;min-width:0}
.dochead .page-actions{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm);
     margin-left:auto;padding:0}
.dochead .page-actions .small{margin:0}
footer{margin-left:var(--sidebar);padding:var(--space-lg) 26px 30px;color:var(--mut);font-size:var(--text-xs)}
h3{margin:var(--space-xl) 0 var(--space-sm);color:var(--mut);font-size:var(--text-xs);
   font-weight:var(--weight-semi);letter-spacing:.6px;text-transform:uppercase}

/* ── карточки: подъём и тень при наведении ───────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-md);
      padding:18px 20px;margin-bottom:var(--space-lg);box-shadow:var(--shadow-md);
      overflow-x:auto;overscroll-behavior-x:contain;
      transition:transform var(--motion-base) var(--ease-standard),
                 box-shadow var(--motion-base) var(--ease-standard),
                 border-color var(--motion-base) var(--ease-standard);
      animation:card-in var(--motion-slow) var(--ease-standard) both}
.card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);border-color:var(--line-strong)}
.card h2{margin:0 0 var(--space-lg);display:flex;align-items:center;gap:var(--space-sm);flex-wrap:wrap;
      min-width:0;font-size:var(--text-lg);font-weight:var(--weight-semi)}
.card h2 .ico{color:var(--mut)}
.card h2 .pill,.card h2 .btn{font-size:var(--text-sm)}
.cards{display:flex;flex-wrap:wrap;gap:var(--space-md)}
/* появление карточек по очереди: заметная пауза, без «прыжков» */
main > .card:nth-child(2){animation-delay:40ms}
main > .card:nth-child(3){animation-delay:80ms}
main > .card:nth-child(4){animation-delay:120ms}
main > .card:nth-child(5){animation-delay:160ms}
main > .card:nth-child(6){animation-delay:200ms}
main > .card:nth-child(7){animation-delay:240ms}
main > .card:nth-child(n+8){animation-delay:280ms}

/* ── таблицы ─────────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:var(--text-base)}
th,td{text-align:left;padding:10px var(--space-sm);border-bottom:1px solid var(--line-soft);
      vertical-align:top;transition:background-color var(--motion-fast) var(--ease-standard)}
th{position:sticky;top:0;z-index:1;background:var(--surface-head);color:var(--mut);
    font-size:var(--text-xs);font-weight:var(--weight-semi);letter-spacing:.4px;
    text-transform:uppercase;white-space:nowrap;border-bottom:1px solid var(--line)}
td{word-break:break-word;overflow-wrap:anywhere}
tbody tr{transition:background-color var(--motion-fast) var(--ease-standard)}
tbody tr:hover{background:var(--surface-raised)}
tbody tr:last-child td{border-bottom:0}
td b{color:var(--ink);font-weight:var(--weight-semi)}
th.col-key{width:260px}
tbody tr.is-found{background:var(--glow);box-shadow:inset 3px 0 0 var(--acc)}

/* ── формы ───────────────────────────────────────────────────────────────── */
input,select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line-strong);
     border-radius:var(--radius-sm);background:var(--surface-sunken);color:var(--ink);font:inherit;
     transition:border-color var(--motion-fast) var(--ease-standard),
                box-shadow var(--motion-fast) var(--ease-standard),
                background-color var(--motion-fast) var(--ease-standard)}
input::placeholder,textarea::placeholder{color:var(--mut)}
input:hover,select:hover,textarea:hover{border-color:var(--acc-line)}
input:focus,select:focus,textarea:focus{outline:0;background:var(--surface);
     border-color:var(--acc);box-shadow:var(--shadow-focus)}
input[type=checkbox],input[type=radio]{width:auto;accent-color:var(--acc)}
textarea{min-height:110px;resize:vertical}
label{display:block;margin:var(--space-md) 0 var(--space-xs);color:var(--mut);
      font-size:var(--text-sm);font-weight:var(--weight-semi);letter-spacing:.2px}
.grid{display:flex;flex-wrap:wrap;align-items:flex-end;gap:var(--space-md)}
.grid>*{flex:1 1 180px}
.grid .full{flex:1 1 100%}
form.inline{display:inline}

/* ── кнопки ──────────────────────────────────────────────────────────────── */
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 15px;
     border:1px solid transparent;border-radius:var(--radius-sm);background:var(--acc);
     color:var(--acc-ink);font:inherit;font-weight:var(--weight-medium);cursor:pointer;
     text-decoration:none;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                box-shadow var(--motion-fast) var(--ease-standard),
                transform var(--motion-fast) var(--ease-standard)}
button:hover,.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow-md);text-decoration:none}
button:active,.btn:active{transform:translateY(0);box-shadow:var(--shadow-sm)}
.btn-grey{background:var(--surface-raised);border-color:var(--line);color:var(--ink-soft)}
.btn-grey:hover{background:var(--line);color:var(--ink)}
.btn-ok{background:var(--ok);color:var(--ok-ink)}
.btn-bad{background:var(--bad);color:var(--bad-ink)}
.btn-sm{padding:5px 10px;border-radius:var(--radius-xs);font-size:var(--text-sm)}

/* ── сообщения и метки ───────────────────────────────────────────────────── */
.msg{display:flex;align-items:flex-start;gap:var(--space-sm);padding:12px 14px;margin-bottom:var(--space-lg);
     border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--surface);
     color:var(--ink);font-size:var(--text-base);font-weight:var(--weight-medium);
     animation:card-in var(--motion-slow) var(--ease-standard) both}
.msg .ico{margin-top:1px}
.msg-ok{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok-ink)}
.msg-bad{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad-ink)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;white-space:nowrap;
     border:1px solid var(--line);border-radius:var(--radius-pill);background:var(--surface-raised);
     color:var(--ink-soft);font-size:var(--text-sm);font-weight:var(--weight-medium);
     transition:background-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.pill-on{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok-ink)}
.pill-off{background:var(--surface-sunken);color:var(--mut)}

/* ── показатели ──────────────────────────────────────────────────────────── */
.stat{flex:1 1 160px;padding:14px 16px;border:1px solid var(--line);border-radius:var(--radius-md);
      background:var(--surface);box-shadow:var(--shadow-sm);
      transition:transform var(--motion-base) var(--ease-standard),
                 box-shadow var(--motion-base) var(--ease-standard)}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}
.stat b{display:block;font-size:var(--text-num);line-height:1.15;letter-spacing:-.5px;
     font-weight:var(--weight-bold);font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:var(--text-sm)}
.kpi{display:flex;flex-wrap:wrap;gap:var(--space-md);margin-bottom:var(--space-lg)}
.kpi div{flex:1 1 130px;padding:13px 15px;border:1px solid var(--line);border-radius:var(--radius-md);
     background:var(--surface);box-shadow:var(--shadow-sm);
     transition:transform var(--motion-base) var(--ease-standard),
                box-shadow var(--motion-base) var(--ease-standard)}
.kpi div:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}
.kpi b{display:block;font-size:var(--text-xl);line-height:1.2;font-variant-numeric:tabular-nums}
.kpi span{color:var(--mut);font-size:var(--text-sm)}
.kpi .warn b{color:var(--bad)}
.kpi .good b{color:var(--ok)}

/* ── крупные счётчики «Пульта»: каждый ведёт в свой раздел ────────────────── */
.big-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(186px,1fr));
     gap:var(--space-md);margin-bottom:var(--space-lg)}
.big-stat{position:relative;display:flex;flex-direction:column;gap:1px;padding:15px 17px;
     border:1px solid var(--line);border-radius:var(--radius-md);background:var(--surface);
     box-shadow:var(--shadow-sm);color:inherit;overflow:hidden;text-decoration:none;
     transition:transform var(--motion-base) var(--ease-standard),
                box-shadow var(--motion-base) var(--ease-standard),
                border-color var(--motion-base) var(--ease-standard)}
.big-stat::after{content:"";position:absolute;inset:0 auto 0 0;width:3px;
     background:var(--acc);opacity:.55;
     transition:opacity var(--motion-base) var(--ease-standard)}
.big-stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);
     border-color:var(--line-strong);text-decoration:none}
.big-stat:hover::after{opacity:1}
.big-stat .big-ico{color:var(--mut);margin-bottom:2px}
.big-stat b{font-size:var(--text-num);line-height:1.12;letter-spacing:-.5px;
     font-weight:var(--weight-bold);font-variant-numeric:tabular-nums}
.big-stat .big-txt{font-size:var(--text-xl);line-height:1.25}
.big-stat span{color:var(--mut);font-size:var(--text-sm)}
.big-stat.warn::after{background:var(--warn)}
.big-stat.bad::after{background:var(--bad)}
.big-stat.good::after{background:var(--ok)}
.big-stat.zero{opacity:.72}
/* мелкая полоса «масштаба» под счётчиками: сколько всего в базе */
.scale-line{display:flex;flex-wrap:wrap;gap:var(--space-lg);padding:11px 15px;margin-bottom:var(--space-lg);
     border:1px solid var(--line);border-radius:var(--radius-md);background:var(--surface-sunken);
     color:var(--mut);font-size:var(--text-sm)}
.scale-line b{color:var(--ink-soft);font-weight:var(--weight-semi);font-variant-numeric:tabular-nums}

/* ── «Что требует действия»: строки-ссылки с числом ───────────────────────── */
.todo{margin:0;padding:0;list-style:none}
.todo li+li{margin-top:2px}
.todo-row{display:flex;align-items:center;gap:var(--space-md);padding:10px 12px;
     border:1px solid var(--line-soft);border-radius:var(--radius-sm);background:var(--surface-sunken);
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard)}
.todo-row:hover{background:var(--surface-raised);border-color:var(--line-strong)}
.todo-ico{color:var(--mut)}
.todo-name{flex:1;min-width:0;color:var(--ink-soft);font-size:var(--text-sm);
     overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.todo-n{flex:0 0 auto;min-width:34px;padding:1px 9px;border-radius:var(--radius-pill);
     background:var(--surface-raised);border:1px solid var(--line);color:var(--ink);
     font-size:var(--text-sm);text-align:center;font-variant-numeric:tabular-nums}
.todo-row.hot .todo-n{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad-ink)}
.todo-row.zero .todo-name{color:var(--mut)}
.todo-go{flex:0 0 auto;color:var(--mut);opacity:.35;
     transition:opacity var(--motion-fast) var(--ease-standard)}
.todo-row:hover .todo-go{opacity:1}
.todo-empty{display:flex;align-items:center;gap:var(--space-sm);padding:var(--space-lg);
     color:var(--mut);font-size:var(--text-sm)}

/* ── лента событий ────────────────────────────────────────────────────────── */
.feed{margin:0;padding:0;list-style:none}
.feed li{display:flex;align-items:baseline;gap:var(--space-sm);padding:7px 2px;
     border-bottom:1px solid var(--line-soft)}
.feed li:last-child{border-bottom:0}
.feed-time{flex:0 0 auto;color:var(--mut);font-size:var(--text-xs);white-space:nowrap;
     font-variant-numeric:tabular-nums}
.feed-what{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
     color:var(--ink-soft)}
.feed-what b{color:var(--ink);font-weight:var(--weight-semi)}
.pager{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm);margin-top:var(--space-lg);
     padding-top:var(--space-md);border-top:1px solid var(--line)}

/* ── копирование значения по клику ────────────────────────────────────────── */
.copy-btn{display:inline-grid;place-items:center;width:26px;height:26px;padding:0;vertical-align:middle;
     border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface-raised);
     color:var(--mut);cursor:pointer;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.copy-btn:hover{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.copy-btn .ico{width:14px;height:14px}
.copy-btn.is-done{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok-ink)}
.code-cell{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}

/* ── точки состояния вместо цветных кружков эмодзи ────────────────────────── */
.dot-state{display:inline-block;width:9px;height:9px;flex:0 0 9px;border-radius:50%;
     background:var(--line-strong);vertical-align:middle}
.dot-state.on{background:var(--ok)}
.dot-state.off{background:var(--mut)}
.dot-state.bad{background:var(--bad)}
.dot-state.warn{background:var(--warn)}

/* ── палитра разделов (Ctrl+K) ────────────────────────────────────────────── */
.palette{position:fixed;inset:0;z-index:70;display:none;align-items:flex-start;justify-content:center;
     padding:var(--space-xxl) var(--space-lg);background:var(--overlay)}
.palette.is-open{display:flex}
.palette-box{width:min(560px,100%);border:1px solid var(--line);border-radius:var(--radius-lg);
     background:var(--surface);box-shadow:var(--shadow-lg);overflow:hidden;
     animation:card-in var(--motion-base) var(--ease-out) both}
.palette-box input{width:100%;border:0;border-bottom:1px solid var(--line);border-radius:0;
     background:var(--surface-sunken);padding:13px var(--space-lg)}
.palette-list{max-height:56vh;margin:0;padding:var(--space-sm);overflow-y:auto;list-style:none}
.palette-list li{margin:0}
.palette-list a{display:flex;align-items:center;gap:var(--space-sm);padding:9px 11px;
     border-radius:var(--radius-sm);color:var(--ink-soft);font-size:var(--text-sm);text-decoration:none;
     transition:background-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.palette-list a .ico{color:var(--mut)}
.palette-list a.on{background:var(--acc-soft);color:var(--acc)}
.palette-list a.on .ico{color:var(--acc)}
.palette-list .pal-group{padding:8px 11px 3px;color:var(--mut);font-size:var(--text-xs);
     letter-spacing:.7px;text-transform:uppercase}
.palette-foot{padding:var(--space-sm) var(--space-lg);border-top:1px solid var(--line);
     color:var(--mut);font-size:var(--text-xs)}

/* ── выделение строки горячими клавишами ──────────────────────────────────── */
[data-hk].hk-on{background:var(--glow);box-shadow:inset 3px 0 0 var(--acc)}
tr.hk-on td{background:var(--glow)}
tr.hk-on td:first-child{box-shadow:inset 3px 0 0 var(--acc)}

/* ── графики ─────────────────────────────────────────────────────────────── */
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:var(--space-lg)}
.chart-box{min-width:0;margin:0;padding:13px 15px;border:1px solid var(--line);
     border-radius:var(--radius-md);background:var(--surface);
     transition:border-color var(--motion-base) var(--ease-standard),
                box-shadow var(--motion-base) var(--ease-standard)}
.chart-box:hover{border-color:var(--line-strong);box-shadow:var(--shadow-md)}
.chart-box figcaption{margin-bottom:var(--space-sm);color:var(--mut);font-size:var(--text-sm);
     font-weight:var(--weight-semi)}
.chart{display:block;width:100%;height:auto}
.chart .grid-line{stroke:var(--chart-grid);stroke-width:1}
.chart .axis{fill:var(--chart-axis);font-size:10px}
.donut-total{fill:var(--ink);font-size:22px;font-weight:var(--weight-bold)}
.donut-sub{fill:var(--mut);font-size:11px}
.donut{width:150px;height:150px;flex:0 0 150px}
.donut-wrap{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-lg)}
.legend{display:flex;flex-wrap:wrap;gap:var(--space-lg);margin-top:6px;color:var(--mut);font-size:var(--text-xs)}
.legend span{display:flex;align-items:center;gap:5px}
.legend i,.legend-list i{display:inline-block;width:10px;height:10px;flex:0 0 10px;
     border-radius:3px;background:var(--chart-h)}
.legend-list{flex:1 1 130px;margin:0;padding:0;list-style:none;font-size:var(--text-sm)}
.legend-list li{display:flex;align-items:center;gap:var(--space-sm);padding:3px 0}
.legend-list span{flex:1;color:var(--ink-soft)}
.legend-list b{font-variant-numeric:tabular-nums}
.hbar-list{margin:0;padding:0;list-style:none;font-size:var(--text-sm)}
.hbar-list li{display:flex;align-items:center;gap:var(--space-sm);padding:3px 0}
.hbar-label{flex:0 0 34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-soft)}
.hbar-track{flex:1;height:12px;border-radius:5px;background:var(--chart-track);overflow:hidden}
.hbar-track i{display:block;height:100%;border-radius:5px;transform-origin:left center;
     animation:grow-x var(--motion-slow) var(--ease-out) both}
.spark svg{height:46px}
.chart-empty{padding:var(--space-xxl) 0;color:var(--mut);font-size:var(--text-sm);text-align:center}

/* ── рабочее место с обращениями ──────────────────────────────────────────── */
.workbench{display:grid;grid-template-columns:minmax(320px,380px) 1fr;gap:var(--space-md);align-items:start}
.wb-queue,.wb-card{min-width:0}
.wb-queue{position:sticky;top:calc(var(--header-h) + var(--space-md))}
.wb-list{display:flex;flex-direction:column;gap:2px;max-height:64vh;overflow-y:auto;padding-right:2px}
.wb-item{display:grid;grid-template-columns:18px 1fr;align-items:start;gap:var(--space-sm);
     padding:8px;border:1px solid transparent;border-radius:var(--radius-sm);cursor:pointer;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard)}
.wb-item:hover{background:var(--surface-raised)}
.wb-item.wb-on{background:var(--acc-soft);border-color:var(--acc-line)}
.wb-item input{margin:3px 0 0;flex:0 0 auto}
.wb-item a{min-width:0;color:inherit;font-size:var(--text-sm);line-height:1.4;text-decoration:none}
.wb-item a:hover{text-decoration:none}
.wb-head{display:flex;align-items:baseline;gap:6px;white-space:nowrap}
.wb-head b{flex:0 0 auto;font-weight:var(--weight-semi)}
.wb-status{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;color:var(--mut)}
.wb-wait{display:inline-flex;align-items:center;gap:4px;color:var(--warn);white-space:nowrap}
.wb-wait .ico{width:14px;height:14px}
.wb-date{flex:0 0 auto;margin-left:auto;color:var(--mut);font-size:var(--text-xs)}
/* ФИО идёт отдельной строкой и переносится по словам: обрезанное многоточием
   имя в очереди бесполезно - по нему обращение не сверяют с человеком. */
.wb-who{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 var(--space-sm);
     margin:0 0 var(--space-xs);color:var(--mut);font-size:var(--text-sm);
     white-space:normal;overflow-wrap:anywhere;word-break:normal}
.wb-who>.ico{align-self:center}
.wb-fio{min-width:0;color:var(--ink);font-size:var(--text-sm);font-weight:var(--weight-semi);
     white-space:normal;overflow-wrap:anywhere;word-break:normal}
.wb-brief{color:var(--mut);font-size:var(--text-xs);margin:0 0 var(--space-xs)}
.wb-name{white-space:normal;overflow-wrap:anywhere;word-break:normal}
.wb-group{padding:0 6px;border:1px solid var(--line);border-radius:var(--radius-sm);
     color:var(--mut);font-size:var(--text-xs);white-space:nowrap}
.wb-id,.wb-when{color:var(--mut);font-size:var(--text-xs)}
.wb-when{margin-left:var(--space-sm)}
.wb-text{display:block;margin-top:1px;overflow:hidden;text-overflow:ellipsis;
     white-space:nowrap;color:var(--mut)}
.wb-bulk{margin-top:10px;padding-top:var(--space-sm);border-top:1px solid var(--line);font-size:var(--text-sm)}
.wb-bulk summary{cursor:pointer;color:var(--mut)}
.wb-card table{table-layout:fixed}
.wb-card table td,.wb-card table th{vertical-align:top;word-break:break-word}
.wb-card table td:nth-child(1){width:130px}
.wb-card table td:nth-child(2){width:180px}
.wb-card table td small{color:var(--mut)}
.wb-access{display:inline-block;margin:0 var(--space-sm) var(--space-xs) 0;
     vertical-align:top}
.wb-access label{display:flex;align-items:center;gap:6px;margin:0;cursor:pointer;font-size:var(--text-sm)}
.wb-access-name{display:flex;flex-direction:column;min-width:0;white-space:normal;
     overflow-wrap:anywhere;word-break:normal}
.wb-access-pos{color:var(--mut);font-size:var(--text-xs)}

/* ── раскрывающиеся детали ───────────────────────────────────────────────── */
details{margin-bottom:var(--space-md);padding:0 var(--space-lg);overflow:hidden;
     border:1px solid var(--line);border-radius:var(--radius-md);background:var(--surface)}
details>summary{display:flex;align-items:center;gap:var(--space-sm);padding:var(--space-md) 0;
     list-style:none;cursor:pointer;color:var(--ink-soft);font-weight:var(--weight-semi);
     transition:color var(--motion-fast) var(--ease-standard)}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{color:var(--ink)}
details>summary::after{content:"";width:9px;height:9px;flex:0 0 9px;margin-left:auto;
     border-right:1.75px solid var(--mut);border-bottom:1.75px solid var(--mut);
     transform:rotate(45deg);transition:transform var(--motion-base) var(--ease-standard)}
details[open]>summary::after{transform:rotate(-135deg)}
details[open]>:not(summary){animation:reveal var(--motion-slow) var(--ease-standard) both}
@supports selector(::details-content){
  :root{interpolate-size:allow-keywords}
  details::details-content{block-size:0;overflow:hidden;
       transition:block-size var(--motion-slow) var(--ease-standard),
                  content-visibility var(--motion-slow) allow-discrete}
  details[open]::details-content{block-size:auto}
}

/* ── тосты ───────────────────────────────────────────────────────────────── */
.toast-stack{position:fixed;right:var(--space-lg);bottom:var(--space-lg);z-index:60;display:flex;
     flex-direction:column;gap:var(--space-sm);max-width:min(360px,calc(100vw - 2 * var(--space-lg)))}
.toast{display:flex;align-items:flex-start;gap:var(--space-sm);padding:12px 14px;color:var(--ink);
     border:1px solid var(--line);border-radius:var(--radius-md);background:var(--surface);
     box-shadow:var(--shadow-lg);font-size:var(--text-sm);
     animation:toast-in var(--motion-slow) var(--ease-out) both}
.toast .ico{margin-top:1px}
.toast-ok{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok-ink)}
.toast-bad{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad-ink)}
.toast.is-out{animation:toast-out var(--motion-base) var(--ease-in) both}

/* ── загрузка и скелетоны ─────────────────────────────────────────────────── */
.skel{border-radius:var(--radius-sm);background-size:200% 100%;
     background:linear-gradient(90deg,var(--skel-a) 0%,var(--skel-b) 50%,var(--skel-a) 100%);
     animation:skel-slide 1.5s linear infinite}
.skel-line{height:12px;margin-bottom:var(--space-sm)}
.skel-line:last-child{width:60%;margin-bottom:0}
.skel-block{height:96px}
.is-loading{opacity:.75}
.loading-row{display:flex;align-items:center;gap:var(--space-sm);padding:var(--space-sm) 0;
     color:var(--mut);font-size:var(--text-sm)}
.loading-dot{width:9px;height:9px;flex:0 0 9px;border-radius:50%;background:var(--acc);
     animation:breathe 1.6s var(--ease-standard) infinite}
.spin{animation:spin 900ms linear infinite}

/* ── пустые состояния ────────────────────────────────────────────────────── */
.empty{display:flex;flex-direction:column;align-items:center;gap:var(--space-sm);text-align:center;
     padding:var(--space-xxl) var(--space-lg);color:var(--mut);border:1px dashed var(--line);
     border-radius:var(--radius-md);background:var(--surface-sunken);
     animation:card-in var(--motion-slow) var(--ease-standard) both}
.empty .ico{width:34px;height:34px;color:var(--line-strong);stroke-width:1.5}
.empty b{color:var(--ink-soft);font-size:var(--text-lg);font-weight:var(--weight-semi)}
.empty span{font-size:var(--text-sm)}

/* ── журнал ──────────────────────────────────────────────────────────────── */
pre{max-height:560px;margin:0;padding:var(--space-lg);overflow:auto;border:1px solid var(--line);
    border-radius:var(--radius-md);background:var(--code-bg);color:var(--code-ink);
    font-family:var(--font-mono);font-size:var(--text-sm);line-height:1.5;
    white-space:pre-wrap;word-break:break-all}

/* ── счётчик: число докручивается средствами CSS, без JavaScript ────────── */
@property --n{syntax:"<integer>";initial-value:0;inherits:false}
.count{--to:0;--n:0;counter-reset:c var(--n);font-variant-numeric:tabular-nums;
     animation:count-up var(--motion-slow) var(--ease-standard) forwards}
.count::after{content:counter(c)}

/* ── анимации ────────────────────────────────────────────────────────────── */
@keyframes page-in{from{opacity:0;translate:0 6px}to{opacity:1;translate:0 0}}
@keyframes card-in{from{opacity:0;translate:0 10px}to{opacity:1;translate:0 0}}
@keyframes nav-in{from{opacity:0;translate:-8px 0}to{opacity:1;translate:0 0}}
@keyframes reveal{from{opacity:0;translate:0 -6px}to{opacity:1;translate:0 0}}
@keyframes grow-x{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes breathe{0%,100%{opacity:.35;scale:.85}50%{opacity:1;scale:1}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes skel-slide{from{background-position:200% 0}to{background-position:-200% 0}}
@keyframes toast-in{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}
@keyframes toast-out{to{opacity:0;transform:translateY(6px) scale(.99)}}
@keyframes count-up{from{--n:0}to{--n:var(--to)}}

/* ── планшет: меню одной строкой, рабочее место в одну колонку ───────────── */
@media (max-width:1000px){
  :root{--sidebar:0px}
  header{flex-wrap:wrap;padding:10px 14px;gap:10px}
  .brand{min-width:0}
  .gsearch{order:3;flex:1 1 100%;max-width:none}
  .theme-toggle{margin-left:auto}
  nav{position:sticky;top:0;width:auto;height:auto;display:flex;flex-wrap:nowrap;gap:6px;
      padding:8px 12px;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x proximity;
      -webkit-overflow-scrolling:touch;scrollbar-width:none;
      border-right:0;border-bottom:1px solid var(--line)}
  nav::-webkit-scrollbar{display:none}
  nav a{margin:0;padding:9px 12px;font-size:var(--text-sm);white-space:nowrap;flex:0 0 auto;
        scroll-snap-align:start}
  nav a:hover{transform:none}
  .nav-txt{white-space:nowrap}
  .nav-groups{flex-direction:row;flex-wrap:nowrap;align-items:center;gap:var(--space-md)}
  .nav-group{flex-direction:row;align-items:center;gap:4px}
  .nav-group-items{flex-direction:row;gap:4px}
  .nav-group-name{display:none}
  .nav-group-label{padding:0 2px}
  .workbench{grid-template-columns:1fr}
  .wb-queue{position:static}
  .wb-list{max-height:40vh}
  main{margin-left:0;padding:16px 12px 40px}
  footer{margin-left:0;padding:12px}
  .page-title{font-size:var(--text-xl)}
  .stat,.kpi div{flex:1 1 44%}
  .charts{grid-template-columns:1fr}
  .card{padding:16px}
  .toast-stack{right:12px;left:12px;bottom:12px;max-width:none}
}

/* ── телефон: всё, до чего дотягиваются пальцем ──────────────────────────── */
@media (max-width:640px){
  header{min-height:auto;padding:8px 12px;gap:8px}
  .brand .logo{width:32px;height:32px}
  .brand .logo .ico{width:20px;height:20px}
  .brand b{font-size:var(--text-base)}
  .brand small{display:none}
  .who{display:none}
  .gsearch input,.gsearch button{padding:9px 12px}
  .gsearch button{width:38px;height:38px;flex:0 0 38px}
  .gsearch input{padding-left:12px}
  nav{padding:7px 10px;gap:5px}
  nav a{padding:11px 13px;font-size:var(--text-base);min-height:44px}
  .nav-groups{gap:var(--space-md)}
  .nav-group-label{padding:0 1px;font-size:10.5px;letter-spacing:.4px}
  .nav-badge{min-width:19px;padding:0 5px;font-size:10.5px;line-height:16px}
  .dochead{gap:var(--space-sm)}
  .dochead .page-actions{margin-left:0;width:100%}
  .big-stats{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
  .big-stat b{font-size:23px}
  .big-stat .big-txt{font-size:var(--text-lg)}
  .copy-btn{width:36px;height:36px;min-height:0}
  .wb-edit,.wb-reply{display:none}
  main{padding:12px 10px 44px}
  .page-title{font-size:var(--text-lg);margin-bottom:var(--space-md)}
  .card{padding:13px 12px;margin-bottom:var(--space-md);border-radius:var(--radius-md)}
  .card:hover{transform:none}
  .card h2{margin-bottom:10px;font-size:var(--text-base)}
  table{font-size:var(--text-sm)}
  th,td{padding:8px 7px}
  th{font-size:11px;white-space:normal}
  th.col-key{width:auto}
  .grid{gap:10px}
  .grid>*{flex:1 1 calc(50% - 5px)}
  .grid .full{flex:1 1 100%}
  button,.btn{padding:11px 14px;min-height:44px}
  .btn-sm{padding:8px 11px}
  input,select,textarea{padding:11px 12px;font-size:16px}
  textarea{min-height:96px}
  label{margin-top:12px}
  .stat,.kpi div{flex:1 1 calc(50% - 6px);padding:12px 13px}
  .stat b{font-size:23px}
  .kpi b{font-size:var(--text-lg)}
  .wb-list{max-height:none}
  .wb-item{padding:11px 9px}
  .wb-head{white-space:normal;flex-wrap:wrap}
  .wb-card table td:nth-child(1),.wb-card table td:nth-child(2){width:auto}
  .donut{width:130px;height:130px;flex:0 0 130px}
  pre{font-size:11.5px;padding:12px;max-height:none}
  footer{padding:14px 12px 30px;text-align:center}
  .msg{padding:11px 12px}
  .empty{padding:var(--space-xl) var(--space-md)}
  .toast-stack{right:10px;left:10px;bottom:10px}
}

/* ── меньше движения: всё, что двигается, замирает ───────────────────────── */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:1ms !important;animation-delay:0ms !important;
     animation-iteration-count:1 !important;transition-duration:1ms !important;
     transition-delay:0ms !important;scroll-behavior:auto !important}
  .skel{background:var(--skel-b)}
  .skel,.loading-dot,.spin{animation:none !important}
  .loading-dot{opacity:1;scale:1}
  .count{animation:none;--n:var(--to)}
}

@media print{
  header,nav,footer,.gsearch,.theme-toggle,.toast-stack,.palette,.copy-btn,
  .dochead .page-actions,.wb-queue,.wb-edit,.wb-reply,.wb-access,.wb-bulk,.wb-tools,
  .no-print{display:none!important}
  main{margin:0;padding:0;max-width:none}
  .workbench{grid-template-columns:1fr}
  .wb-list{max-height:none;overflow:visible}
  /* На бумаге многоточие не видно - текст обрезался бы молча. Поэтому у
     шапки карточки отменяем и ellipsis, и запрет на перенос строк. */
  .wb-who,.wb-fio,.wb-brief,.wb-name,.wb-group,.wb-id,.wb-when,.wb-access-name{
    white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
  .card{break-inside:avoid;box-shadow:none}
  .card:hover{transform:none}
}

/* подвал с версией: внизу каждой страницы, на печати не печатается */
.foot-ver{max-width:1400px;margin:18px auto 0;padding:0 18px 26px;
          color:var(--mut);font-size:12px;display:flex;gap:8px;
          align-items:center;flex-wrap:wrap}
.foot-ver a{color:var(--mut)}
@media print{.foot-ver{display:none}}

"""

STYLESHEET: str = "\n".join([
    "/* Оформление панели: токены, тёмная тема по умолчанию, светлая по data-theme. */",
    theme_root_css(),
    light_rules(),
    _BASE_CSS,
])


# ── переключатель темы и подсветка найденного ───────────────────────────────
# Скрипт вставляется в конец <body>: DOM к этому моменту уже разобран.
# Зависимостей нет, localStorage может быть недоступен (приватный режим) -
# тогда тема просто не запоминается, но страница работает.
_THEME_SCRIPT = """
(function () {
  var KEY = "panel-theme";
  var root = document.documentElement;

  function read() {
    try { return localStorage.getItem(KEY) || ""; } catch (err) { return ""; }
  }
  function remember(theme) {
    try { localStorage.setItem(KEY, theme); } catch (err) {}
  }
  function systemLight() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
  }
  function current() {
    var chosen = root.getAttribute("data-theme");
    if (chosen === "light" || chosen === "dark") { return chosen; }
    return systemLight() ? "light" : "dark";
  }
  function paint() {
    var now = current();
    var buttons = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].innerHTML = (now === "dark" ? '/*ICON_LIGHT*/' : '/*ICON_DARK*/');
      buttons[i].setAttribute("aria-label",
          now === "dark" ? "Включить светлую тему" : "Включить тёмную тему");
      buttons[i].setAttribute("title",
          now === "dark" ? "Светлая тема" : "Тёмная тема");
    }
  }
  function apply(theme) {
    if (theme === "light" || theme === "dark") { root.setAttribute("data-theme", theme); }
    else { root.removeAttribute("data-theme"); }
    paint();
  }
  /* подсветка строк, в которых есть набранный текст */
  function markRows(box) {
    var needle = (box.value || "").trim().toLowerCase();
    var rows = document.querySelectorAll("main table tbody tr");
    for (var i = 0; i < rows.length; i++) {
      var hit = needle.length > 1 &&
          rows[i].textContent.toLowerCase().indexOf(needle) > -1;
      if (rows[i].classList.contains("is-found") !== hit) {
        rows[i].classList.toggle("is-found", hit);
      }
    }
    box.classList.toggle("is-filled", needle.length > 0);
  }
  function start() {
    apply(read());
    var boxes = document.querySelectorAll(".gsearch input");
    for (var i = 0; i < boxes.length; i++) {
      (function (box) {
        box.addEventListener("input", function () { markRows(box); });
      })(boxes[i]);
    }
    document.addEventListener("click", function (event) {
      var target = event.target;
      var button = target && target.closest ? target.closest(".theme-toggle") : null;
      if (!button) { return; }
      event.preventDefault();
      var next = current() === "dark" ? "light" : "dark";
      remember(next);
      apply(next);
    });
    /* слэш фокусирует строку поиска - как в браузере */
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/") { return; }
      var target = event.target || {};
      var tag = target.tagName || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || target.isContentEditable) { return; }
      var box = document.querySelector(".gsearch input");
      if (box) { event.preventDefault(); box.focus(); box.select(); }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
}());
"""


# ── всплывающее сообщение: общее для всех скриптов панели ─────────────────────
# Тосты в разметке не живут (их пока нет ни на одной странице), поэтому стек
# создаётся при первом сообщении. Повторный вызов с тем же текстом не дублирует
# подсказку, а просто обновляет её.
_PANEL_TOAST_JS = """
window.panelToast = window.panelToast || function (text, kind) {
  var stack = document.querySelector(".toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toast-stack";
    document.body.appendChild(stack);
  }
  var found = null;
  for (var i = 0; i < stack.children.length; i++) {
    if (stack.children[i].getAttribute("data-toast") === text) { found = stack.children[i]; }
  }
  if (!found) {
    found = document.createElement("div");
    found.setAttribute("data-toast", text);
    found.className = "toast";
    stack.appendChild(found);
  }
  found.className = "toast" + (kind ? " toast-" + kind : "");
  found.innerHTML = (kind === "bad" ? "/*ICON_WARN*/" : "/*ICON_OK*/") +
      '<span></span>';
  found.lastChild.textContent = text;
  if (found._panelTimer) { clearTimeout(found._panelTimer); }
  found._panelTimer = setTimeout(function () {
    found.classList.add("is-out");
    setTimeout(function () { if (found.parentNode) { found.parentNode.removeChild(found); } },
        /*DELAY*/);
  }, 3200);
};
"""


def panel_toast_js(delay_ms: int = 260) -> str:
    """JS-функция ``window.panelToast(text, kind)``: всплывающая подсказка.

    Кладётся один раз на страницу; остальные скрипты панели только вызывают её,
    поэтому стек тостов создаётся один и наполняется по мере надобности.
    """
    return (_PANEL_TOAST_JS
            .replace("/*ICON_OK*/", icon("check", 16))
            .replace("/*ICON_WARN*/", icon("warning", 16))
            .replace("/*DELAY*/", str(int(delay_ms))))


# ── горячие клавиши и палитра разделов ───────────────────────────────────────
# Клавиши работают в рабочем месте с обращениями и на страницах списков:
#   j / k  - вверх и вниз по очереди,   Enter - открыть выбранное,
#   a      - в архив,                    r     - к полю ответа,
#   e      - к правке обращения,          ?     - подсказка,
#   /      - строка поиска (её обрабатывает theme_script),
#   Ctrl+K - поиск по разделам панели.
# Пока фокус в поле ввода, все клавиши молчат - иначе нельзя было бы печатать.
_HOTKEYS_SCRIPT = """
(function () {
  var SECTIONS = /*SECTIONS*/;
  var HINT = /*HINT*/;

  function editing(target) {
    if (!target) { return false; }
    var tag = target.tagName || "";
    return /^(INPUT|TEXTAREA|SELECT|OPTION)$/.test(tag) || target.isContentEditable === true;
  }
  /* нажатый Enter должен сработать на самой кнопке, а не открывать обращение */
  function clickable(target) {
    var tag = (target && target.tagName) || "";
    return /^(BUTTON|A|SUMMARY|LABEL)$/.test(tag);
  }
  function rows() { return document.querySelectorAll("[data-hk]"); }
  function quiet() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function mark(node) {
    var items = rows();
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on") !== (items[i] === node)) {
        items[i].classList.toggle("hk-on", items[i] === node);
      }
    }
    if (node && node.scrollIntoView) {
      try { node.scrollIntoView({block: "nearest", behavior: quiet() ? "auto" : "smooth"}); }
      catch (err) { node.scrollIntoView(false); }
    }
  }
  function picked() {
    var items = rows(), found = null;
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { found = items[i]; break; }
    }
    return found;
  }
  function move(step) {
    var items = rows();
    if (!items.length) { return; }
    var index = 0;
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { index = i; break; }
    }
    mark(items[Math.min(items.length - 1, Math.max(0, index + step))]);
  }
  function open(node) {
    if (!node) { return; }
    var link = node.querySelector("a[href]");
    var href = node.getAttribute("data-hk-open") || (link ? link.getAttribute("href") : "");
    if (href) { window.location.href = href; }
  }
  /* кнопки-действия на карточке: архив, ответ, правка */
  function send(action) {
    var box = document.querySelector('.wb-card form[action$="/' + action + '"]');
    if (box) { box.submit(); }
  }
  function field(name) {
    var box = document.querySelector('.wb-card [name="' + name + '"]');
    if (box) {
      box.focus();
      if (box.select) { try { box.select(); } catch (err) {} }
    }
  }
  function help() {
    var parts = [];
    for (var i = 0; i < HINT.length; i++) { parts.push(HINT[i][0] + " — " + HINT[i][1]); }
    panelToast(parts.join(" · "), null);
  }

  /* ── палитра разделов (Ctrl+K) ─────────────────────────────────────────── */
  var box = null, list = null, field_ = null, chosen = 0, shown = [];
  function build() {
    if (box) { return; }
    box = document.createElement("div");
    box.className = "palette";
    box.innerHTML = '<div class="palette-box"><input type="text" placeholder="Куда перейти?" ' +
        'autocomplete="off" aria-label="Поиск по разделам панели"><ul class="palette-list"></ul>' +
        '<div class="palette-foot">↑ и ↓ — выбор, Enter — открыть, Esc — закрыть</div></div>';
    document.body.appendChild(box);
    field_ = box.querySelector("input");
    list = box.querySelector("ul");
    box.addEventListener("click", function (event) {
      if (event.target === box) { close(); }
    });
    field_.addEventListener("input", function () { paint(field_.value); });
    field_.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { close(); return; }
      if (event.key === "ArrowDown") { move(1); event.preventDefault(); return; }
      if (event.key === "ArrowUp") { move(-1); event.preventDefault(); return; }
      if (event.key === "Enter") {
        event.preventDefault();
        if (shown[chosen]) { window.location.href = shown[chosen].getAttribute("href"); }
      }
    });
  }
  function paint(needle) {
    var want = (needle || "").trim().toLowerCase();
    list.innerHTML = "";
    shown = [];
    for (var g = 0; g < SECTIONS.length; g++) {
      var group = SECTIONS[g];
      var head = null;
      for (var i = 0; i < group[1].length; i++) {
        var item = group[1][i];
        if (want && item[0].toLowerCase().indexOf(want) < 0) { continue; }
        if (!head) {
          head = document.createElement("li");
          head.className = "pal-group";
          head.textContent = group[0];
          list.appendChild(head);
        }
        var row = document.createElement("li");
        var link = document.createElement("a");
        link.href = item[1];
        link.innerHTML = item[2] + "<span></span>";
        link.lastChild.textContent = item[0];
        row.appendChild(link);
        list.appendChild(row);
        shown.push(link);
      }
    }
    if (!shown.length) {
      var empty = document.createElement("li");
      empty.className = "pal-group";
      empty.textContent = "Ничего не нашлось";
      list.appendChild(empty);
    }
    chosen = 0;
    paintChoice();
  }
  function paintChoice() {
    for (var i = 0; i < shown.length; i++) { shown[i].classList.toggle("on", i === chosen); }
  }
  function move(step) {
    if (!shown.length) { return; }
    chosen = Math.min(shown.length - 1, Math.max(0, chosen + step));
    paintChoice();
    try { shown[chosen].scrollIntoView({block: "nearest"}); } catch (err) {}
  }
  function open() { build(); paint(""); box.classList.add("is-open"); field_.focus(); }
  function close() { if (box) { box.classList.remove("is-open"); } }

  document.addEventListener("keydown", function (event) {
    if (event.key === "k" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (box && box.classList.contains("is-open")) { close(); } else { open(); }
      return;
    }
    if (box && box.classList.contains("is-open")) { return; }
    if (editing(event.target) || event.ctrlKey || event.metaKey || event.altKey) { return; }
    var key = event.key;
    if (key === "?") { event.preventDefault(); help(); return; }
    if (key === "j") { event.preventDefault(); move(1); return; }
    if (key === "k") { event.preventDefault(); move(-1); return; }
    if (key === "a") { event.preventDefault(); send("archive"); return; }
    if (key === "r") { event.preventDefault(); field("text"); return; }
    if (key === "e") { event.preventDefault(); field("text_content"); return; }
    if (key === "Enter" && !clickable(event.target)) { event.preventDefault(); open(picked()); }
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { if (picked()) { mark(picked()); } });
  } else if (picked()) { mark(picked()); }
}());
"""

# Подсказка по «?»: короткая, помещается в тост на любом экране.
HOTKEY_HINT: tuple[tuple[str, str], ...] = (
    ("j / k", "по очереди"),
    ("Enter", "открыть"),
    ("a", "в архив"),
    ("r", "ответ"),
    ("e", "правка"),
    ("/", "поиск"),
    ("Ctrl+K", "разделы"),
)


def hotkeys_script(groups: Iterable[Sequence[Sequence[str]]] = ()) -> str:
    """JS для конца ``<body>``: горячие клавиши и палитра разделов по ``Ctrl+K``.

    ``groups`` - разделы панели как ``[(группа, [(название, путь, иконка), ...]), ...]``;
    из них собирается палитра, поэтому список разделов не дублируется в скрипте.
    Клавиши игнорируются, когда фокус в поле ввода, а при выключенной анимации
    (``prefers-reduced-motion``) прокрутка к выбранной строке идёт без сглаживания.
    """
    sections = [[str(group[0]), [[str(item[0]), str(item[1]), str(item[2])]
                                for item in group[1]]]
                for group in groups if group and group[1]]
    payload = json.dumps(sections, ensure_ascii=False).replace("<", "\\u003c")
    hint = json.dumps([list(item) for item in HOTKEY_HINT], ensure_ascii=False)
    return (_HOTKEYS_SCRIPT
            .replace("/*SECTIONS*/", payload)
            .replace("/*HINT*/", hint))


# ── копирование по клику и сохранённые фильтры ───────────────────────────────
_ACTIONS_SCRIPT = """
(function () {
  /* ── копирование значения: MAX ID, код группы, код приглашения, ссылка ── */
  function put(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "readonly");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(box);
      if (ok) { resolve(); } else { reject(new Error("copy")); }
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("[data-copy]") : null;
    if (!button) { return; }
    event.preventDefault();
    var note = button.getAttribute("data-copy-note") || "Скопировано";
    put(button.getAttribute("data-copy") || "").then(function () {
      button.classList.add("is-done");
      panelToast(note, "ok");
    }, function () {
      panelToast("Браузер не дал скопировать - выделите значение вручную", "bad");
    });
  });

  /* ── сохранённые фильтры рабочего места ───────────────────────────────── */
  var KEY = "panel-filters";
  var form = document.querySelector("[data-filters]");
  if (!form) { return; }
  var names = (form.getAttribute("data-filters") || "status,category,q").split(",");
  function read() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (err) { saved = {}; }
    return saved;
  }
  function write() {
    var data = {};
    for (var i = 0; i < names.length; i++) {
      var box = form.querySelector('[name="' + names[i] + '"]');
      if (box && box.value) { data[names[i]] = box.value; }
    }
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (err) {}
  }
  function apply(saved) {
    var changed = false;
    for (var i = 0; i < names.length; i++) {
      var box = form.querySelector('[name="' + names[i] + '"]');
      if (box && !box.value && saved[names[i]]) { box.value = saved[names[i]]; changed = true; }
    }
    return changed;
  }
  /* восстанавливаем только когда в адресе фильтров не было: ссылка с фильтром
     должна побеждать, иначе «/?scope=waiting» открывался бы чужим фильтром */
  if (form.getAttribute("data-filters-saved") === "1") {
    if (apply(read())) { form.submit(); return; }
  }
  form.addEventListener("change", write);
  form.addEventListener("submit", write);
  var reset = document.querySelector("[data-filters-reset]");
  if (reset) {
    reset.addEventListener("click", function () {
      try { localStorage.removeItem(KEY); } catch (err) {}
      panelToast("Фильтры сброшены", null);
    });
  }
}());
"""


def actions_script() -> str:
    """JS для конца ``<body>``: копирование по клику и сохранённые фильтры.

    Копирование живёт на кнопках ``[data-copy]`` (MAX ID, код группы, код
    приглашения, ссылка на обращение) и подтверждается тостом. Фильтры рабочего
    места лежат в ``localStorage`` под ключом ``panel-filters`` и возвращаются при
    следующем открытии страницы, если в адресе фильтра не было.
    """
    return _ACTIONS_SCRIPT


def theme_script() -> str:
    """Маленький JS для конца ``<body>``: переключение темы и подсветка поиска.

    Выбор темы пишется в ``localStorage`` под ключом ``panel-theme`` и
    возвращается как ``data-theme`` на ``<html>``. Если выбор не делали, тема
    следует за системной настройкой. Клавиша ``/`` (фокус в поиск) остаётся
    здесь: остальные клавиши живут в ``hotkeys_script()`` и ``/`` не трогают.
    """
    return (_THEME_SCRIPT
            .replace("/*ICON_LIGHT*/", icon("moon", 18))
            .replace("/*ICON_DARK*/", icon("sun", 18)))
