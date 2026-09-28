"""Импорт расписаний с сайта колледжа.

Колледж публикует семь PDF: в каждом файле несколько групп колонками, а сам
файл обновляется каждую неделю. Ссылки постоянные, поэтому достаточно один раз
добавить их в справочник - дальше бот сам перечитывает файл, когда он меняется.

Что делает импорт:
  1. скачивает каждый PDF (с проверкой подписи и размера);
  2. находит в нём группы по заголовкам колонок;
  3. заводит группы в справочнике вместе с псевдонимами (как код напечатан
     у колледжа: «24-23 (П)»);
  4. сохраняет ссылку для каждой группы и сразу раскладывает расписание
     в таблицу lessons.

Почему файлы со страницы отбираются. Ссылок на странице колледжа восемнадцать:
семь текущих (по группам) и одиннадцать посторонних — кураторы, график
консультаций, файл 2022 года и архивы прошлых лет. Посторонние лежат на
странице ПОСЛЕ текущих, а ``save_lessons`` перезаписывает занятия безусловно,
так что последний файл просто выигрывал бы и откатывал расписание на годы.
Поэтому автосбор пропускает всё, что не похоже на файл «по группам»
(``is_schedule_pdf``), а ручная ссылка из панели импортируется как есть:
человек, который её вставил, знает, что скачивает.
"""
import logging
import re
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

import httpx

import database as db
import repository as repo
import timetable as tt
from utils import as_str, group_code, norm_group

log = logging.getLogger("bot")

PDF_HREF_RE = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.I)
# Ссылка вместе с подписью: у кураторов и консультаций она осмысленная
# («График консультаций преподавателей на 1-2 полугодие»), у расписаний пустая.
PDF_LINK_RE = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
MAX_PAGE_BYTES = 4 * 1024 * 1024      # страница со ссылками весит меньше мегабайта
MAX_PDFS = 40                          # больше такого количества - явно не страница расписаний

# Слова, по которым документ точно не расписание групп.
JUNK_WORDS = ("куратор", "консультаци", "график")
# Имя файла расписания: две группы и больше через дефис - «23-29-24-25.pdf».
SCHEDULE_FILE_RE = re.compile(r"\d{2}-\d{2}(?:-\d{2}-\d{2})+\.pdf", re.I)
# Ключ настройки с неделей, за которую в базе лежит расписание группы.
WEEK_KEY = "schedule_week:"


def is_pdf_url(url: str) -> bool:
    """Похоже ли на PDF вообще. Для ссылки, введённой руками, этого достаточно."""
    return urlsplit(url).path.lower().endswith(".pdf")


def is_schedule_pdf(url: str, title: str = "") -> bool:
    """Похож ли файл, найденный на странице, на расписание групп.

    Два отсева, и оба нужны. Первый — по словам в ссылке и подписи: «куратор»,
    «консультаци», «график» не про расписание. Второй — по имени файла:
    колледж называет расписание по группам («24-26-25-20.pdf»), и групп в имени
    две или больше. Второй отсев и есть главный: архивы прошлых лет названы так
    же, но стоят на странице после текущих, и без него они откатывали бы неделю.

    Ручная ссылка сюда не ходит: для неё хватает is_pdf_url, иначе человек не смог
    бы импортировать файл с нестандартным именем.
    """
    hay = f"{url} {title}".lower()
    if any(word in hay for word in JUNK_WORDS):
        return False
    name = unquote(as_str(urlsplit(url).path).rsplit("/", 1)[-1])
    return bool(SCHEDULE_FILE_RE.fullmatch(name))


