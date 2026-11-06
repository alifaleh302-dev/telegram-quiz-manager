"""Choose and build the storage backend.

The rest of the application only ever sees a
:class:`app.storage.base.StorageBackend`, so the switch between the local SQLite
file and PostgreSQL lives here and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.base import StorageBackend

logger = get_logger(__name__)


def build_storage(settings: Settings) -> StorageBackend:
    """Build the backend selected by configuration.

    ``DATABASE_URL`` set => PostgreSQL (pooled, multi-worker safe).
    ``DATABASE_URL`` empty => the local SQLite file.
    """
    data_dir = settings.data_dir
    backup_dir = settings.backup_dir

    if settings.uses_postgres:
        from app.storage.postgres_manager import Storage as PostgresStorage

        logger.info("storage backend: postgresql")
        storage = PostgresStorage(
            data_dir,
            backup_dir,
            database_url=settings.database_url,
            pool_max=max(1, settings.db_pool_max),
            schema=settings.db_schema,
        )
        _maybe_import_legacy_sqlite(settings, storage)
        return storage

    from app.storage.sqlite_manager import Storage as SqliteStorage

    logger.info("storage backend: sqlite (%s)", Path(data_dir) / "tqm.db")
    return SqliteStorage(data_dir, backup_dir)


def _maybe_import_legacy_sqlite(settings: Settings, storage: StorageBackend) -> None:
    """One-time copy of an existing ``tqm.db`` into a fresh PostgreSQL database.

    Moving a running deployment from SQLite to PostgreSQL would otherwise start
    from an empty database. This reads the old file — only when it exists and
    the new database is still completely empty — and copies every collection
    into it, so the switch loses nothing. It never touches a database that
    already holds data, so restarts are safe.
    """
    legacy_path = settings.db_path
    if not legacy_path.exists():
        return
    if not storage.is_empty():
        logger.info("postgres database already holds data; skipping tqm.db import")
        return
    from app.storage.sqlite_manager import Storage as SqliteStorage

    logger.warning("importing existing %s into postgresql (one time)", legacy_path.name)
    legacy = SqliteStorage(settings.data_dir, settings.backup_dir)
    try:
        imported = storage.import_from_backend(legacy)
    finally:
        legacy.close()
    if imported:
        logger.info("imported %s collection(s) from %s", len(imported), legacy_path.name)
