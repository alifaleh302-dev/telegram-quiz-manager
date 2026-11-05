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
    require_page_permission,
    require_permission,
)
from app.storage.validators import ValidationError

router = APIRouter(tags=["admin-exams"], prefix="/api/admin")
page_router = APIRouter(tags=["admin-exams"])


def _render_page(request: Request, template: str, context: dict[str, Any], session: Any) -> Response:
    return render(request, template, context, session=session)


class ExamIn(BaseModel):
    title: str
    description: str = ""
    category: str = "General"
    cover_image: str = ""
    access_mode: str = "always"
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


# --- exams -----------------------------------------------------------------

@router.get("/exams")
async def admin_list_exams(
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
async def admin_create_exam(
    request: Request, payload: ExamIn, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    data = payload.model_dump()
    question_ids = data.pop("question_ids", None) or []
    exam = services.exams.create(data, question_ids, actor=session.username)
    return _exam_view(exam, services)


@router.get("/exams/{exam_id}")
async def admin_get_exam(
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
    notified = 0
    if not was_live:
        notified = _notify_if_live(services, exam)
    return {**_exam_view(exam, services), "notified": notified}


@router.delete("/exams/{exam_id}")
async def admin_delete_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    services.exams.delete(exam_id, actor=session.username)
    return {"ok": True, "deleted": exam_id}


@router.post("/exams/{exam_id}/questions")
async def admin_set_exam_questions(
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
    notified = _notify_if_live(services, exam)
    return {"exam": _exam_view(exam, services), "notified": notified}


@router.post("/exams/{exam_id}/notify")
async def admin_notify_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    """Manually (re)send the 'new exam' notice to every eligible student.

    Dedupe means a student already notified for this exam is skipped, so this is
    safe to press again after adding new students.
    """
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        raise ValidationError(f"الاختبار {exam_id} غير موجود.")
    if exam.get("status") not in ("published", "scheduled"):
        raise ValidationError("لا يمكن إشعار الطلاب قبل نشر الاختبار.")
    notified = services.notifications.notify_exam_published(exam)
    return {"ok": True, "notified": notified, "exam": _exam_view(exam, services)}


@router.post("/exams/{exam_id}/close")
async def admin_close_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.close(exam_id, actor=session.username), services)


@router.post("/exams/{exam_id}/archive")
async def admin_archive_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.archive(exam_id, actor=session.username), services)


@router.post("/exams/{exam_id}/reopen")
async def admin_reopen_exam(
    request: Request, exam_id: str, session: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    return _exam_view(services.exams.reopen(exam_id, actor=session.username), services)


@router.get("/exams/{exam_id}/attempts")
async def admin_exam_attempts(
    request: Request, exam_id: str, _: Any = Depends(require_permission("exams"))
) -> dict[str, Any]:
    services = get_services(request)
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    rows = []
    for attempt in services.attempts.attempts_for_exam(exam_id):
        rows.append({**attempt, "student": students.get(attempt["student_id"])})
    return {"attempts": sorted(rows, key=lambda a: a.get("started_at") or "", reverse=True)}


# --- students --------------------------------------------------------------

@router.get("/students")
async def admin_list_students(
    request: Request, _: Any = Depends(require_permission("exams")), search: str = ""
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
async def admin_student_attempts(
    request: Request, student_id: str, _: Any = Depends(require_permission("exams"))
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
async def admin_reset_attempts(
    request: Request,
    student_id: str,
    exam_id: str,
    session: Any = Depends(require_permission("exams")),
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
async def admin_list_attempts(
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
async def admin_attempt_detail(
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
async def admin_exam_analytics(
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

@page_router.get("/exams", response_class=HTMLResponse)
async def exams_page(
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
async def exam_new_page(
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
async def exam_detail_page(
    request: Request, exam_id: str, session: Any = Depends(require_page_permission("exams"))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    exam = services.exams.get(exam_id)
    if exam is None:
        return RedirectResponse(url="/exams", status_code=303)
    students = {s["id"]: services.students.view(s) for s in services.students.list_students()}
    attempts = [
        {**a, "student": students.get(a["student_id"])}
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
        },
        session=session,
    )


@page_router.get("/exams/{exam_id}/edit", response_class=HTMLResponse)
async def exam_edit_page(
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
async def students_page(
    request: Request,
    session: Any = Depends(require_page_permission("exams")),
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
async def attempts_page(
    request: Request,
    session: Any = Depends(require_page_permission("exams")),
    status: str = "",
    search: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
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
    return render(
        request,
        "attempts.html",
        {"attempts": rows, "filters": {"status": status, "search": search}},
        session=session,
    )


@page_router.get("/exam-analytics", response_class=HTMLResponse)
async def exam_analytics_page(
    request: Request, session: Any = Depends(require_page_permission("exams"))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    students = services.students.list_students()
    attempts = services.attempts.all_attempts()
    submitted = [a for a in attempts if a.get("status") == "submitted"]
    # Aggregates count each student's *best* attempt per exam, not every try.
    best = services.attempts.best_per_student(submitted)
    scores = [float(a.get("percentage") or 0) for a in best]
    exams = services.exams.list_exams()

    # Score distribution buckets for the chart.
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

    per_exam = []
    for exam in exams:
        stats = services.exams.stats(exam["id"])
        per_exam.append({"title": exam["title"], "id": exam["id"], **stats})
    per_exam.sort(key=lambda e: e["attempts"], reverse=True)

    return render(
        request,
        "exam_analytics.html",
        {
            "totals": {
                "students": len(students),
                "exams": len(exams),
                "attempts": len(attempts),
                "completed": len(submitted),
                "graded": len(best),
                "average": round(sum(scores) / len(scores), 1) if scores else 0.0,
                "highest": round(max(scores), 1) if scores else 0.0,
                "lowest": round(min(scores), 1) if scores else 0.0,
            },
            "buckets": buckets,
            "per_exam": per_exam,
            "active_students": len({a["student_id"] for a in submitted}),
        },
        session=session,
    )
