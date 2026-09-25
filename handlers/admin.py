"""Панель сис-админа: сотрудники, справочник групп, расписания, настройки, статистика."""
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

import config
import database as db
import repository as repo
from handlers.common import BACK, DEFAULT_WELCOME, admin_of, api, is_super, log, need_super, notify, spawn
from handlers.registry import callback, state
from max_api import btn
from utils import STAFF_CATS, STATUS, as_str, norm_group, short, tail_file, valid_group


# ── общие мелочи для строк из БД ─────────────────────────────────────────────
def _field(row, *names: str, default: str = "") -> str:
    for name in names:
        try:
            value = row[name]
        except (KeyError, IndexError, TypeError):
            continue
        if value is not None:
            return as_str(value)
    return default


def _flag(value) -> bool:
    return as_str(value).strip().lower() in ("1", "true", "yes", "on")


def staff_id_of(a) -> str:
    return _field(a, "id", "user_id", "admin_id")


def role_of(a) -> str:
    return _field(a, "role", "role_type", "position")


def office_of(a) -> str:
    return _field(a, "office", "room")


# ── должности сотрудников ────────────────────────────────────────────────────
STAFF_ROLES = {
    "director": "👔 Директор",
    "deputy_uvr": "👥 Заместитель директора по УВР",
    "deputy_upr": "👥 Заместитель директора по УПР",
    "deputy_unr": "👥 Заместитель директора по УНР",
    "social_pedagogue": "🧑‍🏫 Социальный педагог",
}


def role_text(a) -> str:
    return STAFF_ROLES.get(role_of(a), "не назначена")


def sysadmin_ids() -> list[str]:
    return [str(i) for i in (config.SYSADMIN_IDS or [])]


async def audit(actor: str, text: str) -> None:
    for uid in sysadmin_ids():
        if uid != str(actor):
            await notify(uid, f"🔔 {text}")


async def notify_schedule_subscribers(group: str, text: str) -> None:
    getter = getattr(repo, "schedule_subscribers", None)
    if getter is None:
        return
    try:
        subscribers = await getter(norm_group(as_str(group)))
    except Exception:
        return
    for user_id in subscribers or []:
        try:
            await notify(user_id, text)
        except Exception:
            continue


# ── статистика ────────────────────────────────────────────────────────────────
def status_counts_line(counts: dict) -> str:
    return "\n".join(f"{label}: {counts.get(code, 0)}" for code, label in STATUS.items())


@callback("stats")
async def cb_stats(x, arg):
    if not await need_super(x):
        return
    st = await repo.stats_overview()
    counts = status_counts_line(await repo.status_counts())
    await api.send(
        x,
        f"📊 Статистика\nСтудентов: {st['students']}\nСотрудников: {st['staff']}\n"
        f"Обращений всего: {st['total']} (за 7 дней: {st['week']})\n\n{counts}",
        BACK,
    )


@callback("staffstats")
async def cb_staff_stats(x, arg):
    if not await admin_of(x):
        return
    counts = status_counts_line(await repo.status_counts(x))
    await api.send(x, f"📊 Ваши обращения\n{counts}", BACK)


# ── сотрудники ────────────────────────────────────────────────────────────────
@callback("admins")
async def cb_admins(x, arg):
    if not await need_super(x):
        return
    rows = await repo.list_staff()
    text = "👥 Сотрудники колледжа" if rows else "👥 Сотрудников пока нет. Добавьте первого."
    kb = [[btn(f"{short(r['full_name'], 30)} · ID {r['user_id']} · {STAFF_CATS[r['ticket_category']]}", f"sf:{r['user_id']}")] for r in rows]
    await api.send(x, text, [*kb, [btn("➕ Добавить сотрудника", "sfadd")], *BACK])


