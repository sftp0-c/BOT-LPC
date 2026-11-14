"""Тесты новых возможностей: тихое закрытие, расширенная рассылка, групповые действия."""
import pytest
import repository as repo
from conftest import add_staff, login_panel, post_form, register
from handlers.registry import CALLBACKS

STUDENT = "300"
STAFF = "200"
OTHER = "201"


@pytest.fixture
async def env(env):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    await add_staff(OTHER, "Сидоров Пётр Петрович", position="Кассир")
    return env


async def make_ticket(student=STUDENT, admin=STAFF, text="Тестовое обращение"):
    return await repo.create_ticket(student, admin, "feedback", text, "Тема")


async def test_bulk_close_and_multi_tids(panel_client, env):
    t1 = await make_ticket()
    t2 = await make_ticket()
    assert login_panel(panel_client)
    res = post_form(panel_client, "/panel/tickets/bulk", {
        "tids": f"{t1},{t2}", "action": "close", "value": ""
    })
    assert res.status_code == 303
    row1 = await repo.get_ticket(t1)
    row2 = await repo.get_ticket(t2)
    assert row1["status"] in ("closed", "completed")
    assert row2["status"] in ("closed", "completed")


async def test_bulk_status_russian_mapping(panel_client, env):
    t1 = await make_ticket()
    assert login_panel(panel_client)
    res = post_form(panel_client, "/panel/tickets/bulk", {
        "tids": str(t1), "action": "status", "value": "в работе"
    })
    assert res.status_code == 303
    row = await repo.get_ticket(t1)
    assert row["status"] == "in_progress"


async def test_silent_close_bot(env, api):
    tid = await make_ticket()
    cb = CALLBACKS.get("tclose")
    assert cb is not None
    api.sent.clear()
    await cb(STAFF, str(tid))
    row = await repo.get_ticket(tid)
    assert row["status"] in ("completed", "closed")
    # Проверяем, что студенту не отправлялось лишних уведомлений
    student_messages = [msg for msg in api.sent if msg[0] == STUDENT]
    assert not student_messages


async def test_audience_ids_staff_and_custom(env):
    staff_ids = await repo.audience_ids("staff")
    assert STAFF in staff_ids
    assert OTHER in staff_ids
    assert STUDENT not in staff_ids

    custom_ids = await repo.audience_ids("custom:111, 222, 333")
    assert custom_ids == ["111", "222", "333"]


async def test_ticket_new_notifies_student_with_reply_btn(panel_client, env, api):
    assert login_panel(panel_client)
    res = post_form(panel_client, "/panel/tickets/new", {
        "student_id": STUDENT,
        "category": "feedback",
        "target_admin_id": STAFF,
        "text_content": "Срочно зайдите в учебную часть",
    })
    assert res.status_code == 303
    student_messages = [msg for msg in api.sent if msg[0] == STUDENT]
    assert len(student_messages) >= 1
    last_msg = student_messages[-1]
    assert "учебную часть" in last_msg[1]
    # Проверяем наличие кнопки ответа
    kb = last_msg[2] or []
    flattened_buttons = [b for row in kb for b in row]
    reply_btn = [b for b in flattened_buttons if "rp:" in b.get("payload", "")]
    assert len(reply_btn) == 1


async def test_panel_static_assets_and_security_headers(panel_client):
    res_css = panel_client.get("/panel/static/panel.css")
    assert res_css.status_code == 200
    assert "text/css" in res_css.headers["content-type"]
    assert res_css.headers["x-content-type-options"] == "nosniff"

    res_js = panel_client.get("/panel/static/panel.js")
    assert res_js.status_code == 200
    assert "javascript" in res_js.headers["content-type"]
    assert res_js.headers["x-content-type-options"] == "nosniff"

    res_login = panel_client.get("/panel/login")
    assert res_login.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in res_login.headers["content-security-policy"]


async def test_find_students_fio_and_group(env):
    await register("305", "Смирнов Алексей Викторович", "ис-21")
    await register("306", "Смирнова Екатерина Дмитриевна", "ис-22")

    # Поиск по фамилии и группе одновременно
    res_both = await repo.find_students("Смирнов ис-21")
    assert len(res_both) == 1
    assert res_both[0]["user_id"] == "305"
    assert res_both[0]["group_code"].upper() == "ИС-21"

    # Поиск только по фамилии находит обоих
    res_name = await repo.find_students("Смирнов")
    assert len(res_name) == 2
    uids = {r["user_id"] for r in res_name}
    assert uids == {"305", "306"}


async def test_audience_ids_by_name(env):
    # Иванов (студент) и Петрова (сотрудник) резолвятся из имен
    resolved = await repo.audience_ids("custom:Иванов, Петрова, 999")
    assert STUDENT in resolved
    assert STAFF in resolved
    assert "999" in resolved


async def test_bot_staff_write_student_flow(env, api):
    from handlers.registry import STATES
    import database as db

    # 1. Поиск студента по ФИО и группе
    target_handler = STATES.get("write_student_target")
    assert target_handler is not None
    await target_handler(STAFF, "Иванов 24-23", {})

    state_row = await db.get_state(STAFF)
    assert state_row["state"] == "write_student_text"
    assert state_row["payload"]["student_id"] == STUDENT

    # 2. Ввод текста сообщения
    text_handler = STATES.get("write_student_text")
    api.sent.clear()
    await text_handler(STAFF, "Подойдите после третьей пары", state_row["payload"])

    # Студент получил сообщение с кнопкой ответа
    student_messages = [msg for msg in api.sent if msg[0] == STUDENT]
    assert len(student_messages) == 1
    assert "после третьей пары" in student_messages[0][1]
    buttons = [b for row in student_messages[0][2] for b in row]
    assert any("rp:" in b["payload"] for b in buttons)


async def test_bot_broadcast_custom_flow(env, api):
    from handlers.registry import STATES
    import database as db
    custom_handler = STATES.get("bc_custom")
    assert custom_handler is not None
    # ID "1" - суперадминистратор (всегда имеет право на рассылку)
    await custom_handler("1", "Иванов, 888", {})

    state_row = await db.get_state("1")
    assert state_row is not None
    assert state_row["state"] == "bc_text"
    assert "custom:Иванов, 888" in state_row["payload"]["aud"]


async def test_ticket_new_page_loads(panel_client, env):
    assert login_panel(panel_client)
    res = panel_client.get("/panel/tickets/new")
    assert res.status_code == 200
    assert "Новое обращение" in res.text
    assert "students-list" in res.text
