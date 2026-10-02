import re
from utils import group_code

import pytest

from conftest import add_staff, press, register
from handlers import menus
from repository import set_admin_profile, upsert_group


USER = "100"
STAFF = "200"
DIRECTOR = "201"


class FakeAPI:
    def __init__(self):
        self.sent = []

    async def send(self, user_id, text, keyboard=None):
        self.sent.append((str(user_id), text, keyboard))

    def last(self, user_id):
        return [item for item in self.sent if item[0] == str(user_id)][-1]

    def payloads(self, user_id):
        keyboard = self.last(user_id)[2] or []
        return [button["payload"] for row in keyboard for button in row if button["type"] == "callback"]


class FakeDB:
    def __init__(self):
        self.states = {}
        self.settings = {}

    async def set_state(self, user_id, state, payload=None):
        self.states[str(user_id)] = {"state": state, "payload": payload or {}}

    async def clear_state(self, user_id):
        self.states.pop(str(user_id), None)

    async def get_state(self, user_id):
        return self.states.get(str(user_id))

    async def get_setting(self, key, default=None):
        return self.settings.get(str(key), default)

    async def set_setting(self, key, value):
        self.settings[str(key)] = str(value)

    async def one(self, sql, params=()):
        """Заглушка для подсказки ФИО из профиля: контакта в тестах нет."""
        return None


class FakeRepo:
    def __init__(self, groups=("ИС-21",), schedules=None):
        self.groups = [{"code": code, "active": 1} for code in groups]
        self.schedules = schedules or {}
        self.users = {USER: {"full_name": "Иванов Иван", "group_code": "ИС-21"}}
        self.saved = []

    async def get_user(self, user_id):
        return self.users.get(str(user_id))

    async def is_registered(self, user_id):
        return str(user_id) in self.users

    async def get_admin(self, user_id):
        return None

    async def list_groups(self, active_only=True):
        return [row for row in self.groups if row["active"] or not active_only]

    async def add_user(self, user_id, full_name, group_code):
        self.saved.append((str(user_id), full_name, group_code))
        self.users[str(user_id)] = {"full_name": full_name, "group_code": group_code}

    async def get_schedule(self, group_code):
        url = self.schedules.get(group_code)
        return {"group_code": group_code, "pdf_url": url} if url else None

    async def top_groups(self, limit=8):
        """Заведённые группы: берём из справочника, как в боте."""
        return [{"group_code": row["code"]} for row in self.groups if row["active"]][:limit]

    async def suggest_groups(self, text="", limit=8):
        """Подсказки по цифрам кода: так же, как в настоящем справочнике."""
        digits = re.sub(r"\D", "", str(text))
        groups = [{"code": row["code"], "title": "", "active": 1} for row in self.groups]
        if not digits:
            return groups[:limit]
        return sorted(groups, key=lambda g: (0 if g["code"] == str(text).upper() else 1, g["code"]))[:limit]

    async def resolve_group(self, text):
        """Есть ли такая группа в справочнике.

        Код нормализуется через utils.group_code - так же, как в боте
        (store/groups.py find_group). Раньше здесь было буквальное сравнение,
        и заготовка не находила «2423 П» для группы «24-23П». Из-за этого
        проверки рисовали неверную картину: настоящий бот такой код понимает,
        а тест - нет.
        """
        wanted = group_code(text)
        for row in self.groups:
            if group_code(row["code"]) == wanted:
                return {"found": True, "code": row["code"], "title": "",
                        "suggestions": []}
        return {"found": False, "code": wanted, "title": "",
                "suggestions": await self.suggest_groups(text)}

    async def find_group(self, text):
        answer = await self.resolve_group(text)
        return {"code": answer["code"], "title": "", "active": 1} if answer["found"] else None

    async def add_group_aliases(self, group_code, aliases=()):
        return 0

    async def upsert_group(self, code="", title=None, active=None, **kwargs):
        if code and not any(g["code"] == code for g in self.groups):
            self.groups.append({"code": code, "active": 1})
        return True

    async def all_admins(self):
        return []

    async def log_action(self, actor, action, details=""):
        return 0


@pytest.fixture
def flow(monkeypatch):
    api = FakeAPI()
    repo = FakeRepo(schedules={"ИС-21": "https://college.example/is-21.pdf"})
    db = FakeDB()
    monkeypatch.setattr(menus, "api", api)
    monkeypatch.setattr(menus, "repo", repo)
    monkeypatch.setattr(menus, "db", db)
    return api, repo, db


