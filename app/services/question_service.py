"""Question bank: CRUD, search, bulk actions, import and export."""

from __future__ import annotations

import random
from typing import Any

from app.core.logging import get_logger
from app.services.audit_service import AuditService
from app.storage.base import StorageBackend as Storage, utc_now_iso
from app.storage.validators import (
    ValidationError,
    normalize_imported_question,
    validate_question_payload,
)

logger = get_logger(__name__)


class QuestionService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _next_id(counter: int) -> str:
        return f"Q{counter:04d}"

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.questions.read()["questions"]

    def get(self, question_id: str) -> dict[str, Any] | None:
        return next(
            (q for q in self._all() if q["id"] == question_id), None
        )

    # --- CRUD --------------------------------------------------------------
    def create(
        self, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        data = validate_question_payload(payload)
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["counter"] = int(document.get("counter", 0)) + 1
            question = {
                "id": self._next_id(document["counter"]),
                **data,
                "created_at": now,
                "updated_at": now,
            }
            document["questions"].append(question)
            return question

        question = self._storage.questions.update(_mutate)
        self._audit.record(
            "question.created",
            actor=actor,
            message=f"إضافة السؤال {question['id']}",
            question_id=question["id"],
        )
        return question

    def update(
        self, question_id: str, payload: dict[str, Any], *, actor: str = "system"
    ) -> dict[str, Any]:
        data = validate_question_payload(payload)

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for question in document["questions"]:
                if question["id"] == question_id:
                    question.update(data)
                    question["updated_at"] = utc_now_iso()
                    return question
            raise KeyError(question_id)

        try:
            question = self._storage.questions.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"السؤال {question_id} غير موجود.") from exc

        self._audit.record(
            "question.updated",
            actor=actor,
            message=f"تعديل السؤال {question_id}",
            question_id=question_id,
        )
        return question

    def delete(self, question_id: str, *, actor: str = "system") -> None:
        def _mutate(document: dict[str, Any]) -> None:
            before = len(document["questions"])
            document["questions"] = [
                q for q in document["questions"] if q["id"] != question_id
            ]
            if len(document["questions"]) == before:
                raise KeyError(question_id)

        try:
            self._storage.questions.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"السؤال {question_id} غير موجود.") from exc

        self._audit.record(
            "question.deleted",
            actor=actor,
            level="warning",
            message=f"حذف السؤال {question_id}",
            question_id=question_id,
        )

    def duplicate(self, question_id: str, *, actor: str = "system") -> dict[str, Any]:
        source = self.get(question_id)
        if source is None:
            raise ValidationError(f"السؤال {question_id} غير موجود.")
        payload = {
            k: source[k]
            for k in (
                "question",
                "options",
                "correct_option_id",
                "options_feedback",
                "explanation",
                "category",
                "difficulty",
            )
            if k in source
        }
        payload["status"] = "draft"
        payload["question"] = f"{source['question']} (نسخة)"
        return self.create(payload, actor=actor)

    # --- bulk --------------------------------------------------------------
    def bulk_delete(self, ids: list[str], *, actor: str = "system") -> int:
        target = set(ids)

        def _mutate(document: dict[str, Any]) -> int:
            before = len(document["questions"])
            document["questions"] = [
                q for q in document["questions"] if q["id"] not in target
            ]
            return before - len(document["questions"])

        removed = self._storage.questions.update(_mutate)
        self._audit.record(
            "question.bulk_deleted",
            actor=actor,
            level="warning",
            message=f"حذف {removed} سؤالاً",
            count=removed,
        )
        return removed

    def bulk_set_status(
        self, ids: list[str], status: str, *, actor: str = "system"
    ) -> int:
        if status not in ("draft", "ready"):
            raise ValidationError("الحالة المسموحة للتعديل الجماعي هي draft أو ready.")
        target = set(ids)

        def _mutate(document: dict[str, Any]) -> int:
            changed = 0
            for question in document["questions"]:
                if question["id"] in target and question["status"] != "published":
                    question["status"] = status
                    question["updated_at"] = utc_now_iso()
                    changed += 1
            return changed

        changed = self._storage.questions.update(_mutate)
        self._audit.record(
            "question.bulk_status",
            actor=actor,
            message=f"تغيير حالة {changed} سؤالاً إلى {status}",
        )
        return changed

    # --- queries -----------------------------------------------------------
    def list_questions(
        self,
        *,
        search: str = "",
        category: str = "",
        status: str = "",
        difficulty: str = "",
    ) -> list[dict[str, Any]]:
        questions = self._all()
        needle = search.strip().lower()
        if needle:
            questions = [
                q
                for q in questions
                if needle in q["question"].lower()
                or any(needle in opt.lower() for opt in q["options"])
                or needle in q["id"].lower()
            ]
        if category:
            questions = [q for q in questions if q["category"] == category]
        if status:
            questions = [q for q in questions if q["status"] == status]
        if difficulty:
            questions = [q for q in questions if q["difficulty"] == difficulty]
        return questions

    def categories(self) -> list[str]:
        return sorted({q["category"] for q in self._all() if q.get("category")})

    @staticmethod
    def filter_by_channel_publish(
        questions: list[dict[str, Any]],
        published_pairs: set[tuple[str, str]],
        channel_id: str,
        state: str,
    ) -> list[dict[str, Any]]:
        """Keep questions by whether they went out on ``channel_id``.

        ``state`` is ``published`` (only those already sent to the channel) or
        ``not_published`` (only those never sent there). Any other value, or a
        missing channel, returns the list untouched — this is an opt-in filter.
        """
        if not channel_id or state not in ("published", "not_published"):
            return questions

        def is_published(question: dict[str, Any]) -> bool:
            return (channel_id, question["id"]) in published_pairs

        if state == "published":
            return [q for q in questions if is_published(q)]
        return [q for q in questions if not is_published(q)]

    def stats(self) -> dict[str, int]:
        questions = self._all()
        return {
            "total": len(questions),
            "published": sum(1 for q in questions if q["status"] == "published"),
            "ready": sum(1 for q in questions if q["status"] == "ready"),
            "draft": sum(1 for q in questions if q["status"] == "draft"),
            "by_difficulty": {
                level: sum(1 for q in questions if q["difficulty"] == level)
                for level in ("easy", "medium", "hard")
            },
        }

    def by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for question in self._all():
            counts[question["category"]] = counts.get(question["category"], 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    def pick_candidates(
        self,
        *,
        selection: str = "sequential",
        category_filter: str = "",
        exclude_ids: set[str] | None = None,
        published_pairs: set[tuple[str, str]] | None = None,
        channel_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return publishable questions in the order the schedule wants them.

        A question qualifies when its status is ``ready``. When ``channel_id``
        is given, questions already published *to that channel* are excluded
        (``published_pairs`` is the ``(channel_id, question_id)`` set from the
        publishing history); a question published only to another channel stays
        available here. Without a channel the old global behaviour applies and
        any question whose status is ``published`` is dropped.
        """
        exclude_ids = exclude_ids or set()
        published_pairs = published_pairs or set()

        def _eligible(question: dict[str, Any]) -> bool:
            if question["id"] in exclude_ids:
                return False
            if channel_id:
                return question["status"] == "ready" and (
                    channel_id,
                    question["id"],
                ) not in published_pairs
            return question["status"] == "ready"

        candidates = [q for q in self._all() if _eligible(q)]
        if category_filter:
            candidates = [q for q in candidates if q["category"] == category_filter]

        if selection == "random":
            random.shuffle(candidates)
        elif selection == "category":
            candidates.sort(key=lambda q: (q["category"], q["id"]))
        else:
            candidates.sort(key=lambda q: q["id"])
        return candidates

    # --- import / export ---------------------------------------------------
    def export_document(self) -> dict[str, Any]:
        return self._storage.questions.read()

    def import_questions(
        self,
        raw_questions: list[dict[str, Any]],
        *,
        actor: str = "system",
        duplicate_policy: str = "skip",
    ) -> dict[str, Any]:
        """Import a list of raw questions.

        ``duplicate_policy`` is ``skip`` (default), ``update`` or ``create``.
        Returns a report with the counts and the rejected rows.
        """
        if not isinstance(raw_questions, list):
            raise ValidationError("ملف الاستيراد يجب أن يحتوي على قائمة أسئلة.")

        imported: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, Any]] = []

        for index, raw in enumerate(raw_questions):
            try:
                data = normalize_imported_question(raw)
            except ValidationError as exc:
                errors.append({"index": index, "error": str(exc)})
                continue

            existing = self._find_by_text(data["question"])
            if existing and duplicate_policy == "skip":
                skipped.append(existing["id"])
                continue
            if existing and duplicate_policy == "update":
                self.update(existing["id"], data, actor=actor)
                updated.append(existing["id"])
                continue

            created = self.create(data, actor=actor)
            imported.append(created["id"])

        report = {
            "imported": len(imported),
            "updated": len(updated),
            "skipped": len(skipped),
            "failed": len(errors),
            "errors": errors[:20],
            "imported_ids": imported,
        }
        self._audit.record(
            "question.imported",
            actor=actor,
            level="warning" if errors else "info",
            message=(
                f"استيراد {report['imported']} سؤالاً، "
                f"تحديث {report['updated']}، تخطي {report['skipped']}، "
                f"فشل {report['failed']}"
            ),
        )
        return report

    def _find_by_text(self, text: str) -> dict[str, Any] | None:
        normalized = text.strip().lower()
        return next(
            (q for q in self._all() if q["question"].strip().lower() == normalized),
            None,
        )
