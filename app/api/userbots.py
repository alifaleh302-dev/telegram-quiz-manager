"""Userbot accounts: CRUD plus the two-step MTProto login flow.

All endpoints are behind ``api_guard`` (session + CSRF). Raw api_id, api_hash
and session strings are never returned — only masked status.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import api_guard, get_services

router = APIRouter(tags=["userbots"])


class UserbotIn(BaseModel):
    name: str
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    role: str = "primary"
    enabled: bool = True


class LoginStartIn(BaseModel):
    phone: str = ""


class LoginCompleteIn(BaseModel):
    code: str = ""
    password: str = ""


@router.get("/api/userbots")
async def list_userbots(request: Request, _: Any = Depends(api_guard)) -> dict[str, Any]:
    services = get_services(request)
    return {
        "accounts": services.userbot_accounts.views(),
        "available": services.userbots.available(),
        "default_mode": services.telegram.default_mode,
        "pending": services.userbots.pending_accounts(),
    }


@router.post("/api/userbots", status_code=201)
async def create_userbot(
    request: Request, payload: UserbotIn, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    return services.userbot_accounts.create(payload.model_dump(), actor=session.username)


@router.put("/api/userbots/{account_id}")
async def update_userbot(
    request: Request,
    account_id: str,
    payload: UserbotIn,
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    account = services.userbot_accounts.update(
        account_id, payload.model_dump(), actor=session.username
    )
    services.userbots.invalidate(account_id)
    return account


@router.post("/api/userbots/{account_id}/toggle")
async def toggle_userbot(
    request: Request, account_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    account = services.userbot_accounts.get(account_id)
    if account is None:
        return {"ok": False, "error": "الحساب غير موجود."}
    updated = services.userbot_accounts.set_enabled(
        account_id, not account.get("enabled", True), actor=session.username
    )
    return {"ok": True, "account": updated}


@router.post("/api/userbots/{account_id}/default")
async def set_default_userbot(
    request: Request, account_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.userbot_accounts.set_default(account_id, actor=session.username)
    return {"ok": True}


@router.delete("/api/userbots/{account_id}")
async def delete_userbot(
    request: Request, account_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    services.userbot_accounts.delete(account_id, actor=session.username)
    services.userbots.invalidate(account_id)
    return {"ok": True}


@router.post("/api/userbots/{account_id}/login/start")
async def start_login(
    request: Request,
    account_id: str,
    payload: LoginStartIn,
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    result = await services.userbots.start_login(account_id, payload.phone)
    return {"ok": True, **result}


@router.post("/api/userbots/{account_id}/login/complete")
async def complete_login(
    request: Request,
    account_id: str,
    payload: LoginCompleteIn,
    session: Any = Depends(api_guard),
) -> dict[str, Any]:
    services = get_services(request)
    result = await services.userbots.complete_login(
        account_id,
        code=payload.code.strip(),
        password=payload.password,
        actor=session.username,
    )
    return {"ok": True, **result}


@router.post("/api/userbots/{account_id}/login/cancel")
async def cancel_login(
    request: Request, account_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    services = get_services(request)
    await services.userbots.cancel_login(account_id)
    return {"ok": True}


@router.post("/api/userbots/{account_id}/test")
async def test_userbot(
    request: Request, account_id: str, session: Any = Depends(api_guard)
) -> dict[str, Any]:
    """Verify the stored session still logs in, and report the account identity."""
    services = get_services(request)
    client = services.userbots.client_for(account_id)
    if client is None:
        return {"ok": False, "error": "لا توجد جلسة صالحة لهذا الحساب. سجّل الدخول أولاً."}
    try:
        me = await client.get_me()
    finally:
        await client.close()
        services.userbots.invalidate(account_id)
    if not me:
        return {"ok": False, "error": "فشل الاتصال بتليجرام؛ تحقق من المفاتيح والجلسة."}
    services.audit.record(
        "userbot.tested",
        actor=session.username,
        message=f"اختبار حساب userbot {account_id}: نجح",
    )
    return {"ok": True, "account": me}
