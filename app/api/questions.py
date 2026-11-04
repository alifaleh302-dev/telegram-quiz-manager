"""Questions: HTML pages, JSON CRUD, bulk actions, import and export."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    get_services,
    render,
    require_page_permission,
    require_permission,
)
from app.storage.validators import ValidationError

router = APIRouter(tags=["questions"])


class QuestionIn(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)
    correct_option_id: int = 0
    explanation: str = ""
    category: str = "General"
    difficulty: str = "medium"
    status: str = "draft"


class BulkAction(BaseModel):
    ids: list[str]
    action: str
    status: str | None = None
    # Bulk delete is destructive and irreversible from the UI; require an explicit
    # acknowledgement so a stray click can never empty the bank.
    confirm: bool = False


class ImportPayload(BaseModel):
    questions: list[dict[str, Any]]
    duplicate_policy: str = "skip"


# --- HTML pages ------------------------------------------------------------

@router.get("/questions", response_class=HTMLResponse)
async def questions_page(
    request: Request,
    session=Depends(require_page_permission('questions')),
    search: str = "",
    category: str = "",
    status: str = "",
    difficulty: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    channels = services.channels.list_channels()
    channel_names = {c["id"]: c["name"] for c in channels}
    # Publishing state is per channel, so show which channels each question has
    # already gone out on instead of a single global "published" flag.
    published_pairs = services.publishing.published_pairs()
    published_ids = {qid for (_, qid) in published_pairs}

    # The "published" filter is now a view over the history (published to *some*
    # channel), not a stored question status.
    service_status = "" if status == "published" else status
    questions = services.questions.list_questions(
        search=search, category=category, status=service_status, difficulty=difficulty
    )
    if status == "published":
        questions = [q for q in questions if q["id"] in published_ids]

    pacing_interval, _ = services.publishing.pacing()
    for question in questions:
        question["published_channels"] = [
            {
                "id": channel["id"],
                "name": channel_names.get(channel["id"], channel["id"]),
            }
            for channel in channels
            if (channel["id"], question["id"]) in published_pairs
        ]
        question["is_published"] = bool(question["published_channels"])
    return render(
        request,
        "questions.html",
        {
            "questions": questions,
            "categories": services.questions.categories(),
            "stats": {**services.questions.stats(), "published": len(published_ids)},
            "channels": channels,
            "pacing_interval": pacing_interval,
            "filters": {
                "search": search,
                "category": category,
                "status": status,
                "difficulty": difficulty,
            },
        },
        session=session,
    )


@router.get("/questions/new", response_class=HTMLResponse)
async def question_new_page(
    request: Request, session=Depends(require_page_permission('questions'))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "question_editor.html",
        {
            "question": None,
            "categories": services.questions.categories(),
            "mode": "create",
        },
        session=session,
    )


@router.get("/questions/{question_id}/edit", response_class=HTMLResponse)
async def question_edit_page(
    request: Request, question_id: str, session=Depends(require_page_permission('questions'))
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    question = services.questions.get(question_id)
    if question is None:
        return RedirectResponse(url="/questions", status_code=303)
    return render(
        request,
        "question_editor.html",
        {
            "question": question,
            "categories": services.questions.categories(),
            "mode": "edit",
        },
        session=session,
    )


# --- JSON API --------------------------------------------------------------

@router.get("/api/questions")
async def list_questions(
    request: Request,
    _: Any = Depends(require_permission('questions')),
    search: str = "",
    category: str = "",
    status: str = "",
    difficulty: str = "",
) -> dict[str, Any]:
    services = get_services(request)
    return {
        "questions": services.questions.list_questions(
            search=search, category=category, status=status, difficulty=difficulty
        ),
        "stats": services.questions.stats(),
    }


@router.post("/api/questions", status_code=201)
async def create_question(
    request: Request, payload: QuestionIn, session: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    question = services.questions.create(payload.model_dump(), actor=session.username)
    services.engine.reload()
    return question


@router.put("/api/questions/{question_id}")
async def update_question(
    request: Request,
    question_id: str,
    payload: QuestionIn,
    session: Any = Depends(require_permission('questions')),
) -> dict[str, Any]:
    services = get_services(request)
    return services.questions.update(
        question_id, payload.model_dump(), actor=session.username
    )


@router.delete("/api/questions/{question_id}")
async def delete_question(
    request: Request, question_id: str, session: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    services.questions.delete(question_id, actor=session.username)
    return {"ok": True, "deleted": question_id}


@router.post("/api/questions/{question_id}/duplicate", status_code=201)
async def duplicate_question(
    request: Request, question_id: str, session: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    return services.questions.duplicate(question_id, actor=session.username)


@router.post("/api/questions/bulk")
async def bulk_questions(
    request: Request, payload: BulkAction, session: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    if payload.action == "delete":
        if not payload.confirm:
            raise ValidationError(
                "تأكيد الحذف الجماعي مطلوب. حدّد الأسئلة ثم أكّد الحذف."
            )
        count = services.questions.bulk_delete(payload.ids, actor=session.username)
    elif payload.action == "set_status":
        if not payload.status:
            raise ValidationError("يجب تحديد الحالة المطلوبة.")
        count = services.questions.bulk_set_status(
            payload.ids, payload.status, actor=session.username
        )
    else:
        raise ValidationError("إجراء جماعي غير معروف.")
    services.engine.reload()
    return {"ok": True, "affected": count}


@router.post("/api/questions/import")
async def import_questions(
    request: Request, payload: ImportPayload, session: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    report = services.questions.import_questions(
        payload.questions,
        actor=session.username,
        duplicate_policy=payload.duplicate_policy,
    )
    services.engine.reload()
    return report


@router.post("/api/questions/import-file")
async def import_questions_file(
    request: Request,
    file: UploadFile = File(...),
    duplicate_policy: str = Form("skip"),
    session: Any = Depends(require_permission('questions')),
) -> dict[str, Any]:
    services = get_services(request)
    raw = (await file.read()).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"الملف ليس JSON صالحاً: {exc}") from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("questions", [])
    report = services.questions.import_questions(
        parsed, actor=session.username, duplicate_policy=duplicate_policy
    )
    services.engine.reload()
    return report


@router.get("/api/questions/export")
async def export_questions(request: Request, _: Any = Depends(require_permission('questions'))) -> JSONResponse:
    services = get_services(request)
    document = services.questions.export_document()
    headers = {"Content-Disposition": 'attachment; filename="questions.json"'}
    return JSONResponse(document, headers=headers)


# Registered last so the literal paths above ("export", "import-file") win over
# this catch-all parameter route.
@router.get("/api/questions/{question_id}")
async def get_question(
    request: Request, question_id: str, _: Any = Depends(require_permission('questions'))
) -> dict[str, Any]:
    services = get_services(request)
    question = services.questions.get(question_id)
    if question is None:
        raise ValidationError(f"السؤال {question_id} غير موجود.")
    return question
