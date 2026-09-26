"""Колледжный бот для мессенджера MAX — точка входа.

Запуск: uvicorn bot:app --host 0.0.0.0 --port 8080
Режим определяется настройкой MAX_WEBHOOK_URL: задан — webhook, пусто — long polling.

Структура:
* handlers/ — обработчики кнопок и состояний (регистрируются в handlers.registry);
* repository.py — SQL-запросы; database.py — схема и доступ к SQLite;
* updates.py — разбор «сырых» обновлений MAX API; max_api.py — клиент API;
* webpanel.py — веб-панель сис-админа (/panel);
* config.py — настройки из .env / переменных окружения.
"""
import asyncio
import hmac
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import config
import database as db
import repository as repo
from handlers import admin, broadcast, menus, tickets  # noqa: F401  — регистрация обработчиков при импорте
from handlers.common import api, log, notify, pending_tasks, spawn
from handlers.registry import CALLBACKS
from updates import callback_id, callback_payload, is_dialog, message_text, profile_of, sender_id, update_key
from utils import UserLocks, as_str
from webpanel import router as panel_router
import webpanel


def setup_logging() -> None:
    """Настройка журнала: консоль + rotating-файл (его читает панель и команда /logs)."""
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    if config.LOG_FILE and not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        try:
            path = Path(config.LOG_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:  # нет прав на каталог — работаем только с консоли
            root.warning("файл журнала %s недоступен: %s", config.LOG_FILE, exc)


setup_logging()
logging.getLogger("httpx").setLevel(logging.WARNING)  # не пишем в лог каждый HTTP-запрос

_locks = UserLocks()  # один пользователь — один обработчик за раз; ключи чистятся, память не растёт
SHUTDOWN_TIMEOUT = 60  # сколько ждём завершения фоновых задач при остановке (docker stop_grace_period = 70s)

# Кнопки, которые продолжают диалог выдачи прав: нажатие подсказки не должно
# стирать список сотрудников, ждущий категорию или должность.
STATE_KEEPING_CALLBACKS = frozenset({"bcgo", "mph", "mkc", "sfbc"})


async def on_callback(x: str, payload: str):
    name, _, arg = payload.partition(":")
    handler = CALLBACKS.get(name)
    if not handler:
        return log.warning("неизвестный callback %r от %s", payload, x)
    if name not in STATE_KEEPING_CALLBACKS:
        # нажатие любой кнопки прерывает незавершённый ввод
        await db.clear_state(x)
    return await handler(x, arg)


async def remember_contact(u: dict, x: str, kind: str) -> None:
    """Запоминает пользователя в реестре: ник и имя из профиля MAX, текст последнего сообщения.

    Так панель видит всех, кто писал боту, даже если человек так и не зарегистрировался.
    Сбой записи не должен мешать ответить: поэтому исключение только логируется.
    """
    if kind not in ("message_created", "message_callback", "bot_started"):
        return
    text = message_text(u) if kind == "message_created" else ""
    profile = profile_of(u)
    try:
        await repo.touch_contact(x, profile["username"], profile["display_name"], text)
    except Exception as exc:
        log.warning("не удалось обновить контакт %s: %s", x, exc)


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
            await remember_contact(u, x, kind)
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
    except sqlite3.OperationalError as exc:
        # Чаще всего это удалённая вручную таблица: отвечаем прямо, а не «попробуйте ещё раз»
        log.error("база неполна: %s", exc)
        if "no such table" in str(exc) or "no such column" in str(exc):
            missing = await db.missing_objects()
            log.error("в базе не хватает: %s", ", ".join(missing) or exc)
            if x:
                await notify(x, "⚠️ В базе бота не хватает данных. Сообщите администратору: "
                                "в панели есть кнопка «Восстановить схему».")
        elif x:
            await notify(x, "⚠️ База данных недоступна. Сообщите администратору.")
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


async def backup_loop():
    """Резервные копии базы по расписанию: снимок работающей базы, старые удаляются.

    Нужны, чтобы потеря данных (удалённая таблица, сбой диска) не зависела от того,
    вспомнил ли сис-админ нажать кнопку. Ошибка копирования не должна ронять бота:
    сообщаем в журнал и сис-админам, ждём следующего круга.
    """
    period = max(1, config.BACKUP_EVERY_HOURS) * 3600
    warned = 0
    while True:
        await asyncio.sleep(period)
        try:
            path = await db.backup_to()
            kept = len(db.list_backups())
            log.info("автокопия базы: %s (%s, хранится %s)", os.path.basename(path),
                     webpanel_human_size(os.path.getsize(path)), kept)
            warned = 0
        except Exception as exc:  # noqa: BLE001 — копирование не критично, но молчать нельзя
            log.error("автокопия базы не удалась: %s", exc)
            warned += 1
            if warned == 1:  # не спамим: одно сообщение о первой неудаче, потом только журнал
                for admin_id in {str(value) for value in config.SYSADMIN_IDS} | await _db_sysadmins():
                    await notify(admin_id, "⚠️ Не удалось сделать резервную копию базы. "
                                           "Подробности в журнале; вкладка «База данных» в панели.")
        finally:
            await repo.log_action("bot", "автокопия базы", "ошибка" if warned else "создана копия")


async def _db_sysadmins() -> set:
    try:
        return {as_str(row["user_id"]) for row in await db.many(
            "SELECT user_id FROM admins WHERE role_type IN ('sysadmin','superadmin')")}
    except Exception:  # noqa: BLE001 — уведомление о копии не стоит падения из-за базы
        return set()


def webpanel_human_size(value) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


# ───────────────────────── FastAPI ─────────────────────────


async def shutdown(poller, maintenance=None):
    """Остановка фоновых задач и HTTP-клиента. Выполняется всегда: и при штатном выходе, и при сбое старта."""
    try:
        if poller:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        # даём фоновым задачам (например, незавершённой рассылке) доработать,
        # иначе итог и запись в историю рассылок потеряются при рестарте/деплое
        pending = [t for t in pending_tasks() if not t.done()]
        if pending:
            log.info("Ожидание %d фоновых задач перед остановкой…", len(pending))
            done, still = await asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT)
            for t in still:
                t.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
    finally:
        try:
            await api.close()
        except Exception as exc:  # сбой закрытия клиента не должен замаскировать причину остановки
            log.warning("не удалось закрыть API-клиент: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = None
    maintenance = None
    try:
        for warning in config.validate():
            log.warning(warning)
        await db.init_db()
        if config.BACKUP_EVERY_HOURS:
            maintenance = spawn(backup_loop())
            log.info("Автокопия базы: раз в %s ч, хранится %s копий",
                     config.BACKUP_EVERY_HOURS, config.BACKUP_KEEP)
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
    finally:  # try/finally обязателен: при сбое старта клиент тоже должен закрыться
        if maintenance:
            maintenance.cancel()
        await shutdown(poller)


app = FastAPI(title="College MAX bot", lifespan=lifespan)
app.include_router(panel_router)


@app.exception_handler(sqlite3.OperationalError)
async def broken_schema(request: Request, exc: sqlite3.OperationalError):
    """Удалённая таблица или потерянная колонка: вместо 500 со стектрейсом — понятная страница.

    Причина обычно одна: в базе нет объекта, который есть в схеме в коде
    (например, таблицу удалили вручную). Схему восстанавливает кнопка на
    странице и вкладка «База данных» панели.
    """
    text = str(exc)
    if "no such table" not in text and "no such column" not in text:
        log.exception("ошибка SQLite при обработке %s", request.url.path)
        return JSONResponse({"error": "database_error", "detail": text}, status_code=500)
    missing = await db.missing_objects() if os.path.exists(config.DATABASE_PATH) else []
    detail = ", ".join(missing) or text
    log.error("панель: в базе не хватает объектов: %s", detail)
    if request.url.path.startswith("/panel/api/") or request.url.path.startswith("/api/"):
        return JSONResponse({"error": "broken_schema", "missing": missing}, status_code=503)
    return webpanel.schema_broken_page(request, detail, missing)



@app.get("/health")
async def health():
    """Состояние сервиса: база читается и её схема соответствует коду.

    missing_schema пустой — схема в порядке. Проверка дешёвая: список таблиц и
    несколько PRAGMA table_info, без обхода данных.
    """
    await db.one("SELECT 1")
    missing = await db.missing_objects()
    return {
        "ok": not missing,
        "platform": "MAX",
        "mode": "webhook" if config.WEBHOOK_URL else "polling",
        "missing_schema": missing,
    }


@app.post("/webhook")
async def webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
    if not config.WEBHOOK_URL:  # в режиме long polling принимать входящие запросы нельзя — их можно подделать
        raise HTTPException(status_code=404)
    if not hmac.compare_digest((x_max_bot_api_secret or "").encode(), config.WEBHOOK_SECRET.encode()):
        raise HTTPException(status_code=401, detail="bad secret")
    spawn(process(await request.json()))
    return {"ok": True}
