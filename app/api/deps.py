"""Shared FastAPI dependencies: auth guard, CSRF, services and templates."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthenticationError,
    Session,
    load_session,
    verify_csrf,
)
from app.services.container import Services

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_templates: Jinja2Templates | None = None


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_templates(request: Request) -> Jinja2Templates:
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(request.app.state.templates_dir))
        _templates.env.globals["app_name"] = request.app.state.app_name
    return _templates


def _session_from_request(request: Request) -> Session | None:
    try:
        return load_session(request.cookies.get(SESSION_COOKIE))
    except AuthenticationError:
        return None


def current_session(request: Request) -> Session:
    session = _session_from_request(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="يجب تسجيل الدخول."
        )
    return session


def require_session(request: Request) -> Session:
    """Dependency for JSON APIs: rejects with 401 when not logged in."""
    return current_session(request)


def require_page_session(request: Request) -> Session | RedirectResponse:
    """Dependency for HTML pages: redirects to the login page instead of 401."""
    session = _session_from_request(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return session


async def api_guard(request: Request, session: Session = Depends(require_session)) -> Session:
    """Auth + CSRF guard for every mutating API call."""
    if request.method.upper() in _SAFE_METHODS:
        return session

    submitted = request.headers.get(CSRF_HEADER) or request.headers.get("X-CSRFToken")
    if not submitted and "form" in request.headers.get("content-type", ""):
        form = await request.form()
        submitted = form.get("csrf_token")
    if not verify_csrf(session, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="رمز CSRF غير صالح."
        )
    return session


async def page_guard(
    request: Request, session: Session = Depends(require_page_session)
) -> Session | RedirectResponse:
    """Auth + CSRF guard for classic HTML form submissions."""
    if isinstance(session, RedirectResponse):
        return session
    if request.method.upper() in _SAFE_METHODS:
        return session

    submitted = request.headers.get(CSRF_HEADER)
    if not submitted and "form" in request.headers.get("content-type", ""):
        form = await request.form()
        submitted = form.get("csrf_token")
    if not verify_csrf(session, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="رمز CSRF غير صالح."
        )
    return session


_PAGE_MAP = {
    "/": "dashboard",
    "/questions": "questions",
    "/channels": "channels",
    "/schedules": "schedules",
    "/history": "history",
    "/analytics": "analytics",
    "/settings": "settings",
}


def active_page_for(path: str) -> str:
    if path in _PAGE_MAP:
        return _PAGE_MAP[path]
    for prefix, name in _PAGE_MAP.items():
        if prefix != "/" and path.startswith(prefix):
            return name
    return ""


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    session: Session | None = None,
    status_code: int = 200,
) -> Any:
    templates = get_templates(request)
    payload: dict[str, Any] = {
        "request": request,
        "app_name": request.app.state.app_name,
        "session": session,
        "csrf_token": session.csrf_token if session else "",
        "dry_run": request.app.state.services.dry_run,
        "active_page": active_page_for(request.url.path),
    }
    payload.update(context or {})
    return templates.TemplateResponse(
        request, template_name, payload, status_code=status_code
    )


def static_dir(request: Request) -> Path:
    return request.app.state.static_dir


def iter_services(request: Request) -> Iterator[Services]:
    yield get_services(request)
