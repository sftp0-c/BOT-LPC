"""Дополнение к tests/test_tunnel.py: выбор адреса для ссылок-приглашений."""
import os
from pathlib import Path

import pytest

import tunnel

ТУННЕЛЬ = "https://abc-def.trycloudflare.com"


@pytest.fixture(autouse=True)
def _чистое_окружение(monkeypatch, tmp_path):
    monkeypatch.setenv("TUNNEL_URL_FILE", str(tmp_path / "tunnel_url.txt"))
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    yield
    os.environ.pop("PUBLIC_URL", None)


def _туннель_живой(monkeypatch, url=ТУННЕЛЬ, age=5):
    Path(os.environ["TUNNEL_URL_FILE"]).write_text(url, encoding="utf-8")
    monkeypatch.setattr(tunnel, "url_age_seconds", lambda: age)


@pytest.mark.parametrize("адрес,ожидание", [
    ("http://192.168.0.102:8090", True),
    ("http://10.1.2.3:8090", True),
    ("http://172.20.0.3:8090", True),
    ("http://127.0.0.1:8090", True),
    ("http://localhost:8090", True),
    ("http://[::1]:8090", True),
    ("http://bot:8090", True),
    ("http://panel.local", True),
    ("", True),
    ("https://panel.college-lan.ru", False),
    ("https://abc-def.trycloudflare.com", False),
])
def test_is_private_url(адрес, ожидание):
    assert tunnel.is_private_url(адрес) is ожидание


def test_туннель_важнее_внутреннего_адреса(monkeypatch):
    """Главный случай: в .env внутренний адрес, но туннель работает."""
    monkeypatch.setenv("PUBLIC_URL", "http://192.168.0.102:8090")
    _туннель_живой(monkeypatch)
    assert tunnel.public_url() == ТУННЕЛЬ


def test_публичный_public_url_важнее_туннеля(monkeypatch):
    """Именованный туннель с доменом настраивают руками - он главнее."""
    monkeypatch.setenv("PUBLIC_URL", "https://panel.college-lan.ru/")
    _туннель_живой(monkeypatch)
    assert tunnel.public_url() == "https://panel.college-lan.ru"


def test_протухший_туннель_не_подставляется(monkeypatch):
    """Мёртвый адрес хуже, чем честная внутренняя ссылка."""
    monkeypatch.setenv("PUBLIC_URL", "http://192.168.0.102:8090")
    _туннель_живой(monkeypatch, age=tunnel.STALE_AFTER_SECONDS + 60)
    assert tunnel.public_url() == "http://192.168.0.102:8090"


def test_без_туннеля_остаётся_public_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://192.168.0.102:8090")
    assert tunnel.public_url() == "http://192.168.0.102:8090"


def test_без_всего_пусто(monkeypatch):
    assert tunnel.public_url() == ""


def test_статус_виден_и_не_противоречит_выбору(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://192.168.0.102:8090")
    _туннель_живой(monkeypatch)
    state = tunnel.status()
    assert state["usable"] is True
    assert state["public_url"] == ТУННЕЛЬ
    assert state["url"] == ТУННЕЛЬ
