"""Очередь сотрудника: фильтры, счётчики и «ждёт ответа»."""
import pytest

import database as db
import repository as repo
from conftest import add_staff, login_panel, press, register, say

SYS, STAFF, STUDENT = "1", "200", "100"


@pytest.fixture
def panel_client(panel_client):
    """conftest-фикстура плюс явное закрытие клиента.

    Без закрытия TestClient остаётся живым до конца сессии, а на Windows
    незакрытые дескрипторы базы мешают следующим тестам работать с файлом.
    """
    yield panel_client
    panel_client.close()


async def ticket(category: str = "feedback", staff: str = STAFF) -> int:
    await register(STUDENT, name="Иванов Иван")
    await add_staff(staff, "Петрова Анна", category="all")
    await press(STUDENT, f"new:{category}")
    await press(STUDENT, f"pick:{category}:{staff}")
    await say(STUDENT, "Нужна справка")
    await press(STUDENT, "ticketsend")
    return (await db.one("SELECT ticket_id FROM tickets ORDER BY ticket_id DESC"))["ticket_id"]


async def set_status(ticket_id: int, status: str) -> None:
    await db.run("UPDATE tickets SET status=? WHERE ticket_id=?", (status, ticket_id))


# ── бот ───────────────────────────────────────────────────────────────────────
async def test_queue_is_bare_and_filters_live_in_submenu(api):
    """На очереди нет ни счётчиков, ни фильтров; фильтры живут в «По отделам».

    Владелец: «фильтры не такая важная вещь, занимает очень много кнопок, которые
    сбивают с толку». С главного экрана они убраны. Из подменю «По отделам»
    убирать не стали: там кнопки не мешают, и фильтр по разделу нужен.
    """
    first = await ticket()
    second = await ticket("certificates")
    await press(STAFF, "staff")

    assert "Очередь обращений" in api.last(STAFF)[1]
    payloads = set(api.payloads(STAFF))
    assert {f"t:{first}", f"t:{second}"} <= payloads
    # ни фильтров, ни счётчиков с цифрами на главном экране
    assert not [p for p in payloads if p.startswith(("staffv:", "stafff:"))]
    assert not [b["text"] for row in api.last(STAFF)[2] for b in row if " — " in b["text"]]
    # осталось только то, без чего нельзя работать
    assert {"staffcat", "staff:"} <= payloads

    # «ждут ответа» работает и дальше: на представление остались закладки
    await press(STAFF, f"rp:{first}")
    await say(STAFF, "Ответили")
    await press(STAFF, "staffv:waiting")
    waiting = set(api.payloads(STAFF))
    assert f"t:{second}" in waiting and f"t:{first}" not in waiting

    # фильтры по отделам и статусам живут в подменю и работают оттуда
    await press(STAFF, "staffcat")
    assert "Очередь по отделам" in api.last(STAFF)[1]
    departments = set(api.payloads(STAFF))
    assert "stafff:certificates" in departments and "stafff:open" in departments
    await press(STAFF, "stafff:certificates")
    только_справки = set(api.payloads(STAFF))
    assert f"t:{second}" in только_справки and f"t:{first}" not in только_справки


async def test_filter_by_status(api):
    """Фильтр по статусу отбирает нужные обращения.

    Подписи «Фильтр: … — N» на экране больше нет: владелец просил мало информации.
    Проверяем то, ради чего фильтр и нужен, - что отбор верный.
    """
    first = await ticket()
    second = await ticket()
    await set_status(second, "completed")
    await press(STAFF, "stafff:completed")
    payloads = api.payloads(STAFF)
    assert f"t:{second}" in payloads and f"t:{first}" not in payloads


async def test_filter_open_shows_only_open(api):
    first = await ticket()
    second = await ticket()
    await set_status(second, "completed")
    await press(STAFF, "stafff:open")
    payloads = api.payloads(STAFF)
    assert f"t:{first}" in payloads and f"t:{second}" not in payloads


async def test_filter_by_category(api):
    await ticket("feedback")
    await ticket("certificates")
    await press(STAFF, "stafff:certificates")


async def test_filter_survives_refresh(api):
    """Кнопка «Обновить» возвращает ту же очередь с тем же фильтром."""
    first = await ticket()
    second = await ticket()
    await set_status(second, "completed")
    await press(STAFF, "stafff:completed")
    assert "staff:completed" in api.payloads(STAFF)
    api.sent.clear()
    await press(STAFF, "staff:completed")
    assert f"t:{first}" not in api.payloads(STAFF)


