"""Состояние туннеля Cloudflare: только чтение, никакой сети.

Адрес выдаёт контейнер `tunnel` (сервис в docker-compose.yml). Он же кладёт
адрес в файл, который живёт в общем томе `bot_data`:
    у бота   том смонтирован в /app/data  -> /app/data/tunnel_url.txt
    у туннеля том смонтирован в /data    -> /data/tunnel_url.txt
То есть это один и тот же файл, просто под разными именами.

Путь переопределяется переменной окружения TUNNEL_URL_FILE - на хосте можно
указать `TUNNEL_URL_FILE=data/tunnel_url.txt` и читать адрес без контейнера.

Файл обновляется писателем каждые ~30 секунд (даже если адрес не изменился) -
по времени его изменения видно, жив ли писатель. Молчит дольше
STALE_AFTER_SECONDS - значит контейнер `tunnel` упал, а адрес в файле уже не
работает (см. stale()).

Все функции безопасны при отсутствии базы и файла: наружу исключений не
бросают, их можно звать из панели и обработчиков без try/except.
"""

import ipaddress
import os
import re
import time
from pathlib import Path

# Куда смотрит бот. Внутри контейнера это /app/data/tunnel_url.txt.
URL_FILE = Path(os.getenv("TUNNEL_URL_FILE") or "/app/data/tunnel_url.txt")

# Имя настройки в базе: значение можно переключать из панели/бота.
SETTING_KEY = "tunnel_enabled"

# Сколько секунд молчания писателя считаем поломкой туннеля.
STALE_AFTER_SECONDS = 30 * 60

# Быстрый туннель даёт https://<случайные-слова>.trycloudflare.com, именованный
# со своим доменом - любой https://домен. Проверка намеренно строгая: мусор в
# файле (обрыв записи, лог, пустая строка) не должен попасть в ссылки.
_URL_RE = re.compile(r"^https://[^\s/?#:]+\.[^\s/?#:]+$")

_TRUTHY = {"1", "true", "yes", "on", "y", "t", "да", "вкл", "вкл."}
_FALSY = {"0", "false", "no", "off", "n", "f", "нет", "выкл", "выкл."}

# Последнее прочитанное из базы значение. Прямо из базы читать нельзя: функции
# синхронные, а db.get_setting - async, и вызывают их из шаблонов панели.
_db_enabled: bool | None = None


def _path() -> Path:
    """Путь к файлу адреса. Переменная окружения важнее константы: её могли
    прочитать из .env уже после импорта модуля (load_dotenv в config)."""
    return Path(os.getenv("TUNNEL_URL_FILE") or URL_FILE)


def _flag(raw: str | None, default: bool) -> bool:
    """Разбор значения настройки: 1/да/true - включено, 0/нет/false - выключено.

    Пустая строка - это «не задано» (настройки в базе может не быть вовсе),
    поэтому остаётся значение по умолчанию, а не «выключено».
    """
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if not value:
        return default
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


def current_url() -> str:
    """Текущий публичный адрес или "" - если файла нет или адрес не похож на https."""
    try:
        raw = _path().read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    return raw if _URL_RE.match(raw) else ""


def is_enabled() -> bool:
    """Включён ли туннель. Переменная окружения TUNNEL_ENABLED главнее базы,
    последнее прочитанное значение базы - второе, иначе туннель включён."""
    raw = os.getenv("TUNNEL_ENABLED")
    if raw is not None and raw.strip():
        return _flag(raw, True)
    if _db_enabled is not None:
        return _db_enabled
    return True


async def refresh_enabled() -> bool:
    """Перечитывает настройку tunnel_enabled из базы и запоминает её.

    Самостоятельный вызов не нужен: значение кэшируется, и is_enabled() отдаёт
    последнее прочитанное. Нужен там, где настройку меняли (панель, команда бота).
    """
    global _db_enabled
    try:
        import database as db  # ленивый импорт: модуль не тянет базу при чтении файла

        raw = await db.get_setting(SETTING_KEY, "")
    except Exception:
        return is_enabled()
    _db_enabled = _flag(raw, True)
    return _db_enabled


def url_age_seconds() -> int:
    """Сколько секунд назад обновляли файл адреса. -1 - файла нет."""
    try:
        return max(0, int(time.time() - _path().stat().st_mtime))
    except Exception:
        return -1


def stale() -> bool:
    """Похоже, что туннель переподключился, а адрес не обновился.

    Писатель (контейнер tunnel) трогает файл каждые ~30 секунд, поэтому
    тишина дольше получаса - это не «старый адрес», а упавший писатель:
    показывать такой адрес в ссылках нельзя.
    """
    age = url_age_seconds()
    return 0 <= age > STALE_AFTER_SECONDS


def is_private_url(value: str) -> bool:
    """Адрес, доступный только внутри сети: локальный IP или имя без домена."""
    text = (value or "").strip()
    if not text:
        return True
    if text.startswith(("http://localhost", "http://127.", "http://0.0.0.0")):
        return True
    host = text.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    if host.startswith("[") and "]" in host:          # [::1]:8080
        host = host[1:host.index("]")]
    else:
        host = host.split(":", 1)[0]
    if not host:
        return True
    if host in {"localhost", "bot"} or host.endswith((".local", ".internal")):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        # имя без точки - локальное (например, college-panel)
        return "." not in host


def public_url() -> str:
    """Адрес для ссылок-приглашений.

    Порядок выбора неочевиден, поэтому по порядку:

    1. `PUBLIC_URL` из `.env`, если он публичный. Так настраивают именованный
       туннель с доменом: он меняется редко, вводить вручную удобнее, чем
       ловить новый случайный адрес.
    2. Адрес работающего туннеля. Раньше здесь был второй шаг, но с
       `PUBLIC_URL=http://192.168.0.102:8080` в `.env` приглашения уходили
       на адрес, который снаружи колледжа не работает, а туннель поднимался
       рядом и просто не использовался.
    3. `PUBLIC_URL` как есть, даже если он внутренний: лучше ссылка, которую
       откроют с телефона в сети Wi-Fi, чем пустота.
    """
    configured = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
    url = current_url()
    tunnel_works = bool(url) and not stale()
    if configured and not is_private_url(configured):
        return configured
    if tunnel_works:
        return url
    return configured


def status() -> dict:
    """Готовое состояние для панели: что показать сис-админу."""
    url = current_url()
    age = url_age_seconds()
    return {
        "enabled": is_enabled(),
        "url": url,
        "public_url": public_url(),
        "age_seconds": age,
        "stale": stale(),
        # Адрес можно показывать только пока контейнер им владеет.
        "usable": bool(url) and not stale(),
    }
