"""Слежение за PDF со страницы расписаний колледжа.

Зачем это нужно. Семь PDF на сайте колледжа обновляются каждую неделю, а
студент узнаёт о перемене только тогда, когда сам откроет расписание — и иногда
опоздает. Здесь бот проверяет файлы сам и пишет тем, кто подписался на расписание
своей группы.

Как устроен круг:
  1. берём страницу со ссылками на PDF и находим сами файлы
     (``schedule_import.collect_pdf_urls``);
  2. скачиваем каждый файл ОДИН раз и сравниваем его отпечаток (SHA-256) с тем,
     что сохранён в ``schedules.parsed_hash`` — это база пары «PDF — разбор»;
  3. отпечаток прежний — файл не трогаем вообще: ни разбора, ни уведомлений;
     отпечаток новый — раскладываем файл существующей функцией
     ``handlers.schedules.parse_group`` и смотрим, изменились ли сами занятия;
  4. изменившиеся группы раскладываем подписчикам — из той же таблицы
     ``schedule_subscriptions``, что и кнопка «Подписаться» в боте.

Когда идёт круг. Первая проверка — вскоре после старта бота (FIRST_CHECK_SECONDS),
дальше — через интервал. Откладывать первую проверку на весь интервал было
ошибкой: бота часто перезапускают (деплой, перезагрузка компьютера), и круг
просто не наступал ни разу — месяцами. Чтобы и не скачивать семь PDF при
каждом перезапуске, время последнего круга лежит в настройках, и раньше
MIN_GAP_SECONDS следующая проверка не делается (check_due).

Что гарантирует модуль:
  * один сбойный файл (не скачался, не разобрался) не мешает остальным: причина
    попадает в ``errors`` списком, следующий файл проверяется как обычно;
  * повторный запуск в тот же день ничего не пришлёт: после разбора отпечаток
    файла сохранён, поэтому следующий круг увидит «файл прежний»;
  * PDF, который на сайте просто перезаписали (тот же текст, новый файл), не
    считается изменением — сравниваются занятия, а не байты файла;
  * проверка никогда не поднимает исключение наружу: сбой сети ждёт следующего
    круга, а сис-админы получают одно сообщение о нём (не чаще раза в 6 часов).
"""
import asyncio
import logging
import time
from urllib.parse import unquote, urlsplit

import clock
import config
import database as db
import repository as repo
import schedule_import
from handlers import schedules as schedule_service  # загрузка, разбор, отпечаток файла
from handlers.common import notify, spawn
from max_api import btn
from utils import as_str, group_code, to_int

log = logging.getLogger("bot")

# Настройки интервала в config.py нет, а править его отсюда нельзя: пока бот
# проверяет PDF раз в 12 часов. Если в config появится SCHEDULE_WATCH_HOURS
# (часы; 0 или отсутствие значения — взять 12), watch_interval_hours()
# подхватит её сама, дублировать константу в .env не нужно.
WATCH_INTERVAL_HOURS = 12
# Сколько групп разбирать за один круг. Групп у колледжа десятки, запас есть.
WATCH_GROUP_LIMIT = 400
# Одно и то же сообщение сис-админам о сбое — не чаще раза в 6 часов
# (как в bot.py для ошибок обработки событий).
ALERT_COOLDOWN = 6 * 3600
# Первая проверка — вскоре после старта бота, а не через полный интервал.
FIRST_CHECK_SECONDS = 5 * 60
# ... но и не при каждом перезапуске: между кругами должно пройти не меньше
# этого. PDF колледж меняет раз в неделю, чаще смотреть незачем, а семи PDF
# при каждом старте бота — это и лишняя нагрузка на сайт.
MIN_GAP_SECONDS = 3 * 3600

_alerts: dict[str, float] = {}  # текст сбоя -> когда сообщали в последний раз
_watcher: "asyncio.Task | None" = None  # задача фонового круга
# Снимок занятий ДО разбора и сам разбор с неделей файла: из них change_text
# собирает сообщение — «что поменялось» и «с какой даты». Живут один круг,
# обновляются в check_college_updates.
_before: dict = {}
_parsed: dict = {}
# Когда в последний раз проверяли. В базе — чтобы пережить перезапуск, в памяти —
# чтобы решение «пора ли смотреть сайт» принималось без похода в базу.
LAST_CHECK_KEY = "watch_last_check"
_checked_on_start = None   # круг, записанный в базе ДО старта бота
_checked_this_run = None   # круг, сделанный этим процессом


