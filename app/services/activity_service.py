"""Live student activity: who is interacting with the Mini App right now.

Tracking is **action-based**: a student is recorded only when they actually do
something (open an exam, start an attempt, answer a question, submit, review).
There is deliberately no idle heartbeat, so a student who leaves the app open
without touching anything stops counting as active once their last action ages
past :data:`TTL_SECONDS`.

The store keeps at most one row per ``(student, exam)`` pair, so it stays tiny;
:meth:`sweep` prunes rows older than :data:`RETENTION_SECONDS` and the scheduler
runs it alongside the attempt sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.audit_service import AuditService
from app.storage.base import StorageBackend as Storage, utc_now_iso

# A student counts as active while their most recent action is younger than
# this. Five minutes covers the gap between reading a question and answering it
# without keeping a student who walked away "active" indefinitely.
TTL_SECONDS = 300
# Rows are kept this long (for "last seen" context) before the sweep removes
# them; activity older than the TTL is simply not reported as active.
RETENTION_SECONDS = 24 * 60 * 60

EVENT_VIEW = "view"
EVENT_OPEN_EXAM = "open_exam"
EVENT_START = "start"
EVENT_ANSWER = "answer"
EVENT_SUBMIT = "submit"
EVENT_REVIEW = "review"

EVENT_LABELS: dict[str, str] = {
    EVENT_VIEW: "تصفّح التطبيق",
    EVENT_OPEN_EXAM: "فتح اختباراً",
    EVENT_START: "بدأ محاولة",
    EVENT_ANSWER: "أجاب عن سؤال",
    EVENT_SUBMIT: "سلّم محاولة",
    EVENT_REVIEW: "يراجع النتيجة",
}


def _parse_instant(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


class ActivityService:
    # Exposed on the class so callers (and templates) can show the rule without
    # importing the module constant.
    TTL_SECONDS = TTL_SECONDS

    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- writes ------------------------------------------------------------
    def touch(
        self,
        student_id: str,
        event: str,
        *,
        exam_id: str = "",
        question_id: str = "",
    ) -> dict[str, Any]:
        """Record one action, upserting the student's row for this exam.

        Called from the student API on the meaningful steps; a no-op action list
        keeps the collection from growing with rows nobody reads.
        """
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            rows = document["activity"]
            for row in rows:
                if row["student_id"] == student_id and row.get("exam_id", "") == exam_id:
                    row["last_at"] = now
                    row["last_event"] = event
                    row["event_count"] = int(row.get("event_count", 0)) + 1
                    if question_id:
                        row["last_question_id"] = question_id
                    return row
            row = {
                "id": f"AC{len(rows) + 1:06d}",
                "student_id": student_id,
                "exam_id": exam_id,
                "last_event": event,
                "last_question_id": question_id,
                "event_count": 1,
                "first_at": now,
                "last_at": now,
            }
            rows.append(row)
            return row

        return self._storage.student_activity.update(_mutate)

    def sweep(self) -> int:
        """Drop rows whose last action is older than the retention window."""
        cutoff = _now() - timedelta(seconds=RETENTION_SECONDS)

        def _mutate(document: dict[str, Any]) -> int:
            before = len(document["activity"])
            document["activity"] = [
                row
                for row in document["activity"]
                if (_parse_instant(row.get("last_at")) or cutoff) >= cutoff
            ]
            return before - len(document["activity"])

        return self._storage.student_activity.update(_mutate)

    # --- reads -------------------------------------------------------------
    def _active_rows(self, *, ttl: int = TTL_SECONDS) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(seconds=max(0, int(ttl)))
        rows = []
        for row in self._storage.student_activity.read()["activity"]:
            last = _parse_instant(row.get("last_at"))
            if last is not None and last >= cutoff:
                rows.append(
                    {
                        **row,
                        "_last": last,
                        "event_label": EVENT_LABELS.get(row.get("last_event", ""), ""),
                    }
                )
        rows.sort(key=lambda r: r["_last"], reverse=True)
        return rows

    def active_for_exam(
        self, exam_id: str, *, ttl: int = TTL_SECONDS
    ) -> dict[str, dict[str, Any]]:
        """``student_id -> activity`` for students active in one exam right now."""
        return {
            row["student_id"]: row
            for row in self._active_rows(ttl=ttl)
            if row.get("exam_id", "") == exam_id
        }

    def active_students(self, *, ttl: int = TTL_SECONDS) -> list[dict[str, Any]]:
        """Every active student, newest action first (one row per student)."""
        seen: dict[str, dict[str, Any]] = {}
        for row in self._active_rows(ttl=ttl):
            # A student active in two exams appears once, under their latest action.
            seen.setdefault(row["student_id"], row)
        return list(seen.values())

    def snapshot(
        self,
        *,
        students: Any = None,
        exams: Any = None,
        ttl: int = TTL_SECONDS,
    ) -> dict[str, Any]:
        """The payload the admin live feed polls: active students, enriched.

        ``students``/``exams`` are the services used to resolve ids to the names
        an operator recognises; both are optional so the service stays usable in
        tests without them.
        """
        now = _now()
        student_names: dict[str, str] = {}
        exam_titles: dict[str, str] = {}
        if students is not None:
            student_names = {s["id"]: students.display_name(s) for s in students.list_students()}
        if exams is not None:
            exam_titles = {e["id"]: e.get("title", e["id"]) for e in exams.list_exams()}

        items = []
        for row in self.active_students(ttl=ttl):
            last = row["_last"]
            items.append(
                {
                    "student_id": row["student_id"],
                    "student_name": student_names.get(row["student_id"], row["student_id"]),
                    "exam_id": row.get("exam_id", ""),
                    "exam_title": exam_titles.get(row.get("exam_id", ""), ""),
                    "event": row.get("last_event", ""),
                    "event_label": EVENT_LABELS.get(row.get("last_event", ""), ""),
                    "last_at": row.get("last_at", ""),
                    "seconds_ago": max(0, int((now - last).total_seconds())),
                }
            )
        return {"ttl_seconds": ttl, "count": len(items), "active": items}
