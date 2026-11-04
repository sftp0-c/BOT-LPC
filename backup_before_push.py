"""Бекап перед пушем: метка в git и согласованная копия живой базы.

Зачем это отдельной программой, а не «не забудь». Правило «не пушить без
бекапа» держится на памяти, а память устаёт. Git сам зовёт эту программу перед
любым пушем, поэтому пропустить её нельзя: забыть не того, кто пушит, а
забыть невозможно.

Что делает.
1. Ставит метку `backup/ГГГГ-ММ-ДД-ЧЧММСС` на тот коммит, который сейчас
   уедет в main. Метка - это точная точка отката кода, и она видна в истории.
2. Делает SQLite-снимок живой базы через backup API внутри контейнера и
   переносит его во временный локальный файл. Поэтому записи из WAL входят в
   копию, даже когда бот продолжает работать.
3. Проверяет копию через `PRAGMA integrity_check`, атомарно переименовывает её
   и оставляет последние N копий. Старые убирает, чтобы не забить диск.

Когда копия базы не получилась. Пуш останавливается, и это правильно: пушить
код, который меняет поведение бота, без копии базы - значит получить неработающего
бота без возможности вернуть обращения и сотрудников. Если бэкап базы не нужен
(например, меняются только тексты), обойти можно один раз:
    BOTLPC_SKIP_DB_BACKUP=1 git push
Обход виден в записи и не должен становиться привычкой.
"""
import os
import posixpath
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUP_DIR = ROOT / "backups"
KEEP = 12
# Имя контейнера и путь к базе читаются при вызове, а не один раз при загрузке.
# Иначе подмена окружения (в том числе проверкой) не действует, и копирование
# идёт не туда. На другой машине имя контейнера может быть иным.
CONTAINER = "bot-lpc"
DB_IN_CONTAINER = "/app/data/database.db"

# Код исполняется штатным Python внутри контейнера. Один процесс создаёт
# согласованный снимок, отдаёт его в stdout и в любом исходе убирает сам файл и
# возможные служебные файлы SQLite. Так между созданием и очисткой нет второго
# docker exec, который мог бы не запуститься после ошибки копирования.
SQLITE_SNAPSHOT_SCRIPT = r"""
import os
import sqlite3
import sys
from pathlib import Path

source_path, snapshot_path = sys.argv[1:3]
source = None
snapshot = None
stored = None


def remove_snapshot_files():
    first_error = None
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(snapshot_path + suffix)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


try:
    remove_snapshot_files()
    source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    snapshot = sqlite3.connect(snapshot_path, timeout=30)
    source.backup(snapshot, pages=256, sleep=0.1)
    snapshot.close()
    snapshot = None
    source.close()
    source = None

    stored = open(snapshot_path, "rb")
    while True:
        chunk = stored.read(1024 * 1024)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
finally:
    cleanup_error = None
    for resource in (stored, snapshot, source):
        if resource is None:
            continue
        try:
            resource.close()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
    try:
        remove_snapshot_files()
    except Exception as exc:
        if cleanup_error is None:
            cleanup_error = exc
    if cleanup_error is not None:
        raise cleanup_error
""".strip()


def container() -> str:
    return os.getenv("BOTLPC_CONTAINER") or CONTAINER


def db_in_container() -> str:
    return os.getenv("BOTLPC_DB_PATH") or DB_IN_CONTAINER


EKATERINBURG = timezone(timedelta(hours=5))          # зона колледжа, см. clock.py


def now_stamp() -> str:
    """Время по Екатеринбургу: метка должна читаться, а не показывать UTC."""
    return datetime.now(EKATERINBURG).strftime("%Y-%m-%d-%H%M%S")


def git(*args: str) -> tuple[int, str]:
    finished = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    return finished.returncode, (finished.stdout or finished.stderr or "").strip()


def make_tag(stamp: str) -> str:
    """Ставит метку на текущий коммит. Имя занимает счётчик, если уже есть."""
    name = f"backup/{stamp}"
    counter = 2
    while git("rev-parse", "-q", "--verify", f"refs/tags/{name}")[0] == 0:
        name = f"backup/{stamp}-{counter}"
        counter += 1
    code, output = git("tag", name)
    if code != 0:
        raise RuntimeError(f"метку поставить не вышло: {output}")
    return name


