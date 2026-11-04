"""Thin wrapper around the Telegram Bot API used for publishing quizzes.

The client is deliberately small: it knows how to send one quiz poll and how to
classify the result. Everything else (scheduling, retries, history) lives in
the services so the transport can be swapped or faked in tests.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.secret_store import SecretStore

logger = get_logger(__name__)

# "Flood control exceeded. Retry in 32 seconds" — the number Telegram reports is
# the only reliable source; the exception attribute names differ between the Bot
# API and the MTProto forks, so the message is parsed as the fallback.
_RETRY_IN_RE = re.compile(r"retry in (\d+)", re.IGNORECASE)


def retry_after_seconds(exc: Exception) -> int | None:
    """Extract Telegram's requested wait, or ``None`` when it is not a rate limit."""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, timedelta):
        return max(0, int(value.total_seconds()))
    if isinstance(value, (int, float)):
        return max(0, int(value))
    # Pyrogram's FloodWait carries the seconds in ``value``.
    seconds = getattr(exc, "value", None) or getattr(exc, "seconds", None)
    if isinstance(seconds, (int, float)):
        return max(0, int(seconds))
    match = _RETRY_IN_RE.search(str(exc))
    return int(match.group(1)) if match else None


# Errors that are worth retrying: network hiccups and Telegram being busy.
_RETRYABLE = {"TimedOut", "NetworkError", "RetryAfter", "FloodWait"}
# Rate-limit errors: Telegram tells us how long to wait, and the wait must be
# honoured rather than retried immediately (that only extends the ban).
_RATE_LIMIT = {"RetryAfter", "FloodWait"}
# A subset where the request may already have been applied on Telegram's side:
# the outcome is unknown, so a blind retry risks a duplicate post.
_AMBIGUOUS = {"TimedOut", "NetworkError"}
# Errors that will never succeed on retry without operator action.
_PERMANENT = {
    "Forbidden",
    "ChatNotFound",
    "ChatAdminRequired",
    "BadRequest",
    "Unauthorized",
    "InvalidToken",
}


@dataclass
class PublishResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    dry_run: bool = False
    poll_id: str | None = None
    # Seconds Telegram asked us to wait before retrying (flood control). ``None``
    # means "not a rate limit", so the caller can tell it apart from a wait of 0.
    retry_after: int | None = None

    @property
    def status(self) -> str:
        return "success" if self.ok else "failed"

    @property
    def flood_wait(self) -> bool:
        """True when the failure is Telegram throttling us."""
        return self.error_code in _RATE_LIMIT

    @property
    def ambiguous(self) -> bool:
        """The request may have reached Telegram even though we saw an error.

        A timeout or dropped connection can happen *after* the quiz was
        delivered but before we read the reply, so the outcome is unknown:
        retrying blindly could post the same question twice.
        """
        return self.error_code in _AMBIGUOUS


@dataclass
class PollSnapshot:
    """The vote counts of one quiz, as returned by ``stopPoll``."""

    ok: bool
    poll_id: str | None = None
    is_closed: bool = False
    total_voters: int = 0
    options: list[dict[str, Any]] = field(default_factory=list)
    correct_option_ids: list[int] = field(default_factory=list)
    explanation: str = ""
    error: str | None = None
    error_code: str | None = None


