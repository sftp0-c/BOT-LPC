"""Безопасный host-only генератор истории выпусков панели."""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
import os
import pprint
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "web" / "updates_data.py"
SEMVER_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
URL_CREDENTIALS = re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.I)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:token|secret|password|passwd|пароль|пароля|invite|приглашение|приглашения)"
    r"[ ]*(?:[:=_-][ ]*|[ ]+)[A-Za-z0-9_./+=-]{6,}"
)
CODE_VALUE = re.compile(r"\bкод(?:а|ом|ы)?[ ]+(?:[A-Z0-9][A-Z0-9_-]{5,})\b")
LONG_TOKEN = re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|[A-Za-z0-9_-]{32,})\b")
LONG_NUMBER = re.compile(r"(?<![0-9])[1-9][0-9]{9,18}(?![0-9])")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
NOTES = {
    (2, 2, 0): (
        "Витрина показывает семь шагов работы сервиса на синтетических данных.",
        "Эмблема колледжа добавлена в сайт, панель, кабинет и социальную карточку.",
    ),
    (2, 1, 0): (
        "Обновлены вход и рабочее место сотрудников; обратная связь получила паузу и безопасное удаление.",
    ),
    (2, 0, 0): (
        "Добавлены веб-кабинет сотрудников, отзывные сеансы и отдельные права на работу с обращениями.",
    ),
}


class BuildError(RuntimeError):
    pass


def git(*args: str, root: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="strict")
    if result.returncode:
        raise BuildError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_TAG.fullmatch(value)
    if not match:
        raise BuildError(f"Недопустимая метка выпуска: {value}")
    return tuple(int(item) for item in match.groups())


def safe_subject(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 220 or CONTROL.search(value) or URL_CREDENTIALS.search(value) \
            or SECRET_ASSIGNMENT.search(value) or CODE_VALUE.search(value) \
            or LONG_TOKEN.search(value) or LONG_NUMBER.search(value):
        raise BuildError("Тема коммита требует ручной проверки и не может попасть в историю")
    return value


def human_date(value: str) -> str:
    moment = datetime.strptime(value, "%Y-%m-%d")
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def parse_records(text: str) -> tuple[dict, ...]:
    """Разобрать NUL-разделённые записи git log --numstat."""
    rows = []
    blocks = text.split("\x1e")
    for block in blocks:
        block = block.strip("\n\x00")
        if not block:
            continue
        header, _, stats = block.partition("\n")
        parts = header.split("\x1f", 3)
        if len(parts) != 4:
            raise BuildError("Повреждён вывод git log")
        short, full, date, subject = parts
        if not re.fullmatch(r"[0-9a-f]{7,12}", short) or not re.fullmatch(r"[0-9a-f]{40,64}", full):
            raise BuildError("Некорректный хеш коммита")
        files = added = deleted = 0
        for line in stats.splitlines():
            columns = line.split("\t", 2)
            if len(columns) < 3:
                continue
            files += 1
            if columns[0].isdigit():
                added += int(columns[0])
            if columns[1].isdigit():
                deleted += int(columns[1])
        rows.append({"hash": short, "full": full, "date": date,
                     "when": datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y"),
                     "subject": safe_subject(subject), "files": files,
                     "add": added, "del": deleted})
    return tuple(rows)


def commits(revision_range: str, *, root: Path = ROOT) -> tuple[dict, ...]:
    value = git("log", "--no-merges", "--date=format:%Y-%m-%d",
                "--pretty=format:%x1e%h%x1f%H%x1f%ad%x1f%s", "--numstat",
                revision_range, root=root)
    return parse_records(value)


def build(*, root: Path = ROOT, source_head: str = "HEAD", output: Path = OUTPUT) -> dict:
    if git("status", "--porcelain=v1", "--untracked-files=no", root=root).strip():
        raise BuildError("Рабочее дерево содержит незакоммиченные отслеживаемые файлы")
    full_head = git("rev-parse", f"{source_head}^{{commit}}", root=root).strip()
    short_head = git("rev-parse", "--short=7", full_head, root=root).strip()
    tags = [item for item in git("tag", "--merged", full_head, "--list", "v*", root=root).splitlines() if item]
    ordered = sorted(tags, key=semver)
    if len(set(ordered)) != len(ordered):
        raise BuildError("Повторяющиеся метки выпуска")
    tag_heads = {}
    for tag in ordered:
        tag_head = git("rev-list", "-n", "1", tag, root=root).strip()
        tag_heads[tag] = tag_head
        if subprocess.run(["git", "merge-base", "--is-ancestor", tag_head, full_head], cwd=root).returncode:
            raise BuildError(f"Метка {tag} не является предком SOURCE_HEAD")
    for previous, following in zip(ordered, ordered[1:]):
        if subprocess.run(["git", "merge-base", "--is-ancestor", tag_heads[previous], tag_heads[following]], cwd=root).returncode:
            raise BuildError(f"Метки {previous} и {following} не образуют линейную историю")
    releases = []
    if ordered:
        current = commits(f"{ordered[-1]}..{full_head}", root=root)
        if current:
            releases.append({"version": "0.0.0", "tag": "", "state": "development",
                             "date": human_date(current[0]["date"]), "notes": (), "commits": current})
    for index in range(len(ordered) - 1, -1, -1):
        tag = ordered[index]
        previous = ordered[index - 1] if index else None
        revision_range = f"{previous}..{tag}" if previous else tag
        rows = commits(revision_range, root=root)
        if not rows:
            raise BuildError(f"Пустой выпуск {tag}")
        releases.append({"version": tag[1:], "tag": tag, "state": "released",
                         "date": human_date(git("log", "-1", "--format=%as", tag, root=root).strip()),
                         "notes": NOTES.get(semver(tag), ()), "commits": rows})
    clean = []
    for release in releases:
        clean.append({**release, "commits": tuple({key: value for key, value in row.items()
                                                    if key not in {"full", "date"}}
                                                   for row in release["commits"])})
    snapshot = {"source_head": short_head, "releases": tuple(clean)}
    text = ('"""Снимок истории выпусков; генерируется make_updates.py, вручную не изменять."""\n\n'
            f'SOURCE_HEAD = {short_head!r}\n'
            'RELEASES = ' + pprint.pformat(snapshot["releases"], width=110, sort_dicts=False) + '\n')
    if full_head in text or any("/" in row["hash"] for rel in clean for row in rel["commits"]):
        raise BuildError("Snapshot содержит запрещённые данные")
    temporary = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=output.parent, prefix=output.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(b"\xef\xbb\xbf" + text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        decoded = temporary.read_text(encoding="utf-8-sig")
        compile(decoded, str(output), "exec")
        tree = ast.parse(decoded)
        if len(tree.body) != 3:
            raise BuildError("Snapshot содержит лишний исполняемый код")
        temporary.replace(output)
        if output.read_text(encoding="utf-8-sig") != decoded:
            raise BuildError("Проверка записи snapshot не прошла")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return snapshot


def main() -> None:
    snapshot = build()
    total = sum(len(item["commits"]) for item in snapshot["releases"])
    print(f"updates_data.py: SOURCE_HEAD={snapshot['source_head']}, выпусков={len(snapshot['releases'])}, коммитов={total}")


if __name__ == "__main__":
    main()
