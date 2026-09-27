"""Заглушки PDF для тестов импорта расписаний."""
import httpx
import pytest

import schedule_import as imp
import timetable as tt
from conftest import login_panel, post_form, press

PAGE = "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"
PAGE_HTML = """
<html><body>
<a href="/files/23-29-24-25.pdf">1 курс</a>
<a href="https://collegelan.ru/files/24-26-25-20.pdf">2 курс</a>
<a href="/files/raspisanie%20konsultaciy.pdf">Консультации</a>
<a href="/files/23-29-24-25.pdf">тот же файл ещё раз</a>
</body></html>
"""


def make_schedule(group: str, lessons_per_day: int = 2) -> tt.GroupSchedule:
    """Настоящий GroupSchedule нужного вида - чтобы save_lessons увидел то же, что в бою."""
    days = {}
    for weekday in range(lessons_per_day):
        days[weekday] = tt.DaySchedule(weekday=weekday, lessons=[
            tt.Lesson(number=i + 1, subject=f"Предмет {i + 1}", teacher="Иванов И. И.",
                      room="101", start="09:00", end="09:45")
            for i in range(lessons_per_day)
        ])
    return tt.GroupSchedule(group=group, days=days)


class FakeResponse:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=httpx.Request("GET", PAGE), response=None)


class FakeClient:
    """Подмена httpx.AsyncClient: отдаём заранее заданные ответы."""

    def __init__(self, pages):
        self.pages, self.requests = pages, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.requests.append(url)
        for prefix, content in self.pages.items():
            if url.startswith(prefix):
                return FakeResponse(content)
        return FakeResponse(status=404)


@pytest.fixture
def fake_page(monkeypatch):
    def install(html=PAGE_HTML.encode()):
        client = FakeClient({PAGE: html})
        monkeypatch.setattr(imp.httpx, "AsyncClient", lambda **kwargs: client)
        return client
    return install


SYS = "1"


@pytest.fixture
async def sysadmin():
    """Сис-админ в базе: кнопки сис-админа проверяем по-настоящему."""
    import repository as repo

    await repo.grant_sysadmin("1", "Иванов Иван Иванович")
    return "1"


@pytest.fixture
def college_pdf(monkeypatch):
    """PDF с двумя группами в заголовках колонок, как на сайте колледжа."""
    async def fake_download(url):
        if url.endswith("broken.pdf"):
            raise ValueError("не скачался PDF")
        return b"%PDF-1.7\nfake"

    async def fake_times():
        return {}

    def fake_extract(data):
        return [1]

    def fake_groups_in_pages(pages):
        return ["24-23 (П)", "24-21(2С)", "График консультаций"]

    def fake_parse(data, name, clock):
        return make_schedule(name), True

    monkeypatch.setattr("handlers.schedules.download", fake_download)
    monkeypatch.setattr("handlers.schedules.file_hash", lambda data: "hash-1")
    monkeypatch.setattr("handlers.schedules.lesson_times", fake_times)
    monkeypatch.setattr(imp.tt, "extract_pages", fake_extract)
    monkeypatch.setattr(imp.tt, "groups_in_pages", fake_groups_in_pages)
    monkeypatch.setattr(imp.tt, "parse_pdf_bytes", fake_parse)


# ── разбор того, что ввёл человек ─────────────────────────────────────────────
async def test_page_expands_to_pdf_links(fake_page):
    fake_page()
    assert await imp.collect_pdf_urls(PAGE) == [
        "https://collegelan.ru/files/23-29-24-25.pdf",
        "https://collegelan.ru/files/24-26-25-20.pdf",
        "https://collegelan.ru/files/raspisanie%20konsultaciy.pdf",
    ]


async def test_direct_pdf_is_kept_as_is(fake_page):
    fake_page()
    assert await imp.collect_pdf_urls("https://college.example/24-23.pdf") == [
        "https://college.example/24-23.pdf"]


async def test_multiline_list_with_blank_lines_and_duplicates(fake_page):
    fake_page()
    source = """
https://college.example/23-29-24-25.pdf
https://college.example/24-26-25-20.pdf

https://college.example/23-29-24-25.pdf
"""
    assert await imp.collect_pdf_urls(source) == [
        "https://college.example/23-29-24-25.pdf",
        "https://college.example/24-26-25-20.pdf",
    ]


