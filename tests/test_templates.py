"""Шаблоны ответов: хранение, выбор в боте, управление в панели."""
import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, press, register, say

SYS, STAFF, STUDENT = "1", "200", "100"


async def make_ticket(category: str = "feedback") -> int:
    await register(STUDENT, name="Иванов Иван")
    await add_staff(STAFF, "Петрова Анна", category="all")
    await press(STUDENT, f"new:{category}")
    await press(STUDENT, f"pick:{category}:{STAFF}")
    await say(STUDENT, "Нужна справка")
    return (await db.one("SELECT ticket_id FROM tickets ORDER BY ticket_id DESC"))["ticket_id"]


# ── репозиторий ───────────────────────────────────────────────────────────────
async def test_add_and_read_templates():
    template_id = await repo.add_template("Справка готова", "Заберите справку в кабинете 214.", "certificates", "1")
    row = await repo.get_template(template_id)
    assert row["title"] == "Справка готова" and row["category"] == "certificates"
    assert await repo.templates_count() == 1


async def test_list_templates_filters_by_category():
    await repo.add_template("Общее", "Текст для всех", "all", "1")
    await repo.add_template("Справки", "Текст про справки", "certificates", "1")
    for_all = await repo.list_templates()
    for_cert = await repo.list_templates("certificates")
    for_feedback = await repo.list_templates("feedback")
    assert len(for_all) == 2
    assert [row["title"] for row in for_cert] == ["Справки", "Общее"]  # свои первыми
    assert [row["title"] for row in for_feedback] == ["Общее"]          # общий виден всем


async def test_most_used_template_is_first():
    await repo.add_template("Редкий", "раз", "all", "1")
    used = await repo.add_template("Частый", "два", "all", "1")
    for _ in range(3):
        await repo.count_template_use(used)
    titles = [row["title"] for row in await repo.list_templates()]
    assert titles[0] == "Частый"


async def test_delete_template():
    template_id = await repo.add_template("Лишний", "текст", "all", "1")
    await repo.delete_template(template_id)
    assert await repo.get_template(template_id) is None
    assert await repo.templates_count() == 0


# ── бот: выбор шаблона в карточке обращения ───────────────────────────────────
async def test_templates_button_in_ticket_card(api):
    await make_ticket()
    await press(STAFF, "t:1")
    assert "tpl:1" in api.payloads(STAFF)


async def test_staff_picks_template_and_sends_it(api):
    await make_ticket()
    await repo.add_template("Справка готова", "Заберите справку в кабинете 214.", "all", "1")
    await press(STAFF, "tpl:1")
    assert "tplu:" in " ".join(api.payloads(STAFF))
    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]

    await press(STAFF, f"tplu:{template_id}:1")
    assert "tplsend:1" in api.payloads(STAFF)
    await press(STAFF, "tplsend:1")
    assert "отправлено" in api.last(STAFF)[1]
    assert "Заберите справку в кабинете 214." in api.last(STUDENT)[1]
    # статус перешёл в «принято», применение шаблона посчитано
    assert (await db.one("SELECT status FROM tickets WHERE ticket_id=1"))["status"] == "accepted"
    assert (await db.one("SELECT used_count FROM reply_templates"))["used_count"] == 1


async def test_staff_appends_text_to_template(api):
    await make_ticket()
    await repo.add_template("Справка готова", "Заберите справку в 214.", "all", "1")
    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]
    await press(STAFF, f"tplu:{template_id}:1")
    await say(STAFF, "Подпись: секретарь")
    message = api.last(STUDENT)[1]
    assert "Заберите справку в 214." in message
    assert "Подпись: секретарь" in message
    # порядок важен: сначала шаблон, потом дописанное
    assert message.index("Заберите справку") < message.index("Подпись")


async def test_templates_list_is_empty_message(api):
    await make_ticket()
    await press(STAFF, "tpl:1")
    text = api.last(STAFF)[1]
    assert "Шаблонов для этого раздела пока нет" in text
    assert "/panel/templates" not in text  # ссылку не даём - это не путь для сотрудника


async def test_student_cannot_open_templates(api):
    """Студенту объясняем, что шаблоны - инструмент сотрудника, и ничего не раскрываем."""
    ticket = await make_ticket()
    api.sent.clear()
    await press(STUDENT, f"tpl:{ticket}")
    text = api.last(STUDENT)[1]
    assert "Шаблоны доступны сотруднику" in text
    assert not [p for p in api.payloads(STUDENT) if p.startswith("tplu")]


# ── панель ────────────────────────────────────────────────────────────────────
async def test_panel_manages_templates(panel_client):
    assert login_panel(panel_client)
    assert "Шаблонов пока нет" in panel_client.get("/panel/templates").text

    assert post_form(panel_client, "/panel/templates/add", {
        "title": "Справка готова", "text": "Заберите в кабинете 214.", "category": "certificates",
    }).status_code == 303
    body = panel_client.get("/panel/templates").text
    assert "Справка готова" in body and "Заберите в кабинете 214." in body
    assert await repo.templates_count() == 1

    template_id = (await db.one("SELECT id FROM reply_templates"))["id"]
    assert post_form(panel_client, f"/panel/templates/{template_id}/delete").status_code == 303
    assert await repo.templates_count() == 0


async def test_panel_template_requires_title_and_text(panel_client):
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/templates/add",
                     {"title": "", "text": "", "category": "all"}).status_code == 303
    assert "Нужны и название, и текст" in panel_client.get("/panel/templates").text
    assert await repo.templates_count() == 0


async def test_panel_template_requires_csrf(panel_client):
    assert login_panel(panel_client)
    assert panel_client.post("/panel/templates/add",
                             data={"title": "X", "text": "Y"}).status_code == 403
    assert await repo.templates_count() == 0


async def test_templates_page_survives_empty(panel_client):
    """Страница не падает, пока шаблонов нет - их может не быть очень долго."""
    assert login_panel(panel_client)
    assert panel_client.get("/panel/templates").status_code == 200
    assert panel_client.get("/panel/templates", params={"q": "x"}).status_code == 200


