"""Досье человека на одной странице и правка сотрудника прямо в строке.

Проверки поведенческие: не «в разметке есть такая строка», а что страница
показывает, что она делает с базой и кому отказывает. Каждая проверка валится,
если убрать правило, которое она сторожит: счётчики без списка, фильтр без
отсечения, подтверждение без проверки, пустое поле с обнулением.

Числа для сверки берутся из атрибутов строк и счётчиков, а не из текста: иначе
подпись «Новое: 2» пришлось бы искать регуляркой по всей странице.
"""
import re

import pytest

import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, register

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel

SYS = "1"            # сис-админ из SYSADMIN_IDS (см. conftest)
STUDENT = "100"      # человек, о котором собираем досье
STAFF = "200"        # сотрудник, которого правим в строке
OTHER = "300"        # второй человек: его запись в журнале нам не показывают

DOSSIER = f"/panel/people/{STUDENT}/dossier"
QUICK = f"/panel/staff/{STAFF}/quick"

COUNTERS = re.compile(r'data-ds-status="([^"]+)" data-ds-count="(\d+)"')
ROWS = re.compile(r'data-ds-ticket="(\d+)" data-ds-state="([^"]+)"')


async def seed_person() -> tuple[int, int]:
    """Человек, о котором есть что показать: регистрация, обращения, переписка."""
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    await repo.touch_contact(STUDENT, "ivanov_i", "Иван", "Здравствуйте")
    await repo.set_schedule_subscription(STUDENT, "ис-21")
    await add_staff(STAFF, "Петрова Анна", position="Секретарь", office="204")
    await repo.set_staff_see_all(STAFF, True)
    first = await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка", "Справка")
    await repo.add_ticket_message(first, STAFF, "staff", "Готовим справку к пятнице")
    second = await repo.create_ticket(STUDENT, STAFF, "feedback", "Когда сдача сессии", "Сессия")
    await repo.set_ticket_status(second, "completed")
    await repo.log_action(SYS, "должность изменена", f"{STAFF}: Преподаватель → Секретарь")
    await repo.log_action(SYS, "обращение создано из панели", f"№1, студент {STUDENT}")
    # «№1000» - это не «№100»: граница цифр в журнале проверяется отдельно
    await repo.log_action(SYS, "обращение в архиве", "№1000")
    await repo.log_action(SYS, "сотрудник удалён", f"999 Никто {OTHER}")
    return first, second


def staff_row(body: str, name: str) -> str:
    """Одна строка таблицы сотрудников - вместе со всем, что в ней стоит."""
    return next(part for part in body.split("<tr") if name in part)


def tickets_block(body: str) -> str:
    """Кусок страницы от списка обращений до следующей карточки."""
    return body.split("Обращения:", 1)[1].split("</table>", 1)[0]


# ── что на странице ───────────────────────────────────────────────────────────
async def test_dossier_shows_the_whole_person_at_once(panel_client):
    """Личность, согласие, обращения, подписки и журнал - на одной странице."""
    await seed_person()
    assert login_panel(panel_client)
    response = panel_client.get(DOSSIER)
    assert response.status_code == 200
    body = response.text

    # кто он: ФИО, MAX ID, ник со ссылкой на профиль, группа, срок в боте
    assert "Иванов Иван Иванович" in body
    assert f"<code>{STUDENT}</code>" in body
    assert "https://max.ru/ivanov_i" in body
    assert "ИС-21" in body
    assert "в боте" in body
    # согласие: когда дал и на какой редакции текста
    assert "согласия нет" not in body and "согласие есть" in body
    assert "Редакция текста" in body and "1.0" in body
    # обращения
    assert "№1" in body and "№2" in body
    # подписки: группа стоит именно в своей карточке, а не где-то ещё на странице
    subs = body.split("Подписки на расписание", 1)[1].split("</div>", 1)[0]
    assert "ИС-21" in subs, "в карточке подписок нет группы"
    # история действий сис-админа именно с ним
    history = body.split("Действия сис-админа с человеком", 1)[1].split("</table>", 1)[0]
    assert "обращение создано из панели" in history
    assert STUDENT in history
    assert "в архиве" not in history, "подошла запись про №1000 - это не его обращение"
    assert f"999 Никто {OTHER}" not in history, "в досье попала чужая запись журнала"


