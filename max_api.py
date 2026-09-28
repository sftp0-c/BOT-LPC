"""Тонкий клиент MAX Bot API — https://dev.max.ru/docs-api

* база: platform-api2.max.ru (сертификаты НУЦ Минцифры берутся из системного хранилища, MAX_CA_BUNDLE или .certs/max-ca.pem);
* токен передаётся в заголовке Authorization (без «Bearer»);
* лимиты платформы: ≤ 2 сообщений/сек в один диалог, текст ≤ 4000 символов — оба соблюдаются здесь.
"""
import asyncio
import logging
import ssl
import time
from pathlib import Path

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
MAX_ROWS = 30       # строк клавиатуры в одном сообщении: дальше MAX отвечает
                    # errors.maxRows (предел проверен запросами к API)


# MAX показывает подпись кнопки в одну строку и обрезает её многоточием: на
# телефоне половина кнопок превращалась в «Должность изме…». Держим подпись
# короче, чем обрезка, и режем сами - по границе слова, чтобы не отрезать смысл.
#
# Предел зависит от числа кнопок в ряду (замер калибровочным сообщением в MAX:
# в ряду из двух кнопок на компьютере целиком видно 16 символов, а третья
# подпись в 18 символов уже обрезается). Поэтому одна кнопка в ряду получает
# больше места, чем пять.
BUTTON_TEXT = 20

# Знаки, которые MAX рисует в две ячейки. Проверено скриншотом из MAX:
# «⚙️ Сис-админ» - это 12 символов, но 13 ячеек, и подпись режется.
#
# Диапазоны заданы числами, а не строкой «A-Z»: строка дала бы набор из
# символов «A», «-» и «Z», и проверка ширины молча работала бы неправильно.
_WIDE_RANGES = (
    (0x1F000, 0x1FAFF),   # эмодзи и значки
    (0x1F1E6, 0x1F1FF),   # региональные индикаторы флагов
    (0x2600, 0x27BF),     # знаки: ⚙ ✅ ❌ ✍ ✉ и подобные
    (0x2B00, 0x2BFF),     # стрелки и прочее
    (0x1F900, 0x1F9FF),   # дополнительные эмодзи
)
# Знаки, которые не занимают ячейку: вариационные селекторы, точка после
# эмодзи, соединитель нулевой ширины.
_ZERO_WIDTH = frozenset("\uFE0E\uFE0F\u200D\u200B\u20E3")


def is_wide(char: str) -> bool:
    """Занимает ли знак две ячейки."""
    point = ord(char)
    return any(low <= point <= high for low, high in _WIDE_RANGES)


def display_width(text: str) -> int:
    """Ширина строки в ячейках, как её считает интерфейс MAX.

    Обычная буква - одна ячейка, эмодзи - две, знак после эмодзи - ноль.
    Именно эта величина определяет, обрежет подпись мессенджер или нет:
    подпись из 12 символов с эмодзи в начале не влезает туда же, куда
    влезают 12 букв.
    """
    width = 0
    for char in str(text or ""):
        if char in _ZERO_WIDTH:
            continue
        width += 2 if is_wide(char) else 1
    return width


def row_limit(buttons: int) -> int:
    """Сколько ячеек помещается в подписи при таком числе кнопок в ряду.

    Величины в ячейках, а не в символах, и взяты из измерений:

    - одна кнопка: на скриншоте «💬 Обратная связь» - 18 ячеек - помещается
      целиком, значит 20 с запасом;
    - две кнопки: пользователь измерил калибровочным сообщением 16 символов,
      на скриншоте «💰 Бухгалтерия» - 14 ячеек - целиком, берём 16;
    - три и более: на скриншоте «⚙️ Сис-админ» - 12 ячеек - и «👤 Профиль» -
      10 ячеек - обрезались многоточием, поэтому 9 ячеек с запасом.

    Раньше здесь стояли 26/16/14/12 в символах. Из-за эмодзи (две ячейки) и
    слишком щедрых пределов в тесных рядах подписи резались, хотя проверка
    считала их допустимыми.
    """
    if buttons <= 1:
        return 20
    if buttons == 2:
        return 16
    return 9


