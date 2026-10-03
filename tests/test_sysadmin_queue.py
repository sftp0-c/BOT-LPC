"""Очередь бота: сис-админ видит и берёт в работу все обращения.

Раньше сис-админ видел в очереди в основном назначенные ему дела, а право
«видит все обращения» открывало очередь, но не карточку - кнопка вела в
«не найдено». Теперь у сис-админа полные права на обращения: чужие дела видны,
открываются и переходят к нему по кнопке «✋ Взять». Обычный сотрудник
по-прежнему работает только со своими.
"""
import max_api
import pytest

import database as db
import repository as repo
from conftest import card_more, add_staff, press, register, say

SYS = "1"                 # сис-админ из SYSADMIN_IDS (см. conftest)
STAFF = "500"             # сотрудник, которому назначили обращение
OTHER = "501"             # сотрудник без права видеть чужие обращения
MASTER = "502"            # сотрудник с правом «видит все обращения»
STUDENT = "100"
FOREIGN = "101"


async def make_ticket(student: str = STUDENT, staff: str = STAFF,
                      text: str = "Не работает электронный журнал") -> int:
    """Обращение от студента к сотруднику — так, как это делает человек."""
    await register(student, name="Иванов Иван Иванович")
    await add_staff(staff, "Соколова Мария Сергеевна", category="all", position="Методист")
    await press(student, "new:feedback")
    await press(student, f"pick:feedback:{staff}")
    await say(student, text)
    await press(student, "ticketsend")
    return (await db.one("SELECT MAX(ticket_id) n FROM tickets"))["n"]


async def owner_of(ticket_id: int) -> str:
    row = await db.one("SELECT target_admin_id FROM tickets WHERE ticket_id=?", (ticket_id,))
    return str(row["target_admin_id"])


def labels(api, user) -> list[str]:
    return [b["text"] for row in (api.last(user)[2] or []) for b in row]


def assert_fits(api, user, where: str = "") -> None:
    """Ни одна подпись очереди не обрезается: ширина в ячейках против предела ряда."""
    for row in (api.last(user)[2] or []):
        limit = max_api.row_limit(len(row))
        for button in row:
            text = button["text"]
            assert max_api.display_width(text) <= limit, f"{where}: длинная подпись «{text}»"
            assert not text.endswith("…"), f"{where}: обрезанная подпись «{text}»"


# ── сис-админ видит всю очередь ──────────────────────────────────────────────
async def test_sysadmin_queue_shows_foreign_tickets(api):
    mine = await make_ticket()
    theirs = await make_ticket(FOREIGN, STAFF, "Нужна справка")
    await press(SYS, "staff")
    assert f"t:{mine}" in api.payloads(SYS) and f"t:{theirs}" in api.payloads(SYS)
    # строки про права на экране нет: владелец просил мало информации, а право
    # сотрудник и так знает - оно проверяется самим фактом, что чужие дела видны
    assert "системные права" not in api.last(SYS)[1]
    assert api.last(SYS)[1].startswith("📬 Очередь обращений")
    assert_fits(api, SYS, "очередь сис-админа")


async def test_sysadmin_opens_foreign_ticket(api):
    tid = await make_ticket()
    await press(SYS, f"t:{tid}")
    card = api.last(SYS)
    assert "Не работает электронный журнал" in card[1]
    адреса, _ = await card_more(api, SYS, tid)
    assert f"rp:{tid}" in адреса          # отвечать можно
    assert f"st:{tid}:accepted" in адреса  # и статус менять
    assert f"ttake:{tid}" in адреса        # и взять в работу
    assert f"tdel:{tid}" in адреса         # и удалить
    # подписи меряем на самой карточке, а не на подменю
    await press(SYS, f"t:{tid}")
    assert_fits(api, SYS, "карточка чужого дела")


async def test_sysadmin_answers_foreign_ticket(api):
    tid = await make_ticket()
    await press(SYS, f"rp:{tid}")
    await say(SYS, "Проверим и ответим сегодня")
    assert "Проверим и ответим сегодня" in "\n".join(text for _, text, _ in api.to(STUDENT))


async def test_sysadmin_takes_ticket_into_work(api):
    tid = await make_ticket()
    await press(SYS, f"ttake:{tid}")
    assert await owner_of(tid) == SYS
    # прежний сотрудник узнаёт об этом, а не обнаружит пропажу по тишине
    note = "\n".join(text for _, text, _ in api.to(STAFF))
    assert f"№{tid}" in note and "взял в работу" in note
    # после взятия обращение в его собственной очереди
    await press(SYS, "staff:mine")
    assert f"t:{tid}" in api.payloads(SYS)
    assert_fits(api, SYS, "своя очередь")


async def test_sysadmin_switches_between_all_and_mine(api):
    tid = await make_ticket()
    await press(SYS, "staff:mine")
    assert f"t:{tid}" not in api.payloads(SYS)     # чужих дел в «моих» нет
    # кнопки переключения на экране нет: возвращаться к своим делам не нужно,
    # очередь и так своя. Само представление работает - на него остались закладки.
    assert f"t:{tid}" not in api.payloads(SYS)     # чужих дел в «моих» нет
    await press(SYS, "staff:")
    assert f"t:{tid}" in api.payloads(SYS)         # а во всех - есть
    assert "staff:mine" not in api.payloads(SYS)    # и кнопки переключения нет


