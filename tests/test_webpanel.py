"""Веб-панель сис-админа: вход, права, правка данных, JSON-API.

Панель работает в том же процессе, что и бот, поэтому тесты бьют по ней через
TestClient с настоящей (временной) базой.
"""
import pytest
from fastapi.testclient import TestClient

import bot
import config
import database as db
import repository as repo
import webpanel
from conftest import click, press, register, say  # noqa: F401  — единый стиль остальных тестов

SYS = "1"        # сис-админ из SYSADMIN_IDS (см. conftest)
PASSWORD = "test-panel-pass"


def fakefake_pdf() -> bytes:
    """Подставной PDF: разбор страниц в этих тестах тоже подменяется."""
    return b"%PDF-1.4 fake"


async def fake_download(url: str) -> bytes:
    """Заглушка вместо скачивания PDF из сети."""
    return fakefake_pdf()


async def fake_probe(url: str) -> tuple:
    """Заглушка вместо проверки ссылки на PDF."""
    return True, ""


@pytest.fixture(autouse=True)
def panel(monkeypatch, env):
    """Панель включена, пароль задан, сессии чистые.

    TestClient без `with` — lifespan не запускается: базу уже поднял conftest,
    а поллер и сетевые вызовы бота в этих тестах не нужны.
    """
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "WEB_PANEL_HOURS", 12)
    webpanel._sessions.clear()
    webpanel._flash = ""
    return TestClient(bot.app)


def login(client, user_id: str = SYS) -> bool:
    return client.post("/panel/login", data={"user_id": user_id, "password": PASSWORD},
                       follow_redirects=False).status_code == 303


def csrf(client) -> str:
    """CSRF-токен формы: он отдельный от cookie сессии (берём из сессии напрямую)."""
    return webpanel._csrf.get(client.cookies.get(webpanel.COOKIE, ""), "")


def post(client, path: str, data: dict | None = None):
    return client.post(path, data={**(data or {}), "csrf": csrf(client)}, follow_redirects=False)


# ── вход и доступ ─────────────────────────────────────────────────────────────
def test_panel_requires_login(panel):
    for path in ("/panel/", "/panel/staff", "/panel/logs", "/panel/api/stats"):
        assert panel.get(path, follow_redirects=False).status_code == 303, path


def test_login_rejects_wrong_password_and_stranger(panel):
    assert panel.post("/panel/login", data={"user_id": SYS, "password": "неверно"},
                      follow_redirects=False).status_code == 403
    # обычный сотрудник — не сис-админ
    assert panel.post("/panel/login", data={"user_id": "555", "password": PASSWORD},
                      follow_redirects=False).status_code == 403


def test_login_and_logout(panel):
    assert login(panel)
    assert panel.get("/panel/").status_code == 200
    assert panel.get("/panel/logout", follow_redirects=False).status_code == 303
    assert panel.get("/panel/", follow_redirects=False).status_code == 303


def test_panel_disabled_without_password(panel, monkeypatch):
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "")
    response = panel.get("/panel/", follow_redirects=False)
    assert response.status_code == 503


def test_dashboard_shows_counts(panel):
    assert login(panel)
    body = panel.get("/panel/").text
    assert "Обзор" in body and "студентов" in body and "событий обработано" in body


# ── правка данных ─────────────────────────────────────────────────────────────
async def test_panel_adds_and_edits_staff(panel):
    assert login(panel)
    assert post(panel, "/panel/staff/add", {
        "user_id": "700", "full_name": "Новиков Пётр", "role": "director",
        "office": "101", "ticket_category": "feedback", "can_broadcast": "1",
    }).status_code == 303
    row = await db.one("SELECT * FROM admins WHERE user_id='700'")
    assert (row["full_name"], row["role"], row["office"]) == ("Новиков Пётр", "director", "101")
    assert row["ticket_category"] == "feedback" and row["can_broadcast"] == 1

    assert post(panel, "/panel/staff/700", {
        "full_name": "Новиков Пётр Сергеевич", "role": "", "office": "202",
        "ticket_category": "all", "can_broadcast": "0",
    }).status_code == 303
    row = await db.one("SELECT * FROM admins WHERE user_id='700'")
    assert row["full_name"] == "Новиков Пётр Сергеевич" and row["office"] == "202"
    assert row["role"] == "" and row["can_broadcast"] == 0


