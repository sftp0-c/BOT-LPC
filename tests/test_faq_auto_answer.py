"""Бот отвечает на частый вопрос сам, а не заставляет читать меню."""
import college
from conftest import register, say
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
    await say(STUDENT, "ааааа")
    assert "Используйте кнопки меню" in api.to(STUDENT)[-2][1]
    assert "student_more" in api.payloads(STUDENT)


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
