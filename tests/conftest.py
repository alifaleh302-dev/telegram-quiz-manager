"""Shared pytest fixtures.

Tests run against a temporary data directory so they never touch real data.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services.container import Services, build_services  # noqa: E402
from app.telegram.telegram_client import PublishResult  # noqa: E402


class FakeTelegramClient:
    """A stand-in for the Telegram transport.

    The real client talks to an external network service, which cannot be
    exercised in tests. This fake implements the same small interface so the
    publishing logic itself (claiming, status transitions, history) is tested
    for real; only the network hop is replaced.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        # ``fail_with`` fails every send; ``transient_failure`` fails the next
        # ``fail_times`` sends then succeeds (used by the flood-retry tests).
        self.fail_with: Exception | None = None
        self.transient_failure: Exception | None = None
        self.fail_times = 0
        self.next_message_id = 1000
        self.dry_run = False
        # ``{message_id: snapshot}`` the fake returns from ``fetch_poll_results``.
        self.poll_snapshots: dict[int, Any] = {}
        self.member_counts: dict[str, int] = {}
        self.fetch_error: Exception | None = None
        self.attempts = 0

    async def send_quiz(
        self, chat_id, question, *, explanation=True, open_period=0, anonymous=True, channel_id=""
    ):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            return self._failure(self.transient_failure)
        if self.fail_with is not None:
            return self._failure(self.fail_with)
        self.next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "question": question,
                "message_id": self.next_message_id,
                "channel_id": channel_id,
            }
        )
        return PublishResult(
            ok=True,
            message_id=self.next_message_id,
            poll_id=f"poll-{self.next_message_id}",
        )

    def _failure(self, exc: Exception | None):
        from app.telegram.telegram_client import TelegramClient

        return TelegramClient._classify(exc if exc is not None else RuntimeError("fake failure"))

    async def fetch_poll_results(self, chat_id, message_id):
        from app.telegram.telegram_client import PollSnapshot

        if self.fetch_error is not None:
            from app.telegram.telegram_client import TelegramClient

            classified = TelegramClient._classify(self.fetch_error)
            return PollSnapshot(
                ok=False, error=classified.error, error_code=classified.error_code
            )
        snapshot = self.poll_snapshots.get(message_id)
        if snapshot is None:
            return PollSnapshot(
                ok=False, error="Poll can't be stopped", error_code="BadRequest"
            )
        return snapshot

    async def get_member_count(self, chat_id):
        return self.member_counts.get(chat_id)

    async def test_channel(self, chat_id):
        return PublishResult(ok=True, message_id=1)

    async def get_me(self):
        return {"username": "test_bot", "id": 42, "dry_run": False}

    async def close(self):
        return None


class RecordingSleeper:
    """Captures pacing/flood waits instead of really sleeping.

    Publishing is paced in production, so without this every burst test would
    spend real seconds in ``asyncio.sleep``. Tests assert on ``waits`` when the
    duration matters.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.waits)


@pytest.fixture
def fake_sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = Settings(
        _env_file=None,
        secret_key="test-secret-key-not-for-production",
        admin_username="admin",
        admin_password="TestPassword123!",
        app_base_dir=str(tmp_path),
        telegram_bot_token="",
    )
    config.ensure_directories()
    return config


@pytest.fixture
def fake_telegram() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def services(
    settings: Settings, fake_telegram: FakeTelegramClient, fake_sleeper: RecordingSleeper
) -> Services:
    built = build_services(settings)
    # Swap the network transport for the fake, keeping every other service real.
    # Publishing goes through the transport router, so we replace the router's
    # *bot* leg (userbot channels are faked per-test where needed).
    built.telegram._bot = fake_telegram  # noqa: SLF001
    # Pacing waits are recorded, not slept, so burst tests stay fast.
    built.publishing._sleep = fake_sleeper  # noqa: SLF001
    return built


@pytest.fixture
def question_payload() -> dict:
    return {
        "question": "Which protocol uses port 53?",
        "options": ["HTTP", "DNS", "FTP", "SSH"],
        "correct_option_id": 1,
        "explanation": "DNS commonly uses port 53.",
        "category": "Networking",
        "difficulty": "easy",
        "status": "ready",
    }
