"""Реестр обработчиков: кнопки (callback) и состояния диалога.

Обработчики регистрируются декораторами в момент импорта модулей-хендлеров,
поэтому порядок импортов важен — его обеспечивает bot.py.
"""
CALLBACKS: dict = {}
STATES: dict = {}


def callback(name: str):
    def deco(fn):
        CALLBACKS[name] = fn
        return fn

    return deco


def state(name: str):
    def deco(fn):
        STATES[name] = fn
        return fn

    return deco
