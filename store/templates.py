"""Шаблоны ответов сотрудника."""

import clock
import database as db
from utils import as_str


# ── шаблоны ответов ───────────────────────────────────────────────────────────
async def list_templates(category: str = "", limit: int = 50) -> list:
    """Шаблоны для раздела (пусто - все); сначала подходящие, потом общие.

    Порядок: шаблон именно этого раздела, затем часто используемые.
    """
    where, params = ("WHERE category=? OR category='all'", (category,)) if category else ("", ())
    return await db.many(
        f"SELECT * FROM reply_templates {where} "
        "ORDER BY (category='all') ASC, used_count DESC, created_at DESC, id DESC LIMIT ?",
        (*params, max(1, int(limit))),
    )


async def get_template(template_id: int):
    return await db.one("SELECT * FROM reply_templates WHERE id=?", (int(template_id),))


async def add_template(title: str, text: str, category: str = "all", created_by: str = "") -> int:
    """Добавляет шаблон ответа и возвращает его id."""
    return await db.run(
        "INSERT INTO reply_templates(title, text, category, created_by, created_at) VALUES(?,?,?,?,?)",
        (as_str(title).strip()[:80], as_str(text).strip()[:2000], category or "all", as_str(created_by),
         clock.stamp()),
    )


async def update_template_text(template_id: int, text: str) -> None:
    await db.run("UPDATE reply_templates SET text=? WHERE id=?", (as_str(text).strip()[:2000], int(template_id)))


async def count_template_use(template_id: int) -> None:
    """Отмечает применение шаблона: так видно, какие реально нужны."""
    await db.run("UPDATE reply_templates SET used_count=used_count+1 WHERE id=?", (int(template_id),))


async def delete_template(template_id: int) -> None:
    await db.run("DELETE FROM reply_templates WHERE id=?", (int(template_id),))


async def templates_count() -> int:
    row = await db.one("SELECT COUNT(*) n FROM reply_templates")
    return row["n"]
