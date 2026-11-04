"""Security helpers: password hashing, signed sessions and CSRF tokens.

Sessions are stateless: the signed cookie carries the username, the session id
and the CSRF token. ``itsdangerous`` provides both the signature and the
expiry check, so a leaked cookie cannot be forged or replayed forever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

_hasher = PasswordHasher()

SESSION_COOKIE = "tqm_session"
CSRF_HEADER = "X-CSRF-Token"


class AuthenticationError(Exception):
    """Raised when a session cookie is missing, invalid or expired."""


class SecretDecryptionError(Exception):
    """Raised when an encrypted secret cannot be decrypted with the current key."""


# --- Passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


# --- Sessions --------------------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="tqm-session")


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Session:
    username: str
    session_id: str
    csrf_token: str
    issued_at: str

    @property
    def issued_at_dt(self) -> datetime:
        return datetime.fromisoformat(self.issued_at)


def create_session_token(username: str, session_id: str | None = None) -> str:
    payload: dict[str, Any] = {
        "u": username,
        "sid": session_id or new_session_id(),
        "csrf": new_csrf_token(),
        "iat": datetime.now(UTC).isoformat(),
    }
    return _serializer().dumps(payload)


def load_session(token: str | None) -> Session:
    if not token:
        raise AuthenticationError("missing session cookie")
    try:
        payload = _serializer().loads(token, max_age=get_settings().session_max_age)
    except SignatureExpired as exc:
        raise AuthenticationError("session expired") from exc
    except BadSignature as exc:
        raise AuthenticationError("invalid session signature") from exc

    try:
        return Session(
            username=str(payload["u"]),
            session_id=str(payload["sid"]),
            csrf_token=str(payload["csrf"]),
            issued_at=str(payload["iat"]),
        )
    except KeyError as exc:
        raise AuthenticationError("malformed session payload") from exc


def verify_csrf(session: Session, submitted: str | None) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(session.csrf_token, submitted)


# --- Secret encryption -----------------------------------------------------
# Stored secrets (currently the Telegram bot token) are encrypted with a key
# derived from ``SECRET_KEY``, so the JSON file never holds the token in clear
# and a copy of the data directory alone is not enough to recover it.

def _fernet() -> Fernet:
    secret = get_settings().secret_key
    if not secret:
        raise SecretDecryptionError(
            "SECRET_KEY غير مضبوط؛ لا يمكن تشفير الأسرار المخزّنة."
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "تعذّر فك تشفير السر؛ قد تكون SECRET_KEY قد تغيّرت."
        ) from exc


def mask_secret(value: str, *, visible: int = 4) -> str:
    """Render a secret for display without revealing it."""
    value = value or ""
    if not value:
        return ""
    if len(value) <= visible:
        return "•" * len(value)
    return "•" * (len(value) - visible) + value[-visible:]
