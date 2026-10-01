"""Уведомления подписчикам об изменениях расписания.

Проверяются правила, из-за которых сообщения не доходили:
  * круг слежения должен случиться вскоре после старта бота, а не через полный
    интервал — раньше круг не наступал ни разу, сколько бот ни перезапускали;
  * повторный круг и повторный старт бота не присылают то же самое снова;
  * подписчик есть, а файл прежний — тишина; файл прежний, а подписка появилась
    позже — тоже тишина;
  * сообщение отвечает на три вопроса: что изменилось, с какой даты и что делать;
  * писать можно только подписчикам своей группы.

Сети нет: страница со ссылками, скачивание и разбор подменяются, база временная.
"""
import asyncio
import hashlib
from datetime import timedelta

import pytest

import clock
import database as db
import repository as repo
import schedule_import
import schedule_watch as watch
import timetable as tt
from handlers import schedules as schedule_service

PAGE_URL = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"
PDF = "https://collegelan.ru/files/24-26-25-20%20изм.pdf"
PDF_OLD = "https://collegelan.ru/files/23-29-24-25.pdf"
FILE_TEXT = "расписание 2 курса, первая версия"

GROUP = "24-29П"
NEIGHBOUR = "25-20"
WATCHER = "1001"     # подписан на 24-29П
SECOND = "1002"      # подписан на 25-20
LATE = "1003"        # подпишется позже
STRANGER = "1004"    # подписок нет ни на одну группу


