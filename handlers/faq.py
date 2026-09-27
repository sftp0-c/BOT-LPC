"""FAQ-бот: бот сам отвечает на частые вопросы.

Схема работы. Вопросы и ответы лежат в таблице faq (см. database.SCHEMA), их
заводит и правит сис-админ в панели; черновик наполнения - college.DEFAULT_FAQ.
Точки входа:
  * «Частые вопросы» в меню - список активных вопросов кнопками, постранично;
  * «Спросить бота» - свободный вопрос: бот ищет ответ по ключевым словам и
    фрагменту вопроса, а если не нашёл - заводит обращение сотруднику;
  * «Задать свой вопрос» - кнопки, ведущие в готовый сценарий обращения
    (handlers.tickets, payload sub:cert / new:feedback) - сам сценарий тут не трогаем.

Главное правило: бот не выдумывает. Нашёлся ответ - показываем его, не нашёлся -
так и говорим и предлагаем написать сотруднику. Поиск устроен так, чтобы вместо
тихого «не знаю» был честный отказ: при равном балле у двух разных вопросов бот
не выбирает ни один.

Пределы MAX соблюдаются: не больше FAQ_PAGE вопросов на экран (вместе со
служебными кнопками это меньше max_api.MAX_ROWS = 30 строк) и не больше двух
кнопок в ряду.
"""
import logging
import re

import college
import database as db
import repository as repo
from handlers.common import BACK, api, notify
from handlers.registry import callback, state
from max_api import btn, link_btn
from utils import as_str, short, to_int

log = logging.getLogger("bot")

FAQ_PAGE = 12      # вопросов на экране меню: вместе со служебными кнопками меньше 30 строк
ASK_BUTTON = "✍️ Задать свой вопрос"
CERT_BUTTON = "📄 Справки и другие документы"
STAFF_BUTTON = "💬 Написать сотруднику"
LIST_BUTTON = "☝️ Все частые вопросы"
SEARCH_BUTTON = "🔍 Спросить бота"
NO_ANSWER = "🤔 Точного ответа в списке частых вопросов нет - выдумывать я не буду."

# Вес совпадений. Порог MIN_SCORE отсекает единичные общие слова, а разбор по
# баллам с «покрытием сообщения» ловит короткие запросы вида «стипендия»,
# где в вопросе всего одно слово.
ALL_KEYWORDS = 10      # нашлись все ключевые слова записи
MOST_KEYWORDS = 6      # нашлась большая часть ключевых слов
TWO_KEYWORDS = 4       # нашлись два ключевых слова
ONE_KEYWORD = 2        # нашлось одно ключевое слово
QUESTION_TAIL = 5      # в сообщении есть все слова вопроса
QUESTION_HALF = 3      # в сообщении есть два и больше слов вопроса
QUERY_ALL = 8          # сообщение объяснено этой записью целиком
QUERY_MOST = 3         # объяснена как минимум половина слов сообщения
MIN_SCORE = 5
MIN_STEM = 4           # короче этого не режем: «тест» и «тестировщик» должны различаться
STEM_PASSES = 2        # сколько окончаний снимаем: «стипендию» → «стипенди» → «стипенд»
# Окончания отбрасываются от длинных к коротким, чтобы «стипендию» и «стипендия»
# стали одним словом, а «приёмная» и «приёма» остались разными. Отрез по
# префиксу здесь не годится: у этих слов первые буквы совпадают.
ENDINGS: tuple[str, ...] = (
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему", "ая", "яя",
    "ое", "ее", "ые", "ие", "ах", "ях", "ов", "ев", "ую", "юю", "ия", "ья",
    "ье", "ой", "ей", "ый", "ам", "ям", "ом", "ем", "ла", "ло", "ли", "ть",
    "ся", "сь", "ул", "юл",
    "а", "я", "ы", "и", "о", "е", "у", "ю", "ь", "й",
)
STOP_WORDS = frozenset({
    "как", "какой", "какая", "какие", "где", "когда", "кто", "что", "чем", "сколько",
    "это", "этого", "этом", "если", "для", "под", "над", "про", "или", "не", "нет", "да",
    "мне", "нам", "вам", "нас", "вас", "их", "им", "мой", "моя", "мои", "моё", "наш",
    "ваш", "свой", "себя", "себе", "все", "всё", "еще", "ещё", "уже", "ещё", "тоже",
    "там", "тут", "вот", "быть", "есть", "была", "были", "зачем", "почему", "очень",
    "также", "нужны", "нужен", "нужна", "нужно", "можно", "надо", "который", "которая",
    "которое", "которые", "вроде", "ещё", "сейчас", "пожалуйста",
    "вы", "ваш", "ваша", "ваше", "тебя", "меня", "него", "нее", "её", "чем", "будет",
})
MIN_WORD = 4           # слова короче этого в счёт не идут: «всё», «для» и подобные


