"""Архив обращений: сотрудник сам убирает закрытые дела и возвращает их в работу.

Раньше архивировать мог только сис-админ (и то из веб-панели), поэтому закрытые
дела годами висели в общей очереди. Теперь кнопка есть у ответственного за
обращение и у того, кому открыт весь список.
"""
import pytest

import database as db
import max_api
import repository as repo
from conftest import add_staff, press, register, say
from utils import as_str

SYS = "1"
STAFF = "500"          # ответственный за обращения
OTHER = "501"          # сотрудник без права видеть чужие обращения
MASTER = "502"         # сотрудник с правом видеть всю очередь
STUDENT = "100"
FOREIGN = "101"        # чужой студент

LONG_NAME = "Ковалевский Константин Юрьевич"
async def jump_ticket_numbers(to: int = 1233) -> None:
    """Довести счётчик обращений до четырёхзначных номеров.

    Подпись кнопки собирается из номера, статуса и пометки, поэтому 1234 -
    самый строгий случай: лишний знак съедает строку.
    """
    await db.run("DELETE FROM sqlite_sequence WHERE name='tickets'")
    await db.run("INSERT INTO sqlite_sequence(name, seq) VALUES('tickets', ?)", (to,))


def labels(api, user) -> list[str]:
    """Подписи всех кнопок последнего сообщения."""
    return [b["text"] for row in (api.to(user)[-1][2] or []) for b in row]


async def make_ticket(staff: str = STAFF, student: str = STUDENT) -> int:
    """Обращение от студента к сотруднику — как в боте: выбор, текст, отправка."""
    await register(student, name="Иванов Иван Иванович")
    await add_staff(staff, LONG_NAME, category="all", position="Методист учебной части")
    await press(student, "new:feedback")
    await press(student, f"pick:feedback:{staff}")
    await say(student, "Нужна справка для поступления")
    await press(student, "ticketsend")
    return (await db.one("SELECT ticket_id FROM tickets ORDER BY ticket_id DESC"))["ticket_id"]


async def archived(ticket_id: int) -> bool:
    row = await db.one("SELECT deleted_at FROM tickets WHERE ticket_id=?", (ticket_id,))
    return bool(as_str(row["deleted_at"]).strip()) if row else False


def not_cut(labels_: list[str], where: str = "") -> None:
    """Подпись кнопки обязана помещаться в строку MAX и не кончаться многоточием."""
    for label in labels_:
        assert len(label) <= max_api.BUTTON_TEXT, f"{where}: длинная подпись «{label}»"
        assert not label.endswith("…"), f"{where}: обрезанная подпись «{label}»"


# ── сотрудник архивирует ─────────────────────────────────────────────────────
async def test_staff_sees_archive_button_on_own_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"t:{tid}")
    assert f"tarch:{tid}" in api.payloads(STAFF)
    assert "🗄 В архив" in " ".join(labels(api, STAFF))


async def test_staff_archives_own_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    assert await archived(tid)
    # очередь закрытого дела больше не показывает
    await press(STAFF, "staff")
    assert f"t:{tid}" not in api.payloads(STAFF)
    # это архив, а не удаление: обращение и переписка на месте
    kept = await repo.get_ticket(tid, include_archived=True)
    assert kept["text_content"] and as_str(kept["deleted_at"]).strip()
    assert await repo.ticket_thread(tid, 10)


