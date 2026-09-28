"""Скрипт установки: он должен оставаться рабочим, а не просто лежать в репозитории."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install.ps1"


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


# ── файл существует и читается как PowerShell ───────────────────────────────
def test_script_exists():
    assert SCRIPT.is_file(), "нет install.ps1"


def test_script_has_utf8_bom():
    """Windows PowerShell читает .ps1 как ANSI без BOM - русский текст сломается."""
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_script_parses():
    """Синтаксис должен быть валидным: ошибка разбора делает скрипт бесполезным."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", "$errors = $null; "
                     "[void][System.Management.Automation.Language.Parser]::ParseFile("
                     f"'{SCRIPT}', [ref]$null, [ref]$errors); "
                     "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── что скрипт обещает ──────────────────────────────────────────────────────
def test_script_checks_docker():
    text = source()
    assert "docker version" in text and "docker compose version" in text


def test_script_creates_env_from_example():
    text = source()
    assert ".env.example" in text and "Copy-Item" in text


def test_script_generates_panel_password():
    text = source()
    assert "WEB_PANEL_PASSWORD" in text
    assert "Get-Random" in text or "token_urlsafe" in text


def test_script_asks_for_token_and_sysadmin_id():
    text = source()
    assert "MAX_BOT_TOKEN" in text and "Read-Host" in text
    assert "SYSADMIN_IDS" in text


def test_script_waits_for_health():
    text = source()
    assert "/health" in text and "Invoke-RestMethod" in text


def test_script_prints_panel_addresses():
    text = source()
    assert "/panel" in text
    assert "Get-NetIPAddress" in text          # адрес для телефона, а не только localhost


def test_script_supports_restart_modes():
    text = source()
    for switch in ("-Start", "-SkipBuild", "-NoStart"):
        assert switch in text


def test_script_does_not_print_password_again():
    """Пароль печатается только в момент генерации, не на каждом запуске."""
    text = source()
    lines = text.splitlines()
    printed = [line for line in lines if "пароль панели сгенерирован" in line]
    assert len(printed) == 1
    # и он внутри ветки создания .env, а не в конце скрипта
    assert text.index("Создан .env") < text.index("пароль панели сгенерирован")


# ── скрипт и .env.example согласованы ────────────────────────────────────────
def test_env_example_documents_required_keys():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("MAX_BOT_TOKEN", "SYSADMIN_IDS", "WEB_PANEL_PASSWORD", "ROOT_IDS"):
        assert f"\n{key}=" in example, f"{key} не описан в .env.example"


def test_readme_links_the_script():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "install.ps1" in readme
    assert "## Установка: полная инструкция" in readme
    for step in ("docker compose up -d --build", "/health", "SYSADMIN_IDS"):
        assert step in readme, f"в инструкции нет шага про {step}"
