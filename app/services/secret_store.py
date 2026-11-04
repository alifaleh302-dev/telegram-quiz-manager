"""Encrypted storage for runtime-editable secrets (the Telegram bot token).

The token can be set from the dashboard, so it has to live somewhere durable.
It is kept out of ``settings.json`` and stored encrypted in ``secrets.json``,
using a key derived from ``SECRET_KEY`` (see :mod:`app.core.security`). The raw
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
from app.storage.json_manager import Storage, utc_now_iso
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
