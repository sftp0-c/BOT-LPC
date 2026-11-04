"""Реестр обработчиков кнопок и состояний диалога.

Обработчики регистрируются декораторами в момент импорта модулей. Повторное имя
не переопределяется молча: иначе поведение зависит от порядка импортов.
"""
from collections.abc import Callable


CALLBACKS: dict[str, Callable] = {}
STATES: dict[str, Callable] = {}

# Старые сообщения MAX остаются в переписке и могут быть нажаты после обновления.
# Здесь перечислены только намеренно поддерживаемые старые адреса и состояния.
COMPATIBILITY_MANIFEST = {
    "callbacks": {
        "today": "по роли: admin_today или пары на сегодня",
        "registration_name": "regname",
        "registration_group": "regpick",
        "savegrp": "строгая смена группы из старой кнопки",
        "saveprofile": "строгое обновление профиля из старой кнопки",
    },
    "states": {
        "registration_name": "reg_name",
        "registration_group": "reg_group",
        "req_position": "по payload: staff_request_position или admin_request_position",
    },
}


def _register(registry: dict[str, Callable], kind: str, name: str, fn: Callable) -> Callable:
    if name in registry:
        previous = registry[name]
        raise RuntimeError(
            f"Повторная регистрация {kind} {name!r}: "
            f"{previous.__module__}.{previous.__name__} и {fn.__module__}.{fn.__name__}"
        )
    registry[name] = fn
    return fn


def callback(name: str):
    def deco(fn: Callable) -> Callable:
        return _register(CALLBACKS, "callback", name, fn)

    return deco


def state(name: str):
    def deco(fn: Callable) -> Callable:
        return _register(STATES, "state", name, fn)

    return deco
