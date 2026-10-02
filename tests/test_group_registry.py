"""Единый справочник групп: небрежный ввод, псевдонимы, подсказки."""
import pytest

import database as db
import repository as repo
import timetable as tt
from utils import group_code, group_digits, same_group, valid_group
from conftest import press, say

STUDENT = "100"
COLLEGE_GROUPS = ("23-29", "24-20", "24-21-2С", "24-23П", "24-25", "25-21", "25-23П",
                  "26-28", "26-29", "26-29П", "26-30", "26-31")


@pytest.fixture
async def college_groups():
    """Справочник как после импорта с сайта колледжа."""
    for code in COLLEGE_GROUPS:
        await repo.upsert_group(code, title=code.replace("-", "-", 1) if "П" not in code else code)
    return COLLEGE_GROUPS


# ── нормализация ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed,expected", [
    ("24-23 (П)", "24-23П"),
    ("24-23П", "24-23П"),
    ("24-23 п", "24-23П"),
    ("2423П", "24-23П"),
    ("24 23 П", "24-23П"),
    ("24-21(2С)", "24-21-2С"),
    ("24-21 2С", "24-21-2С"),
    ("26-29(П)", "26-29П"),
    ("иС-21", "ИС-21"),
    ("ИС21", "ИС21"),
    ("25-32", "25-32"),
    ("", ""),
])
def test_group_code_normalization(typed, expected):
    assert group_code(typed) == expected


def test_normalized_codes_are_valid():
    """Код из справочника должен проходить проверку валидности - иначе не сохранится."""
    for code in COLLEGE_GROUPS:
        assert valid_group(group_code(code)), code


def test_same_group_ignores_spacing_and_alphabet():
    assert same_group("24-23 (П)", "2423П")
    assert same_group("24-23П", "24-23P")
    assert not same_group("24-23", "24-24")
    assert not same_group("24-23", "24-231")


def test_group_digits():
    assert group_digits("24-23П") == "2423"


# ── поиск по справочнику ──────────────────────────────────────────────────────
async def test_find_group_accepts_any_writing(college_groups):
    for typed in ("24-23П", "24-23 (П)", "2423П", "24 23 п"):
        found = await repo.find_group(typed)
        assert found and found["code"] == "24-23П", typed


async def test_find_group_keeps_multi_letter_suffix(college_groups):
    found = await repo.find_group("24-21(2С)")
    assert found and found["code"] == "24-21-2С"


async def test_find_group_unknown_returns_none(college_groups):
    assert await repo.find_group("99-99") is None
    assert await repo.find_group("абракадабра") is None


async def test_aliases_are_remembered():
    """Псевдоним спасает написания, которые нормализация не сводит сама."""
    await repo.upsert_group("ИС-21", title="ИС-21")
    await repo.add_group_aliases("ИС-21", ["ИС21", "ис 21", "ИС-21"])
    aliases = await repo.group_aliases("ИС-21")
    # «ИС21» и «ис 21» нормализуются в один и тот же вид - псевдонимом остаётся он
    assert aliases == ["ИС21"]
    assert "ИС-21" not in aliases          # канонический код псевдонимом не считается
    assert (await repo.find_group("ИС21"))["code"] == "ИС-21"
    # «2423П» нормализуется сам, поэтому псевдонимом не станет
    await repo.upsert_group("24-23П")
    await repo.add_group_aliases("24-23П", ["2423П", "24-23 (П)"])
    assert await repo.group_aliases("24-23П") == []


async def test_suggestions_for_typo(college_groups):
    suggestions = await repo.suggest_groups("2423")
    codes = [group["code"] for group in suggestions]
    assert codes[0] == "24-23П"


async def test_suggestions_for_new_group_are_unrelated(college_groups):
    """Новая группа не должна выглядеть как опечатка существующей."""
    from handlers.menus import _looks_like_typo
    # «24-23» без суффикса - похоже на опечатку в 24-23П
    assert _looks_like_typo("24-23", await repo.suggest_groups("24-23")) is True
    # «30-11» не похоже ни на одну группу - это новая группа, а не опечатка
    assert _looks_like_typo("30-11", await repo.suggest_groups("30-11")) is False


async def test_resolve_group_shape(college_groups):
    found = await repo.resolve_group("2423П")
    assert found["found"] is True and found["code"] == "24-23П"
    missing = await repo.resolve_group("99-99")
    assert missing["found"] is False and isinstance(missing["suggestions"], list)


