"""Программа моста: работает на компьютере владельца, отвечает в MAX.

Что она делает. Стоит на компьютере фоном, спрашивает у бота «есть задание?» и,
если есть, запускает работу через opencode. Готовый ответ отправляет обратно в
бот, а бот показывает его владельцу в том же чате MAX. Связь только в одну
сторону не нужна: всё, что ты пишешь боту с командного слова, приходит сюда.

Почему программа отдельно от бота. Бот работает в контейнере и не видит файлов
компьютера. Общее у них одно - адреса служебной части панели. Так и сделано:
программа ходит по локальному адресу с токеном, и больше ей ничего не нужно.

Зачем нужен токен и проверка адреса. Служебные адреса бот отдаёт наружу, порт
слушает все интерфейсы. Поэтому проверяются две вещи: запрос с этой машины И
знание токена. Одной проверки мало - за обратным прокси «локальный» клиент
может оказаться чужим.

Зачем нужен замок. Работа идёт по файлам проекта. Если в это же время человек
сидит в консоли и правит те же файлы, две правки одного файла - это испорченный
файл. Замок берётся до начала работы: кто начал, тот и держит. Вторая сторона
ждёт. Если программа упала, её процесс перестаёт существовать, и проект
освобождается сам, без всякой разблокировки.

Как запускать.

    python bridge_worker.py --once     # одно задание и выход (для проверки)
    python bridge_worker.py            # работать фоном
    python bridge_worker.py --status   # что в очереди, кто держит проект
    python bridge_worker.py --install  # создать настройки и задачание в планировщик

Настройки лежат в bridge_worker.json рядом с программой, токен и пароль - там
же. Файл НЕ в проекте и не в git: пароль от службы opencode туда попадать не
должен.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bridge_lock                                            # noqa: E402 - нужен проект рядом

# Настройки и журнал лежат НЕ в папке проекта: там пароль от службы opencode
# и токен моста, и папка проекта попадает в сборку образа и в архивы. Своё
# место - папка состояния приложения на этой машине (та же, что у замка).
STATE_DIR = bridge_lock.default_path().parent
SETTINGS = STATE_DIR / "bridge_worker.json"
JOURNAL = STATE_DIR / "bridge_worker.log"
TOKEN_LENGTH = 32
DEFAULT_POLL_SECONDS = 20
DEFAULT_TIMEOUT_SECONDS = 5400


# ── журнал ─────────────────────────────────────────────────────────────────
def say(message: str) -> None:
    """Пишет в журнал и на экран. Журнал растёт понемногу, иначе забивает диск."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if JOURNAL.stat().st_size > 2_000_000:      # два мегабайта - уже многовато
            JOURNAL.replace(JOURNAL.with_suffix(".old.log"))
    except OSError:
        pass


