"""Регрессии long polling: маркер событий MAX нельзя терять.

Если API не вернул новый marker, сбросить его на None нельзя: MAX отдаст уже
обработанные события заново (дубли сообщений), а потерянный после обрыва маркер
означает повторную доставку всей ленты. Поэтому poll сохраняет предыдущий маркер
и меняет его только когда пришёл новый.
"""
import asyncio

import pytest
from conftest import FakeAPI
from max_api import MaxAPIError

import bot
from handlers.common import pending_tasks


class PollingAPI(FakeAPI):
    """FakeAPI + сценарий ответов /updates. Сеть не используется."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.markers = []  # с каким маркером пришёл каждый вызов updates

    async def updates(self, marker=None):
        self.markers.append(marker)
        if not self.script:  # сценарий исчерпан — останавливаем бесконечный цикл poll
            raise asyncio.CancelledError
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def poller(env, monkeypatch):
    """Подменяет api у bot на сценарный (handlers.* остаются на общем FakeAPI)."""
    api = PollingAPI([])
    monkeypatch.setattr(bot, "api", api)
    return api


@pytest.fixture
def no_backoff(monkeypatch):
    """Убирает паузу в 5 секунд после ошибки в poll, иначе тест ждал бы слишком долго."""
    real_sleep = asyncio.sleep

    async def instant(delay, *args, **kwargs):
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)


async def run_poll(api: PollingAPI, script) -> list:
    """Прогоняет poll по сценарию ответов и возвращает последовательность маркеров."""
    api.script = list(script)
    with pytest.raises(asyncio.CancelledError):  # fake API обрывает цикл
        await bot.poll()
    return api.markers


def message(user_id: int, text: str) -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": user_id, "is_bot": False},
            "recipient": {"chat_id": 1, "chat_type": "dialog"},
            "body": {"text": text},
        },
    }


async def test_poll_starts_without_marker(poller):
    """Первый запрос уходит без маркера — иначе MAX не отдаст накопленные события."""
    assert await run_poll(poller, [{"updates": [], "marker": 1}]) == [None, 1]


async def test_poll_keeps_previous_marker_when_response_has_no_marker(poller):
    """Ответ без ключа marker — не повод откатываться на None: следующий запрос
    повторяет последний известный маркер, иначе события приедут повторно."""
    markers = await run_poll(
        poller,
        [
            {"updates": [], "marker": 7},
            {"updates": []},  # маркера нет вовсе
            {"updates": [], "marker": None},  # маркер есть, но пустой
            {"updates": [], "marker": 8},
        ],
    )
    assert markers == [None, 7, 7, 7, 8]


async def test_poll_advances_marker_when_api_returns_new_one(poller):
    """Новый маркер всегда применяется — иначе бот читает ленту с одного места."""
    markers = await run_poll(
        poller,
        [{"updates": [], "marker": 10}, {"updates": [], "marker": 11}, {"updates": [], "marker": 12}],
    )
    assert markers == [None, 10, 11, 12]
    known = [m for m in markers if m is not None]
    assert all(b >= a for a, b in zip(known, known[1:]))  # маркер не уезжает назад


async def test_poll_does_not_reset_marker_after_api_error(poller, no_backoff):
    """Обрыв связи после последнего маркера: повторяем тот же маркер, а не None,
    иначе события между маркером и обрывом приедут второй раз."""
    markers = await run_poll(
        poller,
        [
            {"updates": [], "marker": 42},
            MaxAPIError(503, "service unavailable"),
            {"updates": [], "marker": 43},
        ],
    )
    assert markers == [None, 42, 42, 43]


async def test_poll_keeps_marker_when_updates_list_missing(poller):
    """Ответ без поля updates (или с None) не должен ронять цикл и не должен
    сбрасывать маркер."""
    markers = await run_poll(poller, [{"marker": 5}, {"updates": None, "marker": None}, {"marker": 6}])
    assert markers == [None, 5, 5, 6]


async def test_poll_processes_received_updates(poller, api):
    """События из ответа уходят в обработку, а не теряются между итерациями.
    Ответ отправки смотрим в общем FakeAPI: сама отправка делается из handlers.*,
    а не из bot, где подменён только источник обновлений."""
    poller.script = [{"updates": [message(555, "привет")], "marker": 3}]
    with pytest.raises(asyncio.CancelledError):
        await bot.poll()
    tasks = pending_tasks()
    if tasks:  # задачи могли завершиться ещё во время poll
        await asyncio.gather(*tasks, return_exceptions=True)
    assert [uid for uid, _, _ in api.sent] == ["555"]
    assert poller.markers == [None, 3]

