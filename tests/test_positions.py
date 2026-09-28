"""Справочник должностей: нормализация, подсказки и сохранение в панели.

Список должностей лежит в utils (POSITIONS). Здесь проверяется поведение, а не
строки: синонимы должны сходиться к одному названию, подсказки в панели должны
покрывать весь справочник, а сохранённая должность - быть в каноническом виде.
Проверки бьют по результату - сводят одну и ту же должность, написанную по-
разному, и ждут одну и ту же строку, - поэтому сломанное правило валит тест.
"""
import pytest

import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, press, say
from utils import (POSITION_ALIASES, POSITION_CODES, POSITION_TITLES, POSITIONS_BTN,
                   group_by_position, has_position, norm_position, position_code, position_group,
                   position_label)

STAFF = "500"

# Написания одной и той же должности: раньше каждое становилось своим человеком.
SYNONYMS = (
    ("ПК", "admissions"),
    ("пк", "admissions"),
    ("Приёмная комиссия", "admissions"),
    ("приемная  комиссия", "admissions"),
    ("Уч. часть", "study_office"),
    ("уч.часть", "study_office"),
    ("УЧЕБНАЯ ЧАСТЬ", "study_office"),
    ("зам директора", "deputy"),
    ("ЗАМ  ДИРЕКТОРА", "deputy"),
    ("бухгалтер", "accounting"),
    ("главбух", "chief_accountant"),
    ("соцпед", "social_pedagogue"),
)


# ── нормализация ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed,code", SYNONYMS)
def test_synonyms_come_to_one_position(typed, code):
    """«ПК» и «Приёмная комиссия» - одна должность, а не две разных записи."""
    assert position_code(typed) == code
    assert norm_position(typed) == POSITION_TITLES[code]
    assert position_group(typed) == code


def test_registry_positions_are_known():
    """Каждая должность справочника узнаётся и по названию, и по коду."""
    for code in POSITION_CODES:
        assert position_code(POSITION_TITLES[code]) == code
        assert position_code(code) == code
        assert norm_position(code) == POSITION_TITLES[code]
        assert position_label(code) == POSITIONS_BTN[code]


def test_aliases_point_at_real_positions():
    """Синоним, который указывает в пустоту, - это опечатка, а не сокращение."""
    for alias, code in POSITION_ALIASES.items():
        assert code in POSITION_CODES, f"синоним «{alias}» ведёт в {code}"
        assert position_code(alias) == code, f"синоним «{alias}» не работает"


def test_case_and_spaces_do_not_matter():
    """Регистр и лишние пробелы не должны заводить новую должность."""
    for typed in ("  Секретарь  ", "секретарь", "СЕКРЕТАРЬ"):
        assert norm_position(typed) == "Секретарь"


def test_every_spelling_of_the_same_position_is_one_group():
    """Главное обещание: две записи, отличающиеся написанием, - одна группа."""
    rows = [
        {"user_id": "1", "position": "ПК", "role": ""},
        {"user_id": "2", "position": "приёмная  комиссия", "role": ""},
        {"user_id": "3", "position": "Приёмная комиссия", "role": "admissions"},
        {"user_id": "4", "position": "Преподаватель информатики", "role": ""},
    ]
    groups = group_by_position(rows)
    assert sorted(groups) == ["admissions"]
    assert {str(row["user_id"]) for row in groups["admissions"]} == {"1", "2", "3"}


def test_position_falls_back_to_role_when_own_text():
    """Своя должность без кода группируется по типу роли, а не заводит свой код."""
    assert position_group("Преподаватель информатики", "director") == "director"
    assert position_group("Преподаватель информатики", "") == ""
    assert position_group("", "") == ""


def test_own_position_is_kept_as_typed():
    """Должности вне справочника не выдумываем: она остаётся текстом."""
    assert norm_position("  преподаватель  математики ") == "Преподаватель математики"
    assert position_code("Преподаватель математики") == ""


@pytest.mark.parametrize("value", ["", None, "   ", "—", "-", "\n\t"])
def test_empty_position_gives_understandable_result(value):
    """Пустая должность - пустая строка и «раздела нет», а не исключение."""
    assert norm_position(value) == ""
    assert position_code(value) == ""
    assert position_group(value) == ""
    assert has_position(value) is False


# ── бот: подсказки из справочника ────────────────────────────────────────────
async def test_position_hints_cover_whole_registry(api):
    """Подсказки в карточке сотрудника - весь справочник, а не шесть строк."""
    await add_staff(STAFF, "Петрова Анна")
    await press("1", f"sfr:{STAFF}")
    payloads = set(api.payloads("1"))
    assert {f"sfph:{STAFF}:{code}" for code in POSITION_CODES} <= payloads
    text = " ".join(b["text"] for row in api.last("1")[2] for b in row)
    assert "Приёмная" in text and "Учебная" in text


async def test_position_hints_are_not_cut(api):
    """Подсказки стоят по две в ряду и не режутся: иначе все станут «👔 Дирек…»."""
    import max_api

    await add_staff(STAFF, "Петрова Анна")
    await press("1", f"sfr:{STAFF}")
    rows = max_api.fit_keyboard(api.last("1")[2] or [])
    assert rows, "подсказок нет"
    assert all(len(row) <= 2 for row in rows), [len(row) for row in rows]
    assert sum(len(row) for row in rows) == len(POSITION_CODES)
    for row in rows:
        for button in row:
            assert not button["text"].endswith("…"), button["text"]
            assert max_api.display_width(button["text"]) <= max_api.row_limit(len(row)), button["text"]