def short_label(text: str, limit: int = BUTTON_TEXT) -> str:
    """Подпись кнопки в пределах того, что MAX показывает целиком.

    Обрезает по последнему пробелу и ставит многоточие. Если обрезать нечего
    (строка из одного длинного слова) - режем жёстко, но многоточие оставляем.
    Ширина считается в ячейках, а не в символах: эмодзи занимает две.
    """
    value = " ".join(str(text or "").split())
    if display_width(value) <= limit:
        return value
    cut = ""
    for char in value:
        if display_width(cut + char) > limit - 1:
            break
        cut += char
    cut = cut.rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:                      # не отбрасываем полслова ради пары букв
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def btn(text: str, payload: str) -> dict:
    """Callback-кнопка (payload ≤ 1024 символов — усекается при превышении).

    Здесь режется только предельный случай (кнопка одна в ряду), а настоящую
    подгонку под ширину ряда делает fit_keyboard перед отправкой.
    """
    return {"type": "callback", "text": short_label(text), "payload": payload[:MAX_PAYLOAD]}


def link_btn(text: str, url: str) -> dict:
    return {"type": "link", "text": short_label(text), "url": url}


def fit_keyboard(keyboard: list | None) -> list:
    """Подгоняет подписи под ширину ряда: 2 кнопки — 16 символов, 5 — 12.

    Смысл в том, чтобы ни одна подпись не дошла до обрезки многоточием на
    телефоне: в тесном ряду короткая подпись лучше, чем «Должность изме…».
    """
    rows = []
    for row in keyboard or []:
        if not row:
            continue
        limit = row_limit(len(row))
        rows.append([dict(button, text=short_label(button.get("text", ""), limit))
                     if isinstance(button, dict) else button for button in row])
    return rows


def split_keyboard(keyboard: list | None, limit: int = MAX_ROWS) -> list[list[list]]:
    """Режет клавиатуру на части по limit строк.

    MAX не принимает сообщение с 31+ строкой кнопок, поэтому длинные списки
    (35 групп, 60 сотрудников) уходят несколькими сообщениями, а не падают.
    """
    rows = [row for row in (keyboard or []) if row]
    if not rows:
        return []
    if limit < 1:
        limit = 1
    return [rows[i:i + limit] for i in range(0, len(rows), limit)]


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


_LOCAL_CA_BUNDLE = Path(__file__).resolve().parent / ".certs" / "max-ca.pem"


def _ssl_context() -> ssl.SSLContext:
    if config.CA_BUNDLE:
        return ssl.create_default_context(cafile=config.CA_BUNDLE)
    context = ssl.create_default_context()
    if _LOCAL_CA_BUNDLE.is_file():
        context.load_verify_locations(cafile=_LOCAL_CA_BUNDLE)
    return context


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
        """Отправляет сообщение пользователю. keyboard — список рядов кнопок.

        Длинный текст режется на части, длинная клавиатура — на несколько
        сообщений: предел MAX — 30 строк кнопок, иначе API отвечает
        errors.maxRows и событие падает с «Ошибка при обработке».
        """
        user_id = str(user_id)
        # user_id в системе — строка; MAX API ожидает числовой id
        try:
            uid: int | str = int(user_id)
        except (TypeError, ValueError):
            uid = user_id
        parts = split_text(text or "…")
        # подписи подгоняем под ширину ряда до разбиения на сообщения
        chunks = split_keyboard(fit_keyboard(keyboard))
        # первая часть клавиатуры идёт вместе с последней частью текста,
        # остальные - отдельными сообщениями с подписью
        result: dict = {}
        for index, part in enumerate(parts):
            body: dict = {"text": part}
            if chunks and index == len(parts) - 1:
                body["attachments"] = [{"type": "inline_keyboard",
                                        "payload": {"buttons": chunks[0]}}]
            await self._user_wait(user_id)
            result = await self._request("POST", "/messages", params={"user_id": uid}, json=body)
        for index in range(1, len(chunks)):
            caption = ("👆 Кнопки ниже" if index == 1
                       else f"👆 Продолжение: {index} из {len(chunks) - 1}")
            await self._user_wait(user_id)
            body = {"text": caption,
                    "attachments": [{"type": "inline_keyboard", "payload": {"buttons": chunks[index]}}]}
            result = await self._request("POST", "/messages", params={"user_id": uid}, json=body)
        return result

    async def set_commands(self, commands: list[dict]) -> dict:
        """Регистрирует команды бота: из них MAX собирает нижнее меню чата.

        Боты в MAX не умеют рисовать нижние кнопки, но могут управлять меню
        команд - поэтому «кнопки снизу» это команды, а не вложение в сообщение.
        """
        return await self._request("PATCH", "/me/commands", json={"commands": commands})

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
