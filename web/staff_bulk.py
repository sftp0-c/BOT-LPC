"""Массовый выпуск сотрудников: список в панели и ссылка-приглашение на каждого.

Страница устроена в два шага. Сначала «Проверить»: список разбирается,
должности сводятся к справочнику, и всё, что с каждой строкой не так,
показывается таблицей - при этом не создаётся ничего. Потом «Создать
приглашения»: на каждую хорошую строку появляется одноразовая ссылка,
которую сис-админ отправляет сотруднику.

Самого сотрудника страница не заводит: он появится, когда человек откроет
свою ссылку в MAX и подтвердит вход кнопкой (см. handlers.invites). Так
соблюдается и правило платформы - права нельзя выдать через диплинк молча.

Путь раздела - /panel/invites, а не /panel/staff/…: последний перехватывал
бы запрос строки @router.get("/staff/{user_id}") из раздела сотрудников, и
страница молча показывала бы карточку несуществующего человека.
"""
import config
import database as db
from fastapi import Request
from panel_theme import icon
from store.access import active_invite_names, create_invites_bulk, invite_link
from store.staff import staff_names
from store.sysadmin import log_action
from utils import (CODE_TTL_CHOICES, STAFF_CATS, STAFF_CATS_BTN, as_str, norm_position,
                   position_code, to_int, ttl_label)

from .common import (code_cell, csrf, esc, log, page, pill, plain, require_form,
                     require_user, select, value)
from .router import router


# Формат строки: ФИО; должность; кабинет; раздел. Поля с конца можно не писать,
# раздел по умолчанию - «всё», иначе сотрудник не увидит ни одного обращения.
FIELDS = ("full_name", "position", "office", "category")
FIELD_SEPARATORS = (";", "\t")
CATEGORY_ALL = "all"
ROW_LIMIT = 200                       # больше строк в одну пачку не берём

FORMAT_HINT = (
    "Одна строка - один человек: <code>ФИО; должность; кабинет; раздел</code>. "
    "Разделителем может быть точка с запятой, таб или запятая - список обычно "
    "копируют из таблицы. Раздел можно не писать: тогда сотрудник получит все обращения."
)
NO_BOT_NAME = ("Бот ещё не сообщил свой ник в MAX, поэтому ссылку собрать не из чего. "
               "Перезапустите бота и попробуйте снова.")


# ── разбор строк ─────────────────────────────────────────────────────────────
def category_key(value) -> str:
    """Ключ раздела для сравнения: строчные буквы, без эмодзи и знаков."""
    text = "".join(char for char in as_str(value).lower().replace("ё", "е")
                   if char.isalnum() or char.isspace())
    return " ".join(text.split())


CATEGORY_ALIASES = {
    "все": CATEGORY_ALL, "всё": CATEGORY_ALL, "все разделы": CATEGORY_ALL,
    "справка": "certificates", "справки": "certificates", "документы": "certificates",
    "учёба": "academic", "учеба": "academic", "деньги": "accounting", "связь": "feedback",
}


def category_index() -> dict:
    """Написание раздела -> код раздела: и полное название, и подпись кнопки."""
    index: dict = {}
    for code, title in STAFF_CATS.items():
        for form in (code, title, STAFF_CATS_BTN.get(code, "")):
            key = category_key(form)
            if key:
                index.setdefault(key, code)
    for alias, code in CATEGORY_ALIASES.items():
        index.setdefault(category_key(alias), code)
    return index


CATEGORIES = category_index()


def norm_category(value) -> str:
    """Раздел обращений по справочнику; '' - если такого раздела нет.

    Пустое поле - это «всё»: раздел по умолчанию у каждого сотрудника один,
    а отсутствие раздела означало бы, что он не увидит ни одного обращения.
    """
    raw = " ".join(as_str(value).split())
    if not raw:
        return CATEGORY_ALL
    return CATEGORIES.get(category_key(raw), "")


