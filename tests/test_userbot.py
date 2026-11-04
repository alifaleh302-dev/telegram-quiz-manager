"""Userbot accounts, credential encryption, transport routing and login flow.

The MTProto client itself is never exercised (it would need real Telegram
credentials); the userbot *client* is faked at the transport boundary, so the
routing, encryption and lifecycle logic under test is all real code.
"""

from __future__ import annotations

import pytest

from app.services.userbot_service import STATUS_ACTIVE, STATUS_PENDING
from app.storage.validators import ValidationError
from app.telegram.telegram_client import PollSnapshot, PublishResult
from app.telegram.transport import MODE_BOT, MODE_USERBOT


@pytest.fixture
def account(services):
    return services.userbot_accounts.create(
        {
            "name": "Publishing account",
            "api_id": "1234567",
            "api_hash": "0123456789abcdef0123456789abcdef",
            "phone": "+966500000000",
        }
    )


# --- account CRUD + credentials -------------------------------------------

def test_create_account_encrypts_credentials(services, account) -> None:
    raw = services.storage.secrets.read()
    bucket = raw["userbot"][account["id"]]
    # Nothing is stored in clear text, and the plaintext never appears.
    assert "1234567" not in bucket["api_id"]
    assert "0123456789abcdef0123456789abcdef" not in bucket["api_hash"]

    creds = services.secrets.userbot_credentials(account["id"])
    assert creds["api_id"] == 1234567
    assert creds["api_hash"] == "0123456789abcdef0123456789abcdef"
    assert creds["session"] == ""


def test_account_metadata_file_has_no_secrets(services, account) -> None:
    document = services.storage.userbot_accounts.read()
    assert account["id"] in [a["id"] for a in document["accounts"]]
    serialized = str(document)
    assert "0123456789abcdef0123456789abcdef" not in serialized


def test_create_rejects_bad_api_id_and_hash(services) -> None:
    with pytest.raises(ValidationError, match="api_id"):
        services.userbot_accounts.create(
            {"name": "x", "api_id": "abc", "api_hash": "0123456789abcdef0123456789abcdef"}
        )
    with pytest.raises(ValidationError, match="api_hash"):
        services.userbot_accounts.create({"name": "x", "api_id": "123", "api_hash": "short"})


def test_status_is_masked(services, account) -> None:
    view = services.userbot_accounts.view(services.userbot_accounts.get(account["id"]))
    assert view["credentials"]["api_id"] == "1234567"
    assert view["credentials"]["api_hash_masked"].startswith("•")
    assert "0123456789abcdef" not in view["credentials"]["api_hash_masked"]


def test_mark_logged_in_activates_account(services, account) -> None:
    assert services.userbot_accounts.get(account["id"])["status"] == STATUS_PENDING
    updated = services.userbot_accounts.mark_logged_in(
        account["id"], session="SESSION-STRING", username="publisher", telegram_id=42
    )
    assert updated["status"] == STATUS_ACTIVE
    assert updated["username"] == "publisher"
    assert services.secrets.userbot_credentials(account["id"])["session"] == "SESSION-STRING"


def test_first_account_is_default_and_delete_reassigns(services) -> None:
    first = services.userbot_accounts.create(
        {"name": "A", "api_id": "1", "api_hash": "a" * 32}
    )
    second = services.userbot_accounts.create(
        {"name": "B", "api_id": "2", "api_hash": "b" * 32}
    )
    assert services.userbot_accounts.get(first["id"])["default"] is True
    assert services.userbot_accounts.get(second["id"])["default"] is False

    services.userbot_accounts.delete(first["id"])
    assert services.userbot_accounts.get(second["id"])["default"] is True
    # Deleting an account also removes its encrypted credentials.
    assert services.storage.secrets.read()["userbot"].get(first["id"]) is None


def test_usable_accounts_requires_login(services, account) -> None:
    assert services.userbot_accounts.usable_accounts() == []
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    assert len(services.userbot_accounts.usable_accounts()) == 1


# --- transport routing -----------------------------------------------------

