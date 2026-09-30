"""Ответ за сотрудника: то, ради чего устроен раздел «Тест».

Пользованик просил: завести тестовых сотрудников и отвечать на сайте за них.
Смысл стенда в том, что ответ уходит ОТ ИМЕНИ сотрудника, а не от того, кто
сидит в панели. Без этого тестовый сотрудник бессмысленен - нарисовать его
можно, а ответить от его имени нельзя.

Проверяем сквозной путь: тестовый сотрудник заведён -> студент пишет ему
обращение -> сис-админ в панели выбирает «Отвечает как: этот сотрудник» ->
в переписке автором становится тестовый сотрудник -> студенту в MAX уходит
уведомление, где назван ответивший, и тема обращения.
"""
import pytest

import config
import database as db
import repository as repo
from conftest import add_staff, login_panel, post_form, register
from store import staff as staff_repo
from web.tickets import _answerer_label, _ticket_subject

pytestmark = pytest.mark.panel

OWNER = "46010397"
STUDENT, LIVE_STAFF = "100", "200"
TEST_NAME = "Тестовый сотрудник"
TEST_POSITION = "Секретарь"
TEST_OFFICE = "214"


@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


@pytest.fixture
async def ticket_to_test_staff(env):
    """Студент пишет обращение тестовому сотруднику - как это делает телефон."""
    await register(STUDENT, "Иванов Иван Иванович", "ис-21")
    test_id = await staff_repo.add_test_staff(TEST_NAME, TEST_POSITION,
                                             "учебная часть", f"каб. {TEST_OFFICE}")
    await add_staff(LIVE_STAFF, "Петрова Анна Сергеевна", category="all",
                    position="Методист", office="215")
    tid = await repo.create_ticket(STUDENT, test_id, "feedback", "Нужна справка",
                                   topic="Справка для поступления")
    return tid, test_id


def last_sent(api, user: str) -> str:
    # Что ушло студенту. За сообщениями следит фикстура api из conftest,
    # а не панель: TestClient сообщения не хранит.
    return api.last(user)[1]


# ── выбор в форме ──────────────────────────────────────────────────────────
async def test_reply_form_offers_to_answer_as_someone(owner_client, ticket_to_test_staff):
    """В форме ответа есть выбор «Отвечает как» и там есть тестовый."""
    tid, test_id = ticket_to_test_staff
    body = owner_client.get(f"/panel/tickets?t={tid}").text
    assert 'name="as_staff"' in body, "в форме ответа нет выбора отвечающего"
    assert "Отвечает как" in body
    assert f'value="{test_id}"' in body, "тестового сотрудника нет в выборе"
    assert TEST_NAME in body, "тестового нет в списке под его именем"


async def test_form_says_answer_goes_from_the_staff_name(owner_client, ticket_to_test_staff):
    """Подсказка объясняет, что ответ уходит от имени сотрудника."""
    tid, _test_id = ticket_to_test_staff
    body = owner_client.get(f"/panel/tickets?t={tid}").text
    assert "ОТ ИМЕНИ" in body, "подсказка не говорит, от чьего имени ответ"


# ── ответ за тестового ─────────────────────────────────────────────────────
async def test_answer_is_recorded_from_the_test_staff(owner_client, ticket_to_test_staff):
    """Главное: автор сообщения - тестовый сотрудник, а не вошедший в панель."""
    tid, test_id = ticket_to_test_staff
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Добрый день! Справку собираем, будет готова завтра.",
        "template": "", "as_staff": test_id,
    })
    thread = await repo.ticket_thread(tid, 10)
    last = thread[0]
    assert last["sender_id"] == test_id, "автором стал не тестовый сотрудник"
    assert last["sender_name"] == TEST_NAME
    assert last["position"] == TEST_POSITION
    assert last["sender_role"] == "staff"


