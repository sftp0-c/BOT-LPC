"""Настройки: приём обращений, тексты для студентов и список ключей."""
import config
import database as db
import repository as repo
from fastapi import Request
from panel_theme import icon
from utils import as_str, fmt_when

from .common import csrf, esc, flash, page, redirect, require_form, require_user, value
from .router import router


# ── настройки ─────────────────────────────────────────────────────────────────
# Что сис-админ вправе видеть и править в общем списке «ключ → значение».
#
# Список закрытый, и это осознанно. Проверка «похоже на служебное» не годится:
# таблица settings общая для бота и панели, и в ней лежит всё, что бот пишет
# сам, - отметки о миграциях (tz_migrated, groups_backfill_v1), ник бота
# (bot_username), состояние меню каждого человека (menu_view:<id>), отметка
# «шаблон уже показывали» (tpl:<user>:<id>). Такие ключи меняются из кода, их
# правка руками ничего не меняет, только ломает бота, а сис-админ их видит и
# думает, что это его настройки. Поэтому список белый: новый ключ попадёт в
# панель только когда его сюда впишут, - и это осознанное решение, а не забыть.
#
# В списке только то, что колледж настраивает осмысленно:
#   welcome_text / consent_text - тексты, которые читает студент;
#   tickets_enabled / tunnel_enabled - переключатели 1/0, у которых на
#   страницах панели нет отдельной кнопки.
#
# Чего здесь нет и почему (всё это настраивается в других местах панели):
#   faq_enabled   - своя кнопка «Частые вопросы» на странице /panel/college;
#   lesson_times  - форма времени звонков на странице расписания (и это JSON);
#   college:*     - справочник колледжа, у него готовая форма на /panel/college.
HUMAN_SETTINGS = (
    ("welcome_text", "Приветствие новым студентам: пишет первым сообщением"),
    ("consent_text", "Согласие на обработку данных: пусто - бот берёт свою заготовку"),
    ("tickets_enabled", "Приём обращений: 1 - включён, 0 - выключен"),
    ("tunnel_enabled", "Внешняя ссылка на панель: 1 - включена, 0 - выключена"),
)


HUMAN_SETTING_KEYS = tuple(key for key, _about in HUMAN_SETTINGS)


def human_settings(stored: dict) -> list[dict]:
    """Строки для редактора: известные ключи и их значения, включая ещё не заданные.

    Показываем все ключи списка, а не только те, что уже лежат в базе: иначе
    включить, например, внешнюю ссылку было бы нечем - поля просто нет.
    """
    return [{"key": key, "value": as_str(stored.get(key, "")), "about": about}
            for key, about in HUMAN_SETTINGS]


