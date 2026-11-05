"""Exam lifecycle: composition, snapshots, availability and publishing.

Design notes
------------
* An exam is composed from the *existing* question bank; there is no second
  bank. :meth:`ExamService.set_questions` copies each bank question into an
  ``exam_questions`` row as an immutable **snapshot**, so editing or deleting
  the bank question later cannot change a published exam (doc rule 7).
* Snapshots are **versioned**. While an exam has no attempts its snapshot rows
  are replaced in place; once any attempt exists, a question edit appends a new
  version instead of overwriting, so a completed (or in-progress) attempt keeps
  pointing at the exact questions it was served (doc rule 8).
* Availability is computed **server-side** from ``access_mode`` and the stored
  UTC instants; the client clock is never consulted (doc rule: "التحقق ... في
  Backend").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.audit_service import AuditService
from app.services.question_service import QuestionService
from app.storage.sqlite_manager import Storage, utc_now_iso
from app.storage.validators import ValidationError, validate_exam_payload

# Student-facing effective states (derived, never stored).
STATE_DRAFT = "draft"
STATE_UPCOMING = "upcoming"
STATE_AVAILABLE = "available"
STATE_CLOSED = "closed"
STATE_ARCHIVED = "archived"


def _parse_instant(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


class ExamService:
    def __init__(
        self, storage: Storage, audit: AuditService, questions: QuestionService
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._questions = questions

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _next_id(counter: int) -> str:
        return f"EX{counter:04d}"

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.exams.read()["exams"]

    def get(self, exam_id: str) -> dict[str, Any] | None:
        return next((e for e in self._all() if e["id"] == exam_id), None)

    def list_exams(
        self, *, status: str = "", category: str = "", search: str = ""
    ) -> list[dict[str, Any]]:
        exams = self._all()
        if status:
            exams = [e for e in exams if e.get("status") == status]
        if category:
            exams = [e for e in exams if e.get("category") == category]
        text = (search or "").strip().lower()
        if text:
            exams = [e for e in exams if text in str(e.get("title", "")).lower()]
        return sorted(exams, key=lambda e: e.get("created_at") or "", reverse=True)

    def categories(self) -> list[str]:
        return sorted({e["category"] for e in self._all() if e.get("category")})

    # --- CRUD --------------------------------------------------------------
    def create(
        self,
        payload: dict[str, Any],
        question_ids: list[str] | None = None,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        data = validate_exam_payload(payload)
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["counter"] = int(document.get("counter", 0)) + 1
            exam = {
                "id": self._next_id(document["counter"]),
                **data,
                "question_version": 0,
                "published_at": None,
                "closed_at": None,
                "archived_at": None,
                "created_at": now,
                "updated_at": now,
            }
            document["exams"].append(exam)
            return exam

        exam = self._storage.exams.update(_mutate)
        if question_ids:
            self.set_questions(exam["id"], question_ids, actor=actor)
        self._audit.record(
            "exam.created",
            actor=actor,
            message=f"إنشاء الاختبار «{exam['title']}»",
            exam_id=exam["id"],
        )
        return self.get(exam["id"]) or exam

    def update(
        self,
        exam_id: str,
        payload: dict[str, Any],
        question_ids: list[str] | None = None,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        current = self.get(exam_id)
        if current is None:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.")

        # Merge over the stored exam so a partial update (e.g. only the title)
        # keeps every other field. ``validate_exam_payload`` always returns a
        # full document, so validating the raw payload alone would reset the
        # omitted fields to their defaults (dropping ``status`` back to draft).
        base = dict(current)
        # When the caller supplies window fields, discard the stored instants so
        # they are recomputed from the new date/time instead of being reused.
        if {"start_date", "start_time", "end_date", "end_time", "starts_at", "ends_at"} & payload.keys():
            base.pop("starts_at", None)
            base.pop("ends_at", None)
        data = validate_exam_payload({**base, **payload})

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for exam in document["exams"]:
                if exam["id"] == exam_id:
                    exam.update(data)
                    exam["updated_at"] = utc_now_iso()
                    return exam
            raise KeyError(exam_id)

        try:
            self._storage.exams.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.") from exc

        if question_ids is not None:
            self.set_questions(exam_id, question_ids, actor=actor)
        self._audit.record(
            "exam.updated", actor=actor, message=f"تعديل الاختبار {exam_id}", exam_id=exam_id
        )
        return self.get(exam_id)  # type: ignore[return-value]

    def delete(self, exam_id: str, *, actor: str = "system") -> None:
        if self.attempts_for(exam_id):
            raise ValidationError(
                "لا يمكن حذف اختبار له محاولات. أرشِفه بدلاً من ذلك."
            )

        def _mutate(document: dict[str, Any]) -> None:
            before = len(document["exams"])
            document["exams"] = [e for e in document["exams"] if e["id"] != exam_id]
            if len(document["exams"]) == before:
                raise KeyError(exam_id)

        try:
            self._storage.exams.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.") from exc

        self._mutate_exam_questions(
            lambda rows: [r for r in rows if r["exam_id"] != exam_id]
        )
        self._audit.record(
            "exam.deleted", actor=actor, level="warning", message=f"حذف الاختبار {exam_id}"
        )

    # --- exam questions (versioned snapshots) ------------------------------
    def _all_exam_questions(self) -> list[dict[str, Any]]:
        return self._storage.exam_questions.read()["exam_questions"]

    def _mutate_exam_questions(self, mutator: Any) -> Any:
        def _apply(document: dict[str, Any]) -> Any:
            # The mutator returns a new list; assign it back so the change is
            # actually persisted (``update`` writes whatever ``document`` holds).
            document["exam_questions"] = mutator(document["exam_questions"])
            return document["exam_questions"]

        return self._storage.exam_questions.update(_apply)

    def _has_attempts(self, exam_id: str) -> bool:
        return bool(self.attempts_for(exam_id))

    def attempts_for(self, exam_id: str) -> list[dict[str, Any]]:
        return [
            a for a in self._storage.attempts.read()["attempts"] if a["exam_id"] == exam_id
        ]

    def set_questions(
        self, exam_id: str, question_ids: list[str], *, actor: str = "system"
    ) -> dict[str, Any]:
        exam = self.get(exam_id)
        if exam is None:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.")

        ids = [str(q).strip() for q in (question_ids or []) if str(q).strip()]
        if not ids:
            raise ValidationError("اختر سؤالاً واحداً على الأقل للاختبار.")
        seen: set[str] = set()
        unique_ids: list[str] = []
        for qid in ids:
            if qid not in seen:
                seen.add(qid)
                unique_ids.append(qid)

        bank = {q["id"]: q for q in self._questions.list_questions()}
        missing = [qid for qid in unique_ids if qid not in bank]
        if missing:
            raise ValidationError(f"أسئلة غير موجودة في البنك: {', '.join(missing)}")

        # If attempts exist, append a new snapshot version; otherwise replace.
        if self._has_attempts(exam_id):
            new_version = int(exam.get("question_version", 0)) + 1
        else:
            new_version = 1

        rows = [
            self._snapshot_row(exam_id, bank[qid], position, new_version)
            for position, qid in enumerate(unique_ids)
        ]

        def _mutate(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not self._has_attempts(exam_id):
                existing = [r for r in existing if r["exam_id"] != exam_id]
            return existing + rows

        self._mutate_exam_questions(_mutate)

        def _set_version(document: dict[str, Any]) -> None:
            for item in document["exams"]:
                if item["id"] == exam_id:
                    item["question_version"] = new_version
                    item["updated_at"] = utc_now_iso()

        self._storage.exams.update(_set_version)
        self._audit.record(
            "exam.questions_set",
            actor=actor,
            message=f"تحديد {len(rows)} سؤالاً للاختبار {exam_id}",
            exam_id=exam_id,
        )
        return {"exam_id": exam_id, "version": new_version, "count": len(rows)}

    def _snapshot_row(
        self, exam_id: str, question: dict[str, Any], position: int, version: int
    ) -> dict[str, Any]:
        return {
            "id": f"{exam_id}-V{version}-P{position + 1}",
            "exam_id": exam_id,
            "question_id": question["id"],
            "version": version,
            "position": position,
            "points": 1,
            "question_snapshot": question["question"],
            "options_snapshot": list(question["options"]),
            "correct_answer_snapshot": int(question["correct_option_id"]),
            "explanation_snapshot": question.get("explanation", ""),
            "category_snapshot": question.get("category", ""),
            "difficulty_snapshot": question.get("difficulty", ""),
        }

    def questions_for(
        self, exam_id: str, version: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [r for r in self._all_exam_questions() if r["exam_id"] == exam_id]
        if version is None:
            exam = self.get(exam_id)
            version = int((exam or {}).get("question_version", 0))
        rows = [r for r in rows if int(r.get("version", 0)) == version]
        return sorted(rows, key=lambda r: int(r.get("position", 0)))

    def question_count(self, exam_id: str) -> int:
        return len(self.questions_for(exam_id))

    # --- lifecycle ---------------------------------------------------------
    def _set_status(
        self, exam_id: str, status: str, *, actor: str, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for exam in document["exams"]:
                if exam["id"] == exam_id:
                    exam["status"] = status
                    exam["updated_at"] = now
                    if extra:
                        exam.update(extra)
                    return exam
            raise KeyError(exam_id)

        try:
            exam = self._storage.exams.update(_mutate)
        except KeyError as exc:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.") from exc
        self._audit.record(
            "exam.status_changed",
            actor=actor,
            message=f"حالة الاختبار {exam_id} → {status}",
            exam_id=exam_id,
        )
        return exam

    def publish(self, exam_id: str, *, actor: str = "system") -> dict[str, Any]:
        exam = self.get(exam_id)
        if exam is None:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.")
        if not self.questions_for(exam_id):
            raise ValidationError("لا يمكن نشر اختبار بلا أسئلة.")
        # A scheduled exam whose window has not opened stays "scheduled".
        target = "scheduled" if exam.get("access_mode") == "scheduled" else "published"
        return self._set_status(
            exam_id, target, actor=actor, extra={"published_at": utc_now_iso()}
        )

    def close(self, exam_id: str, *, actor: str = "system") -> dict[str, Any]:
        return self._set_status(
            exam_id, "closed", actor=actor, extra={"closed_at": utc_now_iso()}
        )

    def archive(self, exam_id: str, *, actor: str = "system") -> dict[str, Any]:
        return self._set_status(
            exam_id, "archived", actor=actor, extra={"archived_at": utc_now_iso()}
        )

    def reopen(self, exam_id: str, *, actor: str = "system") -> dict[str, Any]:
        exam = self.get(exam_id)
        if exam is None:
            raise ValidationError(f"الاختبار {exam_id} غير موجود.")
        target = "scheduled" if exam.get("access_mode") == "scheduled" else "published"
        return self._set_status(
            exam_id, target, actor=actor, extra={"closed_at": None, "archived_at": None}
        )

    # --- availability ------------------------------------------------------
    def effective_state(
        self, exam: dict[str, Any], now: datetime | None = None
    ) -> str:
        """Derive the student-facing state (draft/upcoming/available/closed/archived)."""
        status = exam.get("status", "draft")
        if status == "draft":
            return STATE_DRAFT
        if status == "archived":
            return STATE_ARCHIVED
        if status == "closed":
            return STATE_CLOSED

        moment = now or _now()
        starts = _parse_instant(exam.get("starts_at"))
        ends = _parse_instant(exam.get("ends_at"))

        if exam.get("access_mode") == "scheduled":
            if starts and moment < starts:
                return STATE_UPCOMING
            if ends and moment > ends:
                return STATE_CLOSED
            return STATE_AVAILABLE
        return STATE_AVAILABLE

    def is_visible_to_students(self, exam: dict[str, Any]) -> bool:
        """Published exams are visible; drafts and archived are not."""
        return exam.get("status") in ("scheduled", "published", "closed")

    def student_library(self) -> list[dict[str, Any]]:
        return [e for e in self.list_exams() if self.is_visible_to_students(e)]

    def can_start(
        self, exam: dict[str, Any], now: datetime | None = None
    ) -> tuple[bool, str]:
        """Whether a *new* attempt may begin, with an Arabic reason when not."""
        state = self.effective_state(exam, now)
        if state == STATE_AVAILABLE:
            return True, ""
        if state == STATE_UPCOMING:
            return False, "لم يبدأ موعد الاختبار بعد."
        if state == STATE_CLOSED:
            return False, "انتهى موعد الاختبار."
        if state == STATE_ARCHIVED:
            return False, "الاختبار مؤرشف."
        return False, "الاختبار غير متاح."

    # --- aggregates --------------------------------------------------------
    def stats(self, exam_id: str) -> dict[str, Any]:
        attempts = self.attempts_for(exam_id)
        submitted = [a for a in attempts if a.get("status") == "submitted"]
        started_students = {a["student_id"] for a in attempts}
        # A student's counted result is their best submitted attempt, so a
        # repeat sitter does not drag the average or skew highest/lowest.
        best: dict[str, float] = {}
        for attempt in submitted:
            score = float(attempt.get("percentage") or 0)
            sid = attempt["student_id"]
            if sid not in best or score > best[sid]:
                best[sid] = score
        scores = list(best.values())
        return {
            "questions": self.question_count(exam_id),
            "attempts": len(attempts),
            "completed": len(submitted),
            "students_started": len(started_students),
            "students_graded": len(best),
            "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "highest": round(max(scores), 1) if scores else 0.0,
            "lowest": round(min(scores), 1) if scores else 0.0,
        }
