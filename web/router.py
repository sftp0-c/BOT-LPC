"""Роутеры панели: их создаёт один модуль, остальные на них подписываются.

Два APIRouter на всё приложение. Создавать их прямо в webpanel.py нельзя: пути
объявляются декораторами в момент импорта модуля раздела, а модули пакета web
импортируются как раз из webpanel.py - вышел бы круг импортов. Поэтому создание
живёт здесь, в листе пакета, а наружу (bot.py, тестам) оба роутера отдаёт
webpanel.py.
"""
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute

import repository as repo

log = logging.getLogger("panel.audit")


class AuditRoute(APIRoute):
    """Событие пишется после ответа, а не при проверке CSRF до выполнения действия."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            if request.method != "POST":
                return await original(request)
            result = "failed"
            try:
                response = await original(request)
                result = "denied" if 400 <= response.status_code < 500 else (
                    "failed" if response.status_code >= 500 else "ok")
                result = getattr(request.state, "audit_result", result)
                return response
            except HTTPException as exc:
                result = "denied" if exc.status_code < 500 else "failed"
                raise
            finally:
                if not getattr(request.state, "audit_skip", False):
                    await self.record(request, result)
        return handler

    async def record(self, request: Request, result: str) -> None:
        uid = getattr(request.state, "audit_user", "")
        # У запроса без сессии автора нет. Значение из формы не угадываем.
        if not uid:
            return
        target_type = target_id = ""
        for key, kind in (("ticket_id", "ticket"), ("user_id", "user"),
                          ("template_id", "template"), ("faq_id", "faq"), ("rowid", "row"),
                          ("feedback_id", "public_feedback")):
            raw = str(request.path_params.get(key, ""))
            if re.fullmatch(r"[0-9]{1,20}", raw):
                target_type, target_id = kind, raw
                break
        try:
            role = "owner" if await repo.is_owner(uid) else (
                "sysadmin" if await repo.is_sysadmin(uid) else "unknown")
            if self.name == "quick_search" and result == "ok":
                result = "received"  # поиск ничего не меняет
            await repo.log_audit(uid, role, self.name, target_type, target_id, result)
        except Exception as exc:  # потеря журнала видна, но не ломает завершённый ответ
            log.warning("Не записан аудит маршрута %s: %s", self.name, type(exc).__name__)


router = APIRouter(prefix="/panel", tags=["panel"], route_class=AuditRoute)

# Приглашения открыты всем, поэтому отдельный роутер без префикса /panel.
open_router = APIRouter(tags=["join"])
