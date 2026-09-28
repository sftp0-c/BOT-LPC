"""Тесты: отбор файлов расписания и защита от отката недели.

Имена файлов и подписи — настоящие, со страницы
https://collegelan.ru/studentam/raspisanie-zanyatiy.php (18 ссылок, снято запросом).
Из них 7 текущих (по группам) и 11 посторонних: кураторы, график консультаций,
файл 2022 года и архивы прошлых лет. Импортировать их нельзя: они идут по
странице ПОСЛЕ текущих, а занятия перезаписываются безусловно, и последний файл
победил бы.

Ручная ссылка из панели — наоборот: импортируется любая, даже с нестандартным
именем, ведь её вписал человек.
"""
import logging
from datetime import date, timedelta
from urllib.parse import unquote, urlsplit

import pytest

import database as db
import repository as repo
import schedule_import as imp
import timetable as tt

PAGE = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"
FOLDER = "https://collegelan.ru/studentam/разписание"
GROUP = "26-23"

# Семь текущих: по группам, в имени две группы и больше через дефис.
CURRENT = [
    "23-29-24-25.pdf",
    "24-26-25-20.pdf",
    "25-21-25-26.pdf",
    "25-27-25-31.pdf",
    "25-32-26-22.pdf",
    "26-23-26-27.pdf",
    "26-28-26-31.pdf",
]

# Одиннадцать посторонних: имя файла и подпись ссылки как на странице.
JUNK = [
    ("кураторы групп на 2026-2027.pdf", "Кураторы групп на 2026-2027 учебный год"),
    ("График консультаций преподавателей 2025-2026.pdf",
     "График консультаций преподавателей на 1-2 полугодие 2025-2026 учебного года"),
    ("04.07-05.07.20222.pdf", ""),
    ("21-26,21-2837.pdf", ""),
    ("15-21,16-22.pdf", ""),
    ("16-23,16-28.pdf", ""),
    ("17-20,17-26.pdf", ""),
    ("17-27,18-22.pdf", ""),
    ("18-24,18-28.pdf", ""),
    ("18-29.pdf", ""),
    ("16-28.pdf", ""),
]


def file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def page_html() -> str:
    """Страница со ссылками как на сайте: сначала текущие, потом посторонние."""
    rows = [f'<a href="/studentam/разписание/{name}"></a>' for name in CURRENT]
    rows += [f'<a href="/studentam/разписание/{name}">{title}</a>' for name, title in JUNK]
    return "<html><body>\n" + "\n".join(rows) + "\n</body></html>"


# ── отбор файлов со страницы ──────────────────────────────────────────────────
@pytest.mark.parametrize("name", CURRENT)
def test_current_files_pass_the_filter(name):
    assert imp.is_schedule_pdf(f"{FOLDER}/{name}") is True


@pytest.mark.parametrize("name", [name for name, _ in JUNK])
def test_junk_files_are_rejected_by_name(name):
    assert imp.is_schedule_pdf(f"{FOLDER}/{name}") is False


@pytest.mark.parametrize("title", ["Кураторы групп на 2026-2027",
                                   "График консультаций преподавателей",
                                   "расписание консультаций"])
def test_junk_title_rejects_even_a_good_file_name(title):
    """Имя файла может оказаться любым — подпись ссылки тоже смотрим."""
    assert imp.is_schedule_pdf(f"{FOLDER}/26-23-26-27.pdf", title) is False


