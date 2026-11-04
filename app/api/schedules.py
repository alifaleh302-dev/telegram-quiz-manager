"""Schedules: HTML page, CRUD, pause/resume, skip-next and run-now."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import api_guard, get_services, render, require_page_session

router = APIRouter(tags=["schedules"])


class ScheduleIn(BaseModel):
    channel_id: str
    type: str = "daily"
    enabled: bool = True
    times: list[str] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    interval_minutes: int = 0
    timezone: str = ""
    question_selection: str = "sequential"
    category_filter: str = ""
    questions_per_day: int = 0
    questions_per_run: int = 1
    batch_interval_seconds: int = 0
    start_date: str = ""
    end_date: str = ""
    name: str = ""


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request, session=Depends(require_page_session)) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    schedules = services.schedules.list_schedules()
    for schedule in schedules:
        schedule["next_run_at"] = services.engine.next_run_for(schedule["id"])
    return render(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "channels": services.channels.list_channels(),
            "categories": services.questions.categories(),
            "timezone": services.app_settings.get().get("timezone", "UTC"),
        },
        session=session,
    )


@router.get("/api/schedules")
async def list_schedules(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    schedules = services.schedules.list_schedules()
    for schedule in schedules:
        schedule["next_run_at"] = services.engine.next_run_for(schedule["id"])
    return {"schedules": schedules}


@router.post("/api/schedules", status_code=201)
async def create_schedule(
    request: Request, payload: ScheduleIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    schedule = services.schedules.create(payload.model_dump(), actor=session.username)
    services.engine.reload()
    return schedule


@router.put("/api/schedules/{schedule_id}")
async def update_schedule(
    request: Request,
    schedule_id: str,
    payload: ScheduleIn,
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    schedule = services.schedules.update(
        schedule_id, payload.model_dump(), actor=session.username
    )
    services.engine.reload()
    return schedule


@router.post("/api/schedules/{schedule_id}/toggle")
async def toggle_schedule(
    request: Request, schedule_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    schedule = services.schedules.get(schedule_id)
    if schedule is None:
        return {"ok": False, "error": "الجدول غير موجود."}
    updated = services.schedules.set_enabled(
        schedule_id, not schedule.get("enabled", True), actor=session.username
    )
    services.engine.reload()
    return {"ok": True, "schedule": updated}


@router.post("/api/schedules/{schedule_id}/skip-next")
async def skip_next(
    request: Request, schedule_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)

    def _mutate(document: dict[str, Any]) -> None:
        for schedule in document["schedules"]:
            if schedule["id"] == schedule_id:
                schedule["skip_next"] = True

    services.schedules._storage.schedules.update(_mutate)  # noqa: SLF001
    services.audit.record(
        "schedule.skip_requested",
        actor=session.username,
        level="warning",
        message=f"طلب تجاوز الموعد القادم للجدول {schedule_id}",
    )
    return {"ok": True}


@router.post("/api/schedules/{schedule_id}/run-now")
async def run_now(
    request: Request, schedule_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    result = await services.engine.trigger_now(schedule_id)
    return {"ok": bool(result.get("ok")), "result": result}


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(
    request: Request, schedule_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.schedules.delete(schedule_id, actor=session.username)
    services.engine.reload()
    return {"ok": True}
