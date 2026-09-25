import database as db
import repository as repo
from handlers import admin, menus


URL = "https://college.example/is-21.pdf"
UPDATED_URL = "https://college.example/is-21-v2.pdf"


async def test_schedule_subscription_toggles_and_normalizes(env):
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.upsert_schedule(" ис-21 ", URL)

    assert not await repo.is_schedule_subscribed("100", "ИС-21")

    await repo.set_schedule_subscription("100", "  ис-21  ")
    assert await repo.is_schedule_subscribed("100", "ИС-21")
    assert (await db.one("SELECT group_code FROM schedule_subscriptions WHERE user_id=?", ("100",)))["group_code"] == "ИС-21"

    await repo.delete_schedule_subscription("100")
    assert not await repo.is_schedule_subscribed("100", "ИС-21")


async def test_schedule_subscription_callback_toggles(api, env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.upsert_schedule("ИС-21", URL)

    await menus.cb_schedsub("100", " ис-21 ")
    assert await repo.is_schedule_subscribed("100", "ИС-21")
    assert any("подписались" in text for _, text, _ in api.to("100"))

    await menus.cb_schedsub("100", "ИС-21")
    assert not await repo.is_schedule_subscribed("100", "ИС-21")
    assert any("отписались" in text for _, text, _ in api.to("100"))


async def test_view_schedules_unites_active_groups_and_schedules(api, env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.upsert_group("ИС-21")
    await repo.upsert_group("АКТ-22", active=False)
    await repo.upsert_schedule("БУХ-20", URL)
    await repo.upsert_schedule("АП-23", URL)

    await menus.cb_view_schedules("100", "")

    payloads = api.payloads("100")
    assert "sched:АКТ-22" not in payloads
    assert {"sched:ИС-21", "sched:БУХ-20", "sched:АП-23"} <= set(payloads)
    assert {"schedsub:ИС-21", "schedsub:БУХ-20", "schedsub:АП-23"} <= set(payloads)


async def test_schedule_update_and_delete_notify_subscribers(api, env, monkeypatch):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await repo.upsert_schedule("ИС-21", URL)
    await repo.set_schedule_subscription("100", "ИС-21")

    async def probe(url):
        return True, ""

    monkeypatch.setattr(admin, "probe_pdf_url", probe)
    await admin.save_schedule("1", "ис-21", UPDATED_URL, edit=True)
    assert any("Расписание группы ИС-21 обновлено" in text and UPDATED_URL in text for _, text, _ in api.to("100"))

    await admin.cb_schedule_delete("1", "ис-21")
    assert any("Расписание группы ИС-21 удалено" in text for _, text, _ in api.to("100"))


async def test_rename_group_updates_all_group_data(api, env):
    await repo.upsert_user("100", "Иванов Иван", "ис-21")
    await repo.upsert_schedule("ИС-21", URL)
    await repo.set_schedule_subscription("100", "ИС-21")

    assert await repo.rename_group("ис-21", "ИФ-22")
    assert (await repo.get_user("100"))["group_code"] == "ИФ-22"
    assert (await repo.get_schedule("ИФ-22"))["pdf_url"] == URL
    assert await repo.is_schedule_subscribed("100", "ИФ-22")
    assert await repo.get_group("ИС-21") is None
    assert (await repo.get_group("ИФ-22"))["active"] == 1

    await repo.upsert_group("ИС-21")
    assert not await repo.rename_group("ИФ-22", "ИС-21")
    assert (await repo.get_user("100"))["group_code"] == "ИФ-22"


async def test_audience_ids_matches_normalized_legacy_group_codes(env):
    await repo.upsert_user("100", "Иванов Иван", "ИС-21")
    await db.run(
        "INSERT INTO users(user_id, full_name, group_code) VALUES(?,?,?)",
        ("101", "Петров Пётр", "  ис-21  "),
    )
    await repo.upsert_user("102", "Сидоров Сидор", "БУХ-20")

    assert set(await repo.audience_ids("ис-21")) == {"100", "101"}
    assert set(await repo.audience_ids("all")) == {"100", "101", "102"}
