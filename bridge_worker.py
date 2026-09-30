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
import re
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
    data.setdefault("panel_url", "http://127.0.0.1:8090")
    data.setdefault("opencode_server", "http://127.0.0.1:49374")
    data.setdefault("opencode_password", "")
    data.setdefault("session", "")
    data.setdefault("working_dir", str(HERE))          # HERE и есть корень проекта
    data.setdefault("poll_seconds", DEFAULT_POLL_SECONDS)
    data.setdefault("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return data


def save_settings(data: dict) -> Path:
    """Записывает настройки. Папку состояния создаёт, если её нет.

    Создание папки здесь обязательно: на свежей машине её нет, и первый запуск
    настройки падал бы с FileNotFoundError. У журнала папка создаётся так же.
    """
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
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


# Постоянных инструкций в задании нет намеренно. Они лежат в AGENTS.md в корне
# проекта, и opencode читает его сам - проверено на живом запуске.
#
# Почему не вставлять в текст задания: инструкции и письмо владельца оказываются
# одним сообщением, и модель отвечает на то, что видит последним. На практике это
# выглядело так: на вопрос «что сделал» приходило «твоё сообщение обрывается на
# слове “пишет”» (это было из инструкций), а после добавления тегов - «я готов
# принимать инструкции». Менялись формулировки, суть не менялась: письмо должно
# оставаться письмом, а инструкциям - лежать в файле.
#
# Проверка, что этого не сломают снова: tests/test_bridge_prompt.py требует, чтобы
# задание уходило как есть, без обёрток.


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


    environment = dict(os.environ)
    if settings.get("opencode_password"):
        environment["OPENCODE_PASSWORD"] = settings["opencode_password"]

    say(f"запускаю работу: {task[:120]}")
    try:
        finished = subprocess.run(
            command + [task],
            cwd=settings["working_dir"], env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=int(settings["timeout_seconds"]))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"работа не уложилась в {settings['timeout_seconds']} секунд") from None
    except FileNotFoundError:
        raise RuntimeError("opencode не найден в PATH - программа не установлена") from None

    remember_session(settings, finished.stdout)
    answer = _answer_from(finished.stdout)
    if not answer:
        tail = (finished.stderr or finished.stdout or "").strip()[-300:]
        raise RuntimeError(
            "opencode отработал, но разобрать ответ не вышло. "
            f"Код возврата: {finished.returncode}. "
            f"Встреченные события: {seen_event_types()}. "
            f"Конец вывода: {tail}"
        )
    return answer


_LAST_EVENT_TYPES: set = set()


def _answer_from(output: str) -> str:
    """Достаёт мой ответ из вывода opencode.

    `opencode run --format json` печатает поток событий, по одному объекту в
    строке, а не один объект с ответом:
        {"type":"step_start", ...}
        {"type":"text", ..., "part":{..., "text":"Да."}}
    Поэтому ответ собирается из событий типа text по порядку: при потоковой
    выдаче их несколько, и это части одного ответа.

    Отдельно поддержан объект с ключом "response" - если формат вернётся или
    попадётся другой режим, ответ не потеряется.

    Пустой результат означает не «модель промолчала», а «формат вывода не тот»,
    и поэтому в сообщении об ошибке перечисляются встреченные типы событий:
    иначе следующая смена формата будет выглядеть как поломка моста, и искать
    придётся вслепую.
    """
    # Части группируются по messageID: куски одной реплики имеют общий
    # messageID, разные реплики - разные. Без группировки ответ удваивался:
    # модель писала сначала «давай осмотрюсь», потом сам ответ, а склейка
    # выдавала оба подряд. Берём последнюю группу - это и есть ответ.
    группы: dict = {}
    порядок: list = []
    без_идентификатора: list = []
    plain = ""
    seen_types: set = set()

    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("type") or ""):
            seen_types.add(str(data["type"]))
        if str(data.get("response") or "").strip():
            plain = str(data["response"]).strip()
        if str(data.get("type") or "") == "text":
            part = data.get("part")
            if isinstance(part, dict) and str(part.get("text") or ""):
                кусок = str(part["text"])
                идентификатор = str(part.get("messageID") or part.get("message_id") or "")
                if not идентификатор:
                    без_идентификатора.append(кусок)
                    continue
                if идентификатор not in группы:
                    группы[идентификатор] = []
                    порядок.append(идентификатор)
                группы[идентификатор].append(кусок)
    if порядок:
        # последняя реплика целиком: это ответ, а не черновик перед ним
        return "".join(группы[порядок[-1]]).strip()
    if без_идентификатора:
        # messageID не пришёл - ведём себя как раньше, склеиваем всё
        return "".join(без_идентификатора).strip()
    if plain:
        return plain
    # global обязателен: без него присваивание сделало бы имя локальным, и
    # подсказка в сообщении об ошибке всегда была бы «ни одного».
    # Обновляем ВСЕГДА, в том числе пустым множеством: иначе после разбора без
    # событий подсказка продолжала бы показывать события прошлого запуска, и
    # человек решил бы, что формат не изменился.
    global _LAST_EVENT_TYPES
    _LAST_EVENT_TYPES = seen_types
    return ""