async def send_staff_card(x: str, staff_id: str):
    a = await admin_of(staff_id)
    if not a or is_super(a):
        return await api.send(x, "Сотрудник не найден.", [[btn("↩️ К списку", "admins")]])
    sid = staff_id_of(a) or as_str(staff_id)
    cat = _field(a, "ticket_category", default="all") or "all"
    text = (
        f"👤 {_field(a, 'full_name')}\nMAX ID: {sid}\n"
        f"Должность: {role_text(a)}\nКабинет: {office_of(a) or '—'}\n"
        f"Обращения: {STAFF_CATS.get(cat, cat)}\n"
        f"Рассылка: {'разрешена' if _flag(_field(a, 'can_broadcast', default='0')) else 'запрещена'}"
    )
    cat_row = [btn(("● " if code == cat else "") + label, f"sfc:{sid}:{code}")
               for code, label in STAFF_CATS.items()]
    cat_rows = [cat_row[i:i + 2] for i in range(0, len(cat_row), 2)]
    await api.send(
        x,
        text,
        [
            [btn("✏️ Должность", f"sfr:{sid}"), btn("🏢 Кабинет", f"sfo:{sid}")],
            *cat_rows,
            [btn("📢 Рассылка: " + ("запретить" if _flag(_field(a, "can_broadcast", default="0")) else "разрешить"), f"sfb:{sid}")],
            [btn("🗑 Удалить", f"sfd:{sid}"), btn("↩️ К списку", "admins")],
        ],
    )


@callback("sf")
async def cb_staff_card(x, arg):
    if await need_super(x):
        await send_staff_card(x, arg)


