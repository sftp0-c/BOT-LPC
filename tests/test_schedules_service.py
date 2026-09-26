"""Сервис расписания: скачивание PDF, кэш, запись в БД и уведомления.

Сеть в тестах не используется: download подменяется, разбор страниц — тоже,
чтобы проверять поведение сервиса (кэш, отпечаток файла, запись в базу,
уведомление подписчиков) независимо от вёрстки PDF.
"""
import pytest

import database as db
import repository as repo
from conftest import press, register, say
from handlers import schedules
from test_timetable import cyrillic_week

STUDENT, STAFF = "100", "200"
PDF_URL = "https://college.example/raspisanie.pdf"


@pytest.fixture
def fake_pdf(monkeypatch, tmp_path):
    """Подмена сети и разбора: страницы с кириллицей, счётчик скачиваний."""
    calls = {"download": 0}

    async def download(url):
        calls["download"] += 1
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(schedules, "download", download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    return calls


# ── разбор и сохранение ───────────────────────────────────────────────────────
async def test_parse_group_saves_lessons(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    result = await schedules.parse_group("ИС-21")

    assert result.has_lessons
    assert result.schedule.lessons_count == 3
    assert sorted(result.schedule.days) == [0, 3]
    assert await repo.lessons_count("ИС-21") == 3
    row = await db.one("SELECT * FROM schedules WHERE group_code='ИС-21'")
    assert row["parsed_hash"] and row["parse_error"] == ""
    assert "ИС-21" in row["found_groups"]
    assert fake_pdf["download"] == 1


async def test_second_call_uses_cache(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    first = await schedules.parse_group("ИС-21")
    second = await schedules.parse_group("ИС-21")
    assert first.from_cache is False and second.from_cache is True
    assert fake_pdf["download"] == 1  # PDF второй раз не качался
    assert second.has_lessons


async def test_force_reparses(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await schedules.parse_group("ИС-21")
    await schedules.parse_group("ИС-21", force=True)
    assert fake_pdf["download"] == 2


async def test_expired_cache_revalidates_without_reparse(api, fake_pdf):
    """Через SCHEDULE_CACHE_HOURS PDF скачивается заново, но неизменённый файл не разбирается второй раз."""
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await schedules.parse_group("ИС-21")
    await db.run("UPDATE schedules SET parsed_at=datetime('now','-2 days')")
    result = await schedules.parse_group("ИС-21")
    assert fake_pdf["download"] == 2   # файл проверили заново
    assert result.from_cache is True   # но разбор не переделывали
    assert result.has_lessons and await repo.lessons_count("ИС-21") == 3


async def test_changed_file_triggers_reparse(api, fake_pdf, monkeypatch):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await schedules.parse_group("ИС-21")
    # файл на сервере обновили, и прошло больше времени кэша
    monkeypatch.setattr(schedules, "file_hash", lambda data: "newsha1")
    await db.run("UPDATE schedules SET parsed_at=datetime('now','-2 days')")
    result = await schedules.parse_group("ИС-21")
    assert fake_pdf["download"] == 2
    assert result.from_cache is False
    assert result.has_lessons
    stamp = await db.one("SELECT parsed_hash FROM schedules WHERE group_code='ИС-21'")
    assert stamp["parsed_hash"] == "newsha1"


async def test_group_without_pdf_returns_reason(api):
    result = await schedules.parse_group("НЕТ-99")
    assert not result.has_lessons
    assert "не задан PDF" in result.reason


async def test_download_failure_is_stored(api, monkeypatch):
    await repo.upsert_schedule("ИС-21", PDF_URL)

    async def broken(url):
        raise ValueError("503 Service Unavailable")

    monkeypatch.setattr(schedules, "download", broken)
    result = await schedules.parse_group("ИС-21")
    assert not result.has_lessons
    assert "не скачался" in result.reason
    row = await db.one("SELECT parse_error FROM schedules WHERE group_code='ИС-21'")
    assert "503" in row["parse_error"]
    assert await repo.lessons_count("ИС-21") == 0


async def test_broken_pdf_is_reported(api, monkeypatch, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)

    def boom(data, max_pages=14):
        raise ValueError("Неверная таблица символов")

    monkeypatch.setattr(schedules.tt, "extract_pages", boom)
    result = await schedules.parse_group("ИС-21")
    assert not result.has_lessons
    assert "не разобрался PDF" in result.reason


async def test_group_not_in_pdf_is_reported(api, fake_pdf):
    await repo.upsert_schedule("БУХ-20", PDF_URL)
    result = await schedules.parse_group("БУХ-20")
    assert not result.has_lessons
    assert "не нашлось занятий группы БУХ-20" in result.reason
    assert "ИС-21" in result.reason  # подсказка, какие группы нашлись


async def test_group_code_mismatch_is_rescued(api, fake_pdf):
    """В справочнике «ИС21», в PDF «ИС-21»: разбор идёт по найденному написанию."""
    await repo.upsert_schedule("ИС21", PDF_URL)
    result = await schedules.parse_group("ИС21")
    assert result.has_lessons
    assert await repo.lessons_count("ИС21") == 3


# ── настройки звонков ─────────────────────────────────────────────────────────
async def test_default_lesson_times_cover_college_schedule(api):
    """Звонки хранятся по номерам уроков: 1 и 2 — первая пара, 3 и 4 — вторая."""
    times = await schedules.lesson_times()
    assert times[0] == ("08:00", "08:45")
    assert times[1] == ("08:50", "09:35")
    assert times[2] == ("09:50", "10:35")
    assert times[3] == ("10:55", "11:40")
    assert times[8] == ("15:45", "17:15")   # 9 урок — пятая пара, один урок
    assert times[10] == ("19:00", "20:30")  # 11 урок — седьмая пара


async def test_lesson_times_setting_round_trip(api):
    await db.set_setting("lesson_times", schedules.times_to_json((("07:30", "08:15"), ("08:20", "09:05"))))
    times = await schedules.lesson_times()
    assert times[:2] == (("07:30", "08:15"), ("08:20", "09:05"))


async def test_lesson_times_accept_pair_format(api):
    """Настройка, записанная по парам, разворачивается в номера уроков подряд."""
    await db.set_setting("lesson_times", '{"1": [["08:00", "08:45"], ["08:50", "09:35"]], '
                                         '"2": [["09:50", "10:35"], ["10:55", "11:40"]]}')
    assert (await schedules.lesson_times())[:4] == (
        ("08:00", "08:45"), ("08:50", "09:35"), ("09:50", "10:35"), ("10:55", "11:40"),
    )


async def test_lesson_times_accepts_old_format(api):
    """Старый формат (пара -> один отрезок) тоже читается как номер урока."""
    await db.set_setting("lesson_times", '{"1": ["08:00", "08:45"]}')
    assert (await schedules.lesson_times())[0] == ("08:00", "08:45")


async def test_broken_lesson_times_setting_falls_back(api):
    await db.set_setting("lesson_times", "не json")
    assert (await schedules.lesson_times())[0] == ("08:00", "08:45")
    await db.set_setting("lesson_times", '{"1": [["25:99", "abc"]], "x": [["09:00", "09:45"]]}')
    assert (await schedules.lesson_times())[0] == ("08:00", "08:45")  # неверные значения отброшены


async def test_reapply_lesson_times_updates_saved_lessons(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await schedules.parse_group("ИС-21")
    assert (await repo.lessons_for_group("ИС-21"))[0]["start"] == "08:00"

    assert await repo.reapply_lesson_times((("07:00", "07:45"), ("07:55", "08:40"))) == 3
    by_number = {(row["weekday"], row["lesson_num"]): row for row in await repo.lessons_for_group("ИС-21")}
    assert (by_number[(0, 1)]["start"], by_number[(0, 1)]["end"]) == ("07:00", "07:45")
    assert by_number[(0, 3)]["start"] == ""      # третьего урока в этих звонках нет
    assert by_number[(3, 2)]["start"] == "07:55"  # 2-й урок получил второе время


async def test_lesson_time_edit_is_stored(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await schedules.parse_group("ИС-21")
    assert await repo.set_lesson_time("ИС-21", 0, 1, "08:30", "09:15") == 1
    assert (await repo.lessons_for_group("ИС-21"))[0]["start"] == "08:30"
    assert await repo.set_lesson_time("ИС-21", 0, 9, "08:30", "09:15") == 0


# ── уведомление об изменениях ─────────────────────────────────────────────────
async def test_refresh_all_notifies_subscribers_only_on_change(api, fake_pdf):
    await register(STUDENT)
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await repo.set_schedule_subscription(STUDENT, "ИС-21")

    results = await schedules.refresh_all()
    assert "ИС-21" in results and results["ИС-21"].has_lessons
    assert any("обновилось" in text for _, text, _ in api.to(STUDENT))

    # повторный разбор без изменений подписчиков не тревожит
    api.sent.clear()
    await schedules.refresh_all()
    assert not any("обновилось" in text for _, text, _ in api.to(STUDENT))

    # а вот изменение занятий — повод для уведомления
    await repo.set_lesson_time("ИС-21", 0, 1, "07:00", "07:45")
    await db.run("UPDATE lessons SET subject='Математика (лекция)' WHERE group_code='ИС-21'")
    api.sent.clear()
    await schedules.refresh_all()
    assert any("обновилось" in text for _, text, _ in api.to(STUDENT))


async def test_refresh_all_reports_broken_groups(api, fake_pdf):
    await repo.upsert_schedule("БУХ-20", PDF_URL)
    results = await schedules.refresh_all()
    assert not results["БУХ-20"].has_lessons
    assert "не нашлось" in results["БУХ-20"].reason


# ── расписание в боте ─────────────────────────────────────────────────────────
async def test_bot_shows_parsed_schedule_instead_of_link(api, fake_pdf):
    await register(STUDENT)
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await press(STUDENT, "sched")
    text = api.last(STUDENT)[1]
    assert "Расписание группы ИС-21" in text
    assert "Математика" in text and "08:00–08:45" in text and "ауд. 204" in text
    assert "Иванова А. А." in text
    assert f"schedday:ИС-21:{__import__('datetime').datetime.now().weekday()}" in api.payloads(STUDENT)


async def test_bot_schedule_buttons_work(api, fake_pdf):
    await register(STUDENT)
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await press(STUDENT, "sched")
    await press(STUDENT, "schednext:ИС-21")
    assert "Ближайшие занятия" in api.last(STUDENT)[1]
    await press(STUDENT, "schedday:ИС-21:3")
    assert "Четверг" in api.last(STUDENT)[1]
    await press(STUDENT, "schedday:ИС-21:5")  # суббота: пар нет
    assert "пар нет" in api.last(STUDENT)[1]


async def test_bot_reload_works(api, fake_pdf):
    await register(STUDENT)
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await press(STUDENT, "sched")
    await press(STUDENT, "schedreload:ИС-21")
    assert "Расписание группы ИС-21" in api.last(STUDENT)[1]
    assert fake_pdf["download"] == 2


async def test_bot_falls_back_to_link_when_parse_fails(api, fake_pdf, monkeypatch):
    await register(STUDENT)
    await repo.upsert_schedule("ИС-21", PDF_URL)

    async def broken(url):
        raise ValueError("404")

    monkeypatch.setattr(schedules, "download", broken)
    await press(STUDENT, "sched")
    text = api.last(STUDENT)[1]
    assert PDF_URL in text
    assert "разобрать не удалось" in text


async def test_bot_guest_can_read_schedule(api, fake_pdf):
    await repo.upsert_schedule("ИС-21", PDF_URL)
    await say("555", "/start")
    await press("555", "who:guest")
    await press("555", "sched:ИС-21")
    assert "Математика" in api.last("555")[1]
