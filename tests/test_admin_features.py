import httpx
import pytest

import bot
import config
import database as db
from conftest import click, msg
from handlers import admin, common
from handlers.registry import CALLBACKS, STATES

SYS, SYS2, STAFF, STUDENT = "1", "2", "200", "300"
ROLES = ("director", "deputy_uvr", "deputy_upr", "deputy_unr", "social_pedagogue")


def admin_row(aid, name, role="", office="", role_type="staff", category="all", broadcast=0,
              position="", department=""):
    return {
        "id": aid, "user_id": aid, "full_name": name,
        "role": role, "role_type": role_type, "office": office,
        "position": position, "department": department,
        "ticket_category": category, "can_broadcast": broadcast,
    }


class FakeRepo:
    def __init__(self, groups=(), schedules=()):
        self.admins = {
            SYS: admin_row(SYS, "Сис-админ", "director", "101", role_type="sysadmin", broadcast=1),
            SYS2: admin_row(SYS2, "Второй Админ", role_type="sysadmin"),
            STAFF: admin_row(STAFF, "Петрова Анна"),
        }
        self.registry = [{"code": c, "active": a} for c, a in groups]
        self.schedules = dict(schedules)
        self.calls = []

    async def get_admin(self, user_id):
        return self.admins.get(str(user_id))

    async def set_admin_profile(self, admin_id, role="", office="", position="", department=""):
        self.calls.append(("set_admin_profile", str(admin_id), role, office))
        self.calls.append(("set_position", str(admin_id), position))
        self.calls.append(("set_department", str(admin_id), department))
        a = self.admins.get(str(admin_id))
        if a:
            if role:
                a["role"] = role
            if office:
                a["office"] = office
            if position:
                a["position"] = position
            if department:
                a["department"] = department

    async def clear_admin_fields(self, admin_id, *names):
        self.calls.append(("clear_admin_fields", str(admin_id)) + tuple(names))
        a = self.admins.get(str(admin_id))
        if a:
            for name in names:
                a[name] = ""

    async def list_staff(self):
        return [a for a in self.admins.values() if a["role_type"] == "staff"]

    async def staff_activity(self, days=30):
        return {}

    async def list_groups(self, active_only=False):
        rows = [{"code": g["code"], "active": g["active"]} for g in self.registry]
        return [r for r in rows if r["active"] or not active_only]

    async def upsert_group(self, group_code, title=None, active=None):
        self.calls.append(("upsert_group", group_code, title, active))
        for g in self.registry:
            if g["code"] == group_code:
                return
        self.registry.append({"code": group_code, "active": 1 if active is None else int(active)})

    async def set_group_active(self, group_code, active):
        self.calls.append(("set_group_active", group_code, active))
        for g in self.registry:
            if g["code"] == group_code:
                g["active"] = int(bool(active))

    async def delete_group(self, group_code):
        self.calls.append(("delete_group", group_code))
        self.registry = [g for g in self.registry if g["code"] != group_code]

    async def get_schedule(self, group_code):
        url = self.schedules.get(group_code)
        return {"group_code": group_code, "pdf_url": url} if url else None

    async def schedule_groups(self, limit=25):
        return [{"group_code": c} for c in sorted(self.schedules)[:limit]]

    async def upsert_schedule(self, group_code, pdf_url):
        self.calls.append(("upsert_schedule", group_code, pdf_url))
        self.schedules[group_code] = pdf_url

    async def delete_schedule(self, group_code):
        self.calls.append(("delete_schedule", group_code))
        self.schedules.pop(group_code, None)


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo(groups=[("ИС-21", 1), ("БУХ-20", 0)], schedules={"БУХ-20": "https://college.example/buh-20.pdf"})
    for module in (admin, common):
        monkeypatch.setattr(module, "repo", fake)
    return fake


def tap(payload, x=SYS, arg=""):
    return CALLBACKS[payload](x, arg)


async def state_of(x=SYS):
    st = await db.get_state(x)
    return (st["state"], st["payload"]) if st else (None, None)


async def say_state(x, text):
    name, payload = await state_of(x)
    return await STATES[name](x, text, payload)


def texts(api, uid):
    return "\n".join(m[1] for m in api.to(uid))


def pdf_probe(monkeypatch, handler):
    requests = []

    def transport(request):
        requests.append(request)
        return handler(request)

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True, timeout=8.0)

    monkeypatch.setattr(admin, "probe_client", client)
    return requests


