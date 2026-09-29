"""Разбиение панели на модули: короткий фасад webpanel.py и пакет web/.

Проверки здесь поведенческие: не «в файле есть такая строка», а «панель отдаёт
ровно то же, что и раньше».

* webpanel.py остался фасадом, а не новым монолитом;
* ни один маршрут не потерян: пути, объявленные в исходниках панели, совпадают
  с теми, что реально зарегистрированы в приложении;
* наружу не пропало ни одно имя, которым панель пользуются снаружи: bot.py,
  handlers, schedule_watch.py, тесты и сам пакет web/;
* webpanel.router — тот же самый объект, что подключён в bot.app;
* между модулями пакета нет кругов импортов: любой из них импортируется сам по
  себе, в чистом интерпретаторе.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

import bot
import webpanel

ROOT = Path(__file__).resolve().parent.parent
FACADE = ROOT / "webpanel.py"
PACKAGE = ROOT / "web"

# Префиксы роутеров панели: те же, что были в webpanel.py до разбиения.
PREFIXES = {"router": "/panel", "open_router": ""}
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# Модули пакета, которые можно импортировать по одному.
SECTION_MODULES = ("access", "common", "database", "diagnostics", "directory", "mailer",
                   "overview", "people", "router", "schedules", "settings", "dossier", "staff_bulk", "tickets")

# '_flash' пишет conftest как обычный атрибут модуля - самого такого имени в
# панели нет и раньше не было; 'app' - это bot.app, у панели своего нет.
IGNORED_EXTERNAL = {"_flash", "app"}


def panel_sources() -> list:
    """Файлы, в которых панель объявляет маршруты: фасад и модули пакета."""
    return [FACADE] + sorted(PACKAGE.glob("*.py"))


def declared_routes(source: str) -> set:
    """Пути и методы, объявленные декораторами @router.* в исходнике."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            owner = dec.func.value
            if not isinstance(owner, ast.Name) or owner.id not in PREFIXES:
                continue
            if dec.func.attr not in HTTP_METHODS or not dec.args:
                continue
            found.add((dec.func.attr.upper(), PREFIXES[owner.id] + ast.literal_eval(dec.args[0])))
    return found


def registered_routes(routes) -> set:
    """Пути и методы, которые FastAPI видит у приложения.

    app.routes плоский не у всех версий: подключённый роутер лежит внутри
    объекта-обёртки, поэтому заходим внутрь.
    """
    found = set()
    for route in routes:
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if path and methods:
            found |= {(method, path) for method in methods
                      if method not in ("HEAD", "OPTIONS") and method.isupper()}
            continue
        for attr in ("original_router", "router", "app"):
            inner = getattr(route, attr, None)
            if hasattr(inner, "routes"):
                found |= registered_routes(inner.routes)
                break
        else:
            if hasattr(route, "routes"):
                found |= registered_routes(route.routes)
    return found


def mounted_routers(routes) -> list:
    """Роутеры, подключённые к приложению (в том числе внутри обёрток)."""
    found = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if hasattr(inner, "routes"):
            found.append(inner)
            found += mounted_routers(inner.routes)
        elif hasattr(route, "routes"):
            found += mounted_routers(route.routes)
    return found


def panel_routes() -> set:
    """Реально зарегистрированные маршруты панели: /panel/* и открытое /join/*."""
    found = registered_routes(bot.app.routes)
    return {item for item in found if item[1].startswith(("/panel", "/join"))}


# ── фасад ─────────────────────────────────────────────────────────────────────
def test_facade_is_short():
    """webpanel.py — фасад: чуть-чуть кода, а не весь панельный монолит.

    Ориентир 100-250 строк, потолок 300: панель в пакете web/ занимает в разы
    больше, и если фасад снова распухнет, разбиение перестало быть разбиением.
    """
    lines = len(FACADE.read_text(encoding="utf-8-sig").splitlines())
    assert lines < 300, f"webpanel.py вырос до {lines} строк - разбиение размылось"


def test_facade_keeps_the_panel_package():
    """Фасад — это webpanel плюс пакет web/, а не одинокий файл."""
    assert PACKAGE.is_dir(), "нет пакета web/ - панель не разбита"
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    missing = sorted(set(SECTION_MODULES) - modules)
    assert not missing, "в пакете web/ нет модулей: " + ", ".join(missing)
    assert (PACKAGE / "__init__.py").exists(), "у пакета web/ нет __init__.py"


