"""Кабинет сотрудников: правила OTP, cookie, CSRF, лимитов и отзыва сессий."""
import asyncio
import logging
import re
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

import config
import database as db
from handlers import common as bot_common
from store import web_access as access
from web import workspace_auth as auth
from web.router import workspace_router


@pytest.fixture
async def staff_auth(env, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://college.example/base")
    await db.run("INSERT INTO admins(user_id,full_name,role_type,position) VALUES('7123456789','Анна Сотрудник','staff','Куратор')")
    assert await access.set_staff_web_access("7123456789", True, access.WEB_WORK_PERMISSIONS, "1")
    return env


@pytest.fixture
def ws_client(staff_auth):
    app = FastAPI()
    app.include_router(workspace_router)

    @app.get("/_staff_probe")
    async def probe(request: Request):
        user = await auth.require_staff_web(request)
        return HTMLResponse(str(user["user_id"]) + auth.staff_csrf(request))

    # Cookie Path=/workspace намеренно не пересылается в /panel и соседние URL.
    @app.get("/workspace/_auth_probe")
    async def workspace_probe(request: Request):
        user = await auth.require_staff_web(request)
        return HTMLResponse(str(user["user_id"]) + auth.staff_csrf(request))

    return TestClient(app, base_url="https://college.example", follow_redirects=False)


def hidden(response, name):
    found = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', response.text)
    assert found, name
    return found[1]


def request_code(client, user_id="7123456789"):
    login = client.get("/workspace/login")
    return client.post("/workspace/login/request", data={"user_id": user_id, "csrf": hidden(login, "csrf")},
                       headers={"Origin": "https://college.example"})


def last_code(api, user_id="7123456789"):
    return re.search(r"\b[0-9]{8}\b", api.last(user_id)[1])[0]


def verify_code(client, response, code):
    return client.post("/workspace/login/verify",
                       data={"csrf": hidden(response, "csrf"), "challenge": hidden(response, "challenge"), "code": code},
                       headers={"Origin": "https://college.example"})


def login_staff(client, api):
    response = request_code(client)
    assert verify_code(client, response, last_code(api)).status_code == 303
    return client.cookies.get(auth.STAFF_WEB_COOKIE)


async def test_code_login_is_separate_and_revocable(ws_client, staff_auth):
    await db.set_state("7123456789", "existing-dialog", {"step": 7})
    requested = request_code(ws_client)
    assert requested.status_code == 200 and auth.NEUTRAL_REQUEST in requested.text
    assert "7123456789" not in requested.text
    assert len(staff_auth.sent) == 1 and not staff_auth.sent[0][2]
    assert "https://college.example/workspace/login" in staff_auth.sent[0][1]
    code = last_code(staff_auth)
    result = verify_code(ws_client, requested, code)
    assert result.status_code == 303 and result.headers["location"] == "/workspace"
    token = ws_client.cookies.get(auth.STAFF_WEB_COOKIE)
    assert len(token) == 43 and "7123456789" not in token
    row = await db.one("SELECT * FROM staff_web_sessions")
    assert row["token_hash"] == access.staff_web_session_hash(token) != token
    assert row["expires_at"] - row["created_at"] == 12 * 3600
    assert row["revoked_at"] == 0
    challenge = await db.one("SELECT * FROM staff_web_login_challenges")
    assert challenge["code_hash"] != code and challenge["consumed_at"] > 0
    assert challenge["challenge_hash"] != hidden(requested, "challenge")
    assert challenge["browser_hash"] != auth.WORKSPACE_BROWSER_COOKIE
    principal = await access.staff_web_principal(row["token_hash"])
    assert principal["permissions"] == list(access.WEB_WORK_PERMISSIONS)
    assert set(principal) == {"user_id", "full_name", "position", "permissions", "auth_version", "session_hash"}
    assert (await db.get_state("7123456789"))["state"] == "existing-dialog"
    cookie = result.headers.get("set-cookie", "")
    for flag in ("Path=/workspace", "HttpOnly", "SameSite=lax", "Secure", "Max-Age=43200"):
        assert flag in cookie
    assert ws_client.get("/_staff_probe").status_code == 303
    probe = ws_client.get("/workspace/_auth_probe")
    csrf = hidden(probe, "csrf")
    assert ws_client.post("/workspace/logout", data={"csrf": "bad"}).status_code == 403
    assert (await access.staff_web_principal(row["token_hash"])) is not None
    out = ws_client.post("/workspace/logout", data={"csrf": csrf})
    assert out.status_code == 303
    assert await access.staff_web_principal(row["token_hash"]) is None
    ws_client.cookies.set(auth.STAFF_WEB_COOKIE, token, domain="college.example", path="/workspace")
    assert ws_client.get("/workspace/_auth_probe").status_code == 303


@pytest.mark.parametrize("target,role,is_test,enabled", [
    ("7555555555", "staff", 0, 0), ("7555555555", "staff", 1, 1),
    ("7555555555", "sysadmin", 0, 1), ("7555555555", "owner", 0, 1),
    ("7555555555", "student", 0, 1), ("7555555555", "missing", 0, 0),
    ("fake-staff", "staff", 0, 1), ("0", "staff", 0, 1),
    ("9223372036854775808", "staff", 0, 1),
])
async def test_unknown_disabled_and_wrong_roles_are_neutral(ws_client, staff_auth, target, role, is_test, enabled):
    if role != "missing":
        await db.run("INSERT INTO admins(user_id,full_name,role_type,is_test) VALUES(?,?,?,?)",
                     (target, "Не отправлять", role, is_test))
        await db.run("INSERT INTO staff_web_access(user_id,enabled,permissions) VALUES(?,?,'[]')", (target, enabled))
    response = request_code(ws_client, target)
    assert response.status_code == 200 and auth.NEUTRAL_REQUEST in response.text
    assert not staff_auth.sent
    assert verify_code(ws_client, response, "12345678").status_code == 200
    assert not ws_client.cookies.get(auth.STAFF_WEB_COOKIE)
    assert len(hidden(response, "challenge")) == 43
    if len(target) > 5:
        assert target not in hidden(response, "challenge")


async def test_browser_cookie_is_reused_without_sliding_expiry(ws_client, staff_auth, monkeypatch):
    now = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: now)
    first = ws_client.get("/workspace/login")
    cookie = ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE)
    csrf = hidden(first, "csrf")
    assert auth._browser_expires(cookie, now) == now + 600
    monkeypatch.setattr(auth, "_now", lambda: now + 599)
    second = ws_client.get("/workspace/login")
    assert ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE) == cookie
    assert hidden(second, "csrf") == csrf
    assert "set-cookie" not in second.headers
    monkeypatch.setattr(auth, "_now", lambda: now + 600)
    third = ws_client.get("/workspace/login")
    assert ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE) != cookie
    assert hidden(third, "csrf") != csrf


