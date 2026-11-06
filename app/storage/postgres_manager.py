"""PostgreSQL storage backend.

Same document contract as the SQLite backend, but built for many concurrent
readers and more than one worker process:

* a **connection pool** instead of one shared connection, so concurrent reads do
  not queue behind each other;
* **MVCC reads** — a plain ``SELECT`` never blocks a writer and vice versa, which
  is the whole reason to move off the single-connection SQLite design;
* **row-level writes** — only the rows that changed are written, instead of the
  whole collection being deleted and re-inserted;
* **one advisory lock for writes** — ``pg_advisory_xact_lock`` serialises
  read-modify-write on a collection so two requests cannot interleave, the same
  guarantee the SQLite re-entrant lock gave. It is held for the transaction and
  released automatically at commit. Reads never take it.

Schema mirrors SQLite so the two backends stay interchangeable:

* ``documents(key, value, updated_at)`` — the scalar part of each collection.
* one table per list-bearing collection ``(id, idx, value)``.

``value`` is ``jsonb`` in both cases, so rows stay queryable with SQL.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from app.core.logging import get_logger
from app.storage.base import (
    ROWS_KEYS,
    ROWS_TABLES,
    CollectionStore,
    StorageBackend,
    StorageError,
    checked_table,
    collection_name_for,
    default_collections,
    utc_now_iso,
)

logger = get_logger(__name__)

T = TypeVar("T")

# One advisory-lock key for every write transaction. See the class docstring:
# per-collection locks would need consistent ordering across nested updates to
# stay deadlock-free, and writes are a small fraction of this workload.
_WRITE_LOCK_KEY = 0x7A71_6D01  # "tqm" + 01


class _TxState:
    """The in-flight write transaction for one thread (for re-entrancy)."""

    __slots__ = ("conn", "depth")

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.depth = 1


def _schema_sql() -> list[str]:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS documents (
            key        text PRIMARY KEY,
            value      jsonb NOT NULL,
            updated_at timestamptz
        )
        """
    ]
    for table in sorted(set(ROWS_TABLES.values())):
        checked_table(table)
        statements.append(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id    text PRIMARY KEY,
                idx   integer NOT NULL,
                value jsonb NOT NULL
            )
            """
        )
        statements.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_order ON {table}(idx)")
    return statements


class PostgresDatabase:
    """Owns the connection pool and the schema."""

    def __init__(
        self,
        conninfo: str,
        *,
        pool_min: int = 1,
        pool_max: int = 10,
        connect_timeout: float = 10.0,
        search_path: str = "",
    ) -> None:
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise StorageError(
                "psycopg is required for the PostgreSQL backend: "
                "pip install 'psycopg[binary,pool]'"
            ) from exc

        self.conninfo = conninfo
        self.search_path = search_path
        self._local = threading.local()

        def _configure(conn: Any) -> None:
            # Pin every pooled connection to the same schema (used to isolate
            # test runs); an empty value leaves the server default in place.
            # The pool requires the connection to be returned idle, so the SET
            # must be committed or the connection stays in a transaction.
            if search_path:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{search_path}"')
                conn.commit()

        self._pool = ConnectionPool(
            conninfo,
            min_size=pool_min,
            max_size=pool_max,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=False,
        )
        try:
            self._pool.open(wait=True, timeout=connect_timeout)
        except Exception as exc:  # pragma: no cover - connection failure
            raise StorageError(f"تعذّر الاتصال بقاعدة البيانات: {exc}") from exc
        self._ensure_schema()

    # --- connection --------------------------------------------------------
    @property
    def pool(self) -> Any:
        return self._pool

    def _ensure_schema(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                if self.search_path:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.search_path}"')
                for statement in _schema_sql():
                    cur.execute(statement)

    @contextmanager
    def read_connection(self) -> Iterator[Any]:
        """A pooled connection for reads; joins the thread's write tx if any."""
        state = getattr(self._local, "tx", None)
        if state is not None:
            yield state.conn
            return
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def write_transaction(self) -> Iterator[Any]:
        """A write transaction serialised by a global advisory lock.

        Re-entrant: a nested ``update()`` on any collection joins the outer
        transaction rather than opening a second one, so a service that writes
        two collections in sequence stays consistent.
        """
        state = getattr(self._local, "tx", None)
        if state is not None:
            state.depth += 1
            try:
                yield state.conn
            finally:
                state.depth -= 1
            return

        conn = self._pool.getconn()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
                self._local.tx = _TxState(conn)
                try:
                    yield conn
                finally:
                    self._local.tx = None
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.close()


