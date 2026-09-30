"""handlers/bridge.py - ворота моста на стороне бота.

Что делает модуль. Владелец пишет боту с командного слова (по умолчанию «!»),
и вместо обычной логики меню сообщение попадает в очередь. Ответ приходит
другим путём: программа на компьютере забирает задание и присылает ответ,
который бот отправляет владельцу.

Ворота. Сработать может ТОЛЬКО сообщение владельца: сравнение его ID с
config.ROOT_IDS, строка в строку. Никаких «похожих», никаких «если в списке
системных». Студент, даже если знает командное слово, дальше этих ворот не
пойдёт - и это проверяется тестом.

Почему отдельный модуль, а не строчка в bot.py. Здесь живёт вся логика ворот,
а в bot.py остаётся одна проверка. Если проект когда-нибудь перейдёт к другому
владельцу, мост удаляется: этот файл, одна строка в bot.py и три переменные
в .env. Больше ничего.

Состояние занятости. Владельцу полезно знать, что происходит: ждёт ли
компьютер задание или уже взял его. Поэтому «принято» и «взято в работу»
сообщаются разными фразами.
"""
import config
import repository as repo
from handlers.common import api, log
from utils import as_str

BRIDGE = repo.bridge_store

BUSY_TEXT = "⏳ Уже работаю над предыдущим. Это сообщение в очереди, сделаю следом."
OFF_TEXT = ("Мост выключен. Включается переменными BRIDGE_ENABLED=1 и "
            "BRIDGE_TOKEN в окружении.")


def is_owner(user_id: str) -> bool:
    """Владелец ли это. Сравнение строгое: только точный ID из ROOT_IDS."""
    return as_str(user_id) in set(config.ROOT_IDS)


def is_bridge_text(user_id: str, text: str) -> bool:
    """Похоже ли это сообщение на мост: владелец и командное слово впереди."""
    if not config.BRIDGE_ENABLED or not is_owner(user_id):
        return False
    body = as_str(text).strip()
    prefix = as_str(config.BRIDGE_PREFIX).strip()
    return bool(prefix) and body.startswith(prefix)


async def on_owner_message(user_id: str, text: str) -> bool:
    """Принимает ли бот это сообщение в мост. True - сообщение обработано.

    Вызывается до обычной логики меню. Если вернулось True, вызывающий
    обработку на этом останавливается: сообщение не должно ещё и попасть в
    диалог студента.
    """
    if not is_owner(user_id):
        return False
    body = as_str(text).strip()
    prefix = as_str(config.BRIDGE_PREFIX).strip()
    if not prefix or not body.startswith(prefix):
        return False
    if not config.BRIDGE_ENABLED:
        await api.send(user_id, OFF_TEXT)
        return True

    # пустое командное слово - это не задание, а «что ты умеешь»
    task = body[len(prefix):].strip()
    if not task:
        await api.send(user_id, _help_text())
        return True
    if task in ("?", "?", "помощь", "помоги"):
        await api.send(user_id, _help_text())
        return True

    await repo.bridge_store.requeue_stale()
    busy = await repo.bridge_store.count_pending() > 1
    await repo.bridge_store.add_message(user_id, task)
    log.info("мост: сообщение владельца принято в очередь (%d символов)", len(task))
    await api.send(user_id, BUSY_TEXT if busy else _accepted_text())
    return True


def _accepted_text() -> str:
    return ("✅ Принял. Как сделаю — пришлю отчёт сюда же.\n"
            "Пока идёт работа, лучше не править те же файлы в консоли.")


def _help_text() -> str:
    return ("Мост: напиши сюда задание с символа «%s» — я возьму его в работу и "
            "отчитаюсь в этом же чате.\n"
            "Писать можно только с твоего номера. Студенты сюда не попадают."
            % as_str(config.BRIDGE_PREFIX))