def _page_links(html: str, base: str) -> list[tuple[str, str]]:
    """Пары (адрес, подпись) со страницы, в порядке как на странице."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, label in PDF_LINK_RE.findall(html):
        pairs.append((urljoin(base, href), tt.clean(TAG_RE.sub(" ", label))))
        seen.add(href)
    for href in PDF_HREF_RE.findall(html):
        if href not in seen:      # ссылка без подписи или без нормального <a>
            pairs.append((urljoin(base, href), ""))
            seen.add(href)
    return pairs


async def find_pdf_links(url: str) -> list[str]:
    """Находит на HTML-странице ссылки на PDF расписаний.

    Со страницы берём только файлы расписаний: на странице колледжа лежат ещё
    кураторы, график консультаций и архивы, а импорт идёт по порядку страницы и
    перезаписывает занятия безусловно — мусор в конце списка победил бы.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (college-schedule-import)"}) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_PAGE_BYTES:
            raise ValueError("страница слишком большая - это не страница расписаний")
    links = []
    for full, title in _page_links(response.text, url):
        if not is_pdf_url(full) or not is_schedule_pdf(full, title):
            log.debug("страница расписаний: %s не расписание групп, пропускаем", full)
            continue
        if full not in links:
            links.append(full)
        if len(links) >= MAX_PDFS:
            break
    return links


def is_group_looks_like(name: str) -> bool:
    """Отсеиваем «График консультаций» и прочие документы: у них нет кода группы в клетке."""
    code = group_code(name)
    return bool(re.match(r"^\d{2}-\d{1,3}[А-ЯЁA-Z0-9-]*$", code))


def file_name(url: str) -> str:
    """Имя PDF для журнала и отчёта: «24-26-25-20.pdf»."""
    return unquote(as_str(urlsplit(url).path).rsplit("/", 1)[-1]) or as_str(url)


async def saved_week(code: str) -> date | None:
    """Неделя, за которую в базе уже лежит расписание группы. None - неизвестно.

    Хранится в настройках служебным ключом: своей колонки у расписания нет, а
    править database.py из этого модуля нельзя. В панели такие ключи не видны —
    список настроек там белый.
    """
    raw = as_str(await db.get_setting(WEEK_KEY + code, "")).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def remember_week(code: str, week: date) -> None:
    """Запоминает неделю, за которую расписание группы теперь в базе."""
    await db.set_setting(WEEK_KEY + code, week.isoformat())


async def week_is_fresh(code: str, week: date | None, url: str) -> bool:
    """Не даст файлу постарше откатить уже сохранённое расписание.

    Правило: неделя новее сохранённой — пишем; та же — тоже пишем, файл могли
    перезалить тем же содержимым; строго старше — не трогаем базу и говорим в
    журнал, какие группы пропущены. Неделю из PDF прочитать не удалось (None) —
    ведём себя как раньше, но говорим об этом: молчать нельзя, иначе непонятно,
    почему файл не применился.
    """
    if week is None:
        log.info("группа %s: неделя в PDF %s не читается, записываю как есть", code, file_name(url))
        return True
    known = await saved_week(code)
    if known is not None and week < known:
        log.warning("группа %s: файл %s за неделю %s, в базе уже %s — расписание не тронуто",
                    code, file_name(url), week, known)
        return False
    return True


async def import_pdf(url: str, times: dict | None = None) -> dict:
    """Скачивает один PDF и заводит все найденные в нём группы.

    Возвращает {"groups": [...], "lessons": int, "url": url, "skipped": [...]}.
    В skipped — группы, чьё расписание не тронуто, потому что файл за неделю
    старше уже сохранённой (см. week_is_fresh).
    """
    from handlers.schedules import download, file_hash, lesson_times  # ленивый импорт: нет цикла

    data = await download(url)
    pages = tt.extract_pages(data)
    names = [name for name in tt.groups_in_pages(pages) if is_group_looks_like(name)]
    result = {"url": url, "groups": [], "lessons": 0, "skipped": []}
    if not names:
        return result
    clock = times if times is not None else await lesson_times()
    digest = file_hash(data)
    for name in names:
        code = group_code(name)
        if not code:
            continue
        schedule, _found = tt.parse_pdf_bytes(data, name, clock)
        if not schedule.lessons_count:
            continue
        await repo.upsert_group(code, title=name)
        await repo.add_group_aliases(code, [name, norm_group(name)])
        if not await week_is_fresh(code, schedule.week, url):
            # Файл старше уже сохранённого: ссылку тоже не меняем, иначе
            # upsert_schedule сбросит отпечаток и бот будет перечитывать файл
            # по кругу, ни разу не записав занятия.
            result["skipped"].append(code)
            continue
        await repo.upsert_schedule(code, url)
        await repo.save_lessons(code, schedule, digest, [name])
        if schedule.week:
            await remember_week(code, schedule.week)
        result["groups"].append(code)
        result["lessons"] += schedule.lessons_count
    if result["skipped"]:
        log.warning("файл %s: не записано групп из-за старой недели: %s",
                    file_name(url), ", ".join(result["skipped"]))
    return result


