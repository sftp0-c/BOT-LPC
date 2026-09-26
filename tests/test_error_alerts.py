"""Оповещение сис-админов об ошибках: работает и не спамит."""
import bot
import repository as repo
from conftest import say

SYS = "1"


def break_handler(monkeypatch, message="подсовываем ошибку"):
    """Ломает обработчик сообщения - так системный код видит настоящую ошибку."""
    async def boom(*_args, **_kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(bot.menus, "on_message", boom)


async def test_alert_goes_to_sysadmin(api, monkeypatch):
    bot._error_alerts.clear()
    break_handler(monkeypatch)
    await say("200", "Привет")
    notes = "\n".join(text for _, text, _ in api.to(SYS))
    assert "Ошибка при обработке" in notes
    assert "RuntimeError" in notes and "подсовываем ошибку" in notes


async def test_user_still_gets_its_own_message(api, monkeypatch):
    bot._error_alerts.clear()
    break_handler(monkeypatch)
    await say("200", "Привет")
    assert "Что-то пошло не так" in api.last("200")[1]


async def test_alert_is_not_spammed(api, monkeypatch):
    """Одна и та же ошибка - одно сообщение, даже если повторяется."""
    bot._error_alerts.clear()
    break_handler(monkeypatch, "одна и та же ошибка")
    for _ in range(3):
        await say("200", "Привет")
    alerts = [text for _, text, _ in api.to(SYS) if "Ошибка при обработке" in text]
    assert len(alerts) == 1, f"ожидался один алерт, пришло {len(alerts)}"


async def test_different_errors_are_reported_separately(api, monkeypatch):
    bot._error_alerts.clear()
    break_handler(monkeypatch, "первая ошибка")
    await say("200", "Привет")
    break_handler(monkeypatch, "вторая ошибка")
    await say("201", "Привет")
    alerts = [text for _, text, _ in api.to(SYS) if "Ошибка при обработке" in text]
    assert len(alerts) == 2


async def test_alert_reaches_db_sysadmins(api, monkeypatch):
    """Система ловит и тех, кого выдали прямо в панели, а не только из .env."""
    bot._error_alerts.clear()
    await repo.add_sysadmin("700")
    break_handler(monkeypatch, "ошибка для проверки")
    await say("701", "Привет")
    assert any("Ошибка при обработке" in text for _, text, _ in api.to("700"))


async def test_exc_summary_is_short_and_readable():
    try:
        raise ValueError("очень длинный текст ошибки " * 20)
    except ValueError:
        text = bot.exc_summary(limit=60)
    assert text.startswith("ValueError:")
    assert len(text) <= 60
