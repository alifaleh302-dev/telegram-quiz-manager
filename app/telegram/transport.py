"""Routes publishing and result collection to the right transport.

Two transports exist:

* **bot** — the Bot API client (``@BotFather`` token).
* **userbot** — a logged-in user account (MTProto, via
  :class:`~app.telegram.userbot_manager.UserbotManager`).

The choice is made per *channel* (``channel["transport"]``), falling back to the
global ``settings["transport"]["default_mode"]``. Both transports expose the
same small interface (``send_quiz``, ``fetch_poll_results``, ``get_member_count``,
``test_channel``, ``get_me``), so the publishing and analytics services call the
router exactly as they used to call the bot client.

The router keeps that interface signature (``fetch_poll_results(chat_id,
message_id)``) and resolves the channel from the ``chat_id``, so no existing
call site has to change. A channel pinned to ``userbot`` with no usable account
raises a :class:`~app.storage.validators.ValidationError` rather than silently
falling back to the bot: publishing through the wrong identity is worse than a
clear error the operator can fix.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.storage.validators import ValidationError
from app.telegram.telegram_client import TelegramClient

logger = get_logger(__name__)

MODE_BOT = "bot"
MODE_USERBOT = "userbot"
TRANSPORT_MODES = (MODE_BOT, MODE_USERBOT)


class TransportRouter:
    """Presents one transport interface over the bot and the userbot pool."""

    def __init__(
        self,
        bot: TelegramClient,
        userbots: Any,
        channels: Any,
        settings_service: Any,
    ) -> None:
        self._bot = bot
        self._userbots = userbots
        self._channels = channels
        self._settings = settings_service

    # --- selection ---------------------------------------------------------
    @property
    def default_mode(self) -> str:
        mode = (self._settings.get().get("transport") or {}).get("default_mode", MODE_BOT)
        return mode if mode in TRANSPORT_MODES else MODE_BOT

    def mode_for_channel(self, channel: dict[str, Any] | None) -> str:
        if channel is None:
            return self.default_mode
        mode = channel.get("transport") or self.default_mode
        return mode if mode in TRANSPORT_MODES else MODE_BOT

    def _channel_by_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        target = str(chat_id)
        return next(
            (c for c in self._channels.list_channels() if str(c.get("chat_id")) == target),
            None,
        )

    def _pick(self, channel: dict[str, Any] | None) -> Any:
        mode = self.mode_for_channel(channel)
        if mode == MODE_USERBOT:
            if not getattr(self._userbots, "available", lambda: False)():
                raise ValidationError(
                    "القناة مضبوطة على userbot لكن مكتبة Pyrogram غير مثبّتة على السيرفر."
                )
            client = self._userbots.resolve_for_channel(channel or {})
            if client is None:
                name = (channel or {}).get("name") or (channel or {}).get("id") or "؟"
                raise ValidationError(
                    f"القناة «{name}» مضبوطة على userbot ولا يوجد حساب مسجّل وجاهز. "
                    "أضف حساباً من الإعدادات أو بدّل نوع النشر إلى بوت."
                )
            return client
        return self._bot

    def _pick_for_chat(self, chat_id: str) -> Any:
        return self._pick(self._channel_by_chat_id(chat_id))

    # --- transport interface ----------------------------------------------
    async def send_quiz(
        self,
        chat_id: str,
        question: dict[str, Any],
        *,
        explanation: bool = True,
        open_period: int = 0,
        anonymous: bool = True,
        channel_id: str = "",
    ) -> Any:
        channel = (
            self._channels.get(channel_id) if channel_id else self._channel_by_chat_id(chat_id)
        )
        client = self._pick(channel)
        result = await client.send_quiz(
            chat_id,
            question,
            explanation=explanation,
            open_period=open_period,
            anonymous=anonymous,
        )
        account_id = getattr(client, "account_id", None)
        if account_id:
            if result.ok:
                self._userbots.record_used(account_id)
            else:
                # Surface the failure on the account so the operator sees which
                # one is broken, instead of only a generic history entry.
                self._userbots.record_error(account_id, result.error or "فشل النشر.")
        return result

    async def fetch_poll_results(self, chat_id: str, message_id: int) -> Any:
        return await self._pick_for_chat(chat_id).fetch_poll_results(chat_id, message_id)

    async def get_member_count(self, chat_id: str) -> int | None:
        return await self._pick_for_chat(chat_id).get_member_count(chat_id)

    async def test_channel(self, chat_id: str, *, channel_id: str = "") -> Any:
        channel = (
            self._channels.get(channel_id) if channel_id else self._channel_by_chat_id(chat_id)
        )
        return await self._pick(channel).test_channel(chat_id)

    async def get_me(self) -> dict[str, Any] | None:
        return await self._bot.get_me()

    async def send_message(
        self, chat_id: str | int, text: str, *, reply_markup: Any = None
    ) -> Any:
        """Send a direct message to a user.

        Notifications always go through the *bot*: a userbot messaging arbitrary
        users would look like spam and risks the account, whereas the bot may
        message anyone who has started a chat with it.
        """
        return await self._bot.send_message(chat_id, text, reply_markup=reply_markup)

    def reset(self) -> None:
        self._bot.reset()

    async def close(self) -> None:
        await self._bot.close()
        await self._userbots.close_all()
