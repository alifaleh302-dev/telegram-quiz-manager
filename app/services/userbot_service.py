"""Userbot accounts: metadata CRUD plus encrypted credential handling.

A *userbot* logs in as a real Telegram user account (api_id + api_hash +
session) instead of a Bot API token, which is useful when the target channel
does not allow bots to post polls. Several accounts can be registered; a
channel is then pinned to one account, or to a rotation pool.

Only non-secret metadata lives in the ``userbot_accounts`` collection. The api_id,
api_hash and session string are encrypted in the ``secrets`` collection through
:class:`~app.services.secret_store.SecretStore`, so the account list can be
backed up or exported without leaking credentials.
"""

from __future__ import annotations

from typing import Any

from app.services.audit_service import AuditService
from app.services.secret_store import SecretStore, validate_api_id, validate_api_hash
from app.storage.base import StorageBackend as Storage, utc_now_iso
from app.storage.validators import ValidationError

# Account lifecycle: a freshly created account has no session yet; ``active``
# means it is logged in and can publish; ``disabled`` keeps the credentials but
# takes the account out of rotation.
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

_ROLES = ("primary", "backup")


class UserbotAccountService:
    def __init__(self, storage: Storage, audit: AuditService, secrets: SecretStore) -> None:
        self._storage = storage
        self._audit = audit
        self._secrets = secrets

    # --- reads -------------------------------------------------------------
    def _all(self) -> list[dict[str, Any]]:
        return self._storage.userbot_accounts.read()["accounts"]

    def list_accounts(self) -> list[dict[str, Any]]:
        return self._all()

    def get(self, account_id: str) -> dict[str, Any] | None:
        return next((a for a in self._all() if a["id"] == account_id), None)

    def usable_accounts(self) -> list[dict[str, Any]]:
        """Accounts that can actually publish: logged in and not disabled."""
        return [
            account
            for account in self._all()
            if account.get("status") == STATUS_ACTIVE and account.get("enabled", True)
        ]

    def default_account(self) -> dict[str, Any] | None:
        usable = self.usable_accounts()
        if not usable:
            return None
        return next((a for a in usable if a.get("default")), usable[0])

    def view(self, account: dict[str, Any]) -> dict[str, Any]:
        """Account metadata merged with the masked credential status."""
        return {**account, "credentials": self._secrets.userbot_status(account["id"])}

    def views(self) -> list[dict[str, Any]]:
        return [self.view(account) for account in self._all()]

    # --- writes ------------------------------------------------------------
    def create(self, payload: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("اسم الحساب مطلوب.")
        api_id = validate_api_id(payload.get("api_id"))
        api_hash = validate_api_hash(payload.get("api_hash"))
        phone = str(payload.get("phone") or "").strip()
        role = str(payload.get("role") or "primary").strip().lower()
        if role not in _ROLES:
            raise ValidationError("نوع الحساب يجب أن يكون primary أو backup.")

        if any(a.get("phone") and a["phone"] == phone and phone for a in self._all()):
            raise ValidationError("يوجد حساب مسجّل بنفس رقم الهاتف.")
        if any(
            (self._secrets.userbot_credentials(a["id"]).get("api_id") == api_id)
            for a in self._all()
        ):
            # The same api_id may legitimately back several accounts, so this is
            # only a warning in the audit log, not a hard error.
            self._audit.record(
                "userbot.duplicate_api_id",
                actor=actor,
                level="warning",
                message=f"api_id {api_id} مستخدم في أكثر من حساب userbot.",
            )

        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["counter"] = int(document.get("counter", 0)) + 1
            account = {
                "id": f"UB{document['counter']:03d}",
                "name": name,
                "phone": phone,
                "role": role,
                "enabled": bool(payload.get("enabled", True)),
                "status": STATUS_PENDING,
                "default": not document["accounts"],
                "username": "",
                "telegram_id": None,
                "last_used_at": None,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            document["accounts"].append(account)
            return account

        account = self._storage.userbot_accounts.update(_mutate)
        self._secrets.save_userbot_credentials(
            account["id"], api_id=api_id, api_hash=api_hash, actor=actor
        )
        self._audit.record(
            "userbot.created",
            actor=actor,
            message=f"إضافة حساب userbot {account['name']} ({account['id']})",
        )
        return self.view(account)

    def update(
        self, account_id: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        account = self.get(account_id)
        if account is None:
            raise ValidationError(f"الحساب {account_id} غير موجود.")

        name = str(payload.get("name") or account["name"]).strip()
        if not name:
            raise ValidationError("اسم الحساب مطلوب.")
        role = str(payload.get("role") or account.get("role") or "primary").strip().lower()
        if role not in _ROLES:
            raise ValidationError("نوع الحساب يجب أن يكون primary أو backup.")

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["accounts"]:
                if entry["id"] == account_id:
                    entry.update(
                        {
                            "name": name,
                            "role": role,
                            "phone": str(payload.get("phone") or entry.get("phone") or "").strip(),
                            "updated_at": utc_now_iso(),
                        }
                    )
                    return entry
            raise KeyError(account_id)

        updated = self._storage.userbot_accounts.update(_mutate)

        # Rotating the credentials is optional: only touch them when provided.
        if payload.get("api_id") and payload.get("api_hash"):
            self._secrets.save_userbot_credentials(
                account_id,
                api_id=payload["api_id"],
                api_hash=payload["api_hash"],
                actor=actor,
            )
        elif payload.get("session"):
            self._secrets.save_userbot_session(account_id, str(payload["session"]), actor=actor)

        self._audit.record(
            "userbot.updated", actor=actor, message=f"تعديل حساب userbot {account_id}"
        )
        return self.view(updated)

    def set_enabled(
        self, account_id: str, enabled: bool, *, actor: str = "system"
    ) -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["accounts"]:
                if entry["id"] == account_id:
                    entry["enabled"] = enabled
                    entry["updated_at"] = utc_now_iso()
                    return entry
            raise KeyError(account_id)

        try:
            account = self._storage.userbot_accounts.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الحساب {account_id} غير موجود.") from exc
        self._audit.record(
            "userbot.toggled",
            actor=actor,
            message=f"{'تفعيل' if enabled else 'تعطيل'} حساب userbot {account_id}",
        )
        return self.view(account)

    def set_default(self, account_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            if not any(a["id"] == account_id for a in document["accounts"]):
                raise KeyError(account_id)
            for entry in document["accounts"]:
                entry["default"] = entry["id"] == account_id

        try:
            self._storage.userbot_accounts.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الحساب {account_id} غير موجود.") from exc
        self._audit.record(
            "userbot.default_set", actor=actor, message=f"تعيين {account_id} افتراضياً"
        )

    def mark_logged_in(
        self,
        account_id: str,
        *,
        session: str,
        username: str = "",
        telegram_id: int | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Record a successful login: store the session and flip to ``active``."""
        self._secrets.save_userbot_session(account_id, session, actor=actor)

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for entry in document["accounts"]:
                if entry["id"] == account_id:
                    entry["status"] = STATUS_ACTIVE
                    entry["username"] = username
                    entry["telegram_id"] = telegram_id
                    entry["last_error"] = None
                    entry["updated_at"] = utc_now_iso()
                    return entry
            raise KeyError(account_id)

        try:
            account = self._storage.userbot_accounts.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الحساب {account_id} غير موجود.") from exc
        self._audit.record(
            "userbot.logged_in",
            actor=actor,
            level="warning",
            message=f"تسجيل دخول حساب userbot {account_id} (@{username or '?'})",
        )
        return self.view(account)

    def record_error(self, account_id: str, message: str) -> None:
        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["accounts"]:
                if entry["id"] == account_id:
                    entry["last_error"] = message[:300]
                    entry["updated_at"] = utc_now_iso()
                    if entry["status"] == STATUS_ACTIVE and "session" in message.lower():
                        entry["status"] = STATUS_PENDING

        self._storage.userbot_accounts.update(_mutate)

    def record_used(self, account_id: str) -> None:
        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["accounts"]:
                if entry["id"] == account_id:
                    entry["last_used_at"] = utc_now_iso()

        self._storage.userbot_accounts.update(_mutate)

    def delete(self, account_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            before = len(document["accounts"])
            document["accounts"] = [a for a in document["accounts"] if a["id"] != account_id]
            if len(document["accounts"]) == before:
                raise KeyError(account_id)
            if document["accounts"] and not any(a.get("default") for a in document["accounts"]):
                document["accounts"][0]["default"] = True

        try:
            self._storage.userbot_accounts.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الحساب {account_id} غير موجود.") from exc
        self._secrets.clear_userbot_credentials(account_id, actor=actor)
        self._audit.record(
            "userbot.deleted",
            actor=actor,
            level="warning",
            message=f"حذف حساب userbot {account_id}",
        )
