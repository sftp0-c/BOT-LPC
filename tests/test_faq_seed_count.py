"""Тест: повторная заливка вопросов не врёт в журнале."""
from handlers import faq


async def test_second_seed_reports_zero():
    assert await faq.seed_defaults() == 17      # первый запуск всё добавил
    assert await faq.seed_defaults() == 0        # второй - ничего не изменил


async def test_seed_after_deletion_adds_only_missing():
    import college
    import database as db

    await faq.seed_defaults()
    await db.run("DELETE FROM faq WHERE question=?", (college.DEFAULT_FAQ[0]["question"],))
    assert await faq.seed_defaults() == 1
