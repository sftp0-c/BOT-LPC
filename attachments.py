"""Файлы в обращениях: скачивание вложения и хранение рядом с базой."""
import hashlib
import logging
import os
import re
from urllib.parse import urlsplit

import httpx

import config

log = logging.getLogger("bot")

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024     # больше справки или скана не бывает
ALLOWED_SUFFIXES = (".jpg", ".jpeg", ".png", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt")
SAFE_NAME = re.compile(r"[^A-Za-zА-Яа-яЁё0-9._-]+")


def attachments_dir() -> str:
    """Папка рядом с базой: в Docker это том, переживающий пересборку."""
    base = os.path.dirname(os.path.abspath(config.DATABASE_PATH))
    return os.path.join(base, "attachments")


def safe_name(name: str, fallback: str = "file") -> str:
    """Имя файла без путей и лишних символов - иначе в папку пролезет «../../».

    Сначала отбрасываем «папки» (иначе получится мусор вида _.._etc_), потом
    чистим символы: в папку с данными колледжа попадает только имя файла.
    """
    base = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = SAFE_NAME.sub("_", base)[:60].lstrip(".")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    if not os.path.splitext(cleaned)[1]:
        cleaned += ".bin"
    return cleaned


async def download_attachment(url: str, name: str = "") -> tuple[str, str]:
    """Скачивает вложение в папку рядом с базой. Возвращает (путь, имя файла).

    Тело проверяется по размеру и расширению: в папку с данными колледжа
    попадает только то, что похоже на документ или фото.
    """
    folder = attachments_dir()
    os.makedirs(folder, exist_ok=True)
    original = safe_name(name or (urlsplit(url).path.rsplit("/", 1)[-1] or "file"))
    if not original.lower().endswith(ALLOWED_SUFFIXES):
        raise ValueError(f"тип файла не поддерживается: {os.path.splitext(original)[1] or 'без расширения'}")
    target = os.path.join(folder, original)
    # одинаковые имена не затирают друг друга: добавляем короткий хеш содержимого
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (college-bot)"}) as client:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise ValueError(f"файл не отдаётся: код {response.status_code}")
            digest = hashlib.sha256()
            written = 0
            with open(target, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > MAX_ATTACHMENT_BYTES:
                        handle.close()
                        os.remove(target)
                        raise ValueError("файл больше 20 МБ")
                    digest.update(chunk)
                    handle.write(chunk)
    if written == 0:
        os.remove(target)
        raise ValueError("файл пустой")
    if os.path.exists(target) and os.path.getsize(target) != written:
        os.remove(target)
    stamp = digest.hexdigest()[:8]
    stem, suffix = os.path.splitext(original)
    final = f"{stem}_{stamp}{suffix}"
    os.replace(target, os.path.join(folder, final))
    return os.path.join(folder, final), final
