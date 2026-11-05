"""Shared FastAPI dependencies: auth guard, CSRF, services and templates."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.permissions import is_admin, permissions_for
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


def require_permission(permission: str):
    """Dependency factory: auth + CSRF + a specific grant.

    ``api_guard`` already enforces auth and CSRF, so this composes on top of it
    and then checks the caller's effective grants. Admins hold every grant.
    """

    async def _guard(
        request: Request, session: Session = Depends(api_guard)
    ) -> Session:
        services = get_services(request)
        if permission not in permissions_for(services.users.get_user(session.username)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="لا تملك صلاحية تنفيذ هذا الإجراء.",
            )
        return session

    return _guard


def require_page_permission(permission: str):
    """HTML-page variant of :func:`require_permission`.

    Non-authorised users are redirected to the dashboard rather than shown a
    raw 403, matching the behaviour of ``require_admin_page``.
    """

    def _dep(
        request: Request,
        session: Session | RedirectResponse = Depends(require_page_session),
    ) -> Session | RedirectResponse:
        if isinstance(session, RedirectResponse):
            return session
        services = get_services(request)
        if permission not in permissions_for(services.users.get_user(session.username)):
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return session

    return _dep


async def require_admin(
    request: Request, session: Session = Depends(api_guard)
) -> Session:
    """Dependency: auth + CSRF + admin role only."""
    services = get_services(request)
    if not is_admin(services.users.get_user(session.username)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="هذا الإجراء متاح للمدير فقط.",
        )
    return session


def require_admin_page(
    request: Request, session: Session | RedirectResponse = Depends(require_page_session)
) -> Session | RedirectResponse:
    """Admin-only HTML page: redirects non-admins to the dashboard."""
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    if not is_admin(services.users.get_user(session.username)):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return session


_PAGE_MAP = {
    "/": "dashboard",
    "/questions": "questions",
    "/channels": "channels",
    "/schedules": "schedules",
    "/history": "history",
    "/analytics": "analytics",
    "/users": "users",
    "/settings": "settings",
    "/exams": "exams",
    "/students": "students",
    "/attempts": "attempts",
    "/exam-analytics": "exam-analytics",
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
    services = get_services(request)
    user = services.users.get_user(session.username) if session else None
    payload: dict[str, Any] = {
        "request": request,
        "app_name": request.app.state.app_name,
        "session": session,
        "csrf_token": session.csrf_token if session else "",
        "dry_run": services.dry_run,
        "active_page": active_page_for(request.url.path),
        "current_user": services.users.view(user) if user else None,
        "permissions": permissions_for(user),
        "is_admin": is_admin(user),
    }
    payload.update(context or {})
    return templates.TemplateResponse(
        request, template_name, payload, status_code=status_code
    )


def static_dir(request: Request) -> Path:
    return request.app.state.static_dir


def iter_services(request: Request) -> Iterator[Services]:
    yield get_services(request)