# ── карточка сотрудника: должность, отдел и кабинет ───────────────────────────
async def test_staff_card_shows_role_office_and_buttons(api, repo):
    await tap("sf", arg=STAFF)
    text = api.last(SYS)[1]
    assert "Петрова Анна" in text and f"MAX ID: {STAFF}" in text
    assert "Должность: не назначена" in text and "Кабинет: —" in text
    assert {"sfr:%s" % STAFF, "sfo:%s" % STAFF, "sfdep:%s" % STAFF, "sft:%s" % STAFF} <= set(api.payloads(SYS))


async def test_sfr_asks_for_free_text_position(api, repo):
    await tap("sfr", arg=STAFF)
    assert await state_of() == ("staff_position", {"admin_id": STAFF})
    text = api.last(SYS)[1]
    assert "Введите должность" in text and "Преподаватель информатики" in text


async def test_sfr_saves_free_text_position_and_reopens_card(api, repo):
    await tap("sfr", arg=STAFF)
    await say_state(SYS, "  Преподаватель информатики ")
    assert ("set_position", STAFF, "Преподаватель информатики") in repo.calls
    assert await state_of() == (None, None)
    assert "Должность: Преподаватель информатики" in api.last(SYS)[1]


async def test_sfdep_saves_department_and_groups_list_by_it(api, repo):
    await tap("sfdep", arg=STAFF)
    assert await state_of() == ("staff_department", {"admin_id": STAFF})
    await say_state(SYS, "Учебная часть")
    assert ("set_department", STAFF, "Учебная часть") in repo.calls
    assert "Отдел: Учебная часть" in api.last(SYS)[1]
    await tap("admins")
    assert any("Учебная часть" in str(b.get("text", ""))
               for b in [btn for row in api.last(SYS)[2] for btn in row])


async def test_position_wins_over_role_preset_in_card(api, repo):
    repo.admins[STAFF]["position"] = "Главный бухгалтер"
    await tap("sf", arg=STAFF)
    assert "Должность: Главный бухгалтер" in api.last(SYS)[1]


async def test_sft_offers_all_five_roles_with_srset_payloads(api, repo):
    await tap("sft", arg=STAFF)
    payloads = api.payloads(SYS)
    assert all(f"srset:{STAFF}:{role}" in payloads for role in ROLES)
    assert f"srset:{STAFF}:-" in payloads


async def test_srset_updates_profile_and_reopens_card(api, repo):
    await tap("srset", arg=f"{STAFF}:deputy_uvr")
    assert ("set_admin_profile", STAFF, "deputy_uvr", "") in repo.calls
    assert "Должность: 👥 Заместитель директора по УВР" in api.last(SYS)[1]
    assert {"sfr:%s" % STAFF, "sfdel:%s" % STAFF} <= set(api.payloads(SYS))


async def test_srset_with_dash_clears_role(api, repo):
    await tap("srset", arg=f"{STAFF}:-")
    assert ("clear_admin_fields", STAFF, "role") in repo.calls
    assert "Должность: не назначена" in api.last(SYS)[1]


async def test_srset_ignores_unknown_role(api, repo):
    await tap("srset", arg=f"{STAFF}:president")
    assert not [c for c in repo.calls if c[0] == "set_admin_profile"]


async def test_sfo_sets_state_staff_office_with_admin_id(api, repo):
    await tap("sfo", arg=STAFF)
    assert await state_of() == ("staff_office", {"admin_id": STAFF})
    assert "Петрова Анна" in api.last(SYS)[1]


async def test_staff_office_state_saves_room_and_shows_card(api, repo):
    await tap("sfo", arg=STAFF)
    await say_state(SYS, " 214 ")
    assert ("set_admin_profile", STAFF, "", "214") in repo.calls
    assert await state_of() == (None, None)
    assert "Кабинет: 214" in api.last(SYS)[1]


async def test_office_is_saved_through_the_message_flow(api, repo):
    await tap("sfo", arg=STAFF)
    await bot.process(msg(SYS, "317"))
    assert ("set_admin_profile", STAFF, "", "317") in repo.calls
    assert "Кабинет: 317" in api.last(SYS)[1]


async def test_profile_buttons_work_through_the_full_update_flow(api, repo):
    await bot.process(click(SYS, f"sft:{STAFF}"))
    assert f"srset:{STAFF}:director" in api.payloads(SYS)
    await bot.process(click(SYS, f"srset:{STAFF}:director"))
    assert ("set_admin_profile", STAFF, "director", "") in repo.calls
    assert "Директор" in api.last(SYS)[1]


