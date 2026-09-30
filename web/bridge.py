"""web/bridge.py - два адреса для программы моста на компьютере.

Зачем они. Мост живёт на компьютере владельца, бот - в контейнере, и файлов
друг друга они не видят. Общее место одно - база. Поэтому программа ходит в
бот по HTTP: забирает задание и приносит ответ.

Две защиты, обе обязательные:
1. Клиент должен быть с этой машины. Порт 8080 слушает все интерфейсы, и за
   обратным прокси «локальный» клиент может оказаться чужим.
2. Токен. Случайная строка в 32 знака, живёт только в .env. Подобрать нельзя.

Одной защиты мало: первая обходится прокси, вторая - подбором. Вместе они
означают, что ответить по этим адресам может только та программа, которая
лежит на этом же компьютере и знает токен.

Чего здесь нет. Ни входа в панель, ни пароля: это не панель, а служебные
адреса. Они не появляются в меню и не в палитре - иначе про них узнает тот,
кто смотрит исходный код.
"""
import hmac
import json

import repository as repo
from fastapi import HTTPException, Request

from handlers.common import api, log
from utils import as_str

import config
from .router import router

BRIDGE = repo.bridge_store

DENIED = "мост недоступен"
NOTHING = {"empty": True}


def _client_host(request: Request) -> str:
    """Адрес, с которого пришёл запрос, без доверия заголовкам.

    X-Forwarded-For не смотрим намеренно: за обратным прокси его пишет тот,
    кто снаружи, и подделать его можно. Смотрим на реальный сокет.
    """
    client = request.client
    return as_str(client.host if client else "")


def _authorized(request: Request) -> bool:
    if not config.BRIDGE_ENABLED:
        return False
    if _client_host(request) not in config.BRIDGE_LOCAL_HOSTS:
        return False
    given = request.headers.get("x-bridge-token", "")
    return hmac.compare_digest(as_str(given), as_str(config.BRIDGE_TOKEN))


async def _guard(request: Request) -> None:
    if not _authorized(request):
        raise HTTPException(status_code=403, detail=DENIED)


@router.get("/bridge/next")
async def bridge_next(request: Request):
    """Отдаёт следующее задание и сразу помечает его взятым.

    Помечает - здесь же, а не отдельным вызовом: иначе два опроса подряд заберут
    одно задание, и владелец получит два одинаковых отчёта.
    """
    await _guard(request)
    await BRIDGE.requeue_stale()
    for row in await BRIDGE.pending(3):
        if not await BRIDGE.take(row["id"]):
            continue          # уже забрали, пробуем следующее
        return {"id": int(row["id"]), "user_id": as_str(row["user_id"]),
                "text": as_str(row["text"])}
    return NOTHING


@router.post("/bridge/{message_id}/answer")
async def bridge_answer(request: Request, message_id: int):
    """Принимает мой ответ и отправляет его владельцу в MAX."""
    await _guard(request)
    payload = await _request_json(request)
    answer = as_str(payload.get("answer")).strip()
    if not answer:
        raise HTTPException(status_code=422, detail="Пустой ответ")
    row = await BRIDGE.get_message(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if not await BRIDGE.finish(message_id, answer):
        raise HTTPException(status_code=409, detail="Сообщение уже закрыто")
    await api.send(as_str(row["user_id"]), answer)
    log.info("мост: ответ на сообщение %s отправлен владельцу", message_id)
    return {"ok": True}


@router.post("/bridge/{message_id}/fail")
async def bridge_fail(request: Request, message_id: int):
    """Сообщение о неудаче: владельцу тоже нужно знать, что задание не сделано."""
    await _guard(request)
    payload = await _request_json(request)
    reason = as_str(payload.get("error")).strip() or "неизвестная ошибка"
    row = await BRIDGE.get_message(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if not await BRIDGE.fail(message_id, reason):
        raise HTTPException(status_code=409, detail="Сообщение уже закрыто")
    await api.send(as_str(row["user_id"]), f"❌ Не получилось сделать: {reason}")
    log.info("мост: задание %s не выполнено: %s", message_id, reason)
    return {"ok": True}


@router.post("/bridge/{message_id}/defer")
async def bridge_defer(request: Request, message_id: int):
    """Возвращает задание в очередь и, если есть, объясняет владельцу почему.

    Мосту это нужно, когда он взял задание, но проект держит консоль: ждать
    надо не полтора часа, а до конца работы в консоли. Задание при этом не
    теряется и не считается невыполненным - оно просто снова попадёт в очередь.
    """
    await _guard(request)
    payload = await _request_json(request)
    row = await BRIDGE.get_message(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    if not await BRIDGE.defer(message_id):
        raise HTTPException(status_code=409, detail="Сообщение уже закрыто или не в работе")
    note = as_str(payload.get("text")).strip()
    if note:
        await api.send(as_str(row["user_id"]), note)
    log.info("мост: задание %s возвращено в очереду%s",
             message_id, " с пояснением" if note else "")
    return {"ok": True}


@router.get("/bridge/status")
async def bridge_status(request: Request):
    """Сколько ждёт и что последнее делалось. Программа пишет это в журнал."""
    await _guard(request)
    return {"waiting": await BRIDGE.count_pending(),
            "last": [as_str(r["answered_at"]) for r in await BRIDGE.last_answer(3)]}


async def _request_json(request: Request) -> dict:
    """Читает тело запроса как объект: мусор должен давать 422, а не 500."""
    try:
        data = json.loads(await request.body())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Не разобран JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Ожидался объект")
    return data
