"""Раздел панели «Тест»: тестовые сотрудники и сквозная проверка пути.

Задача раздела - дать администратору место, где можно проверить весь путь целиком,
не занимая живого сотрудника: студент пишет обращение с телефона, выбирает тестового
сотрудника, обращение приходит в панель, администратор отвечает за этого сотрудника
с сайта, и ответ уходит студенту обратно в MAX.

Тестовый сотрудник - обычная строка таблицы ``admins`` с меткой ``is_test=1``.
Настоящего MAX ID у него нет и быть не может: идентификатор выдаёт мессенджер.
Поэтому ID синтетический - «test-1», «test-2», … (раздаёт его ``store.staff``).
Числом он быть не должен, и проект его числом и не считает: колонка ``user_id`` -
это TEXT, ``max_api.send`` сам оставляет строку, когда ``int()`` не получился, а
``handlers.common.notify`` гасит неудачную отправку и пишет в журнал. В MAX тестовый
сотрудник не пишет, обращений не читает и не отвечает - он цель для проверки пути.
В боте он виден как обычный сотрудник, и студент выбирает его так же, как любого
другого. Хранить его в отдельной таблице незачем: всё, что делает раздел, - это
строки ``admins`` и ``admin_log``.

Раздел открыт только владельцу бота (``config.ROOT_IDS``) - тем же приёмом, что
«Данные»: не-владельцу 404, а не 403, и попытка попадает в журнал. Здесь создаются
и удаляются люди (пусть и вымышленные), и такой доступ не должен быть побочным
эффектом входа обычного сис-админа.

Остальные правила - как в «Данных»: CSRF на каждой форме, пустое поле означает
«не трогать», необратимое действие - только после слова, перед ним снимок базы,
а в журнал - кто и что, без содержимого чужих обращений и без ФИО студентов.

Ответ за тестового сотрудника пишет другой раздел панели («Обращения»): здесь
только заводят, правят и убирают самих сотрудников.
"""
import database as db
import repository as repo
from fastapi import HTTPException, Request
from panel_theme import icon
from store.staff import (add_test_staff, delete_test_staff, department_names, get_admin,
                         get_test_staff, list_staff, list_test_staff, set_staff_test,
                         test_staff_count, update_test_staff)
from utils import POSITION_TITLES, STAFF_CATS, as_str

from .common import (_action_form, code_cell, esc, flash, form, log, page, pill, plain,
                     redirect, require_form, require_user, select)
from .router import router


# Путь раздела: им же page() помечает активный пункт меню.
TAB = "/test"
BASE = f"/panel{TAB}"
# Слово, которым подтверждается удаление. Кнопка «да» не защищает ничего: её
# нажимают вместе с соседней, а слово приходится набрать руками.
CONFIRM_WORD = "удалить"
# Идентификаторы списков подсказок (datalist): один на страницу.
POSITION_LIST = "test-positions"
DEPARTMENT_LIST = "test-departments"
# Право видеть чужие обращения. Это решение человека, а не галочка по умолчанию,
# поэтому выбор всегда явный: пустого значения у списка не бывает.
SEE_ALL = {"0": "нет, только свои", "1": "да, вижу и чужие"}


# ── доступ ───────────────────────────────────────────────────────────────────
async def require_owner(request: Request) -> str:
    """Раздел «Тест» открыт только владельцу бота (``config.ROOT_IDS``).

    Приём тот же, что у «Данных» (``web.data.require_owner``): владелец заводится
    в таблицу ``admins`` при старте бота, а сис-админ без прав владельца получает
    404, а не 403 - раздела для него нет, и ответ «доступ запрещён» только
    напомнил бы, что он существует. Так же закрыт вебхук в ``bot.py``.
    """
    user = await require_user(request)
    if not await repo.is_owner(user):
        log.warning("панель: «Тест» попытался открыть не владелец: id=%s", user)
        raise HTTPException(status_code=404, detail="Раздел не найден")
    return user


async def require_owner_form(request: Request) -> str:
    """require_owner плюс CSRF-токен формы: как require_form, но для владельца.

    Порядок тот же, что в «Данных»: сначала 404 тому, кому раздел не положен, и
    только потом 403 форме без токена. Иначе по коду ответа можно было бы перебором
    узнать, что раздел существует.
    """
    await require_owner(request)          # 404 тому, кому раздел не положен
    return await require_form(request)   # 403 форме без токена


