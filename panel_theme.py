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

Стиль, который держит лист (он же проверяется и в ``tests/test_panel_theme.py``):

* плоские поверхности вместо тяжёлых теней: карточка, показатель и плашка
  отличаются от страницы рамкой и своим слоем, а тень остаётся только на
  наведении и у настоящих оверлеев (палитра, тосты);
* один акцент на всё: он красит линию под шапкой, полосу у выбранного пункта
  меню, знак колледжа, заголовок страницы и счётчики; состояние «сейчас тут»
  показывают три вещи сразу - полоса, заливка и цвет текста;
* цифры - моноширинным шрифтом с разрядами (``.num``, ``.stat b``, ``.kpi b``,
  ``.feed-time``, ``.wb-date``), поэтому колонки чисел стоят по разрядам;
  в заголовках таблиц моноширинного шрифта нет намеренно - русские слова в нём
  шире, и колонка с длинными ФИО отдала бы под них лишние пиксели;
* сообщения и метки состояния - насыщенным цветом на своей мягкой подложке
  (контраст от 5.7:1), а не цветом «для заливки»: в светлой теме тот давал
  1.1:1, и сообщение было нечитаемым.

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
from pathlib import Path
from collections.abc import Iterable, Sequence

# ── токены оформления ────────────────────────────────────────────────────────
# Тёмная тема - основная: панель чаще открывают ночью, и тёмный фон не бьёт
# по глазам. Значения подобраны так, чтобы приглушённый текст (--mut) читался
# на тёмной поверхности: контраст около 7:1, а не «серый по серому».
TOKENS: dict[str, str] = {
    # ── тёмная тема (по умолчанию) ─────────────────────────────────────────
    # Холодный графит вместо прежнего сине-чёрного: насыщенный синий на
    # почти чёрном фоне «плыл», а графит с мятным акцентом держит контраст
    # и не даёт фону казаться грязным. Подписи (--mut) рассчитаны на 6.8:1
    # к поверхности, а не на 4.5:1 «впритык»: тёмная тема открывают ночью,
    # и приглушённый текст обязан оставаться читаемым.
    "dark.bg": "#0f1216",                 # фон страницы
    "dark.bg-deep": "#090b0e",            # самый глубокий слой
    "dark.surface": "#171b21",            # карточка, панель меню
    "dark.surface-raised": "#1e242c",     # наведение, плашки
    "dark.surface-sunken": "#12161c",     # поля ввода, шапка таблицы
    "dark.line": "#2c333d",               # граница
    "dark.line-soft": "#232932",          # мягкая граница: строки таблицы
    "dark.line-strong": "#3d4854",        # граница в фокусе
    "dark.ink": "#edf1f6",                # основной текст
    "dark.ink-soft": "#c5cedb",           # вторичный текст
    "dark.mut": "#98a4b4",                # подписи, даты
    "dark.acc": "#35d9b1",                # акцент: ссылки, активный пункт
    "dark.acc-ink": "#04211c",            # текст на акценте
    "dark.mark-plate": "#ffffff",         # подложка знака: белая в обеих темах
    "dark.acc-soft": "#0e2b26",           # мягкая подложка акцента
    "dark.acc-line": "#1f5f4f",           # граница мягкой подложки
    "dark.ok": "#4fd47f",
    "dark.ok-ink": "#05200f",
    "dark.ok-soft": "#10281a",
    "dark.ok-line": "#21502f",
    "dark.bad": "#ff8b7f",
    "dark.bad-ink": "#2a0a06",
    "dark.bad-soft": "#2c1512",
    "dark.bad-line": "#5f2c26",
    "dark.warn": "#f2c65c",
    "dark.warn-ink": "#241903",
    "dark.warn-soft": "#2a2110",
    "dark.warn-line": "#55421a",
    "dark.surface-head": "#131820",       # шапка страницы и таблицы
    "dark.head-ink": "#edf1f6",
    "dark.head-mut": "#a3b0c0",
    "dark.head-field": "rgba(255,255,255,.07)",
    "dark.head-line": "#39424f",
    "dark.code-bg": "#0b0e12",            # журнал: тёмный блок в обеих темах
    "dark.code-ink": "#ccd8e6",
    "dark.chart-a": "#35d9b1",
    "dark.chart-b": "#4fd47f",
    "dark.chart-c": "#f2c65c",
    "dark.chart-d": "#ff8b7f",
    "dark.chart-e": "#b49bff",
    "dark.chart-f": "#5ad4e0",
    "dark.chart-g": "#ff9ecb",
    "dark.chart-h": "#98a4b4",
    "dark.chart-grid": "#232a34",
    "dark.chart-axis": "#98a4b4",
    "dark.chart-track": "#1e242c",
    "dark.skel-a": "#181d25",
    "dark.skel-b": "#242c36",
    "dark.sel-bg": "#1f5f4f",             # выделение текста мышью
    "dark.glow": "rgba(53,217,177,.16)",  # подсветка найденной строки
    "dark.overlay": "rgba(6,9,12,.74)",
    "dark.shadow-sm": "0 1px 2px rgba(0,0,0,.40)",
    "dark.shadow-md": "0 1px 2px rgba(0,0,0,.32),0 8px 22px -14px rgba(0,0,0,.68)",
    "dark.shadow-lg": "0 2px 6px rgba(0,0,0,.36),0 20px 40px -20px rgba(0,0,0,.80)",
    "dark.shadow-focus": "0 0 0 3px rgba(53,217,177,.26)",
    "dark.canvas-a": "rgba(53,217,177,.085)",
    "dark.canvas-b": "rgba(111,125,238,.075)",
    "dark.shell-glass": "rgba(18,24,31,.88)",
    "dark.nav-glass": "rgba(19,25,32,.94)",
    "dark.hero-start": "#153a35",
    "dark.hero-end": "#18282e",
    "dark.hero-line": "#28594e",
    "dark.hero-ink": "#effdf9",
    "dark.hero-muted": "#a6d6ca",
    "dark.gradient-end": "#80ebcc",
    "dark.decor-line": "rgba(130,224,200,.16)",
    "dark.brand-shadow": "0 10px 38px -14px rgba(26,216,169,.28)",

    # ── светлая тема: белые поверхности и официальный синий акцент ────────
    # Геометрия и тёмная тема прежние. Насыщенный синий читается на белом;
    # зелёный, красный и янтарный оставлены для смысловых состояний.
    "light.bg": "#f5f8ff",
    "light.bg-deep": "#e8effa",
    "light.surface": "#ffffff",
    "light.surface-raised": "#f8faff",
    "light.surface-sunken": "#eef3fb",
    "light.line": "#d4dff0",
    "light.line-soft": "#e4ebf5",
    "light.line-strong": "#a8bad6",
    "light.ink": "#172c49",
    "light.ink-soft": "#344d6c",
    "light.mut": "#516783",               # тёмный текст, а не блёклый синий
    "light.acc": "#1d4ed8",
    "light.acc-ink": "#ffffff",
    "light.mark-plate": "#ffffff",
    "light.acc-soft": "#eaf1ff",
    "light.acc-line": "#afc5f4",
    "light.ok": "#0f6b31",
    "light.ok-ink": "#ffffff",
    "light.ok-soft": "#e4f4ea",
    "light.ok-line": "#a9d9bd",
    "light.bad": "#a72f26",
    "light.bad-ink": "#ffffff",
    "light.bad-soft": "#fbeae8",
    "light.bad-line": "#eebcb6",
    "light.warn": "#7a5003",
    "light.warn-ink": "#ffffff",
    "light.warn-soft": "#fbf1d9",
    "light.warn-line": "#e3c88f",
    "light.surface-head": "#ffffff",
    "light.head-ink": "#172c49",
    "light.head-mut": "#516783",
    "light.head-field": "rgba(29,78,216,.05)",
    "light.head-line": "#d4dff0",
    "light.code-bg": "#171b21",
    "light.code-ink": "#d7e2ee",
    "light.chart-a": "#1d4ed8",
    "light.chart-b": "#0f6b31",
    "light.chart-c": "#7a5003",
    "light.chart-d": "#a72f26",
    "light.chart-e": "#6d43c4",
    "light.chart-f": "#0b6a72",
    "light.chart-g": "#b02a63",
    "light.chart-h": "#516783",
    "light.chart-grid": "#dce6f4",
    "light.chart-axis": "#516783",
    "light.chart-track": "#e4ecf8",
    "light.skel-a": "#e8effa",
    "light.skel-b": "#f8faff",
    "light.sel-bg": "#afc5f4",
    "light.glow": "rgba(29,78,216,.14)",
    "light.overlay": "rgba(17,38,70,.46)",
    "light.shadow-sm": "0 1px 2px rgba(23,44,73,.08)",
    "light.shadow-md": "0 1px 2px rgba(23,44,73,.06),0 8px 22px -14px rgba(23,44,73,.22)",
    "light.shadow-lg": "0 2px 6px rgba(23,44,73,.07),0 20px 40px -20px rgba(23,44,73,.28)",
    "light.shadow-focus": "0 0 0 3px rgba(29,78,216,.18)",
    "light.canvas-a": "rgba(29,78,216,.045)",
    "light.canvas-b": "rgba(45,114,201,.035)",
    "light.shell-glass": "rgba(255,255,255,.94)",
    "light.nav-glass": "rgba(248,250,255,.96)",
    "light.hero-start": "#173b85",
    "light.hero-end": "#1e4f99",
    "light.hero-line": "#587ec1",
    "light.hero-ink": "#ffffff",
    "light.hero-muted": "#d9e7ff",
    "light.gradient-end": "#245fb3",
    "light.decor-line": "rgba(171,201,247,.22)",
    "light.brand-shadow": "0 10px 36px -16px rgba(29,78,216,.22)",

    # ── независимые от темы значения ──────────────────────────────────────
    # радиусы: три ступени, никаких «своих» значений по месту
    "base.radius-xs": "8px",
    "base.shadow-inset": "0 2px 6px rgba(0,0,0,.24)",
    "base.radius-sm": "10px",
    "base.radius-md": "18px",
    "base.radius-lg": "24px",
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
    "base.text-xxl": "30px",
    "base.text-num": "32px",
    "base.weight-normal": "400",
    "base.weight-medium": "500",
    "base.weight-semi": "600",
    "base.weight-bold": "700",
    # интервалы: шаг 4/8/12/16/22/30, как и раньше, но карточка и панель
    # берут из шкалы, а не пишут свои пиксели
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
    "base.sidebar": "244px",               # ширина бокового меню
    "base.header-h": "72px",                # высота шапки
    "base.content-max": "1600px",           # предел ширины содержимого
    "base.tap-min": "44px",                 # минимальная цель нажатия на телефоне
    "base.field-min": "38px",               # высота поля и кнопки: одна у всех
    "base.field-line": "1.45",              # межстрочный интервал внутри поля
    "base.nav-indent": "17px",              # отступ подпункта меню
    "base.table-min": "460px",              # минимальная ширина таблицы переписки
    "base.table-min-wide": "600px",         # ... и таблицы с произвольными колонками
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
    "logout": '<path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h5"/>'
              '<path d="M10 12h10m-4-4 4 4-4 4"/>',
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
    "audit": '<path d="M12 3.2 19.2 6v5.4c0 4.5-2.9 7.7-7.2 9.4-4.3-1.7-7.2-4.9-7.2-9.4V6L12 3.2Z"/>'
             '<path d="M8.7 12.1 10.8 14.2 15.6 9.4"/>',
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
    "spark": '<path d="M12 3.8c.9 4.2 3.8 7.3 8.2 8.2-4.4.9-7.3 3.8-8.2 8.2-.9-4.4-3.8-7.3-8.2-8.2 4.4-.9 7.3-3.8 8.2-8.2Z"/>',
    "dot": '<circle cx="12" cy="12" r="3.2"/>',
}

