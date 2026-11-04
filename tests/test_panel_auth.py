"""Вход в панель: подписанная cookie, remember-me, CSRF и безопасный выход."""
import config
import repository as repo
import webpanel
from conftest import csrf_of, login_panel, post_form
from web import common


def cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


def test_regular_and_remembered_cookie_have_different_lifetimes(panel_client, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(common.time, "time", lambda: now)
    regular = panel_client.post("/panel/login", data={
        "user_id": "1", "password": "test-panel-pass",
    }, follow_redirects=False)
    assert regular.status_code == 303
    regular_token = panel_client.cookies.get(webpanel.COOKIE)
    regular_exp = common._decode_session(regular_token)[1]
    assert regular_exp == now + config.WEB_PANEL_HOURS * 3600
    assert f"Max-Age={config.WEB_PANEL_HOURS * 3600}" in cookie_header(regular)

    panel_client.cookies.clear()
    remembered = panel_client.post("/panel/login", data={
        "user_id": "1", "password": "test-panel-pass", "remember": "1",
    }, follow_redirects=False)
    remembered_token = panel_client.cookies.get(webpanel.COOKIE)
    remembered_exp = common._decode_session(remembered_token)[1]
    assert remembered_exp == now + 30 * 24 * 3600
    assert "Max-Age=2592000" in cookie_header(remembered)


def test_signed_cookie_survives_process_memory_reset(panel_client):
    assert login_panel(panel_client)
    cookie = panel_client.cookies.get(webpanel.COOKIE)
    csrf = csrf_of(panel_client)
    assert cookie and csrf
    common._sessions.clear()
    common._csrf.clear()
    assert panel_client.get("/panel/").status_code == 200
    assert csrf_of(panel_client) == csrf


def test_changed_or_expired_cookie_is_rejected(panel_client, monkeypatch):
    assert login_panel(panel_client)
    token = panel_client.cookies.get(webpanel.COOKIE)
    changed = token[:-1] + ("A" if token[-1] != "A" else "B")
    panel_client.cookies.set(webpanel.COOKIE, changed)
    assert panel_client.get("/panel/", follow_redirects=False).status_code == 303

    monkeypatch.setattr(common.time, "time", lambda: 1000)
    expired = common.start_session("1", hours=1)
    monkeypatch.setattr(common.time, "time", lambda: 1000 + 3601)
    panel_client.cookies.set(webpanel.COOKIE, expired)
    assert panel_client.get("/panel/", follow_redirects=False).status_code == 303


def test_signature_secret_change_revokes_cookie(panel_client, monkeypatch):
    assert login_panel(panel_client)
    token = panel_client.cookies.get(webpanel.COOKIE)
    monkeypatch.setattr(config, "WEB_SESSION_SECRET", "another-session-secret-that-is-long-enough-98765")
    panel_client.cookies.set(webpanel.COOKIE, token)
    assert panel_client.get("/panel/", follow_redirects=False).status_code == 303


def test_cookie_flags_follow_https_proxy(panel_client):
    response = panel_client.post("/panel/login", data={
        "user_id": "1", "password": "test-panel-pass",
    }, headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    header = cookie_header(response).lower()
    assert "httponly" in header and "samesite=lax" in header
    assert "path=/panel" in header and "secure" in header


def test_logout_is_post_with_csrf(panel_client):
    assert login_panel(panel_client)
    assert panel_client.get("/panel/logout", follow_redirects=False).status_code == 405
    assert panel_client.post("/panel/logout", data={}, follow_redirects=False).status_code == 403
    assert post_form(panel_client, "/panel/logout").status_code == 303
    assert panel_client.get("/panel/", follow_redirects=False).status_code == 303


async def test_role_revocation_stops_existing_remembered_cookie(panel_client):
    assert login_panel(panel_client)
    assert panel_client.get("/panel/").status_code == 200
    await repo.add_sysadmin("2", "Запасной сис-админ")
    await repo.revoke_sysadmin("1")
    assert panel_client.get("/panel/", follow_redirects=False).status_code == 403


def test_failed_login_does_not_set_cookie(panel_client):
    response = panel_client.post("/panel/login", data={
        "user_id": "1", "password": "неверный",
    }, follow_redirects=False)
    assert response.status_code == 403
    assert webpanel.COOKIE not in cookie_header(response)


def test_csrf_is_not_session_signature(panel_client):
    assert login_panel(panel_client)
    cookie = panel_client.cookies.get(webpanel.COOKIE)
    payload, signature = cookie.split(".", 1)
    csrf = csrf_of(panel_client)
    assert csrf and csrf != cookie and csrf != signature and payload not in csrf
