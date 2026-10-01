"""Карточка студента: все сведения о студенте на одной странице.

Проверки поведенческие: не «в разметке есть такая строка», а что страница
показывает, чему отказывает и сколько стоит. Каждая валится, если убрать
правило, которое она сторожит: экранирование текста из базы, проверку прав,
переадресацию без входа, разделение подписки и разбора расписания, а также
независимость числа запросов от объёма жизни человека.

Моменты времени в базу пишем свои и сверяем по замороженным часам
(``frozen_college_clock``): иначе «сегодня 14:32» от прогона к прогону менялось
бы, и проверка даты проверяла бы не дату.
"""
import re
from urllib.parse import quote

import pytest

import clock
import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, register

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

STUDENT = "100"      # студент, карточку которого смотрим
OTHER = "300"        # второй человек: его обращения в карточку попасть не должны
STAFF = "200"        # сотрудник, к которому уходят обращения
CARD = f"/panel/students/{STUDENT}"
GROUP = "ИС-21"
GROUP_URL = quote(GROUP)

# Моменты времени, которые мы сами пишем в базу, и то, как они должны выглядеть
# на странице. Все они старше недели до frozen_college_clock, поэтому fmt_when
# отдаёт их полной датой, а не «чт 09:00».
REGISTERED = ("2026-01-08 08:30:00", "08.01.2026 08:30")
FIRST_SEEN = ("2026-01-05 12:00:00", "05.01.2026 12:00")
DAYS_IN_BOT = "267 дней в боте"
# обращение -> (создано в базе, создано на странице, движение на странице)
TICKET_DATES = {
    1: ("2026-01-10 09:00:00", "10.01.2026 09:00", "11.01.2026 10:00"),
    2: ("2026-01-12 09:00:00", "12.01.2026 09:00", "13.01.2026 10:00"),
}
UPDATED = {1: "2026-01-11 10:00:00", 2: "2026-01-13 10:00:00"}

# Точные плашки темы: по слову «подписан» не отличить «подписан» от
# «не подписан», а проверка должна ловить именно эту разницу.
ON, OFF = "pill pill-on", "pill pill-off"
PARSED = f'<span class="{ON}">разобрано</span>'
FAILED = f'<span class="{OFF}">ошибка разбора</span>'
SUBSCRIBED = f'<span class="{ON}">подписан</span>'
NOT_SUBSCRIBED = f'<span class="{OFF}">не подписан</span>'
pill_off = f'<span class="{OFF}">'

ROWS = re.compile(r'data-sc-ticket="(\d+)" data-sc-state="([^"]+)"')


async def seed_student(subscribe: str = GROUP, group: str = "ис-21") -> tuple[int, int]:
    """Студент, о котором есть что показать: регистрация, обращения, подписка."""
    await register(STUDENT, "Иванов Иван Иванович", group)
    await repo.touch_contact(STUDENT, "ivanov_i", "Иван", "Здравствуйте")
    await db.run("UPDATE users SET created_at=? WHERE user_id=?", (REGISTERED[0], STUDENT))
    await db.run("UPDATE contacts SET first_seen=?, last_seen=? WHERE user_id=?",
                 (FIRST_SEEN[0], "2026-01-12 18:40:00", STUDENT))
    if subscribe:
        await repo.set_schedule_subscription(STUDENT, subscribe)
    await add_staff(STAFF, "Петрова Анна", position="Секретарь", office="204")
    first = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    second = await repo.create_ticket(STUDENT, STAFF, "academic", "Когда сдача сессии", "Сессия")
    await repo.set_ticket_status(second, "completed")
    for index, ticket_id in enumerate((first, second), start=1):
        await db.run("UPDATE tickets SET created_at=?, updated_at=? WHERE ticket_id=?",
                     (TICKET_DATES[index][0], UPDATED[index], ticket_id))
    return first, second


async def seed_parsed_schedule(lessons: int = 3, error: str = "") -> None:
    """Расписание группы, как оно лежит в базе после разбора PDF сис-админом."""
    await repo.upsert_schedule(GROUP, "https://collegelan.ru/rasp/24-21.pdf")
    await db.run(
        "UPDATE schedules SET parsed_at=?, parsed_hash=?, found_groups=?, parse_error=? "
        "WHERE group_code=?", (clock.stamp(), "hash-123", "24-21,24-23", error, GROUP))
    for number in range(lessons):
        await db.run("INSERT INTO lessons(group_code, weekday, lesson_num, subject) "
                     "VALUES(?,?,?,?)", (GROUP, 0, number + 1, "Математика"))


