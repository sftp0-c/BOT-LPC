"""Нижняя клавиатура MAX: кнопки под полем ввода вместо кнопок в сообщении."""

import max_api
from max_api import MaxAPI, bottom_btn, bottom_keyboard
from conftest import press, register


# ── форма, которую принимает MAX (проверено запросами к API) ─────────────────
def test_bottom_btn_is_callback_with_payload():
    button = bottom_btn("📚 Расписание", "view_schedules")
    assert button == {"type": "callback", "text": "📚 Расписание", "payload": "view_schedules"}


def test_bottom_btn_without_payload_sends_text():
    assert bottom_btn("Помощь") == {"type": "text", "text": "Помощь"}


def test_bottom_text_is_shortened():
    assert len(bottom_btn("х" * 200)["text"]) == 64
    assert len(bottom_btn("х", "p" * 2000)["payload"]) == max_api.MAX_PAYLOAD


def test_bottom_keyboard_none_and_empty_differ():
    """None - не трогаем клавиатуру, [] - снимаем её."""
    assert bottom_keyboard(None) is None
    assert bottom_keyboard([]) == {"buttons": []}
    assert bottom_keyboard([bottom_btn("а", "b")]) == {"buttons": [bottom_btn("а", "b")]}


# ── отправка ──────────────────────────────────────────────────────────────────
class Recorder:
    def __init__(self):
        self.sent = []

    async def _user_wait(self, user_id):
        return None

    async def _request(self, method, path, params=None, json=None, **kwargs):
        self.sent.append(json or {})
        return {}


def api_with(recorder: Recorder) -> MaxAPI:
    api = MaxAPI.__new__(MaxAPI)
    api._user_wait = recorder._user_wait
    api._request = recorder._request
    return api


async def test_bottom_buttons_go_to_reply_markup():
    recorder = Recorder()
    await api_with(recorder).send("1", "Меню", None, [bottom_btn("🏠 Меню", "home")])
    body = recorder.sent[0]
    assert body["reply_markup"] == {"buttons": [{"type": "callback", "text": "🏠 Меню",
                                                 "payload": "home"}]}
    assert "attachments" not in body      # инлайн-кнопок в сообщении нет


async def test_bottom_and_inline_can_be_together():
    recorder = Recorder()
    await api_with(recorder).send("1", "Карточка", [[bottom_btn("а", "b")]], [bottom_btn("в", "г")])
    body = recorder.sent[0]
    assert body["attachments"][0]["type"] == "inline_keyboard"
    assert body["reply_markup"]["buttons"][0]["payload"] == "г"


async def test_bottom_markup_repeats_on_every_text_part():
    recorder = Recorder()
    await api_with(recorder).send("1", "а" * 8000, None, [bottom_btn("🏠 Меню", "home")])
    assert len(recorder.sent) == 3
    assert all(body.get("reply_markup") for body in recorder.sent)


async def test_no_bottom_means_no_reply_markup():
    recorder = Recorder()
    await api_with(recorder).send("1", "привет", [[bottom_btn("а", "b")]])
    assert "reply_markup" not in recorder.sent[0]


# ── нижнее меню бота ──────────────────────────────────────────────────────────
def test_student_bottom_menu_is_short_and_actionable():
    from handlers import menus

    buttons = menus.bottom_menu("student")
    payloads = [b["payload"] for b in buttons]
    assert payloads == ["home", "view_schedules", "tickets", "new:feedback", "profile"]
    assert all(b["type"] == "callback" for b in buttons)
    assert all(len(b["text"]) <= 20 for b in buttons)   # помещаются в одну строку


def test_staff_bottom_menu_has_queue_and_stats():
    from handlers import menus

    a = {"role_type": "staff", "can_broadcast": 0}
    payloads = [b["payload"] for b in menus.bottom_menu("staff", a)]
    assert "staff" in payloads and "staffstats" in payloads
    assert "broadcast" not in payloads
    assert "sysadm" not in payloads


def test_superadmin_bottom_menu_has_broadcast_and_sysadmin():
    from handlers import menus

    a = {"role_type": "sysadmin", "can_broadcast": 1}
    payloads = [b["payload"] for b in menus.bottom_menu("admin", a)]
    assert {"staff", "sysadm", "broadcast", "staffstats"} <= set(payloads)


def test_bottom_menu_never_exceeds_max_rows():
    """Даже самый полный набор кнопок снизу не превышает предел MAX."""
    from handlers import menus

    a = {"role_type": "sysadmin", "can_broadcast": 1}
    for role in ("student", "staff", "admin"):
        assert len(menus.bottom_menu(role, a)) <= max_api.MAX_ROWS


# ── экранные тесты ───────────────────────────────────────────────────────────
async def test_home_attaches_bottom_menu_for_student(env, api):
    await register("300", "Иванов Иван Иванович", "24-23")
    await press("300", "home")
    assert api.bottom_payloads("300") == ["home", "view_schedules", "tickets",
                                          "new:feedback", "profile"]


async def test_home_bottom_button_leads_to_schedules(env, api):
    import repository as repo

    await register("300", "Иванов Иван Иванович", "24-23")
    await repo.upsert_schedule("24-23", "https://college.example/24-23.pdf")
    await press("300", "home")
    assert "view_schedules" in api.bottom_payloads("300")
    await press("300", "view_schedules")
    assert "sched:24-23" in api.payloads("300")


async def test_home_bottom_menu_for_sysadmin(env, api):
    import repository as repo

    await repo.grant_sysadmin("1", "Иванов Иван Иванович")
    await press("1", "home")
    payloads = api.bottom_payloads("1")
    assert "staff" in payloads and "sysadm" in payloads


async def test_tapping_bottom_button_cancels_pending_input(env, api):
    """Кнопка снизу во время ввода текста не должна ломать сценарий."""
    import database as db
    from conftest import say

    await register("300", "Иванов Иван Иванович", "24-23")
    await db.set_state("300", "ticket", {"cat": "feedback"})
    assert await db.get_state("300") is not None     # сценарий идёт, состояние есть
    await press("300", "home")            # как будто человек ткнул «Меню» снизу
    assert await db.get_state("300") is None
    # следующий текст уже не попадает в сценарий обращения
    await say("300", "просто текст")
    assert await db.get_state("300") is None