async def test_page_and_direct_pdfs_together(fake_page):
    fake_page()
    source = f"{PAGE}\nhttps://college.example/26-28-26-31.pdf"
    urls = await imp.collect_pdf_urls(source)
    assert urls[0].endswith("23-29-24-25.pdf")
    assert urls[-1].endswith("26-28-26-31.pdf")
    assert len(urls) == 4


async def test_garbage_source_yields_nothing(fake_page):
    fake_page()
    assert await imp.collect_pdf_urls("не ссылка\nпросто текст") == []


# ── импорт целиком ─────────────────────────────────────────────────────────────
async def test_import_groups_from_columns_and_survives_broken_file(env, college_pdf):
    """Каждая колонка-PDF даёт свою группу; один сломанный файл не мешает остальным."""
    result = await imp.import_sources("https://college.example/24-23.pdf\n"
                                     "https://college.example/broken.pdf")
    assert result["groups"] == ["24-21-2С", "24-23П"]
    assert result["total"] == 2
    assert result["lessons"] == 8
    assert any("не скачался PDF" in problem for problem in result["problems"])


async def test_import_skips_documents_without_groups(env, monkeypatch):
    """«График консультаций» не должен попасть в справочник групп."""
    async def fake_times():
        return {}

    monkeypatch.setattr("handlers.schedules.download", lambda url: _bytes(b"%PDF-1.7\nfake"))
    monkeypatch.setattr("handlers.schedules.file_hash", lambda data: "hash-1")
    monkeypatch.setattr("handlers.schedules.lesson_times", fake_times)
    monkeypatch.setattr(imp.tt, "extract_pages", lambda data: [1])
    monkeypatch.setattr(imp.tt, "groups_in_pages", lambda pages: ["Кураторы групп БУ ЛПК"])
    result = await imp.import_sources("https://college.example/consult.pdf")
    assert result["groups"] == []
    assert any("группы не найдены" in problem for problem in result["problems"])


async def test_import_saves_group_schedule_and_lessons(env, college_pdf):
    """После импорта группа доступна в справочнике и по любому написанию."""
    import database as db
    import repository as repo

    await imp.import_sources("https://college.example/24-23.pdf")

    group = await db.one("SELECT * FROM groups WHERE group_code=?", ("24-23П",))
    assert group and group["title"] == "24-23 (П)"
    assert (await repo.get_schedule("24-23П"))["pdf_url"] == "https://college.example/24-23.pdf"
    count = await db.one("SELECT COUNT(*) n FROM lessons WHERE group_code=?", ("24-23П",))
    assert count["n"] == 4
    assert (await repo.find_group("24 23 п"))["code"] == "24-23П"
    # повторный импорт того же файла не плодит дубли
    await imp.import_sources("https://college.example/24-23.pdf")
    count = await db.one("SELECT COUNT(*) n FROM lessons WHERE group_code=?", ("24-23П",))
    assert count["n"] == 4


def _bytes(value):
    async def inner():
        return value
    return inner()


# ── бот ───────────────────────────────────────────────────────────────────────
async def test_schedules_menu_has_import_button(env, sysadmin, api):
    await press("1", "schedules")
    assert "scimport" in api.payloads("1")


async def test_import_button_does_not_block_the_bot(env, sysadmin, api, monkeypatch):
    """Нажатие кнопки отвечает сразу, отчёт приходит отдельным сообщением."""
    from handlers import admin

    started = []

    async def fake_run(user_id):
        started.append(user_id)
        await admin.notify(user_id, "✅ Готово: файлов 7, групп 35, занятий 1181")

    monkeypatch.setattr(admin, "_run_import", fake_run)
    await press("1", "scimport")
    assert "Скачиваю расписания" in "\n".join(body for _, body, _ in api.to("1"))
    assert started == ["1"]
    assert "Готово: файлов 7" in "\n".join(body for _, body, _ in api.to("1"))


