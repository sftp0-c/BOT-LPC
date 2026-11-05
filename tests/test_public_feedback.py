"""Анонимная обратная связь: CSRF, приватность, скользящие лимиты и закрытая очередь."""
import asyncio
import hashlib
import html
import logging
import re
import sqlite3
from pathlib import Path
import shutil
import subprocess
from types import ModuleType
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import bot
import version
import config
import database as db
import repository as repo
from web import common, feedback
from store import feedback as feedback_store

NOW = 1_793_888_000
FORM_URL = "https://feedback.example"
BODY = "Нужно улучшить отображение расписания."


@pytest.fixture
def client(monkeypatch, env):
    monkeypatch.setattr(config, "PUBLIC_URL", FORM_URL)
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-panel-pass")
    monkeypatch.setattr(feedback, "_now", lambda: NOW)
    monkeypatch.setattr(feedback_store, "_now", lambda: feedback._now())
    return TestClient(bot.app, base_url=FORM_URL, follow_redirects=False)


def hidden(response, name):
    match = re.search(r'name="' + re.escape(name) + r'" value="([^"]+)"', response.text)
    assert match, name
    return html.unescape(match.group(1))


def fields(client):
    response = client.get("/feedback")
    assert response.status_code == 200
    return {"kind": "bug", "description": BODY, "website": "",
            "csrf": hidden(response, "csrf"), "submission_nonce": hidden(response, "submission_nonce")}


def authenticate(client, uid="1"):
    token = common.start_session(uid)
    client.cookies.set(common.COOKIE, token, path="/")
    return common._csrf_for(token)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


async def test_form_separate_cookie_headers_and_no_database(client, monkeypatch):
    async def no_db(*args, **kwargs):
        raise AssertionError("Публичный GET не должен читать базу")

    monkeypatch.setattr(db, "one", no_db)
    monkeypatch.setattr(db, "many", no_db)
    root = client.get("/")
    assert root.status_code == 200 and "set-cookie" not in root.headers
    first = client.get("/feedback")
    assert first.status_code == 200
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    second = client.get("/feedback")
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie
    assert hidden(first, "submission_nonce") != hidden(second, "submission_nonce")
    assert hidden(first, "csrf") == hidden(second, "csrf") != cookie
    assert hidden(first, "submission_nonce") != cookie
    header = first.headers["set-cookie"]
    for value in ("HttpOnly", "SameSite=lax", "Secure", "Path=/feedback", "Max-Age=7200"):
        assert value in header
    assert common.COOKIE not in client.cookies
    assert not common._decode_session(cookie)
    csp = first.headers["content-security-policy"]
    for value in ("form-action 'self'", "connect-src 'none'", "script-src 'self'", "style-src 'self'"):
        assert value in csp
    assert "unsafe-inline" not in csp
    assert f'<script src="/feedback/assets/feedback.js?v={version.__version__}" defer></script>' in first.text
    assert first.text.count("<script") == 1 and "<style" not in first.text and "style=" not in first.text
    assert f'href="/public/landing.css?v={version.__version__}"' in first.text
    assert "Бета-тестирование" in first.text
    assert first.headers["cache-control"] == "no-store"
    # no-referrer превращает Origin обычной POST-формы браузера в null.
    assert first.headers["referrer-policy"] == "same-origin"
    assert 'name="user_id"' not in first.text and 'type="file"' not in first.text
    thanks = client.get("/feedback/thanks")
    assert thanks.status_code == 200 and "set-cookie" not in thanks.headers


@pytest.mark.parametrize("url,configured,secure", [
    ("http://testserver", "", False),
    ("https://testserver", "", True),
    ("http://testserver", "https://feedback.example/base", True),
])
def test_cookie_secure_for_request_or_config(client, monkeypatch, url, configured, secure):
    monkeypatch.setattr(config, "PUBLIC_URL", configured)
    other = TestClient(bot.app, base_url=url)
    response = other.get("/feedback")
    assert ("; Secure" in response.headers["set-cookie"]) is secure


@pytest.mark.parametrize("bad", ["", "короткий", " " * 64, None])
def test_no_secret_fallback(client, monkeypatch, bad):
    monkeypatch.setattr(config, "WEB_SESSION_SECRET", bad)
    for response in (client.get("/feedback"), client.post("/feedback", data={"description": BODY})):
        assert response.status_code == 503
        assert "временно недоступна" in response.text and "Traceback" not in response.text
        assert "set-cookie" not in response.headers
        assert "test-panel-pass" not in response.text