# ── мелочи разметки ──────────────────────────────────────────────────────────
def category_label(code) -> str:
    """Название раздела обращений по справочнику, без эмодзи: в таблице их нет."""
    raw = as_str(code)
    return plain(STAFF_CATS.get(raw, raw)) or "—"


def _datalists(departments: list) -> str:
    """Подсказки для полей: должности из справочника и отделы, которые уже есть.

    Отдел в базе пишут по-разному («учебная часть», «Учебная часть»), поэтому
    список показывает то, чем раздел уже заведён, а не выдумывает новый перечень.
    """
    positions = ('<datalist id="' + POSITION_LIST + '">'
                 + "".join(f'<option value="{esc(title)}"></option>'
                           for title in POSITION_TITLES.values()) + "</datalist>")
    names = ('<datalist id="' + DEPARTMENT_LIST + '">'
             + "".join(f'<option value="{esc(name)}"></option>' for name in departments)
             + "</datalist>")
    return positions + names


def _fields(full_name: str, position: str, office: str, department: str,
            category: str, see_all: str) -> str:
    """Поля формы тестового сотрудника: ФИО, должность, кабинет, отдел, раздел, права.

    Одни и те же поля и в форме добавления, и в форме правки: разница только в
    значениях. Пустое поле - это «не трогать», поэтому в правке подставляется
    текущее значение, а не пустая строка.
    """
    return (
        f'<div class="full"><label>ФИО</label><input name="full_name" '
        f'value="{esc(full_name)}" autocomplete="off" placeholder="Тестовый сотрудник 1"></div>'
        f'<div><label>Должность</label><input name="position" list="{POSITION_LIST}" '
        f'value="{esc(position)}" autocomplete="off" placeholder="например, Секретарь"></div>'
        f'<div><label>Кабинет</label><input name="office" value="{esc(office)}" '
        f'autocomplete="off" placeholder="каб. 000"></div>'
        f'<div><label>Отдел</label><input name="department" list="{DEPARTMENT_LIST}" '
        f'value="{esc(department)}" autocomplete="off" placeholder="учебная часть"></div>'
        f'{select("ticket_category", dict(STAFF_CATS), category, label="Раздел обращений")}'
        f'{select("see_all", SEE_ALL, see_all, label="Чужие обращения")}'
    )


def _switch_value(data) -> str:
    """Право видеть чужие обращения из формы: только «0» или «1», иначе пусто."""
    raw = as_str(data.get("see_all")).strip()
    return raw if raw in ("0", "1") else ""


def _confirmed(data) -> bool:
    """Введено ли слово-подтверждение (регистр и лишние пробелы не важны)."""
    return as_str(data.get("word")).strip().casefold() == CONFIRM_WORD


# ── снимок базы перед удалением ─────────────────────────────────────────────
async def _snapshot(note: str) -> str:
    """Копия базы через VACUUM INTO перед удалением строки.

    Пустая строка - снимок не получился: действие всё равно выполняется, а причина
    уходит в журнал, чтобы не молчала. Так же ведёт себя снимок в «Данных».
    """
    try:
        path = await db.backup_to()
    except Exception as exc:  # noqa: BLE001 - снимок не должен ронять удаление
        log.error("панель «Тест»: снимок базы перед «%s» не создан: %s", note, exc)
        return ""
    log.warning("панель «Тест»: снимок базы перед «%s» - %s", note, path)
    return path


def _snapshot_text(path: str) -> str:
    """Как показать снимок сис-админу: путь и что он уже сделан."""
    return f"Снимок базы: {path}" if path else "Резервная копия не создана - см. вкладку «База данных»."


