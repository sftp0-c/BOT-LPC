"""Публичная витрина: статична, доступна и отделена от рабочих данных."""
from pathlib import Path

from fastapi.testclient import TestClient

import bot
import database as db


def test_landing_is_static_and_does_not_touch_database(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Публичная страница обратилась к рабочей базе")
    monkeypatch.setattr(db, "one", forbidden)
    monkeypatch.setattr(db, "many", forbidden)
    client = TestClient(bot.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "ДЕМО · СИНТЕТИЧЕСКИЕ ДАННЫЕ" in response.text
    assert "Студент пишет в MAX" in response.text
    assert "lpc_panel" not in response.headers.get("set-cookie", "")


def test_landing_has_strict_security_and_no_inline_code():
    response = TestClient(bot.app).get("/")
    csp = response.headers["content-security-policy"]
    for rule in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                 "connect-src 'none'", "frame-ancestors 'none'", "form-action 'none'"):
        assert rule in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<script src=\"/public/landing.js\" defer></script>" in response.text
    assert "<script>" not in response.text and "onclick=" not in response.text
    assert "https://fonts" not in response.text and "cdn" not in response.text.lower()


def test_seo_and_service_routes():
    client = TestClient(bot.app)
    page = client.get("/").text
    for marker in ("canonical", "og:title", "og:description", "og:image",
                   "meta name=\"description\"", "theme-color"):
        assert marker in page
    assert client.get("/robots.txt").status_code == 200
    assert "Sitemap:" in client.get("/robots.txt").text
    assert "https://bot-lpc.ru/" in client.get("/sitemap.xml").text
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_public_assets_are_allowlisted_and_self_contained():
    client = TestClient(bot.app)
    for name in ("landing.css", "landing.js", "social-card.svg"):
        response = client.get(f"/public/{name}")
        assert response.status_code == 200 and response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/public/../bot.py").status_code == 404
    root = Path("web/static")
    css = (root / "landing.css").read_text(encoding="utf-8-sig")
    js = (root / "landing.js").read_text(encoding="utf-8-sig")
    assert "prefers-reduced-motion" in css and "IntersectionObserver" in js
    assert "wheel" not in js and "preventDefault" not in js
    assert "http://" not in css + js and "https://" not in css + js


def test_mobile_rules_and_accessibility_are_present():
    page = TestClient(bot.app).get("/").text
    css = Path("web/static/landing.css").read_text(encoding="utf-8-sig")
    assert page.count("<h1>") == 1
    for landmark in ("<header", "<nav", "<main", "<section", "<footer"):
        assert landmark in page
    assert 'class="lp-skip"' in page and 'href="#content"' in page
    assert "@media(max-width:560px)" in css
    assert "min-height:46px" in css
    assert "overflow-x:hidden" in css
    assert "position:sticky" in css and "position:relative" in css