async def test_dossier_shows_staff_rights_and_load(panel_client):
    """Сотрудник: роль, кабинет, раздел обращений, рассылка и нагрузка."""
    await seed_person()
    await repo.update_admin(STAFF, ticket_category="certificates", can_broadcast=1)
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/people/{STAFF}/dossier").text
    assert "Петрова Анна" in body
    rights = body.split("Права", 1)[1].split("</table>", 1)[0]
    assert "сотрудник" in rights
    assert "204" in rights                      # кабинет из карточки сотрудника
    assert "Справки" in rights                   # раздел обращений
    assert "Видит чужие обращения" in rights and "рассылка" in rights.lower()
    assert "Отпуск до" in rights
    assert "Нагрузка за 90 дней" in body


async def test_student_dossier_has_no_rights_block(panel_client):
    """У студента прав в боте нет: пустой таблицы «собаки не ждут» не показываем."""
    await seed_person()
    assert login_panel(panel_client)
    body = panel_client.get(DOSSIER).text
    assert "Нагрузка за 90 дней" not in body
    assert "Видит чужие обращения" not in body


# ── переписка на той же странице ──────────────────────────────────────────────
async def test_thread_opens_on_the_same_page_and_is_searchable(panel_client):
    """Переписка открывается номером обращения и ищется по тексту сообщений."""
    first, _ = await seed_person()
    assert login_panel(panel_client)
    body = panel_client.get(DOSSIER, params={"ticket": first}).text
    assert "Нужна справка" in body and "Готовим справку к пятнице" in body
    assert f'href="{DOSSIER}?ticket={first}"' in body   # переход без ухода со страницы

    found = panel_client.get(DOSSIER, params={"ticket": first, "q": "Готовим"}).text
    thread = found.split("Переписка по обращению", 1)[1].split("</table>", 1)[0]
    assert "Готовим справку к пятнице" in thread
    assert "Нужна справка" not in thread, "поиск нашёл то, чего в тексте нет"

    empty = panel_client.get(DOSSIER, params={"ticket": first, "q": "мимо"}).text
    assert "Сообщений с этим текстом нет" in empty


async def test_thread_of_a_stranger_ticket_is_not_opened(panel_client):
    """Чужое обращение по подставленному номеру не открывается."""
    first, _ = await seed_person()
    await register(OTHER, "Соколова Мария", "ис-22")
    alien = await repo.create_ticket(OTHER, STAFF, "feedback", "Чужое обращение", "Чужое")
    assert login_panel(panel_client)
    body = panel_client.get(DOSSIER, params={"ticket": alien}).text
    assert "Чужое обращение" not in body
    assert f"Переписка по обращению №{alien}" not in body
    # своё обращение при этом по-прежнему открывается
    assert "Нужна справка" in panel_client.get(DOSSIER, params={"ticket": first}).text


# ── счётчики и фильтр ─────────────────────────────────────────────────────────
async def test_status_counters_match_the_rows_of_the_list(panel_client):
    """Счётчики по статусам и список строк - одно и то же, а не две правды."""
    await seed_person()
    assert login_panel(panel_client)
    body = panel_client.get(DOSSIER).text
    counters = {code: int(number) for code, number in COUNTERS.findall(body)}
    rows = ROWS.findall(body)
    assert counters == {"new": 1, "completed": 1}
    assert sum(counters.values()) == len(rows) == 2
    for _ticket_id, state in rows:
        assert state in counters


async def test_status_filter_narrows_the_list(panel_client):
    """Выбранный статус оставляет в списке только его, счётчик сходится."""
    first, second = await seed_person()
    assert login_panel(panel_client)
    body = panel_client.get(DOSSIER, params={"status": "completed"}).text
    counters = {code: int(number) for code, number in COUNTERS.findall(body)}
    rows = ROWS.findall(body)
    assert [number for number, _state in rows] == [str(second)]
    assert counters["completed"] == len(rows) == 1
    assert f"№{first}" not in tickets_block(body)
    assert f"№{second}" in tickets_block(body)

    missing = panel_client.get(DOSSIER, params={"status": "rejected"}).text
    assert "Обращений с таким статусом нет" in missing


