"""Вход сотрудника по ссылке-приглашению.

Ссылка приходит диплинком MAX, по переходу бот обязан показать данные и
спросить человека, а не выдать права молча: так требуют правила платформы.
Проверяем именно это, а ещё одноразовость ссылки - по ней нельзя войти
дважды, даже разным людям.
"""
import asyncio
from itertools import count

import pytest

import database as db
import max_api
import repository as repo
import utils
from bot import process
from conftest import press
from handlers import invites
from store.access import (claim_invite, create_invites_bulk, get_invite, invite_code,
                          invite_link, invite_payload, invite_status)
from utils import PROFILE_LINK
import updates

NEWSTAFF, OTHER, SYS = "700", "701", "1"
BOT_NAME = "se14445139_bot"
PERSON = {"full_name": "Иванов Иван Иванович", "position": "Секретарь",
          "office": "214", "category": "certificates"}

_starts = count(1)


@pytest.fixture(autouse=True)
def invites_api(monkeypatch, api):
    """Клиент MAX для нового модуля: подменяем его так же, как остальные."""
    monkeypatch.setattr(invites, "api", api)
    return api


def start_event(user, payload: str = "") -> dict:
    """Событие bot_started - так приходит переход по ссылке ?start=<payload>."""
    event = {"update_type": "bot_started",
             "timestamp": f"2026-09-29T10:{next(_starts):02d}:00.000Z",
             "user": {"user_id": int(user), "first_name": "Иван", "last_name": "Иванов"}}
    if payload:
        event["payload"] = payload
    return event


async def issue(**overrides) -> str:
    """Приглашение, как его выпускает панель: одна строка пачки."""
    entry = {**PERSON, **overrides}
    issued = await create_invites_bulk([entry], created_by=SYS, ttl_hours=24)
    return issued[0]["code"]


def all_text(api, user) -> str:
    """Всё, что бот написал человеку, одной строкой."""
    return "\n".join(message[1] for message in api.to(user))


# ── payload ссылки ───────────────────────────────────────────────────────────
def test_payload_is_read_from_bot_started_event():
    """Ровно то поле, которое MAX присылает при переходе по диплинку."""
    assert updates.start_payload(start_event(NEWSTAFF, "inv_ABCD1234")) == "inv_ABCD1234"
    assert invite_code("inv_ABCD1234") == "ABCD1234"


def test_plain_start_has_no_payload():
    """Обычный /start приходит без payload, и это не поломка ссылки."""
    assert updates.start_payload(start_event(NEWSTAFF)) == ""
    assert updates.start_payload({"update_type": "message_created", "payload": "inv_X"}) == ""


def test_payload_from_foreign_link_is_not_an_invite():
    """Чужой payload приглашением не считается: код из него не достаётся."""
    assert invite_code("ref_ABCD1234") == ""
    assert invite_code("") == ""


def test_payload_never_exceeds_the_platform_limit():
    """MAX кладёт в ссылку не больше 128 символов - и бот режет длиннее."""
    long_payload = "inv_" + "A" * 300
    assert len(updates.start_payload(start_event(NEWSTAFF, long_payload))) <= updates.MAX_START_PAYLOAD


async def test_start_without_payload_shows_ordinary_menu(api):
    """Ссылка не наша - человек видит обычный вход, а не экран приглашения."""
    await process(start_event(NEWSTAFF))
    assert "Кто вы?" in all_text(api, NEWSTAFF)


async def test_start_with_payload_shows_invite_screen(api):
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    assert "Вас пригласили" in all_text(api, NEWSTAFF)


# ── экран подтверждения ──────────────────────────────────────────────────────
async def test_screen_shows_all_four_data_lines(api):
    """Все четыре строки данных показаны всегда - по ним человек себя сверяет."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    text = all_text(api, NEWSTAFF)
    assert "ФИО: Иванов Иван Иванович" in text
    assert "Должность: Секретарь" in text
    assert "Кабинет: 214" in text
    assert "Раздел обращений: Справки" in text


async def test_screen_says_so_about_empty_fields(api):
    """Пустое поле не прячется: «кабинет не указан» иначе читается как потеря данных."""
    code = await issue(office="")
    await process(start_event(NEWSTAFF, invite_payload(code)))
    text = all_text(api, NEWSTAFF)
    assert "Кабинет: кабинет не указан" in text
    assert "Должность: Секретарь" in text


async def test_screen_shows_the_deadline_in_words(api):
    """Срок виден человеческим языком, а не только датой в базе."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    assert "Приглашение действует до" in all_text(api, NEWSTAFF)


