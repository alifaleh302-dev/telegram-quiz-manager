"""Permission catalog for multi-user access control.

Two roles exist:

* ``admin`` — full control, including system settings and managing users.
* ``user`` — only the areas an admin explicitly grants.

Grants are stored per user in the database. The admin role implicitly holds
every grant, so a new grant added to :data:`GRANTS` is available to admins
without touching any user record.
"""

from __future__ import annotations

from typing import Any

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

# ``key`` -> Arabic label shown in the users admin UI.
GRANTS: dict[str, str] = {
    "questions": "إدارة بنك الأسئلة",
    "channels": "إدارة القنوات",
    "schedules": "إدارة الجداول",
    "publish": "النشر اليدوي الفوري",
    "automation": "تشغيل الأتمتة وإيقافها",
    "analytics": "الإحصائيات وجمع النتائج",
}


def clean_permissions(values: Any) -> list[str]:
    """Keep only known grant keys, preserving catalog order and de-duplicating."""
    if not isinstance(values, (list, tuple, set)):
        return []
    wanted = {str(v) for v in values}
    return [key for key in GRANTS if key in wanted]


def normalize_role(role: Any) -> str:
    """Coerce a stored role to a valid one.

    Legacy records predate roles; every account then had full access, so a
    missing/unknown role is treated as ``admin`` to preserve behaviour.
    """
    text = str(role or "").strip().lower()
    return text if text in ROLES else ROLE_ADMIN


def permissions_for(user: dict[str, Any] | None) -> frozenset[str]:
    """Effective grants for ``user`` (admins hold every grant)."""
    if not user:
        return frozenset()
    if normalize_role(user.get("role")) == ROLE_ADMIN:
        return frozenset(GRANTS) | {ROLE_ADMIN}
    return frozenset(clean_permissions(user.get("permissions")))


def has_permission(user: dict[str, Any] | None, permission: str) -> bool:
    return permission in permissions_for(user)


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and normalize_role(user.get("role")) == ROLE_ADMIN