# ── отказы ────────────────────────────────────────────────────────────────────
async def test_unknown_person_gets_an_honest_page(panel_client):
    """Нет такого человека - честная страница, а не 500 и не пустота."""
    await seed_person()
    assert login_panel(panel_client)
    response = panel_client.get("/panel/people/777777/dossier")
    assert response.status_code == 200
    assert "Internal Server Error" not in response.text
    assert "Такого человека бот не знает" in response.text
    assert "/panel/people" in response.text


async def test_dossier_is_open_to_sysadmin_and_closed_to_the_rest(panel_client):
    """Сессия студента или постороннего есть, а прав сис-админа нет - 403."""
    from web.common import COOKIE, start_session

    await seed_person()
    assert login_panel(panel_client)
    assert panel_client.get(DOSSIER).status_code == 200

    panel_client.cookies.set(COOKIE, start_session(STUDENT))     # студент, не сис-админ
    assert panel_client.get(DOSSIER).status_code == 403
    assert panel_client.get(f"/panel/people/{STAFF}/dossier").status_code == 403
    assert panel_client.post(QUICK, data={"position": "Взлом"}).status_code == 403
    assert (await repo.get_admin(STAFF))["office"] == "204"

    panel_client.cookies.set(COOKIE, start_session("777777"))     # посторонний
    assert panel_client.get(DOSSIER).status_code == 403


