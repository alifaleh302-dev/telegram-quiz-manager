#!/usr/bin/env python3
"""Diagnose the Telegram connection: bot identity, reachable chats and channels.

Run it on the server to find out why publishing fails:

    python3 tools/telegram_check.py

It never prints the bot token. For each configured channel it reports whether
the bot can reach it, and it lists any chat the bot has recently seen in its
update queue so the correct ``chat_id`` can be copied into the dashboard.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.audit_service import AuditService  # noqa: E402
from app.services.secret_store import SecretStore  # noqa: E402
from app.storage.json_manager import Storage  # noqa: E402
from app.telegram.telegram_client import TelegramClient  # noqa: E402


async def main() -> int:
    config = get_settings()
    storage = Storage(config.data_dir, config.backup_dir)
    secrets = SecretStore(storage, AuditService(storage))

    status = secrets.status()
    print(f"التوكن: {'مضبوط' if status['configured'] else 'غير مضبوط'}"
          f" (المصدر: {status['source']})")

    client = TelegramClient(secret_store=secrets)
    if client.dry_run:
        print("النظام في الوضع التجريبي؛ لا يوجد توكن فعلي للاختبار.")
        return 1

    try:
        me = await client.get_me()
        print(f"البوت: @{me['username']} (id={me['id']})")

        bot = await client._get_bot()  # noqa: SLF001 - diagnostic tool
        info = await bot.get_webhook_info()
        print(f"Webhook: {info.url or '(غير مضبوط)'} | تحديثات معلّقة: {info.pending_update_count}")

        channels = storage.channels.read()["channels"]
        print(f"\nالقنوات المضبوطة ({len(channels)}):")
        for channel in channels:
            result = await client.test_channel(channel["chat_id"])
            verdict = "متاحة" if result.ok else f"غير متاحة — {result.error_code}: {result.error}"
            print(f"  {channel['id']}  {channel['chat_id']:<24} {verdict}")

        updates = await bot.get_updates(timeout=1)
        seen: dict[int, str] = {}
        for update in updates:
            for attr in ("message", "channel_post", "edited_channel_post", "my_chat_member"):
                obj = getattr(update, attr, None)
                chat = getattr(obj, "chat", None)
                if chat is not None:
                    seen[chat.id] = f"{chat.type}: {chat.title or chat.username or chat.full_name}"
        if seen:
            print("\nمحادثات رصدها البوت (انسخ المعرّف إلى القناة):")
            for chat_id, description in seen.items():
                print(f"  {chat_id}  {description}")
        else:
            print("\nلم يرصد البوت أي محادثة. تأكد من إضافته كمشرف في القناة،"
                  " ثم أرسل أي رسالة في القناة وأعد تشغيل هذا الفحص.")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