def norm(text) -> str:
    """Нормализованный текст: нижний регистр, «ё» → «е», только буквы и цифры."""
    value = as_str(text).lower().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", value).split())


def stem(word: str) -> str:
    """Отбрасывает окончания, чтобы «стипендию» и «стипендия» совпали, а «приёмная»
    и «приёма» - нет. Снятие идёт в несколько проходов, потому что слово может
    оканчиваться сразу на два («условий» → «услови» → «услов»)."""
    for _ in range(STEM_PASSES):
        for ending in ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
                word = word[:-len(ending)]
                break
        else:
            break
    return word


def keywords_of(value) -> list[str]:
    """Ключевые слова из поля keywords: слова через запятую (можно и через точку с запятой)."""
    return [word for word in (norm(part) for part in as_str(value).replace(";", ",").split(",")) if word]


def words_of(text) -> list[str]:
    """Значимые длинные слова текста: без коротких служебных и стоп-слов.

    Короткие слова отбрасываются, потому что для сравнения с вопросом они шума
    больше пользы, но для «покрытия сообщения» (см. score) учитываются все - иначе
    запрос из одного «огэ» или «СНИЛС» не нашёл бы нужный вопрос вовсе.
    """
    return [word for word in norm(text).split() if len(word) >= MIN_WORD and word not in STOP_WORDS]


def core_of(text) -> list[str]:
    """Слова сообщения, по которым судим о совпадении.

    Из текста убираются стоп-слова и одиночные буквы («а», «у» - это о настроении,
    а не о вопросе), но трёхбуквенные сокращения вроде «огэ» и «инн» остаются: иначе
    вопрос из одного такого слова не нашёл бы нужную запись.
    """
    return [word for word in norm(text).split() if len(word) > 1 and word not in STOP_WORDS]


def find_in(needles: list[str], haystack: str, stems: set[str]) -> list[str]:
    """Какие из needles встретились в тексте: слова - по стволу, фразы - все слова сразу.

    Второй способ (фраза целиком в тексте) нужен для коротких слов вроде «огэ»:
    в стволы сообщения они не попадают.
    """
    padded = f" {haystack} "
    found = []
    for needle in needles:
        words = needle.split()
        if {stem(word) for word in words} <= stems or f" {' '.join(words)} " in padded:
            found.append(needle)
    return found


def score(row, text: str) -> int:
    """Насколько вопрос из строки faq подходит к тексту сообщения. Больше - лучше.

    Ключевые слова важнее слов вопроса: на «когда принимаете документы» важно
    попадание в «приёмная комиссия», а не совпадение общего слова.
    """
    haystack = norm(text)
    query = core_of(haystack)
    stems = {stem(word) for word in query}
    hits = find_in(keywords_of(row["keywords"]), haystack, stems)
    tail_hits = find_in(words_of(row["question"]), haystack, stems)
    points = 0
    if hits and len(hits) == len(keywords_of(row["keywords"])):
        points += ALL_KEYWORDS
    elif hits and len(hits) * 2 >= len(keywords_of(row["keywords"])):
        points += MOST_KEYWORDS
    elif len(hits) >= 2:
        points += TWO_KEYWORDS
    elif hits:
        points += ONE_KEYWORD
    if len(tail_hits) >= 2 and len(tail_hits) == len(words_of(row["question"])):
        points += QUESTION_TAIL
    elif len(tail_hits) >= 2:
        points += QUESTION_HALF
    # «Покрытие сообщения»: короткий вопрос из одного слова - это почти точное
    # попадание в ключевое слово, и наоборот, длинный вопрос из разных тем
    # половиной совпадений не должен выглядеть как ответ.
    if query:
        # Покрытым считаем слово, если оно нашлось в ключевом слове или в вопросе.
        # Разбираем найденное по словам: «часы приёма» - это два слова, а не одно.
        covered = {stem(word) for needle in hits + tail_hits for word in needle.split()}
        got = sum(1 for word in query if stem(word) in covered)
        if got == len(query):
            points += QUERY_ALL
        elif got * 2 >= len(query):
            points += QUERY_MOST
    return points