# ── источник файлов ────────────────────────────────────────────────────────────
def college_page() -> str:
    """Адрес страницы со ссылками на PDF расписаний.

    Константа живёт в webpanel, но импортировать его на уровне модуля нельзя:
    webpanel тянет за собой handlers.admin, а этот модуль импортирует сам бот —
    получился бы цикл. Поэтому берём лениво, ровно как уже сделано в
    handlers/admin.py.
    """
    # ленивый импорт: webpanel нельзя тянуть наверху, пока не начался цикл
    from webpanel import COLLEGE_SCHEDULE_PAGE

    return COLLEGE_SCHEDULE_PAGE


def file_name(url: str) -> str:
    """Имя PDF для журнала и сообщений: «24-26 25-20.pdf»."""
    return unquote(as_str(urlsplit(url).path).rsplit("/", 1)[-1]) or as_str(url)


# ── проверка файлов ───────────────────────────────────────────────────────────
async def check_college_updates() -> dict:
    """Сверяет PDF на сайте с сохранёнными разборами.

    Возвращает:
      * ``changed`` — группы, у которых занятия изменились (их надо разослать);
      * ``new``     — группы, которые разобраны впервые (подписчики тоже узнают);
      * ``errors``  — список причин: один сбойный файл не мешает остальным;
      * ``checked`` — сколько файлов удалось скачать и сравнить;
      * ``groups``  — сколько групп смотрели.
    """
    changed: list[str] = []
    fresh: list[str] = []
    errors: list[str] = []
    checked = 0
    _before.clear()   # снимки и разборы относятся к этому кругу
    _parsed.clear()
    try:
        urls = await schedule_import.collect_pdf_urls(college_page())
    except Exception as exc:  # noqa: BLE001 — сайт недоступен: ждём следующего круга
        log.warning("не удалось получить список PDF: %s", exc)
        return {"changed": [], "new": [], "errors": [f"страница расписаний: {exc}"],
                "checked": 0, "groups": 0}
    if not urls:
        return {"changed": [], "new": [], "errors": ["на странице не нашлось ссылок на PDF"],
                "checked": 0, "groups": 0}

    known = await _groups_by_url()
    # Группы, чей PDF исчез со страницы (файл переименовали), молча пропускаем:
    # новую ссылку должен поставить сис-админ, сам бот её не угадает.
    for url in urls:
        name = file_name(url)
        try:
            data = await schedule_service.download(url)
            digest = schedule_service.file_hash(data)
        except Exception as exc:  # noqa: BLE001 — сбойный файл не должен ронять круг
            errors.append(f"{name}: {exc}")
            log.warning("не удалось скачать расписание %s: %s", name, exc)
            continue
        checked += 1
        groups = known.get(url) or []
        if not groups:
            # Файл ещё не привязан ни к одной группе (новый курс или семестр):
            # импортируем его целиком, так же как кнопка в панели.
            fresh.extend(await _import_new_file(url, name, errors))
            continue
        for code, stamp in groups:
            if stamp == digest:
                continue  # файл прежний — ни разбора, ни уведомлений
            verdict = await _refresh_group(code, stamp, name, errors)
            if verdict == "changed":
                changed.append(code)
            elif verdict == "new":
                fresh.append(code)
    return {"changed": changed, "new": fresh, "errors": errors,
            "checked": checked, "groups": sum(len(groups) for groups in known.values())}


async def _groups_by_url() -> dict:
    """Ссылка на PDF -> список (код группы, отпечаток последнего разбора)."""
    result: dict[str, list[tuple[str, str]]] = {}
    for row in await repo.schedule_groups(WATCH_GROUP_LIMIT):
        code = group_code(as_str(row["group_code"]))
        if not code:
            continue
        schedule = await repo.get_schedule(code)
        if not schedule:
            continue
        url = as_str(schedule["pdf_url"])
        if not url:
            continue
        stamp = repo.schedule_stamp(schedule)
        result.setdefault(url, []).append((code, stamp["parsed_hash"]))
    return result


async def _import_new_file(url: str, name: str, errors: list[str]) -> list[str]:
    """Заводит группы из ещё не привязанного PDF. Возвращает их коды."""
    try:
        result = await schedule_import.import_pdf(url)
    except Exception as exc:  # noqa: BLE001 — остальные файлы не зависят от него
        errors.append(f"{name}: {exc}")
        log.warning("импорт нового файла расписания %s не удался: %s", name, exc)
        return []
    skipped = result.get("skipped") or []      # ключ может не быть: import_pdf подменяют в тестах
    if not result["groups"]:
        if skipped:
            errors.append(f"{name}: файл старше сохранённой недели, "
                          f"пропущено групп: {len(skipped)}")
        else:
            errors.append(f"{name}: группы не найдены")
        return []
    log.info("новый файл расписания %s: %s", name, ", ".join(result["groups"]))
    return list(result["groups"])


