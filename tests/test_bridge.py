"""Мост: ворота, очередь и служебные адреса.

Главное, что здесь проверяется, - НЕДОСТУПНОСТЬ. Мост принимает сообщения
только владельца бота, и это должно быть доказано, а не обещано: студент не
должен попасть в ветку, даже если знает командное слово.

Две особенности тестовой обвязки, на которые ушла часть времени:

1. Фикстура настроек моста ОБЯЗАНА зависеть от env. Фикстура env в conftest
   сбрасывает config.ROOT_IDS в пустой список - владелец в тестах появляется
   только там, где это нужно явно. Фикстура без зависимости от env отработала
   раньше неё, и её настройки затёрлись, а ворота молча не срабатывали.

2. У служебных адресов свой TestClient с адресом 127.0.0.1. У общего клиента
   адрес «testclient», и проверка «запрос с этой машины» отвечала бы отказом -
   счастливый путь не проверялся бы вовсе. Ослаблять саму проверку ради теста
   нельзя, значит тесту задают настоящий локальный адрес.
"""
import json

import pytest

import bridge_service
import config
import database as db
import repository as repo
import webpanel
from conftest import PANEL_PASSWORD, register, say
from handlers import bridge as gate

pytestmark = pytest.mark.panel

OWNER = "46010397"
SYS = "1"
STUDENT, OTHER = "100", "101"
TOKEN = "t" * 32
PREFIX = "!"


@pytest.fixture(autouse=True)
def bridge_on(monkeypatch, env):
    """Мост включён, токен задан, владелец назначен.

    env обязателен: без этой зависимости настройки здесь применяются раньше env
    и затёртываются его сбросом ROOT_IDS.
    """
    monkeypatch.setattr(config, "BRIDGE_ENABLED", True)
    monkeypatch.setattr(config, "BRIDGE_TOKEN", TOKEN)
    monkeypatch.setattr(config, "BRIDGE_PREFIX", PREFIX)
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    assert gate.is_owner(OWNER), "фикстура не задала владельца - ворота не сработают"


def authed(token: str = TOKEN) -> dict:
    return {"X-Bridge-Token": token}


@pytest.fixture
def bridge_client(monkeypatch, env):
    """Служебные адреса моста: отдельная служба и верный токен.

    Именно bridge_service, а не основное приложение: мост живёт в отдельной
    службе на порту, который снаружи не виден, и по общему порту панели его
    адресов нет вовсе. Проверяем ровно то, чем пользуется программа-мост.
    """
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PANEL_PASSWORD)
    webpanel._sessions.clear()
    return TestClient(bridge_service.app)


async def take_pending() -> int:
    rows = await repo.bridge_store.pending(3)
    assert rows, "в очереди пусто, а тест ждёт задание"
    assert await repo.bridge_store.take(rows[0]["id"]), "задание не взялось"
    return int(rows[0]["id"])


# ── ворота: кто может попасть в мост ───────────────────────────────────────
def test_only_the_owner_passes_the_gate():
    """Строгое сравнение с ROOT_IDS: ни подстроки, ни «похожего» номера."""
    assert gate.is_owner(OWNER) is True
    for alien in (STUDENT, OTHER, SYS, "", " 46010397", "460103970", "046010397", "провенция"):
        assert gate.is_owner(alien) is False, alien


def test_command_word_alone_is_not_a_bridge_text():
    """Командное слово есть, а отправитель не владелец - это не мост."""
    assert gate.is_bridge_text(STUDENT, f"{PREFIX}удали всех студентов") is False
    assert gate.is_bridge_text(OWNER, f"{PREFIX}удали всех студентов") is True


def test_owner_without_command_word_goes_to_the_normal_bot():
    """Владелец без командного слова - обычное сообщение, мост его не берёт."""
    assert gate.is_bridge_text(OWNER, "привет") is False


def test_bridge_off_means_nothing_is_a_bridge_text(monkeypatch):
    """Выключенный мост не ловит даже сообщения владельца."""
    monkeypatch.setattr(config, "BRIDGE_ENABLED", False)
    assert gate.is_bridge_text(OWNER, f"{PREFIX}сделай") is False


async def test_student_never_reaches_the_queue(api, env):
    """Студент пишет командное слово - в очередь не попадает ничего."""
    await register(STUDENT, "Иванов Иван", "ис-21")
    await say(STUDENT, f"{PREFIX}удали всех студентов")
    assert await repo.bridge_store.count_pending() == 0, "студент попал в очередь моста"
    assert await db.one("SELECT * FROM bridge_messages WHERE user_id=?", (STUDENT,)) is None