# ── таблицы ─────────────────────────────────────────────────────────────────
def _staff_table(request: Request, rows: list) -> str:
    """Кто заведён: карточка тестового сотрудника и кнопки «править» и «удалить»."""
    body = ""
    for row in rows:
        uid = as_str(row["user_id"])
        rights = pill("видит чужие", "on") if row["see_all_tickets"] else pill("только свои", "off")
        body += (
            f"<tr><td><b>{esc(row['full_name'])}</b>"
            f'<div class="small mut">{code_cell(uid, "ID скопирован")}</div></td>'
            f"<td>{esc(as_str(row['position']) or '—')}</td>"
            f"<td>{esc(as_str(row['office']) or '—')}</td>"
            f"<td>{esc(as_str(row['department']) or '—')}</td>"
            f"<td>{esc(category_label(row['ticket_category']))}</td>"
            f"<td>{pill('тестовый', 'on')}</td>"
            f"<td>{rights}</td>"
            f'<td><a class="btn-grey" href="{BASE}/{esc(uid)}">{icon("edit", 16)} Править</a> '
            f'<a class="btn-grey" href="{BASE}/{esc(uid)}/delete">{icon("delete", 16)} Удалить</a>'
            "</td></tr>"
        )
    if not body:
        body = ('<tr><td class="mut">Тестовых сотрудников нет. Заведите первого формой ниже - '
                'студент сможет выбрать его в боте как обычного сотрудника.</td></tr>')
    return (f'<div class="card"><h2>{icon("keyboard", 20)} Кто заведён</h2>'
            f'<table class="data-table"><tr><th>ФИО</th><th>Должность</th><th>Кабинет</th>'
            f'<th>Отдел</th><th>Раздел обращений</th><th>Метка</th><th>Права</th>'
            f"<th>Действия</th></tr>{body}</table></div>")


def _switch_table(request: Request, rows: list) -> str:
    """Обычные сотрудники: метку можно поставить живому и снять потом.

    Отдельная таблица рядом с тестовыми, а не фильтр: показывать живых сотрудников
    среди тестовых нельзя, иначе правка и удаление едва не начнут доставать их.
    """
    button = f"{icon('refresh', 16)} Сделать тестовым"
    body = ""
    for row in rows:
        uid = as_str(row["user_id"])
        body += (
            f"<tr><td><b>{esc(row['full_name'])}</b>"
            f'<div class="small mut">{code_cell(uid, "ID скопирован")}</div></td>'
            f"<td>{esc(as_str(row['position']) or '—')}</td>"
            f"<td>{esc(as_str(row['department']) or '—')}</td>"
            f"<td>{_action_form(request, f'{BASE}/{esc(uid)}/switch', button)}</td></tr>"
        )
    if not body:
        body = '<tr><td class="mut">Обычных сотрудников нет</td></tr>'
    return (f'<div class="card"><h2>{icon("staff", 20)} Обычный сотрудник → тестовый</h2>'
            f'<p class="small mut">Метка ничего не меняет в карточке: тот же человек и те же '
            f"обращения, меняется только пометка. Поэтому путь можно проверить на реальном "
            f"сотруднике и потом снять метку, а не заводить дубля.</p>"
            f'<table class="data-table"><tr><th>Сотрудник</th><th>Должность</th><th>Отдел</th>'
            f"<th>Что сделать</th></tr>{body}</table></div>")


# ── памятки ─────────────────────────────────────────────────────────────────
def _count_card(total: int) -> str:
    """Счётчик и главное, что надо знать про тестового сотрудника."""
    return f"""
<div class="card"><h2>{icon("keyboard", 20)} Тестовых сотрудников заведено: {total}</h2>
<p class="small mut">Настоящего MAX ID у тестового сотрудника нет: идентификатор выдаёт
мессенджер, и выдумать его нельзя. Раздел выдаёт синтетический - <code>test-1</code>,
<code>test-2</code> и так далее. Это не число, и проект числом его не считает.
В MAX тестовый сотрудник не пишет, обращений не читает и не отвечает - он нужен
только как цель, чтобы студент выбрал его в боте. В боте он выглядит как обычный
сотрудник, поэтому MAX ID не спрашивается ни при выборе, ни при ответе.</p>
<p><a class="btn-grey" href="#put">{icon("activity", 16)} Путь целиком</a></p></div>"""


