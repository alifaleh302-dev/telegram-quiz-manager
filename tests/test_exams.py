"""Exam platform: services (exams, attempts, grading, notifications) and the
admin/student APIs, driven through the real app with a fake Telegram transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.telegram_auth import build_init_data  # noqa: E402
from app.storage.validators import ValidationError  # noqa: E402

BOT_TOKEN = "123456:TEST-bot-token-for-exams"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "TestPassword123!"
STUDENT_USER = {
    "id": 700700,
    "first_name": "سارة",
    "last_name": "العلي",
    "username": "sara",
    "language_code": "ar",
}


# --- service-level tests ---------------------------------------------------

@pytest.fixture
def svc(services):
    return services


def _ready(svc, text="سؤال؟", options=None, correct=0):
    return svc.questions.create(
        {
            "question": text,
            "options": options or ["أ", "ب", "ج", "د"],
            "correct_option_id": correct,
            "status": "ready",
        }
    )


class _TG:
    def __init__(self, user_id, first="A", last="", username=""):
        self.user_id = user_id
        self.first_name = first
        self.last_name = last
        self.username = username
        self.language_code = "ar"
        self.photo_url = ""


def test_exam_snapshot_survives_bank_edit(svc) -> None:
    q = _ready(svc, "2+2؟", ["3", "4"], 1)
    exam = svc.exams.create({"title": "حساب", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])

    student = svc.students.touch(_TG(1))
    attempt = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.save_answer(attempt["id"], student["id"], q["id"], 1)
    done = svc.attempts.submit(attempt["id"], student["id"])
    assert done["percentage"] == 100.0

    # Rewrite the bank question entirely; the stored attempt must not move.
    svc.questions.update(
        q["id"], {"question": "مختلف", "options": ["x", "y"], "correct_option_id": 0, "status": "ready"}
    )
    review = svc.attempts.review(attempt["id"], student["id"])
    assert review["questions"][0]["question"] == "2+2؟"
    assert review["questions"][0]["correct_option_id"] == 1
    assert svc.attempts.get(attempt["id"])["percentage"] == 100.0


def test_attempt_limit_enforced(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "محدود", "access_mode": "always", "max_attempts": 1}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(2))

    first = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.submit(first["id"], student["id"])
    with pytest.raises(ValidationError):
        svc.attempts.start(exam["id"], student["id"])


def test_resume_in_progress_attempt(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "استئناف", "access_mode": "always", "max_attempts": 1}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(3))

    first = svc.attempts.start(exam["id"], student["id"])
    again = svc.attempts.start(exam["id"], student["id"])
    assert first["id"] == again["id"]


def test_unlimited_attempts_when_zero(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "غير محدود", "access_mode": "always", "max_attempts": 0}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(4))
    for _ in range(3):
        attempt = svc.attempts.start(exam["id"], student["id"])
        svc.attempts.submit(attempt["id"], student["id"])
    assert svc.attempts.remaining_attempts(exam, student["id"]) is None


def test_scheduled_exam_not_startable_before_window(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {
            "title": "مجدول",
            "access_mode": "scheduled",
            "start_date": "2999-01-01",
            "start_time": "10:00",
            "timezone": "Asia/Riyadh",
        },
        [q["id"]],
    )
    svc.exams.publish(exam["id"])
    exam = svc.exams.get(exam["id"])
    assert exam["status"] == "scheduled"
    assert svc.exams.effective_state(exam) == "upcoming"
    allowed, reason = svc.exams.can_start(exam)
    assert allowed is False and reason


def test_ownership_blocks_other_student(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create({"title": "ملكية", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    owner = svc.students.touch(_TG(5))
    other = svc.students.touch(_TG(6))
    attempt = svc.attempts.start(exam["id"], owner["id"])
    with pytest.raises(ValidationError):
        svc.attempts.get_owned(attempt["id"], other["id"])


def test_question_version_appended_after_attempts(svc) -> None:
    q1 = _ready(svc, "س1")
    q2 = _ready(svc, "س2")
    exam = svc.exams.create({"title": "نسخ", "access_mode": "always"}, [q1["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(7))
    attempt = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.submit(attempt["id"], student["id"])

    # Adding a question after attempts exist appends a new version.
    result = svc.exams.set_questions(exam["id"], [q1["id"], q2["id"]])
    assert result["version"] == 2
    # The finished attempt still resolves against version 1.
    assert len(svc.exams.questions_for(exam["id"], 1)) == 1


def test_notifications_dedupe(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create({"title": "إشعار", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(8))
    first = svc.notifications.enqueue(
        student_id=student["id"], exam_id=exam["id"], type="exam_published", title="t", body="b"
    )
    second = svc.notifications.enqueue(
        student_id=student["id"], exam_id=exam["id"], type="exam_published", title="t", body="b"
    )
    assert first is not None and second is None
    assert svc.notifications.unread_count(student["id"]) == 1


def test_remaining_attempts_ignores_abandoned_when_configured(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {
            "title": "تجاهل المهجورة",
            "access_mode": "always",
            "max_attempts": 1,
            "count_abandoned_attempts": False,
        },
        [q["id"]],
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(9))
    attempt = svc.attempts.start(exam["id"], student["id"])
    # Abandon (admin reset leaves no attempt; simulate an abandoned status).
    def _abandon(doc):
        for a in doc["attempts"]:
            if a["id"] == attempt["id"]:
                a["status"] = "abandoned"
    svc.storage.attempts.update(_abandon)
    # An abandoned attempt does not count, so a fresh start is allowed.
    restarted = svc.attempts.start(exam["id"], student["id"])
    assert restarted["id"] != attempt["id"]


# --- API-level tests -------------------------------------------------------

@pytest.fixture
def exam_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-exam-api")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.delenv("APP_DEBUG", raising=False)
    get_settings.cache_clear()

    from app.main import create_app

    application = create_app()
    with TestClient(application) as client:
        from tests.conftest import RecordingSleeper

        application.state.services.publishing._sleep = RecordingSleeper()
        yield client
    get_settings.cache_clear()


def _login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session = client.cookies.get("tqm_session")
    from app.core.security import load_session

    return load_session(session).csrf_token


def _student_headers() -> dict[str, str]:
    init_data = build_init_data(STUDENT_USER, BOT_TOKEN)
    return {"X-Telegram-Init-Data": init_data}


def _make_exam(client: TestClient, csrf: str, questions: int = 2) -> str:
    qids = []
    for index in range(questions):
        created = client.post(
            "/api/questions",
            json={
                "question": f"سؤال {index + 1}",
                "options": ["أ", "ب", "ج", "د"],
                "correct_option_id": index % 4,
                "status": "ready",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201, created.text
        qids.append(created.json()["id"])
    response = client.post(
        "/api/admin/exams",
        json={
            "title": "اختبار API",
            "access_mode": "always",
            "max_attempts": 2,
            "question_ids": qids,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    exam_id = response.json()["id"]
    published = client.post(
        f"/api/admin/exams/{exam_id}/publish", headers={"X-CSRF-Token": csrf}
    )
    assert published.status_code == 200, published.text
    return exam_id


def test_student_auth_requires_valid_signature(exam_client) -> None:
    bad = exam_client.post("/api/student/auth", json={"init_data": "auth_date=1&user=%7B%7D&hash=deadbeef"})
    assert bad.status_code == 401
    good = exam_client.post(
        "/api/student/auth", json={"init_data": build_init_data(STUDENT_USER, BOT_TOKEN)}
    )
    assert good.status_code == 200
    body = good.json()
    assert body["token"] and body["profile"]["name"] == "سارة العلي"


def test_student_requires_auth(exam_client) -> None:
    assert exam_client.get("/api/student/dashboard").status_code == 401


def test_full_student_flow(exam_client) -> None:
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=2)
    headers = _student_headers()

    library = exam_client.get("/api/student/exams", headers=headers)
    assert library.status_code == 200
    assert any(e["id"] == exam_id for e in library.json()["exams"])

    started = exam_client.post(f"/api/student/exams/{exam_id}/start", headers=headers)
    assert started.status_code == 200, started.text
    payload = started.json()
    attempt_id = payload["attempt"]["id"]
    assert len(payload["questions"]) == 2
    # The session payload must never leak the correct answer.
    assert "correct_option_id" not in payload["questions"][0]

    first_q = payload["questions"][0]
    saved = exam_client.post(
        f"/api/student/attempts/{attempt_id}/answers",
        json={"question_id": first_q["question_id"], "selected_option": 0},
        headers=headers,
    )
    assert saved.status_code == 200

    submitted = exam_client.post(f"/api/student/attempts/{attempt_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert "percentage" in submitted.json()["result"]

    review = exam_client.get(f"/api/student/attempts/{attempt_id}/review", headers=headers)
    assert review.status_code == 200
    assert len(review.json()["questions"]) == 2


def test_student_cannot_read_other_attempt(exam_client) -> None:
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=1)
    headers = _student_headers()
    attempt_id = exam_client.post(
        f"/api/student/exams/{exam_id}/start", headers=headers
    ).json()["attempt"]["id"]

    other = build_init_data({**STUDENT_USER, "id": 999999}, BOT_TOKEN)
    response = exam_client.get(
        f"/api/student/attempts/{attempt_id}", headers={"X-Telegram-Init-Data": other}
    )
    assert response.status_code == 400


def test_admin_exam_analytics(exam_client) -> None:
    csrf = _login(exam_client)
    _make_exam(exam_client, csrf, questions=1)
    headers = _student_headers()
    exam_id = exam_client.get("/api/student/exams", headers=headers).json()["exams"][0]["id"]
    attempt_id = exam_client.post(
        f"/api/student/exams/{exam_id}/start", headers=headers
    ).json()["attempt"]["id"]
    exam_client.post(f"/api/student/attempts/{attempt_id}/submit", headers=headers)

    analytics = exam_client.get("/api/admin/analytics", headers={"X-CSRF-Token": csrf})
    assert analytics.status_code == 200
    totals = analytics.json()["totals"]
    assert totals["students"] >= 1
    assert totals["completed"] >= 1


def test_admin_requires_exam_permission(exam_client) -> None:
    # Unauthenticated admin call is rejected.
    assert exam_client.get("/api/admin/exams").status_code == 401


# --- initData verification -------------------------------------------------

def test_init_data_rejects_expired_and_tampered() -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.telegram_auth import TelegramAuthError, verify_init_data

    user = {"id": 1, "first_name": "A"}
    stale = build_init_data(user, BOT_TOKEN, auth_date=int((datetime.now(UTC) - timedelta(days=2)).timestamp()))
    with pytest.raises(TelegramAuthError):
        verify_init_data(stale, BOT_TOKEN, max_age=3600)

    valid = build_init_data(user, BOT_TOKEN)
    assert verify_init_data(valid, BOT_TOKEN).user_id == 1
    # A different token must not verify the same payload.
    with pytest.raises(TelegramAuthError):
        verify_init_data(valid, "999:other-token")
    # A tampered payload must not verify.
    with pytest.raises(TelegramAuthError):
        verify_init_data(valid.replace("first_name", "last_name"), BOT_TOKEN)


def test_student_token_roundtrip() -> None:
    from app.core.telegram_auth import TelegramAuthError, create_student_token, load_student_token

    token = create_student_token(4242)
    assert load_student_token(token) == 4242
    with pytest.raises(TelegramAuthError):
        load_student_token(token + "tamper")


# --- notifications ---------------------------------------------------------

def test_notification_dispatch_delivers_and_marks(svc) -> None:
    import asyncio

    q = _ready(svc)
    exam = svc.exams.create({"title": "إشعار", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(20))
    svc.notifications.enqueue(
        student_id=student["id"], exam_id=exam["id"], type="exam_published", title="t", body="b"
    )
    summary = asyncio.run(svc.notifications.dispatch())
    assert summary["sent"] == 1
    note = svc.notifications.for_student(student["id"])[0]
    assert note["status"] == "sent"


def test_notification_dispatch_skips_disabled_student(svc) -> None:
    import asyncio

    q = _ready(svc)
    exam = svc.exams.create({"title": "إشعار", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(21))
    svc.students.set_notifications(student["id"], False)
    svc.notifications.enqueue(
        student_id=student["id"], exam_id=exam["id"], type="exam_published", title="t", body="b"
    )
    summary = asyncio.run(svc.notifications.dispatch())
    assert summary["skipped"] == 1
    assert svc.notifications.for_student(student["id"])[0]["status"] == "skipped"


# --- expiry ----------------------------------------------------------------

def test_expired_attempt_is_swept_and_submitted(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "مؤقّت", "access_mode": "always", "duration_minutes": 30}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(22))
    attempt = svc.attempts.start(exam["id"], student["id"])

    # Force the deadline into the past, then sweep.
    def _expire(document):
        for row in document["attempts"]:
            if row["id"] == attempt["id"]:
                row["expires_at"] = "2000-01-01T00:00:00Z"

    svc.storage.attempts.update(_expire)
    assert svc.attempts.sweep_expired() == 1
    assert svc.attempts.get(attempt["id"])["status"] == "expired"


def test_admin_reset_attempts_clears_history(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "إعادة ضبط", "access_mode": "always", "max_attempts": 1}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(23))
    attempt = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.submit(attempt["id"], student["id"])
    with pytest.raises(ValidationError):
        svc.attempts.start(exam["id"], student["id"])

    removed = svc.attempts.reset_student_attempts(exam["id"], student["id"])
    assert removed == 1
    # With history cleared the student may start again.
    restarted = svc.attempts.start(exam["id"], student["id"])
    assert restarted["id"] != attempt["id"]


def test_delete_exam_with_attempts_is_blocked(svc) -> None:
    q = _ready(svc)
    exam = svc.exams.create({"title": "حذف", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(24))
    attempt = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.submit(attempt["id"], student["id"])
    with pytest.raises(ValidationError):
        svc.exams.delete(exam["id"])


def test_admin_pages_render(exam_client) -> None:
    _login(exam_client)
    for path in ("/exams", "/exams/new", "/students", "/attempts", "/exam-analytics"):
        response = exam_client.get(path)
        assert response.status_code == 200, path
    # The Mini App shell is public (identity is resolved client-side via initData).
    app_page = exam_client.get("/app")
    assert app_page.status_code == 200
    assert "telegram-web-app.js" in app_page.text


def test_exam_editor_page_lists_bank_questions(exam_client) -> None:
    csrf = _login(exam_client)
    _make_exam(exam_client, csrf, questions=1)
    page = exam_client.get("/exams/new")
    assert page.status_code == 200
    assert "سؤال 1" in page.text


def _submit_one_attempt(client: TestClient, exam_id: str) -> str:
    """Start and submit a single attempt for the standard student, return its id."""
    headers = _student_headers()
    started = client.post(f"/api/student/exams/{exam_id}/start", headers=headers)
    assert started.status_code == 200, started.text
    attempt_id = started.json()["attempt"]["id"]
    client.post(
        f"/api/student/attempts/{attempt_id}/submit", headers=headers
    )
    return attempt_id


def test_attempts_search_matches_student_name_username_and_exam_title(exam_client) -> None:
    """The attempts table shows names/titles, so search must resolve those too."""
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=1)
    attempt_id = _submit_one_attempt(exam_client, exam_id)

    # By first name (what the operator sees in the table).
    by_name = exam_client.get("/api/admin/attempts", params={"search": "سارة"})
    assert any(a["id"] == attempt_id for a in by_name.json()["attempts"])
    # By handle, with and without the leading @.
    for handle in ("sara", "@sara", "@SARA"):
        hit = exam_client.get("/api/admin/attempts", params={"search": handle})
        assert any(a["id"] == attempt_id for a in hit.json()["attempts"]), handle
    # By exam title.
    by_exam = exam_client.get("/api/admin/attempts", params={"search": "اختبار API"})
    assert any(a["id"] == attempt_id for a in by_exam.json()["attempts"])
    # By raw attempt id still works.
    by_id = exam_client.get("/api/admin/attempts", params={"search": attempt_id})
    assert any(a["id"] == attempt_id for a in by_id.json()["attempts"])


def test_attempts_status_filter(exam_client) -> None:
    """The status filter must narrow the rows and match the rendered page."""
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=1)
    attempt_id = _submit_one_attempt(exam_client, exam_id)

    submitted = exam_client.get("/api/admin/attempts", params={"status": "submitted"})
    assert any(a["id"] == attempt_id for a in submitted.json()["attempts"])
    assert all(a["status"] == "submitted" for a in submitted.json()["attempts"])

    in_progress = exam_client.get("/api/admin/attempts", params={"status": "in_progress"})
    assert all(a["status"] == "in_progress" for a in in_progress.json()["attempts"])
    assert not any(a["id"] == attempt_id for a in in_progress.json()["attempts"])

    page = exam_client.get("/attempts", params={"status": "submitted", "search": "سارة"})
    assert page.status_code == 200
    assert attempt_id in page.text


def test_student_search_ignores_at_prefix(svc) -> None:
    student = svc.students.touch(_TG(50, first="راشد", last="المانع", username="Rashed_Almanie"))
    assert [s["id"] for s in svc.students.list_students(search="@Rashed_Almanie")] == [student["id"]]
    assert [s["id"] for s in svc.students.list_students(search="راشد")] == [student["id"]]
    assert [s["id"] for s in svc.students.list_students(search="المانع")] == [student["id"]]


def test_exam_stats_count_best_attempt_per_student(svc) -> None:
    """A repeat sitter contributes their best score, not their latest or every try."""
    q = _ready(svc)
    exam = svc.exams.create(
        {"title": "أفضل نتيجة", "access_mode": "always", "max_attempts": 0}, [q["id"]]
    )
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(51))

    first = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.save_answer(first["id"], student["id"], q["id"], 1)  # wrong
    svc.attempts.submit(first["id"], student["id"])

    second = svc.attempts.start(exam["id"], student["id"])
    svc.attempts.save_answer(second["id"], student["id"], q["id"], 0)  # correct
    svc.attempts.submit(second["id"], student["id"])

    stats = svc.exams.stats(exam["id"])
    assert stats["attempts"] == 2
    assert stats["completed"] == 2
    assert stats["students_graded"] == 1
    # Only the 100% attempt counts, so the average is 100 — not the 50 the two
    # attempts would produce if both were counted.
    assert stats["average"] == 100.0
    assert stats["lowest"] == 100.0

    best = svc.attempts.best_per_student(svc.attempts.attempts_for_exam(exam["id"]))
    assert len(best) == 1
    assert best[0]["id"] == second["id"]


def test_partial_update_keeps_status_and_questions(exam_client) -> None:
    """A PUT that only sends the title must not reset status or drop questions."""
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=2)
    response = exam_client.put(
        f"/api/admin/exams/{exam_id}",
        json={"title": "عنوان محدّث"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "عنوان محدّث"
    assert body["status"] == "published"
    assert body["question_count"] == 2
    # The exam stays visible to students after a title-only edit.
    detail = exam_client.get(f"/api/admin/exams/{exam_id}")
    assert len(detail.json()["questions"]) == 2


def test_exam_detail_page(exam_client) -> None:
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=2)
    detail = exam_client.get(f"/exams/{exam_id}")
    assert detail.status_code == 200
    assert "اختبار API" in detail.text
    edit = exam_client.get(f"/exams/{exam_id}/edit")
    assert edit.status_code == 200


def test_nav_shows_exam_section_for_admin(exam_client) -> None:
    _login(exam_client)
    page = exam_client.get("/")
    assert "منصة الاختبارات" in page.text


def test_resume_payload_reports_last_answered_question(exam_client) -> None:
    """Reopening an in-progress attempt returns every saved answer, so the Mini
    App can jump straight to the last question the student answered."""
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=3)
    headers = _student_headers()

    started = exam_client.post(f"/api/student/exams/{exam_id}/start", headers=headers)
    assert started.status_code == 200, started.text
    payload = started.json()
    attempt_id = payload["attempt"]["id"]
    questions = payload["questions"]
    # Nothing answered yet: every selected_option is null.
    assert all(q["selected_option"] is None for q in questions)

    # Answer the first two, leaving the third untouched.
    for question, option in zip(questions[:2], (1, 2)):
        saved = exam_client.post(
            f"/api/student/attempts/{attempt_id}/answers",
            json={"question_id": question["question_id"], "selected_option": option},
            headers=headers,
        )
        assert saved.status_code == 200

    resumed = exam_client.get(f"/api/student/attempts/{attempt_id}", headers=headers)
    assert resumed.status_code == 200, resumed.text
    rows = resumed.json()["questions"]
    answered = [i for i, q in enumerate(rows) if q["selected_option"] is not None]
    assert answered == [0, 1]
    assert rows[1]["selected_option"] == 2
    assert rows[2]["selected_option"] is None


# --- bot Mini App URL resolution ------------------------------------------
class _FakeMenuButton:
    def __init__(self, url: str = "") -> None:
        self.web_app = type("WA", (), {"url": url})()


class _FakeBot:
    def __init__(self, url: str = "") -> None:
        self._url = url
        self.set_calls: list[str] = []

    async def get_chat_menu_button(self) -> _FakeMenuButton:
        return _FakeMenuButton(self._url)

    async def set_chat_menu_button(self, menu_button=None) -> None:
        self.set_calls.append(menu_button.web_app.url)


class _FakeApp:
    def __init__(self, url: str = "") -> None:
        self.bot = _FakeBot(url)


def _bot_manager():
    from app.telegram.bot import BotManager

    return BotManager({}, secret_store=None)


def test_mini_app_url_prefers_setting(monkeypatch) -> None:
    import asyncio

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "mini_app_url", "https://quiz.example.com/")
    monkeypatch.setattr(settings, "telegram_webhook_url", "")
    url = asyncio.run(_bot_manager()._resolve_mini_app_url(_FakeApp()))
    assert url == "https://quiz.example.com/app"


def test_mini_app_url_falls_back_to_webhook_origin(monkeypatch) -> None:
    import asyncio

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "mini_app_url", "")
    monkeypatch.setattr(
        settings, "telegram_webhook_url", "https://prod.example.com/telegram/hook"
    )
    url = asyncio.run(_bot_manager()._resolve_mini_app_url(_FakeApp()))
    assert url == "https://prod.example.com/app"


def test_mini_app_url_falls_back_to_existing_menu_button(monkeypatch) -> None:
    import asyncio

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "mini_app_url", "")
    monkeypatch.setattr(settings, "telegram_webhook_url", "")
    app = _FakeApp("https://live.example.com/app")
    url = asyncio.run(_bot_manager()._resolve_mini_app_url(app))
    assert url == "https://live.example.com/app"


def test_mini_app_url_empty_when_nothing_known(monkeypatch) -> None:
    import asyncio

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "mini_app_url", "")
    monkeypatch.setattr(settings, "telegram_webhook_url", "")
    assert asyncio.run(_bot_manager()._resolve_mini_app_url(_FakeApp(""))) == ""


# --- editable bot messages + notification toggles --------------------------

def test_render_message_uses_defaults_and_substitutes() -> None:
    from app.core.messages import render, template_text

    assert "{exam_title}" in template_text({}, "exam_published_body")
    rendered = render({}, "exam_published_body", {"exam_title": "الشبكات", "question_count": 3})
    assert "الشبكات" in rendered and "3" in rendered
    assert "{" not in rendered  # every placeholder was substituted


def test_render_message_unknown_placeholder_is_left_alone() -> None:
    from app.core.messages import render

    rendered = render({"exam_published_body": "أهلاً {exam_title} {unknown}"}, "exam_published_body", {"exam_title": "س"})
    assert rendered == "أهلاً س {unknown}"


def test_render_message_falls_back_when_blank() -> None:
    from app.core.messages import render, template_text

    # A blank stored template must never produce an empty message.
    assert template_text({"welcome": "   "}, "welcome") == template_text({}, "welcome")
    assert render({"welcome": ""}, "welcome") != ""


def test_settings_messages_roundtrip(svc) -> None:
    svc.app_settings.update({"messages": {"welcome": "مرحباً {exam_title}"}})
    assert svc.app_settings.messages()["welcome"] == "مرحباً {exam_title}"
    # Omitted keys keep their previous value / default.
    assert svc.app_settings.messages()["exam_published_title"]


def test_settings_messages_reject_overlong(svc) -> None:
    with pytest.raises(ValidationError):
        svc.app_settings.update({"messages": {"welcome": "x" * 2001}})


def test_settings_notifications_roundtrip(svc) -> None:
    svc.app_settings.update({"notifications": {"on_publish": False, "types": {"exam_published": False}}})
    config = svc.app_settings.notifications_config()
    assert config["on_publish"] is False
    assert config["types"]["exam_published"] is False
    assert config["types"]["attempt_completed"] is True  # untouched default


def test_notify_exam_published_uses_custom_message(svc) -> None:
    import asyncio

    svc.app_settings.update({"messages": {"exam_published_title": "عنوان مخصص", "exam_published_body": "اختبار {exam_title}"}})
    q = _ready(svc)
    exam = svc.exams.create({"title": "الشبكات", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    student = svc.students.touch(_TG(31))
    created = svc.notifications.notify_exam_published(exam)
    assert created == 1
    note = svc.notifications.for_student(student["id"])[0]
    assert note["title"] == "عنوان مخصص"
    assert note["body"] == "اختبار الشبكات"
    asyncio.run(svc.notifications.dispatch())
    assert svc.notifications.for_student(student["id"])[0]["status"] == "sent"


def test_notify_exam_published_respects_type_toggle(svc) -> None:
    svc.app_settings.update({"notifications": {"on_publish": True, "types": {"exam_published": False}}})
    q = _ready(svc)
    exam = svc.exams.create({"title": "معطّل", "access_mode": "always"}, [q["id"]])
    svc.exams.publish(exam["id"])
    svc.students.touch(_TG(32))
    assert svc.notifications.notify_exam_published(exam) == 0


def test_due_scheduled_exam_is_notified_once_window_opens(svc) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    q = _ready(svc)
    future = svc.exams.create(
        {
            "title": "مجدول",
            "access_mode": "scheduled",
            "starts_at": (now + timedelta(hours=2)).isoformat(),
            "ends_at": (now + timedelta(hours=4)).isoformat(),
        },
        [q["id"]],
    )
    svc.exams.publish(future["id"])
    student = svc.students.touch(_TG(33))
    # Window has not opened: no notification yet.
    assert svc.notifications.notify_due_exams() == 0
    assert svc.notifications.for_student(student["id"]) == []

    # Move the window into the past (now open) and sweep again.
    def _open(doc):
        for e in doc["exams"]:
            if e["id"] == future["id"]:
                e["starts_at"] = (now - timedelta(minutes=1)).isoformat()

    svc.storage.exams.update(_open)
    assert svc.notifications.notify_due_exams() == 1
    # A second sweep must not duplicate.
    assert svc.notifications.notify_due_exams() == 0
    assert len(svc.notifications.for_student(student["id"])) == 1


def test_publish_via_put_status_notifies_students(exam_client) -> None:
    csrf = _login(exam_client)
    q = exam_client.post(
        "/api/questions",
        json={"question": "س", "options": ["أ", "ب"], "correct_option_id": 0, "status": "ready"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    created = exam_client.post(
        "/api/admin/exams",
        json={"title": "بالتعديل", "access_mode": "always", "question_ids": [q["id"]]},
        headers={"X-CSRF-Token": csrf},
    ).json()
    exam_id = created["id"]
    # A draft exam notifies nobody, so the student exists before it goes live.
    exam_client.post(
        "/api/student/auth",
        json={"init_data": build_init_data(STUDENT_USER, BOT_TOKEN)},
    )
    updated = exam_client.put(
        f"/api/admin/exams/{exam_id}",
        json={"title": "بالتعديل", "status": "published"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["notified"] == 1


def test_manual_notify_endpoint(exam_client) -> None:
    csrf = _login(exam_client)
    exam_id = _make_exam(exam_client, csrf, questions=1)
    # The publish call already notified the (then-unknown) student set; a fresh
    # student added afterwards is picked up by the manual notify.
    exam_client.post(
        "/api/student/auth",
        json={"init_data": build_init_data({**STUDENT_USER, "id": 880880}, BOT_TOKEN)},
    )
    response = exam_client.post(
        f"/api/admin/exams/{exam_id}/notify", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    assert response.json()["notified"] >= 1


def test_manual_notify_rejects_draft(exam_client) -> None:
    csrf = _login(exam_client)
    q = exam_client.post(
        "/api/questions",
        json={"question": "س", "options": ["أ", "ب"], "correct_option_id": 0, "status": "ready"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    exam = exam_client.post(
        "/api/admin/exams",
        json={"title": "مسودة", "question_ids": [q["id"]]},
        headers={"X-CSRF-Token": csrf},
    ).json()
    response = exam_client.post(
        f"/api/admin/exams/{exam['id']}/notify", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400


def test_settings_page_shows_messages_section(exam_client) -> None:
    _login(exam_client)
    response = exam_client.get("/settings")
    assert response.status_code == 200
    assert "رسائل البوت والترحيب" in response.text
    assert "welcome" in response.text
    assert "notifyOnPublish" in response.text


def test_welcome_message_uses_custom_template(svc) -> None:
    from app.telegram.bot import BotManager

    svc.app_settings.update({"messages": {"welcome": "أهلاً بك في المنصة"}})
    bot = BotManager({"app_settings": svc.app_settings}, secret_store=None)
    assert bot._welcome_text() == "أهلاً بك في المنصة"