async def test_owner_message_goes_to_the_queue(api, env):
    """Владелец с командным словом - задание в очереди."""
    await register(OWNER, "Иванов Иван", "ис-21")
    await say(OWNER, f"{PREFIX}добавь в раздел справок тему про военную часть")
    assert await repo.bridge_store.count_pending() == 1
    row = await db.one("SELECT * FROM bridge_messages WHERE user_id=?", (OWNER,))
    assert "военную часть" in row["text"]
    assert row["status"] == "new"


async def test_owner_does_not_lose_the_normal_bot(api, env):
    """Мост не съел обычную жизнь: сообщение без командного слова идёт в бота."""
    await register(OWNER, "Иванов Иван", "ис-21")
    await say(OWNER, "привет")
    assert "Мост" not in api.last(OWNER)[1], "сообщение без командного слова ушло в мост"
    assert await repo.bridge_store.count_pending() == 0


async def test_command_word_alone_shows_help_and_queues_nothing(api, env):
    """Голое командное слово - это справка, а не пустое задание."""
    await register(OWNER, "Иванов Иван", "ис-21")
    api.sent.clear()
    await say(OWNER, PREFIX)
    assert "Мост" in api.last(OWNER)[1]
    assert await repo.bridge_store.count_pending() == 0


async def test_bridge_off_tells_the_owner_it_is_off(api, env, monkeypatch):
    """Выключенный мост отвечает прямо, а не молчит."""
    monkeypatch.setattr(config, "BRIDGE_ENABLED", False)
    await register(OWNER, "Иванов Иван", "ис-21")
    api.sent.clear()
    await say(OWNER, f"{PREFIX}сделай правку")
    assert "выключен" in api.last(OWNER)[1].lower()
    assert await repo.bridge_store.count_pending() == 0


# ── очередь ───────────────────────────────────────────────────────────────
async def test_queue_is_ordered(env):
    """Порядок работы - порядок сообщений, а не случайный."""
    for note in ("первое", "второе", "третье"):
        await repo.bridge_store.add_message(OWNER, note)
    rows = await repo.bridge_store.pending(10)
    assert [r["text"] for r in rows] == ["первое", "второе", "третье"]


async def test_message_is_taken_only_once(env):
    """Два опроса подряд не заберут одно задание дважды."""
    await repo.bridge_store.add_message(OWNER, "одна задача")
    message_id = await take_pending()
    assert await repo.bridge_store.take(message_id) is False, "взяли повторно"


async def test_taken_message_is_not_handed_out_again(env):
    """Взятое задание не должно выпадать следующему опросу."""
    await repo.bridge_store.add_message(OWNER, "одна задача")
    await take_pending()
    assert await repo.bridge_store.pending(3) == []


async def test_answer_closes_the_message(env):
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = await take_pending()
    assert await repo.bridge_store.finish(message_id, "готово, коммит abc123") is True
    row = await repo.bridge_store.get_message(message_id)
    assert row["status"] == "done"
    assert "abc123" in row["answer"]
    assert await repo.bridge_store.count_pending() == 0


async def test_failed_message_is_visible_with_the_reason(env):
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = await take_pending()
    assert await repo.bridge_store.fail(message_id, "компьютер выключен") is True
    row = await repo.bridge_store.get_message(message_id)
    assert row["status"] == "failed"
    assert "выключен" in row["error"]


async def test_stale_message_returns_to_the_queue(env):
    """Программа взяла задание и упала - задание не пропало.

    Время не подменяем: проставляем taken_at руками, двумя часами раньше.
    Так проверка не зависит от того, как устроены часы в тестах.
    """
    await repo.bridge_store.add_message(OWNER, "зависшее")
    message_id = await take_pending()
    assert await repo.bridge_store.pending(3) == []
    import clock
    await db.run("UPDATE bridge_messages SET taken_at=? WHERE id=?",
                 (clock.stamp_at(-120), message_id))
    returned = await repo.bridge_store.requeue_stale(90)
    assert returned == 1, "зависшее задание не вернулось в очередь"
    assert [int(r["id"]) for r in await repo.bridge_store.pending(3)] == [message_id]


async def test_fresh_busy_message_is_not_returned(env):
    """Свежее задание в работе не трогаем: задача бывает долгой."""
    await repo.bridge_store.add_message(OWNER, "свежее")
    await take_pending()
    assert await repo.bridge_store.requeue_stale(90) == 0
    assert await repo.bridge_store.pending(3) == []


async def test_busy_message_is_counted_as_waiting(env):
    """Взятое задание всё ещё «ждёт владельца ответа» - счётчик это видит."""
    await repo.bridge_store.add_message(OWNER, "в работе")
    await take_pending()
    assert await repo.bridge_store.count_pending() == 1


async def test_done_message_is_not_counted(env):
    await repo.bridge_store.add_message(OWNER, "сделано")
    message_id = await take_pending()
    await repo.bridge_store.finish(message_id, "готово")
    assert await repo.bridge_store.count_pending() == 0


