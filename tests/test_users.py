"""Multi-user access control and the manual multi-question publish flow.

These drive the real FastAPI app and the real ``UserService``; only the
Telegram network hop is faked (see ``conftest.FakeTelegramClient``).
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
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-user-tests")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.delenv("APP_DEBUG", raising=False)

    get_settings.cache_clear()

    from app.main import create_app

    application = create_app()
    with TestClient(application) as client:
        # Pacing waits are recorded, not slept, so burst tests stay fast.
        from tests.conftest import RecordingSleeper

        application.state.services.publishing._sleep = RecordingSleeper()
        yield client
    get_settings.cache_clear()


def _login(client: TestClient, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("tqm_session")


def _csrf(client: TestClient) -> str:
    from app.core.security import load_session

    return load_session(client.cookies.get("tqm_session")).csrf_token


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": _csrf(client)}


def _make_question(client: TestClient, headers: dict[str, str], text: str = "Q?") -> dict:
    return client.post(
        "/api/questions",
        json={
            "question": text,
            "options": ["A", "B"],
            "correct_option_id": 0,
            "status": "ready",
        },
        headers=headers,
    ).json()


def _make_channel(client: TestClient, headers: dict[str, str], name: str = "Main") -> dict:
    return client.post(
        "/api/channels",
        json={"name": name, "chat_id": f"@{name.lower()}_channel", "enabled": True},
        headers=headers,
    ).json()


def _new_user(
    client: TestClient,
    headers: dict[str, str],
    username: str,
    *,
    role: str = "user",
    permissions: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/users",
        json={
            "username": username,
            "password": "UserPassword123!",
            "role": role,
            "permissions": permissions or [],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


# --- user management API ---------------------------------------------------

def test_users_endpoints_require_auth(app_client) -> None:
    assert app_client.get("/api/users").status_code == 401
    assert app_client.get("/users", follow_redirects=False).status_code == 303


def test_admin_can_create_and_list_users(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)

    created = _new_user(app_client, headers, "editor", permissions=["questions"])
    assert created["username"] == "editor"
    assert created["role"] == "user"
    assert created["permissions"] == ["questions"]
    # The hash is never exposed through the API.
    assert "password_hash" not in created

    listed = app_client.get("/api/users").json()
    usernames = {u["username"] for u in listed["users"]}
    assert {"admin", "editor"} <= usernames
    assert "questions" in listed["grants"]


def test_create_user_rejects_duplicate_and_weak_password(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor")

    duplicate = app_client.post(
        "/api/users",
        json={"username": "editor", "password": "UserPassword123!"},
        headers=headers,
    )
    assert duplicate.status_code == 400
    assert "موجود" in duplicate.json()["error"]

    weak = app_client.post(
        "/api/users",
        json={"username": "shorty", "password": "123"},
        headers=headers,
    )
    assert weak.status_code == 400


def test_unknown_permission_keys_are_dropped(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    created = _new_user(
        app_client, headers, "editor", permissions=["questions", "not-a-real-grant"]
    )
    assert created["permissions"] == ["questions"]


def test_admin_role_implicitly_holds_every_grant(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    created = _new_user(app_client, headers, "second-admin", role="admin")
    assert created["role"] == "admin"

    # Grants are not stored for admins: they hold every grant implicitly, so a
    # grant added to the catalog later is available without editing records.
    response = app_client.get("/api/users").json()
    stored = next(u for u in response["users"] if u["username"] == "second-admin")
    assert stored["permissions"] == []

    # The new admin really can manage users.
    app_client.cookies.clear()
    _login(app_client, "second-admin", "UserPassword123!")
    assert app_client.get("/api/users").status_code == 200


def test_last_admin_cannot_be_demoted_or_deleted(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)

    demote = app_client.put(
        "/api/users/admin", json={"role": "user"}, headers=headers
    )
    assert demote.status_code == 400
    assert "آخر" in demote.json()["error"]

    delete = app_client.delete("/api/users/admin", headers=headers)
    assert delete.status_code == 400


def test_user_can_be_deleted_but_not_self(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor")

    self_delete = app_client.delete("/api/users/admin", headers=headers)
    assert self_delete.status_code == 400

    removed = app_client.delete("/api/users/editor", headers=headers)
    assert removed.status_code == 200
    usernames = {u["username"] for u in app_client.get("/api/users").json()["users"]}
    assert "editor" not in usernames


def test_password_reset_lets_user_log_in(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor")

    reset = app_client.post(
        "/api/users/editor/password",
        json={"new_password": "BrandNewPass99"},
        headers=headers,
    )
    assert reset.status_code == 200

    app_client.cookies.clear()
    _login(app_client, "editor", "BrandNewPass99")


def test_non_admin_cannot_reach_user_management(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")

    assert app_client.get("/api/users").status_code == 403
    # The HTML page redirects non-admins to the dashboard rather than 403.
    page = app_client.get("/users", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/"


def test_user_mutations_require_csrf(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor")

    response = app_client.post(
        "/api/users",
        json={"username": "sneaky", "password": "UserPassword123!"},
    )
    assert response.status_code == 403


# --- permission gating on existing areas -----------------------------------

def test_granted_user_can_manage_questions_only(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    user_headers = _headers(app_client)

    created = app_client.post(
        "/api/questions",
        json={
            "question": "Allowed?",
            "options": ["A", "B"],
            "correct_option_id": 0,
            "status": "ready",
        },
        headers=user_headers,
    )
    assert created.status_code == 201

    # ...but not channels, schedules, settings or users.
    assert (
        app_client.post(
            "/api/channels",
            json={"name": "X", "chat_id": "@x", "enabled": True},
            headers=user_headers,
        ).status_code
        == 403
    )
    assert app_client.get("/api/schedules").status_code == 403
    assert app_client.get("/api/settings").status_code == 403
    assert app_client.get("/api/users").status_code == 403


def test_granting_publish_enables_manual_publish(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    question = _make_question(app_client, headers)
    channel = _make_channel(app_client, headers)
    _new_user(app_client, headers, "publisher", permissions=["publish"])

    app_client.cookies.clear()
    _login(app_client, "publisher", "UserPassword123!")
    user_headers = _headers(app_client)

    allowed = app_client.post(
        "/api/publishing/manual",
        json={"question_id": question["id"], "channel_id": channel["id"]},
        headers=user_headers,
    )
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True

    # Automation controls are a separate grant.
    assert app_client.post("/api/publishing/pause", headers=user_headers).status_code == 403


def test_granting_automation_enables_pause_resume(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    question = _make_question(app_client, headers)
    channel = _make_channel(app_client, headers)
    _new_user(app_client, headers, "operator", permissions=["automation"])

    app_client.cookies.clear()
    _login(app_client, "operator", "UserPassword123!")
    user_headers = _headers(app_client)

    assert app_client.post("/api/publishing/pause", headers=user_headers).status_code == 200
    assert app_client.post("/api/publishing/resume", headers=user_headers).status_code == 200

    # Automation is a separate grant from publishing: this operator cannot send.
    blocked = app_client.post(
        "/api/publishing/manual",
        json={"question_id": question["id"], "channel_id": channel["id"]},
        headers=user_headers,
    )
    assert blocked.status_code == 403


def test_self_service_password_change_allowed_without_grants(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor")

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    user_headers = _headers(app_client)

    changed = app_client.post(
        "/api/settings/password",
        json={"current_password": "UserPassword123!", "new_password": "AnotherPass456"},
        headers=user_headers,
    )
    assert changed.status_code == 200
    assert changed.json()["ok"] is True


def test_users_page_renders_for_admin(app_client) -> None:
    _login(app_client)
    page = app_client.get("/users")
    assert page.status_code == 200
    assert "المستخدمون" in page.text
    assert "admin" in page.text


def test_permission_gated_pages_redirect_unauthorised_users(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")

    # Allowed page renders, including the bulk-publish controls.
    questions = app_client.get("/questions")
    assert questions.status_code == 200
    assert "bulkPublishModal" not in questions.text  # no publish grant -> hidden
    assert "نشر المحدد" not in questions.text

    # Pages for ungranted areas redirect to the dashboard.
    for path in ("/channels", "/schedules", "/analytics"):
        page = app_client.get(path, follow_redirects=False)
        assert page.status_code == 303, path
        assert page.headers["location"] == "/", path


def test_bulk_publish_controls_shown_when_both_grants_held(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "publisher", permissions=["questions", "publish"])

    app_client.cookies.clear()
    _login(app_client, "publisher", "UserPassword123!")

    page = app_client.get("/questions")
    assert page.status_code == 200
    assert "bulkPublishModal" in page.text
    assert "نشر المحدد" in page.text


def test_settings_page_hides_system_panels_from_regular_users(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    user_page = app_client.get("/settings")
    assert user_page.status_code == 200
    # Self-service stays; system-wide panels and the users shortcut do not.
    assert "الحساب وكلمة المرور" in user_page.text
    assert "حالة البوت والتوكن" not in user_page.text
    assert "سجل تسجيل الدخول" not in user_page.text
    assert "النسخ الاحتياطي والاستعادة" not in user_page.text
    assert "/users" not in user_page.text

    app_client.cookies.clear()
    _login(app_client)
    admin_page = app_client.get("/settings")
    assert "حالة البوت والتوكن" in admin_page.text
    assert "سجل تسجيل الدخول" in admin_page.text
    assert "النسخ الاحتياطي والاستعادة" in admin_page.text


def test_dashboard_hides_activity_feed_from_regular_users(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    page = app_client.get("/")
    assert page.status_code == 200
    # The system audit feed and its "view all" shortcut are admin-only.
    assert "آخر العمليات" not in page.text
    assert "عرض الكل" not in page.text
    # The user's own publishing summary stays.
    assert "ملخص النشر" in page.text

    app_client.cookies.clear()
    _login(app_client)
    admin_page = app_client.get("/")
    assert "آخر العمليات" in admin_page.text


def test_dashboard_api_hides_recent_from_regular_users(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    body = app_client.get("/api/dashboard").json()
    assert "recent" not in body
    assert "questions" in body

    app_client.cookies.clear()
    _login(app_client)
    assert "recent" in app_client.get("/api/dashboard").json()


def test_sidebar_hides_tabs_without_the_grant(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    page = app_client.get("/")
    assert page.status_code == 200
    # Granted tab stays; ungranted ones and admin-only tabs disappear.
    assert 'href="/questions"' in page.text
    for href in ('href="/channels"', 'href="/schedules"', 'href="/analytics"',
                 'href="/users"', 'href="/history"'):
        assert href not in page.text, href
    # Settings is always reachable (self-service account panel).
    assert 'href="/settings"' in page.text

    app_client.cookies.clear()
    _login(app_client)
    admin_page = app_client.get("/")
    for href in ('href="/questions"', 'href="/channels"', 'href="/schedules"',
                 'href="/history"', 'href="/analytics"', 'href="/users"'):
        assert href in admin_page.text, href


def test_sidebar_shows_each_tab_when_granted(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(
        app_client,
        headers,
        "operator",
        permissions=["questions", "channels", "schedules", "analytics"],
    )

    app_client.cookies.clear()
    _login(app_client, "operator", "UserPassword123!")
    page = app_client.get("/")
    for href in ('href="/questions"', 'href="/channels"', 'href="/schedules"',
                 'href="/analytics"'):
        assert href in page.text, href
    # History and users remain admin-only regardless of grants.
    assert 'href="/history"' not in page.text
    assert 'href="/users"' not in page.text


def test_history_page_and_api_are_admin_only(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions", "publish"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    page = app_client.get("/history", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/"
    assert app_client.get("/api/history").status_code == 403

    app_client.cookies.clear()
    _login(app_client)
    assert app_client.get("/history").status_code == 200
    assert app_client.get("/api/history").status_code == 200


def test_regular_user_cannot_change_system_settings(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    _new_user(app_client, headers, "editor", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "editor", "UserPassword123!")
    user_headers = _headers(app_client)

    blocked = app_client.put(
        "/api/settings", json={"timezone": "UTC"}, headers=user_headers
    )
    assert blocked.status_code == 403
    assert app_client.post("/api/backups/create", headers=user_headers).status_code == 403


# --- manual multi-question publish ----------------------------------------

def test_bulk_publish_sends_every_selected_question(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    channel = _make_channel(app_client, headers)
    ids = [_make_question(app_client, headers, f"Q{i}?")["id"] for i in range(3)]

    response = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": ids, "channel_id": channel["id"]},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["total"] == 3
    assert body["succeeded"] == 3
    assert body["failed"] == 0
    assert [r["question_id"] for r in body["results"]] == ids

    history = app_client.get("/api/history").json()["history"]
    published = {h["question_id"] for h in history if h["status"] == "success"}
    assert set(ids) <= published


def test_bulk_publish_deduplicates_and_reports_failures(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    channel = _make_channel(app_client, headers)
    good = _make_question(app_client, headers, "Good?")["id"]

    response = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": [good, good, "Q9999"], "channel_id": channel["id"]},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    # The duplicate is collapsed, the missing id fails but does not abort the run.
    assert body["total"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["ok"] is False
    failed = next(r for r in body["results"] if r["status"] != "success")
    assert failed["question_id"] == "Q9999"


def test_bulk_publish_paces_sends_and_reports_all(app_client) -> None:
    """A multi-question burst must be spaced out, not fired back-to-back."""
    _login(app_client)
    headers = _headers(app_client)
    channel = _make_channel(app_client, headers)
    ids = [_make_question(app_client, headers, f"Q{i}?")["id"] for i in range(5)]

    sleeper = app_client.app.state.services.publishing._sleep  # noqa: SLF001

    response = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": ids, "channel_id": channel["id"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == 5
    # Default interval is 4s, applied between consecutive questions only.
    assert sleeper.waits == [4, 4, 4, 4]


def test_bulk_publish_modal_shows_pacing_interval(app_client) -> None:
    _login(app_client)
    page = app_client.get("/questions")
    assert page.status_code == 200
    assert 'data-pacing-interval="4"' in page.text
    assert "bpEstimate" in page.text


def test_bulk_publish_validates_input(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    channel = _make_channel(app_client, headers)

    empty = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": [], "channel_id": channel["id"]},
        headers=headers,
    )
    assert empty.status_code == 400

    no_channel = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": ["Q0001"], "channel_id": ""},
        headers=headers,
    )
    assert no_channel.status_code == 400


def test_bulk_publish_requires_csrf(app_client) -> None:
    _login(app_client)
    response = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": ["Q0001"], "channel_id": "CH001"},
    )
    assert response.status_code == 403


def test_bulk_publish_requires_publish_grant(app_client) -> None:
    _login(app_client)
    headers = _headers(app_client)
    channel = _make_channel(app_client, headers)
    question = _make_question(app_client, headers)
    _new_user(app_client, headers, "viewer", permissions=["questions"])

    app_client.cookies.clear()
    _login(app_client, "viewer", "UserPassword123!")

    response = app_client.post(
        "/api/publishing/bulk",
        json={"question_ids": [question["id"]], "channel_id": channel["id"]},
        headers=_headers(app_client),
    )
    assert response.status_code == 403
