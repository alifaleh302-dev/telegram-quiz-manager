"""Storage tests: atomic writes, backups, locking and corruption handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.json_manager import JsonStore, Storage, StorageError


def test_read_returns_default_when_file_absent(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "x.json", {"version": 1, "items": []})
    assert store.read() == {"version": 1, "items": []}


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "x.json", {"version": 1, "items": []})
    store.write({"version": 1, "items": [1, 2, 3]})
    assert store.read()["items"] == [1, 2, 3]


def test_read_is_a_deep_copy(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "x.json", {"version": 1, "items": []})
    store.write({"version": 1, "items": [{"id": 1}]})
    document = store.read()
    document["items"][0]["id"] = 999
    assert store.read()["items"][0]["id"] == 1


def test_update_persists_mutation_and_returns_result(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "x.json", {"version": 1, "counter": 0, "items": []})

    def mutate(document: dict) -> int:
        document["counter"] += 1
        document["items"].append("a")
        return document["counter"]

    assert store.update(mutate) == 1
    assert store.read()["counter"] == 1
    assert store.read()["items"] == ["a"]


def test_backup_created_before_overwrite(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    store = JsonStore(tmp_path / "x.json", {"v": 0}, backup_dir=backup_dir, keep_backups=5, backup_min_interval=0)
    store.write({"v": 1})
    # first write: nothing existed to back up
    assert not backup_dir.exists() or not list(backup_dir.iterdir())
    store.write({"v": 2})
    snapshots = list(backup_dir.iterdir())
    assert len(snapshots) == 1
    assert json.loads((snapshots[0] / "x.json").read_text())["v"] == 1


def test_backup_pruning_keeps_only_the_limit(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    store = JsonStore(tmp_path / "x.json", {"v": 0}, backup_dir=backup_dir, keep_backups=2, backup_min_interval=0)
    for value in range(6):
        store.write({"v": value})
    assert len(list(backup_dir.iterdir())) == 2


def test_corrupted_file_raises_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    store = JsonStore(path, {"v": 0})
    with pytest.raises(StorageError, match="not valid JSON"):
        store.read()


def test_non_object_document_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StorageError, match="JSON object"):
        JsonStore(path, {"v": 0}).read()


def test_storage_exposes_all_collections(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    assert set(storage.all_stores()) == {
        "questions", "channels", "schedules", "history", "settings", "users",
        "secrets", "audit_log", "poll_results", "poll_answers", "userbot_accounts",
    }


def test_writes_leave_no_temp_files(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "x.json", {"v": 0})
    for value in range(5):
        store.write({"v": value})
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
