"""Publishing: history page, publish-now, pause/resume and retry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    get_services,
    render,
    require_admin,
    require_admin_page,
    require_permission,
)
from app.services.publishing_service import STATUS_CANCELLED
from app.storage.validators import ValidationError

router = APIRouter(tags=["publishing"])


class PublishNowIn(BaseModel):
    question_id: str = ""
    channel_id: str = ""
    schedule_id: str | None = None


class ManualPublishIn(BaseModel):
    question_id: str
    channel_id: str


class BulkPublishIn(BaseModel):
    question_ids: list[str]
    channel_id: str


class DuplicatesIn(BaseModel):
    question_ids: list[str] = Field(default_factory=list)
    channel_id: str = ""


class DedicatedScheduleIn(BaseModel):
    channel_id: str
    question_ids: list[str] = Field(default_factory=list)
    interval_minutes: int = 30
    start_time: str = ""
    start_date: str = ""
    end_date: str = ""
    questions_per_run: int = 1
    batch_interval_seconds: int = 0
    name: str = ""
    timezone: str = ""
    question_selection: str = "sequential"


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request, session=Depends(require_admin_page), status: str = ""
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
    request: Request, _: Any = Depends(require_admin), status: str = "", limit: int = 100
) -> dict[str, Any]:
    services = get_services(request)
    return {
        "history": services.publishing.history(limit=limit, status=status),
        "stats": services.publishing.stats(),
    }


@router.post("/api/publishing/publish-now")
async def publish_now(
    request: Request, payload: PublishNowIn, session: Any = Depends(require_permission('publish'))
) -> dict[str, Any]:
    services = get_services(request)
    question_id = payload.question_id

    channel_id = payload.channel_id
    if not channel_id:
        default = services.channels.default_channel()
        if default is None:
            return {"ok": False, "error": "لا توجد قناة افتراضية. أضف قناة أولاً."}
        channel_id = default["id"]

    if not question_id:
        # Pick the next question that has not already gone out on *this* channel.
        candidates = services.questions.pick_candidates(
            channel_id=channel_id,
            published_pairs=services.publishing.published_pairs(),
        )
        if not candidates:
            return {"ok": False, "error": "لا يوجد سؤال بحالة «جاهز» للنشر على هذه القناة."}
        question_id = candidates[0]["id"]

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
    request: Request, payload: ManualPublishIn, session: Any = Depends(require_permission('publish'))
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


@router.post("/api/publishing/bulk")
async def bulk_publish(
    request: Request, payload: BulkPublishIn, session: Any = Depends(require_permission("publish"))
) -> dict[str, Any]:
    """Publish several selected questions to one channel, in order.

    Each question is published sequentially through the same path as the single
    manual publish, so the pending-row claim and the audit trail behave the
    same. Sends are spaced out by the configured pacing interval and Telegram
    flood control is honoured, which is what keeps a 50-question burst from
    being rate-limited partway. One failure does not abort the rest; the caller
    gets a per-question report.
    """
    services = get_services(request)

    # Preserve the order the user selected, but never send the same question twice.
    ids = list(dict.fromkeys(qid for qid in payload.question_ids if qid))
    if not ids:
        raise ValidationError("لم يتم تحديد أي سؤال.")
    if not payload.channel_id:
        raise ValidationError("يجب اختيار القناة المستهدفة.")

    results = await services.publishing.publish_many(
        ids, payload.channel_id, actor=session.username
    )
    succeeded = sum(1 for row in results if row["status"] == "success")
    cancelled = sum(1 for row in results if row["status"] == STATUS_CANCELLED)
    failed = len(results) - succeeded - cancelled
    return {
        "ok": failed == 0 and cancelled == 0,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": cancelled,
        "results": results,
    }


@router.post("/api/publishing/duplicates")
async def check_duplicates(
    request: Request,
    payload: DuplicatesIn,
    session: Any = Depends(require_permission("publish")),
) -> dict[str, Any]:
    """Report which of the selected questions were already published to a channel.

    The publish window calls this right after the user picks a channel, so it can
    warn about repeats *for that channel only* and let the user exclude them.
    """
    services = get_services(request)
    if not payload.channel_id:
        raise ValidationError("يجب اختيار القناة المستهدفة.")
    rows = services.publishing.duplicates(payload.question_ids, payload.channel_id)
    questions = {q["id"]: q for q in services.questions.list_questions()}
    for row in rows:
        question = questions.get(row["id"])
        row["text"] = question["question"] if question else ""
        row["category"] = question["category"] if question else ""
    return {"duplicates": rows, "count": len(rows)}


@router.post("/api/publishing/dedicated-schedule", status_code=201)
async def create_dedicated_schedule(
    request: Request,
    payload: DedicatedScheduleIn,
    session: Any = Depends(require_permission("schedules")),
) -> dict[str, Any]:
    """Create a fresh schedule for one batch of questions and pin them to it.

    This is the "create a dedicated schedule" option in the publish window: a
    brand-new ``interval`` schedule (queue-only, so it publishes exactly the
    approved list and never tops up from the bank) whose queue is the selected
    questions in order — repeats the user approved included. It needs the
    ``schedules`` grant, matching the UI which only offers the option to users
    who can manage schedules.
    """
    services = get_services(request)
    ids = list(dict.fromkeys(qid for qid in payload.question_ids if qid))
    if not ids:
        raise ValidationError("لم يتم تحديد أي سؤال.")
    if services.channels.get(payload.channel_id) is None:
        raise ValidationError("القناة المستهدفة غير موجودة.")
    missing = [qid for qid in ids if services.questions.get(qid) is None]
    if missing:
        raise ValidationError(f"أسئلة غير موجودة: {', '.join(missing)}.")

    start_time = (payload.start_time or "").strip()
    schedule_payload = {
        "channel_id": payload.channel_id,
        "type": "interval",
        "enabled": True,
        "times": [],
        "interval_minutes": payload.interval_minutes,
        "start_time": start_time,
        "start_date": payload.start_date,
        "end_date": payload.end_date,
        "questions_per_run": payload.questions_per_run,
        "batch_interval_seconds": payload.batch_interval_seconds,
        "question_selection": payload.question_selection,
        "queue_only": True,
        "name": payload.name or f"جدول مخصص · {len(ids)} سؤالاً",
    }
    if payload.timezone:
        schedule_payload["timezone"] = payload.timezone

    schedule = services.schedules.create(schedule_payload, actor=session.username)
    services.schedules.assign_questions(schedule["id"], ids, actor=session.username)
    services.engine.reload()

    refreshed = services.schedules.get(schedule["id"]) or schedule
    return {
        "ok": True,
        "schedule_id": schedule["id"],
        "queued": len(services.schedules.queue_of(refreshed)),
        "next_run_at": services.engine.next_run_for(schedule["id"]),
    }


@router.post("/api/publishing/cancel")
async def cancel_publishing(
    request: Request, session: Any = Depends(require_permission("publish"))
) -> dict[str, Any]:
    """Ask the in-progress publish run to stop after the message now in flight.

    The stop is cooperative: the run checks the flag between sends, so the quiz
    already being delivered to Telegram is not cut off mid-request. Questions
    not yet reached are reported as ``cancelled`` and leave no history row.
    """
    services = get_services(request)
    stopped = services.publishing.request_cancel()
    if stopped:
        services.audit.record(
            "publishing.cancel_requested",
            actor=session.username,
            level="warning",
            message="تم طلب إلغاء النشر الجاري.",
        )
    return {"ok": True, "cancelled": stopped, **services.publishing.progress()}


@router.get("/api/publishing/progress")
async def publishing_progress(
    request: Request, session: Any = Depends(require_permission("publish"))
) -> dict[str, Any]:
    """Poll the in-progress run so the UI can show progress and a cancel button."""
    services = get_services(request)
    return services.publishing.progress()


@router.post("/api/publishing/pause")
async def pause_publishing(request: Request, session: Any = Depends(require_permission('automation'))) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.set_publishing_enabled(False, actor=session.username)
    return {"ok": True, "publishing_enabled": False}


@router.post("/api/publishing/resume")
async def resume_publishing(
    request: Request, session: Any = Depends(require_permission('automation'))
) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.set_publishing_enabled(True, actor=session.username)
    return {"ok": True, "publishing_enabled": True}


@router.post("/api/publishing/retry/{entry_id}")
async def retry_publish(
    request: Request, entry_id: str, session: Any = Depends(require_permission('automation'))
) -> dict[str, Any]:
    services = get_services(request)
    entry = await services.publishing.retry(entry_id, actor=session.username)
    return {"ok": entry["status"] == "success", "entry": entry}


@router.post("/api/publishing/reconcile")
async def reconcile(request: Request, session: Any = Depends(require_permission('automation'))) -> dict[str, Any]:
    services = get_services(request)
    count = services.publishing.reconcile_pending(actor=session.username)
    return {"ok": True, "reconciled": count}
