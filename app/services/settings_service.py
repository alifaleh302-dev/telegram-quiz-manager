"""Application settings (non-secret).

User accounts and login history live in :mod:`app.services.user_service`.
"""

from __future__ import annotations

from typing import Any

from app.core.timezones import is_valid_timezone
from app.services.audit_service import AuditService
from app.storage.json_manager import Storage
from app.storage.validators import ValidationError

_EDITABLE_KEYS = {
    "timezone",
    "publishing_enabled",
    "poll_options",
    "retry",
    "pacing",
    "daily_limit",
    "analytics",
    "transport",
}


class SettingsService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- general settings --------------------------------------------------
    def get(self) -> dict[str, Any]:
        return self._storage.settings.read()

    def update(self, payload: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        unknown = set(payload) - _EDITABLE_KEYS
        if unknown:
            raise ValidationError(f"إعدادات غير معروفة: {', '.join(sorted(unknown))}")

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for key, value in payload.items():
                if key == "retry":
                    document["retry"] = {
                        "max_attempts": int(value.get("max_attempts", 3)),
                        "delay_seconds": int(value.get("delay_seconds", 60)),
                    }
                elif key == "pacing":
                    interval = int(value.get("interval_seconds", 4))
                    max_wait = int(value.get("max_wait_seconds", 120))
                    if interval < 0 or max_wait < 0:
                        raise ValidationError("قيم التباعد الزمني لا يمكن أن تكون سالبة.")
                    document["pacing"] = {
                        "interval_seconds": interval,
                        "max_wait_seconds": max_wait,
                    }
                elif key == "poll_options":
                    document["poll_options"] = {
                        "explanation": bool(value.get("explanation", True)),
                        "open_period": max(0, int(value.get("open_period", 0))),
                        "anonymous": bool(value.get("anonymous", True)),
                        "allows_multiple_answers": False,
                    }
                elif key == "daily_limit":
                    document["daily_limit"] = max(0, int(value))
                elif key == "analytics":
                    document["analytics"] = {
                        "auto_collect": bool(value.get("auto_collect", False)),
                        "auto_collect_hours": max(1, int(value.get("auto_collect_hours", 6))),
                    }
                elif key == "transport":
                    mode = str(value.get("default_mode", "bot")).strip().lower()
                    if mode not in ("bot", "userbot"):
                        raise ValidationError("نوع النشر الافتراضي يجب أن يكون bot أو userbot.")
                    document["transport"] = {"default_mode": mode}
                elif key == "timezone":
                    zone = str(value or "").strip()
                    if not is_valid_timezone(zone):
                        raise ValidationError(f"المنطقة الزمنية «{zone}» غير معروفة.")
                    document["timezone"] = zone
                else:
                    document[key] = value
            return document

        document = self._storage.settings.update(_mutate)
        self._audit.record("settings.updated", actor=actor, message="تحديث الإعدادات")
        return document

    def set_publishing_enabled(self, enabled: bool, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            document["publishing_enabled"] = enabled

        self._storage.settings.update(_mutate)
        self._audit.record(
            "publishing.toggled",
            actor=actor,
            message="تشغيل النشر" if enabled else "إيقاف النشر",
        )

    @property
    def publishing_enabled(self) -> bool:
        return bool(self._storage.settings.read().get("publishing_enabled", True))
