"""Тесты разбора и скачивания вложений."""
import os

import pytest

import attachments
import config
from updates import message_attachments

PHOTO_URL = "https://max.example/files/scan.pdf"


def update_with(attachments_list) -> dict:
    return {"update_type": "message_created",
            "message": {"body": {"text": "", "attachments": attachments_list}}}


# ── разбор события ───────────────────────────────────────────────────────────
def test_parses_file_attachment():
    items = message_attachments(update_with([{"type": "file", "size": 1024,
                                            "url": PHOTO_URL, "title": "справка.pdf"}]))
    assert items == [{"type": "file", "url": PHOTO_URL, "name": "справка.pdf", "size": 1024}]


def test_name_falls_back_to_url_tail():
    items = message_attachments(update_with([{"type": "image", "url": "https://a/b/photo.png"}]))
    assert items[0]["name"] == "photo.png"
    assert items[0]["size"] == 0


def test_accepts_link_key():
    items = message_attachments(update_with([{"type": "image", "link": "https://a/b/p.jpg"}]))
    assert items[0]["url"] == "https://a/b/p.jpg"


def test_unknown_format_is_ignored():
    assert message_attachments(update_with([{"type": "sticker"}])) == []
    assert message_attachments(update_with(["строка", None])) == []
    assert message_attachments({"message": {"body": {}}}) == []
    assert message_attachments({}) == []


def test_bad_size_does_not_break():
    items = message_attachments(update_with([{"type": "file", "url": PHOTO_URL, "size": "много"}]))
    assert items[0]["size"] == 0


# ── безопасное имя ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected_safe", [
    ("../../etc/passwd", "passwd.bin"),
    ("справка (1).pdf", "справка_1_.pdf"),
    ("", "file"),
    ("..", "file"),
    (".hidden", "hidden.bin"),
])
def test_safe_name(raw, expected_safe):
    assert attachments.safe_name(raw) == expected_safe


# ── скачивание ───────────────────────────────────────────────────────────────
class FakeStream:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        step = max(len(self.content) // 3, 1)
        for start in range(0, len(self.content), step):
            yield self.content[start:start + step]


class FakeClient:
    def __init__(self, content=b"%PDF-1.7 data", status=200):
        self.content, self.status = content, status

    def stream(self, method, url, **kwargs):
        return FakeStream(self.content, self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "data" / "bot.db"))
    path = attachments.attachments_dir()
    os.makedirs(path, exist_ok=True)
    return path


async def test_downloads_and_deduplicates_identical_file(monkeypatch, folder):
    monkeypatch.setattr(attachments.httpx, "AsyncClient",
                        lambda **kwargs: FakeClient(b"%PDF-1.7 one"))
    path, name = await attachments.download_attachment(PHOTO_URL, "справка.pdf")
    assert os.path.exists(path)
    assert name.startswith("справка") and name.endswith(".pdf")

    # тот же файл не плодит копию, а другой контент получает своё имя
    _, same = await attachments.download_attachment(PHOTO_URL, "справка.pdf")
    assert same == name
    monkeypatch.setattr(attachments.httpx, "AsyncClient",
                        lambda **kwargs: FakeClient(b"%PDF-1.7 two"))
    path2, name2 = await attachments.download_attachment(PHOTO_URL, "справка.pdf")
    assert name2 != name
    assert os.path.exists(path) and os.path.exists(path2)


async def test_refuses_unsupported_type(monkeypatch, folder):
    monkeypatch.setattr(attachments.httpx, "AsyncClient", lambda **kwargs: FakeClient(b"x"))
    with pytest.raises(ValueError, match="не поддерживается"):
        await attachments.download_attachment(PHOTO_URL, "вирус.exe")


async def test_refuses_bad_response(monkeypatch, folder):
    monkeypatch.setattr(attachments.httpx, "AsyncClient",
                        lambda **kwargs: FakeClient(b"", 404))
    with pytest.raises(ValueError, match="код 404"):
        await attachments.download_attachment(PHOTO_URL, "справка.pdf")


async def test_refuses_empty_file(monkeypatch, folder):
    monkeypatch.setattr(attachments.httpx, "AsyncClient", lambda **kwargs: FakeClient(b""))
    with pytest.raises(ValueError, match="пустой"):
        await attachments.download_attachment(PHOTO_URL, "справка.pdf")
    assert not os.listdir(folder)


async def test_refuses_too_big_file(monkeypatch, folder):
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(attachments.httpx, "AsyncClient",
                        lambda **kwargs: FakeClient(b"x" * 100))
    with pytest.raises(ValueError, match="20 МБ"):
        await attachments.download_attachment(PHOTO_URL, "справка.pdf")
    assert not os.listdir(folder)          # недокачанный файл не остаётся


def test_folder_lives_next_to_database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "data" / "bot.db"))
    assert attachments.attachments_dir() == str(tmp_path / "data" / "attachments")
