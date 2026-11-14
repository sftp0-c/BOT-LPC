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
_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth;scroll-padding-top:100px}
.skip-link{position:fixed;left:var(--space-lg);top:-80px;z-index:100;padding:12px 18px;
     background:var(--acc);color:var(--acc-ink);border-radius:var(--radius-sm)}
.skip-link:focus{top:var(--space-lg)}
.eyebrow{display:block;color:var(--mut);font-size:10px;line-height:1.5;
     font-weight:var(--weight-semi);letter-spacing:1.7px;text-transform:uppercase}
body{margin:0;background:radial-gradient(ellipse at 86% 0%,var(--canvas-a),transparent 46%),
     radial-gradient(ellipse at 0% 70%,var(--canvas-b),transparent 55%),var(--bg);
     background-attachment:fixed;color:var(--ink);font-size:var(--text-base);
     line-height:var(--leading);font-family:var(--font-sans);-webkit-font-smoothing:antialiased;
     transition:background-color var(--motion-base) var(--ease-standard),
                color var(--motion-base) var(--ease-standard)}
::selection{background:var(--sel-bg);color:var(--ink)}
h1,h2,h3{margin:0}
p{margin:0 0 var(--space-md)}
a{color:var(--acc);text-decoration:none;transition:color var(--motion-fast) var(--ease-standard)}
a:hover{text-decoration:underline;text-underline-offset:2px}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
code,kbd{font-family:var(--font-mono);font-size:.92em;background:var(--surface-raised);
     border:1px solid var(--line-soft);border-radius:var(--radius-xs);padding:1px 6px}
.mut{color:var(--mut)}
.small{font-size:var(--text-sm)}
/* Числа моноширинным: колонка цифр встаёт по разрядам, и её видно глазом
   ещё до того, как прочитаешь. Без этого счётчики «пляшут» при обновлении. */
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;
     font-family:var(--font-mono);font-size:.95em;letter-spacing:-.3px}
.lead{color:var(--mut)}

/* иконки: размер задаёт разметка, CSS только подстраховывает */
.ico{width:var(--icon-md);height:var(--icon-md);flex:0 0 auto;display:inline-block;vertical-align:-.18em}
.ico-sm{width:var(--icon-sm);height:var(--icon-sm)}
.ico-lg{width:var(--icon-lg);height:var(--icon-lg)}

/* ── шапка ─────────────────────────────────────────────────────────────────
   Раньше шапка была тёмной полосой поверх светлой страницы: в тёмной теме
   она с фоном сливалась, а в светлой выглядела чужой вставкой. Теперь
   шапка - часть страницы (свой слой), а отделяет её от содержимого
   двухпиксельная линия акцента: её видно сразу и на любом фоне. */
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:var(--space-lg);
       min-height:var(--header-h);padding:10px 20px;color:var(--head-ink);
       background:var(--surface-head);
       border-bottom:1px solid var(--line);
       box-shadow:var(--shadow-sm)}
@supports (backdrop-filter:blur(16px)){
  .app-header{background:var(--shell-glass);backdrop-filter:blur(16px)}
  .app-nav{background:var(--nav-glass);backdrop-filter:blur(16px)}
}
.brand{display:flex;align-items:center;gap:10px;min-width:calc(var(--sidebar) - 28px);color:inherit;text-decoration:none}
.brand:hover{text-decoration:none}
/* Знак колледжа - плашка акцентом: единственное цветное пятно в шапке,
   поэтому взгляд идёт именно сюда. */
.brand .logo{display:grid;place-items:center;width:40px;height:40px;border-radius:14px;
       background:linear-gradient(145deg,var(--acc-soft),var(--surface));color:var(--acc);
       border:1px solid var(--acc-line);
       transition:transform var(--motion-base) var(--ease-standard),
                  background-color var(--motion-base) var(--ease-standard)}
.brand:hover .logo{transform:translateY(-1px)}
.brand b{display:block;font-size:var(--text-base);font-weight:var(--weight-semi);letter-spacing:.2px;line-height:1.2}
.brand em{font-style:normal;color:var(--acc)}
.brand small{display:block;color:var(--head-mut);font-size:var(--text-xs);letter-spacing:.4px;
       text-transform:uppercase}
.gsearch{flex:1;display:flex;align-items:center;justify-content:flex-start;gap:var(--space-sm);
       max-width:520px;min-height:38px;padding:8px 13px;border:1px solid var(--head-line);
       border-radius:var(--radius-pill);background:var(--surface);color:var(--mut);
       font:inherit;font-size:var(--text-sm);font-weight:var(--weight-medium);cursor:pointer;
       transition:border-color var(--motion-fast) var(--ease-standard),
                  background-color var(--motion-fast) var(--ease-standard),
                  color var(--motion-fast) var(--ease-standard),
                  box-shadow var(--motion-fast) var(--ease-standard)}
.gsearch:hover{background:var(--surface-raised);border-color:var(--acc-line);color:var(--ink);transform:none}
.gsearch span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gsearch kbd{margin-left:auto;white-space:nowrap;color:var(--mut)}
.header-account{margin-left:auto;display:flex;align-items:center;gap:var(--space-md)}
.avatar{display:grid;place-items:center;width:34px;height:34px;background:var(--acc-soft);color:var(--acc);border-radius:50%}
.who small{display:block;font-size:9px;line-height:1.2;color:var(--mut)}
.who{margin-left:auto;display:flex;align-items:center;gap:var(--space-md);color:var(--head-mut);
     font-size:var(--text-sm);white-space:nowrap}
.who b{color:var(--head-ink);font-weight:var(--weight-semi)}
.who a{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border:1px solid var(--head-line);
     border-radius:var(--radius-pill);color:var(--head-ink);
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.who .link-button{min-height:0;padding:5px 11px;border:1px solid var(--head-line);
     border-radius:var(--radius-pill);background:transparent;color:var(--head-ink);
     font-size:var(--text-sm);font-weight:var(--weight-medium)}
.who .link-button:hover{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad);transform:none}
.theme-toggle{display:grid;place-items:center;width:34px;height:34px;min-height:0;
      flex:0 0 34px;padding:0;
      border:1px solid var(--head-line);border-radius:50%;background:var(--surface);
      color:var(--ink-soft);cursor:pointer;
      transition:background-color var(--motion-fast) var(--ease-standard),
                 border-color var(--motion-fast) var(--ease-standard),
                 color var(--motion-fast) var(--ease-standard),
                 transform var(--motion-fast) var(--ease-standard)}