async def test_button_labels_fit_the_max_width(api):
    """Подписи кнопок влезают в строку MAX: в ряду из двух там 16 ячеек."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    for row in api.last(NEWSTAFF)[2]:
        for button in row:
            assert max_api.display_width(button["text"]) <= max_api.row_limit(len(row)), button["text"]


async def test_expired_invite_is_honest(api):
    """Просроченное приглашение честно отказывает и ничего не создаёт."""
    code = await issue()
    await db.run("UPDATE staff_invites SET expires_at=datetime('now','-1 hour') WHERE code=?",
                 (code,))
    await process(start_event(NEWSTAFF, invite_payload(code)))
    text = all_text(api, NEWSTAFF)
    assert "Срок приглашения истёк" in text
    assert await repo.get_admin(NEWSTAFF) is None


async def test_used_invite_is_honest(api):
    """Сработавшая ссылка второй раз не пускает."""
    code = await issue()
    await press(NEWSTAFF, f"invok:{code}")
    api.sent.clear()
    await process(start_event(OTHER, invite_payload(code)))
    assert "уже сработала" in all_text(api, OTHER)
    assert await repo.get_admin(OTHER) is None


async def test_unknown_code_is_honest(api):
    """Битая ссылка не создаёт ничего и не выглядит как приглашение."""
    await process(start_event(NEWSTAFF, "inv_ZZZZZZZZ"))
    assert "Приглашение не найдено" in all_text(api, NEWSTAFF)
    assert await repo.get_admin(NEWSTAFF) is None


async def test_unreadable_link_falls_back_to_ordinary_start(api):
    """Код с русскими буквами кодом быть не может - это уже не наша ссылка."""
    await process(start_event(NEWSTAFF, "inv_НЕТТАКОГО"))
    assert "Приглашение не найдено" in all_text(api, NEWSTAFF)
    assert await repo.get_admin(NEWSTAFF) is None


# ── вход по кнопке ───────────────────────────────────────────────────────────
async def test_enter_makes_staff_with_position_office_and_category(api):
    """Главное обещание ссылки: человек стал сотрудником с теми же данными."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    await press(NEWSTAFF, f"invok:{code}")
    row = await repo.get_admin(NEWSTAFF)
    assert row["full_name"] == "Иванов Иван Иванович"
    assert row["position"] == "Секретарь"
    assert row["office"] == "214"
    assert row["ticket_category"] == "certificates"
    assert "Вы зарегистрированы как сотрудник" in all_text(api, NEWSTAFF)


async def test_after_enter_staff_sees_office_and_menu(api):
    """После входа человек видит свой кабинет и меню сотрудника."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    await press(NEWSTAFF, f"invok:{code}")
    assert "Кабинет: 214" in all_text(api, NEWSTAFF)
    labels = [button["text"] for row in api.last(NEWSTAFF)[2] for button in row]
    assert any("Обращения" in label for label in labels)


async def test_invite_burns_and_cannot_be_used_again(api):
    """Одноразовость: вторая кнопка по той же ссылке ничего не создаёт."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    await press(NEWSTAFF, f"invok:{code}")
    before = (await repo.get_admin(NEWSTAFF))["created_at"]
    await press(NEWSTAFF, f"invok:{code}")
    assert invite_status(await get_invite(code)) == "used"
    assert (await repo.get_admin(NEWSTAFF))["created_at"] == before
    assert await repo.get_admin(OTHER) is None


async def test_two_people_cannot_enter_by_one_link(api):
    """Два человека с одной ссылкой не пройдут оба: победит первый."""
    code = await issue()
    await press(NEWSTAFF, f"invok:{code}")
    await press(OTHER, f"invok:{code}")
    assert await repo.get_admin(NEWSTAFF)
    assert await repo.get_admin(OTHER) is None


