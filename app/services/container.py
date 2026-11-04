"""Composition root: builds every service once and shares it via app.state."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.scheduler.engine import SchedulerEngine
from app.scheduler.jobs import ScheduleRunner
from app.services.audit_service import AuditService
from app.services.backup_service import BackupService
from app.services.channel_service import ChannelService
from app.services.publishing_service import PublishingService
from app.services.question_service import QuestionService
from app.services.schedule_service import ScheduleService
from app.services.settings_service import SettingsService
from app.storage.json_manager import Storage
from app.telegram.bot import BotManager
from app.telegram.telegram_client import TelegramClient


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
    backups: BackupService
    runner: ScheduleRunner
    engine: SchedulerEngine
    telegram: TelegramClient
    bot: BotManager

    def as_dict(self) -> dict[str, object]:
        return {
            "questions": self.questions,
            "channels": self.channels,
            "publishing": self.publishing,
        }


def build_services(settings: Settings) -> Services:
    settings.ensure_directories()

    storage = Storage(settings.data_dir, settings.backup_dir)
    audit = AuditService(storage)
    questions = QuestionService(storage, audit)
    channels = ChannelService(storage, audit)
    schedules = ScheduleService(storage, audit, channels)
    app_settings = SettingsService(storage, audit)
    telegram = TelegramClient()
    publishing = PublishingService(
        storage, audit, questions, channels, app_settings, telegram
    )
    backups = BackupService(storage, audit)

    runner = ScheduleRunner(
        questions=questions,
        schedules=schedules,
        publishing=publishing,
        settings=app_settings,
        audit=audit,
    )
    engine = SchedulerEngine(runner, schedules)
    bot = BotManager(
        {"questions": questions, "channels": channels, "publishing": publishing}
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
        backups=backups,
        runner=runner,
        engine=engine,
        telegram=telegram,
        bot=bot,
    )