def seen_event_types() -> str:
    """Типы событий в последнем разобранном выводе - для диагностики."""
    return ", ".join(sorted(_LAST_EVENT_TYPES)) or "ни одного"


def session_of(output: str) -> str:
    """Номер сессии из ответа opencode.

    opencode печатает sessionID в каждом событии. Он нужен, чтобы следующий заход
    продолжил тот же разговор: без этого модель каждый раз начинает с нуля и
    отвечает «это первое сообщение, я ничего не делал».

    Выдумывать номер нельзя: opencode принимает только идентификатор, который
    реально существует, и на выдуманный отвечает ошибкой. Поэтому берём
    настоящий, а если событий не оказалось - возвращаем пусто, и тогда просто
    не подставляем номер, а не выдумываем его.
    """
    номера = re.findall(r'"sessionID"\s*:\s*"([^"]+)"', output or "")
    for номер in номера:
        if str(номер).startswith("ses"):
            return str(номер)
    return ""


def remember_session(settings: dict, output: str) -> None:
    """Запоминает номер сессии, чтобы следующий заход её продолжил.

    Записываем только если он изменился: настройки пишутся в файл, а лишние
    записи на каждом задании только создают шум.
    """
    номер = session_of(output)
    if номер and номер != str(settings.get("session") or ""):
        settings["session"] = номер
        try:
            save_settings(settings)
            log_line = f"мост: запомнена сессия {номер}"
            print(log_line, flush=True)
        except OSError as exc:
            print(f"мост: номер сессии запомнить не вышло: {exc}", flush=True)


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
            call(settings, f"/bridge/{number}/fail", {"error": str(exc)[:400]})
        except Exception as inner:                        # noqa: BLE001
            say(f"  и сообщить об ошибке не вышло: {inner}")
        return
    finally:
        beat.stop()

    try:
        call(settings, f"/bridge/{number}/answer", {"answer": answer})
        say(f"✓ задание {number} выполнено, ответ отправлен ({len(answer)} знаков)")
    except Exception as exc:                              # noqa: BLE001
        # Задание сделано, но сообщить не вышло. Молчать нельзя: владелец ждёт.
        say(f"⚠ задание {number} выполнено, но ответ не доставлен: {exc}")
        try:
            call(settings, f"/bridge/{number}/fail", {"error": "сделано, но ответ не доставился"})
        except Exception:
            pass