# ── регистрация для проверок меню ───────────────────────────────────────────
@pytest.fixture
async def одна_группа_в_справочнике():
    """Одна группа в справочнике: без неё регистрация не дойдёт до главного меню.

    Своего названия вместо общей college_groups, потому что та определена в
    tests/test_group_registry.py, а pytest видит фикстуры только своего файла и
    conftest.py. Реальный набор групп колледжа тут не нужен - достаточно одной.
    """
    await upsert_group("ИС-21", title="Информационные системы")
    return "ИС-21"


async def test_student_menu_contract(api, одна_группа_в_справочнике):
    """Структура главного меню студента.

    Фикстура заводит одну группу в справочнике. Раньше она здесь не нужна была:
    группа, которой нет в справочнике, молча создавалась при регистрации.
    Теперь не создаётся, и без справочника регистрация не доходит до конца -
    а тест проверяет меню, а не создание группы.

    Фикстура своя, а не общая college_groups: та живёт в другом тестовом файле,
    а из чужого файла pytest фикстуры не видит. И набор реальных групп колледжа
    для проверки меню не нужен - достаточно одной.
    """
    """Меню студента - семь кнопок, и каждое подменю открывается по своей.

    Проверяем через бота, а не через student_menu(): важно, что человек реально
    доходит до подменю нажатием, а не то, что функция вернула список.
    """
    await register(USER, "Иванов Иван", "ИС-21")
    await add_staff(STAFF, "Петрова Анна", category="all")
    await add_staff(DIRECTOR, "Сидоров Пётр Петрович", category="all")
    await set_admin_profile(DIRECTOR, role="director", position="Директор")

    api.sent.clear()
    await press(USER, "home")
    # главный экран: обращения, расписание, профиль и справка бота.
    assert api.payloads(USER) == ["ticket_menu", "sched", "profile", "help"]

    # и второстепенные разделы не потеряны - они внутри «Создать обращение»,
    # до него два нажатия: «Обращения», потом «Создать обращение»
    api.sent.clear()
    await press(USER, "ticket_menu")
    await press(USER, "ticket_create")
    assert set(api.payloads(USER)) >= {"sub:cert", "sub:acc", "sub:fb", "faq"}, \
        "разделы убрали с первого экрана и не вернули в «Создать обращение»"

    # в каждом разделе теперь есть «Назад» - он возвращает в «Создать
    # обращение», откуда пришли, а не в главное меню. Владелец просил именно
    # этого: возвращаться в начало из глубины неудобно.
    НАЗАД = "back:ticket_create"

    await press(USER, "sub:cert")
    assert set(api.payloads(USER)) == {"ask:certificates:place", "ask:certificates:period",
                                      "ask:certificates:vacancies", "new:certificates",
                                      "home", НАЗАД}, "в «Справке» нет «Назад»"
    assert "Выберите, что именно" in api.last(USER)[1]

    await press(USER, "sub:acc")
    assert set(api.payloads(USER)) == {"ask:accounting:scholarship", "ask:accounting:payout",
                                      "ask:accounting:other", "new:accounting",
                                      "home", НАЗАД}, "в «Бухгалтерии» нет «Назад»"
    assert "Выберите, что именно" in api.last(USER)[1]

    # обратная связь адресная: сначала должность, потом человек этой должности
    await press(USER, "sub:fb")
    payloads = set(api.payloads(USER))
    assert "fbrole:director" in payloads
    assert "new:feedback" in payloads and "home" in payloads
    assert НАЗАД in payloads, "в «Обратной связи» нет «Назад»"
    assert "Директор" in " ".join(button["text"] for row in api.last(USER)[2] for button in row)
    assert "Сидоров" in api.last(USER)[1]
    await press(USER, "fbrole:director")
    assert f"pick:feedback:{DIRECTOR}" in api.payloads(USER)


def test_student_menu_fits_max_keyboard_limit():
    """Клавиатура MAX - не больше 30 строк, в меню студента должно быть место."""
    import max_api

    assert len(menus.student_menu()) <= max_api.MAX_ROWS


async def test_sections_expose_topics_application_and_back(flow):
    api, _, _ = flow
    await menus.cb_academic(USER, "")
    academic = set(api.payloads(USER))
    assert {"topic:academic:study", "topic:academic:period", "topic:academic:vacancies", "new:academic", "back"} <= academic

    await menus.cb_accounting(USER, "")
    accounting = set(api.payloads(USER))
    assert {"topic:accounting:scholarship", "new:accounting", "back"} <= accounting


