"""Репозиторий разложен по пакетам: проверяем, что публичный слой не пострадал.

Когда `repository.py` разложили на `store/`, главный страх был один: импорт
`import repository as repo` у `bot.py`, всех `handlers/*` и веб-панели должен
продолжить работать без единой правки в них. Поэтому проверяем не строчки
исходника, а поведение интерфейса: те же имена, те же объекты, те же функции.

Список 163 имён снят с монолита командой `git show HEAD:repository.py` и разобран
через `ast` (это чтение, не запись). Пока монолит в git такой же, список здесь не
меняется; если git под рукой есть, тест сверяется с ним ещё раз, чтобы потерянный
при переносе хвост нельзя было протащить молча.
"""
import ast
import importlib
import subprocess
import sys
from pathlib import Path

import repository
import store

ROOT = Path(__file__).resolve().parent.parent
FACADE = ROOT / "repository.py"
PACKAGE = ROOT / "store"

# Снимок того, что было в repository.py до разбиения.
MONOLITH_FUNCTIONS = frozenset("""
   _code _code_forms _contact_filter _duration_ru _group_dict _load_admins
   _parse_day _row_dict _row_value _save_revoked add_group_aliases
   add_internal_note add_staff add_staff_many add_sysadmin add_template
   add_ticket_message admin_ids admin_log admin_log_counts admin_tickets
   admin_today all_admins all_parsed_groups all_settings archive_count
   archive_ticket attempts_count attempts_log audience_ids broadcast_history
   bulk_update clear_admin_fields clear_attempts consent_of contact_full_name
   contact_kind count_template_use create_invite create_staff_request
   create_ticket data_gaps dedupe_stats delete_group delete_invite
   delete_schedule delete_schedule_subscription delete_staff delete_template
   delete_ticket delete_user department_names event_feed_label find_group
   forward_ticket get_admin get_group get_schedule get_staff_request
   get_template get_ticket get_user give_consent grant_sysadmin group_aliases
   group_has_lessons groups invite_state is_owner is_registered
   is_schedule_subscribed is_sysadmin known_groups latest_message_roles
   lessons_count lessons_for_group lessons_for_teacher list_groups
   list_invites list_staff list_sysadmins list_templates list_users
   list_users_count log_action log_broadcast log_ticket_event note_attempt
   on_vacation open_tickets_count people people_count people_overview
   people_without_staff people_without_staff_count reapply_lesson_times
   recent_student_tickets recent_ticket_events rename_group replacement_rule
   resolve_group response_speed restore_ticket revoke_sysadmin
   revoked_sysadmins save_lessons schedule_groups schedule_stamp
   schedule_subscribers search_teachers set_admin_profile set_group_active
   set_invite_ttl set_lesson_time set_schedule_subscription
   set_staff_broadcast set_staff_category set_staff_request_status
   set_staff_see_all set_ticket_ready set_ticket_status set_user_group
   set_user_name set_vacation staff_activity staff_by_role staff_for_category
   staff_load staff_on_vacation staff_requests staff_sees_all
   staff_sees_all_bulk staff_targets stamp_is_fresh stamp_matches_file
   stats_overview status_counts student_open_tickets_count students_by_group
   suggest_groups sysadmin_count teacher_groups templates_count ticket_events
   ticket_thread tickets_by_category tickets_by_day tickets_by_status
   top_groups touch_contact transition_ticket_status update_admin
   update_template_text update_ticket upsert_group upsert_schedule
   upsert_user use_invite user_card users_without_consent vacation_note
   vacation_replacement vacations_bulk
""".split())

# Константы модуля, которыми пользуется веб-панель.
MONOLITH_CONSTANTS = frozenset({
    "ADMIN_FIELDS", "ADMIN_ROLES_SQL", "CONTACT_KINDS", "CONTACT_KIND_LABELS",
    "KIND_TITLES", "STAFF_ROLES_SQL", "TOPIC_SOURCES", "canonical_group",
})