class SingleInstance:
    """Метка «программа уже работает» - отдельная от замка.

    Замок занят работой, а не присутствием: между кругами он свободен, и второй
    экземпляр на нём не поймать. Поэтому метка своя и живёт всё время работы
    программы.

    Проверяется так же, как и в замке: записан номер процесса, и он должен быть
    живым. Иначе после аварийного завершения программа не запустилась бы никогда.
    """

    def __init__(self, path=None):
        self.path = Path(path or (STATE_DIR / "bridge_worker.pid"))

    def holder(self) -> str:
        """Номер процесса, который уже работает. Пусто - если никто не работает."""
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return ""
        if pid <= 0 or pid == os.getpid():
            return ""
        return str(pid) if bridge_lock.pid_alive(pid) else ""

    def take(self) -> bool:
        """Занять метку. False - уже работает живой экземпляр."""
        if self.holder():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release(self) -> None:
        """Отпустить метку. Только свою.

        Файл читаем напрямую, а не через holder(): holder() по замыслу не
        считает занятым случай, когда в файле наш собственный номер (иначе
        программа, взявшая метку, считала бы себя вторым экземпляром). Значит
        через него условие «моя ли метка» никогда не выполнялось бы, и запись
        оставалась бы после выхода.

        Вопросы разные: holder() - «работает ли ДРУГОЙ экземпляр»,
        release() - «моя ли это метка». Одно правило на оба вопроса не годится.
        """
        try:
            записан = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if записан == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass


def one_round(settings: dict, lock) -> bool:
    """Один круг. True - было задание, False - нечего было делать."""
    if not lock.acquire("ожидание задания"):
        return False
    try:
        task = call(settings, "/bridge/next")
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
        print(f"в очереди: {call(settings, '/bridge/status').get('waiting', '?')}")
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

    # Сначала планировщик: он переживает закрытые сессии и даёт журнал.
    # На некоторых машинах запись в него закрыта (ошибка 0x80070005), и тогда
    # задача не создаётся вовсе - повышать права ради этого нельзя.
    task = "BOTLPC-мост"
    script = Path(__file__).resolve()
    # pythonw, а не python: без окна консоли. Журнал всё равно пишется в файл.
    quiet = Path(sys.executable).with_name("pythonw.exe")
    if not quiet.exists():
        quiet = Path(sys.executable)
    command = f'cmd /c ""{quiet}" "{script}""'

    try:
        finished = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                                   "/TN", task, "/TR", command],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
        if finished.returncode == 0:
            print(f"автозапуск: задача в планировщике {task} (при входе в Windows)")
            return 0
        reason = (finished.stderr or finished.stdout or "").strip()
        print("планировщик не даёт создать задачу:", reason[:200] if reason else "без причины")
    except FileNotFoundError:
        print("schtasks не найден")

    # Запасная дверь: ярлык в папке автозагрузки. Папка пользовательская,
    # права администратора не нужны. Срабатывает при входе в Windows - так же,
    # как задача по событию входа, только без переживания закрытых сессий.
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / \
        "Programs" / "Startup"
    if not startup.exists():
        print("не нашёл папку автозагрузки:", startup)
        print("запускай вручную: python bridge_worker.py")
        return 1
    launcher = startup / "Мост - ответы в MAX.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "rem Мост: ждёт задания от владельца и отвечает ему в MAX.\r\n"
        "rem Создано программой bridge_worker.py. Удалить: этот файл.\r\n"
        f'start "" /min "{quiet}" "{script}"\r\n',
        encoding="cp866", errors="replace")
    print("автозапуск: файл в папке автозагрузки ->", launcher)
    print("  (вход в Windows запустит мост; пока не войдёшь - запусти вручную)")
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
    instance = SingleInstance()
    сосед = instance.holder()
    if сосед:
        say(f"✗ программа уже работает (процесс {сосед}). Второй экземпляр не нужен: "
            f"очередь разберут два, а в журнале будет путаница.")
        return 1
    if not instance.take():
        say("✗ не удалось занять метку работы - запускаться не буду")
        return 1
    say("мост запущен")
    if args.once:
        try:
            did = one_round(settings, lock)
            say("круг окончен, задание было" if did else "заданий нет")
        finally:
            instance.release()
        return 0

    poll = max(5, int(settings["poll_seconds"]))
    try:
        while True:
            try:
                one_round(settings, lock)
            except Exception as exc:                      # noqa: BLE001 - цикл не должен умирать
                say(f"⚠ круг не получился: {exc}")
            time.sleep(poll)
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
