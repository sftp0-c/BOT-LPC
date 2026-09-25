import json

import httpx
import pytest

from max_api import MaxAPI, MaxAPIError, btn, split_text


def make_api(handler):
    return MaxAPI(token="TOKEN", base_url="https://platform-api2.max.ru", transport=httpx.MockTransport(handler))


async def test_send_request_shape():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"message": {}})

    api = make_api(handler)
    await api.send(123, "Привет", [[btn("Да", "yes")]])
    await api.close()
    req = calls[0]
    assert req.method == "POST" and req.url.path == "/messages"
    assert req.url.params["user_id"] == "123"
    assert req.headers["Authorization"] == "TOKEN"  # без Bearer
    body = json.loads(req.content)
    assert body["text"] == "Привет"
    assert body["attachments"][0]["type"] == "inline_keyboard"
    assert body["attachments"][0]["payload"]["buttons"] == [[{"type": "callback", "text": "Да", "payload": "yes"}]]


async def test_long_text_is_split_and_keyboard_on_last_part():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={})

    api = make_api(handler)
    await api.send(1, "строка\n" * 1500, [[btn("ok", "ok")]])
    await api.close()
    assert len(calls) >= 2
    assert all(len(c["text"]) <= 4000 for c in calls)
    assert "attachments" not in calls[0] and "attachments" in calls[-1]


async def test_updates_params_and_error_handling():
    def handler(request):
        assert request.url.path == "/updates"
        assert request.url.params["marker"] == "5" and request.url.params["timeout"] == "30"
        return httpx.Response(200, json={"updates": [], "marker": 6})

    api = make_api(handler)
    assert (await api.updates(5))["marker"] == 6
    await api.close()


async def test_http_error_raises():
    api = make_api(lambda r: httpx.Response(401, json={"message": "unauthorized"}))
    with pytest.raises(MaxAPIError) as info:
        await api.send(1, "x")
    assert info.value.status == 401
    await api.close()


async def test_send_is_not_retried_on_connect_error():
    """POST /messages не повторяется при обрыве: первая попытка могла быть применена,
    повтор создал бы пользователю дубликат сообщения."""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    api = make_api(handler)
    with pytest.raises(httpx.ConnectError):
        await api.send(1, "важное сообщение")
    await api.close()
    assert calls == 1


async def test_updates_is_retried_on_connect_error():
    """GET /updates идемпотентен — его можно и нужно повторять при обрыве."""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    api = make_api(handler)
    with pytest.raises(httpx.ConnectError):
        await api.updates(1, timeout=1)
    await api.close()
    assert calls == 3


async def test_send_retries_on_429_but_not_on_500():
    """429 — сервер явно отклонил; 500 после применения — повтор создаст дубль."""
    responses = [httpx.Response(429, json={})]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return responses.pop(0) if responses else httpx.Response(200, json={})

    api = make_api(handler)
    await api.send(1, "привет")
    await api.close()
    assert calls == 2

    calls = 0

    def handler_500(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={})

    api = make_api(handler_500)
    with pytest.raises(Exception):
        await api.send(1, "привет")
    await api.close()
    assert calls == 1


def test_split_text_keeps_everything():
    text = "абв\n" * 3000
    assert "".join(split_text(text)).replace("\n", "") == text.replace("\n", "")
