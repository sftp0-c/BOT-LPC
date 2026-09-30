"""tests/test_templates_edit.py - правка шаблона ответа в панели.

Раздел «Шаблоны ответов» умел только добавить и удалить: битый шаблон выправить
было нечем, а в боевой базе он есть. Проверяем полноценную правку поведением:

* в таблице рядом с «Удалить» стоит «Правка», и она открывает форму;
* форма показывает текущие название, раздел и текст, всё экранировано;
* сохранение меняет название, раздел и текст, но не трогает счётчик применений
  и дату добавления - правка не «применение»;
* личная пометка сотрудника («мои») переживает правку;
* пустое название не сохраняется, отсутствующий шаблон - 404, CSRF обязателен;
* в журнал действий попадают только название и id: ни текста шаблона, ни
  переписки там быть не должно.
"""
import re

import pytest

# Веб-панель: поднимает TestClient, поэтому медленнее обычного экрана.
pytestmark = pytest.mark.panel


import repository as repo
from conftest import login_panel, post_form
from handlers.common import is_personal_template, set_personal_template

LIST = "/panel/templates"
SYS = "1"
TPL_TITLE = "Справка готова"
TPL_TEXT = "Заберите справку в кабинете 214."
SECRET = "тихое дело без свидетелей"


# ── фикстуры ──────────────────────────────────────────────────────────────────
@pytest.fixture
async def env(env):
    """Один шаблон на тест: список ответов в боте и журнал должны быть предсказуемы."""
    return env


async def template(title: str = TPL_TITLE, text: str = TPL_TEXT,
                   category: str = "certificates") -> int:
    return await repo.add_template(title, text, category, SYS)


def edit_page(client, template_id: int) -> str:
    return client.get(f"{LIST}/{template_id}/edit").text


def field_value(body: str, name: str) -> str:
    """Значение поля формы: читаем из HTML то, что увидит браузер."""
    match = re.search(r'<input name="%s" value="([^"]*)"' % name, body)
    assert match, f"в форме правки нет поля {name}"
    return match.group(1)


def textarea_of(body: str, name: str) -> str:
    match = re.search(r'<textarea name="%s"[^>]*>(.*?)</textarea>' % name, body, re.S)
    assert match, f"в форме правки нет поля {name}"
    return match.group(1)


def flash_of(body: str) -> str:
    match = re.search(r'<div class="msg msg-\w+">(.*?)</div>', body, re.S)
    assert match, "на странице нет сообщения"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


async def admin_log_text() -> str:
    return " ".join(f"{row['action']} {row['details']}" for row in await repo.admin_log(50))


# ── ссылка и форма правки ─────────────────────────────────────────────────────
async def test_edit_link_stands_next_to_delete(panel_client, env, clear_templates):
    """В строке таблицы есть «Правка» рядом с «Удалить»."""
    template_id = await template()
    assert login_panel(panel_client)
    body = panel_client.get(LIST).text
    rows = [chunk for chunk in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S)
            if f"{LIST}/{template_id}/delete" in chunk]
    assert rows, "в таблице нет строки этого шаблона"
    assert f'href="{LIST}/{template_id}/edit"' in rows[0]
    assert "Правка" in rows[0] and "Удалить" in rows[0]


async def test_edit_form_shows_current_values(panel_client, env, clear_templates):
    """Форма правки открыта на текущих значениях шаблона."""
    template_id = await template()
    assert login_panel(panel_client)
    body = edit_page(panel_client, template_id)
    assert field_value(body, "title") == TPL_TITLE
    assert textarea_of(body, "text") == TPL_TEXT
    assert re.search(r'<option value="certificates" selected>', body), \
        "текущий раздел не отмечен в списке"


async def test_edit_saves_title_category_and_text(panel_client, env, clear_templates):
    """Сохранение меняет название, раздел и текст."""
    template_id = await template()
    assert login_panel(panel_client)
    response = post_form(panel_client, f"{LIST}/{template_id}/edit", {
        "title": "Справка готова (новый кабинет)",
        "category": "feedback",
        "text": "Заберите справку в кабинете 203.",
    })
    assert response.status_code == 303
    assert response.headers["location"] == LIST
    row = await repo.get_template(template_id)
    assert row["title"] == "Справка готова (новый кабинет)"
    assert row["category"] == "feedback"
    assert row["text"] == "Заберите справку в кабинете 203."
    assert "сохранён" in flash_of(panel_client.get(LIST).text)
    # и правка видна в списке
    assert "Заберите справку в кабинете 203." in panel_client.get(LIST).text


