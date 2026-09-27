"""Слежение за PDF расписаний: сравнение отпечатков, рассылка подписчикам, фоновый круг.

Сети нет: список PDF, скачивание файлов и разбор подменяются (monkeypatch),
база — временная из conftest.
"""
import asyncio
import hashlib

import pytest

import bot
import config
import repository as repo
import schedule_watch as watch
import timetable as tt

PAGE_URL = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"
PDF_1 = "https://collegelan.ru/files/23-29-24-25.pdf"
PDF_2 = "https://collegelan.ru/files/24-26-25-20.pdf"
PDF_NEW = "https://collegelan.ru/files/26-11-26-27.pdf"
FILE_1 = "расписание 1 курса, старая версия"
FILE_2 = "расписание 2 курса, старая версия"
FILE_NEW = "новый файл, которого ещё нет в справочнике"
FILE_1_NEW = "расписание 1 курса, новая версия"
FILE_2_NEW = "расписание 2 курса, новая версия"


def file_stamp(text: str) -> str:
    """Настоящий отпечаток файла — так же, как его считает handlers.schedules."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


WATCHER = "1001"   # подписан на 24-29П
SECOND = "1002"    # подписан на 24-29С
OTHER = "1003"     # подписан на 25-20
STRANGER = "1004"  # подписок нет
SYSADMIN = "1"     # из conftest


def make_schedule(group: str, subject: str = "Математика") -> tt.GroupSchedule:
    """GroupSchedule с одной парой: важны группа и предмет, а не число занятий."""
    return tt.GroupSchedule(group=group, days={0: tt.DaySchedule(weekday=0, lessons=[
        tt.Lesson(number=1, subject=subject, teacher="Иванов И. И.",
                  room="101", start="09:00", end="09:45")])})


@pytest.fixture
async def college(monkeypatch):
    """Сайт колледжа в памяти: список PDF, содержимое файлов, разбор и импорт.

    files — «содержимое» каждого PDF (по нему считается настоящий отпечаток),
    subjects — предмет первой пары в файле (как занятия в реальном PDF);
    fail_on — какие файлы не скачиваются вовсе; broken — для каких групп разбор
    ничего не находит; parsed/imported — что бот трогал.
    """
    state = {
        "files": {PDF_1: FILE_1, PDF_2: FILE_2, PDF_NEW: FILE_NEW},
        "subjects": {PDF_1: "Математика", PDF_2: "Физика"},
        "urls": [PDF_1, PDF_2],
        "fail_on": set(),
        "broken": set(),
        "parsed": [],
        "imported": [],
        "page": "",
    }

    def fresh(url: str) -> None:
        """Колледж обновил файл: и отпечаток, и сами занятия другие."""
        state["files"][url] = f"{state['files'][url]} — новая версия"
        state["subjects"][url] = f"{state['subjects'][url]} (новый семестр)"

    def resave(url: str) -> None:
        """Колледж перезаписал файл, а занятия оставил прежними."""
        state["files"][url] = f"{state['files'][url]} — перезаписан тем же текстом"

    async def collect(source):
        state["page"] = source
        return list(state["urls"])

    async def download(url):
        if url in state["fail_on"]:
            raise RuntimeError("503 Service Unavailable")
        return state["files"][url].encode()

    async def parse_group(code, force=False):
        state["parsed"].append((code, force))
        if code in state["broken"]:
            return watch.schedule_service.ScheduleResult(code, url=PDF_1, error="в PDF не нашлось занятий")
        row = await repo.get_schedule(code)
        url = row["pdf_url"] if row else PDF_1
        schedule = make_schedule(code, state["subjects"].get(url, "Математика"))
        # Настоящий save_lessons обновляет parsed_hash — так проверяем
        # идемпотентность: второй круг в тот же день не должен ничего слать.
        await repo.save_lessons(code, schedule, file_stamp(state["files"].get(url, FILE_1)), [code])
        return watch.schedule_service.ScheduleResult(code, schedule=schedule, url=url)

    async def import_pdf(url, times=None):
        state["imported"].append(url)
        return {"url": url, "groups": [], "lessons": 0}

    state["fresh"] = fresh
    state["resave"] = resave
    monkeypatch.setattr(watch.schedule_import, "collect_pdf_urls", collect)
    monkeypatch.setattr(watch.schedule_import, "import_pdf", import_pdf)
    monkeypatch.setattr(watch.schedule_service, "download", download)
    monkeypatch.setattr(watch.schedule_service, "parse_group", parse_group)
    return state


async def add_group(code: str, url: str, content: str = "") -> None:
    """Группа в справочнике: ссылка на PDF и сохранённый разбор файла."""
    await repo.upsert_schedule(code, url)
    if content:
        await repo.save_lessons(code, make_schedule(code), file_stamp(content), [code])


@pytest.fixture
async def groups(college):
    """Две группы в одном PDF (24-29П, 24-29С) и одна во втором (25-20).

    Подписка в проекте одна на человека (таблица schedule_subscriptions), поэтому
    подписчик у каждой группы свой.
    """
    await add_group("24-29П", PDF_1, FILE_1)
    await add_group("24-29С", PDF_1, FILE_1)
    await add_group("25-20", PDF_2, FILE_2)
    await repo.set_schedule_subscription(WATCHER, "24-29П")
    await repo.set_schedule_subscription(SECOND, "24-29С")
    await repo.set_schedule_subscription(OTHER, "25-20")
    return college


# ── отпечаток файла совпал: тишина ─────────────────────────────────────────────


async def test_same_hash_notifies_nobody(groups, api):
    result = await watch.check_college_updates()
    assert result["changed"] == [] and result["new"] == []
    assert result["checked"] == 2, "оба файла должны быть скачаны и сравнены"
    assert result["errors"] == []
    assert groups["parsed"] == [], "прежний файл разбирать незачем"
    assert api.sent == [], "подписчикам писать не о чем"


async def test_page_url_is_the_college_one(groups):
    assert (await watch.check_college_updates()) is not None
    assert groups["page"] == PAGE_URL


async def test_watch_once_is_quiet_when_nothing_changed(groups, api):
    result = await watch.watch_once()
    assert result["notified"] == 0
    assert api.sent == []


# ── файл изменился: подписчики получают сообщение ─────────────────────────────


async def test_changed_hash_reports_groups(groups):
    groups["fresh"](PDF_1)
    groups["fresh"](PDF_2)
    result = await watch.check_college_updates()
    assert result["changed"] == ["24-29П", "24-29С", "25-20"]
    assert groups["parsed"] == [("24-29П", True), ("24-29С", True), ("25-20", True)], \
        "изменившийся файл читаем принудительно"


async def test_notify_changed_reaches_every_subscriber(groups, api):
    groups["fresh"](PDF_1)
    groups["fresh"](PDF_2)
    result = await watch.check_college_updates()

    sent = await watch.notify_changed(result["changed"])
    assert sent == 3, "по одному сообщению каждому подписчику своей группы"
    assert not api.to(STRANGER), "без подписки никому ничего не отправляется"
    assert len(api.to(WATCHER)) == 1 and len(api.to(SECOND)) == 1
    assert len(api.to(OTHER)) == 1, "подписчик своей группы — одно сообщение"
    for _uid, text, _kb in api.sent:
        assert "Расписание группы" in text and "обновилось" in text


async def test_watch_once_sends_and_counts(groups, api):
    groups["fresh"](PDF_1)
    result = await watch.watch_once()
    assert result["changed"] == ["24-29П", "24-29С"]
    assert result["notified"] == 2
    assert len(api.sent) == 2


async def test_second_round_same_day_sends_nothing(groups, api):
    """Повторный запуск в тот же день: отпечаток уже сохранён — второй раз не пишем."""
    groups["fresh"](PDF_1)
    first = await watch.watch_once()
    assert first["notified"] == 2
    groups["parsed"].clear()

    second = await watch.watch_once()
    assert second["changed"] == [] and second["new"] == []
    assert second["notified"] == 0
    assert groups["parsed"] == [], "повторно разбирать неизменившийся файл не нужно"
    assert len(api.sent) == 2, "студент получил ровно одно сообщение на группу"


async def test_resaved_file_with_same_lessons_is_not_a_change(groups, api):
    """Колледж перезаписал PDF, а занятия те же — уведомлять не о чем."""
    groups["resave"](PDF_1)
    result = await watch.check_college_updates()
    assert result["changed"] == [] and result["new"] == []
    assert await watch.notify_changed(result["changed"]) == 0
    assert api.sent == []


async def test_first_parse_reported_as_new(college, api):
    """Группу добавили, но ни разу не разобрали: подписчик узнаёт, что расписание появилось."""
    await repo.upsert_schedule("24-29П", PDF_1)  # без save_lessons: parsed_hash пуст
    await repo.set_schedule_subscription(WATCHER, "24-29П")
    result = await watch.check_college_updates()
    assert result["new"] == ["24-29П"] and result["changed"] == []
    assert await watch.notify_changed(result["new"], "появилось") == 1
    assert "появилось" in api.last(WATCHER)[1]


async def test_unknown_file_is_imported(college):
    """PDF, которого ещё нет в справочнике, импортируется целиком."""
    await add_group("24-29П", PDF_1, FILE_1)  # известный файл: его не импортируем заново

    async def import_pdf(url, times=None):
        college["imported"].append(url)
        return {"url": url, "groups": ["26-11А"], "lessons": 12}

    college["urls"] = [PDF_1, PDF_NEW]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(watch.schedule_import, "import_pdf", import_pdf)
        result = await watch.check_college_updates()
    assert college["imported"] == [PDF_NEW]
    assert result["new"] == ["26-11А"]
    assert result["checked"] == 2 and result["errors"] == []


# ── сбои не роняют круг ───────────────────────────────────────────────────────


async def test_broken_file_does_not_stop_others(groups, api):
    groups["fail_on"] = {PDF_1}      # первый файл недоступен
    groups["fresh"](PDF_2)           # второй обновился
    result = await watch.check_college_updates()
    assert result["changed"] == ["25-20"]
    assert result["checked"] == 1
    assert result["errors"] and "23-29-24-25.pdf" in result["errors"][0]
    assert await watch.notify_changed(result["changed"]) == 1
    assert len(api.sent) == 1


async def test_unparsable_file_reports_reason(groups, api):
    groups["fresh"](PDF_1)
    groups["broken"] = {"24-29П", "24-29С"}
    result = await watch.check_college_updates()
    assert result["changed"] == []
    assert any("в PDF не нашлось занятий" in reason for reason in result["errors"])
    assert not api.to(WATCHER), "ошибка разбора — не повод тревожить студентов"


async def test_page_unavailable_returns_errors(college, monkeypatch, api):
    async def broken(source):
        raise RuntimeError("таймаут")

    monkeypatch.setattr(watch.schedule_import, "collect_pdf_urls", broken)
    result = await watch.check_college_updates()
    assert result["errors"] == ["страница расписаний: таймаут"]
    assert result["changed"] == [] and result["checked"] == 0
    assert await watch.notify_changed(result["changed"]) == 0
    assert api.sent == []


async def test_notify_failure_does_not_raise(groups, api):
    """Студент заблокировал бота: круг продолжается, остальным сообщение доходит."""
    api.blocked.add(WATCHER)
    groups["fresh"](PDF_1)
    result = await watch.watch_once()
    assert result["changed"] == ["24-29П", "24-29С"]
    assert result["notified"] == 1, "доставлено только тому, кому бот ещё не заблокирован"
    assert not api.to(WATCHER) and len(api.to(SECOND)) == 1


async def test_watch_once_alerts_sysadmin_once(groups, api):
    """Сбой проверки сис-админу показывают один раз, а не каждый круг."""
    watch._alerts.clear()  # кулдаун общий на весь процесс, чистим перед тестом
    groups["fail_on"] = {PDF_1, PDF_2}
    for _ in range(3):
        await watch.watch_once()
    assert len(api.to(SYSADMIN)) == 1, "одно и то же сообщение не спамится"
    assert "503" in api.last(SYSADMIN)[1]


# ── фоновый цикл ──────────────────────────────────────────────────────────────


def test_interval_from_config(monkeypatch):
    # настройки в config.py пока нет — работает константа модуля
    assert watch.watch_interval_hours() == watch.WATCH_INTERVAL_HOURS
    # если настройка появится (или её подставили), берём её и не копируем в .env
    monkeypatch.setattr(config, "SCHEDULE_WATCH_HOURS", 3, raising=False)
    assert watch.watch_interval_hours() == 3
    monkeypatch.setattr(config, "SCHEDULE_WATCH_HOURS", 0)
    assert watch.watch_interval_hours() == watch.WATCH_INTERVAL_HOURS


async def test_start_watcher_twice_keeps_one_task():
    watch.start_watcher()
    task = watch._watcher  # задача не должна плодиться
    assert task is not None and not task.done()
    watch.start_watcher(6)
    assert watch._watcher is task, "повторный запуск не должен создавать вторую задачу"
    watch.stop_watcher()
    await asyncio.sleep(0)
    assert task.cancelled(), "задача слежения должна быть погашена"
    watch.stop_watcher()  # повторная остановка — не ошибка
    assert watch._watcher is None


async def test_loop_keeps_going_after_error(monkeypatch):
    """Сбой в круге не убивает наблюдателя: следующая пауза — и снова проверка."""
    rounds = []

    async def failing_once():
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("сеть недоступна")

    async def short_wait(seconds):
        if len(rounds) >= 2:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    monkeypatch.setattr(watch, "watch_once", failing_once)
    monkeypatch.setattr(watch, "_wait", short_wait)
    task = asyncio.create_task(watch.watch_loop(1))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(rounds) == 2, "после ошибки круги должны продолжаться"
    assert task.done()


# ── запуск и остановка в lifespan ─────────────────────────────────────────────


async def test_lifespan_starts_and_stops_watcher(monkeypatch, api):
    calls = []

    async def idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(config, "MAX_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    monkeypatch.setattr(bot, "poll", idle)
    monkeypatch.setattr(bot, "start_watcher", lambda: calls.append("start"))
    monkeypatch.setattr(bot, "stop_watcher", lambda: calls.append("stop"))
    async with bot.lifespan(bot.app):
        assert calls == ["start"], "слежение запускается при старте бота"
    assert calls == ["start", "stop"], "при остановке слежение гасится"
