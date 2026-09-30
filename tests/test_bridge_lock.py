"""Замок между консолью и мостом: кто держит проект и когда замок протухает.

Что здесь проверяется по-настоящему. Замок защищает от одной беды: две
правки одного файла одновременно. Проверять надо не «файл создан», а три
решения, на которых держится смысл замка:

* чужой живой держатель - не взять;
* свой держатель - взять (программа перезапустилась, ждать сама себя нельзя);
* протухший или умерший - взять, даже если запись лежит.

Отдельно проверяется, что испорченный файл читается как свободный, а не
как ошибка. Иначе мост после сбоя питания не смог бы начать работу вообще.

Правило, которое проверка должна ловить: срок жизни - не украшение. Если его
отключить (ttl = бесконечность), протухший держатель блокирует проект навсегда,
и `test_expired_lock_can_be_taken` обязан покраснеть.
"""
import os
import subprocess
import sys
import time

import pytest

import bridge_lock
from bridge_lock import Lock, LockBusy, is_taken

pytestmark = pytest.mark.panel

NOW = 1_700_000_000.0
TTL = 10


def alive(_pid: int) -> bool:
    return True


def dead(_pid: int) -> bool:
    return False


# ── чистое решение: занят ли замок ─────────────────────────────────────────
def test_no_record_means_free():
    assert is_taken({}, NOW, TTL, alive) is False
    assert is_taken({"holder": ""}, NOW, TTL, alive) is False


def test_fresh_holder_holds_it():
    record = {"holder": "console", "beat": NOW - 60}
    assert is_taken(record, NOW, TTL, alive) is True


def test_holder_at_the_edge_still_holds():
    """Ровно на границе срока жизни замок ещё держится.

    Строгое «больше» вместо «больше или равно» здесь означало бы, что замок
    протухает на минуту раньше срока. Проверяем обе стороны от границы.
    """
    assert is_taken({"holder": "console", "beat": NOW - TTL * 60}, NOW, TTL, alive) is True
    assert is_taken({"holder": "console", "beat": NOW - TTL * 60 - 30}, NOW, TTL, alive) is False


def test_expired_holder_is_free():
    assert is_taken({"holder": "console", "beat": NOW - TTL * 60 - 1}, NOW, TTL, alive) is False


def test_dead_process_frees_the_lock_immediately():
    """Мёртвый процесс не должен держать проект до конца срока."""
    record = {"holder": "bridge", "pid": 4242, "beat": NOW - 60}
    assert is_taken(record, NOW, TTL, dead) is False


def test_living_process_holds_it():
    record = {"holder": "bridge", "pid": 4242, "beat": NOW - 60}
    assert is_taken(record, NOW, TTL, alive) is True


def test_record_without_a_beat_is_free():
    """Нет подтверждения жизни - ждать нечего, иначе проект встал бы навсегда."""
    assert is_taken({"holder": "console", "pid": 1}, NOW, TTL, alive) is False


def test_garbage_beat_is_free():
    """Мусор во времени - тоже отсутствие подтверждения, а не повод ждать."""
    assert is_taken({"holder": "console", "beat": "вчера"}, NOW, TTL, alive) is False


def test_since_is_used_when_beat_is_absent():
    """Старая запись без «сердцебиения» судится по времени начала."""
    assert is_taken({"holder": "console", "since": NOW - 60}, NOW, TTL, alive) is True


# ── файл ───────────────────────────────────────────────────────────────────
def test_missing_file_reads_as_free(tmp_path):
    assert bridge_lock.read(tmp_path / "нет.lock") == {}


def test_broken_file_reads_as_free(tmp_path):
    """Испорченный замок не должен ронять программу: это «свободно»."""
    path = tmp_path / "bridge.lock"
    path.write_text("{это не json", encoding="utf-8")
    assert bridge_lock.read(path) == {}
    assert Lock(path).acquire("задача") is True, "после порчи замок не берётся"


def test_file_with_a_list_reads_as_free(tmp_path):
    path = tmp_path / "bridge.lock"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert bridge_lock.read(path) == {}


def test_write_is_readable_afterwards(tmp_path):
    path = tmp_path / "bridge.lock"
    bridge_lock.write(path, {"holder": "bridge", "beat": NOW})
    assert bridge_lock.read(path)["holder"] == "bridge"


# ── владение ───────────────────────────────────────────────────────────────
def test_free_lock_is_taken(tmp_path):
    lock = Lock(tmp_path / "bridge.lock", holder="bridge")
    assert lock.acquire("починить кнопки") is True
    assert lock.status() == {"free": False, "holder": "bridge", "mine": True,
                             "task": "починить кнопки", "beat": lock.status()["beat"]}


def test_another_holder_cannot_take_it(tmp_path):
    """Главное правило: вдвоём не работаем."""
    path = tmp_path / "bridge.lock"
    mine = Lock(path, holder="bridge")
    theirs = Lock(path, holder="console")
    assert mine.acquire("моя задача") is True
    assert theirs.acquire("их задача") is False, "второй держатель прошёл"
    assert bridge_lock.read(path)["holder"] == "bridge", "запись перебили"
    assert bridge_lock.read(path)["task"] == "моя задача"


