"""Attempts: starting, autosaving, grading, expiry and review.

Integrity rules enforced here (from the spec):
* A student can never exceed ``max_attempts`` (``0`` = unlimited). The check and
  the insert happen inside one storage transaction, so two racing "start" calls
  cannot both slip through.
* A submitted attempt is immutable: answers stop being accepted and the score
  is never recomputed from the live bank.
* Grading runs against the snapshot version pinned when the attempt started, so
  a later exam edit or question edit cannot change an old result.
* Every read is scoped to the owning student; :meth:`get_owned` refuses another
  student's attempt.
* Expiry is judged from server time; an attempt whose window passed is
  auto-submitted on the next touch (and by :meth:`sweep_expired`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.audit_service import AuditService
from app.services.exam_service import ExamService, STATE_ARCHIVED, STATE_CLOSED
from app.storage.base import StorageBackend as Storage, utc_now_iso
from app.storage.validators import ValidationError

STATUS_IN_PROGRESS = "in_progress"
STATUS_SUBMITTED = "submitted"
STATUS_EXPIRED = "expired"

# A score at or above this percentage counts as a pass. Exams carry no per-exam
# pass mark, so analytics uses one shared, documented threshold.
PASS_MARK = 50.0


def _parse_instant(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(UTC)


class AttemptError(ValidationError):
    """A business-rule violation while starting or answering an attempt."""


class ExamAttemptService:
    def __init__(
        self, storage: Storage, audit: AuditService, exams: ExamService
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._exams = exams

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _next_id(counter: int) -> str:
        return f"AT{counter:06d}"

    def _all(self) -> list[dict[str, Any]]:
        return self._storage.attempts.read()["attempts"]

    def _all_answers(self) -> list[dict[str, Any]]:
        return self._storage.attempt_answers.read()["attempt_answers"]

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        return next((a for a in self._all() if a["id"] == attempt_id), None)

    def get_owned(self, attempt_id: str, student_id: str) -> dict[str, Any]:
        """Fetch an attempt, refusing one that belongs to another student."""
        attempt = self.get(attempt_id)
        if attempt is None or attempt.get("student_id") != student_id:
            raise AttemptError("المحاولة غير موجودة.")
        return attempt

    def attempts_for_student(
        self, student_id: str, *, exam_id: str = ""
    ) -> list[dict[str, Any]]:
        rows = [
            a
            for a in self._all()
            if a.get("student_id") == student_id and (not exam_id or a["exam_id"] == exam_id)
        ]
        return sorted(rows, key=lambda a: int(a.get("attempt_number", 0)))

    def attempts_for_exam(self, exam_id: str) -> list[dict[str, Any]]:
        return [a for a in self._all() if a["exam_id"] == exam_id]

    def all_attempts(
        self,
        *,
        status: str = "",
        search: str = "",
        student_ids: set[str] | None = None,
        exam_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._all()
        if status:
            rows = [a for a in rows if a.get("status") == status]
        text = (search or "").strip().lower().lstrip("@")
        if text:
            ids = student_ids or set()
            exams = exam_ids or set()
            rows = [
                a
                for a in rows
                if text in str(a.get("exam_id", "")).lower()
                or text in str(a.get("student_id", "")).lower()
                or text in str(a.get("id", "")).lower()
                or a.get("student_id") in ids
                or a.get("exam_id") in exams
            ]
        return sorted(rows, key=lambda a: a.get("started_at") or "", reverse=True)

    def best_per_student(self, attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the highest-scoring attempt for each (exam, student) pair.

        A student may sit the same exam several times; the score that counts is
        their best one, so every aggregate (average, highest, lowest, buckets)
        must be built on these rows rather than on every attempt.
        """
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for attempt in attempts:
            key = (attempt.get("exam_id", ""), attempt.get("student_id", ""))
            current = best.get(key)
            if current is None or float(attempt.get("percentage") or 0) > float(
                current.get("percentage") or 0
            ):
                best[key] = attempt
        return list(best.values())

    def answers_for(self, attempt_id: str) -> list[dict[str, Any]]:
        return [a for a in self._all_answers() if a["attempt_id"] == attempt_id]

    # --- counting / limits -------------------------------------------------
    def _counting_attempts(self, exam: dict[str, Any], student_id: str) -> list[dict[str, Any]]:
        rows = self.attempts_for_student(student_id, exam_id=exam["id"])
        if exam.get("count_abandoned_attempts", True):
            return rows
        return [a for a in rows if a.get("status") in (STATUS_IN_PROGRESS, STATUS_SUBMITTED)]

    def remaining_attempts(self, exam: dict[str, Any], student_id: str) -> int | None:
        """Remaining attempts, or ``None`` when unlimited."""
        limit = int(exam.get("max_attempts", 1) or 0)
        if limit == 0:
            return None
        used = len(self._counting_attempts(exam, student_id))
        return max(0, limit - used)

    def active_attempt(self, exam_id: str, student_id: str) -> dict[str, Any] | None:
        for attempt in reversed(self.attempts_for_student(student_id, exam_id=exam_id)):
            if attempt.get("status") == STATUS_IN_PROGRESS:
                return attempt
        return None

    # --- starting ----------------------------------------------------------
    def start(self, exam_id: str, student_id: str, *, actor: str = "system") -> dict[str, Any]:
        exam = self._exams.get(exam_id)
        if exam is None:
            raise AttemptError(f"الاختبار {exam_id} غير موجود.")
        if not self._exams.is_visible_to_students(exam):
            raise AttemptError("الاختبار غير متاح حالياً.")

        allowed, reason = self._exams.can_start(exam)
        if not allowed:
            raise AttemptError(reason)

        # Resume an existing in-progress attempt instead of burning a new one.
        existing = self.active_attempt(exam_id, student_id)
        if existing is not None:
            return existing

        if not self._exams.questions_for(exam_id):
            raise AttemptError("الاختبار لا يحتوي على أسئلة.")

        limit = int(exam.get("max_attempts", 1) or 0)
        now = _now()
        expires_at = ""
        remaining_seconds = None
        duration = int(exam.get("duration_minutes", 0) or 0)
        if duration > 0:
            remaining_seconds = duration * 60
            expires_at = _iso(now + timedelta(seconds=remaining_seconds))

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            rows = document["attempts"]
            # Re-count inside the transaction so a concurrent start cannot pass.
            counting = [
                a
                for a in rows
                if a["exam_id"] == exam_id
                and a["student_id"] == student_id
                and (
                    exam.get("count_abandoned_attempts", True)
                    or a.get("status") in (STATUS_IN_PROGRESS, STATUS_SUBMITTED)
                )
            ]
            if limit and len(counting) >= limit:
                raise AttemptError("استنفدت عدد المحاولات المسموح لهذا الاختبار.")
            if any(a["exam_id"] == exam_id and a["student_id"] == student_id
                   and a["status"] == STATUS_IN_PROGRESS for a in rows):
                raise AttemptError("لديك محاولة جارية بالفعل.")

            number = len(
                [a for a in rows if a["exam_id"] == exam_id and a["student_id"] == student_id]
            ) + 1
            document["counter"] = int(document.get("counter", 0)) + 1
            attempt = {
                "id": self._next_id(document["counter"]),
                "exam_id": exam_id,
                "student_id": student_id,
                "attempt_number": number,
                "status": STATUS_IN_PROGRESS,
                "question_version": int(exam.get("question_version", 0)),
                "started_at": _iso(now),
                "submitted_at": None,
                "expires_at": expires_at,
                "remaining_seconds": remaining_seconds,
                "paused_at": None,
                "paused_seconds": 0,
                "score": 0,
                "total_score": 0,
                "percentage": 0.0,
                "duration_seconds": 0,
                "correct_count": 0,
                "wrong_count": 0,
                "unanswered_count": 0,
            }
            rows.append(attempt)
            return attempt

        try:
            attempt = self._storage.attempts.update(_mutate)
        except AttemptError:
            raise
        self._audit.record(
            "attempt.started",
            actor=actor,
            message=f"بدء المحاولة {attempt['id']} للاختبار {exam_id}",
            attempt_id=attempt["id"],
            exam_id=exam_id,
            student_id=student_id,
        )
        return attempt

    # --- timer: pause / resume ---------------------------------------------
    def _remaining_seconds(self, attempt: dict[str, Any], *, at: datetime) -> int | None:
        """Seconds left, or ``None`` for an exam with no time limit.

        A paused attempt keeps the balance it had at the moment it was paused:
        the clock does not advance while the student is away from the session.
        An attempt stored before this feature existed has no banked balance, so
        its deadline is used as the source of truth.
        """
        stored = attempt.get("remaining_seconds")
        expires = _parse_instant(attempt.get("expires_at"))
        if attempt.get("paused_at"):
            if stored is None and expires is None:
                return None
            return max(0, int(stored or 0))
        if expires is not None:
            return max(0, int((expires - at).total_seconds()))
        if stored is None:
            return None
        return max(0, int(stored))

    def time_left(self, attempt: dict[str, Any]) -> int | None:
        """Public wrapper: the effective countdown for an attempt right now."""
        return self._remaining_seconds(attempt, at=_now())

    def _expire_if_due(self, attempt: dict[str, Any]) -> dict[str, Any]:
        """Auto-submit an attempt whose clock ran out while it was running.

        A paused attempt is never expired here: its countdown is frozen, so it
        can only expire once it is resumed and the balance runs out.
        """
        if attempt.get("status") != STATUS_IN_PROGRESS:
            return attempt
        if attempt.get("paused_at"):
            return attempt
        expires = _parse_instant(attempt.get("expires_at"))
        if expires and _now() > expires:
            return self._finalize(attempt["id"], status=STATUS_EXPIRED)
        return attempt

    def pause(self, attempt_id: str, student_id: str, *, actor: str = "system") -> dict[str, Any]:
        """Freeze the countdown, banking the time the student has left."""
        attempt = self.get_owned(attempt_id, student_id)
        if attempt.get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("المحاولة مُسلَّمة بالفعل.")

        # If the clock already ran out, this pause must not rescue it.
        attempt = self._expire_if_due(attempt)
        if attempt.get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("انتهى وقت الاختبار وتم التسليم تلقائياً.")
        now = _now()
        balance = self._remaining_seconds(attempt, at=now)
        if attempt.get("paused_at") or balance is None:
            return attempt

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for item in document["attempts"]:
                if item["id"] == attempt_id and item.get("status") == STATUS_IN_PROGRESS:
                    item["remaining_seconds"] = balance
                    item["paused_at"] = _iso(now)
                    return item
            return attempt

        return self._storage.attempts.update(_mutate)

    def resume(self, attempt_id: str, student_id: str, *, actor: str = "system") -> dict[str, Any]:
        """Restart the countdown from the balance banked at pause time."""
        attempt = self.get_owned(attempt_id, student_id)
        if attempt.get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("المحاولة مُسلَّمة بالفعل.")
        if not attempt.get("paused_at"):
            return attempt

        balance = max(0, int(attempt.get("remaining_seconds") or 0))
        now = _now()
        # Resuming with nothing left is simply the end of the exam.
        if balance <= 0:
            return self._finalize(attempt_id, status=STATUS_EXPIRED)

        paused_at = _parse_instant(attempt.get("paused_at"))
        away = int((now - paused_at).total_seconds()) if paused_at else 0
        paused_seconds = int(attempt.get("paused_seconds") or 0) + max(0, away)
        expires_at = _iso(now + timedelta(seconds=balance))

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for item in document["attempts"]:
                if item["id"] == attempt_id and item.get("status") == STATUS_IN_PROGRESS:
                    item["remaining_seconds"] = balance
                    item["expires_at"] = expires_at
                    item["paused_at"] = None
                    item["paused_seconds"] = paused_seconds
                    return item
            return attempt

        return self._storage.attempts.update(_mutate)

    # --- answering ---------------------------------------------------------
    def _assert_open(self, attempt: dict[str, Any]) -> None:
        if attempt.get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("المحاولة مُسلَّمة بالفعل.")
        if self._expire_if_due(attempt).get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("انتهى وقت الاختبار وتم التسليم تلقائياً.")

    def save_answer(
        self,
        attempt_id: str,
        student_id: str,
        question_id: str,
        selected_option: int,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        attempt = self.get_owned(attempt_id, student_id)
        self._assert_open(attempt)

        rows = self._exams.questions_for(attempt["exam_id"], int(attempt["question_version"]))
        question = next((q for q in rows if q["question_id"] == question_id), None)
        if question is None:
            raise AttemptError("السؤال ليس ضمن هذه المحاولة.")
        options = list(question.get("options_snapshot") or [])
        try:
            selected = int(selected_option)
        except (TypeError, ValueError) as exc:
            raise AttemptError("خيار غير صالح.") from exc
        if not 0 <= selected < len(options):
            raise AttemptError("الخيار خارج نطاق الأسئلة.")

        is_correct = selected == int(question["correct_answer_snapshot"])
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for answer in document["attempt_answers"]:
                if answer["attempt_id"] == attempt_id and answer["question_id"] == question_id:
                    answer.update(
                        {
                            "selected_option": selected,
                            "is_correct": is_correct,
                            "answered_at": now,
                        }
                    )
                    return answer
            document["counter"] = int(document.get("counter", 0)) + 1
            answer = {
                "id": f"AN{document['counter']:06d}",
                "attempt_id": attempt_id,
                "exam_question_id": question["id"],
                "question_id": question_id,
                "selected_option": selected,
                "is_correct": is_correct,
                "awarded_points": 0,
                "answered_at": now,
            }
            document["attempt_answers"].append(answer)
            return answer

        return self._storage.attempt_answers.update(_mutate)

    # --- submitting / grading ---------------------------------------------
    def submit(self, attempt_id: str, student_id: str, *, actor: str = "system") -> dict[str, Any]:
        attempt = self.get_owned(attempt_id, student_id)
        if attempt.get("status") != STATUS_IN_PROGRESS:
            raise AttemptError("المحاولة مُسلَّمة بالفعل.")
        return self._finalize(attempt_id, status=STATUS_SUBMITTED)

    def _finalize(self, attempt_id: str, *, status: str = STATUS_SUBMITTED) -> dict[str, Any]:
        attempt = self.get(attempt_id)
        if attempt is None:
            raise AttemptError("المحاولة غير موجودة.")
        if attempt.get("status") != STATUS_IN_PROGRESS:
            return attempt

        rows = self._exams.questions_for(
            attempt["exam_id"], int(attempt.get("question_version", 0))
        )
        answers = {a["question_id"]: a for a in self.answers_for(attempt_id)}

        correct = wrong = unanswered = 0
        score = 0
        for row in rows:
            points = int(row.get("points", 1) or 1)
            answer = answers.get(row["question_id"])
            if answer is None:
                unanswered += 1
                continue
            if answer.get("is_correct"):
                correct += 1
                score += points
            else:
                wrong += 1

        total = sum(int(r.get("points", 1) or 1) for r in rows) or 1
        percentage = round(score / total * 100, 1)

        started = _parse_instant(attempt.get("started_at"))
        now = _now()
        duration = int((now - started).total_seconds()) if started else 0
        # Time spent paused is not time spent on the exam; a student who leaves
        # and returns must not see the away-time counted as their duration.
        if attempt.get("paused_at"):
            paused_at = _parse_instant(attempt.get("paused_at"))
            if paused_at:
                duration -= max(0, int((now - paused_at).total_seconds()))
        duration -= int(attempt.get("paused_seconds") or 0)
        if duration < 0:
            duration = 0

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for item in document["attempts"]:
                if item["id"] == attempt_id and item.get("status") == STATUS_IN_PROGRESS:
                    item.update(
                        {
                            "status": status,
                            "submitted_at": now.replace(microsecond=0)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "score": score,
                            "total_score": total,
                            "percentage": percentage,
                            "duration_seconds": duration,
                            "correct_count": correct,
                            "wrong_count": wrong,
                            "unanswered_count": unanswered,
                        }
                    )
                    return item
            return attempt

        result = self._storage.attempts.update(_mutate)

        def _award(answers_doc: dict[str, Any]) -> None:
            for answer in answers_doc["attempt_answers"]:
                if answer["attempt_id"] == attempt_id:
                    row = next(
                        (r for r in rows if r["question_id"] == answer["question_id"]), None
                    )
                    answer["awarded_points"] = (
                        int(row.get("points", 1) or 1) if answer.get("is_correct") else 0
                    )

        self._storage.attempt_answers.update(_award)
        self._audit.record(
            "attempt.submitted",
            actor="system",
            message=f"تسليم المحاولة {attempt_id} ({percentage}%)",
            attempt_id=attempt_id,
            exam_id=attempt["exam_id"],
            student_id=attempt["student_id"],
            status=status,
        )
        return result

    def sweep_expired(self) -> int:
        """Auto-submit every attempt whose server-side window has passed.

        A paused attempt is normally left alone: its countdown is frozen, so an
        elapsed wall-clock deadline must not close a session the student has not
        resumed (the balance is re-based on resume). The exception is an exam
        that has itself closed or been archived — nothing can be resumed inside
        a closed exam, so an abandoned paused attempt is settled here rather
        than lingering as "in progress" forever.
        """
        closed = 0
        for attempt in self._all():
            if attempt.get("status") != STATUS_IN_PROGRESS:
                continue
            if attempt.get("paused_at") and not self._exam_is_over(attempt.get("exam_id")):
                continue
            expires = _parse_instant(attempt.get("expires_at"))
            if attempt.get("paused_at") or (expires and _now() > expires):
                self._finalize(attempt["id"], status=STATUS_EXPIRED)
                closed += 1
        return closed

    def _exam_is_over(self, exam_id: str | None) -> bool:
        """True when the parent exam is closed/archived (no resuming possible)."""
        exam = self._exams.get(exam_id) if exam_id else None
        if exam is None:
            return False
        return self._exams.effective_state(exam) in (STATE_CLOSED, STATE_ARCHIVED)

    # --- reading results ---------------------------------------------------
    def exam_analytics(self, exam_id: str) -> dict[str, Any]:
        """Deep, exam-scoped analytics for the admin analytics page.

        Every figure is built on each student's *best* submitted attempt, so a
        repeat sitter neither inflates nor drags the aggregate. Per-question
        difficulty is keyed by snapshot version, because an attempt is graded
        against the version that was current when it started and a later edit
        must not rewrite old results.
        """
        attempts = self.attempts_for_exam(exam_id)
        submitted = [a for a in attempts if a.get("status") in (STATUS_SUBMITTED, STATUS_EXPIRED)]
        best = {}
        for attempt in submitted:
            key = attempt["student_id"]
            score = float(attempt.get("percentage") or 0)
            if key not in best or score > float(best[key].get("percentage") or 0):
                best[key] = attempt
        best_rows = list(best.values())
        scores = [float(a.get("percentage") or 0) for a in best_rows]

        buckets = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
        for score in scores:
            if score <= 20:
                buckets["0-20"] += 1
            elif score <= 40:
                buckets["21-40"] += 1
            elif score <= 60:
                buckets["41-60"] += 1
            elif score <= 80:
                buckets["61-80"] += 1
            else:
                buckets["81-100"] += 1

        # Resolve answers back to the snapshot row they were graded against, so
        # a question edited after an attempt still reports the old result.
        snapshots = {
            (int(r.get("version", 0)), r["question_id"]): r
            for r in self._exams.question_rows(exam_id)
        }
        seen: dict[tuple[int, str], int] = {}
        wrong: dict[tuple[int, str], int] = {}
        unanswered: dict[tuple[int, str], int] = {}
        for attempt in best_rows:
            version = int(attempt.get("question_version", 0))
            answered = {
                a["question_id"]: a for a in self.answers_for(attempt["id"])
            }
            for row in self._exams.questions_for(exam_id, version):
                key = (version, row["question_id"])
                seen[key] = seen.get(key, 0) + 1
                answer = answered.get(row["question_id"])
                if answer is None:
                    unanswered[key] = unanswered.get(key, 0) + 1
                elif not answer.get("is_correct"):
                    wrong[key] = wrong.get(key, 0) + 1

        hardest = sorted(
            (
                {
                    "question_id": key[1],
                    "question": snapshots.get(key, {}).get("question_snapshot", key[1]),
                    "options": list(snapshots.get(key, {}).get("options_snapshot") or []),
                    "correct_option_id": int(
                        snapshots.get(key, {}).get("correct_answer_snapshot", 0)
                    ),
                    "category": snapshots.get(key, {}).get("category_snapshot", ""),
                    "difficulty": snapshots.get(key, {}).get("difficulty_snapshot", ""),
                    "attempts": count,
                    "wrong": wrong.get(key, 0),
                    "unanswered": unanswered.get(key, 0),
                    "wrong_rate": round(
                        (wrong.get(key, 0) + unanswered.get(key, 0)) / count * 100, 1
                    ),
                }
                for key, count in seen.items()
            ),
            key=lambda item: item["wrong_rate"],
            reverse=True,
        )

        total_answers = sum(seen.values())
        total_wrong = sum(wrong.values()) + sum(unanswered.values())
        return {
            "exam_id": exam_id,
            "questions": self._exams.question_count(exam_id),
            "attempts": len(attempts),
            "completed": len(submitted),
            "students_started": len({a["student_id"] for a in attempts}),
            "students_graded": len(best_rows),
            "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "highest": round(max(scores), 1) if scores else 0.0,
            "lowest": round(min(scores), 1) if scores else 0.0,
            "median": round(sorted(scores)[len(scores) // 2], 1) if scores else 0.0,
            "pass_rate": round(
                sum(1 for s in scores if s >= PASS_MARK) / len(scores) * 100, 1
            )
            if scores
            else 0.0,
            "completion_rate": round(
                len({a["student_id"] for a in submitted})
                / max(1, len({a["student_id"] for a in attempts}))
                * 100,
                1,
            )
            if attempts
            else 0.0,
            "buckets": buckets,
            "pass_mark": PASS_MARK,
            "correct_rate": round((total_answers - total_wrong) / total_answers * 100, 1)
            if total_answers
            else 0.0,
            "hardest_questions": hardest[:10],
        }

    def student_state(self, exam: dict[str, Any], student_id: str) -> dict[str, Any]:
        """Per-student view of one exam (new/not_started/in_progress/completed/...)."""
        attempts = self.attempts_for_student(student_id, exam_id=exam["id"])
        viewed = self._viewed(exam["id"], student_id)
        return self._state_from(exam, attempts, viewed)

    def states_for_student(
        self, student_id: str, exams: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Per-student state for *every* exam, in one pass over the data.

        The library/dashboard endpoints need this for each exam in the list.
        Computing it per exam would rescan the whole attempts and viewed-state
        collections once per exam — quadratic in the number of exams. Here each
        collection is read once and grouped in memory.
        """
        attempts_doc = self._storage.attempts.read()["attempts"]
        states_doc = self._storage.student_exam_state.read()["states"]

        by_exam: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts_doc:
            if attempt.get("student_id") == student_id:
                by_exam.setdefault(attempt["exam_id"], []).append(attempt)
        viewed = {
            state["exam_id"]
            for state in states_doc
            if state.get("student_id") == student_id and state.get("viewed")
        }

        result: dict[str, dict[str, Any]] = {}
        for exam in exams:
            rows = sorted(
                by_exam.get(exam["id"], []),
                key=lambda a: int(a.get("attempt_number", 0)),
            )
            result[exam["id"]] = self._state_from(exam, rows, exam["id"] in viewed)
        return result

    def _state_from(
        self,
        exam: dict[str, Any],
        attempts: list[dict[str, Any]],
        viewed: bool,
    ) -> dict[str, Any]:
        """Pure state derivation from already-read rows."""
        submitted = [
            a for a in attempts if a.get("status") in (STATUS_SUBMITTED, STATUS_EXPIRED)
        ]
        active = next(
            (a for a in reversed(attempts) if a.get("status") == STATUS_IN_PROGRESS),
            None,
        )
        remaining = self._remaining_from(exam, attempts)

        if active is not None:
            label = "in_progress"
        elif submitted:
            if remaining == 0:
                label = "attempt_limit_reached"
            elif remaining is not None and remaining > 0:
                label = "available_again"
            else:
                label = "completed"
        elif viewed:
            label = "not_started"
        else:
            label = "new"

        best = max((float(a.get("percentage") or 0) for a in submitted), default=None)
        last = float(submitted[-1].get("percentage") or 0) if submitted else None
        return {
            "label": label,
            "attempts_used": len(attempts),
            "attempts_submitted": len(submitted),
            "remaining_attempts": remaining,
            "best_percentage": best,
            "last_percentage": last,
            "active_attempt_id": active["id"] if active else None,
            "last_attempt_id": submitted[-1]["id"] if submitted else None,
        }

    def _remaining_from(
        self, exam: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> int | None:
        """Remaining attempts from already-read rows (``None`` = unlimited)."""
        limit = int(exam.get("max_attempts", 1) or 0)
        if limit == 0:
            return None
        if exam.get("count_abandoned_attempts", True):
            counting = attempts
        else:
            counting = [
                a
                for a in attempts
                if a.get("status") in (STATUS_IN_PROGRESS, STATUS_SUBMITTED)
            ]
        return max(0, limit - len(counting))

    # --- viewed state ------------------------------------------------------
    def _viewed(self, exam_id: str, student_id: str) -> bool:
        return any(
            s["exam_id"] == exam_id and s["student_id"] == student_id
            for s in self._storage.student_exam_state.read()["states"]
        )

    def mark_viewed(self, exam_id: str, student_id: str) -> dict[str, Any]:
        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for state in document["states"]:
                if state["exam_id"] == exam_id and state["student_id"] == student_id:
                    state["last_viewed_at"] = now
                    state["viewed"] = True
                    return state
            state = {
                "id": f"SV{len(document['states']) + 1:06d}",
                "exam_id": exam_id,
                "student_id": student_id,
                "viewed": True,
                "first_viewed_at": now,
                "last_viewed_at": now,
            }
            document["states"].append(state)
            return state

        return self._storage.student_exam_state.update(_mutate)

    # --- review ------------------------------------------------------------
    def review(self, attempt_id: str, student_id: str) -> dict[str, Any]:
        """Per-question review of an attempt; only for its owner and only once
        the attempt is finished and the exam allows showing solutions."""
        attempt = self.get_owned(attempt_id, student_id)
        if attempt.get("status") == STATUS_IN_PROGRESS:
            raise AttemptError("لا يمكن مراجعة محاولة لم تُسلَّم بعد.")

        exam = self._exams.get(attempt["exam_id"]) or {}
        rows = self._exams.questions_for(
            attempt["exam_id"], int(attempt.get("question_version", 0))
        )
        answers = {a["question_id"]: a for a in self.answers_for(attempt_id)}
        show_solutions = bool(exam.get("show_solutions_after_submit", True))

        items: list[dict[str, Any]] = []
        for row in rows:
            answer = answers.get(row["question_id"])
            selected = answer.get("selected_option") if answer else None
            item = {
                "question_id": row["question_id"],
                "question": row["question_snapshot"],
                "options": list(row.get("options_snapshot") or []),
                "selected_option": selected,
                "is_correct": bool(answer.get("is_correct")) if answer else False,
                "answered": answer is not None,
            }
            if show_solutions:
                item["correct_option_id"] = int(row["correct_answer_snapshot"])
                item["options_feedback"] = list(
                    row.get("options_feedback_snapshot") or []
                )
                item["explanation"] = row.get("explanation_snapshot", "")
            items.append(item)

        return {
            "attempt": attempt,
            "exam": {"id": exam.get("id"), "title": exam.get("title", ""),
                     "show_solutions": show_solutions,
                     "feedback_display": exam.get("feedback_display", "picked")},
            "questions": items,
        }

    # --- admin reset -------------------------------------------------------
    def reset_student_attempts(
        self, exam_id: str, student_id: str, *, actor: str = "system"
    ) -> int:
        """Administrative reset: delete this student's attempts for one exam.

        Recorded in the audit log because it changes a student's result history.
        """
        removed = [
            a["id"]
            for a in self._all()
            if a["exam_id"] == exam_id and a["student_id"] == student_id
        ]

        def _mutate(document: dict[str, Any]) -> int:
            before = len(document["attempts"])
            document["attempts"] = [
                a
                for a in document["attempts"]
                if not (a["exam_id"] == exam_id and a["student_id"] == student_id)
            ]
            return before - len(document["attempts"])

        count = self._storage.attempts.update(_mutate)

        def _clear_answers(document: dict[str, Any]) -> None:
            document["attempt_answers"] = [
                a for a in document["attempt_answers"] if a["attempt_id"] not in removed
            ]

        self._storage.attempt_answers.update(_clear_answers)
        self._audit.record(
            "attempt.reset",
            actor=actor,
            level="warning",
            message=f"إعادة ضبط محاولات الطالب {student_id} في الاختبار {exam_id}",
            exam_id=exam_id,
            student_id=student_id,
        )
        return count