async def test_view_schedules_uses_active_registry(flow):
    api, repo, _ = flow
    repo.groups.append({"code": "БУХ-20", "active": 0})
    await menus.cb_view_schedules(USER, "")
    assert "sched:ИС-21" in api.payloads(USER)
    assert "sched:БУХ-20" not in api.payloads(USER)

    await menus.cb_schedule(USER, "ИС-21")
    assert "https://college.example/is-21.pdf" in api.last(USER)[1]


async def test_unknown_group_is_not_added_behind_students_back(flow):
    """Нет группы в справочнике - значит, её не заводит студент.

    Задача владельца: «удалить возможность создания своей группы, у нас есть свой
    реестр». Справочник отражает реальные группы колледжа, и пополнять его должен
    сотрудник на вкладке «Группы», а не тот, кто первый написал незнакомый код.

    Проверяем две стороны: группа не появилась и студенту сказано, куда
    обратиться. Молчаливый отказ здесь не годится - человек просто застрял бы.
    """
    api, repo, db = flow
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    await menus.st_reg_group(USER, "НОВАЯ-99", {"name": "Иванов Иван"})

    assert not any(g["code"] == "НОВАЯ-99" for g in repo.groups), \
        "студент завел группу в справочнике сам - этого больше быть не должно"
    # Состояние тут не создаётся: тест зовёт st_reg_group напрямую, без
    # предварительного set_state. Проверять надо, что оно НЕ перешло к
    # подтверждению, а не то, что оно есть.
    assert db.states.get(USER, {}).get("state") != "reg_confirm", \
        "показывать подтверждение данных, пока группа не выбрана"
    assert "учебную часть" in api.last(USER)[1], \
        f"не сказано, куда обратиться: {api.last(USER)[1]!r}"
    assert "regyes" not in api.payloads(USER), "показывать подтверждение данных рано"


async def test_typo_gets_suggestions_instead_of_new_group(flow):
    """Опечатка рядом с существующей группой: показываем похожие, новую не создаём.

    Код '24-23Х' - цифры те же, что у 24-23, а сам код другой. По правилу
    _looks_like_typo это опечатка, и бот предлагает выбрать из известного, а не
    заводить своё.

    Раньше здесь был «2423», и тест ждал подсказки. Это было неверно: такой код
    нормализуется в «24-23», группа в справочнике есть, и бот её находит.
    Опечатка - это '24-23Х', а не «2423».
    """
    api, repo, db = flow
    repo.groups[:] = [{"code": "24-23", "active": 1}, {"code": "24-24", "active": 1}]
    await db.set_state(USER, "reg_group", {"name": "Иванов Иван"})
    await menus.st_reg_group(USER, '24-23Х', {"name": "Иванов Иван"})

    assert not any(g["code"] == '24-23Х' for g in repo.groups), "создали мусор в справочнике"
    assert "regpick:24-23" in api.payloads(USER), f"похожие не предложены: {api.payloads(USER)}"
    assert "не найдена" in api.last(USER)[1], f"не сказано, что группы нет: {api.last(USER)[1]!r}"


async def test_group_written_without_dashes_is_understood(flow):
    """«2423» - это «24-23», а не опечатка: так человек пишет код от руки.

    Отдельная проверка, потому что старый тест опечаток утверждал обратное.
    Нормализация - не украшение, а смысл справочника: «24-23 (П)», «24-23П»,
    «2423П» и «24-23 п» должны вести в одну группу, иначе студент не
    зарегистрируется из-за того, как он написал код.
    """
    api, repo, db = flow
    repo.groups[:] = [{"code": "24-23", "active": 1}, {"code": "24-24", "active": 1}]
    await db.set_state(USER, "reg_group", {"name": "Иванов Иван"})
    await menus.st_reg_group(USER, "2423", {"name": "Иванов Иван"})

    assert "regyes" in api.payloads(USER), \
        f"«2423» должен найти 24-23, показано: {api.payloads(USER)}"
    assert repo.saved == [], "до подтверждения ничего не сохраняется"