async def test_student_is_told_who_answered_and_about_what(owner_client, api, ticket_to_test_staff):
    """Студент получает тему обращения и имя ответившего - не голый номер."""
    tid, test_id = ticket_to_test_staff
    # сначала ответ - без него api.last покажет приветствие, а не ответ
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Добрый день! Справку собираем, будет готова завтра.",
        "template": "", "as_staff": test_id,
    })
    sent = last_sent(api, STUDENT)
    assert f"обращению №{tid}" in sent, sent
    assert "Справка для поступления" in sent, f"в уведомлении нет темы: {sent}"
    assert TEST_NAME in sent, f"в уведомлении нет имени ответившего: {sent}"
    # и сам текст ответа не потерялся
    assert "будет готова завтра" in sent


async def test_journal_records_that_someone_answered_instead(owner_client, ticket_to_test_staff):
    """В журнале - кто нажал и за кого, без текста ответа."""
    tid, test_id = ticket_to_test_staff
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Ответ, который не должен попасть в журнал панели.",
        "template": "", "as_staff": test_id,
    })
    rows = await db.many("SELECT actor_id, action, details FROM admin_log "
                         "ORDER BY id DESC LIMIT 10")
    entry = next((r for r in rows if r["action"] == "ответ за сотрудника"), None)
    assert entry is not None, "в журнале нет записи, что ответили за сотрудника"
    assert entry["actor_id"] == OWNER
    assert test_id in entry["details"]
    assert "не должен попасть" not in entry["details"], "текст ответа попал в журнал"


# ── умолчания и отказ от лишнего ──────────────────────────────────────────
async def test_without_choice_answers_the_operator(owner_client, ticket_to_test_staff):
    """Без явного выбора отвечает вошедший - как раньше."""
    tid, _test_id = ticket_to_test_staff
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Ответ без выбора отвечающего.", "template": "", "as_staff": "",
    })
    last = (await repo.ticket_thread(tid, 10))[0]
    assert last["sender_id"] == OWNER


async def test_deleted_answerer_falls_back_to_the_assignee(owner_client, api, ticket_to_test_staff):
    """Выбранного удалили - ответ не теряется, а уходит от исполнителя."""
    tid, test_id = ticket_to_test_staff
    await db.run("DELETE FROM admins WHERE user_id=?", (test_id,))
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Ответ после удаления сотрудника.", "template": "", "as_staff": test_id,
    })
    thread = await repo.ticket_thread(tid, 10)
    assert "Ответ после удаления сотрудника." in thread[0]["text"]
    assert "Ответ после удаления сотрудника." in last_sent(api, STUDENT)


async def test_no_extra_journal_entry_when_answering_as_self(owner_client, ticket_to_test_staff):
    """Отвечал за себя - лишней записи в журнале не надо."""
    tid, _test_id = ticket_to_test_staff
    post_form(owner_client, f"/panel/tickets/{tid}/reply", {
        "text": "Ответ за себя.", "template": "", "as_staff": OWNER,
    })
    rows = await db.many("SELECT action FROM admin_log WHERE action='ответ за сотрудника'")
    assert not rows, "ответ за себя не должен попадать в журнал как ответ за кого-то"


# ── сами помощники ────────────────────────────────────────────────────────
def test_subject_falls_back_to_the_category():
    """Без темы обращения в уведомлении идёт название раздела, а не пустота."""
    assert _ticket_subject({"topic": "Справка", "category": "feedback"}) == "Справка"
    fallback = _ticket_subject({"topic": "", "category": "certificates"})
    assert "Справ" in fallback, fallback
    assert _ticket_subject({"topic": "  ", "category": "feedback"}).strip()


def test_answerer_label_shows_position_and_name(env):
    """Подпись отвечающего: должность и имя, как человек видит себя у студента."""
    row = {"full_name": "Тестовый сотрудник", "position": "Секретарь", "role": ""}
    assert _answerer_label(row, "test-1") == "Секретарь, Тестовый сотрудник"
    bare = {"full_name": "Петрова Анна", "position": "", "role": "Методист"}
    assert _answerer_label(bare, "200") == "Методист, Петрова Анна"
    assert _answerer_label(None, "test-1") == "сотрудник колледжа"
