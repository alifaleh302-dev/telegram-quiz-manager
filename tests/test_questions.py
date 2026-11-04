"""Question service tests: validation, CRUD, bulk actions, import/export."""

from __future__ import annotations

import pytest

from app.storage.validators import ValidationError


def test_create_assigns_sequential_ids(services) -> None:
    first = services.questions.create(_payload("Q one"))
    second = services.questions.create(_payload("Q two"))
    assert first["id"] == "Q0001"
    assert second["id"] == "Q0002"


def _payload(text: str, status: str = "ready") -> dict:
    return {
        "question": text,
        "options": ["A", "B", "C"],
        "correct_option_id": 0,
        "category": "Networking",
        "difficulty": "easy",
        "status": status,
    }


def test_create_rejects_single_option(services) -> None:
    with pytest.raises(ValidationError, match="خيارات على الأقل"):
        services.questions.create({**_payload("bad"), "options": ["only one"]})


def test_create_rejects_out_of_range_correct_answer(services) -> None:
    with pytest.raises(ValidationError, match="خارج نطاق"):
        services.questions.create({**_payload("bad"), "correct_option_id": 7})


def test_create_rejects_duplicate_options(services) -> None:
    with pytest.raises(ValidationError, match="تكرار"):
        services.questions.create({**_payload("bad"), "options": ["A", "A"]})


def test_create_rejects_blank_question(services) -> None:
    with pytest.raises(ValidationError, match="مطلوب"):
        services.questions.create({**_payload(""), "question": "   "})


def test_update_changes_fields_and_timestamp(services) -> None:
    created = services.questions.create(_payload("original"))
    updated = services.questions.update(
        created["id"], {**_payload("changed"), "difficulty": "hard"}
    )
    assert updated["question"] == "changed"
    assert updated["difficulty"] == "hard"


def test_update_missing_question_raises(services) -> None:
    with pytest.raises(ValidationError, match="غير موجود"):
        services.questions.update("Q9999", _payload("x"))


def test_delete_removes_question(services) -> None:
    created = services.questions.create(_payload("to delete"))
    services.questions.delete(created["id"])
    assert services.questions.get(created["id"]) is None


def test_duplicate_creates_draft_copy(services) -> None:
    created = services.questions.create(_payload("source"))
    copy = services.questions.duplicate(created["id"])
    assert copy["id"] != created["id"]
    assert copy["status"] == "draft"
    assert "نسخة" in copy["question"]


def test_search_matches_question_options_and_id(services) -> None:
    services.questions.create(_payload("Which protocol uses port 53?"))
    services.questions.create(_payload("What is the capital of Japan?"))
    assert len(services.questions.list_questions(search="protocol")) == 1
    assert len(services.questions.list_questions(search="capital")) == 1
    assert len(services.questions.list_questions(search="Q0001")) == 1
    assert len(services.questions.list_questions(search="nothing-here")) == 0


def test_filter_by_status_and_category(services) -> None:
    services.questions.create(_payload("a", status="ready"))
    services.questions.create(_payload("b", status="draft"))
    assert len(services.questions.list_questions(status="ready")) == 1
    assert len(services.questions.list_questions(status="draft")) == 1
    assert len(services.questions.list_questions(category="Networking")) == 2
    assert len(services.questions.list_questions(category="Other")) == 0


def test_bulk_delete_and_status(services) -> None:
    a = services.questions.create(_payload("a", status="draft"))
    b = services.questions.create(_payload("b", status="draft"))
    c = services.questions.create(_payload("c", status="draft"))

    assert services.questions.bulk_set_status([a["id"], b["id"]], "ready") == 2
    assert services.questions.get(a["id"])["status"] == "ready"

    assert services.questions.bulk_delete([b["id"], c["id"]]) == 2
    assert len(services.questions.list_questions()) == 1


def test_bulk_status_cannot_unpublish(services) -> None:
    created = services.questions.create(_payload("published"))
    services.questions._storage.questions.update(  # noqa: SLF001
        lambda doc: [q.update({"status": "published"}) for q in doc["questions"]]
    )
    changed = services.questions.bulk_set_status([created["id"]], "draft")
    assert changed == 0
    assert services.questions.get(created["id"])["status"] == "published"


def test_stats_counts_each_state(services) -> None:
    services.questions.create(_payload("a", status="ready"))
    services.questions.create(_payload("b", status="draft"))
    stats = services.questions.stats()
    assert stats["total"] == 2
    assert stats["ready"] == 1
    assert stats["draft"] == 1
    assert stats["published"] == 0


def test_import_skips_duplicates_by_default(services) -> None:
    services.questions.create(_payload("Which protocol uses port 53?"))
    report = services.questions.import_questions(
        [_payload("Which protocol uses port 53?"), _payload("A brand new question")]
    )
    assert report["imported"] == 1
    assert report["skipped"] == 1


def test_import_update_policy_updates_existing(services) -> None:
    created = services.questions.create(_payload("Which protocol uses port 53?"))
    report = services.questions.import_questions(
        [{**_payload("Which protocol uses port 53?"), "explanation": "updated text"}],
        duplicate_policy="update",
    )
    assert report["updated"] == 1
    assert services.questions.get(created["id"])["explanation"] == "updated text"


def test_import_reports_invalid_rows(services) -> None:
    report = services.questions.import_questions(
        [_payload("valid"), {"question": "no options", "options": []}]
    )
    assert report["imported"] == 1
    assert report["failed"] == 1
    assert report["errors"][0]["index"] == 1


def test_import_accepts_exported_option_objects(services) -> None:
    raw = {
        "question": "Object-shaped options",
        "options": [
            {"text": "HTTP", "is_correct": False},
            {"text": "DNS", "is_correct": True},
        ],
    }
    report = services.questions.import_questions([raw])
    assert report["imported"] == 1
    question = services.questions.list_questions(search="Object-shaped")[0]
    assert question["correct_option_id"] == 1


def test_pick_candidates_only_ready_and_excludes_published(services) -> None:
    services.questions.create(_payload("ready one", status="ready"))
    services.questions.create(_payload("draft one", status="draft"))
    published = services.questions.create(_payload("already published", status="ready"))
    services.questions._storage.questions.update(  # noqa: SLF001
        lambda doc: [
            q.update({"status": "published"}) for q in doc["questions"] if q["id"] == published["id"]
        ]
    )
    candidates = services.questions.pick_candidates()
    assert [q["question"] for q in candidates] == ["ready one"]


def test_pick_candidates_random_returns_all(services) -> None:
    for index in range(5):
        services.questions.create(_payload(f"q{index}"))
    assert len(services.questions.pick_candidates(selection="random")) == 5


def test_export_document_shape(services) -> None:
    services.questions.create(_payload("one"))
    document = services.questions.export_document()
    assert document["version"] == 1
    assert len(document["questions"]) == 1