def category_label(code) -> str:
    """Название раздела для панели: без эмодзи, как в заголовках меню."""
    return plain(STAFF_CATS.get(as_str(code), as_str(code) or "—"))


def split_fields(line: str) -> list:
    """Поля строки по разделителю: «;» или таб, а если их нет - запятая.

    Точка с запятой и таб проверяются первыми: в списке из таблицы запятых
    может не быть вовсе, а в напечатанной руками строке их иногда ставят
    вместо точки с запятой.
    """
    for separator in FIELD_SEPARATORS:
        if separator in line:
            return line.split(separator)
    return line.split(",")


def parse_row(number: int, line: str) -> dict:
    """Одна строка списка: что в ней написано и что с ней не так."""
    row = {"number": number, "line": line, "full_name": "", "position": "",
           "office": "", "category": "", "problems": []}
    if not line.strip():
        row["problems"].append("пустая строка")
        return row
    parts = [part.strip() for part in split_fields(line)]
    if len(parts) > len(FIELDS) or line.startswith("… "):
        row["full_name"] = parts[0]
        row["problems"].append("не разобрали: полей больше четырёх" if len(parts) > len(FIELDS)
                               else "список длиннее предела - эти строки не разобраны")
        return row
    row["full_name"] = " ".join(parts[0].split())[:100]
    if not row["full_name"]:
        row["problems"].append("не разобрали: нет ФИО")
        return row
    row["position"] = norm_position(parts[1] if len(parts) > 1 else "")
    row["office"] = parts[2][:40] if len(parts) > 2 else ""
    row["category"] = norm_category(parts[3] if len(parts) > 3 else "")
    if not row["position"]:
        row["problems"].append("должность не указана или её не разобрать")
    elif not position_code(row["position"]):
        row["problems"].append("должность не в справочнике")
    if not row["category"]:
        row["category"] = ""
        row["problems"].append("раздел не в справочнике")
    return row


def parse_rows(raw) -> list:
    """Список сотрудников в строки-разборы. Ничего не создаёт.

    Хвостовые пустые строки - это не строки списка (любой текст склеивает их
    при копировании), а пустая строка в середине - уже ошибка формата, и её
    видно в таблице: молча выкинуть её нельзя, иначе человек не поймёт, что
    один сотрудник из списка потерялся.
    """
    lines = as_str(raw).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > ROW_LIMIT:
        # обрезать молча нельзя: сис-админ решил бы, что people ушли в выпуск,
        # а они просто не попали в разбор - поэтому хвост становится строкой
        # с замечанием, которую видно в таблице
        lines = lines[:ROW_LIMIT] + [f"… ещё {len(lines) - ROW_LIMIT} строк списка"]
    return [parse_row(number, line) for number, line in enumerate(lines, 1)]


def fio_key(value) -> str:
    """ФИО для сравнения: без регистра, «ё» и лишних пробелов."""
    return " ".join(as_str(value).lower().replace("ё", "е").split())


async def check_rows(raw) -> list:
    """Разбор списка плюс проверка по базе. По-прежнему ничего не создаёт."""
    rows = parse_rows(raw)
    taken = {fio_key(name) for name in (await staff_names()) | await active_invite_names()}
    for row in rows:
        key = fio_key(row["full_name"])
        if row["problems"] or not key:
            continue
        if key in taken:
            row["problems"].append("такая уже есть в базе")
        taken.add(key)          # повтор в самом списке - тоже дубль
    return rows


def ready_rows(rows: list) -> list:
    """Строки без замечаний: из них только они и становятся приглашениями."""
    return [row for row in rows if not row["problems"]]


def skipped_rows(rows: list) -> list:
    """Строки с замечаниями: их правят руками, повторять их нельзя."""
    return [row for row in rows if row["problems"]]


