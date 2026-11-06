"""Storage abstraction shared by the SQLite and PostgreSQL backends.

Every collection is a *document*: a plain dict with scalar fields plus, for
list-bearing collections, one list of row dicts. Services only ever call
:meth:`read`, :meth:`update` and :meth:`write` with those dicts, so swapping the
backend underneath is invisible to the rest of the application.

Two implementations satisfy this contract:

* :class:`app.storage.sqlite_manager.Storage` — one local file, a single
  connection serialised by a re-entrant lock.
* :class:`app.storage.postgres_manager.Storage` — a connection pool, one row
  per collection element, with row-level writes instead of whole-collection
  rewrites.

The metadata below (which collections have row tables, what their list key is,
and which legacy JSON file they came from) is backend-independent and lives
here so both backends agree.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StorageError(Exception):
    """Raised when the database cannot be read or written safely."""


# The tables that hold one row per list element. Collections absent from this
# map (settings, users, secrets) keep their list — if any — inside the document.
ROWS_TABLES: dict[str, str] = {
    "questions": "questions",
    "channels": "channels",
    "schedules": "schedules",
    "history": "history",
    "audit_log": "audit_log",
    "poll_results": "poll_results",
    "poll_answers": "poll_answers",
    "userbot_accounts": "userbot_accounts",
    # Exam platform (students, exams, attempts). New collections are additive:
    # their tables are created on open, so no migration step is needed and the
    # existing collections are untouched.
    "students": "students",
    "exams": "exams",
    "exam_questions": "exam_questions",
    "attempts": "attempts",
    "attempt_answers": "attempt_answers",
    "student_exam_state": "student_exam_state",
    "notifications": "notifications",
    "exam_admin_logs": "exam_admin_logs",
    # Live activity tracking: one row per student, holding the last moment they
    # interacted with the Mini App (kept small, swept after a TTL).
    "student_activity": "student_activity",
}

# The key of the list inside each collection's document.
ROWS_KEYS: dict[str, str] = {
    "questions": "questions",
    "channels": "channels",
    "schedules": "schedules",
    "history": "history",
    "audit_log": "entries",
    "poll_results": "results",
    "poll_answers": "answers",
    "userbot_accounts": "accounts",
    "students": "students",
    "exams": "exams",
    "exam_questions": "exam_questions",
    "attempts": "attempts",
    "attempt_answers": "attempt_answers",
    "student_exam_state": "states",
    "notifications": "notifications",
    "exam_admin_logs": "entries",
    "student_activity": "activity",
}

# The JSON file each collection used to live in, for one-time migration.
LEGACY_FILES: dict[str, str] = {
    "questions": "questions.json",
    "channels": "channels.json",
    "schedules": "schedules.json",
    "history": "publishing_history.json",
    "settings": "settings.json",
    "users": "users.json",
    "secrets": "secrets.json",
    "audit_log": "audit_log.json",
    "poll_results": "poll_results.json",
    "poll_answers": "poll_answers.json",
    "userbot_accounts": "userbot_accounts.json",
}

# Every collection the application uses, in a stable order.
COLLECTION_NAMES: tuple[str, ...] = (
    "questions",
    "channels",
    "schedules",
    "history",
    "settings",
    "users",
    "secrets",
    "audit_log",
    "poll_results",
    "poll_answers",
    "userbot_accounts",
    "students",
    "exams",
    "exam_questions",
    "attempts",
    "attempt_answers",
    "student_exam_state",
    "notifications",
    "exam_admin_logs",
    "student_activity",
)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def checked_table(name: str) -> str:
    """Reject a table name that could not be safely interpolated into SQL.

    Table names come from the constants above, never from user input, but the
    check keeps a future refactor from quietly turning that into an injection.
    """
    if not _IDENTIFIER_RE.match(name):
        raise StorageError(f"unsafe table name: {name}")
    return name


def default_settings_document() -> dict[str, Any]:
    """The settings document a fresh database starts with."""
    from app.core.messages import DEFAULTS as DEFAULT_MESSAGES

    return {
        "version": 1,
        "timezone": "Asia/Riyadh",
        "publishing_enabled": True,
        "poll_options": {
            "explanation": True,
            "open_period": 0,
            "anonymous": True,
            "allows_multiple_answers": False,
        },
        "retry": {"max_attempts": 3, "delay_seconds": 60},
        "pacing": {"interval_seconds": 4, "max_wait_seconds": 120},
        "daily_limit": 0,
        "analytics": {"auto_collect": False, "auto_collect_hours": 6},
        "transport": {"default_mode": "bot"},
        "notifications": {
            "on_publish": True,
            "types": {
                "exam_published": True,
                "exam_upcoming": True,
                "exam_closing_soon": True,
                "attempt_completed": True,
                "result_available": True,
            },
        },
        "messages": dict(DEFAULT_MESSAGES),
        "paused_schedules": [],
        "state": {},
    }


def default_secrets_document() -> dict[str, Any]:
    """The secrets document a fresh database starts with (all values empty)."""
    return {
        "version": 1,
        "telegram_bot_token": None,
        "telegram_bot_token_updated_at": None,
        "userbot": {},
    }


def default_collections() -> dict[str, dict[str, Any]]:
    """Default document for every collection, keyed by attribute name.

    Both backends build their stores from this one table so the two can never
    drift; each key is also the attribute the backend exposes
    (``storage.questions``, ``storage.audit``, …). The collection *name* stored
    on disk is the key except for ``audit``, which is the ``audit_log``
    collection.
    """
    return {
        "questions": {"version": 1, "counter": 0, "questions": []},
        "channels": {"version": 1, "counter": 0, "channels": []},
        "schedules": {"version": 1, "counter": 0, "schedules": []},
        "history": {"version": 1, "counter": 0, "history": []},
        "settings": default_settings_document(),
        "users": {"version": 1, "users": []},
        "secrets": default_secrets_document(),
        "audit": {"version": 1, "entries": []},
        "poll_results": {"version": 1, "counter": 0, "results": []},
        "poll_answers": {"version": 1, "counter": 0, "answers": []},
        "userbot_accounts": {"version": 1, "counter": 0, "accounts": []},
        # Exam platform.
        "students": {"version": 1, "counter": 0, "students": []},
        "exams": {"version": 1, "counter": 0, "exams": []},
        "exam_questions": {"version": 1, "counter": 0, "exam_questions": []},
        "attempts": {"version": 1, "counter": 0, "attempts": []},
        "attempt_answers": {"version": 1, "counter": 0, "attempt_answers": []},
        "student_exam_state": {"version": 1, "counter": 0, "states": []},
        "notifications": {"version": 1, "counter": 0, "notifications": []},
        "exam_admin_logs": {"version": 1, "counter": 0, "entries": []},
        "student_activity": {"version": 1, "counter": 0, "activity": []},
    }


# The attribute name -> collection name exceptions (attribute == collection name
# for everything else).
COLLECTION_NAMES_BY_ATTRIBUTE: dict[str, str] = {"audit": "audit_log"}


def collection_name_for(attribute: str) -> str:
    return COLLECTION_NAMES_BY_ATTRIBUTE.get(attribute, attribute)


class CollectionStore(ABC):
    """One collection, exposing the document read/update/write contract."""

    name: str

    @abstractmethod
    def read(self) -> dict[str, Any]:
        """Return the whole collection document."""

    @abstractmethod
    def update(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        """Read-modify-write inside one transaction; returns the mutator result."""

    @abstractmethod
    def write(self, document: dict[str, Any]) -> None:
        """Replace the whole collection with ``document``."""

    @abstractmethod
    def is_empty(self) -> bool:
        """Whether the collection holds no document and no rows."""


class StorageBackend(ABC):
    """Owns the database and every collection used by the application."""

    backend_name: str = "abstract"
    # Name of the file a snapshot is written as (inside a backup directory or
    # archive). Kept per-backend so a Postgres dump is not mistaken for a file.
    snapshot_filename: str = "tqm.db"

    def __init__(self, data_dir: Path, backup_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, CollectionStore] = {}

    def store(self, name: str, default: dict[str, Any]) -> CollectionStore:
        """Return the store for a collection, creating it on first use."""
        existing = self._stores.get(name)
        if existing is not None:
            return existing
        created = self._make_store(name, default)
        self._stores[name] = created
        return created

    @abstractmethod
    def _make_store(self, name: str, default: dict[str, Any]) -> CollectionStore:
        """Build a new collection store (called once per name)."""

    @abstractmethod
    def close(self) -> None:
        """Release every connection."""

    def all_stores(self) -> dict[str, CollectionStore]:
        return dict(self._stores)

    def is_empty(self) -> bool:
        """Whether *every* collection is empty (a fresh, never-written database)."""
        return all(store.is_empty() for store in self._stores.values())

    def import_from_backend(self, source: StorageBackend) -> list[str]:
        """Copy every collection from ``source`` into this (empty) backend.

        Used once when switching an existing deployment from SQLite to
        PostgreSQL: the old ``tqm.db`` is read and its collections are written
        into the fresh database, so no data is lost. Only empty collections are
        touched, so this is a no-op on a database that already holds data and a
        restart never overwrites live rows.
        """
        imported: list[str] = []
        for name in COLLECTION_NAMES:
            target = self.store(name, {})
            if not target.is_empty():
                continue
            target.write(source.store(name, {}).read())
            imported.append(name)
        return imported

    # --- backup / restore --------------------------------------------------
    def snapshot(self, target: Path) -> None:
        """Write a consistent copy of the database to ``target``."""
        raise StorageError(
            f"backend {self.backend_name!r} cannot produce a file snapshot"
        )

    def restore_from_path(self, source: Path, *, actor: str = "system") -> None:
        self.restore_from_bytes(Path(source).read_bytes())

    def restore_from_bytes(self, payload: bytes) -> None:
        raise StorageError(
            f"backend {self.backend_name!r} cannot restore a file snapshot"
        )

    # --- one-time JSON migration -------------------------------------------
    def migrate_from_json(self) -> list[str]:
        """Import legacy ``data/*.json`` files into empty collections.

        Only collections that are still empty are touched, so running this on a
        database that already holds data is a no-op and the legacy files are
        never re-imported over live changes.
        """
        import json

        from app.core.logging import get_logger

        logger = get_logger(__name__)
        migrated: list[str] = []
        for name in COLLECTION_NAMES:
            filename = LEGACY_FILES.get(name)
            if not filename:
                continue
            path = self.data_dir / filename
            store = self.store(name, {})
            if not path.exists() or not store.is_empty():
                continue
            try:
                raw = path.read_text(encoding="utf-8").strip()
                document = json.loads(raw) if raw else None
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("could not import %s: %s", filename, exc)
                continue
            if not isinstance(document, dict):
                logger.warning("skipping %s: not a JSON object", filename)
                continue
            store.write(document)
            migrated.append(filename)
        if migrated:
            logger.info("migrated %s legacy JSON file(s)", len(migrated))
        return migrated