@pytest.mark.parametrize("path", ["/workspace/login/request", "/workspace/login/verify"])
@pytest.mark.parametrize("bad", ["missing", "invalid", "origin", "null", "host", "duplicate", "expired", "cookie"])
async def test_login_post_requires_csrf_origin_and_live_browser(ws_client, staff_auth, monkeypatch, path, bad):
    page = ws_client.get("/workspace/login")
    token = hidden(page, "csrf")
    cookie = ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE)
    data = {"csrf": token, **({"user_id": "7123456789"} if path.endswith("request") else {"challenge": "x", "code": "12345678"})}
    headers = {"Origin": "https://college.example"}
    if bad == "missing":
        data.pop("csrf")
    elif bad == "invalid":
        data["csrf"] = "forged"
    elif bad == "origin":
        headers["Origin"] = "https://evil.example"
    elif bad == "null":
        headers["Origin"] = "null"
    elif bad == "host":
        headers.update({"Host": "evil.example", "Origin": "https://evil.example"})
    elif bad == "expired":
        expires = auth._browser_expires(cookie, auth._now())
        monkeypatch.setattr(auth, "_now", lambda: expires)
    elif bad == "cookie":
        ws_client.cookies.clear()
    if bad == "duplicate":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        result = ws_client.post(path, content=urlencode(data) + "&csrf=" + token, headers=headers)
        assert result.status_code == 400
    else:
        result = ws_client.post(path, data=data, headers=headers)
        assert result.status_code == 403
    assert not staff_auth.sent
    assert not await db.many("SELECT * FROM staff_web_sessions")
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/workspace/login", "/workspace/login/request", "/workspace/login/verify", "/workspace/logout"])
@pytest.mark.parametrize("secret", ["", "short", " " * 50])
async def test_all_public_auth_fails_closed_without_long_secret(ws_client, monkeypatch, path, secret):
    monkeypatch.setattr(config, "WEB_SESSION_SECRET", secret)
    response = ws_client.get(path) if path == "/workspace/login" else ws_client.post(path, data={"csrf": "anything"})
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


