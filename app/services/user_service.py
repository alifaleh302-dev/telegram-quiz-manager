"""User accounts: authentication, profiles and admin-managed access.

Accounts live in ``users.json``. Login history lives in ``settings.json``
(``login_history``) because it is display-only state, not an account attribute.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.permissions import (
    GRANTS,
    ROLE_ADMIN,
    ROLE_USER,
    clean_permissions,
    normalize_role,
    permissions_for,
)
from app.core.security import hash_password, verify_password
from app.services.audit_service import AuditService
from app.storage.json_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
LOGIN_HISTORY_LIMIT = 200


class UserService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- bootstrap ---------------------------------------------------------
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
                    "role": ROLE_ADMIN,
                    "permissions": [],
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
            )
        )
        self._audit.record(
            "user.created",
            message=f"إنشاء حساب المدير {config.admin_username}",
        )

    # --- lookups -----------------------------------------------------------
    def get_user(self, username: str) -> dict[str, Any] | None:
        return next(
            (u for u in self._storage.users.read()["users"] if u["username"] == username),
            None,
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self._storage.users.read()["users"]

    def view(self, user: dict[str, Any]) -> dict[str, Any]:
        """Public shape for templates/APIs: never exposes the password hash."""
        return {
            "username": user["username"],
            "email": user.get("email", ""),
            "role": normalize_role(user.get("role")),
            "permissions": clean_permissions(user.get("permissions")),
            "created_at": user.get("created_at", ""),
            "updated_at": user.get("updated_at", ""),
        }

    def list_views(self) -> list[dict[str, Any]]:
        return [self.view(u) for u in self.list_users()]

    def permissions_of(self, username: str) -> frozenset[str]:
        return permissions_for(self.get_user(username))

    def count_admins(self) -> int:
        return sum(1 for u in self.list_users() if normalize_role(u.get("role")) == ROLE_ADMIN)

    def authenticate(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if user is None:
            return False
        return verify_password(user["password_hash"], password)

    # --- admin CRUD --------------------------------------------------------
    def create_user(
        self, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        username = str(payload.get("username", "")).strip()
        if not _USERNAME_RE.match(username):
            raise ValidationError(
                "اسم المستخدم يجب أن يكون بين 3 و32 حرفاً إنجليزياً (حروف، أرقام، . _ -)."
            )
        password = str(payload.get("password", ""))
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"كلمة المرور يجب أن تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل."
            )
        email = str(payload.get("email", "")).strip()
        if email and not _EMAIL_RE.match(email):
            raise ValidationError("البريد الإلكتروني غير صالح.")
        if self.get_user(username) is not None:
            raise ValidationError(f"المستخدم {username} موجود بالفعل.")
        role = str(payload.get("role", ROLE_USER)).strip().lower()
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise ValidationError("الدور غير صالح.")
        permissions = clean_permissions(payload.get("permissions"))

        now = utc_now_iso()
        record = {
            "username": username,
            "password_hash": hash_password(password),
            "email": email,
            "role": role,
            "permissions": [] if role == ROLE_ADMIN else permissions,
            "created_at": now,
            "updated_at": now,
        }
        self._storage.users.update(lambda doc: doc["users"].append(record))
        self._audit.record(
            "user.created",
            actor=actor,
            message=f"إنشاء المستخدم {username}",
            username=username,
        )
        return self.view(record)

    def update_user(
        self, username: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        target = self.get_user(username)
        if target is None:
            raise ValidationError("المستخدم غير موجود.")

        role = str(payload.get("role", normalize_role(target.get("role")))).strip().lower()
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise ValidationError("الدور غير صالح.")
        email = str(payload.get("email", target.get("email", ""))).strip()
        if email and not _EMAIL_RE.match(email):
            raise ValidationError("البريد الإلكتروني غير صالح.")
        permissions = clean_permissions(payload.get("permissions"))

        # Never let the last admin be demoted: that would lock everyone out.
        if (
            normalize_role(target.get("role")) == ROLE_ADMIN
            and role != ROLE_ADMIN
            and self.count_admins() <= 1
        ):
            raise ValidationError("لا يمكن إزالة دور المدير عن آخر حساب مدير.")

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["users"]:
                if entry["username"] == username:
                    entry["role"] = role
                    entry["email"] = email
                    entry["permissions"] = [] if role == ROLE_ADMIN else permissions
                    entry["updated_at"] = utc_now_iso()
                    return entry
            raise ValidationError("المستخدم غير موجود.")

        updated = self._storage.users.update(_mutate)
        self._audit.record(
            "user.updated",
            actor=actor,
            message=f"تحديث صلاحيات المستخدم {username}",
            username=username,
        )
        return self.view(updated)

    def set_permissions(
        self, username: str, permissions: Any, *, actor: str = "system"
    ) -> dict[str, Any]:
        return self.update_user(username, {"permissions": permissions}, actor=actor)

    def set_password(
        self, username: str, new_password: str, *, actor: str = "system"
    ) -> None:
        if self.get_user(username) is None:
            raise ValidationError("المستخدم غير موجود.")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"كلمة المرور يجب أن تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل."
            )

        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["users"]:
                if entry["username"] == username:
                    entry["password_hash"] = hash_password(new_password)
                    entry["updated_at"] = utc_now_iso()

        self._storage.users.update(_mutate)
        self._audit.record(
            "user.password_reset",
            actor=actor,
            level="warning",
            message=f"إعادة تعيين كلمة مرور {username}",
            username=username,
        )

    def delete_user(self, username: str, *, actor: str = "system") -> None:
        target = self.get_user(username)
        if target is None:
            raise ValidationError("المستخدم غير موجود.")
        if username == actor:
            raise ValidationError("لا يمكنك حذف حسابك الحالي.")
        if normalize_role(target.get("role")) == ROLE_ADMIN and self.count_admins() <= 1:
            raise ValidationError("لا يمكن حذف آخر حساب مدير.")

        self._storage.users.update(
            lambda doc: doc.__setitem__(
                "users", [u for u in doc["users"] if u["username"] != username]
            )
        )
        self._audit.record(
            "user.deleted",
            actor=actor,
            level="warning",
            message=f"حذف المستخدم {username}",
            username=username,
        )

    # --- self service ------------------------------------------------------
    def change_password(
        self, username: str, current: str, new: str, *, actor: str = "system"
    ) -> None:
        user = self.get_user(username)
        if user is None:
            raise ValidationError("المستخدم غير موجود.")
        if not verify_password(user["password_hash"], current):
            raise ValidationError("كلمة المرور الحالية غير صحيحة.")
        if len(new) < MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"كلمة المرور الجديدة يجب أن تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل."
            )

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
            del history[LOGIN_HISTORY_LIMIT:]

        self._storage.settings.update(_mutate)

    def login_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._storage.settings.read().get("login_history", [])[:limit]


def grant_catalog() -> dict[str, str]:
    """The ``key -> label`` map used to render permission checkboxes."""
    return dict(GRANTS)
