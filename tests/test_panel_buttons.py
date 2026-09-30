"""Кнопки не смыкаются: страховка от повторной жалобы «кнопки слипшиеся».

Жалоба повторялась трижды, и каждый раз по одной причине: панель печатает
кнопки прямо в контейнер - в шапке страницы (``.page-actions``), в карточке
обращения (``.wb-tools``), в форме (``.grid``) и даже прямо в ячейке таблицы,
а зазор был записан только для одного из этих случаев. Ячейку таблицы не
ловил вообще: там кнопки стояли бортами и читались как одна.

Поэтому проверка не «сравнивает картинку», а требует самого механизма: у
каждого известного ряда кнопок должно быть правило с ``gap``, и должен быть
страховочный селектор, который даёт зазор контейнеру с двумя и более
кнопками подряд - тогда новая разметка без класса тоже не окажется слипшейся.

Вторая половина файла - про поля, таблицы и про печать: поля не залезают
друг на друга, узкие колонки не ложатся в «букву в столбик», а печатная
версия по-прежнему убирает органы управления и оставляет переписку.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                      # модуль лежит в корне репозитория
    sys.path.insert(0, str(ROOT))

import panel_theme as theme                        # noqa: E402  (путь задаём выше)

CSS = re.sub(r"/\*.*?\*/", "", theme.STYLESHEET, flags=re.S)
RULE = re.compile(r"([^{}]+)\{([^}]*)\}")


def selectors(head: str) -> list[str]:
    """Селекторы правила по отдельности: ``button,.btn`` - это два селектора."""
    return [part.strip() for part in head.split(",")]


def bodies(name: str) -> list[str]:
    """Тела всех правил, в списке селекторов которых есть name."""
    return [body for head, body in RULE.findall(CSS) if name in selectors(head)]


def body(name: str) -> str:
    """Тело первого правила с селектором name."""
    found = bodies(name)
    assert found, f"нет ни одного правила с селектором {name}"
    return found[0]


def shared(*names: str) -> str:
    """Тело правила, в котором селекторы стоят рядом: ``button,.btn``."""
    for head, text in RULE.findall(CSS):
        if all(name in selectors(head) for name in names):
            return text
    raise AssertionError(f"нет правила с селекторами {', '.join(names)}")


def print_rules() -> str:
    """Все блоки @media print вместе: их в теме два (палитра и вёрстка)."""
    found = []
    for match in re.finditer(r"@media print\s*\{", CSS):
        depth, index = 1, match.end()
        while index < len(CSS) and depth:
            depth += (CSS[index] == "{") - (CSS[index] == "}")
            index += 1
        found.append(CSS[match.end():index - 1])
    assert found, "в теме нет печатной версии"
    return "\n".join(found)


# ── 1. у каждого ряда кнопок есть зазор ─────────────────────────────────────
# Список закрытый: сюда попадает всё, чем панель печатает ряд кнопок. Класс,
# которого здесь нет, надо сначала вписать - иначе он снова окажется без
# зазора, и ни одна проверка об этом не скажет.
BUTTON_ROWS = (".page-actions", ".wb-tools", ".btn-row", ".actions", ".toolbar")


def test_every_button_row_has_a_gap():
    for name in BUTTON_ROWS:
        rule = body(name)
        assert "display:flex" in rule, f"{name} не стал рядом: {rule}"
        assert "gap:var(--space-sm)" in rule, f"у {name} нет зазора: {rule}"
        assert "flex-wrap:wrap" in rule, f"{name} не переносится: {rule}"


def test_form_grid_keeps_its_gap():
    """``.grid`` - тоже ряд: кнопки и поля в форме стояли вплотную друг к другу."""
    assert "gap:var(--space-md)" in body(".grid")
    # ячейка с кнопкой не растягивается на всю строку: из-за этого ряд
    # действий выглядел разбросанным по странице, а не собраным в рядок
    assert "flex:0 0 auto" in body(".grid>button")
    assert "flex:1 1 180px" in body(".grid>*"), "поля в форме перестали тянуться"


def test_pager_and_cards_keep_their_gap():
    """Готовые ряды панели: постраничный переход и карточки-показатели."""
    for name in (".pager", ".cards"):
        assert "gap:var(--space" in body(name), f"у {name} нет зазора"


def test_any_container_with_two_buttons_gets_a_gap():
    """Страховка от повторения жалобы: класс не поставили - зазор всё равно есть.

    Селектор-счётчик соседей: контейнер, у которого две и более кнопки подряд,
    становится рядом сам. Без него любая новая разметка с рядом кнопок
    повторит старую ошибку, и заметить её можно было бы только глазами.
    """
    found = [(head, text) for head, text in RULE.findall(CSS)
             if ":has(" in head and ":is(button" in head]
    assert found, "нет правила, которое ловит ряд кнопок по соседям"
    for head, text in found:
        assert "display:flex" in text and "gap:var(--space-sm)" in text, text
        assert "form.inline" in head, "форма-обёртка кнопки не посчитана кнопкой"
    # список тегов задан явно: с уголком правило задевало бы .card
    for head, _text in found:
        assert ":where(>" not in head, f"у правила уголок в списке тегов: {head}"


def test_buttons_keep_their_own_spacing():
    """У кнопки есть отступы и своя высота: иначе она липла к рамке контейнера."""
    rule = shared("button", ".btn")
    assert "margin:0" in rule, f"у кнопки не обнулён отступ: {rule}"
    assert "min-height:var(--field-min)" in rule, f"у кнопки нет своей высоты: {rule}"
    assert "padding:9px 15px" in rule
    assert "gap:6px" in rule, "иконка в кнопке липла к подписи"
    # квадратные кнопки (поиск, тема, копирование) сбрасывают общую высоту поля
    for name in (".copy-btn", ".gsearch button", ".theme-toggle"):
        assert any("min-height:0" in text for text in bodies(name)), (
            f"{name} растянулся под общую высоту поля")


# ── 2. поля не заезжают друг на друга ──────────────────────────────────────
def test_fields_have_predictable_geometry():
    rule = shared("input", "select", "textarea")
    assert "min-width:0" in rule, "поле не может сжаться и распирает соседей"
    assert "max-width:100%" in rule
    assert "display:block" in rule, "поле встаёт в строку подписи"
    assert "min-height:var(--field-min)" in rule, "у полей разная высота"
    assert "font:inherit" in rule and "line-height:var(--field-line)" in rule


def test_label_stays_above_its_field():
    """Подпись - отдельный блок, а в ячейке формы верхний отступ не нужен."""
    assert "display:block" in body("label")
    assert "margin:var(--space-md) 0 var(--space-xs)" in body("label")
    # внутри .grid ритм задаёт зазор, иначе подписи соседних ячеек стояли бы
    # на разной высоте, а это и есть «поля заходят друг на друга»
    assert "margin-top:0" in body(".grid>div>label")


def test_checkbox_is_not_a_field():
    """Флажок не должен растягиваться под общую высоту поля и съедать ячейку."""
    rule = body("input[type=checkbox]")
    assert "width:auto" in rule and "min-height:0" in rule and "min-width:0" in rule


# ── 3. таблицы не разъезжаются ─────────────────────────────────────────────
def test_table_cells_wrap_and_can_shrink():
    cells = shared("th", "td")
    assert "min-width:0" in cells, "длинное слово распирает колонку"
    assert "overflow-wrap:break-word" in cells
    assert any("word-break:break-word" in text for text in bodies("td")), (
        "длинное слово без пробелов не переносится по ячейке")
    # липкая шапка на бумаге пристаёт к следующей странице
    assert "th{position:static" in print_rules()


def test_narrow_table_columns_do_not_fold_into_letters():
    """У таблиц с неизвестным содержимым есть минимальная ширина."""
    assert "min-width:var(--table-min)" in CSS
    assert "min-width:var(--table-min-wide)" in CSS
    assert "table-layout:auto" in CSS
    # на телефоне минимальная ширина только мешала бы: карточка в одну колонку
    assert ".wb-card table{min-width:0}" in print_rules() or ".wb-card table{min-width:0}" in CSS


# ── 4. печать: на бумагу попадает переписка ────────────────────────────────
def test_print_hides_every_control():
    rule = print_rules()
    for name in (".wb-edit", ".wb-reply", ".wb-tools", ".wb-queue", ".wb-access",
                 ".wb-bulk", ".copy-btn", ".toast-stack", ".palette"):
        assert name in rule, f"на печати не убран {name}"
    # кнопки в шапке и во всех рядах на бумаге не нужны
    for name in (".dochead .page-actions", ".page-actions", ".btn-row", ".toolbar"):
        assert name in rule, f"на печати не убран {name}"


def test_print_keeps_the_thread_readable():
    rule = print_rules()
    for name in (".wb-fio", ".wb-who", ".wb-name"):
        assert name in rule, f"в печати нет правила для {name}"
    assert "text-overflow:clip" in rule, "многоточие на бумаге не видно"


# ── 5. шаблон ответа подставляется в поле ──────────────────────────────────
def test_template_choice_fills_the_reply_field():
    """Выбор шаблона кладёт его текст в поле ответа и ставит туда курсор."""
    script = theme.actions_script()
    assert 'addEventListener("change"' in script, "нет обработчика выбора"
    assert 'node.getAttribute("name") === TEMPLATE_FIELD' in script
    assert 'TEMPLATE_FIELD = "template"' in script
    assert "data-text" in script, "текст шаблона берётся не из пункта списка"
    assert 'querySelector(\'textarea[name="text"]\')' in script, "нет поля ответа рядом"
    assert "field.value = text" in script, "текст шаблона не кладётся в поле"
    assert "field.focus()" in script, "курсор не переходит в поле"


def test_own_text_is_never_silently_replaced():
    """Написанное руками спрашиваем, а помеченный шаблоном - нет."""
    script = theme.actions_script()
    assert "window.confirm(" in script, "своё молча затирается"
    assert "Заменить набранный ответ" in script, "в вопросе не сказано, что именно"
    # помеченное шаблоном поле повторно не спрашивает, а ручная правка
    # пометку снимает
    assert 'field.setAttribute("data-from-template", "1")' in script
    assert 'field.getAttribute("data-from-template") !== "1"' in script
    assert 'removeAttribute("data-from-template")' in script


def test_template_script_keeps_the_rest_of_the_page():
    """Скрипт не ломает ни копирование, ни фильтры, ни тему."""
    script = theme.actions_script()
    for needle in ("navigator.clipboard", "panel-filters", "localStorage.getItem(KEY)",
                   "localStorage.removeItem(KEY)", "panelToast"):
        assert needle in script, f"скрипт потерял {needle}"
    # обработчика клавиш в нём нет вовсе: j/k/Enter остаются у hotkeys_script
    assert 'addEventListener("keydown"' not in script
    assert "<script" not in script and "</script>" not in script


# ── 7. подпись кнопки не переносится (жалоба «по буквам вниз») ─────────────
def test_button_label_never_wraps():
    """Подпись кнопки обязана быть в одну строку.

    Без этого правила .grid (flex:1 1 180px) сжимает кнопку до ширины ячейки,
    и подпись переносится: «Вкл/выкл» превращалась в восемь строк.
    """
    found = [rule for rule in bodies("button") if "white-space:nowrap" in rule]
    found += [rule for rule in bodies(".btn") if "white-space:nowrap" in rule]
    assert found, (
        "в теме нет правила white-space:nowrap для кнопок - подпись снова "
        "начнёт переноситься по буквам"
    )


def test_button_still_wraps_as_a_row():
    """Запрет переноса подписи не должен ломать перенос самих кнопок.

    Кнопка не переносит текст ВНУТРИ, но кнопки в .grid обязаны переноситься
    ДРУГ ЗА ДРУГОМ, когда не помещаются: иначе длинная кнопка наедет на соседнюю.
    """
    assert "flex-wrap:wrap" in body(".grid"), "кнопки в .grid перестали переноситься"


# ── 8. колонка действий не схлопывается в букву ───────────────────────────
def test_action_column_is_protected_from_squeezing():
    """Колонка с кнопкой не должна сжиматься до ширины одной буквы."""
    assert "white-space:nowrap" in body(".col-act"), "у .col-act нет запрета переноса"
    act_widths = " ".join(bodies("th.col-act") + bodies("td.col-act"))
    assert "width:1%" in act_widths, (
        "колонке действий не задан минимальный размер - она отдаст ширину "
        "колонке с текстом и кнопка снова развалится"
    )


def test_action_column_class_is_actually_used():
    """Класс есть в теме - значит его кто-то должен и печатать."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1]
    used = []
    for module in sorted((root / "web").glob("*.py")):
        body_text = module.read_text(encoding="utf-8-sig")
        if "col-act" in body_text:
            used.append(module.name)
    assert used, "никто не печатает col-act - правило в теме мёртвое"
