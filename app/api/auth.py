"""Login / logout pages and the HTML shell routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import (
    get_services,
    get_templates,
    require_page_session,
)
from app.core.security import SESSION_COOKIE, create_session_token
from app.core.config import get_settings

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    services = get_services(request)
    session_cookie = request.cookies.get(SESSION_COOKIE)
    if session_cookie:
        from app.core.security import AuthenticationError, load_session

        try:
            load_session(session_cookie)
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        except AuthenticationError:
            pass
    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "app_name": request.app.state.app_name,
            "error": None,
            "dry_run": services.dry_run,
            "bootstrap_ready": bool(get_settings().admin_password)
            or bool(services.storage.users.read()["users"]),
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    services = get_services(request)
    templates = get_templates(request)
    ok = services.app_settings.authenticate(username, password)
    services.app_settings.record_login(
        username, success=ok, ip=_client_ip(request),
        detail="" if ok else "بيانات دخول غير صحيحة",
    )

    if not ok:
        services.audit.record(
            "auth.login_failed",
            level="warning",
            actor=username,
            message=f"محاولة دخول فاشلة للمستخدم {username}",
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "app_name": request.app.state.app_name,
                "error": "اسم المستخدم أو كلمة المرور غير صحيحة.",
                "dry_run": services.dry_run,
                "bootstrap_ready": True,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_session_token(username)
    services.audit.record(
        "auth.login", actor=username, message=f"تسجيل دخول {username}"
    )
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=get_settings().session_max_age,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request, session=Depends(require_page_session)) -> Response:
    services = get_services(request)
    if not isinstance(session, RedirectResponse):
        services.audit.record("auth.logout", actor=session.username, message="تسجيل خروج")
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/logout")
async def logout_get(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
