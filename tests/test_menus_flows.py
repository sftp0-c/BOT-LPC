import pytest

from handlers import menus


USER = "100"


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
        """Подсказки групп при регистрации: берём из справочника."""
        return [{"group_code": row["code"]} for row in self.groups if row["active"]][:limit]


@pytest.fixture
def flow(monkeypatch):
    api = FakeAPI()
    repo = FakeRepo(schedules={"ИС-21": "https://college.example/is-21.pdf"})
    db = FakeDB()
    monkeypatch.setattr(menus, "api", api)
    monkeypatch.setattr(menus, "repo", repo)
    monkeypatch.setattr(menus, "db", db)
    return api, repo, db


def test_student_menu_contract():
    payloads = [button["payload"] for row in menus.student_menu() for button in row]
    assert {"academic", "accounting", "new:feedback", "view_schedules", "profile"} <= set(payloads)
    assert "new:certificates" not in payloads


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


async def test_unknown_group_waits_for_confirmation(flow):
    api, repo, db = flow
    repo.groups[:] = [{"code": "ИС-21", "active": 1}]
    await menus.st_reg_group(USER, "НОВАЯ-99", {"name": "Иванов Иван"})

    assert repo.saved == []
    payloads = api.payloads(USER)
    assert "regok:НОВАЯ-99:Иванов Иван" in payloads
    assert "editname" in payloads
    assert db.states[USER]["state"] == "reg_group"


async def test_regok_saves_normalized_group_and_preserves_colon(flow):
    api, repo, _ = flow
    await menus.cb_regok(USER, "НОВАЯ-99:Иванов:Иван")

    assert repo.saved == [(USER, "Иванов:Иван", "НОВАЯ-99")]
    payloads = set(api.payloads(USER))
    assert {"academic", "accounting", "new:feedback", "view_schedules", "profile"} <= payloads


async def test_empty_registry_accepts_normalized_group(flow):
    """Группа нормализуется, а перед сохранением человек её подтверждает."""
    api, repo, db = flow
    repo.groups[:] = []
    await menus.st_reg_group(USER, " новый-7 ", {"name": "Иванов Иван"})

    assert repo.saved == []          # пока не подтвердил
    assert db.states[USER]["state"] == "reg_confirm"
    assert "НОВЫЙ-7" in api.last(USER)[1]
    assert "regyes" in api.payloads(USER)

    await menus.cb_registration_confirm(USER, "")
    assert repo.saved == [(USER, "Иванов Иван", "НОВЫЙ-7")]
    assert {"academic", "accounting", "new:feedback", "view_schedules", "profile"} <= set(api.payloads(USER))


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


async def test_profile_group_confirmation_does_not_save_until_regok(flow):
    api, repo, _ = flow
    await menus.st_edit_group(USER, "НОВАЯ-99", {})

    assert repo.saved == []
    assert "regok:НОВАЯ-99:Иванов Иван" in api.payloads(USER)


async def test_saveprofile_without_fields_is_a_noop(flow):
    api, repo, _ = flow
    await menus.cb_saveprofile(USER, "")

    assert repo.saved == []
    assert api.sent == []