# ── работа с таблицей faq ─────────────────────────────────────────────────────
async def active_items() -> list:
    """Активные вопросы в порядке показа: сначала без ключевых слов (общие), потом с ними."""
    rows = await db.many("SELECT * FROM faq WHERE active=1 ORDER BY id")
    return sorted(rows, key=lambda row: bool(keywords_of(row["keywords"])))


async def find_answer(x, text) -> tuple[str, str]:
    """Ищет ответ на свободный вопрос. Возвращает (ответ, заголовок) или ("", "").

    Пустой результат означает «не нашёл»: выдумывать ответ нельзя, поэтому при
    ничьей или слабом совпадении вопрос уходит сотруднику.
    """
    if not await ask_enabled() or not core_of(text):
        return "", ""
    best, best_rows = 0, []
    for row in await active_items():
        points = score(row, text)
        if points > best:
            best, best_rows = points, [row]
        elif points == best and points >= MIN_SCORE:
            best_rows.append(row)
    if best < MIN_SCORE:
        return "", ""
    titles = {as_str(row["question"]) for row in best_rows}
    if len(titles) > 1:
        # два разных вопроса подошли одинаково: лучше не ответить, чем ответить не
        # на тот вопрос - так и говорим студенту
        log.info("FAQ: неоднозначный вопрос от %s (%s): %s", x, best, "; ".join(sorted(titles)))
        return "", ""
    return as_str(best_rows[0]["answer"]), as_str(best_rows[0]["question"])


async def ask_enabled() -> bool:
    """Включены ли ответы на частые вопросы (настройка faq_enabled)."""
    return await db.get_setting("faq_enabled", "1") == "1"


async def set_ask_enabled(value: bool) -> None:
    """Переключить ответы на частые вопросы - из панели."""
    await db.set_setting("faq_enabled", "1" if value else "0")
    log.info("FAQ: ответы на частые вопросы %s", "включены" if value else "выключены")


async def seed_defaults() -> int:
    """Залить черновик college.DEFAULT_FAQ в таблицу faq. Возвращает, сколько записей в списке.

    Существующие вопросы не трогает (сравнение по тексту вопроса), поэтому кнопку
    можно нажимать повторно и правки сис-админа не затираются.
    """
    for item in college.DEFAULT_FAQ:
        await db.run(
            "INSERT INTO faq(question, answer, keywords) SELECT ?, ?, ? "
            "WHERE NOT EXISTS(SELECT 1 FROM faq WHERE question=?)",
            (item["question"], item["answer"], item["keywords"], item["question"]),
        )
    return len(college.DEFAULT_FAQ)


# ── меню и ответы ────────────────────────────────────────────────────────────
async def answer_text(x, text) -> bool:
    """Ответ на вопрос, написанный обычным сообщением. False - ответа нет.

    Отдельный вход для menus.on_message: там, где раньше был только отказ
    «используйте кнопки», теперь человек получает ответ сразу.
    """
    answer, title = await find_answer(x, text)
    if not answer:
        return False
    await api.send(
        x, f"❓ {title}\n\n{answer}",
        [[link_btn("Сайт колледжа", college.SITE)],
         [btn(ASK_BUTTON, "new:feedback")],
         [btn(LIST_BUTTON, "faq")], *BACK],
    )
    return True


