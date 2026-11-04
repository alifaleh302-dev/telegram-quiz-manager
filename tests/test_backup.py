"""Backup and restore of the SQLite database."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services.audit_service import AuditService
from app.services.backup_service import BackupService
from app.storage.sqlite_manager import Storage
from app.storage.validators import ValidationError


@pytest.fixture
def backup(tmp_path: Path) -> tuple[BackupService, Storage]:
    storage = Storage(tmp_path / "data", tmp_path / "data" / "backups")
    service = BackupService(storage, AuditService(storage))
    return service, storage


def test_snapshot_and_restore_roundtrip(backup) -> None:
    service, storage = backup
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "keep"}]}
    )
    name = service.create_snapshot(label="test")
    assert service.list_backups()[0]["name"] == name

    storage.questions.write({"version": 1, "counter": 0, "questions": []})
    service.restore_snapshot(name)
    assert storage.questions.read()["questions"] == [{"id": "Q0001", "question": "keep"}]


def test_export_archive_carries_data(backup) -> None:
    """The archive must include writes still sitting in the WAL journal."""
    service, storage = backup
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "fresh"}]}
    )
    archive = service.export_archive()
    assert len(archive) > 500, "a 4 KB empty database means the data was not included"

    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        assert "tqm.db" in zipped.namelist()
        payload = zipped.read("tqm.db")
    assert b"fresh" in payload


def test_restore_from_archive_replaces_database(backup, tmp_path: Path) -> None:
    service, storage = backup
    storage.questions.write(
        {"version": 1, "counter": 1, "questions": [{"id": "Q0001", "question": "keep"}]}
    )
    archive = service.export_archive()

    storage.questions.write({"version": 1, "counter": 0, "questions": []})
    service.restore_from_archive(archive)
    assert storage.questions.read()["questions"] == [{"id": "Q0001", "question": "keep"}]


def test_restore_from_archive_rejects_bad_zip(backup) -> None:
    service, _ = backup
    with pytest.raises(ValidationError, match="ZIP"):
        service.restore_from_archive(b"not a zip file")


def test_restore_from_archive_rejects_empty_zip(backup) -> None:
    service, _ = backup
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("readme.txt", "nothing useful")
    with pytest.raises(ValidationError, match="ملف بيانات"):
        service.restore_from_archive(buffer.getvalue())


def test_restore_legacy_json_archive(backup) -> None:
    """An archive downloaded before the SQLite migration must still restore."""
    service, storage = backup
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(
            "questions.json",
            json.dumps(
                {"version": 1, "counter": 2, "questions": [{"id": "Q0002", "question": "legacy"}]}
            ),
        )
        zipped.writestr("settings.json", json.dumps({"version": 1, "timezone": "Europe/Paris"}))

    result = service.restore_from_archive(buffer.getvalue())
    assert "questions.json" in result["restored"]
    assert storage.questions.read()["questions"] == [{"id": "Q0002", "question": "legacy"}]
    assert storage.settings.read()["timezone"] == "Europe/Paris"


def test_restore_missing_snapshot_raises(backup) -> None:
    service, _ = backup
    with pytest.raises(ValidationError, match="غير موجودة"):
        service.restore_snapshot("does-not-exist")