class PostgresStore(CollectionStore):
    """One collection, exposing the same document API as ``SqliteStore``."""

    def __init__(
        self,
        database: PostgresDatabase,
        name: str,
        default: dict[str, Any],
    ) -> None:
        self._db = database
        self.name = name
        self._default = default
        self._rows_table = ROWS_TABLES.get(name)
        self._rows_key = ROWS_KEYS.get(name)
        if self._rows_table:
            checked_table(self._rows_table)

    # --- reading -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        with self._db.read_connection() as conn:
            return self._read_conn(conn)

    def _read_conn(self, conn: Any) -> dict[str, Any]:
        document = copy.deepcopy(self._default)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM documents WHERE key = %s", (self.name,))
            row = cur.fetchone()
            if row is not None:
                document.update(row["value"])
            if self._rows_table and self._rows_key:
                cur.execute(
                    f"SELECT value FROM {self._rows_table} ORDER BY idx ASC"
                )
                document[self._rows_key] = [r["value"] for r in cur.fetchall()]
        return document

    def is_empty(self) -> bool:
        with self._db.read_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM documents WHERE key = %s", (self.name,))
                if cur.fetchone():
                    return False
                if self._rows_table:
                    cur.execute(f"SELECT 1 FROM {self._rows_table} LIMIT 1")
                    if cur.fetchone():
                        return False
        return True

    # --- writing -----------------------------------------------------------
    def write(self, document: dict[str, Any]) -> None:
        with self._db.write_transaction() as conn:
            self._write_conn(conn, document)

    def update(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        with self._db.write_transaction() as conn:
            document = self._read_conn(conn)
            result = mutator(document)
            self._write_conn(conn, document)
            return result

    def _write_conn(self, conn: Any, document: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        scalars = {
            key: value
            for key, value in document.items()
            if not (self._rows_key and key == self._rows_key)
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (key, value, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (self.name, Jsonb(scalars), utc_now_iso()),
            )
            if not (self._rows_table and self._rows_key):
                return
            self._sync_rows(cur, document.get(self._rows_key) or [])

    def _sync_rows(self, cur: Any, rows: list[dict[str, Any]]) -> None:
        """Write only the rows that were added, changed or removed.

        The SQLite backend rewrites the whole collection on every update, which
        is fine for one local file but wasteful here: marking one exam viewed
        would otherwise re-serialise every row in the table. Comparing against
        the stored rows makes that write touch a single row.
        """
        from psycopg.types.json import Jsonb

        table = self._rows_table
        desired: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, item in enumerate(rows):
            desired[str(item.get("id", index))] = (index, item)

        cur.execute(f"SELECT id, idx, value FROM {table}")
        existing = {row["id"]: (row["idx"], row["value"]) for row in cur.fetchall()}

        removed = [row_id for row_id in existing if row_id not in desired]
        if removed:
            cur.executemany(
                f"DELETE FROM {table} WHERE id = %s", [(row_id,) for row_id in removed]
            )

        changed = [
            (row_id, index, item)
            for row_id, (index, item) in desired.items()
            if existing.get(row_id) != (index, item)
        ]
        if changed:
            cur.executemany(
                f"""
                INSERT INTO {table} (id, idx, value) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET idx = excluded.idx,
                                               value = excluded.value
                """,
                [(row_id, index, Jsonb(item)) for row_id, index, item in changed],
            )


class Storage(StorageBackend):
    """Owns the PostgreSQL database and every collection used by the app."""

    backend_name = "postgres"
    snapshot_filename = "tqm.json"

    def __init__(
        self,
        data_dir: Path,
        backup_dir: Path,
        *,
        database_url: str,
        pool_min: int = 1,
        pool_max: int = 10,
        schema: str = "",
    ) -> None:
        super().__init__(data_dir, backup_dir)
        if not database_url:
            raise StorageError("DATABASE_URL غير مضبوط لقاعدة بيانات PostgreSQL.")
        self.database_url = database_url
        self.schema = schema
        self.db = PostgresDatabase(
            database_url, pool_min=pool_min, pool_max=pool_max, search_path=schema
        )

        for attribute, default in default_collections().items():
            setattr(
                self,
                attribute,
                self.store(collection_name_for(attribute), default),
            )

    def _make_store(self, name: str, default: dict[str, Any]) -> PostgresStore:
        return PostgresStore(self.db, name, default or {})

    def store(self, name: str, default: dict[str, Any]) -> PostgresStore:
        return super().store(name, default)  # type: ignore[return-value]

    def all_stores(self) -> dict[str, PostgresStore]:
        return self._stores  # type: ignore[return-value]

    # --- backup / restore --------------------------------------------------
    def snapshot(self, target: Path) -> None:
        """Write every collection to a JSON file (portable across backends)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.export_document()
        target.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def export_document(self) -> dict[str, Any]:
        """Every collection as one JSON-serialisable document."""
        return {
            "version": 1,
            "backend": self.backend_name,
            "collections": {
                name: store.read() for name, store in self.all_stores().items()
            },
        }

    def restore_from_bytes(self, payload: bytes) -> None:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"ملف النسخة الاحتياطية غير صالح: {exc}") from exc
        if not isinstance(parsed, dict):
            raise StorageError("ملف النسخة الاحتياطية يجب أن يحتوي على كائن JSON.")
        collections = parsed.get("collections", parsed)
        if not isinstance(collections, dict):
            raise StorageError("ملف النسخة الاحتياطية لا يحتوي على مجموعات.")
        for name, document in collections.items():
            store = self._stores.get(name)
            if store is None or not isinstance(document, dict):
                continue
            store.write(document)

    def restore_from_path(self, source: Path, *, actor: str = "system") -> None:
        self.restore_from_bytes(Path(source).read_bytes())

    def close(self) -> None:
        self.db.close()
