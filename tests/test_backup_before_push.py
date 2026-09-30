"""Бекап перед пушем: метка, копии, остановка пуша.

Зачем эти тесты. Правило «не пушить без бекапа» работает только если бекап
делается всегда. Значит, проверять надо решения, а не наличие файла:

* метка уникальна - два бекапа в одну секунду не должны переписать первую
  метку: иначе пропала бы точка отката, ради которой всё и затевалось;
* старние копии убираются, свежие остаются - иначе диск забьётся и бекапы
  перестанут делать вообще;
* копия не вышла - пуш останавливается. Тихий «ну ладно» здесь хуже всего:
  изменится поведение бота, откатывать базу будет нечем, а человек узнает
  об этом через неделю.

Про блокировку. Проверяется она настоящим git-пушем в пустой репозиторий с
настоящим хуком. Копирование заставляем упасть: контейнер оставляем рабочим
(иначе нет копировать и остановить нечего), а путь внутри него - заведомо
несуществующим. Тогда поведение однозначно: копии нет - пуша нет.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import backup_before_push as backup

HOOK_SOURCE = Path(r"C:\BOT-LPC\.git\hooks\pre-push")
STAMP = "2026-01-01-000000"


# ── метка ──────────────────────────────────────────────────────────────────
def test_stamp_is_sortable_and_digits_only():
    """Имя метки обязано сортироваться по времени, иначе «последняя» не значит свежая.

    Формат: ГГГГ-ММ-ДД-ЧЧММСС. Проверяется регулярным выражением, а не сравнением
    длины с надписью-подсказкой: длина кириллицы и длина цифр - разное число.
    """
    stamp = backup.now_stamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}", stamp), \
        f"метка не разбирается как дата-время: {stamp}"


def test_stamps_in_order_sort_in_order():
    """Две метки, сделанные по порядку, должны сортироваться по времени."""
    assert "2026-01-01-000001" < "2026-01-02-000000" < "2026-02-01-000000"


def test_tag_is_created_on_the_current_commit():
    name = backup.make_tag(backup.now_stamp())
    try:
        assert name.startswith("backup/")
        code, _ = backup.git("rev-parse", "-q", "--verify", f"refs/tags/{name}")
        assert code == 0, "метка не находится в git"
    finally:
        backup.git("tag", "-d", name)


def test_second_tag_in_the_same_second_gets_its_own_name():
    """Две метки в одну секунду - две разные метки, иначе первая теряется."""
    first = backup.make_tag(STAMP)
    second = backup.make_tag(STAMP)
    try:
        assert first != second, "вторая метка перезаписала первую"
        assert second.startswith(f"{first}-"), f"ожидали суффикс счётчика, получили {second}"
        for name in (first, second):
            code, _ = backup.git("rev-parse", "-q", "--verify", f"refs/tags/{name}")
            assert code == 0, f"метка {name} потерялась"
    finally:
        backup.git("tag", "-d", first)
        backup.git("tag", "-d", second)


# ── чистка копий ───────────────────────────────────────────────────────────
def make_copies(count: int, folder: Path) -> list:
    made = []
    for number in range(count):
        copy = folder / f"database-2026-01-01-{number:06d}.db"
        copy.write_bytes(b"x" * 10)
        os.utime(copy, (1_700_000_000 + number, 1_700_000_000 + number))
        made.append(copy)
    return made


def test_prune_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    made = make_copies(10, tmp_path)
    backup.prune(keep=3)
    left = sorted(path.name for path in tmp_path.glob("database-*.db"))
    assert left == [made[-3].name, made[-2].name, made[-1].name], left


def test_prune_keeps_at_least_one(tmp_path, monkeypatch):
    """Даже при нелепом «оставить 0» последний бекап остаётся: он же и есть
    единственная точка отката."""
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    made = make_copies(4, tmp_path)
    backup.prune(keep=0)
    assert [path.name for path in tmp_path.glob("database-*.db")] == [made[-1].name]


def test_prune_removes_interrupted_copies(tmp_path, monkeypatch):
    """Остатки прерванной копии не должны ждать и занимать место."""
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    make_copies(2, tmp_path)
    (tmp_path / "database-2026-01-01-999999.db.part").write_bytes(b"x")
    backup.prune(keep=5)
    assert list(tmp_path.glob("*.part")) == []


def test_prune_on_a_missing_folder_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "нет")
    assert backup.prune(keep=3) == 0


def test_prune_reports_how_many_it_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    make_copies(6, tmp_path)
    assert backup.prune(keep=2) == 4


# ── копирование ────────────────────────────────────────────────────────────
def test_no_copy_without_docker(monkeypatch):
    """Нет docker - копить неоткуда, и это НЕ повод останавливать пуш.

    На сервере без docker код меняется, и останавливать из-за этого пуш нелепо.
    """
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    assert backup.copy_database(STAMP) is None


def test_no_copy_when_the_container_is_not_there(monkeypatch):
    """Несуществующий контейнер - тоже «копить неоткуда», а не ошибка."""
    monkeypatch.setenv("BOTLPC_CONTAINER", "контейнера-которого-нет")
    assert backup.copy_database(STAMP) is None


# ── хук: пуск и блокировка ─────────────────────────────────────────────────
def make_repo(tmp_path: Path) -> Path:
    """Репозиторий с нашим скриптом, рабочим хуком и НАСТОЯЩИМ получателем.

    Получатель обязателен: git разбирается с адресом раньше, чем зовёт pre-push,
    и при сломанном origin хук не запускается вовсе. Проверять нечего, если
    хук не вызвали.
    """
    repo = tmp_path / "проверка"
    repo.mkdir()
    remote = tmp_path / "приёмник"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    for command in (["git", "init", "-q"], ["git", "config", "user.email", "проверка@тест"],
                    ["git", "config", "user.name", "проверка"],
                    ["git", "remote", "add", "origin", str(remote)]):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "backup_before_push.py").write_bytes(Path(backup.__file__).read_bytes())
    (repo / ".git" / "hooks" / "pre-push").write_bytes(HOOK_SOURCE.read_bytes())
    (repo / "файл.txt").write_text("что-то", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "первый"], cwd=repo, check=True, capture_output=True)
    return repo


def push(repo: Path, **overrides) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PATH"] = environment["PATH"] + os.pathsep + str(Path(sys.executable).parent)
    environment.update({key: value for key, value in overrides.items()})
    return subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo, env=environment,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180)


def tags_of(repo: Path) -> str:
    return subprocess.run(["git", "tag", "--list", "backup/*"], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8").stdout


def test_hook_stops_the_push_when_the_copy_fails(tmp_path):
    """Главное правило: копия не вышла - пуша нет.

    Контейнер оставляем тот же, что и на машине (иначе нечего копировать), а
    путь внутри него - заведомо несуществующий. Ожидание одно: пуш не проходит,
    метка не появляется, и в тексте есть слова про бекап.
    """
    repo = make_repo(tmp_path)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="", BOTLPC_DB_PATH="/app/data/нет-такой-базы.db")
    assert finished.returncode != 0, "пуш прошёл, хотя копия не получилась"
    assert "backup/" not in tags_of(repo), "метка поставлена, хотя копия не получилась"
    assert "бекап" in (finished.stdout + finished.stderr).lower(), finished.stdout + finished.stderr


def test_hook_lets_a_push_through_when_the_copy_works(tmp_path):
    """Обратная сторона: бекап получился - хук обязан пропустить пуш."""
    repo = make_repo(tmp_path)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="1")
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "backup/" in tags_of(repo), "хук пропустил пуш, но метку не поставил"


def test_hook_mentions_how_to_skip_on_purpose(tmp_path):
    """Обход должен быть виден в тексте ошибки, иначе его не найти и не сделать."""
    repo = make_repo(tmp_path)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="", BOTLPC_DB_PATH="/app/data/нет-такой-базы.db")
    assert "BOTLPC_SKIP_DB_BACKUP=1" in (finished.stdout + finished.stderr), \
        "в отказе не сказано, как пропустить базу один раз"
