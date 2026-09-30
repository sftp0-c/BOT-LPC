"""Бекап перед пушем: метка в git и копия живой базы.

Зачем это отдельной программой, а не «не забудь». Правило «не пушить без
бекапа» держится на памяти, а память устаёт. Git сам зовёт эту программу перед
любым пушем, поэтому пропустить её нельзя: забыть не того, кто пушит, а
забыть невозможно.

Что делает.
1. Ставит метку `backup/ГГГГ-ММ-ДД-ЧЧММСС` на тот коммит, который сейчас
   уедет в main. Метка - это точная точка отката кода, и она видна в истории.
2. Копирует живую базу бота в backups/. База лежит в томе Docker, поэтому
   копия идёт через docker cp, а не простым копированием файла.
3. Оставляет последние N копий, старые убирает. Бекапы, которые никто не
   удаляет, через полгода занимают диск, и тогда их перестанут делать.

Когда копия базы не получилась. Пуш останавливается, и это правильно: пушить
код, который меняет поведение бота, без копии базы - значит получить неработающего
бота без возможности вернуть обращения и сотрудников. Если бэкап базы не нужен
(например, меняются только тексты), обойти можно один раз:
    BOTLPC_SKIP_DB_BACKUP=1 git push
Обход виден в записи и не должен становиться привычкой.
"""
import os
import shutil
import subprocess
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


def copy_database(stamp: str) -> Path | None:
    """Копирует базу из контейнера. None - если копить неоткуда.

    None означает «системы нет», а не «ошибка»: на машине без docker копить
    неоткуда, и это не повод останавливать пуш. Ошибка - когда контейнер есть,
    а копия не вышла: тогда пуш стопорится.
    """
    if shutil.which("docker") is None:
        return None
    probe = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container()],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"database-{stamp}.db"
    # Сначала во временный файл и только потом переименовываем: прерванная
    # копия не должна остаться под именем «свежего бекапа».
    temporary = target.with_suffix(".db.part")
    finished = subprocess.run(["docker", "cp", f"{container()}:{db_in_container()}", str(temporary)],
                              cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    if finished.returncode != 0:
        if temporary.exists():
            temporary.unlink()
        raise RuntimeError(f"базу скопировать не вылось: "
                           f"{(finished.stderr or finished.stdout or '').strip()[:200]}")
    temporary.replace(target)
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