class FakeUserbotClient:
    """Stands in for :class:`UserbotClient` at the transport boundary."""

    def __init__(self, account_id: str, *, session: str = "") -> None:
        self.account_id = account_id
        self.session_fingerprint = session
        self.dry_run = False
        self.sent: list[dict] = []
        self.message_id = 5000
        self.fail = False

    def reset(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send_quiz(self, chat_id, question, **kwargs):
        if self.fail:
            return PublishResult(ok=False, error="Forbidden: not enough rights", error_code="Forbidden")
        self.message_id += 1
        self.sent.append({"chat_id": chat_id, "question": question})
        return PublishResult(ok=True, message_id=self.message_id, poll_id=f"poll-{self.message_id}")

    async def fetch_poll_results(self, chat_id, message_id):
        return PollSnapshot(ok=True, poll_id="p1", total_voters=3, options=[])

    async def get_member_count(self, chat_id):
        return 10

    async def test_channel(self, chat_id):
        return PublishResult(ok=True, message_id=1)

    async def get_me(self):
        return {"username": "userbot", "id": 1, "dry_run": False}


def test_router_uses_bot_for_bot_channel(services, fake_telegram) -> None:
    channel = services.channels.create({"name": "Bot channel", "chat_id": "@bot_chan"})
    assert services.telegram.mode_for_channel(channel) == MODE_BOT


def test_router_requires_an_account_for_userbot_channel(services) -> None:
    channel = services.channels.create(
        {"name": "UB channel", "chat_id": "@ub_chan", "transport": MODE_USERBOT}
    )
    # A userbot channel with no usable account is a clear error, not a silent
    # fall back to the bot identity.
    with pytest.raises(ValidationError, match="userbot"):
        services.telegram._pick(channel)  # noqa: SLF001


def test_router_resolves_pinned_account(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    channel = services.channels.create(
        {
            "name": "UB channel",
            "chat_id": "@ub_chan",
            "transport": MODE_USERBOT,
            "userbot_account_id": account["id"],
        }
    )
    client = services.telegram._pick(channel)  # noqa: SLF001
    assert client is not None
    assert client.account_id == account["id"]


async def test_publish_through_userbot_records_success(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    fake = FakeUserbotClient(account["id"], session="S")
    services.userbots._clients[account["id"]] = fake  # noqa: SLF001

    channel = services.channels.create(
        {
            "name": "UB channel",
            "chat_id": "@ub_chan",
            "transport": MODE_USERBOT,
            "userbot_account_id": account["id"],
        }
    )
    question = services.questions.create(
        {
            "question": "Q?",
            "options": ["a", "b"],
            "correct_option_id": 0,
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    assert entry["status"] == "success"
    assert entry["message_id"] == fake.message_id
    assert fake.sent[0]["chat_id"] == "@ub_chan"
    assert fake.sent[0]["question"]["id"] == question["id"]
    # The account is stamped as used.
    assert services.userbot_accounts.get(account["id"])["last_used_at"] is not None


async def test_publish_failure_is_recorded_on_the_account(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    fake = FakeUserbotClient(account["id"], session="S")
    fake.fail = True
    services.userbots._clients[account["id"]] = fake  # noqa: SLF001

    channel = services.channels.create(
        {
            "name": "UB channel",
            "chat_id": "@ub_chan",
            "transport": MODE_USERBOT,
            "userbot_account_id": account["id"],
        }
    )
    question = services.questions.create(
        {"question": "Q?", "options": ["a", "b"], "correct_option_id": 0, "status": "ready"}
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    assert entry["status"] == "failed"
    # The broken account is flagged so the operator can see which one failed.
    assert services.userbot_accounts.get(account["id"])["last_error"]


def test_resolve_for_channel_falls_back_to_default(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    channel = services.channels.create(
        {"name": "UB channel", "chat_id": "@ub_chan", "transport": MODE_USERBOT}
    )
    client = services.telegram._pick(channel)  # noqa: SLF001
    assert client.account_id == account["id"]


def test_default_transport_setting_is_honoured(services) -> None:
    services.app_settings.update({"transport": {"default_mode": MODE_USERBOT}})
    assert services.telegram.default_mode == MODE_USERBOT
    # A channel without an explicit transport inherits the default.
    assert services.telegram.mode_for_channel({"id": "CH1"}) == MODE_USERBOT


def test_settings_reject_unknown_transport_mode(services) -> None:
    with pytest.raises(ValidationError, match="bot"):
        services.app_settings.update({"transport": {"default_mode": "carrier-pigeon"}})


def test_invalidate_drops_cached_client(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    fake = FakeUserbotClient(account["id"])
    services.userbots._clients[account["id"]] = fake  # noqa: SLF001
    services.userbots.invalidate(account["id"])
    assert account["id"] not in services.userbots._clients  # noqa: SLF001


# --- login flow ------------------------------------------------------------

async def test_start_login_without_credentials_is_rejected(services) -> None:
    empty = services.userbot_accounts.create({"name": "No creds", "api_id": "9", "api_hash": "c" * 32})
    services.secrets.clear_userbot_credentials(empty["id"])
    with pytest.raises(ValidationError, match="api_id"):
        await services.userbots.start_login(empty["id"], "+966500000001")


async def test_complete_login_requires_a_started_flow(services, account) -> None:
    with pytest.raises(ValidationError, match="لم تبدأ"):
        await services.userbots.complete_login(account["id"], code="12345")


class _FakeSentCode:
    phone_code_hash = "hash-123"


class _FakeMe:
    username = "publisher"
    id = 987654321


class _FakePyrogramClient:
    """Minimal stand-in for ``pyrogram.Client`` covering the 2-step login."""

    needs_password = False

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.signed_in = None
        self.checked_password = None
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def send_code(self, phone: str):
        self.phone = phone
        return _FakeSentCode()

    async def sign_in(self, phone: str, phone_code_hash: str, code: str):
        if self.needs_password:
            from pyrogram.errors import SessionPasswordNeeded

            raise SessionPasswordNeeded()
        self.signed_in = (phone, phone_code_hash, code)

    async def check_password(self, password: str) -> None:
        self.checked_password = password

    async def export_session_string(self) -> str:
        return "EXPORTED-SESSION"

    async def get_me(self):
        return _FakeMe()

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def fake_pyrogram(monkeypatch):
    import pyrogram

    created: list[_FakePyrogramClient] = []

    def _factory(**kwargs):
        client = _FakePyrogramClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(pyrogram, "Client", _factory)
    return created


async def test_full_login_flow_activates_account(services, account, fake_pyrogram) -> None:
    started = await services.userbots.start_login(account["id"], "+966500000000")
    assert started["step"] == "code"
    assert services.userbots.pending_accounts() == [account["id"]]

    result = await services.userbots.complete_login(
        account["id"], code="54321", actor="admin"
    )
    assert result["step"] == "done"
    assert result["account"]["status"] == STATUS_ACTIVE
    assert result["account"]["username"] == "publisher"
    assert result["account"]["telegram_id"] == 987654321

    # The session was persisted encrypted and the login flow cleaned up.
    assert services.secrets.userbot_credentials(account["id"])["session"] == "EXPORTED-SESSION"
    assert services.userbots.pending_accounts() == []
    assert fake_pyrogram[0].disconnected is True


async def test_login_flow_handles_two_factor_password(services, account, fake_pyrogram) -> None:
    await services.userbots.start_login(account["id"], "+966500000000")
    fake_pyrogram[0].needs_password = True

    first = await services.userbots.complete_login(account["id"], code="54321")
    assert first["step"] == "password"
    # Still pending: the flow is not done until the password is accepted.
    assert services.userbots.pending_accounts() == [account["id"]]

    second = await services.userbots.complete_login(
        account["id"], code="54321", password="s3cret"
    )
    assert second["step"] == "done"
    assert fake_pyrogram[0].checked_password == "s3cret"
    assert services.userbot_accounts.get(account["id"])["status"] == STATUS_ACTIVE


async def test_cancel_login_discards_pending(services, account, fake_pyrogram) -> None:
    await services.userbots.start_login(account["id"], "+966500000000")
    await services.userbots.cancel_login(account["id"])
    assert services.userbots.pending_accounts() == []
    assert fake_pyrogram[0].disconnected is True


async def test_start_login_maps_telegram_errors(services, account, fake_pyrogram, monkeypatch) -> None:
    async def _boom(phone):
        from pyrogram.errors import PhoneNumberInvalid

        raise PhoneNumberInvalid()

    monkeypatch.setattr(_FakePyrogramClient, "send_code", staticmethod(_boom))
    with pytest.raises(ValidationError, match="رقم الهاتف"):
        await services.userbots.start_login(account["id"], "+966000000000")


# --- client construction ---------------------------------------------------

def test_client_for_returns_none_without_session(services, account) -> None:
    assert services.userbots.client_for(account["id"]) is None


def test_client_for_builds_and_caches(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S", username="u")
    first = services.userbots.client_for(account["id"])
    second = services.userbots.client_for(account["id"])
    assert first is not None
    assert first is second  # cached between calls


def test_client_rebuilt_when_session_changes(services, account) -> None:
    services.userbot_accounts.mark_logged_in(account["id"], session="S1", username="u")
    first = services.userbots.client_for(account["id"])
    services.secrets.save_userbot_session(account["id"], "S2")
    second = services.userbots.client_for(account["id"])
    assert first is not second


def test_userbot_error_classification() -> None:
    from app.telegram.userbot_client import UserbotClient

    class FloodWait(Exception):
        pass

    class Forbidden(Exception):
        pass

    retryable = UserbotClient._classify(FloodWait("wait"))  # noqa: SLF001
    assert retryable.retryable is True

    permanent = UserbotClient._classify(Forbidden("no"))  # noqa: SLF001
    assert permanent.retryable is False
    assert permanent.ok is False


def test_account_service_requires_name(services) -> None:
    with pytest.raises(ValidationError, match="اسم"):
        services.userbot_accounts.create({"api_id": "1", "api_hash": "a" * 32})
