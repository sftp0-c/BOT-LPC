"""Фильтр по группе падал, и честные страницы требовали offset.

`LEFT JOIN contacts c ON c.user_id=u.user_id{where}` - между JOIN и WHERE не
было пробела, поэтому любой `/panel/students?group=…` возвращал
«near "u": syntax error». Панель это обошла отбором на Python, но поломка была
в репозитории, и в любом другом месте вылезла бы снова.

Здесь проверяется то, что чинили: фильтр работает, страницы не пересекаются
и не теряют строки, а правило подбора заместителя одно и то же поштучное и
массовое.
"""
import pytest

import repository as repo

GROUP_A = "24-23П"        # канонический вид, как пишет бот
GROUP_B = "24-25"


@pytest.fixture
async def студенты(env):
    for index in range(5):
        await repo.upsert_user(f"4{index:03d}01", f"Студент {index} Тестов",
                               GROUP_A if index % 2 == 0 else GROUP_B)
    return True


async def test_фильтр_по_группе_не_падает(env, студенты):
    """Главное: раньше здесь был sqlite3.OperationalError."""
    rows = await repo.list_users(group_code=GROUP_A)
    assert rows, "фильтр по группе вернул пусто"
    assert all(row["group_code"] == GROUP_A for row in rows)


async def test_фильтр_ловит_код_со_скобками(env):
    """Та самая поломка: «24-23 (П)» записывается со скобками, а искалось без.

    Обе формы кода встречаются в данных, поэтому фильтр обязан ловить и ту,
    и другую - иначе группа, записанная как «24-23 (П)», не находится никогда.
    """
    await repo.upsert_user("40001", "Скобки Тест", "24-23 (П)")
    со_скобками = await repo.list_users(group_code="24-23 (П)")
    без_скобок = await repo.list_users(group_code="24-23П")
    assert со_скобками, "фильтр не нашёл группу, записанную со скобками"
    assert без_скобок, "фильтр не нашёл ту же группу в каноническом виде"
    assert со_скобками[0]["user_id"] == без_скобок[0]["user_id"]
    assert await repo.list_users_count("24-23 (П)") == 1


async def test_фильтр_по_группе_без_склейки(env, студенты):
    """Пустое значение и неизвестная группа не дают исключения."""
    assert await repo.list_users(group_code="") == await repo.list_users()
    assert await repo.list_users(group_code="НЕТ-ТАКОЙ") == []


async def test_счётчик_учитывает_фильтр(env, студенты):
    всего = await repo.list_users_count()
    по_группе = await repo.list_users_count(GROUP_A)
    assert всего == 5
    assert по_группе == 3
    assert по_группе < всего


async def test_offset_даёт_честные_страницы(env, студенты):
    первая = await repo.list_users(limit=2, offset=0)
    вторая = await repo.list_users(limit=2, offset=2)
    assert len(первая) == 2 and len(вторая) == 2
    assert not ({r["user_id"] for r in первая} & {r["user_id"] for r in вторая}), \
        "страницы пересеклись"
    хвост = await repo.list_users(limit=2, offset=4)
    assert len(хвост) == 1, "хвост потерян"
    всего = {r["user_id"] for r in первая + вторая + хвост}
    assert len(всего) == 5, "постраничный обход потерял строку"


async def test_offset_с_фильтром(env, студенты):
    """Страницы внутри фильтра тоже не пересекаются."""
    первая = await repo.list_users(limit=1, offset=0, group_code=GROUP_A)
    вторая = await repo.list_users(limit=1, offset=1, group_code=GROUP_A)
    assert первая and вторая
    assert первая[0]["user_id"] != вторая[0]["user_id"]


async def test_offset_не_отрицательный(env, студенты):
    rows = await repo.list_users(limit=2, offset=-5)
    assert rows, "отрицательный offset должен вести себя как нулевой"


# ── правило заместителя ──────────────────────────────────────────────────────
async def test_правило_заместителя_совпадает_с_поштучным(env):
    from conftest import add_staff
    await add_staff("200", "Петрова Мария", position="Методист")
    await add_staff("201", "Соколова Анна", position="Методист")
    await add_staff("202", "Иванов Пётр", position="Кассир")
    # из бази приходит sqlite3.Row, у него нет .get - оборачиваем в словари
    rows = [dict(row) for row in await repo.list_staff()]
    away = set()
    for row in rows:
        поштучно = await repo.vacation_replacement(row)
        массово = repo.replacement_rule(row, rows, away)
        поштучно = dict(поштучно) if поштучно is not None else None
        массово = dict(массово) if массово is not None else None
        assert (поштучно or {}).get("user_id") == (массово or {}).get("user_id"), \
            f"правила разошлись для {row['full_name']}"


async def test_правило_заместителя_пропускает_отпускников(env):
    from conftest import add_staff
    await add_staff("200", "Петрова Мария", position="Методист")
    await add_staff("201", "Соколова Анна", position="Методист")
    rows = [dict(row) for row in await repo.list_staff()]
    цель = [r for r in rows if r["user_id"] == "200"][0]
    away = {"201"}
    замена = repo.replacement_rule(цель, rows, away)
    assert замена is None or dict(замена)["user_id"] != "201", \
        "отпускник не должен назначаться заместителем"
