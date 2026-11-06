"""PostgreSQL backend: same document contract as SQLite, plus row-level writes.

These tests need a real PostgreSQL server. Set ``TQM_TEST_DATABASE_URL`` to run
them; each test gets its own schema so runs never collide:

    TQM_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/tqm \
        python -m pytest tests/test_postgres_storage.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage.base import StorageError  # noqa: E402

DATABASE_URL = os.environ.get("TQM_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TQM_TEST_DATABASE_URL is not set"
)


def _storage(tmp_path: Path, schema: str):
    from app.storage.postgres_manager import Storage

    return Storage(
        tmp_path / "data",
        tmp_path / "data" / "backups",
        database_url=DATABASE_URL,
        schema=schema,
    )


def _drop_schema(schema: str) -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL, autocommit=True) as con:
        with con.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def storage(tmp_path: Path, request: pytest.FixtureRequest):
    schema = f"tqm_pgtest_{request.node.name[:24]}"
    _drop_schema(schema)  # a previous run must not leak into this one
    backend = _storage(tmp_path, schema)
    yield backend
    backend.close()
    _drop_schema(schema)


def test_document_roundtrip(storage) -> None:
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q1", "question": "؟"}]}
    )
    document = storage.questions.read()
    assert document["counter"] == 1
    assert document["questions"] == [{"id": "Q1", "question": "؟"}]


def test_order_is_preserved(storage) -> None:
    ids = [f"Q{i:03d}" for i in range(25)]
    storage.questions.write(
        {"version": 1, "counter": 25, "questions": [{"id": i} for i in ids]}
    )
    assert [q["id"] for q in storage.questions.read()["questions"]] == ids


def test_update_only_touches_changed_rows(storage) -> None:
    storage.students.write(
        {
            "version": 1,
            "counter": 0,
            "students": [{"id": f"S{i}", "n": i} for i in range(5)],
        }
    )
    storage.students.update(lambda doc: doc["students"].append({"id": "S9", "n": 9}))
    storage.students.update(lambda doc: doc["students"].pop(0))
    storage.students.update(lambda doc: doc["students"][0].__setitem__("n", 100))

    rows = storage.students.read()["students"]
    assert [r["id"] for r in rows] == ["S1", "S2", "S3", "S4", "S9"]
    assert rows[0]["n"] == 100


def test_scalar_only_collection_keeps_lists_in_the_document(storage) -> None:
    storage.settings.write({"version": 1, "timezone": "Europe/Paris", "state": {"c": 5}})
    document = storage.settings.read()
    assert document["timezone"] == "Europe/Paris"
    assert document["state"] == {"c": 5}


def test_collections_are_isolated(storage) -> None:
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q1"}]})
    storage.channels.write({"version": 1, "counter": 1, "channels": [{"id": "CH1"}]})
    assert storage.questions.read()["questions"] == [{"id": "Q1"}]
    assert storage.channels.read()["channels"] == [{"id": "CH1"}]


def test_nested_updates_join_one_transaction(storage) -> None:
    storage.questions.write({"version": 1, "counter": 0, "questions": []})

    def outer(document: dict) -> None:
        document["questions"].append({"id": "Q1"})
        storage.channels.update(lambda doc: doc["channels"].append({"id": "CH1"}))

    storage.questions.update(outer)
    assert storage.questions.read()["questions"] == [{"id": "Q1"}]
    assert storage.channels.read()["channels"] == [{"id": "CH1"}]


def test_nested_failure_rolls_back_everything(storage) -> None:
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q1"}]})

    def outer(document: dict) -> None:
        storage.channels.update(lambda doc: doc["channels"].append({"id": "CH1"}))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        storage.questions.update(outer)

    assert storage.questions.read()["questions"] == [{"id": "Q1"}]
    assert storage.channels.read()["channels"] == []


def test_snapshot_and_restore_roundtrip(storage, tmp_path: Path) -> None:
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q1", "question": "keep"}]}
    )
    snapshot = tmp_path / "snap.json"
    storage.snapshot(snapshot)
    assert snapshot.exists()

    storage.questions.write({"version": 1, "counter": 0, "questions": []})
    assert storage.questions.read()["questions"] == []

    storage.restore_from_path(snapshot)
    assert storage.questions.read()["questions"] == [{"id": "Q1", "question": "keep"}]


def test_restore_rejects_garbage(storage) -> None:
    storage.questions.write({"version": 1, "counter": 1, "questions": [{"id": "Q1"}]})
    with pytest.raises(StorageError):
        storage.restore_from_bytes(b"not json at all")
    assert storage.questions.read()["questions"] == [{"id": "Q1"}]


def test_exposes_the_same_collections_as_sqlite(storage, tmp_path: Path) -> None:
    """Both backends must build the same collection set from the shared defaults."""
    from app.storage.sqlite_manager import Storage as SqliteStorage

    sqlite = SqliteStorage(tmp_path / "s", tmp_path / "s" / "backups")
    try:
        assert set(storage.all_stores()) == set(sqlite.all_stores())
        # Same default document per collection.
        for name in storage.all_stores():
            assert storage.store(name, {}).read() == sqlite.store(name, {}).read()
    finally:
        sqlite.close()


def test_import_from_sqlite_backend(storage, tmp_path: Path) -> None:
    """A fresh Postgres database can be seeded from an existing SQLite one."""
    from app.storage.sqlite_manager import Storage as SqliteStorage

    legacy = SqliteStorage(tmp_path / "old", tmp_path / "old" / "backups")
    try:
        legacy.questions.write(
            {"version": 1, "counter": 1, "questions": [{"id": "Q1", "question": "legacy"}]}
        )
        legacy.settings.write({"version": 1, "timezone": "Europe/Paris"})

        imported = storage.import_from_backend(legacy)
    finally:
        legacy.close()

    assert "questions" in imported and "settings" in imported
    assert storage.questions.read()["questions"] == [{"id": "Q1", "question": "legacy"}]
    assert storage.settings.read()["timezone"] == "Europe/Paris"

    # A second import must not overwrite the now-populated database.
    legacy2 = SqliteStorage(tmp_path / "old2", tmp_path / "old2" / "backups")
    try:
        legacy2.questions.write(
            {"version": 1, "counter": 9, "questions": [{"id": "Q9", "question": "newer"}]}
        )
        assert storage.import_from_backend(legacy2) == []
    finally:
        legacy2.close()
    assert storage.questions.read()["questions"][0]["id"] == "Q1"


def test_factory_selects_postgres_and_imports_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """`build_storage` picks Postgres when DATABASE_URL is set and seeds it."""
    from app.core.config import get_settings
    from app.storage.factory import build_storage
    from app.storage.sqlite_manager import Storage as SqliteStorage

    data_dir = tmp_path / "data"
    legacy = SqliteStorage(data_dir, data_dir / "backups")
    try:
        legacy.questions.write(
            {"version": 1, "counter": 1, "questions": [{"id": "Q1", "question": "old"}]}
        )
    finally:
        legacy.close()

    schema = f"tqm_pgtest_{request.node.name[:20]}"
    _drop_schema(schema)
    monkeypatch.setenv("APP_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("DB_SCHEMA", schema)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    storage = build_storage(get_settings())
    try:
        assert storage.backend_name == "postgres"
        assert storage.questions.read()["questions"] == [{"id": "Q1", "question": "old"}]
    finally:
        storage.close()
        get_settings.cache_clear()


def test_missing_database_url_is_rejected(tmp_path: Path) -> None:
    from app.storage.postgres_manager import Storage

    with pytest.raises(StorageError, match="DATABASE_URL"):
        Storage(tmp_path / "data", tmp_path / "data" / "backups", database_url="")
