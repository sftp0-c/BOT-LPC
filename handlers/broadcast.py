"""Рассылки объявлений студентам (все или одной группе)."""
import database as db
import repository as repo
from handlers.admin import STAFF_ROLES, office_of, role_of
from handlers.common import BACK, admin_of, api, can_broadcast, is_super, notify, spawn
from handlers.registry import callback, state
from max_api import btn
from utils import as_str, norm_group

HEADLINE = "📢 Объявление колледжа:"


def headline(text: str, signature: str = "") -> str:
    """Текст рассылки для получателя: объявление и подпись отправителя."""
    return f"{HEADLINE}\n\n{text}\n\n— {signature}" if signature else f"{HEADLINE}\n\n{text}"


async def sender_signature(sender_id: str) -> str:
    """Подпись отправителя: «ФИО, должность, каб. …» — чтобы студент видел, от кого объявление."""
    a = await admin_of(sender_id)
    if not a:
        return ""
    parts = [as_str(a["full_name"]).strip()]
    position = "Сис-админ" if is_super(a) else STAFF_ROLES.get(role_of(a), "")
    if position:
        parts.append(position)
    office = office_of(a)
    if office:
        parts.append(f"каб. {office}")
    return ", ".join(part for part in parts if part)


async def sender_card(sender_id: str) -> tuple[str, str]:
    """Имя и должность отправителя для журнала рассылок."""
    a = await admin_of(sender_id)
    name = as_str(a["full_name"]).strip() if a else ""
    position = ("Сис-админ" if is_super(a) else STAFF_ROLES.get(role_of(a), "")) if a else ""
    return name, position


@callback("broadcast")
async def cb_broadcast(x, arg):
    if not can_broadcast(await admin_of(x)):
        return await api.send(x, "У вас нет права на рассылку.", BACK)
    await api.send(x, "📢 Кому отправить?",
                   [[btn("👥 Всем студентам", "bcaud:all")], [btn("🎓 Одной группе", "bcaud:group")], *BACK])


@callback("bcaud")
async def cb_broadcast_audience(x, arg):
    if not can_broadcast(await admin_of(x)):
        return
    if arg == "group":
        await db.set_state(x, "bc_group")
        return await api.send(x, "Введите код группы (или /cancel).")
    await db.set_state(x, "bc_text", {"aud": "all"})
    await api.send(x, "Введите текст рассылки для всех студентов (или /cancel).")


@state("bc_group")
async def st_bc_group(x, text, p):
    if not can_broadcast(await admin_of(x)):
        return await db.clear_state(x)
    group = norm_group(text)
    n = len(await repo.audience_ids(group))
    if not n:
        return await api.send(x, f"В группе «{group}» нет зарегистрированных студентов. Проверьте код и введите ещё раз.")
    await db.set_state(x, "bc_text", {"aud": group})
    await api.send(x, f"Группа {group}: получателей {n}. Введите текст рассылки (или /cancel).")


@state("bc_text")
async def st_bc_text(x, text, p):
    if not can_broadcast(await admin_of(x)):
        return await db.clear_state(x)
    text = text.strip()[:3500]
    n = len(await repo.audience_ids(p["aud"]))
    await db.set_state(x, "bc_confirm", {"aud": p["aud"], "text": text})
    where = "всем студентам" if p["aud"] == "all" else f"группе {p['aud']}"
    preview = headline(text, await sender_signature(x))
    await api.send(x, f"Отправить {where} ({n} чел.)?\n\n{preview}",
                   [[btn("✅ Отправить", "bcgo"), btn("❌ Отмена", "home")]])


@state("bc_confirm")
async def st_bc_confirm(x, text, p):
    await api.send(x, "Нажмите «✅ Отправить» или «❌ Отмена» под сообщением с текстом рассылки (либо /cancel).")


@callback("bcgo")
async def cb_broadcast_go(x, arg):
    st = await db.get_state(x)
    if not (can_broadcast(await admin_of(x)) and st and st["state"] == "bc_confirm"):
        return await api.send(x, "Нет подготовленной рассылки. Начните заново.", BACK)
    await db.clear_state(x)
    spawn(run_broadcast(x, st["payload"]["aud"], st["payload"]["text"]))
    await api.send(x, "⏳ Рассылка запущена, пришлю итог по завершении.")


async def run_broadcast(sender: str, aud: str, text: str):
    sent = failed = 0
    message = headline(text, await sender_signature(sender))
    try:
        for user_id in await repo.audience_ids(aud):
            if await notify(user_id, message):
                sent += 1
            else:
                failed += 1
    finally:
        name, position = await sender_card(sender)
        await repo.log_broadcast(sender, aud, text, sent, failed, name, position)
        await notify(sender, f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.", BACK)