async def test_claim_is_the_thing_that_makes_it_once():
    """Одноразовость держится на атомарном заборе, а не на проверке в хендлере."""
    code = await issue()
    assert await claim_invite(code, NEWSTAFF) == (True, "")
    again, reason = await claim_invite(code, OTHER)
    assert again is False and reason
    assert invite_status(await get_invite(code)) == "used"


async def test_two_simultaneous_clauses_give_it_to_one(monkeypatch):
    """Два человека жмут кнопку одновременно - приглашение достанется одному.

    Гонка устроена настоящая: оба забора успевают прочитать строку приглашения
    (там used_by ещё пуст), и только потом оба пробуют её погасить. Побеждает
    UPDATE с условием used_by='' - он меняет строку ровно один раз, поэтому
    второму остаётся ноль строк и отказ.
    """
    code = await issue()
    arrived, release = [], asyncio.Event()
    original = db.run_count

    async def slow_run_count(sql, params=()):
        if "UPDATE staff_invites SET used_by" in sql:
            arrived.append(1)
            if len(arrived) == 1:
                await release.wait()      # ждём, пока второй тоже дойдёт
            else:
                release.set()
        return await original(sql, params)

    monkeypatch.setattr(db, "run_count", slow_run_count)
    claims = await asyncio.wait_for(asyncio.gather(
        claim_invite(code, NEWSTAFF), claim_invite(code, OTHER)), timeout=30)
    assert len(arrived) == 2, "оба забора должны дойти до погашения"
    assert [ok for ok, _reason in claims].count(True) == 1


async def test_expired_invite_does_not_let_enter(api):
    """Даже нажатие кнопки по просроченной ссылке не заводит сотрудника."""
    code = await issue()
    await db.run("UPDATE staff_invites SET expires_at=datetime('now','-1 hour') WHERE code=?",
                 (code,))
    await press(NEWSTAFF, f"invok:{code}")
    assert await repo.get_admin(NEWSTAFF) is None
    assert "Срок приглашения истёк" in all_text(api, NEWSTAFF)
    assert invite_status(await get_invite(code)) == "expired"


async def test_cancel_creates_nothing_and_keeps_the_link(api):
    """Отказ - это вежливо: ничего не создаётся, но и ссылка не тратится."""
    code = await issue()
    await process(start_event(NEWSTAFF, invite_payload(code)))
    await press(NEWSTAFF, f"invno:{code}")
    assert await repo.get_admin(NEWSTAFF) is None
    assert invite_status(await get_invite(code)) == "active"
    assert "не подтверждаю" in all_text(api, NEWSTAFF)


async def test_staff_enters_without_burning_someone_elses_link(api):
    """Тот, кто уже сотрудник, чужую ссылку не сжигает и не портится сам."""
    code = await issue()
    await repo.add_staff(NEWSTAFF, "Соколова Мария Сергеевна", position="Бухгалтерия")
    await process(start_event(NEWSTAFF, invite_payload(code)))
    assert "Вы уже сотрудник" in all_text(api, NEWSTAFF)
    await press(NEWSTAFF, f"invok:{code}")
    assert invite_status(await get_invite(code)) == "active"


# ── ссылка ───────────────────────────────────────────────────────────────────
def test_link_is_built_from_the_bot_username():
    """Адрес берётся из шаблона профилей MAX и ника бота, а не пишется руками."""
    assert invite_link(BOT_NAME, "ABCD1234") == (
        f"{PROFILE_LINK.format(username=BOT_NAME)}?start=inv_ABCD1234")


def test_link_follows_the_profile_template(monkeypatch):
    """Смена адреса MAX в настройке меняет и ссылки приглашений - адрес не зашит."""
    monkeypatch.setattr(utils, "PROFILE_LINK", "https://example.org/bot/{username}")
    assert invite_link(BOT_NAME, "ABCD1234") == "https://example.org/bot/se14445139_bot?start=inv_ABCD1234"


def test_link_is_empty_without_bot_name_or_code():
    """Нет ника или кода - нет и ссылки: выдумывать адрес опасно."""
    assert invite_link("", "ABCD1234") == ""
    assert invite_link(BOT_NAME, "") == ""


def test_payload_of_link_is_short_enough_for_max():
    """payload ссылки укладывается в 128 символов, которые даёт платформа."""
    assert len(invite_payload("ABCD1234")) <= 128
