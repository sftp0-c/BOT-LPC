"""Проверки упрощения обращений: очередь, карточка, подменю, шаблоны.

Задачи владельца, под которые писались проверки
----------------------------------------------
1. «Фильтры не такая важная вещь, занимает очень много кнопок, которые сбивают
   с толку.» - фильтров и счётчиков на экране быть не должно.
2. «Нет понятного ответить и потом закрыть... чтобы легко было взять обращение,
   обработать и закрыть, чтобы оно ушло из списка.» - «Ответ и закрыть» первое.
3. «В шаблонах текст обрезан.» - в списке видно начало текста, а не только
   название.
4. Найденный баг: «Ждут ответа» и «В работе» не фильтровали, а подпись обещала
   отфильтрованное, и рядом стояли противоречащие числа.

Кнопки берутся от живых функций, а не из ожиданий: так проверка ломается вместе
с экраном, а не молча про него забывает.
"""
import database as db
import repository as repo

from handlers import tickets as t

from conftest import add_staff, press

OWNER = "1"


async def _новое_обращение(category="certificates", target=OWNER):
    """Создаёт обращение прямо в базе - быстрее и надёжнее, чем через бота."""
    tid = await db.run(
        "INSERT INTO tickets(student_id, target_admin_id, category, text_content, status) "
        "VALUES(?,?,?,?, 'new')", ("100", target, category, "Нужна справка"))
    return await db.one("SELECT * FROM tickets WHERE ticket_id=?", (int(tid),))


def _подписи(клавиатура: list) -> list:
    return [b["text"] for ряд in клавиатура for b in ряд]


def _адреса(клавиатура: list) -> list:
    return [b["payload"] for ряд in клавиатура for b in ряд]


def _тикеты(клавиатура: list) -> list:
    return [a.split(":")[1] for a in _адреса(клавиатура) if a.startswith("t:")]


async def _системник():
    await add_staff(OWNER, "Владелец Тестовый")


# ── 1. очередь: ни фильтров, ни счётчиков ───────────────────────────────
async def test_queue_has_no_filters_and_no_counter_wall(api):
    """На экране очереди нет ни фильтров, ни строки из пяти счётчиков.

    Владелец: фильтры «не такая важная вещь», занимают много кнопок и сбивают с
    толку. Раньше было четыре кнопки-фильтра, архив, по отделам, обновить,
    «только мои» и «сбросить фильтр» - девять кнопок обслуживания.
    """
    await _системник()
    await _новое_обращение()

    await press(OWNER, "staff")

    текст, клавиатура = api.last(OWNER)[1], api.last(OWNER)[2] or []

    assert "Очередь обращений" in текст
    assert " — 0 · " not in текст, f"осталась строка счётчиков: {текст!r}"
    assert "Все обращения: у вас системные права" not in текст, \
        f"осталась строка про права: {текст!r}"
    for адрес in _адреса(клавиатура):
        assert not адрес.startswith("staffv:"), f"фильтр остался: {адрес}"
    подписи = _подписи(клавиатура)
    for убранное in ("Сбросить фильтр", "Только мои", "Ждут ответа", "В работе"):
        assert убранное not in подписи, f"осталось: {убранное}"
    for нужное in ("Обновить", "Архив", "По отделам"):
        assert any(нужное in п for п in подписи), f"пропало: {нужное}"


async def test_queue_shows_only_not_closed(api):
    """В очереди только незакрытые обращения.

    «Готово к выдаче» тоже незакрытое - документ сделан, но ещё не отдан.
    Закрытые живут в архиве, и в рабочем списке им не место.
    """
    await _системник()
    открытое = await _новое_обращение()
    await db.run("UPDATE tickets SET status='completed' WHERE ticket_id=?",
                 (int(открытое["ticket_id"]),))
    второе = await _новое_обращение()

    await press(OWNER, "staff")

    assert _тикеты(api.last(OWNER)[2] or []) == [str(второе["ticket_id"])], \
        "закрытое обращение попало в очередь"


async def test_empty_queue_says_so(api):
    """Пустая очередь говорит об этом словами, а не пустотой."""
    await _системник()

    await press(OWNER, "staff")

    assert "Пока пусто" in api.last(OWNER)[1]
    assert _адреса(api.last(OWNER)[2] or []), "у пустой очереди должны быть кнопки"


