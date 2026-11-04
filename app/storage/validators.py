"""Validation helpers shared by the API and the services.

The API layer uses these to reject bad input early; the import service uses
them to check every question in an uploaded file before anything is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.timezones import is_valid_timezone

DIFFICULTIES = ("easy", "medium", "hard")
QUESTION_STATUSES = ("draft", "ready", "published")
SCHEDULE_TYPES = ("daily", "times", "weekdays", "interval", "once", "manual")
SELECTION_MODES = ("sequential", "random", "category")

MIN_OPTIONS = 2
MAX_OPTIONS = 10


class ValidationError(ValueError):
    """Raised when user supplied data is not acceptable."""


def _clean_text(value: Any, field: str, *, max_length: int, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValidationError(f"الحقل «{field}» مطلوب.")
    if len(text) > max_length:
        raise ValidationError(f"الحقل «{field}» أطول من {max_length} حرفاً.")
    return text


def validate_question_payload(
    payload: dict[str, Any], *, require_status: bool = True
) -> dict[str, Any]:
    question = _clean_text(payload.get("question"), "نص السؤال", max_length=300)
    raw_options = payload.get("options") or []
    if not isinstance(raw_options, list):
        raise ValidationError("الخيارات يجب أن تكون قائمة.")

    options: list[str] = []
    for index, option in enumerate(raw_options):
        text = _clean_text(option, f"الخيار {index + 1}", max_length=100)
        options.append(text)

    if len(options) < MIN_OPTIONS:
        raise ValidationError(f"يجب إدخال {MIN_OPTIONS} خيارات على الأقل.")
    if len(options) > MAX_OPTIONS:
        raise ValidationError(f"لا يمكن إدخال أكثر من {MAX_OPTIONS} خيارات.")
    if len(set(options)) != len(options):
        raise ValidationError("لا يمكن تكرار الخيارات.")

    correct = payload.get("correct_option_id")
    try:
        correct_int = int(correct)
    except (TypeError, ValueError) as exc:
        raise ValidationError("يجب تحديد الإجابة الصحيحة.") from exc
    if not 0 <= correct_int < len(options):
        raise ValidationError("رقم الإجابة الصحيحة خارج نطاق الخيارات.")

    difficulty = str(payload.get("difficulty") or "medium").strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValidationError("مستوى الصعوبة يجب أن يكون easy أو medium أو hard.")

    status = str(payload.get("status") or "draft").strip().lower()
    if require_status and status not in QUESTION_STATUSES:
        raise ValidationError("حالة السؤال غير معروفة.")

    return {
        "question": question,
        "options": options,
        "correct_option_id": correct_int,
        "explanation": _clean_text(
            payload.get("explanation"), "الشرح", max_length=500, required=False
        ),
        "category": _clean_text(
            payload.get("category"), "التصنيف", max_length=60, required=False
        )
        or "General",
        "difficulty": difficulty,
        "status": status,
    }


def validate_channel_payload(payload: dict[str, Any]) -> dict[str, Any]:
    name = _clean_text(payload.get("name"), "اسم القناة", max_length=100)
    chat_id = _clean_text(payload.get("chat_id"), "معرف القناة", max_length=120)
    if not (chat_id.startswith("@") or chat_id.lstrip("-").isdigit()):
        raise ValidationError(
            "معرف القناة يجب أن يبدأ بـ @ (قناة عامة) أو يكون رقمياً (معرف داخلي)."
        )
    transport = str(payload.get("transport") or "bot").strip().lower()
    if transport not in ("bot", "userbot"):
        raise ValidationError("نوع النشر يجب أن يكون bot أو userbot.")

    userbot_account_id = str(payload.get("userbot_account_id") or "").strip()
    raw_pool = payload.get("userbot_account_ids") or []
    if isinstance(raw_pool, str):
        raw_pool = [p for p in raw_pool.replace(",", " ").split() if p]
    if not isinstance(raw_pool, list):
        raise ValidationError("قائمة حسابات userbot غير صالحة.")
    userbot_account_ids = [str(p).strip() for p in raw_pool if str(p).strip()]

    return {
        "name": name,
        "chat_id": chat_id,
        "enabled": bool(payload.get("enabled", True)),
        "transport": transport,
        "userbot_account_id": userbot_account_id,
        "userbot_account_ids": userbot_account_ids,
        "description": _clean_text(
            payload.get("description"), "الوصف", max_length=200, required=False
        ),
    }


def _parse_time(value: Any) -> str:
    text = _clean_text(value, "الوقت", max_length=5)
    parts = text.split(":")
    if len(parts) != 2:
        raise ValidationError(f"صيغة الوقت غير صحيحة: {text} (المطلوب HH:MM).")
    hour, minute = parts
    if not (hour.isdigit() and minute.isdigit()):
        raise ValidationError(f"صيغة الوقت غير صحيحة: {text} (المطلوب HH:MM).")
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValidationError(f"الوقت خارج النطاق المسموح: {text}.")
    return f"{h:02d}:{m:02d}"


def validate_schedule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    channel_id = _clean_text(payload.get("channel_id"), "القناة", max_length=40)
    schedule_type = str(payload.get("type") or "daily").strip().lower()
    if schedule_type not in SCHEDULE_TYPES:
        raise ValidationError("نوع الجدولة غير مدعوم.")

    times: list[str] = []
    if schedule_type in ("daily", "times", "weekdays", "once"):
        raw_times = payload.get("times") or []
        if isinstance(raw_times, str):
            raw_times = [t for t in raw_times.replace(",", " ").split() if t]
        if not isinstance(raw_times, list) or not raw_times:
            raise ValidationError("يجب تحديد وقت نشر واحد على الأقل.")
        times = sorted({_parse_time(t) for t in raw_times})
        if schedule_type == "once" and len(times) != 1:
            raise ValidationError("الجدولة لمرة واحدة تقبل وقتاً واحداً فقط.")

    weekdays: list[int] = []
    if schedule_type == "weekdays":
        raw_days = payload.get("weekdays") or []
        for day in raw_days:
            try:
                day_int = int(day)
            except (TypeError, ValueError) as exc:
                raise ValidationError("أيام الأسبوع يجب أن تكون أرقاماً.") from exc
            if not 0 <= day_int <= 6:
                raise ValidationError("أيام الأسبوع من 0 (الاثنين) إلى 6 (الأحد).")
            weekdays.append(day_int)
        if not weekdays:
            raise ValidationError("يجب اختيار يوم واحد على الأقل.")

    interval_minutes = 0
    if schedule_type == "interval":
        try:
            interval_minutes = int(payload.get("interval_minutes") or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("الفاصل الزمني غير صالح.") from exc
        if not 5 <= interval_minutes <= 24 * 60:
            raise ValidationError("الفاصل الزمني يجب أن يكون بين 5 دقائق و 24 ساعة.")

    selection = str(payload.get("question_selection") or "sequential").strip().lower()
    if selection not in SELECTION_MODES:
        raise ValidationError("طريقة اختيار الأسئلة غير مدعومة.")

    try:
        per_day = int(payload.get("questions_per_day") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("عدد الأسئلة اليومية غير صالح.") from exc
    if per_day < 0:
        raise ValidationError("عدد الأسئلة اليومية لا يمكن أن يكون سالباً.")

    raw_per_run = payload.get("questions_per_run")
    if raw_per_run in (None, ""):
        raw_per_run = 1
    try:
        per_run = int(raw_per_run)
    except (TypeError, ValueError) as exc:
        raise ValidationError("عدد الأسئلة في كل مرة غير صالح.") from exc
    if not 1 <= per_run <= MAX_OPTIONS:
        raise ValidationError(f"عدد الأسئلة في كل مرة يجب أن يكون بين 1 و {MAX_OPTIONS}.")

    try:
        batch_interval = int(payload.get("batch_interval_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("الفارق الزمني بين الأسئلة غير صالح.") from exc
    if not 0 <= batch_interval <= 3600:
        raise ValidationError("الفارق الزمني يجب أن يكون بين 0 و 3600 ثانية.")

    category_filter = _clean_text(
        payload.get("category_filter"), "تصنيف الأسئلة", max_length=60, required=False
    )
    if selection == "category" and not category_filter:
        raise ValidationError("اختر تصنيفاً عند اختيار الأسئلة حسب التصنيف.")

    start_date = _clean_text(
        payload.get("start_date"), "تاريخ البداية", max_length=10, required=False
    )
    end_date = _clean_text(
        payload.get("end_date"), "تاريخ النهاية", max_length=10, required=False
    )
    for label, value in (("تاريخ البداية", start_date), ("تاريخ النهاية", end_date)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValidationError(f"{label} يجب أن يكون بصيغة YYYY-MM-DD.") from exc
    if start_date and end_date and start_date > end_date:
        raise ValidationError("تاريخ البداية يجب أن يسبق تاريخ النهاية.")

    timezone = _clean_text(
        payload.get("timezone"), "المنطقة الزمنية", max_length=64, required=False
    )
    if timezone and not is_valid_timezone(timezone):
        raise ValidationError(f"المنطقة الزمنية «{timezone}» غير معروفة.")

    return {
        "channel_id": channel_id,
        "type": schedule_type,
        "enabled": bool(payload.get("enabled", True)),
        "times": times,
        "weekdays": sorted(set(weekdays)),
        "interval_minutes": interval_minutes,
        "timezone": timezone,
        "question_selection": selection,
        "category_filter": category_filter,
        "questions_per_day": per_day,
        "questions_per_run": per_run,
        "batch_interval_seconds": batch_interval,
        "start_date": start_date,
        "end_date": end_date,
        "name": _clean_text(
            payload.get("name"), "اسم الجدول", max_length=100, required=False
        ),
    }


def normalize_imported_question(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate one question coming from an uploaded JSON file."""
    if not isinstance(raw, dict):
        raise ValidationError("كل سؤال في الملف يجب أن يكون كائن JSON.")

    # Accept both the internal shape and the documented export shape.
    options = raw.get("options")
    if isinstance(options, list) and options and isinstance(options[0], dict):
        correct = next(
            (i for i, opt in enumerate(options) if opt.get("is_correct")), None
        )
        raw = {
            **raw,
            "options": [str(opt.get("text", "")).strip() for opt in options],
            "correct_option_id": raw.get("correct_option_id", correct),
        }

    return validate_question_payload(raw, require_status=False)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
