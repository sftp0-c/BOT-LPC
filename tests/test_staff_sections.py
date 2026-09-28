"""Свой раздел сотрудника: он есть у человека с должностью и честно объясняет
своё отсутствие у человека без неё.

Проверки бьют по поведению: раздел открывается кнопкой из кабинета, показывает
счётчики по статусам, шаблоны своего раздела и кабинет, а без должности вместо
пустого подменю бот прямо говорит, что её заполнить нечем. Отдельно checked,
что две записи, отличающиеся только написанием должности, попадают в одно
подменю «Обратная связь».
"""
import pytest

import repository as repo
from conftest import add_staff, press, register, say
from utils import POSITION_TITLES, STATUS

STUDENT, STAFF, COLLEAGUE = "100", "200", "201"


def labels(api, user: str) -> list:
    return [b["text"] for row in (api.last(user)[2] or []) for b in row]


async def ticket_from_student(staff: str, category: str = "feedback") -> int:
    await press(STUDENT, f"new:{category}")
    await press(STUDENT, f"pick:{category}:{staff}")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    return (await repo.get_ticket(
        (await repo.admin_tickets(None, 50))[0]["ticket_id"]))["ticket_id"]


# ── раздел есть у человека с должностью ──────────────────────────────────────
async def test_staff_home_has_section_button(api):
    """В кабинете сотрудника появляется кнопка его раздела."""
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия", office="214")
    await press(STAFF, "home")
    assert "mysection" in api.payloads(STAFF)


async def test_section_shows_position_counters_room_and_templates(api, clear_templates):
    """Раздел отвечает на четыре вопроса сразу: чем занят, где кабинет, что за
    раздел обращений и какими шаблонами отвечать."""
    await register(STUDENT)
    await repo.add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия", office="214",
                         ticket_category="feedback")
    await repo.add_template("Справка для поступления", "Текст справки", "feedback")
    await ticket_from_student(STAFF)
    await press(STAFF, "mysection")

    text = api.last(STAFF)[1]
    assert "Приёмная комиссия" in text
    assert "214" in text
    assert "Справка для поступления" in text
    assert "Обратная связь" in text        # раздел обращений сотрудника
    # счётчики по статусам: есть новое обращение, значит «Новое — 1»
    for code in ("new", "accepted", "in_progress"):
        assert f"{STATUS[code]} — {1 if code == 'new' else 0}" in text
    payloads = set(api.payloads(STAFF))
    assert {"staff", "home", "staffstats"} <= payloads
    assert any(p.startswith("mystpl:") for p in payloads), payloads
    assert "stafff:feedback" in payloads          # прыжок в очередь своего раздела


async def test_section_lists_colleagues_of_same_position(api):
    """В разделе видно, кто ещё работает в этой должности и где он сидит."""
    await add_staff(STAFF, "Петрова Анна", position="Учебная часть", office="214")
    await add_staff(COLLEAGUE, "Сидоров Пётр", position="Учебная часть", office="215")
    await press(STAFF, "mysection")
    text = api.last(STAFF)[1]
    assert "Сидоров Пётр" in text and "215" in text


