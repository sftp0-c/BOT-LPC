"""Файлы в обращениях: скачивание вложения и хранение рядом с базой."""
import hashlib
import logging
import os
import re
import secrets
import tempfile
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


async def download_attachment(url: str, name: str = "", *, unique: bool = False) -> tuple[str, str]:
    """Скачивает без перезаписи чужих файлов; unique даёт отдельное доверенное вложение.

    Старые вызовы сохраняют дедупликацию одинакового содержимого. Для обращения
    создаётся новое непредсказуемое имя, даже если байты совпали с другим делом.
    """
    folder = attachments_dir()
    original = safe_name(name or (urlsplit(url).path.rsplit("/", 1)[-1] or "file"))
    if not original.lower().endswith(ALLOWED_SUFFIXES):
        raise ValueError(f"тип файла не поддерживается: {os.path.splitext(original)[1] or 'без расширения'}")
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=folder)
    digest, written = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as handle:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (college-bot)"}) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise ValueError(f"файл не отдаётся: код {response.status_code}")
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > MAX_ATTACHMENT_BYTES:
                            raise ValueError("файл больше 20 МБ")
                        digest.update(chunk)
                        handle.write(chunk)
        if not written:
            raise ValueError("файл пустой")
        stamp = secrets.token_hex(16) if unique else digest.hexdigest()[:32]
        stem, suffix = os.path.splitext(original)
        stem = stem[:60 - len(stamp) - len(suffix) - 1]
        final = f"{stem}_{stamp}{suffix}"
        target = os.path.join(folder, final)
        try:
            # Создать, но никогда не заменить: ни дедупликация, ни гонка не портят
            # прежний файл. Временное имя принадлежит только этому скачиванию.
            os.link(temporary, target)
        except FileExistsError:
            if unique or os.path.islink(target) or not os.path.isfile(target):
                raise ValueError("имя вложения уже занято") from None
            with open(target, "rb") as saved:
                existing = hashlib.file_digest(saved, "sha256").digest()
            if existing != digest.digest():
                raise ValueError("имя вложения уже занято") from None
        return target, final
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
