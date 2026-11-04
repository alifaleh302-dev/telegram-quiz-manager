"""SQLite storage: one database file, atomic transactions, snapshot backups.

This replaces the per-collection JSON files. Every logical collection is kept
in the *same* :class:`Database` file but in its own table, so a write to one
collection never rewrites another, and SQLite's own journal guarantees that a
crash mid-write cannot leave a half-written file behind (the failure mode the
old ``.tmp`` + ``os.replace`` dance had to work around by hand).

Layout
------
* ``documents`` — one row per collection holding the scalar part of the
  document (``version``, ``counter``, ``timezone``, ...) as JSON.
* one row-table per list-bearing collection (``questions``, ``history``, ...)
  holding each element as a JSON ``value`` with an ``idx`` preserving order.

Why split scalars from list rows instead of storing one big JSON blob? So a
write to a scalar-only collection (``settings``, ``secrets``, ``users``) never
touches the row tables, and so every row is queryable with SQL later without
another migration. A write to a row collection still rewrites that collection's
rows inside one transaction — the same work the JSON layer did, but atomic.

Concurrency
-----------
The app is single-process. One connection is opened with
``check_same_thread=False`` and *all* access is serialised through one
re-entrant lock, so two threads can never interleave a read-modify-write. Writes
run inside an explicit ``BEGIN IMMEDIATE`` transaction, which also makes them
atomic across the whole collection.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

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

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StorageError(Exception):
    """Raised when the database cannot be read or written safely."""


def _schema() -> str:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS documents (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT
        )
        """
    ]
    for table in sorted(set(ROWS_TABLES.values())):
        if not _IDENTIFIER_RE.match(table):
            raise StorageError(f"unsafe table name: {table}")
        statements.append(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id    TEXT PRIMARY KEY,
                idx   INTEGER NOT NULL,
                value TEXT NOT NULL
            )
            """
        )
        statements.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_order ON {table}(idx)")
    return ";\n".join(statements)


class _Transaction:
    """Explicit ``BEGIN IMMEDIATE`` transaction holding the database lock.

    Re-entrant: a nested ``with database.transaction()`` joins the outer one
    instead of issuing a second ``BEGIN`` (which SQLite rejects), and only the
    outermost exit commits. If any level failed, the whole transaction rolls
    back so a partially-applied write can never be committed.
    """

    def __init__(self, database: "Database") -> None:
        self._db = database

    def __enter__(self) -> sqlite3.Connection:
        db = self._db
        if db._depth == 0:
            db._lock.acquire()
            try:
                db._conn.execute("BEGIN IMMEDIATE")
            except Exception:
                db._lock.release()
                raise
            db._failed = False
        db._depth += 1
        return db._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        db = self._db
        if exc_type is not None:
            db._failed = True
        db._depth -= 1
        if db._depth == 0:
            try:
                if db._failed:
                    db._conn.rollback()
                else:
                    db._conn.commit()
            finally:
                db._failed = False
                db._lock.release()
        return False


class Database:
    """Owns the single SQLite connection and the schema."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._depth = 0
        self._failed = False
        self._open()

    # --- connection --------------------------------------------------------
    def _open(self) -> None:
        connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        self._connection = connection
        self._depth = 0
        self._failed = False
        with self._lock:
            connection.executescript(_schema())

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:  # pragma: no cover - defensive
            raise StorageError("database connection is closed")
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # --- access helpers ----------------------------------------------------
    def transaction(self) -> _Transaction:
        return _Transaction(self)

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Serialise access to the shared connection without a write lock."""
        with self._lock:
            yield self._conn

    # --- backup / restore --------------------------------------------------
    def snapshot(self, target: Path) -> None:
        """Write a consistent copy of the database to ``target``.

        ``VACUUM INTO`` produces a compact, fully-consistent copy even while the
        connection is open, which is exactly what a backup needs.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        with self._lock:
            self._conn.execute("VACUUM INTO ?", (str(target),))

    def restore_from_path(self, source: Path, *, actor: str = "system") -> None:
        source = Path(source)
        self._replace_file(source.read_bytes())

    def restore_from_bytes(self, payload: bytes) -> None:
        self._replace_file(payload)

    def _replace_file(self, payload: bytes) -> None:
        """Swap the database file for ``payload`` and reopen the connection."""
        # Validate before touching the live file so a bad upload cannot destroy
        # the running database.
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".restore.", suffix=".db"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._verify_sqlite(tmp_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            shutil.copy2(tmp_path, self.path)
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            self._open()
        tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _verify_sqlite(path: Path) -> None:
        try:
            probe = sqlite3.connect(path)
        except sqlite3.Error as exc:  # pragma: no cover - corrupt file
            raise StorageError(f"الملف ليس قاعدة بيانات صالحة: {exc}") from exc
        try:
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise StorageError(f"الملف ليس قاعدة بيانات صالحة: {exc}") from exc
        finally:
            probe.close()


class SqliteStore:
    """A single collection, exposing the old document read/update/write API.

    Services call :meth:`read`, :meth:`update` and :meth:`write` with plain
    dictionaries, so keeping that contract means the SQLite migration is
    invisible to every service above the storage layer.
    """

    def __init__(
        self,
        database: Database,
        name: str,
        default: dict[str, Any],
    ) -> None:
        self._db = database
        self.name = name
        self._default = default
        self._rows_table = ROWS_TABLES.get(name)
        self._rows_key = ROWS_KEYS.get(name)

    # --- reading -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        with self._db.locked() as conn:
            return self._read_conn(conn)

    def _read_conn(self, conn: sqlite3.Connection) -> dict[str, Any]:
        document = copy.deepcopy(self._default)
        row = conn.execute(
            "SELECT value FROM documents WHERE key = ?", (self.name,)
        ).fetchone()
        if row is not None:
            document.update(json.loads(row["value"]))
        if self._rows_table and self._rows_key:
            rows = conn.execute(
                f"SELECT value FROM {self._rows_table} ORDER BY idx ASC"
            ).fetchall()
            document[self._rows_key] = [json.loads(r["value"]) for r in rows]
        return document

    def is_empty(self) -> bool:
        with self._db.locked() as conn:
            has_document = conn.execute(
                "SELECT 1 FROM documents WHERE key = ?", (self.name,)
            ).fetchone()
            if has_document:
                return False
            if self._rows_table:
                has_rows = conn.execute(
                    f"SELECT 1 FROM {self._rows_table} LIMIT 1"
                ).fetchone()
                if has_rows:
                    return False
            return True

    # --- writing -----------------------------------------------------------
    def write(self, document: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            self._write_conn(conn, document)

    def update(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        """Read-modify-write inside one transaction; returns the mutator result."""
        with self._db.transaction() as conn:
            document = self._read_conn(conn)
            result = mutator(document)
            self._write_conn(conn, document)
            return result

    def _write_conn(self, conn: sqlite3.Connection, document: dict[str, Any]) -> None:
        scalars = {
            key: value
            for key, value in document.items()
            if not (self._rows_key and key == self._rows_key)
        }
        conn.execute(
            """
            INSERT INTO documents (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (self.name, json.dumps(scalars, ensure_ascii=False), utc_now_iso()),
        )
        if not (self._rows_table and self._rows_key):
            return
        rows = document.get(self._rows_key) or []
        conn.execute(f"DELETE FROM {self._rows_table}")
        conn.executemany(
            f"INSERT INTO {self._rows_table} (id, idx, value) VALUES (?, ?, ?)",
            [
                (
                    str(item.get("id", index)),
                    index,
                    json.dumps(item, ensure_ascii=False),
                )
                for index, item in enumerate(rows)
            ],
        )


class Storage:
    """Owns the database and every collection used by the application."""

    def __init__(self, data_dir: Path, backup_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.data_dir / "tqm.db")

        self.questions = SqliteStore(
            self.db, "questions", {"version": 1, "counter": 0, "questions": []}
        )
        self.channels = SqliteStore(
            self.db, "channels", {"version": 1, "counter": 0, "channels": []}
        )
        self.schedules = SqliteStore(
            self.db, "schedules", {"version": 1, "counter": 0, "schedules": []}
        )
        self.history = SqliteStore(
            self.db, "history", {"version": 1, "counter": 0, "history": []}
        )
        self.settings = SqliteStore(
            self.db,
            "settings",
            {
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
                "paused_schedules": [],
                "state": {},
            },
        )
        self.users = SqliteStore(self.db, "users", {"version": 1, "users": []})
        self.secrets = SqliteStore(
            self.db,
            "secrets",
            {
                "version": 1,
                "telegram_bot_token": None,
                "telegram_bot_token_updated_at": None,
                "userbot": {},
            },
        )
        self.audit = SqliteStore(self.db, "audit_log", {"version": 1, "entries": []})
        self.poll_results = SqliteStore(
            self.db, "poll_results", {"version": 1, "counter": 0, "results": []}
        )
        self.poll_answers = SqliteStore(
            self.db, "poll_answers", {"version": 1, "counter": 0, "answers": []}
        )
        self.userbot_accounts = SqliteStore(
            self.db, "userbot_accounts", {"version": 1, "counter": 0, "accounts": []}
        )

        self.migrate_from_json()

    def all_stores(self) -> dict[str, SqliteStore]:
        return {
            "questions": self.questions,
            "channels": self.channels,
            "schedules": self.schedules,
            "history": self.history,
            "settings": self.settings,
            "users": self.users,
            "secrets": self.secrets,
            "audit_log": self.audit,
            "poll_results": self.poll_results,
            "poll_answers": self.poll_answers,
            "userbot_accounts": self.userbot_accounts,
        }

    # --- one-time JSON migration -------------------------------------------
    def migrate_from_json(self) -> list[str]:
        """Import legacy ``data/*.json`` files into an empty database.

        Only collections that are still empty are touched, so running this on a
        database that already holds data is a no-op and the legacy files are
        never re-imported over live changes.
        """
        migrated: list[str] = []
        for name, store in self.all_stores().items():
            filename = LEGACY_FILES.get(name)
            if not filename:
                continue
            path = self.data_dir / filename
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
            logger.info(
                "migrated %s legacy JSON file(s) into %s",
                len(migrated),
                self.db.path.name,
            )
        return migrated