def tree_of(path: Path) -> ast.Module:
    # utf-8-sig: часть файлов проекта сохранена с BOM
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def monolith_names() -> frozenset:
    """Имена из монолита в git; пустое множество, если git недоступен."""
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:repository.py"],
                             capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    found = ast.parse(raw.decode("utf-8-sig")).body
    return frozenset(node.name for node in found
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


def store_modules() -> list:
    return sorted(path for path in PACKAGE.glob("*.py") if path.name != "__init__.py")


def import_edges(path: Path) -> set:
    """Разделы пакета, из которых этот модуль что-то импортирует напрямую."""
    edges = set()
    for node in ast.walk(tree_of(path)):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            edges.add(node.module)
    return edges


# ── публичный слой ─────────────────────────────────────────────────────────────
def test_facade_is_short():
    """Фасад без логики: держать его в голове можно, держать монолит было нельзя."""
    assert len(FACADE.read_text(encoding="utf-8-sig").splitlines()) < 250


def test_facade_defines_nothing():
    """В фасаде нет ни одной функции: разбиение, а не переписывание."""
    defined = [node.name for node in tree_of(FACADE).body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert not defined, "в фасаде появились определения: " + ", ".join(defined)


def test_facade_imports_only_from_store():
    """Единственный источник имён - пакет store, больше фасад ничего не знает."""
    sources = set()
    for node in tree_of(FACADE).body:
        if isinstance(node, ast.Import):
            sources |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            sources.add(node.module or "")
    assert sources == {"store"}, f"фасад импортирует ещё что-то: {sorted(sources)}"


def test_monolith_snapshot_is_still_the_truth():
    """Снимок не должен молча разойтись с git: перенос ничего не потерял."""
    from_git = monolith_names()
    if from_git:
        assert from_git == MONOLITH_FUNCTIONS, (
            "набор функций в git и в тесте разошёлся: нет "
            + ", ".join(sorted(from_git ^ MONOLITH_FUNCTIONS)))


def test_every_function_still_exported():
    """`repo.<имя>` из бота и панели работает - все 163 на месте."""
    missing = sorted(name for name in MONOLITH_FUNCTIONS
                     if not callable(getattr(repository, name, None)))
    assert not missing, "фасад больше не отдаёт: " + ", ".join(missing)
    assert len(MONOLITH_FUNCTIONS) == 163


def test_functions_are_re_exported_not_wrapped():
    """Это именно реэкспорт: подменённая обёртка тихо сломала бы подмены в тестах."""
    wrong = [name for name in MONOLITH_FUNCTIONS
             if getattr(repository, name) is not getattr(store, name)]
    assert not wrong, "фасад подменяет объекты вместо реэкспорта: " + ", ".join(wrong)


def test_package_exports_the_same_interface():
    """`import store` даёт тот же плоский интерфейс, что и `import repository`."""
    missing = sorted(name for name in MONOLITH_FUNCTIONS | MONOLITH_CONSTANTS
                     if not hasattr(store, name))
    assert not missing, "в store нет: " + ", ".join(missing)


def test_constants_stay_available():
    """Панель читает подписи видов контактов прямо из репозитория."""
    people = importlib.import_module("store.people")
    assert repository.CONTACT_KIND_LABELS == people.CONTACT_KIND_LABELS
    assert repository.KIND_TITLES == people.KIND_TITLES
    assert repository.TOPIC_SOURCES is not None


# ── где именно живёт каждая функция ────────────────────────────────────────────
def test_functions_live_in_store_modules():
    """Ни одна функция не осталась в фасаде: всё определено в store/*.py."""
    misplaced = []
    for name in sorted(MONOLITH_FUNCTIONS):
        function = getattr(repository, name)
        module = function.__module__
        if not module.startswith("store."):
            misplaced.append(f"{name} -> {module}")
            continue
        source = ROOT / (module.replace(".", "/") + ".py")
        if not source.exists() or f"def {name}(" not in source.read_text(encoding="utf-8-sig"):
            misplaced.append(f"{name} -> {module} (нет определения в файле)")
    assert not misplaced, "функции живут не в store: " + "; ".join(misplaced)


def test_every_store_module_holds_something():
    """Пустой раздел - это не разбиение, а забытая копия файла."""
    for path in store_modules():
        tree = tree_of(path)
        assert tree.body, f"{path.name} пуст"
        assert ast.get_docstring(tree), f"{path.name} без докстринга: непонятно, что он делает"
        assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for node in tree.body), f"{path.name} не содержит функций"


def test_no_module_imports_the_facade_back():
    """store не должен знать про repository: иначе пакет нельзя импортировать сам."""
    for path in store_modules():
        for node in ast.walk(tree_of(path)):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("repository")
                           for alias in node.names), f"{path.name} импортирует repository"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "repository", f"{path.name} импортирует repository"


# ── граф импортов ──────────────────────────────────────────────────────────────
def test_import_graph_is_acyclic():
    """Цикл между разделами не даст импортировать ни один из них по отдельности."""
    names = {path.stem for path in store_modules()}
    for path in store_modules():
        unknown = import_edges(path) - names
        assert not unknown, f"{path.name} импортирует несуществующий раздел: {sorted(unknown)}"

    def walk(start, path):
        assert start not in path, "цикл импортов: " + " -> ".join(path + [start])
        for step in import_edges(PACKAGE / f"{start}.py"):
            walk(step, path + [start])

    for name in sorted(names):
        walk(name, [])


def test_every_module_imports_standalone():
    """В новом процессе раздел импортируется сам по себе - без заранее загруженных соседей."""
    names = sorted(path.stem for path in store_modules())
    probe = "\n".join([
        "import importlib, sys",
        *[
            f"module = importlib.import_module('store.{name}');"
            f" assert module.__name__ == 'store.{name}'"
            for name in names
        ],
        "assert 'repository' not in sys.modules, 'раздел потянул за собой фасад'",
    ])
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", probe],
                            capture_output=True, cwd=str(ROOT), timeout=120)
    assert result.returncode == 0, (
        "раздел не импортируется сам по себе: " + result.stderr.decode("utf-8", "replace"))


def test_store_does_not_depend_on_the_rest_of_the_project():
    """Раздел тянет только стандартную библиотеку, database, clock, config, utils и соседей."""
    allowed = {"clock", "config", "database", "datetime", "typing", "utils", ""}
    for path in store_modules():
        for node in ast.walk(tree_of(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name in allowed, f"{path.name} импортирует {alias.name}"
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module in allowed, f"{path.name} импортирует {node.module}"