async def test_hint_button_saves_canonical_position(api):
    """Подсказка кладёт в базу название из справочника, а не выбранный код."""
    await add_staff(STAFF, "Петрова Анна")
    await press("1", f"sfph:{STAFF}:admissions")
    assert (await repo.get_admin(STAFF))["position"] == "Приёмная комиссия"
    assert "Приёмная комиссия" in api.last("1")[1]


async def test_typed_position_is_normalised(api):
    """Введённый руками синоним тоже сводится к канону - иначе группа распадётся."""
    await add_staff(STAFF, "Петрова Анна")
    await press("1", f"sfr:{STAFF}")
    await say("1", "  пк ")
    assert (await repo.get_admin(STAFF))["position"] == "Приёмная комиссия"


# ── панель ───────────────────────────────────────────────────────────────────
def staff_row(panel_client) -> str:
    """Только строка сотрудника из таблицы: сис-админ без должности стоит рядом."""
    body = panel_client.get("/panel/staff").text
    return next(part for part in body.split("<tr") if "Петрова Анна" in part)


@pytest.mark.panel
async def test_panel_hints_cover_whole_registry(panel_client):
    """Подсказки в панели есть у каждой должности справочника."""
    await add_staff(STAFF, "Петрова Анна", position="Секретарь")
    assert login_panel(panel_client)
    for path in ("/panel/staff", f"/panel/staff/{STAFF}"):
        body = panel_client.get(path).text
        missing = [title for title in POSITION_TITLES.values() if title not in body]
        assert not missing, f"{path}: нет подсказок {missing}"
        # подсказка - именно кнопка, которая ставит должность
        assert f'action="/panel/staff/{STAFF}/position"' in body
        assert 'name="position_pick"' in body


@pytest.mark.panel
async def test_panel_hint_button_saves_canonical_position(panel_client):
    """Выбрал должность кнопкой - в базе каноническое название."""
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    response = post_form(panel_client, f"/panel/staff/{STAFF}/position",
                         {"position_pick": POSITION_TITLES["study_office"]})
    assert response.status_code in (302, 303)
    assert (await repo.get_admin(STAFF))["position"] == "Учебная часть"


@pytest.mark.panel
async def test_panel_hint_button_without_choice_keeps_position(panel_client):
    """Кнопка подсказки без выбора - это не команда «стереть должность»."""
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/staff/{STAFF}/position", {})
    assert (await repo.get_admin(STAFF))["position"] == "Приёмная комиссия"


@pytest.mark.panel
async def test_panel_saves_synonym_as_canonical(panel_client):
    """Вписал «ПК» руками - сохранилось «Приёмная комиссия»."""
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/staff/{STAFF}", {"position": "  ПК "})
    assert (await repo.get_admin(STAFF))["position"] == "Приёмная комиссия"


@pytest.mark.panel
async def test_panel_keeps_own_position(panel_client):
    """Свою должность вписать можно - нормализация не должна её выкинуть."""
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/staff/{STAFF}", {"position": "Преподаватель информатики"})
    assert (await repo.get_admin(STAFF))["position"] == "Преподаватель информатики"


@pytest.mark.panel
async def test_empty_position_field_does_not_wipe_saved(panel_client):
    """Пустое поле - не команда «стереть должность», а «я ничего не поменял»."""
    await add_staff(STAFF, "Петрова Анна", position="Приёмная комиссия")
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/staff/{STAFF}",
              {"full_name": "Петрова Анна", "position": "", "department": "", "office": ""})
    assert (await repo.get_admin(STAFF))["position"] == "Приёмная комиссия"


@pytest.mark.panel
async def test_panel_marks_staff_without_position(panel_client):
    """Пробел в данных виден сразу, а не прячется в пустое «-»."""
    await add_staff(STAFF, "Петрова Анна")
    assert login_panel(panel_client)
    assert "должность не заполнена" in staff_row(panel_client)
    post_form(panel_client, f"/panel/staff/{STAFF}/position",
              {"position_pick": POSITION_TITLES["secretary"]})
    row = staff_row(panel_client)
    assert "должность не заполнена" not in row
    assert "Секретарь" in row


@pytest.mark.panel
async def test_position_gap_is_counted_in_data_gaps(panel_client):
    """«Пробелы в данных» считают сотрудника без должности - проверим на деле.

    Считается «без должности или роли»: сначала человек без обеих строк, потом с
    заполненными. Пробел виден панели и до, и после заполнения.
    """
    await add_staff(STAFF, "Петрова Анна")
    gaps = {row["key"]: row["count"] for row in await repo.data_gaps()}
    assert gaps["staff"] >= 1
    await db.run("UPDATE admins SET position=?, role=? WHERE user_id=?", ("ПК", "admissions", STAFF))
    after = {row["key"]: row["count"] for row in await repo.data_gaps()}
    assert after["staff"] == gaps["staff"] - 1
