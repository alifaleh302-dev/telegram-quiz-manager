"""Security tests: password hashing, session signing and CSRF."""

from __future__ import annotations

import pytest

from app.core.security import (
    AuthenticationError,
    create_session_token,
    hash_password,
    load_session,
    needs_rehash,
    new_csrf_token,
    verify_csrf,
    verify_password,
)


def test_password_hash_is_not_reversible() -> None:
    digest = hash_password("CorrectHorse1!")
    assert digest != "CorrectHorse1!"
    assert digest.startswith("$argon2")


def test_verify_password_accepts_correct_and_rejects_wrong() -> None:
    digest = hash_password("CorrectHorse1!")
    assert verify_password(digest, "CorrectHorse1!")
    assert not verify_password(digest, "wrong-password")


def test_verify_password_handles_garbage_hash() -> None:
    assert not verify_password("not-a-hash", "anything")
    assert needs_rehash("not-a-hash")


def test_session_roundtrip_carries_username_and_csrf() -> None:
    token = create_session_token("admin")
    session = load_session(token)
    assert session.username == "admin"
    assert session.csrf_token
    assert session.session_id


def test_tampered_session_is_rejected() -> None:
    token = create_session_token("admin")
    with pytest.raises(AuthenticationError):
        load_session(token + "tampered")


def test_missing_session_is_rejected() -> None:
    with pytest.raises(AuthenticationError, match="missing"):
        load_session(None)
    with pytest.raises(AuthenticationError, match="missing"):
        load_session("")


def test_expired_session_is_rejected(monkeypatch) -> None:
    from app.core import config as config_module

    token = create_session_token("admin")
    monkeypatch.setattr(config_module.get_settings(), "session_max_age", -1)
    with pytest.raises(AuthenticationError, match="expired"):
        load_session(token)


def test_csrf_verification() -> None:
    session = load_session(create_session_token("admin"))
    assert verify_csrf(session, session.csrf_token)
    assert not verify_csrf(session, "wrong-token")
    assert not verify_csrf(session, None)
    assert not verify_csrf(session, "")


def test_csrf_tokens_are_unique() -> None:
    assert len({new_csrf_token() for _ in range(50)}) == 50
