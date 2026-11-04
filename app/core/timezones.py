"""IANA timezone helpers.

``zoneinfo`` resolves names from the system tz database (or the ``tzdata``
package). A slim container image may ship neither, and a single bad name from
stored data must never be able to crash startup — so every lookup goes through
:func:`safe_timezone`, which validates the name and degrades to UTC.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = "UTC"

_KNOWN: frozenset[str] | None = None


def known_timezones() -> frozenset[str]:
    """Every IANA zone this interpreter can resolve (cached)."""
    global _KNOWN
    if _KNOWN is None:
        try:
            _KNOWN = frozenset(available_timezones()) | {UTC}
        except Exception:  # pragma: no cover - only when tz data is unreadable
            _KNOWN = frozenset({UTC})
    return _KNOWN


def is_valid_timezone(name: str | None) -> bool:
    """True when ``name`` is an IANA zone this interpreter can load."""
    text = (name or "").strip()
    if not text:
        return False
    if text in known_timezones():
        return True
    # ``available_timezones`` can be incomplete on minimal images; a direct
    # lookup is the final authority.
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def safe_timezone(name: str | None) -> ZoneInfo:
    """Return a usable :class:`ZoneInfo`, falling back to UTC.

    Never raises: a schedule or setting pointing at an unknown zone should
    still run (in UTC) instead of taking the whole app down.
    """
    text = (name or "").strip() or UTC
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(UTC)
