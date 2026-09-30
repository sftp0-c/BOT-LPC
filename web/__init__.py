"""Пакет веб-панели: код панели разложен по разделам, наружу его отдаёт webpanel.py.

Здесь только сборка: модули разделов подключаются один раз, и каждый из них
можно импортировать сам по себе - кругов импортов между ними нет. Публичные
имена панели живут в webpanel.py, этот пакет для внешних потребителей закрыт.
"""
from . import (access, bridge, common, data, database, diagnostics, directory, dossier,
               mailer, overview, people, router, schedules, settings, staff_bulk, test_lab,
               tickets)

__all__ = ["access", "bridge", "common", "data", "database", "diagnostics", "directory", "dossier", "mailer",
           "overview", "people", "router", "schedules", "settings", "staff_bulk", "test_lab", "tickets"]