# ── маршруты ─────────────────────────────────────────────────────────────────
def test_no_route_is_lost():
    """Каждый путь, который объявляет исходник, реально зарегистрирован в app.

    Сверяем не текст, а поведение: собираем пути из декораторов панели и
    сравниваем с тем, что видит FastAPI. Расхождение в любую сторону — ошибка
    разбиения: потерянный путь исчезает молча, лишний ловится не на той
    странице.
    """
    declared = set()
    for path in panel_sources():
        declared |= declared_routes(path.read_text(encoding="utf-8-sig"))
    registered = panel_routes()
    assert declared, "в исходниках панели не нашлось ни одного маршрута"
    assert not declared - registered, "потеряны маршруты: %s" % sorted(declared - registered)
    assert not registered - declared, "лишние маршруты: %s" % sorted(registered - declared)
    assert len(registered) >= 94, "панель вдруг похудела: %d маршрутов" % len(registered)


def test_routes_match_the_file_before_the_split():
    """Сверка с исходником, каким webpanel.py был до разбиения.

    Пока файл не закоммичен, его прежнюю версию видно через ``git show``; если
    её уже нет (разбиение закоммичено), проверка уступает дорогу постоянной
    сверке из test_no_route_is_lost.
    """
    done = subprocess.run(["git", "show", "HEAD:webpanel.py"], cwd=ROOT,
                          capture_output=True)
    if done.returncode != 0:
        pytest.skip("git недоступен или прежнего webpanel.py нет в HEAD")
    before = declared_routes(done.stdout.decode("utf-8-sig"))
    if not before:
        pytest.skip("в HEAD webpanel.py уже фасад - сверяться не с чем")
    now = panel_routes()
    assert not before - now, "потеряны маршруты: %s" % sorted(before - now)
    assert not now - before, "лишние маршруты: %s" % sorted(now - before)


def test_router_is_the_mounted_one():
    """webpanel.router — тот же объект, что подключён в приложении.

    Роутеры панели создаёт web/router.py, а отдаёт webpanel.py. Если эти два
    места разойдутся, bot.py подключит пустой роутер, и панель молча станет
    пустой — без единой ошибки при импорте.
    """
    assert isinstance(bot.app, FastAPI), "bot.app больше не приложение FastAPI"
    assert isinstance(webpanel.router, APIRouter) and webpanel.router.prefix == "/panel"
    assert isinstance(webpanel.open_router, APIRouter)
    assert webpanel.router in mounted_routers(bot.app.routes), (
        "webpanel.router не подключён в bot.app")
    assert webpanel.open_router in mounted_routers(bot.app.routes), (
        "webpanel.open_router не подключён в bot.app")


