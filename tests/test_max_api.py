import json

import httpx

from max_api import MaxAPI, btn, split_text


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
    import pytest

    from max_api import MaxAPIError

    api = make_api(lambda r: httpx.Response(401, json={"message": "unauthorized"}))
    with pytest.raises(MaxAPIError) as info:
        await api.send(1, "x")
    assert info.value.status == 401
    await api.close()


def test_split_text_keeps_everything():
    text = "абв\n" * 3000
    assert "".join(split_text(text)).replace("\n", "") == text.replace("\n", "")