async def test_login_security_headers_and_no_panel_navigation(ws_client):
    response = ws_client.get("/workspace/login")
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    assert '/workspace/assets/workspace.css' in response.text
    assert 'class="ws-auth"' in response.text
    assert 'class="app-nav"' not in response.text
    assert 'data-panel-user=' not in response.text
    assert 'action="/panel/' not in response.text
    assert 'href="/panel/database' not in response.text
    assert 'name="remember"' not in response.text
    assert 'aria-describedby="id-hint"' in response.text


async def test_request_rate_limits_survive_reload_and_sqlite_reopen(ws_client, staff_auth):
    for _ in range(7):
        assert request_code(ws_client).status_code == 200
    assert len(staff_auth.sent) == 5
    await db.init_db()
    assert request_code(ws_client).status_code == 200
    assert len(staff_auth.sent) == 5
    buckets = await db.many("SELECT bucket_key FROM staff_web_rate_buckets")
    assert len(buckets) >= 3
    for row in buckets:
        assert "7123456789" not in row["bucket_key"] and "testclient" not in row["bucket_key"]


async def test_otp_wrong_attempts_exhaust_atomically(staff_auth):
    now = access._now()
    browser, peer = access.staff_web_hash("test", "browser"), access.staff_web_hash("test", "peer")
    delivery = await access.request_staff_web_code("7123456789", browser, peer, now=now, browser_expires=now + 600)
    wrong = "00000000" if delivery["code"] != "00000000" else "11111111"
    results = await asyncio.gather(*[access.verify_staff_web_code(delivery["challenge"], wrong, browser, peer, now=now)
                                     for _ in range(8)])
    assert results == [None] * 8
    assert await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now) is None
    row = await db.one("SELECT attempts,consumed_at FROM staff_web_login_challenges")
    assert row["attempts"] == 5 and row["consumed_at"]


async def test_otp_one_use_concurrent_and_browser_bound(staff_auth):
    now = access._now()
    browser, peer = access.staff_web_hash("test", "browser"), access.staff_web_hash("test", "peer")
    other = access.staff_web_hash("test", "other")
    delivery = await access.request_staff_web_code("7123456789", browser, peer, now=now, browser_expires=now + 600)
    assert await access.verify_staff_web_code(delivery["challenge"], delivery["code"], other, peer, now=now) is None
    values = await asyncio.gather(*[access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now)
                                    for _ in range(4)])
    assert len([item for item in values if item]) == 1
    assert len(await db.many("SELECT * FROM staff_web_sessions")) == 1


async def test_otp_absolute_expiry_and_domain_separation(staff_auth):
    now = access._now()
    browser, peer = access.staff_web_hash("test", "browser"), access.staff_web_hash("test", "peer")
    delivery = await access.request_staff_web_code("7123456789", browser, peer, now=now, browser_expires=now + 600)
    row = await db.one("SELECT * FROM staff_web_login_challenges")
    assert row["expires_at"] == now + 300
    assert len({access.staff_web_hash(purpose, "same") for purpose in ("code", "session", "challenge", "browser", "peer")}) == 5
    assert await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now + 300) is None


async def test_identifier_browser_and_peer_rates_are_independent(staff_auth):
    now = access._now()
    peer = access.staff_web_hash("test", "peer")
    for i in range(6):
        browser = access.staff_web_hash("test", f"browser-{i}")
        result = await access.request_staff_web_code("7123456789", browser, peer, now=now, browser_expires=now + 600)
        assert bool(result["code"]) is (i < 5)
    await db.run("DELETE FROM staff_web_rate_buckets")
    browser = access.staff_web_hash("test", "browser")
    for i in range(6):
        result = await access.request_staff_web_code(str(8000000000 + i), browser, peer, now=now, browser_expires=now + 600)
    count = await db.one("SELECT COUNT(*) AS n FROM staff_web_login_challenges WHERE browser_hash=?", (browser,))
    assert count["n"] == 5
    await db.run("DELETE FROM staff_web_rate_buckets")
    before = len(await db.many("SELECT * FROM staff_web_login_challenges"))
    for i in range(32):
        await access.request_staff_web_code(str(9000000000 + i), access.staff_web_hash("test", f"new-{i}"), peer,
                                           now=now, browser_expires=now + 600)
    assert len(await db.many("SELECT * FROM staff_web_login_challenges")) == before + 30


