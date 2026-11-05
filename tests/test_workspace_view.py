"""Чистые проверки SSR и progressive JS: синтетические данные, без БД и браузера."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

import clock
from panel_theme import css_names


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web" / "workspace_view.py"
CSS = ROOT / "web" / "static" / "workspace.css"
JS = ROOT / "web" / "static" / "workspace.js"
SPEC = importlib.util.spec_from_file_location("workspace_renderer_tested", SOURCE)
view = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(view)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Перекрываем общую env: этому модулю не нужна даже временная SQLite."""
    monkeypatch.setenv("TZ", "Asia/Yekaterinburg")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 5, 12, 0, 0))


class Element:
    def __init__(self, tag="root", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def nodes(self, tag=None, **attrs):
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if (tag is None or child.tag == tag) and all(child.attrs.get(key) == value for key, value in attrs.items()):
                yield child
            yield from child.nodes(tag, **attrs)

    def text(self):
        return "".join(child.text() if isinstance(child, Element) else child for child in self.children)

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class Parsed(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Element()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"input", "img", "br", "hr", "meta", "link", "source", "wbr", "area", "base", "embed", "param"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Element(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def dom(text):
    return Parsed(text).root


def cls(root, name):
    return next(node for node in root.nodes() if node.has_class(name))


def posts(root):
    return list(root.nodes("form", method="post"))


def actions(root):
    return {button.attrs["value"] for button in root.nodes("button", name="action")}


@pytest.fixture
def payload():
    ticket = {
        "ticket_id": 71, "student_name": "Егорова Анна Сергеевна", "student_group": "26-11",
        "staff_name": "Иванова Мария Петровна", "staff_position": "Секретарь учебной части",
        "target_admin_id": "42", "status": "in_progress", "category": "certificates",
        "text_content": "Нужна справка об обучении.\nСпасибо!", "created_at": "2026-11-04 09:00:00",
        "in_progress_at": "2026-11-05 10:00:00", "latest_message_at": "2026-11-05 11:00:00",
        "pickup_place": "115", "ready_until": "6 ноября", "is_waiting": True,
    }
    return {
        "base_path": "/workspace/tickets",
        "actor": {"user_id": "42", "full_name": "Иванова Мария Петровна", "position": "Секретарь",
                  "is_admin": False, "permissions": []},
        "queue": {"rows": [deepcopy(ticket)], "total": 63, "page": 2, "page_size": 25,
                  "counts": {"open": 39, "waiting": 8, "closed": 24, "archive": 3}},
        "detail": {"ticket": ticket, "revision": "rev-7", "allowed_actions": sorted(view.ACTIONS),
                   "thread": [
                       {"message_id": 5, "sender_role": "staff", "sender_name": "Иванова Мария Петровна",
                        "text": "Справку подготовим завтра.", "created_at": "2026-11-05 11:00:00", "delivery_status": "sent"},
                       {"message_id": 4, "sender_role": "student", "sender_name": "Егорова Анна Сергеевна",
                        "text": "Можно забрать после занятий?", "created_at": "2026-11-05 10:30:00"},
                   ],
                   "events": [
                       {"id": 9, "event": "note", "actor_name": "Иванова Мария Петровна",
                        "detail": "Проверить сведения в учебной части.", "created_at": "2026-11-05 10:15:00"},
                       {"id": 8, "event": "status", "actor_name": "Иванова Мария Петровна",
                        "detail": "В работе", "created_at": "2026-11-05 10:00:00"},
                   ],
                   "templates": [{"id": 3, "title": "Справка к выдаче", "text": "Не показывать сырой {ФИО}"}],
                   "recipients": [{"user_id": "88", "full_name": "Смирнова Ольга Ивановна", "position": "Бухгалтер"}],
                   "files": [{"filename": "справка.pdf", "href": "/workspace/tickets/71/files/2", "size_label": "24 КБ"}],
                   "allowed_statuses": ["in_progress", "ready", "completed", "rejected"]},
        "filters": {"tab": "open", "q": "Анна & справка", "category": "certificates", "date_from": "2026-11-01"},
        "csrf_token": "csrf-42-71", "operation_key": "operation-42-71", "standalone": False,
    }


def test_pure_fragment_and_input_unchanged(payload):
    before = deepcopy(payload)
    html = view.render_workbench(**payload)
    assert payload == before
    tree = dom(html)
    assert not list(tree.nodes("html"))
    assert not list(tree.nodes("body"))
    assert not list(tree.nodes("script"))
    assert not list(tree.nodes("style"))
    assert "landing" not in html and "wb-" not in html
    assert cls(tree, "ws-workbench").attrs["data-user-id"] == "42"


def test_no_backend_or_session_imports():
    source = SOURCE.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    imports = {item.module for item in ast.walk(tree) if isinstance(item, ast.ImportFrom)}
    imports |= {alias.name for item in ast.walk(tree) if isinstance(item, ast.Import) for alias in item.names}
    assert imports <= {"__future__", "collections.abc", "datetime", "html", "re", "urllib.parse", "clock", "panel_theme", "utils"}
    assert not any(isinstance(item, ast.AsyncFunctionDef) for item in ast.walk(tree))


def test_assets_are_optional_and_only_workspace():
    head, script = view.workspace_assets()
    assert dom(head).nodes("link")
    assert 'href="/workspace/assets/workspace.css"' in head
    assert 'src="/workspace/assets/workspace.js"' in script and "defer" in script
    assert "landing" not in head + script


@pytest.mark.parametrize("base", ["https://foreign.invalid/tickets", "//foreign.invalid", "/feedback", "/workspace/tickets/1"])
def test_base_path_rejects_other_routes(payload, base):
    payload["base_path"] = base
    with pytest.raises(ValueError, match="путь"):
        view.render_workbench(**payload)


def _assert_escaped(html):
    tree = dom(html)
    assert not list(tree.nodes("script"))
    assert not list(tree.nodes("img"))
    assert not any(key.lower().startswith("on") for node in tree.nodes() for key in node.attrs)


def test_escapes_every_untrusted_surface(payload):
    unsafe = '\"><img src=x onerror=alert(1)><script>секрет</script>&'
    payload["actor"]["full_name"] = unsafe
    payload["actor"]["user_id"] = unsafe
    payload["csrf_token"] = unsafe
    payload["operation_key"] = unsafe
    payload["detail"]["revision"] = unsafe
    for key in ("student_name", "staff_name", "topic", "text_content", "pickup_place", "ready_until"):
        payload["detail"]["ticket"][key] = unsafe
        payload["queue"]["rows"][0][key] = unsafe
    for item in payload["detail"]["thread"]:
        item["sender_name"], item["text"] = unsafe, unsafe
    payload["detail"]["events"][0]["detail"] = unsafe
    payload["detail"]["templates"][0]["title"] = unsafe
    payload["detail"]["recipients"][0]["full_name"] = unsafe
    payload["detail"]["files"][0]["filename"] = unsafe
    payload["detail"]["draft_text"] = unsafe
    payload["detail"]["notice"] = {"kind": "error", "text": unsafe}
    payload["filters"]["q"] = unsafe
    html = view.render_workbench(**payload)
    _assert_escaped(html)
    assert unsafe in cls(dom(html), "ws-message-incoming").text()
    assert next(dom(html).nodes("textarea", name="text")).text() == unsafe
    assert next(dom(html).nodes("input", name="csrf")).attrs["value"] == unsafe


def _assert_csrf(html):
    forms = posts(dom(html))
    assert forms
    for form in forms:
        names = {node.attrs.get("name"): node.attrs.get("value") for node in form.nodes("input")}
        assert names["csrf"] == "csrf-42-71"
        assert names["operation_key"] == "operation-42-71"
        assert names["revision"] == "rev-7"


@pytest.mark.parametrize("base", ["/workspace/tickets", "/panel/tickets"])
def test_action_forms_same_base_csrf_revision_actor_identity(payload, base):
    payload["base_path"] = base
    tree = dom(view.render_workbench(**payload))
    _assert_csrf(view.render_workbench(**payload))
    for form in posts(tree):
        assert form.attrs["action"] == base + "/71/action"
        assert not list(form.nodes("form"))
        assert not list(form.nodes("input", name="as_staff"))
        assert not list(form.nodes("select", name="as_staff"))
        params = parse_qs(urlsplit(next(form.nodes("input", name="return")).attrs["value"]).query)
        assert params["q"] == ["Анна & справка"]
        assert params["page"] == ["2"]
        assert params["t"] == ["71"]
    assert actions(tree) == view.ACTIONS


@pytest.mark.parametrize("allowed", [[], ["reply"], ["note"], ["forward"], ["status"], ["template_preview"], ["drop_database", "attachments", "templates"]])
def test_buttons_are_exact_parent_whitelist(payload, allowed):
    payload["detail"]["allowed_actions"] = allowed
    tree = dom(view.render_workbench(**payload))
    assert actions(tree) == set(allowed) & view.ACTIONS
    if "forward" not in allowed:
        assert "Смирнова Ольга Ивановна" not in tree.text()
    if "template_preview" not in allowed:
        assert "Справка к выдаче" not in tree.text()


def test_staff_has_no_global_navigation_or_extra_admin_html(payload):
    payload["detail"]["extra_admin_html"] = '<a href="/panel/people">Глобальный список</a>'
    payload["detail"]["admin_links"] = [{"href": "/panel/tickets/71/edit", "label": "Правка карточки"}]
    payload["queue"]["extra_admin_html"] = '<p>Секретные настройки</p>'
    payload["actor"]["people"] = [{"full_name": "Скрытый сотрудник"}]
    html = view.render_workbench(**payload)
    assert "/panel/" not in html
    assert "Глобальный список" not in html and "Скрытый сотрудник" not in html
    assert "Секретные настройки" not in html
    assert not list(dom(html).nodes("nav"))


def test_admin_keeps_existing_entrypoints_and_explicit_extra(payload):
    payload["actor"]["is_admin"] = True
    payload["base_path"] = "/panel/tickets"
    payload["detail"]["extra_admin_html"] = '<div id="original-admin-controls">Оригинальные действия</div>'
    payload["detail"]["admin_links"] = [{"href": "/panel/tickets/71/edit", "label": "Править"}]
    tree = dom(view.render_workbench(**payload))
    assert list(tree.nodes("div", id="original-admin-controls"))
    assert list(tree.nodes("a", href="/panel/tickets/71/edit"))
    assert any(node.attrs.get("href", "").startswith("/panel/tickets/new?") for node in tree.nodes("a"))


@pytest.mark.parametrize("cap", [{"can_export": True}, {"permissions": ["export"]}, {"is_admin": True}])
def test_csv_explicit_capability_preserves_filters(payload, cap):
    payload["actor"].update(cap)
    tree = dom(view.render_workbench(**payload))
    link = next(node for node in tree.nodes("a") if node.attrs.get("href", "").startswith("/workspace/tickets.csv?"))
    params = parse_qs(urlsplit(link.attrs["href"]).query)
    assert params["tab"] == ["open"] and params["q"] == ["Анна & справка"]
    assert params["date_from"] == ["2026-11-01"]


def test_csv_hidden_without_capability(payload):
    assert "/workspace/tickets.csv" not in view.render_workbench(**payload)


@pytest.mark.parametrize("cap", [{"can_view_archive": True}, {"permissions": ["archive"]}, {"is_admin": True}])
def test_archive_only_explicitly_allowed_and_tabs_exit_archive(payload, cap):
    payload["actor"].update(cap)
    payload["filters"]["view"] = "archive"
    tree = dom(view.render_workbench(**payload))
    selector = next(tree.nodes("select", name="view"))
    assert list(selector.nodes("option", value="archive", selected=None))
    tabs = [node for node in tree.nodes("a") if "data-ws-tab" in node.attrs]
    assert len(tabs) == 3
    for link in tabs:
        params = parse_qs(urlsplit(link.attrs["href"]).query)
        assert params.get("view") != ["archive"]
        assert params.get("status") is None and params.get("scope") is None
        assert params["page"] == ["1"]


def test_archive_not_in_staff_filter_without_permission(payload):
    payload["filters"]["view"] = "archive"
    tree = dom(view.render_workbench(**payload))
    assert not list(tree.nodes("select", name="view"))
    assert not any(parse_qs(urlsplit(node.attrs.get("href", "")).query).get("view") == ["archive"] for node in tree.nodes("a"))


@pytest.mark.parametrize("archived", ["detail", "ticket"])
def test_archived_detail_is_read_only_even_stale_actions(payload, archived):
    target = payload["detail"] if archived == "detail" else payload["detail"]["ticket"]
    target["archived"] = True
    assert not posts(dom(view.render_workbench(**payload)))


def test_real_counts_pagination_and_not_invented_rows(payload):
    tree = dom(view.render_workbench(**payload))
    assert cls(tree, "ws-range").text() == "26–26 из 63"
    assert "Страница 2 из 3" in cls(tree, "ws-pagination").text()
    tabs = [node for node in tree.nodes("a") if "data-ws-tab" in node.attrs]
    assert [cls(node, "ws-count").text() for node in tabs] == ["39", "8", "24"]
    assert len(list(cls(tree, "ws-ticket-list").nodes("li"))) == 1
    for rel, expected in (("prev", "1"), ("next", "3")):
        link = next(tree.nodes("a", rel=rel))
        params = parse_qs(urlsplit(link.attrs["href"]).query)
        assert params["page"] == [expected]
        assert params["q"] == ["Анна & справка"] and params["t"] == ["71"]


def test_missing_counts_are_not_fabricated(payload):
    payload["queue"]["counts"] = {}
    tree = dom(view.render_workbench(**payload))
    tabs = cls(tree, "ws-tabs")
    assert not any(node.has_class("ws-count") for node in tabs.nodes())


def test_out_of_range_page_gives_first_page_link(payload):
    payload["queue"].update(rows=[], page=9, total=3)
    tree = dom(view.render_workbench(**payload))
    assert "На этой странице нет обращений" in tree.text()
    assert "Страница 9 из 1" in tree.text()
    assert "На первую страницу" in tree.text()


def test_query_whitelist_and_escaping(payload):
    payload["filters"].update(q='Анна & "Иван" + #%', **{"evil": "<script>", "return": "https://foreign.invalid"})
    tree = dom(view.render_workbench(**payload))
    for link in tree.nodes("a"):
        parts = urlsplit(link.attrs.get("href", ""))
        assert not parts.netloc
        params = parse_qs(parts.query)
        assert "evil" not in params
        if "q" in params:
            assert params["q"] == ['Анна & "Иван" + #%']


def test_standalone_returns_full_card_and_canonical_paths(payload):
    payload["standalone"] = True
    tree = dom(view.render_workbench(**payload))
    assert cls(tree, "ws-workbench").has_class("ws-standalone")
    assert not any(node.has_class("ws-queue") for node in tree.nodes())
    return_to = next(posts(tree)[0].nodes("input", name="return")).attrs["value"]
    assert urlsplit(return_to).path == "/workspace/tickets/71"
    assert "t" not in parse_qs(urlsplit(return_to).query)
    assert urlsplit(cls(tree, "ws-back").attrs["href"]).path == "/workspace/tickets"


def test_search_visible_advanced_collapsed_and_unique_ids(payload):
    tree = dom(view.render_workbench(**payload))
    advanced = cls(tree, "ws-filters-advanced")
    assert "open" not in advanced.attrs
    assert not list(advanced.nodes("input", name="q"))
    assert list(tree.nodes("input", name="q", type="search"))
    assert list(advanced.nodes("input", name="date_from", type="date"))
    ids = [node.attrs["id"] for node in tree.nodes() if "id" in node.attrs]
    assert len(ids) == len(set(ids))
    for label in tree.nodes("label"):
        if label.attrs.get("for"):
            assert label.attrs["for"] in ids


def test_live_age_is_principal_scoped_and_timezone_aware(payload):
    tree = dom(view.render_workbench(**payload))
    timers = [node for node in tree.nodes() if node.has_class("live-age")]
    assert len(timers) == 2
    for node in timers:
        assert node.attrs["data-principal-id"] == "42"
        assert node.attrs["data-time"].endswith("+05:00")
        assert node.attrs["data-timer-kind"] == "in_progress"
        assert int(node.attrs["data-time-ms"]) > 0


def test_unknown_timer_is_not_invented(payload):
    payload["detail"]["ticket"]["in_progress_at"] = "нет даты"
    payload["queue"]["rows"] = []
    tree = dom(view.render_workbench(**payload))
    assert "в работе · время не указано" in tree.text()
    assert not any(node.has_class("live-age") for node in tree.nodes())


def test_timeline_chronology_notes_delivery_and_names(payload):
    tree = dom(view.render_workbench(**payload))
    timeline = cls(tree, "ws-timeline")
    text = timeline.text()
    assert text.index("Нужна справка") < text.index("Статус изменён") < text.index("Проверить сведения")
    assert text.index("Проверить сведения") < text.index("Можно забрать") < text.index("Справку подготовим")
    assert cls(timeline, "ws-message-note").text().startswith("Внутренняя заметка")
    assert "Студенту не отправляется" in cls(timeline, "ws-message-note").text()
    assert "Отправлено в MAX" in cls(timeline, "ws-message-outgoing").text()
    assert "Доставлено" not in cls(timeline, "ws-message-outgoing").text()
    assert "ИсполнительИванова Мария Петровна" in cls(tree, "ws-context").text()


def test_initial_ticket_text_is_not_duplicated(payload):
    ticket = payload["detail"]["ticket"]
    payload["detail"]["thread"].append({"sender_role": "student", "sender_name": ticket["student_name"],
                                           "text": ticket["text_content"], "created_at": ticket["created_at"]})
    timeline = cls(dom(view.render_workbench(**payload)), "ws-timeline")
    assert timeline.text().count(ticket["text_content"]) == 1


def test_notices_no_technical_secrets(payload):
    payload["detail"].update(conflict=True, legacy_files_restricted=True, raw_error="SECRET_TOKEN")
    payload["detail"]["thread"][0].update(delivery_code=403, error="raw stack SECRET_TOKEN")
    html = view.render_workbench(**payload)
    assert "Обращение уже изменено" in html and "MAX не разрешил доставку" in html
    assert "Старые вложения без подтверждённой связи" in html
    assert "SECRET_TOKEN" not in html
    assert not list(dom(html).nodes("a", href="raw stack SECRET_TOKEN"))


def test_forbidden_detail_renders_no_ticket_secrets_or_controls(payload):
    payload["detail"].update(http_status=403)
    payload["detail"]["ticket"]["text_content"] = "Недоступный текст карточки"
    payload["detail"]["thread"][0]["text"] = "Недоступная переписка"
    html = view.render_workbench(**payload)
    assert "Обращение недоступно" in html
    assert "Недоступный текст карточки" not in html and "Недоступная переписка" not in html
    assert not posts(dom(html))
    assert cls(dom(html), "ws-workbench").attrs["data-ticket-id"] == ""


@pytest.mark.parametrize("href", ["javascript:alert(1)", "https://foreign.invalid/a.pdf", "//foreign.invalid/a.pdf", "/%2fforeign.invalid", "/\\foreign.invalid", "/safe%0aInjected"])
def test_file_links_reject_nonlocal_urls(payload, href):
    payload["detail"]["files"][0]["href"] = href
    files = cls(dom(view.render_workbench(**payload)), "ws-files")
    assert not list(files.nodes("a"))
    assert "Ссылка недоступна" in files.text()


def test_templates_progressive_and_text_limits(payload):
    tree = dom(view.render_workbench(**payload))
    reply = cls(tree, "ws-form-reply")
    preview = next(reply.nodes("button", value="template_preview"))
    assert "formnovalidate" in preview.attrs
    assert list(reply.nodes("select", name="template_id"))
    assert not list(reply.nodes("select", name="template"))
    assert "Не показывать сырой" not in tree.text()
    for mode, maximum in (("reply", "4000"), ("note", "200"), ("forward", "200")):
        field = next(cls(tree, "ws-form-" + mode).nodes("textarea"))
        assert field.attrs["maxlength"] == maximum
        assert field.attrs["rows"] == "5"


def test_allowed_statuses_do_not_add_current_forbidden_option(payload):
    payload["detail"]["allowed_statuses"] = ["completed"]
    tree = dom(view.render_workbench(**payload))
    select = next(cls(tree, "ws-form-status").nodes("select", name="status"))
    assert {option.attrs["value"] for option in select.nodes("option")} == {"", "completed"}


def test_server_draft_survives_conflict_and_opens_correct_mode(payload):
    payload["detail"].update(draft_mode="note", draft_text="Не потерять заметку <>&", conflict=True)
    tree = dom(view.render_workbench(**payload))
    assert next(cls(tree, "ws-form-note").nodes("textarea")).text() == "Не потерять заметку <>&"
    assert next(cls(tree, "ws-form-reply").nodes("textarea")).text() == ""
    assert "open" in cls(tree, "ws-more-actions").attrs
    assert "data-success-action" not in cls(tree, "ws-workbench").attrs


@pytest.mark.parametrize("location", ["detail", "queue"])
def test_prg_success_explicit_marker_not_query(payload, location):
    payload[location]["action_success"] = {"ticket_id": 71, "action": "reply_close", "operation_key": 'operation-"71'}
    root = cls(dom(view.render_workbench(**payload)), "ws-workbench")
    assert root.attrs["data-success-ticket"] == "71"
    assert root.attrs["data-success-action"] == "reply_close"
    assert root.attrs["data-success-operation"] == 'operation-"71'
    payload[location].pop("action_success")
    payload["filters"].update(success="1", action="reply_close", operation_key='operation-"71')
    root = cls(dom(view.render_workbench(**payload)), "ws-workbench")
    assert "data-success-action" not in root.attrs


def test_preview_is_never_success_marker(payload):
    payload["detail"]["action_success"] = {"ticket_id": 71, "action": "template_preview", "operation_key": "some"}
    assert "data-success-action" not in cls(dom(view.render_workbench(**payload)), "ws-workbench").attrs


def test_empty_queue_and_detail(payload):
    payload.update(detail=None, filters={})
    payload["queue"].update(rows=[], total=0, counts={"open": 0, "waiting": 0, "closed": 0}, page=1)
    tree = dom(view.render_workbench(**payload))
    assert "Здесь пока нет обращений" in tree.text()
    assert "Выберите обращение" in tree.text()
    assert not posts(tree)
    assert not any(node.has_class("ws-pagination") for node in tree.nodes())


def test_small_dict_aliases(payload):
    payload["detail"]["ticket"] = {"id": 72, "author_name": "Синтетический автор", "handler_name": "Синтетический исполнитель", "text": "Текст", "status": "new"}
    payload["detail"]["thread"] = []
    tree = dom(view.render_workbench(**payload))
    assert "Синтетический автор" in cls(tree, "ws-detail").text()
    assert "Синтетический исполнитель" in cls(tree, "ws-context").text()
    assert all(form.attrs["action"].endswith("/72/action") for form in posts(tree))


def test_css_tokens_scoping_mobile_accessibility_and_auth():
    css = CSS.read_text(encoding="utf-8-sig")
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert used and used <= css_names()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css)
    assert "wb-" not in css and "landing" not in css
    assert re.search(r"@media\s*\(max-width:\s*1000px\)", css)
    assert re.search(r"\.ws-has-detail\s+\.ws-queue\s*\{\s*display:\s*none", css)
    assert ".ws-standalone .ws-layout" in css
    assert "font-size: 18px" in css and "font-size: 16px" in css
    assert "min-height: var(--tap-min)" in css and "prefers-reduced-motion" in css
    assert "position: fixed" not in css
    for name in (".ws-auth-shell", ".ws-auth-card", ".ws-auth-brand", ".ws-auth-intro", ".ws-auth-form", ".ws-auth-field", ".ws-auth-hint", ".ws-auth-notice", ".ws-app .ws-app-main", ".ws-app .ws-app-header"):
        assert name in css


def test_js_no_html_injection_no_localstorage_or_caret_jumps():
    js = JS.read_text(encoding="utf-8-sig")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "eval(", "setSelectionRange", ".focus(", "history.pushState", "location.replace"):
        assert forbidden not in js
    assert "sessionStorage" in js and "JSON.stringify" in js and "textContent" in js
    assert 'credentials: "same-origin"' in js and 'redirect: "error"' in js
    assert 'Accept: "text/plain, application/json"' in js
    assert 'input[name="csrf"]' in js
    assert '"beforeunload"' in js and '"pageshow"' in js


@pytest.mark.parametrize("path", [SOURCE, CSS, JS, Path(__file__)])
def test_sources_utf8_bom_and_readable_russian(path):
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "\ufffd" not in text
    assert re.search("[А-Яа-я]", text)


@pytest.mark.parametrize("mutation", ["csrf", "escaping", "actions"])
def test_important_rules_detect_in_memory_mutations(payload, mutation):
    source = SOURCE.read_text(encoding="utf-8-sig")
    if mutation == "csrf":
        source = source.replace('_hidden("csrf", csrf_token)', '\'\'')
    elif mutation == "escaping":
        source = source.replace('return escape(_text(value), quote=True)', 'return _text(value)')
        payload["detail"]["ticket"]["text_content"] = '<img src=x onerror="bad()">'
    else:
        source = source.replace('ACTIONS.intersection(detail.get("allowed_actions") or [])', 'ACTIONS')
        payload["detail"]["allowed_actions"] = []
    altered = ModuleType("workspace_renderer_mutated")
    exec(compile(source, str(SOURCE), "exec"), altered.__dict__)
    html = altered.render_workbench(**payload)
    with pytest.raises((AssertionError, KeyError)):
        if mutation == "csrf":
            _assert_csrf(html)
        elif mutation == "escaping":
            _assert_escaped(html)
        else:
            assert not actions(dom(html))


# Маленькая модель DOM для поведения JS. Это не браузерная проверка вёрстки.
JS_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const values = new Map();
const key = 'ws-draft:v2:%2Fworkspace%2Ftickets:42:71:reply';
const record = text => ({v: 1, text, pending: {operation: 'operation-42-71', action: 'reply', text}});
if (['restore', 'success', 'mismatch', 'private'].includes(scenario)) values.set(key, JSON.stringify(record('Черновик')));
const events = [];
const calls = [];
let consent = false;
let resolveFetch;
class Target {
  constructor() { this.listeners = {}; this.attrs = {}; this.dataset = {}; this.textContent = ''; }
  addEventListener(kind, fn) { (this.listeners[kind] ||= []).push(fn); }
  fire(kind, event = {}) { (this.listeners[kind] || []).forEach(fn => fn(event)); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
}
const document = new Target();
const window = new Target();
const root = new Target();
root.dataset = {userId: '42', ticketId: '71', basePath: '/workspace/tickets'};
if (scenario === 'success' || scenario === 'mismatch') Object.assign(root.dataset, {successAction: 'reply', successTicket: '71', successOperation: scenario === 'success' ? 'operation-42-71' : 'another-operation'});
const form = new Target();
form.dataset = {draftMode: 'reply', ticketId: '71'};
form.setAttribute('action', '/workspace/tickets/71/action');
// Кнопки name=action перекрывают JS-свойство формы в настоящем браузере.
form.action = [{name: 'action', value: 'reply'}, {name: 'action', value: 'template_preview'}];
const field = new Target();
field.value = ['failed', 'bfcache', 'bfcache-new', 'preview-html', 'private'].includes(scenario) ? 'Свой ответ' : '';
field.maxLength = 4000;
field.focus = () => { throw Error('Курсор не должен прыгать'); };
const select = new Target();
select.value = '3'; select.form = form; select.matches = s => s === '[data-ws-template]';
const previewButton = new Target();
previewButton.name = 'action'; previewButton.value = 'template_preview'; previewButton.textContent = 'Подставить';
const submitButton = new Target();
submitButton.name = 'action'; submitButton.value = 'reply';
const status = new Target();
const announcer = new Target();
const hidden = {csrf: {value: 'csrf-42-71'}, operation_key: {value: 'operation-42-71'}, revision: {value: 'rev-7'}};
form.querySelector = s => {
  if (s === '[data-ws-draft-field]') return field;
  if (s === '[data-ws-template]') return select;
  if (s === '[data-ws-template-preview]') return previewButton;
  if (s === '[data-ws-draft-status]') return status;
  const m = s.match(/^input\[name="([^"]+)"\]$/); return m ? hidden[m[1]] || null : null;
};
root.querySelector = s => s === '[data-ws-announcer]' ? announcer : null;
root.querySelectorAll = s => s === '[data-ws-action-form]' ? [form] : [];
document.querySelector = s => s === '[data-workspace]' ? root : null;
document.hidden = false;
window.location = {href: 'https://college.test/workspace/tickets?t=71', origin: 'https://college.test', pathname: '/workspace/tickets', search: '?t=71'};
window.setTimeout = (fn, ms) => { events.push({fn, ms}); };
window.confirm = () => consent;
window.sessionStorage = {
  getItem: k => { if (scenario === 'private') throw Error('Недоступно'); return values.get(k) || null; },
  setItem: (k, v) => { if (scenario === 'private') throw Error('Недоступно'); values.set(k, v); },
  removeItem: k => { if (scenario === 'private') throw Error('Недоступно'); values.delete(k); }
};
class FormData {
  constructor() { this.values = {csrf: hidden.csrf.value, operation_key: hidden.operation_key.value, revision: hidden.revision.value, template_id: select.value, text: field.value}; }
  set(k, v) { this.values[k] = v; }
}
function response(type = 'text/plain') {
  return {ok: true, status: 200, headers: {get: () => type}, text: async () => '<b>Подставленный текст</b>', json: async () => ({text: '<b>Подставленный текст</b>'})};
}
window.fetch = async (url, opts) => {
  calls.push({url, method: opts.method, credentials: opts.credentials, accept: opts.headers.Accept, mime: opts.headers["Content-Type"], data: Object.fromEntries(opts.body)});
  if (scenario === 'preview-race') return new Promise(resolve => { resolveFetch = resolve; });
  return response(scenario === 'preview-html' ? 'text/html' : 'text/plain');
};
function event(target, submitter) {
  return {target, submitter, defaultPrevented: false, button: 0,
    preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; }};
}
vm.runInNewContext(source, {document, window, URL, URLSearchParams, FormData, AbortController, console});
const result = {restored: field.value};
(async () => {
  if (scenario === 'restore' || scenario === 'private') {
    field.value = 'Изменённый ответ'; field.fire('input');
    const leave = event(); window.fire('beforeunload', leave); result.guarded = leave.defaultPrevented;
    const link = new Target(); link.href = 'https://college.test/workspace/tickets?t=72'; link.attrs.href = link.href;
    const click = event({closest: () => link}); document.fire('click', click); result.linkCancelled = click.defaultPrevented;
  }
  if (['failed', 'bfcache', 'bfcache-new'].includes(scenario)) {
    const submit = event(form, submitButton); document.fire('submit', submit);
    result.submitCancelled = submit.defaultPrevented; result.disabled = Boolean(submitButton.disabled);
    result.afterSubmit = JSON.parse(values.get(key));
    if (scenario.startsWith('bfcache')) {
      values.set(key, JSON.stringify({v: 1, text: scenario === 'bfcache-new' ? 'Новый черновик' : '', confirmed: {operation: 'operation-42-71', action: 'reply'}}));
      window.fire('pageshow', {persisted: true});
    }
  }
  if (scenario.startsWith('preview')) {
    consent = true;
    const submit = event(form, previewButton); document.fire('submit', submit); result.submitCancelled = submit.defaultPrevented;
    if (scenario === 'preview-race') { field.value = 'Набрано во время загрузки'; field.fire('input'); resolveFetch(response()); }
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
  }
  result.text = field.value; result.saved = values.has(key) ? JSON.parse(values.get(key)) : null;
  result.message = status.textContent || announcer.textContent; result.calls = calls;
  console.log(JSON.stringify(result));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def run_js(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node не установлен: чистые проверки JS не запущены")
    result = subprocess.run([node, "-e", JS_HARNESS, str(JS), scenario], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_restore_guard_and_scoped_storage():
    result = run_js("restore")
    assert result["restored"] == "Черновик"
    assert result["saved"]["text"] == "Изменённый ответ"
    assert result["guarded"] and result["linkCancelled"]


def test_js_private_storage_does_not_break_composer():
    result = run_js("private")
    assert result["text"] == "Изменённый ответ"
    assert result["guarded"] and result["linkCancelled"]
    assert "не разрешил" in result["message"]


def test_js_confirmed_success_clears_matching_submission_only():
    result = run_js("success")
    assert result["text"] == "" and result["saved"]["text"] == ""
    assert result["saved"]["confirmed"]["operation"] == "operation-42-71"
    other = run_js("mismatch")
    assert other["text"] == "Черновик" and other["saved"]["text"] == "Черновик"


def test_js_submission_keeps_draft_and_submitter_action():
    result = run_js("failed")
    assert not result["submitCancelled"] and not result["disabled"]
    assert result["afterSubmit"]["text"] == "Свой ответ"
    assert result["afterSubmit"]["pending"]["action"] == "reply"


@pytest.mark.parametrize("scenario, expected", [("bfcache", ""), ("bfcache-new", "Новый черновик")])
def test_js_back_cache_does_not_revive_confirmed_message(scenario, expected):
    assert run_js(scenario)["text"] == expected


def test_js_template_preview_plain_text_csrf_and_no_mutating_submit():
    result = run_js("preview")
    assert result["submitCancelled"]
    assert result["text"] == "<b>Подставленный текст</b>"
    call = result["calls"][0]
    assert call["method"] == "POST" and call["credentials"] == "same-origin"
    assert call["mime"] == "application/x-www-form-urlencoded;charset=UTF-8"
    assert call["data"]["csrf"] == "csrf-42-71" and call["data"]["revision"] == "rev-7"
    assert call["data"]["action"] == "template_preview"
    assert result["saved"]["pending"] is None


def test_js_template_race_preserves_user_typing():
    result = run_js("preview-race")
    assert result["text"] == "Набрано во время загрузки"
    assert "изменили ответ" in result["message"]


def test_js_template_html_response_never_becomes_composer_text():
    result = run_js("preview-html")
    assert result["text"] == "Свой ответ"
    assert "не вернул текст" in result["message"]


def test_admin_legacy_filter_storage_is_explicit_no_staff_restore(payload):
    staff = cls(dom(view.render_workbench(**payload)), "ws-filters")
    assert "data-filters" not in staff.attrs and "data-filter-key" not in staff.attrs
    payload["actor"]["is_admin"] = True
    payload["filters"]["mine"] = "1"
    tree = dom(view.render_workbench(**payload))
    form = cls(tree, "ws-filters")
    assert form.attrs["data-filters"] == "status,category,q,scope,view,mine,date_from,date_to"
    assert form.attrs["data-filter-key"] == "tickets:42"
    assert "data-filters-saved" not in form.attrs
    mine = next(form.nodes("select", name="mine"))
    assert list(mine.nodes("option", value="1", selected=None))
    assert any("data-filters-reset" in node.attrs for node in form.nodes("a"))


def test_overdue_control_is_in_advanced_and_uses_real_count(payload):
    payload["queue"]["counts"]["overdue"] = 4
    tree = dom(view.render_workbench(**payload))
    link = cls(cls(tree, "ws-filters-advanced"), "ws-filter-overdue")
    assert "Дольше суток" in link.text() and cls(link, "ws-count").text() == "4"
    query = parse_qs(urlsplit(link.attrs["href"]).query)
    assert query["scope"] == ["overdue"] and query["tab"] == ["open"] and query["page"] == ["1"]
    assert query["q"] == ["Анна & справка"]
    payload["filters"]["scope"] = "overdue"
    off = cls(dom(view.render_workbench(**payload)), "ws-filter-overdue")
    assert "Все сроки" in off.text()
    assert "scope" not in parse_qs(urlsplit(off.attrs["href"]).query)



def test_blank_message_event_is_not_repeated_and_status_is_human(payload):
    message = next(row for row in payload["detail"]["thread"] if row["sender_role"] == "staff")
    message["sender_id"] = "42"
    payload["detail"]["events"] = [
        {"event": "message_staff", "detail": "", "actor_id": "42", "created_at": message["created_at"]},
        {"event": "status", "detail": "accepted", "actor_id": "42", "created_at": message["created_at"]},
    ]
    tree = dom(view.render_workbench(**payload))
    timeline = cls(tree, "ws-timeline")
    descriptions = [node.text() for node in timeline.nodes() if node.has_class("ws-event-description")]
    assert "Принято" in descriptions and "accepted" not in descriptions
    titles = [node.text() for node in timeline.nodes() if node.has_class("ws-event-title")]
    assert "Ответ сотрудника" not in titles


def test_plain_admin_entry_restores_filters_only_when_requested(payload):
    payload["actor"].update(is_admin=True, restore_filters=True)
    tree = dom(view.render_workbench(**payload))
    assert cls(tree, "ws-filters").attrs["data-filters-saved"] == "1"
    payload["actor"]["restore_filters"] = False
    assert "data-filters-saved" not in cls(dom(view.render_workbench(**payload)), "ws-filters").attrs


def test_composer_cannot_overlay_history():
    css = CSS.read_text(encoding="utf-8-sig")
    rule = re.search(r"\.ws-composer\s*\{([^}]*)\}", css)[1]
    assert "position: static" in rule
    assert "position: sticky" not in rule and "position: fixed" not in rule


@pytest.mark.parametrize("allowed", [["reply", "reply_close"], ["reply"], ["reply_close"]])
def test_plain_reply_is_first_and_close_is_quiet(payload, allowed):
    payload["detail"]["allowed_actions"] = allowed
    tree = dom(view.render_workbench(**payload))
    buttons = list(cls(tree, "ws-compose-actions").nodes("button"))
    assert [button.attrs["value"] for button in buttons] == allowed
    for button in buttons:
        assert button.has_class("ws-btn-primary") == (button.attrs["value"] == "reply")
        assert button.has_class("ws-btn-quiet") == (button.attrs["value"] == "reply_close")


def test_history_scrolls_with_document_and_queue_passes_wheel():
    css = CSS.read_text(encoding="utf-8-sig")
    history = re.findall(r"\.ws-history-scroll\s*\{([^}]*)\}", css)
    assert history and all("max-height: none" in rule and "overflow: visible" in rule for rule in history)
    queue = re.search(r"\.ws-ticket-list\s*\{([^}]*)\}", css)[1]
    assert "overflow-y: auto" in queue and "overscroll-behavior: auto" in queue
    assert "overscroll-behavior: contain" not in css
    assert ".ws-app .ws-app-header .ws-brand" in css


def test_auth_layout_has_real_header_and_reduced_motion():
    css = CSS.read_text(encoding="utf-8-sig")
    body = re.search(r"body\.ws-auth\s*\{([^}]*)\}", css)[1]
    assert "display: flex" in body and "flex-direction: column" in body
    assert "place-items: center" not in body
    assert ".ws-auth .ws-auth-header" in css and ".ws-auth .ws-auth-theme" in css
    assert "grid-template-columns: minmax(0, 1.08fr) minmax(340px, .92fr)" in css
    assert "@media (max-width: 900px)" in css and ".ws-auth *::before" in css
