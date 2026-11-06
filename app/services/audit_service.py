"""Audit log: a compact, append-only record of meaningful actions."""

from __future__ import annotations

from typing import Any

from app.storage.base import StorageBackend as Storage, utc_now_iso

MAX_ENTRIES = 500


class AuditService:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def record(
        self,
        action: str,
        *,
        level: str = "info",
        message: str = "",
        actor: str = "system",
        **extra: Any,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": utc_now_iso(),
            "action": action,
            "level": level,
            "message": message,
            "actor": actor,
            **extra,
        }

        def _mutate(document: dict[str, Any]) -> None:
            document["entries"].insert(0, entry)
            del document["entries"][MAX_ENTRIES:]

        self._storage.audit.update(_mutate)
        return entry

    def recent(self, limit: int = 20, level: str | None = None) -> list[dict[str, Any]]:
        entries = self._storage.audit.read()["entries"]
        if level:
            entries = [e for e in entries if e.get("level") == level]
        return entries[:limit]
