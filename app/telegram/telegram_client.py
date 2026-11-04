"""Thin wrapper around the Telegram Bot API used for publishing quizzes.

The client is deliberately small: it knows how to send one quiz poll and how to
classify the result. Everything else (scheduling, retries, history) lives in
the services so the transport can be swapped or faked in tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Errors that are worth retrying: network hiccups and Telegram being busy.
_RETRYABLE = {"TimedOut", "NetworkError", "RetryAfter", "FloodWait"}
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

    @property
    def status(self) -> str:
        return "success" if self.ok else "failed"


class TelegramClient:
    """Sends quiz polls to channels. Falls back to dry-run when unconfigured."""

    def __init__(self, token: str | None = None, *, dry_run: bool | None = None) -> None:
        config = get_settings()
        self.token = (token if token is not None else config.telegram_bot_token).strip()
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self._bot: Any = None
        self._lock = asyncio.Lock()

    async def _get_bot(self) -> Any:
        if self._bot is not None:
            return self._bot
        from telegram import Bot

        async with self._lock:
            if self._bot is None:
                self._bot = Bot(token=self.token)
        return self._bot

    async def close(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.shutdown()
            except Exception as exc:  # pragma: no cover - best effort cleanup
                logger.debug("bot shutdown failed: %s", exc)
            self._bot = None

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
            return PublishResult(ok=True, message_id=message.message_id)
        except Exception as exc:  # noqa: BLE001 - telegram raises a wide hierarchy
            return self._classify(exc)

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
        )
