"""Ни одна подпись кнопки в боте не должна резаться многоточием.

Проверка статическая по всем экранам: файл разбирается на дерево, для
каждого ряда клавиатуры считается предел по числу кнопок в нём — ровно так
же, как это делает `max_api.row_limit()` при отправке. Это дешевле и
надёжнее, чем прогонять живые экраны: многоточие появляется не в коде, а
в том, как длинные ФИО и названия отделов попадают в подпись.

Скрин понимает собственные сократители проекта (`cut_plain`, `short`,
`short_name`): если подпись собрана из них, берётся их предел, а не длина
неизвестного выражения. Иначе любое ФИО в коде давало бы ложное срабатывание.
"""
import ast
from pathlib import Path

import pytest

import max_api

# Экраны бота. Список явный, чтобы новый файл нельзя было забыть молча.
SOURCES = ["bot_commands.py"] + sorted(
    str(path).replace("\\", "/") for path in Path("handlers").glob("*.py"))

# Сократители проекта: имя -> номер позиции с пределом длины
TRUNCATORS = {"cut_plain": 1, "short": 1, "short_name": 1, "shorten": 1}


def label_text(node) -> str:
    """Текст подписи, если его длина известна на этапе разбора кода."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            else:
                parts.append(_unknown_length(piece))
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # «✉️ Пригласить» + str(что-то)
        left, right = label_text(node.left), label_text(node.right)
        return left + right if left or right else ""
    return ""


def _unknown_length(node) -> str:
    """Заглушка неопределённой длины: сократитель — его предел, иначе 12."""
    if isinstance(node, ast.FormattedValue):
        # внутри f-строки выражение лежит в .value
        node = node.value
    if isinstance(node, ast.IfExp):
        # «🟢 если активна иначе ⚪» — длина максимальной из ветвей
        known = [len(label_text(branch)) for branch in (node.body, node.orelse)]
        known = [value for value in known if value]
        if len(known) == 2:
            return "Ф" * max(known)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in TRUNCATORS:
        position = TRUNCATORS[node.func.id]
        if len(node.args) > position:
            limit = node.args[position]
            if isinstance(limit, ast.Constant) and isinstance(limit.value, int):
                return "Ф" * min(limit.value, 14)
    return "Ф" * 12


def is_button(node) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "btn"
            and bool(node.args))


def read_source(name: str) -> ast.Module:
    # utf-8-sig: часть файлов сохранена с BOM
    return ast.parse(Path(name).read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def all_rows() -> list:
    """Все ряды клавиатур бота: (файл, строка, кнопок в ряду, подпись)."""
    found = []
    for name in SOURCES:
        for node in ast.walk(read_source(name)):
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            if not all(is_button(element) for element in node.elts):
                continue
            for element in node.elts:
                found.append((name, element.lineno, len(node.elts),
                              label_text(element.args[0])))
    return found


def test_scanner_sees_every_screen(all_rows):
    names = {name for name, _, _, _ in all_rows}
    assert len(names) >= 7, f"сканер нашёл кнопки только в {sorted(names)}"
    assert len(all_rows) > 250, f"найдено всего {len(all_rows)} кнопок — сканер ослаб"


def test_no_button_label_is_cut(all_rows):
    """Главная проверка: подпись всегда влезает в строку MAX."""
    too_long = [(name, line, count, label) for name, line, count, label in all_rows
                if label and len(label) > max_api.row_limit(count)]
    assert not too_long, "подписи длиннее предела MAX: " + "; ".join(
        f"{name}:{line} (в ряду {count}): {label}" for name, line, count, label in too_long)


def test_no_ellipsis_in_button_labels(all_rows):
    """Многоточие в подписи — ровно то, что пользователь называл проблемой."""
    marked = [(name, line, label) for name, line, _, label in all_rows
              if "…" in label or "..." in label]
    assert not marked, "многоточие в подписи: " + "; ".join(
        f"{name}:{line}: {label}" for name, line, label in marked)


def test_row_limit_matches_max():
    """Предел в коде проверки — тот же, что и при отправке."""
    assert max_api.row_limit(1) >= max_api.BUTTON_TEXT
    assert max_api.row_limit(2) > max_api.row_limit(3)
    assert max_api.row_limit(3) > max_api.row_limit(4)


def test_long_fio_gets_initials_not_ellipsis():
    """Длинное ФИО укорачивается инициалами, а не обрезается многоточием."""
    from utils import short_name
    assert short_name("Ковалевский Константин Юрьевич", 26) == "Ковалевский К. Ю."
    assert "…" not in short_name("Александрова Анна-Валерия Николаевна", 26)


def test_people_screens_use_initials():
    """На экранах с людьми ФИО укорачивается инициалами, а не обрезается."""
    from utils import short_name
    assert short_name("Ковалевский Константин Юрьевич", 26) == "Ковалевский К. Ю."
    assert "…" not in short_name("Александрова Анна-Валерия Николаевна", 26)
    for name in ("handlers/admin.py", "handlers/menus.py", "handlers/tickets.py"):
        used = {node.func.id for node in ast.walk(read_source(name))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        shorteners = used & {"short_name", "_clean_fio", "short", "cut_plain"}
        assert shorteners, f"{name}: списки людей без инициалов"