FALLBACK_ICON = "dot"          # на неизвестное имя - нейтральная точка
ICON_STROKE = "1.75"           # толщина обводки у всех иконок одинаковая

# Путь раздела панели -> имя иконки. Заменяет прежний NAV_ICONS с эмодзи.
ICON_NAMES_BY_PATH: dict[str, str] = {
    "/": "home",
    "/activity": "activity",
    "/tickets": "tickets",
    "/analytics": "analytics",
    "/people": "people",
    "/nostaff": "user-off",
    "/college": "college",
    "/students": "students",
    "/staff": "staff",
    "/staff-access": "lock",
    "/invites": "link",
    "/access": "access",
    "/templates": "templates",
    "/groups": "groups",
    "/schedules": "schedules",
    "/broadcasts": "broadcasts",
    "/database": "database",
    "/data": "archive",
    "/settings": "settings",
    "/test": "check",
    "/logs": "logs",
    "/audit": "audit",
    "/feedback": "reply",
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
_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
_BASE_CSS_FILE = _STATIC_DIR / "panel.css"
_BASE_CSS = _BASE_CSS_FILE.read_text(encoding="utf-8-sig") if _BASE_CSS_FILE.exists() else ""

STYLESHEET: str = "\n".join([
    "/* Оформление панели: токены, тёмная тема по умолчанию, светлая по data-theme. */",
    theme_root_css(),
    light_rules(),
    _BASE_CSS,
    # Знак бренда -- растровая эмблема колледжа: квадратный слот, пропорции знака.
    "img.ico-navigator{object-fit:contain}",
    # Эмблема несёт свой синий цвет, на тёмных плашках она глохнет --
    # в тёмных темах знак стоит на белой скруглённой подложке.
    'html:not([data-theme="light"]) :is(.brand,.login-brand) .logo{background:var(--mark-plate);border-color:var(--mark-plate)}',
    ".orbit-core{left:246px;top:24px}",
    ".orbit-core .ico{width:64px;height:64px;background:var(--mark-plate);padding:10px;border-radius:18px;box-sizing:border-box}",
    ".hero-sign .ico{background:var(--mark-plate);padding:9px;border-radius:16px;box-sizing:border-box}",
])


def theme_boot_script() -> str:
    """Применяет сохранённую тему в head, до первого кадра страницы."""
    return """(function(){try{var t=localStorage.getItem('panel-theme');
if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}
else if(window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches){
document.documentElement.setAttribute('data-theme','light');}}catch(e){}}());"""


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
    stack.setAttribute("aria-live", kind === "bad" ? "assertive" : "polite");
    stack.setAttribute("aria-atomic", "true");
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
  found.setAttribute("role", kind === "bad" ? "alert" : "status");
  found.innerHTML = (kind === "bad" ? "/*ICON_WARN*/" : "/*ICON_OK*/") +
      '<span></span><button type="button" class="toast-close" aria-label="Закрыть">×</button>';
  found.querySelector("span").textContent = text;
  found.addEventListener("mouseenter", function () { clearTimeout(found._panelTimer); });
  found.addEventListener("focusin", function () { clearTimeout(found._panelTimer); });
  found.querySelector("button").addEventListener("click", function () {
    if (found.parentNode) { found.parentNode.removeChild(found); }
  });
  if (found._panelTimer) { clearTimeout(found._panelTimer); }
  found._panelTimer = setTimeout(function () {
    found.classList.add("is-out");
    setTimeout(function () { if (found.parentNode) { found.parentNode.removeChild(found); } },
        /*DELAY*/);
  }, 3200);
};
(function () {
  function showFlash() {
    var flash = document.querySelector("[data-flash]");
    if (!flash) { return; }
    var text = (flash.textContent || "").trim();
    if (text) { panelToast(text, flash.getAttribute("data-kind") || null); }
    flash.classList.add("msg-fallback-hidden");
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", showFlash); }
  else { showFlash(); }
}());
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
  var RESULT_ICONS = /*RESULT_ICONS*/;
  var HINT = /*HINT*/;

  function editing(target) {
    if (!target) { return false; }
    var tag = target.tagName || "";
    return /^(INPUT|TEXTAREA|SELECT|OPTION)$/.test(tag) || target.isContentEditable === true;
  }
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
      items[i].classList.toggle("hk-on", items[i] === node);
    }
    if (node && node.scrollIntoView) {
      try { node.scrollIntoView({block: "nearest", behavior: quiet() ? "auto" : "smooth"}); }
      catch (err) { node.scrollIntoView(false); }
    }
  }
  function picked() {
    var items = rows();
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { return items[i]; }
    }
    return null;
  }
  function move(step) {
    var items = rows();
    if (!items.length) { return; }
    var index = -1;
    for (var i = 0; i < items.length; i++) {
      if (items[i].classList.contains("hk-on")) { index = i; break; }
    }
    mark(items[Math.min(items.length - 1, Math.max(0, index + step))]);
  }
  function openRow(node) {
    if (!node) { return; }
    var link = node.querySelector("a[href]");
    var href = node.getAttribute("data-hk-open") || (link ? link.getAttribute("href") : "");
    if (href) { window.location.href = href; }
  }
  function send(action) {
    var form = document.querySelector('.wb-card form[action$="/' + action + '"]');
    if (form) { form.submit(); }
  }
  function focusField(name) {
    var field = document.querySelector('.wb-card [name="' + name + '"]');
    if (field) { field.focus(); if (field.select) { try { field.select(); } catch (err) {} } }
  }
  function help() {
    var parts = [];
    for (var i = 0; i < HINT.length; i++) { parts.push(HINT[i][0] + " — " + HINT[i][1]); }
    panelToast(parts.join(" · "), null);
  }

  var palette = null, list = null, search = null, chosen = 0, shown = [], ALL = [];
  var requestNo = 0, waitTimer = null, priorFocus = null;
  function collect(items, group, out, nested) {
    for (var i = 0; i < items.length; i++) {
      out.push([group, String(items[i][0]), String(items[i][1]),
                String(items[i][2] || ""), nested ? 1 : 0]);
      if (items[i][3] && items[i][3].length) { collect(items[i][3], group, out, true); }
    }
  }
  function build() {
    if (palette) { return; }
    palette = document.createElement("div");
    palette.className = "palette";
    palette.setAttribute("role", "dialog");
    palette.setAttribute("aria-modal", "true");
    palette.setAttribute("aria-label", "Поиск по панели");
    palette.innerHTML = '<div class="palette-box"><input type="text" placeholder="Раздел, обращение, человек или группа" ' +
        'autocomplete="off" aria-label="Поиск по панели"><ul class="palette-list"></ul>' +
        '<div class="palette-foot">↑ и ↓ — выбор, Enter — открыть, Esc — закрыть</div></div>';
    document.body.appendChild(palette);
    for (var g = 0; g < SECTIONS.length; g++) {
      if (SECTIONS[g] && SECTIONS[g].length) { collect(SECTIONS[g][1], String(SECTIONS[g][0]), ALL, false); }
    }
    search = palette.querySelector("input");
    list = palette.querySelector("ul");
    palette.addEventListener("click", function (event) { if (event.target === palette) { closePalette(); } });
    search.addEventListener("input", function () {
      requestNo++;
      paintSections(search.value);
      clearTimeout(waitTimer);
      var value = search.value.trim();
      if (value.length >= 2 || /^[0-9]+$/.test(value)) {
        waitTimer = setTimeout(function () { remoteSearch(value); }, 180);
      }
    });
    palette.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") { return; }
      var focusable = [search].concat(shown);
      var index = focusable.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) {
        event.preventDefault(); focusable[focusable.length - 1].focus();
      } else if (!event.shiftKey && index === focusable.length - 1) {
        event.preventDefault(); search.focus();
      }
    });
    search.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { closePalette(); return; }
      if (event.key === "ArrowDown") { choose(1); event.preventDefault(); return; }
      if (event.key === "ArrowUp") { choose(-1); event.preventDefault(); return; }
      if (event.key === "Enter") {
        event.preventDefault();
        if (shown[chosen]) { window.location.href = shown[chosen].getAttribute("href"); }
      }
    });
  }
  function addGroup(name) {
    var head = document.createElement("li");
    head.className = "pal-group";
    head.textContent = name;
    list.appendChild(head);
  }
  function addResult(group, label, href, iconHtml, nested) {
    var link = document.createElement("a");
    link.href = href;
    link.innerHTML = (iconHtml || "") + "<span></span>";
    link.lastChild.textContent = label;
    if (nested) { link.className = "pal-sub"; link.setAttribute("data-group", group); }
    var item = document.createElement("li");
    item.appendChild(link);
    list.appendChild(item);
    shown.push(link);
  }
  function paintSections(needle) {
    var want = (needle || "").trim().toLowerCase();
    list.innerHTML = "";
    shown = [];
    var group = "";
    for (var i = 0; i < ALL.length; i++) {
      var row = ALL[i];
      if (want && row[1].toLowerCase().indexOf(want) < 0) { continue; }
      if (row[0] !== group) { group = row[0]; addGroup(group); }
      addResult(row[0], row[1], row[2], row[3], row[4]);
    }
    chosen = 0;
    paintChoice();
  }
  function loading() {
    addGroup("Ищем в данных");
    var item = document.createElement("li");
    item.className = "pal-loading";
    item.innerHTML = '<span class="skel skel-line"></span><span class="skel skel-line"></span>';
    list.appendChild(item);
  }
  function iconFor(name) { return RESULT_ICONS[name] || RESULT_ICONS.people; }
  function csrfToken() {
    var field = document.querySelector('form[action="/panel/logout"] input[name="csrf"]');
    return field ? field.value : "";
  }
  function remoteSearch(value) {
    var serial = ++requestNo;
    loading();
    var body = new URLSearchParams();
    body.set("q", value);
    body.set("csrf", csrfToken());
    fetch("/panel/search/quick", {method: "POST", credentials: "same-origin", body: body,
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}})
      .then(function (response) { if (!response.ok) { throw new Error("search"); } return response.json(); })
      .then(function (payload) {
        if (serial !== requestNo || search.value.trim() !== value) { return; }
        paintSections(value);
        var loadingNode = list.querySelector(".pal-loading");
        if (loadingNode) { loadingNode.remove(); }
        var results = payload.results || [], group = "";
        for (var i = 0; i < results.length; i++) {
          if (results[i].kind !== group) { group = results[i].kind; addGroup(group); }
          if (!results[i].href || results[i].href.indexOf("/panel/") !== 0) { continue; }
          addResult(group, results[i].label, results[i].href, iconFor(results[i].icon));
        }
        if (!shown.length) { addGroup("Ничего не нашлось"); }
        chosen = 0; paintChoice();
      }).catch(function () {
        if (serial !== requestNo || search.value.trim() !== value) { return; }
        var loadingNode = list.querySelector(".pal-loading");
        if (loadingNode) { loadingNode.remove(); }
        panelToast("Поиск временно недоступен", "bad");
      });
  }
  function paintChoice() {
    for (var i = 0; i < shown.length; i++) { shown[i].classList.toggle("on", i === chosen); }
  }
  function choose(step) {
    if (!shown.length) { return; }
    chosen = Math.min(shown.length - 1, Math.max(0, chosen + step));
    paintChoice();
    try { shown[chosen].scrollIntoView({block: "nearest"}); } catch (err) {}
  }
  function openPalette() {
    priorFocus = document.activeElement; build(); search.value = ""; requestNo++;
    paintSections(""); palette.classList.add("is-open"); search.focus();
  }
  function closePalette() {
    requestNo++; clearTimeout(waitTimer);
    if (palette) { palette.classList.remove("is-open"); }
    if (priorFocus && priorFocus.focus) { priorFocus.focus(); }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target && event.target.closest ? event.target.closest("[data-palette-open]") : null;
    if (trigger) { event.preventDefault(); openPalette(); }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "k" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (palette && palette.classList.contains("is-open")) { closePalette(); } else { openPalette(); }
      return;
    }
    if (palette && palette.classList.contains("is-open")) {
      if (event.key === "Escape") { event.preventDefault(); closePalette(); }
      return;
    }
    if (editing(event.target) || event.ctrlKey || event.metaKey || event.altKey) { return; }
    var key = event.key;
    if (key === "/") { event.preventDefault(); openPalette(); return; }
    if (key === "?") { event.preventDefault(); help(); return; }
    if (key === "j") { event.preventDefault(); move(1); return; }
    if (key === "k") { event.preventDefault(); move(-1); return; }
    if (key === "a") { event.preventDefault(); send("archive"); return; }
    if (key === "r") { event.preventDefault(); focusField("text"); return; }
    if (key === "e") { event.preventDefault(); focusField("text_content"); return; }
    if (key === "Enter" && !clickable(event.target)) { event.preventDefault(); openRow(picked()); }
  });
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
    ("Ctrl+K", "поиск"),
)