async def test_staff_profile_buttons_are_closed_for_outsiders(api, repo):
    await tap("sfr", arg=STAFF, x=STUDENT)
    await tap("sfo", arg=STAFF, x=STUDENT)
    assert api.to(STUDENT) == []


# ── справочник групп ──────────────────────────────────────────────────────────
async def test_groups_listing_shows_registry_and_actions(api, repo):
    await tap("groups")
    payloads = api.payloads(SYS)
    assert {"groupadd", "groupedit:ИС-21", "grouptoggle:ИС-21", "groupdel:ИС-21"} <= set(payloads)
    assert "groupedit:БУХ-20" in payloads  # скрытые группы тоже видны сис-админу


async def test_group_add_normalizes_code_and_saves(api, repo):
    await tap("groupadd")
    assert (await state_of())[0] == "group_add"
    await say_state(SYS, "  ив-22 ")
    assert ("upsert_group", "ИВ-22", None, None) in repo.calls
    assert "ИВ-22" in api.last(SYS)[1]


async def test_group_add_rejects_duplicate(api, repo):
    await tap("groupadd")
    await say_state(SYS, "ис-21")
    assert not [c for c in repo.calls if c[0] == "upsert_group"]
    assert "уже есть" in api.last(SYS)[1]


async def test_group_add_rejects_invalid_code(api, repo):
    await tap("groupadd")
    await say_state(SYS, "ИС 21")
    assert not [c for c in repo.calls if c[0] == "upsert_group"]
    assert "дефисов" in api.last(SYS)[1]


async def test_groupedit_sets_state_with_original_code(api, repo):
    await tap("groupedit", arg="ИС-21")
    assert await state_of() == ("group_edit", {"code": "ИС-21"})


async def test_group_edit_renames_group(api, repo):
    await tap("groupedit", arg="ИС-21")
    await say_state(SYS, "ис-22")
    assert ("upsert_group", "ИС-22", None, None) in repo.calls
    assert ("delete_group", "ИС-21") in repo.calls
    assert "ИС-21" in api.last(SYS)[1] and "ИС-22" in api.last(SYS)[1]


async def test_group_edit_refuses_existing_code(api, repo):
    await tap("groupedit", arg="ИС-21")
    await say_state(SYS, "БУХ-20")
    assert not [c for c in repo.calls if c[0] in ("upsert_group", "delete_group")]
    assert "уже есть" in api.last(SYS)[1]


async def test_group_toggle_flips_active_flag(api, repo):
    await tap("grouptoggle", arg="ИС-21")
    assert ("set_group_active", "ИС-21", False) in repo.calls
    await tap("grouptoggle", arg="ИС-21")
    assert ("set_group_active", "ИС-21", True) in repo.calls


async def test_group_delete_existing_and_unknown(api, repo):
    await tap("groupdel", arg="НЕТ-99")
    assert not [c for c in repo.calls if c[0] == "delete_group"]
    await tap("groupdel", arg="ИС-21")
    assert ("delete_group", "ИС-21") in repo.calls
    assert "удалена" in api.last(SYS)[1]


async def test_groups_registry_is_closed_for_students(api, repo):
    await tap("groups", x=STUDENT)
    await tap("groupadd", x=STUDENT)
    assert api.to(STUDENT) == []


class LegacyRepo(FakeRepo):
    list_groups = None
    groups = FakeRepo.list_groups


async def test_registry_falls_back_to_groups_lister(api, repo, monkeypatch):
    legacy = LegacyRepo(groups=[("ИС-21", 1)], schedules={})
    for module in (admin, common):
        monkeypatch.setattr(module, "repo", legacy)
    await tap("groups")
    assert "groupedit:ИС-21" in api.payloads(SYS)


