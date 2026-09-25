from itertools import count

import pytest

import bot
import config
import database as db
from handlers import admin, broadcast, common, menus, tickets

BOT_ID = 999

_click_seq = count(1)


class FakeAPI:
    """Подмена MaxAPI: запоминает исходящие сообщения."""

    def __init__(self):
        self.sent = []
        self.answers = []
        self.blocked = set()

    async def send(self, user_id, text, keyboard=None):
        if str(user_id) in self.blocked:
            raise RuntimeError("user blocked the bot")
        self.sent.append((str(user_id), text, keyboard))
        return {}

    async def answer(self, callback_id, notification=None):
        self.answers.append(callback_id)

    async def close(self):
        pass

    def to(self, uid):
        return [m for m in self.sent if m[0] == str(uid)]

    def last(self, uid):
        return self.to(uid)[-1]

    def payloads(self, uid):
        kb = self.last(uid)[2] or []
        return [b["payload"] for row in kb for b in row if b["type"] == "callback"]


def msg(user, text):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": int(user), "is_bot": False},
            "recipient": {"chat_id": 1, "chat_type": "dialog"},
            "body": {"text": text},
        },
    }


def click(user, payload):
    # как в реальном MAX: message.sender — бот, нажавший пользователь — в callback.user,
    # а callback_id уникален для каждого нажатия (нужно для проверок дедупликации)
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": f"cb-{next(_click_seq)}-{payload}",
            "payload": payload,
            "user": {"user_id": int(user)},
        },
        "message": {
            "sender": {"user_id": BOT_ID, "is_bot": True},
            "recipient": {"chat_id": 1, "chat_type": "dialog"},
            "body": {"text": "menu"},
        },
    }


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "SYSADMIN_IDS", ["1"])
    fake = FakeAPI()
    for module in (bot, common, admin, broadcast, menus, tickets):
        monkeypatch.setattr(module, "api", fake)
    bot._locks.clear()
    await db.init_db()
    return fake


@pytest.fixture
def api(env):
    return env


async def say(user, text):
    await bot.process(msg(user, text))


async def press(user, payload):
    await bot.process(click(user, payload))


async def register(user, name="Иванов Иван Иванович", group="ис-21"):
    await say(user, "/start")
    await say(user, name)
    await say(user, group)


async def add_staff(staff_id, name, category="all", broadcast=False):
    """Добавляет сотрудника так, как это делает superadmin (ID=1) через меню."""
    await press("1", "sfadd")
    await say("1", str(staff_id))
    await say("1", name)
    await press("1", f"sfc:{staff_id}:{category}")
    if broadcast:
        await press("1", f"sfb:{staff_id}")