.theme-toggle:hover{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.theme-toggle:active{transform:scale(.94)}

/* ── боковое меню (на узком экране превращается в верхнюю ленту) ────────── */
.app-nav{position:fixed;top:var(--header-h);bottom:0;left:0;width:var(--sidebar);z-index:20;
    padding:24px 14px var(--space-xxl);background:var(--surface);
    border-right:1px solid var(--line);overflow-y:auto;overscroll-behavior:contain;
    animation:nav-in var(--motion-slow) var(--ease-standard) both}
/* Пункт меню - строка с левым отступом под активную полосу: «сейчас тут»
   показывают три вещи сразу (полоса, заливка, цвет текста), поэтому
   выбранный раздел не спутать с наведённым. */
.app-nav a{position:relative;display:flex;align-items:center;gap:10px;padding:9px 11px;
      margin-bottom:2px;
      border:1px solid transparent;border-radius:var(--radius-sm);color:var(--ink-soft);
      font-size:var(--text-sm);font-weight:var(--weight-medium);text-decoration:none;
      transition:background-color var(--motion-fast) var(--ease-standard),
                 color var(--motion-fast) var(--ease-standard),
                 border-color var(--motion-fast) var(--ease-standard),
                 transform var(--motion-fast) var(--ease-standard)}
.app-nav a:hover{background:var(--surface-raised);color:var(--ink);text-decoration:none;transform:translateX(2px)}
.app-nav a.on{background:linear-gradient(110deg,var(--acc-soft),var(--surface));border-color:var(--acc-line);color:var(--acc);
     font-weight:var(--weight-semi);box-shadow:inset 3px 0 0 var(--acc)}
.nav-ico{display:grid;place-items:center;width:22px;height:22px;flex:0 0 22px;color:var(--mut);
     transition:color var(--motion-fast) var(--ease-standard)}
.nav-ico .ico{width:18px;height:18px}
.app-nav a:hover .nav-ico{color:var(--ink-soft)}
.app-nav a.on .nav-ico{color:var(--acc)}
.nav-txt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── меню по группам: пять разделов вместо шестнадцати вкладок ────────────── */
.nav-caption{padding:0 11px 20px;color:var(--mut);font-size:10px;letter-spacing:1.4px;text-transform:uppercase}
.nav-groups{display:flex;flex-direction:column;gap:var(--space-xl)}
.nav-group{display:flex;flex-direction:column;gap:2px;min-width:0}
.nav-group-items{display:flex;flex-direction:column;gap:2px}
.nav-group-label{display:flex;align-items:center;gap:7px;padding:0 11px 3px;color:var(--mut);
     font-size:var(--text-xs);font-weight:var(--weight-semi);letter-spacing:.7px;
     text-transform:uppercase;white-space:nowrap;
     transition:color var(--motion-fast) var(--ease-standard)}
.nav-group.on .nav-group-label{color:var(--acc)}
/* Бейдж: сколько ждёт внимания. Число на плашке - насыщенным цветом на
   своей мягкой подложке, а не «серым по серому». */
.nav-badge{min-width:21px;padding:0 6px;border:1px solid var(--line);border-radius:var(--radius-pill);
     background:var(--surface-raised);color:var(--ink-soft);font-size:11px;line-height:17px;
     text-align:center;font-variant-numeric:tabular-nums;text-transform:none;letter-spacing:0;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.nav-badge.hot{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad);font-weight:var(--weight-semi)}
/* ── подпункты меню: подкатегории внутри группы ─────────────────────────────
   Внутри «Люди» шесть разделов, внутри «Справочники» четыре, и половина из
   них открывают раз в месяц: плоский список выглядит списком равных, хотя
   половина пунктов - «прочие». Подпункт - это подпись и свои ссылки внутри
   той же группы, поэтому разметка остаётся <a> внутри <nav>, и все прежние
   проверки меню живы: подпись подпункта намеренно .nav-sub-label, а не
   .nav-group-label (число подписей групп в меню сверяется проверками).
   Пометку «on» на подпункте ставит разметка, а не CSS. */
.nav-sub{display:flex;flex-direction:column;gap:2px;min-width:0;
     margin:var(--space-xs) 0 var(--space-sm);padding-left:var(--nav-indent);
     border-left:1px solid var(--line)}
.nav-sub-label{display:block;padding:3px 10px 2px;color:var(--mut);
     font-size:var(--text-xs);font-weight:var(--weight-semi);letter-spacing:.4px;
     text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav-sub-items{display:flex;flex-direction:column;gap:2px;min-width:0}
.nav-sub a{padding:7px 10px;font-size:var(--text-sm)}
.nav-sub .nav-ico{width:20px;height:20px;flex:0 0 20px}
.nav-sub .nav-ico .ico{width:16px;height:16px}

/* ── основная область ────────────────────────────────────────────────────── */
main{margin-left:var(--sidebar);padding:var(--space-xl) 26px var(--space-xxl);
     max-width:var(--content-max);animation:page-in var(--motion-slow) var(--ease-standard) both}
/* Заголовок страницы: тот же акцентный маркер, что у пунктов меню, - у
   страницы и у её раздела один язык. */
.page-title{margin:0 0 var(--space-lg);display:flex;align-items:center;gap:10px;flex-wrap:wrap;
     padding-left:0;border:0;
     font-size:var(--text-xxl);font-weight:var(--weight-bold);letter-spacing:-.9px}
/* шапка страницы: заголовок слева, действия справа - экспорт и печать на виду */
.dochead{display:flex;align-items:center;gap:var(--space-md);flex-wrap:wrap;margin:0 0 var(--space-lg)}
.dochead .page-title{margin:3px 0 0;min-width:0}
.dochead-title{min-width:0}
.dochead{padding:6px 0 12px}
.dochead .page-actions{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm);
     margin-left:auto;padding:0}
.dochead .page-actions .small{margin:0}
footer{margin-left:var(--sidebar);padding:var(--space-lg) 26px 30px;color:var(--mut);font-size:var(--text-xs)}
/* Подзаголовок раздела: тонкая линейка слева вместо серой капли текста -
   строка перестаёт теряться среди карточек. */
h3{margin:var(--space-xl) 0 var(--space-sm);padding-left:var(--space-sm);color:var(--ink-soft);
   border-left:2px solid var(--line-strong);font-size:var(--text-xs);
   font-weight:var(--weight-semi);letter-spacing:.6px;text-transform:uppercase}

/* ── карточки ─────────────────────────────────────────────────────────────
   Плоская панель: рамка и почти незаметная тень в покое, подъём и тень -
   на наведении. Радиус и внутренний отступ у карточки, панели показателей
   и пустой вставки один и тот же, поэтому страница выглядит собранной. */
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-md);
      padding:var(--space-xl) var(--space-xl);margin-bottom:var(--space-lg);box-shadow:var(--shadow-sm);
      overflow-x:auto;overscroll-behavior-x:contain;
      transition:transform var(--motion-base) var(--ease-standard),
                 box-shadow var(--motion-base) var(--ease-standard),
                 border-color var(--motion-base) var(--ease-standard);
      animation:card-in var(--motion-slow) var(--ease-standard) both}
.card:hover{border-color:var(--line-strong)}
/* Заголовок карточки отделён линейкой во всю ширину: подзаголовок и текст
   больше не начинаются вплотную к заголовку. */
.card h2{margin:0 0 var(--space-lg);padding-bottom:var(--space-sm);display:flex;
      align-items:center;gap:var(--space-sm);flex-wrap:wrap;
      min-width:0;border-bottom:0;
      font-size:var(--text-lg);font-weight:var(--weight-semi);letter-spacing:-.1px}
.card h2 .ico{color:var(--acc)}
.card h2 .pill,.card h2 .btn{font-size:var(--text-sm)}
.cards{display:flex;flex-wrap:wrap;gap:var(--space-md)}
.cards>.card{margin-bottom:0}
/* появление карточек по очереди: заметная пауза, без «прыжков» */
main > .card:nth-child(2){animation-delay:40ms}
main > .card:nth-child(3){animation-delay:80ms}
main > .card:nth-child(4){animation-delay:120ms}
main > .card:nth-child(5){animation-delay:160ms}
main > .card:nth-child(6){animation-delay:200ms}
main > .card:nth-child(7){animation-delay:240ms}
main > .card:nth-child(n+8){animation-delay:280ms}

/* Доступ сотрудников: отдельные состояния и формы, без второго бокового меню. */
.staff-web p{max-width:80ch;line-height:1.65;color:var(--ink-soft)}
.staff-web-intro{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--space-lg);flex-wrap:wrap}
.staff-web-intro>div{flex:1 1 360px;min-width:0}
.staff-web-intro p{margin:0 0 var(--space-md)}
.staff-web .btn{white-space:normal;text-align:center;min-height:44px}
.staff-web-search{display:flex;flex-wrap:wrap;align-items:flex-end;gap:var(--space-sm);margin-top:var(--space-lg)}
.staff-web-search>div{flex:1 1 260px;min-width:0}
.staff-web-search label{margin-top:0}
.staff-web-list{list-style:none;margin:0;padding:0}
.staff-web-list>li{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,.55fr) auto;
    align-items:center;gap:var(--space-lg);padding:var(--space-lg) 0;border-top:1px solid var(--line-soft)}
.staff-web-list>li:first-child{border-top:0;padding-top:0}
.staff-web-list>li:last-child{padding-bottom:0}
.staff-web-person{min-width:0;overflow-wrap:anywhere}
.staff-web-person strong{display:block;font-size:var(--text-base);font-weight:var(--weight-semi)}
.staff-web-person p{margin:var(--space-xs) 0}
.staff-web-person small,.staff-web-status small{display:block;color:var(--mut);line-height:1.5}
.staff-web-status small{margin-top:var(--space-xs)}
.staff-web-settings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-lg);margin:var(--space-lg) 0}
.staff-web-settings label{margin-top:0}
.staff-web-permissions{min-width:0;margin:var(--space-lg) 0;padding:var(--space-lg);border:1px solid var(--line);border-radius:var(--radius-sm)}
.staff-web-permissions legend{padding:0 var(--space-sm);font-weight:var(--weight-semi)}
.staff-web-checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-sm)}
.staff-web-check{display:flex;align-items:flex-start;gap:var(--space-sm);margin:0;padding:var(--space-sm);
    min-height:44px;border:1px solid var(--line-soft);border-radius:var(--radius-sm);cursor:pointer}
