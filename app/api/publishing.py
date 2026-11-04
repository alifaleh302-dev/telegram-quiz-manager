"""Publishing: history page, publish-now, pause/resume and retry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import api_guard, get_services, render, require_page_session

router = APIRouter(tags=["publishing"])


class PublishNowIn(BaseModel):
    question_id: str = ""
    channel_id: str = ""
    schedule_id: str | None = None


class ManualPublishIn(BaseModel):
    question_id: str
    channel_id: str


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request, session=Depends(require_page_session), status: str = ""
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    entries = services.publishing.history(limit=200, status=status)
    channels = {c["id"]: c for c in services.channels.list_channels()}
    questions = {q["id"]: q for q in services.questions.list_questions()}
    for entry in entries:
        entry["channel_name"] = channels.get(entry["channel_id"], {}).get(
            "name", entry["channel_id"]
        )
        question = questions.get(entry["question_id"])
        entry["question_text"] = question["question"] if question else entry["question_id"]
    return render(
        request,
        "history.html",
        {
            "entries": entries,
            "stats": services.publishing.stats(),
            "status_filter": status,
            "channels": services.channels.list_channels(),
            "questions": services.questions.list_questions(status="ready"),
        },
        session=session,
    )


@router.get("/api/history")
async def list_history(
    request: Request, _: Any = Depends(api_guard), status: str = "", limit: int = 100
) -> dict[str, Any]:
    services = get_services(request)
    return {
        "history": services.publishing.history(limit=limit, status=status),
        "stats": services.publishing.stats(),
    }


@router.post("/api/publishing/publish-now")
async def publish_now(
    request: Request, payload: PublishNowIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    question_id = payload.question_id
    if not question_id:
        candidates = services.questions.pick_candidates()
        if not candidates:
            return {"ok": False, "error": "لا يوجد سؤال بحالة «جاهز» للنشر."}
        question_id = candidates[0]["id"]

    channel_id = payload.channel_id
    if not channel_id:
        default = services.channels.default_channel()
        if default is None:
            return {"ok": False, "error": "لا توجد قناة افتراضية. أضف قناة أولاً."}
        channel_id = default["id"]

    entry = await services.publishing.publish(
        question_id,
        channel_id,
        schedule_id=payload.schedule_id,
        trigger="manual",
        actor=session.username,
        respect_pause=False,
    )
    return {"ok": entry["status"] == "success", "entry": entry}


@router.post("/api/publishing/manual")
async def manual_publish(
    request: Request, payload: ManualPublishIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    """Publish a chosen question to a chosen channel right now.

    This is the human-initiated path, so re-publishing a question that already
    went out is allowed on purpose (the caller is explicitly asking for it) and
    the pause flag is ignored, matching the existing "publish now" button.
    """
    services = get_services(request)
    entry = await services.publishing.publish(
        payload.question_id,
        payload.channel_id,
        trigger="manual",
        actor=session.username,
        respect_pause=False,
        allow_repeat=True,
    )
    return {"ok": entry["status"] == "success", "entry": entry}


@router.post("/api/publishing/pause")
async def pause_publishing(request: Request, session: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.set_publishing_enabled(False, actor=session.username)
    return {"ok": True, "publishing_enabled": False}


@router.post("/api/publishing/resume")
async def resume_publishing(
    request: Request, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.set_publishing_enabled(True, actor=session.username)
    return {"ok": True, "publishing_enabled": True}


@router.post("/api/publishing/retry/{entry_id}")
async def retry_publish(
    request: Request, entry_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    entry = await services.publishing.retry(entry_id, actor=session.username)
    return {"ok": entry["status"] == "success", "entry": entry}


@router.post("/api/publishing/reconcile")
async def reconcile(request: Request, session: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    count = services.publishing.reconcile_pending(actor=session.username)
    return {"ok": True, "reconciled": count}
