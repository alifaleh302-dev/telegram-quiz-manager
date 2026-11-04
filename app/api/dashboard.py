"""Dashboard page and its data endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import api_guard, get_services, render, require_page_session
from app.core.permissions import is_admin
from app.core.security import Session
from app.services.container import Services

router = APIRouter(tags=["dashboard"])


def _dashboard_payload(services: Services, *, is_admin: bool = False) -> dict[str, Any]:
    questions = services.questions.stats()
    publishing = services.publishing.stats()
    channels = services.channels.list_channels()
    settings_doc = services.app_settings.get()

    payload: dict[str, Any] = {
        "questions": questions,
        "by_category": services.questions.by_category(),
        "publishing": publishing,
        "channels": {
            "total": len(channels),
            "enabled": sum(1 for c in channels if c.get("enabled")),
            "items": channels,
        },
        "schedules": {
            "total": len(services.schedules.list_schedules()),
            "enabled": sum(
                1 for s in services.schedules.list_schedules() if s.get("enabled")
            ),
        },
        "publishing_enabled": bool(settings_doc.get("publishing_enabled", True)),
        "next_runs": services.engine.next_runs(limit=5),
        "dry_run": services.dry_run,
        "timezone": settings_doc.get("timezone"),
        "daily_limit": settings_doc.get("daily_limit", 0),
    }
    # The activity feed is the system audit log; only admins may see it.
    if is_admin:
        payload["recent"] = services.audit.recent(limit=12)
    return payload


def _is_admin_session(services: Services, session: Session) -> bool:
    return is_admin(services.users.get_user(session.username))


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, session=Depends(require_page_session)) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "dashboard.html",
        _dashboard_payload(services, is_admin=_is_admin_session(services, session)),
        session=session,
    )


@router.get("/api/dashboard")
async def dashboard_api(
    request: Request, session: Session = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    return _dashboard_payload(services, is_admin=_is_admin_session(services, session))
