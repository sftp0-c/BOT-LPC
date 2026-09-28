"""Подпись кнопки не может быть пустой: иначе MAX роняет всё сообщение.

Найдено по журналу: MAX отвечает 400 proto.payload «Field 'text' size (0) must
be at least 1», когда кнопка без текста. Подпись становилась пустой, когда у
человека не заполнено ФИО: short_name("") возвращала пустую строку. В такой
экран не попадал никто - падало сообщение целиком.
"""
import ast
from pathlib import Path

import pytest

import max_api
import utils

# Файл, из которого берём экраны со списками людей
ADMIN = Path("handlers/admin.py")


def test_button_label_is_never_empty():
    assert max_api.btn("", "x")["text"].strip()
    assert max_api.btn("   ", "x")["text"].strip()
    assert max_api.btn(None, "x")["text"].strip()
    assert max_api.link_btn("", "https://example.org")["text"].strip()


def test_fitting_keeps_label_even_if_it_was_empty():
    """Подгонка под ширину ряда тоже не должна выпускать пустую подпись."""
    rows = max_api.fit_keyboard([[{"type": "callback", "text": "", "payload": "x"}]])
    assert rows[0][0]["text"].strip(), "подгонка оставила пустую подпись"


def test_short_name_says_something_instead_of_nothing():
    assert utils.short_name("") == "Без имени"
    assert utils.short_name("   ") == "Без имени"
    assert utils.short_name("Иванов Иван") == "Иванов Иван"


def test_person_without_name_is_identifiable_by_id():
    """Без ФИО человека не опознать: ник в профиле MAX бывает скрыт."""
    assert utils.person_label("", "300") == "ID 300"
    assert utils.person_label("   ", "46010397") == "ID 46010397"
    # не-цифры из идентификатора не тащим в подпись
    assert utils.person_label("", "u-300") == "ID 300"
    # без ФИО и без id остаётся хоть что-то
    assert utils.person_label("", "") == "Без имени"


def test_person_with_name_is_not_replaced_by_id():
    assert utils.person_label("Ковалевский Константин Юрьевичович", "300") == "Ковалевский К. Ю."


def test_people_screens_use_person_label():
    """Списки людей зовут person_label, а не short_name: без ID они нечитаемы."""
    source = ADMIN.read_text(encoding="utf-8-sig")
    assert "person_label(" in source, "в списках людей нет person_label"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "btn"):
            continue
        if node.args and isinstance(node.args[0], ast.Call) \
                and isinstance(node.args[0].func, ast.Name) \
                and node.args[0].func.id == "short_name":
            pytest.fail(f"в admin.py:{node.lineno} кнопка человека без person_label")


def test_every_literal_button_label_is_non_empty():
    """Ни в одной коде-кнопке нет пустой строки."""
    for path in list(Path("handlers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"btn", "link_btn"} and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and not first.value.strip():
                pytest.fail(f"{path}:{node.lineno} пустая подпись кнопки")