def file_stamp(text: str) -> str:
    """Настоящий отпечаток файла — так же, как его считает handlers.schedules."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def after_start(start: str, minutes: int = 45) -> str:
    """Конец урока: пара в колледже идёт 45 минут, время в расписании сдвинули."""
    hour, minute = (int(part) for part in start.split(":"))
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def make_schedule(group: str, subject: str = "Математика", room: str = "204",
                  week=None, start: str = "08:00") -> tt.GroupSchedule:
    """Расписание из двух уроков: понедельник и четверг.

    Меняется только пара в понедельник: так видно, что в сообщении перечислены
    именно поменявшиеся пары, а не весь день целиком.
    """
    return tt.GroupSchedule(group=group, week=week, days={
        0: tt.DaySchedule(weekday=0, lessons=[
            tt.Lesson(number=1, subject=subject, teacher="Иванов И. И.", room=room,
                      start=start, end=after_start(start))]),
        3: tt.DaySchedule(weekday=3, lessons=[
            tt.Lesson(number=2, subject="Физика", teacher="Петров П. П.", room="101",
                      start="09:50", end=after_start("09:50"))]),
    })


@pytest.fixture
async def college(monkeypatch, frozen_college_clock):
    """Сайт колледжа в памяти: один файл, содержимое и занятия в нём.

    state["file"] — «содержимое» PDF (по нему считается отпечаток),
    state["subject"], state["room"], state["start"] — как выглядит пара в файле,
    state["week"] — неделя, за которую файл выложен.
    """
    state = {"file": FILE_TEXT, "subject": "Математика", "room": "204",
             "start": "08:00", "week": tt.today_monday(), "urls": [PDF],
             "parsed": [], "imported": []}

    async def collect(source):
        return list(state["urls"])

    async def download(url):
        return state["file"].encode()

    async def parse_group(code, force=False):
        # Так же, как настоящий parse_group: записывает занятия, отпечаток файла
        # и неделю. Из-за отпечатка второй круг не должен ничего слать.
        state["parsed"].append(code)
        schedule = make_schedule(code, state["subject"], state["room"],
                                 state["week"], state["start"])
        await repo.save_lessons(code, schedule, file_stamp(state["file"]), [code])
        await schedule_import.remember_week(code, state["week"])
        return schedule_service.ScheduleResult(code, schedule=schedule, url=PDF)

    async def import_pdf(url, times=None):
        state["imported"].append(url)
        return {"url": url, "groups": [], "lessons": 0, "skipped": []}

    def changed(**what) -> dict:
        """Колледж обновил файл: и отпечаток, и сами занятия другие."""
        state.update(what)
        state["file"] += " — новая версия"
        return state

    state["changed"] = changed
    monkeypatch.setattr(watch.schedule_import, "collect_pdf_urls", collect)
    monkeypatch.setattr(watch.schedule_import, "import_pdf", import_pdf)
    monkeypatch.setattr(watch.schedule_service, "download", download)
    monkeypatch.setattr(watch.schedule_service, "parse_group", parse_group)
    return state


async def put_group(code: str, url: str = PDF, file_text: str = FILE_TEXT) -> None:
    """Группа в справочнике: ссылка на PDF и разбор этого файла."""
    await repo.upsert_schedule(code, url)
    await repo.save_lessons(code, make_schedule(code), file_stamp(file_text), [code])
    await schedule_import.remember_week(code, tt.today_monday())


@pytest.fixture
async def college_with_group(college):
    """Файл прежний, группа в справочнике, подписчик есть."""
    await put_group(GROUP)
    await repo.set_schedule_subscription(WATCHER, GROUP)
    return college


def restart_watcher() -> None:
    """Перезапуск бота: память процесса потеряна, в базе остался круг."""
    watch._checked_this_run = None
    watch._checked_on_start = None


# ── перемена есть: сообщение доходит ──────────────────────────────────────────


async def test_subscriber_gets_message_when_lessons_changed(college_with_group, api):
    college_with_group["changed"](subject="Физика")
    result = await watch.watch_once()
    assert result["changed"] == [GROUP]
    assert result["notified"] == 1
    assert len(api.to(WATCHER)) == 1
    assert "Расписание группы 24-29П" in api.last(WATCHER)[1]


async def test_message_answers_what_changed_from_when_and_what_to_do(college_with_group, api):
    """Студент должен понять из сообщения три вещи: что, с какой даты и что делать."""
    college_with_group["changed"](subject="Физика")
    await watch.watch_once()
    text = api.last(WATCHER)[1]
    assert "Физика" in text, "не сказано, что поменялось"
    assert "Математика" in text, "не сказано, что было вместо Физики"
    assert "Действует с этой недели" in text, "не сказано, с какой даты это"
    assert "Ничего делать не нужно" in text, "не сказано, что делать студенту"
    assert "пн, 1 урок" in text, "не назван день и урок, которые поменялись"


async def test_message_says_which_lesson_moved(college_with_group, api):
    college_with_group["changed"](subject="Физика")
    await watch.watch_once()
    text = api.last(WATCHER)[1]
    assert "«Математика» → «Физика»" in text
    assert "чт, 2 урок" not in text, "непоменявшаяся пара в списке лишняя"


async def test_room_and_time_change_are_reported(college_with_group, api):
    """Переехала аудитория или сдвинулось время — для студента это тоже перемена."""
    college_with_group["changed"](room="305", start="10:00")
    await watch.watch_once()
    text = api.last(WATCHER)[1]
    assert "аудитория 204 → 305" in text
    assert "время 08:00–08:45 → 10:00–10:45" in text


async def test_message_offers_to_open_the_schedule(college_with_group, api):
    college_with_group["changed"](subject="Физика")
    await watch.watch_once()
    keyboard = api.last(WATCHER)[2]
    payloads = [b["payload"] for row in keyboard for b in row]
    assert f"sched:{GROUP}" in payloads, "в сообщении некуда посмотреть расписание"


async def test_next_week_file_says_the_date(college_with_group, api):
    """Файл на следующую неделю: сообщение обязано назвать дату, а не сегодняшний день."""
    college_with_group["changed"](week=tt.today_monday() + timedelta(days=7), subject="Физика")
    await watch.watch_once()
    text = api.last(WATCHER)[1]
    future = tt.today_monday() + timedelta(days=7)
    assert f"Действует с недели {future:%d.%m.%Y}" in text
    assert f"{future:%d.%m.%Y}" in text, "ближайшие занятия показаны не с той недели"


# ── перемены нет: тишина ──────────────────────────────────────────────────────


async def test_second_round_sends_the_same_message_no_more(college_with_group, api):
    """Главное правило: повторный круг не присылает то же самое снова."""
    college_with_group["changed"](subject="Физика")
    first = await watch.watch_once()
    assert first["notified"] == 1
    api.sent.clear()
    college_with_group["parsed"].clear()

    second = await watch.watch_once()
    assert second["changed"] == [] and second["new"] == []
    assert second["notified"] == 0
    assert api.sent == [], "студент получил то же самое второй раз"
    assert college_with_group["parsed"] == [], "прежний файл разбирать незачем"


async def test_restart_right_after_round_sends_nothing(college_with_group, api):
    """Перезапуск бота сразу после круга — тоже не повод писать заново."""
    college_with_group["changed"](subject="Физика")
    assert (await watch.watch_once())["notified"] == 1
    api.sent.clear()

    restart_watcher()
    await watch.load_last_check()
    assert watch.check_due() is False, "после перезапуска проверять слишком рано"
    assert (await watch.watch_once())["notified"] == 0
    assert api.sent == []


async def test_round_is_allowed_again_after_min_gap(college_with_group, api, monkeypatch,
                                                   frozen_college_clock):
    """Через MIN_GAP_SECONDS проверка снова нужна: колледж мог поменять файл."""
    college_with_group["changed"](subject="Физика")
    await watch.watch_once()
    restart_watcher()
    await watch.load_last_check()
    assert watch.check_due() is False
    monkeypatch.setattr(clock, "now", lambda: frozen_college_clock + timedelta(hours=4))
    assert watch.check_due() is True


async def test_subscriber_without_change_hears_nothing(college_with_group, api):
    """Файл прежний, подписка есть — уведомлений быть не должно."""
    result = await watch.watch_once()
    assert result["changed"] == [] and result["new"] == []
    assert result["notified"] == 0
    assert api.sent == []


async def test_subscription_after_change_sends_nothing(college_with_group, api):
    """Файл прежний, ученик подписался позже — ему нечего сообщать."""
    college_with_group["changed"](subject="Физика")
    assert (await watch.watch_once())["notified"] == 1
    api.sent.clear()

    await repo.set_schedule_subscription(LATE, GROUP)
    result = await watch.watch_once()
    assert result["notified"] == 0
    assert not api.to(LATE), "подписавшийся позже получил сообщение о старой перемене"


async def test_resaved_file_with_same_lessons_is_silent(college_with_group, api):
    """Колледж перезаписал PDF, а занятия те же — сообщать не о чем."""
    college_with_group["changed"]()      # файл новый, предмет прежний
    result = await watch.watch_once()
    assert result["changed"] == [] and result["new"] == []
    assert result["notified"] == 0
    assert api.sent == []


# ── только подписчикам своей группы ───────────────────────────────────────────


async def test_only_subscribers_of_that_group_are_written_to(college, api):
    """Две группы, подписчик у каждой: сообщение получает только свой."""
    await put_group(GROUP)
    await put_group(NEIGHBOUR, PDF_OLD)
    await repo.set_schedule_subscription(WATCHER, GROUP)
    await repo.set_schedule_subscription(SECOND, NEIGHBOUR)

    college["changed"](subject="Физика")
    result = await watch.watch_once()
    assert result["changed"] == [GROUP], "файл второй группы тоже помечен как изменившийся"
    assert len(api.to(WATCHER)) == 1
    assert not api.to(SECOND), "подписчику чужой группы написали"
    assert not api.to(STRANGER), "написали тому, кто не подписывался"


async def test_group_without_subscribers_stays_quiet(college, api):
    """Занятия изменились, но подписчиков нет — никому ничего не отправляется."""
    await put_group(GROUP)
    college["changed"](subject="Физика")
    result = await watch.watch_once()
    assert result["changed"] == [GROUP]
    assert result["notified"] == 0
    assert api.sent == []


async def test_first_parse_of_group_is_reported_as_appeared(college, api):
    """Группу только что занесли: подписчик узнаёт, что расписание появилось."""
    await repo.upsert_schedule(GROUP, PDF)     # без разбора: отпечатка нет
    await repo.set_schedule_subscription(WATCHER, GROUP)
    result = await watch.watch_once()
    assert result["new"] == [GROUP]
    assert len(api.to(WATCHER)) == 1
    assert "появилось" in api.last(WATCHER)[1]
    assert "Ничего делать не нужно" in api.last(WATCHER)[1], "новому подписчику нужен тот же ответ"


# ── когда идёт круг ───────────────────────────────────────────────────────────


async def test_first_check_does_not_wait_full_interval(monkeypatch):
    """Круг должен случиться вскоре после старта, а не через полный интервал."""
    waits, rounds = [], []

    async def fast_wait(seconds):
        waits.append(seconds)

    async def once():
        rounds.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(watch, "_wait", fast_wait)
    monkeypatch.setattr(watch, "watch_once", once)
    with pytest.raises(asyncio.CancelledError):   # остановка круга после первого тика
        await watch.watch_loop(12)
    assert rounds == [1], "после старта бота круг не состоялся"
    assert waits[0] == watch.FIRST_CHECK_SECONDS
    assert waits[0] < 12 * 3600, "первая проверка снова отложена на весь интервал"


async def test_next_checks_wait_full_interval(monkeypatch):
    """Дальше круг идёт через интервал, а не непрерывной очередью."""
    waits, rounds = [], []

    async def fast_wait(seconds):
        waits.append(seconds)

    async def once():
        rounds.append(1)
        if len(rounds) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(watch, "_wait", fast_wait)
    monkeypatch.setattr(watch, "watch_once", once)
    with pytest.raises(asyncio.CancelledError):   # остановка круга после третьего тика
        await watch.watch_loop(12)
    assert waits == [watch.FIRST_CHECK_SECONDS, 12 * 3600, 12 * 3600]
    assert len(rounds) == 3


async def test_loop_skips_round_when_it_recently_checked(college_with_group, monkeypatch):
    """Перезапуски и соседние круги не должны превращаться в поток запросов к сайту.

    Круг делается один раз, а следующие тики проходят мимо: check_due() видит,
    что минуту назад уже смотрели.
    """
    runs, ticks = [], []

    async def fake_wait(seconds):
        ticks.append(seconds)
        if len(ticks) > 3:
            raise asyncio.CancelledError      # три тика достаточно, дальше не идём

    async def fake_once():
        runs.append(1)
        await watch._remember_check()

    monkeypatch.setattr(watch, "_wait", fake_wait)
    monkeypatch.setattr(watch, "watch_once", fake_once)
    with pytest.raises(asyncio.CancelledError):   # остановка круга после четвёртого тика
        await watch.watch_loop(12)
    assert len(ticks) == 4
    assert len(runs) == 1, "после только что сделанного круга сайт смотрен заново"


async def test_round_remembers_its_time_in_settings(college_with_group, frozen_college_clock):
    """Круг оставляет след в базе: без него перезапуск забыл бы, что уже смотрел."""
    assert await db.get_setting(watch.LAST_CHECK_KEY, "") == ""
    await watch.watch_once()
    assert clock.parse(await db.get_setting(watch.LAST_CHECK_KEY, "")) == frozen_college_clock
    restart_watcher()
    await watch.load_last_check()
    assert watch.check_due() is False


async def test_broken_setting_does_not_block_checking(college_with_group, monkeypatch):
    """Мусор в настройке не должен молча отключать проверку расписаний."""
    await db.set_setting(watch.LAST_CHECK_KEY, "не дата")
    restart_watcher()
    await watch.load_last_check()
    assert watch.check_due() is True


# ── разбор по-настоящему: неделя и отпечаток ───────────────────────────────────


@pytest.fixture
def real_parser(monkeypatch, frozen_college_clock):
    """Настоящий parse_group с подменённой сетью и разбором PDF."""
    state = {"week": tt.today_monday(), "subject": "Математика"}

    async def download(url):
        return b"%PDF-1.7\n" + state["subject"].encode()

    async def times():
        return {}

    def parse_bytes(data, name, lesson_times):
        return make_schedule(name, state["subject"], week=state["week"]), [{"text": ""}]

    monkeypatch.setattr(schedule_service, "download", download)
    monkeypatch.setattr(schedule_service, "lesson_times", times)
    monkeypatch.setattr(schedule_service.tt, "parse_pdf_bytes", parse_bytes)
    return state


async def test_parse_group_remembers_week_of_file(real_parser):
    """Неделя файла запоминается: без неё в уведомлении не сказать «с какой даты»."""
    await repo.upsert_schedule(GROUP, PDF)
    result = await schedule_service.parse_group(GROUP, force=True)
    assert result.has_lessons
    assert await schedule_import.saved_week(GROUP) == tt.today_monday()


async def test_message_works_without_fresh_parse(college, api):
    """Даже без разбора текущего круга текст собирается: неделя берётся из базы."""
    await put_group(GROUP)
    await repo.set_schedule_subscription(WATCHER, GROUP)
    text = await watch.change_text(GROUP)
    assert "Расписание группы 24-29П" in text
    assert "Действует с этой недели" in text
    assert "Ближайшие занятия" in text


async def test_stale_file_is_remembered_and_not_reparsed_again(real_parser):
    """Файл за прошлую неделю запоминается, и круг слежения его больше не разбирает.

    Именно на совпадении отпечатка schedule_watch решает, разбирать файл или
    пропустить его целиком. Пока отпечаток не записан, такой файл перечитывался бы
    при каждом круге: скачали, разобрали, не записали, скачали снова.
    """
    fresh = tt.today_monday()
    real_parser["week"] = fresh - timedelta(days=14)
    await repo.upsert_schedule(GROUP, PDF)
    await repo.save_lessons(GROUP, make_schedule(GROUP, "Свежая неделя", week=fresh),
                            "hash-fresh", [GROUP])
    await schedule_import.remember_week(GROUP, fresh)

    result = await schedule_service.parse_group(GROUP, force=True)
    assert result.has_lessons, "студенту показали пустоту вместо сохранённой недели"
    subjects = {row["subject"] for row in await repo.lessons_for_group(GROUP)}
    assert "Свежая неделя" in subjects, "свежая неделя вытеснена старой"
    assert "Математика" not in subjects, "старый файл записался поверх свежего"

    stamp = repo.schedule_stamp(await repo.get_schedule(GROUP))
    digest = schedule_service.file_hash(b"%PDF-1.7\n" + "Математика".encode())
    assert stamp["parsed_hash"] == digest, "отпечаток старого файла не запомнен"
    assert stamp["parse_error"] == "", "старый файл - не ошибка разбора"