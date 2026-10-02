"""Согласие на обработку данных при регистрации."""
import database as db
import repository as repo
from conftest import press, register, say

STUDENT = "300"


async def register_without_consent(group="24-23"):
    """Регистрация доходит до подтверждения — согласие ещё не спрашивали.

    Группа заранее заводится в справочник — так же, как это делает сис-админ на
    вкладке «Группы». Раньше это было не нужно: неизвестный код молча создавал
    группу при регистрации, а теперь не создаётся, и проверки файла падали бы на
    том, что регистрация не доходит до подтверждения.
    """
    await repo.upsert_group(group, title=group)
    await say(STUDENT, "/start")
    await press(STUDENT, "who:student")
    await say(STUDENT, "Иванов Иван Иванович")
    await say(STUDENT, group)
    await press(STUDENT, "regyes")


# ── согласие спрашивается ───────────────────────────────────────────────────
async def test_registration_asks_for_consent(api):
    await register_without_consent()
    body = "\n".join(text for _, text, _ in api.to(STUDENT))
    assert "Согласие на обработку данных" in body
    assert "consentyes" in api.payloads(STUDENT)
    assert "consentno" in api.payloads(STUDENT)


async def test_without_consent_data_is_not_saved():
    await register_without_consent()
    assert await repo.get_user(STUDENT) is None


async def test_consent_saves_registration_with_date():
    await register_without_consent()
    await press(STUDENT, "consentyes")
    user = await repo.get_user(STUDENT)
    assert user and user["group_code"] == "24-23"
    consent = await repo.consent_of(STUDENT)
    assert consent["at"], "дата согласия должна сохраняться"
    assert consent["version"]


async def test_refusal_keeps_nothing_and_explains(api):
    await register_without_consent()
    await press(STUDENT, "consentno")
    assert await repo.get_user(STUDENT) is None
    assert "без согласия" in api.to(STUDENT)[-1][1]
    assert await db.get_state(STUDENT) is None


async def test_consent_asked_only_once():
    """Смена группы согласия не спрашивает - человек его уже давал.

    Раньше проверка опиралась на экран подтверждения несуществующей группы.
    Экрана больше нет: группу нельзя завести, поэтому и подтверждать нечего.
    Смысл проверки сохранён на рабочем пути - меняем группу на другую из
    справочника.
    """
    await repo.upsert_group("25-27", title="Другая группа")
    await register_without_consent()
    await press(STUDENT, "consentyes")
    assert (await repo.get_user(STUDENT))["group_code"] == "24-23"

    # смена группы уже зарегистрированным человеком согласия не спрашивает
    await press(STUDENT, "savegrp:25-27")

    человек = await repo.get_user(STUDENT)
    assert человек["group_code"] == "25-27", "группа не сменилась"
    согласие = await repo.consent_of(STUDENT)
    assert согласие["at"], "согласие потерялось при смене группы"


async def test_consent_text_is_editable_from_settings(api):
    await db.set_setting("consent_text", "Своя редакция согласия для ЛПК")
    await register_without_consent()
    body = "\n".join(text for _, text, _ in api.to(STUDENT))
    assert "Своя редакция согласия" in body


async def test_consent_no_saves_nothing_even_after_back():
    await register_without_consent()
    await press(STUDENT, "home")           # ушёл в меню, не ответив
    assert await repo.get_user(STUDENT) is None
    assert (await repo.consent_of(STUDENT))["at"] == ""


# ── список без согласия ─────────────────────────────────────────────────────
async def test_users_without_consent_listed():
    """Список для панели: кто зарегистрирован, но согласия не давал."""
    await repo.upsert_user(STUDENT, "Иванов Иван Иванович", "24-23")
    await register("301", "Петров Пётр Петрович", "24-24")
    rows = await repo.users_without_consent()
    assert [row["user_id"] for row in rows] == [STUDENT]
    assert rows[0]["group_code"] == "24-23"


async def test_users_with_consent_not_in_list():
    await register_without_consent()
    await press(STUDENT, "consentyes")
    assert await repo.users_without_consent() == []