class TelegramClient:
    """Sends quiz polls to channels. Falls back to dry-run when unconfigured."""

    def __init__(
        self,
        token: str | None = None,
        *,
        dry_run: bool | None = None,
        secret_store: "SecretStore | None" = None,
    ) -> None:
        self._secret_store = secret_store
        self._fixed_token = token
        self._fixed_dry_run = dry_run
        self._bot: Any = None
        self._bot_token: str | None = None
        self._lock = asyncio.Lock()

    # --- runtime configuration --------------------------------------------
    @property
    def token(self) -> str:
        if self._fixed_token is not None:
            return self._fixed_token.strip()
        if self._secret_store is not None:
            return self._secret_store.bot_token()
        return get_settings().telegram_bot_token.strip()

    @property
    def dry_run(self) -> bool:
        if self._fixed_dry_run is not None:
            return self._fixed_dry_run
        return not self.token

    def reset(self) -> None:
        """Drop the cached Bot so a new token takes effect on the next call."""
        self._bot = None
        self._bot_token = None

    async def _get_bot(self) -> Any:
        from telegram import Bot
        from telegram.request import HTTPXRequest

        token = self.token
        if self._bot is not None and self._bot_token == token:
            return self._bot
        async with self._lock:
            if self._bot is None or self._bot_token != token:
                if self._bot is not None:
                    try:
                        await self._bot.shutdown()
                    except Exception as exc:  # pragma: no cover - best effort
                        logger.debug("bot shutdown failed: %s", exc)
                timeout = get_settings().telegram_timeout
                request = HTTPXRequest(connect_timeout=timeout, read_timeout=timeout)
                self._bot = Bot(token=token, request=request)
                self._bot_token = token
        return self._bot

    async def close(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.shutdown()
            except Exception as exc:  # pragma: no cover - best effort cleanup
                logger.debug("bot shutdown failed: %s", exc)
            self._bot = None
            self._bot_token = None

    # --- publishing --------------------------------------------------------
    async def send_quiz(
        self,
        chat_id: str,
        question: dict[str, Any],
        *,
        explanation: bool = True,
        open_period: int = 0,
        anonymous: bool = True,
    ) -> PublishResult:
        """Send one Single-Choice quiz poll and return the outcome."""
        text = question["question"]
        options = list(question["options"])
        correct = int(question["correct_option_id"])
        explanation_text = question.get("explanation") or ""

        if self.dry_run:
            logger.info(
                "[dry-run] would publish %s to %s: %s",
                question.get("id", "?"),
                chat_id,
                text,
            )
            await asyncio.sleep(0)
            return PublishResult(ok=True, message_id=None, dry_run=True)

        try:
            bot = await self._get_bot()
            message = await bot.send_poll(
                chat_id=chat_id,
                question=text[:300],
                options=options[:10],
                type="quiz",
                correct_option_id=correct,
                is_anonymous=anonymous,
                allows_multiple_answers=False,
                explanation=explanation_text[:200] if (explanation and explanation_text) else None,
                open_period=open_period or None,
            )
            return PublishResult(ok=True, message_id=message.message_id, poll_id=message.poll.id)
        except Exception as exc:  # noqa: BLE001 - telegram raises a wide hierarchy
            return self._classify(exc)

    # --- result collection -------------------------------------------------
    async def fetch_poll_results(self, chat_id: str, message_id: int) -> PollSnapshot:
        """Read the vote counts of a quiz.

        Telegram exposes poll counts only through ``stopPoll``, and that call
        also closes the poll. We deliberately accept that trade-off: results are
        collected on demand (or once the poll is already closed) rather than
        polled continuously, which would freeze every quiz the moment it opens.
        """
        try:
            bot = await self._get_bot()
            poll = await bot.stop_poll(chat_id=chat_id, message_id=message_id)
            options = [
                {"text": option.text, "voter_count": int(option.voter_count)}
                for option in poll.options
            ]
            return PollSnapshot(
                ok=True,
                poll_id=poll.id,
                is_closed=bool(poll.is_closed),
                total_voters=int(poll.total_voter_count),
                options=options,
                correct_option_ids=list(poll.correct_option_ids or []),
                explanation=poll.explanation or "",
            )
        except Exception as exc:  # noqa: BLE001
            return PollSnapshot(ok=False, error=str(exc), error_code=type(exc).__name__)

    async def get_member_count(self, chat_id: str) -> int | None:
        if self.dry_run:
            return None
        try:
            bot = await self._get_bot()
            return int(await bot.get_chat_member_count(chat_id=chat_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_chat_member_count failed for %s: %s", chat_id, exc)
            return None

    async def test_channel(self, chat_id: str) -> PublishResult:
        """Send a harmless message to confirm the bot can publish there."""
        if self.dry_run:
            return PublishResult(ok=True, message_id=None, dry_run=True)
        try:
            bot = await self._get_bot()
            message = await bot.send_message(
                chat_id=chat_id,
                text="✅ اختبار الاتصال: البوت جاهز للنشر في هذه القناة.",
            )
            return PublishResult(ok=True, message_id=message.message_id)
        except Exception as exc:  # noqa: BLE001
            return self._classify(exc)

    async def get_me(self) -> dict[str, Any] | None:
        if self.dry_run:
            return {"username": "dry-run-bot", "id": 0, "dry_run": True}
        try:
            bot = await self._get_bot()
            me = await bot.get_me()
            return {"username": me.username, "id": me.id, "dry_run": False}
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_me failed: %s", exc)
            return None

    @staticmethod
    def _classify(exc: Exception) -> PublishResult:
        name = type(exc).__name__
        text = str(exc)
        retryable = name in _RETRYABLE
        if name in _PERMANENT:
            retryable = False
        elif name not in _RETRYABLE and "timed out" in text.lower():
            retryable = True
        return PublishResult(
            ok=False,
            error=text,
            error_code=name,
            retryable=retryable,
            retry_after=retry_after_seconds(exc),
        )