async def test_delivery_failure_and_debug_logs_never_expose_secrets(ws_client, staff_auth, monkeypatch, caplog):
    page = ws_client.get("/workspace/login")
    cookie = ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE)
    secrets_seen = []

    async def failed_send(uid, text, keyboard=None):
        secrets_seen.extend([str(uid), re.search(r"\b[0-9]{8}\b", text)[0]])
        logging.getLogger("httpx").info("POST https://max.invalid/messages?user_id=%s %s", uid, text)
        raise RuntimeError("секретная ошибка " + str(uid) + " " + text)

    monkeypatch.setattr(bot_common.api, "send", failed_send)
    caplog.set_level(logging.DEBUG)
    response = ws_client.post("/workspace/login/request", data={"csrf": hidden(page, "csrf"), "user_id": "7123456789"},
                              headers={"Origin": "https://college.example", "User-Agent": "PRIVATE-UA-9227",
                                       "X-Forwarded-For": "198.51.100.177"})
    assert response.status_code == 200 and auth.NEUTRAL_REQUEST in response.text
    assert "RuntimeError" in caplog.text and "секретная ошибка" not in caplog.text
    challenge = hidden(response, "challenge")
    for value in [*secrets_seen, cookie, challenge, "PRIVATE-UA-9227", "198.51.100.177"]:
        assert value not in caplog.text
    assert (await db.one("SELECT consumed_at FROM staff_web_login_challenges"))["consumed_at"] > 0


async def test_sessions_absolute_expiry_and_secret_rotation(ws_client, staff_auth, monkeypatch):
    token = login_staff(ws_client, staff_auth)
    session_hash = access.staff_web_session_hash(token)
    row = await db.one("SELECT expires_at FROM staff_web_sessions")
    monkeypatch.setattr(access, "_now", lambda: row["expires_at"] - 1)
    assert await access.staff_web_principal(session_hash)
    ws_client.get("/workspace/_auth_probe")
    assert (await db.one("SELECT expires_at FROM staff_web_sessions"))["expires_at"] == row["expires_at"]
    monkeypatch.setattr(access, "_now", lambda: row["expires_at"])
    assert await access.staff_web_principal(session_hash) is None
    monkeypatch.setattr(config, "WEB_SESSION_SECRET", "changed-secret-" * 4)
    assert ws_client.get("/workspace/_auth_probe").status_code == 303


@pytest.mark.parametrize("kind", ["oversized", "multipart", "duplicate", "encoding"])
async def test_login_body_limit_and_parser(ws_client, kind):
    page = ws_client.get("/workspace/login")
    payload = urlencode({"csrf": hidden(page, "csrf"), "user_id": "7123456789"})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    expected = 400
    if kind == "oversized":
        payload += "&padding=" + "x" * auth.MAX_LOGIN_BODY
        headers["Content-Length"] = "1"
        expected = 413
    elif kind == "multipart":
        headers["Content-Type"] = "multipart/form-data"
        expected = 415
    elif kind == "duplicate":
        payload += "&user_id=7123456789"
    else:
        payload += "&broken=%ZZ"
    response = ws_client.post("/workspace/login/request", content=payload, headers=headers)
    assert response.status_code == expected


@pytest.mark.parametrize("bad,expected", [("oversized", 413), ("multipart", 415), ("encoding", 415), ("duplicate", 400), ("null", 403)])
async def test_staff_post_body_is_bounded_before_form_parse(ws_client, staff_auth, bad, expected):
    token = login_staff(ws_client, staff_auth)
    page = ws_client.get("/workspace/_auth_probe")
    payload = urlencode({"csrf": hidden(page, "csrf")})
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://college.example"}
    if bad == "oversized":
        payload += "&padding=" + "x" * auth.MAX_STAFF_BODY
        headers["Content-Length"] = "1"
    elif bad == "multipart":
        headers["Content-Type"] = "multipart/form-data"
    elif bad == "encoding":
        headers["Content-Encoding"] = "gzip"
    elif bad == "duplicate":
        payload += "&csrf=second"
    else:
        headers["Origin"] = "null"
    response = ws_client.post("/workspace/logout", content=payload, headers=headers)
    assert response.status_code == expected
    assert await access.staff_web_principal(access.staff_web_session_hash(token))


