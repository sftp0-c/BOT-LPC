"""Тонкий клиент MAX Bot API — https://dev.max.ru/docs-api

* база: platform-api2.max.ru (сертификат НУЦ Минцифры должен быть в системном хранилище — Dockerfile это делает);
* токен передаётся в заголовке Authorization (без «Bearer»);
* лимиты платформы: ≤ 2 сообщений/сек в один диалог, текст ≤ 4000 символов — оба соблюдаются здесь.
"""
import asyncio
import logging
import ssl
import time

import httpx

import config

log = logging.getLogger("max_api")

MAX_TEXT = 3900  # лимит API — 4000 символов, оставляем запас
GLOBAL_RPS = 20  # общий предел запросов в секунду от бота
PER_USER_GAP = 0.55  # пауза между сообщениями одному пользователю, сек
UPDATE_TYPES = ["message_created", "message_callback", "bot_started"]


class MaxAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"MAX API {status}: {body[:300]}")
        self.status = status
        self.body = body


MAX_PAYLOAD = 1024  # предельная длина callback-payload по спецификации MAX API


def btn(text: str, payload: str) -> dict:
    """Callback-кнопка (payload ≤ 1024 символов — усекается при превышении)."""
    return {"type": "callback", "text": text[:128], "payload": payload[:MAX_PAYLOAD]}


def link_btn(text: str, url: str) -> dict:
    return {"type": "link", "text": text[:128], "url": url}


def split_text(text: str, size: int = MAX_TEXT) -> list[str]:
    """Режет длинный текст на части, по возможности по переносам строк."""
    text = text or ""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def _ssl_context() -> ssl.SSLContext:
    if config.CA_BUNDLE:
        return ssl.create_default_context(cafile=config.CA_BUNDLE)
    return ssl.create_default_context()  # системное хранилище (в Docker — с сертификатами Минцифры)


class MaxAPI:
    def __init__(self, token: str | None = None, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._base_url = base_url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._next_slot = 0.0
        self._user_next: dict[str, float] = {}

    # ── служебное ────────────────────────────────────────────────────────────
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs = {"transport": self._transport} if self._transport else {"verify": _ssl_context()}
            self._client = httpx.AsyncClient(
                base_url=(self._base_url or config.MAX_API_URL),
                headers={"Authorization": self._token or config.MAX_BOT_TOKEN},
                timeout=httpx.Timeout(20.0, connect=10.0),
                **kwargs,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _global_wait(self) -> None:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + 1 / GLOBAL_RPS
        if slot > now:
            await asyncio.sleep(slot - now)

    async def _user_wait(self, user_id: str) -> None:
        now = time.monotonic()
        slot = max(now, self._user_next.get(user_id, 0.0))
        self._user_next[user_id] = slot + PER_USER_GAP
        if len(self._user_next) > 5000:
            self._user_next = {k: v for k, v in self._user_next.items() if v > now}
        if slot > now:
            await asyncio.sleep(slot - now)

    # Идемпотентные методы: их можно безопасно повторить после обрыва соединения.
    # POST (отправка/подтверждение) не повторяем: запрос мог быть применён
    # сервером до обрыва — повтор создал бы пользователю дубликат сообщения.
    _SAFE_METHODS = {"GET", "DELETE"}

    async def _request(self, method: str, path: str, *, params=None, json=None, timeout=None, retry_5xx=False):
        safe = method in self._SAFE_METHODS
        last: Exception | None = None
        for attempt in range(1, 4):
            await self._global_wait()
            try:
                resp = await self._http().request(method, path, params=params, json=json, timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                if not safe or attempt == 3:
                    break
                log.warning("MAX API недоступен (%s), попытка %s/3", exc, attempt)
                await asyncio.sleep(attempt)
                continue
            if resp.status_code == 429 or (retry_5xx and resp.status_code >= 500):
                last = MaxAPIError(resp.status_code, resp.text)
                try:
                    delay = float(resp.headers.get("Retry-After", attempt))
                except ValueError:
                    delay = attempt
                await asyncio.sleep(min(delay, 10))
                continue
            if resp.status_code >= 400:
                raise MaxAPIError(resp.status_code, resp.text)
            return resp.json() if resp.content else {}
        raise last or MaxAPIError(0, "unknown error")

    # ── сообщения ────────────────────────────────────────────────────────────
    async def send(self, user_id, text: str, keyboard: list | None = None) -> dict:
        """Отправляет сообщение пользователю. keyboard — список рядов кнопок."""
        user_id = str(user_id)
        parts = split_text(text or "…")
        result: dict = {}
        for i, part in enumerate(parts):
            body: dict = {"text": part}
            if keyboard and i == len(parts) - 1:
                body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]
            await self._user_wait(user_id)
            # user_id в системе — строка; MAX API ожидает числовой id
            try:
                uid: int | str = int(user_id)
            except (TypeError, ValueError):
                uid = user_id
            result = await self._request("POST", "/messages", params={"user_id": uid}, json=body)
        return result

    async def answer(self, callback_id: str, notification: str | None = None) -> None:
        """Подтверждает нажатие кнопки (убирает «крутилку» на кнопке)."""
        await self._request(
            "POST", "/answers", params={"callback_id": callback_id}, json={"notification": notification} if notification else {}
        )

    # ── получение обновлений ────────────────────────────────────────────────
    async def updates(self, marker: int | None = None, timeout: int = 30, limit: int = 100) -> dict:
        params: dict = {"timeout": timeout, "limit": limit, "types": ",".join(UPDATE_TYPES)}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params, timeout=timeout + 15, retry_5xx=True)

    async def subscribe(self, url: str, secret: str) -> dict:
        body = {"url": url, "update_types": UPDATE_TYPES, "secret": secret}
        return await self._request("POST", "/subscriptions", json=body)

    async def subscriptions(self) -> list:
        data = await self._request("GET", "/subscriptions", retry_5xx=True)
        return data.get("subscriptions", [])

    async def unsubscribe(self, url: str) -> dict:
        return await self._request("DELETE", "/subscriptions", params={"url": url})

    async def me(self) -> dict:
        return await self._request("GET", "/me", retry_5xx=True)