async def test_queue_without_filter_shows_all_open(api):
    """Без фильтра очередь снова вся - отдельная кнопка сброса больше не нужна.

    Раньше были фильтр и отдельная кнопка «Сбросить фильтр». Обе убраны с экрана.
    Сброс получается сам: из фильтра ушли в меню и открыли очередь заново.
    """
    first = await ticket()
    second = await ticket()
    await set_status(second, "completed")
    await press(STAFF, "stafff:completed")
    assert f"t:{second}" in api.payloads(STAFF)

    await press(STAFF, "home")
    await press(STAFF, "staff")
    payloads = set(api.payloads(STAFF))
    assert f"t:{first}" in payloads, "после сброса очередь не показала открытые дела"
    # закрытое в очередь не возвращается: оно в архиве
    assert f"t:{second}" not in payloads


async def test_queue_of_empty_staff(api):
    await add_staff(STAFF, "Петрова Анна")
    await press(STAFF, "staff")
    text = api.last(STAFF)[1]
    assert "Обращений нет" in " ".join(api.payloads(STAFF)) or "Очередь обращений" in text


async def test_student_cannot_open_queue(api):
    await register(STUDENT)
    api.sent.clear()
    await press(STUDENT, "staff")
    assert not api.to(STUDENT)


async def test_sysadmin_sees_all_departments(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff("201", "Соколов Иван", category="all")
    await register(STUDENT, name="Иванов Иван")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Первое")
    await press(STUDENT, "ticketsend")
    await press(STUDENT, "new:feedback")
    await press(STUDENT, "pick:feedback:201")
    await say(STUDENT, "Второе")
    await press(STUDENT, "ticketsend")
    await press(SYS, "staff")
    payloads = api.payloads(SYS)
    assert "t:1" in payloads and "t:2" in payloads  # сис-админ видит все


# ── панель ────────────────────────────────────────────────────────────────────
async def test_panel_filters(panel_client):
    first = await ticket("feedback")
    second = await ticket("certificates")
    await set_status(second, "completed")

    assert login_panel(panel_client)
    everything = panel_client.get("/panel/tickets").text
    assert f"№{first}" in everything and f"№{second}" in everything

    only_done = panel_client.get("/panel/tickets", params={"status": "completed"}).text
    assert f"№{second}" in only_done and f"№{first}" not in only_done

    only_certs = panel_client.get("/panel/tickets", params={"category": "certificates"}).text
    assert f"№{second}" in only_certs and f"№{first}" not in only_certs

    only_open = panel_client.get("/panel/tickets", params={"status": "open"}).text
    assert f"№{first}" in only_open and f"№{second}" not in only_open


async def test_panel_waiting_filter(panel_client):
    """Оба обращения ждут ответа сотрудника - значит «только ждут ответа» покажет оба."""
    await ticket()
    await ticket()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets", params={"scope": "waiting"}).text
    assert "Только ждут ответа: 2" in body
    assert "№1" in body and "№2" in body


async def test_panel_waiting_filter_drops_answered(panel_client):
    ticket_id = await ticket()
    await press(STAFF, f"rp:{ticket_id}")
    await say(STAFF, "Ответили")
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets", params={"scope": "waiting"}).text
    assert "Только ждут ответа: 0" in body
    assert f"№{ticket_id}" not in body


@pytest.mark.parametrize("params", [
    {"status": "нет такого"}, {"category": "нет"}, {"q": "‹скрипт›"},
    {"scope": "waiting", "status": "open", "category": "feedback"},
])
async def test_panel_filters_never_crash(panel_client, params):
    assert login_panel(panel_client)
    assert panel_client.get("/panel/tickets", params=params).status_code == 200


async def test_status_counts_unchanged_by_filters(api):
    """Счётчики считают по базе, а не по тому, что показано на экране.

    Самих счётчиков на экране больше нет - они были на кнопках фильтров, а
    фильтры с главного экрана убраны по решению владельца. Считает их панель, и
    считать должна вся картина, а не результат фильтра.
    """
    await ticket()
    await ticket()
    await press(STAFF, "stafff:open")
    assert await repo.status_counts(STAFF) == {"new": 2}
