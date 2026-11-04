"""Questions: HTML pages, JSON CRUD, bulk actions, import and export."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import api_guard, get_services, render, require_page_session
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


class ImportPayload(BaseModel):
    questions: list[dict[str, Any]]
    duplicate_policy: str = "skip"


# --- HTML pages ------------------------------------------------------------

@router.get("/questions", response_class=HTMLResponse)
async def questions_page(
    request: Request,
    session=Depends(require_page_session),
    search: str = "",
    category: str = "",
    status: str = "",
    difficulty: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    questions = services.questions.list_questions(
        search=search, category=category, status=status, difficulty=difficulty
    )
    return render(
        request,
        "questions.html",
        {
            "questions": questions,
            "categories": services.questions.categories(),
            "stats": services.questions.stats(),
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
async def question_new_page(request: Request, session=Depends(require_page_session)) -> Response:
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
    request: Request, question_id: str, session=Depends(require_page_session)
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
    _: Any = Depends(api_guard),
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
    request: Request, payload: QuestionIn, session: Any = Depends(api_guard)
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
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    return services.questions.update(
        question_id, payload.model_dump(), actor=session.username
    )


@router.delete("/api/questions/{question_id}")
async def delete_question(
    request: Request, question_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.questions.delete(question_id, actor=session.username)
    return {"ok": True, "deleted": question_id}


@router.post("/api/questions/{question_id}/duplicate", status_code=201)
async def duplicate_question(
    request: Request, question_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    return services.questions.duplicate(question_id, actor=session.username)


@router.post("/api/questions/bulk")
async def bulk_questions(
    request: Request, payload: BulkAction, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    if payload.action == "delete":
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
    request: Request, payload: ImportPayload, session: Any = Depends(api_guard)
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
    session: Any = Depends(api_guard),
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
async def export_questions(request: Request, _: Any = Depends(api_guard)) -> JSONResponse:
    services = get_services(request)
    document = services.questions.export_document()
    headers = {"Content-Disposition": 'attachment; filename="questions.json"'}
    return JSONResponse(document, headers=headers)
