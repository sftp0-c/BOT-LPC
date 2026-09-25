"""Рассылки объявлений студентам (все или одной группе)."""
import database as db
import repository as repo
from handlers.common import BACK, admin_of, api, can_broadcast, notify, spawn
from handlers.registry import callback, state
from max_api import btn
from utils import norm_group


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
    await api.send(x, f"Отправить {where} ({n} чел.)?\n\n📢 Объявление колледжа:\n\n{text}",
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
    try:
        for user_id in await repo.audience_ids(aud):
            if await notify(user_id, "📢 Объявление колледжа:\n\n" + text):
                sent += 1
            else:
                failed += 1
    finally:
        await repo.log_broadcast(sender, aud, text, sent, failed)
        await notify(sender, f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.", BACK)
