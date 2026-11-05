"""Telegram Mini App identity: initData verification and student sessions.

A Mini App runs inside Telegram and receives a signed ``initData`` string. We
never trust ``initDataUnsafe`` (the browser can set anything there) — identity
comes only from :func:`verify_init_data`, which recomputes Telegram's HMAC with
a key derived from the bot token and compares it in constant time.

After a successful check the backend mints a short-lived signed *student token*
(:func:`create_student_token`) that the Mini App sends on every later request,
so identity is decided by the server on each call, not re-derived client-side.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

# Telegram signs the WebApp payload with a key derived from this literal.
_WEBAPP_DATA_KEY = b"WebAppData"
# A signed student token older than this is refused (mirrors session lifetime).
STUDENT_TOKEN_MAX_AGE = 12 * 60 * 60
# initData itself is short-lived; refuse anything older than this.
DEFAULT_AUTH_MAX_AGE = 24 * 60 * 60


class TelegramAuthError(Exception):
    """Raised when initData is missing, malformed, expired or badly signed."""


@dataclass(frozen=True)
class TelegramUser:
    user_id: int
    first_name: str
    last_name: str
    username: str
    language_code: str
    photo_url: str

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username or f"طالب {self.user_id}"


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(_WEBAPP_DATA_KEY, bot_token.encode(), hashlib.sha256).digest()


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = DEFAULT_AUTH_MAX_AGE,
    now: datetime | None = None,
) -> TelegramUser:
    """Validate a Mini App ``initData`` string and return the verified user.

    Raises :class:`TelegramAuthError` for anything that does not verify. The
    signature comparison uses :func:`hmac.compare_digest` to avoid leaking
    timing information.
    """
    if not init_data or not init_data.strip():
        raise TelegramAuthError("بيانات Telegram مفقودة.")
    if not bot_token:
        raise TelegramAuthError("توكن البوت غير مهيأ.")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise TelegramAuthError("صيغة بيانات Telegram غير صحيحة.") from exc

    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise TelegramAuthError("توقيع بيانات Telegram مفقود.")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    expected = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise TelegramAuthError("توقيع بيانات Telegram غير صالح.")

    raw_auth_date = pairs.get("auth_date", "")
    try:
        auth_date = int(raw_auth_date)
    except (TypeError, ValueError) as exc:
        raise TelegramAuthError("تاريخ مصادقة Telegram غير صالح.") from exc

    reference = (now or datetime.now(UTC)).timestamp()
    if auth_date > reference + 60:
        raise TelegramAuthError("تاريخ مصادقة Telegram في المستقبل.")
    if max_age and reference - auth_date > max_age:
        raise TelegramAuthError("انتهت صلاحية بيانات Telegram؛ أعد فتح التطبيق.")

    raw_user = pairs.get("user", "")
    if not raw_user:
        raise TelegramAuthError("لا يوجد مستخدم في بيانات Telegram.")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise TelegramAuthError("بيانات المستخدم غير صالحة.") from exc

    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramAuthError("معرّف مستخدم Telegram غير صالح.") from exc

    return TelegramUser(
        user_id=user_id,
        first_name=str(user.get("first_name", "")).strip(),
        last_name=str(user.get("last_name", "")).strip(),
        username=str(user.get("username", "")).strip(),
        language_code=str(user.get("language_code", "")).strip(),
        photo_url=str(user.get("photo_url", "")).strip(),
    )


# --- Student session token -------------------------------------------------

def _student_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="tqm-student")


def create_student_token(telegram_user_id: int) -> str:
    payload = {
        "tg": int(telegram_user_id),
        "jti": secrets.token_urlsafe(12),
        "iat": datetime.now(UTC).isoformat(),
    }
    return _student_serializer().dumps(payload)


def load_student_token(token: str | None) -> int:
    """Return the verified Telegram user id, or raise :class:`TelegramAuthError`."""
    if not token:
        raise TelegramAuthError("جلسة الطالب مفقودة.")
    try:
        payload = _student_serializer().loads(token, max_age=STUDENT_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise TelegramAuthError("انتهت جلسة الطالب.") from exc
    except BadSignature as exc:
        raise TelegramAuthError("جلسة الطالب غير صالحة.") from exc
    try:
        return int(payload["tg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramAuthError("جلسة الطالب غير صالحة.") from exc


# --- Test / tooling helper -------------------------------------------------

def build_init_data(
    user: dict[str, Any], bot_token: str, *, auth_date: int | None = None
) -> str:
    """Produce a correctly signed ``initData`` string (tests and tooling).

    Kept beside the verifier so a test signs exactly the way Telegram does; a
    hand-rolled signer in the test suite could drift from the real format.
    """
    from urllib.parse import urlencode

    stamp = auth_date if auth_date is not None else int(datetime.now(UTC).timestamp())
    pairs = {
        "auth_date": str(stamp),
        "query_id": "AAExampleQueryId",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    pairs["hash"] = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(pairs)
