"""The logic executed when a schedule fires.

Kept separate from the APScheduler wiring so it can be unit-tested by calling
:func:`run_schedule` directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.core.logging import get_logger
from app.services.audit_service import AuditService
from app.services.publishing_service import PublishingService
from app.services.question_service import QuestionService
from app.services.schedule_service import ScheduleService
from app.services.settings_service import SettingsService
from app.storage.validators import ValidationError

logger = get_logger(__name__)


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


class ScheduleRunner:
    """Executes one firing of a schedule against the publishing service."""

    def __init__(
        self,
        *,
        questions: QuestionService,
        schedules: ScheduleService,
        publishing: PublishingService,
        settings: SettingsService,
        audit: AuditService,
    ) -> None:
        self._questions = questions
        self._schedules = schedules
        self._publishing = publishing
        self._settings = settings
        self._audit = audit

    def _in_window(self, schedule: dict[str, Any], today: str) -> bool:
        start = schedule.get("start_date") or ""
        end = schedule.get("end_date") or ""
        if start and today < start:
            return False
        if end and today > end:
            return False
        return True

    def _published_today(self, schedule: dict[str, Any]) -> int:
        today = _today_iso()
        return sum(
            1
            for entry in self._publishing.history(limit=1000)
            if entry.get("schedule_id") == schedule["id"]
            and entry["status"] == "success"
            and entry["published_at"][:10] == today
        )

    def _published_today_global(self) -> int:
        today = _today_iso()
        return sum(
            1
            for entry in self._publishing.history(limit=1000)
            if entry["status"] == "success" and entry["published_at"][:10] == today
        )

    async def run(self, schedule_id: str, *, trigger: str = "schedule") -> dict[str, Any]:
        """Run one firing. Returns a small result dict for the logs."""
        schedule = self._schedules.get(schedule_id)
        if schedule is None:
            return {"ok": False, "reason": "missing-schedule"}

        if schedule.get("skip_next"):
            self._schedules._storage.schedules.update(  # noqa: SLF001
                lambda doc: [
                    s.update({"skip_next": False})
                    for s in doc["schedules"]
                    if s["id"] == schedule_id
                ]
            )
            self._audit.record(
                "schedule.skipped",
                level="warning",
                message=f"تجاوز موعد واحد للجدول {schedule_id} بطلب من المستخدم.",
            )
            return {"ok": False, "reason": "skipped"}

        if not schedule.get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        if not self._settings.publishing_enabled:
            return {"ok": False, "reason": "paused"}

        today = _today_iso()
        if not self._in_window(schedule, today):
            return {"ok": False, "reason": "out-of-window"}

        per_day = int(schedule.get("questions_per_day") or 0)
        if per_day and self._published_today(schedule) >= per_day:
            return {"ok": False, "reason": "schedule-daily-limit"}

        global_limit = int(self._settings.get().get("daily_limit") or 0)
        if global_limit and self._published_today_global() >= global_limit:
            self._audit.record(
                "publishing.limit_reached",
                level="warning",
                message=f"تم بلوغ الحد اليومي العام ({global_limit})؛ لم يُنشر سؤال جديد.",
            )
            return {"ok": False, "reason": "global-daily-limit"}

        channel = schedule["channel_id"]
        candidates = self._questions.pick_candidates(
            selection=schedule.get("question_selection", "sequential"),
            category_filter=schedule.get("category_filter", ""),
        )
        if not candidates:
            self._audit.record(
                "publishing.no_questions",
                level="warning",
                message=f"لا يوجد سؤال بحالة «جاهز» للجدول {schedule_id}.",
                schedule_id=schedule_id,
            )
            return {"ok": False, "reason": "no-questions"}

        question = candidates[0]
        try:
            entry = await self._publishing.publish(
                question["id"],
                channel,
                schedule_id=schedule_id,
                trigger=trigger,
            )
        except ValidationError as exc:
            logger.info("schedule %s skipped: %s", schedule_id, exc)
            return {"ok": False, "reason": str(exc)}

        advanced = entry["status"] == "success"
        self._schedules.record_run(schedule_id, advance=advanced)

        # Retry policy: queue a follow-up when the failure is transient.
        if entry["status"] == "failed" and entry.get("retryable"):
            self._audit.record(
                "publishing.retry_scheduled",
                level="warning",
                message=(
                    f"سيُعاد محاولة نشر {question['id']} وفق سياسة إعادة المحاولة "
                    f"(المحاولة {entry['attempts']})."
                ),
            )
        return {"ok": advanced, "status": entry["status"], "question_id": question["id"]}


def weekdays_to_cron(days: list[int]) -> str:
    """Convert 0=Monday..6=Sunday integers to an APScheduler day_of_week value."""
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return ",".join(names[d] for d in sorted(set(days)) if 0 <= d <= 6)


def once_datetime(schedule: dict[str, Any]) -> datetime | None:
    """Resolve the single datetime for a ``once`` schedule."""
    times = schedule.get("times") or []
    if not times:
        return None
    try:
        hour, minute = (int(part) for part in times[0].split(":"))
    except (ValueError, TypeError):
        return None
    raw_date = schedule.get("start_date")
    if raw_date:
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            return None
    else:
        day = datetime.now(UTC).date()
    return datetime(day.year, day.month, day.day, hour, minute)
