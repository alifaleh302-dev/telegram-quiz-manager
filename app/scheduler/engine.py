"""APScheduler wiring: one job per enabled schedule, rebuilt on demand."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.core.timezones import safe_timezone
from app.scheduler.jobs import ScheduleRunner, once_datetime, weekdays_to_cron
from app.services.schedule_service import ScheduleService

logger = get_logger(__name__)

JOB_PREFIX = "schedule:"
ANALYTICS_JOB_ID = "analytics:auto-collect"
# Exam platform: close timed-out attempts and deliver pending notifications.
EXAM_SWEEP_JOB_ID = "exam:sweep-attempts"
NOTIFY_JOB_ID = "exam:dispatch-notifications"


class SchedulerEngine:
    """Keeps the APScheduler job store in sync with the stored schedules."""

    def __init__(
        self,
        runner: ScheduleRunner,
        schedules: ScheduleService,
        analytics: Any | None = None,
        settings: Any | None = None,
        telegram: Any | None = None,
        attempts: Any | None = None,
        notifications: Any | None = None,
    ) -> None:
        self._runner = runner
        self._schedules = schedules
        self._analytics = analytics
        self._settings = settings
        self._telegram = telegram
        self._attempts = attempts
        self._notifications = notifications
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._started = False

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if not self._started:
            self._scheduler.start()
            self._started = True
        self.reload()

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    # --- job management ----------------------------------------------------
    def reload(self) -> int:
        """Drop every job and rebuild from the current schedules."""
        for job in self._scheduler.get_jobs():
            job.remove()

        count = 0
        for schedule in self._schedules.list_schedules():
            try:
                if self._add_schedule(schedule):
                    count += 1
            except Exception:  # noqa: BLE001 - one bad schedule must not stop the rest
                logger.exception(
                    "skipping schedule %s: could not build a trigger", schedule.get("id")
                )
        self._schedule_analytics()
        self._schedule_exam_jobs()
        logger.info("scheduler loaded %s active job(s)", count)
        return count

    def _schedule_analytics(self) -> None:
        """Register (or drop) the periodic result-collection job."""
        if self._analytics is None or self._settings is None or self._telegram is None:
            return
        config = self._settings.get().get("analytics") or {}
        if not config.get("auto_collect"):
            return
        hours = max(1, int(config.get("auto_collect_hours", 6)))
        self._scheduler.add_job(
            self._collect_analytics,
            trigger=IntervalTrigger(hours=hours),
            id=ANALYTICS_JOB_ID,
            name="جمع نتائج التفاعل",
            replace_existing=True,
            max_instances=1,
        )

    async def _collect_analytics(self) -> None:
        try:
            await self._analytics.collect(self._telegram, trigger="auto")
        except Exception:  # noqa: BLE001 - a failing job must not kill the scheduler
            logger.exception("automatic analytics collection failed")

    def _schedule_exam_jobs(self) -> None:
        """Register the exam maintenance jobs (attempt sweep + notifications).

        Both are registered whenever the services exist: the sweep closes timed
        out attempts (so a student is not left "in progress" forever), and the
        dispatch delivers any pending notification. Both are cheap when there is
        nothing to do.
        """
        if self._attempts is not None:
            self._scheduler.add_job(
                self._sweep_attempts,
                trigger=IntervalTrigger(minutes=5),
                id=EXAM_SWEEP_JOB_ID,
                name="إغلاق المحاولات المنتهية",
                replace_existing=True,
                max_instances=1,
            )
        if self._notifications is not None:
            self._scheduler.add_job(
                self._dispatch_notifications,
                trigger=IntervalTrigger(minutes=2),
                id=NOTIFY_JOB_ID,
                name="إرسال تنبيهات الطلاب",
                replace_existing=True,
                max_instances=1,
            )

    async def _sweep_attempts(self) -> None:
        try:
            closed = self._attempts.sweep_expired()
            if closed:
                logger.info("closed %s expired exam attempt(s)", closed)
        except Exception:  # noqa: BLE001 - a failing job must not kill the scheduler
            logger.exception("exam attempt sweep failed")

    async def _dispatch_notifications(self) -> None:
        try:
            summary = await self._notifications.dispatch()
            if summary.get("sent") or summary.get("failed"):
                logger.info("notification dispatch: %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception("notification dispatch failed")

    def _add_schedule(self, schedule: dict[str, Any]) -> bool:
        if not schedule.get("enabled", True):
            return False
        trigger = self._build_trigger(schedule)
        if trigger is None:
            logger.warning("schedule %s has no usable trigger", schedule["id"])
            return False

        self._scheduler.add_job(
            self._run_job,
            trigger=trigger,
            args=[schedule["id"]],
            id=f"{JOB_PREFIX}{schedule['id']}",
            name=schedule.get("name") or schedule["id"],
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
            max_instances=1,
        )
        return True

    def _build_trigger(self, schedule: dict[str, Any]) -> Any:
        # An unknown zone must not crash the scheduler; fall back to UTC.
        raw_zone = schedule.get("timezone")
        timezone = safe_timezone(raw_zone)
        if raw_zone and str(timezone) != raw_zone:
            logger.warning(
                "schedule %s has unknown timezone %r; using UTC",
                schedule.get("id"),
                raw_zone,
            )
        schedule_type = schedule.get("type", "daily")
        times = schedule.get("times") or []

        if schedule_type == "manual":
            return None

        if schedule_type == "once":
            when = once_datetime(schedule)
            if when is None:
                return None
            return DateTrigger(run_date=when, timezone=timezone)

        if schedule_type == "interval":
            minutes = int(schedule.get("interval_minutes") or 0)
            if minutes <= 0:
                return None
            # An optional start time anchors the first firing to the clock (e.g.
            # start at 20:00, then every N minutes) instead of "now + N minutes".
            start_time = str(schedule.get("start_time") or "")
            start_date = schedule.get("start_date") or ""
            start = None
            if start_time:
                try:
                    hour, minute = (int(part) for part in start_time.split(":"))
                except (TypeError, ValueError):
                    hour, minute = 0, 0
                try:
                    day = date.fromisoformat(start_date) if start_date else None
                except ValueError:
                    day = None
                now = datetime.now(timezone)
                base = (
                    datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone)
                    if day
                    else now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                )
                start = base
            return IntervalTrigger(minutes=minutes, start_date=start, timezone=timezone)

        if not times:
            return None

        hours = sorted({int(t.split(":")[0]) for t in times})
        minutes = sorted({int(t.split(":")[1]) for t in times})

        if schedule_type == "weekdays":
            return CronTrigger(
                day_of_week=weekdays_to_cron(schedule.get("weekdays") or []),
                hour=",".join(str(h) for h in hours),
                minute=",".join(str(m) for m in minutes),
                timezone=timezone,
            )

        # "daily" and "times" share the same shape; "times" simply lists more.
        return CronTrigger(
            hour=",".join(str(h) for h in hours),
            minute=",".join(str(m) for m in minutes),
            timezone=timezone,
        )

    async def _run_job(self, schedule_id: str) -> None:
        try:
            result = await self._runner.run(schedule_id)
            logger.debug("schedule %s finished: %s", schedule_id, result)
        except Exception:  # noqa: BLE001 - a failing job must not kill the scheduler
            logger.exception("schedule %s raised", schedule_id)
        finally:
            self._sync_next_run(schedule_id)

    def _sync_next_run(self, schedule_id: str) -> None:
        job = self._scheduler.get_job(f"{JOB_PREFIX}{schedule_id}")
        next_run = getattr(job, "next_run_time", None) if job else None
        self._schedules.set_next_run(
            schedule_id, next_run.isoformat() if next_run else None
        )

    def next_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for job in self._scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            runs.append(
                {
                    "job_id": job.id,
                    "name": job.name,
                    "next_run_at": next_run.isoformat() if next_run else None,
                }
            )
        runs.sort(key=lambda r: r["next_run_at"] or "9999")
        return runs[:limit]

    def next_run_for(self, schedule_id: str) -> str | None:
        job = self._scheduler.get_job(f"{JOB_PREFIX}{schedule_id}")
        next_run = getattr(job, "next_run_time", None) if job else None
        return next_run.isoformat() if next_run else None

    async def trigger_now(self, schedule_id: str) -> dict[str, Any]:
        """Fire a schedule immediately, ignoring its clock but not its rules."""
        return await self._runner.run(schedule_id, trigger="manual-schedule")


def run_async(coro: Any) -> Any:  # pragma: no cover - convenience helper
    return asyncio.get_event_loop().run_until_complete(coro)
