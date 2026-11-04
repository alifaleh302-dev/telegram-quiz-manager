"""Application settings (non-secret) plus user accounts and login history."""

from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.services.audit_service import AuditService
from app.storage.json_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError

_EDITABLE_KEYS = {
    "timezone",
    "publishing_enabled",
    "poll_options",
    "retry",
    "daily_limit",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
                elif key == "poll_options":
                    document["poll_options"] = {
                        "explanation": bool(value.get("explanation", True)),
                        "open_period": max(0, int(value.get("open_period", 0))),
                        "anonymous": bool(value.get("anonymous", True)),
                        "allows_multiple_answers": False,
                    }
                elif key == "daily_limit":
                    document["daily_limit"] = max(0, int(value))
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

    # --- users -------------------------------------------------------------
    def ensure_admin(self) -> None:
        """Create the bootstrap admin the first time the app runs."""
        document = self._storage.users.read()
        if document["users"]:
            return
        config = get_settings()
        if not config.admin_password:
            self._audit.record(
                "user.bootstrap_skipped",
                level="warning",
                message="ADMIN_PASSWORD غير مضبوط؛ لم يتم إنشاء حساب مدير.",
            )
            return
        self._storage.users.update(
            lambda doc: doc["users"].append(
                {
                    "username": config.admin_username,
                    "password_hash": hash_password(config.admin_password),
                    "email": "",
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
            )
        )
        self._audit.record(
            "user.created",
            message=f"إنشاء حساب المدير {config.admin_username}",
        )

    def get_user(self, username: str) -> dict[str, Any] | None:
        return next(
            (u for u in self._storage.users.read()["users"] if u["username"] == username),
            None,
        )

    def authenticate(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if user is None:
            return False
        return verify_password(user["password_hash"], password)

    def change_password(
        self, username: str, current: str, new: str, *, actor: str = "system"
    ) -> None:
        user = self.get_user(username)
        if user is None:
            raise ValidationError("المستخدم غير موجود.")
        if not verify_password(user["password_hash"], current):
            raise ValidationError("كلمة المرور الحالية غير صحيحة.")
        if len(new) < 8:
            raise ValidationError("كلمة المرور الجديدة يجب أن تكون 8 أحرف على الأقل.")

        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["users"]:
                if entry["username"] == username:
                    entry["password_hash"] = hash_password(new)
                    entry["updated_at"] = utc_now_iso()

        self._storage.users.update(_mutate)
        self._audit.record(
            "user.password_changed",
            actor=actor,
            level="warning",
            message=f"تغيير كلمة مرور {username}",
        )

    def update_profile(
        self, username: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> None:
        email = str(payload.get("email", "")).strip()
        if email and not _EMAIL_RE.match(email):
            raise ValidationError("البريد الإلكتروني غير صالح.")

        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["users"]:
                if entry["username"] == username:
                    entry["email"] = email
                    entry["updated_at"] = utc_now_iso()

        self._storage.users.update(_mutate)
        self._audit.record("user.profile_updated", actor=actor, message="تحديث الحساب")

    # --- login history -----------------------------------------------------
    def record_login(
        self, username: str, *, success: bool, ip: str = "", detail: str = ""
    ) -> None:
        entry = {
            "timestamp": utc_now_iso(),
            "username": username,
            "success": success,
            "ip": ip,
            "detail": detail,
        }

        def _mutate(document: dict[str, Any]) -> None:
            history = document.setdefault("login_history", [])
            history.insert(0, entry)
            del history[200:]

        self._storage.settings.update(_mutate)

    def login_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._storage.settings.read().get("login_history", [])[:limit]