# ── страница ─────────────────────────────────────────────────────────────────
@router.get("/invites")
async def invites_page(request: Request):
    """Пустая форма пачки: сюда же возвращается и результат проверки."""
    user = await require_user(request)
    return page("Выпуск сотрудников по ссылкам",
                _body_html(request, "", config.STAFF_CODE_TTL, None, None), user, "/staff")


@router.post("/invites")
async def invites_submit(request: Request):
    """«Проверить» не создаёт ничего, «Создать приглашения» - по хорошим строкам."""
    user = await require_form(request)
    data = await request.form()
    raw = value(data, "rows")
    ttl = to_int(value(data, "ttl_hours", default=str(config.STAFF_CODE_TTL)), config.STAFF_CODE_TTL)
    rows = await check_rows(raw)
    title = "Выпуск сотрудников по ссылкам"
    if as_str(data.get("action")).strip() != "create":
        return page(title, _body_html(request, raw, ttl, rows, None), user, "/staff")
    issued = await create_invites_bulk(ready_rows(rows), created_by=user, ttl_hours=ttl)
    # коды в журнал и в ленту действий не пишем: иначе они уедут в /panel/logs
    log.info("панель: выпущено %d приглашений по ссылкам, срок %s (сис-админ %s)",
             len(issued), ttl_label(ttl), user)
    await log_action(user, "приглашения по ссылкам выпущены",
                     f"{len(issued)} шт., срок {ttl_label(ttl)}")
    links = await _links(issued)
    return page(title,
                _body_html(request, raw, ttl, rows, (links, skipped_rows(rows))), user, "/staff")


async def _links(issued: list) -> list:
    """Готовые ссылки на каждое приглашение: [(приглашение, ссылка)].

    Ник бота лежит в настройке: бот узнаёт его о себе при старте. Пока настройки
    нет, ссылка пустая, и страница говорит об этом прямо, а не отдаёт битую.
    """
    username = as_str(await db.get_setting("bot_username", "")).strip().lstrip("@")
    return [(item, invite_link(username, item["code"])) for item in issued]


def _card(title: str, icon_name: str, body: str) -> str:
    return f'<div class="card"><h2>{icon(icon_name, 20)} {esc(title)}</h2>{body}</div>'


def _row_label(row: dict) -> str:
    """ФИО строки, а если его нет - сама строка: чтобы ошибку было видно."""
    return row["full_name"] or row["line"].strip() or "—"


def _form_html(request: Request, raw: str, ttl: int) -> str:
    hours = {value: label for value, label in CODE_TTL_CHOICES}
    textarea = (f'<div class="full"><label>Список сотрудников</label>'
                f'<textarea name="rows" rows="10" placeholder='
                f'"Иванов Иван Иванович; Секретарь; 214; Справки">{esc(raw)}</textarea></div>')
    return _card("Кто становится сотрудником", "staff", f"""
<p class="small mut">{FORMAT_HINT}</p>
<form method="post" action="/panel/invites" class="grid">
{csrf(request)}
{textarea}
<div>{select("ttl_hours", hours, ttl, label="Срок ссылки")}</div>
<div><button class="btn-grey" name="action" value="check">{icon("check", 16)} Проверить</button></div>
<div><button class="btn-ok" name="action" value="create">{icon("plus", 16)} Создать приглашения</button></div>
</form>
<p class="small mut">Проверка ничего не создаёт: она только показывает, что не так.
Создание заводит по ссылке на каждого - сотрудника заводит сам человек, нажав кнопку в боте.</p>""")


