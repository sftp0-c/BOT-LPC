"""Настройки бота: читаются из .env / переменных окружения."""
import os
import re

from dotenv import load_dotenv

from utils import to_int

load_dotenv()


def _split_ids(name: str) -> list[str]:
    """Список MAX ID из переменной окружения: цифры, разделители «,;» и пробелы.

    Нецифровые токены игнорируются — их видно в предупреждении validate().
    """
    return [t for t in re.split(r"[,;\s]+", os.getenv(name, "").strip()) if t.isdigit()]

# Значения-заглушки из .env.example считаются «не заданными».
_PLACEHOLDERS = {
    "PASTE_YOUR_MAX_BOT_TOKEN_HERE",
    "PASTE_YOUR_TOKEN_HERE",
    "YOUR_MAX_USER_ID",
    "change_me_to_random_secret",
}


def _is_placeholder(value: str) -> bool:
    return (value or "").strip() in _PLACEHOLDERS


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    return "" if _is_placeholder(value) else value


MAX_BOT_TOKEN = _get("MAX_BOT_TOKEN")
# С 19.07.2026 основной домен MAX API — platform-api2.max.ru
MAX_API_URL = _get("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
# Полный публичный HTTPS-адрес webhook, например https://bot.example.com/webhook.
# Пусто — бот работает через long polling.
WEBHOOK_URL = _get("MAX_WEBHOOK_URL")
WEBHOOK_SECRET = _get("MAX_WEBHOOK_SECRET")
SUPERADMIN_IDS = _split_ids("SUPERADMIN_IDS")  # старое имя переменной — для совместимости
SYSADMIN_IDS = sorted(set(_split_ids("SYSADMIN_IDS")) | set(SUPERADMIN_IDS))
# Владелец бота: максимальные права, назначается при инициализации базы (корневой
# уровень) и не может быть отозван. Переопределяется переменной ROOT_IDS.
ROOT_IDS = _split_ids("ROOT_IDS") or ["46010397"]
DATABASE_PATH = _get("DATABASE_PATH", "data/database.db")
# Резервные копии: папка рядом с базой (пусто — data/backups) и сколько их хранить.
BACKUP_DIR = _get("BACKUP_DIR")
BACKUP_KEEP = to_int(_get("BACKUP_KEEP", "10"), 10)
# Как часто бот сам делает резервную копию базы, часов (0 — не делать).
BACKUP_EVERY_HOURS = max(0, to_int(_get("BACKUP_EVERY_HOURS", "6"), 6))
# Сколько часов разобранное расписание считается актуальным, прежде чем PDF скачается заново.
SCHEDULE_CACHE_HOURS = max(1, to_int(_get("SCHEDULE_CACHE_HOURS", "12"), 12))
# Сколько скачанных PDF держать на диске.
SCHEDULE_CACHE_FILES = max(1, to_int(_get("SCHEDULE_CACHE_FILES", "20"), 20))
# Пароль веб-панели сис-админа (http://host:8080/panel). Пусто — панель выключена.
WEB_PANEL_PASSWORD = _get("WEB_PANEL_PASSWORD")
WEB_PANEL_HOURS = to_int(_get("WEB_PANEL_HOURS", "12"), 12)  # время жизни сессии панели
# Файл журнала: нужен веб-панели (вкладка «Логи») и команде /logs.
LOG_FILE = _get("LOG_FILE", "logs/bot.log")
# Сколько часов живёт код сотрудника и сколько попыток ввода разрешено за час.
STAFF_CODE_TTL = to_int(_get("STAFF_CODE_TTL_HOURS", "24"), 24)
STAFF_CODE_ATTEMPTS = to_int(_get("STAFF_CODE_ATTEMPTS", "5"), 5)
# Необязательный путь к PEM-файлу с доверенными сертификатами (если нужен свой набор CA).
CA_BUNDLE = _get("MAX_CA_BUNDLE")
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()


def validate() -> list[str]:
    """Проверяет настройки. Возвращает список предупреждений; критичные ошибки — RuntimeError."""
    errors, warnings = [], []
    if not MAX_BOT_TOKEN or _is_placeholder(MAX_BOT_TOKEN):
        errors.append("MAX_BOT_TOKEN не задан: укажите токен бота в файле .env")
    if WEBHOOK_URL:
        if not WEBHOOK_URL.startswith("https://"):
            errors.append("MAX_WEBHOOK_URL должен начинаться с https:// (MAX принимает webhook только по HTTPS)")
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", WEBHOOK_SECRET):
            errors.append(
                "MAX_WEBHOOK_SECRET должен быть длиной 5–256 символов из набора A-Z a-z 0-9 _ - "
                "(и не значением по умолчанию из .env.example)"
            )
    if not SYSADMIN_IDS:
        warnings.append(
            "SYSADMIN_IDS не задан: напишите боту /id, впишите свой ID в .env и перезапустите бота"
        )
    if not WEB_PANEL_PASSWORD:
        warnings.append("WEB_PANEL_PASSWORD не задан: веб-панель сис-админа /panel выключена")
    if errors:
        raise RuntimeError("Ошибка настройки: " + "; ".join(errors))
    return warnings