def _palette_item(item: Sequence[Sequence[str]]) -> list:
    """Пункт палитры: название, путь, иконка и - необязательно - подпункты.

    Четвёртый элемент нужен подкатегориям меню: развернуть их должен сам
    скрипт, иначе подпункты пришлось бы дописывать в палитру руками. Раздел
    без подпунктов остаётся тройкой, поэтому прежние вызовы не меняются.
    """
    row = [str(item[0]), str(item[1]), str(item[2]) if len(item) > 2 else ""]
    subs = [_palette_item(sub) for sub in (item[3] if len(item) > 3 else ()) or ()]
    if subs:
        row.append(subs)
    return row


def hotkeys_script(groups: Iterable[Sequence[Sequence[str]]] = ()) -> str:
    """JS для конца ``<body>``: горячие клавиши и палитра разделов по ``Ctrl+K``.

    ``groups`` - разделы панели как ``[(группа, [(название, путь, иконка), ...]), ...]``;
    из них собирается палитра, поэтому список разделов не дублируется в скрипте.
    Пункт может нести подпункты подкатегории четвёртым элементом - они попадают
    в палитру как вложенные строки с отступом.
    Клавиши игнорируются, когда фокус в поле ввода, а при выключенной анимации
    (``prefers-reduced-motion``) прокрутка к выбранной строке идёт без сглаживания.
    """
    sections = [[str(group[0]), [_palette_item(item) for item in group[1]]]
                for group in groups if group and group[1]]
    payload = json.dumps(sections, ensure_ascii=False).replace("<", "\\u003c")
    hint = json.dumps([list(item) for item in HOTKEY_HINT], ensure_ascii=False)
    return (_HOTKEYS_SCRIPT
            .replace("/*SECTIONS*/", payload)
            .replace("/*HINT*/", hint)
            .replace("/*RESULT_ICONS*/", json.dumps(
                {name: icon(name, 18) for name in ("tickets", "people", "staff", "groups")},
                ensure_ascii=False).replace("<", "\\u003c")))