def _snapshot_path(source: str) -> str:
    """Уникальный временный файл рядом с базой внутри контейнера."""
    folder = posixpath.dirname(source) or "."
    name = posixpath.basename(source) or "database.db"
    token = uuid.uuid4().hex
    return posixpath.join(folder, f".{name}.pre-push-{token}.tmp")


def _command_error(finished: subprocess.CompletedProcess) -> str:
    value = finished.stderr or finished.stdout or b""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()[:200]


def _check_local_copy(path: Path) -> None:
    """Проверяет локальный снимок до того, как он получит имя бекапа."""
    try:
        if path.stat().st_size == 0:
            raise RuntimeError("копия базы пуста")
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except RuntimeError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"копия базы не прошла PRAGMA integrity_check: {exc}") from exc
    if rows != [("ok",)]:
        details = "; ".join(str(row[0]) for row in rows) or "нет результата"
        raise RuntimeError(f"копия базы не прошла PRAGMA integrity_check: {details[:200]}")


def copy_database(stamp: str) -> Path | None:
    """Копирует согласованный снимок базы. None - если копить неоткуда.

    None означает «системы нет», а не «ошибка»: на машине без docker копить
    неоткуда, и это не повод останавливать пуш. Ошибка - когда контейнер есть,
    а снимок, перенос или проверка не вышли: тогда пуш стопорится.
    """
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        probe = subprocess.run([docker, "inspect", "-f", "{{.State.Running}}", container()],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    except OSError:
        return None
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None

    target = BACKUP_DIR / f"database-{stamp}.db"
    # Сначала во временный файл и только потом переименовываем: прерванная
    # передача или повреждённая копия не должны выглядеть свежим бекапом.
    temporary = target.with_suffix(".db.part")
    source = db_in_container()
    snapshot = _snapshot_path(source)
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            finished = subprocess.run(
                [docker, "exec", container(), "python", "-c",
                 SQLITE_SNAPSHOT_SCRIPT, source, snapshot],
                cwd=ROOT, stdout=output, stderr=subprocess.PIPE,
            )
        if finished.returncode != 0:
            details = _command_error(finished)
            raise RuntimeError(
                "согласованный снимок базы получить не удалось"
                + (f": {details}" if details else "")
            )
        _check_local_copy(temporary)
        temporary.replace(target)
    except RuntimeError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"базу сохранить не удалось: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Пуш уже будет остановлен при основной ошибке, а prune уберёт
            # такой остаток при следующем запуске.
            pass
    return target


def prune(keep: int = KEEP) -> int:
    """Оставляет последние N копий. Возвращает, сколько убрано."""
    if not BACKUP_DIR.exists():
        return 0
    copies = sorted(BACKUP_DIR.glob("database-*.db"),
                    key=lambda path: path.stat().st_mtime, reverse=True)
    removed = 0
    for old in copies[max(1, int(keep)):]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    for stray in BACKUP_DIR.glob("*.part"):
        try:
            stray.unlink()                      # остатки прерванной копии
        except OSError:
            pass
    return removed


def main() -> int:
    stamp = now_stamp()
    if os.getenv("BOTLPC_SKIP_DB_BACKUP") == "1":
        print("бекап базы пропущен по просьбе (BOTLPC_SKIP_DB_BACKUP=1)")
    else:
        try:
            copy = copy_database(stamp)
        except RuntimeError as exc:
            print(f"✗ {exc}")
            print("  Пуш остановлен: без копии базы откатить обращения нечем.")
            print("  Если меняются только тексты - повтори с BOTLPC_SKIP_DB_BACKUP=1")
            return 1
        if copy is None:
            print("  контейнер не найден или docker недоступен - копия базы пропущена")
        else:
            print(f"  база: {copy.relative_to(ROOT)} "
                  f"({copy.stat().st_size // 1024} КБ)")

    try:
        name = make_tag(stamp)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    prune()
    print(f"  метка: {name}")
    print(f"  откатить код: git reset --hard {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
