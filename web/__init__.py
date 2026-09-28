"""Пакет веб-панели: код панели разложен по разделам, наружу его отдаёт webpanel.py.

Здесь только сборка: модули разделов подключаются один раз, и каждый из них
можно импортировать сам по себе - кругов импортов между ними нет. Публичные
имена панели живут в webpanel.py, этот пакет для внешних потребителей закрыт.
"""
from . import (access, common, database, diagnostics, directory, mailer, overview, people, router,
               schedules, settings, tickets)

__all__ = ["access", "common", "database", "diagnostics", "directory", "mailer", "overview",
           "people", "router", "schedules", "settings", "tickets"]