async def test_import_button_denied_for_student(env, sysadmin, api):
    await press("300", "scimport")
    assert not any("Скачиваю расписания" in body for _, body, _ in api.to("300"))


async def test_import_reports_failure_to_admin(env, sysadmin, api, monkeypatch):
    from handlers import admin

    async def boom(user_id):
        await admin.notify(user_id, "❌ Импорт не удался: таймаут")

    monkeypatch.setattr(admin, "_run_import", boom)
    await press("1", "scimport")
    assert "Импорт не удался" in "\n".join(body for _, body, _ in api.to("1"))


async def test_import_button_actually_imports(env, sysadmin, api, college_pdf):
    """Полный путь: кнопка -> импорт -> группы в справочнике."""
    import repository as repo
    from handlers import admin

    await admin._run_import("1")
    assert (await repo.find_group("24-23 (П)"))["code"] == "24-23П"
    report = "\n".join(body for _, body, _ in api.to("1"))
    assert "Готово: файлов 18" in report
    assert "24-23П" in report and "24-21-2С" in report


# ── панель ────────────────────────────────────────────────────────────────────
def test_panel_import_form_posts_source(panel_client):
    assert login_panel(panel_client)
    response = panel_client.get("/panel/schedules")
    assert 'action="/panel/schedules/import"' in response.text
    assert 'name="source"' in response.text
    assert "collegelan.ru" in response.text


def test_panel_import_route_reports_result(panel_client, monkeypatch):
    """Список из семи ссылок из панели импортируется целиком."""
    import webpanel

    async def fake_import(source, times=None):
        return {"files": 7, "groups": ["24-23П"], "lessons": 1181, "problems": [],
                "total": 35, "urls": ["https://college.example/23-29-24-25.pdf"] * 7}

    monkeypatch.setattr(webpanel.schedule_import, "import_sources", fake_import)
    assert login_panel(panel_client)
    response = post_form(panel_client, "/panel/schedules/import",
                         {"source": "https://collegelan.ru/studentam/raspisanie-zanyatiy.php"})
    assert response.status_code in (200, 303)
    follow = panel_client.get("/panel/schedules")
    assert "Импортировано файлов: 7" in follow.text
    assert "групп: 35" in follow.text


def test_panel_import_without_links_warns(panel_client, monkeypatch):
    import webpanel

    async def fake_import(source, times=None):
        return {"files": 0, "groups": [], "lessons": 0, "problems": [], "total": 0, "urls": []}

    monkeypatch.setattr(webpanel.schedule_import, "import_sources", fake_import)
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/schedules/import", {"source": "мусор"})
    assert "Не нашлось ни одной ссылки" in panel_client.get("/panel/schedules").text


def test_panel_import_survives_network_error(panel_client, monkeypatch):
    import webpanel

    async def boom(source, times=None):
        raise httpx.HTTPStatusError("bad", request=httpx.Request("GET", PAGE), response=None)

    monkeypatch.setattr(webpanel.schedule_import, "import_sources", boom)
    assert login_panel(panel_client)
    post_form(panel_client, "/panel/schedules/import", {"source": "https://college.example/"})
    assert "Не удалось импортировать" in panel_client.get("/panel/schedules").text


# ── нижнее меню MAX (команды) ────────────────────────────────────────────────
async def test_schedules_menu_has_menu_refresh_button(env, sysadmin, api):
    await press(SYS, "schedules")
    assert "scmenu" in api.payloads(SYS)


async def test_refresh_menu_button_registers_commands(env, sysadmin, api, monkeypatch):
    import bot
    import bot_commands

    calls = []

    async def fake_set_commands(commands):
        calls.append(commands)
        return {}

    monkeypatch.setattr(bot.api, "set_commands", fake_set_commands, raising=False)
    await press(SYS, "scmenu")
    assert len(calls) == 1
    assert [c["name"] for c in calls[0]] == [c["name"] for c in bot_commands.bot_command_list()]
    assert "/schedule" in "\n".join(body for _, body, _ in api.to(SYS))


async def test_refresh_menu_denied_for_student(env, api):
    await press("300", "scmenu")
    assert not any("Нижнее меню" in body for _, body, _ in api.to("300"))
