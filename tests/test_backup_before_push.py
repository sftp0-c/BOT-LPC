"""Бекап перед пушем: метка, согласованный снимок и остановка пуша.

Зачем эти тесты. Правило «не пушить без бекапа» работает только если бекап
делается всегда. Значит, проверять надо решения, а не наличие файла:

* метка уникальна - два бекапа в одну секунду не должны переписать первую;
* старые копии убираются, свежие остаются - иначе диск забьётся;
* SQLite-снимок сохраняет запись, которая ещё находится только в WAL;
* временные файлы удаляются и после успеха, и после ошибки;
* ошибка снимка, переноса или integrity_check останавливает пуш.

Блокировка проверяется настоящим git-пушем в пустой репозиторий с настоящим
хуком. Только Docker в дочернем Python подменён через временный sitecustomize:
контейнер считается работающим, а создание снимка принудительно падает. Поэтому
проверка не зависит от Docker на машине и однозначно доказывает блокировку.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import backup_before_push as backup

HOOK_SOURCE = Path(r"C:\BOT-LPC\.git\hooks\pre-push")
STAMP = "2026-01-01-000000"


# ── метка ──────────────────────────────────────────────────────────────────
def test_stamp_is_sortable_and_digits_only():
    """Имя метки обязано сортироваться по времени, иначе «последняя» не значит свежая."""
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
    """Даже при «оставить 0» последний бекап остаётся единственной точкой отката."""
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


# ── согласованный SQLite-снимок ────────────────────────────────────────────
def run_snapshot(source: Path, snapshot: Path, local_copy: Path):
    with local_copy.open("wb") as output:
        return subprocess.run(
            [sys.executable, "-c", backup.SQLITE_SNAPSHOT_SCRIPT,
             str(source), str(snapshot)],
            stdout=output, stderr=subprocess.PIPE,
        )


def snapshot_leftovers(snapshot: Path) -> list[Path]:
    return [Path(str(snapshot) + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")
            if Path(str(snapshot) + suffix).exists()]


def test_snapshot_keeps_a_record_that_is_only_in_wal(tmp_path):
    """Обычная копия database.db теряет строку из WAL, backup API - сохраняет."""
    live = tmp_path / "live.db"
    snapshot = tmp_path / ".container-snapshot.tmp"
    local_copy = tmp_path / "copied.db"
    main_file_only = tmp_path / "main-file-only.db"

    writer = sqlite3.connect(live)
    try:
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        writer.execute("INSERT INTO proof(value) VALUES('запись из WAL')")
        writer.commit()
        assert Path(str(live) + "-wal").stat().st_size > 0

        # Контрольная мутация: небезопасное копирование только database.db строку
        # не видит. Значит, зелёная проверка ниже действительно зависит от WAL.
        shutil.copyfile(live, main_file_only)
        with sqlite3.connect(main_file_only) as detached:
            assert detached.execute("SELECT COUNT(*) FROM proof").fetchone() == (0,)

        finished = run_snapshot(live, snapshot, local_copy)
        assert finished.returncode == 0, finished.stderr.decode("utf-8", errors="replace")
        with sqlite3.connect(local_copy) as copied:
            assert copied.execute("SELECT value FROM proof").fetchone() == ("запись из WAL",)
            assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert snapshot_leftovers(snapshot) == []
    finally:
        writer.close()


def test_snapshot_removes_container_files_after_an_error(tmp_path):
    """Даже ошибка backup API не оставляет снимок и служебные файлы SQLite."""
    broken = tmp_path / "broken.db"
    broken.write_bytes("это не SQLite".encode("utf-8") * 100)
    snapshot = tmp_path / ".container-snapshot.tmp"
    local_copy = tmp_path / "copied.db.part"
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(snapshot) + suffix).write_bytes("старый остаток".encode("utf-8"))

    finished = run_snapshot(broken, snapshot, local_copy)
    assert finished.returncode != 0
    assert snapshot_leftovers(snapshot) == []


# ── перенос и локальная проверка ───────────────────────────────────────────
def sqlite_payload(path: Path) -> bytes:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof(value) VALUES('сохранено')")
        connection.commit()
    return path.read_bytes()


def fake_running_docker(monkeypatch, payload: bytes = b"", *, returncode: int = 0,
                        stderr: bytes = b"") -> list[list[str]]:
    calls = []
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "docker")

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")
        assert command[1] == "exec"
        kwargs["stdout"].write(payload)
        return subprocess.CompletedProcess(command, returncode, stdout=None, stderr=stderr)

    monkeypatch.setattr(backup.subprocess, "run", run)
    return calls


def test_no_copy_without_docker(monkeypatch):
    """Нет docker - копить неоткуда, и это не повод останавливать пуш."""
    monkeypatch.setattr(backup.shutil, "which", lambda _name: None)
    assert backup.copy_database(STAMP) is None


def test_no_copy_when_the_container_is_not_there(monkeypatch):
    """Несуществующий контейнер - тоже «копить неоткуда», а не ошибка."""
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="нет контейнера"
        ),
    )
    assert backup.copy_database(STAMP) is None


def test_copy_database_checks_and_atomically_publishes_snapshot(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    calls = fake_running_docker(monkeypatch, sqlite_payload(tmp_path / "payload.db"))

    result = backup.copy_database(STAMP)

    assert result == backup_dir / f"database-{STAMP}.db"
    assert result.exists()
    assert not list(backup_dir.glob("*.part"))
    with sqlite3.connect(result) as copied:
        assert copied.execute("SELECT value FROM proof").fetchone() == ("сохранено",)
    execute = calls[-1]
    assert execute[1:4] == ["exec", backup.container(), "python"]
    assert "source.backup(snapshot" in execute[5]
    assert execute[6] == backup.db_in_container()
    assert execute[7].startswith("/app/data/.database.db.pre-push-")


def test_snapshot_failure_is_runtime_error_and_removes_local_part(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    fake_running_docker(monkeypatch, "неполная копия".encode("utf-8"), returncode=1,
                        stderr=b"snapshot failed")

    with pytest.raises(RuntimeError, match="согласованный снимок"):
        backup.copy_database(STAMP)

    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("database-*.db"))


def test_copy_transport_failure_is_runtime_error_and_removes_part(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "docker")

    def run(command, **_kwargs):
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")
        raise OSError("оборвался поток docker exec")

    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="базу сохранить не удалось"):
        backup.copy_database(STAMP)

    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("database-*.db"))


def test_integrity_failure_is_runtime_error_and_removes_part(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    fake_running_docker(monkeypatch, "это не база SQLite".encode("utf-8"))

    with pytest.raises(RuntimeError, match="integrity_check"):
        backup.copy_database(STAMP)

    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("database-*.db"))


# ── хук: пуск и блокировка ─────────────────────────────────────────────────
def make_repo(tmp_path: Path) -> Path:
    """Репозиторий с нашим скриптом, рабочим хуком и настоящим получателем."""
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


def failing_docker_shim(repo: Path) -> Path:
    """Подменяет Docker только в Python, который запускает проверяемый хук."""
    folder = repo / "python-shim"
    folder.mkdir()
    code = textwrap.dedent(r"""
        import shutil
        import subprocess

        _original_which = shutil.which
        _original_run = subprocess.run


        def which(name, *args, **kwargs):
            if name == "docker":
                return "docker"
            return _original_which(name, *args, **kwargs)


        def run(command, *args, **kwargs):
            if (isinstance(command, (list, tuple)) and len(command) > 1
                    and command[0] == "docker"):
                if command[1] == "inspect":
                    return subprocess.CompletedProcess(
                        command, 0, stdout="true\n", stderr=""
                    )
                if command[1] == "exec":
                    output = kwargs.get("stdout")
                    if output is not None:
                        output.write(b"partial snapshot")
                    return subprocess.CompletedProcess(
                        command, 1, stdout=None, stderr=b"snapshot failed"
                    )
            return _original_run(command, *args, **kwargs)


        shutil.which = which
        subprocess.run = run
    """).lstrip()
    (folder / "sitecustomize.py").write_text(code, encoding="utf-8")
    return folder


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


def test_hook_stops_the_push_when_the_snapshot_fails(tmp_path):
    """Главное правило: контейнер есть, но снимок не вышел - пуша нет."""
    repo = make_repo(tmp_path)
    shim = failing_docker_shim(repo)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="", PYTHONPATH=str(shim))
    assert finished.returncode != 0, "пуш прошёл, хотя снимок не получился"
    assert "backup/" not in tags_of(repo), "метка поставлена, хотя снимок не получился"
    assert "бекап" in (finished.stdout + finished.stderr).lower(), finished.stdout + finished.stderr


def test_hook_lets_a_push_through_when_the_copy_is_skipped_on_purpose(tmp_path):
    """Явный разовый обход оставляет прежнюю семантику и ставит метку."""
    repo = make_repo(tmp_path)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="1")
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "backup/" in tags_of(repo), "хук пропустил пуш, но метку не поставил"


def test_hook_mentions_how_to_skip_on_purpose(tmp_path):
    """В отказе должно быть сказано, как явно пропустить базу один раз."""
    repo = make_repo(tmp_path)
    shim = failing_docker_shim(repo)
    finished = push(repo, BOTLPC_SKIP_DB_BACKUP="", PYTHONPATH=str(shim))
    assert "BOTLPC_SKIP_DB_BACKUP=1" in (finished.stdout + finished.stderr), \
        "в отказе не сказано, как пропустить базу один раз"