async def test_edit_does_not_count_as_usage(panel_client, env, clear_templates):
    """Счётчик применений и дата добавления - не поля правки."""
    template_id = await template()
    await repo.count_template_use(template_id)
    before = await repo.get_template(template_id)
    assert login_panel(panel_client)
    post_form(panel_client, f"{LIST}/{template_id}/edit",
              {"title": TPL_TITLE, "category": "all", "text": "Другой текст"})
    row = await repo.get_template(template_id)
    assert row["used_count"] == before["used_count"] == 1
    assert row["created_at"] == before["created_at"]


async def test_personal_mark_survives_the_edit(panel_client, env, clear_templates):
    """Личная пометка сотрудника при правке не теряется."""
    template_id = await template()
    await set_personal_template(SYS, template_id)
    assert login_panel(panel_client)
    assert "мои" in edit_page(panel_client, template_id), "пометка не показана"
    post_form(panel_client, f"{LIST}/{template_id}/edit",
              {"title": "Переименован", "category": "all", "text": "Новый текст"})
    assert await is_personal_template(SYS, template_id), "шаблон перестал быть личным"


async def test_edit_warns_about_unknown_placeholders(panel_client, env, clear_templates):
    """Неизвестная подстановка в тексте видна тому, кто правит."""
    template_id = await template(text="Причина: {причина}")
    assert login_panel(panel_client)
    body = edit_page(panel_client, template_id)
    assert "неизвестные подстановки" in body and "{причина}" in body


async def test_edit_escapes_what_it_shows(panel_client, env, clear_templates):
    """Название и текст из базы экранируются: разметка не должна просачиваться."""
    template_id = await template(title="Правка <b>названия</b> & прочее",
                                 text="Привет <script>alert(1)</script>")
    assert login_panel(panel_client)
    body = edit_page(panel_client, template_id)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;b&gt;названия&lt;/b&gt; &amp; прочее" in field_value(body, "title")


# ── отказы: пустое название, нет шаблона, нет токена ─────────────────────────
async def test_empty_title_is_not_saved(panel_client, env, clear_templates):
    """Без названия шаблон бесполезен: правка не проходит, старый текст цел."""
    template_id = await template()
    assert login_panel(panel_client)
    response = post_form(panel_client, f"{LIST}/{template_id}/edit",
                         {"title": "   ", "category": "all", "text": "Новый текст"})
    assert response.status_code == 303
    assert f"{LIST}/{template_id}/edit" in response.headers["location"]
    row = await repo.get_template(template_id)
    assert row["title"] == TPL_TITLE and row["text"] == TPL_TEXT
    assert "Нужны и название" in flash_of(edit_page(panel_client, template_id))


async def test_empty_text_is_not_saved(panel_client, env, clear_templates):
    """Пустой ответ студенту бесполезен: текст обязателен, как при добавлении."""
    template_id = await template()
    assert login_panel(panel_client)
    post_form(panel_client, f"{LIST}/{template_id}/edit",
              {"title": TPL_TITLE, "category": "all", "text": "  "})
    assert (await repo.get_template(template_id))["text"] == TPL_TEXT


async def test_missing_template_is_404(panel_client, env, clear_templates):
    """Нет такого шаблона - 404 и на странице правки, и на сохранении."""
    assert login_panel(panel_client)
    assert panel_client.get(f"{LIST}/999/edit").status_code == 404
    assert post_form(panel_client, f"{LIST}/999/edit",
                     {"title": "X", "category": "all", "text": "Y"}).status_code == 404


async def test_edit_requires_csrf(panel_client, env, clear_templates):
    """Без токена формы правка не проходит, шаблон не тронут."""
    template_id = await template()
    assert login_panel(panel_client)
    assert panel_client.post(f"{LIST}/{template_id}/edit",
                             data={"title": "Взлом", "category": "all",
                                   "text": SECRET}).status_code == 403
    row = await repo.get_template(template_id)
    assert row["title"] == TPL_TITLE and row["text"] == TPL_TEXT


# ── журнал действий ──────────────────────────────────────────────────────────
async def test_edit_is_logged_with_title_and_id_only(panel_client, env, clear_templates):
    """Кто, что и какой шаблон - без текста шаблона."""
    template_id = await template()
    assert login_panel(panel_client)
    post_form(panel_client, f"{LIST}/{template_id}/edit",
              {"title": "Справка выдана", "category": "all", "text": SECRET})
    entry = next(row for row in await repo.admin_log(50)
                 if row["action"] == "шаблон ответа изменён")
    assert entry["actor_id"] == SYS
    assert "Справка выдана" in entry["details"]
    assert f"ID {template_id}" in entry["details"]
    assert SECRET not in await admin_log_text(), "текст шаблона попал в журнал"
