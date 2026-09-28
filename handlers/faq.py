"""FAQ-бот: бот сам отвечает на частые вопросы.

Схема работы. Вопросы и ответы лежат в таблице faq (см. database.SCHEMA), их
заводит и правит сис-админ в панели; черновик наполнения - college.DEFAULT_FAQ.
Вопросы разложены по разделам college.FAQ_CATEGORIES, раздел хранится в
колонке faq.category. Точки входа:
  * «Частые вопросы» в меню (payload «faq») - ряд кнопок разделов;
  * «faq:код» и «faq:код:страница» - вопросы одного раздела постранично,
    «faqall:страница» - все вопросы сразу;
  * «Спросить бота» - свободный вопрос: бот ищет ответ по ключевым словам и
    фрагменту вопроса во всех разделах сразу, а если не нашёл - заводит
    обращение сотруднику;
  * «Задать свой вопрос» - кнопки, ведущие в готовый сценарий обращения
    (handlers.tickets, payload sub:cert / new:feedback) - сам сценарий тут не трогаем.

Голос. Отдельного API для голоса не нужно: в MAX голосовое сообщение приходит
тем же текстом, поэтому вход один, а normalize_question() приводит разговорную
речь к обычному вопросу («какие документы?» → «какие документы», «ё» → «е»,
повтор слова убирается). Обрывок из пары слов в обращение не превращается:
бота вежливо просят сформулировать вопрос - иначе сотрудники получают мусор.

Обратная связь об ответе. Под каждым ответом есть кнопка «Это не помогло»: она
тут же заводит обращение сотруднику с темой «Не помог ответ на вопрос: …», а
адресат - сотрудник раздела, к которому вопрос относится. Повторное нажатие
обращение не плодит: второй раз бот отвечает «уже отправлено».

Главное правило: бот не выдумывает. Нашёлся ответ - показываем его, не нашёлся -
так и говорим и предлагаем написать сотруднику. Поиск устроен так, чтобы вместо
тихого «не знаю» был честный отказ: при равном балле у двух разных вопросов бот
не выбирает ни один.

Пределы MAX соблюдаются: не больше FAQ_PAGE вопросов на экран (вместе со
служебными кнопками это меньше max_api.MAX_ROWS = 30 строк), в ряду номеров не
больше NUMBER_ROWS кнопок, а подписи разделов взяты из college.FAQ_CATEGORY_BUTTONS
и режутся btn(). Поэтому вопросы в списке уходят в текст сообщения, а кнопка
несёт только номер: номер не обрезается никогда, а длинный вопрос обрезался бы
всегда.
"""
import logging
import re

import college
import config
import database as db
import repository as repo
from handlers.common import BACK, api, notify
from handlers.registry import callback, state
from max_api import btn, link_btn
from utils import STAFF_CATS, as_str, short, to_int

log = logging.getLogger("bot")

FAQ_PAGE = 12      # вопросов на экране раздела: вместе со служебными кнопками меньше 30 строк
NUMBER_ROWS = 7    # кнопок с номерами вопросов в одном ряду (MAX не любит тесные ряды)
ASK_BUTTON = "✍️ Задать свой вопрос"
CERT_BUTTON = "📄 Справки и другие документы"
STAFF_BUTTON = "💬 Написать сотруднику"
LIST_BUTTON = "☝️ Все частые вопросы"
ALL_BUTTON = "☝️ Все вопросы"
# контакты колледжа живут внутри частых вопросов: вопрос «куда звонить» частый,
# а отдельная кнопка в меню студента только размножала кнопки
COLLEGE_BUTTON = "🏫 Контакты колледжа"
SEARCH_BUTTON = "🔍 Спросить бота"
NO_ANSWER = "🤔 Точного ответа в списке частых вопросов нет - выдумывать я не буду."
VOICE_HINT = "Можно голосом — нажмите и скажите вопрос: бот поймёт его как текст."
TOO_SHORT = "🤔 Пока не понял вопрос"
MIN_QUESTION_WORDS = 3     # короче - это шум в микрофон, а не вопрос
# «Это не помогло»: жалоба на ответ уходит сотруднику раздела вопроса
NO_HELP_BUTTON = "👎 Это не помогло"
NO_HELP_TEXT = "Ответ на вопрос не подошёл"
NO_HELP_TOPIC = "Не помог ответ на вопрос: "
NO_HELP_DONE = "✅ Уже отправлено - это обращение создано один раз, второе не нужно."
STAFF_ASK_BUTTON = "📥 Спросить сотрудника"

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
TAIL_PUNCT = re.compile(r"[\s,.;:!?'\"«»()\[\]-]+$")   # хвост, который в вопросе не нужен
LEAD_PUNCT = re.compile(r"^[\s,.;:!?'\"«»()\[\]-]+")    # и начало разговора тоже


