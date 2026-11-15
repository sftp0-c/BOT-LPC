"""История выпусков панели: только заранее подготовленные данные, только владельцу."""
import re
from collections.abc import Mapping

from fastapi import HTTPException, Request

import repository as repo
import version

from .common import esc, log, page, require_user
from .router import router
from .updates_data import RELEASES, SOURCE_HEAD

SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHORT_HASH = re.compile(r"[0-9a-f]{7,12}")


async def require_owner(request: Request) -> str:
    """Чужому сис-админу раздел отвечает 404, как «Данные» и «Тест»."""
    user = await require_user(request)
    if not await repo.is_owner(user):
        log.warning("панель: раздел обновлений попытался открыть не владелец: id=%s", user)
        raise HTTPException(status_code=404, detail="Раздел не найден")
    return user


def _number(value, *, maximum: int = 100_000_000) -> int:
    return value if type(value) is int and 0 <= value <= maximum else 0


def _safe_release(item: Mapping) -> dict | None:
    """Строгая структура не позволяет данным snapshot стать HTML-контрактом."""
    value = str(item.get("version", ""))
    tag = str(item.get("tag", ""))
    state = str(item.get("state", ""))
    base_version = str(item.get("base_version", ""))
    if state == "released":
        if not SEMVER.fullmatch(value) or tag != "v" + value or base_version:
            return None
    elif state == "development":
        if value or tag or not SEMVER.fullmatch(base_version):
            return None
    else:
        return None
    notes = tuple(str(note)[:300] for note in item.get("notes", ()) if isinstance(note, str))[:12]
    commits = []
    for row in item.get("commits", ()):
        if not isinstance(row, Mapping):
            continue
        short = str(row.get("hash", ""))
        subject = str(row.get("subject", ""))
        when = str(row.get("when", ""))
        if not SHORT_HASH.fullmatch(short) or not subject or len(subject) > 220 or len(when) > 32:
            continue
        commits.append({"hash": short, "subject": subject, "when": when,
                        "files": _number(row.get("files")), "add": _number(row.get("add")),
                        "del": _number(row.get("del"))})
    if not commits:
        return None
    return {"version": value, "tag": tag, "state": state, "base_version": base_version,
            "date": str(item.get("date", ""))[:40], "notes": notes, "commits": tuple(commits)}


def _word(number: int, forms: tuple[str, str, str]) -> str:
    value = abs(number) % 100
    if 11 <= value <= 14:
        return forms[2]
    return {1: forms[0], 2: forms[1], 3: forms[1], 4: forms[1]}.get(value % 10, forms[2])


def _commit(row: Mapping) -> str:
    files = _number(row["files"])
    return (f'<li class="upd-commit"><code class="upd-hash">{esc(row["hash"])}</code>'
            f'<span class="upd-subject">{esc(row["subject"])}</span>'
            f'<span class="upd-meta">{esc(row["when"])} · {files} '
            f'{_word(files, ("файл", "файла", "файлов"))} · '
            f'<b class="upd-add">+{_number(row["add"])}</b> '
            f'<b class="upd-del">−{_number(row["del"])}</b></span></li>')


def _release(item: Mapping, current: str) -> str:
    rel = _safe_release(item)
    if rel is None:
        return ""
    live = rel["state"] == "released" and rel["version"] == current
    development = rel["state"] == "development"
    label = f'После {rel["base_version"]}' if development else rel["version"]
    badge = ('<b class="upd-badge upd-badge-live">Сейчас на сервере</b>' if live else
             '<b class="upd-badge upd-badge-dev">Разрабатывается</b>' if development else "")
    notes = "".join(f'<li>{esc(note)}</li>' for note in rel["notes"])
    notes_html = f'<ul class="upd-highlights">{notes}</ul>' if notes else ""
    commits = "".join(_commit(row) for row in rel["commits"])
    total_files = sum(row["files"] for row in rel["commits"])
    dom_id = "release-development" if development else "release-" + rel["version"].replace(".", "-")
    return (f'<li class="upd-release" id="{esc(dom_id)}" data-release="{esc(label)}">'
            f'<details{" open" if live or development else ""}><summary class="upd-summary">'
            '<span class="upd-node" aria-hidden="true"><img src="/public/logo-emblem.png" '
            'width="16" height="16" alt=""></span>'
            f'<span class="upd-version">{esc(label)}</span>'
            f'<span class="upd-date">{esc(rel["date"])}</span>'
            f'<span class="upd-stats">{len(rel["commits"])} '
            f'{_word(len(rel["commits"]), ("коммит", "коммита", "коммитов"))}, '
            f'{total_files} {_word(total_files, ("файл", "файла", "файлов"))}</span>{badge}'
            f'</summary><div class="upd-body">{notes_html}<ul class="upd-commits">{commits}'
            '</ul></div></details></li>')


def render_updates(releases=RELEASES, source_head: str = SOURCE_HEAD, current: str | None = None) -> str:
    current = version.__version__ if current is None else current
    if not SHORT_HASH.fullmatch(str(source_head)):
        raise ValueError("Некорректный источник истории")
    cards = "".join(_release(item, current) for item in releases if isinstance(item, Mapping))
    if not cards:
        cards = '<li class="upd-empty">История выпусков пока не собрана.</li>'
    return f"""<section class="upd-wrap" data-updates-root>
<p class="upd-lead">Версии и заметные изменения «Навигатора ЛПК». История подготовлена
при разработке и не обращается к GitHub, Git или рабочей базе во время просмотра.</p>
<div class="upd-tools"><label class="upd-search" for="updates-filter"><span>Поиск</span>
<input id="updates-filter" type="search" autocomplete="off" placeholder="Версия, слово или хеш"></label>
<button class="btn btn-grey btn-sm" type="button" data-updates-expand>Развернуть все</button>
<button class="btn btn-grey btn-sm" type="button" data-updates-collapse>Свернуть все</button></div>
<p class="upd-results" data-updates-results role="status" aria-live="polite"></p>
<ol class="upd-timeline">{cards}</ol>
<p class="upd-note">История кода по коммиту <code>{esc(source_head)}</code>. Коммит,
который только обновляет этот снимок, намеренно не включает сам себя.</p></section>"""


@router.get("/updates", include_in_schema=False)
async def updates_page(request: Request):
    user = await require_owner(request)
    return page("Обновления", render_updates(), user=user, tab="updates",
                extra_scripts=f'<script src="/panel/static/updates.js?v={version.__version__}" defer></script>')