async def test_section_template_shows_text_and_where_to_use_it(api, clear_templates):
    """Шаблон из раздела открывается и объясняет, куда его вставлять."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    template_id = await repo.add_template("Справка для поступления", "Выдаём справку", "feedback")
    await ticket_from_student(STAFF)
    await press(STAFF, "mysection")
    await press(STAFF, f"mystpl:{template_id}")
    text = api.last(STAFF)[1]
    assert "Выдаём справку" in text
    assert "Шаблоны" in text


# ── без должности раздела нет, и бот это говорит ─────────────────────────────
async def test_no_section_button_without_position(api):
    """Без должности кнопки раздела нет: пустая кнопка - это враньё."""
    await add_staff(STAFF, "Петрова Анна")
    await press(STAFF, "home")
    assert "mysection" not in api.payloads(STAFF)
    assert "Должность не заполнена" in api.last(STAFF)[1]


async def test_section_asks_to_fill_position_instead_of_empty_menu(api):
    """Зашёл в раздел без должности - получил объяснение, а не пустой список."""
    await add_staff(STAFF, "Петрова Анна")
    await press(STAFF, "mysection")
    text = api.last(STAFF)[1]
    assert "Должность не заполнена" in text
    assert "сис-админ" in text
    # и ничего похожего на пустое подменю: ни счётчиков, ни шаблонов
    assert "Очередь:" not in text
    assert not [p for p in api.payloads(STAFF) if p.startswith("mystpl:")]


# ── две записи, отличающиеся написанием, - одно подменю ──────────────────────
async def test_two_spellings_land_in_one_feedback_submenu(api):
    """«ПК» и «Приёмная комиссия» - один человек в одном подменю, а не два."""
    await register(STUDENT)
    await repo.add_staff(STAFF, "Петрова Анна", position="ПК")
    await repo.add_staff(COLLEAGUE, "Сидоров Пётр", position="Приёмная комиссия")
    await press(STUDENT, "sub:fb")
    payloads = api.payloads(STUDENT)
    assert "fbrole:admissions" in payloads
    assert len([p for p in payloads if p.startswith("fbrole:")]) == 1
    await press(STUDENT, "fbrole:admissions")
    people = set(api.payloads(STUDENT))
    assert {f"pick:feedback:{STAFF}", f"pick:feedback:{COLLEAGUE}"} <= people


async def test_staff_without_position_is_not_in_feedback_submenu(api):
    """Сотрудник без должности не занимает чужое подменю."""
    await register(STUDENT)
    await repo.add_staff(STAFF, "Петрова Анна")
    await press(STUDENT, "sub:fb")
    assert not [p for p in api.payloads(STUDENT) if p.startswith("fbrole:")]
    assert "не назначены" in api.last(STUDENT)[1]


async def test_position_registry_drives_feedback_submenu(api):
    """Подменю строится по справочнику: назначили бухгалтера - есть кнопка."""
    await register(STUDENT)
    await repo.add_staff(STAFF, "Петрова Анна", position="Главный бухгалтер")
    await press(STUDENT, "sub:fb")
    code = "chief_accountant"
    assert f"fbrole:{code}" in api.payloads(STUDENT)
    assert POSITION_TITLES[code] == "Главный бухгалтер"
    assert "Петрова Анна" in api.last(STUDENT)[1]


# ── экран не должен обрезать подписи ─────────────────────────────────────────
async def test_section_labels_fit_max(api):
    """Длинные должности и ФИО не превращают кнопки в «Должность не…»."""
    import max_api

    await register(STUDENT)
    await repo.add_staff(STAFF, "Ковалевский Константин Юрьевич",
                        position="Методист учебно-производственного отдела факультета")
    await repo.add_staff(COLLEAGUE, "Соколова Мария Сергеевна", position="Соколова Мария Сергеевна",
                         office="кабинет 204")
    await repo.add_template("Напоминание о сроках оплаты обучения", "Текст", "feedback")
    before = len(api.sent)
    await press(STAFF, "mysection")
    for _to, text, keyboard in api.sent[before:]:
        for row in max_api.fit_keyboard(keyboard or []):
            limit = max_api.row_limit(len(row))
            for button in row:
                assert max_api.display_width(button["text"]) <= limit, (
                    f"{button['text']!r} длиннее предела {limit} на экране {text[:40]!r}")
                assert not button["text"].endswith("…"), (button["text"], text[:40])


@pytest.mark.parametrize("payload", ["mysection", "mystpl:999"])
async def test_section_survives_bad_input(api, payload):
    """Мусор в payload не роняет бота."""
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    await press(STAFF, payload)
    assert api.to(STAFF)


async def test_student_cannot_open_section(api):
    """Раздел сотрудника студенту не открывается."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    api.sent.clear()
    await press(STUDENT, "mysection")
    assert not api.to(STUDENT)


# ── а раздел действительно привязан к должности ──────────────────────────────
async def test_section_counts_only_own_tickets(api):
    """Счётчики раздела - это его собственные обращения, а не чужие."""
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    await add_staff(COLLEAGUE, "Сидоров Пётр", position="Учебная часть")
    await ticket_from_student(STAFF)
    await ticket_from_student(COLLEAGUE)
    await press(STAFF, "mysection")
    text = api.last(STAFF)[1]
    assert f"{STATUS['new']} — 1" in text
