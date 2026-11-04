"""Publishing lifecycle: claim, send, confirm, log, and reconcile.

Guarantees implemented here:

* A question is only marked ``published`` after Telegram confirms delivery.
* A question is *claimed* (a ``pending`` history row) before the network call,
  so a crash mid-send leaves an auditable row instead of a silent duplicate.
* Two publishes for the same question+channel cannot run at the same time.
* On startup, ``pending`` rows that are too old are marked ``unknown`` and
  surfaced in the dashboard for manual review instead of being retried blindly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.services.audit_service import AuditService
from app.services.channel_service import ChannelService
from app.services.question_service import QuestionService
from app.services.settings_service import SettingsService
from app.storage.json_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError
from app.telegram.telegram_client import PublishResult, TelegramClient

logger = get_logger(__name__)

# A pending row older than this is treated as "we do not know the outcome".
PENDING_TIMEOUT = timedelta(minutes=10)

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"


class PublishingService:
    def __init__(
        self,
        storage: Storage,
        audit: AuditService,
        questions: QuestionService,
        channels: ChannelService,
        settings_service: SettingsService,
        client: TelegramClient,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._questions = questions
        self._channels = channels
        self._settings = settings_service
        self._client = client
        self._lock = asyncio.Lock()

    # --- history -----------------------------------------------------------
    def history(self, limit: int = 100, status: str = "") -> list[dict[str, Any]]:
        entries = self._storage.history.read()["history"]
        if status:
            entries = [e for e in entries if e["status"] == status]
        return entries[:limit]

    def history_for_question(self, question_id: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self._storage.history.read()["history"]
            if e["question_id"] == question_id
        ]

    def _claim(
        self,
        question_id: str,
        channel_id: str,
        schedule_id: str | None,
        trigger: str,
    ) -> dict[str, Any]:
        """Insert a pending row, refusing to double-claim the same target."""

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["history"]:
                if (
                    entry["status"] == STATUS_PENDING
                    and entry["question_id"] == question_id
                    and entry["channel_id"] == channel_id
                ):
                    raise ValidationError("عملية نشر لهذا السؤال على هذه القناة قيد التنفيذ.")
                if entry["status"] == STATUS_SUCCESS and entry["question_id"] == question_id:
                    raise ValidationError(
                        f"السؤال {question_id} نُشر مسبقاً بنجاح على القناة "
                        f"{entry['channel_id']}؛ لن يُعاد نشره."
                    )

            document["counter"] = int(document.get("counter", 0)) + 1
            entry = {
                "id": f"PUB{document['counter']:05d}",
                "question_id": question_id,
                "channel_id": channel_id,
                "schedule_id": schedule_id,
                "message_id": None,
                "status": STATUS_PENDING,
                "error": None,
                "error_code": None,
                "attempts": 1,
                "retryable": False,
                "trigger": trigger,
                "published_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            document["history"].insert(0, entry)
            return entry

        return self._storage.history.update(_mutate)

    def _finish(
        self,
        entry_id: str,
        *,
        status: str,
        message_id: int | None = None,
        error: str | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        bump_attempts: bool = False,
    ) -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["history"]:
                if entry["id"] == entry_id:
                    entry["status"] = status
                    entry["message_id"] = message_id
                    entry["error"] = error
                    entry["error_code"] = error_code
                    entry["retryable"] = retryable
                    entry["updated_at"] = utc_now_iso()
                    if bump_attempts:
                        entry["attempts"] = int(entry.get("attempts", 0)) + 1
                    return entry
            raise KeyError(entry_id)

        return self._storage.history.update(_mutate)

    def _mark_question_published(self, question_id: str) -> None:
        self._questions._storage.questions.update(  # noqa: SLF001 - intentional
            lambda doc: [
                q.update({"status": "published", "updated_at": utc_now_iso()})
                for q in doc["questions"]
                if q["id"] == question_id
            ]
        )

    # --- publishing --------------------------------------------------------
    async def publish(
        self,
        question_id: str,
        channel_id: str,
        *,
        schedule_id: str | None = None,
        trigger: str = "manual",
        actor: str = "system",
        respect_pause: bool = True,
    ) -> dict[str, Any]:
        """Publish a single question once. Returns the history entry."""
        if respect_pause and not self._settings.publishing_enabled:
            raise ValidationError("النشر متوقف حالياً. شغّل النشر ثم أعد المحاولة.")

        question = self._questions.get(question_id)
        if question is None:
            raise ValidationError(f"السؤال {question_id} غير موجود.")
        channel = self._channels.get(channel_id)
        if channel is None:
            raise ValidationError(f"القناة {channel_id} غير موجودة.")
        if not channel.get("enabled", True):
            raise ValidationError(f"القناة {channel['name']} معطّلة.")

        options = self._settings.get().get("poll_options", {})

        # The lock serialises attempts so two schedules cannot publish at once.
        async with self._lock:
            entry = self._claim(question_id, channel_id, schedule_id, trigger)
            result: PublishResult = await self._client.send_quiz(
                channel["chat_id"],
                question,
                explanation=bool(options.get("explanation", True)),
                open_period=int(options.get("open_period", 0)),
                anonymous=bool(options.get("anonymous", True)),
            )

            if result.ok:
                final = self._finish(
                    entry["id"], status=STATUS_SUCCESS, message_id=result.message_id
                )
                self._mark_question_published(question_id)
                self._channels.record_publish(channel_id, STATUS_SUCCESS)
                self._audit.record(
                    "publishing.success",
                    actor=actor,
                    message=(
                        f"نشر السؤال {question_id} على {channel['name']}"
                        + (" (تجريبي)" if result.dry_run else "")
                    ),
                    question_id=question_id,
                    channel_id=channel_id,
                )
            else:
                final = self._finish(
                    entry["id"],
                    status=STATUS_FAILED,
                    error=result.error,
                    error_code=result.error_code,
                    retryable=result.retryable,
                )
                self._channels.record_publish(channel_id, STATUS_FAILED)
                self._audit.record(
                    "publishing.failed",
                    actor=actor,
                    level="error",
                    message=f"فشل نشر السؤال {question_id}: {result.error_code}",
                    question_id=question_id,
                    channel_id=channel_id,
                )
            return final

    async def retry(self, entry_id: str, *, actor: str = "system") -> dict[str, Any]:
        """Retry a previously failed publish using its original target."""
        entries = {e["id"]: e for e in self._storage.history.read()["history"]}
        entry = entries.get(entry_id)
        if entry is None:
            raise ValidationError("سجل النشر المطلوب غير موجود.")
        if entry["status"] == STATUS_SUCCESS:
            raise ValidationError("هذه العملية نجحت مسبقاً؛ لن يُعاد النشر.")
        if entry["status"] == STATUS_PENDING:
            raise ValidationError("هذه العملية قيد التنفيذ حالياً.")

        question = self._questions.get(entry["question_id"])
        channel = self._channels.get(entry["channel_id"])
        if question is None or channel is None:
            raise ValidationError("السؤال أو القناة المرتبطة بهذه العملية لم تعد موجودة.")

        options = self._settings.get().get("poll_options", {})
        async with self._lock:
            result = await self._client.send_quiz(
                channel["chat_id"],
                question,
                explanation=bool(options.get("explanation", True)),
                open_period=int(options.get("open_period", 0)),
                anonymous=bool(options.get("anonymous", True)),
            )
            if result.ok:
                final = self._finish(
                    entry_id,
                    status=STATUS_SUCCESS,
                    message_id=result.message_id,
                    bump_attempts=True,
                )
                self._mark_question_published(entry["question_id"])
                self._channels.record_publish(entry["channel_id"], STATUS_SUCCESS)
            else:
                final = self._finish(
                    entry_id,
                    status=STATUS_FAILED,
                    error=result.error,
                    error_code=result.error_code,
                    retryable=result.retryable,
                    bump_attempts=True,
                )
            self._audit.record(
                "publishing.retried",
                actor=actor,
                level="info" if result.ok else "error",
                message=f"إعادة محاولة نشر {entry['question_id']} ({final['status']})",
            )
            return final

    # --- reconciliation ----------------------------------------------------
    def reconcile_pending(self, *, actor: str = "system") -> int:
        """Mark stale pending rows as unknown so they are reviewed by a human."""
        cutoff = datetime.now(UTC) - PENDING_TIMEOUT
        reconciled = 0

        def _mutate(document: dict[str, Any]) -> int:
            count = 0
            for entry in document["history"]:
                if entry["status"] != STATUS_PENDING:
                    continue
                try:
                    created = datetime.fromisoformat(entry["updated_at"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    created = cutoff - timedelta(seconds=1)
                if created < cutoff:
                    entry["status"] = STATUS_UNKNOWN
                    entry["error"] = "انقطاع غير محسوم أثناء النشر؛ يلزم مراجعة يدوية."
                    entry["updated_at"] = utc_now_iso()
                    count += 1
            return count

        reconciled = self._storage.history.update(_mutate)
        if reconciled:
            self._audit.record(
                "publishing.reconciled",
                actor=actor,
                level="warning",
                message=f"تم وسم {reconciled} عملية نشر بحالة غير محسومة للمراجعة.",
            )
        return reconciled

    def stats(self) -> dict[str, Any]:
        entries = self._storage.history.read()["history"]
        success = sum(1 for e in entries if e["status"] == STATUS_SUCCESS)
        failed = sum(1 for e in entries if e["status"] == STATUS_FAILED)
        pending = sum(1 for e in entries if e["status"] == STATUS_PENDING)
        unknown = sum(1 for e in entries if e["status"] == STATUS_UNKNOWN)
        today = utc_now_iso()[:10]
        today_count = sum(1 for e in entries if e["published_at"][:10] == today)
        return {
            "total": len(entries),
            "success": success,
            "failed": failed,
            "pending": pending,
            "unknown": unknown,
            "today": today_count,
        }
