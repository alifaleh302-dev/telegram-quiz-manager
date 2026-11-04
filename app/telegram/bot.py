"""Optional Telegram bot listener.

The dashboard is the primary control surface; the bot only answers a couple of
informational commands so the operator can check status from Telegram itself.
It is skipped entirely when no token is configured (dry-run mode).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class BotManager:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self._application: Any = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._application is not None

    async def start(self) -> None:
        config = get_settings()
        if config.dry_run:
            logger.info("Telegram token not set; bot listener disabled (dry-run mode).")
            return
        try:
            from telegram.ext import Application, CommandHandler
        except ImportError:  # pragma: no cover
            logger.error("python-telegram-bot is not installed; bot listener disabled.")
            return

        application = Application.builder().token(config.telegram_bot_token).build()
        application.add_handler(CommandHandler("start", self._cmd_start))
        application.add_handler(CommandHandler("help", self._cmd_start))
        application.add_handler(CommandHandler("status", self._cmd_status))

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

        self._application = application
        logger.info("Telegram bot listener started.")

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
