"""Топология обработчиков и совместимость старых кнопок v1.6.0."""
import pytest

import database as db
import repository as repo
from conftest import press, register, say
from handlers import admin, menus
from handlers.registry import (CALLBACKS, COMPATIBILITY_MANIFEST, STATES, callback,
                               state)

SYS, STUDENT, REQUESTER = "1", "300", "301"


def test_duplicate_callback_fails_loudly():
    existing = CALLBACKS["home"]
    with pytest.raises(RuntimeError, match="Повторная регистрация callback 'home'"):
        callback("home")(lambda *_: None)
    assert CALLBACKS["home"] is existing


def test_duplicate_state_fails_loudly():
    existing = STATES["reg_name"]
    with pytest.raises(RuntimeError, match="Повторная регистрация state 'reg_name'"):
        state("reg_name")(lambda *_: None)
    assert STATES["reg_name"] is existing


def test_registry_has_one_owner_for_split_routes():
    assert CALLBACKS["today"] is menus.cb_today
    assert CALLBACKS["admin_today"] is admin.cb_admin_today
    assert STATES["staff_request_position"] is menus.st_staff_request_position
    assert STATES["admin_request_position"] is admin.st_admin_request_position
    assert STATES["req_position"] is menus.st_legacy_req_position


def test_compatibility_manifest_names_live_handlers():
    assert set(COMPATIBILITY_MANIFEST) == {"callbacks", "states"}
    assert set(COMPATIBILITY_MANIFEST["callbacks"]) <= CALLBACKS.keys()
    assert set(COMPATIBILITY_MANIFEST["states"]) <= STATES.keys()


async def test_new_staff_request_uses_unambiguous_state(api):
    await press(REQUESTER, "staffreq")
    await say(REQUESTER, "Соколова Мария")
    assert (await db.get_state(REQUESTER))["state"] == "staff_request_position"
    await say(REQUESTER, "Секретарь")
    assert (await db.get_state(REQUESTER))["state"] == "req_office"


async def test_admin_request_edit_uses_unambiguous_state(api):
    await repo.create_staff_request(REQUESTER, "Соколова Мария", "Секретарь")
    await press(SYS, f"reqpos:{REQUESTER}")
    assert (await db.get_state(SYS))["state"] == "admin_request_position"
    await say(SYS, "Методист")
    assert (await repo.get_staff_request(REQUESTER))["position"] == "Методист"


async def test_legacy_req_position_dispatches_by_payload(api):
    await db.set_state(REQUESTER, "req_position", {"name": "Соколова Мария"})
    await say(REQUESTER, "Секретарь")
    assert (await db.get_state(REQUESTER))["state"] == "req_office"

    await repo.create_staff_request(REQUESTER, "Соколова Мария", "Секретарь")
    await db.set_state(SYS, "req_position", {"id": REQUESTER, "actor": SYS})
    await say(SYS, "Методист")
    assert (await repo.get_staff_request(REQUESTER))["position"] == "Методист"


async def test_legacy_req_position_without_shape_recovers_visibly(api):
    await db.set_state(REQUESTER, "req_position", {})
    await say(REQUESTER, "что угодно")
    assert "Старая заявка потеряла данные" in api.last(REQUESTER)[1]
    assert await db.get_state(REQUESTER) is None


async def test_legacy_savegrp_is_strict(api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    await repo.upsert_group("25-27", "Другая группа")
    before = (await repo.get_user(STUDENT))["group_code"]
    await press(STUDENT, "savegrp:25-27:мусор")
    assert (await repo.get_user(STUDENT))["group_code"] == before
    assert "Старая кнопка профиля" in api.last(STUDENT)[1]


async def test_legacy_saveprofile_is_atomic(api):
    await register(STUDENT, "Иванов Иван Иванович", "24-23")
    before = dict(await repo.get_user(STUDENT))
    await press(STUDENT, "saveprofile:name=Петров Пётр;unknown=данные")
    after = dict(await repo.get_user(STUDENT))
    assert (after["full_name"], after["group_code"]) == (before["full_name"], before["group_code"])
    assert "Старая кнопка профиля" in api.last(STUDENT)[1]
