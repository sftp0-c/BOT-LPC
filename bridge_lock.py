"""Замок: я и консоль не должны работать с проектом одновременно.

Зачем. Мост берёт задание из MAX и идёт править проект. В это же время
человек может сидеть в консоли и править те же файлы. Две правки одного
файла — это не «две правки», это испорченный файл. Поэтому кто начал, тот и
держит; вторая сторона ждёт и говорит об этом, а не лезет вслед.

Почему не обычный файл-«занято». Потому что держатель может упасть. Если
программу убили или компьютер выключили, простой файл остался бы занятым
навсегда, и мост молчал бы до перезагрузки. Поэтому у замка есть срок жизни:
если от держателя давно нет «сердцебиения», замок считается протухшим и его
можно взять.

Кто такой «я». Держателей ровно два: `console` - ты в терминале, `bridge` -
программа моста. Имена нужны не для красоты: в сообщении владельцу должно быть
видно, кто именно сейчас держит проект.

Проверка «жив ли процесс». Замок протухает по сроку жизни, но если держатель
явно мёртв, ждать срока незачем: упавшая программа освобождает проект сразу.
На Windows это делается через GetExitCodeProcess, и обязательно с объявленными
типами - иначе 64-битный дескриптор уходит как 32-битный, и проверка отвечает
«мёртв» на любом номере.

Где живёт файл. Рядом с программой, не в проекте: `data/` и `logs/` в
`.gitignore`, и туда же кладутся бекапы. Замок - не часть кода колледжа и
в контейнер не попадает (в `Dockerfile` файлы перечисляются поимённо).

Почему файл, а не запись в базе. Бот в контейнере, программа моста - на
компьютере, и общей у них только база, а она в том же контейнере. Замок
должен быть виден обеим сторонам на одной машине - значит, в файле. Заодно
он переживает перезапуск контейнера: база контейнера может переехать, файл
на диске останется.
"""
import json
import os
import time
from pathlib import Path

import clock

# Сколько ждать «сердцебиения», прежде чем считать держателя пропавшим.
# Запас большой: задача бывает долгой, а heartbeat идёт и во время работы
# (мост шлёт его отдельной ниткой). Если heartbeat всё же опоздал, значит
# процесс действительно завис, и взять проект у него безопаснее, чем ждать.
TTL_MINUTES = 12

# Что видит владелец вместо «занято».
HOLDER_LABELS = {
    "console": "ты в консоли",
    "bridge": "мост с телефона",
}


def default_path() -> Path:
    """Где лежит замок по умолчанию: папка данных приложения на этой машине."""
    base = os.getenv("BOTLPC_STATE_DIR") or str(Path.home() / "AppData" / "Local" / "botlpc")
    return Path(base) / "bridge.lock"


# ── чистое ядро: решение принимается здесь, без файла ─────────────────────
def is_taken(record: dict, now: float, ttl_minutes: int = TTL_MINUTES,
             pid_alive=None) -> bool:
    """Занят ли замок кем-то живым.

    `pid_alive` - необязательная проверка «жив ли такой-то процесс». Если её
    передали, она ускоряет освобождение: ждать срока жизни процесс, который
    уже мёртв, незачем.
    """
    if not record or not record.get("holder"):
        return False
    beat = record.get("beat") or record.get("since")
    if not beat:
        # запись без времени - считаем протухшей: держатель не подтвердил
        # existence и ждать его мы не вправе
        return False
    try:
        age_minutes = (now - float(beat)) / 60.0
    except (TypeError, ValueError):
        return False
    if age_minutes > float(ttl_minutes):
        return False
    if pid_alive is not None:
        pid = record.get("pid")
        try:
            if pid is not None and not pid_alive(int(pid)):
                return False
        except (TypeError, ValueError):
            return False
    return True


