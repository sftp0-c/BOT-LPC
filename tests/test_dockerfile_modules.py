"""Dockerfile не должен забывать модули проекта.

Модули в образ попадают списком в строке `COPY`, а не папкой целиком. Список
разъезжается: модуль добавили, а в `COPY` не вписали - и бот падает на
импорте. Нашлось так с `clock.py`: сборка падала на проверке импортов, хотя
на машине всё работало.

Тест сравнивает список в `COPY` с настоящими файлами проекта.
"""
import re
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
COPY_MARKER = "COPY config.py"

# Служебные файлы: не модули, в образ им не место
SERVICE = {
    "__init__",
    "conftest",
    "setup",
    "version",
    "conftest_local",
}


def copied_names() -> set:
    """Имена файлов из строк COPY, продолжающихся обратной косой чертой."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    block: list = []
    collecting = False
    for line in text.splitlines():
        if line.startswith(COPY_MARKER):
            collecting = True
        if not collecting:
            continue
        block.append(line)
        if not line.rstrip().endswith("\\"):
            break
    joined = " ".join(block)
    return {name for name in re.findall(r"[\w.]+\.py", joined)}


def project_modules() -> set:
    """Модули верхнего уровня, которые обязаны попасть в образ."""
    names = {path.name for path in Path(".").glob("*.py")}
    names -= SERVICE
    # файлы, которые живут только для разработки
    names -= {name for name in names if name.startswith("test_")}
    return names


def test_copy_block_found():
    assert copied_names(), "не нашли блок COPY с перечислением модулей"


def test_every_module_is_copied_into_image():
    missing = sorted(project_modules() - copied_names())
    assert not missing, (
        "модули не попадут в образ, сборка упадёт на импорте: " + ", ".join(missing))


def test_copy_has_no_extra_names():
    extra = sorted(name for name in copied_names() if not Path(name).exists())
    assert not extra, "в COPY перечислены несуществующие файлы: " + ", ".join(extra)


def test_clock_is_in_image():
    """Часовой пояс обязателен: без него даты в боте идут на пять часов мимо."""
    assert "clock.py" in copied_names()


def test_import_check_covers_every_copied_module():
    """Проверка на этапе сборки ловит забытый модуль раньше первого запуска."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    check = re.search(r'RUN python -c "import ([^"]+)"', text)
    assert check, "в Dockerfile нет проверки импортов"
    # после списка импортов идёт «; print(...)» - его отбрасываем
    listed = check.group(1).split(";", 1)[0]
    checked = {name.strip() for name in listed.split(",")}
    missing = sorted(name[:-3] for name in copied_names() if name[:-3] not in checked)
    assert not missing, "модули копируются, но не проверяются импортом: " + ", ".join(missing)


def test_timezone_is_set_in_image_and_compose():
    """Без TZ в контейнере всё время снова уедет в UTC."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "Yekaterinburg" in dockerfile, "в образе нет следа про часовой пояс"
    assert "zoneinfo" in dockerfile, "в образе не создана ссылка Europe/Yekaterinburg"
    assert "TZ" in compose, "в docker-compose.yml не передан TZ"
    assert "Yekaterinburg" in compose, "TZ в compose не указывает на Екатеринбург"
