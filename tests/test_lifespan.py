"""Lifecycle приложения: HTTP-клиент MAX закрывается и при штатном выходе, и при сбое старта.

Все подмены локальные (monkeypatch), conftest.py не меняется.
"""
import asyncio

import pytest

import bot
import config
from handlers.common import pending_tasks, spawn


class FakeAPI:
    """Подмена MaxAPI: считает закрытия и умеет имитировать сбой на старте."""

    def __init__(self):
        self.closed = 0
        self.calls = []
        self.fail_on = set()

    async def _call(self, name):
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed")

    async def subscriptions(self):
        await self._call("subscriptions")
        return []

    async def subscribe(self, url, secret):
        await self._call("subscribe")

    async def unsubscribe(self, url):
        await self._call("unsubscribe")

    async def updates(self, marker=None, **kwargs):
        await self._call("updates")
        return {"marker": (marker or 0) + 1, "updates": []}

    async def close(self):
        self.closed += 1


@pytest.fixture
def fake(monkeypatch, env):  # env из conftest: поднимает временную БД и чистит _locks
    api = FakeAPI()
    monkeypatch.setattr(bot, "api", api)
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "SYSADMIN_IDS", ["1"])
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
    return api


def fake_poll(state):
    """Поллер, который живёт вечно и запоминает отмену."""

    async def poll():
        state["started"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    return poll


async def let_tasks_run():
    """Дать созданным задачам (поллеру) реально стартовать до отмены."""
    for _ in range(3):
        await asyncio.sleep(0)


# ── штатная остановка ────────────────────────────────────────────────────────


async def test_shutdown_cancels_poller_and_closes_api(fake, monkeypatch):
    state = {}
    monkeypatch.setattr(bot, "poll", fake_poll(state))
    async with bot.lifespan(bot.app):
        await let_tasks_run()
        assert state["started"], "long polling должен быть запущен"
        assert fake.closed == 0, "до выхода из lifespan клиент открыт"
    assert state["cancelled"], "поллер должен быть отменён при выходе из lifespan"
    assert fake.closed == 1, "клиент MAX должен быть закрыт ровно один раз"
    assert not [t for t in pending_tasks() if not t.done()], "фоновых задач остаться не должно"


async def test_shutdown_waits_for_pending_tasks(fake, monkeypatch):
    state = {}
    monkeypatch.setattr(bot, "poll", fake_poll(state))
    bg = {}
    release = asyncio.Event()

    async def background():
        bg["started"] = True
        await release.wait()
        bg["finished"] = True

    async with bot.lifespan(bot.app):
        task = spawn(background())
        await let_tasks_run()
        assert task in pending_tasks()
        release.set()  # задача успевает завершиться — отменять её не нужно
    assert bg.get("finished"), "shutdown должен дождаться фоновой задачи, а не отменить её"
    assert task.done() and task.exception() is None
    assert fake.closed == 1


async def test_shutdown_cancels_task_that_outlives_timeout(fake, monkeypatch):
    monkeypatch.setattr(bot, "SHUTDOWN_TIMEOUT", 0.05)
    state = {}
    monkeypatch.setattr(bot, "poll", fake_poll(state))
    bg = {}

    async def background():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            bg["cancelled"] = True
            raise

    async with bot.lifespan(bot.app):
        task = spawn(background())
        await let_tasks_run()
    assert bg.get("cancelled"), "зависшая фоновая задача должна быть отменена по таймауту"
    assert task.cancelled()
    assert fake.closed == 1


async def test_webhook_mode_does_not_start_poller(fake, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://bot.example.com/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret_value")
    state = {}
    monkeypatch.setattr(bot, "poll", fake_poll(state))
    async with bot.lifespan(bot.app):
        await let_tasks_run()
        assert "subscribe" in fake.calls
    assert not state.get("started"), "в webhook-режиме long polling не запускается"
    assert fake.closed == 1


# ── сбой старта ──────────────────────────────────────────────────────────────


async def test_startup_error_closes_api(fake, monkeypatch):
    async def broken_db():
        raise RuntimeError("db is locked")

    monkeypatch.setattr(bot.db, "init_db", broken_db)
    with pytest.raises(RuntimeError, match="db is locked"):
        async with bot.lifespan(bot.app):
            pytest.fail("lifespan не должен доходить до yield при сбое старта")
    assert fake.closed == 1, "при сбое старта клиент MAX тоже должен быть закрыт"


async def test_config_error_closes_api(fake, monkeypatch):
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
    with pytest.raises(RuntimeError, match="MAX_BOT_TOKEN"):
        async with bot.lifespan(bot.app):
            pytest.fail("lifespan не должен доходить до yield при ошибке настроек")
    assert fake.closed == 1


async def test_subscribe_error_closes_api_without_poller(fake, monkeypatch):
    fake.fail_on = {"subscribe"}
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://bot.example.com/webhook")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret_value")
    state = {}
    monkeypatch.setattr(bot, "poll", fake_poll(state))
    with pytest.raises(RuntimeError, match="subscribe failed"):
        async with bot.lifespan(bot.app):
            pytest.fail("lifespan не должен доходить до yield, если подписка не создалась")
    assert fake.closed == 1
    assert not state.get("started"), "до успешного старта поллер запускаться не должен"


async def test_close_error_does_not_mask_startup_error(fake, monkeypatch):
    async def broken_close():
        fake.closed += 1
        raise RuntimeError("close failed")

    async def broken_db():
        raise RuntimeError("db is locked")

    monkeypatch.setattr(fake, "close", broken_close)
    monkeypatch.setattr(bot.db, "init_db", broken_db)
    with pytest.raises(RuntimeError, match="db is locked"):
        async with bot.lifespan(bot.app):
            pytest.fail("lifespan не должен доходить до yield при сбое старта")
    assert fake.closed == 1


# ── заглушки в настройках ────────────────────────────────────────────────────


def test_placeholder_tokens_are_not_settings(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
    assert config._get("MAX_BOT_TOKEN") == "", "значение-заглушка считается незаданным"
    assert config._is_placeholder("PASTE_YOUR_MAX_BOT_TOKEN_HERE")
    monkeypatch.setenv("MAX_BOT_TOKEN", "настоящий-токен")
    assert config._get("MAX_BOT_TOKEN") == "настоящий-токен"


def test_validate_rejects_placeholder_token(monkeypatch):
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
    monkeypatch.setattr(config, "SYSADMIN_IDS", ["1"])
    with pytest.raises(RuntimeError, match="MAX_BOT_TOKEN"):
        config.validate()