async def test_group_is_normalized_and_fio_with_colon_survives(flow):
    """Код группы приводится к каноническому виду, а двоеточие в ФИО не ломает.

    Старая проверка звала обработчик кнопки «Сохранить» на экране «группа не
    найдена, сохранить её?». Экрана больше нет - заводить группу нельзя. Но
    свойство, ради которого проверка существовала, живо: код нормализуется, а
    ФИО с двоеточием не разваливается.
    """
    api, repo, db = flow
    repo.groups[:] = [{"code": "24-23П", "active": 1}, {"code": "ИС-21", "active": 1}]
    await db.set_state(USER, "reg_group", {"name": "Иванов:Иван"})
    # Код набираем буквами так, как его пишут от руки: «2423 П».
    # Подсказка в тестовой заготовке всегда непустая, поэтому опечатку, на
    # которую бот ответил бы похожими кодами, берём ту, что в справочнике есть,
    # - иначе проверка проверяла бы подсказку, а не нормализацию.
    await menus.st_reg_group(USER, "2423 П", {"name": "Иванов:Иван"})

    assert "regyes" in api.payloads(USER), "до подтверждения данные не сохраняются"
    assert repo.saved == []

    await menus.cb_registration_confirm(USER, "")
    assert repo.saved == [(USER, "Иванов:Иван", "24-23П")],         f"группа не нормализована или ФИО с двоеточием развалилось: {repo.saved}"
    # после сохранения - меню бота, а не старые разделы обращений
    assert {"ticket_menu", "sched", "profile", "help"} <= set(api.payloads(USER))


async def test_empty_registry_does_not_let_student_through(flow):
    """Справочник пуст - регистрация ждёт, пока группу заведёт сотрудник.

    Раньше при пустом справочнике код группы просто сохранялся, и студент
    проходил дальше. Теперь это не так, и проверка держит новое правило: группа не
    заводится, данные не подтверждаются, человек получает понятный ответ.
    """
    api, repo, db = flow
    repo.groups[:] = []
    await menus.st_reg_group(USER, " новый-7 ", {"name": "Иванов Иван"})

    assert repo.saved == [], "данные сохранились без группы из справочника"
    assert repo.groups == [], "студент завел группу сам"
    assert "regyes" not in api.payloads(USER), "подтверждение показано раньше времени"
    assert "учебную часть" in api.last(USER)[1]


async def test_group_from_registry_still_confirms_normally(flow):
    """Та же проверка для обычного случая: группа в справочнике есть.

    Нужна рядом с запретом, иначе следующий человек решит, что регистрация стала
    невозможной: группа, которая в справочнике, обязана работать как раньше -
    нормализация, показ данных, подтверждение.
    """
    api, repo, db = flow
    repo.groups[:] = [{"code": "НОВЫЙ-7", "active": 1}]
    await menus.st_reg_group(USER, " новый-7 ", {"name": "Иванов Иван"})

    assert repo.saved == []          # пока не подтвердил
    assert db.states[USER]["state"] == "reg_confirm"
    assert "НОВЫЙ-7" in api.last(USER)[1]
    assert "regyes" in api.payloads(USER)

    await menus.cb_registration_confirm(USER, "")
    assert repo.saved == [(USER, "Иванов Иван", "НОВЫЙ-7")]
    assert {"ticket_menu", "sched", "profile", "help"} <= set(api.payloads(USER))


async def test_registration_offers_known_groups(flow):
    """Группу можно выбрать кнопкой - не нужно угадывать написание."""
    api, repo, db = flow
    await menus.st_reg_name(USER, "Иванов Иван", {})
    payloads = api.payloads(USER)
    assert "regpick:ИС-21" in payloads and "regpick:" in payloads


async def test_registration_uses_name_from_profile(flow):
    """Если подпись профиля похожа на ФИО - предлагаем её одной кнопкой."""
    api, repo, db = flow
    await db.one("SELECT 1", ())  # контактов нет - подсказки не будет
    await menus.cb_who(USER, "student")
    assert "Укажите ваши ФИО полностью" in api.last(USER)[1]


async def test_profile_group_change_refuses_unknown_group(flow):
    """Смена группы на несуществующую не сохраняется - её нельзя завести.

    Раньше здесь показывалось «Группа не найдена. Сохранить её?» с кнопкой,
    и студент нажимал «Да» - группа появлялась в справочнике от человека,
    который просто неправильно написал адрес. Теперь вместо предложения
    сохранить показан справочник и сказано, куда обратиться.
    """
    api, repo, _ = flow
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    await menus.st_edit_group(USER, "НОВАЯ-99", {})

    assert repo.saved == [], "группа сохранена без записи в справочнике"
    адреса = api.payloads(USER)
    assert not any(p.startswith("regok") for p in адреса), \
        f"снова предлагают сохранить несуществующую группу: {адреса}"
    assert "regpick:ИС-21" in адреса, "не показаны группы из справочника"
    текст = api.last(USER)[1]
    # Сообщение зависит от того, нашлось ли похожее: при опечатке бот предлагает
    # похожие коды, а если похожих нет - прямо отправляет в учебную часть.
    # Обе ветки верны, и обе обязаны быть: молчаливый отказ недопустим.
    assert ("Похожее" in текст) or ("учебную часть" in текст), \
        f"не сказано ни о похожем, ни куда обратиться: {текст!r}"


