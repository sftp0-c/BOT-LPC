"""Премиальная оболочка остаётся доступной, локальной и функциональной."""
import json
from pathlib import Path

import pytest

import bot
import charts
import config
import database as db
import panel_theme
import repository as repo
from conftest import login_panel, post_form, msg, click
from web import common


def test_login_has_brand_hero_and_isolated_form(panel_client):
    response = panel_client.get("/panel/login")
    text = response.text
    markup = text.split("</head>", 1)[1].split("<script>", 1)[0]
    assert 'class="login-story"' in markup and 'class="card login-card"' in markup
    assert "Меньше рутины" in markup and "Навигатор ЛПК" in markup
    assert "<nav" not in markup and "/panel/logout" not in markup
    assert 'for="login-id"' in markup and 'for="login-password"' in markup
    assert response.headers["cache-control"] == "no-store"
    assert 'localStorage.getItem' in text.split("</head>", 1)[0]


def test_panel_shell_and_bento_keep_navigation(panel_client):
    assert login_panel(panel_client)
    body = panel_client.get("/panel/").text
    for marker in ('class="app-header"', 'class="app-nav"', 'id="main-content"',
                   'class="dashboard-hero"', 'class="bento"', 'bento-day', 'bento-actions',
                   'bento-audit', 'bento-events', 'aria-current="page"'):
        assert marker in body
    for name in ("Создано", "Взято", "Отвечено", "Закрыто"):
        assert name in body
    assert charts.palette_css(theme_only=True) in body


def test_palette_is_contained_and_has_real_svg_icons():
    script = panel_theme.hotkeys_script(common.NAV_SECTIONS)
    assert 'aria-modal' in script and 'priorFocus' in script
    assert 'RESULT_ICONS' in script and 'ico-people' in script
    assert 'key === "/"' in script
    assert 'requestNo++' in script and 'search.value = ""' in script


def test_phone_keeps_reply_and_logout():
    css = panel_theme.STYLESHEET
    before_print = css.split('@media print{', 1)[0]
    assert '.wb-edit,.wb-reply{display:none}' not in before_print
    assert '.shell-logout .link-button{width:44px;height:44px' in css
    assert '.card:hover{border-color:var(--line-strong)}' in css


@pytest.mark.parametrize('token', ['.', 'текст.ошибка', 'a.b.c', 'x.' + 'z'*43, 'x'*3000])
def test_bad_cookie_never_crashes_panel(panel_client, token):
    assert common._decode_session(token) is None


def test_session_rejects_altered_actor_and_exact_expiry(panel_client, monkeypatch):
    monkeypatch.setattr(common.time, 'time', lambda: 1800000000)
    token = common.start_session('1')
    payload, signature = token.split('.')
    fields = json.loads(common._unb64(payload))
    fields[0] = '46010397'
    tampered = common._b64(json.dumps(fields).encode()) + '.' + signature
    assert common._decode_session(tampered) is None
    expiry = common._decode_session(token)[1]
    monkeypatch.setattr(common.time, 'time', lambda: expiry)
    assert common._decode_session(token) is None


async def test_audit_distinguishes_csrf_denial_validation_and_success(panel_client):
    assert login_panel(panel_client)
    response = panel_client.post('/panel/settings/welcome', data={'value': 'test'}, follow_redirects=False)
    assert response.status_code == 403
    rows = await repo.audit_entries()
    assert rows[0]['action'] == 'settings_welcome' and rows[0]['result'] == 'denied'
    post_form(panel_client, '/panel/templates/add', {'title': '', 'text': ''})
    rows = await repo.audit_entries()
    assert rows[0]['action'] == 'templates_add' and rows[0]['result'] == 'denied'
    post_form(panel_client, '/panel/settings/welcome', {'value': 'секретный текст'})
    rows = await repo.audit_entries()
    assert rows[0]['result'] == 'ok'
    assert 'секретный текст' not in str([dict(row) for row in rows])


async def test_audit_records_server_error_after_handler(panel_client, monkeypatch):
    assert login_panel(panel_client)
    async def broken(*args, **kwargs):
        raise RuntimeError('ошибка с секретом')
    monkeypatch.setattr(db, 'set_setting', broken)
    with pytest.raises(RuntimeError):
        post_form(panel_client, '/panel/settings/welcome', {'value': 'x'})
    rows = await repo.audit_entries()
    assert rows[0]['action'] == 'settings_welcome' and rows[0]['result'] == 'failed'
    assert 'секрет' not in str([dict(row) for row in rows])


async def test_privacy_canaries_never_enter_audit_or_technical_logs(panel_client, env):
    secret = 'PRIVATE-canary-857391'
    await bot.process(msg('100', secret))
    await bot.process(click('100', 'home:' + secret))
    await bot.on_callback('100', secret + ':other')
    await bot.process({'update_type':'bot_started', 'user':{'user_id':100}, 'payload':'inv_' + secret})
    panel_client.post('/panel/login', data={'user_id':secret, 'password':secret}, follow_redirects=False)
    assert login_panel(panel_client)
    post_form(panel_client, '/panel/access/code', {'ttl_hours': '24'})
    invite = (await repo.list_invites(1))[0]['code']
    entries = []
    for table in ('audit_log', 'admin_log'):
        entries.extend(dict(row) for row in await db.many('SELECT * FROM ' + table))
    logs = str(entries)
    technical = Path(config.LOG_FILE).read_text(encoding='utf-8')
    assert secret not in logs and secret not in technical
    assert invite not in logs and invite not in technical


async def test_audit_owner_role_is_not_lost(panel_client, monkeypatch):
    monkeypatch.setattr(config, 'ROOT_IDS', ['46010397'])
    await db.init_db()
    await bot.audit_event('46010397', 'message_received')
    row = (await repo.audit_entries(1))[0]
    assert row['role'] == 'owner' and row['result'] == 'received'


@pytest.mark.parametrize('path', ['/panel/audit?date_from=bad', '/panel/audit.csv?date_from=2026-11-04&date_to=2026-11-03'])
def test_audit_invalid_dates_are_rejected(panel_client, path):
    assert login_panel(panel_client)
    assert panel_client.get(path).status_code == 400