def _preview_html(rows: list) -> str:
    """Таблица разбора: что получилось и что с каждой строкой не так."""
    if not rows:
        return _card("Проверено строк: 0", "check",
                     '<p class="small mut">Список пуст: вставьте хотя бы одну строку вида '
                     "<code>ФИО; должность; кабинет; раздел</code>.</p>")
    body = []
    for row in rows:
        mark = pill("готова", "on") if not row["problems"] else pill("пропущена", "off")
        note = "" if not row["problems"] else (
            f'<div class="msg msg-bad"><span>{esc("; ".join(row["problems"]))}</span></div>')
        body.append(
            f"<tr><td>{esc(_row_label(row))}</td><td>{esc(row['position'] or '—')}</td>"
            f"<td>{esc(row['office'] or '—')}</td>"
            f"<td>{esc(category_label(row['category']) if row['category'] else '—')}</td>"
            f"<td>{mark}{note}</td></tr>"
        )
    table = ("<table><tr><th>ФИО</th><th>Должность</th><th>Кабинет</th><th>Раздел</th>"
             f"<th>Готовность</th></tr>{''.join(body)}</table>")
    return _card(f"Проверено строк: {len(rows)}", "check", table)


def _links_html(links: list) -> str:
    """Список выданных ссылок: по одной на человека, с копированием."""
    if not links:
        return ""
    rows = "".join(
        f"<tr><td>{esc(item['full_name'])}</td><td>{esc(item['position'] or '—')}</td>"
        f"<td>{esc(item['office'] or '—')}</td>"
        f"<td>{code_cell(link, 'Ссылка скопирована') if link else esc(NO_BOT_NAME)}</td></tr>"
        for item, link in links
    )
    everything = "\n".join(link for _item, link in links if link)
    # кнопка «скопировать всё» без единой ссылки только обманчива: жать её нечего
    copy_all = (f'<button class="btn-grey" type="button" data-copy="{esc(everything)}" '
                f'data-copy-note="Все ссылки скопированы">'
                f'{icon("copy", 16)} Скопировать всё списком</button>') if everything else ""
    table = ("<table><tr><th>ФИО</th><th>Должность</th><th>Кабинет</th><th>Ссылка</th></tr>"
             f"{rows}</table>")
    return _card("Ссылка на каждого", "link",
                 f"<p>{copy_all}</p>{table}<p class=\"small mut\">"
                 "Ссылка одноразовая и со сроком: человек открывает её в MAX, сверяет "
                 "свои данные и сам нажимает кнопку входа.</p>")


def _skipped_html(skipped: list) -> str:
    """Что не создано и почему - те же строки, что и в предпросмотре."""
    if not skipped:
        return ""
    rows = "".join(
        f"<tr><td>{esc(_row_label(row))}</td><td>{esc('; '.join(row['problems']))}</td></tr>"
        for row in skipped)
    return _card("Пропущено", "warning",
                 f"<p>Эти строки приглашений не получили - их надо поправить и "
                 f"загрузить снова.</p><table><tr><th>Строка</th><th>Что не так</th></tr>{rows}</table>")


def _report_html(count: int, skipped: list, created: bool = False) -> str:
    """Итог одной строкой: сколько вышло и сколько строк придётся поправить.

    Слова разные намеренно: при проверке ничего не создано, и писать «создано»
    там было бы враньём - сис-админ решил бы, что приглашения уже выпущены.
    """
    kind = "ok" if not skipped else "bad"
    head = "Создано приглашений" if created else "Готово к выпуску"
    return (f'<div class="msg msg-{kind}"><span>{head}: <b>{count}</b>, '
            f"пропущено строк: <b>{len(skipped)}</b>.</span></div>")


def _body_html(request: Request, raw: str, ttl: int, rows, result) -> str:
    """Страница целиком: форма сверху, под ней проверка или выданные ссылки.

    После «Создать приглашения» таблица разбора не дублируется: там уже есть
    список ссылок и отдельный список пропущенных строк.
    """
    parts = [_form_html(request, raw, ttl)]
    if result is not None:
        links, skipped = result
        parts += [_report_html(len(links), skipped, created=True),
                  _links_html(links), _skipped_html(skipped)]
    elif rows is not None:
        parts += [_report_html(len(ready_rows(rows)), skipped_rows(rows)), _preview_html(rows)]
    return "".join(part for part in parts if part)
