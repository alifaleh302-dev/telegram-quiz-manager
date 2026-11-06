"""Backup and restore of the application database.

A snapshot is a consistent copy of the database taken while the app is running:
a ``VACUUM INTO`` copy for SQLite, or a JSON export of every collection for
PostgreSQL. The export/import endpoints speak the same ZIP format as before, so
an archive downloaded from an older version (a bag of ``*.json`` files) can
still be restored: those files are merged back through the normal storage layer.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.audit_service import AuditService
from app.storage.base import LEGACY_FILES, StorageBackend
from app.storage.validators import ValidationError

# Kept so an archive from a pre-SQLite version can still be imported.
LEGACY_DATA_FILES = tuple(LEGACY_FILES.values())


class BackupService:
    def __init__(self, storage: StorageBackend, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

    @property
    def _snapshot_filename(self) -> str:
        return self._storage.snapshot_filename

    def list_backups(self) -> list[dict[str, Any]]:
        root = self._storage.backup_dir
        if not root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for directory in sorted(root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            files = [f for f in directory.iterdir() if f.is_file()]
            entries.append(
                {
                    "name": directory.name,
                    "created_at": directory.name,
                    "files": len(files),
                    "size": sum(f.stat().st_size for f in files),
                }
            )
        return entries

    def create_snapshot(self, *, label: str = "", actor: str = "system") -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        name = f"{stamp}-{label}" if label else stamp
        target = self._storage.backup_dir / name
        target.mkdir(parents=True, exist_ok=True)
        self._storage.snapshot(target / self._snapshot_filename)
        self._audit.record(
            "backup.created",
            actor=actor,
            message=f"إنشاء نسخة احتياطية {name}",
        )
        return name

    def export_archive(self) -> bytes:
        """Return a zip archive holding a consistent copy of the database.

        A raw copy of the ``.db`` file would miss whatever still sits in the WAL
        journal, so a snapshot is taken first and *that* consistent file is
        archived.
        """
        buffer = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / self._snapshot_filename
            self._storage.snapshot(snapshot)
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, arcname=self._snapshot_filename)
        return buffer.getvalue()

    def restore_from_archive(
        self, payload: bytes, *, actor: str = "system"
    ) -> dict[str, Any]:
        """Restore from an uploaded archive.

        Accepts either the current format (a ``tqm.db`` inside the zip) or a
        legacy archive of ``*.json`` files, which are merged into the database.
        A snapshot is taken first so the previous state can always be recovered.
        """
        self.create_snapshot(label="pre-restore", actor=actor)

        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
                if self._snapshot_filename in names:
                    self._storage.restore_from_bytes(
                        archive.read(self._snapshot_filename)
                    )
                    restored = [self._snapshot_filename]
                elif "tqm.db" in names:
                    # An archive exported by the SQLite backend.
                    self._storage.restore_from_bytes(archive.read("tqm.db"))
                    restored = ["tqm.db"]
                else:
                    restored = self._restore_legacy(archive, names)
        except zipfile.BadZipFile as exc:
            raise ValidationError("الملف المرفوع ليس أرشيف ZIP صالحاً.") from exc

        if not restored:
            raise ValidationError("لم يُعثر على أي ملف بيانات معروف داخل الأرشيف.")

        self._audit.record(
            "backup.restored",
            actor=actor,
            level="warning",
            message=f"استعادة {len(restored)} ملف من نسخة احتياطية",
        )
        return {"restored": restored}

    def _restore_legacy(self, archive: zipfile.ZipFile, names: set[str]) -> list[str]:
        """Merge a pre-SQLite archive of JSON files into the database."""
        stores = self._storage.all_stores()
        by_filename = {filename: name for name, filename in LEGACY_FILES.items()}
        restored: list[str] = []
        for filename in LEGACY_DATA_FILES:
            if filename not in names:
                continue
            try:
                parsed = json.loads(archive.read(filename).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(
                    f"أحد الملفات داخل الأرشيف ليس JSON صالحاً: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValidationError(f"{filename} يجب أن يحتوي على كائن JSON.")
            stores[by_filename[filename]].write(parsed)
            restored.append(filename)
        return restored

    def restore_snapshot(self, name: str, *, actor: str = "system") -> dict[str, Any]:
        source = self._storage.backup_dir / name
        if not source.is_dir():
            raise ValidationError("النسخة الاحتياطية المطلوبة غير موجودة.")
        database = source / self._snapshot_filename
        if not database.exists():
            # A snapshot written by the SQLite backend.
            legacy = source / "tqm.db"
            if legacy.exists():
                database = legacy
            else:
                raise ValidationError(
                    "النسخة الاحتياطية لا تحتوي على قاعدة بيانات صالحة."
                )

        self.create_snapshot(label="pre-restore", actor=actor)
        self._storage.restore_from_path(database, actor=actor)
        self._audit.record(
            "backup.restored",
            actor=actor,
            level="warning",
            message=f"استعادة النسخة {name}",
        )
        return {"restored": [database.name], "source": name}

    def prune(self, keep: int = 30) -> int:
        root = self._storage.backup_dir
        if not root.exists():
            return 0
        directories = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
        removed = 0
        for directory in directories[:-keep] if keep > 0 else directories:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
        return removed
