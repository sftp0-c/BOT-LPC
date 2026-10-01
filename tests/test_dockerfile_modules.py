"""Dockerfile не должен забывать модули проекта.

Модули в образ попадают списком в строке `COPY`, а не папкой целиком. Список
разъезжается: модуль добавили, а в `COPY` не вписали - и бот падает на
импорте. Нашлось так с `clock.py`: сборка падала на проверке импортов, хотя
на машине всё работало.

Тест сравнивает список в `COPY` с настоящими файлами и папками проекта.
Папки (`store/`, `web/`) копируются отдельными строками «COPY имя ./имя»,
и раньше сюда не попадали: забытая папка проходила проверку молча.
"""
import re
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
COPY_MARKER = "COPY config.py"
# Папки копируются отдельной строкой «COPY имя ./имя».
PACKAGE_MARKER = re.compile(r"^COPY\s+(\w+)\s+\./\1\s*$", re.M)
# Папки, которые не обязаны попадать в образ: данные, окружение, кэш.
SKIP_DIRS = {"tests", "data", "backups", "schedules", "attachments",
            ".venv", ".git", "site-packages", "__pycache__", ".idea"}

# Служебные файлы: не модули, в образ им не место
SERVICE = {
    "__init__",
    "conftest",
    "setup",
    "version",
    "conftest_local",
}

# Модули, которые работают только на этой машине, и в образе им не место.
#   backup_before_push.py  - зовёт git-хук pre-push, контейнеру не нужен
#
# Исключение опасно тем, что им можно спрятать настоящую зависимость: дописать
# сюда модуль, который боту необходим. Поэтому ниже есть проверка host_only -
# она требует, чтобы бот ни одного из этих файлов не импортировал.
HOST_ONLY = {"backup_before_push.py"}


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


def copied_packages() -> set:
    """Имена папок-пакетов из строк «COPY имя ./имя»."""
    return set(PACKAGE_MARKER.findall(DOCKERFILE.read_text(encoding="utf-8")))


def project_packages() -> set:
    """Папки проекта с модулями: их тоже надо копировать в образ.

    Признак - наличие .py внутри, а не __init__.py: папка handlers импортируется
    как пакет, но __init__.py в ней нет, и по первому признаку она выпадала
    бы из проверки, а строка «COPY handlers ./handlers» считалась бы лишней.
    """
    return {path.name for path in Path(".").iterdir()
            if path.is_dir() and path.name not in SKIP_DIRS
            and any(path.glob("*.py"))}


def project_modules() -> set:
    """Модули верхнего уровня, которые обязаны попасть в образ."""
    names = {path.name for path in Path(".").glob("*.py")}
    names -= SERVICE
    # файлы, которые живут только для разработки
    names -= {name for name in names if name.startswith("test_")}
    # инструменты этой машины: в контейнере им делать нечего
    names -= HOST_ONLY
    return names


def host_only_modules() -> set:
    """Инструменты этой машины, которые действительно лежат в проекте."""
    return {path.name for path in Path(".").glob("*.py")} & HOST_ONLY


def test_copy_block_found():
    assert copied_names(), "не нашли блок COPY с перечислением модулей"


def test_every_module_is_copied_into_image():
    missing = sorted(project_modules() - copied_names())
    assert not missing, (
        "модули не попадут в образ, сборка упадёт на импорте: " + ", ".join(missing))


def test_copy_has_no_extra_names():
    extra = sorted(name for name in copied_names() if not Path(name).exists())
    assert not extra, "в COPY перечислены несуществующие файлы: " + ", ".join(extra)


def test_every_package_is_copied_into_image():
    missing = sorted(project_packages() - copied_packages())
    assert not missing, (
        "папки не попадут в образ, сборка упадёт на импорте: " + ", ".join(missing))


def test_copied_packages_exist():
    extra = sorted(copied_packages() - project_packages())
    assert not extra, "в COPY перечислены несуществующие папки: " + ", ".join(extra)


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
    missing += sorted(name for name in copied_packages() if name not in checked)
    assert not missing, "копируются, но не проверяются импортом: " + ", ".join(missing)


def test_host_only_list_is_real():
    """Исключение не должно быть пустым: иначе оно ни о чём не говорит.

    Список «просто на всякий случай» - это не исключение, а способ отключить
    проверку, не заметив этого.
    """
    missing = sorted(HOST_ONLY - host_only_modules())
    assert not missing, (
        "в HOST_ONLY перечислены файлы, которых в проекте нет: "
        f"{missing}. Либо файл удалён, либо список протух."
    )


def test_bot_does_not_import_host_only_modules():
    """Бот не должен зависеть от инструментов этой машины.

    Если такая зависимость появится - сборка упадёт на импорте в контейнере,
    где этих файлов нет. Проверка ловит это здесь, с внятным текстом.
    """
    sources = ["bot.py", "webpanel.py", "repository.py", "database.py", "config.py",
               "clock.py", "utils.py", "max_api.py"]
    sources += [str(path) for path in Path("handlers").glob("*.py")]
    sources += [str(path) for path in Path("store").glob("*.py")]
    sources += [str(path) for path in Path("web").glob("*.py")]

    offenders = []
    for source in sources:
        try:
            text = Path(source).read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for name in sorted(HOST_ONLY):
            stem = name[:-3]
            for form in (f"import {stem}", f"from {stem} import", f"import {name}"):
                if form in text:
                    offenders.append(f"{source} -> {form}")
    assert not offenders, (
        "бот начал зависеть от инструментов этой машины, которых нет в образе: "
        + "; ".join(offenders)
    )


def test_timezone_is_set_in_image_and_compose():
    """Без TZ в контейнере всё время снова уедет в UTC."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "Yekaterinburg" in dockerfile, "в образе нет следа про часовой пояс"
    assert "zoneinfo" in dockerfile, "в образе не создана ссылка Europe/Yekaterinburg"
    assert "TZ" in compose, "в docker-compose.yml не передан TZ"
    assert "Yekaterinburg" in compose, "TZ в compose не указывает на Екатеринбург"
