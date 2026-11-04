"""Encrypted storage for runtime-editable secrets (the Telegram bot token).

The token can be set from the dashboard, so it has to live somewhere durable.
It is kept out of the general settings and stored encrypted in the ``secrets``
collection, using a key derived from ``SECRET_KEY`` (see :mod:`app.core.security`). The raw
value is never returned by the API or written to the log; only a masked form is
exposed for display.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.security import (
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from app.services.audit_service import AuditService
from app.storage.sqlite_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError

# A Telegram bot token is "<numeric id>:<35 url-safe chars>".
_MIN_TOKEN_LENGTH = 20


def validate_bot_token(token: str) -> str:
    token = (token or "").strip()
    if len(token) < _MIN_TOKEN_LENGTH:
        raise ValidationError("التوكن قصير جداً؛ انسخه كاملاً من @BotFather.")
    if ":" not in token:
        raise ValidationError("صيغة التوكن غير صحيحة (يجب أن يحتوي على «:»).")
    if any(character.isspace() for character in token):
        raise ValidationError("التوكن لا يجب أن يحتوي على مسافات.")
    return token


def validate_api_id(api_id: str | int) -> int:
    """A Telegram ``api_id`` is the numeric id from my.telegram.org."""
    text = str(api_id or "").strip()
    if not text.isdigit():
        raise ValidationError("api_id يجب أن يكون رقماً (من my.telegram.org).")
    value = int(text)
    if value <= 0:
        raise ValidationError("api_id غير صالح.")
    return value


def validate_api_hash(api_hash: str) -> str:
    """``api_hash`` is a 32-character hexadecimal string."""
    text = (api_hash or "").strip()
    if len(text) != 32 or not all(c in "0123456789abcdefABCDEF" for c in text):
        raise ValidationError("api_hash يجب أن يكون 32 حرفاً ست عشرياً.")
    return text.lower()


class SecretStore:
    """Reads and writes encrypted secrets, falling back to the environment."""

    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- reading -----------------------------------------------------------
    def _stored_token(self) -> str:
        """The decrypted stored token, or "" when absent/unreadable."""
        document = self._storage.secrets.read()
        ciphertext = document.get("telegram_bot_token")
        if not ciphertext:
            return ""
        try:
            return decrypt_secret(ciphertext)
        except SecretDecryptionError:
            self._audit.record(
                "secret.decrypt_failed",
                level="error",
                message=(
                    "تعذّر فك تشفير توكن البوت المخزّن (تغيّرت SECRET_KEY؟). "
                    "أعد إدخال التوكن من الإعدادات."
                ),
            )
            return ""

    def bot_token(self) -> str:
        """Effective token: a dashboard-set secret wins over the environment."""
        stored = self._stored_token()
        if stored:
            return stored
        return get_settings().telegram_bot_token.strip()

    def has_stored_token(self) -> bool:
        return bool(self._storage.secrets.read().get("telegram_bot_token"))

    def source(self) -> str:
        """Where the effective token comes from: ``stored``, ``env`` or ``none``."""
        if self.has_stored_token():
            return "stored"
        return "env" if get_settings().telegram_bot_token.strip() else "none"

    def status(self) -> dict[str, Any]:
        token = self.bot_token()
        document = self._storage.secrets.read()
        return {
            "configured": bool(token),
            "source": self.source(),
            "masked": mask_secret(token),
            "updated_at": document.get("telegram_bot_token_updated_at"),
        }

    # --- writing -----------------------------------------------------------
    def set_bot_token(self, token: str, *, actor: str = "system") -> dict[str, Any]:
        token = validate_bot_token(token)
        ciphertext = encrypt_secret(token)
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> None:
            document["telegram_bot_token"] = ciphertext
            document["telegram_bot_token_updated_at"] = now

        self._storage.secrets.update(_mutate)
        self._audit.record(
            "secret.token_set",
            actor=actor,
            level="warning",
            message="تحديث توكن بوت تيليجرام من لوحة التحكم",
        )
        return self.status()

    def clear_bot_token(self, *, actor: str = "system") -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> None:
            document["telegram_bot_token"] = None
            document["telegram_bot_token_updated_at"] = utc_now_iso()

        self._storage.secrets.update(_mutate)
        self._audit.record(
            "secret.token_cleared",
            actor=actor,
            level="warning",
            message="حذف توكن البوت المخزّن؛ سيُستخدم توكن ملف .env إن وُجد.",
        )
        return self.status()

    # --- userbot credentials (api_id / api_hash / session) -----------------
    # Each userbot account keeps its own encrypted credentials, keyed by the
    # account id. The document shape is
    #   {"userbot": {"<account_id>": {"api_id": <cipher>, "api_hash": <cipher>,
    #                                 "session": <cipher>, "updated_at": iso}}}
    # so nothing here is ever written in clear text.

    def _userbot_bucket(self) -> dict[str, Any]:
        return self._storage.secrets.read().get("userbot") or {}

    def _decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            return decrypt_secret(value)
        except SecretDecryptionError:
            self._audit.record(
                "secret.decrypt_failed",
                level="error",
                message=(
                    "تعذّر فك تشفير بيانات حساب userbot (تغيّرت SECRET_KEY؟). "
                    "أعد إدخال المفاتيح من الإعدادات."
                ),
            )
            return ""

    def save_userbot_credentials(
        self,
        account_id: str,
        *,
        api_id: int | str,
        api_hash: str,
        session: str = "",
        actor: str = "system",
    ) -> None:
        """Store (or update) the encrypted credentials of one userbot account."""
        normalized_id = str(validate_api_id(api_id))
        normalized_hash = validate_api_hash(api_hash)
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> None:
            bucket = document.setdefault("userbot", {})
            entry = bucket.setdefault(account_id, {})
            entry["api_id"] = encrypt_secret(normalized_id)
            entry["api_hash"] = encrypt_secret(normalized_hash)
            if session:
                entry["session"] = encrypt_secret(session)
            entry["updated_at"] = now

        self._storage.secrets.update(_mutate)
        self._audit.record(
            "secret.userbot_saved",
            actor=actor,
            level="warning",
            message=f"حفظ مفاتيح حساب userbot {account_id}",
        )

    def save_userbot_session(
        self, account_id: str, session: str, *, actor: str = "system"
    ) -> None:
        """Persist only the session string (used after a successful login)."""
        session = (session or "").strip()
        if not session:
            raise ValidationError("نص الجلسة فارغ.")

        def _mutate(document: dict[str, Any]) -> None:
            bucket = document.setdefault("userbot", {})
            entry = bucket.setdefault(account_id, {})
            entry["session"] = encrypt_secret(session)
            entry["updated_at"] = utc_now_iso()

        self._storage.secrets.update(_mutate)
        self._audit.record(
            "secret.userbot_session",
            actor=actor,
            level="warning",
            message=f"حفظ جلسة حساب userbot {account_id}",
        )

    def userbot_credentials(self, account_id: str) -> dict[str, Any]:
        """Decrypted credentials for one account (empty strings when missing)."""
        entry = self._userbot_bucket().get(account_id) or {}
        api_id_text = self._decrypt(entry.get("api_id"))
        return {
            "api_id": int(api_id_text) if api_id_text.isdigit() else None,
            "api_hash": self._decrypt(entry.get("api_hash")),
            "session": self._decrypt(entry.get("session")),
            "updated_at": entry.get("updated_at"),
        }

    def userbot_has_session(self, account_id: str) -> bool:
        return bool((self._userbot_bucket().get(account_id) or {}).get("session"))

    def userbot_status(self, account_id: str) -> dict[str, Any]:
        """Masked view for the dashboard: never exposes the raw secrets."""
        entry = self._userbot_bucket().get(account_id) or {}
        api_id_text = self._decrypt(entry.get("api_id"))
        return {
            "configured": bool(entry.get("api_id") and entry.get("api_hash")),
            "has_session": bool(entry.get("session")),
            "api_id": api_id_text,
            "api_hash_masked": mask_secret(self._decrypt(entry.get("api_hash"))),
            "session_masked": mask_secret(self._decrypt(entry.get("session"))),
            "updated_at": entry.get("updated_at"),
        }

    def clear_userbot_credentials(self, account_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            bucket = document.get("userbot") or {}
            bucket.pop(account_id, None)
            document["userbot"] = bucket

        self._storage.secrets.update(_mutate)
        self._audit.record(
            "secret.userbot_cleared",
            actor=actor,
            level="warning",
            message=f"حذف مفاتيح حساب userbot {account_id}",
        )
