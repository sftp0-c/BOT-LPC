"""Подмена MAX API обязана быть полной - и проверка не должна зависеть от имён.

Что случилось, из-за чего проверка вообще понадобилась. Фикстура подменяла
атрибут `api` у перечисленных модулей. Но `from handlers.common import api`
кладёт в модуль ССЫЛКУ на объект, и модуль, которого нет в списке, продолжает
держать настоящий MaxAPI. За приём я забыл об этом три раза: сначала про
handlers.bridge, потом про web.bridge, потом про web.diagnostics, где объект
импортирован под именем max_api. Каждый раз тест падал не своей ошибкой, а
«Что-то пошло не так», и виновника приходилось искать отдельно.

Что теперь сделано. Подменяются МЕТОДЫ настоящего объекта, а не имена в
модулях. Ссылка на объект у всех одна, поэтому хватает одной подмены, и новые
модули защищены автоматически.

Что тут проверяется. Важно не то, что модуль ДЕРЖИТ настоящий объект, - это
безобидно, - а то, что он не может через него уйти в сеть. Проверки ищут
незаглушенный способ уйти, а не сам факт хранения.
"""
import importlib
import pkgutil

import pytest

import max_api

pytestmark = pytest.mark.panel


def modules_of(package_name: str) -> list:
    """Все модули пакета: и сам пакет, и его содержимое."""
    package = importlib.import_module(package_name)
    found = [package]
    for info in pkgutil.iter_modules(package.__path__):
        try:
            found.append(importlib.import_module(f"{package_name}.{info.name}"))
        except Exception:               # noqa: BLE001 — модуль может требовать окружения
            continue
    return found


def api_objects() -> list:
    """Все объекты MaxAPI, до которых можно дотянуться через импорты."""
    found = []
    for package in ("handlers", "web"):
        for module in modules_of(package):
            for name, value in vars(module).items():
                if isinstance(value, max_api.MaxAPI) and not any(value is item for item in found):
                    found.append(value)
    return found


def is_sealed(obj, name: str) -> bool:
    """Заглушен ли способ уйти в сеть."""
    method = getattr(obj, name, None)
    return getattr(method, "__name__", "") == f"fake_{name}"


def unsealed() -> list:
    """Что ещё может уйти в сеть мимо заглушек."""
    from conftest import API_NETWORK_METHODS

    holes = []
    for obj in api_objects():
        for name in API_NETWORK_METHODS:
            if not is_sealed(obj, name):
                holes.append(name)
    return sorted(set(holes))


# ── главное ────────────────────────────────────────────────────────────────
def test_nothing_can_reach_the_network(env):
    """Ни один объект MAX в проекте не может отправить запрос."""
    assert unsealed() == [], (
        "эти способы уйти в сеть не заглушены, тесты через них долетят до "
        "настоящего MAX API: " + ", ".join(unsealed())
    )


def test_a_module_outside_the_replacement_list_is_safe(env):
    """Конкретный случай, который и ломал всё: web.diagnostics не в списке.

    Он держит настоящий объект - и это нормально. Проверяем, что отправка
    через него попадает в подделку, а не в сеть.
    """
    from web import diagnostics

    assert isinstance(diagnostics.max_api, max_api.MaxAPI), (
        "проверка потеряла смысл: если объект подменён на уровне импорта, "
        "проверять тут нечего"
    )
    assert is_sealed(diagnostics.max_api, "send"), "способ отправки не заглушен"


# ── сети: заглушены все ────────────────────────────────────────────────────
def test_send_is_sealed_everywhere(env):
    for index, obj in enumerate(api_objects()):
        assert is_sealed(obj, "send"), f"объект MAX №{index} может отправлять в сеть"


def test_the_service_methods_are_sealed_too(env):
    """Заглушены не только отправка, но и обновления с подписками.

    Забыть updates или subscribe - значит тест уйдёт в сеть, ничем не
    показав, что это он. Поэтому проверяем весь список.
    """
    from conftest import API_NETWORK_METHODS

    for index, obj in enumerate(api_objects()):
        for name in API_NETWORK_METHODS:
            if hasattr(max_api.MaxAPI, name):
                assert is_sealed(obj, name), f"у объекта №{index} не заглушен {name}"


def test_every_network_method_of_the_class_is_known(env):
    """Ни один сетевой способ MaxAPI не остался без заглушки.

    Если в MaxAPI добавят новый метод, который ходит в сеть, он попадёт сюда
    только если его допишут в API_NETWORK_METHODS. Проверка напоминает об этом
    сравнением с классом - новый публичный метод будет виден как незаглушенный
    только после того, как его внесу.
    """
    from conftest import API_NETWORK_METHODS

    живые = {name for name in vars(max_api.MaxAPI)
             if callable(getattr(max_api.MaxAPI, name)) and not name.startswith("_")}
    unknown = sorted(живые - set(API_NETWORK_METHODS))
    assert not unknown, (
        "в MaxAPI есть способы, которых нет в API_NETWORK_METHODS: "
        f"{unknown}. Если они ходят в сеть - допиши в conftest, иначе тест "
        "сможет уйти в настоящий MAX."
    )


# ── проверка не должна быть слепой ─────────────────────────────────────────
def test_the_check_sees_a_planted_leak(env, monkeypatch):
    """Если подложить незаглушенный объект, проверка обязана его увидеть.

    Без этого вышестоящие проверки рискуют ничего не находить - например, если
    бы обход модулей перестал работать и тихо возвращал пустой список.
    """
    import handlers.menus

    подделка = max_api.MaxAPI()
    monkeypatch.setattr(handlers.menus, "api", подделка, raising=False)
    дыры = unsealed()
    assert any(name == "send" for name in дыры), f"проверка промолчала: {дыры}"


def test_module_walk_is_not_shrinking(env):
    """Обход модулей не должен молча сокращаться до пустоты."""
    from web import bridge as web_bridge

    for package in ("handlers", "web"):
        assert len(modules_of(package)) >= 8, f"в пакете {package} мало модулей"
    assert web_bridge.__name__.endswith("web.bridge")
