"""Ни одна подпись кнопки не должна обрезаться многоточием в MAX.

MAX рисует кнопку в одну строку и обрезает подпись. Чтобы это не повторилось,
проверяем дважды: текстом в исходниках (статически) и живым проходом по экранам.
"""
import re
from pathlib import Path

import max_api
import pytest

import database as db
import repository as repo
from conftest import add_staff, press, register, say
from handlers import tickets
from utils import short_name

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


# ── длинные ФИО в кнопках ───────────────────────────────────────────────────
LONG_NAME = "Ковалевский Константин Юрьевич Петрович"   # 36 символов - 30 и больше
async def jump_ticket_numbers(to: int = 1233) -> None:
    """Довести счётчик обращений до четырёхзначных номеров.

    Подпись кнопки собирается из номера, статуса и пометки, поэтому 1234 -
    самый строгий случай: лишний знак съедает строку.
    """
    await db.run("DELETE FROM sqlite_sequence WHERE name='tickets'")
    await db.run("INSERT INTO sqlite_sequence(name, seq) VALUES('tickets', ?)", (to,))


def test_short_name_keeps_surname_and_initials():
    assert short_name("Ковалевский Константин Юрьевич") == "Ковалевский К. Ю."


def test_short_name_keeps_short_name_whole():
    """Короткое ФИО кнопка вмещает целиком - сокращать его незачем."""
    assert short_name("Соколова Мария") == "Соколова Мария"
    assert short_name("Петрова Анна") == "Петрова Анна"


def test_short_name_never_ends_with_dots():
    for name in (LONG_NAME, "Иванов", "Иванов Иван Иванович", "Ф" * 40, ""):
        label = short_name(name)
        assert not label.endswith("…"), f"многоточие в подписи: «{label}»"
        assert len(label) <= max_api.BUTTON_TEXT


