"""End-to-end API tests through the real FastAPI app and TestClient.

These exercise authentication, CSRF enforcement and the JSON endpoints using
the actual routers and services (only the Telegram transport is faked).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "TestPassword123!"


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-api-tests")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.delenv("APP_DEBUG", raising=False)

    get_settings.cache_clear()

    from app.main import create_app

    application = create_app()
    with TestClient(application) as client:
        yield client
    get_settings.cache_clear()


def _login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("tqm_session")


def _csrf(client: TestClient) -> str:
    from app.core.security import load_session

    return load_session(client.cookies.get("tqm_session")).csrf_token


# --- auth ------------------------------------------------------------------

def test_login_page_renders(app_client) -> None:
    response = app_client.get("/login")
    assert response.status_code == 200
    assert "Telegram Quiz Manager" in response.text


def test_protected_page_redirects_to_login(app_client) -> None:
    response = app_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_api_requires_auth(app_client) -> None:
    assert app_client.get("/api/questions").status_code == 401
    assert app_client.get("/api/dashboard").status_code == 401


def test_login_with_wrong_password_fails(app_client) -> None:
    response = app_client.post(
        "/login", data={"username": ADMIN_USER, "password": "nope"}, follow_redirects=False
    )
    assert response.status_code == 401
    assert "غير صحيحة" in response.text


def test_successful_login_grants_dashboard_access(app_client) -> None:
    _login(app_client)
    response = app_client.get("/")
    assert response.status_code == 200
    assert "لوحة التحكم" in response.text


def test_logout_clears_session(app_client) -> None:
    _login(app_client)
    app_client.get("/logout", follow_redirects=False)
    assert app_client.get("/api/questions").status_code == 401


# --- CSRF ------------------------------------------------------------------

def test_mutation_without_csrf_token_is_rejected(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/questions",
        json={
            "question": "no csrf",
            "options": ["a", "b"],
            "correct_option_id": 0,
        },
    )
    assert response.status_code == 403


def test_mutation_with_wrong_csrf_token_is_rejected(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/questions",
        json={"question": "bad csrf", "options": ["a", "b"], "correct_option_id": 0},
        headers={"X-CSRF-Token": "definitely-wrong"},
    )
    assert response.status_code == 403


def test_read_request_needs_no_csrf(app_client) -> None:
    _login(app_client)
    assert app_client.get("/api/questions").status_code == 200


# --- questions API ---------------------------------------------------------

def test_question_crud_through_api(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}

    created = app_client.post(
        "/api/questions",
        json={
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS", "FTP", "SSH"],
            "correct_option_id": 1,
            "explanation": "DNS",
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        },
        headers=headers,
    )
    assert created.status_code == 201
    question_id = created.json()["id"]

    listed = app_client.get("/api/questions")
    assert len(listed.json()["questions"]) == 1

    updated = app_client.put(
        f"/api/questions/{question_id}",
        json={
            "question": "Updated text",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "hard",
            "status": "ready",
        },
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["question"] == "Updated text"

    deleted = app_client.delete(f"/api/questions/{question_id}", headers=headers)
    assert deleted.status_code == 200
    assert app_client.get("/api/questions").json()["questions"] == []


def test_invalid_question_returns_400_with_message(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/questions",
        json={"question": "bad", "options": ["only one"], "correct_option_id": 0},
        headers={"X-CSRF-Token": _csrf(app_client)},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_questions_export_returns_document(app_client) -> None:
    _login(app_client)
    response = app_client.get("/api/questions/export")
    assert response.status_code == 200
    assert response.json()["version"] == 1


def test_bulk_delete_requires_explicit_confirmation(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    ids = [
        app_client.post(
            "/api/questions",
            json={
                "question": f"Question {n}",
                "options": ["A", "B"],
                "correct_option_id": 0,
                "status": "ready",
            },
            headers=headers,
        ).json()["id"]
        for n in (1, 2)
    ]

    unconfirmed = app_client.post(
        "/api/questions/bulk",
        json={"ids": ids, "action": "delete"},
        headers=headers,
    )
    assert unconfirmed.status_code == 400
    assert len(app_client.get("/api/questions").json()["questions"]) == 2

    confirmed = app_client.post(
        "/api/questions/bulk",
        json={"ids": ids, "action": "delete", "confirm": True},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["affected"] == 2
    assert app_client.get("/api/questions").json()["questions"] == []


# --- channels, schedules, publishing --------------------------------------

def test_channel_creation_and_listing(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    created = app_client.post(
        "/api/channels",
        json={"name": "Main", "chat_id": "@main_channel", "enabled": True},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["default"] is True
    assert len(app_client.get("/api/channels").json()["channels"]) == 1


def test_duplicate_channel_rejected(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    body = {"name": "Main", "chat_id": "@main_channel", "enabled": True}
    app_client.post("/api/channels", json=body, headers=headers)
    duplicate = app_client.post("/api/channels", json=body, headers=headers)
    assert duplicate.status_code == 400


def test_schedule_creation_requires_channel(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/schedules",
        json={"channel_id": "CH999", "type": "daily", "times": ["20:00"]},
        headers={"X-CSRF-Token": _csrf(app_client)},
    )
    assert response.status_code == 400


def test_full_schedule_lifecycle(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    channel = app_client.post(
        "/api/channels",
        json={"name": "Main", "chat_id": "@main_channel", "enabled": True},
        headers=headers,
    ).json()

    schedule = app_client.post(
        "/api/schedules",
        json={
            "channel_id": channel["id"],
            "type": "daily",
            "times": ["20:00"],
            "timezone": "Asia/Riyadh",
            "question_selection": "sequential",
            "questions_per_day": 0,
            "enabled": True,
        },
        headers=headers,
    )
    assert schedule.status_code == 201
    schedule_id = schedule.json()["id"]

    toggled = app_client.post(f"/api/schedules/{schedule_id}/toggle", headers=headers)
    assert toggled.status_code == 200
    assert toggled.json()["schedule"]["enabled"] is False

    assert app_client.get("/api/schedules").json()["schedules"][0]["id"] == schedule_id
    assert app_client.delete(f"/api/schedules/{schedule_id}", headers=headers).status_code == 200


def test_schedule_batch_fields_roundtrip(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    channel = app_client.post(
        "/api/channels",
        json={"name": "Main", "chat_id": "@main_channel", "enabled": True},
        headers=headers,
    ).json()

    created = app_client.post(
        "/api/schedules",
        json={
            "channel_id": channel["id"],
            "type": "times",
            "times": ["08:00", "20:00"],
            "questions_per_run": 4,
            "batch_interval_seconds": 90,
        },
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["questions_per_run"] == 4
    assert body["batch_interval_seconds"] == 90

    bad = app_client.post(
        "/api/schedules",
        json={
            "channel_id": channel["id"],
            "type": "daily",
            "times": ["20:00"],
            "questions_per_run": 0,
        },
        headers=headers,
    )
    assert bad.status_code == 400

    bad_gap = app_client.post(
        "/api/schedules",
        json={
            "channel_id": channel["id"],
            "type": "daily",
            "times": ["20:00"],
            "batch_interval_seconds": 5000,
        },
        headers=headers,
    )
    assert bad_gap.status_code == 400


def test_publish_now_requires_ready_question(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    app_client.post(
        "/api/channels",
        json={"name": "Main", "chat_id": "@main_channel", "enabled": True},
        headers=headers,
    )
    response = app_client.post("/api/publishing/publish-now", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "جاهز" in response.json()["error"]


def test_pause_and_resume_publishing(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    paused = app_client.post("/api/publishing/pause", headers=headers)
    assert paused.json()["publishing_enabled"] is False
    resumed = app_client.post("/api/publishing/resume", headers=headers)
    assert resumed.json()["publishing_enabled"] is True


def test_history_endpoint_shape(app_client) -> None:
    _login(app_client)
    body = app_client.get("/api/history").json()
    assert body["history"] == []
    assert body["stats"]["total"] == 0


# --- settings --------------------------------------------------------------

def test_settings_update_roundtrip(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    updated = app_client.put(
        "/api/settings",
        json={"timezone": "Europe/London", "daily_limit": 5},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["timezone"] == "Europe/London"
    assert app_client.get("/api/settings").json()["daily_limit"] == 5


def test_unknown_setting_key_rejected(app_client) -> None:
    _login(app_client)
    response = app_client.put(
        "/api/settings",
        json={"secret_backdoor": "x"},
        headers={"X-CSRF-Token": _csrf(app_client)},
    )
    assert response.status_code == 400


def test_password_change_flow(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    wrong = app_client.post(
        "/api/settings/password",
        json={"current_password": "wrong", "new_password": "BrandNewPass1!"},
        headers=headers,
    )
    assert wrong.status_code == 400

    ok = app_client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "BrandNewPass1!"},
        headers=headers,
    )
    assert ok.status_code == 200


def test_backup_create_and_export(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    created = app_client.post("/api/backups/create", headers=headers)
    assert created.status_code == 200
    assert app_client.get("/api/backups").json()["backups"]
    archive = app_client.get("/api/export")
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"


def test_health_endpoint(app_client) -> None:
    assert app_client.get("/healthz").json() == {"status": "ok"}


# --- analytics -------------------------------------------------------------

def test_analytics_page_renders(app_client) -> None:
    _login(app_client)
    response = app_client.get("/analytics")
    assert response.status_code == 200
    assert "إحصائيات التفاعل" in response.text
    assert "جمع النتائج الآن" in response.text


def test_analytics_requires_auth(app_client) -> None:
    assert app_client.get("/api/analytics").status_code == 401
    assert app_client.get("/analytics", follow_redirects=False).status_code == 303


def test_analytics_payload_shape(app_client) -> None:
    _login(app_client)
    response = app_client.get("/api/analytics")
    assert response.status_code == 200
    data = response.json()
    assert set(data) >= {
        "overview", "per_question", "by_channel", "by_category", "activity"
    }
    assert data["per_question"] == []
    assert data["overview"]["polls"] == 0
    assert len(data["activity"]["labels"]) == 14


def test_analytics_poll_detail_missing_returns_error(app_client) -> None:
    _login(app_client)
    response = app_client.get("/api/analytics/poll/does-not-exist")
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_collect_requires_csrf(app_client) -> None:
    _login(app_client)
    assert app_client.post("/api/analytics/collect", json={}).status_code == 403


def test_collect_reports_dry_run(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/analytics/collect", json={}, headers={"X-CSRF-Token": _csrf(app_client)}
    )
    assert response.status_code == 200
    body = response.json()
    # No bot token configured in tests, so collection is refused in dry-run mode.
    assert body["ok"] is False
    assert "تجريبي" in body["error"]


def test_analytics_settings_roundtrip(app_client) -> None:
    _login(app_client)
    response = app_client.put(
        "/api/settings",
        json={"analytics": {"auto_collect": True, "auto_collect_hours": 3}},
        headers={"X-CSRF-Token": _csrf(app_client)},
    )
    assert response.status_code == 200
    assert response.json()["analytics"] == {"auto_collect": True, "auto_collect_hours": 3}


# --- manual publish (right-click) -----------------------------------------

def test_get_single_question_by_id(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    created = app_client.post(
        "/api/questions",
        json={
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "status": "ready",
        },
        headers=headers,
    ).json()

    fetched = app_client.get(f"/api/questions/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["question"] == "Which protocol uses port 53?"

    # The literal export path must still win over the {question_id} route.
    assert app_client.get("/api/questions/export").json()["version"] == 1


def test_get_missing_question_returns_400(app_client) -> None:
    _login(app_client)
    response = app_client.get("/api/questions/Q9999")
    assert response.status_code == 400
    assert "غير موجود" in response.json()["error"]


def test_manual_publish_allows_repeat(app_client) -> None:
    _login(app_client)
    headers = {"X-CSRF-Token": _csrf(app_client)}
    question = app_client.post(
        "/api/questions",
        json={
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "status": "ready",
        },
        headers=headers,
    ).json()
    channel = app_client.post(
        "/api/channels",
        json={"name": "Main", "chat_id": "@main_channel", "enabled": True},
        headers=headers,
    ).json()

    payload = {"question_id": question["id"], "channel_id": channel["id"]}
    first = app_client.post("/api/publishing/manual", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["ok"] is True

    # The same question may be sent again on purpose from the manual path.
    second = app_client.post("/api/publishing/manual", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert second.json()["entry"]["id"] != first.json()["entry"]["id"]


def test_manual_publish_requires_csrf(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/publishing/manual",
        json={"question_id": "Q0001", "channel_id": "CH001"},
    )
    assert response.status_code == 403