async def test_page_keeps_seven_and_drops_eleven(env, monkeypatch):
    """Страница отдаёт 18 ссылок, в импорт уходят 7 — и именно текущие."""

    class FakeResponse:
        content = page_html().encode("utf-8")
        text = content.decode("utf-8")

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(imp.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    links = await imp.find_pdf_links(PAGE)
    assert [file_name(link) for link in links] == CURRENT
    assert len(links) == 7
    assert len(CURRENT) + len(JUNK) == 18      # столько ссылок лежит на странице


async def test_manual_link_with_odd_name_is_still_imported(env, monkeypatch):
    """Файл, вписанный руками, импортируется даже с нестандартным именем.

    is_schedule_pdf к такой ссылке не применяется: человек знает, что качает.
    """
    url = "https://college.example/26-23-26-27%20(копия).pdf"
    assert imp.is_pdf_url(url) is True
    assert imp.is_schedule_pdf(url) is False
    assert await imp.collect_pdf_urls(url) == [url]

    install(monkeypatch, files={url: (MONDAY_NEW, {0: ["Математика", "Физика"]})})
    result = await imp.import_sources(url)
    assert result["groups"] == [GROUP]
    assert result["total"] == 1
    assert (await repo.get_schedule(GROUP))["pdf_url"] == url
    assert len(await repo.lessons_for_group(GROUP)) == 2


# ── страницы для проверки недели ──────────────────────────────────────────────
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница")


def make_pages(group: str, monday: date | None, subjects: dict[int, list[str]]) -> list[dict]:
    """Страницы недели в том виде, который отдаёт pdfplumber.

    Шапка таблицы двухстрочная, как на сайте: коды групп, а под ними
    «Предмет, вид занятия, преподаватель | Ауд.». monday=None — даты в шапке нет,
    и неделю из PDF прочитать не удаётся.
    """
    pages = []
    for weekday, day_subjects in sorted(subjects.items()):
        header = f"День - {WEEKDAYS[weekday]}"
        if monday:
            header += f", {(monday + timedelta(days=weekday)):%d.%m.%Y}"
        table = [["№", group, None],
                 ["№", "Предмет, вид занятия, преподаватель", "Ауд."]]
        for number, subject in enumerate(day_subjects, start=1):
            table.append([str(number), f"{subject}\n(лекция)\nИванова А. А.", f"10{number}"])
        pages.append({"text": header, "tables": [table]})
    return pages


def install(monkeypatch, group: str = GROUP, files: dict | None = None) -> None:
    """Подмена сети и разбора PDF: у каждой ссылки своя неделя и свои предметы."""
    files = files or {}

    async def fake_download(url: str) -> bytes:
        if url not in files:
            raise ValueError(f"нет такого файла: {url}")
        return url.encode("utf-8")

    def fake_extract(data: bytes) -> list[dict]:
        for url, (monday, subjects) in files.items():
            if url.encode("utf-8") in data:
                return make_pages(group, monday, subjects)
        return []

    monkeypatch.setattr("handlers.schedules.download", fake_download)
    monkeypatch.setattr(imp.tt, "extract_pages", fake_extract)


MONDAY_NEW = date(2026, 9, 28)
MONDAY_OLD = date(2026, 9, 21)
NEW_URL = "https://college.example/26-23-26-27.pdf"
OLD_URL = "https://college.example/25-23-25-26.pdf"
SAME_WEEK_URL = "https://college.example/26-23-26-27-v2.pdf"
NO_DATE_URL = "https://college.example/26-23-26-27-bez-daty.pdf"
FRESH = {NEW_URL: (MONDAY_NEW, {0: ["Новая математика", "Новая физика"]}),
         OLD_URL: (MONDAY_OLD, {0: ["Старая математика", "Старая физика"]}),
         SAME_WEEK_URL: (MONDAY_NEW, {0: ["Исправленная математика", "Новая физика"]}),
         NO_DATE_URL: (None, {0: ["Без даты", "Тоже без даты"]})}


def file_hash(url: str) -> str:
    from handlers.schedules import file_hash as digest
    return digest(url.encode("utf-8"))


async def subjects_in_db(group: str = GROUP) -> list[str]:
    rows = await repo.lessons_for_group(group)
    return [row["subject"] for row in sorted(rows, key=lambda item: (item["weekday"], item["lesson_num"]))]


# ── неделя: откат, равенство, неизвестная неделя ──────────────────────────────
async def test_new_week_is_saved_with_its_date(env, monkeypatch):
    install(monkeypatch, files=FRESH)
    result = await imp.import_pdf(NEW_URL)
    assert result["groups"] == [GROUP] and result["skipped"] == []
    assert await imp.saved_week(GROUP) == MONDAY_NEW
    assert await subjects_in_db() == ["Новая математика | (лекция)", "Новая физика | (лекция)"]


async def test_old_week_does_not_roll_back_fresh_one(env, monkeypatch, caplog):
    """Файл позапрошлой недели не должен откатывать уже сохранённое расписание."""
    install(monkeypatch, files=FRESH)
    await imp.import_pdf(NEW_URL)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bot"):
        result = await imp.import_pdf(OLD_URL)

    assert result["groups"] == [] and result["lessons"] == 0
    assert result["skipped"] == [GROUP]
    assert await imp.saved_week(GROUP) == MONDAY_NEW           # база не тронута
    assert await subjects_in_db() == ["Новая математика | (лекция)", "Новая физика | (лекция)"]
    # ссылка и отпечаток файла прежние: бот не будет перечитывать файл по кругу
    schedule = await repo.get_schedule(GROUP)
    assert schedule["pdf_url"] == NEW_URL
    assert repo.schedule_stamp(schedule)["parsed_hash"] == file_hash(NEW_URL)
    # и в журнал сказано, какие группы пропущены и почему
    assert GROUP in caplog.text and "не тронуто" in caplog.text


async def test_equal_week_is_written_again(env, monkeypatch):
    """Ту же неделю перезалили: содержимое могло поменяться, поэтому пишем."""
    install(monkeypatch, files=FRESH)
    await imp.import_pdf(NEW_URL)
    result = await imp.import_pdf(SAME_WEEK_URL)
    assert result["groups"] == [GROUP] and result["skipped"] == []
    assert await imp.saved_week(GROUP) == MONDAY_NEW
    assert await subjects_in_db() == ["Исправленная математика | (лекция)",
                                      "Новая физика | (лекция)"]


async def test_newer_week_replaces_older(env, monkeypatch):
    install(monkeypatch, files=FRESH)
    await imp.import_pdf(OLD_URL)
    result = await imp.import_pdf(NEW_URL)
    assert result["groups"] == [GROUP] and result["skipped"] == []
    assert await imp.saved_week(GROUP) == MONDAY_NEW
    assert "Новая математика" in (await subjects_in_db())[0]


async def test_unknown_week_writes_but_says_so(env, monkeypatch, caplog):
    """Даты в PDF нет: пишем как раньше, но молчать про это нельзя."""
    install(monkeypatch, files=FRESH)
    with caplog.at_level(logging.INFO, logger="bot"):
        result = await imp.import_pdf(NO_DATE_URL)
    assert result["groups"] == [GROUP] and result["skipped"] == []
    assert await imp.saved_week(GROUP) is None          # неделя так и осталась неизвестной
    assert "не читается" in caplog.text
    assert len(await subjects_in_db()) == 2


async def test_import_report_names_the_groups_skipped_for_old_week(env, monkeypatch):
    install(monkeypatch, files=FRESH)
    await imp.import_pdf(NEW_URL)
    result = await imp.import_sources(OLD_URL)
    assert result["skipped"] == [GROUP]
    assert result["total"] == 0
    assert any("старше сохранённой недели" in problem for problem in result["problems"])


async def test_saved_week_survives_broken_value(env):
    await imp.remember_week(GROUP, MONDAY_NEW)
    assert await imp.saved_week(GROUP) == MONDAY_NEW
    await db.set_setting(imp.WEEK_KEY + GROUP, "не дата")
    assert await imp.saved_week(GROUP) is None
    assert await imp.saved_week("такой-группы") is None


# ── разбор страницы: неделя и своя аудитория ─────────────────────────────────
def test_week_is_read_from_page_header():
    schedule = tt.build_schedule(make_pages(GROUP, MONDAY_NEW, {0: ["Математика"], 2: ["Физика"]}), GROUP)
    assert schedule.week == MONDAY_NEW
    assert sorted(schedule.days) == [0, 2]
    assert schedule.day(0).lessons[0].room == "101"


def test_pages_without_date_have_no_week():
    assert tt.build_schedule(make_pages(GROUP, None, {0: ["Математика"]}), GROUP).week is None


def test_room_is_taken_from_own_column_not_the_last_group():
    """Колонки в файле идут парами «предмет | ауд.» — своя аудитория своя."""
    table = [["№", GROUP, None, "26-24", None],
             ["№", "Предмет, вид занятия, преподаватель", "Ауд.",
              "Предмет, вид занятия, преподаватель", "Ауд."],
             ["1", "Математика\n(лекция)\nИванова А. А.", "101", "Физика\n(лекция)\nПетров П. П.", "999"]]
    page = {"text": "День - Понедельник, 28.09.2026", "tables": [table]}
    assert tt.build_schedule([page], GROUP).day(0).lessons[0].room == "101"
    assert tt.build_schedule([page], "26-24").day(0).lessons[0].room == "999"