# ── служебные адреса ───────────────────────────────────────────────────────
async def test_endpoints_are_closed_without_token(bridge_client, env):
    """Знание адреса без токена ничего не даёт."""
    await db.init_db()
    assert bridge_client.get("/bridge/next").status_code == 403


async def test_endpoints_are_closed_with_wrong_token(bridge_client, env):
    await db.init_db()
    assert bridge_client.get("/bridge/next", headers=authed("x" * 32)).status_code == 403


async def test_client_address_does_not_affect_access(monkeypatch, env):
    """Адрес клиента НЕ влияет на допуск - и это намеренно.

    Так было задумано сначала и сломалось на живом боте: Docker Desktop
    публикует порт через виртуальную машину, бот видит адрес её шлюза даже для
    запроса с этой же машины, и мост переставал работать вовсе. Ослабить
    проверку до «шлюза» - значит не проверять ничего: из сети приходит то же.

    Границу держит порт, слушающий только локально на хосте, а не код. Проверка
    фиксирует это решение, чтобы проверка адреса не вернулась тихо.
    Бьём по службе моста - единственной, где эти адреса вообще есть: в основном
    приложении их нет, и это отдельная, соседняя проверка.
    """
    from fastapi.testclient import TestClient

    await db.init_db()
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PANEL_PASSWORD)
    чужой = TestClient(bridge_service.app, client=("10.9.9.9", 50000))
    assert чужой.get("/bridge/next", headers=authed()).status_code == 200, (
        "адрес клиента снова влияет на допуск: мост с этой машины работать не будет"
    )


async def test_endpoints_are_closed_when_disabled(bridge_client, monkeypatch, env):
    """Мост выключен - адреса не работают вовсе, даже с верным токеном."""
    await db.init_db()
    monkeypatch.setattr(config, "BRIDGE_ENABLED", False)
    assert bridge_client.get("/bridge/next", headers=authed()).status_code == 403


async def test_endpoints_need_no_panel_login(bridge_client, env):
    """Служебные адреса не про панель: токен важнее входа сис-админа."""
    await db.init_db()
    assert bridge_client.get("/bridge/next", headers=authed()).status_code == 200


async def test_bridge_hands_out_a_task_and_takes_it(bridge_client, env):
    """Рабочий сценарий программы: получили задание, оно помечено взятым."""
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "почини кнопки")
    response = bridge_client.get("/bridge/next", headers=authed())
    assert response.status_code == 200, response.status_code
    data = response.json()
    assert data["text"] == "почини кнопки"
    assert data["user_id"] == OWNER
    again = bridge_client.get("/bridge/next", headers=authed()).json()
    assert again == {"empty": True}, "задание отдали второй раз"


async def test_empty_queue_is_reported_as_empty(bridge_client, env):
    await db.init_db()
    body = bridge_client.get("/bridge/next", headers=authed()).json()
    assert body == {"empty": True}


async def test_bridge_answer_reaches_the_owner(bridge_client, api, env):
    """Ответ программы уходит владельцу в MAX."""
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "сделай отчёт")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    api.sent.clear()
    response = bridge_client.post(f"/bridge/{message_id}/answer", headers=authed(),
                                 content=json.dumps({"answer": "Готово: поправил, коммит 93c1b67"}))
    assert response.status_code == 200, response.status_code
    assert "93c1b67" in api.last(OWNER)[1]
    assert (await repo.bridge_store.get_message(message_id))["status"] == "done"


async def test_bridge_fail_reaches_the_owner(bridge_client, api, env):
    """О неудаче владелец тоже должен знать: иначе он ждёт ответа впустую."""
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "сделай отчёт")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    api.sent.clear()
    response = bridge_client.post(f"/bridge/{message_id}/fail", headers=authed(),
                                 content=json.dumps({"error": "модель не отвечает"}))
    assert response.status_code == 200
    assert "не отвечает" in api.last(OWNER)[1]
    assert (await repo.bridge_store.get_message(message_id))["status"] == "failed"


async def test_bridge_refuses_empty_answer(bridge_client, env):
    """Пустой ответ не должен закрывать задание молча."""
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    response = bridge_client.post(f"/bridge/{message_id}/answer", headers=authed(),
                                 content=json.dumps({"answer": "   "}))
    assert response.status_code == 422
    assert (await repo.bridge_store.get_message(message_id))["status"] == "busy"


async def test_bridge_refuses_broken_json(bridge_client, env):
    """Мусор в теле запроса - 422, а не падение бота."""
    await db.init_db()
    response = bridge_client.post("/bridge/1/answer", headers=authed(),
                                 content="{это не json")
    assert response.status_code == 422


