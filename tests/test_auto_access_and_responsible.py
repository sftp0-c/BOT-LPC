"""Тесты автоматического режима отправки кодов сотрудникам и назначения ответственных за разделы обращений."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import repository as repo
from conftest import add_staff, press, register
from store import web_access as access
from web import common as panel_common
from web.router import router

STUDENT = "300"
STAFF1 = "7111222333"
STAFF2 = "7222333444"


@pytest.fixture
def panel_client(env, monkeypatch):
    monkeypatch.setattr(config, "WEB_PANEL_PASSWORD", "test-pass")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url="https://panel.example", follow_redirects=False)
    token = panel_common.start_session("1")
    client.cookies.set(panel_common.COOKIE, token, domain="panel.example", path="/")
    return client


async def test_auto_code_mode_default_and_dispatch(env):
    # По умолчанию режим включён
    assert await access.is_staff_auto_codes_enabled() is True
    await add_staff(STAFF1, "Иванова Анна", category="certificates", position="Секретарь")

    # Сотрудник не был настроен вручную
    assert await access.get_staff_web_access(STAFF1) is None

    now = access._now()
    browser = access.staff_web_hash("test-browser", STAFF1)
    peer = access.staff_web_hash("test-peer", STAFF1)

    # Запрашиваем код: в авто-режиме код генерируется и возвращается
    delivery = await access.request_staff_web_code(STAFF1, browser, peer, now=now, browser_expires=now + 600)
    assert delivery["user_id"] == STAFF1
    assert len(delivery["code"]) == 8

    # Проверяем код: вход успешен
    token = await access.verify_staff_web_code(delivery["challenge"], delivery["code"], browser, peer, now=now)
    assert token
    st = await access.get_staff_web_access(STAFF1)
    assert st["enabled"] == 1


async def test_auto_code_mode_disabled(env):
    await add_staff(STAFF2, "Петров Сергей", category="academic")
    assert await access.set_staff_auto_codes_mode(False, "1")
    assert await access.is_staff_auto_codes_enabled() is False

    now = access._now()
    browser = access.staff_web_hash("test-browser", STAFF2)
    peer = access.staff_web_hash("test-peer", STAFF2)

    # При выключенном режиме ненастроенный сотрудник не получает код
    delivery = await access.request_staff_web_code(STAFF2, browser, peer, now=now, browser_expires=now + 600)
    assert not delivery["user_id"]
    assert not delivery["code"]


async def test_explicitly_disabled_staff_never_gets_code_in_auto_mode(env):
    await add_staff(STAFF1, "Иванова Анна", category="certificates")
    # Явно отключаем сотрудника
    assert await access.set_staff_web_access(STAFF1, False, [], "1")

    now = access._now()
    browser = access.staff_web_hash("test-browser", STAFF1)
    peer = access.staff_web_hash("test-peer", STAFF1)

    delivery = await access.request_staff_web_code(STAFF1, browser, peer, now=now, browser_expires=now + 600)
    assert not delivery["user_id"]
    assert not delivery["code"]


async def test_enable_all_staff_web_access(env):
    await add_staff(STAFF1, "Иванова Анна")
    await add_staff(STAFF2, "Петров Сергей")

    count = await access.enable_all_staff_web_access("1")
    assert count >= 2
    row1 = await access.get_staff_web_access(STAFF1)
    row2 = await access.get_staff_web_access(STAFF2)
    assert row1["enabled"] == 1
    assert row2["enabled"] == 1


async def test_panel_staff_access_auto_mode_endpoints(panel_client):
    # Проверяем карточку авто-режима в списке
    res = panel_client.get("/panel/staff-access")
    assert res.status_code == 200
    assert "Режим отправки кодов" in res.text

    # Переключаем режим через POST
    csrf = res.text.split('name="csrf" value="')[1].split('"')[0]
    toggle = panel_client.post("/panel/staff-access/auto-mode", data={"csrf": csrf, "enabled": "0"})
    assert toggle.status_code == 303
    assert await access.is_staff_auto_codes_enabled() is False

    # Включаем обратно
    toggle_on = panel_client.post("/panel/staff-access/auto-mode", data={"csrf": csrf, "enabled": "1"})
    assert toggle_on.status_code == 303
    assert await access.is_staff_auto_codes_enabled() is True


async def test_category_responsible_auto_pick_in_bot(api):
    await register(STUDENT, "Иванов Иван", "24-23")
    await add_staff(STAFF1, "Петрова Мария", category="certificates", position="Секретарь")

    # 1. При одном сотруднике с категорией certificates — он единственный ответственный
    resp = await repo.get_category_responsible("certificates")
    assert resp and str(resp["user_id"]) == STAFF1

    # Студент выбирает справку
    await press(STUDENT, "sub:cert")
    await press(STUDENT, "ask:certificates:place")

    # Не должно быть кнопок выбора сотрудника — обращение сразу переходит к вводу текста
    assert not any(p.startswith("pick:certificates:") for p in api.payloads(STUDENT))
    last_text = api.last(STUDENT)[1]
    assert "Петрова Мария" in last_text
    assert "Напишите обращение одним сообщением" in last_text

    # 2. Назначаем явного ответственного через set_category_responsible
    await add_staff(STAFF2, "Сидоров Олег", category="all", position="Дежурный")
    await repo.set_category_responsible("certificates", STAFF2, "1")
    resp2 = await repo.get_category_responsible("certificates")
    assert resp2 and str(resp2["user_id"]) == STAFF2

    api.sent.clear()
    await press(STUDENT, "sub:cert")
    await press(STUDENT, "ask:certificates:period")
    last_text2 = api.last(STUDENT)[1]
    assert "Сидоров Олег" in last_text2
    assert "Напишите обращение одним сообщением" in last_text2


async def test_category_without_responsible_shows_picker(api):
    await register(STUDENT, "Иванов Иван", "24-23")
    # Два сотрудника с категорией all и без явного ответственного
    await add_staff(STAFF1, "Сотрудник 1", category="all")
    await add_staff(STAFF2, "Сотрудник 2", category="all")

    await press(STUDENT, "sub:acc")
    await press(STUDENT, "ask:accounting:scholarship")

    # Должен остаться выбор сотрудника (pick кнопки)
    payloads = api.payloads(STUDENT)
    assert any(p.startswith(f"pick:accounting:{STAFF1}") for p in payloads)
    assert any(p.startswith(f"pick:accounting:{STAFF2}") for p in payloads)


async def test_panel_settings_responsible_form(panel_client):
    await add_staff(STAFF1, "Иванова Анна")
    res = panel_client.get("/panel/settings")
    assert res.status_code == 200
    assert "Ответственные за разделы обращений" in res.text
    assert "resp_certificates" in res.text

    csrf = res.text.split('name="csrf" value="')[1].split('"')[0]
    post_res = panel_client.post("/panel/settings/responsible", data={
        "csrf": csrf,
        "resp_certificates": STAFF1,
        "resp_accounting": "",
        "resp_academic": "",
        "resp_feedback": "",
    })
    assert post_res.status_code == 303
    assert (await repo.get_category_responsible("certificates"))["user_id"] == STAFF1
