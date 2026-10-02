"""Проверки: ФИО в списке людей и терпимость contact_full_name к строке.

Задача владельца: «почему я в пользователях вижу только айдишники».

Что было
--------
Запрос реестра отдаёт ФИО студента под именем fio, а сотрудника - staff_name
(store/common.py, _CONTACT_SELECT). Поля full_name в результате нет. Экран читал
именно его, получал пустую строку и person_label по задумке показывал
«ID <номер>». Так и вышло: у всех людей в списках вместо ФИО шёл номер MAX.

Вторая ошибка нашлась сразу
---------------------------
Первая правка - подставить contact_full_name - уронила бы экран: функция сделана
через card.get(...), а db.many отдаёт sqlite3.Row, у которого метода get нет.
Ловится только на живом вызове со строкой из реестра, поэтому проверка ниже и
это делает.

Что проверяем
-------------
1. contact_full_name терпит и строку из реестра, и словарь - одинаково.
2. Экран списка берёт ФИО, а не пустую строку: у студента фамилия должна быть
   видна, а не «ID».
3. Гость без ФИО остаётся с номером - это правильно, имени у него в системе нет.
"""
import sqlite3

import pytest

from store.people import contact_full_name

from handlers import admin as admin_mod

USER = "100"

OWNER_ID = "1"

СТУДЕНТ = {
    "user_id": USER,
    "username": "ivanov",
    "display_name": "Иванов Иван Иванович",
    "fio": "Пахтусова Ольна Павловна",
    "group_code": "24-28",
    "staff_name": "",
}
ГОСТЬ = {
    "user_id": "300",
    "username": "guest",
    "display_name": "",
    "fio": "",
    "group_code": "",
    "staff_name": "",
}
СОТРУДНИК = {
    "user_id": "200",
    "username": "petrov",
    "display_name": "Пётр Петров",
    "fio": "",
    "group_code": "",
    "staff_name": "Петров Пётр Сергеевич",
}


def test_contact_full_name_takes_dict():
    """Словарь - обычный случай, из карточки."""
    assert contact_full_name(dict(СТУДЕНТ)) == "Пахтусова Ольна Павловна"
    assert contact_full_name(dict(СОТРУДНИК)) == "Петров Пётр Сергеевич"


def test_contact_full_name_takes_sqlite_row():
    """Строка из реестра - тоже обычный случай, и он ронял экран.

    db.many отдаёт sqlite3.Row, у него нет метода get. Раньше функция звала
    card.get(...), и любой вызов из списка заканчивался AttributeError - то есть
    500 на рабочем экране сис-админа.
    """
    кон = sqlite3.connect(":memory:")
    кон.row_factory = sqlite3.Row
    # Сначала создаём таблицу, потом наполняем: ALTER до CREATE невозможен.
    кон.execute("CREATE TABLE t (fio TEXT, staff_name TEXT, display_name TEXT)")
    кон.execute("INSERT INTO t VALUES (?,?,?)",
                (СТУДЕНТ["fio"], "", СТУДЕНТ["display_name"]))
    row = кон.execute("SELECT * FROM t").fetchone()
    кон.close()

    assert not hasattr(row, "get"), "sqlite3.Row всё же не должен иметь get"
    assert contact_full_name(row) == "Пахтусова Ольна Павловна"


def test_contact_full_name_same_for_row_and_dict():
    """Оба вида строк дают одно и то же - иначе результат зависит от вызова."""
    кон = sqlite3.connect(":memory:")
    кон.row_factory = sqlite3.Row
    кон.execute("CREATE TABLE t (fio TEXT, staff_name TEXT, display_name TEXT)")
    кон.execute("INSERT INTO t VALUES (?,?,?)",
                (СОТРУДНИК["fio"], СОТРУДНИК["staff_name"], СОТРУДНИК["display_name"]))
    row = кон.execute("SELECT * FROM t").fetchone()
    кон.close()

    assert contact_full_name(row) == contact_full_name(dict(СОТРУДНИК))


@pytest.mark.parametrize("пусто", [None, {}, "", 0])
def test_contact_full_name_survives_empty(пусто):
    """Пустой карточки быть не должно, но экран не должен из-за неё падать."""
    assert contact_full_name(пусто) == ""


def test_contact_full_name_falls_back_through_all_sources():
    """Порядок источников: ФИО, потом сотрудник, потом подпись из контактов."""
    только_подпись = {"fio": "", "staff_name": "", "display_name": "Ковалевский К. Ю."}
    assert contact_full_name(только_подпись) == "Ковалевский К. Ю."

    пусто_везде = {"fio": "", "staff_name": "", "display_name": ""}
    assert contact_full_name(пусто_везде) == ""


async def test_people_list_shows_name_not_id(monkeypatch, api):
    """Главное: в списке людей фамилия видна, а не номер MAX.

    Раньше экран читал поле full_name, которого в запросе реестра нет, и у всех
    подряд показывал «ID <номер>» - включая студентов с нормальными ФИО.
    """
    class Строка(dict):
        # send_people передаёт строки в person_label и _field, поэтому строка
        # должна вести себя как словарь: уметь keys() и получать по ключу.

        def keys(self):
            return dict.keys(self)

    async def сколько(*args, **kwargs):
        """people_count возвращает число: экран сравнивает с ним смещение."""
        return 2

    async def список(*args, **kwargs):
        """people возвращает строки реестра."""
        return [Строка(СТУДЕНТ), Строка(ГОСТЬ)]

    monkeypatch.setattr(admin_mod.repo, "people_count", сколько, raising=False)
    monkeypatch.setattr(admin_mod.repo, "people", список, raising=False)
    monkeypatch.setattr(admin_mod, "need_super", lambda x: True, raising=False)

    await admin_mod.send_people(OWNER_ID, "", 0)

    текст, кнопки = api.last(OWNER_ID)[1], api.last(OWNER_ID)[2] or []
    подписи = [b["text"] for ряд in кнопки for b in ряд]

    assert any("Пахтусова" in п for п in подписи), \
        f"фамилии студента в списке нет: {подписи}"
    assert not any(п.startswith("ID") and "300" not in п for п in подписи), \
        f"у зарегистрированных людей вместо ФИО номер: {подписи}"
    # у гостя имени в системе нет - номер там честный и правильный
    assert any("300" in п for п in подписи), \
        f"гость должен показываться по номеру: {подписи}"
    assert текст


OWNER_ID = "1"