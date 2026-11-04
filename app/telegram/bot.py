"""Optional Telegram bot listener.

The dashboard is the primary control surface; the bot only answers a couple of
informational commands so the operator can check status from Telegram itself.
It is skipped entirely when no token is configured (dry-run mode), and it can be
restarted from the settings page after the token is set there.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.secret_store import SecretStore

logger = get_logger(__name__)


class BotManager:
    def __init__(
        self, services: dict[str, Any], secret_store: "SecretStore | None" = None
    ) -> None:
        self._services = services
        self._secret_store = secret_store
        self._application: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._token: str = ""

    @property
    def running(self) -> bool:
        return self._application is not None

    def _current_token(self) -> str:
        if self._secret_store is not None:
            return self._secret_store.bot_token()
        from app.core.config import get_settings

        return get_settings().telegram_bot_token.strip()

    async def start(self) -> None:
        from app.core.config import get_settings

        config = get_settings()
        token = self._current_token()
        if not token:
            logger.info("Telegram token not set; bot listener disabled (dry-run mode).")
            return
        try:
            from telegram.ext import Application, CommandHandler
        except ImportError:  # pragma: no cover
            logger.error("python-telegram-bot is not installed; bot listener disabled.")
            return

        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", self._cmd_start))
        application.add_handler(CommandHandler("help", self._cmd_start))
        application.add_handler(CommandHandler("status", self._cmd_status))

        try:
            await application.initialize()
            await application.start()
            if config.telegram_mode == "webhook" and config.telegram_webhook_url:
                await application.bot.set_webhook(
                    url=config.telegram_webhook_url,
                    secret_token=config.telegram_webhook_secret or None,
                )
                logger.info("Telegram webhook registered at %s", config.telegram_webhook_url)
            else:
                self._task = asyncio.create_task(self._poll(application))
        except Exception:  # noqa: BLE001 - a bad token must not crash startup
            logger.exception("Telegram bot listener failed to start.")
            try:
                await application.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass
            return

        self._application = application
        self._token = token
        logger.info("Telegram bot listener started.")

    async def restart(self) -> bool:
        """Stop the listener (if any) and start it again with the current token."""
        await self.stop()
        await self.start()
        return self.running

    async def _poll(self, application: Any) -> None:
        try:
            await application.updater.start_polling(drop_pending_updates=True)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram polling stopped unexpectedly.")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._application is not None:
            try:
                if self._application.updater and self._application.updater.running:
                    await self._application.updater.stop()
                await self._application.stop()
                await self._application.shutdown()
            except Exception:  # noqa: BLE001 - best effort shutdown
                logger.exception("error while shutting the bot listener down")
            self._application = None
        self._token = ""

    # --- commands ----------------------------------------------------------
    async def _cmd_start(self, update: Any, context: Any) -> None:
        text = (
            "🤖 *Telegram Quiz Manager*\n\n"
            "هذا البوت ينشر اختبارات الاختيار من متعدد في القنوات المحددة.\n"
            "التحكم الكامل يتم من لوحة الويب الخاصة بالإدارة.\n\n"
            "الأوامر المتاحة:\n"
            "/status — حالة النظام\n"
            "/help — هذه الرسالة"
        )
        await update.effective_message.reply_text(text, parse_mode="Markdown")

    async def _cmd_status(self, update: Any, context: Any) -> None:
        questions = self._services["questions"].stats()
        publishing = self._services["publishing"].stats()
        channels = self._services["channels"].list_channels()
        enabled = sum(1 for c in channels if c.get("enabled"))
        text = (
            "📊 *حالة النظام*\n\n"
            f"الأسئلة: {questions['total']} (منشور {questions['published']} / "
            f"جاهز {questions['ready']} / مسودة {questions['draft']})\n"
            f"القنوات المفعّلة: {enabled} من {len(channels)}\n"
            f"عمليات النشر: نجاح {publishing['success']} / فشل {publishing['failed']}"
        )
        await update.effective_message.reply_text(text, parse_mode="Markdown")
