"""Engagement analytics: the stats page and its JSON endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import api_guard, get_services, render, require_page_session

router = APIRouter(tags=["analytics"])


def _analytics_payload(services: Any, channel_id: str = "") -> dict[str, Any]:
    return {
        "overview": services.analytics.overview(),
        "per_question": services.analytics.per_question(channel_id=channel_id),
        "by_channel": services.analytics.by_channel(),
        "by_category": services.analytics.by_category(),
        "activity": services.analytics.activity_series(days=14),
        "channels": services.channels.list_channels(),
        "channel_filter": channel_id,
        "dry_run": services.dry_run,
    }


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    session=Depends(require_page_session),
    channel: str = "",
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "analytics.html",
        _analytics_payload(services, channel),
        session=session,
    )


@router.get("/api/analytics")
async def analytics_api(
    request: Request, _: Any = Depends(api_guard), channel: str = ""
) -> dict[str, Any]:
    return _analytics_payload(get_services(request), channel)


@router.get("/api/analytics/poll/{poll_id}")
async def poll_detail(
    request: Request, poll_id: str, _: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    detail = services.analytics.poll_detail(poll_id)
    if detail is None:
        return {"ok": False, "error": "لا توجد نتائج لهذا الاستطلاع."}
    return {"ok": True, "poll": detail}


class CollectIn(BaseModel):
    channel_id: str = ""
    poll_ids: list[str] | None = None


@router.post("/api/analytics/collect")
async def collect(
    request: Request, payload: CollectIn | None = None, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    if services.dry_run:
        return {"ok": False, "error": "البوت في وضع تجريبي؛ لا توجد نتائج لجمعها."}
    payload = payload or CollectIn()
    summary = await services.analytics.collect(
        services.telegram,
        poll_ids=payload.poll_ids,
        channel_id=payload.channel_id,
        trigger="manual",
        actor=session.username,
    )
    return summary
