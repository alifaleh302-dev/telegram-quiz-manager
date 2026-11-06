"""Admin Exam API: manage exams, students, attempts and exam analytics.

All routes sit under ``/api/admin`` and require the ``exams`` grant (admins hold
every grant). They reuse the existing question bank through :class:`ExamService`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    get_services,
    render,
    require_any_permission,
    require_page_permission,
    require_permission,
)
from app.storage.validators import ValidationError

router = APIRouter(tags=["admin-exams"], prefix="/api/admin")
page_router = APIRouter(tags=["admin-exams"])


def _render_page(request: Request, template: str, context: dict[str, Any], session: Any) -> Response:
    return render(request, template, context, session=session)


def _activity_snapshot(services: Any, exam_id: str = "") -> dict[str, Any]:
    """The live feed: active students, optionally scoped to one exam.

    Resolution to names/titles happens here so the client only renders what it
    receives. ``ttl_seconds`` is sent so the UI can explain the inactivity rule.
    """
    snapshot = services.activity.snapshot(
        students=services.students, exams=services.exams
    )
    if exam_id:
        snapshot["active"] = [a for a in snapshot["active"] if a["exam_id"] == exam_id]
        snapshot["count"] = len(snapshot["active"])
    # "Solved now": active students whose latest action was a submit, i.e. they
    # just finished rather than still working through the questions.
    snapshot["solved_now"] = sum(1 for a in snapshot["active"] if a["event"] == "submit")
    return snapshot


class ExamIn(BaseModel):
    title: str
    description: str = ""
    category: str = "General"
    cover_image: str = ""
    access_mode: str = "always"
    mode: str = "timed"
    # Either pre-combined instants or the separate date/time fields.
    starts_at: str = ""
    ends_at: str = ""
    start_date: str = ""
    start_time: str = ""
    end_date: str = ""
    end_time: str = ""
    timezone: str = "Asia/Riyadh"
    duration_minutes: int = 0
    max_attempts: int = 1
    allow_retake: bool = True
    count_abandoned_attempts: bool = True
    shuffle_questions: bool = False
    shuffle_options: bool = False
    show_results: bool = True
    show_solutions_after_submit: bool = True
    status: str = "draft"
    # ``None`` means "leave the question set untouched" (partial update); an
    # explicit list replaces it. On create, ``None`` is treated as empty.
    question_ids: list[str] | None = None


class ExamQuestionsIn(BaseModel):
    question_ids: list[str] = Field(default_factory=list)


def _exam_view(exam: dict[str, Any], services: Any) -> dict[str, Any]:
    stats = services.exams.stats(exam["id"])
    return {
        **exam,
        "question_count": stats["questions"],
        "stats": stats,
        "effective_state": services.exams.effective_state(exam),
    }


def _notify_if_live(services: Any, exam: dict[str, Any]) -> int:
    """Broadcast a 'new exam' notice when an exam is (or becomes) live.

    Honours the global ``notifications.on_publish`` switch. A ``scheduled`` exam
    whose window has not opened notifies nobody yet; the due-exam sweep in the
    scheduler sends it once the window opens.
    """
    if not services.app_settings.notifications_config().get("on_publish", True):
        return 0
    if exam.get("status") not in ("published", "scheduled"):
        return 0
    return services.notifications.notify_exam_published(exam)


async def _notify_and_dispatch(services: Any, exam: dict[str, Any]) -> dict[str, int]:
    """Enqueue the 'new exam' notice and deliver it immediately.

    Enqueueing alone only queues rows for the scheduler's periodic sweep, so an
    interactive action would report students as notified while they have not
    received anything yet. Delivery is attempted right away; anything still
    undelivered (rate-limited or an offline student) stays ``pending`` for the
    retry sweep.
    """
    enqueued = _notify_if_live(services, exam)
    if not enqueued:
        return {"notified": 0, "enqueued": 0, "sent": 0, "failed": 0, "skipped": 0}
    summary = await services.notifications.dispatch(exam_id=exam["id"])
    # ``notified`` is kept for compatibility; ``sent`` is the number of messages
    # Telegram actually accepted.
    return {"notified": enqueued, "enqueued": enqueued, **summary}


# --- exams -----------------------------------------------------------------

@router.get("/exams")
def admin_list_exams(
    request: Request,
    _: Any = Depends(require_permission("exams")),
    status: str = "",
    category: str = "",
    search: str = "",
) -> dict[str, Any]:
    services = get_services(request)
    exams = services.exams.list_exams(status=status, category=category, search=search)
    return {
        "exams": [_exam_view(e, services) for e in exams],
        "categories": services.exams.categories(),
    }


@router.post("/exams", status_code=201)
def admin_create_exam(
    request: Request, payload: ExamIn, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    data = payload.model_dump()
    question_ids = data.pop("question_ids", None) or []
    exam = services.exams.create(data, question_ids, actor=session.username)
    return _exam_view(exam, services)


@router.get("/exams/{exam_id}")
def admin_get_exam(
    request: Request, exam_id: str, _: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        raise ValidationError(f"الاختبار {exam_id} غير موجود.")
    return {
        "exam": _exam_view(exam, services),
        "questions": services.exams.questions_for(exam_id),
    }


@router.put("/exams/{exam_id}")
async def admin_update_exam(
    request: Request,
    exam_id: str,
    payload: ExamIn,
    session: Any = Depends(require_permission("exams")),
) -> dict[str, Any]:
    services = get_services(request)
    # A PUT is treated as a partial update: only the fields actually sent are
    # changed, so editing a title does not silently reset status or questions.
    data = payload.model_dump(exclude_unset=True)
    previous = services.exams.get(exam_id)
    was_live = (previous or {}).get("status") in ("published", "scheduled")
    question_ids = data.pop("question_ids", None)
    exam = services.exams.update(exam_id, data, question_ids, actor=session.username)
    # Editing an exam into a live status (draft → published) must announce it,
    # exactly as the dedicated publish endpoint does; the (student, exam, type)
    # dedupe stops a re-save from sending it twice.
    result = {"enqueued": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not was_live:
        result = await _notify_and_dispatch(services, exam)
    return {**_exam_view(exam, services), **result}


@router.delete("/exams/{exam_id}")
def admin_delete_exam(
    request: Request,
    exam_id: str,
    force: bool = False,
    session: Any = Depends(require_permission("exams")),
) -> dict[str, Any]:
    services = get_services(request)
    services.exams.delete(exam_id, force=force, actor=session.username)
    return {"ok": True, "deleted": exam_id, "force": force}


@router.post("/exams/{exam_id}/questions")
def admin_set_exam_questions(
    request: Request,
    exam_id: str,
    payload: ExamQuestionsIn,
    session: Any = Depends(require_permission("exams")),
) -> dict[str, Any]:
    services = get_services(request)
    return services.exams.set_questions(exam_id, payload.question_ids, actor=session.username)


@router.post("/exams/{exam_id}/publish")
async def admin_publish_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    exam = services.exams.publish(exam_id, actor=session.username)
    # Notify students only for a genuinely live exam (not a future window).
    result = await _notify_and_dispatch(services, exam)
    return {"exam": _exam_view(exam, services), **result}


@router.post("/exams/{exam_id}/notify")
async def admin_notify_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    """Manually (re)send the 'new exam' notice to every eligible student.

    Dedupe means a student already notified for this exam is skipped, so this is
    safe to press again after adding new students. Delivery is attempted before
    responding, so the count reflects messages actually sent.
    """
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        raise ValidationError(f"الاختبار {exam_id} غير موجود.")
    if exam.get("status") not in ("published", "scheduled"):
        raise ValidationError("لا يمكن إشعار الطلاب قبل نشر الاختبار.")
    result = await _notify_and_dispatch(services, exam)
    return {"ok": True, **result, "exam": _exam_view(exam, services)}


@router.post("/exams/{exam_id}/close")
def admin_close_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.close(exam_id, actor=session.username), services)


@router.post("/exams/{exam_id}/archive")
def admin_archive_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.archive(exam_id, actor=session.username), services)


@router.post("/exams/{exam_id}/reopen")
def admin_reopen_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.reopen(exam_id, actor=session.username), services)


@router.get("/exams/{exam_id}/attempts")
def admin_exam_attempts(
    request: Request, exam_id: str, _: Any = Depends(require_permission("attempts_manage"))
) -> dict[str, Any]:
    services = get_services(request)
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    activity = services.activity.active_for_exam(exam_id)
    rows = []
    for attempt in services.attempts.attempts_for_exam(exam_id):
        rows.append(
            {
                **attempt,
                "student": students.get(attempt["student_id"]),
                "activity": activity.get(attempt["student_id"]),
            }
        )
    return {"attempts": sorted(rows, key=lambda a: a.get("started_at") or "", reverse=True)}


@router.get("/activity")
def admin_activity(
    request: Request,
    exam_id: str = "",
    _: Any = Depends(require_any_permission("exams", "attempts_manage")),
) -> dict[str, Any]:
    """Live activity feed polled by the admin UI.

    Read-only and cheap: one scan of the small activity collection, so polling
    it every few seconds is fine. Scoping by ``exam_id`` shows only students
    currently interacting with that exam.
    """
    services = get_services(request)
    return _activity_snapshot(services, exam_id)


# --- students --------------------------------------------------------------

@router.get("/students")
def admin_list_students(
    request: Request, _: Any = Depends(require_permission("students_manage")), search: str = ""
) -> dict[str, Any]:
    services = get_services(request)
    students = services.students.list_students(search=search)
    rows = []
    for student in students:
        attempts = services.attempts.attempts_for_student(student["id"])
        submitted = [a for a in attempts if a.get("status") == "submitted"]
        best = services.attempts.best_per_student(submitted)
        scores = [float(a.get("percentage") or 0) for a in best]
        rows.append(
            {
                **services.students.view(student),
                "attempts": len(attempts),
                "completed": len(submitted),
                "graded": len(best),
                "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            }
        )
    return {"students": rows, "total": len(rows)}


@router.get("/students/{student_id}/attempts")
def admin_student_attempts(
    request: Request, student_id: str, _: Any = Depends(require_permission("students_manage"))
) -> dict[str, Any]:
    services = get_services(request)
    student = services.students.get(student_id)
    if student is None:
        raise ValidationError(f"الطالب {student_id} غير موجود.")
    exams = {e["id"]: e for e in services.exams.list_exams()}
    rows = [
        {**a, "exam_title": exams.get(a["exam_id"], {}).get("title", a["exam_id"])}
        for a in services.attempts.attempts_for_student(student_id)
    ]
    return {"student": services.students.view(student), "attempts": rows}


@router.post("/students/{student_id}/exams/{exam_id}/reset")
def admin_reset_attempts(
    request: Request,
    student_id: str,
    exam_id: str,
    session: Any = Depends(require_permission("attempts_manage")),
) -> dict[str, Any]:
    services = get_services(request)
    removed = services.attempts.reset_student_attempts(
        exam_id, student_id, actor=session.username
    )
    return {"ok": True, "removed": removed}


# --- attempts --------------------------------------------------------------

def _search_scopes(services: Any, search: str) -> tuple[set[str], set[str]]:
    """Resolve a free-text attempt search into student ids and exam ids.

    The attempts table shows a student *name* and an exam *title*, so a search
    typed against what the operator sees (``@Rashed_Almanie`` or an exam title)
    must resolve through the student and exam records rather than the raw ids.
    """
    text = (search or "").strip().lstrip("@").lower()
    if not text:
        return set(), set()
    student_ids = {s["id"] for s in services.students.list_students(search=text)}
    exam_ids = {e["id"] for e in services.exams.list_exams(search=text)}
    return student_ids, exam_ids


@router.get("/attempts")
def admin_list_attempts(
    request: Request,
    _: Any = Depends(require_permission("exams")),
    status: str = "",
    search: str = "",
) -> dict[str, Any]:
    services = get_services(request)
    exams = {e["id"]: e for e in services.exams.list_exams()}
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    student_ids, exam_ids = _search_scopes(services, search)
    rows = []
    for attempt in services.attempts.all_attempts(
        status=status, search=search, student_ids=student_ids, exam_ids=exam_ids
    ):
        rows.append(
            {
                **attempt,
                "exam_title": exams.get(attempt["exam_id"], {}).get("title", attempt["exam_id"]),
                "student": students.get(attempt["student_id"]),
            }
        )
    return {"attempts": rows}


@router.get("/attempts/{attempt_id}")
def admin_attempt_detail(
    request: Request, attempt_id: str, _: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    attempt = services.attempts.get(attempt_id)
    if attempt is None:
        raise ValidationError(f"المحاولة {attempt_id} غير موجودة.")
    answers = services.attempts.answers_for(attempt_id)
    return {"attempt": attempt, "answers": answers}


# --- analytics -------------------------------------------------------------

@router.get("/analytics")
def admin_exam_analytics(
    request: Request, _: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    students = services.students.list_students()
    attempts = services.attempts.all_attempts()
    submitted = [a for a in attempts if a.get("status") == "submitted"]
    best = services.attempts.best_per_student(submitted)
    scores = [float(a.get("percentage") or 0) for a in best]
    exams = services.exams.list_exams()

    # Per-question difficulty: count wrong answers against each bank question.
    wrong_by_question: dict[str, int] = {}
    seen_by_question: dict[str, int] = {}
    for attempt in submitted:
        for answer in services.attempts.answers_for(attempt["id"]):
            qid = answer["question_id"]
            seen_by_question[qid] = seen_by_question.get(qid, 0) + 1
            if not answer.get("is_correct"):
                wrong_by_question[qid] = wrong_by_question.get(qid, 0) + 1

    questions = {q["id"]: q for q in services.questions.list_questions()}
    hardest = sorted(
        (
            {
                "question_id": qid,
                "question": questions.get(qid, {}).get("question", qid),
                "attempts": seen_by_question[qid],
                "wrong": wrong,
                "wrong_rate": round(wrong / seen_by_question[qid] * 100, 1),
            }
            for qid, wrong in wrong_by_question.items()
        ),
        key=lambda item: item["wrong_rate"],
        reverse=True,
    )[:10]

    return {
        "totals": {
            "students": len(students),
            "exams": len(exams),
            "published_exams": sum(1 for e in exams if e.get("status") in ("published", "scheduled")),
            "attempts": len(attempts),
            "completed": len(submitted),
            "graded": len(best),
            "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "highest": round(max(scores), 1) if scores else 0.0,
            "lowest": round(min(scores), 1) if scores else 0.0,
            "completion_rate": round(
                len({a["student_id"] for a in submitted}) / len(students) * 100, 1
            )
            if students
            else 0.0,
        },
        "hardest_questions": hardest,
    }


# --- HTML pages ------------------------------------------------------------

def _exam_rows(services: Any) -> list[dict[str, Any]]:
    """Sidebar rows: every exam with the handful of figures it shows."""
    rows = []
    for exam in services.exams.list_exams():
        stats = services.exams.stats(exam["id"])
        rows.append(
            {
                "id": exam["id"],
                "title": exam["title"],
                "status": exam.get("status", "draft"),
                "questions": stats["questions"],
                "attempts": stats["attempts"],
                "completed": stats["completed"],
                "average": stats["average"],
            }
        )
    rows.sort(key=lambda e: (e["attempts"], e["title"]), reverse=True)
    return rows


def _overview_context(services: Any) -> dict[str, Any]:
    """Site-wide aggregates shown before a specific exam is picked."""
    students = services.students.list_students()
    attempts = services.attempts.all_attempts()
    submitted = [a for a in attempts if a.get("status") == "submitted"]
    # Aggregates count each student's *best* attempt per exam, not every try.
    best = services.attempts.best_per_student(submitted)
    scores = [float(a.get("percentage") or 0) for a in best]

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

    return {
        "totals": {
            "students": len(students),
            "exams": len(services.exams.list_exams()),
            "attempts": len(attempts),
            "completed": len(submitted),
            "graded": len(best),
            "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "highest": round(max(scores), 1) if scores else 0.0,
            "lowest": round(min(scores), 1) if scores else 0.0,
        },
        "buckets": buckets,
        "active_students": len({a["student_id"] for a in submitted}),
    }


def _detail_context(services: Any, exam_id: str) -> dict[str, Any]:
    exam = services.exams.get(exam_id)
    if exam is None:
        raise ValidationError(f"الاختبار {exam_id} غير موجود.")
    return {"exam": exam, "stats": services.attempts.exam_analytics(exam_id)}


def _htmx_guard(session: Any) -> Response | None:
    """HTMX swaps cannot follow a login redirect, so ask the client to navigate."""
    if isinstance(session, RedirectResponse):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return None


@page_router.get("/exam-analytics", response_class=HTMLResponse)
def exam_analytics_page(
    request: Request,
    session: Any = Depends(require_page_permission("exam_analytics")),
    exam: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    rows = _exam_rows(services)
    context: dict[str, Any] = {"exam_rows": rows, "active_exam": ""}
    if exam:
        context.update(_detail_context(services, exam))
        context["active_exam"] = exam
    else:
        context.update(_overview_context(services))
    return render(request, "exam_analytics.html", context, session=session)


@page_router.get("/exam-analytics/overview", response_class=HTMLResponse)
def exam_analytics_overview(
    request: Request, session: Any = Depends(require_page_permission("exam_analytics"))
) -> Response:
    guard = _htmx_guard(session)
    if guard is not None:
        return guard
    services = get_services(request)
    return render(
        request,
        "partials/exam_analytics_overview.html",
        _overview_context(services),
        session=session,
    )


@page_router.get("/exam-analytics/detail/{exam_id}", response_class=HTMLResponse)
def exam_analytics_detail(
    request: Request,
    exam_id: str,
    session: Any = Depends(require_page_permission("exam_analytics")),
) -> Response:
    guard = _htmx_guard(session)
    if guard is not None:
        return guard
    services = get_services(request)
    return render(
        request,
        "partials/exam_analytics_detail.html",
        _detail_context(services, exam_id),
        session=session,
    )


@page_router.get("/exams", response_class=HTMLResponse)
def exams_page(
    request: Request,
    session: Any = Depends(require_page_permission("exams")),
    status: str = "",
    search: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    exams = services.exams.list_exams(status=status, search=search)
    rows = []
    for exam in exams:
        rows.append({**_exam_view(exam, services), "stats": services.exams.stats(exam["id"])})
    return render(
        request,
        "exams.html",
        {
            "exams": rows,
            "categories": services.exams.categories(),
            "totals": {
                "all": len(services.exams.list_exams()),
                "published": len(services.exams.list_exams(status="published")),
                "draft": len(services.exams.list_exams(status="draft")),
                "students": services.students.count(),
            },
            "filters": {"status": status, "search": search},
        },
        session=session,
    )


@page_router.get("/exams/new", response_class=HTMLResponse)
def exam_new_page(
    request: Request, session: Any = Depends(require_page_permission("exams"))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "exam_editor.html",
        {
            "exam": None,
            "mode": "create",
            "questions": services.questions.list_questions(),
            "categories": services.questions.categories(),
            "timezone": services.app_settings.get().get("timezone", "Asia/Riyadh"),
            "selected_ids": [],
        },
        session=session,
    )


@page_router.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_detail_page(
    request: Request, exam_id: str, session: Any = Depends(require_page_permission("exams"))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        return RedirectResponse(url="/exams", status_code=303)
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    activity = services.activity.active_for_exam(exam_id)
    attempts = [
        {
            **a,
            "student": students.get(a["student_id"]),
            "activity": activity.get(a["student_id"]),
        }
        for a in services.attempts.attempts_for_exam(exam_id)
    ]
    attempts.sort(key=lambda a: a.get("started_at") or "", reverse=True)
    return render(
        request,
        "exam_detail.html",
        {
            "exam": _exam_view(exam, services),
            "questions": services.exams.questions_for(exam_id),
            "attempts": attempts,
            "stats": services.exams.stats(exam_id),
            "active_count": len(activity),
            "activity_ttl": services.activity.TTL_SECONDS,
        },
        session=session,
    )


@page_router.get("/exams/{exam_id}/edit", response_class=HTMLResponse)
def exam_edit_page(
    request: Request, exam_id: str, session: Any = Depends(require_page_permission("exams"))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        return RedirectResponse(url="/exams", status_code=303)
    return render(
        request,
        "exam_editor.html",
        {
            "exam": exam,
            "mode": "edit",
            "questions": services.questions.list_questions(),
            "categories": services.questions.categories(),
            "timezone": exam.get("timezone", "Asia/Riyadh"),
            "selected_ids": [q["question_id"] for q in services.exams.questions_for(exam_id)],
        },
        session=session,
    )


@page_router.get("/students", response_class=HTMLResponse)
def students_page(
    request: Request,
    session: Any = Depends(require_page_permission("students_manage")),
    search: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    rows = []
    for student in services.students.list_students(search=search):
        attempts = services.attempts.attempts_for_student(student["id"])
        submitted = [a for a in attempts if a.get("status") == "submitted"]
        best = services.attempts.best_per_student(submitted)
        scores = [float(a.get("percentage") or 0) for a in best]
        rows.append(
            {
                **services.students.view(student),
                "attempts": len(attempts),
                "completed": len(submitted),
                "graded": len(best),
                "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
            }
        )
    return render(
        request,
        "students.html",
        {"students": rows, "search": search},
        session=session,
    )


@page_router.get("/attempts", response_class=HTMLResponse)
def attempts_page(
    request: Request,
    session: Any = Depends(require_page_permission("attempts_manage")),
    status: str = "",
    search: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    exams = {e["id"]: e for e in services.exams.list_exams()}
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    student_ids, exam_ids = _search_scopes(services, search)
    live = {
        row["student_id"]: row
        for row in services.activity.active_students()
    }
    rows = []
    for attempt in services.attempts.all_attempts(
        status=status, search=search, student_ids=student_ids, exam_ids=exam_ids
    ):
        rows.append(
            {
                **attempt,
                "exam_title": exams.get(attempt["exam_id"], {}).get("title", attempt["exam_id"]),
                "student": students.get(attempt["student_id"]),
                # Present only while the student is active right now; the row
                # then shows an "online" dot next to the attempt.
                "activity": live.get(attempt["student_id"]),
            }
        )
    return render(
        request,
        "attempts.html",
        {
            "attempts": rows,
            "filters": {"status": status, "search": search},
            "active_count": len(live),
            "activity_ttl": services.activity.TTL_SECONDS,
        },
        session=session,
    )
