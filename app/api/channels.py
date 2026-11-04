"""Channels: HTML page, CRUD, default selection and publish test."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import api_guard, get_services, render, require_page_session

router = APIRouter(tags=["channels"])


class ChannelIn(BaseModel):
    name: str
    chat_id: str
    enabled: bool = True
    description: str = ""
    # Which transport publishes to this channel: "bot" (Bot API) or "userbot".
    transport: str = "bot"
    # Optional pin to one userbot account, plus a fallback rotation pool.
    userbot_account_id: str = ""
    userbot_account_ids: list[str] = []


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request, session=Depends(require_page_session)) -> Response:
    if isinstance(session, RedirectResponse):
        return session
    services = get_services(request)
    channels = services.channels.list_channels()
    return render(
        request,
        "channels.html",
        {
            "channels": channels,
            "schedules": services.schedules.list_schedules(),
            "bot": await services.telegram.get_me(),
            "userbot_accounts": services.userbot_accounts.list_accounts(),
            "userbots_available": services.userbots.available(),
        },
        session=session,
    )


@router.get("/api/channels")
async def list_channels(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    return {"channels": services.channels.list_channels()}


@router.post("/api/channels", status_code=201)
async def create_channel(
    request: Request, payload: ChannelIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    return services.channels.create(payload.model_dump(), actor=session.username)


@router.put("/api/channels/{channel_id}")
async def update_channel(
    request: Request,
    channel_id: str,
    payload: ChannelIn,
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    return services.channels.update(
        channel_id, payload.model_dump(), actor=session.username
    )


@router.post("/api/channels/{channel_id}/toggle")
async def toggle_channel(
    request: Request, channel_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    channel = services.channels.get(channel_id)
    if channel is None:
        return {"ok": False, "error": "القناة غير موجودة."}
    updated = services.channels.set_enabled(
        channel_id, not channel.get("enabled", True), actor=session.username
    )
    return {"ok": True, "channel": updated}


@router.post("/api/channels/{channel_id}/default")
async def set_default_channel(
    request: Request, channel_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.channels.set_default(channel_id, actor=session.username)
    return {"ok": True}


@router.delete("/api/channels/{channel_id}")
async def delete_channel(
    request: Request, channel_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.channels.delete(channel_id, actor=session.username)
    return {"ok": True}


@router.post("/api/channels/{channel_id}/test")
async def test_channel(
    request: Request, channel_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    channel = services.channels.get(channel_id)
    if channel is None:
        return {"ok": False, "error": "القناة غير موجودة."}
    result = await services.telegram.test_channel(channel["chat_id"], channel_id=channel_id)
    services.audit.record(
        "channel.tested",
        actor=session.username,
        level="info" if result.ok else "error",
        message=(
            f"اختبار القناة {channel['name']}: "
            + ("نجح" if result.ok else f"فشل ({result.error_code})")
        ),
    )
    return {
        "ok": result.ok,
        "dry_run": result.dry_run,
        "error": result.error,
        "error_code": result.error_code,
        "transport": services.telegram.mode_for_channel(channel),
    }
