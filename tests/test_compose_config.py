"""docker-compose.yml и .env.example: конфигурация туннеля не должна разъехаться
с кодом. Тест читает файлы как текст - запускать docker не нужно.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def env_example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def service(name: str) -> str:
    """Текст одного сервиса: от `  имя:` до следующего сервиса или секции."""
    lines = compose().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}:")
    rest = lines[start + 1:]
    end = len(rest)
    for i, line in enumerate(rest):
        if line.strip() and not line.startswith("    "):
            end = i
            break
    return "\n".join(rest[:end])


# ── сервис tunnel ────────────────────────────────────────────────────────────
def test_tunnel_service_exists():
    assert "  tunnel:" in compose(), "в docker-compose.yml нет сервиса tunnel"


def test_tunnel_never_stops_on_its_own():
    block = service("tunnel")
    assert "restart: unless-stopped" in block


def test_tunnel_shares_volume_with_bot():
    """Адрес пишется в том bot_data, который бот читает как /app/data."""
    assert "bot_data:/data" in service("tunnel")
    assert "bot_data:/app/data" in service("bot")
    assert "name: bot-lpc-data" in compose()


def test_tunnel_looks_at_bot_by_service_name():
    block = service("tunnel")
    assert "http://bot:8090" in block
    # ждём готовности бота, а не просто его старта
    assert "depends_on:" in block and "condition: service_healthy" in block


def test_tunnel_writes_url_file():
    block = service("tunnel")
    assert "/data/tunnel_url.txt" in block
    assert "trycloudflare" in block, "адрес должен вылавливаться из лога туннеля"
    assert "/data/tunnel.log" in block, "лог cloudflared нужен, чтобы выловить адрес"


def test_tunnel_can_be_switched_off_by_env():
    """Выключается одной строкой в .env, без правки compose."""
    block = service("tunnel")
    assert "TUNNEL_ENABLED: ${TUNNEL_ENABLED:-1}" in block
    assert "TUNNEL_ENABLED" in block


def test_tunnel_does_not_publish_ports():
    """Наружу торчит только туннель Cloudflare, порт бота наружу не выставляем."""
    assert "ports:" not in service("tunnel")


# ── сервис bot ───────────────────────────────────────────────────────────────
def test_bot_has_timezone_and_tunnel_file():
    block = service("bot")
    assert "TZ: ${TZ:-Europe/Yekaterinburg}" in block
    assert "TUNNEL_URL_FILE: /app/data/tunnel_url.txt" in block
    assert "TUNNEL_ENABLED: ${TUNNEL_ENABLED:-1}" in block


def test_bot_and_tunnel_read_the_same_file():
    """Один файл в томе bot_data: у бота /app/data, у туннеля /data."""
    assert "TUNNEL_URL_FILE: /app/data/tunnel_url.txt" in service("bot")
    assert "/data/tunnel_url.txt" in service("tunnel")


# ── .env.example ─────────────────────────────────────────────────────────────
def test_env_example_documents_tunnel_keys():
    text = env_example()
    for key in ("TZ", "TUNNEL_ENABLED", "TUNNEL_URL_FILE", "CLOUDFLARE_TUNNEL_TOKEN", "PUBLIC_URL"):
        assert f"\n{key}=" in text, f"{key} не описан в .env.example"


def test_env_example_explains_public_url():
    text = env_example()
    assert "PUBLIC_URL=" in text
    assert "приглашени" in text, "надо пояснить, зачем нужен PUBLIC_URL"
    assert "туннел" in text.lower(), "надо сказать, что адрес подхватывается из туннеля"
