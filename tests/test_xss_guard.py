# -*- coding: utf-8 -*-
"""Статический AST-контроль безопасности и защита от XSS в web/ панели.

Проверяет, что все динамические подстановки в HTML f-строках экранируются (esc/quote/_esc),
и что ни одна сырая колонка из базы (row[...], hit[...], data[...]) не попадает в HTML напрямую.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SAFE_NUMERIC_KEYS = {
    'tickets', 'messages', 'events', 'page_count', 'freelist_count',
    'open', 'sent', 'failed', 'count', 'total', 'days'
}


def _is_html_fstring(node: ast.JoinedStr) -> bool:
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            if '<' in part.value and '>' in part.value:
                return True
    return False


def _check_file_for_unsafe_subscripts(filepath: Path):
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        src = f.read()
    tree = ast.parse(src, filename=str(filepath))

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or not _is_html_fstring(node):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            val = part.value
            if isinstance(val, ast.Subscript):
                base_obj = ast.unparse(val.value)
                if base_obj in {'row', 'hit', 't', 'card', 'person', 'data', 'admin', 'item', 'c'}:
                    key = ast.unparse(val.slice).strip("\"'")
                    if key not in SAFE_NUMERIC_KEYS:
                        violations.append((filepath.name, getattr(node, 'lineno', 0), ast.unparse(val)))
    return violations


def test_no_unescaped_row_subscripts_in_web_modules():
    """В f-строках с HTML запрещены неэкранированные обращения к полям строк БД."""
    all_violations = []
    for file_path in sorted(ROOT.glob('web/*.py')):
        violations = _check_file_for_unsafe_subscripts(file_path)
        all_violations.extend(violations)
    assert not all_violations, f'Найдены неэкранированные подстановки из базы в HTML: {all_violations}'


def test_core_web_modules_xss_protection():
    """Особое внимание: web/data.py, web/people.py, web/overview.py, web/workspace_view.py."""
    targets = ['web/data.py', 'web/people.py', 'web/overview.py', 'web/workspace_view.py']
    for rel_path in targets:
        filepath = ROOT / rel_path
        assert filepath.exists(), f'{rel_path} не найден'
        violations = _check_file_for_unsafe_subscripts(filepath)
        assert not violations, f'Неэкранированные поля в {rel_path}: {violations}'


def test_data_search_hits_escapes_xss():
    """Проверяет, что результаты поиска в данных экранируют XSS."""
    from web.data import _search_hits

    payload = '<script>alert(1)</script>" onerror="alert(2)'
    hits = [{
        'table': 'users',
        'rowid': 123,
        'column': 'full_name',
        'value': payload,
        'skipped': False,
    }]
    html = _search_hits(hits, payload)
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'alert(1)' in html


def test_workspace_view_escapes_xss_payloads():
    """Проверяет экранирование в компонентах рабочего места сотрудника."""
    from web.workspace_view import _hidden, _textarea, _submit

    payload = '<script>alert("xss")</script>" onfocus="bad()'
    hidden_html = _hidden('field_name', payload)
    assert '<script>' not in hidden_html
    assert '&lt;script&gt;' in hidden_html

    textarea_html = _textarea({'drafts': {'reply': payload}}, 'reply', 'Ответ', 'Подсказка')
    assert '<script>' not in textarea_html
    assert '&lt;script&gt;' in textarea_html

    submit_html = _submit('action_name', payload)
    assert '<script>' not in submit_html
    assert '&lt;script&gt;' in submit_html


def test_no_direct_sql_in_web_modules():
    """Проверяет, что web/data.py, web/directory.py, web/tickets.py, web/access.py не содержат прямых SQL-вызовов db.*."""
    for rel_path in ['web/data.py', 'web/directory.py', 'web/tickets.py', 'web/access.py']:
        filepath = ROOT / rel_path
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            src = f.read()
        tree = ast.parse(src, filename=str(filepath))
        db_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == 'db':
                    db_calls.append((node.lineno, node.func.attr))
        assert not db_calls, f'Прямые вызовы db.* найдены в {rel_path}: {db_calls}'