async def _refresh_group(code: str, stamp: str, name: str, errors: list[str]) -> str:
    """Перечитывает файл группы, у которой изменился отпечаток.

    Возвращает "changed" (занятия другие), "new" (первый разбор) или "" —
    разбор не дал занятий либо занятия не изменились; причина в errors.

    Попутно оставляет снимок занятий ДО разбора и сам разбор, в котором есть
    неделя файла: из них change_text собирает сообщение подписчикам.
    """
    before = await schedule_service.lessons_snapshot(code)
    # Разбор отдаём существующей функции: она сама кладёт PDF в кэш, записывает
    # занятия и сохраняет отпечаток файла. Файл тут качается второй раз, но
    # только для действительно изменившегося — то есть раз в неделю, не чаще.
    result = await schedule_service.parse_group(code, force=True)
    if not result.has_lessons:
        errors.append(f"{code} ({name}): {result.reason}")
        return ""
    if before and await schedule_service.lessons_snapshot(code) == before:
        # Файл перезаписали, а занятия те же — уведомлять не о чем.
        return ""
    _before[code] = before
    _parsed[code] = result.schedule
    return "new" if not stamp else "changed"


# ── уведомления ───────────────────────────────────────────────────────────────
async def change_text(code: str, intro: str = "обновилось") -> str:
    """Текст уведомления: что изменилось, с какой даты и что делать студенту.

    PDF не качаем — всё нужное уже разобрано и лежит в базе. Разбор текущего
    круга (_parsed) берём, а не пересобираем из базы: в разборе есть неделя
    файла, а из строк lessons её не восстановить, и без неё в сообщении нечем
    ответить на вопрос «с какой даты это».
    """
    try:
        return await schedule_service.change_notice(code, _parsed.get(code), _before.get(code), intro)
    except Exception as exc:  # noqa: BLE001 — студент получит хотя бы заголовок
        log.warning("не удалось собрать расписание %s для уведомления: %s", code, exc)
        return f"🔔 Расписание группы {code} {intro}."


async def notify_changed(groups, intro: str = "обновилось") -> int:
    """Пишет подписчикам расписания указанных групп. Возвращает, сколько
    сообщений доставлено.

    Подписки берём из той же таблицы, что и кнопка «Подписаться» в боте
    (``repo.schedule_subscribers``), а отправляем через общий ``notify``: в
    отличие от ``handlers.admin.notify_schedule_subscribers`` он возвращает
    результат, и видно, сколько сообщений реально дошло.
    """
    sent = 0
    for raw in groups or []:
        code = group_code(as_str(raw))
        if not code:
            continue
        try:
            subscribers = await repo.schedule_subscribers(code)
        except Exception as exc:  # noqa: BLE001 — группа без уведомлений, не круга
            log.warning("не удалось получить подписчиков группы %s: %s", code, exc)
            continue
        if not subscribers:
            continue
        text = await change_text(code, intro)
        keyboard = [[btn(f"📚 Расписание {code}", f"sched:{code}")]]
        for user_id in subscribers:
            if await notify(user_id, text, keyboard):
                sent += 1
    return sent


# ── полный круг ───────────────────────────────────────────────────────────────
async def watch_once() -> dict:
    """Проверить файлы и разослать изменения. Ответ — как у check_college_updates,
    плюс ``notified`` — сколько уведомлений доставлено."""
    result = await check_college_updates()
    result["notified"] = await notify_changed(result["changed"])
    result["notified"] += await notify_changed(result["new"], "появилось")
    await _remember_check()
    # Журнал пишется каждый круг, а не только при переменах: по нему видно,
    # что слежение вообще работает (раньше не срабатывало ни разу).
    log.info("проверка расписаний: файлов %s, обновлено групп %s, уведомлений %s",
             result["checked"], len(result["changed"]) + len(result["new"]), result["notified"])
    details = f"файлов: {result['checked']}, групп обновлено: {len(result['changed'])}, " \
              f"уведомлений: {result['notified']}"
    if result["errors"]:
        details += f", ошибок: {len(result['errors'])} ({result['errors'][0][:80]})"
    try:
        await repo.log_action("bot", "проверка расписаний", details)
    except Exception as exc:  # noqa: BLE001 — запись в историю не должна ломать круг
        log.warning("не удалось записать результат проверки: %s", exc)
    if result["errors"]:
        await _alert_sysadmins("⚠️ Проверка расписаний: " + "; ".join(result["errors"][:3]))
    return result