# ── публичный фасад ──────────────────────────────────────────────────────────
def external_names() -> set:
    """Имена, которые берут у webpanel снаружи: bot.py, handlers, тесты, web/.

    Собираем по дереву разбора, а не регуляркой по всему тексту: иначе в список
    попадают совпадения внутри строк вроде Path("webpanel.py"). Отдельно ловим
    getattr/setattr - там имя приходит строкой, и разбор его не видит.
    """
    names = set()
    files = [ROOT / "bot.py", ROOT / "schedule_watch.py"]
    files += sorted((ROOT / "handlers").glob("*.py"))
    files += sorted((ROOT / "tests").glob("*.py"))
    files += sorted(PACKAGE.glob("*.py"))
    files = [path for path in files if path.resolve() != Path(__file__).resolve()]
    by_string = re.compile(r"""\bwebpanel\s*,\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ("webpanel", "wp")):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module == "webpanel":
                names |= {alias.name for alias in node.names}
        names |= set(by_string.findall(text))
    return names


def test_nothing_external_disappeared():
    """Каждое имя, которым пользуются снаружи, на месте в webpanel.

    Список берём из кода, а не из памяти: так новая проверка не застанет врасплох
    ни бота, ни тесты.
    """
    wanted = external_names() - IGNORED_EXTERNAL
    missing = sorted(name for name in wanted if not hasattr(webpanel, name))
    assert not missing, "webpanel больше не отдаёт: " + ", ".join(missing)
    assert wanted, "список внешних имён пуст - проверка ничего не проверяет"


def test_facade_exports_what_it_claims():
    """__all__ фасада не врёт: каждый названный имень есть — и лишнего нет.

    Второе тоже важно: если кто-то снаружи полезет за именем, которого нет в
    __all__, разбиение обязано это заметить — иначе фасад незаметно перестанет
    быть полным списком того, чем панель живёт снаружи.
    """
    exported = set(webpanel.__all__)
    missing = sorted(name for name in exported if not hasattr(webpanel, name))
    assert not missing, "в __all__ есть имена, которых нет в модуле: " + ", ".join(missing)
    wanted = external_names() - IGNORED_EXTERNAL
    assert wanted <= exported, "в __all__ нет: " + ", ".join(sorted(wanted - exported))
    assert exported <= wanted, "в __all__ лишние имена: " + ", ".join(sorted(exported - wanted))


def test_state_stays_shared():
    """Сессии, CSRF-токены и флеш-сообщения — те же объекты, что раньше.

    Их чистят снаружи (conftest и проверки берут webpanel._sessions напрямую),
    поэтому реэкспорт должен отдавать именно тот же словарь, а не копию.
    """
    import web.common as common

    assert webpanel._sessions is common._sessions
    assert webpanel._csrf is common._csrf
    assert webpanel._flashes is common._flashes
    assert webpanel.COOKIE == common.COOKIE


def test_patchable_names_reach_their_module():
    """Подмена webpanel.run_broadcast / probe_pdf_url доходит до модуля-владельца.

    Проверки подменяют эти два имени на фасаде. Когда они лежали в webpanel.py,
    это работало само собой; после разбиения обычный setattr изменил бы только
    ссылку на фасаде, а маршрут позвал бы старую функцию — подмена вышла бы
    тихой, и проверка панели проверяла бы не то.
    """
    import web.mailer as mailer
    import web.schedules as schedules

    for name, module in (("run_broadcast", mailer), ("probe_pdf_url", schedules)):
        original = getattr(module, name)

        def replacement(*args, **kwargs):
            return None

        setattr(webpanel, name, replacement)
        try:
            assert getattr(module, name) is replacement, (
                "подмена webpanel.%s не дошла до модуля-владельца" % name)
        finally:
            setattr(webpanel, name, original)
        assert getattr(module, name) is original, (
            "откат подмены webpanel.%s не дошёл до модуля-владельца" % name)


# ── круги импортов ───────────────────────────────────────────────────────────
def package_edges() -> dict:
    """Кто кого импортирует внутри пакета web/: имя модуля -> список имён."""
    edges = {}
    for path in sorted(PACKAGE.glob("*.py")):
        targets = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                targets.add(node.module.split(".")[0])
        edges[path.stem] = targets
    return edges


def test_package_has_no_import_cycles():
    """Модули пакета не образуют кругов: граф импортов ацикличен."""
    edges = package_edges()
    state = {}

    def walk(name):
        state[name] = 1
        for other in sorted(edges.get(name, ())):
            if state.get(other) == 1:
                return [f"{name} -> {other}"]
            if state.get(other) is None:
                found = walk(other)
                if found:
                    return found
        state[name] = 2
        return []

    for name in sorted(edges):
        if state.get(name) is None:
            cycle = walk(name)
            assert not cycle, "круг импортов в web/: " + " -> ".join(cycle)


@pytest.mark.parametrize("module", SECTION_MODULES)
def test_module_imports_standalone(module):
    """Любой модуль пакета импортируется сам по себе, в чистом интерпретаторе.

    Проверка ловит именно круг импортов: если web.tickets тянет web.people, а тот
    web.tickets, то `import web.tickets` в одиночку упадёт — а в приложении, где
    пакет импортируют целиком, ничего не заметно.
    """
    done = subprocess.run([sys.executable, "-c", f"import web.{module}"], cwd=ROOT,
                          capture_output=True)
    assert done.returncode == 0, (
        f"import web.{module} падает в одиночку:\n"
        + done.stderr.decode("utf-8", "replace")[-800:])


def test_no_module_pulls_webpanel_at_import_time():
    """Модули пакета не берут webpanel на верхнем уровне.

    Круг webpanel -> web/* -> webpanel сломал бы и этот модуль, и проверку выше.
    Разрешён только локальный импорт внутри функции: к моменту вызова фасад уже
    загружен.
    """
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in tree.body:                       # только верхний уровень
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "webpanel":
                pytest.fail("%s импортирует webpanel на верхнем уровне" % path.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "webpanel":
                        pytest.fail("%s импортирует webpanel на верхнем уровне" % path.name)
