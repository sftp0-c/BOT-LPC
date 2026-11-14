"""Публичная витрина: безопасные ресурсы, понятные примеры и доступное содержимое."""
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

import bot
import version
import database as db


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.nodes = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def test_landing_is_static_and_does_not_touch_database(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Публичная страница обратилась к рабочей базе")
    monkeypatch.setattr(db, "one", forbidden)
    monkeypatch.setattr(db, "many", forbidden)
    monkeypatch.setattr(db, "run", forbidden)
    monkeypatch.setattr(db, "_conn", forbidden)
    response = TestClient(bot.app).get("/")
    assert response.status_code == 200
    assert "Синтетические данные" in response.text
    assert "Пример интерфейса" in response.text
    assert "Студент пишет в MAX" in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-cache"
    def count_roots(routes):
        total = 0
        for route in routes:
            if getattr(route, "path", None) == "/" and "GET" in getattr(route, "methods", ()):
                total += 1
            inner = getattr(route, "original_router", None)
            if inner is not None:
                total += count_roots(inner.routes)
        return total
    assert count_roots(bot.app.routes) == 1


def test_landing_has_strict_security_and_no_inline_code():
    response = TestClient(bot.app).get("/")
    csp = response.headers["content-security-policy"]
    for rule in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                 "connect-src 'none'", "frame-ancestors 'none'", "form-action 'none'"):
        assert rule in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    for tag, attrs in Markup(response.text).nodes:
        assert "style" not in attrs
        assert not any(key.startswith("on") for key in attrs)
        if tag == "script":
            assert attrs.get("src") == f"/public/landing.js?v={version.__version__}" and "defer" in attrs
        assert tag not in ("iframe", "object", "embed", "style", "form")
        if tag in ("img", "script", "link"):
            src = attrs.get("src", attrs.get("href", ""))
            if attrs.get("rel") != "canonical":
                assert src.startswith("/public/")
        if tag == "link" and attrs.get("rel") == "icon":
            # версия в URL: без неё браузер оставляет старую фавиконку в кеше
            assert attrs.get("href") == f"/public/mark.svg?v={version.__version__}"


def test_seo_and_service_routes():
    client = TestClient(bot.app)
    page = client.get("/").text
    for marker in ("canonical", "og:title", "og:description", "og:image",
                   'meta name="description"', "theme-color"):
        assert marker in page
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Sitemap:" in robots.text
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/x-icon"
    assert favicon.headers["x-content-type-options"] == "nosniff"
    assert favicon.headers["cache-control"] == "public, max-age=3600"
    assert favicon.content[:4] == b"\x00\x00\x01\x00"  # настоящая ICO-сигнатура
    assert "Disallow: /feedback" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert "https://bot-lpc.ru/" in sitemap.text and "feedback" not in sitemap.text
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_public_assets_are_allowlisted_and_self_contained():
    client = TestClient(bot.app)
    for name in ("landing.css", "landing.js", "social-card.svg", "mark.svg", "service-map.svg"):
        response = client.get(f"/public/{name}")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        if name.endswith(".svg"):
            tree = ET.fromstring(response.content.decode("utf-8-sig"))
            assert not any(node.tag.rsplit("}", 1)[-1] in ("script", "foreignObject") for node in tree.iter())
            assert not any(key.lower().startswith("on") for node in tree.iter() for key in node.attrib)
    for path in ("/public/../bot.py", "/public/bot.py", "/public/.env", "/public/not-listed.svg"):
        assert client.get(path).status_code == 404
    css = Path("web/static/landing.css").read_text(encoding="utf-8-sig")
    js = Path("web/static/landing.js").read_text(encoding="utf-8-sig")
    assert "http://" not in css + js and "https://" not in css + js
    assert "wheel" not in js and "preventDefault" not in js
    assert "fetch(" not in js and "XMLHttpRequest" not in js


def test_content_beta_screens_and_planned_rights():
    page = TestClient(bot.app).get("/").text
    nodes = Markup(page).nodes
    figures = [attrs for tag, attrs in nodes if tag == "figure" and attrs.get("id", "").startswith("screen-")]
    assert [item["id"] for item in figures] == [
        "screen-welcome", "screen-schedule", "screen-faq", "screen-request",
        "screen-reply", "screen-workspace", "screen-panel"]
    for text in ("Бета-тестирование", "Планируется", "Расширение пилота",
                 "Веб-кабинет сотрудника: вход по коду из MAX",
                 "Доступ выдаёт сис-админ", "переписка реальных студентов"):
        assert text in page
    assert any(tag == "nav" and "lp-gallery-map" in (attrs.get("class") or "")
               for tag, attrs in nodes)
    hrefs = {attrs["href"] for tag, attrs in nodes if tag == "a" and "href" in attrs}
    assert {"/feedback?kind=bug", "/feedback?kind=suggestion", "/panel/login"} <= hrefs
    ids = {attrs["id"] for _tag, attrs in nodes if "id" in attrs}
    for href in hrefs:
        target = urlsplit(href)
        if href.startswith("#"):
            assert target.fragment in ids, href
    assert not re.search(r"\b(?:100%|24/7|нулевые расходы)\b", page)


def test_accessibility_and_progressive_enhancement():
    page = TestClient(bot.app).get("/").text
    nodes = Markup(page).nodes
    assert sum(tag == "h1" for tag, _ in nodes) == 1
    for landmark in ("header", "nav", "main", "section", "footer"):
        assert any(tag == landmark for tag, _ in nodes)
    assert 'class="lp-skip"' in page and 'href="#content"' in page
    assert any(tag == "div" and attrs.get("class") == "lp-gallery" and attrs.get("tabindex") == "0"
               for tag, attrs in nodes)
    for tag, attrs in nodes:
        if tag == "img":
            assert attrs.get("width") and attrs.get("height")
            if attrs.get("aria-hidden") == "true":
                # Декоративная картинка: пустой alt обязателен, иначе
                # скринридер прочитает её в дополнение к соседнему тексту.
                assert attrs.get("alt") == "", attrs
            else:
                assert attrs.get("alt"), attrs
        if tag == "button":
            assert attrs.get("aria-label")
    css = Path("web/static/landing.css").read_text(encoding="utf-8-sig")
    js = Path("web/static/landing.js").read_text(encoding="utf-8-sig")
    assert "prefers-reduced-motion" in css and "prefers-reduced-motion" in js
    assert "IntersectionObserver" in js and "unobserve" in js
    assert ".lp-reveal{opacity:1}" in css
    assert "scroll-behavior:auto" in css.rsplit("@media(prefers-reduced-motion:reduce)", 1)[-1]
    assert ".lp-gallery-section .lp-device{color:var(--ink)}" in css
    assert ".lp-gallery-section .lp-demo-dashboard{color:var(--ink)}" in css
    assert "opacity:0" not in css.split("@keyframes")[0]
    assert "min-height:58px" in css and "max-width:560px" in css
    assert "color-scheme:light" in css and "--bg:#fff" in css
    assert "scroll-snap-type:x proximity" in css
    assert "history.scrollRestoration" not in js