async def import_schedules(urls: list[str], times: dict | None = None) -> dict:
    """Импортирует список PDF. Одна ошибка не должна останавливать остальные файлы.

    skipped в ответе — сколько и каких групп не записано из-за старой недели.
    """
    done: list[str] = []
    skipped: list[str] = []
    lessons = 0
    problems: list[str] = []
    for url in urls:
        name = file_name(url) or url
        try:
            result = await import_pdf(url, times)
        except Exception as exc:  # noqa: BLE001 — импорт должен довести остальные файлы
            problems.append(f"{name}: {exc}")
            log.warning("импорт расписания %s не удался: %s", name, exc)
            continue
        left = result.get("skipped") or []      # import_pdf подменяют и в тестах
        skipped.extend(left)
        if not result["groups"]:
            if left:
                problems.append(f"{name}: файл старше сохранённой недели, "
                                f"пропущено групп: {len(left)}")
            else:
                problems.append(f"{name}: группы не найдены")
            continue
        done.extend(result["groups"])
        lessons += result["lessons"]
    return {"files": len(urls), "groups": sorted(set(done)), "lessons": lessons,
            "problems": problems, "total": len(set(done)), "skipped": sorted(set(skipped))}


async def collect_pdf_urls(source: str) -> list[str]:
    """Собирает PDF из того, что ввёл человек.

    Принимает и страницу со ссылками, и список ссылок (по одной в строке, можно
    вставлять кусками), и любую смесь: прямые PDF остаются как есть, страницы
    раскрываются в их PDF. Порядок и повторы сохраняются - так видно, что
    импортировалось, а что нет.

    Прямая ссылка не проверяется is_schedule_pdf: файл, вписанный руками, может
    называться как угодно, а вот со страницы берём только расписания групп.
    """
    urls: list[str] = []
    for line in re.split(r"[\r\n]+", as_str(source)):
        line = line.strip()
        if not line.startswith(("http://", "https://")):
            continue
        if is_pdf_url(line):
            targets = [line]
        else:
            targets = await find_pdf_links(line)
        for target in targets:
            if target not in urls:
                urls.append(target)
    return urls


async def import_from_page(url: str, times: dict | None = None) -> dict:
    """Находит PDF на странице сайта и импортирует их все."""
    links = await find_pdf_links(url)
    result = await import_schedules(links, times)
    result["page"] = url
    return result


async def import_sources(source: str, times: dict | None = None) -> dict:
    """Импорт из строки: страница со ссылками, список PDF или и то и другое."""
    urls = await collect_pdf_urls(source)
    result = await import_schedules(urls, times)
    result["urls"] = urls
    return result


async def refresh_group(group: str) -> dict:
    """Перечитывает расписание одной группы из её PDF - «освежить сейчас»."""
    code = group_code(group)
    schedule = await repo.get_schedule(code)
    if not schedule or not as_str(schedule["pdf_url"]):
        return {"group": code, "updated": False, "reason": "нет ссылки на PDF"}
    try:
        result = await import_pdf(as_str(schedule["pdf_url"]))
    except Exception as exc:  # noqa: BLE001
        return {"group": code, "updated": False, "reason": str(exc)}
    if code not in result["groups"]:
        return {"group": code, "updated": False, "reason": "в файле нет такой группы"}
    return {"group": code, "updated": True, "lessons": result["lessons"]}