def block(body: str, anchor: str) -> str:
    """Кусок страницы от заданной подписи до конца её таблицы.

    Якорь - уникальная подпись строки или заголовка, а не название раздела:
    «Расписание» есть и в меню, и в карточке, и split по нему разрезал бы
    страницу по меню, а не по карточке.
    """
    assert anchor in body, f"на странице нет «{anchor}»"
    return body[body.index(anchor):].split("</table>", 1)[0]


# ── что на карточке ───────────────────────────────────────────────────────────
async def test_card_shows_the_whole_student_at_once(panel_client, frozen_college_clock):
    """ФИО, MAX ID, группа, дата регистрации и срок в боте - на одной странице."""
    await seed_student()
    assert login_panel(panel_client)
    response = panel_client.get(CARD)
    assert response.status_code == 200
    body = response.text

    assert "Иванов Иван Иванович" in body
    assert f"<code>{STUDENT}</code>" in body
    assert f'data-copy="{STUDENT}"' in body
    head = block(body, "Кто это")
    assert GROUP in head
    assert REGISTERED[1] in head, "дата регистрации показана не из базы"
    assert FIRST_SEEN[1] in head and DAYS_IN_BOT in head, "срок в боте не из базы"
    assert "https://max.ru/ivanov_i" in body
    # ссылка на карточку копируется целиком, а не только MAX ID
    assert f"/panel/students/{STUDENT}" in body


async def test_card_lists_every_ticket_with_status_and_date(panel_client, frozen_college_clock):
    """Все обращения студента списком: номер, статус, раздел, тема и даты."""
    first, second = await seed_student()
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text

    rows = ROWS.findall(body)
    assert [ticket for ticket, _state in rows] == [str(second), str(first)]
    assert dict(rows) == {str(first): "new", str(second): "completed"}
    tickets = block(body, "Обращения:")
    for index, ticket_id in enumerate((first, second), start=1):
        created, created_human, moved_human = TICKET_DATES[index]
        assert f"№{ticket_id}" in tickets
        assert created_human in tickets, f"нет даты создания обращения {ticket_id}"
        assert moved_human in tickets, f"нет даты движения по обращению {ticket_id}"
    assert "Справка" in tickets and "Сессия" in tickets
    assert "Обратная связь" in tickets and "Учебные вопросы" in tickets
    assert "Новое" in tickets and "Завершено" in tickets


async def test_card_marks_archived_ticket_and_names_the_staff(panel_client):
    """Архивное обращение помечено, а ведущего видно по имени, а не по его ID."""
    first, _second = await seed_student()
    await repo.archive_ticket(first, actor_id="1")
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    tickets = block(body, "Обращения:")
    assert f'{pill_off}в архиве</span>' in tickets
    assert "Петрова Анна" in tickets, "ведущего обращения видно только по его ID"


async def test_card_does_not_show_a_stranger_tickets(panel_client):
    """Чужое обращение в карточку не попадает, даже если его номер известен."""
    first, second = await seed_student()
    await register(OTHER, "Соколова Мария", "ис-22")
    await repo.create_ticket(OTHER, STAFF, "feedback", "Чужое обращение", "Чужое")
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    assert "Чужое обращение" not in body
    assert [ticket for ticket, _ in ROWS.findall(body)] == [str(second), str(first)]


# ── расписание: подписка и разбор - разные вещи ───────────────────────────────
async def test_card_says_subscription_and_parse_separately(panel_client):
    """Видно и то, что студент подписан, и то, разобрано ли расписание группы."""
    await seed_student()
    await seed_parsed_schedule()
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    schedule = block(body, "Подписка на свою группу")

    assert SUBSCRIBED in schedule
    assert "Подписан на группы" in schedule and GROUP in schedule
    assert "Ссылка на PDF" in schedule and "24-21.pdf" in schedule
    assert PARSED in schedule and "3 пар" in schedule
    assert "Группы в самом PDF" in schedule and "24-23" in schedule
    # из карточки видно и саму группу, и её пары: обе ссылки ведут в расписания
    assert f'href="/panel/schedules/{GROUP_URL}"' in body
    assert f"Пары группы {GROUP}" in body