@router.get("/settings")
async def settings_page(request: Request):
    user = await require_user(request)
    rows = human_settings({as_str(row["key"]): row["value"] for row in await repo.all_settings()})
    welcome = await db.get_setting("welcome_text", "")
    consent_text = await db.get_setting("consent_text", "")
    tickets_enabled = await db.get_setting("tickets_enabled", "1") == "1"
    actions = await repo.admin_log(15)
    counts = await repo.admin_log_counts(30)
    sysadmins = await repo.list_sysadmins()
    action_rows = "".join(
        f"<tr><td class='small mut'>{esc(fmt_when(item['created_at']))}</td>"
        f"<td class='small'>{esc(item['actor_name'] or item['actor_id'])}</td>"
        f"<td>{esc(item['action'])}</td><td class='small'>{esc(item['details'])}</td></tr>"
        for item in actions
    ) or "<tr><td colspan='4' class='mut'>Действий пока не было</td></tr>"
    count_rows = "".join(
        f"<tr><td>{esc(action)}</td><td>{esc(number)}</td></tr>"
        for action, number in counts.items()
    ) or "<tr><td class='mut'>Пусто</td></tr>"
    body = f"""
<div class="card"><h2>{icon("tickets", 20)} Приём обращений</h2>
<form method="post" action="/panel/settings/tickets">{csrf(request)}
<input type="hidden" name="enabled" value="{"0" if tickets_enabled else "1"}">
<button class="{"btn-bad" if tickets_enabled else "btn-ok"}">{icon("close" if tickets_enabled else "check", 16)} {"Выключить" if tickets_enabled else "Включить"}</button>
<span class="small mut">сейчас: {"включён" if tickets_enabled else "выключен"}</span></form></div>
<div class="card"><h2>{icon("check", 20)} Согласие на обработку данных</h2>
<p class="small mut">Этот текст студент видит при регистрации. Пустое поле - бот покажет
свою заготовку. Согласие хранится с датой и редакцией текста.</p>
<form method="post" action="/panel/settings/consent">{csrf(request)}
<textarea name="value" maxlength="1000" style="min-height:110px">{esc(consent_text)}</textarea>
<div class="grid" style="margin-top:10px"><button>Сохранить</button></div></form></div>
<div class="card"><h2>{icon("info", 20)} Приветствие студентов</h2>
<form method="post" action="/panel/settings/welcome">{csrf(request)}
<textarea name="value" maxlength="500">{esc(welcome)}</textarea>
<div class="grid" style="margin-top:10px"><button>Сохранить</button></div></form></div>
<div class="grid" style="align-items:stretch">
  <div class="card" style="flex:2"><h2>{icon("logs", 20)} Действия сис-админов</h2>
  <table><tr><th>Когда</th><th>Кто</th><th>Что сделал</th><th>Подробности</th></tr>{action_rows}</table>
  <p class="small mut">Кто и когда менял должности, выдавал коды, удалял людей и трогал базу.
  Записи ведутся при каждом действии и не удаляются вместе с данными.</p></div>
  <div class="card" style="flex:1"><h2>{icon("analytics", 20)} За 30 дней</h2>
  <table><tr><th>Действие</th><th>Раз</th></tr>{count_rows}</table></div>
</div>
<div class="card"><h2>{icon("settings", 20)} Настройки (ключ → значение)</h2>
<p class="small mut">Только те настройки, которые меняет человек. Служебные ключи бота
(состояния меню, отметки миграций, ник бота) сюда не попадают: их пишет код, и правка
руками только мешает. Новую настройку добавляют в панели разработчика.</p>
<form method="post" action="/panel/settings/raw">{csrf(request)}<table>
<tr><th>Ключ</th><th>Значение</th><th>Что это</th></tr>"""
    for row in rows:
        body += (f"<tr><td class='col-key'><input name='key' value='{esc(row['key'])}'></td>"
                 f"<td><input name='value' value='{esc(row['value'])}'></td>"
                 f"<td class='small mut'>{esc(row['about'])}</td></tr>")
    body += f"""</table>
<div class="grid" style="margin-top:10px"><button>Сохранить</button>
<a class="btn btn-grey" href="/panel/settings">Обновить список</a></div></form></div>
<div class="card"><h2>{icon("settings", 20)} Служебное</h2><table>
<tr><th>Режим</th><td>{"webhook" if config.WEBHOOK_URL else "long polling"}</td></tr>
<tr><th>Адрес API</th><td>{esc(config.MAX_API_URL)}</td></tr>
<tr><th>Файл журнала</th><td>{esc(config.LOG_FILE)}</td></tr>
<tr><th>Сис-админов в базе</th><td>{len(sysadmins)} ({esc(", ".join(item["full_name"] for item in sysadmins) or "—")})</td></tr>
<tr><th>Системные администраторы (SYSADMIN_IDS в .env)</th><td>{esc(", ".join(str(i) for i in config.SYSADMIN_IDS) or "—")}</td></tr>
<tr><th>Автокопия базы</th><td>{("раз в %s ч" % config.BACKUP_EVERY_HOURS) if config.BACKUP_EVERY_HOURS else "выключена"}</td></tr>
<tr><th>Перечитывание PDF расписания</th><td>раз в {config.SCHEDULE_CACHE_HOURS} ч</td></tr>
</table><p class="small mut">Секреты (.env, токен бота) панель не показывает.</p></div>"""
    return page("Настройки", body, user, "/settings")


@router.post("/settings/tickets")
async def settings_tickets(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("tickets_enabled", "0" if value(data, "enabled") == "1" else "1")
    flash("Настройка сохранена.")
    return redirect("/panel/settings")


@router.post("/settings/consent")
async def settings_consent(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("consent_text", as_str(data.get("value", "")).strip()[:1000])
    flash("Текст согласия сохранён.")
    return redirect("/panel/settings")


@router.post("/settings/welcome")
async def settings_welcome(request: Request):
    await require_form(request)
    data = await request.form()
    await db.set_setting("welcome_text", as_str(data.get("value", "")).strip()[:500])
    flash("Приветствие сохранено.")
    return redirect("/panel/settings")


@router.post("/settings/raw")
async def settings_raw(request: Request):
    await require_form(request)
    data = await request.form()
    keys = data.getlist("key") if hasattr(data, "getlist") else []
    values = data.getlist("value") if hasattr(data, "getlist") else []
    saved = skipped = 0
    for key, val in zip(keys, values, strict=False):
        name = as_str(key).strip()
        if not name:
            continue
        if name not in HUMAN_SETTING_KEYS:
            # подделанное или забытое имя ключа: молча писать нельзя, но и
            # сохранять служебные настройки из формы нельзя тем более
            skipped += 1
            continue
        await db.set_setting(name, as_str(val).strip())
        saved += 1
    flash(f"Сохранено настроек: {saved}." + (f" Пропущено чужих ключей: {skipped}." if skipped else ""))
    return redirect("/panel/settings")