async def test_csrf_cookie_binding_expiry_and_nonce_purpose(client, monkeypatch):
    form = fields(client)
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    for changed in ({"csrf": ""}, {"csrf": "чужой"}, {"submission_nonce": cookie}):
        assert client.post("/feedback", data={**form, **changed}).status_code == 403
    other = TestClient(bot.app, base_url=FORM_URL, follow_redirects=False)
    other_form = fields(other)
    assert other.post("/feedback", data={**other_form, "csrf": form["csrf"]}).status_code == 403
    assert other.post("/feedback", data={**other_form, "submission_nonce": form["submission_nonce"]}).status_code == 403
    monkeypatch.setattr(feedback, "_now", lambda: NOW + feedback.FEEDBACK_TTL)
    assert client.post("/feedback", data=form).status_code == 403
    fresh = fields(client)
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) != cookie
    assert client.post("/feedback", data=fresh).status_code == 303
    assert await repo.count_public_feedback() == 1


async def test_panel_session_cannot_author_public_feedback(client):
    panel_csrf = authenticate(client)
    panel_cookie = client.cookies.get(common.COOKIE)
    client.cookies.set(feedback.FEEDBACK_COOKIE, panel_cookie, path="/feedback")
    response = client.post("/feedback", data={"csrf": panel_csrf, "kind": "bug",
                                             "description": BODY, "submission_nonce": panel_cookie})
    assert response.status_code == 403
    assert await repo.count_public_feedback() == 0


@pytest.mark.parametrize("origin,expected", [
    (None, 303), (FORM_URL, 303), (FORM_URL + ":443", 303),
    ("https://evil.example", 403), ("null", 403), ("http://feedback.example", 403),
    (FORM_URL + "/path", 403), (FORM_URL + "/", 403), (FORM_URL + ":444", 403),
])
async def test_optional_origin_only_trusted_config(client, origin, expected):
    form = fields(client)
    headers = {"Origin": origin} if origin is not None else {}
    assert client.post("/feedback", data=form, headers=headers).status_code == expected
    assert await repo.count_public_feedback() == (1 if expected == 303 else 0)


async def test_unconfigured_origin_does_not_trust_host_or_forwarded(client, monkeypatch):
    form = fields(client)
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    response = client.post("/feedback", data=form, headers={
        "Origin": FORM_URL, "Host": "feedback.example", "X-Forwarded-Host": "feedback.example"})
    assert response.status_code == 403
    assert await repo.count_public_feedback() == 0


@pytest.mark.parametrize("patch,expected", [
    ({"description": "123456789"}, 422), ({"description": "1234567890"}, 303),
    ({"description": "ю" * 4000}, 303), ({"description": "ю" * 4001}, 422),
    ({"description": " " * 20}, 422), ({"description": "привет\x00скрыто"}, 422),
    ({"kind": "suggestion"}, 303), ({"kind": "BUG"}, 422),
    ({"kind": "<script>alert(1)</script>"}, 422), ({"website": "заполнено"}, 400),
    ({"user_id": "123"}, 400), ({"contact": "адрес"}, 400),
])
async def test_validation_and_no_extra_identity_fields(client, patch, expected):
    response = client.post("/feedback", data={**fields(client), **patch})
    assert response.status_code == expected
    assert await repo.count_public_feedback() == (1 if expected == 303 else 0)


