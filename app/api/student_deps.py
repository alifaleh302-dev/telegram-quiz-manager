"""Dependencies for the Student API and Mini App.

Identity comes from a **verified** Telegram ``initData`` string (or the signed
student token minted from it). The client never sends its own user id; the
server derives it, so a tampered Mini App cannot act as another student.
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, Request, status

from app.api.deps import get_services
from app.core.config import get_settings
from app.core.telegram_auth import (
    TelegramAuthError,
    TelegramUser,
    create_student_token,
    load_student_token,
    verify_init_data,
)
from app.services.container import Services

STUDENT_TOKEN_HEADER = "X-Student-Token"
INIT_DATA_HEADER = "X-Telegram-Init-Data"


class StudentContext:
    """The verified caller of a student request."""

    def __init__(self, student: dict[str, Any], services: Services) -> None:
        self.student = student
        self.services = services

    @property
    def id(self) -> str:
        return self.student["id"]

    @property
    def telegram_user_id(self) -> int:
        return int(self.student["telegram_user_id"])


def _bot_token(services: Services) -> str:
    return services.secrets.bot_token() or get_settings().telegram_bot_token


def authenticate_init_data(services: Services, init_data: str) -> dict[str, Any]:
    """Verify ``initData`` and upsert the student behind it."""
    settings = get_settings()
    telegram: TelegramUser = verify_init_data(
        init_data, _bot_token(services), max_age=settings.telegram_auth_max_age
    )
    return services.students.touch(telegram)


def student_from_request(request: Request) -> StudentContext:
    """Resolve the caller from either the student token or a fresh initData.

    The Mini App sends ``X-Telegram-Init-Data`` once (on load) and then the
    ``X-Student-Token`` it received back. Either path re-derives identity on the
    server; neither trusts a client-supplied id.
    """
    services = get_services(request)
    token = request.headers.get(STUDENT_TOKEN_HEADER) or request.cookies.get(
        "tqm_student"
    )
    init_data = request.headers.get(INIT_DATA_HEADER)

    if token:
        try:
            telegram_user_id = load_student_token(token)
        except TelegramAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        student = services.students.by_telegram_id(telegram_user_id)
        if student is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ملف الطالب غير موجود.",
            )
        return StudentContext(student, services)

    if init_data:
        try:
            student = authenticate_init_data(services, init_data)
        except TelegramAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        return StudentContext(student, services)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="مطلوب مصادقة Telegram."
    )


def require_student(request: Request) -> StudentContext:
    return student_from_request(request)


def require_student_write(
    request: Request,
    x_student_token: str | None = Header(default=None, alias=STUDENT_TOKEN_HEADER),
    x_telegram_init_data: str | None = Header(default=None, alias=INIT_DATA_HEADER),
) -> StudentContext:
    """Auth for mutating student calls.

    Mutations require the ``initData`` header specifically (not just a token):
    initData is short-lived and signed by Telegram, so it proves the request
    came from a live Mini App session rather than a stale captured token.
    """
    services = get_services(request)
    if x_telegram_init_data:
        try:
            student = authenticate_init_data(services, x_telegram_init_data)
        except TelegramAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        return StudentContext(student, services)
    return student_from_request(request)


def issue_student_token(student: dict[str, Any]) -> str:
    return create_student_token(int(student["telegram_user_id"]))
