"""Предел клавиатуры MAX: 30 строк, длинные списки режутся и листаются."""
import pytest

import max_api
from max_api import MaxAPI, split_keyboard

USER = "46010397"


# ── разбиение ─────────────────────────────────────────────────────────────────
def test_split_keyboard_keeps_short_one_as_is():
    rows = [[{"type": "callback", "text": "a", "payload": "a"}] for _ in range(30)]
    assert split_keyboard(rows) == [rows]


def test_split_keyboard_cuts_over_the_limit():
    rows = [[{"type": "callback", "text": str(i), "payload": str(i)}] for i in range(35)]
    chunks = split_keyboard(rows)
    assert [len(chunk) for chunk in chunks] == [30, 5]
    assert [button[0]["payload"] for chunk in chunks for button in chunk] == \
           [str(i) for i in range(35)]


def test_split_keyboard_ignores_empty():
    button = {"type": "callback", "text": "a", "payload": "a"}
    assert split_keyboard(None) == []
    assert split_keyboard([]) == []
    # пустые ряды выбрасываются, лишняя вложенность не появляется
    assert split_keyboard([[], [button]]) == [[[button]]]


def test_split_keyboard_never_returns_more_than_the_limit():
    for count in (31, 60, 100, 250):
        rows = [[{"type": "callback", "text": str(i), "payload": str(i)}] for i in range(count)]
        for chunk in split_keyboard(rows):
            assert len(chunk) <= max_api.MAX_ROWS


def test_limit_is_the_real_max_limit():
    """Предел MAX - 30 строк; при 31 API отвечает errors.maxRows."""
    assert max_api.MAX_ROWS == 30


# ── отправка ──────────────────────────────────────────────────────────────────
class Recorder:
    def __init__(self):
        self.sent = []

    async def _user_wait(self, user_id):
        return None

    async def _request(self, method, path, params=None, json=None, **kwargs):
        self.sent.append({"method": method, "path": path, "params": params, "json": json})
        return {}


@pytest.fixture
def recorder():
    return Recorder()


def rows(count: int) -> list:
    return [[{"type": "callback", "text": str(i), "payload": str(i)}] for i in range(count)]


async def test_long_text_with_keyboard_still_keeps_one_keyboard(recorder):
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    await api.send(USER, "а" * 8000, rows(3))
    # текст режется, кнопки уходят только к последней части
    assert len(recorder.sent) == 3
    assert sum(1 for call in recorder.sent if call["json"].get("attachments")) == 1
    assert recorder.sent[-1]["json"]["attachments"][0]["payload"]["buttons"] == rows(3)


async def test_long_keyboard_goes_as_several_messages_with_captions(recorder):
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    await api.send(USER, "📅 Группы", rows(35))
    assert len(recorder.sent) == 2
    first, second = recorder.sent
    assert len(first["json"]["attachments"][0]["payload"]["buttons"]) == 30
    assert len(second["json"]["attachments"][0]["payload"]["buttons"]) == 5
    assert second["json"]["text"] == "👆 Кнопки ниже"
    # ни одно сообщение не превышает предел MAX
    for call in recorder.sent:
        buttons = call["json"].get("attachments", [{}])[0].get("payload", {}).get("buttons", [])
        assert len(buttons) <= max_api.MAX_ROWS


async def test_keyboard_split_after_long_text_still_labelled(recorder):
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    await api.send(USER, "а" * 8000, rows(35))
    # 3 части текста: первая половина кнопок - под текстом, вторая - отдельным сообщением
    assert len(recorder.sent) == 4
    assert len(recorder.sent[1]["json"]) == 1          # без кнопок
    assert len(recorder.sent[2]["json"]["attachments"][0]["payload"]["buttons"]) == 30
    assert len(recorder.sent[3]["json"]["attachments"][0]["payload"]["buttons"]) == 5
    assert recorder.sent[3]["json"]["text"] == "👆 Кнопки ниже"


async def test_no_keyboard_sends_single_message(recorder):
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    await api.send(USER, "привет")
    assert len(recorder.sent) == 1
    assert "attachments" not in recorder.sent[0]["json"]


async def test_non_numeric_user_id_is_preserved(recorder):
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    await api.send("chat-1", "привет")
    assert recorder.sent[0]["params"] == {"user_id": "chat-1"}


# ── меню: выбор группы листается ──────────────────────────────────────────────
async def test_group_picker_is_paged(env, api, monkeypatch):
    import database as db
    import repository as repo
    from conftest import press
    from handlers import menus

    for index in range(40):
        await repo.upsert_schedule(f"25-{index:02d}", f"https://college.example/{index}.pdf")
    await press("300", "view_schedules")
    body = api.to("300")[-1][1]
    assert "Групп: 40" in body
    assert "страница 1 из" in body
    payloads = api.payloads("300")
    assert "view_schedules:1" in payloads
    # ни одна страница не превышает предел MAX
    page_rows = len(api.to("300")[-1][2])
    assert page_rows <= max_api.MAX_ROWS
    assert page_rows == menus.GROUPS_PAGE + 2      # группы + переходы + «домой»

    await press("300", "view_schedules:3")
    assert "страница 4 из 4" in api.to("300")[-1][1]
    assert "view_schedules:4" not in api.payloads("300")
    assert db is not None


async def test_group_picker_fits_the_max_limit(env, api):
    """Даже самый длинный список не упирается в errors.maxRows."""
    import repository as repo
    from conftest import press

    for index in range(120):
        await repo.upsert_schedule(f"25-{index:03d}", f"https://college.example/{index}.pdf")
    for page in range(10):
        await press("300", f"view_schedules:{page}")
        assert len(api.to("300")[-1][2]) <= max_api.MAX_ROWS


async def test_group_picker_still_opens_a_schedule(env, api):
    import repository as repo
    from conftest import press

    await repo.upsert_schedule("24-23П", "https://college.example/24-23.pdf")
    await press("300", "view_schedules")
    assert "sched:24-23П" in api.payloads("300")
