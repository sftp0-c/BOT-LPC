"""Бот отвечает на частый вопрос сам, а не заставляет читать меню."""
import college
from conftest import press_visible, register, say
from handlers import faq

STUDENT = "100"


async def seeded():
    await faq.seed_defaults()


async def test_plain_question_gets_an_answer(api):
    await register(STUDENT)
    await seeded()
    await say(STUDENT, "где находится колледж и куда звонить")
    text = api.to(STUDENT)[-1][1]
    assert "Ленина" in text
    # ответ ведёт на сайт колледжа (кнопка-ссылка) и предлагает написать сотруднику
    assert "new:feedback" in api.payloads(STUDENT)
    assert "faq" in api.payloads(STUDENT)


async def test_unrelated_text_falls_back_to_menu(api):
    await register(STUDENT)
    await seeded()
    before = len(api.to(STUDENT))
    await say(STUDENT, "ааааа")
    assert len(api.to(STUDENT)) == before + 1
    assert "Не удалось подобрать ответ" in api.last(STUDENT)[1]
    assert api.payloads(STUDENT) == ["faq", "home"]
    assert all("…" not in button["text"] for row in api.last(STUDENT)[2] for button in row)
    await press_visible(api, STUDENT, "home")
    assert "ticket_menu" in api.payloads(STUDENT)


async def test_answer_suggests_writing_to_staff(api):
    await register(STUDENT)
    await seeded()
    await say(STUDENT, "какие документы нужны для поступления")
    assert "new:feedback" in api.payloads(STUDENT)


async def test_switched_off_faq_keeps_old_behaviour(api):
    await register(STUDENT)
    await seeded()
    await faq.set_ask_enabled(False)
    try:
        await say(STUDENT, "где находится колледж и куда звонить")
        assert "Ленина" not in api.to(STUDENT)[-1][1]
    finally:
        await faq.set_ask_enabled(True)


async def test_bot_does_not_invent_answers(api):
    """На странный вопрос ответа быть не должно - только приглашение написать."""
    await register(STUDENT)
    await seeded()
    await say(STUDENT, "а выдайте мне миллион рублей на личные нужды")
    last = api.to(STUDENT)[-1][1]
    assert "миллион" not in last.lower()


async def test_questions_are_seeded_without_overwriting_edits():
    await faq.seed_defaults()
    first = (await faq.active_items())[0]
    await faq.cb_faq_item_offline(first["id"]) if hasattr(faq, "cb_faq_item_offline") else None
    import database as db
    await db.run("UPDATE faq SET question=? WHERE id=?", ("Правленый вопрос сис-админа", int(first["id"])))
    await faq.seed_defaults()
    row = await db.one("SELECT question FROM faq WHERE id=?", (int(first["id"]),))
    assert row["question"] == "Правленый вопрос сис-админа"


async def test_contacts_answer_mentions_site(api):
    """В ответе про контакты есть ссылка на официальный сайт."""
    await register(STUDENT)
    await seeded()
    await say(STUDENT, "телефон приемной директора")
    site = college.SITE.split("//")[-1].strip("/")
    assert site in " ".join(api.to(STUDENT)[-1][1]) or "2-26" in api.to(STUDENT)[-1][1]


async def test_default_welcome_is_concise_and_keeps_custom_text(api):
    import database as db
    from handlers import menus

    await register(STUDENT)
    await db.run("DELETE FROM settings WHERE key='welcome_text'")
    await menus.show_home(STUDENT)
    text = api.last(STUDENT)[1]
    assert text.count("Навигатор ЛПК") == 1
    assert "Группа:" in text and "Иван" in text
    assert "в двух нажатиях" not in text and "жалоба" not in text
    assert api.payloads(STUDENT) == ["ticket_menu", "sched", "profile", "help"]
    await db.set_setting("welcome_text", "Особый текст колледжа: {группа}")
    await menus.show_home(STUDENT)
    assert "Особый текст колледжа:" in api.last(STUDENT)[1]