# ── расписания ────────────────────────────────────────────────────────────────
def pdf_response(request):
    return httpx.Response(206, content=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3", headers={"content-type": "application/pdf"})


async def test_schedule_group_chooser_unites_schedules_and_registry(api, repo):
    await tap("scadd")
    payloads = api.payloads(SYS)
    assert (await state_of())[0] == "sc_group"
    assert "scaddg:БУХ-20" in payloads  # есть расписание
    assert "scaddg:ИС-21" in payloads  # есть только в справочнике


async def test_scaddg_sets_add_sched_state_with_group(api, repo):
    await tap("scaddg", arg="ис-21")
    assert await state_of() == ("add_sched", {"group": "ИС-21"})


async def test_group_code_then_url_sets_add_sched_state(api, repo):
    await tap("scadd")
    await say_state(SYS, " иф-23 ")
    assert await state_of() == ("add_sched", {"group": "ИФ-23"})


async def test_group_code_is_validated(api, repo):
    await tap("scadd")
    await say_state(SYS, "ИФ 23")
    assert (await state_of())[0] == "sc_group"
    assert "дефисов" in api.last(SYS)[1]


async def test_scedit_sets_update_sched_state_with_group(api, repo):
    await tap("scedit", arg="БУХ-20")
    assert await state_of() == ("update_sched", {"group": "БУХ-20"})


async def test_add_sched_saves_verified_pdf_link(api, repo, monkeypatch):
    requests = pdf_probe(monkeypatch, pdf_response)
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "https://college.example/is-21.pdf")
    assert ("upsert_schedule", "ИС-21", "https://college.example/is-21.pdf") in repo.calls
    assert requests[0].headers["Range"] == "bytes=0-2047"  # большой файл не качаем
    assert admin.PROBE_TIMEOUT == 8
    assert "сохранена" in texts(api, SYS)
    assert await state_of() == (None, None)


async def test_update_sched_saves_verified_pdf_link(api, repo, monkeypatch):
    pdf_probe(monkeypatch, pdf_response)
    await tap("scedit", arg="БУХ-20")
    await say_state(SYS, "https://college.example/buh-20-v2.pdf")
    assert ("upsert_schedule", "БУХ-20", "https://college.example/buh-20-v2.pdf") in repo.calls


async def test_add_sched_keeps_state_when_link_is_not_pdf(api, repo, monkeypatch):
    pdf_probe(monkeypatch, lambda request: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "https://college.example/is-21")
    assert not [c for c in repo.calls if c[0] == "upsert_schedule"]
    assert "не сохранена" in api.last(SYS)[1]
    assert (await state_of())[0] == "add_sched"  # ввод можно повторить


async def test_add_sched_rejects_bad_scheme(api, repo, monkeypatch):
    requests = pdf_probe(monkeypatch, pdf_response)
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "ftp://college.example/is-21.pdf")
    assert requests == [] and not [c for c in repo.calls if c[0] == "upsert_schedule"]
    assert "https://" in api.last(SYS)[1]


@pytest.mark.parametrize("url", [
    "http://localhost/is-21.pdf",
    "http://127.0.0.1/is-21.pdf",
    "http://10.0.0.5/is-21.pdf",
    "http://192.168.1.7/is-21.pdf",
    "http://169.254.169.254/is-21.pdf",
    "http://[::1]/is-21.pdf",
])
async def test_add_sched_rejects_private_and_loopback_hosts(api, repo, monkeypatch, url):
    requests = pdf_probe(monkeypatch, pdf_response)
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, url)
    assert requests == [] and not [c for c in repo.calls if c[0] == "upsert_schedule"]
    assert "локальный" in api.last(SYS)[1]


async def test_add_sched_rejects_unreachable_host(api, repo, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route to host")

    pdf_probe(monkeypatch, boom)
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "https://college.example/is-21.pdf")
    assert not [c for c in repo.calls if c[0] == "upsert_schedule"]
    assert "не открывается" in api.last(SYS)[1]


async def test_add_sched_rejects_http_error(api, repo, monkeypatch):
    pdf_probe(monkeypatch, lambda request: httpx.Response(404, content=b"not found"))
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "https://college.example/is-21.pdf")
    assert not [c for c in repo.calls if c[0] == "upsert_schedule"]
    assert "404" in api.last(SYS)[1]


async def test_schedule_changes_notify_other_sysadmins(api, repo, monkeypatch):
    monkeypatch.setattr(config, "SYSADMIN_IDS", [SYS, SYS2])
    pdf_probe(monkeypatch, pdf_response)
    await tap("scaddg", arg="ИС-21")
    await say_state(SYS, "https://college.example/is-21.pdf")
    assert any("ИС-21" in m[1] for m in api.to(SYS2))

    api.sent.clear()
    await tap("scdel", arg="ИС-21")
    assert ("delete_schedule", "ИС-21") in repo.calls
    assert any("удалил" in m[1] for m in api.to(SYS2))


async def test_schedule_delete_requires_existing_row(api, repo):
    await tap("scdel", arg="НЕТ-99")
    assert not [c for c in repo.calls if c[0] == "delete_schedule"]
    assert "не найдено" in api.last(SYS)[1]
