"""Bot token management from the dashboard: encrypted storage and endpoints."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.secret_store import SecretStore, validate_bot_token  # noqa: E402
from app.storage.validators import ValidationError  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "TestPassword123!"
FAKE_TOKEN = "123456789:AAFakeTokenForTestingPurposes12345"


# --- validation ------------------------------------------------------------

def test_validate_bot_token_accepts_well_formed_token() -> None:
    assert validate_bot_token(f"  {FAKE_TOKEN}  ") == FAKE_TOKEN


@pytest.mark.parametrize("bad", ["", "short", "no-colon-token-value-here", "12 34:abc"])
def test_validate_bot_token_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate_bot_token(bad)


# --- encrypted storage -----------------------------------------------------

def test_set_bot_token_encrypts_at_rest(services) -> None:
    store: SecretStore = services.secrets
    store.set_bot_token(FAKE_TOKEN)

    # The token must never be stored in clear anywhere in the database file.
    raw = services.storage.db.path.read_bytes()
    assert FAKE_TOKEN.encode("utf-8") not in raw, "the token must never be stored in clear"

    document = services.storage.secrets.read()
    assert document["telegram_bot_token"], "an encrypted value should be present"

    assert store.bot_token() == FAKE_TOKEN
    assert store.source() == "stored"


def test_status_masks_the_token(services) -> None:
    services.secrets.set_bot_token(FAKE_TOKEN)
    status = services.secrets.status()
    assert status["configured"] is True
    assert status["masked"].endswith(FAKE_TOKEN[-4:])
    assert FAKE_TOKEN not in status["masked"]


def test_clear_bot_token_falls_back_to_env(services, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:EnvTokenForTesting1234567890abcd")
    get_settings.cache_clear()
    services.secrets.set_bot_token(FAKE_TOKEN)
    assert services.secrets.source() == "stored"

    services.secrets.clear_bot_token()
    assert services.secrets.bot_token() == "999:EnvTokenForTesting1234567890abcd"
    assert services.secrets.source() == "env"
    get_settings.cache_clear()


def test_dry_run_reflects_stored_token(services) -> None:
    from app.telegram.telegram_client import TelegramClient

    client = TelegramClient(secret_store=services.secrets)
    assert services.dry_run is True
    assert client.dry_run is True

    services.secrets.set_bot_token(FAKE_TOKEN)
    assert services.dry_run is False
    assert client.dry_run is False
    assert client.token == FAKE_TOKEN


# --- API endpoints ---------------------------------------------------------

@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-token-tests")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()

    from app.main import create_app

    application = create_app()
    with TestClient(application) as client:
        # Keep the bot listener out of the network during tests.
        async def _no_restart() -> bool:
            return False

        application.state.services.bot.restart = _no_restart  # type: ignore[assignment]
        yield client
    get_settings.cache_clear()


def _login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("tqm_session")


def _csrf(client: TestClient) -> str:
    from app.core.security import load_session

    return load_session(client.cookies.get("tqm_session")).csrf_token


def test_token_endpoints_require_auth(app_client) -> None:
    assert app_client.get("/api/settings/telegram-token").status_code == 401
    assert app_client.post("/api/settings/telegram-token", json={"token": "x"}).status_code == 401


def test_token_endpoints_require_csrf(app_client) -> None:
    _login(app_client)
    response = app_client.post("/api/settings/telegram-token", json={"token": FAKE_TOKEN})
    assert response.status_code == 403


def test_set_get_and_clear_token_via_api(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}

    created = app_client.post(
        "/api/settings/telegram-token", json={"token": FAKE_TOKEN}, headers=headers
    )
    assert created.status_code == 200, created.text
    assert created.json()["source"] == "stored"
    assert FAKE_TOKEN not in created.text, "the raw token must not be echoed back"

    status = app_client.get("/api/settings/telegram-token").json()
    assert status["configured"] is True
    assert status["masked"].endswith(FAKE_TOKEN[-4:])

    cleared = app_client.delete("/api/settings/telegram-token", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_settings_page_shows_token_panel(app_client) -> None:
    _login(app_client)
    page = app_client.get("/settings")
    assert page.status_code == 200
    assert 'id="tokenForm"' in page.text
