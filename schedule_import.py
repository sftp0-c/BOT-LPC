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
"""
import logging
import re
from urllib.parse import unquote, urljoin, urlsplit

import httpx

import repository as repo
import timetable as tt
from utils import as_str, group_code, norm_group

log = logging.getLogger("bot")

PDF_HREF_RE = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.I)
MAX_PAGE_BYTES = 4 * 1024 * 1024      # страница со ссылками весит меньше мегабайта
MAX_PDFS = 40                          # больше такого количества - явно не страница расписаний


def is_pdf_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


async def find_pdf_links(url: str) -> list[str]:
    """Находит на HTML-странице ссылки на PDF расписаний."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (college-schedule-import)"}) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_PAGE_BYTES:
            raise ValueError("страница слишком большая - это не страница расписаний")
    links = []
    for href in PDF_HREF_RE.findall(response.text):
        full = urljoin(url, href)
        if is_pdf_url(full) and full not in links:
            links.append(full)
        if len(links) >= MAX_PDFS:
            break
    return links


def is_group_looks_like(name: str) -> bool:
    """Отсеиваем «График консультаций» и прочие документы: у них нет кода группы в клетке."""
    code = group_code(name)
    return bool(re.match(r"^\d{2}-\d{1,3}[А-ЯЁA-Z0-9-]*$", code))


async def import_pdf(url: str, times: dict | None = None) -> dict:
    """Скачивает один PDF и заводит все найденные в нём группы.

    Возвращает {"groups": [...], "lessons": int, "url": url}.
    """
    from handlers.schedules import download, file_hash, lesson_times  # ленивый импорт: нет цикла

    data = await download(url)
    pages = tt.extract_pages(data)
    names = [name for name in tt.groups_in_pages(pages) if is_group_looks_like(name)]
    result = {"url": url, "groups": [], "lessons": 0}
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
        await repo.upsert_schedule(code, url)
        await repo.save_lessons(code, schedule, digest, [name])
        result["groups"].append(code)
        result["lessons"] += schedule.lessons_count
    return result


async def import_schedules(urls: list[str], times: dict | None = None) -> dict:
    """Импортирует список PDF. Одна ошибка не должна останавливать остальные файлы."""
    done: list[str] = []
    lessons = 0
    problems: list[str] = []
    for url in urls:
        name = unquote(as_str(urlsplit(url).path).rsplit("/", 1)[-1]) or url
        try:
            result = await import_pdf(url, times)
        except Exception as exc:  # noqa: BLE001 — импорт должен довести остальные файлы
            problems.append(f"{name}: {exc}")
            log.warning("импорт расписания %s не удался: %s", name, exc)
            continue
        if not result["groups"]:
            problems.append(f"{name}: группы не найдены")
            continue
        done.extend(result["groups"])
        lessons += result["lessons"]
    return {"files": len(urls), "groups": sorted(set(done)), "lessons": lessons,
            "problems": problems, "total": len(set(done))}


async def collect_pdf_urls(source: str) -> list[str]:
    """Собирает PDF из того, что ввёл человек.

    Принимает и страницу со ссылками, и список ссылок (по одной в строке, можно
    вставлять кусками), и любую смесь: прямые PDF остаются как есть, страницы
    раскрываются в их PDF. Порядок и повторы сохраняются - так видно, что
    импортировалось, а что нет.
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
