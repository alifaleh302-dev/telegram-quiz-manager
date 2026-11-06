"""Student API: identity, exam library, attempts, results and notifications.

Every handler works from the verified :class:`StudentContext`; no endpoint
accepts a student id from the client. Exam payloads deliberately omit the
correct answer until the attempt is finished and the exam allows solutions, so
the answers are not merely hidden in JavaScript but absent from the response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.deps import get_services, get_templates
from app.api.student_deps import (
    StudentContext,
    issue_student_token,
    require_student,
    require_student_write,
)
from app.services import activity_service as activity
from app.services.exam_attempt_service import AttemptError
from app.storage.validators import ValidationError

router = APIRouter(tags=["student"], prefix="/api/student")
page_router = APIRouter(tags=["student"])


class InitDataIn(BaseModel):
    init_data: str


class AnswerIn(BaseModel):
    question_id: str
    selected_option: int


class ActivityIn(BaseModel):
    exam_id: str = ""
    question_id: str = ""


class NotificationPrefsIn(BaseModel):
    enabled: bool


def _exam_card(
    exam: dict[str, Any],
    services: Any,
    student_id: str,
    *,
    student_state: dict[str, Any] | None = None,
    question_count: int | None = None,
) -> dict[str, Any]:
    state = services.exams.effective_state(exam)
    if student_state is None:
        student_state = services.attempts.student_state(exam, student_id)
    if question_count is None:
        question_count = services.exams.question_count(exam["id"])
    return {
        "id": exam["id"],
        "title": exam["title"],
        "description": exam.get("description", ""),
        "category": exam.get("category", ""),
        "cover_image": exam.get("cover_image", ""),
        "state": state,
        "status": exam.get("status"),
        "access_mode": exam.get("access_mode"),
        "mode": exam.get("mode", "timed"),
        "question_count": question_count,
        "duration_minutes": exam.get("duration_minutes", 0),
        "max_attempts": exam.get("max_attempts", 1),
        "starts_at": exam.get("starts_at", ""),
        "ends_at": exam.get("ends_at", ""),
        "published_at": exam.get("published_at"),
        "created_at": exam.get("created_at", ""),
        **student_state,
    }


def _exam_cards(
    exams: list[dict[str, Any]], services: Any, student_id: str
) -> list[dict[str, Any]]:
    """Build every exam card with two batched reads instead of two per exam."""
    exam_ids = [e["id"] for e in exams]
    states = services.attempts.states_for_student(student_id, exams)
    counts = services.exams.question_counts(exam_ids)
    return [
        _exam_card(
            exam,
            services,
            student_id,
            student_state=states.get(exam["id"]),
            question_count=counts.get(exam["id"], 0),
        )
        for exam in exams
    ]


# --- Mini App page ---------------------------------------------------------

@page_router.get("/app", response_class=HTMLResponse)
def student_mini_app(request: Request) -> Any:
    """Serve the Student Mini App shell (identity is resolved in-app via initData)."""
    templates = get_templates(request)
    return templates.TemplateResponse(
        request, "student.html", {"app_name": request.app.state.app_name}
    )


# --- identity --------------------------------------------------------------

@router.post("/auth")
def student_auth(request: Request, payload: InitDataIn) -> dict[str, Any]:
    """Exchange a verified ``initData`` for a student token + profile."""
    from app.api.student_deps import authenticate_init_data

    services = get_services(request)
    try:
        student = authenticate_init_data(services, payload.init_data)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean 401 by the caller
        from app.core.telegram_auth import TelegramAuthError

        if isinstance(exc, TelegramAuthError):
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        raise
    return {
        "token": issue_student_token(student),
        "profile": services.students.view(student),
    }


@router.get("/profile")
def student_profile(ctx: StudentContext = Depends(require_student)) -> dict[str, Any]:
    services = ctx.services
    services.activity.touch(ctx.id, activity.EVENT_VIEW)
    return {
        "profile": services.students.view(ctx.student),
        "unread_notifications": services.notifications.unread_count(ctx.id),
    }


# --- dashboard / library ---------------------------------------------------

@router.get("/dashboard")
def student_dashboard(ctx: StudentContext = Depends(require_student)) -> dict[str, Any]:
    services = ctx.services
    exams = services.exams.student_library()
    cards = _exam_cards(exams, services, ctx.id)

    completed = [c for c in cards if c["attempts_submitted"] > 0]
    scores = [c["best_percentage"] for c in completed if c["best_percentage"] is not None]
    in_progress = [c for c in cards if c["label"] == "in_progress"]
    new_exams = [c for c in cards if c["label"] == "new"]
    upcoming = [c for c in cards if c["state"] == "upcoming"]

    return {
        "summary": {
            "total_available": len(cards),
            "completed_exams": len(completed),
            "new_exams": len(new_exams),
            "in_progress": len(in_progress),
            "average_percentage": round(sum(scores) / len(scores), 1) if scores else 0.0,
        },
        "new_exams": new_exams[:6],
        "continue": in_progress[:6],
        "recent_results": sorted(
            completed, key=lambda c: c.get("last_attempt_id") or "", reverse=True
        )[:6],
        "upcoming": upcoming[:6],
    }


@router.get("/exams")
def student_exams(
    request: Request, tab: str = "", category: str = "", search: str = "",
    ctx: StudentContext = Depends(require_student),
) -> dict[str, Any]:
    services = ctx.services
    cards = _exam_cards(services.exams.student_library(), services, ctx.id)

    if category:
        cards = [c for c in cards if c["category"] == category]
    text = (search or "").strip().lower()
    if text:
        cards = [c for c in cards if text in c["title"].lower()]

    if tab == "new":
        cards = [c for c in cards if c["label"] == "new"]
    elif tab == "not_started":
        cards = [c for c in cards if c["label"] in ("not_started", "new")]
    elif tab == "completed":
        cards = [c for c in cards if c["attempts_submitted"] > 0]
    elif tab == "in_progress":
        cards = [c for c in cards if c["label"] == "in_progress"]
    elif tab == "closed":
        cards = [c for c in cards if c["state"] in ("closed", "archived")]
    elif tab == "upcoming":
        cards = [c for c in cards if c["state"] == "upcoming"]

    return {
        "exams": cards,
        "categories": services.exams.categories(),
        "tab": tab or "all",
    }


@router.get("/exams/{exam_id}")
def student_exam_detail(
    exam_id: str, ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    exam = services.exams.get(exam_id)
    if exam is None or not services.exams.is_visible_to_students(exam):
        raise ValidationError("الاختبار غير موجود.")
    # Opening the detail marks the exam as viewed (so it stops counting as new).
    services.attempts.mark_viewed(exam_id, ctx.id)
    services.activity.touch(ctx.id, activity.EVENT_OPEN_EXAM, exam_id=exam_id)
    card = _exam_card(exam, services, ctx.id)
    attempts = [
        _attempt_summary(a)
        for a in services.attempts.attempts_for_student(ctx.id, exam_id=exam_id)
        if a.get("status") != "in_progress"
    ]
    return {"exam": card, "attempts": attempts}


@router.get("/exams/{exam_id}/eligibility")
def student_exam_eligibility(
    exam_id: str, ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    exam = services.exams.get(exam_id)
    if exam is None or not services.exams.is_visible_to_students(exam):
        raise ValidationError("الاختبار غير موجود.")
    allowed, reason = services.exams.can_start(exam)
    remaining = services.attempts.remaining_attempts(exam, ctx.id)
    active = services.attempts.active_attempt(exam_id, ctx.id)
    return {
        "can_start": allowed and (remaining is None or remaining > 0),
        "reason": reason,
        "remaining_attempts": remaining,
        "active_attempt_id": active["id"] if active else None,
        "state": services.exams.effective_state(exam),
    }


# --- attempts --------------------------------------------------------------

def _attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": attempt["id"],
        "exam_id": attempt["exam_id"],
        "attempt_number": attempt["attempt_number"],
        "status": attempt["status"],
        "started_at": attempt.get("started_at"),
        "submitted_at": attempt.get("submitted_at"),
        "expires_at": attempt.get("expires_at"),
        "remaining_seconds": attempt.get("remaining_seconds"),
        "paused": bool(attempt.get("paused_at")),
        "score": attempt.get("score", 0),
        "total_score": attempt.get("total_score", 0),
        "percentage": attempt.get("percentage", 0),
        "duration_seconds": attempt.get("duration_seconds", 0),
        "correct_count": attempt.get("correct_count", 0),
        "wrong_count": attempt.get("wrong_count", 0),
        "unanswered_count": attempt.get("unanswered_count", 0),
    }


@router.post("/exams/{exam_id}/start")
def student_start_attempt(
    exam_id: str, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    services = ctx.services
    try:
        attempt = services.attempts.start(exam_id, ctx.id)
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc
    services.activity.touch(ctx.id, activity.EVENT_START, exam_id=exam_id)
    return _attempt_payload(attempt, services, ctx.id)


@router.get("/attempts")
def student_attempts(
    exam_id: str = "", ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    rows = [
        a
        for a in services.attempts.attempts_for_student(ctx.id, exam_id=exam_id)
        if a.get("status") != "in_progress"
    ]
    exams = {e["id"]: e for e in services.exams.list_exams()}
    items = []
    for attempt in sorted(rows, key=lambda a: a.get("submitted_at") or "", reverse=True):
        exam = exams.get(attempt["exam_id"], {})
        items.append(
            {
                **_attempt_summary(attempt),
                "exam_title": exam.get("title", attempt["exam_id"]),
                "exam_category": exam.get("category", ""),
            }
        )
    return {"attempts": items}


@router.get("/attempts/{attempt_id}")
def student_attempt(
    attempt_id: str, ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    attempt = services.attempts.get_owned(attempt_id, ctx.id)
    if attempt.get("status") != "in_progress":
        raise ValidationError("المحاولة مُسلَّمة؛ استخدم نقطة المراجعة.")
    return _attempt_payload(attempt, services, ctx.id)


def _attempt_payload(attempt: dict[str, Any], services: Any, student_id: str) -> dict[str, Any]:
    """The in-session payload: questions and saved answers, **no** correct answer."""
    exam = services.exams.get(attempt["exam_id"]) or {}
    rows = services.exams.questions_for(
        attempt["exam_id"], int(attempt.get("question_version", 0))
    )
    saved = {
        a["question_id"]: a["selected_option"]
        for a in services.attempts.answers_for(attempt["id"])
    }
    summary = _attempt_summary(attempt)
    if attempt.get("status") == "in_progress":
        # Report the *effective* countdown, not the balance frozen at start, so
        # a reopened session resumes from the real time left.
        summary["remaining_seconds"] = services.attempts.time_left(attempt)
    practice = exam.get("mode") == "practice"

    def _question(row: dict[str, Any]) -> dict[str, Any]:
        item = {
            "question_id": row["question_id"],
            "position": row["position"],
            "question": row["question_snapshot"],
            "options": list(row.get("options_snapshot") or []),
            "selected_option": saved.get(row["question_id"]),
        }
        # A resumed practice session must keep showing the feedback for the
        # questions already answered, otherwise the colours would vanish.
        if practice and item["selected_option"] is not None:
            correct = int(row["correct_answer_snapshot"])
            item["is_correct"] = item["selected_option"] == correct
            item["correct_option_id"] = correct
            item["options_feedback"] = list(row.get("options_feedback_snapshot") or [])
            item["explanation"] = row.get("explanation_snapshot", "")
        return item

    return {
        "attempt": summary,
        "exam": {
            "id": exam.get("id"),
            "title": exam.get("title", ""),
            "mode": exam.get("mode", "timed"),
            "feedback_display": exam.get("feedback_display", "picked"),
            "duration_minutes": exam.get("duration_minutes", 0),
            "shuffle_questions": exam.get("shuffle_questions", False),
            "shuffle_options": exam.get("shuffle_options", False),
        },
        "questions": [_question(row) for row in rows],
    }


@router.post("/attempts/{attempt_id}/answers")
def student_save_answer(
    attempt_id: str,
    payload: AnswerIn,
    ctx: StudentContext = Depends(require_student_write),
) -> dict[str, Any]:
    services = ctx.services
    try:
        services.attempts.save_answer(
            attempt_id, ctx.id, payload.question_id, payload.selected_option
        )
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc
    attempt = services.attempts.get(attempt_id) or {}
    exam_id = attempt.get("exam_id", "")
    services.activity.touch(
        ctx.id,
        activity.EVENT_ANSWER,
        exam_id=exam_id,
        question_id=payload.question_id,
    )
    # Practice mode is the only place the client is told whether it was right,
    # and only for the option just picked — the timed mode stays sealed until
    # submit so it can never be mined for answers.
    if (services.exams.get(exam_id) or {}).get("mode") == "practice":
        return {"ok": True, **_practice_feedback(services, attempt, payload)}
    return {"ok": True}


def _practice_feedback(
    services: Any, attempt: dict[str, Any], payload: AnswerIn
) -> dict[str, Any]:
    rows = services.exams.questions_for(
        attempt["exam_id"], int(attempt.get("question_version", 0))
    )
    row = next((r for r in rows if r["question_id"] == payload.question_id), None)
    if row is None:
        return {}
    correct = int(row["correct_answer_snapshot"])
    return {
        "is_correct": int(payload.selected_option) == correct,
        "correct_option_id": correct,
        "options_feedback": list(row.get("options_feedback_snapshot") or []),
        "explanation": row.get("explanation_snapshot", ""),
    }


@router.post("/attempts/{attempt_id}/pause")
def student_pause_attempt(
    attempt_id: str, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    """Freeze the countdown while the student is away from the session."""
    services = ctx.services
    try:
        attempt = services.attempts.pause(attempt_id, ctx.id)
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc
    return {"attempt": _attempt_summary(attempt)}


@router.post("/attempts/{attempt_id}/resume")
def student_resume_attempt(
    attempt_id: str, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    """Restart the countdown from the balance banked at pause time."""
    services = ctx.services
    try:
        attempt = services.attempts.resume(attempt_id, ctx.id)
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc
    services.activity.touch(ctx.id, activity.EVENT_VIEW, exam_id=attempt["exam_id"])
    return {"attempt": _attempt_summary(attempt)}


@router.post("/attempts/{attempt_id}/submit")
def student_submit_attempt(
    attempt_id: str, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    services = ctx.services
    try:
        attempt = services.attempts.submit(attempt_id, ctx.id)
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc

    exam = services.exams.get(attempt["exam_id"]) or {}
    services.notifications.notify_attempt_completed(attempt, exam)
    services.activity.touch(ctx.id, activity.EVENT_SUBMIT, exam_id=attempt["exam_id"])
    return {"result": _result_payload(attempt, exam)}


def _result_payload(attempt: dict[str, Any], exam: dict[str, Any]) -> dict[str, Any]:
    return {
        **_attempt_summary(attempt),
        "exam_title": exam.get("title", ""),
        "show_results": bool(exam.get("show_results", True)),
        "show_solutions": bool(exam.get("show_solutions_after_submit", True)),
    }


@router.get("/attempts/{attempt_id}/review")
def student_review_attempt(
    attempt_id: str, ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    try:
        review = services.attempts.review(attempt_id, ctx.id)
    except AttemptError as exc:
        raise ValidationError(str(exc)) from exc
    services.activity.touch(
        ctx.id, activity.EVENT_REVIEW, exam_id=review["attempt"]["exam_id"]
    )
    exam = review["exam"]
    return {
        "attempt": _attempt_summary(review["attempt"]),
        "exam": exam,
        "questions": review["questions"],
    }


@router.get("/attempts/{attempt_id}/result")
def student_attempt_result(
    attempt_id: str, ctx: StudentContext = Depends(require_student)
) -> dict[str, Any]:
    services = ctx.services
    attempt = services.attempts.get_owned(attempt_id, ctx.id)
    if attempt.get("status") == "in_progress":
        raise ValidationError("المحاولة لم تُسلَّم بعد.")
    exam = services.exams.get(attempt["exam_id"]) or {}
    return {"result": _result_payload(attempt, exam)}


# --- notifications ---------------------------------------------------------

@router.get("/notifications")
def student_notifications(
    ctx: StudentContext = Depends(require_student),
) -> dict[str, Any]:
    services = ctx.services
    return {
        "notifications": services.notifications.for_student(ctx.id),
        "unread": services.notifications.unread_count(ctx.id),
        "enabled": bool(ctx.student.get("notifications_enabled", True)),
    }


@router.post("/notifications/{notification_id}/read")
def student_read_notification(
    notification_id: str, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    ctx.services.notifications.mark_read(ctx.id, notification_id)
    return {"ok": True}


@router.post("/notifications/read-all")
def student_read_all_notifications(
    ctx: StudentContext = Depends(require_student_write),
) -> dict[str, Any]:
    """Clear the whole unread badge (called when the list is opened)."""
    changed = ctx.services.notifications.mark_all_read(ctx.id)
    return {"ok": True, "marked": changed, "unread": 0}


@router.post("/activity/heartbeat")
def student_activity_heartbeat(
    payload: ActivityIn, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    """Mark the student active while an attempt session is open.

    The Mini App sends this only while the student is genuinely present in a
    session (the tab is visible) — the client stops it the moment they leave, so
    this is not an idle keep-alive. It is what makes the live feed show who is
    answering *right now* between two answers.
    """
    ctx.services.activity.touch(
        ctx.id,
        activity.EVENT_VIEW,
        exam_id=payload.exam_id,
        question_id=payload.question_id,
    )
    return {"ok": True}


@router.post("/notifications/prefs")
def student_notification_prefs(
    payload: NotificationPrefsIn, ctx: StudentContext = Depends(require_student_write)
) -> dict[str, Any]:
    student = ctx.services.students.set_notifications(ctx.id, payload.enabled)
    return {"ok": True, "enabled": bool(student.get("notifications_enabled", True))}