@pytest.mark.parametrize("extra", ["&kind=bug", "&csrf=x", "&body=ещё", "&website=", "&x=1" * 10, "&bad=%ZZ"])
async def test_duplicate_ambiguous_and_bounded_form_fields(client, extra):
    response = client.post("/feedback", content=(urlencode(fields(client)) + extra).encode(),
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400
    assert await repo.count_public_feedback() == 0


async def test_body_alias(client):
    form = fields(client)
    form["body"] = form.pop("description")
    assert client.post("/feedback", data=form).status_code == 303
    assert (await repo.get_public_feedback(1))["body"] == BODY


@pytest.mark.parametrize("mode", ["json", "multipart", "text"])
async def test_urlencoded_only(client, mode):
    form = fields(client)
    if mode == "json":
        response = client.post("/feedback", json=form)
    elif mode == "multipart":
        response = client.post("/feedback", data=form, files={"file": ("test.txt", b"secret")})
    else:
        response = client.post("/feedback", content=urlencode(form), headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert await repo.count_public_feedback() == 0


@pytest.mark.parametrize("declared_length", [None, "1", "999999999"])
async def test_actual_body_bytes_before_parse_and_no_payload_logs(client, monkeypatch, caplog, declared_length):
    form = fields(client)
    marker = "НЕ_ЖУРНАЛИРОВАТЬ_ПОЛЕ_76091"
    raw = (urlencode(form) + "&description=" + marker * 4000).encode()
    chunks = [raw[:32000], raw[32000:64000], raw[64000:80000], raw[80000:]]
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    headers = [(b"content-type", b"application/x-www-form-urlencoded"),
               (b"cookie", f"lpc_feedback={cookie}".encode())]
    if declared_length is not None:
        headers.append((b"content-length", declared_length.encode()))
    scope = {"type": "http", "method": "POST", "scheme": "https", "path": "/feedback", "query_string": b"",
             "headers": headers, "client": ("203.0.113.11", 3210), "server": ("feedback.example", 443)}
    consumed = []

    async def receive():
        chunk = chunks[len(consumed)]
        consumed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": len(consumed) < len(chunks)}

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("Слишком большое тело попало в parse_qs")

    monkeypatch.setattr(feedback, "parse_qs", forbidden_parse)
    caplog.set_level(logging.DEBUG)
    response = await feedback.public_feedback_submit(Request(scope, receive))
    assert response.status_code == 413
    assert len(consumed) == 3
    assert marker not in caplog.text and marker.encode() not in response.body
    assert cookie not in caplog.text and form["csrf"] not in caplog.text
    assert await repo.count_public_feedback() == 0


async def test_exact_body_limit_and_lying_length(client):
    form = fields(client)
    # Пробелы в конце единственного текстового поля убираются после разбора.
    form.pop("description")
    prefix = (urlencode(form) + "&description=1234567890").encode()
    raw = prefix + b" " * (feedback.MAX_BODY_BYTES - len(prefix))
    response = client.post("/feedback", content=raw,
                           headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "1"})
    assert response.status_code == 303
    assert (await repo.get_public_feedback(1))["body"] == "1234567890"


async def test_replay_immutable_new_nonce_and_cookie_limit(client, monkeypatch):
    form = fields(client)
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    assert client.post("/feedback", data=form).status_code == 303
    changed = {**form, "kind": "suggestion", "description": "Другой текст для прежнего nonce"}
    assert client.post("/feedback", data=changed).status_code == 303
    item = await repo.get_public_feedback(1)
    assert item["body"] == BODY and item["kind"] == "bug"
    for index in range(1, 4):
        monkeypatch.setattr(feedback, "_now", lambda value=NOW + index * 30: value)
        assert client.post("/feedback", data=fields(client)).status_code == 303
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie
    assert await repo.count_public_feedback() == 4
    limited = client.post("/feedback", data=fields(client))
    assert limited.status_code == 429 and limited.headers["retry-after"] == "510"
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 600)
    assert client.post("/feedback", data=fields(client)).status_code == 303


async def test_failed_csrf_attempts_count_and_get_does_not_reset(client):
    form = fields(client)
    for _ in range(5):
        assert client.post("/feedback", data={**form, "csrf": "неверный"}).status_code == 403
    assert client.post("/feedback", data=fields(client)).status_code == 429
    assert await repo.count_public_feedback() == 0


async def test_peer_limit_cannot_be_bypassed_with_cookie_or_xff(client):
    for index in range(31):
        client.cookies.clear()
        form = fields(client)
        response = client.post("/feedback", data=form, headers={"X-Forwarded-For": f"8.8.8.{index}"})
        assert response.status_code == (303 if index < 30 else 429)
    assert await repo.count_public_feedback() == 30
    buckets = [dict(row) for row in await db.many("SELECT * FROM public_feedback_rate_buckets")]
    assert "8.8.8." not in repr(buckets) and "testclient" not in repr(buckets)


async def test_global_cap_and_ttl_prune(env):
    for index in range(121):
        result, wait = await repo.submit_public_feedback(
            now=NOW, peer_key=digest(index), cookie_key=digest(f"cookie-{index}"), shared_peer=True)
        assert result == ("ignored" if index < 120 else "limited")
    assert wait == 3600
    assert await repo.prune_public_feedback_rates(NOW + 3600) > 0
    assert not await db.many("SELECT * FROM public_feedback_rate_buckets")


async def test_peer_rules_do_not_read_forwarded_identity(env):
    for client_host, headers, shared in [
        ("8.8.8.8", [], False), ("127.0.0.1", [], True),
        ("8.8.8.8", [(b"x-forwarded-for", b"1.1.1.1")], True),
    ]:
        request = Request({"type": "http", "headers": headers, "client": (client_host, 1)})
        assert feedback._peer(request) == (client_host, shared)
    assert feedback._peer(Request({"type": "http", "headers": []})) == ("unknown", True)