.staff-web-check:hover{background:var(--surface-raised);border-color:var(--acc-line)}
.staff-web-check:has(input:checked){background:var(--acc-soft);border-color:var(--acc-line)}
.staff-web-check input[type=checkbox]{flex:0 0 18px;width:18px;height:18px;margin:2px 0 0;padding:0}
.staff-web-check span{min-width:0}
.staff-web-check small{display:block;margin-top:var(--space-xs);font-weight:var(--weight-normal);color:var(--mut);letter-spacing:0;line-height:1.5}
.staff-web-steps{margin:0;padding-left:22px;line-height:1.65;color:var(--ink-soft)}
.staff-web-steps li+li{margin-top:var(--space-xs)}
.staff-web-help{margin-top:var(--space-lg)}
.staff-web-empty{padding:var(--space-lg) 0}
.staff-web-empty p{margin:var(--space-xs) 0 var(--space-md)}
@media (max-width:640px){
  .staff-web-list>li{grid-template-columns:minmax(0,1fr);gap:var(--space-sm)}
  .staff-web-list>li>.btn{justify-self:start}
  .staff-web-settings,.staff-web-checks{grid-template-columns:minmax(0,1fr)}
  .staff-web-permissions{padding:var(--space-sm)}
  .staff-web-intro>div{flex-basis:100%}
}

/* ── таблицы ─────────────────────────────────────────────────────────────
   Разделитель строки - тонкая линия, шапка - капитель с моноширинными
   цифрами и своей линией снизу. Наведение подсвечивает всю строку и
   добавляет слева акцентную полосу: строка видна целиком, а не пятном. */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:var(--text-base);
     table-layout:auto;border:1px solid var(--line-soft);border-radius:var(--radius-sm);overflow:hidden}
/* min-width:0 у ячейки - обязателен: иначе длинное слово без пробелов (код
   группы, ссылка, ФИО латиницей) распирает колонку и выталкивает соседние за
   пределы карточки. Разрывать такой текст умеет overflow-wrap ниже. */
th,td{text-align:left;padding:10px var(--space-sm);border-bottom:1px solid var(--line-soft);
      vertical-align:top;min-width:0;overflow-wrap:break-word;
      transition:background-color var(--motion-fast) var(--ease-standard)}
/* Заголовок - капитель основным шрифтом, а не моноширинным: русские слова в
   моноширинном шрифте шире, и колонка с длинными ФИО отдавала под них лишние
   пиксели - должности рвались посреди слова. Моноширинным остаются числа,
   даты и идентификаторы (см. .num, .feed-time, .wb-date, .scale-line b). */
th{position:sticky;top:0;z-index:1;background:var(--surface-head);color:var(--ink-soft);
    font-size:var(--text-xs);font-weight:var(--weight-semi);
    letter-spacing:.6px;text-transform:uppercase;white-space:nowrap;
    border-bottom:1px solid var(--line-strong)}
td{word-break:break-word;overflow-wrap:anywhere}
tbody tr{transition:background-color var(--motion-fast) var(--ease-standard)}
tbody tr:hover{background:var(--surface-raised)}
tbody tr:hover td:first-child{box-shadow:inset 3px 0 0 var(--acc-line)}
tbody tr:last-child td{border-bottom:0}
td b{color:var(--ink);font-weight:var(--weight-semi)}
th.col-key{width:260px}
/* Колонка действий. Тема задаёт ячейкам td{overflow-wrap:anywhere}, при
   котором минимальная ширина текста равна одному символу: колонка
   схлопывалась до буквы, и подпись кнопки шла по буквам вниз. Запрет
   переноса даёт ячейке минимум по всей подписи, width:1% отдаёт
   остальное колонке с текстом - тот же приём, что у .data-table. */
.col-act{white-space:nowrap}
th.col-act,td.col-act{width:1%}
/* Ряд подсказок должностей и ряд действий сотрудника - разные ряды,
   иначе кнопки одного ряда наезжали на кнопки другого. */
.staff-hints{margin-top:var(--space-sm)}
.staff-actions{margin-top:var(--space-sm)}
/* Форма правки в строке: без минимума ширины таблица отдавала колонке
 190px и поля в ней выглядели сжатыми. */
.staff-table th:last-child,.staff-table td:last-child{min-width:210px}

tbody tr.is-found{background:var(--glow);box-shadow:inset 3px 0 0 var(--acc)}
/* Узкая колонка не должна ложиться в «букву в столбик»: у таблиц, где
   содержимое заранее неизвестно, есть минимальная ширина, а лишнее уходит в
   горизонтальную прокрутку карточки - она для того и прокручивается. */
.data-table{min-width:var(--table-min-wide)}
.wb-card table{min-width:var(--table-min)}

/* ── формы ───────────────────────────────────────────────────────────────── */
/* Поле не ложится на подпись и не наезжает на соседнее по трём причинам:
   подпись - отдельный блок, само поле - блочная коробка с min-width:0 (без
   него длинный <select> распирает ячейку .grid, и ряд выходит за карточку),
   а вертикальный ритм задаёт зазор .grid, а не отступы внутри ячеек. */
input,select,textarea{width:100%;max-width:100%;min-width:0;display:block;
      min-height:var(--field-min);padding:9px 11px;border:1px solid var(--line-strong);
      border-radius:var(--radius-sm);background:var(--surface-sunken);color:var(--ink);
      font:inherit;line-height:var(--field-line);
      transition:border-color var(--motion-fast) var(--ease-standard),
                 box-shadow var(--motion-fast) var(--ease-standard),
                 background-color var(--motion-fast) var(--ease-standard)}
input::placeholder,textarea::placeholder{color:var(--mut)}
input:hover,select:hover,textarea:hover{border-color:var(--acc-line)}
input:focus,select:focus,textarea:focus{outline:0;background:var(--surface);
      border-color:var(--acc);box-shadow:var(--shadow-focus)}
/* Флажок - не поле ввода: своя ширина и своя высота, иначе он растянулся бы
   на всю ячейку .grid и съел её. */
input[type=checkbox],input[type=radio]{display:inline-block;width:auto;min-width:0;
      min-height:0;margin:0;accent-color:var(--acc)}
/* Высота у всех полей одна (--field-min): у select своя метка, у textarea
   своя высота, и без общего минимума подписи в одной строке расходились. */
select{padding-right:var(--space-xl)}
textarea{min-height:110px;resize:vertical}
label{display:block;margin:var(--space-md) 0 var(--space-xs);color:var(--ink-soft);
      font-size:var(--text-sm);font-weight:var(--weight-semi);letter-spacing:.2px;
      overflow-wrap:anywhere}
.grid{display:flex;flex-wrap:wrap;align-items:flex-end;gap:var(--space-md)}
.grid>*{flex:1 1 180px;min-width:0}
.grid .full{flex:1 1 100%}
/* Подпись внутри ячейки формы: верхний отступ не нужен, его даёт зазор .grid. */
.grid>div>label{margin-top:0}
/* Кнопка в .grid - по размеру своего текста, а не во всю строку. Раньше
   .grid>* тянул и одиночную «Сохранить», и каждую кнопку ряда: действия
   выглядели разбросанными по странице (это и есть «неправильно расположенные
   кнопки»). Ячейка с полем по-прежнему растягивается - за это отвечает
   условие «в ячейке больше нет полей ввода». */
.grid>button,.grid>.btn,.grid>form.inline,
.grid>div:has(> button):not(:has(input,select,textarea)),
.grid>div:has(> .btn):not(:has(input,select,textarea)),
.grid>div:has(> form.inline):not(:has(input,select,textarea)){flex:0 0 auto}
form.inline{display:inline}

