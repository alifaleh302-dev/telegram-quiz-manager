"""Student identity and profile management.

A student is identified by their Telegram user id, which the backend only
accepts after verifying the Mini App ``initData`` (see
:mod:`app.core.telegram_auth`). There is no separate username/password: the
Telegram identity *is* the account.

This is deliberately separate from :mod:`app.services.user_service` — the
existing ``users`` collection holds *dashboard administrators*, and mixing the
two would put student rows in front of the admin login screen.
"""

from __future__ import annotations

from typing import Any

from app.services.audit_service import AuditService
from app.storage.base import StorageBackend as Storage, utc_now_iso


class StudentService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _next_id(counter: int) -> str:
        return f"ST{counter:05d}"

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.students.read()["students"]

    def get(self, student_id: str) -> dict[str, Any] | None:
        return next((s for s in self._all() if s["id"] == student_id), None)

    def by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        return next(
            (
                s
                for s in self._all()
                if int(s.get("telegram_user_id", 0)) == int(telegram_user_id)
            ),
            None,
        )

    def list_students(self, search: str = "") -> list[dict[str, Any]]:
        students = self._all()
        text = (search or "").strip().lower().lstrip("@")
        if text:
            students = [
                s
                for s in students
                if text in str(s.get("first_name", "")).lower()
                or text in str(s.get("last_name", "")).lower()
                or text in str(s.get("username", "")).lower()
                or text in self.display_name(s).lower()
                or text in str(s.get("telegram_user_id", ""))
            ]
        return sorted(
            students, key=lambda s: s.get("last_seen_at") or "", reverse=True
        )

    # --- identity ----------------------------------------------------------
    def touch(self, telegram: Any, *, actor: str = "system") -> dict[str, Any]:
        """Upsert the student behind a verified Telegram identity.

        ``telegram`` is an :class:`app.core.telegram_auth.TelegramUser`. New
        students are created; existing ones have their profile refreshed and
        ``last_seen_at`` bumped. Returns the stored record.
        """
        now = utc_now_iso()
        tg_id = int(telegram.user_id)

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for student in document["students"]:
                if int(student.get("telegram_user_id", 0)) == tg_id:
                    student.update(
                        {
                            "first_name": telegram.first_name or student.get("first_name", ""),
                            "last_name": telegram.last_name or student.get("last_name", ""),
                            "username": telegram.username or student.get("username", ""),
                            "language_code": telegram.language_code
                            or student.get("language_code", ""),
                            "photo_url": telegram.photo_url or student.get("photo_url", ""),
                            "last_seen_at": now,
                        }
                    )
                    return student

            document["counter"] = int(document.get("counter", 0)) + 1
            student = {
                "id": self._next_id(document["counter"]),
                "telegram_user_id": tg_id,
                "first_name": telegram.first_name,
                "last_name": telegram.last_name,
                "username": telegram.username,
                "language_code": telegram.language_code,
                "photo_url": telegram.photo_url,
                "notifications_enabled": True,
                "created_at": now,
                "last_seen_at": now,
            }
            document["students"].append(student)
            return student

        existed = self.by_telegram_id(tg_id) is not None
        student = self._storage.students.update(_mutate)
        if not existed:
            self._audit.record(
                "student.registered",
                actor=actor,
                message=f"تسجيل طالب جديد: {student['id']}",
                student_id=student["id"],
            )
        return student

    def set_notifications(
        self, student_id: str, enabled: bool, *, actor: str = "system"
    ) -> dict[str, Any]:
        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for student in document["students"]:
                if student["id"] == student_id:
                    student["notifications_enabled"] = bool(enabled)
                    return student
            raise KeyError(student_id)

        try:
            student = self._storage.students.update(_mutate)
        except KeyError as exc:
            raise ValueError(f"الطالب {student_id} غير موجود.") from exc
        return student

    # --- aggregates --------------------------------------------------------
    def view(self, student: dict[str, Any] | None) -> dict[str, Any] | None:
        """Public shape for the Mini App / admin tables (no secrets to hide yet)."""
        if not student:
            return None
        return {
            "id": student["id"],
            "telegram_user_id": student.get("telegram_user_id"),
            "name": self.display_name(student),
            "username": student.get("username", ""),
            "photo_url": student.get("photo_url", ""),
            "notifications_enabled": bool(student.get("notifications_enabled", True)),
            "created_at": student.get("created_at", ""),
            "last_seen_at": student.get("last_seen_at", ""),
        }

    @staticmethod
    def display_name(student: dict[str, Any]) -> str:
        full = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip()
        return full or student.get("username") or f"طالب {student.get('telegram_user_id')}"

    def count(self) -> int:
        return len(self._all())

    def recent_active(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.list_students()[:limit]
