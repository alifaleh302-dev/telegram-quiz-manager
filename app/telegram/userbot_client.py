"""MTProto userbot transport, backed by Pyrogram.

This is the second publishing transport alongside the Bot API client. It logs
in as a real Telegram *user* account (api_id + api_hash + session string), which
matters when a channel does not let a bot post quizzes, or when several
accounts should share the publishing load.

The public surface deliberately mirrors
:class:`app.telegram.telegram_client.TelegramClient` (``send_quiz``,
``fetch_poll_results``, ``get_member_count``, ``test_channel``, ``get_me``,
``close``), so services can use either transport without knowing which one is
behind the call.

Pyrogram is an optional dependency: the module imports fine without it and
raises a clear error only when a userbot is actually used.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.telegram.telegram_client import PollSnapshot, PublishResult

logger = get_logger(__name__)

try:  # pragma: no cover - exercised only when the extra is installed
    from importlib.util import find_spec

    PYROGRAM_AVAILABLE = find_spec("pyrogram") is not None
except ImportError:  # pragma: no cover
    PYROGRAM_AVAILABLE = False

# Errors worth retrying: Telegram throttling or a transient server problem.
_RETRYABLE = {"FloodWait", "FloodPremiumWait", "SlowmodeWait", "ServiceUnavailable",
              "InternalServerError", "Timeout", "Timedout", "ConnectionError", "OSError"}
# The request may already have been applied: retrying blindly could duplicate.
_AMBIGUOUS = {"Timeout", "Timedout", "ConnectionError", "OSError", "ServiceUnavailable"}
# The session or the account itself is unusable until the operator acts.
_PERMANENT = {
    "AuthKeyUnregistered",
    "AuthKeyInvalid",
    "AuthKeyDuplicated",
    "SessionRevoked",
    "SessionExpired",
    "UserDeactivated",
    "Unauthorized",
    "Forbidden",
    "ChatForbidden",
    "ChatAdminRequired",
    "ChatWriteForbidden",
    "ChannelPrivate",
    "ChannelInvalid",
    "PeerIdInvalid",
    "UserBannedInChannel",
    "ChatSendPlainForbidden",
    "ChatSendMediaForbidden",
}

# Cache the correct kwarg name for the quiz answer once per process: the live
# Pyrogram forks use ``correct_option_ids`` (plural, list) while the archived
# upstream used ``correct_option_id`` (singular, int).
_ANSWER_KWARG: str | None = None


def _answer_kwarg() -> str:
    global _ANSWER_KWARG
    if _ANSWER_KWARG is None:
        import inspect

        from pyrogram import Client

        params = inspect.signature(Client.send_poll).parameters
        _ANSWER_KWARG = "correct_option_ids" if "correct_option_ids" in params else "correct_option_id"
    return _ANSWER_KWARG


def _plain_text(value: Any) -> str:
    """Pyrogram wraps poll text in ``FormattedText``; unwrap it to a string."""
    if value is None:
        return ""
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else str(value)


class UserbotError(RuntimeError):
    """Raised when a userbot cannot be used at all (missing library/credentials)."""


class UserbotClient:
    """One logged-in user account, used as a publishing transport."""

    def __init__(
        self,
        account_id: str,
        *,
        api_id: int | None,
        api_hash: str,
        session: str,
        timeout: float = 30.0,
    ) -> None:
        self.account_id = account_id
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._timeout = timeout
        self._app: Any = None
        self._lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._api_id and self._api_hash and self._session)

    @property
    def session_fingerprint(self) -> str:
        """The session this client was built from, so a rotation can be detected."""
        return self._session

    @property
    def dry_run(self) -> bool:
        return not self.configured

    async def start(self) -> None:
        if not PYROGRAM_AVAILABLE:
            raise UserbotError(
                "مكتبة Pyrogram غير مثبّتة؛ ثبّت الحزمة «kurigram» لتشغيل حسابات userbot."
            )
        if not self.configured:
            raise UserbotError("بيانات الحساب غير مكتملة (api_id/api_hash/جلسة).")
        if self._app is not None:
            return
        from pyrogram import Client

        async with self._lock:
            if self._app is None:
                self._app = Client(
                    name=f"userbot-{self.account_id}",
                    api_id=self._api_id,
                    api_hash=self._api_hash,
                    session_string=self._session,
                    in_memory=True,
                    no_updates=True,  # publishing only; updates come from the bot
                )
                await self._app.start()

    async def close(self) -> None:
        app = self._app
        self._app = None
        if app is not None:
            try:
                await app.stop()
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                logger.debug("userbot %s stop failed: %s", self.account_id, exc)

    def reset(self) -> None:
        """Force a reconnect on the next call (used after a credential change)."""
        self._app = None

    async def _get_app(self) -> Any:
        if self._app is None:
            await self.start()
        return self._app

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
        """Send one Single-Choice quiz poll as the logged-in user."""
        if self.dry_run:
            logger.info(
                "[userbot dry-run] would publish %s to %s", question.get("id", "?"), chat_id
            )
            await asyncio.sleep(0)
            return PublishResult(ok=True, message_id=None, dry_run=True)

        from pyrogram import enums

        text = question["question"][:255]
        options = [str(o)[:100] for o in question["options"]][:10]
        correct = int(question["correct_option_id"])
        explanation_text = (question.get("explanation") or "")[:200]
        kwargs: dict[str, Any] = {
            "chat_id": self._resolve(chat_id),
            "question": text,
            "options": options,
            "is_anonymous": anonymous,
            "type": enums.PollType.QUIZ,
            "allows_multiple_answers": False,
        }
        if _answer_kwarg() == "correct_option_ids":
            kwargs["correct_option_ids"] = [correct]
        else:  # pragma: no cover - only on the archived upstream release
            kwargs["correct_option_id"] = correct
        if explanation and explanation_text:
            kwargs["explanation"] = explanation_text
        if open_period:
            kwargs["open_period"] = max(5, int(open_period))

        try:
            app = await self._get_app()
            message = await app.send_poll(**kwargs)
            poll = getattr(message, "poll", None)
            return PublishResult(
                ok=True,
                message_id=getattr(message, "id", None),
                poll_id=str(getattr(poll, "id", "") or "") or None,
            )
        except Exception as exc:  # noqa: BLE001 - pyrogram raises a wide hierarchy
            return self._classify(exc)

    async def fetch_poll_results(self, chat_id: str, message_id: int) -> PollSnapshot:
        """Read a quiz's vote counts without closing it.

        Unlike the Bot API (where counts only come from ``stopPoll``), a user
        account can read the poll attached to a message, so collection here is
        read-only and the poll stays open for voters.
        """
        try:
            app = await self._get_app()
            message = await app.get_messages(self._resolve(chat_id), message_ids=message_id)
            poll = getattr(message, "poll", None)
            if poll is None:
                return PollSnapshot(
                    ok=False, error="لا يوجد استطلاع في هذه الرسالة.", error_code="BadRequest"
                )
            correct = getattr(poll, "correct_option_ids", None)
            if correct is None:
                single = getattr(poll, "correct_option_id", None)
                correct = [single] if single is not None else []
            return PollSnapshot(
                ok=True,
                poll_id=str(getattr(poll, "id", "") or ""),
                is_closed=bool(getattr(poll, "is_closed", False)),
                total_voters=int(getattr(poll, "total_voter_count", 0) or 0),
                options=[
                    {
                        "text": _plain_text(getattr(option, "text", "")),
                        "voter_count": int(getattr(option, "voter_count", 0) or 0),
                    }
                    for option in (getattr(poll, "options", None) or [])
                ],
                correct_option_ids=[int(i) for i in (correct or [])],
                explanation=_plain_text(getattr(poll, "explanation", "")),
            )
        except Exception as exc:  # noqa: BLE001
            classified = self._classify(exc)
            return PollSnapshot(
                ok=False, error=classified.error, error_code=classified.error_code
            )

    async def get_member_count(self, chat_id: str) -> int | None:
        if self.dry_run:
            return None
        try:
            app = await self._get_app()
            return int(await app.get_chat_members_count(self._resolve(chat_id)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("userbot get_chat_members_count failed for %s: %s", chat_id, exc)
            return None

    async def test_channel(self, chat_id: str) -> PublishResult:
        """Send a harmless message to confirm this account can publish there."""
        if self.dry_run:
            return PublishResult(ok=True, message_id=None, dry_run=True)
        try:
            app = await self._get_app()
            message = await app.send_message(
                self._resolve(chat_id),
                "✅ اختبار الاتصال: حساب userbot جاهز للنشر في هذه القناة.",
            )
            return PublishResult(ok=True, message_id=getattr(message, "id", None))
        except Exception as exc:  # noqa: BLE001
            return self._classify(exc)

    async def get_me(self) -> dict[str, Any] | None:
        if self.dry_run:
            return {"username": "userbot-dry-run", "id": 0, "dry_run": True}
        try:
            app = await self._get_app()
            me = await app.get_me()
            return {
                "username": getattr(me, "username", None),
                "id": getattr(me, "id", None),
                "first_name": getattr(me, "first_name", None),
                "dry_run": False,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("userbot get_me failed: %s", exc)
            return None

    @staticmethod
    def _resolve(chat_id: str) -> str | int:
        """Pyrogram accepts ``@name`` directly; numeric ids are passed as ints."""
        text = str(chat_id).strip()
        if text.lstrip("-").isdigit():
            return int(text)
        return text

    @staticmethod
    def _classify(exc: Exception) -> PublishResult:
        name = type(exc).__name__
        text = str(exc)
        retryable = name in _RETRYABLE
        if name in _PERMANENT:
            retryable = False
        elif name not in _RETRYABLE and any(
            hint in text.lower() for hint in ("timed out", "timeout", "connection", "reset")
        ):
            retryable = True
        return PublishResult(ok=False, error=text, error_code=name, retryable=retryable)