def test_same_holder_can_take_its_own_lock(tmp_path):
    """Программа перезапустилась - ждать сама себя до срока нельзя."""
    path = tmp_path / "bridge.lock"
    first = Lock(path, holder="bridge")
    second = Lock(path, holder="bridge")
    assert first.acquire("задача") is True
    assert second.acquire("задача") is True


def test_expired_lock_can_be_taken(tmp_path):
    """Упавшая программа не должна держать проект до перезагрузки."""
    path = tmp_path / "bridge.lock"
    bridge_lock.write(path, {"holder": "bridge", "pid": os.getpid(),
                             "since": time.time() - 4000, "beat": time.time() - 4000})
    other = Lock(path, holder="console")
    assert other.acquire("забрать проект") is True
    assert other.status()["mine"] is True


def test_lock_of_a_dead_process_is_taken(tmp_path):
    """Процесса нет - значит и держателя нет, независимо от срока."""
    path = tmp_path / "bridge.lock"
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    bridge_lock.write(path, {"holder": "bridge", "pid": finished.pid,
                             "since": time.time(), "beat": time.time()})
    other = Lock(path, holder="console")
    assert bridge_lock.pid_alive(finished.pid) is False
    assert other.acquire("забрать") is True


# ── сердцебиение и освобождение ────────────────────────────────────────────
def test_heartbeat_extends_our_lock(tmp_path):
    path = tmp_path / "bridge.lock"
    lock = Lock(path, holder="bridge")
    assert lock.acquire("задача") is True
    before = bridge_lock.read(path)["beat"]
    time.sleep(0.01)
    assert lock.heartbeat() is True
    assert bridge_lock.read(path)["beat"] > before


def test_heartbeat_of_a_stranger_does_nothing(tmp_path):
    """Продлить чужой замок нельзя: иначе его можно было бы украсть, «помогая»."""
    path = tmp_path / "bridge.lock"
    mine = Lock(path, holder="bridge")
    theirs = Lock(path, holder="console")
    assert mine.acquire("задача") is True
    before = bridge_lock.read(path)["beat"]
    assert theirs.heartbeat() is False
    assert bridge_lock.read(path)["beat"] == before


def test_heartbeat_fails_when_the_lock_is_gone(tmp_path):
    lock = Lock(tmp_path / "bridge.lock", holder="bridge")
    assert lock.heartbeat() is False


def test_release_frees_the_lock(tmp_path):
    path = tmp_path / "bridge.lock"
    lock = Lock(path, holder="bridge")
    assert lock.acquire("задача") is True
    assert lock.release() is True
    assert lock.status()["free"] is True
    assert Lock(path, holder="console").acquire("можно") is True


def test_release_does_not_touch_a_strangers_lock(tmp_path):
    path = tmp_path / "bridge.lock"
    mine = Lock(path, holder="bridge")
    assert mine.acquire("задача") is True
    assert Lock(path, holder="console").release() is False
    assert mine.status()["mine"] is True, "чужой отпустил наш замок"


def test_release_of_a_free_lock_is_false(tmp_path):
    assert Lock(tmp_path / "bridge.lock", holder="bridge").release() is False


# ── сообщения владельцу ────────────────────────────────────────────────────
def test_who_names_the_holder_in_words(tmp_path):
    path = tmp_path / "bridge.lock"
    console = Lock(path, holder="console")
    assert console.acquire("правка") is True
    assert Lock(path, holder="bridge").who() == "ты в консоли"
    assert console.who() == "ты в консоли"


def test_who_says_nobody_when_free(tmp_path):
    assert Lock(tmp_path / "bridge.lock", holder="bridge").who() == "никто"


def test_unknown_holder_is_shown_as_is(tmp_path):
    path = tmp_path / "bridge.lock"
    bridge_lock.write(path, {"holder": "что-то новое", "beat": time.time()})
    assert Lock(path, holder="bridge").who() == "что-то новое"


# ── удобная обёртка ────────────────────────────────────────────────────────
def test_context_manager_takes_and_releases(tmp_path):
    path = tmp_path / "bridge.lock"
    with Lock(path, holder="bridge"):
        assert Lock(path, holder="console").acquire("нельзя") is False
    assert Lock(path, holder="console").acquire("теперь можно") is True


def test_context_manager_refuses_a_busy_lock(tmp_path):
    path = tmp_path / "bridge.lock"
    assert Lock(path, holder="console").acquire("правка") is True
    with pytest.raises(LockBusy) as ошибка:
        with Lock(path, holder="bridge"):
            pass
    assert "консоли" in str(ошибка.value)


# ── проверка на живой машине ───────────────────────────────────────────────
def test_pid_alive_recognises_this_process():
    assert bridge_lock.pid_alive(os.getpid()) is True


def test_pid_alive_rejects_nonsense():
    """Невозможные номера не должны считаться живыми: иначе проект встал бы."""
    for pid in (0, -1, 2 ** 31):
        assert bridge_lock.pid_alive(pid) is False


def test_default_path_ends_with_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTLPC_STATE_DIR", str(tmp_path))
    path = bridge_lock.default_path()
    assert path.name == "bridge.lock"
    assert str(tmp_path) in str(path)


def test_stamp_comes_from_the_project_clock():
    """Время замка - то же, что у проекта (ЕКБ), а не локальное время машины."""
    assert bridge_lock.stamp() == bridge_lock.clock.stamp()