# ── 2. два фильтра, которые не работали ─────────────────────────────────
async def test_waiting_filter_actually_filters(api):
    """«Ждут ответа» показывает только те, где последнее слово за студентом.

    Ошибся я, а не код: обработчик кнопки сам переводит waiting в
    "__waiting__", поэтому условие было правильным, и фильтр работал. Проверка
    остаётся - чтобы ветка принимала оба написания и не сломалась, когда значение
    придёт не из кнопки.
    """
    await _системник()
    ждёт = await _новое_обращение()
    отвечено = await _новое_обращение()
    await db.run("INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) "
                 "VALUES(?,?,?,?)", (int(ждёт["ticket_id"]), "100", "student", "вопрос"))
    await db.run("INSERT INTO ticket_messages(ticket_id, sender_id, sender_role, text) "
                 "VALUES(?,?,?,?)", (int(отвечено["ticket_id"]), OWNER, "staff", "ответ"))

    # Через кнопку: обработчик сам переводит waiting в "__waiting__".
    await press(OWNER, "staffv:waiting")
    assert _тикеты(api.last(OWNER)[2] or []) == [str(ждёт["ticket_id"])], \
        "фильтр ждущих показал не то через кнопку"

    # И напрямую: значение "waiting" тоже должно фильтровать, а не показывать
    # всё под подписью «отфильтровано». Раньше проверка шла только через кнопку
    # и потому проходила даже с испорченным условием.
    await t.send_staff_queue(OWNER, "waiting")
    assert _тикеты(api.last(OWNER)[2] or []) == [str(ждёт["ticket_id"])], \
        "фильтр ждущих показал не то при прямом вызове"


async def test_in_progress_filter_actually_filters(api):
    """«В работе» показывает только обращения в работе.

    in_progress не входил в ключи STAFF_QUEUE_FILTERS, поэтому попадал в ветку
    «показать все» с подписью «Фильтр: В работе» - и рядом стояли числа 1 и 0.
    """
    await _системник()
    в_работе = await _новое_обращение()
    await db.run("UPDATE tickets SET status='in_progress' WHERE ticket_id=?",
                 (int(в_работе["ticket_id"]),))
    await _новое_обращение()   # второе остаётся «новым», его фильтр должен отсечь

    await press(OWNER, "staffv:in_progress")

    assert _тикеты(api.last(OWNER)[2] or []) == [str(в_работе["ticket_id"])], \
        "фильтр «в работе» показал не то"


# ── 3. карточка: главное действие видно сразу ───────────────────────────
async def test_card_first_button_is_answer_and_close(api):
    """Первая кнопка карточки - «Ответ и закрыть», и она одна во всю ширину.

    Владелец: «нет понятного ответить и потом закрыть». Раньше «Ответить» была
    первой, а закрытие спряталось вторым, рядом с четырьмя другими способами
    закончить дело.
    """
    await _системник()
    обращение = await _новое_обращение()

    клавиатура = t.ticket_kb(обращение, staff_side=True, can_delete=True,
                             can_archive=True, can_take=True)
    подписи = _подписи(клавиатура)

    assert подписи[0] == "✅ Ответ и закрыть", f"первая кнопка {подписи[0]!r}"
    assert len(клавиатура[0]) == 1, "«Ответ и закрыть» должна быть одна в ряду"
    assert "💬 Ответить" in подписи
    assert "⚡ Ещё" in подписи
    assert "↩️ К списку" in подписи


async def test_card_has_four_buttons_not_eleven(api):
    """На карточке сис-админа четыре кнопки, а не одиннадцать.

    Владелец: «очень много кнопок». Раньше на карточке сис-админа было до
    одиннадцати кнопок, и «ответить и закрыть» среди них не читалось.
    """
    await _системник()
    обращение = await _новое_обращение()

    клавиатура = t.ticket_kb(обращение, staff_side=True, can_delete=True,
                             can_archive=True, can_take=True)
    подписи = _подписи(клавиатура)

    assert len(подписи) == 4, f"на карточке {len(подписи)} кнопок: {подписи}"
    for спрятанное in ("\U0001f5c4 В архив", "\U0001f5d1 Удалить", "📝 Заметка",
                       "↪️ Переслать", "⚡ Шаблоны", "👌 Принято", "❌ Отклонено"):
        assert спрятанное not in подписи, f"«{спрятанное}» осталась на главном экране"


