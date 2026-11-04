"""Channel management: CRUD, default channel and publish-readiness flags."""

from __future__ import annotations

from typing import Any

from app.services.audit_service import AuditService
from app.storage.json_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError, validate_channel_payload


class ChannelService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.channels.read()["channels"]

    def get(self, channel_id: str) -> dict[str, Any] | None:
        return next((c for c in self._all() if c["id"] == channel_id), None)

    def list_channels(self) -> list[dict[str, Any]]:
        return self._all()

    def default_channel(self) -> dict[str, Any] | None:
        channels = self._all()
        return next((c for c in channels if c.get("default")), channels[0] if channels else None)

    def create(self, payload: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        data = validate_channel_payload(payload)
        if any(c["chat_id"] == data["chat_id"] for c in self._all()):
            raise ValidationError(f"القناة {data['chat_id']} مضافة مسبقاً.")
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["counter"] = int(document.get("counter", 0)) + 1
            channel = {
                "id": f"CH{document['counter']:03d}",
                **data,
                "default": not document["channels"],
                "last_publish_at": None,
                "last_publish_status": None,
                "created_at": now,
                "updated_at": now,
            }
            document["channels"].append(channel)
            return channel

        channel = self._storage.channels.update(_mutate)
        self._audit.record(
            "channel.created",
            actor=actor,
            message=f"إضافة القناة {channel['name']} ({channel['chat_id']})",
            channel_id=channel["id"],
        )
        return channel

    def update(
        self, channel_id: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        data = validate_channel_payload(payload)

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for other in document["channels"]:
                if other["id"] != channel_id and other["chat_id"] == data["chat_id"]:
                    raise ValidationError(f"القناة {data['chat_id']} مضافة مسبقاً.")
            for channel in document["channels"]:
                if channel["id"] == channel_id:
                    channel.update(data)
                    channel["updated_at"] = utc_now_iso()
                    return channel
            raise KeyError(channel_id)

        try:
            channel = self._storage.channels.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"القناة {channel_id} غير موجودة.") from exc

        self._audit.record(
            "channel.updated", actor=actor, message=f"تعديل القناة {channel_id}"
        )
        return channel

    def set_enabled(
        self, channel_id: str, enabled: bool, *, actor: str = "system"
    ) -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for channel in document["channels"]:
                if channel["id"] == channel_id:
                    channel["enabled"] = enabled
                    channel["updated_at"] = utc_now_iso()
                    return channel
            raise KeyError(channel_id)

        try:
            channel = self._storage.channels.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"القناة {channel_id} غير موجودة.") from exc

        self._audit.record(
            "channel.toggled",
            actor=actor,
            message=f"{'تفعيل' if enabled else 'تعطيل'} القناة {channel['name']}",
        )
        return channel

    def set_default(self, channel_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            if not any(c["id"] == channel_id for c in document["channels"]):
                raise KeyError(channel_id)
            for channel in document["channels"]:
                channel["default"] = channel["id"] == channel_id

        try:
            self._storage.channels.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"القناة {channel_id} غير موجودة.") from exc

        self._audit.record(
            "channel.default_set", actor=actor, message=f"تعيين القناة {channel_id} افتراضية"
        )

    def delete(self, channel_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            before = len(document["channels"])
            document["channels"] = [
                c for c in document["channels"] if c["id"] != channel_id
            ]
            if len(document["channels"]) == before:
                raise KeyError(channel_id)
            if document["channels"] and not any(c.get("default") for c in document["channels"]):
                document["channels"][0]["default"] = True

        try:
            self._storage.channels.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"القناة {channel_id} غير موجودة.") from exc

        self._audit.record(
            "channel.deleted",
            actor=actor,
            level="warning",
            message=f"حذف القناة {channel_id}",
        )

    def record_publish(self, channel_id: str, status: str) -> None:
        def _mutate(document: dict[str, Any]) -> None:
            for channel in document["channels"]:
                if channel["id"] == channel_id:
                    channel["last_publish_at"] = utc_now_iso()
                    channel["last_publish_status"] = status

        self._storage.channels.update(_mutate)
