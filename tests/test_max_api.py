import json

import httpx
import pytest

from max_api import MAX_PAYLOAD, MaxAPI, MaxAPIError, btn, split_text


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


# ── регрессии: нечисловой ID пользователя ─────────────────────────────────────


async def test_send_with_non_numeric_user_id_does_not_raise_value_error():
    """ID приходит извне (ручной /id, ответы админских меню) и не обязателен числовым.
    Раньше int(user_id) ронял отправку с ValueError — пользователю не уходило ни сообщения,
    ни внятной ошибки: process() глотал исключение и показывал «Что-то пошло не так»."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    api = make_api(handler)
    await api.send("user-abc", "Привет")  # раньше ValueError
    await api.send("u_777:42", "Ещё раз")
    await api.send("  42  ", "С пробелами")  # int() терпит, значит приводится к числу
    await api.close()

    assert [r.url.params["user_id"] for r in calls] == ["user-abc", "u_777:42", "42"]


async def test_send_with_empty_user_id_is_sent_as_is():
    """Пустой/None ID не должен превращаться в исключение — MAX ответит кодом ошибки,
    и бот покажет пользователю штатное сообщение."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"message": "user_id is required"})

    api = make_api(handler)
    with pytest.raises(MaxAPIError) as info:
        await api.send(None, "Привет")
    await api.close()
    assert info.value.status == 400
    assert calls[0].url.params["user_id"] == "None"


# ── регрессии: усечение кнопок до лимитов MAX API ─────────────────────────────


def test_btn_truncates_payload_to_1024_and_text_to_128():
    button = btn("т" * 200, "p" * 2000)
    assert button["type"] == "callback"
    assert len(button["payload"]) == MAX_PAYLOAD == 1024
    assert button["payload"] == "p" * 1024  # усечение, а не потеря payload целиком
    assert len(button["text"]) == 128
    assert button["text"] == "т" * 128


def test_btn_keeps_short_values_untouched():
    assert btn("Да", "yes") == {"type": "callback", "text": "Да", "payload": "yes"}


async def test_send_sends_truncated_button_within_api_limits():
    """Усечение проверяется на реальном теле запроса: MAX отклоняет кнопку
    с payload > 1024 или text > 128, и сообщение не доходит до пользователя."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    api = make_api(handler)
    await api.send(1, "меню", [[btn("Очень длинный текст кнопки " * 30, "d" * 1500)]])
    await api.close()

    button = bodies[0]["attachments"][0]["payload"]["buttons"][0][0]
    assert len(button["payload"]) <= 1024
    assert len(button["text"]) <= 128
