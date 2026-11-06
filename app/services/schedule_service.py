"""Schedule management: CRUD, pause/resume and per-schedule state."""

from __future__ import annotations

from typing import Any

from app.services.audit_service import AuditService
from app.services.channel_service import ChannelService
from app.storage.base import StorageBackend as Storage, utc_now_iso
from app.storage.validators import ValidationError, validate_schedule_payload


class ScheduleService:
    def __init__(
        self, storage: Storage, audit: AuditService, channels: ChannelService
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._channels = channels

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.schedules.read()["schedules"]

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        return next((s for s in self._all() if s["id"] == schedule_id), None)

    def list_schedules(self) -> list[dict[str, Any]]:
        return self._all()

    def list_for_channel(self, channel_id: str) -> list[dict[str, Any]]:
        return [s for s in self._all() if s["channel_id"] == channel_id]

    def _check_channel(self, channel_id: str) -> None:
        if self._channels.get(channel_id) is None:
            raise ValidationError(f"القناة {channel_id} غير موجودة.")

    def create(self, payload: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        data = validate_schedule_payload(payload)
        self._check_channel(data["channel_id"])
        if not data["timezone"]:
            data["timezone"] = self._storage.settings.read().get("timezone", "UTC")
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["counter"] = int(document.get("counter", 0)) + 1
            schedule = {
                "id": f"SCH{document['counter']:03d}",
                **data,
                "cursor": 0,
                "published_count": 0,
                "queue": [],
                "last_run_at": None,
                "next_run_at": None,
                "created_at": now,
                "updated_at": now,
            }
            document["schedules"].append(schedule)
            return schedule

        schedule = self._storage.schedules.update(_mutate)
        self._audit.record(
            "schedule.created",
            actor=actor,
            message=f"إنشاء الجدول {schedule['id']} للقناة {schedule['channel_id']}",
            schedule_id=schedule["id"],
        )
        return schedule

    def update(
        self, schedule_id: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        data = validate_schedule_payload(payload)
        self._check_channel(data["channel_id"])

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule.update(data)
                    schedule["updated_at"] = utc_now_iso()
                    return schedule
            raise KeyError(schedule_id)

        try:
            schedule = self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

        self._audit.record(
            "schedule.updated", actor=actor, message=f"تعديل الجدول {schedule_id}"
        )
        return schedule

    def set_enabled(
        self, schedule_id: str, enabled: bool, *, actor: str = "system"
    ) -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["enabled"] = enabled
                    schedule["updated_at"] = utc_now_iso()
                    return schedule
            raise KeyError(schedule_id)

        try:
            schedule = self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

        self._audit.record(
            "schedule.toggled",
            actor=actor,
            message=f"{'تفعيل' if enabled else 'إيقاف'} الجدول {schedule_id}",
        )
        return schedule

    def delete(self, schedule_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            before = len(document["schedules"])
            document["schedules"] = [
                s for s in document["schedules"] if s["id"] != schedule_id
            ]
            if len(document["schedules"]) == before:
                raise KeyError(schedule_id)

        try:
            self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

        self._audit.record(
            "schedule.deleted",
            actor=actor,
            level="warning",
            message=f"حذف الجدول {schedule_id}",
        )

    # --- runtime state -----------------------------------------------------
    def queue_of(self, schedule: dict[str, Any]) -> list[str]:
        """The ordered list of question IDs pinned to this schedule."""
        return [str(qid) for qid in (schedule.get("queue") or [])]

    def assign_questions(
        self, schedule_id: str, question_ids: list[str], *, actor: str = "system"
    ) -> dict[str, Any]:
        """Append ``question_ids`` to a schedule's queue, preserving order.

        A question already queued is not added twice, so re-running an
        assignment is idempotent instead of publishing the same item twice.
        """
        ordered = list(dict.fromkeys(str(qid) for qid in question_ids if qid))
        if not ordered:
            raise ValidationError("لم يتم تحديد أي سؤال.")

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    queue = self.queue_of(schedule)
                    queue.extend(qid for qid in ordered if qid not in queue)
                    schedule["queue"] = queue
                    schedule["updated_at"] = utc_now_iso()
                    return schedule
            raise KeyError(schedule_id)

        try:
            schedule = self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

        self._audit.record(
            "schedule.assigned",
            actor=actor,
            message=(
                f"ربط {len(ordered)} سؤالاً بالجدول {schedule_id} "
                f"(إجمالي القائمة {len(self.queue_of(schedule))})."
            ),
            schedule_id=schedule_id,
            count=len(ordered),
        )
        return schedule

    def consume_queue(
        self, schedule_id: str, question_ids: list[str]
    ) -> dict[str, Any]:
        """Remove published questions from a schedule's queue (FIFO order kept)."""
        published = {str(qid) for qid in question_ids}

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["queue"] = [
                        qid for qid in self.queue_of(schedule) if qid not in published
                    ]
                    schedule["updated_at"] = utc_now_iso()
                    return schedule
            raise KeyError(schedule_id)

        try:
            return self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

    def clear_queue(self, schedule_id: str, *, actor: str = "system") -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["queue"] = []
                    schedule["updated_at"] = utc_now_iso()
                    return schedule
            raise KeyError(schedule_id)

        try:
            schedule = self._storage.schedules.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الجدول {schedule_id} غير موجود.") from exc

        self._audit.record(
            "schedule.queue_cleared",
            actor=actor,
            level="warning",
            message=f"إفراغ قائمة الأسئلة المرتبطة بالجدول {schedule_id}.",
            schedule_id=schedule_id,
        )
        return schedule

    def record_run(
        self,
        schedule_id: str,
        *,
        next_run_at: str | None = None,
        advance: bool = False,
        advance_by: int = 1,
    ) -> None:
        def _mutate(document: dict[str, Any]) -> None:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["last_run_at"] = utc_now_iso()
                    schedule["next_run_at"] = next_run_at
                    if advance:
                        step = max(0, int(advance_by))
                        schedule["cursor"] = int(schedule.get("cursor", 0)) + step
                        schedule["published_count"] = (
                            int(schedule.get("published_count", 0)) + step
                        )

        self._storage.schedules.update(_mutate)

    def set_next_run(self, schedule_id: str, next_run_at: str | None) -> None:
        def _mutate(document: dict[str, Any]) -> None:
            for schedule in document["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["next_run_at"] = next_run_at

        self._storage.schedules.update(_mutate)