async def test_panel_rejects_bad_staff_id(panel):
    assert login(panel)
    assert post(panel, "/panel/staff/add", {"user_id": "не цифры"}).status_code == 303
    assert await db.one("SELECT 1 FROM admins WHERE user_id='не цифры'") is None


async def test_panel_adds_sysadmin_without_env(panel):
    """Роль сис-админа из панели работает сразу — без правки SYSADMIN_IDS."""
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin", {"user_id": "701"}).status_code == 303
    assert await webpanel.is_sysadmin("701")
    # и второй сис-админ может войти под своим ID
    assert login(panel, "701")


async def test_panel_deletes_staff_only_without_open_tickets(panel):
    await register("100")
    assert login(panel)
    await press(SYS, "sfadd")
    await say(SYS, "800")
    await say(SYS, "Сидоров Пётр")
    assert post(panel, "/panel/staff/800/delete").status_code == 303
    assert await repo.get_admin("800") is None


async def test_panel_group_rename_moves_students(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    assert post(panel, "/panel/groups/rename", {"old": "ИС-21", "code": "ИС-22"}).status_code == 303
    assert (await db.one("SELECT group_code FROM users WHERE user_id='100'"))["group_code"] == "ИС-22"


async def test_panel_group_toggle_and_add(panel):
    assert login(panel)
    assert post(panel, "/panel/groups/add", {"code": "ис-30", "title": "ИС-30", "active": "1"}).status_code == 303
    assert (await repo.get_group("ИС-30"))["active"] == 1
    assert post(panel, "/panel/groups/toggle", {"code": "ИС-30"}).status_code == 303
    assert (await repo.get_group("ИС-30"))["active"] == 0


async def test_panel_ticket_status_notifies_student(panel, api):
    await register("100")
    await add_ticket()
    assert login(panel)
    api.sent.clear()
    assert post(panel, "/panel/tickets/1/status", {"status": "accepted"}).status_code == 303
    assert (await repo.get_ticket(1))["status"] == "accepted"
    assert any("Статус обращения" in text for _, text, _ in api.to("100"))


async def test_panel_broadcast_starts_with_right_audience(panel, monkeypatch):
    """Панель запускает рассылку в фоне: проверяем переданные данные, а не сам запуск."""
    await register("100")
    started = []

    async def fake_run(sender, audience, text):
        started.append((sender, audience, text))

    monkeypatch.setattr(webpanel, "run_broadcast", fake_run)
    assert login(panel)
    assert post(panel, "/panel/broadcasts/send", {"audience": "all", "text": "Проверка панели"}).status_code == 303
    await bot.asyncio.sleep(0)  # дать фоновой задаче стартовать
    assert started == [(SYS, "all", "Проверка панели")]


async def test_panel_broadcast_needs_text(panel, monkeypatch):
    started = []

    async def fake_run(sender, audience, text):
        started.append((sender, audience, text))

    monkeypatch.setattr(webpanel, "run_broadcast", fake_run)
    assert login(panel)
    assert post(panel, "/panel/broadcasts/send", {"audience": "all", "text": "  "}).status_code == 303
    assert not started and "Введите текст объявления" in panel.get("/panel/broadcasts").text


async def test_panel_settings_toggle(panel):
    assert login(panel)
    assert post(panel, "/panel/settings/tickets", {"enabled": "1"}).status_code == 303
    assert await db.get_setting("tickets_enabled") == "0"
    assert post(panel, "/panel/settings/welcome", {"value": "Здравствуйте"}).status_code == 303
    assert await db.get_setting("welcome_text") == "Здравствуйте"


async def test_panel_logs_and_test_actions(panel, api):
    assert login(panel)
    assert post(panel, "/panel/logs/test", {"action": "ping"}).status_code == 303
    assert "тестовая запись" in panel.get("/panel/logs").text
    assert post(panel, "/panel/logs/test", {"to": SYS}).status_code == 303
    assert any("Тестовое сообщение" in text for _, text, _ in api.to(SYS))


# ── реестр пользователей ──────────────────────────────────────────────────────
async def test_panel_people_lists_users_with_profile_links(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact("100", "ivanov_i", "Иван", "Здравствуйте")
    assert login(panel)
    body = panel.get("/panel/people").text
    assert "Иванов Иван Иванович" in body
    assert "https://max.ru/ivanov_i" in body and "ИС-21" in body

    filtered = panel.get("/panel/people", params={"kind": "student"}).text
    assert "Иванов" in filtered
    assert "Никого не найдено" in panel.get("/panel/people", params={"q": "мимо"}).text


async def test_panel_people_card_and_make_staff(panel, api):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact("100", "ivanov_i", "Иван", "Здравствуйте")
    assert login(panel)
    body = panel.get("/panel/people/100").text
    assert "Иванов Иван Иванович" in body and "https://max.ru/ivanov_i" in body

    api.sent.clear()
    assert post(panel, "/panel/access/make/100",
                {"full_name": "Иванов Иван Иванович", "position": "Преподаватель"}).status_code == 303
    a = await repo.get_admin("100")
    assert a["position"] == "Преподаватель" and a["full_name"] == "Иванов Иван Иванович"
    assert any("сотрудник" in text for _, text, _ in api.to("100"))


async def test_panel_people_csv_export(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact("100", "ivanov_i", "Иван", "Здравствуйте")
    assert login(panel)
    response = panel.get("/panel/people.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")  # BOM, чтобы Excel открыл кириллицу
    assert "Иванов Иван Иванович" in text and "https://max.ru/ivanov_i" in text
    assert "ИС-21" in text


def test_panel_people_requires_login(panel):
    for path in ("/panel/people", "/panel/people.csv", "/panel/people/100", "/panel/access"):
        assert panel.get(path, follow_redirects=False).status_code == 303, path


def test_api_people_and_requests(panel):
    assert login(panel)
    data = panel.get("/panel/api/people").json()
    assert data["count"] == 0
    requests = panel.get("/panel/api/requests").json()
    assert requests["requests"] == [] and requests["invites"] == [] and "attempts" in requests


async def test_panel_deletes_student_from_card(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact("100", "ivanov_i", "Иван", "Здравствуйте")
    assert login(panel)
    card = panel.get("/panel/people/100").text
    assert "Удалить пользователя" in card and "Удалить вместе с обращениями" in card

    assert post(panel, "/panel/people/100/delete").status_code == 303
    assert await db.one("SELECT 1 FROM users WHERE user_id='100'") is None
    assert await db.one("SELECT 1 FROM contacts WHERE user_id='100'") is None
    body = panel.get("/panel/people").text
    assert "Пользователь Иванов Иван Иванович удалён" in body
    assert "/panel/people/100" not in body  # больше не в реестре
    assert "ещё не писал боту" in panel.get("/panel/people/100").text


async def test_panel_delete_keeps_tickets_unless_asked(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_ticket()
    await db.run("UPDATE tickets SET status='completed' WHERE ticket_id=1")  # закрытое обращение
    assert login(panel)
    assert post(panel, "/panel/people/100/delete").status_code == 303
    assert (await repo.get_ticket(1))["student_id"] == "100"
    assert "обращения оставлены" in panel.get("/panel/people").text

    # отдельный студент с открытым обращением: удаляем вместе с обращением
    await register("101", "Петров Пётр Петрович", "ис-21")
    await repo.create_ticket("101", "200", "feedback", "Ещё вопрос")
    assert post(panel, "/panel/people/101/delete", {"with_tickets": "1"}).status_code == 303
    assert await db.one("SELECT 1 FROM tickets WHERE student_id='101'") is None
    assert await db.one("SELECT 1 FROM users WHERE user_id='101'") is None
    assert "удалено обращений: 1" in panel.get("/panel/people").text


async def test_panel_refuses_to_delete_user_with_open_tickets(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await add_ticket()  # обращение в статусе new
    assert login(panel)
    assert post(panel, "/panel/people/100/delete").status_code == 303
    body = panel.get("/panel/people/100").text
    assert "Удаление не выполнено" in body and "открытых обращений" in body
    assert await db.one("SELECT 1 FROM users WHERE user_id='100'") is not None
    assert (await repo.get_ticket(1))["student_id"] == "100"


async def test_panel_refuses_to_delete_sysadmin(panel):
    await repo.touch_contact(SYS, "admin_me", "Сис", "привет")
    assert login(panel)
    # у карточки сис-админа кнопки удаления нет вовсе
    assert "Удалить пользователя" not in panel.get(f"/panel/people/{SYS}").text

    assert post(panel, f"/panel/people/{SYS}/delete").status_code == 303
    body = panel.get(f"/panel/people/{SYS}").text
    assert "Удаление не выполнено" in body and "сис-админ" in body
    assert await repo.get_admin(SYS) is not None


def test_panel_delete_unknown_user_is_404(panel):
    assert login(panel)
    assert post(panel, "/panel/people/999/delete").status_code == 404


def test_panel_delete_requires_csrf(panel):
    assert login(panel)
    assert panel.post("/panel/people/100/delete", follow_redirects=False).status_code == 403


async def test_students_table_has_delete_button(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    assert login(panel)
    body = panel.get("/panel/students").text
    assert "/panel/people/100/delete" in body
    assert post(panel, "/panel/people/100/delete").status_code == 303
    assert "Студентов не найдено" in panel.get("/panel/students").text


async def test_panel_parses_schedule_and_shows_lessons(panel, monkeypatch, tmp_path):
    """Сохранение ссылки сразу разбирает PDF: сис-админ видит счётчик пар и ошибки."""
    from handlers import schedules
    from test_timetable import cyrillic_week

    monkeypatch.setattr(schedules, "download", fake_download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    monkeypatch.setattr(webpanel, "probe_pdf_url", fake_probe)

    assert login(panel)
    assert post(panel, "/panel/schedules/save",
                {"group_code": "ис-21", "pdf_url": "https://college.example/r.pdf"}).status_code == 303
    body = panel.get("/panel/schedules").text
    assert "разобрано: 3 пар" in body
    assert "3 пар" in body
    assert await repo.lessons_count("ИС-21") == 3

    card = panel.get("/panel/schedules/ИС-21").text
    assert "Понедельник" in card and "Математика" in card and "Иванова А. А." in card
    assert "08:00" in card


async def test_panel_shows_parse_error(panel, monkeypatch, tmp_path):
    from handlers import schedules

    async def broken(url):
        raise ValueError("404 Not Found")

    monkeypatch.setattr(schedules, "download", broken)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(webpanel, "probe_pdf_url", fake_probe)
    assert login(panel)
    assert post(panel, "/panel/schedules/save",
                {"group_code": "ИС-21", "pdf_url": "https://college.example/r.pdf"}).status_code == 303
    body = panel.get("/panel/schedules").text
    assert "не скачался PDF" in body
    assert "404" in body


async def test_panel_parses_one_group_and_all(panel, monkeypatch, tmp_path):
    from handlers import schedules
    from test_timetable import cyrillic_week

    monkeypatch.setattr(schedules, "download", fake_download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    monkeypatch.setattr(webpanel, "probe_pdf_url", fake_probe)
    for group in ("ИС-21", "ИС-22"):
        await repo.upsert_schedule(group, "https://college.example/r.pdf")
    assert login(panel)

    assert post(panel, "/panel/schedules/parse", {"group_code": "ИС-21"}).status_code == 303
    assert "разобрано 3 пар" in panel.get("/panel/schedules").text

    assert post(panel, "/panel/schedules/parse_all").status_code == 303
    assert "Обновлено расписаний: 2" in panel.get("/panel/schedules").text


async def test_panel_edits_lesson_time(panel, monkeypatch, tmp_path):
    from handlers import schedules
    from test_timetable import cyrillic_week

    monkeypatch.setattr(schedules, "download", fake_download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    await repo.upsert_schedule("ИС-21", "https://college.example/r.pdf")
    await schedules.parse_group("ИС-21")
    assert login(panel)

    assert post(panel, "/panel/schedules/lesson-time",
                {"group_code": "ИС-21", "weekday": "0", "lesson_num": "1",
                 "start": "08:30", "end": "09:15"}).status_code == 303
    assert "Время пары сохранено" in panel.get("/panel/schedules/ИС-21").text
    assert (await repo.lessons_for_group("ИС-21"))[0]["start"] == "08:30"

    assert post(panel, "/panel/schedules/lesson-time",
                {"group_code": "ИС-21", "weekday": "0", "lesson_num": "1",
                 "start": "bad", "end": "09:15"}).status_code == 303
    assert "в формате ЧЧ:ММ" in panel.get("/panel/schedules/ИС-21").text


async def test_panel_saves_lesson_times(panel, monkeypatch, tmp_path):
    from handlers import schedules
    from test_timetable import cyrillic_week

    monkeypatch.setattr(schedules, "download", fake_download)
    monkeypatch.setattr(schedules, "cache_folder", lambda: str(tmp_path / "schedules"))
    monkeypatch.setattr(schedules.tt, "extract_pages", lambda data, max_pages=14: cyrillic_week())
    await repo.upsert_schedule("ИС-21", "https://college.example/r.pdf")
    await schedules.parse_group("ИС-21")
    assert login(panel)

    # звонки задаются по номерам уроков, как их показывает бот
    assert post(panel, "/panel/schedules/times",
                {"t1a": "07:30", "t1b": "08:15", "t2a": "08:20", "t2b": "09:05"}).status_code == 303
    body = panel.get("/panel/schedules").text
    assert "Звонки сохранены" in body and "пересчитано у 3 занятий" in body
    times = await schedules.lesson_times()
    assert times[:2] == (("07:30", "08:15"), ("08:20", "09:05"))
    # время сразу пересчитано у уже разобранных занятий, PDF перечитывать не нужно
    assert (await repo.lessons_for_group("ИС-21"))[0]["start"] == "07:30"

    # можно задать звонки только для первого урока
    assert post(panel, "/panel/schedules/times", {"t1a": "06:30", "t1b": "07:15"}).status_code == 303
    assert (await schedules.lesson_times())[0] == ("06:30", "07:15")
    assert post(panel, "/panel/schedules/times", {"t1a": "", "t1b": ""}).status_code == 303
    assert "ни одного корректного времени" in panel.get("/panel/schedules").text


async def test_panel_schedule_card_without_parse(panel):
    await repo.upsert_schedule("ИС-21", "https://college.example/r.pdf")
    assert login(panel)
    body = panel.get("/panel/schedules/ИС-21").text
    assert "не разобрано" in body


# ── сис-админы в базе ─────────────────────────────────────────────────────────
async def test_panel_grants_and_revokes_sysadmin(panel, api):
    """Права выдаются и забираются из панели; отзыв переживает перезапуск бота."""
    assert login(panel)
    body = panel.get("/panel/staff").text
    assert "Сис-админы" in body and "ID 1" in body

    assert post(panel, "/panel/staff/sysadmin",
                {"user_id": "700", "full_name": "Новиков Пётр"}).status_code == 303
    assert (await repo.get_admin("700"))["role_type"] == "sysadmin"
    assert any("права сис-админа" in text.lower() for _, text, _ in api.to("700"))
    assert "Новиков Пётр" in panel.get("/panel/staff").text

    # забрать права
    api.sent.clear()
    assert post(panel, "/panel/staff/sysadmin/700/revoke").status_code == 303
    assert await repo.get_admin("700") is None
    assert "сняты" in "\n".join(text for _, text, _ in api.to("700")).lower()
    assert "Новиков Пётр" not in panel.get("/panel/staff").text

    # отзыв записан: перезапуск бота (.env) его не вернёт
    assert "700" in await repo.revoked_sysadmins()
    assert all(item["user_id"] != "700" for item in await repo.list_sysadmins())


async def test_panel_cannot_revoke_last_sysadmin(panel):
    assert await repo.sysadmin_count() == 1  # только сис-админ из SYSADMIN_IDS
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin/1/revoke").status_code == 303
    assert await repo.get_admin("1") is not None
    assert "последний сис-админ" in panel.get("/panel/staff").text


async def test_revoked_sysadmin_can_be_restored(panel):
    await repo.add_sysadmin("700")
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin/700/revoke").status_code == 303
    assert "700" in await repo.revoked_sysadmins()
    assert post(panel, "/panel/staff/sysadmin/700/restore").status_code == 303
    assert "700" not in await repo.revoked_sysadmins()
    assert (await repo.get_admin("700"))["role_type"] == "sysadmin"
    assert "снова сис-админ" in panel.get("/panel/staff").text


async def test_panel_rejects_bad_sysadmin_id(panel):
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin", {"user_id": "не-цифры"}).status_code == 303
    assert "только из цифр" in panel.get("/panel/staff").text
    assert await repo.sysadmin_count() == 1


async def test_env_sysadmin_is_not_revived_after_restart(panel):
    """Ключевая гарантия: снятый в панели сис-админ не возвращается при старте бота."""
    await repo.add_sysadmin("700")  # чтобы последним сис-админом остаться не 1
    assert login(panel)
    assert post(panel, "/panel/staff/sysadmin/1/revoke").status_code == 303
    assert await repo.get_admin("1") is None

    await db.init_db()  # эмулируем перезапуск: init_db импортирует SYSADMIN_IDS из .env
    assert await repo.get_admin("1") is None
    assert (await repo.get_admin("700"))["role_type"] == "sysadmin"
    assert await db.missing_objects() == []


# ── коды и заявки ─────────────────────────────────────────────────────────────
async def test_panel_creates_and_revokes_code(panel):
    assert login(panel)
    assert post(panel, "/panel/access/code", {"user_id": "500", "full_name": "Соколова"}).status_code == 303
    codes = [row["code"] for row in await repo.list_invites(10)]
    assert len(codes) == 1
    assert (await db.one("SELECT user_id FROM staff_invites WHERE code=?", (codes[0],)))["user_id"] == "500"

    body = panel.get("/panel/access").text
    assert codes[0] in body and "Соколова" in body
    assert post(panel, "/panel/access/code/delete", {"code": codes[0]}).status_code == 303
    assert await repo.list_invites(10) == []


async def test_panel_rejects_bad_id_in_code_form(panel):
    assert login(panel)
    assert post(panel, "/panel/access/code", {"user_id": "не-цифры"}).status_code == 303
    assert await repo.list_invites(10) == []
    assert "только из цифр" in panel.get("/panel/access").text


async def test_panel_approves_and_rejects_requests(panel, api):
    await repo.touch_contact("500", "sokolova", "Мария", "Здравствуйте")
    await repo.create_staff_request("500", "Соколова Мария", "Секретарь", "104", "приказ №12")
    assert login(panel)
    body = panel.get("/panel/access").text
    assert "Соколова Мария" in body and "Секретарь" in body

    api.sent.clear()
    assert post(panel, "/panel/access/request/500/ok").status_code == 303
    assert (await repo.get_admin("500"))["position"] == "Секретарь"
    assert (await repo.get_staff_request("500"))["status"] == "approved"
    assert any("одобрена" in text for _, text, _ in api.to("500"))

    await repo.create_staff_request("501", "Кузнецов Пётр", "Инженер")
    api.sent.clear()
    assert post(panel, "/panel/access/request/501/no").status_code == 303
    assert (await repo.get_staff_request("501"))["status"] == "rejected"
    assert await repo.get_admin("501") is None
    assert any("отклонена" in text.lower() for _, text, _ in api.to("501"))


# ── быстрый ответ в обращении ─────────────────────────────────────────────────
async def test_panel_quick_reply_notifies_student(panel, api):
    await register("100")
    await add_ticket()
    assert login(panel)
    api.sent.clear()
    assert post(panel, "/panel/tickets/1/reply", {"text": "Справка готовится"}).status_code == 303
    thread = await repo.ticket_thread(1, 10)
    assert [m["text"] for m in reversed(thread)] == ["Не открывается журнал", "Справка готовится"]
    assert (await repo.get_ticket(1))["status"] == "accepted"  # ответ принял обращение
    assert any("Справка готовится" in text for _, text, _ in api.to("100"))

    body = panel.get("/panel/tickets/1").text
    assert "Петрова Анна" in body and "История" in body and "Справка готовится" in body


async def test_panel_reply_needs_text(panel):
    await register("100")
    await add_ticket()
    assert login(panel)
    assert post(panel, "/panel/tickets/1/reply", {"text": "   "}).status_code == 303
    assert [m["text"] for m in reversed(await repo.ticket_thread(1, 10))] == ["Не открывается журнал"]


# ── защита форм и API ─────────────────────────────────────────────────────────
def test_post_without_csrf_is_rejected(panel):
    assert login(panel)
    response = panel.post("/panel/groups/add", data={"code": "ИС-40"}, follow_redirects=False)
    assert response.status_code == 403
    assert webpanel._flash == ""


async def test_csrf_token_differs_from_session_cookie(panel):
    """Токен формы не должен совпадать с cookie сессии: утечка cookie не даёт CSRF."""
    assert login(panel)
    cookie = panel.cookies.get(webpanel.COOKIE, "")
    token = csrf(panel)
    assert token and token != cookie
    assert f'name="csrf" value="{token}"' in panel.get("/panel/settings").text
    # с cookie вместо токена форма не принимается
    wrong = panel.post("/panel/settings/welcome", data={"value": "x", "csrf": cookie},
                       follow_redirects=False)
    assert wrong.status_code == 403
    assert post(panel, "/panel/settings/welcome", {"value": "Привет"}).status_code == 303
    assert await db.get_setting("welcome_text", "") == "Привет"


async def test_session_cookie_cannot_post_after_logout(panel):
    assert login(panel)
    token = csrf(panel)
    assert panel.get("/panel/logout", follow_redirects=False).status_code == 303
    after = panel.post("/panel/settings/welcome", data={"value": "x", "csrf": token},
                       follow_redirects=False)
    assert after.status_code == 303  # на вход: сессии больше нет
    assert await db.get_setting("welcome_text", "") != "x"


# ── журнал действий сис-админа ────────────────────────────────────────────────
async def test_actions_are_logged_and_shown(panel):
    await register("100", "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact("100", "ivanov_i", "Иван", "Здравствуйте")
    assert login(panel)

    assert post(panel, "/panel/access/make/100",
                {"full_name": "Иванов Иван Иванович", "position": "Преподаватель"}).status_code == 303
    assert post(panel, "/panel/groups/add",
                {"code": "ИС-30", "title": "ИС-30", "active": "1"}).status_code == 303

    body = panel.get("/panel/settings").text
    assert "Действия сис-админов" in body
    assert "сделать сотрудником" in body or "Права" in body or "Группа" in body
    assert "Иванов Иван Иванович" in body
    assert "За 30 дней" in body

    log_rows = await repo.admin_log(20)
    assert any("сотрудником" in item["action"] for item in log_rows)
    assert all(item["actor_id"] == SYS for item in log_rows)
    counts = await repo.admin_log_counts(30)
    assert sum(counts.values()) == len(log_rows)


def test_api_stats_and_health(panel):
    assert login(panel)
    stats = panel.get("/panel/api/stats").json()
    assert stats["overview"]["students"] == 0 and "statuses" in stats
    assert panel.get("/panel/api/logs").json()["file"] == config.LOG_FILE
    assert panel.get("/panel/api/health").json()["ok"] is True
    assert panel.get("/panel/api/health").json()["panel_enabled"] is True


# ── вспомогательное ───────────────────────────────────────────────────────────
async def add_ticket():
    """Обращение студента 100 к сотруднику 200 через бота."""
    await repo.add_staff("200", "Петрова Анна")
    await repo.create_ticket("100", "200", "feedback", "Не открывается журнал")
    return 1