@callback("sfr")
async def cb_staff_role_pick(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    current = role_of(a)
    await api.send(
        x,
        f"Должность сотрудника {_field(a, 'full_name')}:",
        [
            [btn(("● " if code == current else "") + label, f"srset:{staff_id_of(a) or as_str(arg)}:{code}")]
            for code, label in STAFF_ROLES.items()
        ],
    )


@callback("srset")
async def cb_staff_role_set(x, arg):
    sid, _, role = arg.partition(":")
    a = await admin_of(sid)
    if await need_super(x) and a and not is_super(a) and role in STAFF_ROLES:
        await repo.set_admin_profile(sid, role=role)
        await send_staff_card(x, sid)


@callback("sfo")
async def cb_staff_office_ask(x, arg):
    if not await need_super(x):
        return
    a = await admin_of(arg)
    if not a or is_super(a):
        return await send_staff_card(x, arg)
    await db.set_state(x, "staff_office", {"admin_id": staff_id_of(a) or as_str(arg)})
    await api.send(x, f"Отправьте кабинет сотрудника {_field(a, 'full_name')} (например, 214) или /cancel.")


@state("staff_office")
async def st_staff_office(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    sid = as_str((p or {}).get("admin_id"))
    a = await admin_of(sid)
    if not a or is_super(a):
        await db.clear_state(x)
        return await send_staff_card(x, sid)
    office = short(text, 100)
    if not office:
        return await api.send(x, "Введите кабинет сотрудника.")
    await repo.set_admin_profile(sid, office=office)
    await db.clear_state(x)
    await send_staff_card(x, sid)


@callback("sfc")
async def cb_staff_category(x, arg):
    staff_id, _, cat = arg.partition(":")
    a = await admin_of(staff_id)
    if await need_super(x) and a and not is_super(a) and cat in STAFF_CATS:
        await repo.set_staff_category(staff_id, cat)
        await send_staff_card(x, staff_id)


@callback("sfb")
async def cb_staff_broadcast(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await repo.set_staff_broadcast(arg, not _flag(_field(a, "can_broadcast", default="0")))
        await send_staff_card(x, arg)


@callback("sfd")
async def cb_staff_delete_ask(x, arg):
    a = await admin_of(arg)
    if await need_super(x) and a and not is_super(a):
        await api.send(x, f"Удалить сотрудника {_field(a, 'full_name')}?",
                       [[btn("🗑 Да, удалить", f"sfdy:{arg}"), btn("Отмена", f"sf:{arg}")]])


@callback("sfdy")
async def cb_staff_delete(x, arg):
    a = await admin_of(arg)
    if not (await need_super(x) and a and not is_super(a)):
        return
    open_n = await repo.open_tickets_count(arg)
    if open_n:
        return await api.send(
            x,
            f"У сотрудника есть открытые обращения ({open_n}). Закройте их (сис-админ может менять статусы) и повторите.",
            [[btn("↩️ К списку", "admins")]],
        )
    await repo.delete_staff(arg)
    await api.send(x, "✅ Сотрудник удалён.", [[btn("↩️ К списку", "admins")]])


@callback("sfadd")
async def cb_staff_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "add_staff_id")
    await api.send(x, "Введите MAX ID сотрудника (цифры). Сотрудник может узнать свой ID, написав боту /id. (или /cancel)")


@state("add_staff_id")
async def st_add_staff_id(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    staff_id = text.strip()
    if not staff_id.isdigit():
        return await api.send(x, "ID состоит только из цифр. Попробуйте ещё раз.")
    if await admin_of(staff_id):
        await db.clear_state(x)
        return await api.send(x, "Этот пользователь уже в списке сотрудников/сис-админов.", [[btn("↩️ К списку", "admins")]])
    await db.set_state(x, "add_staff_name", {"id": staff_id})
    await api.send(x, "Введите ФИО сотрудника (как его увидят студенты).")


@state("add_staff_name")
async def st_add_staff_name(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    name = short(text, 100)
    if not name:
        return await api.send(x, "Введите ФИО сотрудника.")
    await repo.add_staff(p["id"], name)
    await db.clear_state(x)
    delivered = await notify(p["id"], "🏫 Вас назначили сотрудником колледжа в этом боте. Отправьте /start, чтобы открыть кабинет.")
    await api.send(x, "✅ Сотрудник добавлен." + ("" if delivered else "\n⚠️ Он пока не запускал бота — пусть нажмёт «Начать»."))
    await send_staff_card(x, p["id"])


# ── справочник групп ──────────────────────────────────────────────────────────
GROUP_HINT = ("Код группы состоит из букв, цифр, дефисов и точек (без пробелов), до 30 символов.\n"
              "Например: ИС-21. Попробуйте ещё раз.")


async def list_groups(active_only: bool = False):
    lister = getattr(repo, "list_groups", None) or getattr(repo, "groups", None)
    return await lister(active_only=active_only) if lister is not None else []


async def all_groups() -> list[tuple[str, bool]]:
    seen: dict[str, bool] = {}
    for r in await (list_groups(active_only=False) or []):
        code = norm_group(_field(r, "code", "group_code", "group"))
        if code:
            seen[code] = _flag(_field(r, "active", "is_active", default="1"))
    return sorted(seen.items())


async def send_groups(x: str, note: str = ""):
    rows = await all_groups()
    kb = [[btn(f"{'🟢' if active else '⚪'} {code}", f"groupedit:{code}"),
           btn("🔄 Скрыть" if active else "👁 Показать", f"grouptoggle:{code}"),
           btn("🗑 Удалить", f"groupdel:{code}")] for code, active in rows]
    text = f"👥 Группы в справочнике: {len(rows)}" if rows else "👥 Справочник групп пуст. Добавьте первую группу."
    await api.send(x, "\n".join(part for part in (text, note) if part),
                   [*kb, [btn("➕ Добавить группу", "groupadd")], *BACK])


@callback("groups")
async def cb_groups(x, arg):
    if await need_super(x):
        await send_groups(x)


@callback("groupadd")
async def cb_group_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "group_add")
    await api.send(x, "Введите код группы, например ИС-21 (или /cancel).")


@state("group_add")
async def st_group_add(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    code = norm_group(text)
    if not valid_group(code):
        return await api.send(x, GROUP_HINT)
    if code in dict(await all_groups()):
        return await api.send(x, f"Группа {code} уже есть в справочнике.")
    await repo.upsert_group(code)
    await db.clear_state(x)
    await send_groups(x, f"✅ Группа {code} добавлена.")


@callback("groupedit")
async def cb_group_edit(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    if code not in dict(await all_groups()):
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    await db.set_state(x, "group_edit", {"code": code})
    await api.send(x, f"Код группы: {code}\nОтправьте новый код группы (например, ИС-22) или «-», чтобы ничего не менять.")


@state("group_edit")
async def st_group_edit(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    old = norm_group(as_str((p or {}).get("code")))
    if text.strip() in ("-", "—", "0", "не менять", "без изменений"):
        await db.clear_state(x)
        return await api.send(x, f"Код группы не изменился: {old}.")
    code = norm_group(text)
    if not valid_group(code):
        return await api.send(x, GROUP_HINT)
    rows = dict(await all_groups())
    if old not in rows:
        await db.clear_state(x)
        return await send_groups(x, f"⚠️ Группа {old} не найдена в справочнике.")
    if code == old:
        await db.clear_state(x)
        return await send_groups(x, f"Код группы не изменился: {old}.")
    if code in rows:
        return await api.send(x, f"Группа {code} уже есть в справочнике. Введите другой код.")
    renamer = getattr(repo, "rename_group", None)
    if renamer is None:
        await repo.upsert_group(code)
        await repo.delete_group(old)
    else:
        renamed = await renamer(old, code)
        if renamed is False:
            return await api.send(x, f"⚠️ Не удалось переименовать группу {old} → {code}.")
    await db.clear_state(x)
    await send_groups(x, f"✅ Группа переименована: {old} → {code}.")


@callback("grouptoggle")
async def cb_group_toggle(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    rows = dict(await all_groups())
    if code not in rows:
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    active = not rows[code]
    await repo.set_group_active(code, active)
    await send_groups(x, f"{'👁 Группа' if active else '⚪ Группа'} {code} {'активна' if active else 'скрыта'}.")


@callback("groupdel")
async def cb_group_delete(x, arg):
    if not await need_super(x):
        return
    code = norm_group(arg)
    if code not in dict(await all_groups()):
        return await send_groups(x, f"⚠️ Группа {code} не найдена в справочнике.")
    await repo.delete_group(code)
    await send_groups(x, f"🗑 Группа {code} удалена из справочника.")


# ── проверка ссылки на расписание ─────────────────────────────────────────────
PROBE_TIMEOUT = 8.0
PROBE_BYTES = 2048
PDF_TYPES = ("application/pdf", "application/octet-stream", "binary")
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".arpa")


def probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(PROBE_TIMEOUT))


def host_allowed(host) -> bool:
    h = as_str(host).strip().lower().strip("[]")
    if not h or h == "localhost" or h.endswith(BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def probe_pdf_url(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "Нужна ссылка вида https://… (http или https)."
    if not host_allowed(parsed.hostname):
        return False, "Этот адрес проверять нельзя: локальный или служебный хост."
    try:
        async with probe_client() as client:
            async with client.stream("GET", url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"}) as resp:
                status = resp.status_code
                ctype = resp.headers.get("content-type", "").lower()
                async for _ in resp.aiter_bytes():
                    break
    except Exception as exc:
        log.warning("не удалось проверить ссылку %s: %s", url, exc)
        return False, "Ссылка не открывается: сервер недоступен или адрес неверный."
    if status not in (200, 206):
        return False, f"Сервер ответил кодом {status} — по ссылке нет файла."
    if not any(t in ctype for t in PDF_TYPES) and not parsed.path.lower().endswith(".pdf"):
        return False, "По ссылке отдаётся не PDF-файл. Нужна прямая ссылка на .pdf."
    return True, ""


# ── расписания ────────────────────────────────────────────────────────────────
async def schedule_group_codes() -> list[str]:
    codes = {norm_group(_field(r, "group_code", "code")) for r in (await repo.schedule_groups() or [])}
    codes |= {code for code, _ in await all_groups()}
    return sorted(codes - {""})


@callback("schedules")
async def cb_schedules(x, arg):
    if not await need_super(x):
        return
    rows = await repo.schedule_groups()
    text = f"📅 Расписания групп: {len(rows)}" if rows else "📅 Расписаний пока нет."
    kb = [[btn(r["group_code"], f"sc:{r['group_code']}")] for r in rows]
    await api.send(x, text, [*kb, [btn("➕ Добавить / изменить", "scadd")], *BACK])


@callback("sc")
async def cb_schedule_card(x, group):
    row = await repo.get_schedule(group)
    if not (await need_super(x) and row):
        return
    await api.send(
        x,
        f"📅 {row['group_code']}\n{row['pdf_url']}",
        [[btn("✏️ Изменить ссылку", f"scedit:{group}"), btn("🗑 Удалить", f"scdel:{group}")], [btn("↩️ К списку", "schedules")]],
    )


@callback("scdel")
async def cb_schedule_delete(x, group):
    if not await need_super(x):
        return
    code = norm_group(as_str(group))
    if not await repo.get_schedule(code):
        return await api.send(x, f"Расписание группы {code} не найдено.", [[btn("↩️ К списку", "schedules")]])
    await repo.delete_schedule(code)
    await notify_schedule_subscribers(code, f"🗑 Расписание группы {code} удалено.")
    await api.send(x, f"🗑 Расписание группы {code} удалено.")
    await audit(x, f"Сис-админ {x} удалил расписание группы {code}.")
    await cb_schedules(x, "")


@callback("scadd")
async def cb_schedule_add(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "sc_group")
    codes = await schedule_group_codes()
    kb = [[btn(code, f"scaddg:{code}")] for code in codes]
    text = "Введите код группы, например ИС-21, или выберите группу из списка (или /cancel)."
    await api.send(x, text, [*kb, [btn("↩️ К расписаниям", "schedules")]])


@state("sc_group")
async def st_sc_group(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(text)
    if not valid_group(group):
        return await api.send(x, GROUP_HINT)
    await db.set_state(x, "add_sched", {"group": group})
    await api.send(x, f"Отправьте ссылку на расписание группы {group} (http/https). Проверим, что это PDF.")


@callback("scaddg")
async def cb_schedule_add_group(x, arg):
    if not await need_super(x):
        return
    await db.set_state(x, "add_sched", {"group": norm_group(arg)})
    await api.send(x, f"Отправьте ссылку на расписание группы {norm_group(arg)} (http/https). Проверим, что это PDF.")


@callback("scedit")
async def cb_schedule_edit(x, group):
    if not await need_super(x):
        return
    code = norm_group(group)
    await db.set_state(x, "update_sched", {"group": code})
    await api.send(x, f"Отправьте новую ссылку на расписание группы {code} (http/https). Проверим, что это PDF.")


async def save_schedule(x, group: str, url: str, edit: bool) -> None:
    code = norm_group(as_str(group))
    ok, reason = await probe_pdf_url(url)
    if not ok:
        return await api.send(x, f"⚠️ {reason}\nСсылка не сохранена — попробуйте ещё раз.")
    await repo.upsert_schedule(code, url)
    await notify_schedule_subscribers(code, f"📅 Расписание группы {code} обновлено\n{url}")
    await db.clear_state(x)
    await api.send(x, f"✅ Ссылка на расписание группы {code} сохранена.")
    await audit(x, f"Сис-админ {x} {'изменил' if edit else 'добавил'} ссылку на расписание группы {code}.")
    await cb_schedules(x, "")


@state("add_sched")
async def st_add_sched(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(as_str((p or {}).get("group")))
    if not valid_group(group):
        await db.clear_state(x)
        return await api.send(x, "Не задан код группы. Начните заново: «➕ Добавить / изменить».", BACK)
    await save_schedule(x, group, text.strip(), edit=False)


@state("update_sched")
async def st_update_sched(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    group = norm_group(as_str((p or {}).get("group")))
    if not valid_group(group):
        await db.clear_state(x)
        return await api.send(x, "Не задан код группы. Откройте карточку расписания.", BACK)
    await save_schedule(x, group, text.strip(), edit=True)


# ── настройки ─────────────────────────────────────────────────────────────────
async def send_settings(x: str):
    enabled = await db.get_setting("tickets_enabled", "1") == "1"
    welcome = await db.get_setting("welcome_text", DEFAULT_WELCOME)
    await api.send(
        x,
        f"⚙️ Настройки\n\nПриём обращений: {'включён' if enabled else 'выключен'}\nПриветствие студентов:\n{welcome}",
        [
            [btn("Приём обращений: " + ("выключить" if enabled else "включить"), "set:tickets")],
            [btn("✏️ Изменить приветствие", "set:welcome")],
            *BACK,
        ],
    )


@callback("settings")
async def cb_settings(x, arg):
    if await need_super(x):
        await send_settings(x)


@callback("set")
async def cb_set(x, arg):
    if not await need_super(x):
        return
    if arg == "tickets":
        enabled = await db.get_setting("tickets_enabled", "1") == "1"
        await db.set_setting("tickets_enabled", "0" if enabled else "1")
        await send_settings(x)
    elif arg == "welcome":
        await db.set_state(x, "set_welcome")
        await api.send(x, "Отправьте новый текст приветствия студентов (до 500 символов) или /cancel.")


@state("set_welcome")
async def st_set_welcome(x, text, p):
    if not await need_super(x):
        return await db.clear_state(x)
    await db.set_setting("welcome_text", text.strip()[:500] or DEFAULT_WELCOME)
    await db.clear_state(x)
    await send_settings(x)


# ── служебные команды и самопроверка ──────────────────────────────────────────
LOG_TAIL = 15  # сколько строк журнала показывать в ответ на /logs
OPEN = ("new", "accepted", "in_progress")


def panel_url() -> str:
    """Адрес веб-панели: из WEBHOOK_URL, иначе — подсказка для режима polling."""
    if config.WEBHOOK_URL:
        return config.WEBHOOK_URL.rsplit("/", 1)[0] + "/panel"
    return "http://<хост>:8080/panel"


def panel_hint() -> str:
    if config.WEB_PANEL_PASSWORD:
        return panel_url()
    return f"{panel_url()} — выключена, задайте WEB_PANEL_PASSWORD в .env"


async def diagnostics_text(x: str) -> str:
    """Сводка о состоянии бота — для кнопки «🧪 Тест и журнал» и команд /test, /diag."""
    st = await repo.stats_overview()
    counts = await repo.status_counts()
    dedupe = await repo.dedupe_stats()
    log.info("самопроверка: сис-админ %s", x)
    return (
        "🧪 Самопроверка бота\n"
        f"Ваш MAX ID: {x}\n"
        f"Режим: {'webhook' if config.WEBHOOK_URL else 'long polling'}\n"
        f"Журнал: {config.LOG_FILE} (уровень {config.LOG_LEVEL})\n"
        f"Обработано событий: {dedupe['processed']}\n"
        f"Студентов: {st['students']}, сотрудников: {st['staff']}, обращений: {st['total']} "
        f"(открытых {sum(counts.get(code, 0) for code in OPEN)})\n"
        f"Панель: {panel_hint()}"
    )


async def check_api(x: str) -> None:
    """Фоновая проверка связи с MAX API: ответ приходит отдельным сообщением."""
    started = time.monotonic()
    try:
        info = await api.me()
    except Exception as exc:
        log.warning("проверка API не удалась: %s", exc)
        await notify(x, f"❌ MAX API не отвечает: {exc}")
        return
    await notify(x, f"✅ MAX API отвечает ({time.monotonic() - started:.1f} с): {info}")


@callback("diag")
async def cb_diag(x, arg):
    if not await need_super(x):
        return
    await api.send(x, f"{await diagnostics_text(x)}\n\nПроверяю связь с MAX API…", BACK)
    spawn(check_api(x))


async def command(x: str, cmd: str) -> bool:
    """Служебные команды сис-админа. True — обработано (иначе команда считается неизвестной)."""
    if cmd not in ("/logs", "/test", "/diag", "/panel"):
        return False
    if not await need_super(x):
        return True  # не показываем, что команда вообще существует
    if cmd == "/logs":
        records = tail_file(config.LOG_FILE, LOG_TAIL)
        tail = "\n".join(short(line, 150) for line in records) or "Журнал пока пуст."
        dedupe = await repo.dedupe_stats()
        await api.send(
            x, f"🧾 Журнал: {config.LOG_FILE}\nСобытий обработано: {dedupe['processed']}\n\n{tail}", BACK
        )
        return True
    if cmd == "/panel":
        await api.send(x, "🖥 Веб-панель сис-админа\n"
                          f"{panel_hint()}\n\nВход: ваш MAX ID и пароль WEB_PANEL_PASSWORD из .env.", BACK)
        return True
    await api.send(x, f"{await diagnostics_text(x)}\n\nПроверяю связь с MAX API…", BACK)
    spawn(check_api(x))
    return True

    if cmd == "/panel":
        await api.send(x, f"🖥 Веб-панель сис-админа\n{panel_hint()}\n\n"
                          "Вход: ваш MAX ID и пароль WEB_PANEL_PASSWORD из .env.", BACK)
        return True
    await api.send(x, (await diagnostics_text(x)) + "\n\nПроверяю связь с MAX API…", BACK)
    spawn(check_api(x))
    return True
