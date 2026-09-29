"""Колонка, добавленная позже, должна быть и в COLUMN_UPGRADES.

Нашлась настоящая поломка: в staff_invites добавили position, office и category
только в CREATE TABLE. init_db выполняет ALTER TABLE исключительно по списку
COLUMN_UPGRADES, поэтому на уже созданной боевой базе колонок не появлялось и
любой запрос к ним падал с «no such column». CREATE TABLE IF NOT EXISTS старую
таблицу не обновляет.

Сравнивать со всем SCHEMA нельзя: колонки таблиц, созданных при самой первой
публикации, в COLUMN_UPGRADES не перечисляются и не должны - их база получила
вместе с таблицей. Поэтому базой берётся первая версия database.py из истории
git: разница между ней и сегодняшней и есть «добавленные позже колонки».
"""
import re
import subprocess
from pathlib import Path

import pytest

import database as db

COLUMN_TYPES = ("TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC")
ROOT = Path(__file__).resolve().parent.parent


def columns_of(schema: str) -> dict[str, set[str]]:
    """Колонки каждой таблицы как их видит CREATE TABLE."""
    result: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\)",
                             schema, re.S):
        table, body = match.group(1), match.group(2)
        names = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith(("--", "UNIQUE", "CHECK", "FOREIGN", "PRIMARY")):
                continue
            parts = line.split()
            if parts and parts[0].upper() in COLUMN_TYPES:
                names.add(parts[0])
        result[table] = names
    return result


def first_schema() -> dict[str, set[str]] | None:
    """Схема из самого первого коммита, где database.py вообще появился."""
    try:
        first = subprocess.run(
            ["git", "log", "--reverse", "--pretty=format:%H", "--", "database.py"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.split()
    except OSError:
        return None
    for commit in first:
        blob = subprocess.run(["git", "show", f"{commit}:database.py"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8").stdout
        if "CREATE TABLE IF NOT EXISTS" in blob:
            return columns_of(blob)
    return None


def test_schema_parsed():
    """Схема читается, иначе проверки ниже проверяли бы пустоту."""
    parsed = columns_of(db.SCHEMA)
    assert len(parsed) >= 20, "из SCHEMA прочитано подозрительно мало таблиц"
    assert "tickets" in parsed and "staff_invites" in parsed


def test_invite_columns_really_added_to_upgrades():
    """Конкретная поломка, на которой это и нашлось."""
    upgrades = db.COLUMN_UPGRADES.get("staff_invites", {})
    for column in ("position", "office", "category"):
        assert column in upgrades, (
            f"в COLUMN_UPGRADES нет staff_invites.{column}: на боевой базе колонки "
            "не будет, и приглашения упадут с «no such column»")


def test_no_column_added_later_is_lost():
    """Ни одна колонка, добавленная после первой публикации, не теряется."""
    before = first_schema()
    if before is None:
        pytest.skip("история git недоступна - сверять не с чем")

    now = columns_of(db.SCHEMA)
    lost = []
    for table, columns in now.items():
        added = columns - before.get(table, set())
        covered = set(db.COLUMN_UPGRADES.get(table, {}))
        for column in sorted(added - covered):
            lost.append(f"{table}.{column}")
    assert not lost, (
        "добавленные позже колонки не попали в COLUMN_UPGRADES и потеряются на "
        "уже созданной базе: " + ", ".join(lost))


def test_upgrades_do_not_mention_unknown_tables():
    """В COLUMN_UPGRADES нет таблиц, которых нет в SCHEMA."""
    unknown = sorted(set(db.COLUMN_UPGRADES) - set(columns_of(db.SCHEMA)))
    assert not unknown, "в COLUMN_UPGRADES есть несуществующие таблицы: " + ", ".join(unknown)