async def test_subscription_to_another_group_is_not_own_subscription(panel_client):
    """Подписка на чужую группу не выдаётся за подписку на свою."""
    await seed_student(subscribe="ИС-22")
    assert login_panel(panel_client)
    schedule = block(panel_client.get(CARD).text, "Подписка на свою группу")
    assert NOT_SUBSCRIBED in schedule, "подписка на ИС-22 показана как своя"
    assert SUBSCRIBED not in schedule
    assert "ИС-22" in schedule, "не сказано, на что человек подписан на самом деле"


async def test_card_reports_schedule_that_was_never_parsed(panel_client):
    """Нет ссылки на PDF - так и написано, а не молчание вместо ответа."""
    await seed_student()
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    schedule = block(body, "Подписка на свою группу")
    assert "ссылки на PDF нет" in schedule
    assert PARSED not in schedule
    assert "не разобрано" not in schedule


async def test_card_reports_schedule_link_without_parsed_lessons(panel_client):
    """Ссылка есть, а разбора не было: это разные состояния, и видно оба."""
    await seed_student()
    await repo.upsert_schedule(GROUP, "https://collegelan.ru/rasp/24-21.pdf")
    assert login_panel(panel_client)
    schedule = block(panel_client.get(CARD).text, "Подписка на свою группу")
    assert "24-21.pdf" in schedule
    assert "не разобрано" in schedule
    assert PARSED not in schedule


async def test_card_reports_a_failed_parse(panel_client):
    """Ошибка разбора показывается как ошибка, а не как «не разобрано»."""
    await seed_student()
    await seed_parsed_schedule(lessons=0, error="Файл не похож на расписание")
    assert login_panel(panel_client)
    schedule = block(panel_client.get(CARD).text, "Подписка на свою группу")
    assert FAILED in schedule
    assert "Файл не похож на расписание" in schedule
    assert PARSED not in schedule


async def test_freshness_of_the_parse_is_told_honestly(panel_client, monkeypatch):
    """Разбор старше SCHEDULE_CACHE_HOURS не выдаётся за свежий."""
    await seed_student()
    await seed_parsed_schedule()
    assert login_panel(panel_client)
    fresh = block(panel_client.get(CARD).text, "Подписка на свою группу")
    assert PARSED in fresh and "актуально" in fresh

    monkeypatch.setattr(config, "SCHEDULE_CACHE_HOURS", 1)
    await db.run("UPDATE schedules SET parsed_at=? WHERE group_code=?",
                 (clock.stamp_at(-60), GROUP))
    stale = block(panel_client.get(CARD).text, "Подписка на свою группу")
    assert PARSED in stale, "разбор исчез из карточки только из-за возраста"
    assert "актуально" not in stale
    assert "старше 1 ч" in stale


# ── согласие ──────────────────────────────────────────────────────────────────
async def test_card_shows_consent(panel_client):
    """Согласие видно вместе с датой и редакцией текста."""
    await seed_student()
    assert login_panel(panel_client)
    consent = block(panel_client.get(CARD).text, "Согласие на обработку данных")
    assert "согласие есть" in consent
    assert "1.0" in consent
    assert "Когда дал" in consent


async def test_card_says_when_consent_was_never_given(panel_client):
    """Нет отметки - написано «согласия нет», а не тихо."""
    await seed_student()
    await db.run("UPDATE users SET consent_at='', consent_version='' WHERE user_id=?", (STUDENT,))
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    assert "согласия нет" in body
    assert "согласие есть" not in body
    # отдельной учебной подписки в базе нет, и карточка говорит об этом прямо
    assert "учебной подписки в базе не заведено" in body


# ── студент без группы и человек с правами ────────────────────────────────────
async def test_card_copes_with_a_student_without_group(panel_client):
    """Группы нет: карточка объясняет это, а не падает и не врёт про подписку."""
    await seed_student(subscribe="")
    await db.run("UPDATE users SET group_code='' WHERE user_id=?", (STUDENT,))
    assert login_panel(panel_client)
    response = panel_client.get(CARD)
    assert response.status_code == 200 and "Internal Server Error" not in response.text
    body = response.text
    schedule = block(body, "Подписка на свою группу")
    assert "группа не указана" in schedule
    assert NOT_SUBSCRIBED in schedule
    assert f'href="/panel/schedules/{GROUP_URL}"' not in body