/* ── кнопки ────────────────────────────────────────────────────────────────
   Три вида и ровно три: акцентная (действие), контурная (второстепенное) и
   цветная (опасное или подтверждение). Подпись не переносится никогда, а
   нажатие видно по вдавленной рамке - иначе на телефоне не видно, что кнопка
   сработала.
   Высота кнопки - та же, что у поля рядом с ней (--field-min): иначе в одной
   строке формы кнопка либо подпрыгивала, либо липла к рамке. margin:0 - без
   него кнопка в контейнере вставала на пиксель вбок от своих соседей. У
   квадратных кнопок (поиск, тема, копирование) min-height сбрасывается ниже:
   им нужна своя сторона, а не общая высота поля. */
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
      min-height:var(--field-min);margin:0;max-width:100%;padding:9px 15px;
      border:1px solid var(--acc);border-radius:var(--radius-sm);background:var(--acc);
      color:var(--acc-ink);font:inherit;font-weight:var(--weight-semi);cursor:pointer;
      text-decoration:none;
      transition:background-color var(--motion-fast) var(--ease-standard),
                 border-color var(--motion-fast) var(--ease-standard),
                 color var(--motion-fast) var(--ease-standard),
                 box-shadow var(--motion-fast) var(--ease-standard),
                 transform var(--motion-fast) var(--ease-standard)}
/* Подпись кнопки не переносится. Раньше такого правила не было вовсе, и кнопка
 схлопывалась в несколько строк: «Вкл/выкл» - 183px высотой в восемь строк,
 «Удалить» - 66px в три строки. В .grid ячейка становится шире подписи, а
 flex-wrap переносит её на следующую строку - наложения на соседей не будет. */
button,.btn{white-space:nowrap}

button:hover,.btn:hover{background:var(--acc-soft);border-color:var(--acc);color:var(--acc);
     transform:translateY(-1px);box-shadow:var(--shadow-md);text-decoration:none}
button:active,.btn:active{transform:translateY(0);background:var(--acc-soft);
     box-shadow:var(--shadow-inset)}
/* Контурная кнопка: рамка вместо заливки. На тёмном фоне серая заливка
   сливалась с поверхностью, и вторая кнопка в ряду читалась как фон, а не
   как кнопка. */
.btn-grey{background:transparent;border-color:var(--line-strong);color:var(--ink-soft)}
.btn-grey:hover{background:var(--surface-raised);border-color:var(--acc);color:var(--acc)}
.btn-ok{background:var(--ok);border-color:var(--ok);color:var(--ok-ink)}
.btn-ok:hover{background:var(--ok-soft);border-color:var(--ok);color:var(--ok)}
.btn-bad{background:transparent;border-color:var(--bad-line);color:var(--bad)}
.btn-bad:hover{background:var(--bad-soft);border-color:var(--bad);color:var(--bad)}
.btn-sm{padding:5px 10px;border-radius:var(--radius-xs);font-size:var(--text-sm)}

/* ── ряд кнопок ────────────────────────────────────────────────────────────
   Жалоба «кнопки слипшиеся» повторялась несколько раз, потому что панель
   печатает кнопки прямо в контейнер: в шапке страницы (.page-actions), в
   карточке обращения (.wb-tools), в форме (.grid) - и прямо в ячейке
   таблицы, а последнее ни одним правилом не ловилось: «Правка» и «Удалить»
   в списке шаблонов стояли бортами и читались как одна кнопка. Поэтому зазор
   задан тремя независимыми способами, чтобы новая разметка снова не осталась
   без него:

   1. именованный класс - .btn-row, .page-actions, .wb-tools, .actions,
      .toolbar: достаточно одной строки, чтобы ряд разъехался;
   2. счётчик соседей (:has) - контейнер, у которого две и более кнопки
      подряд, становится рядом с зазором сам, даже если класс не проставлен.
      Список тегов задан явно: с уголком правило задевало бы .card и ломало
      бы раскладку. Специфичность нулевая (:where), поэтому именованный класс
      переопределяет его молча - и наоборот, правило ничего не ломает тихо;
   3. .grid и .pager - зазор у них был всегда, ячейка с кнопкой теперь не
      растягивается на всю строку (см. правило .grid выше). */
.btn-row,.page-actions,.wb-tools,.actions,.toolbar{
      display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm);margin:0}
.btn-row>button,.btn-row>.btn,.page-actions>button,.page-actions>.btn,
.wb-tools>button,.wb-tools>.btn,.actions>button,.actions>.btn,
.toolbar>button,.toolbar>.btn{margin:0}
:where(td,th,p,li,summary,label,fieldset,caption)
  :has(> :is(button,.btn,form.inline) + :is(button,.btn,form.inline)){
      display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm)}

/* ── сообщения и метки ───────────────────────────────────────────────────── */
/* Плашка с полосой слева: вид, который нельзя спутать ни с карточкой, ни с
   пустой вставкой. Текст - насыщенным цветом на своей мягкой подложке
   (контраст 5.7:1 и выше), а не цветом «для заливки»: в светлой теме тот
   давал 1.2:1, и сообщение было нечитаемым. */
.msg{display:flex;align-items:flex-start;gap:var(--space-sm);padding:12px 14px;
     margin-bottom:var(--space-lg);
     border:1px solid var(--line);border-left:3px solid var(--line-strong);
     border-radius:var(--radius-sm);background:var(--surface);
     color:var(--ink);font-size:var(--text-base);font-weight:var(--weight-medium);
     animation:card-in var(--motion-slow) var(--ease-standard) both}
.msg .ico{margin-top:1px;flex:0 0 auto}
.msg-ok{background:var(--ok-soft);border-color:var(--ok-line);border-left-color:var(--ok);color:var(--ok)}
.msg-bad{background:var(--bad-soft);border-color:var(--bad-line);border-left-color:var(--bad);color:var(--bad)}
.msg-fallback-hidden{display:none}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;white-space:nowrap;
     border:1px solid var(--line);border-radius:var(--radius-pill);background:var(--surface-raised);
     color:var(--ink-soft);font-size:var(--text-sm);font-weight:var(--weight-medium);
     transition:background-color var(--motion-fast) var(--ease-standard),
                color var(--motion-fast) var(--ease-standard)}
.pill-on{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok);font-weight:var(--weight-semi)}
.pill-off{background:var(--surface-sunken);border-color:var(--line-soft);color:var(--mut)}

/* ── показатели ──────────────────────────────────────────────────────────── */
.stat{flex:1 1 160px;padding:var(--space-lg) var(--space-lg);border:1px solid var(--line);
      border-radius:var(--radius-md);background:var(--surface);box-shadow:var(--shadow-sm);
      transition:transform var(--motion-base) var(--ease-standard),
                 box-shadow var(--motion-base) var(--ease-standard),
                 border-color var(--motion-base) var(--ease-standard)}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-md);border-color:var(--line-strong)}
/* Числа моноширинным и с разрядами - так колонка показателей читается
   взглядом сверху вниз, а не по одной строке за раз. */
.stat b{display:block;font-size:var(--text-num);line-height:1.15;letter-spacing:-.5px;
     font-weight:var(--weight-bold);font-variant-numeric:tabular-nums;
     font-family:var(--font-mono)}
.stat span{color:var(--mut);font-size:var(--text-sm)}
.kpi{display:flex;flex-wrap:wrap;gap:var(--space-md);margin-bottom:var(--space-lg)}
.kpi div{flex:1 1 130px;padding:var(--space-lg);border:1px solid var(--line);
     border-radius:var(--radius-md);background:var(--surface);box-shadow:var(--shadow-sm);
     transition:transform var(--motion-base) var(--ease-standard),
                box-shadow var(--motion-base) var(--ease-standard),
                border-color var(--motion-base) var(--ease-standard)}
.kpi div:hover{transform:translateY(-2px);box-shadow:var(--shadow-md);border-color:var(--line-strong)}
.kpi b{display:block;font-size:var(--text-xl);line-height:1.2;font-variant-numeric:tabular-nums;
     font-family:var(--font-mono);letter-spacing:-.4px}
.kpi span{color:var(--mut);font-size:var(--text-sm)}
.kpi .warn b{color:var(--bad)}
.kpi .good b{color:var(--ok)}