# ── положение меню: отдельно от прокрутки документа и скрипта форм ──────────
_NAV_SCROLL_SCRIPT = """
(function () {
  var nav = document.querySelector(".app-nav");
  var user = document.body && document.body.dataset.panelUser;
  if (!nav || typeof user !== "string" || !user.trim()) { return; }
  var KEY = "panel-nav-scroll:" + user.trim();
  var positions = {};
  var mode = layoutMode();
  var geometry = null;
  var saveFrame = null;

  function layoutMode() { return window.innerWidth > 1000 ? "desktop" : "mobile"; }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function clamp(value, maximum) { return Math.min(Math.max(0, value), maximum); }
  function read() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(KEY) || "{}");
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) { return; }
      ["desktop", "mobile"].forEach(function (axis) {
        if (finite(saved[axis])) { positions[axis] = Math.max(0, saved[axis]); }
      });
    } catch (err) {}
  }
  function write() {
    try { sessionStorage.setItem(KEY, JSON.stringify(positions)); } catch (err) {}
  }
  function measure() {
    var vertical = mode === "desktop";
    var size = vertical ? nav.clientHeight : nav.clientWidth;
    var extent = vertical ? nav.scrollHeight : nav.scrollWidth;
    if (!finite(size) || !finite(extent) || size <= 0) { return null; }
    return {property: vertical ? "scrollTop" : "scrollLeft", size: size,
            maximum: Math.max(0, extent - size), vertical: vertical};
  }
  function remember() {
    /* CSS уже мог сменить ось и обнулить старую до события resize. */
    if (layoutMode() !== mode) { return; }
    var box = measure();
    if (!box || box.maximum <= 0 || !geometry ||
        box.size !== geometry.size || box.maximum !== geometry.maximum) { return; }
    var value = nav[box.property];
    if (finite(value)) { positions[mode] = clamp(value, box.maximum); }
  }
  function revealActive(box) {
    var active = nav.querySelector('a[aria-current="page"], a.on');
    if (!active) { return; }
    var frame = nav.getBoundingClientRect();
    var link = active.getBoundingClientRect();
    var start = box.vertical ? frame.top + nav.clientTop : frame.left + nav.clientLeft;
    var end = start + box.size;
    var first = box.vertical ? link.top : link.left;
    var last = box.vertical ? link.bottom : link.right;
    if (![start, end, first, last].every(finite)) { return; }
    var delta = 0;
    /* Ближайший край; пункт шире окна уже может занимать всё видимое меню. */
    if (first < start && last <= end) { delta = Math.max(first - start, last - end); }
    else if (last > end && first >= start) { delta = Math.min(first - start, last - end); }
    var value = nav[box.property] + delta;
    if (delta && finite(value)) { nav[box.property] = clamp(value, box.maximum); }
  }
  function restore() {
    mode = layoutMode();
    /* Измерение вызывает layout: позиция возвращается до первого кадра. */
    var box = measure();
    if (!box) { return; }
    geometry = box;
    if (finite(positions[mode])) {
      nav[box.property] = clamp(positions[mode], box.maximum);
    }
    revealActive(box);
    remember();
  }
  function flush() {
    if (layoutMode() !== mode) { restore(); }
    remember();
    if (saveFrame !== null) { window.cancelAnimationFrame(saveFrame); saveFrame = null; }
    write();
  }
  read();
  restore();
  nav.addEventListener("scroll", function () {
    if (layoutMode() !== mode) { return; }
    /* Число снимаем сразу: к следующему кадру другая ось может стать нулевой. */
    remember();
    if (saveFrame === null) {
      saveFrame = window.requestAnimationFrame(function () { saveFrame = null; write(); });
    }
  }, {passive: true});
  nav.addEventListener("click", flush, true);
  window.addEventListener("pagehide", flush);
  window.addEventListener("resize", restore);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { read(); restore(); }
  });
}());
"""