async def test_card_says_that_the_person_has_rights_in_the_bot(panel_client):
    """Сотрудник, попавший в список студентов, не выглядит как обычный студент."""
    await seed_student()
    await add_staff(STUDENT, "Иванов Иван Иванович")
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    assert "права в боте" in body
    assert f'href="/panel/people/{STUDENT}/dossier"' in body


# ── отказы ────────────────────────────────────────────────────────────────────
async def test_unknown_student_gets_an_honest_page(panel_client):
    """Нет такого человека - честная страница, а не 500 и не пустота."""
    await seed_student()
    assert login_panel(panel_client)
    response = panel_client.get("/panel/students/777777")
    assert response.status_code == 200
    assert "Internal Server Error" not in response.text
    assert "Такого человека бот не знает" in response.text
    assert "/panel/students" in response.text


def test_card_is_closed_without_login(panel_client):
    """Без входа панель отвечает переадресацией на вход, а не содержимым."""
    response = panel_client.get(CARD, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["Location"] == "/panel/login"


async def test_card_is_open_to_sysadmin_and_closed_to_the_rest(panel_client):
    """Сессия студента или постороннего есть, а прав сис-админа нет - 403."""
    from web.common import COOKIE, start_session

    await seed_student()
    assert login_panel(panel_client)
    assert panel_client.get(CARD).status_code == 200

    panel_client.cookies.set(COOKIE, start_session(STUDENT))     # студент, не сис-админ
    assert panel_client.get(CARD).status_code == 403

    panel_client.cookies.set(COOKIE, start_session("777777"))     # посторонний
    assert panel_client.get(CARD).status_code == 403


# ── безопасность и цена страницы ──────────────────────────────────────────────
async def test_card_escapes_text_taken_from_the_database(panel_client):
    """ФИО, тема и код группы из базы экранируются: разметка не просачивается."""
    await seed_student()
    await db.run("UPDATE users SET full_name=?, group_code=? WHERE user_id=?",
                 ("<script>alert(1)</script>", "<b>ИС-21</b>", STUDENT))
    await db.run("UPDATE tickets SET topic=? WHERE student_id=?", ("<img onerror=1>", STUDENT))
    assert login_panel(panel_client)
    body = panel_client.get(CARD).text
    assert "<script>alert(1)</script>" not in body
    assert "<img onerror=1>" not in body
    assert "<b>ИС-21</b>" not in body
    assert "&lt;script&gt;" in body


def count_queries(monkeypatch) -> list:
    """Считает запросы к базе вместо того, чтобы их делать."""
    seen: list[str] = []
    real_many, real_one = db.many, db.one

    async def many(sql, params=()):
        seen.append(" ".join(str(sql).split()))
        return await real_many(sql, params)

    async def one(sql, params=()):
        seen.append(" ".join(str(sql).split()))
        return await real_one(sql, params)

    monkeypatch.setattr(db, "many", many)
    monkeypatch.setattr(db, "one", one)
    return seen


async def test_card_page_queries_do_not_grow_with_the_student(panel_client, monkeypatch):
    """Карточка собирается в слое данных, а не запросами из шаблона.

    Из счёта выпадает то, что есть на любой странице панели: проверка прав при
    входе и запросы бейджей меню. На саму карточку остаётся горстка: человек,
    обращения, журнал, ссылка на PDF и число занятий - и она не растёт, когда
    обращений у человека становится больше.
    """
    import web.student_card

    await seed_student()
    await seed_parsed_schedule()
    assert login_panel(panel_client)
    calls: list[str] = []
    original = web.student_card.person_dossier

    async def counted(user_id, *args, **kwargs):
        calls.append(user_id)
        return await original(user_id, *args, **kwargs)

    monkeypatch.setattr(web.student_card, "person_dossier", counted)
    seen = count_queries(monkeypatch)
    assert panel_client.get(CARD).status_code == 200
    small = len(seen)
    assert small <= 12, f"карточка делает {small} запросов - это уже не сборка в store"
    assert calls == [STUDENT], "шаблон страницы собрал данные сам, а не одним вызовом"

    for index in range(3, 30):
        await repo.create_ticket(STUDENT, STAFF, "feedback", f"вопрос {index}", f"Тема {index}")
    seen = count_queries(monkeypatch)
    assert panel_client.get(CARD).status_code == 200
    assert len(seen) <= small, f"запросов стало {small} -> {len(seen)} при росте обращений"
    assert calls == [STUDENT, STUDENT]