async def test_atomic_concurrent_submissions_rate_and_replay(client):
    transport = httpx.ASGITransport(app=bot.app, client=("8.8.8.8", 1234))
    async with httpx.AsyncClient(transport=transport, base_url=FORM_URL) as parallel:
        response = await parallel.get("/feedback")
        base = {"kind": "bug", "description": BODY, "website": "", "csrf": hidden(response, "csrf")}
        forms = [{**base, "submission_nonce": hidden(response, "submission_nonce")}]
        for _ in range(6):
            forms.append({**base, "submission_nonce": hidden(await parallel.get("/feedback"), "submission_nonce")})
        responses = await asyncio.gather(*(parallel.post("/feedback", data=form) for form in forms))
        assert sorted(response.status_code for response in responses) == [303] + [429] * 6
    assert await repo.count_public_feedback() == 1
    # Отдельный посетитель одновременно повторяет один и тот же nonce.
    transport = httpx.ASGITransport(app=bot.app, client=("1.1.1.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url=FORM_URL) as parallel:
        response = await parallel.get("/feedback")
        form = {"kind": "suggestion", "description": BODY, "csrf": hidden(response, "csrf"),
                "submission_nonce": hidden(response, "submission_nonce")}
        responses = await asyncio.gather(*(parallel.post("/feedback", data=form) for _ in range(5)))
        assert all(response.status_code == 303 for response in responses)
    assert await repo.count_public_feedback() == 2


async def test_anonymous_storage_xss_no_sends_or_secret_logs(client, env, caplog):
    form = fields(client)
    payload = '</textarea><script>alert("секрет-78901")</script><img src=x onerror=alert(1)>'
    caplog.set_level(logging.DEBUG)
    response = client.post("/feedback", data={**form, "description": payload},
                           headers={"User-Agent": "PRIVATE_UA_78901"})
    assert response.status_code == 303 and response.headers["location"] == "/feedback/thanks"
    row = dict(await db.one("SELECT * FROM public_feedback"))
    assert row["body"] == payload and row["handled_by"] is None and row["status"] == "new"
    assert set(row) == {"id", "kind", "body", "status", "created_at", "updated_at", "handled_by", "submission_nonce"}
    assert not env.sent
    assert not await db.many("SELECT * FROM admin_log")
    assert not await db.many("SELECT * FROM audit_log")
    assert not await db.many("SELECT * FROM contacts")
    assert await db.one("SELECT * FROM tickets") is None
    assert client.get("/feedback/1").status_code == 404
    authenticate(client)
    response = client.get("/panel/feedback/1")
    assert response.status_code == 200
    assert payload not in response.text and html.escape(payload, quote=True) in response.text
    assert form["submission_nonce"] not in response.text
    for secret in (payload, "секрет-78901", "PRIVATE_UA_78901", form["csrf"],
                   form["submission_nonce"], config.WEB_SESSION_SECRET):
        assert secret not in caplog.text


async def test_validation_error_escapes_user_content(client):
    form = fields(client)
    payload = "</textarea><script>bad()</script>"
    response = client.post("/feedback", data={**form, "kind": "no", "description": payload})
    assert response.status_code == 422
    assert payload not in response.text and html.escape(payload) in response.text
    assert hidden(response, "submission_nonce") == form["submission_nonce"]


async def test_closed_admin_access_status_and_filters(client):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    for path in ("/panel/feedback", "/panel/feedback/1"):
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/panel/login"
    authenticate(client, "200")
    assert client.get("/panel/feedback").status_code == 403
    assert client.post("/panel/feedback/1/status", data={"status": "closed"}).status_code == 403
    csrf = authenticate(client)
    assert client.get("/panel/feedback").status_code == 200
    assert client.get("/panel/feedback?status=bad").status_code == 400
    assert client.get("/panel/feedback?status=new&status=closed").status_code == 400
    assert client.get("/panel/feedback?page=-1").status_code == 400
    path = "/panel/feedback/1/status"
    assert client.post(path, data={"status": "closed"}).status_code == 403
    for status in ("new", "in_progress", "closed"):
        assert client.post(path, data={"csrf": csrf, "status": status}).status_code == 303
        row = await repo.get_public_feedback(1)
        assert row["status"] == status and row["handled_by"] == "1"
    for status in ("ready", "", "CLOSED", "closed' OR 1=1--"):
        assert client.post(path, data={"csrf": csrf, "status": status}).status_code == 400
    duplicate = urlencode({"csrf": csrf, "status": "new"}) + "&status=closed"
    assert client.post(path, content=duplicate, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert (await repo.get_public_feedback(1))["status"] == "closed"
    assert client.get("/panel/feedback/9999").status_code == 404
    assert client.post("/panel/feedback/9999/status", data={"csrf": csrf, "status": "closed"}).status_code == 404
    assert "№1</a>" not in client.get("/panel/feedback?status=new").text
    assert "№1</a>" in client.get("/panel/feedback?status=closed").text


async def test_database_and_store_enforce_status_kind_length_nonce(env):
    for kind, body, status in (("other", BODY, "new"), ("bug", "коротко", "new"), ("bug", BODY, "ready")):
        with pytest.raises(sqlite3.IntegrityError):
            await db.run("INSERT INTO public_feedback(kind, body, status, submission_nonce) VALUES(?, ?, ?, ?)",
                         (kind, body, status, "nonce"))
    assert await repo.submit_public_feedback(now=NOW, peer_key=digest("peer"), cookie_key=digest("cookie"),
                                             kind="bug", body=BODY, submission_nonce="nonce") == ("created", 1)
    with pytest.raises(sqlite3.IntegrityError):
        await db.run("INSERT INTO public_feedback(kind, body, submission_nonce) VALUES('bug', ?, 'nonce')", (BODY,))
    for func, args in ((repo.list_public_feedback, ("bad",)), (repo.count_public_feedback, ("bad",)),
                       (repo.set_public_feedback_status, (1, "bad", "1"))):
        with pytest.raises(ValueError):
            await func(*args)
    before = dict(await repo.get_public_feedback(1))
    await db.init_db()
    await db.init_db()
    assert dict(await repo.get_public_feedback(1)) == before


@pytest.mark.parametrize("kind", ["bug", "suggestion"])
def test_kind_selected_by_landing_link(client, kind):
    response = client.get("/feedback?kind=" + kind)
    assert f'value="{kind}" checked' in response.text
    assert response.text.count(' checked') == 1


async def test_admin_status_audit_contains_only_id_and_no_body(client):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    csrf = authenticate(client)
    response = client.post("/panel/feedback/1/status", data={"csrf": csrf, "status": "in_progress"})
    assert response.status_code == 303
    item = await db.one("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1")
    assert item["action"] == "public_feedback_status"
    assert item["target_type"] == "public_feedback" and item["target_id"] == "1"
    assert item["result"] == "ok"
    assert BODY not in repr(dict(item)) and "in_progress" not in repr(dict(item))
    assert not await db.many("SELECT * FROM admin_log")


async def test_feedback_raw_data_read_only_and_nonce_hidden(env):
    from web import data

    assert {"public_feedback", "public_feedback_rate_buckets"} <= set(data.READ_ONLY_TABLES)
    columns = await data.table_columns("public_feedback")
    assert "submission_nonce" not in {column["name"] for column in columns}
    assert "body" in {column["name"] for column in columns}


async def test_cooldown_boundary_keeps_text_cookie_and_nonce(client, monkeypatch):
    form = fields(client)
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    first = client.post("/feedback", data=form)
    assert first.status_code == 303
    wait_cookie = client.cookies.get(feedback.FEEDBACK_WAIT_COOKIE)
    assert wait_cookie and client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie
    assert feedback.FEEDBACK_COOKIE + "=" not in first.headers["set-cookie"]
    again = fields(client)
    await_page = client.get("/feedback")
    assert 'data-wait-seconds="30"' in await_page.text
    assert "disabled" not in await_page.text  # Без JS остаётся рабочая форма, сервер всё равно защищает.
    malicious = '</textarea><script>НЕ_ИСПОЛНЯТЬ_ТЕКСТ</script>'
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 29)
    limited = client.post("/feedback", data={**again, "description": malicious})
    assert limited.status_code == 429 and limited.headers["retry-after"] == "1"
    assert html.escape(malicious) in limited.text and malicious not in limited.text
    assert hidden(limited, "submission_nonce") == again["submission_nonce"]
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie
    assert await repo.count_public_feedback() == 1
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 30)
    assert client.post("/feedback", data=again).status_code == 303
    assert await repo.count_public_feedback() == 2


async def test_replay_and_new_ui_cookie_do_not_reset_cooldown(client, monkeypatch):
    form = fields(client)
    assert client.post("/feedback", data=form).status_code == 303
    marker = dict(await db.one("SELECT * FROM public_feedback_rate_buckets WHERE bucket_key LIKE 'cooldown:%'"))
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 10)
    assert client.post("/feedback", data=form).status_code == 303
    assert dict(await db.one("SELECT * FROM public_feedback_rate_buckets WHERE bucket_key LIKE 'cooldown:%'")) == marker
    # Удаление UI-подсказки убирает лишь таймер, не серверную паузу.
    for item in list(client.cookies.jar):
        if item.name == feedback.FEEDBACK_WAIT_COOKIE:
            client.cookies.delete(item.name, domain=item.domain, path=item.path)
    limited = client.post("/feedback", data=fields(client))
    assert limited.status_code == 429 and limited.headers["retry-after"] == "20"
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie


async def test_cooldown_does_not_block_another_person_at_same_peer(client):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    other = TestClient(bot.app, base_url=FORM_URL, follow_redirects=False)
    assert other.post("/feedback", data=fields(other)).status_code == 303
    assert await repo.count_public_feedback() == 2


async def test_untrusted_wait_hint_not_an_authorization_or_sliding_cookie(client, monkeypatch):
    fields(client)
    cookie = client.cookies.get(feedback.FEEDBACK_COOKIE)
    client.cookies.set(feedback.FEEDBACK_WAIT_COOKIE, "forged", path="/feedback")
    page = client.get("/feedback")
    assert 'data-wait-seconds="0"' in page.text
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 200)
    page = client.get("/feedback")
    assert "Max-Age=7000" in page.headers["set-cookie"]
    assert client.cookies.get(feedback.FEEDBACK_COOKIE) == cookie


async def test_global_cap_applies_without_shared_peer(client):
    for index in range(121):
        result, wait = await repo.submit_public_feedback(now=NOW, peer_key=digest(index), shared_peer=False)
        assert result == ("ignored" if index < 120 else "limited")
    assert wait == 3600


async def test_cooldown_and_expiry_use_clock_after_sqlite_wait(client, monkeypatch):
    peer, cookie = digest("peer"), digest("cookie")
    assert await repo.submit_public_feedback(now=NOW, peer_key=peer, cookie_key=cookie,
                                             kind="bug", body=BODY, submission_nonce="first") == ("created", 1)
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(feedback_store, "_now", lambda: NOW + 29)
        task = asyncio.create_task(repo.submit_public_feedback(peer_key=peer, cookie_key=cookie,
                                                               kind="bug", body=BODY, submission_nonce="second"))
        await asyncio.sleep(0.05)
        monkeypatch.setattr(feedback_store, "_now", lambda: NOW + 30)
        await conn.commit()
    assert await task == ("created", 2)
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(repo.submit_public_feedback(peer_key=peer, cookie_key=cookie,
                                                               kind="bug", body=BODY, submission_nonce="expired",
                                                               expires_at=NOW + 31))
        await asyncio.sleep(0.05)
        monkeypatch.setattr(feedback_store, "_now", lambda: NOW + 31)
        await conn.commit()
    assert await task == ("expired", 0)
    assert await repo.count_public_feedback() == 2