def normalize_question(text) -> str:
    """Вопрос из сообщения (в том числе голосового) в виде, который не стыдно показать.

    Голос в MAX приходит тем же текстом, но выглядит как разговор: «какие
    документы?», «какие  документы  документы», «приёмная  комиссия?». Поэтому
    убираем пунктуацию по краям, приводим «ё» к «е» (как в поиске: иначе одно
    и то же слово ищется по-разному) и схлопываем повторы слов. Регистр не
    трогаем - заглавной делаем только первую букву.
    """
    value = " ".join(as_str(text).replace("ё", "е").replace("Ё", "Е").split())
    value = TAIL_PUNCT.sub("", LEAD_PUNCT.sub("", value))
    words: list[str] = []
    for word in value.split():
        if words and word.lower() == words[-1].lower():
            continue                        # «документы документы» - это одно слово
        words.append(word)
    value = " ".join(words)
    return value[:1].upper() + value[1:]


# Колонка category появилась позже самой таблицы faq, поэтому её заводит
# наполнение, а не database.SCHEMA. Проверка идёт по имени файла базы, а не
# одним флагом на процесс: тесты и панель поднимают отдельные базы по очереди,
# и общий флаг увел бы вторую базу без колонки.
_category_ready: set[str] = set()


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

    Из текста убираются стоп-слова и одиночные буквы («а», «у» - это настроение,
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


# ── разделы и работа с таблицей faq ──────────────────────────────────────────
def category_of(row) -> str:
    """Код раздела вопроса; '' — раздел не задан (вопрос заведён до разделов)."""
    return as_str(row["category"]) if row and "category" in row.keys() else ""


async def ensure_category_column() -> None:
    """Заводит колонку category в таблице faq, если её ещё нет.

    Колонка не входит в database.SCHEMA: её знает только наполнение FAQ, и база,
    созданная до разделов, должна просто доехать до рабочего состояния. db.run
    глушит «duplicate column name», поэтому звать сколько угодно раз безопасно.
    """
    path = config.DATABASE_PATH
    if path in _category_ready:
        return
    await db.run("ALTER TABLE faq ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    _category_ready.add(path)


# Раздел обращения для кнопки «Это не помогло». Разделы FAQ
# (college.FAQ_CATEGORIES) и разделы обращений (utils.CATS) названы по-разному,
# поэтому коды переводятся таблицей: деньги - к бухгалтерии, остальное - к
# обратной связи. Внутри «Учёбы» (там и справки, и оценки) и у старых вопросов
# без раздела раздел угадывается по теме. Подсказки - начала слов, потому что
# окончания у вопросов разные: «стипендия» и «стипендию» должны совпасть.
SECTION_BY_CATEGORY = {"money": "accounting"}
SECTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("accounting", ("стипенди", "выплат", "бухгалтер", "материальн", "пособи", "аванс", "льгот")),
    ("certificates", ("справк", "диплом", "дубликат", "аттестат", "документ", "личн")),
    ("academic", ("учебн", "учеб", "оценк", "сесси", "расписан", "экзамен", "деканат", "куратор",
                  "зачет", "пересдач", "группа", "специальност", "професс", "общежит",
                  "столов", "питани", "медпункт", "медицин", "устав", "правил")),
)


def section_of(item) -> str:
    """Раздел обращения для вопроса: «accounting», «certificates», «academic» или
    «feedback» - когда тему угадать не удалось."""
    section = SECTION_BY_CATEGORY.get(category_of(item), "")
    if section:
        return section
    haystack = norm(f"{as_str(item['question'])} {as_str(item['keywords'])}")
    for section, hints in SECTION_HINTS:
        if any(hint in haystack for hint in hints):
            return section
    return "feedback"


async def _staff_for(item) -> tuple[str, dict | None]:
    """Раздел вопроса и сотрудник, которому такое обращение уходит.

    Сначала сотрудник раздела самого вопроса, иначе - любой, кто принимает
    обращения по разделу «Обратная связь». Нет ни того, ни другого - значит,
    жалобу принять некому, и это честно говорим студенту.
    """
    section = section_of(item)
    staff = await repo.staff_for_category(section, limit=1)
    if staff:
        return section, staff[0]
    if section != "feedback":
        staff = await repo.staff_for_category("feedback", limit=1)
        if staff:
            return "feedback", staff[0]
    return section, None


def _no_help_topic(question: str) -> str:
    """Тема обращения «ответ не помог»: обрезается, чтобы влезть в уведомление."""
    return f"{NO_HELP_TOPIC}{short(question, 60)}"


async def _no_help_seen(x, question: str) -> int:
    """Номер уже созданной жалобы на этот вопрос или 0.

    Ищем в базе, а не в памяти процесса: кнопку можно нажать второй раз и после
    перезапуска бота, а два одинаковых обращения в очереди сотруднику не нужны.
    """
    row = await db.one(
        "SELECT ticket_id FROM tickets WHERE student_id=? AND topic=? "
        "ORDER BY ticket_id DESC LIMIT 1", (str(x), _no_help_topic(question)))
    return to_int(row["ticket_id"]) if row else 0


async def active_items() -> list:
    """Активные вопросы в порядке показа: сначала без ключевых слов (общие), потом с ними."""
    await ensure_category_column()
    rows = await db.many("SELECT * FROM faq WHERE active=1 ORDER BY id")
    return sorted(rows, key=lambda row: bool(keywords_of(row["keywords"])))


async def items_by_category(code: str) -> list:
    """Активные вопросы одного раздела. Пустой код - все разделы сразу."""
    items = await active_items()
    code = as_str(code).strip()
    return items if not code else [row for row in items if category_of(row) == code]


async def find_item(x, text):
    """Строка faq, которая лучше всего подходит под текст, или None.

    None означает «не нашёл»: выдумывать ответ нельзя, поэтому при ничьей или
    слабом совпадении вопрос уходит сотруднику. Отдаём строку, а не только текст:
    экрану с ответом нужен id вопроса для кнопки «Это не помогло».
    """
    if not await ask_enabled() or not core_of(text):
        return None
    best, best_rows = 0, []
    for row in await active_items():
        points = score(row, text)
        if points > best:
            best, best_rows = points, [row]
        elif points == best and points >= MIN_SCORE:
            best_rows.append(row)
    if best < MIN_SCORE:
        return None
    titles = {as_str(row["question"]) for row in best_rows}
    if len(titles) > 1:
        # два разных вопроса подошли одинаково: лучше не ответить, чем ответить не
        # на тот вопрос - так и говорим студенту
        log.info("FAQ: неоднозначный вопрос от %s (%s): %s", x, best, "; ".join(sorted(titles)))
        return None
    return best_rows[0]


async def find_match(x, text) -> tuple[str, str, str]:
    """Ищет ответ на свободный вопрос по всем разделам.

    Возвращает (ответ, сам вопрос, код раздела) или ("", "", ""). Раздел в ответе
    нужен, чтобы человек сразу видел, из какого раздела взят ответ: вопросы
    разложены по темам, и без подписи «стипендия» и «общежитие» выглядят
    одинаково.
    """
    item = await find_item(x, text)
    if not item:
        return "", "", ""
    return as_str(item["answer"]), as_str(item["question"]), category_of(item)


async def find_answer(x, text) -> tuple[str, str]:
    """Ищет ответ на свободный вопрос. Возвращает (ответ, заголовок) или ("", "").

    Пустой результат означает «не нашёл»: выдумывать ответ нельзя, поэтому при
    ничьей или слабом совпадении вопрос уходит сотруднику.
    """
    answer, title, _section = await find_match(x, text)
    return answer, title


def answer_title(title: str, section: str) -> str:
    """Заголовок ответа с разделом: «❓ Поступление: Какие документы нужны?»."""
    label = college.category_label(section)
    return f"❓ {label}: {title}" if label else f"❓ {title}"


async def ask_enabled() -> bool:
    """Включены ли ответы на частые вопросы (настройка faq_enabled)."""
    return await db.get_setting("faq_enabled", "1") == "1"


async def set_ask_enabled(value: bool) -> None:
    """Переключить ответы на частые вопросы - из панели."""
    await db.set_setting("faq_enabled", "1" if value else "0")
    log.info("FAQ: ответы на частые вопросы %s", "включены" if value else "выключены")


async def seed_defaults() -> int:
    """Залить черновик college.DEFAULT_FAQ в таблицу faq. Возвращает, сколько реально добавилось.

    Существующие вопросы не трогает (сравнение по тексту вопроса), поэтому кнопку
    можно нажимать повторно и правки сис-админа не затираются. Повторный запуск
    на заполненной таблице возвращает 0 - иначе в журнале при каждом старте
    врётся «добавлено 50». Раздел у уже заведённых вопросов дописывается:
    до разделов он был пустым, и без этого вопросы не попали бы ни в один раздел.
    """
    await ensure_category_column()
    added = 0
    for item in college.DEFAULT_FAQ:
        # run_count возвращает, сколько строк запрос реально изменил: вставка
        # с WHERE NOT EXISTS не сделает ничего, если такой вопрос уже есть
        added += await db.run_count(
            "INSERT INTO faq(question, answer, keywords, category) SELECT ?, ?, ?, ? "
            "WHERE NOT EXISTS(SELECT 1 FROM faq WHERE question=?)",
            (item["question"], item["answer"], item["keywords"], item.get("category", ""),
             item["question"]),
        )
        await db.run(
            "UPDATE faq SET category=? WHERE question=? AND (category='' OR category IS NULL)",
            (item.get("category", ""), item["question"]),
        )
    return added


# ── меню и ответы ────────────────────────────────────────────────────────────
def _answer_of(item) -> str:
    """Текст ответа. Пустой ответ - тоже дефект, о нём лучше сказать прямо."""
    return as_str(item["answer"]).strip() or "Ответ на этот вопрос ещё не заполнен."


def answer_kb(item_id) -> list:
    """Кнопки под ответом: и жалоба на ответ, и путь к сотруднику.

    Список один для всех экранов с ответом (вопрос из списка, автоответ, поиск):
    ответ один - значит, и кнопки под ним одинаковые. Пять рядов укладываются
    в предел max_api.MAX_ROWS с большим запасом.
    """
    return [[link_btn("Сайт колледжа", college.SITE)],
            [btn(NO_HELP_BUTTON, f"faqno:{item_id}")],
            [btn(ASK_BUTTON, "new:feedback")],
            [btn(CERT_BUTTON, "sub:cert")],
            [btn(LIST_BUTTON, "faq")], *BACK]


async def answer_text(x, text) -> bool:
    """Ответ на вопрос, написанный обычным сообщением. False - ответа нет.

    Отдельный вход для menus.on_message: там, где раньше был только отказ
    «используйте кнопки», теперь человек получает ответ сразу.

    Текст сперва приводится к виду нормализованного вопроса (голос приходит тем
    же текстом), а если после этого ничего не нашлось - пробуем как есть: заменить
    найденный ответ на отказ хуже, чем поискать ещё раз.
    """
    question = normalize_question(text)
    item = await find_item(x, question) or (None if question == text else await find_item(x, text))
    if not item:
        return False
    await api.send(x, f"{answer_title(as_str(item['question']), category_of(item))}\n\n"
                      f"{_answer_of(item)}", answer_kb(item["id"]))
    return True


@callback("faq")
async def cb_faq(x, arg):
    """«Частые вопросы»: без аргумента - разделы, с кодом - вопросы этого раздела.

    Разбор аргумента терпимый: «faq:admission» и «faq:admission:2» - это раздел
    и его вторая страница, а «faq:2» (старая разметка, осталась в сообщениях
    бота) - вторая страница общего списка.
    """
    if not await ask_enabled():
        return await api.send(x, "❓ Частые вопросы сейчас выключены сис-админом.", BACK)
    items = await active_items()
    if not items:
        return await api.send(
            x, "❓ Частые вопросы пока не заполнены. Напишите вопрос сотруднику - "
               "он ответит и, возможно, войдёт в список.",
            [[btn(STAFF_BUTTON, "new:feedback")],
             [btn(COLLEGE_BUTTON, "college")], *BACK])
    arg = as_str(arg or "").strip()
    if not arg:
        return await _sections_menu(x, items)
    code, _, rest = arg.partition(":")
    if to_int(code, -1) >= 0:                  # «faq:2» - страница общего списка
        return await _question_page(x, items, to_int(code, 0), "faqall", "")
    return await _question_page(x, await items_by_category(code),
                                to_int(rest, 0) if rest else 0, "faq", code)


@callback("faqall")
async def cb_faq_all(x, arg):
    """Все активные вопросы сразу, постранично: «faqall:2» - вторая страница."""
    if not await ask_enabled():
        return await api.send(x, "❓ Частые вопросы сейчас выключены сис-админом.", BACK)
    items = await active_items()
    if not items:
        return await cb_faq(x, "")
    page = max(0, to_int(str(arg or "").replace("faqall:", "", 1)))
    return await _question_page(x, items, page, "faqall", "")


async def _question_page(x, items: list, page: int, back_payload: str, section: str) -> None:
    """Экран со списком вопросов: текстом, а кнопкой - только номер.

    Вопрос в подписи кнопки обрезался бы многоточием почти всегда, поэтому он
    уходит в текст сообщения, а кнопка остаётся короткой и всегда рабочей.
    """
    if not items:
        return await api.send(
            x, "❓ В этом разделе пока нет вопросов - выберите другой или спросите бота.",
            [[btn(ALL_BUTTON, "faqall:0")], [btn(SEARCH_BUTTON, "faqask")], *BACK])
    pages = max(1, -(-len(items) // FAQ_PAGE))
    page = min(max(0, page), pages - 1)
    chunk = items[page * FAQ_PAGE:(page + 1) * FAQ_PAGE]
    first = page * FAQ_PAGE + 1
    title = f"Вопросы раздела «{college.category_label(section)}»" if section else "Все частые вопросы"
    lines = [f"{number}. {as_str(item['question'])}"
             for number, item in enumerate(chunk, start=first)]
    keyboard = []
    for start in range(0, len(chunk), NUMBER_ROWS):
        group = list(range(start + first, start + first + min(NUMBER_ROWS, len(chunk) - start)))
        keyboard.append([btn(str(number), f"faqq:{chunk[number - first]['id']}") for number in group])
    # Переходы: «назад» без «вперёд» - это ряд из одной кнопки, MAX такой допускает,
    # и на последней странице лишней кнопки не появляется.
    if section:
        def step(number: int) -> str:
            return f"{back_payload}:{section}:{number}"
    else:
        def step(number: int) -> str:
            return f"{back_payload}:{number}"
    nav = [btn("◀️ Назад", step(page - 1))] if page else []
    if page < pages - 1:
        nav.append(btn("Вперёд ▶️", step(page + 1)))
    if nav:
        keyboard.append(nav)
    keyboard += [[btn(ALL_BUTTON, "faqall:0")],
                 [btn(SEARCH_BUTTON, "faqask")],
                 [btn(STAFF_BUTTON, "new:feedback")],
                 [btn(COLLEGE_BUTTON, "college")], *BACK]
    head = f"❓ {title} ({len(items)})"
    if pages > 1:
        head += f", страница {page + 1} из {pages}"
    return await api.send(x, f"{head}: выберите номер\n\n" + "\n".join(lines), keyboard)


async def _sections_menu(x, items: list | None = None) -> None:
    """Меню разделов: ряд кнопок на раздел плюс служебные кнопки.

    Разделы без вопросов в меню не показываем: кнопка, которая ничего не
    открывает, только отнимает место на экране телефона.
    """
    items = items if items is not None else await active_items()
    codes = college.FAQ_CATEGORY_CODES
    grouped = [(code, title, [row for row in items if category_of(row) == code])
               for code, title in college.FAQ_CATEGORIES]
    grouped = [(code, title, rows) for code, title, rows in grouped if rows]
    leftover = [row for row in items if category_of(row) not in codes]
    keyboard = []
    for start in range(0, len(grouped), 2):
        row = [btn(college.FAQ_CATEGORY_BUTTONS.get(code, title), f"faq:{code}")
               for code, title, _rows in grouped[start:start + 2]]
        keyboard.append(row)
    lines = [f"{college.category_icon(code)} {title} — {len(rows)}".lstrip()
             for code, title, rows in grouped]
    if leftover:
        lines.append(f"Вне разделов — {len(leftover)}")
    keyboard += [[btn(ALL_BUTTON, "faqall:0")],
                 [btn(SEARCH_BUTTON, "faqask")],
                 [btn(STAFF_BUTTON, "new:feedback")],
                 [btn(COLLEGE_BUTTON, "college")], *BACK]
    return await api.send(
        x, f"❓ Частые вопросы: {len(items)}. Выберите раздел — или задайте вопрос боту.\n\n"
           + "\n".join(lines),
        keyboard)


@callback("faqq")
async def cb_faq_item(x, arg):
    """Один вопрос из списка: ответ целиком и кнопки в сценарий обращения."""
    item_id = to_int(str(arg or "").replace("faqq:", "", 1))
    item = await db.one("SELECT * FROM faq WHERE id=? AND active=1", (item_id,))
    if not item:
        return await api.send(x, "Вопрос не найден - возможно, его убрали из списка.",
                              [[btn(LIST_BUTTON, "faq")], *BACK])
    question = " ".join(as_str(item["question"]).split())
    return await api.send(x, f"{answer_title(question, category_of(item))}\n\n"
                             f"{_answer_of(item)}", answer_kb(item_id))


@callback("faqask")
async def cb_faq_ask(x, arg):
    """Кнопка «Спросить бота»: ждём свободный вопрос."""
    if not await ask_enabled():
        return await api.send(x, "❓ Частые вопросы сейчас выключены сис-админом.", BACK)
    await db.set_state(x, "faq_ask", {})
    return await api.send(
        x, "🔍 Напишите вопрос своими словами или скажите его голосом.\n"
           f"{VOICE_HINT}\nБот поищет ответ в списке частых вопросов - если не "
           "найдёт, отправит вопрос сотруднику.",
        [[btn("❌ Отмена", "home")]])


@state("faq_ask")
async def st_faq_ask(x, text, p):
    """Свободный вопрос: нашли ответ - отвечаем, не нашли - заводим обращение.

    Голос и клавиатура приходят в это состояние одинаково, поэтому вопрос сперва
    приводится к общему виду: «какие  документы??» и «какие документы документы»
    должны попасть в один и тот же ответ.
    """
    from handlers.menus import need_author  # импорт внутри: menus сам импортирует faq

    await db.clear_state(x)
    question = normalize_question(text)
    if not question:
        return await api.send(x, "Вопрос пустой - напишите его хотя бы парой слов.", BACK)
    if not await need_author(x):
        return
    item = await find_item(x, question)
    if item:
        return await api.send(
            x, f"{answer_title(as_str(item['question']), category_of(item))}\n\n"
               f"{_answer_of(item)}\n\nЭто ответ из списка частых вопросов; если нужно "
               "уточнить - напишите сотруднику.",
            answer_kb(item["id"]))
    if len(core_of(question)) < MIN_QUESTION_WORDS:
        # голосом нередко прилетает обрывок («ааа», «ну», «какие»): лучше попросить
        # сформулировать, чем слать сотруднику пустое обращение
        return await api.send(
            x, f"{TOO_SHORT}: «{short(question, 60)}».\nСформулируйте вопрос целиком - "
               "например «где», «когда» или «какие», - и я поищу ответ или отправлю "
               "вопрос сотруднику.",
            [[btn(SEARCH_BUTTON, "faqask")], [btn(LIST_BUTTON, "faq")], *BACK])
    return await _ask_staff(x, question)


@callback("faqno")
async def cb_faq_no(x, arg):
    """«Это не помогло»: благодарим и тут же заводим обращение сотруднику.

    Отдельного шага «опишите, что именно не подошло» здесь нет: короткая пометка
    сотруднику полезнее, чем вопрос, на который он уже ответил. Повторное нажатие
    обращение не плодит - иначе в очереди окажется два одинаковых, и смысла в
    кнопке не будет.
    """
    from handlers.menus import need_author  # импорт внутри: menus сам импортирует faq

    item_id = to_int(str(arg or "").replace("faqno:", "", 1))
    item = await db.one("SELECT * FROM faq WHERE id=? AND active=1", (item_id,))
    if not item:
        return await api.send(x, "Вопрос не найден - возможно, его убрали из списка.",
                              [[btn(LIST_BUTTON, "faq")], *BACK])
    question = " ".join(as_str(item["question"]).split())
    answer = _answer_of(item)
    if await _no_help_seen(x, question):
        # тот же экран с ответом, только с пометкой: человек видит, что его поняли,
        # и не гадает, куда делось его обращение
        return await api.send(x, f"{answer_title(question, category_of(item))}\n\n{answer}"
                                  f"\n\n{NO_HELP_DONE}", answer_kb(item_id))
    if not await need_author(x):
        return
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(
            x, "Приём обращений временно отключён, отметить ответ как неподошедший не "
               f"получится. Телефон учебной части: "
               f"{await college.get('телефон_учебная_часть')}.",
            [[btn(LIST_BUTTON, "faq")], *BACK])
    section, target = await _staff_for(item)
    if not target:
        log.warning("FAQ: жалоба на ответ от %s, но сотрудник раздела %s не назначен", x, section)
        return await api.send(
            x, "Спасибо за сигнал! Сотрудник, отвечающий за этот раздел, не назначен - "
               f"позвоните в учебную часть: {await college.get('телефон_учебная_часть')}.",
            [[btn(LIST_BUTTON, "faq")], *BACK])
    ticket_id = await repo.create_ticket(
        x, target["user_id"], section, NO_HELP_TEXT, topic=_no_help_topic(question))
    from handlers.tickets import ticket_kb  # импорт внутри: избегаем цикла с menus

    delivered = await notify(
        target["user_id"],
        f"🔔 Ответ на вопрос не подошёл\nОт: {x}\n\nВопрос: {question}\n\n"
        f"Обращение №{ticket_id}",
        ticket_kb(await repo.get_ticket(ticket_id), True),
    )
    log.info("FAQ: ответ не помог (%s), обращение №%s в раздел %s", question, ticket_id, section)
    lines = [f"✅ Спасибо! Я отметил, что ответ не подошёл, и завёл обращение "
             f"№{ticket_id} - {STAFF_CATS.get(section, section)}."]
    if not delivered:
        lines.append("\n⚠️ Сотрудник пока не запускал бота - уведомление не дошло, но "
                     "обращение сохранено, и он увидит его в панели.")
    return await api.send(x, "\n".join(lines),
                          [[btn("📋 Мои обращения", "tickets")],
                           [btn(LIST_BUTTON, "faq")], *BACK])


async def _ask_staff(x: str, question: str) -> None:
    """Ответа в списке нет: честно говорим об этом и заводим обращение сотруднику."""
    staff = await repo.staff_for_category("feedback", limit=1)
    if not staff:
        log.warning("FAQ: вопрос без ответа от %s, но сотрудник для обращений не назначен", x)
        return await api.send(
            x, f"{NO_ANSWER}\nОбращения сейчас некому передать - позвоните в учебную "
               f"часть: {await college.get('телефон_учебная_часть')}.",
            [[btn(STAFF_ASK_BUTTON, "new:feedback")],
             [btn(SEARCH_BUTTON, "faqask")], *BACK])
    if await db.get_setting("tickets_enabled", "1") != "1":
        return await api.send(
            x, f"{NO_ANSWER}\nПриём обращений временно отключён. Телефон учебной части: "
               f"{await college.get('телефон_учебная_часть')}.",
            [[btn(STAFF_ASK_BUTTON, "new:feedback")],
             [btn(LIST_BUTTON, "faq")], *BACK])
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
                           [btn(STAFF_ASK_BUTTON, "new:feedback")],
                           [btn(LIST_BUTTON, "faq")], *BACK])
