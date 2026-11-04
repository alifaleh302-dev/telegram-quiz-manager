"""Settings, account, backups and export/restore endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import api_guard, get_services, render, require_page_session
from app.storage.validators import ValidationError

router = APIRouter(tags=["settings"])


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class ProfileIn(BaseModel):
    email: str = ""


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session=Depends(require_page_session)) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    return render(
        request,
        "settings.html",
        {
            "config": services.app_settings.get(),
            "bot": await services.telegram.get_me(),
            "backups": services.backups.list_backups()[:15],
            "logins": services.app_settings.login_history(limit=15),
            "env": {
                "telegram_mode": services.settings.telegram_mode,
                "dry_run": services.settings.dry_run,
                "data_dir": str(services.settings.data_dir),
            },
            "user": services.app_settings.get_user(session.username),
        },
        session=session,
    )


@router.get("/api/settings")
async def get_settings_api(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    return get_services(request).app_settings.get()


@router.put("/api/settings")
async def update_settings(
    request: Request, payload: dict[str, Any], session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    document = services.app_settings.update(payload, actor=session.username)
    services.engine.reload()
    return document


@router.post("/api/settings/password")
async def change_password(
    request: Request, payload: PasswordIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.change_password(
        session.username,
        payload.current_password,
        payload.new_password,
        actor=session.username,
    )
    return {"ok": True}


@router.post("/api/settings/profile")
async def update_profile(
    request: Request, payload: ProfileIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.app_settings.update_profile(
        session.username, payload.model_dump(), actor=session.username
    )
    return {"ok": True}


@router.get("/api/backups")
async def list_backups(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    return {"backups": get_services(request).backups.list_backups()}


@router.post("/api/backups/create")
async def create_backup(request: Request, session: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    name = services.backups.create_snapshot(label="manual", actor=session.username)
    return {"ok": True, "name": name}


@router.post("/api/backups/restore/{name}")
async def restore_backup(
    request: Request, name: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    result = services.backups.restore_snapshot(name, actor=session.username)
    services.engine.reload()
    return {"ok": True, **result}


@router.get("/api/export")
async def export_all(request: Request, _: Any = Depends(api_guard)) -> Response:
    services = get_services(request)
    payload = services.backups.export_archive()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tqm-backup.zip"'},
    )


@router.post("/api/import/restore")
async def restore_from_upload(
    request: Request,
    file: UploadFile = File(...),
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    payload = await file.read()
    if not payload:
        raise ValidationError("لم يتم إرفاق أي ملف.")
    result = services.backups.restore_from_archive(payload, actor=session.username)
    services.engine.reload()
    return {"ok": True, **result}