async def test_bridge_refuses_unknown_message(bridge_client, env):
    await db.init_db()
    response = bridge_client.post("/bridge/999/answer", headers=authed(),
                                 content=json.dumps({"answer": "привет"}))
    assert response.status_code == 404


async def test_bridge_status_does_not_leak_content(bridge_client, env):
    """Ответ «статус» не должен выдавать содержимое заданий."""
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "секретное задание")
    body = bridge_client.get("/bridge/status", headers=authed()).text
    assert "секретное задание" not in body, "статус раскрыл содержимое задания"


async def test_bridge_status_counts_waiting(bridge_client, env):
    await db.init_db()
    await repo.bridge_store.add_message(OWNER, "одно")
    await repo.bridge_store.add_message(OWNER, "два")
    data = bridge_client.get("/bridge/status", headers=authed()).json()
    assert data["waiting"] == 2


# ── возврат задания в очередь ─────────────────────────────────────────────────
# Программа взяла задание, а проект держит консоль. Называть это ошибкой
# нельзя: ошибки не было. Задание возвращается в очередь, и владельцу
# честно объясняется, почему он ждёт.


async def test_defer_puts_the_task_back(bridge_client, env):
    """Взятое задание снова попадает в очередь и может быть взято повторно."""
    await repo.bridge_store.add_message(OWNER, "почини кнопки")
    message_id = await take_pending()
    assert await repo.bridge_store.pending(3) == [], "задание не было взято"
    assert await repo.bridge_store.defer(message_id) is True
    again = await repo.bridge_store.pending(3)
    assert [int(r["id"]) for r in again] == [message_id], "задание не вернулось в очередь"


async def test_defer_clears_the_taken_time(bridge_client, env):
    """Время взятия сбрасывается - иначе вернутое задание считалось бы зависшим."""
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = await take_pending()
    assert await repo.bridge_store.defer(message_id) is True
    row = await repo.bridge_store.get_message(message_id)
    assert row["taken_at"] == ""


async def test_defer_keeps_the_task_waiting(bridge_client, env):
    """Вернутое задание снова ждёт владельца ответа."""
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = await take_pending()
    await repo.bridge_store.defer(message_id)
    assert await repo.bridge_store.count_pending() == 1


async def test_defer_does_not_touch_a_closed_task(bridge_client, env):
    """Закрытое задание нельзя вернуть в работу: оно уже выполнено."""
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = await take_pending()
    await repo.bridge_store.finish(message_id, "готово")
    assert await repo.bridge_store.defer(message_id) is False
    assert (await repo.bridge_store.get_message(message_id))["status"] == "done"


async def test_defer_does_not_grab_a_fresh_task(bridge_client, env):
    """Не взятое задание нечего возвращать - оно и так в очереди."""
    await repo.bridge_store.add_message(OWNER, "задача")
    rows = await repo.bridge_store.pending(3)
    row = rows[0]
    assert await repo.bridge_store.defer(int(row["id"])) is False


# ── через адрес ────────────────────────────────────────────────────────────
async def test_defer_over_http_with_a_note(bridge_client, api, env):
    """Владелец узнаёт, почему ждёт, и задание возвращается."""
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    api.sent.clear()
    response = bridge_client.post(f"/bridge/{message_id}/defer", headers=authed(),
                                 content=json.dumps({"text": "Проект держит консоль, жду."}))
    assert response.status_code == 200, response.status_code
    assert "консоль" in api.last(OWNER)[1]
    assert (await repo.bridge_store.get_message(message_id))["status"] == "new"


async def test_defer_over_http_without_a_note(bridge_client, api, env):
    """Без пояснения просто возвращаем - лишних сообщений владельцу не шлём."""
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    api.sent.clear()
    response = bridge_client.post(f"/bridge/{message_id}/defer", headers=authed(),
                                 content=json.dumps({}))
    assert response.status_code == 200
    assert api.to(OWNER) == [], "владельцу пришло лишнее сообщение"
    assert (await repo.bridge_store.get_message(message_id))["status"] == "new"


async def test_defer_of_unknown_task(bridge_client, env):
    assert bridge_client.post("/bridge/999/defer", headers=authed(),
                              content=json.dumps({})).status_code == 404


async def test_defer_of_closed_task_over_http(bridge_client, env):
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    bridge_client.post(f"/bridge/{message_id}/answer", headers=authed(),
                       content=json.dumps({"answer": "готово"}))
    assert bridge_client.post(f"/bridge/{message_id}/defer", headers=authed(),
                              content=json.dumps({})).status_code == 409


async def test_defer_needs_the_token(bridge_client, env):
    await repo.bridge_store.add_message(OWNER, "задача")
    message_id = int(bridge_client.get("/bridge/next", headers=authed()).json()["id"])
    assert bridge_client.post(f"/bridge/{message_id}/defer",
                              content=json.dumps({})).status_code == 403
