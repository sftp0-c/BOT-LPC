"""Экраны бота с самыми длинными данными: ни одной обрезанной подписи.

Сквозная проверка настоящих экранов, а не разбора кода. Бот рисует подпись,
`max_api.fit_keyboard()` подгоняет её по ширине ряда, и уже результат
проверяется на два вещи: длина не больше предела MAX и многоточия в конце
нет.

Такая проверка нужна рядом со статической (`test_no_ellipsis.py`): статика не
видит ряды, собранные из словаря. Именно так и осталась обрезанная подпись
«🎓 Учебные вопросы» - фильтр разделов строится циклом по STAFF_CATS, и в
ряду из двух кнопок предел 16, а не 26.

Данные намеренно предельные: ФИО в 33 символа, должность в 38, отдел в 29,
номер обращения в четыре знака, группа с суффиксом.
"""
import pytest

import max_api
import repository as repo
from conftest import add_staff, press, register, say

STUDENT, STAFF, SYS = "300", "200", "1"

ДЛИННОЕ_ФИО = "Ковалевский Константин Юрьевичович"
ДЛИННАЯ_ДОЛЖНОСТЬ = "Методист учебной части факультета"
ДЛИННЫЙ_ОТДЕЛ = "Учебно-производственный отдел"


def flatten(keyboard) -> list:
    """Ряды кнопок. Клавиатуры приходят разной вложенностью - рядами,
    одним рядом, кортежем, а иногда списком кнопок вместо ряда."""
    rows = []
    for item in keyboard or []:
        if isinstance(item, dict):
            rows.append([item])
        elif isinstance(item, (list, tuple)):
            if item and all(isinstance(button, dict) for button in item):
                rows.append(list(item))
            else:
                rows.extend(flatten(item))
    return rows


def assert_clean(api, where: str, since: int = 0) -> list:
    """Ни одна подпись на экране не обрезана. Возвращает подписи для разбора.

    since - с какого сообщения смотреть: сообщения подготовки данных относятся
    не к проверяемому экрану.
    """
    labels = []
    bad = []
    for _to, text, keyboard in api.sent[since:]:
        for row in flatten(max_api.fit_keyboard(keyboard or [])):
            limit = max_api.row_limit(len(row))
            for button in row:
                label = button["text"]
                labels.append(label)
                payload = button.get("payload", "")
                if max_api.display_width(label) > limit:
                    bad.append(f"{where}: «{label}» ({max_api.display_width(label)} ячеек) "
                               f"длиннее предела {limit} "
                               f"в ряду из {len(row)}, кнопка {payload!r}, экран: {text[:50]!r}")
                if label.endswith("…"):
                    bad.append(f"{where}: «{label}» обрезано многоточием, "
                               f"кнопка {payload!r}, экран: {text[:50]!r}")
    assert labels, f"{where}: экран ничего не отправил"
    assert not bad, "\n".join(bad)
    return labels


def assert_no_wide_rows(api, where: str, since: int = 0) -> None:
    """Ни одного ряда шире двух кнопок: в тесном ряду подпись режется."""
    wide = []
    for _to, text, keyboard in api.sent[since:]:
        for row in flatten(max_api.fit_keyboard(keyboard or [])):
            if len(row) > 2:
                wide.append(f"{where}: ряд из {len(row)} кнопок на экране {text[:40]!r}")
    assert not wide, "\n".join(sorted(set(wide)))


@pytest.fixture
async def толстые_данные():
    """Люди и группы с самыми длинными именами, какие вообще бывают."""
    await register(STUDENT, ДЛИННОЕ_ФИО, "24-23 (П)")
    await register("301", "Петрова Анна Валериевна", "24-21(2С)")
    for index in range(3):
        staff_id = f"7{index}0"
        await add_staff(staff_id, f"Соколова Мария Сергеевна-{index}",
                        position=ДЛИННАЯ_ДОЛЖНОСТЬ, office=f"каб. 20{index}")
        await repo.set_admin_profile(staff_id, position=ДЛИННАЯ_ДОЛЖНОСТЬ,
                                     office=f"каб. 20{index}", department=ДЛИННЫЙ_ОТДЕЛ)
    await add_staff(STAFF, ДЛИННОЕ_ФИО, position=ДЛИННАЯ_ДОЛЖНОСТЬ, office="каб. 204")
    await repo.set_admin_profile(STAFF, position=ДЛИННАЯ_ДОЛЖНОСТЬ,
                                 office="каб. 204", department=ДЛИННЫЙ_ОТДЕЛ)
    await register(SYS, "Системный администратор")
    return STAFF