/* ── крупные счётчики «Пульта»: каждый ведёт в свой раздел ────────────────── */
.big-stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));
     gap:var(--space-md);margin-bottom:var(--space-lg)}
.big-stat{position:relative;display:flex;flex-direction:column;gap:1px;
     padding:var(--space-lg) var(--space-lg);
     border:1px solid var(--line);border-radius:var(--radius-md);background:var(--surface);
     box-shadow:var(--shadow-sm);color:inherit;overflow:hidden;text-decoration:none;min-width:0;
     transition:transform var(--motion-base) var(--ease-standard),
                box-shadow var(--motion-base) var(--ease-standard),
                border-color var(--motion-base) var(--ease-standard)}
/* Полоса слева - единственный признак состояния плитки, поэтому она толще
   и цветнее, чем просто граница. */
.big-stat::after{content:"";position:absolute;inset:0 auto 0 0;width:4px;
     background:var(--acc);opacity:.8;
     transition:opacity var(--motion-base) var(--ease-standard)}
.big-stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);
     border-color:var(--line-strong);text-decoration:none}
.big-stat:hover::after{opacity:1}
.big-stat .big-ico{display:grid;place-items:center;width:32px;height:32px;border-radius:10px;background:var(--acc-soft);color:var(--acc);margin-bottom:12px}
.big-stat b{font-size:var(--text-num);line-height:1.12;letter-spacing:-.5px;
     font-weight:var(--weight-bold);font-variant-numeric:tabular-nums;
     font-family:var(--font-mono)}
.big-stat .big-txt{font-size:var(--text-xl);line-height:1.25}
.big-stat span{color:var(--mut);font-size:var(--text-sm)}
.big-stat.warn::after{background:var(--warn)}
.big-stat.bad::after{background:var(--bad)}
.big-stat.good::after{background:var(--ok)}
.big-stat.zero{opacity:.78}
/* мелкая полоса «масштаба» под счётчиками: сколько всего в базе */
.scale-line{display:flex;flex-wrap:wrap;gap:var(--space-lg);padding:11px 15px;
     margin-bottom:var(--space-lg);
     border:1px solid var(--line-soft);border-radius:var(--radius-md);
     background:var(--surface-sunken);
     color:var(--mut);font-size:var(--text-sm)}
.scale-line b{color:var(--ink-soft);font-weight:var(--weight-semi);font-variant-numeric:tabular-nums;
     font-family:var(--font-mono)}

/* ── обзор: разные задачи занимают разное место ─────────────────────────── */
.dashboard-hero{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:space-between;
     gap:var(--space-lg);padding:26px 30px;margin-bottom:var(--space-lg);border:1px solid var(--hero-line);
     border-radius:var(--radius-lg);background:linear-gradient(115deg,var(--hero-start),var(--hero-end));color:var(--hero-ink)}
.dashboard-hero::after{content:"";position:absolute;right:34px;top:-90px;width:340px;height:340px;
     border:1px solid var(--decor-line);border-radius:50%;pointer-events:none}
.dashboard-hero h2{font-size:24px;letter-spacing:-.5px;line-height:1.2;margin:6px 0 10px}
.dashboard-hero p{color:var(--hero-muted);font-size:var(--text-sm);margin:0;max-width:530px}
.dashboard-hero .eyebrow{color:var(--hero-muted)}
.hero-sign{display:flex;align-items:center;gap:var(--space-md);position:relative;z-index:1;flex:0 0 auto}
.hero-sign .ico{width:58px;height:58px;color:var(--hero-muted)}
.hero-sign strong{display:block;font-size:17px}.hero-sign small{color:var(--hero-muted)}
.bento{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:var(--space-lg);align-items:stretch}
.bento>.card{min-width:0;margin:0;height:100%}
.bento-day{grid-column:span 7}.bento-actions{grid-column:span 5}
.bento-audit,.bento-events{grid-column:span 6}
.bento .card-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.bento .card-title h2{margin:0;padding:0;font-size:16px}
.bento .card-title>a{font-size:11px;white-space:nowrap}
.bento .chart-box{padding:8px 0 0;border:0;box-shadow:none}
.bento .todo-row{padding:11px 0;border:0;border-radius:0;border-bottom:1px solid var(--line-soft);background:transparent}
.bento .todo-name{white-space:normal;font-size:12px}
.bento .todo-row:hover{transform:none}
.bento .feed{font-size:12px}.bento .feed li{padding:11px 0}
.bento .feed-what{white-space:normal;overflow-wrap:anywhere}
.bento .feed-time{font-family:var(--font-sans);font-size:10px;max-width:92px;white-space:normal}
.bento .today-stat{background:var(--surface-sunken);border-color:var(--line-soft)}
.overview-gaps{margin-top:var(--space-lg)}
.overview-gaps h2{font-size:14px;margin:0 0 8px;padding:0}
.overview-gaps p{font-size:12px;margin:0}
.app-footer{display:flex;justify-content:space-between;gap:var(--space-md);border-top:1px solid var(--line-soft)}

/* ── календарная статистика сегодня ─────────────────────────────────────── */
.today-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
     gap:var(--space-sm);margin-bottom:var(--space-lg)}
.today-stat{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:2px var(--space-sm);
     padding:var(--space-md);border:1px solid var(--line);border-radius:var(--radius-md);
     background:var(--surface-sunken)}
.today-stat>span{grid-row:1/3;color:var(--acc)}
.today-stat b{font-size:var(--text-xl);font-family:var(--font-mono);line-height:1}
.today-stat small{color:var(--mut)}

/* ── изолированный вход ──────────────────────────────────────────────────── */
.login-page{min-height:100vh;display:flex;flex-direction:column;justify-content:center;align-items:center;
     padding:56px 28px 24px;background:radial-gradient(ellipse at 12% 18%,var(--canvas-a),transparent 55%),
     radial-gradient(ellipse at 95% 70%,var(--canvas-b),transparent 55%),var(--bg)}
.login-theme{position:absolute;right:28px;top:24px;width:44px;height:44px}
.login-shell{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(330px,.85fr);align-items:center;
     gap:80px;width:min(1060px,100%);margin:0;padding:0;max-width:none}
.login-story{min-width:0;position:relative}
.login-brand{display:flex;align-items:center;gap:14px;margin-bottom:44px}
.login-brand .logo{display:grid;place-items:center;width:52px;height:52px;border-radius:17px;
     border:1px solid var(--acc-line);background:var(--acc-soft);color:var(--acc);box-shadow:var(--brand-shadow)}
.login-brand b{font-size:23px;letter-spacing:-.6px;display:block}.login-brand small{font-size:11px;color:var(--mut);display:block}
.login-story h1{font-size:clamp(32px,4.6vw,52px);line-height:1.1;letter-spacing:-2px;margin:14px 0 20px}
.login-story h1 span{color:var(--acc)}
.login-lead{font-size:15px;line-height:1.75;color:var(--mut);max-width:470px;margin-bottom:24px}
.login-flow{display:flex;flex-wrap:wrap;align-items:center;gap:7px;color:var(--mut);font-size:11px}
.login-flow>span{display:flex;align-items:center;gap:7px;padding:10px;border:1px solid var(--line);
     border-radius:10px;background:var(--surface)}
.login-flow>span:last-child{border-color:var(--acc-line);color:var(--acc);background:var(--acc-soft)}
.login-flow>i{display:flex;font-style:normal}
.login-points{display:flex;flex-wrap:wrap;gap:16px;margin-top:26px;font-size:11px;color:var(--mut)}
.login-points span{display:flex;align-items:center;gap:6px}.login-points .ico{width:15px;height:15px}
.login-orbit{position:relative;height:172px;margin-top:25px;overflow:hidden}
.orbit-ring{position:absolute;left:25px;top:14px;width:320px;height:320px;border:1px solid var(--line);border-radius:50%}
.orbit-ring::after{content:"";position:absolute;inset:30px;border:1px solid var(--line-soft);border-radius:50%}
.orbit-core{position:absolute;left:120px;top:32px;transform:rotate(-12deg);color:var(--acc-line)}
.orbit-core .ico{width:88px;height:88px}
.orbit-note{position:absolute;display:flex;align-items:center;gap:8px;padding:12px 16px;border:1px solid var(--line);
     border-radius:12px;background:var(--surface);box-shadow:var(--shadow-md);font-size:11px;color:var(--ink-soft)}
