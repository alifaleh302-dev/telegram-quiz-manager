"""User management (admin only): list, create, update grants, delete.

Accounts and their grants live in the database. Only an admin may reach these
endpoints; every mutation is CSRF-checked via ``require_admin``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import (
    get_services,
    render,
    require_admin,
    require_admin_page,
)
from app.core.permissions import GRANTS, ROLES

router = APIRouter(tags=["users"])


class UserCreateIn(BaseModel):
    username: str
    password: str
    email: str = ""
    role: str = "user"
    permissions: list[str] = []


class UserUpdateIn(BaseModel):
    email: str | None = None
    role: str | None = None
    permissions: list[str] | None = None


class PasswordResetIn(BaseModel):
    new_password: str


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request, session=Depends(require_admin_page)
) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "users.html",
        {
            "users": services.users.list_views(),
            "grants": GRANTS,
            "roles": ROLES,
            "logins": services.users.login_history(limit=20),
        },
        session=session,
    )


@router.get("/api/users")
async def list_users(request: Request, _: Any = Depends(require_admin)) -> dict[str, Any]:
    services = get_services(request)
    return {
        "users": services.users.list_views(),
        "grants": GRANTS,
    }


@router.post("/api/users")
async def create_user(
    request: Request, payload: UserCreateIn, session: Any = Depends(require_admin)
) -> dict[str, Any]:
    services = get_services(request)
    created = services.users.create_user(payload.model_dump(), actor=session.username)
    return {"ok": True, "user": created}


@router.put("/api/users/{username}")
async def update_user(
    request: Request,
    username: str,
    payload: UserUpdateIn,
    session: Any = Depends(require_admin),
) -> dict[str, Any]:
    services = get_services(request)
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = services.users.update_user(username, data, actor=session.username)
    return {"ok": True, "user": updated}


@router.post("/api/users/{username}/password")
async def reset_user_password(
    request: Request,
    username: str,
    payload: PasswordResetIn,
    session: Any = Depends(require_admin),
) -> dict[str, Any]:
    services = get_services(request)
    services.users.set_password(username, payload.new_password, actor=session.username)
    return {"ok": True}


@router.delete("/api/users/{username}")
async def delete_user(
    request: Request, username: str, session: Any = Depends(require_admin)
) -> dict[str, Any]:
    services = get_services(request)
    services.users.delete_user(username, actor=session.username)
    return {"ok": True}
