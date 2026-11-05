"""Editable bot message templates with safe placeholder substitution.

Admins can customize the wording the bot sends (welcome text, "new exam"
notification, and so on). Stored strings are untrusted input, so rendering is a
plain ``str.replace`` over a fixed placeholder map — never ``str.format`` /
``format_map``, which would let a stray ``{`` in a template raise or read
attributes. A template left blank falls back to the shipped Arabic default, so
an empty field can never send an empty message.
"""

from __future__ import annotations

from typing import Any

# Every template key the admin may edit, with its shipped default.
DEFAULTS: dict[str, str] = {
    "welcome": (
        "🤖 *Telegram Quiz Manager*\n\n"
        "هذا البوت ينشر اختبارات الاختيار من متعدد في القنوات المحددة.\n"
        "ويمكنك الآن خوض الاختبارات التفاعلية من داخل تيليجرام.\n\n"
        "الأوامر المتاحة:\n"
        "/exams — فتح بوابة الاختبارات\n"
        "/status — حالة النظام\n"
        "/help — هذه الرسالة"
    ),
    "exam_published_title": "اختبار جديد متاح",
    "exam_published_body": (
        "اختبار جديد: {exam_title}\n"
        "عدد الأسئلة: {question_count}"
    ),
    "exam_upcoming_title": "اختبار قادم",
    "exam_upcoming_body": (
        "سيبدأ اختبار: {exam_title}\n"
        "الموعد: {starts_at}"
    ),
    "attempt_completed_title": "تم تسليم محاولتك",
    "attempt_completed_body": "{exam_title}\nالنتيجة: {percentage}%",
}

TEMPLATE_KEYS = tuple(DEFAULTS)


def _safe(value: Any) -> str:
    return "" if value is None else str(value)


def template_text(messages: dict[str, Any] | None, key: str) -> str:
    """Return the stored template for ``key``, or its default when unset/blank."""
    if isinstance(messages, dict):
        candidate = _safe(messages.get(key)).strip()
        if candidate:
            return candidate
    return DEFAULTS.get(key, "")


def render(
    messages: dict[str, Any] | None, key: str, context: dict[str, Any] | None = None
) -> str:
    """Render ``key`` with ``{placeholder}`` substitution from ``context``.

    Unknown placeholders are left as-is; missing values become empty strings.
    """
    text = template_text(messages, key)
    for name, value in (context or {}).items():
        text = text.replace("{" + name + "}", _safe(value))
    return text