.orbit-note .ico{color:var(--acc)}.orbit-note-a{left:0;top:28px}.orbit-note-b{left:126px;top:104px}
.login-card{margin:0;padding:36px;border-radius:var(--radius-lg);box-shadow:var(--shadow-lg);
     border-color:var(--line-strong);animation:card-in var(--motion-slow) var(--ease-out) both}
.login-card:hover{transform:none;box-shadow:var(--shadow-lg)}
.login-lock{display:grid;place-items:center;width:48px;height:48px;margin-bottom:20px;border-radius:14px;
     color:var(--acc);background:var(--acc-soft);border:1px solid var(--acc-line)}
.login-card h2{font-size:23px;line-height:1.25;letter-spacing:-.7px;border:0;padding:0;margin:0 0 10px}
.login-card>.mut{font-size:13px;line-height:1.7;margin-bottom:24px}
.login-card form{display:grid;gap:16px}.login-card label{margin:0 0 8px;font-size:12px}
.login-card input:not([type=checkbox]){min-height:46px;padding:12px 14px;font-size:13px}
.login-card .remember-row{display:flex;align-items:center;gap:10px;font-size:11px;margin:2px 0 0;font-weight:var(--weight-medium)}
.login-card .remember-row input{width:16px;height:16px}
.login-submit{width:100%;min-height:46px;justify-content:space-between;margin-top:3px;
     background:linear-gradient(100deg,var(--acc),var(--gradient-end));box-shadow:var(--brand-shadow)}
.login-help{font-size:11px;line-height:1.8;color:var(--mut);margin:20px 0}
.login-security{display:flex;align-items:center;gap:8px;padding-top:16px;border-top:1px solid var(--line-soft);font-size:10px;color:var(--mut)}
.login-footer{margin:36px 0 0;padding:0;color:var(--mut);font-size:10px;text-align:center}

/* ── «Что требует действия»: строки-ссылки с числом ───────────────────────── */
.todo{margin:0;padding:0;list-style:none}
.todo li+li{margin-top:2px}
.todo-row{display:flex;align-items:center;gap:var(--space-md);padding:10px 12px;
     border:1px solid var(--line-soft);border-radius:var(--radius-sm);background:var(--surface);
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard),
                transform var(--motion-fast) var(--ease-standard)}
.todo-row:hover{background:var(--surface-raised);border-color:var(--line-strong);
     transform:translateX(2px)}
.todo-ico{color:var(--mut);flex:0 0 auto}
.todo-name{flex:1;min-width:0;color:var(--ink-soft);font-size:var(--text-sm);
     overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.todo-n{flex:0 0 auto;min-width:34px;padding:1px 9px;border-radius:var(--radius-pill);
     background:var(--surface-raised);border:1px solid var(--line);color:var(--ink);
     font-size:var(--text-sm);text-align:center;font-variant-numeric:tabular-nums;
     font-family:var(--font-mono)}
.todo-row.hot .todo-n{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad);font-weight:var(--weight-semi)}
.todo-row.zero .todo-name{color:var(--mut)}
.todo-go{flex:0 0 auto;color:var(--mut);opacity:.4;
     transition:opacity var(--motion-fast) var(--ease-standard),
                transform var(--motion-fast) var(--ease-standard)}
.todo-row:hover .todo-go{opacity:1;transform:translateX(2px)}
.todo-empty{display:flex;align-items:center;gap:var(--space-sm);padding:var(--space-lg);
     color:var(--mut);font-size:var(--text-sm)}

/* ── лента событий ────────────────────────────────────────────────────────── */
.feed{margin:0;padding:0;list-style:none}
.feed li{display:flex;align-items:baseline;gap:var(--space-sm);padding:8px 2px;
     border-bottom:1px solid var(--line-soft)}
.feed li:last-child{border-bottom:0}
.feed-time{flex:0 0 auto;color:var(--mut);font-size:var(--text-xs);white-space:nowrap;
     font-variant-numeric:tabular-nums;font-family:var(--font-mono)}
.feed-what{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
     color:var(--ink-soft)}
.feed-what b{color:var(--ink);font-weight:var(--weight-semi)}
.pager{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-sm);margin-top:var(--space-lg);
     padding-top:var(--space-md);border-top:1px solid var(--line)}

/* ── копирование значения по клику ────────────────────────────────────────── */
.copy-btn{display:inline-grid;place-items:center;width:26px;height:26px;min-height:0;
      padding:0;vertical-align:middle;
      border:1px solid var(--line);border-radius:var(--radius-xs);background:var(--surface-raised);
      color:var(--mut);cursor:pointer;
      transition:background-color var(--motion-fast) var(--ease-standard),
                 border-color var(--motion-fast) var(--ease-standard),
                 color var(--motion-fast) var(--ease-standard)}