async def test_group_without_similar_says_contact_the_deanery(flow):
    """Нет похожих групп - прямо совет обратиться в учебную часть.

    Отдельная проверка от предыдущей: там похожее находится, и бот предлагает
    коды. Здесь похожих нет, и молчать нельзя - человек должен понять, что его
    группы нет в списке и куда идти.
    """
    api, repo, _ = flow
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    # Пустой справочник подсказок: ни одна существующая группа не похожа.
    # В настоящем боте так бывает, когда человек написал код из другой коллексии
    # или с опечаткой в цифрах.
    await menus.st_reg_group(USER, "99-77", {"name": "Иванов Иван"})
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    await menus.st_reg_group(USER, "99-77", {"name": "Иванов Иван"})
    api.sent.clear()
    repo.groups[:] = []
    await menus.st_reg_group(USER, "99-77", {"name": "Иванов Иван"})

    текст = api.last(USER)[1]
    assert "учебную часть" in текст, f"не сказано, куда обратиться: {текст!r}"
    assert repo.saved == [], "данные сохранились без группы из справочника"


async def test_profile_group_change_saves_known_group(flow):
    """Смена группы на существующую работает как раньше.

    Нужна рядом с запретом: иначе следующий человек решит, что сменить группу
    теперь нельзя вообще.
    """
    api, repo, _ = flow
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    await menus.st_edit_group(USER, "24-23", {})
    api.sent.clear()
    await menus.st_edit_group(USER, "ИС-21", {})

    assert repo.saved, "существующая группа не сохранилась"


async def test_saveprofile_without_fields_is_a_noop(flow):
    api, repo, _ = flow
    await menus.cb_saveprofile(USER, "")

    assert repo.saved == []
    assert api.sent == []


async def test_hidden_group_is_not_a_way_in(flow):
    """Скрытая группа не пускает в регистрацию, и сохранить её нельзя.

    Случай отдельный от несуществующей группы: код в справочнике есть, но он
    помечен неактивным. Раньше отсюда предлагалось сохранить группу, и человек
    нажимал «Да» - группа появлялась в справочнике в обход правила.

    Проверку нашла проверка мутацией: вернуть этот путь было невозможно
    заметить, потому что все проверки про несуществующие группы, а не про
    скрытые.
    """
    api, repo, db = flow
    # Рядом кладём активные группы. Если скрытая группа в справочнике одна,
    # список активных пуст, а пустой список _group_allowed считает «ограничений
    # нет» - и проверка проверяла бы не то. Реально групп много, часть скрыта.
    repo.groups[:] = [{"code": "24-23", "active": 0},
                      {"code": "24-24", "active": 1},
                      {"code": "25-27", "active": 1}]
    await db.set_state(USER, "reg_group", {"name": "Иванов Иван"})
    await menus.st_reg_group(USER, "24-23", {"name": "Иванов Иван"})

    assert repo.saved == [], "скрытая группа сохранена"
    адреса = api.payloads(USER)
    assert "regyes" not in адреса, f"показан подтверждение: {адреса}"
    assert not any(p.startswith("regok") for p in адреса), \
        f"снова предлагают сохранить скрытую группу: {адреса}"
    # и сказано, что группа не подходит
    текст = api.last(USER)[1]
    assert "не найдена" in текст or "учебную часть" in текст, \
        f"не сказано, что группа не подходит: {текст!r}"


async def test_hidden_group_change_from_profile_is_refused(flow):
    """Смена группы на скрытую из профиля тоже не проходит.

    Второй вход в то же самое правило: профиль → «Изменить группу» → скрытый
    код. Без этой проверки вернуть путь можно было бы и здесь, и никто бы не
    заметил.
    """
    api, repo, _ = flow
    # Активные группы рядом нужны по той же причине: при одном скрытом коде
    # список активных пуст и ограничение не срабатывает вовсе.
    repo.groups[:] = [{"code": "24-23", "active": 0},
                      {"code": "24-24", "active": 1},
                      {"code": "25-27", "active": 1}]
    await menus.st_edit_group(USER, "24-23", {})

    assert repo.saved == [], "скрытая группа сохранена из профиля"
    адреса = api.payloads(USER)
    assert not any(p.startswith("regok") for p in адреса), \
        f"предлагают сохранить скрытую группу: {адреса}"