def test_dossier_is_closed_without_login(panel_client):
    """Без входа панель отвечает переадресацией на вход, а не содержимым."""
    response = panel_client.get(DOSSIER, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["Location"] == "/panel/login"


# ── правка сотрудника прямо в строке ──────────────────────────────────────────
async def test_row_edit_form_lives_in_the_staff_row(panel_client):
    """Правка стоит в строке таблицы: в карточку заходить не нужно."""
    await seed_person()
    assert login_panel(panel_client)
    row = staff_row(panel_client.get("/panel/staff").text, "Петрова Анна")
    assert f'action="{QUICK}"' in row
    assert 'name="position"' in row and 'name="office"' in row
    assert 'name="ticket_category"' in row
    assert "csrf" in row


async def test_row_edit_saves_position_and_office_and_writes_log(panel_client):
    """Должность и кабинет сохраняются, а действие попадает в журнал."""
    await seed_person()
    assert login_panel(panel_client)
    response = post_form(panel_client, QUICK,
                         {"position": "  ПК ", "office": "101", "ticket_category": "all"})
    assert response.status_code == 303
    admin = await repo.get_admin(STAFF)
    assert admin["position"] == "Приёмная комиссия"      # синоним свёлся к справочнику
    assert admin["office"] == "101"
    assert admin["ticket_category"] == "all"             # тот же раздел - не менялся
    entries = [f"{row['action']}: {row['details']}" for row in await repo.admin_log(50)]
    assert any(f"{STAFF}" in item and "должность" in item for item in entries)


async def test_row_edit_requires_csrf(panel_client):
    """Без токена формы правка не проходит и база не меняется."""
    await seed_person()
    assert login_panel(panel_client)
    response = panel_client.post(QUICK, data={"position": "Взлом", "office": "999"},
                                 follow_redirects=False)
    assert response.status_code == 403
    admin = await repo.get_admin(STAFF)
    assert admin["office"] == "204" and "Взлом" not in admin["position"]


async def test_empty_field_means_do_not_touch(panel_client):
    """Пустое поле - «не трогать», а не «стереть»: очищенный input не забирает кабинет."""
    await seed_person()
    assert login_panel(panel_client)
    post_form(panel_client, QUICK, {"position": "", "office": "", "ticket_category": "all"})
    admin = await repo.get_admin(STAFF)
    assert admin["position"] == "Секретарь" and admin["office"] == "204"
    assert "не трогать" in panel_client.get("/panel/staff").text


async def test_category_change_asks_for_confirmation(panel_client):
    """Раздел обращений не меняется молча: сначала вопрос, потом подтверждение."""
    await seed_person()
    assert login_panel(panel_client)
    assert (await repo.get_admin(STAFF))["ticket_category"] == "all"

    asked = post_form(panel_client, QUICK, {"ticket_category": "certificates"})
    assert asked.status_code == 303
    assert (await repo.get_admin(STAFF))["ticket_category"] == "all", \
        "раздел сменился без подтверждения"
    location = asked.headers["Location"]
    assert f"cat_ask={STAFF}" in location
    body = panel_client.get(location).text
    assert "Раздел обращений изменится" in body
    assert "Подтвердить смену раздела" in staff_row(body, "Петрова Анна")

    done = post_form(panel_client, QUICK, {"confirm": "1", "ticket_category": "certificates"})
    assert done.status_code == 303
    assert (await repo.get_admin(STAFF))["ticket_category"] == "certificates"
    entries = [f"{row['action']}: {row['details']}" for row in await repo.admin_log(50)]
    assert any("раздел обращений" in item for item in entries)


async def test_category_change_can_be_cancelled(panel_client):
    """Отмена возвращает к тому же: раздел остался прежним, вопрос снят."""
    await seed_person()
    assert login_panel(panel_client)
    post_form(panel_client, QUICK, {"ticket_category": "certificates"})
    post_form(panel_client, QUICK, {"cancel": "1"})
    assert (await repo.get_admin(STAFF))["ticket_category"] == "all"
    assert "Раздел обращений изменится" not in panel_client.get("/panel/staff").text


async def test_cancelling_says_so_instead_of_pretending_nothing_happened(panel_client):
    """Отмена - тоже ответ: сис-админ должен видеть, что нажал."""
    await seed_person()
    assert login_panel(panel_client)
    post_form(panel_client, QUICK, {"cancel": "1"})
    assert "отменена" in panel_client.get("/panel/staff").text


# ── скорость и приватность ────────────────────────────────────────────────────
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


async def test_dossier_page_queries_do_not_grow_with_the_person(panel_client, monkeypatch):
    """Досье собирается одним вызовом: число запросов не зависит от объёма жизни.

    Из счёта выпадает то, что есть на любой странице панели: проверка прав при
    входе и шесть запросов бейджей меню. На само досье остаётся четыре (человек,
    обращения, журнал, переписка) и пять, если человек - сотрудник: добавляется
    его нагрузка.
    """
    import web.dossier

    first, _ = await seed_person()
    assert login_panel(panel_client)
    calls: list[str] = []
    original = web.dossier.person_dossier

    async def counted(user_id, *args, **kwargs):
        calls.append(user_id)
        return await original(user_id, *args, **kwargs)

    monkeypatch.setattr(web.dossier, "person_dossier", counted)
    seen = count_queries(monkeypatch)
    assert panel_client.get(DOSSIER, params={"ticket": first}).status_code == 200
    small = len(seen)
    assert small <= 12, f"досье делает {small} запросов - это уже не один вызов"
    assert calls == [STUDENT], "шаблон страницы собрал данные сам, а не одним вызовом"

    for index in range(3, 30):
        await repo.create_ticket(STUDENT, STAFF, "feedback", f"вопрос {index}", f"Тема {index}")
    seen = count_queries(monkeypatch)
    assert panel_client.get(DOSSIER, params={"ticket": first}).status_code == 200
    assert len(seen) <= small, f"запросов стало {small} -> {len(seen)} при росте обращений"
    assert calls == [STUDENT, STUDENT]


async def test_dossier_keeps_correspondence_out_of_the_admin_log(panel_client):
    """Содержимое переписки в журнал действий не попадает - ни при чтении, ни при правке."""
    first, _ = await seed_person()
    assert login_panel(panel_client)
    panel_client.get(DOSSIER, params={"ticket": first, "q": "Готовим"})
    # и правка в строке: журнал пишется здесь же, и в него нечего положить лишнего
    post_form(panel_client, QUICK, {"position": "Учебная часть", "office": "207",
                                    "ticket_category": "all"})
    joined = " ".join(f"{row['action']} {row['details']}" for row in await repo.admin_log(200))
    assert "Готовим справку к пятнице" not in joined
    assert "Нужна справка" not in joined
    assert "Когда сдача сессии" not in joined
    # а что правка действительно записала - записано. Именами полей, а не их
    # значениями: должность и кабинет в журнале не нужны, а по названию поля
    # видно, что именно кто-то поменял.
    assert "сотрудник изменён в строке" in joined
    assert "должность" in joined and "кабинет" in joined