async def test_more_menu_holds_the_rare_actions(api):
    """В «Ещё» есть всё, что убрано с карточки, и есть выход обратно."""
    await _системник()
    обращение = await _новое_обращение()

    await press(OWNER, f"tmore:{int(обращение['ticket_id'])}")

    подписи = _подписи(api.last(OWNER)[2] or [])
    for ожидаемое in ("📝 Заметка", "↪️ Переслать", "⚡ Шаблоны",
                      "\U0001f5d1 Удалить", "↩️ К обращению"):
        assert ожидаемое in подписи, f"в «Ещё» нет «{ожидаемое}»: {подписи}"


async def test_closed_card_has_no_close_button(api):
    """На закрытом обращении кнопки закрытия нет - закрывать уже нечего."""
    await _системник()
    обращение = await _новое_обращение()
    await db.run("UPDATE tickets SET status='completed' WHERE ticket_id=?",
                 (int(обращение["ticket_id"]),))
    закрытое = await db.one("SELECT * FROM tickets WHERE ticket_id=?",
                            (int(обращение["ticket_id"]),))

    подписи = _подписи(t.ticket_kb(закрытое, staff_side=True, can_delete=True,
                                   can_archive=True, can_take=True))

    assert "✅ Ответ и закрыть" not in подписи, "на закрытом есть кнопка закрытия"


async def test_archived_card_offers_only_restore(api):
    """Архивное дело только возвращается - ответить и закрыть уже нельзя."""
    await _системник()
    обращение = await _новое_обращение()
    await db.run("UPDATE tickets SET deleted_at='2026-10-03 00:00:00' WHERE ticket_id=?",
                 (int(обращение["ticket_id"]),))
    архивное = await db.one("SELECT * FROM tickets WHERE ticket_id=?",
                            (int(обращение["ticket_id"]),))

    подписи = _подписи(t.ticket_kb(архивное, staff_side=True, can_delete=True,
                                   can_archive=True, can_take=True))

    assert "📂 Вернуть из архива" in подписи
    assert "✅ Ответ и закрыть" not in подписи
    assert "⚡ Ещё" not in подписи


# ── 4. шаблоны: видно начало текста ─────────────────────────────────────
async def test_templates_list_shows_text_beginning(api):
    """В списке шаблонов видно начало текста, а не только название.

    Владелец: «в шаблонах текст обрезан». На самом деле текста не было вовсе -
    выводилось только название, и выбирать приходилось вслепую, а шаблон при
    этом применяется к обращению и закрывает его.
    """
    await _системник()
    await repo.add_template("Проверка подписи", "Отличительный текст проверки", "feedback")
    обращение = await _новое_обращение(category="feedback")

    await press(OWNER, f"tpl:{int(обращение['ticket_id'])}")

    текст = api.last(OWNER)[1]
    assert "Проверка подписи" in текст, "названия нет"
    assert "Отличительный текст проверки" in текст, "начала текста нет"


def test_template_preview_strips_the_greeting():
    """Приветствие не съедает предпросмотр.

    Все шаблоны начинаются с «{ФИО}, добрый день!», и без этого девяноста
    символов уходили на приветствие, а различающая часть оставалась за краем.
    """
    результат = t.template_preview("{ФИО}, добрый день! Справка готова, заберите в кабинете 115")

    assert "добрый день" not in результат.lower()
    assert "Справка готова" in результат


def test_template_preview_keeps_text_without_greeting():
    """Текст без приветствия не трогаем."""
    текст = "Документ выдаётся в кабинете 115 с 9:00 до 17:00"
    assert t.template_preview(текст) == текст


def test_template_preview_is_short_and_without_ellipsis():
    """Предпросмотр короче предела и без многоточия."""
    длинный = " ".join(["слово%d" % i for i in range(40)])

    got = t.template_preview(длинный)

    assert len(got) <= 90
    assert not got.endswith(("...", "…")), "многоточие путает: обрезанное слово"


def test_template_preview_survives_empty():
    """Пустой шаблон не ломает предпросмотр."""
    assert t.template_preview("") == ""
    assert t.template_preview("   ") == ""


def test_ready_status_button_not_confused_with_pickup():
    """«К выдаче» и «Готово · 115» больше не называются одинаково.

    На карточке стояли три подписи со словом «готово» - статус, выдача и
    «Ответ и закрыть». Статус переименован в «📋 К выдаче».
    """
    assert t.STAGE_SHORT["ready"] == "\U0001f4cb К выдаче"
    assert t.STAGE_SHORT["ready"] != "\U0001f4c4 Готово"
