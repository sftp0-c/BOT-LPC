from datetime import datetime
from itertools import count
import logging
from logging.handlers import RotatingFileHandler

import pytest

import bot
import clock
import config
import database as db
import webpanel
from handlers import admin, broadcast, common, faq, menus, schedules, tickets

BOT_ID = 999
PANEL_PASSWORD = "test-panel-pass"

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



# Момент, на котором замирают часы в frozen_college_clock. Середина недели,
# вторник: понедельник и пятница ломают проверки дней недели по краям.
FROZEN = datetime(2026, 9, 29, 10, 15, 0)


@pytest.fixture
def frozen_college_clock(monkeypatch):
    """Одна выборка времени на тест и на код под ним.

    clock.now() - единственное место, откуда проект берёт «сейчас» (все
    clock.today/stamp/stamp_at идут через него), поэтому одной подмены
    достаточно: тест и код гарантированно видят один и тот же момент, и
    тест не краснеет, когда прогон пришёлся на полночь.

    Фикстура не автоматическая намеренно: в тестах, где важны интервалы
    («5 дней назад», порядок событий по времени), время должно идти вперёд.
    """
    monkeypatch.setattr(clock, "now", lambda: FROZEN)
    return FROZEN


def _clear_flashes() -> None:
    """Очередь сообщений панели: одна теста не должна всплывать в другой."""
    getattr(webpanel, "_flashes", {}).clear()


def login_panel(client, user_id: str = "1", password: str = PANEL_PASSWORD) -> bool:
    return client.post("/panel/login", data={"user_id": user_id, "password": password},
                       follow_redirects=False).status_code == 303


def csrf_of(client) -> str:
    """CSRF-токен формы: он отдельный от cookie сессии."""
    return webpanel._csrf.get(client.cookies.get(webpanel.COOKIE, ""), "")


@pytest.fixture
def panel_client(monkeypatch, env):
    """TestClient веб-панели: пароль задан, сессии чистые (вход - login_panel).

    Общая фикстура для всех модулей с тестами панели. Без `with`: lifespan
    не запускается, базу поднимает фикстура env.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PANEL_PASSWORD)
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    webpanel._sessions.clear()
    _clear_flashes()            # сообщения прошлого теста не должны всплыть в этом
    webpanel._flash = ""
    return TestClient(bot.app)


def post_form(client, path: str, data: dict | None = None):
    """POST с CSRF-токеном, как это делает браузер сис-админа."""
    return client.post(path, data={**(data or {}), "csrf": csrf_of(client)}, follow_redirects=False)


def _retarget_log_file() -> None:
    """Переводит файловый журнал на config.LOG_FILE.

    Обработчик создаётся один раз при импорте bot.py и держит путь к файлу,
    поэтому одной подмены config.LOG_FILE мало: панель читала бы tmp-файл,
    а писали бы мы в настоящий logs/bot.log.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler):
            root.removeHandler(handler)
            handler.close()
    bot.setup_logging()


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
    # Время в проекте одно — локальное время колледжа (см. clock). Фиксируем
    # зону явно: без неё тесты зависят от машины (на Windows базы часовых
    # поясов нет, clock берёт запасной UTC+5, а в контейнере TZ не задан —
    # и время в базе уехало бы на 5 часов назад). clock.reset() — зона
    # читается один раз, тестам нужен чистый старт.
    monkeypatch.setenv("TZ", "Asia/Yekaterinburg")
    clock.reset()
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "SYSADMIN_IDS", ["1"])
    # владелец в тестах появляется только там, где это проверяется явно
    monkeypatch.setattr(config, "ROOT_IDS", [])
    # журнал тестов не должен попадать в настоящий logs/bot.log: иначе реальный
    # журнал наполняется мусором из прогонов и засоряет вкладку панели
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "test.log"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    _retarget_log_file()
    fake = FakeAPI()
    for module in (bot, common, admin, broadcast, menus, tickets, schedules, faq):
        monkeypatch.setattr(module, "api", fake)
    bot._locks.clear()
    _clear_flashes()
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
    """Регистрация студента так, как это делает человек: выбор роли, ФИО, группа."""
    await say(user, "/start")
    await press(user, "who:student")
    await say(user, name)
    await say(user, group)
    await press(user, "regyes")      # подтверждение данных, последний шаг регистрации
    await press(user, "consentyes")  # согласие на обработку данных


async def add_staff(staff_id, name, category="all", broadcast=False, position="", office="-"):
    """Добавляет сотрудника так, как это делает superadmin (ID=1) через меню."""
    await press("1", "sfadd")
    await say("1", str(staff_id))
    await say("1", name)
    await say("1", position if position else "-")
    await say("1", office)
    await press("1", f"sfc:{staff_id}:{category}")
    if broadcast:
        await press("1", f"sfb:{staff_id}")


@pytest.fixture
async def clear_templates():
    """Чистит шаблоны ответов: при первом запуске бот заливает девять типовых.

    Тесты, которые проверяют пустое состояние или считают количество
    шаблонов, должны начинать с чистой таблицы - иначе они описывают не то
    состояние, которое думают.
    """
    import database as db
    await db.run("DELETE FROM reply_templates")
    return True
