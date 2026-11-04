"""Composition root: builds every service once and shares it via app.state."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.scheduler.engine import SchedulerEngine
from app.scheduler.jobs import ScheduleRunner
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.backup_service import BackupService
from app.services.channel_service import ChannelService
from app.services.publishing_service import PublishingService
from app.services.question_service import QuestionService
from app.services.schedule_service import ScheduleService
from app.services.secret_store import SecretStore
from app.services.settings_service import SettingsService
from app.services.userbot_service import UserbotAccountService
from app.storage.json_manager import Storage
from app.telegram.bot import BotManager
from app.telegram.telegram_client import TelegramClient
from app.telegram.transport import TransportRouter
from app.telegram.userbot_manager import UserbotManager


@dataclass
class Services:
    settings: Settings
    storage: Storage
    audit: AuditService
    questions: QuestionService
    channels: ChannelService
    schedules: ScheduleService
    publishing: PublishingService
    app_settings: SettingsService
    analytics: AnalyticsService
    backups: BackupService
    runner: ScheduleRunner
    engine: SchedulerEngine
    # ``telegram`` is the transport router: it exposes the same interface as the
    # old bot client and dispatches per channel (bot vs userbot).
    telegram: TransportRouter
    bot: BotManager
    secrets: SecretStore
    userbots: UserbotManager
    userbot_accounts: UserbotAccountService

    def as_dict(self) -> dict[str, object]:
        return {
            "questions": self.questions,
            "channels": self.channels,
            "publishing": self.publishing,
        }

    @property
    def dry_run(self) -> bool:
        """True when neither the Bot API nor a userbot account can publish."""
        if self.secrets.bot_token():
            return False
        return not self.userbot_accounts.usable_accounts()


def build_services(settings: Settings) -> Services:
    settings.ensure_directories()

    storage = Storage(settings.data_dir, settings.backup_dir)
    audit = AuditService(storage)
    questions = QuestionService(storage, audit)
    channels = ChannelService(storage, audit)
    schedules = ScheduleService(storage, audit, channels)
    app_settings = SettingsService(storage, audit)
    secrets = SecretStore(storage, audit)
    userbot_accounts = UserbotAccountService(storage, audit, secrets)
    bot_client = TelegramClient(secret_store=secrets)
    userbots = UserbotManager(
        userbot_accounts, secrets, audit, timeout=settings.telegram_timeout
    )
    transport = TransportRouter(bot_client, userbots, channels, app_settings)
    publishing = PublishingService(
        storage, audit, questions, channels, app_settings, transport
    )
    analytics = AnalyticsService(storage, audit, questions, channels)
    backups = BackupService(storage, audit)

    runner = ScheduleRunner(
        questions=questions,
        schedules=schedules,
        publishing=publishing,
        settings=app_settings,
        audit=audit,
    )
    engine = SchedulerEngine(runner, schedules, analytics, app_settings, transport)
    bot = BotManager(
        {
            "questions": questions,
            "channels": channels,
            "publishing": publishing,
            "analytics": analytics,
        },
        secret_store=secrets,
    )

    app_settings.ensure_admin()

    return Services(
        settings=settings,
        storage=storage,
        audit=audit,
        questions=questions,
        channels=channels,
        schedules=schedules,
        publishing=publishing,
        app_settings=app_settings,
        analytics=analytics,
        backups=backups,
        runner=runner,
        engine=engine,
        telegram=transport,
        bot=bot,
        secrets=secrets,
        userbots=userbots,
        userbot_accounts=userbot_accounts,
    )