async def test_secure_cookie_uses_trusted_config_not_forwarded_headers(staff_auth):
    app = FastAPI()
    app.include_router(workspace_router)
    client = TestClient(app, base_url="http://internal.example", follow_redirects=False)
    response = client.get("/workspace/login", headers={"X-Forwarded-Proto": "http"})
    assert "Secure" in response.headers["set-cookie"]
    assert response.headers["referrer-policy"] == "same-origin"


async def test_successful_login_debug_logs_hide_token_hash_code_and_id(ws_client, staff_auth, caplog):
    page = ws_client.get("/workspace/login")
    browser_cookie = ws_client.cookies.get(auth.WORKSPACE_BROWSER_COOKIE)
    caplog.set_level(logging.DEBUG)
    response = ws_client.post("/workspace/login/request", data={"csrf": hidden(page, "csrf"), "user_id": "7123456789"})
    code, challenge = last_code(staff_auth), hidden(response, "challenge")
    assert verify_code(ws_client, response, code).status_code == 303
    token = ws_client.cookies.get(auth.STAFF_WEB_COOKIE)
    await access.staff_web_principal(access.staff_web_session_hash(token))
    for value in ("7123456789", code, challenge, token, access.staff_web_session_hash(token), browser_cookie):
        assert value not in caplog.text


async def test_otp_expiry_rechecked_after_waiting_for_transaction(staff_auth, monkeypatch):
    now = access._now()
    browser, peer = access.staff_web_hash("test", "browser"), access.staff_web_hash("test", "peer")
    delivery = await access.request_staff_web_code("7123456789", browser, peer, now=now, browser_expires=now + 600)
    times = iter((now + 299, now + 300))
    monkeypatch.setattr(access, "_now", lambda: next(times))
    assert await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer) is None
    assert not await db.many("SELECT * FROM staff_web_sessions")


async def test_browser_expiry_rechecked_after_waiting_for_transaction(staff_auth, monkeypatch):
    now = access._now()
    browser, peer = access.staff_web_hash("test", "browser"), access.staff_web_hash("test", "peer")
    times = iter((now + 599, now + 600))
    monkeypatch.setattr(access, "_now", lambda: next(times))
    result = await access.request_staff_web_code("7123456789", browser, peer, browser_expires=now + 600)
    assert not result["code"]
    assert not await db.many("SELECT * FROM staff_web_login_challenges")


async def test_login_help_explains_separate_access_without_enumeration(ws_client, staff_auth):
    for response in (ws_client.get("/workspace/login"), request_code(ws_client, "7555555555")):
        assert "Код не приходит?" in response.text
        assert "Приглашение в MAX не включает веб-доступ" in response.text
        assert 'href="/panel/login"' in response.text
        assert "подождите 10 минут" in response.text
        assert "7555555555" not in response.text
    assert not staff_auth.sent


async def test_auth_composition_keeps_real_forms_and_no_private_demo_data(ws_client, staff_auth):
    for response, step, action in (
        (ws_client.get("/workspace/login"), "Шаг 1 из 2", "/workspace/login/request"),
        (request_code(ws_client), "Шаг 2 из 2", "/workspace/login/verify"),
    ):
        header = re.search(r'<header class="ws-auth-header">(.*?)</header>', response.text, re.S)[1]
        assert header.count('class="theme-toggle ws-auth-theme"') == 1
        assert 'class="ws-auth-brand"' in header
        assert 'class="ws-auth-story"' in response.text and 'aria-label="Как устроена работа с обращением"' in response.text
        assert step in response.text and f'action="{action}"' in response.text
        assert 'class="ws-auth-input-group"' in response.text
        assert len(hidden(response, "csrf")) == 64
        assert 'class="app-nav"' not in response.text
        assert "7123456789" not in response.text
    assert len(staff_auth.sent) == 1


async def test_auth_error_uses_the_same_composition(ws_client):
    response = ws_client.post("/workspace/login/request", data={"csrf": "wrong", "user_id": "1"})
    assert response.status_code == 403
    assert 'class="ws-auth-card"' in response.text and 'class="ws-auth-header"' in response.text
    assert 'id="auth-title">Не удалось войти' in response.text
    assert "Открыть новую форму входа" in response.text
