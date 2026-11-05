"""Storage tests: SQLite transactions, order preservation, backups and migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.storage.sqlite_manager import (
    Database,
    SqliteStore,
    Storage,
    StorageError,
    utc_now_iso,
)


def _store(tmp_path: Path, default: dict | None = None) -> SqliteStore:
    database = Database(tmp_path / "tqm.db")
    return SqliteStore(database, "questions", default or {"version": 1, "counter": 0, "questions": []})


def test_read_returns_default_when_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read() == {"version": 1, "counter": 0, "questions": []}


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        {
            "version": 1,
            "counter": 2,
            "questions": [{"id": "Q0001", "question": "a"}, {"id": "Q0002", "question": "b"}],
        }
    )
    document = store.read()
    assert document["counter"] == 2
    assert [q["id"] for q in document["questions"]] == ["Q0001", "Q0002"]


def test_read_is_a_deep_copy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "a"}]})
    document = store.read()
    document["questions"][0]["question"] = "mutated"
    assert store.read()["questions"][0]["question"] == "a"


def test_update_persists_mutation_and_returns_result(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def mutate(document: dict) -> int:
        document["counter"] += 1
        document["questions"].append({"id": f"Q{document['counter']:04d}"})
        return document["counter"]

    assert store.update(mutate) == 1
    assert store.read()["counter"] == 1
    assert store.read()["questions"] == [{"id": "Q0001"}]


def test_update_rolls_back_on_exception(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})

    def mutate(document: dict) -> None:
        document["counter"] = 99
        document["questions"].clear()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.update(mutate)

    # The failed transaction must leave the previous state untouched.
    assert store.read()["counter"] == 1
    assert store.read()["questions"] == [{"id": "Q0001"}]


def test_row_order_is_preserved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = [f"Q{i:04d}" for i in range(30)]
    store.write({"version": 1, "counter": 30, "questions": [{"id": i} for i in ids]})
    assert [q["id"] for q in store.read()["questions"]] == ids


def test_scalar_only_collection_keeps_lists_in_the_document(tmp_path: Path) -> None:
    database = Database(tmp_path / "tqm.db")
    store = SqliteStore(database, "settings", {"version": 1, "state": {}})
    store.write({"version": 1, "timezone": "Europe/Paris", "state": {"cursor": 5}})
    document = store.read()
    assert document["timezone"] == "Europe/Paris"
    assert document["state"] == {"cursor": 5}


def test_storage_exposes_all_collections(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    assert set(storage.all_stores()) == {
        "questions", "channels", "schedules", "history", "settings", "users",
        "secrets", "audit_log", "poll_results", "poll_answers", "userbot_accounts",
        # Exam platform collections (added with the student portal).
        "students", "exams", "exam_questions", "attempts", "attempt_answers",
        "student_exam_state", "notifications", "exam_admin_logs",
    }


def test_collections_are_isolated(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})
    storage.channels.write({"version": 1, "counter": 1, "channels": [{"id": "CH001"}]})
    # Writing channels must not disturb questions.
    assert storage.questions.read()["questions"] == [{"id": "Q0001"}]
    assert storage.channels.read()["channels"] == [{"id": "CH001"}]


def test_database_file_is_created_and_is_sqlite(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})
    path = storage.db.path
    assert path.exists()
    # A real SQLite file starts with the format header.
    assert path.read_bytes()[:16] == b"SQLite format 3\x00"


def test_snapshot_and_restore_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "keep"}]}
    )
    snapshot = tmp_path / "snap.db"
    storage.db.snapshot(snapshot)

    storage.questions.write({"version": 1, "counter": 0, "questions": []})
    assert storage.questions.read()["questions"] == []

    storage.db.restore_from_path(snapshot)
    assert storage.questions.read()["questions"] == [{"id": "Q0001", "question": "keep"}]


def test_restore_rejects_a_non_sqlite_file(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})
    with pytest.raises(StorageError, match="قاعدة بيانات"):
        storage.db.restore_from_bytes(b"this is not a database at all")
    # The live database must survive a rejected restore.
    assert storage.questions.read()["questions"] == [{"id": "Q0001"}]


def test_migration_imports_legacy_json_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "questions.json").write_text(
        json.dumps(
            {"version": 1, "counter": 3, "questions": [{"id": "Q0003", "question": "legacy"}]}
        ),
        encoding="utf-8",
    )
    (data_dir / "settings.json").write_text(
        json.dumps({"version": 1, "timezone": "Europe/Paris"}), encoding="utf-8"
    )

    storage = Storage(data_dir, data_dir / "backups")
    assert storage.questions.read()["questions"] == [{"id": "Q0003", "question": "legacy"}]
    assert storage.questions.read()["counter"] == 3
    assert storage.settings.read()["timezone"] == "Europe/Paris"


def test_migration_does_not_overwrite_existing_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "questions.json").write_text(
        json.dumps({"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "old"}]}),
        encoding="utf-8",
    )
    first = Storage(data_dir, data_dir / "backups")
    first.questions.write(
        {"version": 1, "counter": 9, "questions": [{"id": "Q0009", "question": "new"}]}
    )
    # A second open (a restart) must not re-import the stale file over live data.
    second = Storage(data_dir, data_dir / "backups")
    assert second.questions.read()["questions"] == [{"id": "Q0009", "question": "new"}]


def test_migration_skips_a_corrupt_legacy_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "questions.json").write_text("{not json", encoding="utf-8")
    # A corrupt legacy file must not stop the app from starting.
    storage = Storage(data_dir, data_dir / "backups")
    assert storage.questions.read()["questions"] == []


def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    storage = Storage(data_dir, data_dir / "backups")
    for value in range(5):
        storage.questions.write({"version": 1, "counter": value, "questions": []})
    leftovers = [p for p in data_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_utc_now_iso_is_zulu() -> None:
    assert utc_now_iso().endswith("Z")


def test_nested_updates_join_one_transaction(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write({"version": 1, "counter": 0, "questions": []})

    def outer(document: dict) -> None:
        document["questions"].append({"id": "Q0001"})
        # A nested update on another collection must not deadlock or fail.
        storage.channels.update(lambda doc: doc["channels"].append({"id": "CH001"}))

    storage.questions.update(outer)
    assert storage.questions.read()["questions"] == [{"id": "Q0001"}]
    assert storage.channels.read()["channels"] == [{"id": "CH001"}]


def test_nested_failure_rolls_back_everything(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})

    def outer(document: dict) -> None:
        storage.channels.update(lambda doc: doc["channels"].append({"id": "CH001"}))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        storage.questions.update(outer)

    # Neither the outer collection nor the nested one may keep a partial write.
    assert storage.questions.read()["questions"] == [{"id": "Q0001"}]
    assert storage.channels.read()["channels"] == []


def test_row_ids_are_stored_verbatim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write({"version": 1, "counter": 1, "questions": [{"id": "Q0001"}]})
    with sqlite3.connect(store._db.path) as conn:  # noqa: SLF001 - inspecting storage
        row = conn.execute("SELECT id FROM questions").fetchone()
    assert row[0] == "Q0001"