# ── файл ───────────────────────────────────────────────────────────────────
def read(path: Path) -> dict:
    """Читает замок. Любая негодь читается как «свободен»."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Пишем целиком и меняем местами: читатель либо видит прежний замок,
    # либо новый, но никогда - наполовину записанный.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# Права на процесс для чтения состояния. PROCESS_QUERY_LIMITED_INFORMATION
# достаточно и есть на любой современной Windows; SYNCHRONIZE не нужен, потому
# что состояние мы читаем через GetExitCodeProcess, а не через ожидание.
_PROCESS_QUERY_LIMITED = 0x1000
_STILL_ACTIVE = 259


def _win_pid_alive(pid: int) -> bool:
    """Жив ли процесс в Windows.

    Типы объявлены явно: без них 64-битный дескриптор передаётся как 32-битный
    и Windows отвечает ошибкой, которую легко принять за «процесс мёртв».
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
    open_process.restype = ctypes.c_void_p
    exit_code = kernel32.GetExitCodeProcess
    exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    handle = open_process(_PROCESS_QUERY_LIMITED, 0, pid)
    if not handle:
        return False                      # нет права или нет такого процесса
    try:
        code = ctypes.c_uint32(0)
        if not exit_code(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        close_handle(handle)


def _posix_pid_alive(pid: int) -> bool:
    """Жив ли процесс в Linux и macOS: сигнал 0 ничего не делает, только проверяет."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # процесс есть, просто не наш
    except OSError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    """Жив ли процесс на этой машине.

    Любая ошибка означает «не удалось доказать, что жив» - и это False. Обратное
    опаснее: решив, что живой процесс мёртв, мы снимем чужой замок.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            return _win_pid_alive(pid)
        return _posix_pid_alive(pid)
    except Exception:                                    # noqa: BLE001
        return False


class Lock:
    """Замок на файле. Один объект - один дер��атель."""

    def __init__(self, path=None, holder: str = "bridge", ttl_minutes: int = TTL_MINUTES):
        self.path = Path(path or default_path())
        self.holder = str(holder)
        self.ttl_minutes = int(ttl_minutes)

    # ── спросить ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Кто держит. `mine` - держу ли я сам."""
        record = read(self.path)
        taken = is_taken(record, time.time(), self.ttl_minutes, pid_alive)
        if not taken:
            return {"free": True, "holder": "", "mine": False, "task": ""}
        return {"free": False, "holder": record.get("holder", ""),
                "mine": record.get("holder") == self.holder,
                "task": record.get("task", ""),
                "beat": record.get("beat", "")}

    def who(self) -> str:
        """Человеческое имя держателя - для сообщения владельцу."""
        current = self.status()
        if current["free"]:
            return "никто"
        return HOLDER_LABELS.get(current["holder"], current["holder"])

    # ── взять и отпустить ───────────────────────────────────────────────────
    def acquire(self, task: str = "") -> bool:
        """Взять замок. False - занят живым держателем.

        Свой замок берём всегда: так программа переживает свою перезапуск и не
        ждёт сама себя до истечения срока.
        """
        moment = time.time()
        current = read(self.path)
        if is_taken(current, moment, self.ttl_minutes, pid_alive) \
                and current.get("holder") != self.holder:
            return False
        write(self.path, {"holder": self.holder, "pid": os.getpid(), "task": str(task)[:200],
                          "since": moment, "beat": moment})
        return True

    def heartbeat(self) -> bool:
        """Продлить замок. False - он уже не наш (забрали или протух)."""
        current = read(self.path)
        if current.get("holder") != self.holder:
            return False
        current["beat"] = time.time()
        current["pid"] = os.getpid()
        write(self.path, current)
        return True

    def release(self) -> bool:
        """Отпустить. Отпускаем только свой."""
        current = read(self.path)
        if current.get("holder") != self.holder:
            return False
        try:
            Path(self.path).unlink()
        except OSError:
            return False
        return True

    # ── удобство ───────────────────────────────────────────────────────────
    def __enter__(self):
        if not self.acquire():
            raise LockBusy(f"проект держит {self.who()}")
        return self

    def __exit__(self, *_):
        self.release()
        return False


class LockBusy(RuntimeError):
    """Замок занят живым держателем. Не ошибка программы, а ожидание."""


def stamp() -> str:
    """Время проекта (ЕКБ) - для журнала и сообщений."""
    return clock.stamp()