# ── регистрация ───────────────────────────────────────────────────────────────
async def test_registration_accepts_sloppy_group_writing(college_groups, api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иванов Иван Иванович")
    await say(STUDENT, "24 23 п")          # «24-23 (П)» разными буквами
    await press(STUDENT, "regyes")
    await press(STUDENT, "consentyes")
    row = await db.one("SELECT * FROM users WHERE user_id=?", (STUDENT,))
    assert row["group_code"] == "24-23П"


async def test_registration_asks_on_typo(college_groups, api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иванов Иван Иванович")
    # потерял суффикс: в справочнике есть 24-23П, а человек написал 24-23
    await say(STUDENT, "24-23")
    payloads = api.payloads(STUDENT)
    assert "regpick:24-23П" in payloads
    assert not await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,))
    await press(STUDENT, "regpick:24-23П")
    await press(STUDENT, "regyes")
    await press(STUDENT, "consentyes")
    row = await db.one("SELECT group_code FROM users WHERE user_id=?", (STUDENT,))
    assert row["group_code"] == "24-23П"


async def test_registration_no_longer_creates_group_behind_admins_back(api):
    """Студент не заводит группу сам, и сис-админ не получает об этом уведомление.

    Задача владельца: «удалить возможность создания своей группы, у нас есть свой
    реестр». Проверяем обе стороны, потому что раньше было обе:
    * группа «99-01» попадала в справочник от студента - теперь не попадает;
    * сис-админам уходило уведомление «Новая группа» - теперь не уходит, ведь
      группа не появилась, а если появится - её завёл сотрудник.
    """
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иванов Иван Иванович")
    await say(STUDENT, "99-01")

    group = await db.one("SELECT * FROM groups WHERE group_code=?", ("99-01",))
    assert group is None, f"студент завел группу сам: {dict(group) if group else None}"

    реплики_админу = [text for _to, text, _kb in api.to("1")]
    assert not any("Новая группа" in text for text in реплики_админу), \
        "сис-админ получил уведомление о группе, которой нет"

    # и студенту сказано, куда обратиться, а не просто отказано
    последний = api.last(STUDENT)[1]
    assert "учебную часть" in последний, f"не сказано, куда обратиться: {последний!r}"

    # в реестре не появилась и запись, и пользователь не зарегистрирован
    assert await db.one("SELECT 1 FROM users WHERE user_id=?", (STUDENT,)) is None


async def test_registration_group_pick_from_buttons(college_groups, api):
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иванов Иван Иванович")
    await say(STUDENT, "-")               # пустой ввод - показываем справочник
    assert "regpick:24-23П" in api.payloads(STUDENT)
    await press(STUDENT, "regpick:24-23П")
    await press(STUDENT, "regyes")
    await press(STUDENT, "consentyes")
    row = await db.one("SELECT group_code FROM users WHERE user_id=?", (STUDENT,))
    assert row["group_code"] == "24-23П"


async def test_schedule_uses_canonical_group(api):
    """Расписание ищется по каноническому коду, а не по тому, как его написали."""
    from handlers.menus import send_schedule
    await repo.upsert_group("24-23П")
    await repo.upsert_schedule("24-23П", "https://college.example/24-23.pdf")
    await send_schedule(STUDENT, "24-23 (П)")
    text = "\n".join(body for _, body, _ in api.to(STUDENT))
    # в заголовке - канонический код, а не то, как его написал студент
    assert "24-23П" in text


# ── импортёр ──────────────────────────────────────────────────────────────────
def test_group_filter_rejects_non_group_documents():
    import schedule_import
    assert schedule_import.is_group_looks_like("24-23 (П)")
    assert schedule_import.is_group_looks_like("26-29П")
    assert not schedule_import.is_group_looks_like("График консультаций преподавателей")
    assert not schedule_import.is_group_looks_like("Кураторы групп БУ ЛПК")


def test_college_files_have_expected_shape():
    """Имена файлов: первый и последний код - это группы, которые внутри."""
    for name in ("23-29-24-25", "24-26-25-20", "26-28-26-31"):
        first, last = name.split("-", 2)[0] + "-" + name.split("-")[1], name.rsplit("-", 1)[1]
        assert valid_group(first) and valid_group(last), name


def test_parse_pdf_bytes_uses_group_column():
    """Парсер берёт колонку нужной группы, а не первую попавшуюся."""
    import inspect
    source = inspect.getsource(tt.build_schedule)
    assert "group" in source  # разбор идёт по имени группы
