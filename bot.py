"""Колледжный бот для мессенджера MAX — точка входа.

Запуск: uvicorn bot:app --host 0.0.0.0 --port 8080
Режим определяется настройкой MAX_WEBHOOK_URL: задан — webhook, пусто — long polling.

Структура:
* handlers/ — обработчики кнопок и состояний (регистрируются в handlers.registry);
* repository.py — SQL-запросы; database.py — схема и доступ к SQLite;
* updates.py — разбор «сырых» обновлений MAX API; max_api.py — клиент API;
* config.py — настройки из .env / переменных окружения.
"""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

import config
import database as db
from handlers import admin, broadcast, menus, tickets  # noqa: F401  — регистрация обработчиков при импорте
from handlers.common import api, log, notify, pending_tasks, spawn
from handlers.registry import CALLBACKS, STATES
from updates import callback_id, callback_payload, is_dialog, message_text, sender_id, update_key
from utils import UserLocks

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не пишем в лог каждый HTTP-запрос

_locks = UserLocks()  # один пользователь — один обработчик за раз; ключи чистятся, память не растёт


async def on_callback(x: str, payload: str):
    name, _, arg = payload.partition(":")
    handler = CALLBACKS.get(name)
    if not handler:
        return log.warning("неизвестный callback %r от %s", payload, x)
    if name != "bcgo":  # нажатие любой кнопки прерывает незавершённый ввод
        await db.clear_state(x)
    return await handler(x, arg)


async def process(u: dict):
    key = update_key(u)
    if key:
        try:
            if not await db.mark_processed(key):
                log.debug("Пропускаю повторную доставку события %s", key)
                return
        except Exception as exc:  # сбой базы не должен ронять обработку события
            log.warning("не удалось отметить событие %s: %s", key, exc)
    x = ""
    try:
        kind = u.get("update_type")
        x = sender_id(u)
        if not x:
            return
        async with _locks.get(x):
            if kind == "bot_started":
                await menus.start(x)
            elif kind == "message_created":
                if not is_dialog(u):
                    return  # группы и каналы не обслуживаем
                text = message_text(u)
                if not text:
                    return await api.send(x, "Пока я понимаю только текстовые сообщения.")
                await menus.on_message(x, text)
            elif kind == "message_callback":
                cid = callback_id(u)
                if cid:
                    try:
                        await api.answer(cid)
                    except Exception as exc:
                        log.debug("answer не удался: %s", exc)
                await on_callback(x, callback_payload(u))
    except Exception:
        log.exception("ошибка обработки обновления")
        if x:
            await notify(x, "⚠️ Что-то пошло не так. Попробуйте ещё раз или отправьте /start.")
    finally:
        if x:
            _locks.release(x)


async def poll():
    marker = None
    while True:
        try:
            data = await api.updates(marker)
            new_marker = data.get("marker")
            if new_marker is not None:  # без свежего маркера повторяем старый — иначе MAX отдаст события заново
                marker = new_marker
            for u in data.get("updates", []):
                spawn(process(u))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("long polling: %s", exc)
            await asyncio.sleep(5)


# ───────────────────────── FastAPI ─────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    for warning in config.validate():
        log.warning(warning)
    await db.init_db()
    poller = None
    if config.WEBHOOK_URL:
        try:  # удаляем старую подписку с этим URL, чтобы при рестартах не было двойной доставки
            for sub in await api.subscriptions():
                if sub.get("url") == config.WEBHOOK_URL:
                    await api.unsubscribe(config.WEBHOOK_URL)
                    log.info("Удалена прежняя подписка %s (защита от дублей событий)", config.WEBHOOK_URL)
                    break
        except Exception as exc:
            log.warning("не удалось проверить/очистить подписки: %s", exc)
        await api.subscribe(config.WEBHOOK_URL, config.WEBHOOK_SECRET)
        log.info("Webhook зарегистрирован: %s", config.WEBHOOK_URL)
    else:
        try:
            if await api.subscriptions():
                log.warning("У бота есть webhook-подписка: long polling не получит события, пока её не удалить (DELETE /subscriptions).")
        except Exception as exc:
            log.warning("не удалось проверить подписки: %s", exc)
        poller = spawn(poll())
        log.info("Запущен long polling")
    yield
    if poller:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    # даём фоновым задачам (например, незавершённой рассылке) доработать,
    # иначе итог и запись в историю рассылок потеряются при рестарте/деплое
    pending = [t for t in pending_tasks() if not t.done()]
    if pending:
        log.info("Ожидание %d фоновых задач перед остановкой…", len(pending))
        done, still = await asyncio.wait(pending, timeout=60)
        for t in still:
            t.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
    await api.close()


app = FastAPI(title="College MAX bot", lifespan=lifespan)


@app.get("/health")
async def health():
    await db.one("SELECT 1")
    return {"ok": True, "platform": "MAX", "mode": "webhook" if config.WEBHOOK_URL else "polling"}


@app.post("/webhook")
async def webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
    if not config.WEBHOOK_URL:  # в режиме long polling принимать входящие запросы нельзя — их можно подделать
        raise HTTPException(status_code=404)
    if not hmac.compare_digest((x_max_bot_api_secret or "").encode(), config.WEBHOOK_SECRET.encode()):
        raise HTTPException(status_code=401, detail="bad secret")
    spawn(process(await request.json()))
    return {"ok": True}
