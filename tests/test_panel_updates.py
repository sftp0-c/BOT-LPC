"""Проверки owner-only истории выпусков и безопасного renderer."""
from copy import deepcopy
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import bot
import config
import database as db
import version
from conftest import login_panel
from web import updates

OWNER = "46010397"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


async def test_updates_owner_only_headers_and_tab(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    guest = panel_client.get("/panel/updates", follow_redirects=False)
    assert guest.status_code == 303 and guest.headers["location"] == "/panel/login"
    assert login_panel(panel_client)
    assert panel_client.get("/panel/updates").status_code == 404
    assert 'href="/panel/updates"' not in panel_client.get("/panel/").text
    assert login_panel(panel_client, OWNER)
    response = panel_client.get("/panel/updates")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.headers["x-frame-options"] == "DENY"
    assert 'aria-label="Обновления"' in response.text
    assert 'aria-current="page"' in response.text
    assert response.text.count('href="/panel/updates"') == 1


async def test_updates_route_does_not_read_database_after_auth(owner_client, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("страница истории не должна читать БД")
    monkeypatch.setattr(db, "one", forbidden)
    monkeypatch.setattr(db, "many", forbidden)
    response = owner_client.get("/panel/updates")
    assert response.status_code == 200 and updates.SOURCE_HEAD in response.text


def test_renderer_escapes_snapshot_and_rejects_unsafe_structure():
    bad = ({"version": '2.2.0\" autofocus onfocus=alert(1)', "tag": "v2.2.0", "state": "released",
            "date": "<script>bad()</script>", "notes": ("<img src=x onerror=bad()>",),
            "commits": ({"hash": "abcdef0", "subject": '<svg onload=bad()>', "when": "сегодня",
                         "files": 1, "add": 2, "del": 0},)},)
    html = updates.render_updates(bad, "abcdef0", "2.2.0")
    assert "autofocus" not in html and "onfocus" not in html
    assert "<script>" not in html and "<img src=x" not in html and "<svg" not in html
    assert "История выпусков пока не собрана" in html

    good = [deepcopy(item) for item in updates.RELEASES]
    good[0]["notes"] = ('<img src=x onerror="bad()">',)
    good[0]["commits"] = ({**good[0]["commits"][0], "subject": '<script>bad()</script>'},)
    html = updates.render_updates(good, updates.SOURCE_HEAD, version.__version__)
    assert "&lt;img" in html and "&lt;script&gt;" in html
    assert "onerror=\"bad()\"" not in html
    assert 'id="release-' in html and 'data-release="' in html


def test_snapshot_shape_privacy_freshness_and_single_live_marker():
    assert re.fullmatch(r"[0-9a-f]{7,12}", updates.SOURCE_HEAD)
    assert updates.RELEASES
    serialized = repr(updates.RELEASES)
    assert not re.search(r"[0-9a-f]{40}", serialized)
    assert not re.search(r"https?://[^\s]+:[^\s]+@", serialized)
    assert not re.search(r"(?<![0-9])[1-9][0-9]{9,18}(?![0-9])", serialized)
    assert "text_content" not in serialized and "submission_nonce" not in serialized
    released = [item for item in updates.RELEASES if item["state"] == "released"]
    assert len([item for item in released if item["version"] == version.__version__]) == 1
    html = updates.render_updates()
    assert html.count("Сейчас на сервере") == 1
    if any(item["state"] == "development" for item in updates.RELEASES):
        assert "Разрабатывается" in html and "После 2.2.0" in html
        assert "0.0.0" not in html


def test_updates_assets_progressive_safe_and_touch_ready():
    css = open("web/static/panel.css", encoding="utf-8-sig").read()
    js = open("web/static/panel.js", encoding="utf-8-sig").read()
    tab_rule = re.search(r"\.updates-tab\{([^}]*)\}", css)
    assert tab_rule and "min-width:44px" in tab_rule[1] and "min-height:44px" in tab_rule[1]
    mobile = css.split("@media(max-width:640px)", 1)[1]
    assert ".updates-tab{width:44px;height:44px}" in mobile
    assert '[aria-current=page]' in css
    marker = "/* История выпусков: прогрессивный локальный поиск и управление details. */"
    assert marker in js
    block = js.split(marker, 1)[1]
    assert 'querySelector("[data-updates-root]")' in block
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch(", "XMLHttpRequest", "localStorage"):
        assert forbidden not in block
    assert "textContent" in block and "items[i].hidden" in block


def test_updates_module_has_no_runtime_git_network_or_database_imports():
    source = open("web/updates.py", encoding="utf-8-sig").read()
    for forbidden in ("subprocess", "httpx", "urllib", "requests", "database", "git "):
        assert forbidden not in source
    assert "updates_data" in source


async def test_owner_guard_denies_non_owner(monkeypatch):
    request = SimpleNamespace()
    async def user(_request):
        return "2"
    async def not_owner(_uid):
        return False
    monkeypatch.setattr(updates, "require_user", user)
    monkeypatch.setattr(updates.repo, "is_owner", not_owner)
    with pytest.raises(HTTPException) as caught:
        await updates.require_owner(request)
    assert caught.value.status_code == 404


def test_registered_route_once():
    def count(routes):
        total = 0
        for route in routes:
            if getattr(route, "path", None) == "/panel/updates":
                total += 1
            inner = getattr(route, "original_router", None)
            if hasattr(inner, "routes"):
                total += count(inner.routes)
        return total
    assert count(bot.app.routes) == 1
