"""Мост не должен терять задание из-за того, что процесс прервали.

Что происходило. Задание отработало 3 минуты 26 секунд, потом opencode получил
отмену (код 0xC000013A, в выводе «^C»), и мост написал владельцу «не получилось
сделать». Работа не была сделана зря, но пропала.

Что здесь проверяется.

* Обрыв отличается от отказа. Код отмены и след «^C» без разбираемого ответа -
  это обрыв, и он повторяется. Отказ модели - не обрыв, и он владельцу
  сообщается сразу: повтор тут не помог бы, только зря потратил бы время.
* Повтор ровно один, и с чистой сессией. Прерванный заход мог оставить сессию в
  неопределённом состоянии. Повторять бесконечно нельзя: настоящая поломка
  превратится в круг, который ест время и жжёт модель.
* Ребёнок получает свою группу процессов. Мост работает без окна, и opencode
  наследует группу; отмена, посланная всей группе, попадает и в него.
"""
import sys


import bridge_worker as worker


class Подделка:
    """Замена результата subprocess.run - только нужные поля."""

    def __init__(self, code: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


# ── различение обрыва и отказа ─────────────────────────────────────────────
def test_код_отмены_это_обрыв():
    assert worker.was_interrupted(Подделка(code=0xC000013A)) is True


def test_след_отмены_без_ответа_тоже_обрыв():
    """Код может прийти обычным, но след отмены в выводе виден."""
    результат = Подделка(code=1, stdout="^C")
    assert worker.was_interrupted(результат) is True


def test_пустой_вывод_без_следа_отмены_не_обрыв():
    """opencode не ответил, но его никто не прерывал. Повтор не поможет."""
    assert worker.was_interrupted(Подделка(code=1, stdout="ничего")) is False


def test_отказ_модели_не_обрыв():
    assert worker.was_interrupted(Подделка(code=2, stdout="ошибка")) is False


def test_успешный_ответ_не_обрыв():
    вывод = ('{"type":"text","part":{"messageID":"m1","text":"Готово"}}\n')
    assert worker.was_interrupted(Подделка(code=0, stdout=вывод)) is False


def test_след_отмены_рядом_с_готовым_ответом_не_обрыв():
    """Ответ есть - работа сделана, даже если в хвосте мелькнул ^C."""
    вывод = ('{"type":"text","part":{"messageID":"m1","text":"Готово"}}\n^C\n')
    assert worker.was_interrupted(Подделка(code=0, stdout=вывод)) is False


def test_нулевой_код_без_вывода_не_обрыв():
    assert worker.was_interrupted(Подделка(code=0, stdout="")) is False


# ── группа процессов ───────────────────────────────────────────────────────
def test_группа_процессов_задаётся():
    """На Windows флаг должен возвращаться, иначе отмена достанет opencode."""
    значение = worker._own_process_group()
    if sys.platform == "win32":
        assert значение == getattr(worker.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        assert значение != 0, "флаг не задан, opencode остаётся в нашей группе"
    else:
        assert значение == 0, "вне Windows флаг не нужен и не переносится"


def test_обрыв_это_отдельный_тип():
    """Отдельный тип, а не RuntimeError: из него следует повтор, а не отказ."""
    assert issubclass(worker.Interrupted, RuntimeError)
    assert worker.Interrupted is not RuntimeError


# ── повтор ─────────────────────────────────────────────────────────────────
def test_прерванное_задание_повторяется_один_раз(monkeypatch, tmp_path):
    """Главное: работа не пропадает, но и не крутится бесконечно.

    Первый заход обрывается, второй возвращает ответ. Проверяем, что заходов
    было ровно два, второй шёл с чистой сессией, а владельцу ушёл ответ.
    """
    настройки = {"panel_url": "http://127.0.0.1:8090", "token": "t", "session": "ses_старая",
                 "working_dir": str(tmp_path), "timeout_seconds": 60}
    вызовы: list = []

    def подделка(settings, task):
        вызовы.append(settings.get("session", ""))
        if len(вызовы) == 1:
            raise worker.Interrupted("прервано")
        return "Готово, коммит abc"

    доставлено: list = []

    def call(settings, путь, payload=None, timeout=30):
        if "answer" in путь:
            доставлено.append(payload["answer"])
        return {}

    monkeypatch.setattr(worker, "run_opencode", подделка)
    monkeypatch.setattr(worker, "call", call)
    monkeypatch.setattr(worker, "say", lambda сообщение: None)
    monkeypatch.setattr(worker.Heartbeat, "start", lambda self: None)
    monkeypatch.setattr(worker.Heartbeat, "stop", lambda self: None)

    # Замок не нужен: Heartbeat подменён, и он его не трогает.
    worker.handle_task(настройки, None, {"id": 5, "text": "почини"})

    assert len(вызовы) == 2, f"заходов было {len(вызовы)}, а ждали два"
    assert вызовы[0] == "ses_старая", "первый заход шёл в прежней сессии"
    assert вызовы[1] == "", "повтор должен идти с чистой сессией"
    assert доставлено == ["Готово, коммит abc"], "ответ не доставлен"


def test_два_обрыва_подряд_больше_не_повторяем(monkeypatch, tmp_path):
    """Второй обрыв - это уже не случайность, а поломка. Сообщаем и stop.

    Иначе настоящая поломка превратится в круг, который ест время и жжёт модель.
    """
    настройки = {"panel_url": "http://127.0.0.1:8090", "token": "t", "session": "",
                 "working_dir": str(tmp_path), "timeout_seconds": 60}
    вызовы: list = []
    отказы: list = []

    def подделка(settings, task):
        вызовы.append(task)
        raise worker.Interrupted("прервано")

    def call(settings, путь, payload=None, timeout=30):
        if "fail" in путь:
            отказы.append(payload["error"])
        return {}

    monkeypatch.setattr(worker, "run_opencode", подделка)
    monkeypatch.setattr(worker, "call", call)
    monkeypatch.setattr(worker, "say", lambda сообщение: None)
    monkeypatch.setattr(worker.Heartbeat, "start", lambda self: None)
    monkeypatch.setattr(worker.Heartbeat, "stop", lambda self: None)

    # Замок не нужен: Heartbeat подменён, и он его не трогает.
    worker.handle_task(настройки, None, {"id": 6, "text": "почини"})

    assert len(вызовы) == 2, f"заходов было {len(вызовы)}, а ждали ровно два"
    assert отказы, "владельцу не сообщили, что не получилось"
    assert "прервано" in отказы[0], "в отказе нет причины"


def test_отказ_модели_не_повторяется(monkeypatch, tmp_path):
    """Отказ - не обрыв. Повтор тут только зря потратит время."""
    настройки = {"panel_url": "http://127.0.0.1:8090", "token": "t", "session": "",
                 "working_dir": str(tmp_path), "timeout_seconds": 60}
    вызовы: list = []

    def подделка(settings, task):
        вызовы.append(task)
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(worker, "run_opencode", подделка)
    monkeypatch.setattr(worker, "call", lambda s, p, payload=None, timeout=30: {})
    monkeypatch.setattr(worker, "say", lambda сообщение: None)
    monkeypatch.setattr(worker.Heartbeat, "start", lambda self: None)
    monkeypatch.setattr(worker.Heartbeat, "stop", lambda self: None)

    # Замок не нужен: Heartbeat подменён, и он его не трогает.
    worker.handle_task(настройки, None, {"id": 7, "text": "почини"})

    assert len(вызовы) == 1, f"отказ повторяли {len(вызовы)} раз(а) - так не делаем"