async def test_staff_gets_short_answer(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    text = api.last(STAFF)[1]
    assert "в архиве" in text
    assert len(text.splitlines()) == 1          # коротко, без простыни


async def test_student_is_told_honestly(api):
    """Студент знает, что дело убрано в архив, а не пропало."""
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    notice = "\n".join(text for _, text, _ in api.to(STUDENT))
    assert "архив" in notice
    assert "Обращение не найдено" not in notice
    assert f"№{tid}" in notice


async def test_archive_is_written_to_the_log(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    entries = [row for row in await repo.admin_log(50)
               if as_str(row["actor_id"]) == STAFF]
    assert any("архив" in as_str(row["action"]) for row in entries)


async def test_missing_ticket_is_graceful(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await press(STAFF, "tarch:999")
    assert "не найдено" in api.last(STAFF)[1]


# ── студенту архив недоступен ────────────────────────────────────────────────
async def test_student_card_has_no_archive_button(api):
    tid = await make_ticket()
    await press(STUDENT, f"t:{tid}")
    assert not [p for p in api.payloads(STUDENT) if p.startswith("tarch")]


async def test_student_cannot_archive_even_by_crafted_payload(api):
    tid = await make_ticket()
    api.sent.clear()
    await press(STUDENT, f"tarch:{tid}")
    assert not await archived(tid)


async def test_foreign_student_cannot_archive_someone_elses_ticket(api):
    tid = await make_ticket()
    await register(FOREIGN, name="Петрова Анна Сергеевна")
    api.sent.clear()
    await press(FOREIGN, f"tarch:{tid}")
    assert not await archived(tid)


# ── чужие обращения архивирует только тот, кому открыт весь список ────────────
async def test_other_staff_cannot_archive_foreign_ticket(api):
    tid = await make_ticket()
    await add_staff(OTHER, "Соколова Мария Сергеевна", category="all")
    api.sent.clear()
    await press(OTHER, f"tarch:{tid}")
    assert not await archived(tid)
    assert "🔒" in api.last(OTHER)[1]


async def test_staff_with_see_all_can_archive_any_ticket(api):
    """Право «видит все обращения» даёт и право убрать чужое дело в архив."""
    tid = await make_ticket()
    await add_staff(MASTER, "Соколова Мария Сергеевна", category="all")
    await repo.set_staff_see_all(MASTER, True)
    await press(MASTER, f"tarch:{tid}")
    assert await archived(tid)
    assert "в архиве" in api.last(MASTER)[1]


async def test_sysadmin_can_archive_anything(api):
    tid = await make_ticket()
    await press(SYS, f"tarch:{tid}")
    assert await archived(tid)


# ── возврат из архива ────────────────────────────────────────────────────────
async def test_restore_button_replaces_archive_button(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await press(STAFF, f"t:{tid}")
    rows = " ".join(labels(api, STAFF))
    assert "📂 Вернуть из архива" in rows
    assert "🗄 В архив" not in rows
    # архивное дело закрыто: отвечать и менять статус в нём нельзя
    payloads = api.payloads(STAFF)
    assert not [p for p in payloads if p.startswith(("rp:", "st:", "note:", "fwd:", "tpl:"))]


async def test_restore_returns_ticket_to_queue(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await press(STAFF, f"tarch:{tid}")
    assert not await archived(tid)
    await press(STAFF, "staff")
    assert f"t:{tid}" in api.payloads(STAFF)


async def test_archived_ticket_still_readable(api):
    """Сотрудник и автор видят архивное дело: архив не молчалка."""
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await press(STAFF, f"t:{tid}")
    assert "Нужна справка для поступления" in api.last(STAFF)[1]
    assert "в архиве" in api.last(STAFF)[1]
    await press(STUDENT, f"t:{tid}")
    assert "в архиве" in api.last(STUDENT)[1]
    assert not [p for p in api.payloads(STUDENT) if p.startswith("tarch")]


# ── архив виден в списках ────────────────────────────────────────────────────
async def test_queue_has_archive_filter(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await press(STAFF, "staff")
    assert "staff:archive" in api.payloads(STAFF)
    await press(STAFF, "staff:archive")
    assert "Архив обращений" in api.last(STAFF)[1]
    assert f"t:{tid}" in api.payloads(STAFF)


async def test_archive_filter_hides_live_tickets(api):
    live = await make_ticket()
    await press(STAFF, "staff:archive")
    assert f"t:{live}" not in api.payloads(STAFF)


async def test_empty_archive_says_so(api):
    await add_staff(STAFF, "Петрова Анна", category="all")
    await press(STAFF, "staff:archive")
    assert "Архив обращений" in api.last(STAFF)[1]
    assert "Архив пуст" in " ".join(labels(api, STAFF))


async def test_archive_of_other_staff_is_hidden(api):
    """Чужой архив показывать нельзя: карточку чужого дела сотрудник не откроет."""
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await add_staff(OTHER, "Соколова Мария Сергеевна", category="all")
    await press(OTHER, "staff:archive")
    assert f"t:{tid}" not in api.payloads(OTHER)
    await press(SYS, "staff:archive")
    assert f"t:{tid}" in api.payloads(SYS)     # весь архив видит сис-админ


async def test_student_list_has_archive_filter(api):
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await press(STUDENT, "tickets")
    assert "tickets:archive" in api.payloads(STUDENT)
    await press(STUDENT, "tickets:archive")
    assert "Архив ваших обращений" in api.last(STUDENT)[1]
    assert f"t:{tid}" in api.payloads(STUDENT)


# ── пределы MAX на новых экранах ─────────────────────────────────────────────
async def test_four_digit_number_does_not_cut_the_label(api):
    """«🔔 №1234 · 🆕 Новое» помещается в строку кнопки, а длинный статус обрезается."""
    await make_ticket()
    await jump_ticket_numbers()
    await press(STUDENT, "new:feedback")
    await press(STUDENT, f"pick:feedback:{STAFF}")
    await say(STUDENT, "Ещё один вопрос")
    await press(STUDENT, "ticketsend")
    assert (await db.one("SELECT MAX(ticket_id) n FROM tickets"))["n"] == 1234

    await press(STAFF, "staff")
    assert "№1234 · 🔔 ждёт" in " ".join(labels(api, STAFF))
    not_cut(labels(api, STAFF), "очередь")
    await press(STAFF, "tarch:1234")
    await press(STAFF, "staff:archive")
    not_cut(labels(api, STAFF), "архив")


@pytest.mark.parametrize("actor", [STAFF, SYS])
async def test_ticket_card_keeps_max_limits(api, actor):
    """Карточка остаётся в пределах MAX: не больше 30 строк и 7 кнопок в ряду."""
    tid = await make_ticket()
    await press(actor, f"t:{tid}")
    keyboard = api.last(actor)[2] or []
    assert len(keyboard) <= max_api.MAX_ROWS
    assert all(len(row) <= 7 for row in keyboard)
    not_cut([b["text"] for row in keyboard for b in row], "карточка")


@pytest.mark.parametrize("actor", [STUDENT, OTHER])
async def test_archive_is_one_way_without_rights(api, actor):
    """Ни студент, ни чужой сотрудник архив не трогают - даже кнопкой «вернуть»."""
    tid = await make_ticket()
    await press(STAFF, f"tarch:{tid}")
    await add_staff(OTHER, "Соколова Мария Сергеевна", category="all")
    api.sent.clear()
    await press(actor, f"tarch:{tid}")
    assert await archived(tid)      # вернуть из архива тоже не вышло
