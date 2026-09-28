"""Тест на защиту недели на втором пути записи.

Импорт со страницы защищён в schedule_import.week_is_fresh(). Но есть ещё
один путь, который пишет занятия: handlers/schedules.parse_group() - его
вызывают «студент открыл своё расписание», кнопка «Обновить» и фоновое
обновление подписок. Он качает PDF группы и зовёт repo.save_lessons()
напрямую, минуя защиту.

Проверяем ровно то, что важно: занятия в базе остаются от свежей недели,
а студенту возвращается то, что в базе уже лежит, - а не устаревший файл
и не пустота.
"""
from datetime import timedelta

import pytest

import handlers.schedules as hs
import repository as repo
import schedule_import as si
import timetable as tt

PDF = "https://college.example/24-23.pdf"


def make_schedule(week, subject: str) -> tt.GroupSchedule:
    return tt.GroupSchedule(group="24-23П", week=week, days={
        0: tt.DaySchedule(weekday=0, lessons=[
            tt.Lesson(number=1, subject=subject, teacher="Иванов И. И.",
                      room="204", start="08:00", end="08:45")]),
    })


@pytest.fixture
async def stored_fresh_week(env):
    """В базе лежит свежая неделя, а качаемый файл - за прошлую."""
    fresh = tt.today_monday()
    old = fresh - timedelta(days=14)
    await repo.upsert_group("24-23П", title="24-23 (П)")
    await repo.upsert_schedule("24-23П", PDF)
    await repo.save_lessons("24-23П", make_schedule(fresh, "Свежая неделя"), "hash-fresh", ["24-23 (П)"])
    await si.remember_week("24-23П", fresh)
    return old


@pytest.fixture
def old_file(monkeypatch, stored_fresh_week):
    """Файл на сервере колледжа оказался за прошлую неделю."""
    async def fake_download(url):
        return b"%PDF-1.7\nold"

    async def fake_times():
        return {}

    def fake_parse(data, name, times):
        return make_schedule(stored_fresh_week, "Старая неделя"), [{"text": ""}]

    monkeypatch.setattr(hs, "download", fake_download)
    monkeypatch.setattr(hs, "lesson_times", fake_times)
    monkeypatch.setattr(hs.tt, "parse_pdf_bytes", fake_parse)
    monkeypatch.setattr(hs.tt, "extract_pages", lambda data: [{"text": ""}])
    monkeypatch.setattr(hs.tt, "groups_in_pages", lambda pages: ["24-23 (П)"])
    return stored_fresh_week


async def test_old_file_does_not_roll_back_the_week(old_file, env):
    """Ключевая проверка: база осталась на свежей неделе."""
    await hs.parse_group("24-23 (П)", force=True)
    stored = await repo.lessons_for_group("24-23П")
    subjects = {row["subject"] for row in stored}
    assert "Свежая неделя" in subjects, "свежая неделя вытеснена старой"
    assert "Старая неделя" not in subjects, "старый файл записался поверх свежего"


async def test_student_still_sees_the_fresh_week(old_file, env):
    """Студент видит свежую неделю, а не устаревшие пары и не пустоту."""
    result = await hs.parse_group("24-23 (П)", force=True)
    assert result.has_lessons, "студенту показали пустое расписание"
    subjects = {lesson.subject for day in result.schedule.days.values()
                for lesson in day.lessons}
    assert subjects == {"Свежая неделя"}