def nav_scroll_script() -> str:
    """Автономный JS в конце body: положение .app-nav, без прокрутки страницы.

    body[data-panel-user] содержит стабильный ID, а sessionStorage хранит
    desktop/scrollTop и mobile/scrollLeft раздельно под panel-nav-scroll:<ID>.
    Скрипт не зависит от форм; без меню или ID ничего не делает.
    """
    return _NAV_SCROLL_SCRIPT


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

  /* ── живой возраст обращения: обновляется без перезагрузки страницы ───── */
  function ageText(node) {
    var raw = node.getAttribute("data-time-ms");
    var ms = raw ? Number(raw) : Date.parse(node.getAttribute("data-time") || "");
    if (!isFinite(ms)) { return ""; }
    var minutes = Math.max(0, Math.floor((Date.now() - ms) / 60000));
    if (minutes < 1) { return "меньше минуты"; }
    if (minutes < 60) { return minutes + " мин"; }
    if (minutes < 1440) { return Math.floor(minutes / 60) + " ч " + (minutes % 60) + " мин"; }
    return Math.floor(minutes / 1440) + " дн " + Math.floor((minutes % 1440) / 60) + " ч";
  }
  function paintAges() {
    var timers = document.querySelectorAll(".live-age[data-time]");
    for (var i = 0; i < timers.length; i++) {
      var age = ageText(timers[i]);
      var prefix = timers[i].getAttribute("data-prefix") || "";
      if (age) { timers[i].textContent = prefix ? prefix + " · " + age : age; }
    }
  }
  paintAges();
  if (document.querySelector(".live-age")) { window.setInterval(paintAges, 60000); }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { paintAges(); } });

  /* ── шаблон ответа подставляется прямо в поле ─────────────────────────────
     Подстановку делает и сервер - она уходит студенту с подстановками <ФИО> и
     номера обращения, и это главный путь. Но сотрудник правит текст здесь, на
     странице, и ждать перезагрузки, чтобы увидеть выбранный шаблон, неудобно:
     список выбора сам себе ничего не делал. Поэтому текст шаблона (он лежит в
     data-text пункта списка) кладём в поле рядом и сразу ставим туда курсор.

     Написанное руками не затираем молча - спрашиваем. Поле помечается, что оно
     заполнено из шаблона, и тогда выбор другого шаблона не спрашивает заново;
     первая же правка руками пометку снимает, и вопрос возвращается. Пометка
     живёт в data-атрибутах, а не в классе: печать и тёмная тема её не видят,
     и в разметку ничего не добавляется. */
  var TEMPLATE_FIELD = "template";
  function replyField(select) {
    var owner = select.form || (select.closest ? select.closest("form") : null);
    return owner ? owner.querySelector('textarea[name="text"]') : null;
  }
  function applyTemplate(select) {
    var option = select.options[select.selectedIndex];
    var text = option ? (option.getAttribute("data-text") || "") : "";
    if (!text) { return; }
    var field = replyField(select);
    if (!field) { return; }
    var own = (field.value || "").trim();
    if (own && field.getAttribute("data-from-template") !== "1" &&
        !window.confirm("Заменить набранный ответ текстом шаблона «" +
                        option.textContent.trim() + "»?")) {
      return;
    }
    field.value = text;
    field.setAttribute("data-from-template", "1");
    field.setAttribute("data-template-text", text);
    field.focus();
    try { field.setSelectionRange(text.length, text.length); } catch (err) {}
  }
  /* Слушатель на документе, а не на сам список: форму ответа может
     перерисовать другой раздел, и тогда подписка на элемент молча отвалилась
     бы вместе с ним. */
  document.addEventListener("change", function (event) {
    var node = event.target;
    if (node && node.tagName === "SELECT" && node.getAttribute("name") === TEMPLATE_FIELD) {
      applyTemplate(node);
    }
  });
  document.addEventListener("input", function (event) {
    var node = event.target;
    if (node && node.tagName === "TEXTAREA" && node.getAttribute("data-from-template") &&
        node.value !== node.getAttribute("data-template-text")) {
      node.removeAttribute("data-from-template");
      node.removeAttribute("data-template-text");
    }
  });

  document.addEventListener("submit", function (event) {
    var target = event.target;
    if (target && target.getAttribute("action") === "/panel/logout") {
      try {
        Object.keys(localStorage).forEach(function (key) {
          if (key.indexOf("panel-filters:") === 0) { localStorage.removeItem(key); }
        });
      } catch (err) {}
    }
  });

  /* ── сохранённые фильтры рабочего места ───────────────────────────────── */
  var form = document.querySelector("[data-filters]");
  if (!form) { return; }
  var KEY = "panel-filters:" + (form.getAttribute("data-filter-key") || form.getAttribute("action") || "common");
  var names = (form.getAttribute("data-filters") || "status,category,q").split(",");
  function read() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (err) { saved = {}; }
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
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
      if (box && !box.value && typeof saved[names[i]] === "string" && saved[names[i]]) {
        box.value = saved[names[i]];
        if (box.value) { changed = true; }
      }
    }
    return changed;
  }
  /* восстанавливаем только когда в адресе фильтров не было: ссылка с фильтром
     должна побеждать, иначе «/?scope=waiting» открывался бы чужим фильтром */
  if (form.getAttribute("data-filters-saved") === "1" && !window.location.search) {
    if (apply(read())) { form.submit(); return; }
  }
  if (window.location.search) { write(); }
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
    """JS для конца ``<body>``: копирование по клику, шаблон ответа и фильтры.

    Копирование живёт на кнопках ``[data-copy]`` (MAX ID, код группы, кода
    приглашения, ссылка на обращение) и подтверждается тостом. Выбор шаблона в
    форме ответа кладёт его текст в ``textarea[name="text"]`` рядом и ставит
    туда курсор; написанный руками текст спрашивает ``confirm()``, а поле,
    заполненное шаблоном, помечается ``data-from-template``, и следующий шаблон
    уже не переспрашивает. Фильтры рабочего места лежат в ``localStorage`` под
    ключом ``panel-filters`` и возвращаются при следующем открытии страницы,
    если в адресе фильтра не было.
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