def delete_fields(client, feedback_id=1):
    csrf = authenticate(client)
    return {"csrf": csrf, "confirm": "УДАЛИТЬ", "feedback_id": str(feedback_id)}


async def test_delete_confirmation_escapes_text_and_deletes_only_feedback(client, env):
    form = fields(client)
    body = '<script>Приватный отзыв 80418</script>'
    assert client.post("/feedback", data={**form, "description": body}).status_code == 303
    await db.run("INSERT INTO public_feedback(kind,body,submission_nonce) VALUES('bug',?,'other')", (BODY,))
    tid = await repo.create_ticket("300", "200", "feedback", "Студенческое обращение не удалять")
    assert tid == 1
    data = delete_fields(client)
    confirm = client.get("/panel/feedback/1/delete")
    assert confirm.status_code == 200 and "Удалить отзыв №1?" in confirm.text
    assert "без возможности восстановления" in confirm.text
    assert body not in confirm.text and html.escape(body) in confirm.text
    assert form["submission_nonce"] not in confirm.text
    assert hidden(confirm, "feedback_id") == "1"
    response = client.post("/panel/feedback/1/delete", data=data)
    assert response.status_code == 303 and response.headers["location"] == "/panel/feedback"
    assert await repo.get_public_feedback(1) is None
    assert await repo.get_public_feedback(2) is not None
    assert await repo.get_ticket(tid) is not None
    assert not env.sent
    assert client.post("/panel/feedback/1/delete", data=data).status_code == 404
    assert client.get("/panel/feedback/1/delete").status_code == 404