# ── настройки ──────────────────────────────────────────────────────────────
def load_settings() -> dict:
    """Читает настройки. Отсутствующие ключи получают разумные значения."""
    data = {}
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
        except ValueError:
            say(f"⚠ {SETTINGS.name} не читается как json - беру значения по умолчанию")
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("panel_url", "http://127.0.0.1:8080")
    data.setdefault("opencode_server", "http://127.0.0.1:49374")
    data.setdefault("opencode_password", "")
    data.setdefault("session", "")
    data.setdefault("working_dir", str(HERE))          # HERE и есть корень проекта
    data.setdefault("poll_seconds", DEFAULT_POLL_SECONDS)
    data.setdefault("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return data


def save_settings(data: dict) -> Path:
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return SETTINGS


def read_bot_token() -> str:
    """Токен моста берём из .env бота: он один и там, и здесь."""
    env_file = Path(load_settings()["working_dir"]) / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("BRIDGE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ── разговор с ботом ───────────────────────────────────────────────────────
def call(settings: dict, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    """Один запрос к служебному адресу бота.

    Сетевые ошибки здесь - не «программа сломалась», а «бот сейчас недоступен».
    Поэтому исключение поднимается наружу с понятным текстом, а вызывающий код
    решает: ждать и повторить или отказать задание.
    """
    url = settings["panel_url"].rstrip("/") + path
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    request.add_header("X-Bridge-Token", settings.get("token") or read_bot_token())
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"бот ответил {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"бот недоступен: {exc}") from exc
    try:
        return json.loads(raw) if raw else {}
    except ValueError as exc:
        raise RuntimeError(f"бот ответил не json: {raw[:200]}") from exc


# ── работа через opencode ──────────────────────────────────────────────────
def run_opencode(settings: dict, task: str) -> str:
    """Передаёт задание opencode и возвращает мой ответ.

    PowerShell в Windows мешает: он не запускает opencode.ps1 и портит вывод.
    Поэтому вызываем через cmd /c, иначе на этой машине ничего не запустится.
    """
    command = ["cmd", "/c", "opencode", "run",
               "--server", settings["opencode_server"], "--auto",
               "--format", "json"]
    if settings.get("session"):
        command += ["--session", settings["session"]]
    command += [task]

    environment = dict(os.environ)
    if settings.get("opencode_password"):
        environment["OPENCODE_PASSWORD"] = settings["opencode_password"]

    say(f"запускаю работу: {task[:120]}")
    try:
        finished = subprocess.run(
            command, cwd=settings["working_dir"], env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=int(settings["timeout_seconds"]))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"работа не уложилась в {settings['timeout_seconds']} секунд") from None
    except FileNotFoundError:
        raise RuntimeError("opencode не найден в PATH - программа не установлена") from None

    answer = _answer_from(finished.stdout)
    if finished.returncode != 0 and not answer:
        tail = (finished.stderr or finished.stdout or "").strip()[-400:]
        raise RuntimeError(f"opencode закончил с кодом {finished.returncode}: {tail}")
    if not answer:
        raise RuntimeError("opencode отработал, но ответ пустой")
    return answer


def _answer_from(output: str) -> str:
    """Достаёт мой ответ из вывода opencode.

    Формат json печатает служебные строки и в конце объект с ответом. Берём
    последний разобранный - он и есть итог, а не промежуточная реплика.
    """
    answer = ""
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and str(data.get("response") or "").strip():
            answer = str(data["response"]).strip()
    return answer


# ── сердцебиение ───────────────────────────────────────────────────────────
class Heartbeat(threading.Thread):
    """Продлевает замок, пока идёт работа.

    Без этого долгая задача выглядела бы упавшей: срок жизни истёк бы, и
    проект можно было бы взять, не дожидаясь конца. Нитка молча продлевает
    замок и тихо умирает вместе с программой - тогда протухание всё равно
    сработает.
    """

    def __init__(self, lock, seconds: int = 60):
        super().__init__(daemon=True)
        self.lock = lock
        self.seconds = max(10, int(seconds))
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.seconds):
            try:
                if not self.lock.heartbeat():
                    say("⚠ замок перестал быть нашим - возможно, проект забрали")
                    return
            except Exception as exc:                      # noqa: BLE001 - нитка не должна падать
                say(f"⚠ не удалось продлить замок: {exc}")

    def stop(self) -> None:
        self.stop_event.set()


# ── один круг работы ───────────────────────────────────────────────────────
def handle_task(settings: dict, lock, task: dict) -> None:
    """Выполняет одно задание и отправляет результат владельцу."""
    number, text = task.get("id"), str(task.get("text") or "")
    beat = Heartbeat(lock, seconds=max(30, bridge_lock.TTL_MINUTES * 60 // 3))
    beat.start()
    try:
        answer = run_opencode(settings, text)
    except Exception as exc:                              # noqa: BLE001 - сюда приходит всё
        say(f"✗ задание {number} не вышло: {exc}")
        try:
            call(settings, f"/panel/bridge/{number}/fail", {"error": str(exc)[:400]})
        except Exception as inner:                        # noqa: BLE001
            say(f"  и сообщить об ошибке не вышло: {inner}")
        return
    finally:
        beat.stop()

    try:
        call(settings, f"/panel/bridge/{number}/answer", {"answer": answer})
        say(f"✓ задание {number} выполнено, ответ отправлен ({len(answer)} знаков)")
    except Exception as exc:                              # noqa: BLE001
        # Задание сделано, но сообщить не вышло. Молчать нельзя: владелец ждёт.
        say(f"⚠ задание {number} выполнено, но ответ не доставлен: {exc}")
        try:
            call(settings, f"/panel/bridge/{number}/fail", {"error": "сделано, но ответ не доставился"})
        except Exception:
            pass


def one_round(settings: dict, lock) -> bool:
    """Один круг. True - было задание, False - нечего было делать."""
    if not lock.acquire("ожидание задания"):
        return False
    try:
        task = call(settings, "/panel/bridge/next")
        if not task or task.get("empty"):
            return False
        if not lock.acquire(str(task.get("text") or "")[:200]):
            return False
        handle_task(settings, lock, task)
        return True
    finally:
        lock.release()


# ── команды ────────────────────────────────────────────────────────────────
def cmd_status(settings: dict) -> int:
    lock = bridge_lock.Lock(holder="bridge")
    print(f"настройки: {SETTINGS}")
    print(f"токен задан: {'да' if (settings.get('token') or read_bot_token()) else 'НЕТ'}")
    print(f"проект держит: {lock.who()}")
    try:
        print(f"в очереди: {call(settings, '/panel/bridge/status').get('waiting', '?')}")
    except Exception as exc:                              # noqa: BLE001
        print(f"бот недоступен: {exc}")
    return 0


def cmd_install() -> int:
    """Создаёт настройки и задачу в планировщике Windows."""
    data = load_settings()
    data["token"] = read_bot_token() or data.get("token", "")
    if not data["token"]:
        print("Токен не найден. Открой .env бота и добавь строки:")
        print("    BRIDGE_ENABLED=1")
        print(f"    BRIDGE_TOKEN={bridge_lock.clock.stamp().replace(' ', '').replace(':', '')}")
        print("(впиши любую длинную строку) и запусти мост заново.")
    path = save_settings(data)
    print(f"настройки: {path}")
    print(f"токен: {'задан' if data['token'] else 'НЕ ЗАДАН - мост работать не будет'}")
    print(f"opencode: {data['opencode_server']} (пароль: "
          f"{'задан' if data['opencode_password'] else 'НЕ ЗАДАН'})")

    task = "BOTLPC-мост"
    command = f'cmd /c ""{sys.executable}" "{Path(__file__).resolve()}""'
    try:
        finished = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                                   "/TN", task, "/TR", command],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
    except FileNotFoundError:
        print("schtasks не найден. Запускай вручную: python bridge_worker.py")
        return 0
    if finished.returncode == 0:
        print(f"задача в планировщике создана: {task} (при входе в Windows)")
    else:
        print("задачу в планировщике создать не вышло:")
        print(" ", (finished.stderr or finished.stdout or "").strip()[:300])
        print("запускай вручную: python bridge_worker.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Мост: ответы ассистента в MAX")
    parser.add_argument("--once", action="store_true", help="одно задание и выход")
    parser.add_argument("--status", action="store_true", help="показать состояние и выйти")
    parser.add_argument("--install", action="store_true", help="создать настройки и задачу в планировщике")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.status:
        return cmd_status(settings)
    if args.install:
        return cmd_install()

    if not (settings.get("token") or read_bot_token()):
        say("токен не задан: добавь BRIDGE_TOKEN в .env бота или в bridge_worker.json")
        return 1

    lock = bridge_lock.Lock(holder="bridge")
    say("мост запущен")
    if args.once:
        did = one_round(settings, lock)
        say("круг окончен, задание было" if did else "заданий нет")
        return 0

    poll = max(5, int(settings["poll_seconds"]))
    while True:
        try:
            one_round(settings, lock)
        except Exception as exc:                          # noqa: BLE001 - цикл не должен умирать
            say(f"⚠ круг не получился: {exc}")
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