.copy-btn:hover{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
.copy-btn .ico{width:14px;height:14px}
.copy-btn.is-done{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.code-cell{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}

/* ── точки состояния вместо цветных кружков эмодзи ────────────────────────── */
.dot-state{display:inline-block;width:9px;height:9px;flex:0 0 9px;border-radius:50%;
     background:var(--line-strong);vertical-align:middle;
     outline:1px solid var(--line-soft);outline-offset:1px}
.dot-state.on{background:var(--ok)}
.dot-state.off{background:var(--mut)}
.dot-state.bad{background:var(--bad)}
.dot-state.warn{background:var(--warn)}

/* ── палитра разделов (Ctrl+K) ────────────────────────────────────────────── */
.palette{position:fixed;inset:0;z-index:70;display:none;align-items:flex-start;justify-content:center;
     padding:var(--space-xxl) var(--space-lg);background:var(--overlay)}
.palette.is-open{display:flex}
.palette-box{width:min(560px,100%);border:1px solid var(--line-strong);border-radius:var(--radius-lg);
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
.palette-list a.on{background:var(--acc-soft);color:var(--acc);font-weight:var(--weight-semi)}
.palette-list a.on .ico{color:var(--acc)}
.palette-list .pal-group{padding:8px 11px 3px;color:var(--mut);font-size:var(--text-xs);
     letter-spacing:.7px;text-transform:uppercase}
/* Подпункт показан с отступом, а справа - название группы: иначе строка
   выглядит ещё одним разделом и не видно, что он часть подкатегории. */
.palette-list a.pal-sub{padding-left:var(--space-xxl);color:var(--mut)}
.palette-list a.pal-sub .ico{width:14px;height:14px}
.palette-list a.pal-sub::after{content:attr(data-group);margin-left:auto;
      font-size:var(--text-xs);color:var(--mut);opacity:.8}
.palette-foot{padding:var(--space-sm) var(--space-lg);border-top:1px solid var(--line);
     color:var(--mut);font-size:var(--text-xs)}
.pal-loading{display:grid;gap:var(--space-sm);padding:var(--space-sm) 11px}
.pal-loading .skel-line{display:block;width:100%;margin:0}
.pal-result-icon{display:grid;place-items:center;width:20px;height:20px;flex:0 0 20px;
     border:1px solid var(--line);border-radius:var(--radius-xs);color:var(--acc)}

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
.chart-box figcaption{margin-bottom:var(--space-sm);color:var(--ink-soft);font-size:var(--text-sm);
     font-weight:var(--weight-semi)}
.chart{display:block;width:100%;height:auto}
.chart .grid-line{stroke:var(--chart-grid);stroke-width:1}
.chart .axis{fill:var(--chart-axis);font-size:11px}
.donut-total{fill:var(--ink);font-size:22px;font-weight:var(--weight-bold);font-family:var(--font-mono)}
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
.legend-list b{font-variant-numeric:tabular-nums;font-family:var(--font-mono)}
.hbar-list{margin:0;padding:0;list-style:none;font-size:var(--text-sm)}
.hbar-list li{display:flex;align-items:center;gap:var(--space-sm);padding:3px 0}
.hbar-label{flex:0 0 34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-soft)}
.hbar-track{flex:1;height:12px;border-radius:5px;background:var(--chart-track);overflow:hidden}
.hbar-track i{display:block;height:100%;border-radius:5px;transform-origin:left center;
     animation:grow-x var(--motion-slow) var(--ease-out) both}
.spark svg{height:46px}
.chart-empty{padding:var(--space-xxl) 0;color:var(--mut);font-size:var(--text-sm);text-align:center}

/* ── рабочее место с обращениями ──────────────────────────────────────────── */
.workbench{display:grid;grid-template-columns:minmax(310px,350px) minmax(0,1fr);gap:var(--space-md);align-items:start}
.wb-queue,.wb-card{min-width:0}
.wb-queue{position:sticky;top:calc(var(--header-h) + var(--space-md))}
.wb-list{display:flex;flex-direction:column;gap:9px;max-height:64vh;overflow-y:auto;padding-right:2px}
.wb-item{display:grid;grid-template-columns:18px 1fr;align-items:start;gap:var(--space-sm);
     padding:14px 12px;border:1px solid var(--line-soft);background:var(--surface-sunken);border-radius:var(--radius-sm);cursor:pointer;
     transition:background-color var(--motion-fast) var(--ease-standard),
                border-color var(--motion-fast) var(--ease-standard)}
.wb-item:hover{background:var(--surface-raised)}
/* Выбранное обращение - то же, что выбранный раздел: полоса, заливка, цвет. */
.wb-item.wb-on{background:var(--acc-soft);border-color:var(--acc-line);
     box-shadow:inset 3px 0 0 var(--acc)}
.wb-item input{margin:3px 0 0;flex:0 0 auto}
.wb-item a{min-width:0;color:inherit;font-size:var(--text-sm);line-height:1.4;text-decoration:none}
.wb-item a:hover{text-decoration:none}
.wb-head{display:flex;align-items:baseline;gap:6px;white-space:nowrap}
.wb-head b{flex:0 0 auto;font-weight:var(--weight-semi)}
.wb-status{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;color:var(--mut)}
.wb-wait{display:inline-flex;align-items:center;gap:4px;color:var(--warn);white-space:nowrap;
     font-weight:var(--weight-semi)}
.wb-wait .ico{width:14px;height:14px}
.wb-age{display:block;color:var(--warn);font-size:11px;margin:6px 0;
     white-space:normal;font-variant-numeric:tabular-nums}
[data-timer-kind="in_progress"]{color:var(--acc)}
[data-timer-kind="message"]{color:var(--mut)}
.wb-filters{padding:4px 0 10px}
.wb-filters .grid{margin:0}
.wb-filters label{font-size:11px;color:var(--mut)}
.wb-queue>.card{padding:16px}
.wb-queue .card>h2{font-size:14px;padding:0}
.wb-card>.card{box-shadow:var(--shadow-md)}
.wb-reply{border:1px solid var(--acc-line);background:var(--acc-soft);padding:16px;border-radius:var(--radius-md)}
.wb-reply textarea{background:var(--surface)}
.wb-date{flex:0 0 auto;margin-left:auto;color:var(--mut);font-size:var(--text-xs);
     font-variant-numeric:tabular-nums;font-family:var(--font-mono)}
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
.wb-id,.wb-when{color:var(--mut);font-size:var(--text-xs);font-variant-numeric:tabular-nums;
     font-family:var(--font-mono)}
.wb-when{margin-left:var(--space-sm)}
.wb-text{display:block;margin-top:1px;overflow:hidden;text-overflow:ellipsis;
     white-space:nowrap;color:var(--mut)}
.wb-bulk{margin-top:10px;padding-top:var(--space-sm);border-top:1px solid var(--line);font-size:var(--text-sm)}
.wb-bulk summary{cursor:pointer;color:var(--ink-soft);font-weight:var(--weight-semi)}
.wb-card table{table-layout:fixed}
.wb-card table td,.wb-card table th{vertical-align:top;word-break:break-word}
.wb-card table td:nth-child(1){width:130px;min-width:130px}
.wb-card table td:nth-child(2){width:180px;min-width:180px}
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
     list-style:none;cursor:pointer;color:var(--ink);font-weight:var(--weight-semi);
     transition:color var(--motion-fast) var(--ease-standard)}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{color:var(--acc)}
details>summary::after{content:"";width:9px;height:9px;flex:0 0 9px;margin-left:auto;
     border-right:1.75px solid var(--mut);border-bottom:1.75px solid var(--mut);
     transform:rotate(45deg);transition:transform var(--motion-base) var(--ease-standard)}
details[open]>summary{color:var(--acc);border-bottom:1px solid var(--line-soft);
     margin-bottom:var(--space-md)}
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
     border:1px solid var(--line-strong);border-radius:var(--radius-md);background:var(--surface);
     box-shadow:var(--shadow-lg);font-size:var(--text-sm);
     animation:toast-in var(--motion-slow) var(--ease-out) both}
.toast .ico{margin-top:1px;flex:0 0 auto}
.toast-close{min-height:0;margin-left:auto;padding:0 2px;border:0;background:transparent;
     color:inherit;font-size:var(--text-lg);line-height:1}