@callback("faq")
async def cb_faq(x, arg):
    """Меню частых вопросов: кнопки из активных вопросов, постранично."""
    if not await ask_enabled():
        return await api.send(x, "❓ Частые вопросы сейчас выключены сис-админом.", BACK)
    items = await active_items()
    if not items:
        return await api.send(
            x, "❓ Частые вопросы пока не заполнены. Напишите вопрос сотруднику - "
               "он ответит и, возможно, войдёт в список.",
            [[btn(STAFF_BUTTON, "new:feedback")], *BACK])
    pages = max(1, -(-len(items) // FAQ_PAGE))
    page = min(max(0, to_int(str(arg or "").replace("faq:", "", 1))), pages - 1)
    chunk = items[page * FAQ_PAGE:(page + 1) * FAQ_PAGE]
    keyboard = [[btn(short(as_str(item["question"]), 60), f"faqq:{item['id']}")] for item in chunk]
    # Переходы: «назад» без «вперёд» - это ряд из одной кнопки, MAX такой допускает,
    # и на последней странице лишней кнопки не появляется.
    nav = [btn("◀️ Назад", f"faq:{page - 1}")] if page else []
    if page < pages - 1:
        nav.append(btn("Вперёд ▶️", f"faq:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard += [[btn(SEARCH_BUTTON, "faqask")],
                 [btn(STAFF_BUTTON, "new:feedback")], *BACK]
    title = f"❓ Частые вопросы: {len(items)}"
    if pages > 1:
        title += f", страница {page + 1} из {pages}"
    return await api.send(x, f"{title}\nВыберите вопрос:", keyboard)


@callback("faqq")
async def cb_faq_item(x, arg):
    """Один вопрос из меню: ответ целиком и кнопки в сценарий обращения."""
    item_id = to_int(str(arg or "").replace("faqq:", "", 1))
    item = await db.one("SELECT * FROM faq WHERE id=? AND active=1", (item_id,))
    if not item:
        return await api.send(x, "Вопрос не найден - возможно, его убрали из списка.",
                              [[btn(LIST_BUTTON, "faq")], *BACK])
    question = as_str(item["question"])
    answer = as_str(item["answer"]).strip() or "Ответ на этот вопрос ещё не заполнен."
    keyboard = [[link_btn("Сайт колледжа", college.SITE)],
                [btn(ASK_BUTTON, "new:feedback")],
                [btn(CERT_BUTTON, "sub:cert")],
                [btn(LIST_BUTTON, "faq")], *BACK]
    return await api.send(x, f"❓ {question}\n\n{answer}", keyboard)


@callback("faqask")
async def cb_faq_ask(x, arg):
    """Кнопка «Спросить бота»: ждём свободный вопрос."""
    if not await ask_enabled():
        return await api.send(x, "❓ Частые вопросы сейчас выключены сис-админом.", BACK)
    await db.set_state(x, "faq_ask", {})
    return await api.send(
        x, "🔍 Напишите вопрос своими словами. Бот поищет ответ в списке частых "
           "вопросов - если не найдёт, отправит вопрос сотруднику.",
        [[btn("❌ Отмена", "home")]])


@state("faq_ask")
async def st_faq_ask(x, text, p):
    """Свободный вопрос: нашли ответ - отвечаем, не нашли - заводим обращение."""
    from handlers.menus import need_author  # импорт внутри: menus сам импортирует faq

    await db.clear_state(x)
    question = " ".join(as_str(text).split())
    if not question:
        return await api.send(x, "Вопрос пустой - напишите его хотя бы парой слов.", BACK)
    if not await need_author(x):
        return
    answer, title = await find_answer(x, question)
    if answer:
        return await api.send(
            x, f"❓ {title}\n\n{answer}\n\nЭто ответ из списка частых вопросов; если "
               "нужно уточнить - напишите сотруднику.",
            [[btn(ASK_BUTTON, "new:feedback")], [btn(LIST_BUTTON, "faq")], *BACK])
    return await _ask_staff(x, question)


async def _ask_staff(x: str, question: str) -> None:
    """Ответа в списке нет: честно говорим об этом и заводим обращение сотруднику."""
    staff = await repo.staff_for_category("feedback", limit=1)
    if not staff:
        log.warning("FAQ: вопрос без ответа от %s, но сотрудник для обращений не назначен", x)
        return await api.send(
            x, f"{NO_ANSWER}\nОбращения сейчас некому передать - позвоните в учебную "
               f"часть: {await college.get('телефон_учебная_часть')}.",
            [[btn(SEARCH_BUTTON, "faqask")], *BACK])
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(
            x, f"{NO_ANSWER}\nПриём обращений временно отключён. Телефон учебной части: "
               f"{await college.get('телефон_учебная_часть')}.",
            [[btn(LIST_BUTTON, "faq")], *BACK])
    target = staff[0]
    ticket_id = await repo.create_ticket(
        x, target["user_id"], "feedback", question, topic=f"Вопрос из FAQ: {short(question, 60)}")
    from handlers.tickets import ticket_kb  # импорт внутри: избегаем цикла с menus

    delivered = await notify(
        target["user_id"],
        f"🔔 Новый вопрос без ответа в FAQ\nОт: {x}\n\n{question}\n\nОбращение №{ticket_id}",
        ticket_kb(await repo.get_ticket(ticket_id), True),
    )
    lines = [NO_ANSWER, f"✅ Вопрос отправлен сотруднику как обращение №{ticket_id}."]
    if not delivered:
        lines.append("\n⚠️ Сотрудник пока не запускал бота - уведомление не дошло, но "
                     "обращение сохранено и он увидит его в панели.")
    lines.append(f"\nТелефон учебной части: {await college.get('телефон_учебная_часть')}")
    return await api.send(x, "\n".join(lines),
                          [[btn("📋 Мои обращения", "tickets")],
                           [btn(LIST_BUTTON, "faq")], *BACK])