def _path_card() -> str:
    """Памятка «Путь целиком»: что делает студент и что делает администратор.

    Обычный текст, без логики: кнопка ведёт на этот блок, и никаких обращений
    раздел не делает и не проверяет - он только рассказывает, что будет дальше.
    """
    return f"""
<div class="card" id="put"><h2>{icon("activity", 20)} Путь целиком</h2>
<p><b>Если студент напишет с телефона</b></p>
<ol>
<li>Студент откроет бота в MAX, зарегистрируется и напишет обращение.</li>
<li>В списке сотрудников он увидит тестового сотрудника - он ничем не отличается от
обычного - и выберет его.</li>
<li>Обращение придёт в панель: вкладка «Обращения» → «Рабочее место», у обращения
в ответественном стоит <code>test-1</code>.</li>
<li>Попытка уведомить сотрудника в MAX не удастся: настоящего адреса нет. Это
ожидаемо и ничего не ломает - в журнале появится строка о неудачной отправке.</li>
</ol>
<p><b>Что нужно сделать администратору</b></p>
<ol>
<li>Открыть обращение на вкладке «Обращения» и ответить за этого сотрудника
прямо с сайта.</li>
<li>Ответ уйдёт студенту в MAX от имени бота. Сам тестовый сотрудник ответа не
получит и в MAX не появится.</li>
<li>Когда проверка закончена, вернуть сотрудника в обычные здесь же или удалить
его: обращения по нему останутся в базе, а в журнале будет запись об удалении.</li>
</ol>
<p class="small mut">Кнопка «Править» открывает карточку тестового сотрудника,
кнопка «Удалить» - подтверждение удаления словом. Ни та, ни другая не трогают
настоящих сотрудников.</p></div>"""


# ── раздел ──────────────────────────────────────────────────────────────────
@router.get("/test")
async def test_lab_page(request: Request):
    """Стенд: счётчик, список тестовых, форма добавления и памятка о пути."""
    user = await require_owner(request)
    rows = await list_test_staff()
    total = await test_staff_count()
    ordinary = [row for row in await list_staff() if not row["is_test"]]
    departments = await department_names()
    add_form = form(request, f"{BASE}/add", _fields("", "", "", "", "all", "0"),
                    "Завести тестового", "btn-ok")
    body = (f"{_count_card(total)}"
            f"{_staff_table(request, rows)}"
            f'<div class="card"><h2>{icon("plus", 20)} Завести тестового сотрудника</h2>'
            f"{_datalists(departments)}{add_form}"
            f'<p class="small mut">MAX ID не спрашивается: его у тестового сотрудника нет, '
            f'номер выдаёт сам раздел. Раздел обращений по умолчанию - «всё»: иначе '
            f"студент не найдёт сотрудника в боте. ФИО нужно обязательно - по нему "
            f"студент выбирает сотрудника из списка.</p></div>"
            f"{_switch_table(request, ordinary)}"
            f"{_path_card()}")
    return page("Тест", body, user, TAB)


@router.get("/test/{user_id}")
async def test_staff_card(request: Request, user_id: str):
    """Карточка тестового сотрудника: правка, удаление и возврат в обычные."""
    user = await require_owner(request)
    row = await test_staff_or_404(user_id)
    uid = as_str(row["user_id"])
    departments = await department_names()
    fields = _fields(as_str(row["full_name"]), as_str(row["position"]), as_str(row["office"]),
                     as_str(row["department"]), as_str(row["ticket_category"]),
                     "1" if row["see_all_tickets"] else "0")
    edit = form(request, f"{BASE}/{esc(uid)}/edit", fields, "Сохранить", "btn-ok")
    back = _action_form(request, f"{BASE}/{esc(uid)}/switch", "Вернуть в обычные",
                        confirm_text=f"Снять метку тестового с {as_str(row['full_name'])}? "
                                     f"Он снова станет обычным сотрудником.",
                        cls="btn-grey")
    body = f"""
<div class="card"><h2>{icon("edit", 20)} {esc(row['full_name'])} {pill('тестовый', 'on')}</h2>
<p class="small mut">ID: {code_cell(uid, 'ID скопирован')}. MAX ID у тестового сотрудника
нет: он не появится в MAX, не ответит и не увидит обращений - он цель для проверки
пути. Пустое поле в форме ниже означает «не трогать», а не «стереть».</p>
{_datalists(departments)}
{edit}</div>
<div class="card"><h2>{icon("warning", 20)} Опасные действия</h2>
<p class="msg msg-bad">{icon("warning", 20)} Удаление стирает строку сотрудника из базы.
Обращения, которые на него пришли, останутся: они хранятся отдельно от карточки.
Перед удалением делается снимок базы, и удалить нужно, введя слово.</p>
<p><a class="btn-grey" href="{BASE}/{esc(uid)}/delete">{icon("delete", 16)} Удалить</a></p>
<p class="small mut">Вернуть в обычные - одна кнопка: метка снимается, карточка и обращения
остаются теми же.</p>
{back}
<p><a class="btn-grey" href="{BASE}">{icon("chevron-left", 16)} К списку тестовых</a></p></div>"""
    return page(f"Тест: {as_str(row['full_name'])}", body, user, TAB)


