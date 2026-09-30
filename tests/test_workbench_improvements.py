"""Два улучшения рабочего места: честная выгрузка и счётчик «дольше суток».

1. ВЫГРУЗКА В CSV. Обработчик знал только статус и раздел: поиск по тексту,
   «только ждут ответа» и архив он не принимал, а из архива отдавал пустоту.
   То есть выгрузка молча расходилась с тем, что человек видит на экране.
   Правила фильтрации вынесены в общую функцию, её зовут и страница, и
   выгрузка - иначе они снова разойдутся.

2. ДОЛЬШЕ СУТОК. Был счётчик «только ждут ответа», но не сказано, как давно.
   Добавлен счётчик и фильтр «дольше суток». Сутки - не просрочка по
   регламенту, а порог, за которым письмо забывают.
"""
import re

import pytest

import clock
import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, register
from web.tickets import OVERDUE_HOURS, filter_tickets, is_overdue

pytestmark = pytest.mark.panel

OWNER = "46010397"
STUDENT, STAFF = "100", "200"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


async def make_ticket(text: str = "Нужна справка", status: str = "new", topic: str = "") -> int:
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await add_staff(STAFF, "Петрова Анна", "all", office="215")
    tid = await repo.create_ticket(STUDENT, STAFF, "feedback", text, topic)
    # статус не передаётся в create_ticket - ставим отдельно, как это делает
    # сотрудник в панели. Иначе тест проверял бы несуществующие закрытые дела
    if status != "new":
        await repo.set_ticket_status(tid, status)
    return tid


async def answer(tid: int) -> None:
    """Сотрудник ответил - теперь ждёт студент."""
    await repo.add_ticket_message(tid, STAFF, "staff", "Ответили.")


async def touch(tid: int, hours: float) -> None:
    """Отматываем обращение назад: столько часов назад его последний раз трогали."""
    # stamp_at принимает МИНУТЫ (минус - назад), а не дату
    moment = clock.stamp_at(-hours * 60)
    await db.run("UPDATE tickets SET updated_at=? WHERE ticket_id=?", (moment, tid))


# ── 1. выгрузка respect'ит фильтры ─────────────────────────────────────────
async def test_csv_export_follows_the_search(owner_client, env):
    """Поиск в панели и в выгрузке должен давать одно и то же."""
    wanted = await make_ticket("Справка для поступления", topic="Поступление")
    await make_ticket("Совсем другое обращение про столовую")

    page = owner_client.get("/panel/tickets?q=поступлени").text
    assert f"№{wanted}" in page

    body = owner_client.get("/panel/tickets.csv?q=поступлени").text
    # разделитель в выгрузке - «;», поэтому ищем строку по номеру обращения
    assert f"{wanted};" in body, "выгрузка не нашла найденное"
    assert "столовую" not in body, "выгрузка протащила лишнее"


async def test_csv_export_of_the_archive_is_not_empty(owner_client, env):
    """Раньше из архива выгрузка была пустой - там вызывалось admin_tickets без archived."""
    tid = await make_ticket("В архиве")
    await repo.archive_ticket(tid, OWNER)
    assert owner_client.get("/panel/tickets?view=archive").text.count("wb-item") >= 1

    body = owner_client.get("/panel/tickets.csv?view=archive").text
    assert "В архиве" in body, "архив выгружается пустым - это та же ошибка"


async def test_csv_export_link_carries_current_filters(owner_client, env):
    """Ссылка «Выгрузить в CSV» несёт текущие фильтры, иначе нажатие снова врёт."""
    await make_ticket("Первое")
    await make_ticket("Второе")
    body = owner_client.get("/panel/tickets?status=new&category=feedback").text
    assert "/panel/tickets.csv?status=new" in body, "ссылка выгрузки потеряла фильтры"
    assert "category=feedback" in body


async def test_csv_and_page_agree_on_counts(owner_client, env):
    """Один набор правил на обоих: сколько строк на экране, столько в файле."""
    for i in range(3):
        await make_ticket(f"Обращение {i}")
    await make_ticket("Ещё одно", status="closed")

    page = owner_client.get("/panel/tickets").text
    # считаем пункты очереди, а не строку класса: «wb-item» есть ещё и в <style>
    # считаем пункты очереди, а не строку класса: «wb-item» есть ещё и в <style>
    on_screen = len(re.findall(r'&amp;t=\d+', page)) + len(re.findall(r'&t=\d+', page))
    body = owner_client.get("/panel/tickets.csv").text
    lines = [line for line in body.splitlines()[1:] if line.strip()]
    assert len(lines) == on_screen, f"на экране {on_screen}, в файле {len(lines)}"

    closed = owner_client.get("/panel/tickets.csv?status=closed").text
    closed_lines = [line for line in closed.splitlines()[1:] if line.strip()]
    assert len(closed_lines) == 1


# ── 2. «дольше суток» ──────────────────────────────────────────────────────
async def test_overdue_means_waiting_more_than_a_day(env):
    """Ждёт сутки и дольше - просрочка по нашей, не по чужой мерке."""
    tid = await make_ticket("Ждёт давно")
    await touch(tid, OVERDUE_HOURS + 2)
    row = await repo.get_ticket(tid)
    assert is_overdue(row, {tid: "student"}) is True


async def test_fresh_waiting_is_not_overdue(env):
    """Только что написанное не считается просрочкой."""
    tid = await make_ticket("Ждёт час")
    await touch(tid, 1)
    row = await repo.get_ticket(tid)
    assert is_overdue(row, {tid: "student"}) is False


async def test_answered_ticket_is_never_overdue(env):
    """Ответили сотрудник - письмо ждёт сотрудника, а не студента."""
    tid = await make_ticket("Ответили вовремя")
    await touch(tid, 48)
    row = await repo.get_ticket(tid)
    assert is_overdue(row, {tid: "staff"}) is False, "ответивший сотрудник ждёт не считается"


async def test_unparsable_date_is_not_overdue(env):
    """Мусор в дате не должен превращаться в «просрочено»."""
    tid = await make_ticket("Без даты")
    await db.run("UPDATE tickets SET updated_at='' WHERE ticket_id=?", (tid,))
    row = await repo.get_ticket(tid)
    assert is_overdue(row, {tid: "student"}) is False


async def test_filter_by_overdue_scope(owner_client, env):
    """Фильтр «дольше суток» оставляет только старые ожидания."""
    old = await make_ticket("Старое ожидание")
    await touch(old, OVERDUE_HOURS + 5)
    fresh = await make_ticket("Свежее ожидание")
    await touch(fresh, 2)

    rows, _latest, _waiting, overdue = await filter_tickets(scope="overdue")
    ids = {int(row["ticket_id"]) for row in rows}
    assert old in ids, "старое ожидание должно попасть в фильтр"
    assert fresh not in ids, "свежее ожидание не должно попасть в фильтр"
    assert overdue == len(rows)


async def test_workbench_shows_the_counter(owner_client, env):
    """Кнопка со счётчиком есть и показывает число."""
    old = await make_ticket("Старое")
    await touch(old, OVERDUE_HOURS + 5)
    body = owner_client.get("/panel/tickets").text
    assert "Дольше суток" in body, "нет кнопки со счётчиком"
    assert "scope=overdue" in body, "кнопка не ведёт в фильтр"
    # подпись идёт после иконки, поэтому со пробелом и без «>» в начале
    assert "Дольше суток: 1" in body, "счётчик не показывает число"

    filtered = owner_client.get("/panel/tickets?scope=overdue").text
    assert f"№{old}" in filtered
    assert "Свежее" not in filtered