async def _remember_check() -> None:
    """Запоминает время круга: после перезапуска бот не должен проверять заново."""
    global _checked_this_run
    _checked_this_run = clock.now()     # в память сразу: дальше решает check_due
    try:
        await db.set_setting(LAST_CHECK_KEY, clock.stamp())
    except Exception as exc:  # noqa: BLE001 — запись в настройки не должна ронять круг
        log.warning("не удалось запомнить время проверки расписаний: %s", exc)


async def load_last_check() -> None:
    """Поднимает из настроек время прошлого круга — при старте бота.

    Читает базу один раз и в фоне: первый круг из-за неё ждать не должен. Если
    время неизвестно, check_due() разрешает проверку, и это правильно — после
    перезапуска смотреть надо.
    """
    global _checked_on_start
    try:
        _checked_on_start = clock.parse(await db.get_setting(LAST_CHECK_KEY, ""))
    except Exception as exc:  # noqa: BLE001 — настройки не прочитались: лучше проверить
        log.warning("не удалось прочитать время прошлой проверки: %s", exc)


def check_due() -> bool:
    """Пора ли смотреть сайт: прошлый круг был не меньше MIN_GAP_SECONDS назад."""
    last = _checked_this_run or _checked_on_start
    if last is None:
        return True   # времени нет (первый запуск) — смотрим
    return (clock.now() - last).total_seconds() >= MIN_GAP_SECONDS


async def _alert_sysadmins(text: str) -> None:
    """Сообщает сис-админам о сбое проверки, но не спамит: одна и та же
    ошибка — не чаще раза в ALERT_COOLDOWN."""
    key = text[:80]
    now = time.monotonic()
    last = _alerts.get(key)
    if last is not None and now - last < ALERT_COOLDOWN:
        return
    _alerts[key] = now
    for admin_id in {str(value) for value in config.SYSADMIN_IDS}:
        await notify(admin_id, text)


# ── фоновый круг ──────────────────────────────────────────────────────────────
def watch_interval_hours() -> int:
    """Интервал проверки в часах: настройка из config, если она есть, иначе
    константа модуля."""
    hours = to_int(getattr(config, "SCHEDULE_WATCH_HOURS", 0))
    return hours if hours > 0 else WATCH_INTERVAL_HOURS


async def _wait(seconds: float) -> None:
    """Пауза между проверками — отдельной функцией, чтобы её можно было
    ускорить в тестах."""
    await asyncio.sleep(seconds)


async def watch_loop(interval_hours: int = 0) -> None:
    """Фоновый круг: сначала короткая пауза, потом проверка через интервал.

    Первая проверка намеренно не отложена на весь интервал: PDF меняются раз
    в неделю, а бот перезапускается часто, и раньше круг не наступал ни разу.
    При этом частая перезагрузка не превращается в поток запросов к сайту —
    круг всё равно пропускается, если прошлый был недавно (check_due).
    """
    global _checked_this_run, _checked_on_start
    period = max(1, to_int(interval_hours) or watch_interval_hours()) * 3600
    log.info("Слежение за расписанием: раз в %s ч, первая проверка через %s мин",
             period // 3600, FIRST_CHECK_SECONDS // 60)
    # Круг этого слежения ещё ничего не проверял; время прошлого круга из базы
    # поднимаем в фоне — первый круг из-за неё ждать не должен.
    _checked_this_run = None
    _checked_on_start = None
    asyncio.ensure_future(load_last_check())
    first = True
    while True:
        await _wait(FIRST_CHECK_SECONDS if first else period)
        first = False
        try:
            if check_due():
                await watch_once()
            else:
                log.info("Проверка расписаний: недавно уже смотрели, ждём следующего круга")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой сети не убивает наблюдателя
            log.error("проверка расписаний не удалась: %s", exc)


def start_watcher(interval_hours: int = 0) -> None:
    """Запускает слежение за расписанием. Повторный вызов ничего не делает:
    лишних задач (и лишних скачиваний PDF) не появится."""
    global _watcher
    if _watcher and not _watcher.done():
        log.debug("слежение за расписанием уже запущено")
        return
    hours = to_int(interval_hours) or watch_interval_hours()
    _watcher = spawn(watch_loop(hours))
    log.info("Слежение за расписанием колледжа: раз в %s ч", hours)


def stop_watcher() -> None:
    """Останавливает слежение: задача гасится сразу, а shutdown в bot.py
    дожидается фоновых задач при остановке."""
    global _watcher
    task, _watcher = _watcher, None
    if not task or task.done():
        return
    task.cancel()
    log.info("Слежение за расписанием остановлено")
