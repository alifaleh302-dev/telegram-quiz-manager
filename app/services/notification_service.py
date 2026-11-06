"""Student notifications: enqueue once, deliver over Telegram without duplicates.

A notification row is the *intent* to notify; delivery is a separate step so a
network failure never loses the intent. Uniqueness is enforced on
``(student_id, exam_id, type)`` — the same event never produces two rows, which
is what "no duplicate notifications" (doc rule) requires.

Delivery respects Telegram's rate limits: a ``429`` (``retry_after``) pauses the
batch, and the row stays ``pending`` for the next dispatch instead of being
dropped. Only the bot transport is used (see ``TransportRouter.send_message``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.core.messages import render as render_message
from app.services.audit_service import AuditService
from app.services.exam_service import ExamService
from app.services.student_service import StudentService
from app.storage.base import StorageBackend as Storage, utc_now_iso

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Default per-type switches; the admin can flip these in settings.
DEFAULT_TYPES: dict[str, bool] = {
    "exam_published": True,
    "exam_upcoming": True,
    "exam_closing_soon": True,
    "attempt_completed": True,
    "result_available": True,
}


class NotificationService:
    def __init__(
        self,
        storage: Storage,
        audit: AuditService,
        students: StudentService,
        exams: ExamService,
        transport: Any = None,
        settings: Any = None,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._students = students
        self._exams = exams
        self._transport = transport
        self._settings = settings
        self._sleep = asyncio.sleep

    def set_transport(self, transport: Any) -> None:
        self._transport = transport

    # --- configuration -----------------------------------------------------
    def _messages(self) -> dict[str, Any]:
        return self._settings.messages() if self._settings is not None else {}

    def _type_enabled(self, type: str) -> bool:
        if self._settings is None:
            return DEFAULT_TYPES.get(type, True)
        return self._settings.notify_type_enabled(type)

    def _published_targets(self, exam: dict[str, Any]) -> list[str]:
        """Student ids eligible for a 'new exam' notice for ``exam``.

        Only students who can actually sit the exam are notified: a published
        exam is available to everyone, but a scheduled one is advertised only to
        students for whom the window has already opened.
        """
        students = self._students.list_students()
        if exam.get("access_mode") != "scheduled":
            return [s["id"] for s in students]
        # A scheduled exam is advertised only once its window has opened.
        return [s["id"] for s in students] if self._exams.can_start(exam)[0] else []

    # --- reads -------------------------------------------------------------
    def _all(self) -> list[dict[str, Any]]:
        return self._storage.notifications.read()["notifications"]

    def for_student(self, student_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [n for n in self._all() if n["student_id"] == student_id]
        return sorted(rows, key=lambda n: n.get("created_at") or "", reverse=True)[:limit]

    def unread_count(self, student_id: str) -> int:
        return sum(
            1
            for n in self._all()
            if n["student_id"] == student_id and not n.get("read_at")
        )

    def mark_read(self, student_id: str, notification_id: str) -> None:
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> None:
            for note in document["notifications"]:
                if note["id"] == notification_id and note["student_id"] == student_id:
                    note["read_at"] = now

        self._storage.notifications.update(_mutate)

    def stats(self) -> dict[str, int]:
        rows = self._all()
        return {
            "total": len(rows),
            "pending": sum(1 for n in rows if n.get("status") == STATUS_PENDING),
            "sent": sum(1 for n in rows if n.get("status") == STATUS_SENT),
            "failed": sum(1 for n in rows if n.get("status") == STATUS_FAILED),
        }

    # --- enqueue -----------------------------------------------------------
    def enqueue(
        self,
        *,
        student_id: str,
        exam_id: str | None,
        type: str,
        title: str,
        body: str,
        dedupe: bool = True,
    ) -> dict[str, Any] | None:
        """Create one notification, or return ``None`` if it already exists.

        ``dedupe`` collapses repeats of the same ``(student, exam, type)`` event,
        so re-publishing an exam or a retried sweep cannot spam students.
        """
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any] | None:
            if dedupe and any(
                n["student_id"] == student_id
                and n.get("exam_id") == exam_id
                and n.get("type") == type
                for n in document["notifications"]
            ):
                return None
            document["counter"] = int(document.get("counter", 0)) + 1
            note = {
                "id": f"NT{document['counter']:06d}",
                "student_id": student_id,
                "exam_id": exam_id,
                "type": type,
                "title": title,
                "body": body,
                "status": STATUS_PENDING,
                "read_at": None,
                "sent_at": None,
                "telegram_message_id": None,
                "error": "",
                "created_at": now,
            }
            document["notifications"].append(note)
            return note

        return self._storage.notifications.update(_mutate)

    def notify_exam_published(
        self, exam: dict[str, Any], *, student_ids: list[str] | None = None
    ) -> int:
        """Fan out a 'new exam' notification to every eligible student.

        The message text comes from the admin-editable ``exam_published``
        templates; the type can be switched off entirely in settings.
        """
        if not self._type_enabled("exam_published"):
            return 0
        targets = (
            list(student_ids)
            if student_ids is not None
            else self._published_targets(exam)
        )
        messages = self._messages()
        title = render_message(messages, "exam_published_title")
        context = {
            "exam_title": exam.get("title", ""),
            "question_count": self._exams.question_count(exam["id"]),
            "starts_at": exam.get("starts_at") or "",
        }
        body = render_message(messages, "exam_published_body", context)
        created = 0
        for student_id in targets:
            if self.enqueue(
                student_id=student_id,
                exam_id=exam["id"],
                type="exam_published",
                title=title,
                body=body,
            ):
                created += 1
        if created:
            self._audit.record(
                "notification.enqueued",
                message=f"إشعار اختبار جديد «{exam['title']}» إلى {created} طالباً",
                exam_id=exam["id"],
            )
        return created

    def notify_attempt_completed(
        self, attempt: dict[str, Any], exam: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self._type_enabled("attempt_completed"):
            return None
        messages = self._messages()
        return self.enqueue(
            student_id=attempt["student_id"],
            exam_id=exam["id"],
            type="attempt_completed",
            title=render_message(messages, "attempt_completed_title"),
            body=render_message(
                messages,
                "attempt_completed_body",
                {
                    "exam_title": exam.get("title", ""),
                    "percentage": attempt.get("percentage", 0),
                },
            ),
        )

    def notify_due_exams(self) -> int:
        """Announce scheduled exams whose window has just opened.

        A scheduled exam is published with status ``scheduled`` and only becomes
        startable later, so it never triggers the publish broadcast. This sweep
        (run by the scheduler) enqueues the same 'new exam' notice once the
        window opens; the ``(student, exam, type)`` dedupe keeps it to one.
        """
        if not self._type_enabled("exam_published"):
            return 0
        created = 0
        for exam in self._exams.list_exams(status="scheduled"):
            if not self._exams.can_start(exam)[0]:
                continue
            created += self.notify_exam_published(exam)
        return created

    # --- delivery ----------------------------------------------------------
    async def dispatch(
        self, *, limit: int = 50, exam_id: str | None = None
    ) -> dict[str, int]:
        """Attempt to deliver pending notifications. Returns a small summary.

        ``exam_id`` restricts the sweep to one exam's notifications, so an
        admin action can report exactly how many students it reached instead of
        waiting for the next periodic sweep.
        """
        if self._transport is None:
            return {"sent": 0, "failed": 0, "skipped": 0}

        pending = [
            n
            for n in self._all()
            if n.get("status") == STATUS_PENDING
            and (exam_id is None or n.get("exam_id") == exam_id)
        ][:limit]
        sent = failed = skipped = 0
        for note in pending:
            student = self._students.get(note["student_id"])
            if student is None or not student.get("notifications_enabled", True):
                self._mark(note["id"], STATUS_SKIPPED, error="الإشعارات معطّلة")
                skipped += 1
                continue

            try:
                result = await self._transport.send_message(
                    int(student["telegram_user_id"]), f"{note['title']}\n\n{note['body']}"
                )
            except Exception as exc:  # noqa: BLE001 - a send must never crash dispatch
                logger.warning("notification send failed: %s", exc)
                self._mark(note["id"], STATUS_FAILED, error=str(exc))
                failed += 1
                continue

            if result.ok:
                self._mark(
                    note["id"], STATUS_SENT, message_id=result.message_id
                )
                sent += 1
            elif getattr(result, "retry_after", None):
                # Rate limited: leave it pending and pause the batch.
                await self._sleep(min(int(result.retry_after), 60))
            else:
                self._mark(note["id"], STATUS_FAILED, error=result.error or "فشل الإرسال")
                failed += 1
        return {"sent": sent, "failed": failed, "skipped": skipped}

    def _mark(
        self,
        notification_id: str,
        status: str,
        *,
        message_id: int | None = None,
        error: str = "",
    ) -> None:
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> None:
            for note in document["notifications"]:
                if note["id"] == notification_id:
                    note["status"] = status
                    if status == STATUS_SENT:
                        note["sent_at"] = now
                        note["telegram_message_id"] = message_id
                    if error:
                        note["error"] = error

        self._storage.notifications.update(_mutate)
