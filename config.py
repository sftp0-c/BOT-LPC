"""Настройки бота: читаются из .env / переменных окружения."""
import os
import re

from dotenv import load_dotenv

load_dotenv()


def _split_ids(name: str) -> list[str]:
    """Список MAX ID из переменной окружения: цифры, разделители «,;» и пробелы.

    Нецифровые токены игнорируются — их видно в предупреждении validate().
    """
    return [t for t in re.split(r"[,;\s]+", os.getenv(name, "").strip()) if t.isdigit()]

# Значения-заглушки из .env.example считаются «не заданными».
_PLACEHOLDERS = {"PASTE_YOUR_MAX_BOT_TOKEN_HERE", "YOUR_MAX_USER_ID", "change_me_to_random_secret"}


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    return "" if value in _PLACEHOLDERS else value


MAX_BOT_TOKEN = _get("MAX_BOT_TOKEN")
# С 19.07.2026 основной домен MAX API — platform-api2.max.ru
MAX_API_URL = _get("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
# Полный публичный HTTPS-адрес webhook, например https://bot.example.com/webhook.
# Пусто — бот работает через long polling.
WEBHOOK_URL = _get("MAX_WEBHOOK_URL")
WEBHOOK_SECRET = _get("MAX_WEBHOOK_SECRET")
SUPERADMIN_IDS = _split_ids("SUPERADMIN_IDS")  # старое имя переменной — для совместимости
SYSADMIN_IDS = sorted(set(_split_ids("SYSADMIN_IDS")) | set(SUPERADMIN_IDS))
DATABASE_PATH = _get("DATABASE_PATH", "data/database.db")
# Необязательный путь к PEM-файлу с доверенными сертификатами (если нужен свой набор CA).
CA_BUNDLE = _get("MAX_CA_BUNDLE")
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()


def validate() -> list[str]:
    """Проверяет настройки. Возвращает список предупреждений; критичные ошибки — RuntimeError."""
    errors, warnings = [], []
    if not MAX_BOT_TOKEN:
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
    if errors:
        raise RuntimeError("Ошибка настройки: " + "; ".join(errors))
    return warnings