def test_short_name_keeps_surname_even_when_it_alone_is_long():
    """Когда инициалы не влезают, остаётся узнаваемая фамилия, а не обрывок."""
    label = short_name("Ковалевский-Абрамович Константин", 16)
    assert label.startswith("Ковалевский")
    assert not label.endswith("…")


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
                    "nostaff", "syslist", "more", "broadcast"):
        await press(SYS, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


async def test_schedule_screens_have_short_labels(api):
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    for payload in ("sched", "teacher", "view_schedules", "groups"):
        await press(STUDENT, payload)
    for label in all_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"длинная подпись: «{label}»"


def fitted_labels(api) -> list[str]:
    """Подписи после подгонки под ширину ряда - ровно то, что рисует MAX.

    max_api.fit_keyboard режет подпись по числу кнопок в ряду (одна - 26 символов,
    две - 16, три - 14, четыре и больше - 12), поэтому проверять надо результат
    подгонки, а не то, что обработчик отдал в api.send.
    """
    out: list[str] = []
    for _to, _text, keyboard in api.sent:
        out += [b["text"] for row in max_api.fit_keyboard(keyboard or []) for b in row]
    return out


def assert_no_cut_labels(api, where: str = "") -> None:
    """Ни одна подпись на экране не обрезана и не длиннее предела MAX."""
    for label in fitted_labels(api):
        assert len(label) <= max_api.BUTTON_TEXT, f"{where}: длинная подпись «{label}»"
        assert not label.endswith("…"), f"{where}: обрезанная подпись «{label}»"


async def ticket_with_long_names() -> int:
    """Обращение №1234 к сотруднику с 36-символьным ФИО.

    Номер в четыре знака и длинное ФИО - самые частые причины обрезки подписи:
    «Ковалевский Константин Юрьевич…» и «№1234 · 📄 Готово к выдаче».
    """
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await add_staff(STAFF, LONG_NAME, category="all", position="Методист учебной части")
    await add_staff(DIRECTOR, LONG_NAME, category="all", position="Методист учебной части")
    await repo.set_admin_profile(DIRECTOR, role="director", position="Методист")
    await repo.add_template("Напоминание о сроках оплаты обучения", "Текст ответа", "feedback")
    await jump_ticket_numbers()
    await press(STUDENT, "sub:fb")
    await press(STUDENT, "fbrole:director")        # список людей должности
    await press(STUDENT, "new:certificates")        # список сотрудников раздела
    await press(STUDENT, "ask:certificates:place")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    return 1234


async def test_student_screens_have_no_cut_labels(api):
    """Экраны студента: списки сотрудников, обращения, сводка - без многоточия."""
    tid = await ticket_with_long_names()
    api.sent.clear()          # проверяем экраны, а не подготовку данных
    for payload in ("home", "sub:cert", "sub:acc", "sub:fb", "profile", "myall",
                    "tickets", "tickets:archive", "college", "faq", "sched",
                    f"t:{tid}"):
        await press(STUDENT, payload)
    assert_no_cut_labels(api, "студент")


async def test_staff_screens_have_no_cut_labels(api):
    """Экраны сотрудника: очередь, карточка, архив и шаблоны - без многоточия."""
    tid = await ticket_with_long_names()
    api.sent.clear()          # проверяем экраны, а не подготовку данных
    for payload in ("home", "staff", "staffv:waiting", "staffv:in_progress",
                    f"staff:{tid}", "staff:archive", f"t:{tid}", f"tpl:{tid}"):
        await press(STAFF, payload)
    assert_no_cut_labels(api, "сотрудник")


async def test_queue_button_with_four_digit_number(api):
    """«№1234 · 🔔 ждёт» и «№1234 · 🔧 В работе» влезают в строку кнопки MAX."""
    await ticket_with_long_names()
    api.sent.clear()          # проверяем экраны, а не подготовку данных
    await press(STAFF, "staff")
    assert "№1234 · 🔔 ждёт" in labels_of(api, STAFF)     # ждёт сотрудник
    await press(STAFF, "rp:1234")
    await say(STAFF, "Отвечаю")
    await press(STAFF, "staff")
    assert any(label.startswith("№1234 · ") and "Принято" in label
               for label in labels_of(api, STAFF))
    await press(STUDENT, "tickets")
    assert "№1234 · 📌 ждёте" in labels_of(api, STUDENT)   # ждёт студент
    assert_no_cut_labels(api, "очередь")


async def test_queue_rows_stay_short_with_long_statuses(env, api):
    """Даже самый длинный статус и номер в четыре знака не выталкивают многоточие."""
    rows = [{"ticket_id": 1234, "status": "completed"},
            {"ticket_id": 5678, "status": "in_progress"},
            {"ticket_id": 9999, "status": "ready"}]
    keyboard = await tickets.ticket_rows_kb(rows, staff_side=True)
    labels = [b["text"] for row in keyboard for b in row]
    assert "№1234 · ✅ Завершено" in labels
    for label in labels:
        assert len(label) <= max_api.BUTTON_TEXT
        assert not label.endswith("…")


async def test_subscription_button_is_short_and_explained(api):
    """«🔔 Подписка» влезает в кнопку, а смысл объяснён текстом над ней."""
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await repo.upsert_group("ИС-21", "Информационные системы")
    await repo.upsert_schedule("ИС-21", "https://college.example/is-21.pdf")
    await press(STUDENT, "view_schedules")
    assert "🔔 Подписка" in labels_of(api, STUDENT)
    assert "Подписка" in api.last(STUDENT)[1]
    await repo.set_schedule_subscription(STUDENT, "ИС-21")
    await press(STUDENT, "view_schedules")
    assert "🔕 Отписка" in labels_of(api, STUDENT)
    assert "Отписка" in api.last(STUDENT)[1]


async def test_labels_have_no_double_spaces(api):
    """Обрезка не должна оставлять висячие пробелы или обрыв слова."""
    await register(STUDENT)
    await press(STUDENT, "sub:cert")
    for label in labels_of(api, STUDENT):
        assert label == label.strip()
        assert "  " not in label
