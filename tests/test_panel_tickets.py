"""Рабочее место: архив открывает обращение, быстрый ответ работает.

Две поломки, которые видны сотруднику сразу, и обе проверяются через настоящий
HTTP: не по строкам кода, а по странице, которую он получает.

Архив. В архиве 5 обращений, ссылки на них в очереди есть, а карточка справа
всегда остаётся заглушкой «Выберите обращение в очереди слева». Причина в ссылке
пункта очереди: она строилась без ``view=archive`` и вела в рабочее место, где
архивного обращения нет, поэтому ``selected`` оставался пустым. Проверка идёт по
самой ссылке со страницы архива - так же, как кликает человек.

Быстрый ответ. В карточке есть список шаблонов, и выбор в нём не делал ничего:
обработчик читал только поле ответа, а подстановки {ФИО} и остальные в панели не
подставлялись вовсе. Теперь ответ уходит по шаблону с подставленными данными
обращения, свой текст важнее шаблона, удалённый шаблон не отправляет пустоту, а
неизвестная подстановка не выбрасывается молча.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
from conftest import add_staff, login_panel, post_form, register

STUDENT, STAFF = "300", "200"
FIO = "Иванов Иван Иванович"
GROUP = "24-23"
TPL_TITLE = "Справка готова"
TPL_TEXT = "Здравствуйте, {ФИО}! Справка по обращению №{номер} готова, группа {группа}."
CARD_STUB = "Выберите обращение в очереди слева"


# ── фикстуры ──────────────────────────────────────────────────────────────────
@pytest.fixture
async def env(env):
    """Студент с ФИО и группой и сотрудник-исполнитель: как живое обращение."""
    await register(STUDENT, FIO, GROUP)
    await add_staff(STAFF, "Петрова Мария Сергеевна", position="Секретарь")
    return env


async def ticket(text: str = "Нужна справка", status: str = "new") -> int:
    """Живое обращение студента на сотрудника."""
    ticket_id = await repo.create_ticket(STUDENT, STAFF, "feedback", text, "Справка")
    if status != "new":
        await repo.set_ticket_status(ticket_id, status)
    return ticket_id


async def archived(text: str = "Нужна справка", status: str = "new",
                   asked: bool = False) -> int:
    """Обращение, убранное в архив, - как после кнопки «В архив».

    ``asked`` - последнее слово в обращении за сотрудником: так обращение
    попадает в фильтр «только ждут ответа».
    """
    ticket_id = await ticket(text, status)
    if asked:
        await repo.add_ticket_message(ticket_id, STUDENT, "student",
                                      "Когда ответите?")
    await repo.archive_ticket(ticket_id, "1")
    return ticket_id


async def template(title: str = TPL_TITLE, text: str = TPL_TEXT,
                   category: str = "all") -> int:
    return await repo.add_template(title, text, category, "1")


async def staff_messages(ticket_id: int) -> list:
    """Ответы сотрудника в переписке: их нет, если ответ не ушёл."""
    return [row for row in await repo.ticket_thread(ticket_id, 20)
            if row["sender_role"] == "staff"]


# ── помощники разбора страницы ───────────────────────────────────────────────
def queue_item(body: str, ticket_id: int) -> str:
    """Разметка пункта очереди по номеру обращения - как его видит человек."""
    items = re.findall(r'<label class="wb-item.*?</label>', body, re.S)
    wanted = [item for item in items if f'value="{ticket_id}"' in item]
    assert wanted, f"в очереди нет обращения {ticket_id}"
    return wanted[0]


def queue_link(body: str, ticket_id: int) -> str:
    """Ссылка пункта очереди на карточку обращения."""
    match = re.search(r'<a href="([^"]+)"', queue_item(body, ticket_id))
    assert match, "в пункте очереди нет ссылки на обращение"
    return match.group(1)


def copied_link(body: str) -> str:
    """Ссылка на обращение из кнопки «скопировать» в карточке."""
    found = re.findall(r'data-copy="([^"]*/panel/tickets\?t=\d+[^"]*)"', body)
    assert found, "в карточке нет копируемой ссылки на обращение"
    return found[0].replace("&amp;", "&")


def flash_of(body: str) -> str:
    """Сообщение сис-админу над страницей: так выглядит flash после кнопки."""
    match = re.search(r'<div class="msg msg-\w+">(.*?)</div>', body, re.S)
    assert match, "на странице нет сообщения"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


def reply_block(body: str) -> str:
    """Блок «Быстрый ответ» целиком: форма и подсказка под ней."""
    match = re.search(r'<h2>Быстрый ответ</h2>(.*?)(?=<div class="card">|$)', body, re.S)
    assert match, "в карточке нет формы быстрого ответа"
    return match.group(0)


def reply_hint(body: str) -> str:
    """Только абзац-подсказка под формой ответа, без самой формы.

    Отдельный кусок обязателен: в блоке выше лежат подстановки шаблона из
    data-text пункта списка, и проверка подсказки проходит, даже если её нет.
    """
    block = reply_block(body)
    after_form = block.split("</form>", 1)
    assert len(after_form) == 2, "в блоке ответа нет закрывающего </form>"
    match = re.search(r'<p class="small mut">(.*?)</p>', after_form[1], re.S)
    assert match, "под формой ответа нет подсказки"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


# ── архив: обращение открывается ─────────────────────────────────────────────
async def test_archive_queue_link_keeps_the_archive(panel_client, env):
    """Ссылка пункта очереди в архиве ведёт в архив, а не в рабочее место."""
    ticket_id = await archived()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets?view=archive").text
    link = queue_link(body, ticket_id)
    assert "view=archive" in link, f"в ссылке архива нет вида: {link}"


async def test_archive_link_really_opens_the_card(panel_client, env):
    """Главное: клик по обращению в архиве открывает карточку, а не заглушку.

    Страница запрашивается по самой ссылке из очереди - ровно так, как кликает
    сотрудник, - и карточка обязана быть на месте.
    """
    ticket_id = await archived("Нужна справка для военной части")
    assert login_panel(panel_client)
    link = queue_link(panel_client.get("/panel/tickets?view=archive").text, ticket_id)
    card = panel_client.get(link).text
    assert f"Обращение №{ticket_id}" in card
    assert CARD_STUB not in card, "карточка не открылась: справа заглушка"
    assert "Быстрый ответ" in card


async def test_archive_link_keeps_filters(panel_client, env):
    """Фильтры в ссылке остаются: сузили очередь - вернулись в ту же очередь."""
    ticket_id = await archived("Нужна справка для военной части",
                              status="completed", asked=True)
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets?view=archive&status=completed"
                            "&category=feedback&q=военной&scope=").text
    link = queue_link(body, ticket_id)
    for part in ("view=archive", "status=completed", "category=feedback",
                 "q=военной", "scope="):
        assert part in link, f"в ссылке потерялось «{part}»: {link}"
    assert f"Обращение №{ticket_id}" in panel_client.get(link).text


async def test_card_link_of_archived_ticket_keeps_the_archive(panel_client, env):
    """Скопированная ссылка на архивное обращение тоже ведёт в архив."""
    ticket_id = await archived()
    assert login_panel(panel_client)
    body = panel_client.get(f"/panel/tickets?view=archive&t={ticket_id}").text
    assert copied_link(body).endswith(f"/tickets?t={ticket_id}&view=archive")


async def test_live_queue_link_has_no_archive(panel_client, env):
    """В рабочем месте вида архива в ссылке нет: правка не должна ломать живое."""
    ticket_id = await ticket()
    assert login_panel(panel_client)
    body = panel_client.get("/panel/tickets").text
    link = queue_link(body, ticket_id)
    assert "view=archive" not in link
    assert f"Обращение №{ticket_id}" in panel_client.get(link).text


# ── быстрый ответ: шаблон с подстановками ────────────────────────────────────
async def test_reply_form_carries_the_template_text(panel_client, env, clear_templates):
    """В пункте списка лежит текст шаблона: иначе форме нечего подставить."""
    template_id = await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    block = reply_block(panel_client.get(f"/panel/tickets?t={ticket_id}").text)
    choice = re.search(r'<select name="template">.*?</select>', block, re.S)
    assert choice, "в форме ответа нет списка шаблонов"
    option = re.search(r'<option value="%d"[^>]*>' % template_id, choice.group(0))
    assert option, "в списке нет добавленного шаблона"
    assert f'data-text="{TPL_TEXT}"' in option.group(0)
    field = re.search(r'<textarea name="text"[^>]*>', block)
    assert field, "в форме ответа нет поля для текста"
    assert "required" not in field.group(0), (
        "поле ответа не должно быть required: выбрал шаблон, оставил текст пустым - "
        "и браузер не отправит форму вовсе")


async def test_template_can_be_chosen_without_typing(panel_client, env,
                                                    clear_templates, api):
    """Шаблон уходит и без единой буквы в поле ответа - как это сделает браузер.

    Форма отправляется ровно так, как её отправит человек, выбравший шаблон и
    не трогая textarea: пустой text, в форме только выбор шаблона.
    """
    template_id = await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    block = reply_block(panel_client.get(f"/panel/tickets?t={ticket_id}").text)
    assert not re.search(r'<textarea name="text"[^>]*required', block), \
        "браузер не отправит пустой ответ, а шаблон без текста - это половина работы"
    post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
              {"text": "", "template": str(template_id)})
    assert f"Здравствуйте, {FIO}!" in api.to(STUDENT)[-1][1]


async def test_template_answer_reaches_the_student_filled(panel_client, env,
                                                          clear_templates, api):
    """Выбранный шаблон уходит студенту с данными обращения, а не с {ФИО}."""
    template_id = await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    assert post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
                      {"text": "", "template": str(template_id)}).status_code == 303
    sent = api.to(STUDENT)[-1][1]
    assert f"Здравствуйте, {FIO}!" in sent, "ФИО не подставилось"
    assert f"обращению №{ticket_id}" in sent, "номер не подставился"
    assert f"группа {GROUP}" in sent, "группа не подставилась"
    assert "{ФИО}" not in sent and "{номер}" not in sent
    thread = await staff_messages(ticket_id)
    # Уведомление теперь: номер, тема обращения, кто отвечает, затем текст.
    header, body = sent.rsplit("\n\n", 1)
    assert f"обращению №{ticket_id}" in header, header
    assert "Отвечает:" in header, header
    # без явного выбора отвечает вошедший в панель - в тесте это «Сис-админ».
    # Студент по уведомлению должен понять, ОТ КОГО ответ.
    assert "Сис-админ" in header, f"в уведомлении нет имени ответившего: {header}"
    assert [row["text"] for row in thread] == [body]


async def test_own_text_is_stronger_than_template(panel_client, env, clear_templates, api):
    """Сотрудник написал своё - шаблон не навязывается."""
    template_id = await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    assert post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
                      {"text": "Отвечаю своим текстом", "template": str(template_id)}
                      ).status_code == 303
    sent = api.to(STUDENT)[-1][1]
    assert "Отвечаю своим текстом" in sent
    assert "Справка по обращению" not in sent, "текст шаблона всё равно ушёл"
    assert [row["text"] for row in await staff_messages(ticket_id)] == ["Отвечаю своим текстом"]


async def test_substitutions_work_in_own_text(panel_client, env, clear_templates, api):
    """Подстановки работают и в собственном тексте сотрудника."""
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
              {"text": f"{FIO}, ваша группа {GROUP}, обращение {{номер}}"})
    sent = api.to(STUDENT)[-1][1]
    assert f"{FIO}, ваша группа {GROUP}, обращение {ticket_id}" in sent
    assert "{номер}" not in sent


async def test_usage_counter_grows_only_for_a_template_answer(panel_client, env,
                                                              clear_templates, api):
    """Счётчик применений - только за ответ шаблоном, а не за любой ответ."""
    template_id = await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
              {"text": "Свой текст", "template": str(template_id)})
    assert (await repo.get_template(template_id))["used_count"] == 0, \
        "переписанный шаблон не должен считаться применением"
    post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
              {"text": "", "template": str(template_id)})
    assert (await repo.get_template(template_id))["used_count"] == 1


async def test_deleted_template_sends_nothing(panel_client, env, clear_templates, api):
    """Шаблон удалили, пока сотрудник писал ответ, - пустого ответа не будет."""
    template_id = await template()
    await repo.delete_template(template_id)
    ticket_id = await ticket()
    assert login_panel(panel_client)
    assert post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
                     {"text": "", "template": str(template_id)}).status_code == 303
    assert await staff_messages(ticket_id) == [], "ответ ушёл, а отправлять было нечего"
    assert "удалён" in flash_of(panel_client.get(f"/panel/tickets?t={ticket_id}").text)


async def test_empty_answer_is_not_sent(panel_client, env, clear_templates, api):
    """Ни текста, ни шаблона - ответа нет и понятное сообщение."""
    ticket_id = await ticket()
    assert login_panel(panel_client)
    assert post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
                     {"text": "   "}).status_code == 303
    assert await staff_messages(ticket_id) == []
    assert "Пустой ответ" in flash_of(panel_client.get(f"/panel/tickets?t={ticket_id}").text)


async def test_unknown_placeholder_is_not_dropped_silently(panel_client, env,
                                                           clear_templates, api):
    """Сотрудник написал {причина}: слово уходит студенту как есть, но сотрудник
    узнаёт об этом - молча выбросить подстановку нельзя."""
    ticket_id = await ticket()
    assert login_panel(panel_client)
    post_form(panel_client, f"/panel/tickets/{ticket_id}/reply",
              {"text": "Причина: {причина}"})
    assert "{причина}" in api.to(STUDENT)[-1][1]
    body = panel_client.get(f"/panel/tickets?t={ticket_id}").text
    notice = flash_of(body)
    assert "Не подставилось" in notice and "{причина}" in notice
    assert 'class="msg msg-bad"' in body, "о неизвестной подстановке должно быть видно"


async def test_hint_tells_what_substitutes_itself(panel_client, env, clear_templates):
    """Под формой написано, что подстановки случатся сами, и перечислены они.

    Смотрят именно на абзац под формой: в data-text пункта списка подстановки
    шаблона есть и без всякой подсказки, и такая проверка проходит вхолостую.
    """
    await template()
    ticket_id = await ticket()
    assert login_panel(panel_client)
    hint = reply_hint(panel_client.get(f"/panel/tickets?t={ticket_id}").text)
    assert "подставятся сами" in hint, f"подсказка молчит про подстановки: {hint}"
    # имена берём у бота: панель и бот должны знать один и тот же набор
    for name in ("ФИО", "группа", "кабинет", "дата"):
        assert "{%s}" % name in hint, f"в подсказке нет подстановки {{{name}}}: {hint}"


async def test_hint_lists_unknown_placeholders(panel_client, env, clear_templates):
    """Оставшиеся неизвестные подстановки перечислены под формой.

    Иначе сотрудник увидит {причина} в тексте шаблона, отправит его студенту
    и не узнает, что слово ушло как есть.
    """
    await template(text="Причина: {причина}")
    ticket_id = await ticket()
    assert login_panel(panel_client)
    hint = reply_hint(panel_client.get(f"/panel/tickets?t={ticket_id}").text)
    assert "Непонятно" in hint and "{причина}" in hint, f"неизвестные не перечислены: {hint}"


async def test_reply_requires_csrf(panel_client, env, clear_templates):
    """Без токена формы ответ не уходит."""
    ticket_id = await ticket()
    assert login_panel(panel_client)
    assert panel_client.post(f"/panel/tickets/{ticket_id}/reply",
                             data={"text": "взлом"}).status_code == 403
    assert await staff_messages(ticket_id) == []


async def test_reply_to_missing_ticket_is_404(panel_client, env, clear_templates):
    """Нет обращения - 404, а не пустой ответ в никуда."""
    assert login_panel(panel_client)
    assert post_form(panel_client, "/panel/tickets/999/reply",
                     {"text": "Привет"}).status_code == 404
