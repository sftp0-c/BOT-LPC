"""Плотный UX обращений v1.6: видимые кнопки, пагинация и сериализация."""
import ast
from pathlib import Path

import database as db
import max_api
import repository as repo
from conftest import add_staff, press_crafted, press_visible, register, say

STUDENT, STAFF, OTHER = "610", "620", "621"


async def seed_ticket(student=STUDENT, staff=STAFF, *, status="new", archived=False,
                      category="feedback", text="Вопрос") -> int:
    ticket_id = await repo.create_ticket(student, staff, category, text)
    await db.run("UPDATE tickets SET status=?, deleted_at=? WHERE ticket_id=?",
                 (status, "2026-11-04 10:00:00" if archived else "", ticket_id))
    return ticket_id


def payloads(message) -> list[str]:
    return [button.get("payload", "") for row in (message[2] or []) for button in row]


def assert_serialized_keyboard(message) -> None:
    for row in message[2] or []:
        assert 1 <= len(row) <= 2
        limit = max_api.row_limit(len(row))
        for button in row:
            assert max_api.display_width(button["text"]) <= limit
            assert not button["text"].endswith("…")


async def test_fake_api_keeps_raw_and_exposes_serialized(api):
    raw = [[max_api.btn("Очень длинная подпись кнопки", "long"),
            max_api.btn("Ещё длинная подпись кнопки", "other")]]
    await api.send(STUDENT, "Экран", raw)
    assert api.raw_sent[-1][2] is raw
    assert api.sent[-1][2] == max_api.fit_keyboard(raw)
    assert api.sent[-1][2][0][0]["text"].endswith("…")


async def test_forward_comment_visible_button_keeps_state_and_sends(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff(OTHER, "Соколов Иван", category="all")
    await press_visible(api, STUDENT, "ticket_menu")
    await press_crafted(STUDENT, "new:feedback")
    await press_visible(api, STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Передайте вопрос специалисту")
    await press_visible(api, STUDENT, "ticketsend")
    ticket_id = int((await db.one("SELECT MAX(ticket_id) n FROM tickets"))["n"])

    await press_crafted(STAFF, f"fwd:{ticket_id}")
    await press_visible(api, STAFF, f"fwdto:{ticket_id}:{OTHER}")
    await press_visible(api, STAFF, f"fwdask:{ticket_id}:{OTHER}")
    state = await db.get_state(STAFF)
    assert state["state"] == "forward_comment"
    await say(STAFF, "Нужна консультация коллеги")
    assert (await repo.get_ticket(ticket_id))["target_admin_id"] == OTHER
    assert "Комментарий: Нужна консультация" in "\n".join(m[1] for m in api.to(OTHER))


async def test_forward_old_ask_alias_opens_comment_input(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff(OTHER, "Соколов Иван", category="all")
    ticket_id = await seed_ticket()
    await press_crafted(STAFF, f"fwdok:{ticket_id}:{OTHER}:ask")
    state = await db.get_state(STAFF)
    assert state["state"] == "forward_comment"
    assert state["payload"]["target"] == OTHER


async def test_empty_archive_has_no_active_noop_but_legacy_noop_is_safe(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await press_crafted(STAFF, "staff:archive")
    assert "Архив пока пуст" in api.last(STAFF)[1]
    assert "noop" not in api.payloads(STAFF)
    before = len(api.sent)
    await press_crafted(STAFF, "noop")
    assert len(api.sent) == before


async def test_staff_queue_filters_before_limit_and_pages_over_twenty(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff(OTHER, "Соколов Иван", category="all")
    wanted = []
    for index in range(22):
        wanted.append(await seed_ticket(staff=STAFF, category="certificates",
                                        text=f"Справка {index}"))
    for index in range(25):
        await seed_ticket(staff=OTHER, category="feedback", text=f"Лишнее {index}")

    await press_crafted(STAFF, "stafff:certificates")
    first = api.last(STAFF)
    shown = [int(value.split(":")[1]) for value in payloads(first) if value.startswith("t:")]
    assert shown == list(reversed(wanted[-20:]))
    assert "Показаны 1–20 из 22" in first[1]
    assert "staff:certificates:p:2" in payloads(first)
    assert_serialized_keyboard(first)

    await press_visible(api, STAFF, "staff:certificates:p:2")
    second = api.last(STAFF)
    assert [int(value.split(":")[1]) for value in payloads(second) if value.startswith("t:")] == list(reversed(wanted[:2]))
    assert "Показаны 21–22 из 22" in second[1]
    assert "staff:certificates:p:1" in payloads(second)


async def test_student_lists_page_over_fifteen_and_old_payloads_work(api):
    await register(STUDENT)
    await add_staff(STAFF, "Петрова Анна", category="all")
    live = [await seed_ticket(text=f"Текущее {index}") for index in range(17)]
    archived = [await seed_ticket(text=f"Архив {index}", archived=True) for index in range(16)]

    await press_crafted(STUDENT, "tickets")
    first = api.last(STUDENT)
    assert "Показаны 1–15 из 17" in first[1]
    assert "tickets:p:2" in payloads(first)
    assert "tickets:archive" in payloads(first)
    await press_visible(api, STUDENT, "tickets:p:2")
    assert "Показаны 16–17 из 17" in api.last(STUDENT)[1]
    assert f"t:{live[0]}" in api.payloads(STUDENT)

    await press_crafted(STUDENT, "tickets:archive")
    archive_first = api.last(STUDENT)
    assert "Показаны 1–15 из 16" in archive_first[1]
    assert "tickets:archive:p:2" in payloads(archive_first)
    await press_visible(api, STUDENT, "tickets:archive:p:2")
    assert f"t:{archived[0]}" in api.payloads(STUDENT)


async def test_main_queue_keeps_removed_filters_removed(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await seed_ticket()
    await press_crafted(STAFF, "staff")
    addresses = api.payloads(STAFF)
    assert not [value for value in addresses if value.startswith(("staffv:", "stafff:"))]
    assert "staff:mine" not in addresses


def test_all_ticket_literal_rows_have_at_most_two_buttons():
    tree = ast.parse(Path("handlers/tickets.py").read_text(encoding="utf-8-sig"))
    wide = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
            continue
        if all(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
               and item.func.id in {"btn", "link_btn"} for item in node.elts):
            if len(node.elts) > 2:
                wide.append(node.lineno)
    assert not wide, f"В handlers/tickets.py есть штатные ряды шире двух: {wide}"
