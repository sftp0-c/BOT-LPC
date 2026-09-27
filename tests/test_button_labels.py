"""Ни одна подпись кнопки не должна обрезаться многоточием в MAX.

MAX рисует кнопку в одну строку и обрезает подпись. Чтобы это не повторилось,
проверяем дважды: текстом в исходниках (статически) и живым проходом по экранам.
"""
import re
from pathlib import Path

import max_api
import pytest
from conftest import add_staff, press, register, say

STUDENT = "100"
STAFF = "500"
DIRECTOR = "501"
SYS = "1"

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [path for path in list(ROOT.glob("*.py")) + list((ROOT / "handlers").glob("*.py"))
           if not path.name.startswith("test_")]


def labels_of(api, user) -> list[str]:
    """Подписи всех кнопок последнего сообщения."""
    return [b["text"] for row in (api.last(user)[2] or []) for b in row]


def all_labels(api) -> list[str]:
    """Подписи всех кнопок во всех сообщениях, которые бот отправил.

    Клавиатуры приходят разной вложенностью (рядами, одним рядом, кортежем),
    поэтому разбираем рекурсивно - проверка должна ловить обрезку везде.
    """
    def walk(node, out: list[str]) -> None:
        if isinstance(node, dict):
            out.append(node.get("text", ""))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, out)

    out: list[str] = []
    for _to, _text, keyboard in api.sent:
        walk(keyboard, out)
    return out


# ── сам механизм обрезки ────────────────────────────────────────────────────
def test_short_label_keeps_short_text():
    assert max_api.short_label("🏠 Меню") == "🏠 Меню"


def test_short_label_cuts_by_word():
    assert max_api.short_label("🗑 Удалить вместе с обращениями") == "🗑 Удалить вместе с…"


def test_short_label_cuts_long_word_too():
    label = max_api.short_label("а" * 50)
    assert len(label) == max_api.BUTTON_TEXT and label.endswith("…")


def test_short_label_trims_spaces():
    assert max_api.short_label("  много   пробелов  ") == "много пробелов"


def test_btn_and_link_btn_both_cut():
    assert len(max_api.btn("б" * 80, "x")["text"]) <= max_api.BUTTON_TEXT
    assert len(max_api.link_btn("б" * 80, "https://example.org")["text"]) <= max_api.BUTTON_TEXT


# ── статическая проверка исходников ────────────────────────────────────────
@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_button_label_longer_than_the_limit(source: Path):
    """В коде не должно быть подписей, которые MAX обрежет.

    f-строки пропускаем: их текст собирается в рантайме и режется в short_label,
    а проверить можно только готовые кнопки (тесты ниже).
    """
    text = source.read_text(encoding="utf-8")
    long_ones = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in re.finditer(r'(?:btn|link_btn)\(\s*f?["\']([^"\']+)["\']', line):
            label = match.group(1)
            if "{" in label:          # f-строка: длину узнать нельзя
                continue
            if len(label) > max_api.BUTTON_TEXT:
                long_ones.append(f"{source.name}:{number} «{label}»")
    assert not long_ones, "подписи длиннее предела: " + ", ".join(long_ones)


# ── живая проверка экранов ──────────────────────────────────────────────────
async def test_student_screens_have_short_labels(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff(DIRECTOR, "Сидоров Пётр Петрович", category="all")
    for payload in ("home", "sub:cert", "sub:acc", "sub:fb", "faq", "college",
                    "profile", "myall", "tickets", "new:certificates", "sched"):
        await press(STUDENT, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


async def test_staff_screens_have_short_labels(api):
    await register(STUDENT)
    await add_staff(STAFF, "Соколова Мария Сергеевна", position="Методист учебной части")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    for payload in ("home", "staff", "staffcat", "staffstats", "tickets", "profile"):
        await press(STAFF, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


async def test_sysadmin_screens_have_short_labels(api):
    await add_staff(STAFF, "Соколова Мария Сергеевна", position="Методист учебной части")
    for payload in ("admins", "people", "codes", "settings", "groups", "today",
                    "nostaff", "syslist", "demo", "more", "broadcast"):
        await press(SYS, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


async def test_schedule_screens_have_short_labels(api):
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    for payload in ("sched", "teacher", "view_schedules", "groups"):
        await press(STUDENT, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


async def test_labels_have_no_double_spaces(api):
    """Обрезка не должна оставлять висячие пробелы или обрыв слова."""
    await register(STUDENT)
    await press(STUDENT, "sub:cert")
    for label in labels_of(api, STUDENT):
        assert label == label.strip()
        assert "  " not in label