@router.get("/test/{user_id}/delete")
async def test_staff_delete_page(request: Request, user_id: str):
    """Подтверждение удаления: карточка, последствия и поле для слова."""
    user = await require_owner(request)
    row = await test_staff_or_404(user_id)
    uid = as_str(row["user_id"])
    fields = (f'<div class="full"><label>Введите слово <code>{esc(CONFIRM_WORD)}</code></label>'
              f'<p class="small mut">Без этого слова сотрудник не удаляется: кнопка рядом '
              f"нажимается вместе с соседней, а слово приходится набрать руками.</p>"
              f'<input name="word" value="" autocomplete="off" '
              f'placeholder="{esc(CONFIRM_WORD)}"></div>')
    body = f"""
<div class="card"><h2>{icon("delete", 20)} Удалить тестового сотрудника</h2>
<p class="msg msg-bad">{icon("warning", 20)} {esc(row['full_name'])} ({esc(uid)}) исчезнет из
списка сотрудников и из выбора в боте. Обращения, которые на него пришли, останутся в
базе: восстановить карточку можно только отсюда или из резервной копии.</p>
{form(request, f"{BASE}/{esc(uid)}/delete", fields, "Удалить", "btn-grey")}
<p class="small mut">Перед удалением делается снимок базы, путь показывается в сообщении,
и в журнал действий пишется, кто удалил и кого.</p>
<p><a class="btn-grey" href="{BASE}/{esc(uid)}">{icon("chevron-left", 16)} Отмена</a></p></div>"""
    return page(f"Тест: удалить {as_str(row['full_name'])}", body, user, TAB)


