"""Publishing lifecycle: claim, send, confirm, log, and reconcile.

Guarantees implemented here:

* A question is only marked ``published`` after Telegram confirms delivery.
* A question is *claimed* (a ``pending`` history row) before the network call,
  so a crash mid-send leaves an auditable row instead of a silent duplicate.
* Two publishes for the same question+channel cannot run at the same time.
* On startup, ``pending`` rows that are too old are marked ``unknown`` and
  surfaced in the dashboard for manual review instead of being retried blindly.
* Sends are *paced* and Telegram flood control is honoured: a burst of manual
  publishes used to hit the per-chat rate limit and silently drop everything
  past ~20 messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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

# A single question may be re-sent after a flood wait at most this many times
# before we give up and log it, so one stubborn question cannot stall a batch.
MAX_FLOOD_RETRIES = 2

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"

# Injected in tests so pacing/flood waits do not actually sleep.
Sleeper = Callable[[float], Awaitable[None]]


class PublishingService:
    def __init__(
        self,
        storage: Storage,
        audit: AuditService,
        questions: QuestionService,
        channels: ChannelService,
        settings_service: SettingsService,
        client: TelegramClient,
        *,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._questions = questions
        self._channels = channels
        self._settings = settings_service
        self._client = client
        self._lock = asyncio.Lock()
        self._sleep = sleep

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
        *,
        allow_repeat: bool = False,
    ) -> dict[str, Any]:
        """Insert a pending row, refusing to double-claim the same target.

        ``allow_repeat`` is set only for a human-initiated manual publish, where
        posting the same question again is an explicit, deliberate choice. Even
        then a concurrent publish of the same question+channel is refused.
        """

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["history"]:
                if (
                    entry["status"] == STATUS_PENDING
                    and entry["question_id"] == question_id
                    and entry["channel_id"] == channel_id
                ):
                    raise ValidationError("عملية نشر لهذا السؤال على هذه القناة قيد التنفيذ.")
                if (
                    not allow_repeat
                    and entry["status"] == STATUS_SUCCESS
                    and entry["question_id"] == question_id
                ):
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
                "poll_id": None,
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
        poll_id: str | None = None,
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
                    if poll_id is not None:
                        entry["poll_id"] = poll_id
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
    def pacing(self) -> tuple[int, int]:
        """``(interval_seconds, max_wait_seconds)`` for a burst of publishes."""
        config = self._settings.get().get("pacing") or {}
        interval = max(0, int(config.get("interval_seconds", 4)))
        max_wait = max(0, int(config.get("max_wait_seconds", 120)))
        return interval, max_wait

    async def _send_with_flood_retry(
        self,
        chat_id: str,
        question: dict[str, Any],
        options: dict[str, Any],
        *,
        channel_id: str,
        max_wait: int,
    ) -> PublishResult:
        """Send one quiz, honouring Telegram's flood wait and retrying.

        A rate-limit error is safe to retry (Telegram did not post anything), so
        we wait the requested number of seconds and try again, up to
        ``MAX_FLOOD_RETRIES`` times. Without this, every question past the
        per-chat limit failed outright — the bug that dropped 35 of 55 sends.
        """
        for attempt in range(MAX_FLOOD_RETRIES + 1):
            result = await self._client.send_quiz(
                chat_id,
                question,
                explanation=bool(options.get("explanation", True)),
                open_period=int(options.get("open_period", 0)),
                anonymous=bool(options.get("anonymous", True)),
                channel_id=channel_id,
            )
            if result.ok or not result.flood_wait:
                return result
            if attempt >= MAX_FLOOD_RETRIES:
                return result

            wait = result.retry_after
            if wait is None:
                wait, _ = self.pacing()
            # Cap the wait so a long ban cannot hang the request indefinitely;
            # when capped we log and move on instead of retrying immediately.
            if max_wait and wait > max_wait:
                logger.warning(
                    "flood wait of %ss exceeds the %ss cap for %s; not retrying",
                    wait,
                    max_wait,
                    question.get("id", "?"),
                )
                return result
            logger.info(
                "flood control: waiting %ss before retrying %s (attempt %s)",
                wait,
                question.get("id", "?"),
                attempt + 1,
            )
            self._audit.record(
                "publishing.flood_wait",
                level="warning",
                message=(
                    f"تيليجرام طلب الانتظار {wait} ثانية قبل نشر "
                    f"{question.get('id', '?')}؛ سيُعاد المحاولة."
                ),
                question_id=question.get("id", ""),
                channel_id=channel_id,
            )
            await self._sleep(wait)
        return result  # pragma: no cover - loop always returns

    async def publish(
        self,
        question_id: str,
        channel_id: str,
        *,
        schedule_id: str | None = None,
        trigger: str = "manual",
        actor: str = "system",
        respect_pause: bool = True,
        allow_repeat: bool = False,
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
        _, max_wait = self.pacing()

        # The lock serialises attempts so two schedules cannot publish at once.
        async with self._lock:
            entry = self._claim(
                question_id, channel_id, schedule_id, trigger, allow_repeat=allow_repeat
            )
            result = await self._send_with_flood_retry(
                channel["chat_id"],
                question,
                options,
                channel_id=channel_id,
                max_wait=max_wait,
            )

            if result.ok:
                final = self._finish(
                    entry["id"],
                    status=STATUS_SUCCESS,
                    message_id=result.message_id,
                    poll_id=result.poll_id,
                )
                self._mark_question_published(question_id)
                self._channels.record_publish(channel_id, STATUS_SUCCESS)
                self._audit.record(
                    "publishing.success",
                    actor=actor,
                    message=(
                        f"نشر السؤال {question_id} على {channel['name']}"
                        + (" (تجريبي)" if result.dry_run else "")
                        + (" (نشر يدوي متكرر)" if allow_repeat else "")
                    ),
                    question_id=question_id,
                    channel_id=channel_id,
                )
            else:
                # A timeout or dropped connection may have happened after the
                # quiz was delivered, so record it as unresolved instead of a
                # plain failure: a human decides whether to retry.
                ambiguous = result.ambiguous
                final = self._finish(
                    entry["id"],
                    status=STATUS_UNKNOWN if ambiguous else STATUS_FAILED,
                    error=(
                        f"{result.error} — قد تكون الرسالة وصلت فعلاً؛ راجع القناة قبل الإعادة."
                        if ambiguous
                        else result.error
                    ),
                    error_code=result.error_code,
                    retryable=result.retryable,
                )
                self._channels.record_publish(channel_id, STATUS_FAILED)
                self._audit.record(
                    "publishing.unresolved" if ambiguous else "publishing.failed",
                    actor=actor,
                    level="warning" if ambiguous else "error",
                    message=(
                        f"نشر السؤال {question_id} غير محسوم ({result.error_code})؛ يلزم مراجعة يدوية."
                        if ambiguous
                        else f"فشل نشر السؤال {question_id}: {result.error_code}"
                    ),
                    question_id=question_id,
                    channel_id=channel_id,
                )
            return final

    async def publish_many(
        self,
        question_ids: list[str],
        channel_id: str,
        *,
        actor: str = "system",
        trigger: str = "manual",
        interval_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Publish several questions to one channel, spaced out to respect limits.

        A per-question failure is recorded and does not abort the batch, and the
        caller gets one result row per question in the original order. Pacing is
        what makes a 50-question burst safe: without it Telegram rate-limits the
        channel after roughly 20 messages in a minute.
        """
        interval, _ = self.pacing()
        if interval_seconds is not None:
            interval = max(0, int(interval_seconds))

        results: list[dict[str, Any]] = []
        for index, question_id in enumerate(question_ids):
            if index and interval:
                await self._sleep(interval)
            try:
                entry = await self.publish(
                    question_id,
                    channel_id,
                    trigger=trigger,
                    actor=actor,
                    respect_pause=False,
                    allow_repeat=True,
                )
                results.append(
                    {
                        "question_id": question_id,
                        "status": entry["status"],
                        "entry_id": entry["id"],
                        "error": entry.get("error") or "",
                    }
                )
            except ValidationError as exc:
                results.append(
                    {"question_id": question_id, "status": STATUS_FAILED, "error": str(exc)}
                )
        return results

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
        _, max_wait = self.pacing()
        async with self._lock:
            result = await self._send_with_flood_retry(
                channel["chat_id"],
                question,
                options,
                channel_id=entry["channel_id"],
                max_wait=max_wait,
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
