"""Адрес возврата из формы: регрессия на 422 и защита от чужого адреса.

Пользователь прислал ошибку с панели:

    {"type":"int_parsing","loc":["path","ticket_id"],
     "input":"status=&category=&q=&scope="}

Причина. В очереди обращений скрытое поле return клало строку запроса БЕЗ
ведущего слеша и БЕЗ вопросительного знака. Обработчик отдавал её в redirect
как есть, FastAPI слал 303 с относительным Location, браузер разрешал его
относительно текущего адреса - и получался "/panel/tickets/status=&category=
&q=&scope=". Маршрут /tickets/{ticket_id} пытался разобрать ticket_id как
целое число и отвечал 422.

Вторая половина той же правки - безопасность. Значение приходит из запроса
и уходит в заголовок Location, поэтому принимать его без проверки нельзя:
можно увести сис-админа на чужой сайт. safe_return() берёт только путь
внутри /panel.
"""
import pytest

import config
import database as db
import repository as repo
from conftest import login_panel, post_form
from web.common import safe_return

pytestmark = pytest.mark.panel

OWNER = "46010397"
SYS = "1"
STUDENT, STAFF = "100", "200"

# Значения, которые нельзя выпускать в Location. Часть из них приводит к 422,
# часть уводит человека на чужой сайт.
BAD = [
    "status=&category=&q=&scope=",                 # ровно то, что видел пользователь
    "?status=&category=&q=&scope=",
    "//evil.example/steal",                          # протокол-относительный
    "http://evil.example/steal",                     # абсолютный чужой
    "https://evil.example/steal",
    "/panel/tickets\r\nLocation: http://evil.example",   # разрыв ответа
    "/panel/../secret",                              # выход из раздела
    "",
    "   ",
    "javascript:alert(1)",
    "../../etc/passwd",
]

# Значения, которые обязаны пройти: это наши же адреса с фильтрами.
GOOD = [
    "/panel/tickets",
    "/panel/tickets?status=&category=&q=&scope=",
    "/panel/tickets?status=new&t=7&view=archive",
    "/panel/test",
    "/panel/data?table=admins",
]


# ── правило целиком ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", BAD)
def test_chuzhyj_ili_bolnoy_adres_zamenyaetsya_na_svoy(raw):
    """Вредный или относительный адрес не должен попасть в Location."""
    data = {"return": raw}
    assert safe_return(data) == "/panel/tickets", raw


@pytest.mark.parametrize("raw", GOOD)
def test_svoi_adresi_prohodyat_kak_est(raw):
    """Свои адреса с фильтрами возвращаются без изменений."""
    assert safe_return({"return": raw}) == raw, raw


def test_pusto_bez_polya_vozvrashchaet_zapasnoe():
    assert safe_return({}) == "/panel/tickets"
    assert safe_return({}, "/panel/test") == "/panel/test"


# ── регрессия: именно то, что прислал пользователь ───────────────────────
@pytest.fixture
async def owner_client(panel_client, monkeypatch, env):
    monkeypatch.setattr(config, "ROOT_IDS", [OWNER])
    await db.init_db()
    assert login_panel(panel_client, OWNER)
    return panel_client


async def seed_ticket(env) -> int:
    await db.run("INSERT OR IGNORE INTO users(user_id, full_name, group_code) VALUES(?,?,?)",
                 (STUDENT, "Иванов Иван", "ИС-21"))
    await db.run("INSERT OR IGNORE INTO admins(user_id, full_name) VALUES(?,?)",
                 (STAFF, "Петрова Анна"))
    return await repo.create_ticket(STUDENT, STAFF, "feedback", "Нужна справка")


def location_of(response) -> str:
    return response.headers.get("location") or ""


@pytest.mark.parametrize("bad", ["status=&category=&q=&scope=", "?status=&category=&q=",
                                 "//evil.example/steal", "http://evil.example/steal"])
async def test_massovoe_deystvie_vozvrashchaet_na_svoj_razdel(owner_client, env, bad):
    """Главное: ответ 303 уводит в раздел панели, а не в 422 и не наружу."""
    tid = await seed_ticket(env)
    response = post_form(owner_client, "/panel/tickets/bulk", {
        "action": "archive", "tids": str(tid), "value": "", "all": "", "return": bad,
    })
    assert response.status_code == 303, response.status_code
    where = location_of(response)
    assert where.startswith("/panel/"), f"редирект наружу: {where}"
    # ровно та поломка, что видел пользователь
    assert "/tickets/status=" not in where, f"снова относительный адрес: {where}"


async def test_massovoe_deystvie_bez_otmetok_takzhe_bezopasno(owner_client, env):
    """Ветка «ничего не отмечено» редиректит так же безопасно."""
    response = post_form(owner_client, "/panel/tickets/bulk", {
        "action": "archive", "tids": "", "value": "", "all": "",
        "return": "status=&category=&q=&scope=",
    })
    assert response.status_code == 303
    assert location_of(response).startswith("/panel/")


async def test_v_ocheredi_lezhit_polnyy_adres_vozvrata(owner_client, env):
    """В форме должен лежать полный адрес, а не строка запроса.

    Смотрим обычную очередь, а не архив: форма массовых действий печатается
    вместе с пунктами очереди, и в пустом архиве её просто нет.
    """
    await seed_ticket(env)
    page = owner_client.get("/panel/tickets").text
    assert 'name="return"' in page, "в очереди нет формы массовых действий"
    assert 'name="return" value="/panel/tickets?' in page, "в return снова строка запроса"
    assert 'name="return" value="status=' not in page, "строка запроса без ведущего слеша"
