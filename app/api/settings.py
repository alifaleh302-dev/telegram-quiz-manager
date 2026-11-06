"""Settings, account, backups and export/restore endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.api.deps import (
    get_services,
    render,
    require_page_session,
    api_guard,
    require_admin,
)
from app.core.logging import get_logger
from app.core.messages import DEFAULTS as MESSAGE_DEFAULTS
from app.storage.validators import ValidationError

logger = get_logger(__name__)

router = APIRouter(tags=["settings"])

# Labels for the editable message templates, in the order shown on the page.
MESSAGE_FIELDS: list[dict[str, Any]] = [
    {
        "key": "welcome",
        "label": "رسالة الترحيب (/start)",
        "hint": "تُرسل عند فتح محادثة البوت أو كتابة /start.",
        "rows": 8,
    },
    {
        "key": "exam_published_title",
        "label": "عنوان إشعار الاختبار الجديد",
        "hint": "سطر العنوان في رسالة «اختبار جديد».",
        "rows": 1,
    },
    {
        "key": "exam_published_body",
        "label": "نص إشعار الاختبار الجديد",
        "hint": "يدعم {exam_title} و {question_count} و {starts_at}.",
        "rows": 3,
    },
    {
        "key": "exam_upcoming_title",
        "label": "عنوان إشعار الاختبار القادم",
        "hint": "يُستخدم للاختبارات المجدولة قبل بدئها.",
        "rows": 1,
    },
    {
        "key": "exam_upcoming_body",
        "label": "نص إشعار الاختبار القادم",
        "hint": "يدعم {exam_title} و {starts_at}.",
        "rows": 3,
    },
    {
        "key": "attempt_completed_title",
        "label": "عنوان إشعار تسليم المحاولة",
        "hint": "سطر العنوان عند تسليم الطالب لمحاولته.",
        "rows": 1,
    },
    {
        "key": "attempt_completed_body",
        "label": "نص إشعار تسليم المحاولة",
        "hint": "يدعم {exam_title} و {percentage}.",
        "rows": 3,
    },
]

# Arabic labels for the notification-type switches.
NOTIFY_TYPE_FIELDS: list[dict[str, str]] = [
    {"key": "exam_published", "label": "نشر اختبار جديد"},
    {"key": "exam_upcoming", "label": "اختبار قادم"},
    {"key": "exam_closing_soon", "label": "قرب إغلاق الاختبار"},
    {"key": "attempt_completed", "label": "تسليم المحاولة"},
    {"key": "result_available", "label": "توفّر النتيجة"},
]


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
            "messages": services.app_settings.messages(),
            "message_fields": [
                {**field, "default": MESSAGE_DEFAULTS.get(field["key"], "")}
                for field in MESSAGE_FIELDS
            ],
            "notify": services.app_settings.notifications_config(),
            "notify_type_fields": NOTIFY_TYPE_FIELDS,
            "bot": await services.telegram.get_me(),
            "token": services.secrets.status(),
            "backups": services.backups.list_backups()[:15],
            "logins": services.users.login_history(limit=15),
            "userbots": services.userbot_accounts.views(),
            "userbots_available": services.userbots.available(),
            "userbot_pending": services.userbots.pending_accounts(),
            "default_transport": services.telegram.default_mode,
            "env": {
                "telegram_mode": services.settings.telegram_mode,
                "bot_listener_enabled": services.settings.telegram_bot_enabled,
                "dry_run": services.dry_run,
                "data_dir": str(services.settings.data_dir),
                "secret_key_set": bool(services.settings.secret_key),
            },
            "user": services.users.get_user(session.username),
        },
        session=session,
    )


@router.get("/api/settings/telegram-token")
async def get_telegram_token(request: Request, _: Any = Depends(require_admin)) -> dict[str, Any]:
    return get_services(request).secrets.status()


@router.post("/api/settings/telegram-token")
async def set_telegram_token(
    request: Request, payload: TelegramTokenIn, session: Any = Depends(require_admin)
) -> dict[str, Any]:
    services = get_services(request)
    status = services.secrets.set_bot_token(payload.token, actor=session.username)
    restarted = await _apply_new_token(services)
    return {"ok": True, "restarted": restarted, **status}


@router.delete("/api/settings/telegram-token")
async def clear_telegram_token(
    request: Request, session: Any = Depends(require_admin)
) -> dict[str, Any]:
    services = get_services(request)
    status = services.secrets.clear_bot_token(actor=session.username)
    await _apply_new_token(services)
    return {"ok": True, **status}


@router.post("/api/settings/telegram-token/test")
async def test_telegram_token(
    request: Request, payload: TelegramTokenIn, session: Any = Depends(require_admin)
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
async def get_settings_api(request: Request, _: Any = Depends(require_admin)) -> dict[str, Any]:
    return get_services(request).app_settings.get()


@router.put("/api/settings")
async def update_settings(
    request: Request, payload: dict[str, Any], session: Any = Depends(require_admin)
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
    services.users.change_password(
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
    services.users.update_profile(
        session.username, payload.model_dump(), actor=session.username
    )
    return {"ok": True}


@router.get("/api/backups")
async def list_backups(request: Request, _: Any = Depends(require_admin)) -> dict[str, Any]:
    return {"backups": get_services(request).backups.list_backups()}


@router.post("/api/backups/create")
async def create_backup(request: Request, session: Any = Depends(require_admin)) -> dict[str, Any]:
    services = get_services(request)
    name = services.backups.create_snapshot(label="manual", actor=session.username)
    return {"ok": True, "name": name}


@router.post("/api/backups/restore/{name}")
async def restore_backup(
    request: Request, name: str, session: Any = Depends(require_admin)
) -> dict[str, Any]:
    services = get_services(request)
    result = services.backups.restore_snapshot(name, actor=session.username)
    services.engine.reload()
    return {"ok": True, **result}


@router.get("/api/export")
async def export_all(request: Request, _: Any = Depends(require_admin)) -> Response:
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
    session: Any = Depends(require_admin),
) -> dict[str, Any]:
    services = get_services(request)
    payload = await file.read()
    if not payload:
        raise ValidationError("لم يتم إرفاق أي ملف.")
    result = services.backups.restore_from_archive(payload, actor=session.username)
    services.engine.reload()
    return {"ok": True, **result}