# ── поиск строки ────────────────────────────────────────────────────────────
async def test_staff_or_404(user_id: str) -> dict:
    """Карточка тестового сотрудника; 404, если такой строки нет или она обычная.

    Отдельный 404, а не пустая страница: тестовых сотрудников видно по метке, и
    обычный сотрудник не должен выглядеть как тестовый только потому, что его
    подставили в адрес.
    """
    row = await get_test_staff(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Такого тестового сотрудника нет")
    return row


# ── запись: создание, правка, удаление, переключение ────────────────────────
@router.post("/test/add")
async def test_lab_add(request: Request):
    """Завести тестового сотрудника: ФИО обязательно, ID выдаёт раздел."""
    actor = await require_owner_form(request)
    data = await request.form()
    name = " ".join(as_str(data.get("full_name")).split())
    if not name:
        flash("!Тестовый сотрудник не заведён: нужно ФИО - по нему студент выбирает "
              "сотрудника в боте.")
        return redirect(BASE)
    category = as_str(data.get("ticket_category"))
    user_id = await add_test_staff(
        full_name=name,
        position=as_str(data.get("position")),
        office=as_str(data.get("office")),
        department=as_str(data.get("department")),
        ticket_category=category,
        see_all=_switch_value(data) == "1",
    )
    if not user_id:
        flash("!Тестовый сотрудник не заведён: не нашлось свободного номера.")
        return redirect(BASE)
    log.warning("панель «Тест»: заведён тестовый сотрудник %s сис-админом %s", user_id, actor)
    # В журнал - кто, ID, ФИО тестового сотрудника и раздел обращений. Ни ФИО
    # студентов, ни содержимого обращений сюда не попадает: их в разделе нет.
    await repo.log_action(actor, "заведён тестовый сотрудник",
                          f"{user_id}: {name}, раздел {category_label(category)}")
    flash(f"Тестовый сотрудник заведён: {user_id} — {name}. Студент выберет его в боте "
          f"как обычного сотрудника.")
    return redirect(BASE)


@router.post("/test/{user_id}/edit")
async def test_staff_edit(request: Request, user_id: str):
    """Правка карточки. Пустое поле - «не трогать», а не «стереть»."""
    actor = await require_owner_form(request)
    data = await request.form()
    row = await test_staff_or_404(user_id)
    was = " ".join(as_str(row["full_name"]).split())
    changed = await update_test_staff(
        user_id,
        full_name=as_str(data.get("full_name")),
        position=as_str(data.get("position")),
        office=as_str(data.get("office")),
        department=as_str(data.get("department")),
        ticket_category=as_str(data.get("ticket_category")),
        see_all=_switch_value(data),
    )
    if not changed:
        flash("Ничего не изменилось: пустое поле означает «не трогать», а не «стереть».")
        return redirect(f"{BASE}/{as_str(user_id)}")
    now = " ".join(as_str(data.get("full_name")).split()) or was
    log.warning("панель «Тест»: изменён тестовый сотрудник %s сис-админом %s", user_id, actor)
    await repo.log_action(actor, "правка тестового сотрудника",
                          f"{user_id}: ФИО «{was}» → «{now}»")
    flash(f"Карточка тестового сотрудника сохранена: {as_str(user_id)}.")
    return redirect(f"{BASE}/{as_str(user_id)}")


@router.post("/test/{user_id}/delete")
async def test_staff_delete(request: Request, user_id: str):
    """Удаление тестового сотрудника: слово, снимок базы, запись в журнал.

    Порядок проверок обратный опасности: сначала «есть ли такой тестовый», потом
    слово, и только потом снимок и само удаление.
    """
    actor = await require_owner_form(request)
    data = await request.form()
    row = await test_staff_or_404(user_id)
    uid = as_str(row["user_id"])
    name = as_str(row["full_name"])
    if not _confirmed(data):
        flash(f"!Слово «{CONFIRM_WORD}» не введено — тестовый сотрудник не удалён.")
        return redirect(f"{BASE}/{uid}/delete")
    path = await _snapshot(f"удаление тестового сотрудника {uid}")
    if not await delete_test_staff(uid):
        flash("!Тестовый сотрудник не удалён: строка уже исчезла или оказалась обычной.")
        return redirect(BASE)
    log.warning("панель «Тест»: удалён тестовый сотрудник %s сис-админом %s", uid, actor)
    await repo.log_action(actor, "удалён тестовый сотрудник", f"{uid}: {name}")
    flash(f"Тестовый сотрудник удалён: {uid} — {name}. Обращения по нему остались в базе. "
          f"{_snapshot_text(path)}")
    return redirect(BASE)


@router.post("/test/{user_id}/switch")
async def test_staff_switch(request: Request, user_id: str):
    """Переключение метки: обычный сотрудник → тестовый и обратно.

    Одно действие в обе стороны: что именно делается, решает текущая метка в
    строке, а не скрытое поле формы. Иначе подменой поля можно было бы снять
    метку с тестового - и он стал бы обычным в списке сотрудников, а это как
    раз то, что проверяют.
    """
    actor = await require_owner_form(request)
    row = await get_admin(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Такого сотрудника нет")
    uid = as_str(row["user_id"])
    name = as_str(row["full_name"])
    to_test = not bool(row["is_test"])
    if not await set_staff_test(uid, to_test):
        raise HTTPException(status_code=404, detail="Такого сотрудника нет")
    log.warning("панель «Тест»: метка тестового для %s %s сис-админом %s", uid,
                "поставлена" if to_test else "снята", actor)
    await repo.log_action(actor, "тестовый сотрудник" if to_test else "снята метка тестового",
                          f"{uid}: {name}")
    if to_test:
        flash(f"{name} ({uid}) помечен как тестовый: студент выберет его в боте как "
              f"обычного сотрудника, а MAX ID у него нет.")
    else:
        flash(f"Метка тестового снята: {name} ({uid}) снова обычный сотрудник.")
    return redirect(BASE)
