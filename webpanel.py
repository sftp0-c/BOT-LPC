"""Веб-панель сис-админа: /panel — те же данные, что и в боте, но редактируются мышью.

Панель живёт в том же процессе, что и бот (FastAPI), поэтому все изменения сразу
видны боту и в MAX. Вход — по MAX ID сис-админа и паролю WEB_PANEL_PASSWORD из .env;
пока пароль не задан, панель отвечает 503.

Вкладки: обзор, обращения, пользователи, сотрудники, коды и заявки, база данных,
настройки, журнал и тесты. JSON-API для скриптов и проверок — /panel/api/*.

Сам код панели разложен по разделам в пакете web/: web/common.py — доступ, формы и
оболочка страницы, web/tickets.py — обращения, web/people.py — люди и так далее.
Этот модуль — фасад: он отдаёт наружу всё, чем панель пользуются снаружи (bot.py
подключает webpanel:router, schedule_watch и handlers берут константы, тесты —
хелперы), поэтому список исчерпывающе задан в __all__.

Здесь нет ни одного маршрута и ни одной строки разметки: всё это лежит в
пакете web/. Модули пакета не берут этот файл - ни на верхнем уровне, ни
локально: иначе получился бы круг webpanel -> web.* -> webpanel, и
``import web.tickets`` в одиночку перестал бы работать.
"""
import sys
from types import ModuleType

import schedule_import
from handlers.admin import probe_pdf_url
from handlers.broadcast import run_broadcast
from panel_theme import actions_script, hotkeys_script
from utils import person_label

from web.access import _join_page_html
from web import data  # noqa: F401  - раздел «Данные» подключается к общему роутеру панели
from web.common import (COOKIE, JOIN_STYLE, NAV_GROUPS, NAV_SECTIONS, STYLE, _csrf, _flashes,
                        _sessions, is_sysadmin, nice_max, page, panel_link)
from web.database import schema_broken_page
from web.mailer import BROADCASTS_PAGE
from web.overview import ACTIVITY_PAGE
from web.people import PEOPLE_PAGE, STUDENTS_PAGE
from web.router import open_router, router
from web.schedules import COLLEGE_SCHEDULE_PAGE
from web.settings import HUMAN_SETTINGS, HUMAN_SETTING_KEYS
from web.tickets import (CERT_PICKUP, TEMPLATES_PAGE, is_certificate, pickup_hint,
                         pickup_options)


# Список исчерпывающий: наружу панель отдаёт ровно то, чем пользуются снаружи.
__all__ = [
    "ACTIVITY_PAGE", "BROADCASTS_PAGE", "CERT_PICKUP", "COLLEGE_SCHEDULE_PAGE", "COOKIE",
    "HUMAN_SETTINGS", "HUMAN_SETTING_KEYS", "JOIN_STYLE", "NAV_GROUPS", "NAV_SECTIONS",
    "PEOPLE_PAGE", "STUDENTS_PAGE", "STYLE", "TEMPLATES_PAGE",
    "_csrf", "_flashes", "_join_page_html", "_sessions",
    "actions_script", "hotkeys_script", "is_certificate", "is_sysadmin", "nice_max",
    "open_router", "page", "panel_link", "person_label", "pickup_hint", "pickup_options",
    "probe_pdf_url", "router", "run_broadcast", "schedule_import", "schema_broken_page",
]


class _Facade(ModuleType):
    """Подмена имени на фасаде доходит до модуля, который им пользуется.

    Проверки подменяют webpanel.run_broadcast и webpanel.probe_pdf_url. Когда обе
    функции лежали в webpanel.py, это работало само собой. Теперь они в
    web/mailer.py и web/schedules.py, и обычный setattr на фасаде изменил бы
    только ссылку здесь, а маршрут позвал бы старую функцию - подмена вышла бы
    тихой и незаметной. Поэтому пишем в оба места.
    """

    PATCHABLE = {"run_broadcast": "web.mailer", "probe_pdf_url": "web.schedules"}

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        owner = self.PATCHABLE.get(name)
        if owner:
            setattr(sys.modules[owner], name, value)


sys.modules[__name__].__class__ = _Facade
