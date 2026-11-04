"""The logic executed when a schedule fires.

Kept separate from the APScheduler wiring so it can be unit-tested by calling
:func:`run_schedule` directly.
"""

from __future__ import annotations

import asyncio
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

# A single firing spaces its questions out, but the total wait is bounded so the
# job (and a manual "run now" HTTP request) can never hang for minutes. Larger
# spacing belongs to the ``interval`` schedule type instead.
MAX_BATCH_WAIT_SECONDS = 120


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

        batch_size = max(1, int(schedule.get("questions_per_run") or 1))
        # A schedule's own gap wins; otherwise fall back to the global pacing
        # interval so a batch never fires back-to-back and hits the rate limit.
        gap = max(0, int(schedule.get("batch_interval_seconds") or 0))
        if not gap and batch_size > 1:
            gap, _ = self._publishing.pacing()

        # A single run must not exceed the schedule's daily allowance.
        remaining = per_day - self._published_today(schedule) if per_day else batch_size
        target = max(1, min(batch_size, remaining)) if per_day else batch_size

        channel = schedule["channel_id"]
        # Questions pinned to this schedule (assigned from the bank) go first, in
        # the exact order they were queued. A queued id is used even if it was
        # already published to this channel before, because pinning it is an
        # explicit re-publish decision (the publish window warns about repeats).
        # Any remaining slots are filled from the normal auto-selection, which is
        # filtered per channel so a question already sent here is not repeated.
        queue_only = bool(schedule.get("queue_only"))
        queued_ids = self._schedules.queue_of(schedule)
        candidates: list[dict[str, Any]] = []
        if queued_ids:
            candidates = [
                question
                for question in (self._questions.get(qid) for qid in queued_ids)
                if question and question.get("status") == "ready"
            ]
        if not queue_only and len(candidates) < target:
            chosen = {q["id"] for q in candidates}
            for question in self._questions.pick_candidates(
                selection=schedule.get("question_selection", "sequential"),
                category_filter=schedule.get("category_filter", ""),
                exclude_ids=chosen,
                channel_id=channel,
                published_pairs=self._publishing.published_pairs(),
            ):
                candidates.append(question)
                if len(candidates) >= target:
                    break
        if not candidates:
            if queue_only:
                # A dedicated schedule whose list is exhausted is retired so it
                # stops firing empty; it is left visible for the operator.
                self._schedules.set_enabled(schedule_id, False, actor="scheduler")
                self._audit.record(
                    "schedule.completed",
                    message=(
                        f"اكتملت قائمة الجدول المخصص {schedule_id}؛ تم إيقافه تلقائياً."
                    ),
                    schedule_id=schedule_id,
                )
                return {"ok": False, "reason": "queue-completed"}
            self._audit.record(
                "publishing.no_questions",
                level="warning",
                message=f"لا يوجد سؤال بحالة «جاهز» للجدول {schedule_id}.",
                schedule_id=schedule_id,
            )
            return {"ok": False, "reason": "no-questions"}

        published = 0
        failures = 0
        cancelled = 0
        last_question_id = ""
        waited = 0.0
        published_ids: list[str] = []
        batch = candidates[:target]
        self._publishing.begin_run(kind="schedule", channel_id=channel, total=len(batch))
        try:
            for index, question in enumerate(batch):
                if self._publishing.cancel_requested:
                    cancelled = len(batch) - index
                    self._audit.record(
                        "publishing.cancelled",
                        level="warning",
                        message=(
                            f"تم إلغاء الدفعة المجدولة {schedule_id} بعد {index} من "
                            f"{len(batch)} سؤالاً."
                        ),
                        schedule_id=schedule_id,
                    )
                    break
                if index and gap and waited < MAX_BATCH_WAIT_SECONDS:
                    delay = min(gap, MAX_BATCH_WAIT_SECONDS - waited)
                    waited += delay
                    await asyncio.sleep(delay)
                if global_limit and self._published_today_global() >= global_limit:
                    break
                last_question_id = question["id"]
                try:
                    entry = await self._publishing.publish(
                        question["id"],
                        channel,
                        schedule_id=schedule_id,
                        trigger=trigger,
                    )
                except ValidationError as exc:
                    logger.info("schedule %s skipped %s: %s", schedule_id, question["id"], exc)
                    continue

                if entry["status"] == "success":
                    published += 1
                    published_ids.append(question["id"])
                else:
                    failures += 1

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
        finally:
            self._publishing.end_run()

        # Drop what we just published from the queue, plus anything queued that
        # is no longer publishable (deleted, or not ``ready``) so the queue
        # cannot get stuck on a dead entry.
        if queued_ids:
            stale = [
                qid
                for qid in queued_ids
                if (question := self._questions.get(qid)) is None
                or question.get("status") != "ready"
            ]
            updated = self._schedules.consume_queue(schedule_id, published_ids + stale)
            # A dedicated schedule that just emptied itself is retired at once so
            # it does not fire one more time only to report an empty queue.
            if queue_only and not self._schedules.queue_of(updated):
                self._schedules.set_enabled(schedule_id, False, actor="scheduler")
                self._audit.record(
                    "schedule.completed",
                    message=(
                        f"اكتملت قائمة الجدول المخصص {schedule_id}؛ تم إيقافه تلقائياً."
                    ),
                    schedule_id=schedule_id,
                )

        self._schedules.record_run(
            schedule_id, advance=published > 0, advance_by=published
        )

        if published > 1:
            self._audit.record(
                "schedule.batch_published",
                message=(
                    f"نُشرت {published} أسئلة دفعة واحدة للجدول {schedule_id}"
                    + (f" بفارق {gap} ثانية." if gap else ".")
                ),
                schedule_id=schedule_id,
            )

        return {
            "ok": published > 0,
            "published": published,
            "failed": failures,
            "cancelled": cancelled,
            "requested": target,
            "gap_seconds": gap,
            "question_id": last_question_id,
            "reason": None if published else "publish-failed",
        }


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
