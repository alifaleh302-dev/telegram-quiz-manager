"""APScheduler wiring: one job per enabled schedule, rebuilt on demand."""

from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.scheduler.jobs import ScheduleRunner, once_datetime, weekdays_to_cron
from app.services.schedule_service import ScheduleService

logger = get_logger(__name__)

JOB_PREFIX = "schedule:"


class SchedulerEngine:
    """Keeps the APScheduler job store in sync with ``schedules.json``."""

    def __init__(self, runner: ScheduleRunner, schedules: ScheduleService) -> None:
        self._runner = runner
        self._schedules = schedules
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
            if self._add_schedule(schedule):
                count += 1
        logger.info("scheduler loaded %s active job(s)", count)
        return count

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
        timezone = schedule.get("timezone") or "UTC"
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
            return IntervalTrigger(minutes=minutes, timezone=timezone)

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