@pytest.mark.parametrize("bad", ["csrf", "confirm", "wrong-id", "duplicate", "extra"])
async def test_delete_needs_csrf_unique_fields_and_confirmation(client, bad):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    data = delete_fields(client)
    if bad == "csrf":
        data["csrf"] = "wrong"
    elif bad == "confirm":
        data["confirm"] = "да"
    elif bad == "wrong-id":
        data["feedback_id"] = "2"
    elif bad == "extra":
        data["delete_all"] = "1"
    raw = urlencode(data) + ("&confirm=УДАЛИТЬ" if bad == "duplicate" else "")
    result = client.post("/panel/feedback/1/delete", content=raw.encode("utf-8"),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert result.status_code == (403 if bad == "csrf" else 400)
    assert await repo.get_public_feedback(1) is not None
    assert not await db.many("SELECT * FROM public_feedback_deleted_nonces")


async def test_delete_forbidden_without_admin_and_store_rechecks(client, monkeypatch):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    assert client.get("/panel/feedback/1/delete").status_code == 303
    assert client.post("/panel/feedback/1/delete", data={}).status_code == 303
    await db.run("INSERT INTO admins(user_id,full_name,role_type) VALUES('222','Тест сотрудника','staff')")
    data = {"csrf": authenticate(client, "222"), "confirm": "УДАЛИТЬ", "feedback_id": "1"}
    assert client.post("/panel/feedback/1/delete", data=data).status_code == 403
    assert await repo.delete_public_feedback(1, "222") == "denied"
    async with db._conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(repo.delete_public_feedback(1, "1"))
        await asyncio.sleep(0.05)
        await conn.execute("UPDATE admins SET role_type='staff' WHERE user_id='1'")
        await conn.commit()
    assert await pending == "denied"
    assert await repo.get_public_feedback(1) is not None


async def test_delete_audit_private_and_replay_never_resurrects(client, caplog, monkeypatch):
    form = fields(client)
    marker = "СЕКРЕТНЫЙ_ТЕКСТ_ОТЗЫВА_80491"
    form["description"] = marker
    assert client.post("/feedback", data=form).status_code == 303
    data = delete_fields(client)
    caplog.set_level(logging.DEBUG)
    assert client.post("/panel/feedback/1/delete", data=data).status_code == 303
    row = dict(await db.one("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1"))
    assert row["action"] == "public_feedback_delete" and row["target_type"] == "public_feedback"
    assert row["target_id"] == "1" and row["user_id"] == "1" and row["result"] == "ok"
    marker_row = dict(await db.one("SELECT * FROM public_feedback_deleted_nonces"))
    assert set(marker_row) == {"nonce_hash", "expires_at"}
    assert len(marker_row["nonce_hash"]) == 64 and marker_row["nonce_hash"] != form["submission_nonce"]
    assert marker_row["expires_at"] == NOW + feedback.FEEDBACK_TTL
    monkeypatch.setattr(feedback, "_now", lambda: NOW + 31)
    assert client.post("/feedback", data=form).status_code == 303
    assert await repo.count_public_feedback() == 0
    assert client.post("/panel/feedback/1/delete", data=data).status_code == 404
    assert (await db.one("SELECT result FROM audit_log ORDER BY id DESC LIMIT 1"))["result"] == "denied"
    assert not await db.many("SELECT * FROM admin_log")
    for secret in (marker, form["submission_nonce"], form["csrf"], marker_row["nonce_hash"]):
        assert secret not in caplog.text


async def test_delete_marker_and_removal_are_atomic(client):
    form = fields(client)
    assert client.post("/feedback", data=form).status_code == 303
    await db.run("CREATE TRIGGER fail_feedback_delete BEFORE DELETE ON public_feedback "
                 "BEGIN SELECT RAISE(ABORT,'синтетический отказ'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await repo.delete_public_feedback(1, "1")
    assert await repo.get_public_feedback(1) is not None
    assert not await db.many("SELECT * FROM public_feedback_deleted_nonces")


async def test_deleted_nonce_cleanup_and_raw_data_hidden(client, monkeypatch):
    from web import data
    assert client.post("/feedback", data=fields(client)).status_code == 303
    assert await repo.delete_public_feedback(1, "1") == "deleted"
    assert "public_feedback_deleted_nonces" not in await data.table_names()
    assert "public_feedback_deleted_nonces" not in data.READ_ONLY_TABLES
    assert "public_feedback_deleted_nonces" in db.EXPECTED_TABLES
    assert not await db.missing_objects()
    await repo.prune_public_feedback_rates(NOW + feedback.FEEDBACK_TTL)
    assert not await db.many("SELECT * FROM public_feedback_deleted_nonces")


async def test_feedback_script_is_local_and_does_not_change_main(client):
    response = client.get("/feedback/assets/feedback.js")
    assert response.status_code == 200 and "javascript" in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"
    root = client.get("/")
    assert "/feedback/assets/feedback.js" not in root.text
    assert client.get("/feedback/assets/.env").status_code == 404
    text = response.text
    assert "Date.now()" in text and "textContent" in text and "pageshow" in text
    assert not any(word in text for word in ("fetch(", "innerHTML", "localStorage", "eval("))


@pytest.mark.parametrize("mutation", ["cooldown", "delete-role", "deleted-replay"])
async def test_feedback_security_rules_detect_in_memory_mutations(client, mutation):
    source_path = Path(feedback_store.__file__)
    source = source_path.read_text(encoding="utf-8-sig")
    if mutation == "cooldown":
        source = source.replace('if cooldown["until"] and cooldown["until"] > now:', 'if False:')
    elif mutation == "delete-role":
        source = source.replace('if not actor or actor["is_test"] or actor["role_type"] not in ("owner", "sysadmin", "superadmin"):', 'if False:')
    else:
        source = source.replace('if existing or deleted:', 'if existing:')
    altered = ModuleType("feedback_mutated")
    exec(compile(source, str(source_path), "exec"), altered.__dict__)
    altered._now = lambda: NOW
    base = dict(now=NOW, peer_key=digest("peer"), cookie_key=digest("cookie"), kind="bug", body=BODY)
    assert await altered.submit_public_feedback(**base, submission_nonce="accepted-nonce") == ("created", 1)
    if mutation == "cooldown":
        base["now"] = NOW + 1
        result = await altered.submit_public_feedback(**base, submission_nonce="new-nonce")
        with pytest.raises(AssertionError):
            assert result[0] == "limited"
    elif mutation == "delete-role":
        await db.run("INSERT INTO admins(user_id,full_name,role_type) VALUES('333','Тест сотрудника','staff')")
        result = await altered.delete_public_feedback(1, "333")
        with pytest.raises(AssertionError):
            assert result == "denied"
    else:
        assert await altered.delete_public_feedback(1, "1") == "deleted"
        base["now"] = NOW + 31
        result = await altered.submit_public_feedback(**base, submission_nonce="accepted-nonce")
        with pytest.raises(AssertionError):
            assert result[0] == "replay" and await repo.count_public_feedback() == 0


def test_feedback_timer_unlocks_and_handles_background_tabs():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node не установлен")
    script = (Path(feedback.__file__).with_name("static") / "feedback.js").read_text(encoding="utf-8-sig")
    runner = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync(0, 'utf8');
let now = 100000, callback;
const hint = {dataset: {waitSeconds: '30'}, textContent: '', setAttribute(k,v) { this[k]=v; }};
const button = {disabled:false};
const listeners = {};
const window = {setInterval(fn) {callback=fn;return 1;}, clearInterval() {}, addEventListener(name,fn) {listeners[name]=fn;}};
const document = {querySelector(selector) {return selector==='[data-feedback-wait]' ? hint : button;}, addEventListener(name,fn) {listeners[name]=fn;}};
vm.runInNewContext(code, {window, document, Date: {now() {return now;}}, Number, Math});
assert.equal(button.disabled,true); assert.ok(hint.textContent.includes('30'));
now += 29000; callback(); assert.equal(button.disabled,true); assert.ok(hint.textContent.includes('1'));
now += 1000; callback(); assert.equal(button.disabled,false); assert.equal(hint.role,'status');
now += 60000; listeners.pageshow(); assert.equal(button.disabled,false);
"""
    result = subprocess.run([node, "-e", runner], input=script, text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr


async def test_delete_csrf_rule_detects_in_memory_mutation(client, monkeypatch):
    assert client.post("/feedback", data=fields(client)).status_code == 303
    data = delete_fields(client)
    data["csrf"] = "forged"
    monkeypatch.setattr(feedback, "require_form", common.require_user)
    result = client.post("/panel/feedback/1/delete", data=data)
    with pytest.raises(AssertionError):
        assert result.status_code == 403
