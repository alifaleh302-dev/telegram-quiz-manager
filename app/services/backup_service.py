"""Backup and restore of the JSON data files."""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.services.audit_service import AuditService
from app.storage.json_manager import Storage
from app.storage.validators import ValidationError

DATA_FILES = (
    "questions.json",
    "channels.json",
    "schedules.json",
    "publishing_history.json",
    "settings.json",
    "users.json",
    "audit_log.json",
)


class BackupService:
    def __init__(self, storage: Storage, audit: AuditService) -> None:
        self._storage = storage
        self._audit = audit

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
        copied = 0
        for filename in DATA_FILES:
            source = self._storage.data_dir / filename
            if source.exists():
                shutil.copy2(source, target / filename)
                copied += 1
        self._audit.record(
            "backup.created", actor=actor, message=f"إنشاء نسخة احتياطية {name} ({copied} ملف)"
        )
        return name

    def export_archive(self) -> bytes:
        """Return a zip archive of every data file, for download."""
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename in DATA_FILES:
                path = self._storage.data_dir / filename
                if path.exists():
                    archive.write(path, arcname=filename)
        return buffer.getvalue()

    def restore_from_archive(
        self, payload: bytes, *, actor: str = "system"
    ) -> dict[str, Any]:
        """Replace data files with the ones found in an uploaded archive.

        A snapshot is taken first so the previous state can always be recovered.
        """
        import io

        self.create_snapshot(label="pre-restore", actor=actor)
        restored: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
                for filename in DATA_FILES:
                    if filename not in names:
                        continue
                    raw = archive.read(filename).decode("utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValidationError(f"{filename} يجب أن يحتوي على كائن JSON.")
                    (self._storage.data_dir / filename).write_text(
                        json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    restored.append(filename)
        except zipfile.BadZipFile as exc:
            raise ValidationError("الملف المرفوع ليس أرشيف ZIP صالحاً.") from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(f"أحد الملفات داخل الأرشيف ليس JSON صالحاً: {exc}") from exc

        if not restored:
            raise ValidationError("لم يُعثر على أي ملف بيانات معروف داخل الأرشيف.")

        self._audit.record(
            "backup.restored",
            actor=actor,
            level="warning",
            message=f"استعادة {len(restored)} ملف من نسخة احتياطية",
        )
        return {"restored": restored}

    def restore_snapshot(self, name: str, *, actor: str = "system") -> dict[str, Any]:
        source = self._storage.backup_dir / name
        if not source.is_dir():
            raise ValidationError("النسخة الاحتياطية المطلوبة غير موجودة.")
        self.create_snapshot(label="pre-restore", actor=actor)
        restored: list[str] = []
        for filename in DATA_FILES:
            candidate = source / filename
            if candidate.exists():
                shutil.copy2(candidate, self._storage.data_dir / filename)
                restored.append(filename)
        self._audit.record(
            "backup.restored",
            actor=actor,
            level="warning",
            message=f"استعادة النسخة {name} ({len(restored)} ملف)",
        )
        return {"restored": restored, "source": name}

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
