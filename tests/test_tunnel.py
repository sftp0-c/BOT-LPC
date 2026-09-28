"""Туннель Cloudflare: модуль читает файл, который пишет контейнер tunnel.

Проверяем главное - он не должен ронять бота и панель: ни файла, ни базы,
ни нормального адреса в наличии не предполагается.
"""
import os
import time

import pytest

import database as db
import tunnel

URL = "https://funny-word-two.trycloudflare.com"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Файл адреса - во временный каталог, кэш настройки - чистый."""
    monkeypatch.setattr(tunnel, "URL_FILE", tmp_path / "tunnel_url.txt")
    monkeypatch.delenv("TUNNEL_URL_FILE", raising=False)
    monkeypatch.delenv("TUNNEL_ENABLED", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    tunnel._db_enabled = None
    yield
    tunnel._db_enabled = None


def put(text: str, age: float = 0) -> None:
    """Кладёт текст в файл адреса и отматывает время изменения на age секунд."""
    path = tunnel.URL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age:
        when = time.time() - age
        os.utime(path, (when, when))


# ── current_url: читает файл, режет пробелы, мусор не пропускает ─────────────
def test_reads_file_and_trims_spaces():
    put(f"  {URL}\n\n")
    assert tunnel.current_url() == URL


def test_empty_without_file():
    assert tunnel.current_url() == ""


@pytest.mark.parametrize("junk", [
    "",
    "   \n",
    "не адрес",
    "http://plain.trycloudflare.com",     # не https
    "https://",                            # нет домена
    "/panel",                               # относительный путь
    "https://a b.trycloudflare.com",       # пробел внутри
    f"{URL} и ещё текст",                  # мусор после адреса
])
def test_empty_on_junk(junk):
    put(junk)
    assert tunnel.current_url() == ""


def test_url_file_from_environment(tmp_path, monkeypatch):
    """На хосте адрес читают из data/, путь задают переменной окружения."""
    path = tmp_path / "хоста.txt"
    path.write_text(URL, encoding="utf-8")
    monkeypatch.setenv("TUNNEL_URL_FILE", str(path))
    assert tunnel.current_url() == URL


# ── возраст адреса: по нему видно, что писатель (контейнер) ещё жив ──────────
def test_age_counts_from_file():
    put(URL, age=120)
    assert 100 <= tunnel.url_age_seconds() <= 300


def test_age_of_fresh_file_is_zero():
    put(URL)
    assert tunnel.url_age_seconds() == 0


def test_age_without_file():
    assert tunnel.url_age_seconds() == -1


def test_stale_when_writer_went_silent():
    """Писатель трогает файл каждые ~30 секунд, молчание - туннель упал."""
    put(URL, age=tunnel.STALE_AFTER_SECONDS + 600)
    assert tunnel.stale() is True
    assert tunnel.current_url() == URL      # адрес ещё читается, но ему не верим


def test_not_stale_while_fresh():
    put(URL, age=tunnel.STALE_AFTER_SECONDS - 60)
    assert tunnel.stale() is False


def test_not_stale_without_file():
    assert tunnel.stale() is False


# ── вкл/выкл: переменная окружения главнее настройки в базе ───────────────────
@pytest.mark.parametrize("value", ["1", "true", "on", "да", "YES"])
def test_enabled_by_environment(monkeypatch, value):
    monkeypatch.setenv("TUNNEL_ENABLED", value)
    assert tunnel.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "нет", "no"])
def test_disabled_by_environment(monkeypatch, value):
    monkeypatch.setenv("TUNNEL_ENABLED", value)
    assert tunnel.is_enabled() is False


def test_empty_environment_falls_back_to_setting(monkeypatch):
    """Пустое значение - это «не задано», а не «выключено»."""
    monkeypatch.setenv("TUNNEL_ENABLED", "  ")
    assert tunnel.is_enabled() is True


async def test_enabled_reads_database_setting():
    await db.set_setting("tunnel_enabled", "0")
    assert await tunnel.refresh_enabled() is False
    assert tunnel.is_enabled() is False

    await db.set_setting("tunnel_enabled", "1")
    assert await tunnel.refresh_enabled() is True
    assert tunnel.is_enabled() is True


async def test_missing_setting_means_enabled():
    """Пока никто не переключал настройку, туннель включён."""
    assert await tunnel.refresh_enabled() is True
    assert tunnel.is_enabled() is True


async def test_environment_wins_over_database(monkeypatch):
    await db.set_setting("tunnel_enabled", "0")
    await tunnel.refresh_enabled()
    monkeypatch.setenv("TUNNEL_ENABLED", "1")
    assert tunnel.is_enabled() is True


# ── публичный адрес и общее состояние для панели ─────────────────────────────
def test_public_url_prefers_env(monkeypatch):
    put(URL)
    monkeypatch.setenv("PUBLIC_URL", "https://panel.example.com/")
    assert tunnel.public_url() == "https://panel.example.com"


def test_public_url_falls_back_to_tunnel(monkeypatch):
    put(URL)
    assert tunnel.public_url() == URL


def test_status_reflects_file():
    put(URL, age=60)
    status = tunnel.status()
    assert status["url"] == URL
    assert status["enabled"] is True
    assert status["stale"] is False
    assert status["usable"] is True


def test_status_of_stale_url_is_not_usable():
    put(URL, age=tunnel.STALE_AFTER_SECONDS + 600)
    assert tunnel.status()["usable"] is False


# ── ни одна функция не бросает исключений наружу ────────────────────────────
def test_survives_broken_file(tmp_path, monkeypatch):
    """Файл адреса оказался каталогом - обычная беда в общем томе."""
    broken = tmp_path / "tunnel_url.txt"
    broken.mkdir()
    monkeypatch.setattr(tunnel, "URL_FILE", broken)
    monkeypatch.setenv("TUNNEL_URL_FILE", str(broken))
    assert tunnel.current_url() == ""
    assert tunnel.url_age_seconds() >= 0
    assert tunnel.stale() is False
    assert tunnel.is_enabled() is True
    assert tunnel.public_url() == ""
    assert tunnel.status()["usable"] is False


async def test_survives_database_without_settings(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("базы нет")

    monkeypatch.setattr(db, "get_setting", broken)
    assert await tunnel.refresh_enabled() is True
    assert tunnel.is_enabled() is True
