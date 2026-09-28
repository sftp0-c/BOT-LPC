"""Версия проекта: файл, health и панель должны говорить одно и то же.

Три места, где живёт номер версии, разъедутся молча: поправил VERSION, забыл
про /health - и по адресу видно одно, а в панели другое. Тест сверяет все три.
"""
from pathlib import Path

import pytest

import version
from conftest import login_panel

VERSION_FILE = Path("VERSION")


def test_version_file_is_readable():
    """VERSION - единственный источник правды, и он существует."""
    assert VERSION_FILE.is_file(), "нет файла VERSION - версию негде посмотреть"
    value = VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    assert value, "файл VERSION пуст"
    assert value == version.__version__, (
        f"файл говорит {value!r}, а version.py - {version.__version__!r}")


def test_version_looks_like_a_version():
    """Номер версии: три числа через точку. Без дат и без мусора."""
    parts = version.__version__.split(".")
    assert len(parts) == 3, f"версия {version.__version__!r} не из трёх чисел"
    assert all(part.isdigit() for part in parts), f"в версии есть не числа: {parts}"


def test_health_reports_the_same_version():
    """По /health видно, какая версия в контейнере."""
    from fastapi.testclient import TestClient

    from bot import app

    with TestClient(app) as client:
        payload = client.get("/health").json()
    assert payload["version"] == version.__version__


def test_panel_footer_shows_the_version(panel_client):
    """В панели версия видна сразу, без захода в контейнер."""
    assert login_panel(panel_client)
    body = panel_client.get("/panel").text
    assert version.__version__ in body, "в подвале панели нет номера версии"
    assert "foot-ver" in body


def test_version_survives_a_missing_file(monkeypatch, tmp_path):
    """Без файла VERSION бот не падает, а отдаёт «неизвестно».

    Проверка версии не должна ронять сервис: старый образ, запуск из папки,
    файл случайно удалили - всё это повод показать «неизвестно», а не 500.
    """
    def boom(self, *args, **kwargs):
        raise OSError("нет файла")

    monkeypatch.setattr(Path, "read_text", boom)
    assert version.read() == version.UNKNOWN


# Где версия вообще показывается: /health и подвал панели.
# webpanel.py в списке нет: это фасад, он страницы не рисует.
SHOWN_IN = ("bot.py", "web/common.py")


@pytest.mark.parametrize("name", SHOWN_IN)
def test_project_says_which_version_it_is(name):
    """В коде версия не зашита строкой - она читается из файла."""
    source = Path(name).read_text(encoding="utf-8-sig")
    assert "import version" in source, f"{name} не читает версию из общего места"
    assert "version.__version__" in source, f"{name} импортирует version, но не показывает её"


def test_no_module_hardcodes_a_version_number():
    """Ни в одном модуле номера версии быть не должно.

    Зашитый номер пережил бы смену файла VERSION и начал врать: в панели и в
    /health показывалось бы разное. Проверяем все модули проекта, включая
    те, что версию не показывают.
    """
    from version import __version__ as number

    offenders = []
    for path in sorted(Path(".").rglob("*.py")):
        parts = set(path.parts)
        if parts & {".venv", "tests", ".git", "__pycache__"}:
            continue
        if path.name == "version.py":
            continue      # в докстринге номер стоит как пример формата
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        if number in source or "v1.0.0" in source:
            offenders.append(str(path))
    assert not offenders, "номер версии зашит в код: " + ", ".join(offenders)