async def test_take_button_is_not_shown_for_own_ticket(api):
    tid = await make_ticket()
    await press(STAFF, f"t:{tid}")
    assert not [p for p in api.payloads(STAFF) if p.startswith("ttake:")]


async def test_take_of_own_ticket_is_refused(api):
    tid = await make_ticket()
    await press(STAFF, f"ttake:{tid}")
    assert await owner_of(tid) == STAFF
    assert "🔒" in api.last(STAFF)[1]


# ── обычный сотрудник остаётся у своих дел ───────────────────────────────────
async def test_plain_staff_sees_only_own_tickets(api):
    mine = await make_ticket()
    theirs = await make_ticket(FOREIGN, STAFF)
    await add_staff(OTHER, "Козлов Иван", category="all")
    await press(OTHER, "staff")
    payloads = api.payloads(OTHER)
    assert f"t:{mine}" not in payloads and f"t:{theirs}" not in payloads
    # строки про права на экране нет - по решению владельца
    assert "назначенные вам" not in api.last(OTHER)[1]
    assert "staff:mine" not in payloads              # переключателя нет: нечего переключать


async def test_plain_staff_cannot_open_foreign_ticket(api):
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await press(OTHER, f"t:{tid}")
    assert "не найдено" in api.last(OTHER)[1].lower()


async def test_plain_staff_cannot_take_foreign_ticket(api):
    """Нет доступа к чужому делу - значит, и взять его нельзя."""
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await press(OTHER, f"ttake:{tid}")
    assert await owner_of(tid) == STAFF
    assert "не найдено" in api.last(OTHER)[1].lower()


async def test_own_ticket_keeps_working_after_replies(api):
    """Своё дело сотрудник по-прежнему ведёт целиком: ответ, статус, готово."""
    tid = await make_ticket()
    await repo.set_admin_profile(STAFF, office="каб. 204")
    await press(STAFF, f"rp:{tid}")
    await say(STAFF, "Отвечаю")
    await press(STAFF, f"st:{tid}:accepted")
    await press(STAFF, f"st:{tid}:ready")
    await press(STAFF, f"rt:{tid}:today")
    row = await db.one("SELECT status, pickup_place FROM tickets WHERE ticket_id=?", (tid,))
    assert (row["status"], row["pickup_place"]) == ("ready", "каб. 204")
    await press(STAFF, f"t:{tid}")
    assert f"ttake:{tid}" not in api.payloads(STAFF)      # своё дело брать не нужно
    assert not [p for p in api.payloads(STAFF) if p.startswith("tdready:")]


# ── право «видит все обращения» из базы ─────────────────────────────────────
async def test_granted_staff_sees_and_opens_foreign_ticket(api):
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await repo.set_staff_see_all(OTHER, True)
    await press(OTHER, "staff")
    assert f"t:{tid}" in api.payloads(OTHER)
    # строки про права на экране нет - по решению владельца
    # право выдано видно по делу: чужое обращение в очереди есть
    assert "право выдано" not in api.last(OTHER)[1]
    await press(OTHER, f"t:{tid}")
    assert "Не работает электронный журнал" in api.last(OTHER)[1]


async def test_granted_staff_takes_ticket(api):
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await repo.set_staff_see_all(OTHER, True)
    await press(OTHER, f"ttake:{tid}")
    assert await owner_of(tid) == OTHER
    await press(OTHER, "staff:mine")
    assert f"t:{tid}" in api.payloads(OTHER)


async def test_granted_staff_sees_foreign_archive(api):
    """Право видеть всю очередь открывает и архив - как у сис-админа."""
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await press(STAFF, f"tarch:{tid}")
    await press(OTHER, "staff:archive")
    assert f"t:{tid}" not in api.payloads(OTHER)
    await repo.set_staff_see_all(OTHER, True)
    await press(OTHER, "staff:archive")
    assert f"t:{tid}" in api.payloads(OTHER)


async def test_revoking_the_right_hides_foreign_queue_again(api):
    tid = await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await repo.set_staff_see_all(OTHER, True)
    await press(OTHER, "staff")
    assert f"t:{tid}" in api.payloads(OTHER)
    await repo.set_staff_see_all(OTHER, False)
    await press(OTHER, "staff")
    assert f"t:{tid}" not in api.payloads(OTHER)


# ── границы прав ───────────────────────────────────────────────────────────
async def test_student_does_not_see_any_queue(api):
    tid = await make_ticket()
    api.sent.clear()
    await press(STUDENT, "staff")
    assert not api.to(STUDENT)          # очередь сотрудника студенту не показывается
    await press(STUDENT, f"ttake:{tid}")
    assert await owner_of(tid) == STAFF


async def test_sysadmin_keeps_delete_right(api):
    tid = await make_ticket()
    await press(SYS, f"tdel:{tid}")
    assert f"tdely:{tid}" in api.payloads(SYS)


@pytest.mark.parametrize("actor", [SYS, STAFF, OTHER])
async def test_queue_labels_fit_for_everyone(api, actor):
    await make_ticket()
    await add_staff(OTHER, "Козлов Иван", category="all")
    await press(actor, "staff")
    assert_fits(api, actor, "очередь")
    assert all(label.strip() and "  " not in label for label in labels(api, actor))
