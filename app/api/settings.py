"""Settings, account, backups and export/restore endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import api_guard, get_services, render, require_page_session
from app.core.logging import get_logger
from app.storage.validators import ValidationError

logger = get_logger(__name__)

router = APIRouter(tags=["settings"])


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class ProfileIn(BaseModel):
    email: str = ""


class TelegramTokenIn(BaseModel):
    token: str = ""


async def _apply_new_token(services: Any) -> bool:
    """Make a freshly stored token take effect without a restart."""
    services.telegram.reset()
    try:
        return await services.bot.restart()
    except Exception:  # noqa: BLE001 - the dashboard must stay usable
        logger.exception("failed to restart the bot listener after a token change")
        return False


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
            "token": services.secrets.status(),
            "backups": services.backups.list_backups()[:15],
            "logins": services.app_settings.login_history(limit=15),
            "userbots": services.userbot_accounts.views(),
            "userbots_available": services.userbots.available(),
            "userbot_pending": services.userbots.pending_accounts(),
            "default_transport": services.telegram.default_mode,
            "env": {
                "telegram_mode": services.settings.telegram_mode,
                "dry_run": services.dry_run,
                "data_dir": str(services.settings.data_dir),
                "secret_key_set": bool(services.settings.secret_key),
            },
            "user": services.app_settings.get_user(session.username),
        },
        session=session,
    )


@router.get("/api/settings/telegram-token")
async def get_telegram_token(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    return get_services(request).secrets.status()


@router.post("/api/settings/telegram-token")
async def set_telegram_token(
    request: Request, payload: TelegramTokenIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    status = services.secrets.set_bot_token(payload.token, actor=session.username)
    restarted = await _apply_new_token(services)
    return {"ok": True, "restarted": restarted, **status}


@router.delete("/api/settings/telegram-token")
async def clear_telegram_token(
    request: Request, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    status = services.secrets.clear_bot_token(actor=session.username)
    await _apply_new_token(services)
    return {"ok": True, **status}


@router.post("/api/settings/telegram-token/test")
async def test_telegram_token(
    request: Request, payload: TelegramTokenIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    """Verify a token (the submitted one, or the stored one) against Telegram."""
    services = get_services(request)
    submitted = payload.token.strip()
    client = services.telegram
    if submitted:
        from app.telegram.telegram_client import TelegramClient

        client = TelegramClient(token=submitted)
    try:
        me = await client.get_me()
    finally:
        if submitted:
            await client.close()
    if not me or me.get("dry_run"):
        return {
            "ok": False,
            "error": "لا يوجد توكن للاختبار. أدخل التوكن أولاً.",
        }
    return {
        "ok": True,
        "username": me.get("username"),
        "id": me.get("id"),
    }


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