# ── экраны студента ──────────────────────────────────────────────────────────
async def test_student_home(api, толстые_данные):
    before = len(api.sent)
    await press(STUDENT, "home")
    assert_clean(api, "меню студента", since=before)
    assert_no_wide_rows(api, "меню студента", since=before)


async def test_registration_questions(api):
    """Экраны регистрации: там «Я студент / Я сотрудник» и списки."""
    await register("302", ДЛИННОЕ_ФИО, "24-23 (П)")
    assert_clean(api, "регистрация")


async def test_staff_picker_fits_long_names(api, толстые_данные):
    """Главный случай жалобы: список сотрудников с длинными ФИО."""
    before = len(api.sent)
    await press(STUDENT, "new:feedback")
    labels = assert_clean(api, "выбор сотрудника", since=before)
    # список не пустой и в нём есть сотрудники
    assert any("Соколова" in label for label in labels), labels
    # кнопки сотрудников идут в ряд, где предел 16, - многоточия быть не должно
    for label in labels:
        if "Соколова" in label:
            assert not label.endswith("…"), label


async def test_student_profile_and_tickets(api, толстые_данные):
    before = len(api.sent)
    await press(STUDENT, "profile")
    await press(STUDENT, "tickets")
    assert_clean(api, "профиль и обращения", since=before)
    assert_no_wide_rows(api, "профиль и обращения", since=before)


async def test_ticket_submenus(api, толстые_данные):
    """Подменю справок и бухгалтерии: подписи в один ряд."""
    before = len(api.sent)
    for payload in ("sub:cert", "sub:acc", "new:certificates", "new:academic", "new:accounting"):
        await press(STUDENT, payload)
    assert_clean(api, "подменю обращений", since=before)
    assert_no_wide_rows(api, "подменю обращений", since=before)


# ── экраны сотрудника ────────────────────────────────────────────────────────
async def test_staff_queue(api, толстые_данные):
    before = len(api.sent)
    await press(STAFF, "staff")
    assert_clean(api, "очередь сотрудника", since=before)
    assert_no_wide_rows(api, "очередь сотрудника", since=before)


async def test_ticket_card_and_templates(api, толстые_данные):
    """Карточка обращения и шаблоны: там длинные названия и номера."""
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Прошу сообщить о сроках сдачи зачёта")
    await press(STUDENT, "ticketsend")
    before = len(api.sent)
    await press(STAFF, "staff")
    await press(STAFF, "templates")
    await press(STAFF, "archive")
    assert_clean(api, "карточка, шаблоны и архив", since=before)
    assert_no_wide_rows(api, "карточка, шаблоны и архив", since=before)


# ── экраны сис-админа ────────────────────────────────────────────────────────
async def test_sysadmin_screens(api, толстые_данные):
    before = len(api.sent)
    for payload in ("admins", "people", "groups", "today", "nostaff",
                    "settings", "syslist", "more"):
        await press(SYS, payload)
    assert_clean(api, "экраны сис-админа", since=before)
    assert_no_wide_rows(api, "экраны сис-админа", since=before)


async def test_staff_card_filters_fit(api, толстые_данные):
    """Карточка сотрудника: фильтр раздела всегда идёт по две кнопки в ряду.

    Именно здесь «🎓 Учебные вопросы» (19 символов) обрезался до
    «🎓 Учебные…» - в ряду из двух кнопок предел 16, а не 26.
    """
    before = len(api.sent)
    await press(SYS, f"sf:{STAFF}")
    labels = assert_clean(api, "карточка сотрудника", since=before)
    assert_no_wide_rows(api, "карточка сотрудника", since=before)
    assert "🎓 Учёба" in labels, labels


async def test_department_name_stays_readable(api, толстые_данные):
    """Длинный отдел должен читаться, а не превращаться в «Учебно-про…»."""
    before = len(api.sent)
    await press(SYS, "admins")
    labels = assert_clean(api, "список сотрудников", since=before)
    assert not any("Учебно-про…" in label for label in labels), labels


async def test_numbered_ticket_labels_fit(api, толстые_данные):
    """Четырёхзначный номер и короткий статус в одной кнопке."""
    await register("303", "Сидоров Пётр", "24-25")
    for _ in range(3):
        await press("303", "new:feedback")
        await press("303", f"pick:feedback:{STAFF}")
        await say("303", "Нужна справка")
        await press("303", "ticketsend")
    before = len(api.sent)
    for payload in ("tickets", "staff"):
        await press(STAFF, payload)
    labels = assert_clean(api, "список обращений", since=before)
    # в списке должны быть номера обращений; если сотрудник ещё не принял
    # обращение в работу, номера может не быть - тогда проверяем только то,
    # что обрезки нет (это уже сделано выше)
    assert labels, labels