.toast-close:hover{background:transparent;color:inherit;transform:none;box-shadow:none}
.toast-ok{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.toast-bad{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
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
     padding:var(--space-xxl) var(--space-lg);color:var(--mut);border:1px dashed var(--line-strong);
     border-radius:var(--radius-md);background:var(--surface-sunken);
     animation:card-in var(--motion-slow) var(--ease-standard) both}
.empty .ico{width:34px;height:34px;color:var(--mut);stroke-width:1.5}
.empty b{color:var(--ink-soft);font-size:var(--text-lg);font-weight:var(--weight-semi)}
.empty span{font-size:var(--text-sm)}

/* ── журнал ──────────────────────────────────────────────────────────────── */
pre{max-height:560px;margin:0;padding:var(--space-lg);overflow:auto;
    border:1px solid var(--line);border-radius:var(--radius-md);
    background:var(--code-bg);color:var(--code-ink);
    font-family:var(--font-mono);font-size:var(--text-sm);line-height:1.5;
    white-space:pre-wrap;word-break:break-all}

/* ── счётчик: число докручивается средствами CSS, без JavaScript ────────── */
@property --n{syntax:"<integer>";initial-value:0;inherits:false}
.count{--to:0;--n:0;counter-reset:c var(--n);font-variant-numeric:tabular-nums;
     font-family:var(--font-mono);
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
  .app-nav{position:sticky;top:0;width:auto;height:auto;display:flex;flex-wrap:nowrap;gap:6px;
      padding:8px 12px;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x proximity;
      -webkit-overflow-scrolling:touch;scrollbar-width:none;
      border-right:0;border-bottom:1px solid var(--line)}
  .app-nav::-webkit-scrollbar{display:none}
  .app-nav a{margin:0;padding:9px 12px;font-size:var(--text-sm);white-space:nowrap;flex:0 0 auto;
        scroll-snap-align:start}
  /* Полоса «сейчас тут» в горизонтальной ленте смотрит сверху: сбоку её
     не видно за соседними пунктами. */
  .app-nav a.on{box-shadow:inset 0 3px 0 var(--acc)}
  .app-nav a:hover{transform:none}
  .nav-txt{white-space:nowrap}
  .nav-groups{flex-direction:row;flex-wrap:nowrap;align-items:center;gap:var(--space-md)}
  .nav-group{flex-direction:row;align-items:center;gap:4px}
  .nav-group-items{flex-direction:row;gap:4px}
  .nav-group-name{display:none}
  .nav-group-label{padding:0 2px}
  /* подпункты в горизонтальной ленте слились бы с основными, поэтому подпись
     убирается, а сами ссылки встают в ряд - как все остальные */
  .nav-sub{flex-direction:row;align-items:center;gap:4px;margin:0;padding-left:0;
      border-left:0}
  .nav-sub-label{display:none}
  .nav-sub-items{flex-direction:row;gap:4px}
  .workbench{grid-template-columns:1fr}
  .wb-queue{position:static}
  .wb-list{max-height:40vh}
  main{margin-left:0;padding:16px 12px 40px}
  footer{margin-left:0;padding:12px}
  .page-title{font-size:var(--text-xl)}
  .stat,.kpi div{flex:1 1 44%}
  .charts{grid-template-columns:1fr}
  .card{padding:var(--space-lg)}
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
  .app-nav{padding:7px 10px;gap:5px}
  .app-nav a{padding:11px 13px;font-size:var(--text-base);min-height:44px}
  .nav-groups{gap:var(--space-md)}
  .nav-group-label{padding:0 1px;font-size:10.5px;letter-spacing:.4px}
  .nav-badge{min-width:19px;padding:0 5px;font-size:10.5px;line-height:16px}
  .dochead{gap:var(--space-sm)}
  .dochead .page-actions{margin-left:0;width:100%}
  .big-stats{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
  .big-stat b{font-size:23px}
  .big-stat .big-txt{font-size:var(--text-lg)}
  .copy-btn{width:44px;height:44px;min-height:44px}
  .wb-edit,.wb-reply{display:block}
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
  input,select,textarea{padding:11px 12px;font-size:16px;min-height:var(--tap-min)}
  textarea{min-height:96px}
  label{margin-top:12px}
  .stat,.kpi div{flex:1 1 calc(50% - 6px);padding:12px 13px}
  .stat b{font-size:23px}
  .kpi b{font-size:var(--text-lg)}
  .wb-list{max-height:none}
  .wb-item{padding:11px 9px}
  .wb-tools{gap:var(--space-xs)}
  .wb-head{white-space:normal;flex-wrap:wrap}
  /* на телефоне минимальная ширина только мешала бы: карточка переписки и без
     того в одну колонку, и лишняя прокрутка вбок тут ни к чему */
  .wb-card table{min-width:0}
  .wb-card table td:nth-child(1),.wb-card table td:nth-child(2){width:auto;min-width:0}
  .donut{width:130px;height:130px;flex:0 0 130px}
  pre{font-size:11.5px;padding:12px;max-height:none}
  footer{padding:14px 12px 30px;text-align:center}
  .msg{padding:11px 12px}
  .empty{padding:var(--space-xl) var(--space-md)}
  .toast-stack{right:10px;left:10px;bottom:10px}
}

@media (max-width:1200px){
  .big-stats{grid-template-columns:repeat(3,minmax(0,1fr))}
  .header-account{gap:8px}.who{gap:8px}.who .avatar{display:none}
  .gsearch{max-width:400px}.brand{min-width:205px}
  .login-shell{gap:40px}
}
@media (max-width:1000px){
  .app-header{min-height:72px;flex-wrap:nowrap;gap:12px;padding:12px 16px}
  .brand{min-width:0}.header-account{flex-shrink:0}.who{display:none}
  .app-nav{top:72px;padding:10px 12px}
  .nav-caption{display:none}.nav-groups{gap:20px}
  .bento-day,.bento-actions,.bento-audit,.bento-events{grid-column:span 6}
  .hero-sign{display:none}.dashboard-hero{padding:24px}
  .bento .today-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .login-shell{grid-template-columns:minmax(0,1fr) minmax(310px,.9fr);gap:28px}
  .login-card{padding:26px}.login-story h1{font-size:38px}
  .login-brand{margin-bottom:30px}.login-points{gap:12px}
}
@media (max-width:640px){
  .app-header{min-height:64px;padding:10px;gap:8px}
  .brand{flex:1;min-width:0}.brand b{font-size:14px}.brand .logo{width:36px;height:36px}
  .gsearch{flex:0 0 44px;min-width:44px;width:44px;height:44px;min-height:44px;padding:0;justify-content:center}
  .gsearch span,.gsearch kbd{display:none}
  .header-account{margin-left:0;gap:6px}.theme-toggle{width:44px;height:44px;flex-basis:44px}
  .shell-logout .link-button{width:44px;height:44px;min-height:44px;padding:0;border-radius:50%}
  .shell-logout .link-button span{display:none}
  .app-nav{top:64px;padding:8px 10px}.nav-groups{gap:18px}
  .dochead-title .page-title{font-size:26px;letter-spacing:-.8px}
  .dashboard-hero{padding:20px;border-radius:20px}.dashboard-hero h2{font-size:21px}.dashboard-hero p{font-size:12px}
  .big-stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .big-stat{padding:16px}.big-stat .big-ico{margin-bottom:9px}.big-stat span{font-size:12px}
  .bento{grid-template-columns:1fr;gap:12px}.bento>.card{grid-column:auto}
  .bento .today-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .bento .todo-name{font-size:12px}.bento .card-title>a{font-size:11px}
  .today-stat{padding:12px}.scale-line{padding:12px;font-size:11px;gap:9px}
  .bento .feed li{flex-wrap:wrap}.bento .feed-time{max-width:none;width:100%}
  .app-footer{font-size:10px;flex-direction:column;gap:4px}
  .wb-item{min-height:100px}.wb-reply{padding:12px}
  .wb-edit,.wb-reply{display:block}.wb-tools .btn{white-space:normal;text-align:center}
  .login-page{padding:72px 18px 24px}.login-shell{display:block;width:min(430px,100%)}
  .login-brand{margin-bottom:26px}.login-brand .logo{width:44px;height:44px}
  .login-brand b{font-size:22px}.login-story h1{font-size:35px;letter-spacing:-1.5px}
  .login-lead{font-size:13px;margin-bottom:18px}.login-flow{font-size:10px;gap:4px}
  .login-flow>span{padding:8px;gap:5px}.login-flow .ico{width:16px;height:16px}
  .login-points,.login-orbit{display:none}.login-story{margin-bottom:24px}
  .login-card{padding:26px 22px}.login-card h2{font-size:22px}
  .login-card input:not([type=checkbox]){font-size:16px}.login-card .remember-row{font-size:12px;min-height:44px}
  .login-footer{font-size:10px;margin-top:24px}.login-theme{top:16px;right:18px}
  .palette{padding:14px 10px}.palette-list a{min-height:44px;white-space:normal}
  .toast-stack .toast-close{min-width:44px;min-height:44px;padding:0}
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
  .dochead .page-actions,.page-actions,.btn-row,.toolbar,.actions,
  .wb-queue,.wb-edit,.wb-reply,.wb-access,.wb-bulk,.wb-tools,
  .no-print{display:none!important}
  main{margin:0;padding:0;max-width:none}
  /* липкая шапка таблицы на бумаге бесполезна и пристаёт к следующей странице */
  th{position:static}
  table{font-size:var(--text-sm)}
  .workbench{grid-template-columns:1fr}
  .wb-list{max-height:none;overflow:visible}
  /* На бумаге многоточие не видно - текст обрезался бы молча. Поэтому у
     шапки карточки отменяем и ellipsis, и запрет на перенос строк. */
  .wb-who,.wb-fio,.wb-brief,.wb-name,.wb-group,.wb-id,.wb-when,.wb-access-name{
    white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
  .card{break-inside:avoid;box-shadow:none}
  .card:hover{transform:none}
}

/* строка с версией и состоянием бота. Разметка ставит её между <header> и
   <nav>, а меню - липкая колонка шириной var(--sidebar). С отступом от левого
   края окна первые 252 пикселя строки перекрывались меню, и надпись
   «версия …» выглядела обрезанной; теперь строка начинается там же, где
   содержимое страницы, и на телефоне сдвигается вместе с ним. */
.foot-ver{margin:0 0 0 var(--sidebar);padding:10px 26px 0;max-width:var(--content-max);
          color:var(--mut);font-size:var(--text-xs);display:flex;gap:var(--space-sm);
          align-items:center;flex-wrap:wrap}
.foot-ver a{color:var(--mut);text-decoration:underline;
            text-decoration-color:var(--line-strong);
            transition:color var(--motion-fast) var(--ease-standard)}
.foot-ver a:hover{color:var(--acc)}
.panel-beta{display:inline-flex;align-items:center;gap:var(--space-xs);padding:1px 7px;
            border:1px solid var(--acc-line);border-radius:var(--radius-pill);
            background:var(--acc-soft);color:var(--acc);font-size:var(--text-xs);
            font-weight:var(--weight-semi);line-height:1.5;white-space:nowrap}
.panel-beta .ico{width:14px;height:14px}
.feedback-body{overflow-wrap:anywhere;line-height:1.7;padding:var(--space-md) 0;margin-bottom:var(--space-md)}
@media (max-width:1000px){.foot-ver{margin-left:0;padding:10px 12px 0}}
@media print{.foot-ver{display:none}}

"""

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
